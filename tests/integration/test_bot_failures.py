"""Falhas do Telegram: token revogado, esperas, erros de rede e bloqueio (a fila com
bot sem acesso e a troca de token; a
chave-mestra trocada).

Contratos: a troca e a leitura do token, a posse, a recepção (reação a cada falha e relógio das
esperas), a saída, a perda de acesso e os problemas visíveis, os eventos, o `problem` de
`bots.get` e o registro `bot.poll_problem`.

Tudo pelo caminho real: API, um worker com `bots` numa thread (`bot_worker_thread`, `FakeClock` do
teste), o adaptador HTTP e a Bot API falsa com falhas programadas. **Esperas:** cada espera do
sistema é um prazo no `FakeClock`, liberado pelo teste com `clock.set`; a
prova é o `clock_at` da chamada seguinte no registro do dublê (o relógio só anda quando o teste
manda), e uma janela curta antes do fim do prazo confere que nada saiu antes (`_assert_quiet`, com
`bot_api.wait_for`). Uma vez por tipo de espera da consulta (`retry_after`, espera crescente, 3 s do
409), a janela com o relógio a 0,1 s do fim é **real e maior
que o próprio prazo** (`_release(…, hold=…)`) e a chamada tem de sair logo depois de o relógio chegar
ao fim: uma espera em tempo real, ou que ignore o prazo, terminaria dentro da janela, e só o avanço do
`FakeClock` explica a repetição. Uma chamada presa no dublê (`_Gate`, gancho `on_call`) segura o
sistema num ponto conhecido quando o caso precisa observar um estado intermediário.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import timedelta
from collections.abc import Callable
from typing import Any

import pytest

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    link_code,
    list_events,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import ERRORS, make_user, monotonic
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import OTHER_MASTER_KEY, all_secrets, bot_secrets

ADMIN = auth(ADMIN_TOKEN)
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
WORKER_ID = "worker-falhas"
LEASE = timedelta(seconds=30)  # `bot_settings` do conftest
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
CALL_TIMEOUT = 10.0
QUIET_SECONDS = 0.3
MAX_EXTRA_STEPS = 60
GATE_HOLD_SECONDS = 1.5  # menor que o timeout do adaptador (2 s; 3 s no getUpdates)
# Só para um `getUpdates` preso: abaixo da leitura de 3 s do adaptador (consulta 1 s + timeout 2 s).
POLL_GATE_HOLD_SECONDS = 2.5
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
# A janela real de silêncio passa o próprio prazo por esta folga (2 s → 2,5 s; 3 s → 3,5 s).
HOLD_MARGIN_SECONDS = 0.5
# Com o relógio no fim do prazo, a chamada seguinte sai "logo em seguida" (a thread confere o
# prazo em intervalos reais de até 0,05 s).
PROMPT_SECONDS = 2.0

# Parâmetros de toda consulta sem `offset` (`poll_timeout_seconds = 1`).
POLL_PARAMS = {"timeout": 1, "limit": 100, "allowed_updates": ["message", "callback_query", "my_chat_member"]}

# `problem.code` → mensagem fixa.
PROBLEMS = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "token_unreadable": "O token guardado não pode ser lido com a chave-mestra atual. Registre o token de novo.",
    "telegram_unavailable": "O Telegram não respondeu; o sistema vai tentar de novo.",
    "rate_limited": "O Telegram pediu uma pausa; o sistema vai tentar de novo depois do tempo indicado.",
    "concurrent_consumer": "Outro programa está consultando este bot. Pare o outro programa.",
    "webhook_active": "Havia um webhook configurado neste bot; ele foi removido.",
}

# O texto de ajuda, por extenso.
HELP = ("Este é o bot do oferecimento Algoritmos e Programação (2031s1). Para participar, toque no link de "
        "convite que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")

BOT_EVENT_TYPES = ("bot.lease_changed", "bot.access_changed", "bot.token_replaced")
# Texto neutro de uma mensagem livre: não pode aparecer em log nenhum.
FREE_TEXT_SENTINEL = "mensagem-livre-sentinela-q7Zp"


# ---------------------------------------------------------------------------------------------
# Apoio


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot (conferido no fim)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


class _Gate:
    """Segura no dublê a próxima chamada de `method` do bot (depois de `arm()`) até `release()`, no
    máximo `hold` segundos (padrão `GATE_HOLD_SECONDS`, abaixo do timeout do adaptador); o gancho
    roda antes do processamento da chamada (`on_call`). Com `when`, só segura a chamada que o satisfaz
    (as outras passam)."""

    def __init__(self, bot_api: Any, method: str, bot: int, hold: float = GATE_HOLD_SECONDS,
                 when: Callable[[Any], bool] | None = None) -> None:
        self._bot = bot
        self._hold = hold
        self._when = when
        self._armed = False
        self._lock = threading.Lock()
        self._open = threading.Event()
        self.arrived = threading.Event()
        self.call: Any = None
        bot_api.on_call(method, self._hook)

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def _hook(self, call: Any) -> None:
        with self._lock:
            if not self._armed or call.bot_id != self._bot or (self._when is not None and not self._when(call)):
                return
            self._armed = False
            self.call = call
        self.arrived.set()
        self._open.wait(self._hold)

    def wait_arrival(self, timeout: float = CALL_TIMEOUT) -> Any:
        assert self.arrived.wait(timeout), "a chamada esperada não chegou à Bot API falsa"
        return self.call

    def release(self) -> None:
        self._open.set()


def _offering(client: Any, bot_api: Any, code: str, username: str) -> tuple[dict, int]:
    """Oferecimento ativo com bot registrado na Bot API falsa."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM,
                               timezone="America/Sao_Paulo")
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _start_worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel,
                  worker_id: str = WORKER_ID, **kwargs: Any) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=worker_id),
                             **kwargs)


def _bot_view(client: Any, code: str) -> dict:
    resp = client.get(f"/v1/offerings/{code}/bot", headers=ADMIN)
    assert resp.status_code == 200, f"bots.get: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("bots.get", 200, body)
    return body


def _wait_bot(bot_api: Any, client: Any, code: str, **fields: Any) -> dict:
    """Espera `bots.get` mostrar os campos dados (ex.: `problem_code="rate_limited"`)."""

    def ready() -> dict | None:
        body = _bot_view(client, code)
        view = dict(body)
        view["problem_code"] = None if body["problem"] is None else body["problem"]["code"]
        return body if all(view[k] == v for k, v in fields.items()) else None

    return bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _assert_bot(body: dict, offering: dict, username: str, bot: int, *, state: str, registered_at: Any,
                last_poll_ok_at: Any, problem: str | None, lease_owner: str | None, marks: list[Any],
                profile_state: str = "configured", token_replaced_at: Any = None) -> None:
    """O corpo inteiro de `Bot`; `lease.until` é `m + posse` para um dos instantes
    `marks` em que o relógio esteve com o worker no ar (a renovação acontece em qualquer um deles)."""
    expected = {
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
        "username": username,
        "telegram_bot_id": bot,
        "state": state,
        "profile_state": profile_state,
        "registered_at": iso(registered_at),
        "token_replaced_at": None if token_replaced_at is None else iso(token_replaced_at),
        "lease": body["lease"] if lease_owner is not None else None,
        "last_poll_ok_at": None if last_poll_ok_at is None else iso(last_poll_ok_at),
        "problem": None if problem is None else {"code": problem, "message": PROBLEMS[problem]},
    }
    assert body == expected, f"Bot {body} != {expected}"
    if lease_owner is not None:
        assert body["lease"]["worker_id"] == lease_owner
        assert body["lease"]["until"] in {iso(m + LEASE) for m in marks}, body["lease"]


def _wait_configured(bot_api: Any, client: Any, code: str) -> None:
    _wait_bot(bot_api, client, code, profile_state="configured")


def _wait_ok_then_polling(bot_api: Any, bot: int) -> Any:
    """Espera uma consulta `ok` e a consulta seguinte em espera (a resposta `ok` já foi gravada)."""

    def ready() -> Any:
        oks = [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.outcome == "ok"]
        pending = bot_api.pending_polls(bot)
        return pending[-1] if oks and pending and pending[-1].seq > oks[-1].seq else None

    return bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _next_poll(bot_api: Any, bot: int, after_seq: int):
    """Procura a primeira `getUpdates` do bot depois de `after_seq`."""

    def find() -> Any:
        found = [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.seq > after_seq]
        return found[0] if found else None

    return find


def _next_call(bot_api: Any, bot: int, method: str, after_seq: int, **where: Any):
    def find() -> Any:
        found = [c for c in bot_api.calls(method, bot_id=bot) if c.seq > after_seq
                 and all(getattr(c, k) == v for k, v in where.items())]
        return found[0] if found else None

    return find


def _ended(bot_api: Any, seq: int) -> Any:
    """A chamada `seq` depois de terminar."""

    def ready() -> Any:
        found = [c for c in bot_api.calls() if c.seq == seq and c.outcome != "pending"]
        return found[0] if found else None

    return bot_api.wait_for(ready, timeout=CALL_TIMEOUT)


def _assert_quiet(bot_api: Any, find: Any, seconds: float = QUIET_SECONDS) -> None:
    """Nada do que `find` procura aparece na janela real de `seconds` (na janela curta, a prova de
    verdade é o `clock_at`; na longa de `_release(…, hold=…)`, é a própria janela)."""
    try:
        early = bot_api.wait_for(find, timeout=seconds)
    except AssertionError:
        return
    raise AssertionError(f"chamada antes do fim da espera: #{early.seq} {early.method} em {early.clock_at}")


def _release(clock: Any, bot_api: Any, find: Any, *, since: Any, seconds: float, exact: bool,
             marks: list[Any], hold: float | None = None) -> Any:
    """Libera uma espera de `seconds` contada de `since` no `FakeClock`: leva o relógio a 0,1 s do
    fim, confere que nada saiu, completa o prazo e devolve a chamada seguinte. `exact`: ela sai no
    instante exato do fim; senão (esperas "pelo menos N s"), o relógio anda de 1 em 1 s até ela sair,
    e ela nunca sai antes do fim. Cada instante posto no relógio entra em `marks` (a renovação da
    posse pode acontecer em qualquer um deles).

    `hold`: o relógio fica a 0,1 s do fim
    por `hold` segundos **reais**, mais que o próprio prazo, sem nenhuma chamada (a busca de `find`
    vê também as chamadas feitas antes de a janela começar), e a chamada tem de sair logo depois de o
    relógio chegar ao fim (`PROMPT_SECONDS`; nas esperas "pelo menos N s", no primeiro passo). Uma
    espera que corra em tempo real, ou que ignore o prazo, termina dentro da janela e é flagrada:
    a espera não termina antes do prazo em nenhum dos dois relógios."""
    target = since + timedelta(seconds=seconds)
    if hold is not None:
        assert hold > seconds, f"a janela real ({hold} s) tem de passar o próprio prazo ({seconds} s)"
    marks.append(clock.set(target - timedelta(seconds=0.1)))
    _assert_quiet(bot_api, find, QUIET_SECONDS if hold is None else hold)
    marks.append(clock.set(target))
    if exact:
        call = bot_api.wait_for(find, timeout=CALL_TIMEOUT if hold is None else PROMPT_SECONDS)
        assert call.clock_at == target, f"chamada em {call.clock_at}, esperada em {target}"
        return call
    for step in range(MAX_EXTRA_STEPS):
        try:
            call = bot_api.wait_for(find, timeout=PROMPT_SECONDS if hold is not None and step == 0 else 1.0)
        except AssertionError:
            marks.append(clock.advance(1))
            continue
        assert call.clock_at >= target, f"chamada em {call.clock_at}, antes de {target}"
        return call
    raise AssertionError(f"a espera não terminou nem {MAX_EXTRA_STEPS} s depois de {target}")


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _last_call(bot_api: Any) -> int:
    calls = bot_api.calls()
    return calls[-1].seq if calls else 0


def _event(type_: str, at: Any, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _lease(offering: dict, worker: str, change: str, reason: str, at: Any) -> dict:
    return _event("bot.lease_changed", at, offering_id=offering["offering_id"], worker_id=worker,
                  change=change, reason=reason)


def _access(offering: dict, access: str, problem: str | None, at: Any, actor: str | None = None) -> dict:
    return _event("bot.access_changed", at, actor, offering_id=offering["offering_id"], access=access,
                  problem=problem)


def _token_replaced(offering: dict, bot: int, version: int, at: Any) -> dict:
    return _event("bot.token_replaced", at, "bootstrap", offering_id=offering["offering_id"],
                  telegram_bot_id=bot, token_version=version)


def _events(client: Any, offering: dict, cursor: int, types: tuple[str, ...] = BOT_EVENT_TYPES) -> list[dict]:
    """Os eventos dos tipos dados depois de `cursor`, só os do oferecimento, sem o `seq`."""
    items = list_events(client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items
            if e["data"].get("offering_id") in (offering["offering_id"], None)]


def _wait_events(bot_api: Any, client: Any, offering: dict, cursor: int, count: int,
                 types: tuple[str, ...] = BOT_EVENT_TYPES) -> list[dict]:
    """Espera pelo menos `count` eventos (de `_events`) e devolve todos."""

    def ready() -> list[dict] | None:
        events = _events(client, offering, cursor, types)
        return events if len(events) >= count else None

    return bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _bot_row(sql: Any, bot: int) -> tuple:
    return sql.execute(
        "SELECT state, access_problem, lease_owner, token_version FROM bots WHERE telegram_bot_id = %s",
        (bot,)).fetchone()


def _processed(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> tuple:
    """(id, kind, state, outcome) da única linha do update, depois de processado."""
    assert update_id is not None

    def ready() -> list[tuple] | None:
        rows = sql.execute(
            """
            SELECT u.id, u.kind, u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
             WHERE b.telegram_bot_id = %s AND u.update_id = %s ORDER BY u.id
            """, (bot, update_id)).fetchall()
        return rows if rows and all(r[2] == "processed" for r in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _update_tasks(sql: Any, row_id: int) -> list[tuple]:
    return sql.execute("SELECT task_type, idempotency_key FROM tasks WHERE payload = %s::jsonb ORDER BY id",
                       (json.dumps({"update_row_id": row_id}),)).fetchall()


def _outbox_row(sql: Any, message_id: int) -> tuple:
    return sql.execute("SELECT state, attempts, available_at, last_error FROM outbox_messages WHERE id = %s",
                       (message_id,)).fetchone()


def _assert_poll_problem(cap: Any, offering: dict, problem: str) -> None:
    """registro `bot.poll_problem` com `offering_id` e `problem` (por inclusão)."""
    found = [r for r in cap.with_event("bot.poll_problem")
             if r.get("offering_id") == offering["offering_id"] and r.get("problem") == problem]
    assert found, f"nenhum bot.poll_problem {problem!r}: {cap.with_event('bot.poll_problem')}"
    assert {r.get("logger") for r in found} == {"cq_server.worker"}


def _assert_logs_clean(cap: Any, bot_api: Any, *values: str) -> None:
    """nenhum registro leva token (inteiro ou a parte secreta), a URL da Bot API,
    `chat_id`, `sender_id` nem texto de update (controle: os valores procurados não são vazios)."""
    tokens = bot_api.all_tokens()
    assert tokens, "controle: a varredura precisa de tokens"
    leaked = find_secrets(cap.text, [*tokens, *(t.split(":", 1)[1] for t in tokens), *all_secrets(),
                                     *bot_secrets()])
    assert leaked == [], f"segredo nos logs: {leaked}"
    assert bot_api.base_url not in cap.text
    for value in values:
        assert value and value not in cap.text, f"valor proibido nos logs: {value!r}"


# ---------------------------------------------------------------------------------------------
# Token recusado, recuperação e troca


def _revoke_and_wait(client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any,
                     settings: Any, code: str, username: str) -> tuple[dict, int, Any, int, Any]:
    """Oferecimento ativo sendo consultado; o token é revogado no meio de uma consulta e o bot fica
    `no_access`. Devolve (oferecimento, bot, t0, cursor de eventos de antes da revogação, 401)."""
    offering, bot = _offering(client, bot_api, code, username)
    t0 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_configured(bot_api, client, code)
    pending = _wait_ok_then_polling(bot_api, bot)
    cursor = _last_seq(sql)

    bot_api.revoke(bot)
    rejected = bot_api.wait_for(lambda: [c for c in bot_api.calls(bot_id=bot) if c.status == 401],
                                timeout=CALL_TIMEOUT)[0]
    assert (rejected.method, rejected.seq) == ("getUpdates", pending.seq)
    _wait_bot(bot_api, client, code, state="no_access")
    return offering, bot, t0, cursor, rejected


def test_revoked_token_marks_no_access_once_and_stops_polling(client: Any, clock: Any, sql: Any,
                                                             bot_api: Any, bot_worker_thread: Any,
                                                             settings: Any) -> None:
    """Perda de acesso: a revogação no meio de uma consulta leva o
    bot a `no_access` (`token_rejected`), limpa a posse, e saem `bot.lease_changed {released,
    no_access}` e um único `bot.access_changed {lost, token_rejected}`; `bots.get` mostra o problema
    exato; depois de um ciclo de reconciliação (outro bot ativado é assumido pelo mesmo worker),
    nenhuma chamada foi feita pelo bot revogado depois do 401."""
    username = "cq_falhas_revogado_bot"
    offering, bot, t0, cursor, rejected = _revoke_and_wait(
        client, clock, sql, bot_api, bot_worker_thread, settings, "mc102-revogado", username)

    expected_events = [_lease(offering, WORKER_ID, "released", "no_access", t0),
                       _access(offering, "lost", "token_rejected", t0)]
    assert _events(client, offering, cursor) == expected_events
    assert _bot_row(sql, bot) == ("no_access", "token_rejected", None, 1)
    _assert_bot(_bot_view(client, offering["code"]), offering, username, bot, state="no_access",
                registered_at=t0, last_poll_ok_at=t0, problem="token_rejected", lease_owner=None, marks=[t0])

    other, other_bot = _offering(client, bot_api, "mc102-controle", "cq_falhas_controle_bot")
    bot_api.wait_for(lambda: bot_api.calls("getUpdates", bot_id=other_bot), timeout=PROCESS_TIMEOUT)
    assert [c for c in bot_api.calls(bot_id=bot) if c.seq > rejected.seq] == []
    assert [c.seq for c in bot_api.calls(bot_id=bot) if c.status == 401] == [rejected.seq]
    assert _events(client, offering, cursor) == expected_events
    assert _bot_row(sql, bot) == ("no_access", "token_rejected", None, 1)


def test_new_token_after_no_access_restores_access_and_reacquires(client: Any, clock: Any, sql: Any,
                                                                 bot_api: Any, bot_worker_thread: Any,
                                                                 settings: Any) -> None:
    """Troca de token: `bots.register` com o token girado
    do mesmo bot devolve o bot `registered`, perfil `pending`, `token_replaced_at` e sem posse nem
    problema; saem `bot.token_replaced {token_version: 2}` e `bot.access_changed {restored, problem:
    null}` (na transação do registro) e, depois, `bot.lease_changed {acquired, acquired}` do worker; a
    nova posse começa por `deleteWebhook(drop_pending_updates=false)` e `getUpdates` sem `offset`, os
    dois com o token novo (versão 2 no dublê).

    Um evento por episódio, na perda de
    acesso: um 401 **depois** da recuperação (perfil `configured` e consulta `ok` com a versão 2)
    abre um episódio novo: nova soltura da posse (`bot.lease_changed {released, no_access}`) e novo
    `bot.access_changed {lost, token_rejected}`, um de cada; o bot volta a `no_access` com a versão
    2 e para de consultar. Conferido depois de um ciclo de reconciliação (outro bot ativado é assumido
    pelo mesmo worker): o histórico inteiro tem exatamente dois episódios de perda."""
    username = "cq_falhas_recupera_bot"
    offering, bot, t0, first_cursor, _ = _revoke_and_wait(
        client, clock, sql, bot_api, bot_worker_thread, settings, "mc102-recupera", username)
    clock.advance(STEP)
    t1 = clock.now()
    cursor = _last_seq(sql)
    seq = _last_call(bot_api)

    new_token = bot_api.rotate_token(bot)
    body = register_bot(client, ADMIN_TOKEN, offering["code"], new_token)
    _assert_bot(body, offering, username, bot, state="registered", profile_state="pending",
                registered_at=t0, token_replaced_at=t1, last_poll_ok_at=t0, problem=None,
                lease_owner=None, marks=[])
    assert [(c.method, c.token_version, c.status) for c in bot_api.calls("getMe", bot_id=bot)
            if c.seq > seq] == [("getMe", 2, 200)]

    events = _wait_events(bot_api, client, offering, cursor, 3)
    assert events == [_token_replaced(offering, bot, 2, t1),
                      _access(offering, "restored", None, t1, actor="bootstrap"),
                      _lease(offering, WORKER_ID, "acquired", "acquired", t1)]
    delete = bot_api.wait_for(_next_call(bot_api, bot, "deleteWebhook", seq), timeout=PROCESS_TIMEOUT)
    poll = bot_api.wait_for(_next_poll(bot_api, bot, seq), timeout=PROCESS_TIMEOUT)
    assert (delete.token_version, delete.params) == (2, {"drop_pending_updates": False})
    assert (poll.token_version, poll.params) == (2, POLL_PARAMS)
    assert delete.seq < poll.seq
    assert _bot_row(sql, bot) == ("registered", None, WORKER_ID, 2)

    # Episódio novo: 401 no meio de uma consulta da versão 2, com a recuperação já completa.
    assert _ended(bot_api, poll.seq).outcome == "ok"
    _wait_configured(bot_api, client, offering["code"])
    pending = _wait_ok_then_polling(bot_api, bot)
    assert pending.token_version == 2
    second_cursor = _last_seq(sql)
    bot_api.revoke(bot)
    rejected = bot_api.wait_for(lambda: [c for c in bot_api.calls(bot_id=bot) if c.status == 401
                                         and c.seq > poll.seq], timeout=CALL_TIMEOUT)[0]
    assert (rejected.method, rejected.seq, rejected.token_version) == ("getUpdates", pending.seq, 2)
    _wait_bot(bot_api, client, offering["code"], state="no_access")
    second_episode = [_lease(offering, WORKER_ID, "released", "no_access", t1),
                      _access(offering, "lost", "token_rejected", t1)]
    assert _events(client, offering, second_cursor) == second_episode
    assert _bot_row(sql, bot) == ("no_access", "token_rejected", None, 2)
    _assert_bot(_bot_view(client, offering["code"]), offering, username, bot, state="no_access",
                registered_at=t0, token_replaced_at=t1, last_poll_ok_at=t1, problem="token_rejected",
                lease_owner=None, marks=[])

    _, other_bot = _offering(client, bot_api, "mc102-recupera-controle", "cq_falhas_recupera2_bot")
    bot_api.wait_for(lambda: bot_api.calls("getUpdates", bot_id=other_bot), timeout=PROCESS_TIMEOUT)
    assert [c for c in bot_api.calls(bot_id=bot) if c.seq > rejected.seq] == []
    assert [c.seq for c in bot_api.calls(bot_id=bot)
            if c.status == 401 and c.seq > poll.seq] == [rejected.seq]
    assert _events(client, offering, second_cursor) == second_episode
    assert _events(client, offering, first_cursor) == [
        _lease(offering, WORKER_ID, "released", "no_access", t0),
        _access(offering, "lost", "token_rejected", t0),
        *events,
        *second_episode,
    ]
    assert _bot_row(sql, bot) == ("no_access", "token_rejected", None, 2)


def test_token_replacement_keeps_the_owner_and_restarts_polling(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any, bot_worker_thread: Any,
                                                               settings: Any) -> None:
    """Troca de token com o mesmo dono:
    trocar o token de um bot `registered` com dono mantém o `lease_owner`, não gera
    `bot.lease_changed`, e a consulta recomeça com o token novo: `deleteWebhook` e `getUpdates` **sem**
    o `offset` que a consulta anterior usava. A chamada antiga, feita com o token velho e respondida
    com 401 depois da troca, não derruba o bot novo (a `token_version` não confere): nenhum
    `bot.access_changed`, o bot continua `registered`. A consulta antiga fica presa no dublê por até
    `POLL_GATE_HOLD_SECONDS` (2,5 s, abaixo da leitura de 3 s do adaptador),
    para que o 401 só chegue depois de o registro gravar a versão 2."""
    username = "cq_falhas_troca_bot"
    offering, bot = _offering(client, bot_api, "mc102-troca-token", username)
    t0 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_configured(bot_api, client, offering["code"])
    user = make_user(301)
    uid = bot_api.push_text(bot, user, "/ajuda", bot_command=True)
    assert _processed(bot_api, sql, bot, uid)[3] == "help"
    bot_api.wait_for(lambda: [c for c in bot_api.pending_polls(bot) if c.params.get("offset") == uid + 1],
                     timeout=PROCESS_TIMEOUT)
    # A resposta sai pela fila depois do processamento: ela precisa ter saído com o token 1 antes da
    # troca, senão o 401 dela (token velho) derrubaria o acesso antes de o registro gravar a versão 2.
    sent = bot_api.wait_for(lambda: bot_api.sent_messages(bot, chat_id=user.user_id), timeout=PROCESS_TIMEOUT)
    assert [m.text for m in sent] == [HELP]
    assert _bot_row(sql, bot) == ("registered", None, WORKER_ID, 1)
    cursor = _last_seq(sql)

    # A consulta pendente (com `offset = uid + 1`) já chegou ao dublê; para que a chamada presa leve um
    # `offset`, uma mensagem sem comando (`not_handled`, sem resposta) é confirmada por ela, e o gate
    # segura só a consulta com `offset` (depois de resposta vazia, a consulta sai sem `offset`).
    gate = _Gate(bot_api, "getUpdates", bot, hold=POLL_GATE_HOLD_SECONDS, when=lambda c: "offset" in c.params)
    gate.arm()
    uid2 = bot_api.push_text(bot, user, "mensagem sem comando")
    try:
        old = gate.wait_arrival()
        assert (old.token_version, old.params) == (1, {**POLL_PARAMS, "offset": uid2 + 1})
        bot_api.fail("getUpdates", error="unauthorized", bot_id=bot, when=lambda c: c.seq == old.seq)
        clock.advance(STEP)
        t1 = clock.now()
        new_token = bot_api.rotate_token(bot)
        body = register_bot(client, ADMIN_TOKEN, offering["code"], new_token)
    finally:
        gate.release()
    _assert_bot(body, offering, username, bot, state="registered", profile_state="pending",
                registered_at=t0, token_replaced_at=t1, last_poll_ok_at=t0, problem=None,
                lease_owner=WORKER_ID, marks=[t0, t1])

    answered = _ended(bot_api, old.seq)
    assert (answered.token_version, answered.status, answered.description) == (1, 401, "Unauthorized")
    delete = bot_api.wait_for(_next_call(bot_api, bot, "deleteWebhook", old.seq, token_version=2),
                              timeout=PROCESS_TIMEOUT)
    poll = bot_api.wait_for(_next_call(bot_api, bot, "getUpdates", delete.seq, token_version=2),
                            timeout=PROCESS_TIMEOUT)
    assert delete.params == {"drop_pending_updates": False}
    assert poll.params == POLL_PARAMS
    assert [c.seq for c in bot_api.calls("getUpdates", bot_id=bot) if old.seq < c.seq < poll.seq] == []
    assert _ended(bot_api, poll.seq).outcome == "ok"
    bot_api.wait_for(_next_poll(bot_api, bot, poll.seq), timeout=CALL_TIMEOUT)

    assert _events(client, offering, cursor) == [_token_replaced(offering, bot, 2, t1)]
    assert _bot_row(sql, bot) == ("registered", None, WORKER_ID, 2)
    _assert_bot(_wait_bot(bot_api, client, offering["code"], profile_state="configured"),
                offering, username, bot, state="registered", registered_at=t0, token_replaced_at=t1,
                last_poll_ok_at=t1, problem=None, lease_owner=WORKER_ID, marks=[t0, t1])
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Esperas e erros da consulta


def test_retry_after_delays_the_next_poll(client: Any, clock: Any, sql: Any, bot_api: Any,
                                          bot_worker_thread: Any, settings: Any) -> None:
    """429 com `retry_after = 5` no `getUpdates` →
    `bots.get.problem = rate_limited` e nenhum `getUpdates` do bot antes de 5 s no `FakeClock`; a
    consulta seguinte sai depois, com os parâmetros de sempre, e a resposta `ok` limpa o problema
    (`null`); registro `bot.poll_problem {rate_limited}` sem token, URL nem texto.

    A espera de pelo menos N segundos (prazo no
    `Clock` injetado): um segundo 429, com `retry_after = 2`, prova que a espera não termina antes do
    prazo em nenhum relógio: o `FakeClock` fica a 0,1 s do fim por 2,5 s **reais** sem nenhum
    `getUpdates` do bot, e a consulta só sai quando o relógio chega ao fim, logo em seguida."""
    username = "cq_falhas_espera_bot"
    with capture_logs() as cap:
        offering, bot = _offering(client, bot_api, "mc102-espera", username)
        t0 = clock.now()
        channel = _DefaultChannel()
        _start_worker(bot_worker_thread, settings, channel)
        _wait_configured(bot_api, client, offering["code"])
        pending = _wait_ok_then_polling(bot_api, bot)
        marks = [t0]

        bot_api.fail("getUpdates", retry_after=5, bot_id=bot)
        limited = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, pending.seq),
                                                   timeout=CALL_TIMEOUT).seq)
        assert (limited.status, limited.retry_after, limited.params) == (429, 5, POLL_PARAMS)
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem_code="rate_limited"), offering,
                    username, bot, state="registered", registered_at=t0, last_poll_ok_at=t0,
                    problem="rate_limited", lease_owner=WORKER_ID, marks=[t0])

        after = _release(clock, bot_api, _next_poll(bot_api, bot, limited.seq), since=limited.clock_at,
                         seconds=5, exact=False, marks=marks)
        assert after.params == POLL_PARAMS
        body = _wait_bot(bot_api, client, offering["code"], problem=None)
        _assert_bot(body, offering, username, bot, state="registered", registered_at=t0,
                    last_poll_ok_at=after.clock_at, problem=None, lease_owner=WORKER_ID, marks=marks)

        # Segundo 429: `retry_after = 2`, com o relógio parado a 0,1 s do fim por uma janela real de 2,5 s.
        pending = _wait_ok_then_polling(bot_api, bot)
        ok_at = clock.now()
        bot_api.fail("getUpdates", retry_after=2, bot_id=bot)
        limited = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, pending.seq),
                                                   timeout=CALL_TIMEOUT).seq)
        assert (limited.status, limited.retry_after, limited.params) == (429, 2, POLL_PARAMS)
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem_code="rate_limited"), offering,
                    username, bot, state="registered", registered_at=t0, last_poll_ok_at=ok_at,
                    problem="rate_limited", lease_owner=WORKER_ID, marks=marks)
        held = _release(clock, bot_api, _next_poll(bot_api, bot, limited.seq), since=limited.clock_at,
                        seconds=2, exact=False, marks=marks, hold=2 + HOLD_MARGIN_SECONDS)
        assert held.params == POLL_PARAMS
        body = _wait_bot(bot_api, client, offering["code"], problem=None)
        _assert_bot(body, offering, username, bot, state="registered", registered_at=t0,
                    last_poll_ok_at=held.clock_at, problem=None, lease_owner=WORKER_ID, marks=marks)
    _assert_poll_problem(cap, offering, "rate_limited")
    _assert_logs_clean(cap, bot_api)
    assert channel.calls == []


def test_unavailable_waits_double_and_reset_after_success(client: Any, clock: Any, sql: Any,
                                                          bot_api: Any, bot_worker_thread: Any,
                                                          settings: Any) -> None:
    """Espera crescente (`min(2·2^(k−1), 60)` s, k = falhas seguidas,
    zerado em toda resposta `ok`) e classificação das falhas: 500, conexão cortada e 502 em HTML seguidos
    → `telegram_unavailable` e esperas de 2, 4 e 8 s no `FakeClock`; depois de um sucesso, um 400 no
    `getUpdates` também é `telegram_unavailable`, e as esperas voltam a 2 e 4 s; no fim, o problema
    some (`null`) e `last_poll_ok_at` é o da última resposta `ok`; registro `bot.poll_problem`.

    No degrau de 2 s (k = 1), o `FakeClock` fica a 0,1 s
    do fim por 2,5 s **reais** sem nenhum `getUpdates` do bot, e a consulta sai logo depois de o
    relógio chegar ao fim, no instante exato: uma espera em tempo real, ou fixa e menor que o degrau,
    é flagrada."""
    username = "cq_falhas_crescente_bot"
    with capture_logs() as cap:
        offering, bot = _offering(client, bot_api, "mc102-crescente", username)
        t0 = clock.now()
        channel = _DefaultChannel()
        _start_worker(bot_worker_thread, settings, channel)
        _wait_configured(bot_api, client, offering["code"])
        pending = _wait_ok_then_polling(bot_api, bot)
        marks = [t0]

        bot_api.fail("getUpdates", error="server_restart", bot_id=bot)
        first = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, pending.seq),
                                                 timeout=CALL_TIMEOUT).seq)
        assert (first.outcome, first.status, first.programmed) == ("error", 500, True)
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem_code="telegram_unavailable"),
                    offering, username, bot, state="registered", registered_at=t0, last_poll_ok_at=t0,
                    problem="telegram_unavailable", lease_owner=WORKER_ID, marks=marks)

        bot_api.fail_network("getUpdates", kind="reset", bot_id=bot)
        second = _release(clock, bot_api, _next_poll(bot_api, bot, first.seq), since=first.clock_at,
                          seconds=2, exact=True, marks=marks, hold=2 + HOLD_MARGIN_SECONDS)
        assert _ended(bot_api, second.seq).outcome == "reset"

        bot_api.fail_network("getUpdates", kind="not_json", bot_id=bot)
        third = _release(clock, bot_api, _next_poll(bot_api, bot, second.seq), since=second.clock_at,
                         seconds=4, exact=True, marks=marks)
        assert (_ended(bot_api, third.seq).outcome, _ended(bot_api, third.seq).status) == ("error", 502)

        fourth = _release(clock, bot_api, _next_poll(bot_api, bot, third.seq), since=third.clock_at,
                          seconds=8, exact=True, marks=marks)
        assert fourth.params == POLL_PARAMS
        # Só com a consulta já em espera no dublê (passou da checagem de falhas) a falha programada
        # agora vale para a seguinte.
        bot_api.wait_for(lambda: [c for c in bot_api.pending_polls(bot) if c.seq == fourth.seq],
                         timeout=CALL_TIMEOUT)
        bot_api.fail("getUpdates", status=400, description="Bad Request: pedido recusado no teste",
                     bot_id=bot, times=2)
        assert _ended(bot_api, fourth.seq).outcome == "ok"

        fifth = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, fourth.seq),
                                                 timeout=CALL_TIMEOUT).seq)
        assert (fifth.status, fifth.clock_at) == (400, fourth.clock_at)
        sixth = _release(clock, bot_api, _next_poll(bot_api, bot, fifth.seq), since=fifth.clock_at,
                         seconds=2, exact=True, marks=marks)
        assert _ended(bot_api, sixth.seq).status == 400
        seventh = _release(clock, bot_api, _next_poll(bot_api, bot, sixth.seq), since=sixth.clock_at,
                           seconds=4, exact=True, marks=marks)
        assert seventh.params == POLL_PARAMS

        body = _wait_bot(bot_api, client, offering["code"], problem=None)
        _assert_bot(body, offering, username, bot, state="registered", registered_at=t0,
                    last_poll_ok_at=seventh.clock_at, problem=None, lease_owner=WORKER_ID, marks=marks)
    _assert_poll_problem(cap, offering, "telegram_unavailable")
    _assert_logs_clean(cap, bot_api)
    assert channel.calls == []


def test_cut_connection_and_hung_response_lose_and_duplicate_nothing(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """uma conexão cortada e uma resposta presa (solta sem resposta) no
    `getUpdates` são falhas temporárias (esperas de 2 e 4 s); a consulta continua do ponto em que
    estava: o update injetado durante as falhas é gravado e respondido uma vez, e um update reentregue
    não gera segunda linha, segunda tarefa nem segunda resposta. Uma mensagem livre fica
    `not_handled`, e nenhum log leva o texto dela nem os ids dos chats."""
    username = "cq_falhas_rede_bot"
    with capture_logs() as cap:
        offering, bot = _offering(client, bot_api, "mc102-rede", username)
        t0 = clock.now()
        channel = _DefaultChannel()
        _start_worker(bot_worker_thread, settings, channel)
        _wait_configured(bot_api, client, offering["code"])
        pending = _wait_ok_then_polling(bot_api, bot)
        first_user, second_user, third_user = make_user(311), make_user(312), make_user(313)
        marks = [t0]

        bot_api.fail_network("getUpdates", kind="reset", bot_id=bot)
        bot_api.fail_network("getUpdates", kind="hang", bot_id=bot)
        uid1 = bot_api.push_text(bot, first_user, "/ajuda", bot_command=True)
        row1 = _processed(bot_api, sql, bot, uid1)
        assert row1[1:] == ("message", "processed", "help")
        cut = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, pending.seq), timeout=CALL_TIMEOUT).seq)
        assert (cut.outcome, cut.clock_at, cut.params) == ("reset", t0, {**POLL_PARAMS, "offset": uid1 + 1})

        uid2 = bot_api.push_text(bot, second_user, "/ajuda", bot_command=True)
        uid3 = bot_api.push_text(bot, third_user, FREE_TEXT_SENTINEL)
        hung = _release(clock, bot_api, _next_poll(bot_api, bot, cut.seq), since=cut.clock_at, seconds=2,
                        exact=True, marks=marks)
        assert hung.outcome == "pending" and hung.seq not in [c.seq for c in bot_api.pending_polls(bot)]
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem_code="telegram_unavailable"),
                    offering, username, bot, state="registered", registered_at=t0, last_poll_ok_at=t0,
                    problem="telegram_unavailable", lease_owner=WORKER_ID, marks=marks)
        bot_api.release_hangs()
        assert _ended(bot_api, hung.seq).outcome == "hang"

        resumed = _release(clock, bot_api, _next_poll(bot_api, bot, hung.seq), since=hung.clock_at,
                           seconds=4, exact=True, marks=marks)
        assert resumed.params == {**POLL_PARAMS, "offset": uid1 + 1}
        row2 = _processed(bot_api, sql, bot, uid2)
        row3 = _processed(bot_api, sql, bot, uid3)
        assert (row2[1:], row3[1:]) == (("message", "processed", "help"), ("message", "processed", "not_handled"))
        bot_api.wait_for(lambda: len(bot_api.sent_messages(bot)) >= 2, timeout=PROCESS_TIMEOUT)

        # A reentrega pode sair na consulta longa já pendente (iniciada antes da última chamada): a
        # procura é pelas consultas que terminaram depois do `redeliver`, e não pelo `seq`.
        mark = monotonic()
        bot_api.redeliver(bot, uid1)
        carrier = bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot)
                                            if c.ended is not None and c.ended > mark and c.outcome == "ok"
                                            and any(u["update_id"] == uid1 for u in c.result)],
                                   timeout=PROCESS_TIMEOUT)[0]
        bot_api.wait_for(_next_poll(bot_api, bot, carrier.seq), timeout=PROCESS_TIMEOUT)

        for uid, row in ((uid1, row1), (uid2, row2), (uid3, row3)):
            assert _processed(bot_api, sql, bot, uid)[0] == row[0]
            assert _update_tasks(sql, row[0]) == [("telegram.update", f"telegram.update:{row[0]}")]
        assert [m.text for m in bot_api.sent_messages(bot, chat_id=first_user.user_id)] == [HELP]
        assert [m.text for m in bot_api.sent_messages(bot, chat_id=second_user.user_id)] == [HELP]
        assert bot_api.sent_messages(bot, chat_id=third_user.user_id) == []
    _assert_poll_problem(cap, offering, "telegram_unavailable")
    _assert_logs_clean(cap, bot_api, FREE_TEXT_SENTINEL, str(first_user.user_id), str(second_user.user_id),
                       str(third_user.user_id))
    assert channel.calls == []


def test_webhook_conflict_deletes_the_webhook_and_goes_on(client: Any, clock: Any, sql: Any,
                                                           bot_api: Any, bot_worker_thread: Any,
                                                           settings: Any) -> None:
    """Webhook ligado: com um webhook ligado por terceiros,
    o `getUpdates` recebe 409 de webhook → `bots.get.problem = webhook_active` (observado com o
    `deleteWebhook` preso no dublê), `deleteWebhook(drop_pending_updates=false)` e a consulta segue
    sem espera; o webhook some e o problema vira `null` depois da consulta seguinte; registros
    `bot.poll_problem` (`rate_limited` e `webhook_active`)."""
    username = "cq_falhas_webhook_bot"
    with capture_logs() as cap:
        offering, bot = _offering(client, bot_api, "mc102-webhook", username)
        t0 = clock.now()
        channel = _DefaultChannel()
        _start_worker(bot_worker_thread, settings, channel)
        _wait_configured(bot_api, client, offering["code"])
        pending = _wait_ok_then_polling(bot_api, bot)
        marks = [t0]

        # Um 429 põe a consulta numa espera do relógio falso: o webhook é ligado sem consulta em curso.
        bot_api.fail("getUpdates", retry_after=1, bot_id=bot)
        limited = _ended(bot_api, bot_api.wait_for(_next_poll(bot_api, bot, pending.seq),
                                                   timeout=CALL_TIMEOUT).seq)
        assert limited.status == 429
        bot_api.set_webhook(bot)
        gate = _Gate(bot_api, "deleteWebhook", bot)
        gate.arm()
        try:
            conflict = _release(clock, bot_api, _next_poll(bot_api, bot, limited.seq), since=limited.clock_at,
                                seconds=1, exact=False, marks=marks)
            delete = gate.wait_arrival()
            assert (_ended(bot_api, conflict.seq).status, _ended(bot_api, conflict.seq).description) == \
                ERRORS["conflict_webhook"]
            assert (delete.params, delete.token_version, delete.seq > conflict.seq) == (
                {"drop_pending_updates": False}, 1, True)
            _assert_bot(_bot_view(client, offering["code"]), offering, username, bot, state="registered",
                        registered_at=t0, last_poll_ok_at=t0, problem="webhook_active",
                        lease_owner=WORKER_ID, marks=marks)
        finally:
            gate.release()
        after = bot_api.wait_for(_next_poll(bot_api, bot, delete.seq), timeout=CALL_TIMEOUT)
        assert (after.params, after.clock_at) == (POLL_PARAMS, conflict.clock_at)
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem=None), offering, username, bot,
                    state="registered", registered_at=t0, last_poll_ok_at=conflict.clock_at, problem=None,
                    lease_owner=WORKER_ID, marks=marks)
        assert bot_api.state(bot).webhook_url is None
    _assert_poll_problem(cap, offering, "rate_limited")
    _assert_poll_problem(cap, offering, "webhook_active")
    _assert_logs_clean(cap, bot_api)
    assert channel.calls == []


def test_external_consumer_is_concurrent_consumer_and_not_no_access(client: Any, clock: Any, sql: Any,
                                                                    bot_api: Any, bot_worker_thread: Any,
                                                                    settings: Any) -> None:
    """409 de outro `getUpdates` → `ConcurrentConsumer` (ainda dona,
    `last_problem = concurrent_consumer` e espera ≥ 3 s): um consumidor externo (o teste, com o
    token) encerra a consulta do sistema com 409; `bots.get` mostra `concurrent_consumer`, o bot
    continua `registered` (sem `bot.access_changed`) e nenhum `getUpdates` do sistema sai antes de 3 s
    no `FakeClock`; depois, a consulta volta e o problema vira `null`; registro `bot.poll_problem`.

    Os 3 s do 409 são prazo no `Clock` injetado: o
    `FakeClock` fica a 0,1 s do fim por 3,5 s **reais** sem nenhum `getUpdates` do bot, e a consulta
    só sai quando o relógio chega ao fim, logo em seguida."""
    username = "cq_falhas_consumidor_bot"
    with capture_logs() as cap:
        offering, bot = _offering(client, bot_api, "mc102-consumidor", username)
        t0 = clock.now()
        cursor = _last_seq(sql)
        channel = _DefaultChannel()
        _start_worker(bot_worker_thread, settings, channel)
        _wait_configured(bot_api, client, offering["code"])
        pending = _wait_ok_then_polling(bot_api, bot)
        marks = [t0]

        token = bot_api.token_of(bot)
        replies: list[Any] = []
        external = threading.Thread(
            target=lambda: replies.append(bot_api.request(token, "getUpdates", {"timeout": 1}, timeout=10)),
            name="consumidor-externo", daemon=True)
        external.start()
        ended, intruder = bot_api.wait_for(lambda: bot_api.conflicts(bot), timeout=CALL_TIMEOUT)[0]
        assert ended.seq == pending.seq
        assert (_ended(bot_api, pending.seq).status, _ended(bot_api, pending.seq).description) == \
            ERRORS["conflict_poll"]
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem_code="concurrent_consumer"),
                    offering, username, bot, state="registered", registered_at=t0, last_poll_ok_at=t0,
                    problem="concurrent_consumer", lease_owner=WORKER_ID, marks=marks)
        external.join(CALL_TIMEOUT)
        assert not external.is_alive()
        assert (replies[0].status, replies[0].json()) == (200, {"ok": True, "result": []})

        after = _release(clock, bot_api, _next_poll(bot_api, bot, intruder.seq), since=pending.clock_at,
                         seconds=3, exact=False, marks=marks, hold=3 + HOLD_MARGIN_SECONDS)
        assert after.params == POLL_PARAMS
        _assert_bot(_wait_bot(bot_api, client, offering["code"], problem=None), offering, username, bot,
                    state="registered", registered_at=t0, last_poll_ok_at=after.clock_at, problem=None,
                    lease_owner=WORKER_ID, marks=marks)
        assert [e for e in _events(client, offering, cursor) if e["type"] == "bot.access_changed"] == []
        assert _bot_row(sql, bot) == ("registered", None, WORKER_ID, 1)
    _assert_poll_problem(cap, offering, "concurrent_consumer")
    _assert_logs_clean(cap, bot_api)
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Falhas na saída


@pytest.mark.parametrize("deactivated", [False, True], ids=["bloqueou-o-bot", "conta-desativada"])
def test_blocked_chat_fails_the_message_and_blocks_the_endpoint(deactivated: bool, client: Any, clock: Any,
                                                                sql: Any, bot_api: Any,
                                                                bot_worker_thread: Any,
                                                                settings: Any) -> None:
    """Chat que bloqueou o bot (`ChatUnreachable` → `Undeliverable(blocked=True)`): a
    boas-vindas a um chat que bloqueou o bot (403 `bot was blocked by the user`, ou `user is
    deactivated`) falha de vez na 1ª tentativa (`chat_unreachable`), o endereço fica `blocked`, e saem
    `outbox.failed` e `endpoint.blocked`, nessa ordem, depois de `team.joined`; nada é entregue."""
    username = "cq_falhas_bloqueio_bot"
    offering, bot = _offering(client, bot_api, f"mc102-bloqueio-{int(deactivated)}", username)
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Chat Bloqueado", ra="5500000301")
    code = link_code(create_team_link(client, ADMIN_TOKEN, offering["code"], person["alias"])["link"])
    t0 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(321)
    bot_api.block_chat(bot, sender, deactivated=deactivated)
    cursor = _last_seq(sql)

    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code))
    assert row[1:] == ("message", "processed", "team_joined")

    def failed() -> list[tuple] | None:
        rows = sql.execute("SELECT id, state FROM outbox_messages ORDER BY id").fetchall()
        return rows if rows and rows[-1][1] == "failed" else None

    message_id = bot_api.wait_for(failed, timeout=PROCESS_TIMEOUT)[-1][0]
    assert _outbox_row(sql, message_id) == ("failed", 1, t0, "chat_unreachable")
    assert sql.execute(
        """
        SELECT p.pseudo_id::text, d.chat_id, d.state, d.created_at, d.changed_at
          FROM delivery_endpoints d JOIN people p ON p.id = d.person_id
        """).fetchall() == [(person["pseudo_id"], sender.user_id, "blocked", t0, t0)]
    error = ERRORS["user_deactivated" if deactivated else "blocked"]
    assert [(c.status, c.description) for c in bot_api.calls("sendMessage", bot_id=bot)] == [error]
    assert bot_api.sent_messages(bot) == []
    pseudo = person["pseudo_id"]
    assert _events(client, offering, cursor, ("team.joined", "outbox.failed", "endpoint.blocked")) == [
        _event("team.joined", t0, offering_id=offering["offering_id"], person=pseudo, endpoint_replaced=False),
        _event("outbox.failed", t0, message_id=message_id, recipient=pseudo, attempts=1, error="chat_unreachable"),
        _event("endpoint.blocked", t0, offering_id=offering["offering_id"], person=pseudo, channel="telegram"),
    ]
    assert channel.calls == []


def test_token_rejected_on_send_is_temporary_until_attempts_run_out(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """Fila com bot sem acesso: o 401 no
    `sendMessage` de uma resposta tira o acesso do bot (`bot.lease_changed {released, no_access}` e
    `bot.access_changed {lost, token_rejected}`) e é falha temporária (`token_rejected`, espera de
    2 s); as tentativas seguintes, com o bot `no_access`, são temporárias sem chamar o Telegram
    (`bot_no_access`, espera de 4 s), e na 3ª (a última) a mensagem falha de vez, com
    `outbox.reply_failed {message_id, reply_to, attempts: 3, error: bot_no_access}`."""
    username = "cq_falhas_envio_bot"
    offering, bot = _offering(client, bot_api, "mc102-envio-401", username)
    t0 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_configured(bot_api, client, offering["code"])
    _wait_ok_then_polling(bot_api, bot)
    cursor = _last_seq(sql)
    user = make_user(331)

    bot_api.fail("sendMessage", error="unauthorized", bot_id=bot)
    uid = bot_api.push_text(bot, user, "/ajuda", bot_command=True)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == ("message", "processed", "help")
    message_id = sql.execute("SELECT id FROM outbox_messages WHERE reply_to_update_id = %s",
                             (row[0],)).fetchone()[0]

    def attempts(n: int, state: str) -> Any:
        def ready() -> tuple | None:
            row = _outbox_row(sql, message_id)
            return row if (row[0], row[1]) == (state, n) else None

        return ready

    assert bot_api.wait_for(attempts(1, "pending"), timeout=PROCESS_TIMEOUT) == (
        "pending", 1, t0 + timedelta(seconds=2), "token_rejected")
    _assert_bot(_wait_bot(bot_api, client, offering["code"], state="no_access"), offering, username, bot,
                state="no_access", registered_at=t0, last_poll_ok_at=t0, problem="token_rejected",
                lease_owner=None, marks=[t0])

    clock.set(t0 + timedelta(seconds=2))
    assert bot_api.wait_for(attempts(2, "pending"), timeout=PROCESS_TIMEOUT) == (
        "pending", 2, t0 + timedelta(seconds=6), "bot_no_access")
    clock.set(t0 + timedelta(seconds=6))
    assert bot_api.wait_for(attempts(3, "failed"), timeout=PROCESS_TIMEOUT) == (
        "failed", 3, t0 + timedelta(seconds=6), "bot_no_access")

    assert [(c.status, c.params) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        (401, {"chat_id": user.user_id, "text": HELP})]
    assert bot_api.sent_messages(bot) == []
    types = ("bot.lease_changed", "bot.access_changed", "outbox.reply_failed", "outbox.failed")
    assert _events(client, offering, cursor, types) == [
        _lease(offering, WORKER_ID, "released", "no_access", t0),
        _access(offering, "lost", "token_rejected", t0),
        _event("outbox.reply_failed", t0 + timedelta(seconds=6), message_id=message_id, reply_to=row[0],
               attempts=3, error="bot_no_access"),
    ]
    assert channel.calls == []


def test_other_master_key_makes_the_token_unreadable_without_calling_telegram(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any,
        bot_settings: Any, stop_one: Any) -> None:
    """Chave-mestra trocada (leitura do token e perda de acesso): com o perfil
    já `configured` por um worker com a chave certa (parado de forma ordenada) e nenhuma mensagem com
    rota pendente, um worker com **outra** chave válida assume a posse e, sem chamar o Telegram, deixa
    o bot `no_access` (`token_unreadable`), com os eventos nesta ordem: `bot.lease_changed {acquired}`,
    `bot.lease_changed {released, no_access}` e `bot.access_changed {lost, token_unreadable}`, um
    único episódio (conferido depois de outro bot ser tratado pelo mesmo worker)."""
    username = "cq_falhas_chave_bot"
    offering, bot = _offering(client, bot_api, "mc102-chave", username)
    t0 = clock.now()
    right = _start_worker(bot_worker_thread, settings, _DefaultChannel(), worker_id="worker-chave-certa")
    _wait_configured(bot_api, client, offering["code"])
    _wait_ok_then_polling(bot_api, bot)
    assert stop_one(right) != float("inf")
    assert (right.error, right.returned) == (None, True)
    assert sql.execute("SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0] == 0
    seq = _last_call(bot_api)
    cursor = _last_seq(sql)

    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, worker_id="worker-chave-trocada",
                  bot_settings=dataclasses.replace(bot_settings, master_key=OTHER_MASTER_KEY))
    expected = [_lease(offering, "worker-chave-trocada", "acquired", "acquired", t0),
                _lease(offering, "worker-chave-trocada", "released", "no_access", t0),
                _access(offering, "lost", "token_unreadable", t0)]
    assert _wait_events(bot_api, client, offering, cursor, 3) == expected

    other, other_bot = _offering(client, bot_api, "mc102-chave-controle", "cq_falhas_chave2_bot")
    other_seq = _last_call(bot_api)
    _wait_events(bot_api, client, other, 0, 1, ("bot.access_changed",))
    assert _events(client, offering, cursor) == expected
    assert [c for c in bot_api.calls(bot_id=bot) if c.seq > seq] == []
    assert [c for c in bot_api.calls(bot_id=other_bot) if c.seq > other_seq] == []
    assert _events(client, offering, 0, ("bot.lease_changed",))[:2] == [
        _lease(offering, "worker-chave-certa", "acquired", "acquired", t0),
        _lease(offering, "worker-chave-certa", "released", "shutdown", t0)]
    assert _bot_row(sql, bot) == ("no_access", "token_unreadable", None, 1)
    _assert_bot(_bot_view(client, offering["code"]), offering, username, bot, state="no_access",
                registered_at=t0, last_poll_ok_at=t0, problem="token_unreadable", lease_owner=None,
                marks=[t0])
    assert channel.calls == []
