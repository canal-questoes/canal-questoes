"""Vínculo da equipe pelo `/start <código>` (conta do Telegram
já vinculada, troca de conta do professor e link
antes da ativação).

Contratos: as entidades do vínculo, o processamento, as regras do vínculo (ordem de avaliação,
reativação do endereço bloqueado e destino das respostas), os textos e os eventos.

Tudo passa pelo caminho real: a API gera o link, um worker com `bots` (fixture `bot_worker_thread`,
`FakeClock` do teste) consulta a Bot API falsa, grava e processa o update, e a resposta sai pela fila
de saída e pelo `sendMessage` do bot. Cada caso confere o `outcome` do update, o convite, a matrícula,
o endereço, as linhas da fila de saída (destino: endereço ou chat do update), a mensagem exata recebida
pelo remetente (`sent_messages`) e os eventos exatos, na ordem.

Os textos esperados são os do vínculo da equipe, escritos aqui por extenso (não vêm do código);
`{course}` e `{term}` são trocados pelos valores neutros do oferecimento. Os remetentes são neutros
(`make_user`, fora da faixa canário). Horários pelo `FakeClock`; os saltos de relógio com o worker no
ar são de 15 s (menos que a posse de 30 s), salvo onde o caso exige vencer o convite. Ids internos
nunca como literal: as linhas são lidas por junção.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets as stdlib_secrets
from datetime import timedelta
from typing import Any

import pytest

from cq_server.events import EventType
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.tasks import TaskRecord
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    create_offering,
    create_team_link,
    link_code,
    list_events,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso

COURSE = "Algoritmos e Programação"
TERM = "2031s1"

# Textos fixos, por extenso.
WELCOME_TEAM_TEMPLATE = ("Pronto! Você faz parte da equipe do oferecimento {course} ({term}). Por este chat "
                         "você vai receber os avisos do oferecimento.")
WELCOME = WELCOME_TEAM_TEMPLATE.replace("{course}", COURSE).replace("{term}", TERM)
HELP_TEMPLATE = ("Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite "
                 "que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
HELP = HELP_TEMPLATE.replace("{course}", COURSE).replace("{term}", TERM)
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."
ALREADY_LINKED = "Você já está vinculado a este oferecimento."
OTHER_PERSON = ("Esta conta do Telegram já está vinculada a outra pessoa neste oferecimento. Fale com a "
                "coordenação.")

BOT_USERNAME = "cq_equipe_vinculo_bot"
WORKER_ID = "worker-equipe"
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
TEAM_JOIN_DEADLINE_SECONDS = 5.0
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
DEFAULT_HOURS = 168
GROUP_CHAT = {"id": -4001234567, "type": "group", "title": "Turma de Teste do Vinculo"}
OTHER_BOT_USERNAME = "outro_bot_da_turma"

TEAM_EVENT_TYPES = ("team.link_created", "team.link_revoked", "team.joined", "team.join_refused",
                    "endpoint.blocked", "outbox.failed", "outbox.reply_failed")


# ---------------------------------------------------------------------------------------------
# Apoio


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot, então nada pode chegar
    aqui (conferido no fim de cada teste)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _offering(client: Any, bot_api: Any, code: str, username: str = BOT_USERNAME, *,
              activate: bool = True) -> tuple[dict, int]:
    """Oferecimento com bot registrado na Bot API falsa (e ativado, salvo `activate=False`)."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM,
                               timezone="America/Sao_Paulo")
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    if activate:
        activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _start_worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=WORKER_ID))


def _link(client: Any, offering: dict, person: dict, **kwargs: Any) -> tuple[str, dict]:
    body = create_team_link(client, ADMIN_TOKEN, offering["code"], person["alias"], **kwargs)
    return link_code(body["link"]), body


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _last_outbox(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _updates(sql: Any, bot: int, update_id: int) -> list[tuple]:
    """(id, kind, chat_id, chat_type, sender_id, text, command, state, outcome, processed_at)."""
    return sql.execute(
        """
        SELECT u.id, u.kind, u.chat_id, u.chat_type, u.sender_id, u.text, u.command, u.state,
               u.outcome, u.processed_at
          FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
         WHERE b.telegram_bot_id = %s AND u.update_id = %s
         ORDER BY u.id
        """, (bot, update_id)).fetchall()


def _processed(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update (tipo fora do allowed_updates)"

    def ready() -> list[tuple] | None:
        rows = _updates(sql, bot, update_id)
        return rows if rows and all(row[7] == "processed" for row in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _private_start(sender: Any, text: str, outcome: str, at: Any, command: str = "/start") -> tuple:
    """A linha esperada (sem o id) de um `/start` do chat privado de `sender`."""
    return ("message", sender.user_id, "private", sender.user_id, text, command, "processed", outcome, at)


def _outbox_after(sql: Any, after: int) -> list[tuple]:
    """(id, text, state, pseudo_id do destinatário, bot, chat do endereço, update respondido)."""
    return sql.execute(
        """
        SELECT o.id, o.text, o.state, p.pseudo_id::text, b.telegram_bot_id, d.chat_id, u.update_id
          FROM outbox_messages o
          LEFT JOIN people p ON p.id = o.recipient_person_id
          LEFT JOIN bots b ON b.id = o.bot_id
          LEFT JOIN delivery_endpoints d ON d.id = o.delivery_endpoint_id
          LEFT JOIN telegram_updates u ON u.id = o.reply_to_update_id
         WHERE o.id > %s
         ORDER BY o.id
        """, (after,)).fetchall()


def _settled_outbox(bot_api: Any, sql: Any, after: int, count: int) -> list[tuple]:
    """Espera `count` mensagens novas na fila, todas em estado final (`sent` ou `failed`)."""

    def ready() -> list[tuple] | None:
        rows = _outbox_after(sql, after)
        return rows if len(rows) >= count and all(r[2] in ("sent", "failed") for r in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == count, f"mensagens novas na fila: {[r[1:] for r in rows]}"
    return rows


def _sent_texts(bot_api: Any, bot: int, sender: Any) -> list[str]:
    return [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)]


def _invite(sql: Any, code: str) -> list[tuple]:
    """(state, expires_at, created_at, used_at, revoked_at) do convite do código."""
    return sql.execute(
        "SELECT state, expires_at, created_at, used_at, revoked_at FROM invites WHERE code_sha256 = %s",
        (_sha256(code),)).fetchall()


def _enrollments(sql: Any) -> list[tuple]:
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, e.role, e.state, e.created_at, e.activated_at
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         ORDER BY e.id
        """).fetchall()


def _endpoints(sql: Any) -> list[tuple]:
    return sql.execute(
        """
        SELECT p.pseudo_id::text, b.telegram_bot_id, d.channel, d.chat_id, d.state, d.created_at,
               d.changed_at
          FROM delivery_endpoints d
          JOIN people p ON p.id = d.person_id
          JOIN bots b ON b.id = d.bot_id
         ORDER BY d.id
        """).fetchall()


def _outbox_ids(sql: Any, after: int) -> list[int]:
    return [row[0] for row in _outbox_after(sql, after)]


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos da equipe e da entrega depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *TEAM_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _all_events_after(client: Any, cursor: int) -> list[dict]:
    """Todos os eventos, de qualquer tipo, depois de `cursor`, sem o `seq`."""
    items = list_events(client, ADMIN_TOKEN, *(t.value for t in EventType), after=cursor, limit=500)["items"]
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: Any, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": None, "data": data}


def _joined(offering: dict, person: dict, at: Any, replaced: bool) -> dict:
    return _event("team.joined", at, offering_id=offering["offering_id"], person=person["pseudo_id"],
                  endpoint_replaced=replaced)


def _refused(offering: dict, at: Any, refusal: str) -> dict:
    return _event("team.join_refused", at, offering_id=offering["offering_id"], refusal=refusal)


def _revoked(offering: dict, person: dict, at: Any, reason: str) -> dict:
    return _event("team.link_revoked", at, offering_id=offering["offering_id"], person=person["pseudo_id"],
                  revoke_reason=reason)


def _update_tasks(bot_api: Any, sql: Any, row_id: int) -> list[tuple]:
    """As tarefas `telegram.update` do update, depois de concluídas."""

    def ready() -> list[tuple] | None:
        rows = sql.execute(
            "SELECT task_type, state, idempotency_key FROM tasks WHERE payload = %s::jsonb ORDER BY id",
            (json.dumps({"update_row_id": row_id}),)).fetchall()
        return rows if rows and all(r[1] in ("done", "failed") for r in rows) else None

    return bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _wait_polling(bot_api: Any, bot: int) -> None:
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)


def _redeliver_and_settle(bot_api: Any, bot: int, update_id: int) -> None:
    """Reentrega o update e espera a resposta que o levou e a consulta seguinte (o lote foi gravado
    antes de o `offset` avançar). A reentrega pode sair na consulta longa que já estava
    pendente (iniciada antes da última chamada), por isso a procura é pelas consultas que terminaram
    depois do `redeliver`, e não pelo `seq`."""
    mark = monotonic()
    bot_api.redeliver(bot, update_id)

    def delivered() -> list[Any]:
        return [c for c in bot_api.calls("getUpdates", bot_id=bot)
                if c.ended is not None and c.ended > mark and c.outcome == "ok"
                and any(u.get("update_id") == update_id for u in (c.result or []))]

    carrier = bot_api.wait_for(delivered, timeout=PROCESS_TIMEOUT)[0]
    bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.seq > carrier.seq],
                     timeout=PROCESS_TIMEOUT)


def _refusal_checks(client: Any, bot_api: Any, sql: Any, offering: dict, bot: int, sender: Any,
                    code: str, at: Any, *, cursor: int, out_cursor: int) -> None:
    """`/start <code>` no privado de `sender`, recusado como inválido (a primeira regra do vínculo):
    `team_refused_invalid`, `LINK_INVALID` ao chat do update (`reply_to_update_id`, sem pessoa nem
    endereço) e o evento `team.join_refused {invalid}`, nada mais (o remetente não recebeu outra
    mensagem antes)."""
    uid = bot_api.push_start(bot, sender, code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code}", "team_refused_invalid", at)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, sender) == [LINK_INVALID]
    assert _events_after(client, cursor) == [_refused(offering, at, "invalid")]


# ---------------------------------------------------------------------------------------------
# Vínculo feito


def test_valid_start_links_the_member_and_welcomes_exactly_once(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any, bot_worker_thread: Any,
                                                               settings: Any) -> None:
    """Vínculo válido (convite pendente, pessoa sem endereço): o
    `/start <código>` válido deixa o convite `used`, a matrícula `active`, grava o endereço `active`
    (chat do remetente) e enfileira `WELCOME_TEAM` ao endereço (pessoa + bot, sem update), numa só
    transação com o processamento (`team_joined`); a boas-vindas chega ao remetente pelo
    `sendMessage` só com `chat_id` e `text`, em até 5 s da chegada do `/start`, uma única vez, mesmo
    com o update reentregue (uma linha, uma tarefa `telegram.update`, uma mensagem); evento
    `team.joined {endpoint_replaced: false}`."""
    offering, bot = _offering(client, bot_api, "mc102-vinculo")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Professora Vinculo", ra="5500000201")
    t_link = clock.now()
    code, _ = _link(client, offering, person)
    clock.advance(120)
    at = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_polling(bot_api, bot)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    sender = make_user(201)

    pushed_at = monotonic()
    uid = bot_api.push_start(bot, sender, code)
    sent = bot_api.wait_for(lambda: bot_api.sent_messages(bot, chat_id=sender.user_id), timeout=TEAM_JOIN_DEADLINE_SECONDS)
    send_call = next(c for c in bot_api.calls("sendMessage", bot_id=bot) if c.seq == sent[0].call_seq)
    assert send_call.started - pushed_at <= TEAM_JOIN_DEADLINE_SECONDS

    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code}", "team_joined", at)
    assert _invite(sql, code) == [("used", t_link + timedelta(hours=DEFAULT_HOURS), t_link, at, None)]
    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "active", t_link, at)]
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", at, at)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME]
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ({"chat_id": sender.user_id, "text": WELCOME}, 200)]
    assert _events_after(client, cursor) == [_joined(offering, person, at, False)]
    assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}")]

    _redeliver_and_settle(bot_api, bot, uid)
    assert [r[0] for r in _updates(sql, bot, uid)] == [row[0]]
    assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}")]
    assert _outbox_ids(sql, out_cursor) == [rows[0][0]]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME]
    assert _events_after(client, cursor) == [_joined(offering, person, at, False)]
    assert channel.calls == []


def test_rerun_of_the_task_after_team_joined_changes_nothing(client: Any, clock: Any, sql: Any,
                                                             bot_api: Any, bot_worker_thread: Any,
                                                             settings: Any, queue: Any) -> None:
    """Cada update processado uma única vez; tarefas idempotentes e
    retomáveis (a fila retoma a tarefa cuja posse vence durante o tratamento); a tarefa
    `telegram.update` com `{"update_row_id": <id>}` e o processamento (o tratador trava a linha e, se
    ela já está `processed`, nada faz): depois do `/start <código>` válido (`team_joined`,
    boas-vindas entregue), uma nova tarefa `telegram.update` com o mesmo payload e sem chave,
    enfileirada pela fila com o relógio 15 s adiante, termina `done` na 1ª tentativa e sem
    efeito: a linha do update continua `team_joined` com o `processed_at` original (não vira
    `team_already_linked`); convite `used`, matrícula `active` e endereço intactos; nenhuma mensagem
    nova na fila (nem `ALREADY_LINKED`), nenhum `sendMessage` novo e nenhum evento, de tipo nenhum.
    Controle positivo: um `/ajuda` seguinte do mesmo chat é processado (`help`) e respondido com o
    `HELP` ao chat do update (a fila e a entrega seguiram rodando depois da tarefa repetida)."""
    offering, bot = _offering(client, bot_api, "mc102-tarefa-repetida")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Tarefa Repetida", ra="5500000217")
    at = clock.now()
    code, _ = _link(client, offering, person)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_polling(bot_api, bot)
    out_cursor = _last_outbox(sql)
    sender = make_user(229)
    pseudo = person["pseudo_id"]

    uid = bot_api.push_start(bot, sender, code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code}", "team_joined", at)
    welcome = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in welcome] == [(WELCOME, "sent", pseudo, bot, sender.user_id, None)]
    assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}")]
    invite = [("used", at + timedelta(hours=DEFAULT_HOURS), at, at, None)]
    enrollments = [(pseudo, offering["offering_id"], "team", "active", at, at)]
    endpoints = [(pseudo, bot, "telegram", sender.user_id, "active", at, at)]
    assert _invite(sql, code) == invite
    assert _enrollments(sql) == enrollments
    assert _endpoints(sql) == endpoints
    sends = [({"chat_id": sender.user_id, "text": WELCOME}, 200)]
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == sends
    # O perfil (e o `bot.profile_updated` dele) fica pronto antes do cursor dos eventos.
    bot_api.wait_for(lambda: sql.execute("SELECT profile_state FROM bots WHERE telegram_bot_id = %s",
                                         (bot,)).fetchone()[0] == "configured", timeout=PROCESS_TIMEOUT)
    cursor = _last_seq(sql)

    clock.advance(STEP)
    t1 = clock.now()
    task_id = queue.enqueue("telegram.update", {"update_row_id": row[0]})
    record = bot_api.wait_for(lambda: (lambda r: r if r.state in ("done", "failed") else None)(
        queue.get(task_id)), timeout=PROCESS_TIMEOUT)
    assert record == TaskRecord(id=task_id, task_type="telegram.update", state="done", attempts=1,
                                available_at=t1, lease_owner=None, lease_until=None, last_error=None,
                                finished_at=t1)
    assert _updates(sql, bot, uid) == [row]
    assert _invite(sql, code) == invite
    assert _enrollments(sql) == enrollments
    assert _endpoints(sql) == endpoints
    assert _outbox_after(sql, out_cursor) == welcome
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == sends
    assert _all_events_after(client, cursor) == []

    help_uid = bot_api.push_text(bot, sender, "/ajuda", bot_command=True)
    help_row = _processed(bot_api, sql, bot, help_uid)
    assert help_row[1:] == _private_start(sender, "/ajuda", "help", t1, command="/ajuda")
    replies = _settled_outbox(bot_api, sql, welcome[0][0], 1)
    assert [r[1:] for r in replies] == [(HELP, "sent", None, bot, None, help_uid)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME, HELP]
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        *sends, ({"chat_id": sender.user_id, "text": HELP}, 200)]
    assert _updates(sql, bot, uid) == [row]
    assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}"),
                                                   ("telegram.update", "done", None)]
    assert _invite(sql, code) == invite
    assert _enrollments(sql) == enrollments
    assert _endpoints(sql) == endpoints
    assert _all_events_after(client, cursor) == []
    assert channel.calls == []


def test_link_created_before_activation_works_after_activation(client: Any, clock: Any, sql: Any,
                                                              bot_api: Any, bot_worker_thread: Any,
                                                              settings: Any) -> None:
    """Link antes da ativação: o link gerado no rascunho e o `/start`
    mandado antes da ativação (pendente no Telegram) só são tratados depois da ativação, e o vínculo
    se completa (`team_joined`, boas-vindas)."""
    offering, bot = _offering(client, bot_api, "mc102-antes", activate=False)
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Link Rascunho", ra="5500000202")
    code, _ = _link(client, offering, person)
    sender = make_user(202)
    uid = bot_api.push_start(bot, sender, code)
    clock.advance(60)
    at = clock.now()
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    out_cursor = _last_outbox(sql)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)

    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code}", "team_joined", at)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME]
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", at, at)]
    assert channel.calls == []


def test_start_with_the_bot_username_in_another_case_is_recognized(client: Any, clock: Any, sql: Any,
                                                                  bot_api: Any, bot_worker_thread: Any,
                                                                  settings: Any) -> None:
    """Processamento (`/start@<username do bot>` sem diferenciar maiúsculas no username): o comando
    com o username em outra caixa vincula (`team_joined`), e o `command` gravado é o literal."""
    offering, bot = _offering(client, bot_api, "mc102-caixa", "cq_Equipe_Caixa_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Caixa Vinculo", ra="5500000203")
    code, _ = _link(client, offering, person)
    at = clock.now()
    out_cursor = _last_outbox(sql)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(203)

    uid = bot_api.push_start(bot, sender, code, username="CQ_EQUIPE_CAIXA_BOT")
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start@CQ_EQUIPE_CAIXA_BOT {code}", "team_joined", at,
                                     command="/start@CQ_EQUIPE_CAIXA_BOT")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME]
    assert _invite(sql, code)[0][0] == "used"
    assert channel.calls == []


def test_start_with_extra_spaces_around_the_code_links(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                      bot_worker_thread: Any, settings: Any) -> None:
    """Processamento (o código é o texto depois do primeiro espaço que segue o comando, sem espaços
    nas pontas): `/start   <código>  ` (três espaços antes do código e dois depois,
    com a entidade `bot_command` só sobre o `/start`) vincula (`team_joined`), com o texto literal
    gravado e o `command` `/start`; convite `used`, matrícula `active`, endereço `active`,
    `WELCOME_TEAM` ao endereço e `team.joined {endpoint_replaced: false}`."""
    offering, bot = _offering(client, bot_api, "mc102-espacos")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Espacos Vinculo", ra="5500000218")
    at = clock.now()
    code, _ = _link(client, offering, person)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    _wait_polling(bot_api, bot)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    sender = make_user(230)

    text = f"/start   {code}  "
    uid = bot_api.push_text(bot, sender, text, bot_command=True)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, text, "team_joined", at)
    assert _invite(sql, code) == [("used", at + timedelta(hours=DEFAULT_HOURS), at, at, None)]
    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "active", at, at)]
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", at, at)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME]
    assert _events_after(client, cursor) == [_joined(offering, person, at, False)]
    assert channel.calls == []


def test_same_account_with_a_new_link_keeps_its_endpoint(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                        bot_worker_thread: Any, settings: Any) -> None:
    """A mesma conta usando de novo o seu próprio endereço vigente com um convite
    pendente novo: `team_joined` sem trocar o endereço (mesma linha, `changed_at` intacto), convite
    novo `used`, matrícula intacta, boas-vindas de novo ao endereço e `team.joined
    {endpoint_replaced: false}`."""
    offering, bot = _offering(client, bot_api, "mc102-mesma-conta")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Mesma Conta", ra="5500000204")
    code1, _ = _link(client, offering, person)
    t1 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(204)
    _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code1))
    _settled_outbox(bot_api, sql, 0, 1)

    clock.advance(STEP)
    t2 = clock.now()
    code2, _ = _link(client, offering, person)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, sender, code2)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code2}", "team_joined", t2)
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", t1, t1)]
    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "active", t1, t1)]
    assert _invite(sql, code2) == [("used", t2 + timedelta(hours=DEFAULT_HOURS), t2, t2, None)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [WELCOME, WELCOME]
    assert _events_after(client, cursor) == [_joined(offering, person, t2, False)]
    assert channel.calls == []


def test_new_link_used_by_another_account_replaces_the_endpoint(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any, bot_worker_thread: Any,
                                                               settings: Any) -> None:
    """Troca de conta do professor (`team_endpoint_replaced`):
    o link novo usado por outra conta deixa o endereço antigo `inactive` e grava o novo `active`;
    boas-vindas ao endereço novo; `team.joined {endpoint_replaced: true}`; a matrícula continua a
    mesma. Depois, o convite antigo tocado pela conta antiga (que não é mais endereço vigente) é
    "usado por outra conta": `LINK_INVALID` ao chat do update (a pessoa não tem endereço `active`
    nesse chat), `team.join_refused {invalid}`."""
    offering, bot = _offering(client, bot_api, "mc102-troca-conta")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Troca Conta", ra="5500000205")
    code1, _ = _link(client, offering, person)
    t1 = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    old, new = make_user(205), make_user(206)
    _processed(bot_api, sql, bot, bot_api.push_start(bot, old, code1))
    _settled_outbox(bot_api, sql, 0, 1)

    clock.advance(STEP)
    t2 = clock.now()
    code2, _ = _link(client, offering, person)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, new, code2)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(new, f"/start {code2}", "team_endpoint_replaced", t2)
    assert _endpoints(sql) == [
        (person["pseudo_id"], bot, "telegram", old.user_id, "inactive", t1, t2),
        (person["pseudo_id"], bot, "telegram", new.user_id, "active", t2, t2),
    ]
    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "active", t1, t1)]
    assert _invite(sql, code2) == [("used", t2 + timedelta(hours=DEFAULT_HOURS), t2, t2, None)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, new.user_id, None)]
    assert _sent_texts(bot_api, bot, new) == [WELCOME]
    assert _sent_texts(bot_api, bot, old) == [WELCOME]
    assert _events_after(client, cursor) == [_joined(offering, person, t2, True)]

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, old, code1)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(old, f"/start {code1}", "team_refused_invalid", t2)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, old) == [WELCOME, LINK_INVALID]
    assert _events_after(client, cursor) == [_refused(offering, t2, "invalid")]
    assert _endpoints(sql)[1][4] == "active" and _endpoints(sql)[0][4] == "inactive"
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Já vinculado e recusas


def test_used_invite_from_the_linked_account_answers_already_linked_even_after_expiry(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """Convite usado (ordem de avaliação: um convite usado nunca é tratado como
    vencido): o mesmo código tocado de novo pela conta vinculada → `team_already_linked`,
    `ALREADY_LINKED` ao endereço (pessoa + bot), sem mudança nem evento; depois do vencimento do
    convite, o mesmo resultado."""
    offering, bot = _offering(client, bot_api, "mc102-ja-vinculado")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Ja Vinculada", ra="5500000206")
    t0 = clock.now()
    code, _ = _link(client, offering, person, valid_hours=1)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(207)
    _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code))
    _settled_outbox(bot_api, sql, 0, 1)
    invite = [("used", t0 + timedelta(hours=1), t0, t0, None)]
    endpoints = [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", t0, t0)]
    enrollments = [(person["pseudo_id"], offering["offering_id"], "team", "active", t0, t0)]
    assert _invite(sql, code) == invite

    expected_texts = [WELCOME]
    for label, jump in (("antes do vencimento", 0), ("depois do vencimento", 2 * 3600)):
        clock.advance(jump)
        at = clock.now()
        cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
        uid = bot_api.push_start(bot, sender, code)
        row = _processed(bot_api, sql, bot, uid)
        assert row[1:] == _private_start(sender, f"/start {code}", "team_already_linked", at), label
        rows = _settled_outbox(bot_api, sql, out_cursor, 1)
        assert [r[1:] for r in rows] == [(ALREADY_LINKED, "sent", person["pseudo_id"], bot, sender.user_id,
                                          None)], label
        expected_texts.append(ALREADY_LINKED)
        assert _sent_texts(bot_api, bot, sender) == expected_texts, label
        assert _events_after(client, cursor) == [], label
        assert _invite(sql, code) == invite, label
        assert _endpoints(sql) == endpoints, label
        assert _enrollments(sql) == enrollments, label
    assert channel.calls == []


def test_used_invite_from_another_account_is_refused(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                    bot_worker_thread: Any, settings: Any) -> None:
    """Convite usado por outra conta: `team_refused_invalid`,
    `LINK_INVALID` ao chat do update de quem tentou (sem pessoa nem endereço na mensagem),
    `team.join_refused {invalid}`; o endereço da pessoa continua o da primeira conta."""
    offering, bot = _offering(client, bot_api, "mc102-usado-outra")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Convite Usado", ra="5500000207")
    code, _ = _link(client, offering, person)
    at = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    first, intruder = make_user(208), make_user(209)
    _processed(bot_api, sql, bot, bot_api.push_start(bot, first, code))
    _settled_outbox(bot_api, sql, 0, 1)
    endpoints = [(person["pseudo_id"], bot, "telegram", first.user_id, "active", at, at)]

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _refusal_checks(client, bot_api, sql, offering, bot, intruder, code, at,
                    cursor=cursor, out_cursor=out_cursor)
    assert _endpoints(sql) == endpoints
    assert _invite(sql, code) == [("used", at + timedelta(hours=DEFAULT_HOURS), at, at, None)]
    assert _sent_texts(bot_api, bot, first) == [WELCOME]
    assert channel.calls == []


def test_unknown_or_malformed_code_is_refused_without_other_change(client: Any, clock: Any, sql: Any,
                                                                  bot_api: Any, bot_worker_thread: Any,
                                                                  settings: Any) -> None:
    """Código inválido (a primeira regra do vínculo; o código é o texto depois do espaço): código
    desconhecido, com 23 caracteres (o código válido com um a mais) e fora do alfabeto base64url →
    `team_refused_invalid`, `LINK_INVALID` ao chat do update e `team.join_refused {invalid}`, a mesma
    mensagem em todos os casos; o convite real continua `pending`, sem matrícula ativa nem endereço.
    Controle positivo: o código real, depois, vincula."""
    offering, bot = _offering(client, bot_api, "mc102-desconhecido")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Codigo Desconhecido", ra="5500000208")
    code, _ = _link(client, offering, person)
    at = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    pending = [("pending", at + timedelta(hours=DEFAULT_HOURS), at, None, None)]
    enrollment = [(person["pseudo_id"], offering["offering_id"], "team", "pending", at, None)]

    bad_codes = {
        "desconhecido": stdlib_secrets.token_urlsafe(16),
        "23 caracteres": code + "A",
        "fora do alfabeto (=)": code[:21] + "=",
        "fora do alfabeto (+)": "+" + code[1:],
    }
    for index, (label, bad) in enumerate(bad_codes.items()):
        assert bad != code
        sender = make_user(210 + index)
        cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
        try:
            _refusal_checks(client, bot_api, sql, offering, bot, sender, bad, at,
                            cursor=cursor, out_cursor=out_cursor)
            assert _invite(sql, code) == pending
            assert _enrollments(sql) == enrollment
            assert _endpoints(sql) == []
        except AssertionError as exc:
            raise AssertionError(f"[{label}] {exc}") from exc

    sender = make_user(219)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code))
    assert row[1:] == _private_start(sender, f"/start {code}", "team_joined", at)
    assert channel.calls == []


def test_expired_invite_is_refused_at_processing_time_with_strict_boundary(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """Vencimento (avaliado no processamento, pelo relógio do sistema);
    o convite vale se e só se `agora < expires_at`: processado no instante exato do
    vencimento, o `/start` é recusado (`LINK_INVALID`, `team.join_refused {invalid}`), mesmo com a
    data da mensagem anterior ao vencimento, e o convite continua `pending` (o vencido é calculado);
    um convite que vence 1 s depois do mesmo instante vincula."""
    offering, bot = _offering(client, bot_api, "mc102-vencido")
    expired_person = register_person(client, ADMIN_TOKEN, name="Pessoa Convite Vencido", ra="5500000209")
    valid_person = register_person(client, ADMIN_TOKEN, name="Pessoa Convite Valido", ra="5500000210")
    t0 = clock.now()
    expired_code, _ = _link(client, offering, expired_person, valid_hours=1)
    clock.advance(1)
    valid_code, _ = _link(client, offering, valid_person, valid_hours=1)
    clock.set(t0 + timedelta(hours=1))
    at = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)

    late = make_user(220)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, late, expired_code, date=t0 + timedelta(minutes=30))
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(late, f"/start {expired_code}", "team_refused_invalid", at)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, late) == [LINK_INVALID]
    assert _events_after(client, cursor) == [_refused(offering, at, "invalid")]
    assert _invite(sql, expired_code) == [("pending", at, t0, None, None)]
    assert _endpoints(sql) == []

    on_time = make_user(221)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, on_time, valid_code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(on_time, f"/start {valid_code}", "team_joined", at)
    assert _invite(sql, valid_code) == [("used", at + timedelta(seconds=1), t0 + timedelta(seconds=1), at, None)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", valid_person["pseudo_id"], bot, on_time.user_id, None)]
    assert _events_after(client, cursor) == [_joined(offering, valid_person, at, False)]
    assert channel.calls == []


def test_revoked_invite_is_refused_and_only_the_new_one_works(client: Any, clock: Any, sql: Any,
                                                             bot_api: Any, bot_worker_thread: Any,
                                                             settings: Any) -> None:
    """depois de gerar outro link para a mesma pessoa, o código anterior (revogado) é
    recusado (`LINK_INVALID`, `team.join_refused {invalid}`) e continua `revoked`; o novo vincula."""
    offering, bot = _offering(client, bot_api, "mc102-revogado")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Convite Revogado", ra="5500000211")
    t0 = clock.now()
    old_code, _ = _link(client, offering, person)
    clock.advance(60)
    t1 = clock.now()
    new_code, _ = _link(client, offering, person)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(222)

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _refusal_checks(client, bot_api, sql, offering, bot, sender, old_code, t1,
                    cursor=cursor, out_cursor=out_cursor)
    assert _invite(sql, old_code) == [("revoked", t0 + timedelta(hours=DEFAULT_HOURS), t0, None, t1)]
    assert _invite(sql, new_code) == [("pending", t1 + timedelta(hours=DEFAULT_HOURS), t1, None, None)]
    assert _endpoints(sql) == []

    out_cursor = _last_outbox(sql)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, new_code))
    assert row[1:] == _private_start(sender, f"/start {new_code}", "team_joined", t1)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", person["pseudo_id"], bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [LINK_INVALID, WELCOME]
    assert channel.calls == []


def test_invite_of_another_offering_is_refused(client: Any, clock: Any, sql: Any, bot_api: Any,
                                              bot_worker_thread: Any, settings: Any) -> None:
    """Convite de outro oferecimento: o código de um link de outro
    oferecimento, mandado a este bot, é recusado como inválido; o convite do outro oferecimento
    continua `pending` e ninguém é matriculado ativo."""
    offering, bot = _offering(client, bot_api, "mc102-este")
    other, _ = _offering(client, bot_api, "mc102-outro", "cq_outro_vinculo_bot", activate=False)
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Outro Oferecimento", ra="5500000212")
    at = clock.now()
    code, _ = _link(client, other, person)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(223)

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _refusal_checks(client, bot_api, sql, offering, bot, sender, code, at,
                    cursor=cursor, out_cursor=out_cursor)
    assert _invite(sql, code) == [("pending", at + timedelta(hours=DEFAULT_HOURS), at, None, None)]
    assert _enrollments(sql) == [(person["pseudo_id"], other["offering_id"], "team", "pending", at, None)]
    assert _endpoints(sql) == []
    assert channel.calls == []


def test_account_linked_to_another_person_is_refused_and_the_invite_revoked(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """Conta do Telegram já vinculada a outra pessoa
    (`team_refused_other_person`): a conta que já é o endereço vigente de outra pessoa no bot recebe
    `OTHER_PERSON` no chat do update (não no endereço da pessoa do convite), o convite fica `revoked`
    e saem `team.link_revoked {refused}` e `team.join_refused {other_person}`, nessa ordem; os
    endereços não mudam; o código revogado não serve mais a ninguém."""
    offering, bot = _offering(client, bot_api, "mc102-outra-pessoa")
    first = register_person(client, ADMIN_TOKEN, name="Pessoa Primeira Conta", ra="5500000213")
    second = register_person(client, ADMIN_TOKEN, name="Pessoa Segunda Conta", ra="5500000214")
    at = clock.now()
    code_first, _ = _link(client, offering, first)
    code_second, _ = _link(client, offering, second)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    account_first, account_second = make_user(224), make_user(225)
    _processed(bot_api, sql, bot, bot_api.push_start(bot, account_first, code_first))
    _processed(bot_api, sql, bot, bot_api.push_start(bot, account_second, code_second))
    _settled_outbox(bot_api, sql, 0, 2)
    endpoints = [
        (first["pseudo_id"], bot, "telegram", account_first.user_id, "active", at, at),
        (second["pseudo_id"], bot, "telegram", account_second.user_id, "active", at, at),
    ]
    assert _endpoints(sql) == endpoints

    clock.advance(STEP)
    t1 = clock.now()
    new_code, _ = _link(client, offering, second)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, account_first, new_code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(account_first, f"/start {new_code}", "team_refused_other_person", t1)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(OTHER_PERSON, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, account_first) == [WELCOME, OTHER_PERSON]
    assert _sent_texts(bot_api, bot, account_second) == [WELCOME]
    assert _invite(sql, new_code) == [("revoked", t1 + timedelta(hours=DEFAULT_HOURS), t1, None, t1)]
    assert _events_after(client, cursor) == [_revoked(offering, second, t1, "refused"),
                                             _refused(offering, t1, "other_person")]
    assert _endpoints(sql) == endpoints

    stranger = make_user(226)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _refusal_checks(client, bot_api, sql, offering, bot, stranger, new_code, t1,
                    cursor=cursor, out_cursor=out_cursor)
    assert _endpoints(sql) == endpoints
    assert channel.calls == []


@pytest.mark.parametrize("where", ["grupo", "outro-bot"])
def test_pending_code_in_a_group_or_for_another_bot_is_revoked_without_reply(
        where: str, client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any,
        settings: Any) -> None:
    """Código exposto (verificado antes de tudo no processamento): `/start <código pendente>` num grupo, ou com
    `/start@<outro bot>` no privado, fica `not_handled`, sem resposta (nenhuma mensagem na fila), e o
    convite é revogado (`team.link_revoked {refused}`, e nenhum `team.join_refused`), para que nenhum
    código utilizável fique no texto gravado; depois, o mesmo código no privado é `LINK_INVALID`."""
    offering, bot = _offering(client, bot_api, f"mc102-{where}")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Codigo Exposto", ra="5500000215")
    t0 = clock.now()
    code, _ = _link(client, offering, person)
    clock.advance(STEP)
    at = clock.now()
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(227)

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    if where == "grupo":
        uid = bot_api.push_start(bot, sender, code, chat=GROUP_CHAT)
        expected = ("message", GROUP_CHAT["id"], "group", sender.user_id, f"/start {code}", "/start",
                    "processed", "not_handled", at)
    else:
        uid = bot_api.push_start(bot, sender, code, username=OTHER_BOT_USERNAME)
        expected = ("message", sender.user_id, "private", sender.user_id,
                    f"/start@{OTHER_BOT_USERNAME} {code}", f"/start@{OTHER_BOT_USERNAME}", "processed",
                    "not_handled", at)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == expected
    assert _outbox_after(sql, out_cursor) == []
    assert _invite(sql, code) == [("revoked", t0 + timedelta(hours=DEFAULT_HOURS), t0, None, at)]
    assert _events_after(client, cursor) == [_revoked(offering, person, at, "refused")]
    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "pending", t0, None)]
    assert _endpoints(sql) == []

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _refusal_checks(client, bot_api, sql, offering, bot, sender, code, at,
                    cursor=cursor, out_cursor=out_cursor)
    assert bot_api.sent_messages(bot, chat_id=GROUP_CHAT["id"]) == []
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Endereço bloqueado


def test_blocked_endpoint_is_reactivated_by_a_valid_start_from_its_chat(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any) -> None:
    """Reativação (um `/start` válido do chat de um endereço `blocked` da mesma
    pessoa o volta a `active`, com `changed_at = agora` e sem evento novo, antes de enfileirar a
    resposta), em `team_already_linked` e em `team_joined` (o bloqueio vem do 403 do
    `sendMessage`: mensagem `failed`, endereço `blocked`, `outbox.failed` e `endpoint.blocked`)."""
    offering, bot = _offering(client, bot_api, "mc102-bloqueio")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Endereco Bloqueado", ra="5500000216")
    t0 = clock.now()
    code1, _ = _link(client, offering, person)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel)
    sender = make_user(228)
    pseudo = person["pseudo_id"]

    def blocked_events(message_id: int, at: Any) -> list[dict]:
        return [_event("outbox.failed", at, message_id=message_id, recipient=pseudo, attempts=1,
                       error="chat_unreachable"),
                _event("endpoint.blocked", at, offering_id=offering["offering_id"], person=pseudo,
                       channel="telegram")]

    # 1) Boas-vindas a um chat que bloqueou o bot: o endereço nasce e fica `blocked`.
    bot_api.block_chat(bot, sender)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, sender, code1)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private_start(sender, f"/start {code1}", "team_joined", t0)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "failed", pseudo, bot, sender.user_id, None)]
    assert _endpoints(sql) == [(pseudo, bot, "telegram", sender.user_id, "blocked", t0, t0)]
    assert _events_after(client, cursor) == [_joined(offering, person, t0, False), *blocked_events(rows[0][0], t0)]
    assert _sent_texts(bot_api, bot, sender) == []

    # 2) Desbloqueado, o `/start` do convite já usado reativa o endereço (`team_already_linked`).
    clock.advance(STEP)
    t1 = clock.now()
    bot_api.unblock_chat(bot, sender)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code1))
    assert row[1:] == _private_start(sender, f"/start {code1}", "team_already_linked", t1)
    assert _endpoints(sql) == [(pseudo, bot, "telegram", sender.user_id, "active", t0, t1)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(ALREADY_LINKED, "sent", pseudo, bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [ALREADY_LINKED]
    assert _events_after(client, cursor) == []

    # 3) Bloqueado de novo pelo envio seguinte.
    clock.advance(STEP)
    t2 = clock.now()
    bot_api.block_chat(bot, sender)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code1))
    assert row[1:] == _private_start(sender, f"/start {code1}", "team_already_linked", t2)
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(ALREADY_LINKED, "failed", pseudo, bot, sender.user_id, None)]
    assert _endpoints(sql) == [(pseudo, bot, "telegram", sender.user_id, "blocked", t0, t2)]
    assert _events_after(client, cursor) == blocked_events(rows[0][0], t2)

    # 4) Desbloqueado, um convite novo usado pelo mesmo chat reativa o endereço (`team_joined`).
    clock.advance(STEP)
    t3 = clock.now()
    bot_api.unblock_chat(bot, sender)
    code2, _ = _link(client, offering, person)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code2))
    assert row[1:] == _private_start(sender, f"/start {code2}", "team_joined", t3)
    assert _endpoints(sql) == [(pseudo, bot, "telegram", sender.user_id, "active", t0, t3)]
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME, "sent", pseudo, bot, sender.user_id, None)]
    assert _sent_texts(bot_api, bot, sender) == [ALREADY_LINKED, WELCOME]
    assert _events_after(client, cursor) == [_joined(offering, person, t3, False)]
    assert _enrollments(sql) == [(pseudo, offering["offering_id"], "team", "active", t0, t0)]
    assert channel.calls == []
