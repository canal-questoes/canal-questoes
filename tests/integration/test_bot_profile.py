"""Configuração do perfil do bot pelo worker.

Cobre o perfil (e o que nele toca da chave-mestra, da perda de acesso e das esperas); a
chave-mestra trocada; a tarefa `bot.sync_profile` (resultados e textos
fixos), a leitura do token, a classificação das falhas e a redação na saída; o runtime dos bots
(`run_worker(…, bots=…)` registra o tratador `bot.sync_profile`) e a fila de tarefas (`RetryTaskLater`).

O worker roda numa thread (`bot_worker_thread`) com o `FakeClock` do teste e a Bot API falsa. As
esperas do sistema são liberadas avançando o relógio; as provas de "ainda não tentou de novo" usam
uma tarefa marcadora de tipo próprio, enfileirada depois do avanço: como a fila pega a tarefa
disponível de menor `id`, quando o marcador é tratado a tarefa do perfil não estava disponível. Os
parâmetros enviados ao Telegram são comparados por igualdade exata no registro do dublê, e os
intervalos pelo `clock_at` de cada chamada. Os textos fixos do perfil estão escritos aqui
(e não importados de `cq_server.botmessages`).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable

import psycopg
import pytest

from cq_server.tasks import TaskRecord
from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_offering, marker, register_bot, register_person
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import OTHER_MASTER_KEY

ADMIN = auth(ADMIN_TOKEN)
TASK_TYPE = "bot.sync_profile"
MARKER_TYPE = "teste.marco_perfil"
PROFILE_METHODS = ("setMyDescription", "setMyCommands")
EVENT_TYPES = ("bot.profile_updated", "bot.access_changed", "bot.lease_changed", "task.failed")

# Textos fixos do perfil.
DESCRIPTION_ACTIVE = ("Bot do oferecimento {course} ({term}). Para participar, toque no link de convite "
                      "que você recebeu da coordenação.")
DESCRIPTION_ARCHIVED = "O oferecimento {course} ({term}) foi encerrado. Este bot não recebe mais mensagens."
COMMANDS = [{"command": "start", "description": "Começar ou usar um link de convite"},
            {"command": "ajuda", "description": "Ver como usar este bot"}]

# Mensagens fixas de `problem`.
PROBLEM_MESSAGES = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "token_unreadable": "O token guardado não pode ser lido com a chave-mestra atual. Registre o token "
                        "de novo.",
}

COURSE = "Algoritmos e Programação"
TERM = "2031s1"

# Do erro de um tratador, a fila guarda só o nome do tipo (`TaskQueue.work_once`).
TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any) -> None:
    """As esperas deste arquivo dependem do `settings` do conftest: 3 tentativas, espera
    `min(2 · 2^(tentativa−1), 60)` s (2 s e depois 4 s) e posse de 30 s."""
    assert (settings.max_attempts, settings.backoff_base_seconds, settings.backoff_max_seconds,
            settings.lease_seconds) == (3, 2, 60, 30)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _text(template: str, course: str = COURSE, term: str = TERM) -> str:
    return template.replace("{course}", course).replace("{term}", term)


def _active_params(course: str = COURSE, term: str = TERM) -> dict[str, dict]:
    return {"setMyDescription": {"description": _text(DESCRIPTION_ACTIVE, course, term)},
            "setMyCommands": {"commands": COMMANDS}}


def _path(ref: str, suffix: str = "/bot") -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}{suffix}"


def _get_ok(client: Any, op_id: str, path: str) -> dict:
    resp = client.get(path, headers=ADMIN)
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _get_bot(client: Any, ref: str) -> dict:
    return _get_ok(client, "bots.get", _path(ref))


def _post_ok(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _archive(client: Any, ref: str) -> dict:
    prepared = _post_ok(client, "offerings.prepare_archive", _path(ref, "/archive/prepare"))
    archived = _post_ok(client, "offerings.archive", _path(ref, "/archive"),
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived"
    return archived


def _bot_body(offering: dict, telegram_bot_id: int, username: str, *, registered_at: str,
              profile_state: str, state: str = "registered", token_replaced_at: str | None = None,
              problem: str | None = None) -> dict:
    """O `Bot` exato do contrato (sem posse nem consulta: oferecimento fora de consulta)."""
    return {
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
        "username": username, "telegram_bot_id": telegram_bot_id, "state": state,
        "profile_state": profile_state, "registered_at": registered_at,
        "token_replaced_at": token_replaced_at, "lease": None, "last_poll_ok_at": None,
        "problem": None if problem is None else {"code": problem, "message": PROBLEM_MESSAGES[problem]},
    }


def _summary(telegram_bot_id: int, username: str, *, profile_state: str, state: str = "registered") -> dict:
    return {"username": username, "telegram_bot_id": telegram_bot_id, "state": state,
            "profile_state": profile_state}


def _internal_bot_id(sql: psycopg.Connection, telegram_bot_id: int) -> int:
    rows = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (telegram_bot_id,)).fetchall()
    assert len(rows) == 1
    return int(rows[0][0])


def _registered(client: Any, bot_api: Any, sql: psycopg.Connection, *, course: str = COURSE,
                term: str = TERM) -> tuple[dict, int, str, int]:
    """Cadastra um oferecimento (rascunho) e registra um bot novo do dublê; devolve
    (oferecimento, id do bot no Telegram, username, id interno do bot)."""
    offering = create_offering(client, ADMIN_TOKEN, course=course, term=term)
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot))
    return offering, bot, bot_api.state(bot).username, _internal_bot_id(sql, bot)


def _sync_tasks(sql: psycopg.Connection) -> list[tuple[Any, ...]]:
    """As tarefas `bot.sync_profile`: (id, payload, chave de idempotência), por id."""
    return [tuple(row) for row in sql.execute(
        "SELECT id, payload, idempotency_key FROM tasks WHERE task_type = %s ORDER BY id",
        (TASK_TYPE,)).fetchall()]


def _only_task(sql: psycopg.Connection, internal: int, purpose: str = "active") -> int:
    """A única tarefa `bot.sync_profile`, com o payload e a chave do contrato."""
    tasks = _sync_tasks(sql)
    assert [(p, k) for _, p, k in tasks] == [({"bot_id": internal}, f"{TASK_TYPE}:{internal}:1:{purpose}")]
    return int(tasks[0][0])


def _task(tid: int, *, state: str, attempts: int, available_at: datetime, last_error: str | None = None,
          finished_at: datetime | None = None) -> TaskRecord:
    return TaskRecord(id=tid, task_type=TASK_TYPE, state=state, attempts=attempts, available_at=available_at,
                      lease_owner=None, lease_until=None, last_error=last_error, finished_at=finished_at)


def _profile_calls(bot_api: Any, bot: int) -> list[Any]:
    return [c for c in bot_api.calls(bot_id=bot) if c.method in PROFILE_METHODS]


def _by_method(calls: list[Any]) -> list[tuple[str, dict]]:
    """(método, parâmetros) das chamadas, em ordem de método (a ordem entre a descrição e os
    comandos não é contrato)."""
    return sorted(((c.method, c.params) for c in calls), key=lambda item: item[0])


def _expected_active(course: str = COURSE, term: str = TERM) -> list[tuple[str, dict]]:
    params = _active_params(course, term)
    return [("setMyCommands", params["setMyCommands"]), ("setMyDescription", params["setMyDescription"])]


def _is_profile_call(call: Any) -> bool:
    return call.method in PROFILE_METHODS


def _wait(bot_api: Any, worker: Any, predicate: Callable[[], Any], what: str, timeout: float = 10.0) -> Any:
    """Espera `predicate()` com prazo (pela Bot API falsa); falha na hora se o worker morreu."""
    def check() -> Any:
        if not worker.is_alive():
            raise AssertionError(f"{what}: o worker terminou antes (erro: {worker.error!r})")
        return predicate()
    try:
        return bot_api.wait_for(check, timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"{what}: {exc}") from None


def _attempted(queue: Any, tid: int, attempts: int, state: str = "pending") -> Callable[[], bool]:
    def check() -> bool:
        record = queue.get(tid)
        return record.attempts == attempts and record.state == state
    return check


def _strip_seq(events: list[dict]) -> list[dict]:
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _events(client: Any) -> list[dict]:
    return _strip_seq(all_events(client, ADMIN_TOKEN, *EVENT_TYPES))


def _profile_event(offering: dict, at: datetime, profile_state: str, purpose: str = "active") -> dict:
    return {"type": "bot.profile_updated", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": offering["offering_id"], "profile_state": profile_state,
                     "purpose": purpose}}


def _access_lost(offering: dict, at: datetime, problem: str) -> dict:
    return {"type": "bot.access_changed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": offering["offering_id"], "access": "lost", "problem": problem}}


def _task_failed(tid: int, at: datetime, attempts: int, error: str) -> dict:
    return {"type": "task.failed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"task_id": tid, "task_type": TASK_TYPE, "attempts": attempts, "error": error}}


def _processed(cap: Any, tid: int) -> list[str | None]:
    """Os estados dos registros `task.processed` da tarefa (contrato da fila, por inclusão)."""
    records = [r for r in cap.with_event("task.processed") if r.get("id") == tid]
    for record in records:
        assert record.get("logger") == "cq_server.worker", record
    return [r.get("state") for r in records]


def _type_name(error: Any) -> str:
    assert isinstance(error, str) and TYPE_NAME.fullmatch(error), f"last_error não é um nome de tipo: {error!r}"
    return error


# ---------------------------------------------------------------------------------------------
# Sucesso
# ---------------------------------------------------------------------------------------------


def test_worker_configures_profile_with_exact_params(client: Any, clock: Any, bot_api: Any,
                                                     sql: psycopg.Connection, queue: Any,
                                                     bot_worker_thread: Any) -> None:
    """Caso de sucesso (disciplina e
    período redigidos de novo na saída; uma pessoa cadastrada depois do oferecimento também é
    redigida): o worker com `bots` chama `setMyDescription` com exatamente
    `{description: DESCRIPTION_ACTIVE}` e `setMyCommands` com exatamente `{commands: COMMANDS}` (sem
    `scope` nem `language_code`), POST JSON sem consulta, com o token do bot; o dublê fica com a
    descrição e os comandos; a tarefa termina `done` na 1ª tentativa; `bots.get` e o resumo do
    oferecimento mostram o perfil `configured`; um único `bot.profile_updated {configured, active}`;
    o registro `task.processed` e nenhuma exceção no log. Nenhuma outra chamada ao Telegram (o
    rascunho não é consultado)."""
    helena = register_person(client, ADMIN_TOKEN, name="Helena Fictícia Moraes")
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql, course="Seminário com Helena "
                                                    "Fictícia Moraes", term="2031s1 RA 5566778899")
    bruno = register_person(client, ADMIN_TOKEN, name="Bruno Inventado Siqueira", ra="5566778899")
    tid = _only_task(sql, internal)
    course = f"Seminário com {marker(helena['alias'])}"
    term = f"2031s1 RA {marker(bruno['alias'])}"
    description = _text(DESCRIPTION_ACTIVE, course, term)
    clock.advance(30)
    t1 = clock.now()

    with capture_logs() as cap:
        worker = bot_worker_thread()
        _wait(bot_api, worker, lambda: queue.get(tid).state == "done", "perfil configurado")

    calls = _profile_calls(bot_api, bot)
    assert _by_method(calls) == [("setMyCommands", {"commands": COMMANDS}),
                                 ("setMyDescription", {"description": description})]
    assert {(c.http_method, c.has_query, c.token_state, c.token_version, c.outcome, c.clock_at)
            for c in calls} == {("POST", False, "valid", 1, "ok", t1)}
    assert sorted(c.method for c in bot_api.calls()) == ["getMe", "setMyCommands", "setMyDescription"]
    state = bot_api.state(bot)
    assert (state.description, state.commands) == (description, tuple(COMMANDS))

    assert queue.get(tid) == _task(tid, state="done", attempts=1, available_at=t0, finished_at=t1)
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="configured")
    got = _get_ok(client, "offerings.get", _path(offering["code"], ""))
    assert got["bot"] == _summary(bot, username, profile_state="configured")
    assert _events(client) == [_profile_event(offering, t1, "configured")]
    assert _processed(cap, tid) == ["done"]
    assert cap.with_event("exception") == []


def test_profile_task_rerun_keeps_state(client: Any, clock: Any, bot_api: Any, sql: psycopg.Connection,
                                        queue: Any, bot_worker_thread: Any) -> None:
    """Tarefa assíncrona e idempotente: rodar de novo a configuração do
    mesmo bot (outra tarefa com o mesmo payload `{"bot_id": <id>}`) repete as duas chamadas com os
    mesmos parâmetros, o perfil continua `configured`, o dublê fica com os mesmos textos, e cada
    execução com sucesso registra o seu `bot.profile_updated {configured, active}`."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: queue.get(tid).state == "done", "primeira configuração")

    clock.advance(10)
    t1 = clock.now()
    again = queue.enqueue(TASK_TYPE, {"bot_id": internal}, idempotency_key="teste:reexecucao-do-perfil")
    _wait(bot_api, worker, lambda: queue.get(again).state == "done", "segunda configuração")

    calls = _profile_calls(bot_api, bot)
    assert len(calls) == 4, bot_api.transcript()
    for group, at in ((calls[:2], t0), (calls[2:], t1)):
        assert _by_method(group) == _expected_active()
        assert {(c.token_state, c.outcome, c.clock_at) for c in group} == {("valid", "ok", at)}
    state = bot_api.state(bot)
    assert (state.description, state.commands) == (_text(DESCRIPTION_ACTIVE), tuple(COMMANDS))
    assert queue.get(again) == _task(again, state="done", attempts=1, available_at=t1, finished_at=t1)
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="configured")
    assert _events(client) == [_profile_event(offering, t0, "configured"),
                               _profile_event(offering, t1, "configured")]


def test_profile_after_token_replacement_uses_current_token(client: Any, clock: Any, bot_api: Any,
                                                            sql: psycopg.Connection, queue: Any,
                                                            bot_worker_thread: Any) -> None:
    """Depois do registro e de cada troca de token (uma tarefa por versão), e
    a tarefa lê o estado atual: com o registro e a troca de token feitos antes do worker, as
    duas tarefas (`…:1:active` e `…:2:active`) usam o token atual (versão 2 no dublê), o perfil fica
    `configured` e sai um `bot.profile_updated` por execução."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    clock.advance(10)
    replaced_at = clock.now()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.rotate_token(bot))
    tasks = _sync_tasks(sql)
    assert [(p, k) for _, p, k in tasks] == [({"bot_id": internal}, f"{TASK_TYPE}:{internal}:1:active"),
                                             ({"bot_id": internal}, f"{TASK_TYPE}:{internal}:2:active")]
    clock.advance(10)
    t1 = clock.now()

    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: all(queue.get(t[0]).state == "done" for t in tasks), "duas configurações")

    calls = _profile_calls(bot_api, bot)
    assert len(calls) == 4, bot_api.transcript()
    assert _by_method(calls) == sorted(_expected_active() * 2, key=lambda item: item[0])
    assert {(c.token_state, c.token_version, c.outcome, c.clock_at) for c in calls} == {("valid", 2, "ok", t1)}
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=iso(t0), token_replaced_at=iso(replaced_at),
        profile_state="configured")
    assert _events(client) == [_profile_event(offering, t1, "configured")] * 2


def test_profile_task_for_archived_offering_uses_archived_description(client: Any, clock: Any,
                                                                     bot_api: Any, sql: psycopg.Connection,
                                                                     queue: Any, bot_worker_thread: Any) -> None:
    """Arquivamento (a descrição do bot é trocada pela de oferecimento
    encerrado; o arquivamento agenda `bot.sync_profile` com `purpose = archived`)
    e a tarefa lê o estado atual (arquivado → só `setMyDescription(DESCRIPTION_ARCHIVED)`;
    `purpose` efetivo): a tarefa do registro e a do arquivamento, executadas depois do arquivamento,
    mandam só a descrição arquivada exata, nunca `setMyCommands`, e registram
    `bot.profile_updated {configured, archived}` cada uma."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    clock.advance(60)
    _archive(client, offering["code"])
    tasks = _sync_tasks(sql)
    assert [(p, k) for _, p, k in tasks] == [({"bot_id": internal}, f"{TASK_TYPE}:{internal}:1:active"),
                                             ({"bot_id": internal}, f"{TASK_TYPE}:{internal}:1:archived")]
    clock.advance(60)
    t1 = clock.now()

    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: all(queue.get(t[0]).state == "done" for t in tasks), "descrição arquivada")

    archived = {"description": _text(DESCRIPTION_ARCHIVED)}
    calls = _profile_calls(bot_api, bot)
    assert [(c.method, c.params, c.outcome, c.clock_at) for c in calls] == [
        ("setMyDescription", archived, "ok", t1)] * 2
    state = bot_api.state(bot)
    assert (state.description, state.commands) == (archived["description"], None)
    assert [queue.get(t[0]) for t in tasks] == [
        _task(t[0], state="done", attempts=1, available_at=at, finished_at=t1)
        for t, at in zip(tasks, (t0, t0 + timedelta(seconds=60)), strict=True)]
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="configured")
    assert _events(client) == [_profile_event(offering, t1, "configured", "archived")] * 2


# ---------------------------------------------------------------------------------------------
# Falhas temporárias: espera mínima e nova tentativa
# ---------------------------------------------------------------------------------------------

TEMPORARY_CASES = [
    pytest.param("fail", {"retry_after": 7}, 7, "RetryTaskLater", id="429-espera-7"),
    pytest.param("fail", {"retry_after": 1}, 2, "RetryTaskLater", id="429-espera-menor-que-a-minima"),
    pytest.param("fail", {"status": 429}, 2, "TelegramUnavailable", id="429-sem-retry-after"),
    pytest.param("fail", {"error": "server_restart"}, 2, "TelegramUnavailable", id="500"),
    pytest.param("network", {"kind": "not_json"}, 2, "TelegramUnavailable", id="502-html"),
    pytest.param("network", {"kind": "reset"}, 2, "TelegramUnavailable", id="conexao-cortada"),
    pytest.param("network", {"kind": "hang"}, 2, "TelegramUnavailable", id="resposta-presa"),
]


@pytest.mark.parametrize(("how", "failure", "wait_seconds", "last_error"), TEMPORARY_CASES)
def test_profile_temporary_failure_waits_then_configures(how: str, failure: dict, wait_seconds: int,
                                                         last_error: str, client: Any, clock: Any,
                                                         bot_api: Any, sql: psycopg.Connection, queue: Any,
                                                         bot_worker_thread: Any) -> None:
    """Esperas (`RetryAfter(n)` → `RetryTaskLater(n)`, com nova
    tentativa não antes de `max(espera da fila, n)`, e `TelegramUnavailable` → falha temporária com a
    espera da fila; 429 sem `retry_after`, 5xx, corpo HTML, conexão cortada e tempo esgotado →
    `TelegramUnavailable`; `RetryTaskLater`): a 1ª chamada do perfil falha (uma só chamada
    nessa tentativa); a tarefa volta a `pending` com `available_at` exato (7 s com `retry_after=7`;
    2 s, a espera da fila, com `retry_after=1` e nas demais) e `last_error` com o nome do tipo; o perfil
    não muda e nenhum evento sai; um instante antes da espera nada é tentado (tarefa marcadora); no
    instante exato (`available_at <= agora`) a tarefa é retomada, as duas chamadas saem com os
    parâmetros exatos e o `clock_at` da retomada, e o perfil fica `configured`."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    if how == "fail":
        bot_api.fail("*", bot_id=bot, when=_is_profile_call, **failure)
    else:
        bot_api.fail_network("*", bot_id=bot, when=_is_profile_call, **failure)
    seen: list[int] = []
    worker = bot_worker_thread(handlers={MARKER_TYPE: lambda task: seen.append(task.id)})
    _wait(bot_api, worker, _attempted(queue, tid, 1), "primeira tentativa")

    retry_at = t0 + timedelta(seconds=wait_seconds)
    assert queue.get(tid) == _task(tid, state="pending", attempts=1, available_at=retry_at, last_error=last_error)
    bot_api.release_hangs()  # a resposta presa (se houver) termina no dublê
    bot_api.wait_for(lambda: all(c.ended is not None for c in _profile_calls(bot_api, bot)))
    [failed] = _profile_calls(bot_api, bot)
    assert (failed.programmed, failed.clock_at, failed.params) == (
        True, t0, _active_params()[failed.method])
    if "retry_after" in failure:
        assert (failed.status, failed.retry_after) == (429, failure["retry_after"])
    assert _get_bot(client, offering["code"])["profile_state"] == "pending"
    assert _events(client) == []

    clock.advance(wait_seconds - 0.001)
    probe = queue.enqueue(MARKER_TYPE, {"caso": "antes-da-espera"})
    _wait(bot_api, worker, lambda: probe in seen, "tarefa marcadora antes da espera")
    assert queue.get(tid).state == "pending"
    assert len(_profile_calls(bot_api, bot)) == 1, bot_api.transcript()

    clock.advance(0.001)
    assert clock.now() == retry_at
    _wait(bot_api, worker, lambda: queue.get(tid).state == "done", "retomada no fim da espera")
    retried = _profile_calls(bot_api, bot)[1:]
    assert _by_method(retried) == _expected_active()
    assert {(c.programmed, c.outcome, c.clock_at) for c in retried} == {(False, "ok", retry_at)}
    assert queue.get(tid) == _task(tid, state="done", attempts=2, available_at=retry_at,
                                   last_error=last_error, finished_at=retry_at)
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="configured")
    assert _events(client) == [_profile_event(offering, retry_at, "configured")]


def test_profile_exhausted_attempts_are_marked_failed_by_handler(client: Any, clock: Any, bot_api: Any,
                                                                 sql: psycopg.Connection, queue: Any,
                                                                 bot_worker_thread: Any) -> None:
    """Última tentativa (falha temporária na última tentativa →
    `failed` e `bot.profile_updated {failed}` gravados pelo tratador antes de relançar; depois o
    `task.failed` da fila): com o Telegram sempre em 500, as tentativas saem em t0, t0+2 s e t0+6 s
    (uma chamada do perfil em cada, pelo `clock_at` do dublê); na 3ª, a tarefa fica `failed` com
    `TelegramUnavailable`, o perfil `failed`, o bot continua `registered` sem problema, e os eventos
    saem nesta ordem: `bot.profile_updated {failed, active}`, `task.failed`."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    bot_api.fail("*", error="server_restart", times=None, bot_id=bot, when=_is_profile_call)

    with capture_logs() as cap:
        worker = bot_worker_thread()
        _wait(bot_api, worker, _attempted(queue, tid, 1), "primeira tentativa")
        assert queue.get(tid) == _task(tid, state="pending", attempts=1, available_at=t0 + timedelta(seconds=2),
                                       last_error="TelegramUnavailable")
        clock.advance(2)
        _wait(bot_api, worker, _attempted(queue, tid, 2), "segunda tentativa")
        assert queue.get(tid) == _task(tid, state="pending", attempts=2, available_at=t0 + timedelta(seconds=6),
                                       last_error="TelegramUnavailable")
        assert _events(client) == []
        assert _get_bot(client, offering["code"])["profile_state"] == "pending"
        clock.advance(4)
        t_end = clock.now()
        _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "tentativas esgotadas")

    assert t_end == t0 + timedelta(seconds=6)
    assert queue.get(tid) == _task(tid, state="failed", attempts=3, available_at=t_end,
                                   last_error="TelegramUnavailable", finished_at=t_end)
    calls = _profile_calls(bot_api, bot)
    assert [(c.clock_at, c.status, c.programmed) for c in calls] == [
        (t0, 500, True), (t0 + timedelta(seconds=2), 500, True), (t_end, 500, True)]
    assert [c.params for c in calls] == [_active_params()[c.method] for c in calls]
    assert _events(client) == [_profile_event(offering, t_end, "failed"),
                               _task_failed(tid, t_end, 3, "TelegramUnavailable")]
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="failed")
    got = _get_ok(client, "offerings.get", _path(offering["code"], ""))
    assert got["bot"] == _summary(bot, username, profile_state="failed")
    state = bot_api.state(bot)
    assert (state.description, state.commands) == (None, None)
    assert _processed(cap, tid) == ["pending", "pending", "failed"]


# ---------------------------------------------------------------------------------------------
# Falhas definitivas
# ---------------------------------------------------------------------------------------------


def test_profile_token_rejected_marks_bot_without_access(client: Any, clock: Any, bot_api: Any,
                                                         sql: psycopg.Connection, queue: Any,
                                                         bot_worker_thread: Any) -> None:
    """Token recusado (`TokenRejected`) e perda de acesso: com o
    token revogado no dublê, a única chamada do perfil recebe 401; o bot fica `no_access`
    (`token_rejected`), o perfil `failed`, a tarefa `failed` na 1ª tentativa; os eventos saem nesta
    ordem: `bot.access_changed {lost, token_rejected}` (sem `bot.lease_changed`: não havia dono),
    `bot.profile_updated {failed, active}` e, por último, `task.failed`; `bots.get` mostra o problema
    com a mensagem exata."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    bot_api.revoke(bot)
    clock.advance(5)
    t1 = clock.now()

    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "falha definitiva")

    error = _type_name(queue.get(tid).last_error)
    assert queue.get(tid) == _task(tid, state="failed", attempts=1, available_at=t0, last_error=error,
                                   finished_at=t1)
    assert [(c.token_state, c.status, c.outcome, c.clock_at) for c in _profile_calls(bot_api, bot)] == [
        ("revoked", 401, "error", t1)]
    assert _events(client) == [_access_lost(offering, t1, "token_rejected"),
                               _profile_event(offering, t1, "failed"), _task_failed(tid, t1, 1, error)]
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=iso(t0), state="no_access", profile_state="failed",
        problem="token_rejected")
    got = _get_ok(client, "offerings.get", _path(offering["code"], ""))
    assert got["bot"] == _summary(bot, username, state="no_access", profile_state="failed")
    assert sql.execute("SELECT state, access_problem, lease_owner, lease_until FROM bots WHERE id = %s",
                       (internal,)).fetchone() == ("no_access", "token_rejected", None, None)


def test_profile_request_rejected_marks_failed(client: Any, clock: Any, bot_api: Any, sql: psycopg.Connection,
                                               queue: Any, bot_worker_thread: Any) -> None:
    """`RequestRejected` (400 → `RequestRejected`): um 400 na
    chamada do perfil é falha definitiva na 1ª tentativa: perfil `failed`, eventos
    `bot.profile_updated {failed, active}` e `task.failed`, nessa ordem; o bot continua
    `registered`, sem problema."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    bot_api.fail("*", status=400, description="Bad Request: invalid parameters", times=1, bot_id=bot,
                 when=_is_profile_call)
    clock.advance(5)
    t1 = clock.now()

    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "falha definitiva")

    error = _type_name(queue.get(tid).last_error)
    assert queue.get(tid) == _task(tid, state="failed", attempts=1, available_at=t0, last_error=error,
                                   finished_at=t1)
    assert [(c.status, c.programmed, c.clock_at) for c in _profile_calls(bot_api, bot)] == [(400, True, t1)]
    assert _events(client) == [_profile_event(offering, t1, "failed"), _task_failed(tid, t1, 1, error)]
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="failed")


def test_profile_bot_without_access_fails_without_calling_telegram(client: Any, clock: Any, bot_api: Any,
                                                                   sql: psycopg.Connection, queue: Any,
                                                                   bot_worker_thread: Any) -> None:
    """Bot sem acesso (sem novas chamadas por ele até a troca do token; um bot
    `no_access` na execução → falha definitiva sem chamar o Telegram): com o bot já
    `no_access`, a tarefa fica `failed` na 1ª tentativa sem nenhuma chamada ao Telegram, com
    `bot.profile_updated {failed, active}` e `task.failed` (sem novo `bot.access_changed`: o episódio
    já estava aberto)."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' WHERE id = %s",
                (internal,))
    clock.advance(5)
    t1 = clock.now()

    worker = bot_worker_thread()
    _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "falha definitiva")

    error = _type_name(queue.get(tid).last_error)
    assert queue.get(tid) == _task(tid, state="failed", attempts=1, available_at=t0, last_error=error,
                                   finished_at=t1)
    assert [c.method for c in bot_api.calls()] == ["getMe"]
    assert _events(client) == [_profile_event(offering, t1, "failed"), _task_failed(tid, t1, 1, error)]
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=iso(t0), state="no_access", profile_state="failed",
        problem="token_rejected")


@pytest.mark.parametrize("cause", ["chave-trocada", "blob-adulterado"])
def test_profile_unreadable_token_marks_no_access_without_calling(cause: str, client: Any, clock: Any,
                                                                  bot_api: Any, bot_settings: Any,
                                                                  sql: psycopg.Connection, queue: Any,
                                                                  bot_worker_thread: Any) -> None:
    """Token ilegível (um token que não possa ser decifrado com a chave atual deixa o bot sem acesso,
    com o motivo, sem chamar o Telegram); a chave-mestra trocada; leitura do token
    e o caso do token ilegível: com um worker de outra chave válida, ou com o blob
    adulterado no banco, a tarefa fica `failed` na 1ª tentativa sem nenhuma chamada ao Telegram; o bot
    fica `no_access` (`token_unreadable`); os eventos saem nesta ordem:
    `bot.access_changed {lost, token_unreadable}`, `bot.profile_updated {failed, active}`,
    `task.failed`; `bots.get` mostra o problema com a mensagem exata."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    kwargs: dict[str, Any] = {}
    if cause == "chave-trocada":
        kwargs["bot_settings"] = dataclasses.replace(bot_settings, master_key=OTHER_MASTER_KEY)
    else:
        blob = bytes(sql.execute("SELECT token_ciphertext FROM bots WHERE id = %s", (internal,)).fetchone()[0])
        tampered = blob[:20] + bytes([blob[20] ^ 0x01]) + blob[21:]
        sql.execute("UPDATE bots SET token_ciphertext = %s WHERE id = %s", (tampered, internal))
    clock.advance(5)
    t1 = clock.now()

    worker = bot_worker_thread(**kwargs)
    _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "falha definitiva")

    error = _type_name(queue.get(tid).last_error)
    assert queue.get(tid) == _task(tid, state="failed", attempts=1, available_at=t0, last_error=error,
                                   finished_at=t1)
    assert [c.method for c in bot_api.calls()] == ["getMe"]
    assert _events(client) == [_access_lost(offering, t1, "token_unreadable"),
                               _profile_event(offering, t1, "failed"), _task_failed(tid, t1, 1, error)]
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=iso(t0), state="no_access", profile_state="failed",
        problem="token_unreadable")
    assert sql.execute("SELECT state, access_problem FROM bots WHERE id = %s", (internal,)).fetchone() == (
        "no_access", "token_unreadable")


def test_profile_worker_without_master_key_retries_then_fails(client: Any, clock: Any, bot_api: Any,
                                                              bot_settings: Any, sql: psycopg.Connection,
                                                              queue: Any, bot_worker_thread: Any) -> None:
    """Sem chave-mestra no worker (falha temporária que
    segue as tentativas da fila, sem chamar o Telegram; na última, `failed` e
    `bot.profile_updated {failed}`; `run_worker` sem chave-mestra registra uma única vez
    `worker.bots_disabled`, WARNING, sem marcar bots): as tentativas em t0, t0+2 s e t0+6 s não chamam
    o Telegram, a tarefa termina `failed` com o mesmo nome de tipo em `last_error`, o bot continua
    `registered` sem problema, e os eventos são `bot.profile_updated {failed, active}` e `task.failed`,
    nessa ordem."""
    t0 = clock.now()
    offering, bot, username, internal = _registered(client, bot_api, sql)
    tid = _only_task(sql, internal)
    no_key = dataclasses.replace(bot_settings, master_key=None)

    with capture_logs() as cap:
        worker = bot_worker_thread(bot_settings=no_key)
        _wait(bot_api, worker, _attempted(queue, tid, 1), "primeira tentativa")
        error = _type_name(queue.get(tid).last_error)
        assert queue.get(tid) == _task(tid, state="pending", attempts=1, available_at=t0 + timedelta(seconds=2),
                                       last_error=error)
        clock.advance(2)
        _wait(bot_api, worker, _attempted(queue, tid, 2), "segunda tentativa")
        assert queue.get(tid) == _task(tid, state="pending", attempts=2, available_at=t0 + timedelta(seconds=6),
                                       last_error=error)
        assert _events(client) == []
        clock.advance(4)
        t_end = clock.now()
        _wait(bot_api, worker, lambda: queue.get(tid).state == "failed", "tentativas esgotadas")

    assert queue.get(tid) == _task(tid, state="failed", attempts=3, available_at=t_end, last_error=error,
                                   finished_at=t_end)
    assert [c.method for c in bot_api.calls()] == ["getMe"]
    assert _events(client) == [_profile_event(offering, t_end, "failed"), _task_failed(tid, t_end, 3, error)]
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=iso(t0),
                                                           profile_state="failed")
    disabled = cap.with_event("worker.bots_disabled")
    assert len(disabled) == 1, disabled
    assert {"logger": "cq_server.worker", "level": "WARNING"}.items() <= disabled[0].items()
    assert _processed(cap, tid) == ["pending", "pending", "failed"]
