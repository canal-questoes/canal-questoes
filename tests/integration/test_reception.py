"""Recepção dos updates pelo worker.

Cobre a recepção, a gravação e o processamento único dos updates, com a ajuda; a mensagem
repetida, a sequência que recomeça, o webhook e as mensagens de quem não está vinculado;
o `offset` e a época; as tabelas (colunas e `details` de cada `kind`, tomada de posse, `getUpdates`,
gravação antes da confirmação, época e desfechos) e o registro `telegram.update`.

Um worker com `bots` (`bot_worker_thread`, `FakeClock` do teste, `BotSettings` do conftest: consulta
de 1 s, timeout de 2 s) consulta a Bot API falsa. Os parâmetros de cada `getUpdates` são comparados
por igualdade exata (`BotApiCall.params`), e a regra do `offset` é conferida sobre a sequência
inteira de chamadas: a 1ª da posse sem `offset`; depois de uma resposta com updates, `offset` = maior
`update_id` + 1; depois de uma resposta vazia, sem `offset`. Um gancho `on_call("getUpdates")` confere
no banco, na chegada de cada chamada, que todo update confirmado por ela já está gravado. Os
recomeços de numeração abaixo do `offset` antigo são feitos por um gancho na chegada da 1ª consulta
depois de uma resposta vazia (fora disso, a consulta pendente do dublê descartaria os updates novos,
como o servidor faria).

Convenções: linhas de `telegram_updates` conferidas por inteiro (menos `id` e `bot_id`);
horários pelo `FakeClock`; `update_id` só por relação (os valores vêm do dublê ou de
`restart_sequence`, que o próprio teste escolhe); esperas liberadas com `clock.advance`; esperas
reais só por outra thread, com `bot_api.wait_for` e prazo.
"""

from __future__ import annotations

import dataclasses
import threading
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.events import EventType
from cq_server.tasks import TaskRecord
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    list_events,
    offering_ref,
    register_bot,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import capture_logs

WORKER_ID = "worker-recepcao"
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
POLL_PARAMS = {"timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES}
# O texto de ajuda (`HELP`), escrito aqui para o teste não depender da implementação.
HELP = ("Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite "
        "que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
UNAVAILABLE_MESSAGE = "O Telegram não respondeu; o sistema vai tentar de novo."
GROUP = {"id": -4000000001, "type": "group", "title": "Grupo de estudos"}
ROW_COLUMNS = ("update_epoch", "update_id", "kind", "chat_id", "chat_type", "sender_id", "text",
               "command", "details", "received_at", "state", "outcome", "processed_at")


@pytest.fixture(autouse=True)
def _harness_bot_settings(bot_settings: Any) -> None:
    """`POLL_PARAMS` depende do `bot_settings` do conftest (consulta de 1 s)."""
    assert (bot_settings.poll_timeout_seconds, bot_settings.telegram_timeout_seconds) == (1, 2)


@dataclasses.dataclass(frozen=True)
class _Bot:
    offering: dict
    fake: int
    row: int


# --- auxiliares ----------------------------------------------------------------------------------


def _setup(client: Any, bot_api: Any, sql: psycopg.Connection, *, activate: bool = True) -> _Bot:
    """Oferecimento com bot registrado na Bot API falsa; ativo, salvo `activate=False`."""
    offering = create_offering(client, ADMIN_TOKEN)
    fake = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), bot_api.token_of(fake))
    row = sql.execute(
        "SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.offering_id = %s",
        (uuid.UUID(offering["offering_id"]),)).fetchone()
    assert row is not None, "o bot registrado não está no banco"
    bot = _Bot(offering=offering, fake=fake, row=row[0])
    if activate:
        _activate(client, bot)
    return bot


def _activate(client: Any, bot: _Bot) -> None:
    activate_bot(client, ADMIN_TOKEN, offering_ref(bot.offering))


def _help(bot: _Bot) -> str:
    return HELP.replace("{course}", bot.offering["course"]).replace("{term}", bot.offering["term"])


def _wait(bot_api: Any, predicate: Callable[[], Any], what: str, *, timeout: float = 15.0,
          workers: tuple[Any, ...] = ()) -> Any:
    """`bot_api.wait_for` com prazo; se um worker morrer antes, falha na hora com a causa."""
    def check() -> Any:
        for worker in workers:
            if not worker.is_alive() and not worker.returned:
                raise AssertionError(f"{what}: o worker {worker.name} terminou antes") from worker.error
        return predicate()

    try:
        return bot_api.wait_for(check, timeout=timeout)
    except AssertionError as exc:
        if exc.__cause__ is not None:
            raise
        raise AssertionError(f"{what}: {exc}") from None


def _rows(sql: psycopg.Connection, bot: _Bot) -> list[dict]:
    """As linhas de `telegram_updates` do bot, em ordem de gravação, sem `id` nem `bot_id`."""
    cursor = sql.execute(
        f"SELECT id, {', '.join(ROW_COLUMNS)} FROM telegram_updates WHERE bot_id = %s ORDER BY id",
        (bot.row,))
    return [dict(zip(("id", *ROW_COLUMNS), row)) for row in cursor.fetchall()]


def _without_id(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]


def _processed(sql: psycopg.Connection, bot: _Bot) -> list[dict]:
    return [row for row in _rows(sql, bot) if row["state"] == "processed"]


def _update_tasks(sql: psycopg.Connection) -> list[tuple[Any, ...]]:
    """(chave de idempotência, payload, estado) das tarefas `telegram.update`, pela chave."""
    return sorted((tuple(r) for r in sql.execute(
        "SELECT idempotency_key, payload, state FROM tasks WHERE task_type = 'telegram.update'"
    ).fetchall()), key=lambda t: t[0])


def _expected_tasks(rows: list[dict]) -> list[tuple[Any, ...]]:
    """Uma tarefa por linha nova, com o id da linha e a chave própria."""
    return sorted(((f"telegram.update:{row['id']}", {"update_row_id": row["id"]}, "done")
                   for row in rows), key=lambda t: t[0])


def _by_update(rows: list[dict]) -> list[dict]:
    """Linhas pela ordem de (época, número): a ordem de gravação dentro de um lote não é contrato."""
    return sorted(rows, key=lambda row: (row["update_epoch"], row["update_id"]))


def _polls(bot_api: Any, bot: _Bot) -> list[Any]:
    return bot_api.calls("getUpdates", bot_id=bot.fake)


def _delivered(bot_api: Any, bot: _Bot) -> dict[int, dict]:
    """Os updates que o dublê entregou ao sistema (respostas `ok` de `getUpdates`), por número."""
    out: dict[int, dict] = {}
    for call in _polls(bot_api, bot):
        if call.outcome == "ok":
            for update in call.result:
                out[update["update_id"]] = update
    return out


def _deliveries_of(bot_api: Any, bot: _Bot, update_id: int) -> list[Any]:
    return [c for c in _polls(bot_api, bot)
            if c.outcome == "ok" and any(u["update_id"] == update_id for u in c.result)]


def _assert_offset_rules(bot_api: Any, bot: _Bot) -> None:
    """As regras do `offset` e da época sobre toda a sequência de `getUpdates` do bot (sem
    falhas): parâmetros exatos; 1ª sem `offset`; depois de resposta com updates, `offset` = maior
    `update_id` + 1; depois de resposta vazia, sem `offset`."""
    polls = _polls(bot_api, bot)
    assert polls, "nenhum getUpdates"
    assert polls[0].params == POLL_PARAMS, bot_api.transcript()
    for prev, nxt in zip(polls, polls[1:]):
        assert prev.outcome == "ok", bot_api.transcript()
        expected = dict(POLL_PARAMS)
        if prev.result:
            expected["offset"] = max(u["update_id"] for u in prev.result) + 1
        assert nxt.params == expected, f"#{nxt.seq}: {nxt.params} != {expected}\n{bot_api.transcript()}"


def _message_row(raw: dict, *, at: datetime, outcome: str, command: str | None = None,
                 epoch: int = 0, chat_type: str = "private") -> dict:
    """A linha esperada de um update `message`, a partir do update entregue."""
    message = raw["message"]
    return {"update_epoch": epoch, "update_id": raw["update_id"], "kind": "message",
            "chat_id": message["chat"]["id"], "chat_type": chat_type,
            "sender_id": message["from"]["id"], "text": message["text"], "command": command,
            "details": {"message_id": message["message_id"], "date": message["date"]},
            "received_at": at, "state": "processed", "outcome": outcome, "processed_at": at}


def _on_first_poll_after_empty(bot_api: Any, bot: _Bot, after_seq: int,
                               action: Callable[[], None]) -> threading.Event:
    """Roda `action()` uma vez, na chegada do 1º `getUpdates` do bot que segue uma resposta vazia
    posterior à chamada `after_seq` (o gancho roda antes de o dublê atender a chamada)."""
    fired = threading.Event()

    def hook(call: Any) -> None:
        if call.bot_id != bot.fake or fired.is_set():
            return
        previous = [c for c in _polls(bot_api, bot) if c.seq < call.seq]
        if not previous:
            return
        last = previous[-1]
        if last.seq <= after_seq or last.outcome != "ok" or last.result:
            return
        fired.set()
        action()

    bot_api.on_call("getUpdates", hook)
    return fired


def _bot_body(client: Any, bot: _Bot) -> dict:
    resp = client.get(f"/v1/offerings/{urllib.parse.quote(bot.offering['code'], safe='')}/bot",
                      headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("bots.get", 200, body)
    return body


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Todos os eventos (de qualquer tipo) depois de `cursor`, sem o `seq` (conferido crescente)."""
    items = list_events(client, ADMIN_TOKEN, *(t.value for t in EventType), after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _lease_event(bot: _Bot, change: str, reason: str, at: datetime) -> dict:
    return {"type": "bot.lease_changed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": bot.offering["offering_id"], "worker_id": WORKER_ID,
                     "change": change, "reason": reason}}


def _profile_event(bot: _Bot, at: datetime) -> dict:
    return {"type": "bot.profile_updated", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": bot.offering["offering_id"], "profile_state": "configured",
                     "purpose": "active"}}


def _profile_state(sql: psycopg.Connection, bot: _Bot) -> str:
    return sql.execute("SELECT profile_state FROM bots WHERE id = %s", (bot.row,)).fetchone()[0]


def _replies(sql: psycopg.Connection, bot: _Bot) -> list[tuple[Any, ...]]:
    """(id, texto, estado, pessoa, update respondido) das mensagens de saída do bot, por `id`."""
    return [tuple(r) for r in sql.execute(
        "SELECT id, text, state, recipient_person_id, reply_to_update_id FROM outbox_messages "
        "WHERE bot_id = %s ORDER BY id", (bot.row,)).fetchall()]


def _update_tasks_by_id(sql: psycopg.Connection) -> list[tuple[Any, ...]]:
    """(chave de idempotência, payload, estado) das tarefas `telegram.update`, pelo `id`."""
    return [tuple(r) for r in sql.execute(
        "SELECT idempotency_key, payload, state FROM tasks WHERE task_type = 'telegram.update' "
        "ORDER BY id").fetchall()]


def _ok_after(bot_api: Any, bot: _Bot, since: datetime) -> bool:
    """Houve uma resposta `ok` a um `getUpdates` que chegou com o relógio em `since` ou depois, e
    uma chamada seguinte já começou (a transação daquela resposta já foi confirmada)."""
    polls = _polls(bot_api, bot)
    for i, call in enumerate(polls):
        if call.outcome == "ok" and call.clock_at >= since and call.ended is not None:
            return any(later.started > call.ended for later in polls[i + 1:])
    return False


# --- tomada de posse e webhook ----------------------------------------------------------------------


def test_takeover_removes_webhook_without_dropping_pending_updates(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any,
        bot_worker_thread: Any) -> None:
    """Webhook ativo; a tomada da posse: com um
    webhook ativo e dois updates injetados antes da ativação, a 1ª chamada da posse é
    `deleteWebhook` com `{"drop_pending_updates": false}` exato, depois `getUpdates` sem `offset`,
    com `timeout`, `limit` e `allowed_updates` exatos em toda chamada; o webhook sai, as pendências
    de antes da ativação chegam e são gravadas com o texto literal, sem 409 e sem
    `getWebhookInfo`; o dublê guarda `allowed_updates` = mensagens, botões e membro do chat."""
    bot = _setup(client, bot_api, sql, activate=False)
    bot_api.set_webhook(bot.fake)
    t0 = clock.now()
    first = bot_api.push_text(bot.fake, make_user(1), "Mensagem de antes da ativação")
    second = bot_api.push_text(bot.fake, make_user(2), "Olá, tudo bem? 👋 Çé ü")
    _activate(client, bot)

    worker = bot_worker_thread()
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 2, "pendências gravadas e processadas",
          workers=(worker,))

    calls = [c for c in bot_api.calls(bot_id=bot.fake)
             if c.method in ("deleteWebhook", "getUpdates", "getWebhookInfo")]
    assert calls[0].method == "deleteWebhook", bot_api.transcript()
    assert calls[0].params == {"drop_pending_updates": False}
    assert calls[0].outcome == "ok"
    assert calls[1].method == "getUpdates" and calls[1].params == POLL_PARAMS, bot_api.transcript()
    assert calls[1].outcome == "ok"
    assert [u["update_id"] for u in calls[1].result] == [first, second]
    assert bot_api.calls("getWebhookInfo") == []
    assert [c.seq for c in bot_api.calls("deleteWebhook", bot_id=bot.fake)] == [calls[0].seq]
    assert [c for c in _polls(bot_api, bot) if c.status == 409] == []
    state = bot_api.state(bot.fake)
    assert state.webhook_url is None
    assert state.allowed_updates == tuple(ALLOWED_UPDATES)
    _assert_offset_rules(bot_api, bot)

    delivered = _delivered(bot_api, bot)
    assert _without_id(_by_update(_rows(sql, bot))) == [
        _message_row(delivered[first], at=t0, outcome="not_handled"),
        _message_row(delivered[second], at=t0, outcome="not_handled"),
    ]


# --- gravar antes de confirmar ----------------------------------------------------------------------


def test_each_update_is_stored_before_the_offset_confirms_it(
        client: Any, bot_api: Any, sql: psycopg.Connection, db_url: str,
        bot_worker_thread: Any) -> None:
    """Cada update fica gravado antes de o worker pedir os seguintes; a recepção e a regra do `offset`:
    um gancho `on_call("getUpdates")` confere no banco, na chegada de cada chamada com `offset`, que
    todo `update_id < offset` já entregue está gravado; em três rodadas de updates, nenhuma
    confirmação antecede a gravação; a sequência inteira segue a regra do `offset` (inclusive
    `offset` vazio depois de resposta vazia); cada update vira uma linha e uma tarefa."""
    bot = _setup(client, bot_api, sql)
    checks: list[tuple[int, list[int]]] = []

    def stored_before_confirmed(call: Any) -> None:
        if call.bot_id != bot.fake or "offset" not in call.params:
            return
        offset = call.params["offset"]
        confirmed = {u["update_id"] for c in _polls(bot_api, bot)
                     if c.seq < call.seq and c.outcome == "ok" for u in c.result
                     if u["update_id"] < offset}
        with psycopg.connect(db_url, autocommit=True) as conn:
            stored = {r[0] for r in conn.execute(
                "SELECT update_id FROM telegram_updates WHERE bot_id = %s", (bot.row,))}
        checks.append((offset, sorted(confirmed - stored)))

    bot_api.on_call("getUpdates", stored_before_confirmed)
    worker = bot_worker_thread()
    pushed: list[int] = []
    for round_ in range(3):
        for k in range(round_ + 2):
            pushed.append(bot_api.push_text(bot.fake, make_user(10 * round_ + k),
                                            f"Rodada {round_}, mensagem {k}"))
        top = max(pushed)
        _wait(bot_api, lambda top=top: any(c.params.get("offset") == top + 1
                                           for c in _polls(bot_api, bot)),
              f"confirmação da rodada {round_}", workers=(worker,))
    last_seq = _polls(bot_api, bot)[-1].seq
    _wait(bot_api, lambda: any(c.seq > last_seq and "offset" not in c.params
                               for c in _polls(bot_api, bot)),
          "consulta sem offset depois de uma resposta vazia", workers=(worker,))
    _wait(bot_api, lambda: len(_processed(sql, bot)) == len(pushed), "updates processados",
          workers=(worker,))
    bot_worker_thread.stop()

    assert len(checks) >= 3, checks
    assert [missing for _, missing in checks if missing] == [], checks
    _assert_offset_rules(bot_api, bot)
    rows = _rows(sql, bot)
    assert [row["update_id"] for row in _by_update(rows)] == sorted(pushed)
    assert len(pushed) == len(set(pushed)) == 9
    assert all(row["state"] == "processed" and row["outcome"] == "not_handled" for row in rows)
    assert _update_tasks(sql) == _expected_tasks(rows)


# --- mensagem repetida ------------------------------------------------------------------------------


def test_redelivered_update_is_stored_and_processed_once(client: Any, bot_api: Any,
                                                         sql: psycopg.Connection, clock: Any,
                                                         bot_worker_thread: Any) -> None:
    """Uma mensagem entregue duas vezes é gravada e processada uma vez só (a mensagem
    repetida): um `/ajuda` entregue três vezes (duas `redeliver`) gera uma linha,
    uma tarefa `telegram.update` e uma única resposta `HELP`, com a regra do `offset` mantida."""
    bot = _setup(client, bot_api, sql)
    t0 = clock.now()
    user = make_user(30)
    worker = bot_worker_thread()
    uid = bot_api.push_text(bot.fake, user, "/ajuda", bot_command=True)
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 1 and bot_api.sent_messages(bot.fake),
          "1ª entrega processada", workers=(worker,))

    for n in (2, 3):
        bot_api.redeliver(bot.fake, uid)
        _wait(bot_api, lambda n=n: len(_deliveries_of(bot_api, bot, uid)) == n,
              f"entrega {n} do mesmo update", workers=(worker,))
        delivered_by = _deliveries_of(bot_api, bot, uid)[-1]
        _wait(bot_api, lambda d=delivered_by: any(c.started > d.ended for c in _polls(bot_api, bot)),
              f"consulta seguinte à entrega {n}", workers=(worker,))
    bot_worker_thread.stop()

    rows = _rows(sql, bot)
    raw = _delivered(bot_api, bot)[uid]
    assert _without_id(rows) == [_message_row(raw, at=t0, outcome="help", command="/ajuda")]
    assert _update_tasks(sql) == _expected_tasks(rows)
    sent = bot_api.sent_messages(bot.fake)
    assert [(m.chat_id, m.text) for m in sent] == [(user.user_id, _help(bot))]
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=bot.fake)] == [
        {"chat_id": user.user_id, "text": _help(bot)}]
    _assert_offset_rules(bot_api, bot)


def test_rerun_of_the_task_of_a_processed_update_changes_nothing(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any, queue: Any,
        bot_worker_thread: Any) -> None:
    """Cada update processado uma única vez e sem segundo processamento;
    o princípio das tarefas idempotentes e retomáveis (a fila retoma a tarefa cuja posse vence durante
    o tratamento); a recepção (tarefa `telegram.update` com `{"update_row_id": <id>}`)
    e os desfechos (o tratador trava a linha e, se ela já está `processed`, nada faz): depois de um `/ajuda`
    processado (`help`, `HELP` entregue), uma nova tarefa `telegram.update` com o mesmo payload e sem
    chave, enfileirada pela fila de tarefas com o relógio 15 s adiante, termina `done` na 1ª tentativa e
    sem efeito: a linha continua exatamente a do 1º processamento (`state`, `outcome` e
    `processed_at` inclusive), nenhuma mensagem nova na fila de saída, nenhum `sendMessage` novo e
    nenhum evento. Controle positivo: um `/ajuda` seguinte de outra pessoa é gravado, processado e
    respondido (a fila e a entrega seguiram rodando depois da tarefa repetida)."""
    bot = _setup(client, bot_api, sql)
    t0 = clock.now()
    first_user, second_user = make_user(90), make_user(91)
    help_text = _help(bot)
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID))
    workers = (worker,)
    uid = bot_api.push_text(bot.fake, first_user, "/ajuda", bot_command=True)
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 1 and len(bot_api.sent_messages(bot.fake)) == 1
          and [r[2] for r in _replies(sql, bot)] == ["sent"] and _profile_state(sql, bot) == "configured"
          and [t[2] for t in _update_tasks_by_id(sql)] == ["done"],
          "1º /ajuda processado e respondido, tarefa concluída, perfil configurado", workers=workers)
    [row] = _rows(sql, bot)
    assert _without_id([row]) == [
        _message_row(_delivered(bot_api, bot)[uid], at=t0, outcome="help", command="/ajuda")]
    replies = _replies(sql, bot)
    assert [r[1:] for r in replies] == [(help_text, "sent", None, row["id"])]
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=bot.fake)] == [
        {"chat_id": first_user.user_id, "text": help_text}]
    assert _update_tasks_by_id(sql) == [
        (f"telegram.update:{row['id']}", {"update_row_id": row["id"]}, "done")]
    cursor = _last_seq(sql)

    t1 = clock.advance(STEP)
    task_id = queue.enqueue("telegram.update", {"update_row_id": row["id"]})
    record = _wait(bot_api, lambda: (lambda r: r if r.state in ("done", "failed") else None)(
        queue.get(task_id)), "tarefa repetida concluída", workers=workers)
    assert record == TaskRecord(id=task_id, task_type="telegram.update", state="done", attempts=1,
                                available_at=t1, lease_owner=None, lease_until=None, last_error=None,
                                finished_at=t1)
    assert _rows(sql, bot) == [row]
    assert _replies(sql, bot) == replies
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=bot.fake)] == [
        {"chat_id": first_user.user_id, "text": help_text}]
    assert _events_after(client, cursor) == []

    control = bot_api.push_text(bot.fake, second_user, "/ajuda", bot_command=True)
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 2 and len(bot_api.sent_messages(bot.fake)) == 2
          and [r[2] for r in _replies(sql, bot)] == ["sent", "sent"]
          and [t[2] for t in _update_tasks_by_id(sql)] == ["done", "done", "done"],
          "controle: 2º /ajuda processado e respondido, tarefas concluídas", workers=workers)
    assert _events_after(client, cursor) == []
    bot_worker_thread.stop()

    rows = _rows(sql, bot)
    assert rows[0] == row
    assert _without_id(rows[1:]) == [
        _message_row(_delivered(bot_api, bot)[control], at=t1, outcome="help", command="/ajuda")]
    assert [r[1:] for r in _replies(sql, bot)] == [(help_text, "sent", None, row["id"]),
                                                   (help_text, "sent", None, rows[1]["id"])]
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(bot.fake)] == [
        (first_user.user_id, help_text), (second_user.user_id, help_text)]
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=bot.fake)] == [
        {"chat_id": first_user.user_id, "text": help_text},
        {"chat_id": second_user.user_id, "text": help_text}]
    assert _update_tasks_by_id(sql) == [
        (f"telegram.update:{row['id']}", {"update_row_id": row["id"]}, "done"),
        (None, {"update_row_id": row["id"]}, "done"),
        (f"telegram.update:{rows[1]['id']}", {"update_row_id": rows[1]["id"]}, "done")]


# --- sequência que recomeça e época -------------------------------------------------------------------


def test_sequence_restart_below_and_above_the_old_offset_loses_nothing(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any,
        bot_worker_thread: Any) -> None:
    """A recepção não supõe que a numeração é contínua; a sequência que recomeça;
    risco de a numeração recomeçar abaixo do `offset`: depois de 5000 e 5010 gravados (`offset` 5011), a
    numeração recomeça em 5005, abaixo do `offset` antigo, na 1ª consulta depois de uma resposta
    vazia; depois, em 7000, acima. Nenhum update se perde: as seis linhas, na ordem de gravação, com
    a mesma época; a consulta que recebe os de 5005 não leva `offset`."""
    bot = _setup(client, bot_api, sql, activate=False)
    t0 = clock.now()
    bot_api.restart_sequence(bot.fake, 5000)
    assert bot_api.push_text(bot.fake, make_user(50), "Antes do recomeço") == 5000
    _activate(client, bot)
    worker = bot_worker_thread()
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 1, "update 5000", workers=(worker,))

    bot_api.restart_sequence(bot.fake, 5010)
    assert bot_api.push_text(bot.fake, make_user(51), "Numeração acima") == 5010
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 2, "update 5010", workers=(worker,))
    deliverer = _deliveries_of(bot_api, bot, 5010)[-1].seq

    below: list[int | None] = []

    def restart_below() -> None:
        bot_api.restart_sequence(bot.fake, 5005)
        below.extend(bot_api.push_text(bot.fake, make_user(52 + k), f"Abaixo do offset antigo {k}")
                     for k in range(3))

    fired = _on_first_poll_after_empty(bot_api, bot, deliverer, restart_below)
    _wait(bot_api, lambda: fired.is_set() and len(_processed(sql, bot)) == 5,
          "updates abaixo do offset antigo", workers=(worker,))
    assert below == [5005, 5006, 5007]
    receiver = _deliveries_of(bot_api, bot, 5005)[0]
    assert receiver.params == POLL_PARAMS
    assert [u["update_id"] for u in receiver.result] == [5005, 5006, 5007]

    bot_api.restart_sequence(bot.fake, 7000)
    assert bot_api.push_text(bot.fake, make_user(55), "Muito acima") == 7000
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 6, "update 7000", workers=(worker,))
    bot_worker_thread.stop()

    delivered = _delivered(bot_api, bot)
    rows = _rows(sql, bot)
    assert [row["update_id"] for row in rows[:2]] == [5000, 5010]
    assert sorted(row["update_id"] for row in rows[2:5]) == [5005, 5006, 5007]
    assert rows[5]["update_id"] == 7000
    assert _without_id(_by_update(rows)) == [
        _message_row(delivered[uid], at=t0, outcome="not_handled")
        for uid in (5000, 5005, 5006, 5007, 5010, 7000)]
    assert _update_tasks(sql) == _expected_tasks(rows)
    _assert_offset_rules(bot_api, bot)


def test_new_epoch_after_48_hours_without_updates(client: Any, bot_api: Any,
                                                  sql: psycopg.Connection, clock: Any,
                                                  bot_worker_thread: Any) -> None:
    """Risco de a numeração recomeçar depois de um intervalo longo; a unicidade por `(bot_id,
    update_epoch, update_id)`) e a recepção (se o último update novo tem mais de 48 h, a época sobe
    1): um update 48 h exatas depois do anterior fica na mesma época; 48 h e 1 s depois do último,
    a numeração recomeça no mesmo `update_id` do primeiro (6000), que é gravado como linha nova na
    época seguinte e processado; nenhum update se perde."""
    bot = _setup(client, bot_api, sql, activate=False)
    bot_api.restart_sequence(bot.fake, 6000)
    t0 = clock.now()
    assert bot_api.push_text(bot.fake, make_user(60), "Primeira da época") == 6000
    _activate(client, bot)
    worker = bot_worker_thread()
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 1, "update 6000", workers=(worker,))

    t1 = clock.advance(48 * 3600)
    assert bot_api.push_text(bot.fake, make_user(61), "Ainda na mesma época") == 6001
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 2, "update 6001", workers=(worker,),
          timeout=20)
    deliverer = _deliveries_of(bot_api, bot, 6001)[-1].seq

    t2 = clock.advance(48 * 3600 + 1)
    again: list[int | None] = []

    def restart_same_number() -> None:
        bot_api.restart_sequence(bot.fake, 6000)
        again.append(bot_api.push_text(bot.fake, make_user(62), "Nova época, mesmo número"))

    fired = _on_first_poll_after_empty(bot_api, bot, deliverer, restart_same_number)
    _wait(bot_api, lambda: fired.is_set() and len(_processed(sql, bot)) == 3,
          "update 6000 na época nova", workers=(worker,), timeout=20)
    bot_worker_thread.stop()
    assert again == [6000]

    rows = _rows(sql, bot)
    first_raw = _deliveries_of(bot_api, bot, 6000)[0].result[0]
    second_raw = _delivered(bot_api, bot)[6001]
    third_raw = _deliveries_of(bot_api, bot, 6000)[-1].result[0]
    assert first_raw["message"]["text"] == "Primeira da época"
    assert third_raw["message"]["text"] == "Nova época, mesmo número"
    assert _without_id(rows) == [
        _message_row(first_raw, at=t0, outcome="not_handled", epoch=0),
        _message_row(second_raw, at=t1, outcome="not_handled", epoch=0),
        _message_row(third_raw, at=t2, outcome="not_handled", epoch=1),
    ]
    assert _update_tasks(sql) == _expected_tasks(rows)


# --- estado da consulta -----------------------------------------------------------------------------


def test_every_ok_response_updates_last_poll_and_clears_the_problem(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """Recepção: toda resposta `ok`, inclusive vazia, grava
    `last_poll_ok_at` = agora e limpa o último problema; um 500 no `getUpdates` deixa
    `problem = telegram_unavailable` (mensagem fixa do contrato) sem mexer em
    `last_poll_ok_at`; liberada a espera crescente no `FakeClock`, a resposta vazia seguinte limpa o
    problema e grava o novo horário. A consulta do bot é conferida por inteiro; cada avanço do
    relógio deixa a posse com menos de `poll_timeout + telegram_timeout + renew_seconds` (13 s), o
    que força a renovação e fixa `lease.until` = agora + 30 s."""
    t_reg = clock.now()
    bot = _setup(client, bot_api, sql)
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-recepcao"))
    _wait(bot_api, lambda: _ok_after(bot_api, bot, t_reg) and sql.execute(
        "SELECT profile_state FROM bots WHERE id = %s", (bot.row,)).fetchone()[0] == "configured",
          "1ª consulta e perfil configurado", workers=(worker,))

    def lease_until() -> datetime:
        return sql.execute("SELECT lease_until FROM bots WHERE id = %s", (bot.row,)).fetchone()[0]

    def expected(now: datetime, last_poll: datetime, problem: dict | None) -> dict:
        return {"offering": {"offering_id": bot.offering["offering_id"],
                             "code": bot.offering["code"]},
                "username": bot_api.state(bot.fake).username, "telegram_bot_id": bot.fake,
                "state": "registered", "profile_state": "configured",
                "registered_at": iso(t_reg), "token_replaced_at": None,
                "lease": {"worker_id": "worker-recepcao", "until": iso(now + timedelta(seconds=30))},
                "last_poll_ok_at": iso(last_poll), "problem": problem}

    t1 = clock.advance(20)
    _wait(bot_api, lambda: _ok_after(bot_api, bot, t1)
          and lease_until() == t1 + timedelta(seconds=30),
          "resposta vazia e renovação depois do avanço", workers=(worker,))
    assert _bot_body(client, bot) == expected(t1, t1, None)

    bot_api.fail("getUpdates", status=500, times=1, bot_id=bot.fake)
    unavailable = {"code": "telegram_unavailable", "message": UNAVAILABLE_MESSAGE}
    _wait(bot_api, lambda: _bot_body(client, bot)["problem"] == unavailable,
          "problema visível depois do 500", workers=(worker,))
    assert _bot_body(client, bot) == expected(t1, t1, unavailable)

    t2 = clock.advance(18)  # libera a espera crescente da 1ª falha (2 s) e força a renovação
    _wait(bot_api, lambda: _ok_after(bot_api, bot, t2)
          and lease_until() == t2 + timedelta(seconds=30),
          "resposta ok e renovação depois da espera", workers=(worker,))
    assert _bot_body(client, bot) == expected(t2, t2, None)
    failed = [c for c in _polls(bot_api, bot) if c.outcome == "error"]
    assert [(c.status, c.programmed) for c in failed] == [(500, True)]


def test_database_failure_while_storing_does_not_advance_the_offset(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any,
        bot_worker_thread: Any) -> None:
    """Nenhuma resposta perdida; a recepção (na falha da gravação, o lote é
    descartado, o `offset` é mantido e a espera cresce): com a gravação em `telegram_updates` falhando
    (gatilho que conta as tentativas numa sequência, fora da transação), o update entregue não é
    confirmado: toda consulta seguinte até a gravação dar certo repete os parâmetros da que falhou e
    recebe o mesmo update; removida a falha e liberada a espera no `FakeClock`, ele é gravado e
    processado uma vez, e só então o `offset` passa ao número dele + 1."""
    bot = _setup(client, bot_api, sql)
    worker = bot_worker_thread()
    _wait(bot_api, lambda: any(c.outcome == "ok" for c in _polls(bot_api, bot)), "1ª consulta",
          workers=(worker,))

    sql.execute("SET lock_timeout = '10s'")
    sql.execute("CREATE SEQUENCE cq_test_update_writes")
    sql.execute("CREATE FUNCTION cq_test_fail_update_write() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM nextval('cq_test_update_writes'); "
                "RAISE EXCEPTION 'falha de teste na gravacao do update'; END $$")
    sql.execute("CREATE TRIGGER cq_test_fail_update_write BEFORE INSERT ON telegram_updates "
                "FOR EACH ROW EXECUTE FUNCTION cq_test_fail_update_write()")
    t0 = clock.now()
    uid = bot_api.push_text(bot.fake, make_user(70), "Mensagem da falha de gravação")
    _wait(bot_api, lambda: sql.execute(
        "SELECT is_called FROM cq_test_update_writes").fetchone()[0] is True,
          "tentativa de gravação", workers=(worker,))
    sql.execute("DROP TRIGGER cq_test_fail_update_write ON telegram_updates")
    sql.execute("DROP FUNCTION cq_test_fail_update_write()")
    assert _rows(sql, bot) == []

    t1 = clock.advance(61)  # libera qualquer espera crescente (no máximo 60 s)
    _wait(bot_api, lambda: len(_processed(sql, bot)) == 1, "update gravado depois da falha",
          workers=(worker,), timeout=20)
    _wait(bot_api, lambda: any(c.params.get("offset") == uid + 1 for c in _polls(bot_api, bot)),
          "confirmação depois da gravação", workers=(worker,))
    bot_worker_thread.stop()

    polls = _polls(bot_api, bot)
    deliveries = _deliveries_of(bot_api, bot, uid)
    assert len(deliveries) >= 2, bot_api.transcript()
    failing = deliveries[0]
    first_index = polls.index(failing)
    last_index = polls.index(deliveries[-1])
    for call in polls[first_index:last_index + 1]:
        assert call.params == failing.params, f"#{call.seq} mudou o offset\n{bot_api.transcript()}"
        assert call.outcome == "ok" and [u["update_id"] for u in call.result] == [uid]
    assert polls[last_index + 1].params == {**POLL_PARAMS, "offset": uid + 1}
    assert "offset" not in failing.params
    assert failing.clock_at == t0
    rows = _rows(sql, bot)
    assert _without_id(rows) == [
        _message_row(_delivered(bot_api, bot)[uid], at=t1, outcome="not_handled")]
    assert _update_tasks(sql) == _expected_tasks(rows)


# --- desfechos -------------------------------------------------------------------------------------


def test_outcomes_help_replies_and_minimal_rows(client: Any, bot_api: Any, sql: psycopg.Connection,
                                               clock: Any, settings: Any,
                                               bot_worker_thread: Any) -> None:
    """Ajuda e desfechos; as mensagens de quem não está vinculado;
    as colunas e o registro `telegram.update`: `/ajuda` e `/start` sem código (com entidade
    `bot_command`) e `/ajuda@<username do bot>` → `help`, com o `HELP` exato enviado ao chat do update
    (`sendMessage` só com `chat_id` e `text`); texto comum, `/ajuda` sem entidade, `/ajuda` num grupo,
    `callback_query` e `my_chat_member` → `not_handled`, sem resposta e sem `answerCallbackQuery`.
    Cada linha tem as colunas e o `details` exatos do seu `kind`, estado `processed` e o desfecho;
    uma tarefa por update; um registro `telegram.update` com `update_row_id` e `outcome` por update
    (por inclusão); a gravação e esses desfechos não geram evento: todos os eventos,
    de qualquer tipo, depois do cursor tomado antes de o worker subir são exatamente os da posse
    (assunção e soltura na parada) e o do perfil (`bot.profile_updated {configured,
    active}`)."""
    bot = _setup(client, bot_api, sql)
    t0 = clock.now()
    username = bot_api.state(bot.fake).username
    users = [make_user(80 + k) for k in range(8)]
    cursor = _last_seq(sql)
    with capture_logs() as cap:
        worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(bot_api, lambda: _polls(bot_api, bot), "1ª consulta", workers=(worker,))
        ids = {
            "ajuda": bot_api.push_text(bot.fake, users[0], "/ajuda", bot_command=True),
            "start": bot_api.push_start(bot.fake, users[1]),
            "ajuda_bot": bot_api.push_text(bot.fake, users[2], f"/ajuda@{username}", bot_command=True),
            "texto": bot_api.push_text(bot.fake, users[3], "Bom dia! Tudo certo por aqui."),
            "sem_entidade": bot_api.push_text(bot.fake, users[4], "/ajuda"),
            "grupo": bot_api.push_text(bot.fake, users[5], "/ajuda", bot_command=True, chat=GROUP),
            "botao": bot_api.push_callback(bot.fake, users[6], "resposta-a"),
            "membro": bot_api.push_my_chat_member(bot.fake, users[7]),
        }
        _wait(bot_api, lambda: len(_processed(sql, bot)) == 8 and len(bot_api.sent_messages(
            bot.fake)) == 3 and sql.execute("SELECT count(*) FROM outbox_messages WHERE state <> "
                                            "'sent'").fetchone()[0] == 0
              and _profile_state(sql, bot) == "configured",
              "updates processados, respostas enviadas e perfil configurado", workers=(worker,))
        bot_worker_thread.stop()

    raw = _delivered(bot_api, bot)
    callback = raw[ids["botao"]]["callback_query"]
    member = raw[ids["membro"]]["my_chat_member"]
    rows = _rows(sql, bot)
    assert list(ids.values()) == sorted(ids.values())
    assert _without_id(_by_update(rows)) == [
        _message_row(raw[ids["ajuda"]], at=t0, outcome="help", command="/ajuda"),
        _message_row(raw[ids["start"]], at=t0, outcome="help", command="/start"),
        _message_row(raw[ids["ajuda_bot"]], at=t0, outcome="help", command=f"/ajuda@{username}"),
        _message_row(raw[ids["texto"]], at=t0, outcome="not_handled"),
        _message_row(raw[ids["sem_entidade"]], at=t0, outcome="not_handled"),
        _message_row(raw[ids["grupo"]], at=t0, outcome="not_handled", command="/ajuda",
                     chat_type="group"),
        {"update_epoch": 0, "update_id": ids["botao"], "kind": "callback_query",
         "chat_id": users[6].user_id, "chat_type": None, "sender_id": users[6].user_id,
         "text": None, "command": None,
         "details": {"callback_id": callback["id"], "data": "resposta-a",
                     "message_id": callback["message"]["message_id"]},
         "received_at": t0, "state": "processed", "outcome": "not_handled", "processed_at": t0},
        {"update_epoch": 0, "update_id": ids["membro"], "kind": "my_chat_member",
         "chat_id": users[7].user_id, "chat_type": "private", "sender_id": users[7].user_id,
         "text": None, "command": None,
         "details": {"date": member["date"], "old_status": "member", "new_status": "kicked"},
         "received_at": t0, "state": "processed", "outcome": "not_handled", "processed_at": t0},
    ]
    assert _update_tasks(sql) == _expected_tasks(rows)

    help_text = _help(bot)
    assert sorted((m.chat_id, m.text) for m in bot_api.sent_messages(bot.fake)) == sorted(
        (users[k].user_id, help_text) for k in (0, 1, 2))
    assert sorted((c.params["chat_id"], c.params["text"], tuple(sorted(c.params)))
                  for c in bot_api.calls("sendMessage")) == sorted(
        (users[k].user_id, help_text, ("chat_id", "text")) for k in (0, 1, 2))
    assert bot_api.calls("answerCallbackQuery") == []

    assert all_events(client, ADMIN_TOKEN, "team.joined", "team.join_refused", "team.link_revoked",
                      "endpoint.blocked", "outbox.reply_failed", "outbox.failed", "task.failed") == []
    events = _events_after(client, cursor)
    assert sorted(events[:2], key=lambda e: e["type"]) == [
        _lease_event(bot, "acquired", "acquired", t0), _profile_event(bot, t0)]
    assert events[2:] == [_lease_event(bot, "released", "shutdown", t0)]

    logged = [(r.get("update_row_id"), r.get("outcome")) for r in cap.with_event("telegram.update")]
    assert sorted(logged) == sorted((row["id"], row["outcome"]) for row in rows)
    for record in cap.with_event("telegram.update"):
        assert record.get("logger") == "cq_server.worker", record
