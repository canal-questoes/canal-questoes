"""Arquivamento com convites e envios de aluno, e código de aluno exposto.

Cobre o arquivamento com convites e envios pendentes e o código de aluno exposto.

Contratos: o arquivamento (os convites `pending` revogados em ordem de `invites.id`, os
de aluno com `student.invite_revoked {archived}` e os de equipe com `team.link_revoked {archived}`; os
envios `pending` não mudam, e a primeira transação do envio que começa com o oferecimento fora de `active` os cancela
`offering_inactive`; o `pending_links` do resumo conta os dois papéis; envio em voo; códigos expostos com
`revoke_reason: refused`), o envio do convite (as duas transações do envio), o `/start` (fora do chat privado ou para outro
bot segue a revogação por exposição; com o convite revogado, `/start` → `LINK_INVALID`,
`student_refused_invalid`, sem evento) e os eventos; a injeção de falhas nos testes (o
`ScriptedVerificationChannel` com o gancho `on_send`).

Tudo passa pelo caminho real: a API importa, prepara, confirma, encerra e arquiva; um worker com bots
(`bot_worker_thread`, `FakeClock` do teste, `worker_id` fixo) consulta a Bot API falsa e roda as
tarefas `invite.email` com o adaptador real sobre o SMTP falso. As esperas são com prazo (tempo real),
nunca `sleep` como prova; horários pelo `FakeClock` (`iso`); ids internos, `seq` e códigos nunca como
literal. Os eventos são lidos pela API (`events.list`, esquema congelado) depois de um cursor, por
inteiro e na ordem de `seq`. Os registros de log são conferidos por inclusão do subconjunto exigido
(como nos registros de log da API base), com contagem e ordem exatas. O e-mail capturado é comparado por igualdade no
envelope, nos cabeçalhos (`Date` e `Message-ID` por forma) e no texto decodificado (o corpo do
e-mail do convite seguido da quebra de linha final que o DATA exige).
"""

from __future__ import annotations

import dataclasses
import email.utils
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from cq_server.ports.verification import Delivered, VerificationMessage
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    import_students,
    list_events,
    prepare_invites,
    register_bot,
    register_person,
    resend_invites,
    send_invites,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.channels import ScriptedVerificationChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import Captured, capture_logs
from tests.helpers.secrets import invite_code_digest
from tests.helpers.smtp import SENDER

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"

WORKER = "worker-arquivo-alunos"
PROCESS_TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
STEP = 10  # salto do relógio com o worker no ar (menor que a posse de 30 s)
DEFAULT_HOURS = 168
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
GROUP_CHAT = {"id": -4001234599, "type": "group", "title": "Turma de Teste do Arquivamento"}
OTHER_BOT_USERNAME = "outro_bot_da_turma"

# Texto fixo de oferecimentos e bots, que o vínculo do aluno não muda.
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."

# remetente e assunto do e-mail do convite.
FROM_HEADER = f"Canal de Questões <{SENDER}>"
SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
EMAIL_HEADER_NAMES = ("from", "to", "subject", "date", "message-id", "mime-version", "content-type",
                      "content-transfer-encoding")

# Todos os tipos de evento: os da API base, os de oferecimentos e bots e os 8 do vínculo do aluno.
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
STUDENT_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                       "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                       "student.invite_revoked", "student.joined")
ALL_EVENT_TYPES = CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_EVENT_TYPES
# Sem o `bot.profile_updated`: a tarefa do perfil arquivado roda no worker em paralelo aos passos
# conferidos, e os testes de oferecimentos e bots já a cobrem (test_offerings_lifecycle.py).
ARCHIVE_EVENT_TYPES = tuple(t for t in ALL_EVENT_TYPES if t != "bot.profile_updated")
EXPOSURE_EVENT_TYPES = ("team.link_created", "team.link_revoked", "team.joined", "team.join_refused",
                        "endpoint.blocked", "outbox.failed", "outbox.reply_failed") + STUDENT_EVENT_TYPES


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Offering:
    created: dict  # o corpo de `offerings.create`
    bot: int  # o `telegram_bot_id` na Bot API falsa
    username: str

    @property
    def ref(self) -> str:
        return self.created["code"]

    @property
    def summary(self) -> dict:
        """O `OfferingRef` das respostas do vínculo do aluno."""
        return {"offering_id": self.created["offering_id"], "code": self.created["code"]}

    def body(self, *, state: str, activated_at: datetime | None = None, closed_at: datetime | None = None,
             archived_at: datetime | None = None, profile_state: str = "configured") -> dict:
        """O `Offering` esperado, com o resumo do bot."""
        return {**self.created, "state": state,
                "activated_at": None if activated_at is None else iso(activated_at),
                "closed_at": None if closed_at is None else iso(closed_at),
                "archived_at": None if archived_at is None else iso(archived_at),
                "bot": {"username": self.username, "telegram_bot_id": self.bot, "state": "registered",
                        "profile_state": profile_state}}


def _row(name: str, ra: str, email_address: str) -> dict[str, str]:
    return {"name": name, "ra": ra, "email": email_address}


def _active_offering(client: Any, bot_api: Any, code: str, username: str,
                     before_activation: Callable[[str], None] | None = None) -> _Offering:
    """Oferecimento ativo com um bot novo da Bot API falsa; `before_activation(ref)` roda entre o
    registro do bot e a ativação (ex.: gerar um link de equipe)."""
    created = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    if before_activation is not None:
        before_activation(code)
    activate_bot(client, ADMIN_TOKEN, code)
    return _Offering(created, bot, username)


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _get_offering(client: Any, ref: str) -> dict:
    return _ok(client.get(f"/v1/offerings/{ref}", headers=auth(ADMIN_TOKEN)), "offerings.get")


def _close(client: Any, ref: str) -> dict:
    return _ok(client.post(f"/v1/offerings/{ref}/close", headers=auth(ADMIN_TOKEN)), "offerings.close")


def _prepare_archive(client: Any, ref: str) -> dict:
    body = _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=auth(ADMIN_TOKEN)),
               "offerings.prepare_archive")
    assert CONFIRMATION_RE.fullmatch(body["confirmation_code"]), body["confirmation_code"]
    return body


def _archive(client: Any, ref: str, code: str) -> dict:
    return _ok(client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": code},
                           headers=auth(ADMIN_TOKEN)), "offerings.archive")


def _import(client: Any, offering: _Offering, rows: list[dict[str, str]]) -> None:
    """Importa `rows` (pessoas novas), com a resposta exata."""
    n = len(rows)
    assert import_students(client, ADMIN_TOKEN, offering.ref, rows) == {
        "offering": offering.summary, "received": n, "people_created": n, "people_reused": 0,
        "enrollments_created": n, "already_enrolled": 0}


def _prepare_and_confirm(client: Any, clock: Any, offering: _Offering, eligible: int) -> None:
    """Prepara e confirma o lote, com as respostas exatas."""
    now = clock.now()
    prepared = prepare_invites(client, ADMIN_TOKEN, offering.ref)
    code = prepared["confirmation_code"]
    assert CONFIRMATION_RE.fullmatch(code), code
    assert prepared == {"offering": offering.summary, "eligible": eligible, "valid_hours": DEFAULT_HOURS,
                        "confirmation_code": code, "expires_at": iso(now + CONFIRMATION)}
    assert send_invites(client, ADMIN_TOKEN, offering.ref, code) == {
        "offering": offering.summary, "queued": eligible, "valid_hours": DEFAULT_HOURS}


def _worker(bot_worker_thread: Any, settings: Any, **kwargs: Any) -> Any:
    return bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER), **kwargs)


def _wait(predicate: Callable[[], Any], message: str, timeout: float = PROCESS_TIMEOUT) -> Any:
    """Espera real (threads do worker), sempre com prazo; devolve o valor verdadeiro."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


def _wait_ready(client: Any, bot_api: Any, offering: _Offering) -> None:
    """O worker consulta o bot (posse assumida) e já configurou o perfil."""
    bot_api.wait_for(lambda: bot_api.calls("getUpdates", bot_id=offering.bot), timeout=PROCESS_TIMEOUT)
    bot_api.wait_for(lambda: _get_offering(client, offering.ref)["bot"]["profile_state"] == "configured",
                     timeout=PROCESS_TIMEOUT)


def _pseudo(sql: psycopg.Connection, ra: str) -> str:
    """O pseudônimo da pessoa do RA (leitura da área restrita, só no teste)."""
    return sql.execute("SELECT p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id "
                       "WHERE i.ra = %s", (ra,)).fetchone()[0]


SEND_COLUMNS = ("id", "person", "origin", "valid_hours", "state", "failure", "cancel_reason", "error_type",
                "attempts", "created_at", "finished_at")


def _sends(sql: psycopg.Connection, offering: _Offering) -> list[dict]:
    """As linhas de `invite_sends` do oferecimento, em ordem de `id`."""
    rows = sql.execute(
        """
        SELECT s.id, p.pseudo_id::text, s.origin, s.valid_hours, s.state, s.failure, s.cancel_reason,
               s.error_type, s.attempts, s.created_at, s.finished_at
          FROM invite_sends s
          JOIN enrollments e ON e.id = s.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s
         ORDER BY s.id
        """, (uuid.UUID(offering.created["offering_id"]),)).fetchall()
    return [dict(zip(SEND_COLUMNS, row)) for row in rows]


def _send(row: dict, *, state: str, attempts: int, finished_at: datetime | None,
          cancel_reason: str | None = None) -> dict:
    """O envio esperado a partir da linha lida (`id`, pessoa, origem e criação vêm dela)."""
    return {**row, "valid_hours": DEFAULT_HOURS, "state": state, "failure": None, "cancel_reason": cancel_reason,
            "error_type": None, "attempts": attempts, "finished_at": finished_at}


INVITE_COLUMNS = ("id", "role", "person", "invite_send_id", "code_sha256", "state", "expires_at", "created_at",
                  "used_at", "revoked_at")


def _invites(sql: psycopg.Connection, offering: _Offering) -> list[dict]:
    """Os convites do oferecimento (equipe e aluno), em ordem de `id`."""
    rows = sql.execute(
        """
        SELECT i.id, e.role, p.pseudo_id::text, i.invite_send_id, i.code_sha256, i.state, i.expires_at,
               i.created_at, i.used_at, i.revoked_at
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s
         ORDER BY i.id
        """, (uuid.UUID(offering.created["offering_id"]),)).fetchall()
    return [dict(zip(INVITE_COLUMNS, row)) for row in rows]


def _invite(row: dict, *, state: str, created_at: datetime, revoked_at: datetime | None = None) -> dict:
    """O convite esperado a partir da linha lida (`id`, papel, pessoa, envio e resumo vêm dela)."""
    return {**row, "state": state, "expires_at": created_at + timedelta(hours=DEFAULT_HOURS),
            "created_at": created_at, "used_at": None, "revoked_at": revoked_at}


def _invite_tasks(sql: psycopg.Connection) -> list[tuple]:
    """(payload, estado, tentativas, `last_error`) das tarefas `invite.email`, em ordem de `id`."""
    return [tuple(row) for row in sql.execute(
        "SELECT payload, state, attempts, last_error FROM tasks WHERE task_type = 'invite.email' ORDER BY id")]


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int, types: tuple[str, ...]) -> list[dict]:
    """Os eventos dos tipos dados depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: datetime, data: dict, actor: str | None = None) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _revoked_event(offering: _Offering, invite: dict, at: datetime, reason: str, actor: str | None) -> dict:
    """O evento de revogação do convite, pelo papel: `team.link_revoked` ou
    `student.invite_revoked` (com a origem do envio que criou o convite; todos os de aluno aqui vêm
    do lote)."""
    data = {"offering_id": offering.created["offering_id"], "person": invite["person"]}
    if invite["role"] == "team":
        return _event("team.link_revoked", at, {**data, "revoke_reason": reason}, actor)
    return _event("student.invite_revoked", at, {**data, "origin": "batch", "revoke_reason": reason}, actor)


def _invite_email_records(cap: Captured) -> list[tuple[Any, Any]]:
    """Os registros `invite.email`, em ordem, como (`invite_send_id`, `result`); cada um,
    sem o `time`, é exatamente `{level: INFO, logger: cq_server.worker, event, invite_send_id, result}`
    (nenhuma chave a mais: nada de `error_type` nem texto do servidor)."""
    records = cap.with_event("invite.email")
    for record in records:
        rest = {key: value for key, value in record.items() if key != "time"}
        expected = {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email",
                    "invite_send_id": record.get("invite_send_id"), "result": record.get("result")}
        assert rest == expected, record
    return [(r.get("invite_send_id"), r.get("result")) for r in records]


def _validity(moment: datetime) -> str:
    """A validade no fuso do oferecimento, calculada aqui, fora do código testado."""
    local = moment.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def _invite_body(link: str, validity: str) -> str:
    """O corpo do e-mail do convite, por extenso."""
    return "\n".join((
        "Olá!",
        "",
        f"Você está na lista da turma do oferecimento {COURSE} ({TERM}). As questões da disciplina chegam "
        "por um bot do Telegram, e este é o seu link pessoal para começar:",
        "",
        link,
        "",
        "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot "
        "vai mostrar o termo de uso, que você pode aceitar ou recusar.",
        "",
        f"O link vale até {validity}. Não o repasse: ele é só seu.",
        "",
        "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
        "e-mail institucional (o mesmo da lista da turma) para receber um novo link.",
        "",
        "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.",
    ))


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_email(message: Any, *, address: str, text: str, not_before: datetime, not_after: datetime) -> None:
    """Um e-mail de convite capturado pelo SMTP falso: envelope, cabeçalhos (`Date` em
    UTC entre `not_before` e `not_after`, tempo real, e `Message-ID` com o domínio do remetente, por
    forma) e texto decodificado exatos; nada acima de 127 no bruto."""
    assert message.mail_from == SENDER
    assert message.rcpt_to == (address,)
    assert message.acknowledged is True
    assert max(message.raw) <= 0x7F
    headers = {name.lower(): value for name, value in message.headers}
    assert len(headers) == len(message.headers), f"cabeçalho repetido: {message.header_names()}"
    assert sorted(headers) == sorted(EMAIL_HEADER_NAMES), message.header_names()
    sent_at = email.utils.parsedate_to_datetime(headers.pop("date"))
    assert sent_at.tzinfo is not None and sent_at.utcoffset() == timedelta(0), sent_at
    assert not_before.replace(microsecond=0) <= sent_at <= not_after, (not_before, sent_at, not_after)
    message_id = headers.pop("message-id")
    assert MESSAGE_ID_RE.fullmatch(message_id), message_id
    assert headers == {"from": FROM_HEADER, "to": address, "subject": SUBJECT, "mime-version": "1.0",
                       "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    # O DATA sempre termina em CRLF, que o texto decodificado mostra como a quebra de linha final.
    assert message.text == text + "\n"


def _updates(sql: psycopg.Connection, bot: int, update_id: int) -> list[tuple]:
    """(id, kind, chat_id, chat_type, sender_id, text, command, state, outcome, processed_at)."""
    return sql.execute(
        """
        SELECT u.id, u.kind, u.chat_id, u.chat_type, u.sender_id, u.text, u.command, u.state,
               u.outcome, u.processed_at
          FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
         WHERE b.telegram_bot_id = %s AND u.update_id = %s
         ORDER BY u.id
        """, (bot, update_id)).fetchall()


def _processed(bot_api: Any, sql: psycopg.Connection, bot: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update (tipo fora do allowed_updates)"

    def ready() -> list[tuple] | None:
        rows = _updates(sql, bot, update_id)
        return rows if rows and all(row[7] == "processed" for row in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _outbox_after(sql: psycopg.Connection, after: int) -> list[tuple]:
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


def _last_outbox(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _polls_after(bot_api: Any, bot: int, mark: float) -> list[Any]:
    return [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.started > mark]


# ---------------------------------------------------------------------------------------------
# Arquivamento com convites dos dois papéis e envios na fila
# ---------------------------------------------------------------------------------------------


def test_archive_revokes_student_and_team_invites_and_cancels_queued_sends(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any) -> None:
    """Arquivamento (os convites pendentes ficam revogados, com evento, e os envios da fila que ainda não
    começaram não saem: são cancelados, com evento); o arquivamento e
    o envio do convite (primeira transação: oferecimento fora de `active` → `cancelled`/`offering_inactive`, com
    `student.invite_cancelled`, sem gerar código); num oferecimento com dois links de equipe
    pendentes, dois convites de aluno já enviados (lote) e dois envios na fila com o worker parado (o
    lote de um aluno novo e um reenvio da coordenação), o resumo do preparo do arquivamento conta os
    quatro convites válidos dos dois papéis (`pending_links = 4`); a confirmação revoga os
    quatro, em ordem de `invites.id`, com `team.link_revoked {archived}` para os de equipe e
    `student.invite_revoked {archived}` (origem `batch`) para os de aluno, e o `offering.state_changed`
    por último, sem mexer nos envios; ao subir o worker de novo, os dois envios da fila são cancelados
    (`offering_inactive`, `attempts = 0`, registros `invite.email` `cancelled`), sem nenhum convite
    novo nem e-mail: nenhum envio cuja primeira transação comece depois do arquivamento sai; os únicos e-mails são os
    dois do lote, exatos, com os códigos dos convites revogados."""
    team_one = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Arquivo Um", ra="5600000201")
    team_two = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Arquivo Dois", ra="5600000202")
    t0 = clock.now()
    offering = _active_offering(
        client, bot_api, "arquivo-alunos", "cq_arquivo_alunos_bot",
        before_activation=lambda ref: create_team_link(client, ADMIN_TOKEN, ref, team_one["alias"]))
    first_worker = _worker(bot_worker_thread, settings, verification=mail_channel)
    _wait_ready(client, bot_api, offering)
    rows = [_row("Pessoa Aluna Arquivo Um", "5600000211", "aluna.arquivo.um@turma.example.org"),
            _row("Pessoa Aluna Arquivo Dois", "5600000212", "aluna.arquivo.dois@turma.example.org")]
    _import(client, offering, rows)
    not_before = _real_now()
    _prepare_and_confirm(client, clock, offering, eligible=2)
    _wait(lambda: [s["state"] for s in _sends(sql, offering)] == ["sent", "sent"] and len(smtp.messages()) == 2,
          "os dois convites do lote não saíram")
    not_after = _real_now()
    person_one, person_two = _pseudo(sql, "5600000211"), _pseudo(sql, "5600000212")
    address_of = {person_one: rows[0]["email"], person_two: rows[1]["email"]}

    clock.advance(STEP)
    t1 = clock.now()
    create_team_link(client, ADMIN_TOKEN, offering.ref, team_two["alias"])
    assert bot_worker_thread.stop_one(first_worker) < 10

    # Envios na fila, com o worker parado: o lote de uma aluna nova e um reenvio da coordenação.
    _import(client, offering, [_row("Pessoa Aluna Arquivo Tres", "5600000213",
                                    "aluna.arquivo.tres@turma.example.org")])
    _prepare_and_confirm(client, clock, offering, eligible=1)
    person_three = _pseudo(sql, "5600000213")
    assert resend_invites(client, ADMIN_TOKEN, offering.ref, [person_one]) == {
        "offering": offering.summary, "queued": 1, "valid_hours": DEFAULT_HOURS, "not_students": [],
        "not_found_count": 0}
    batch_a, batch_b, queued_batch, queued_resend = _sends(sql, offering)
    assert {batch_a["person"], batch_b["person"]} == {person_one, person_two}
    assert (queued_batch["person"], queued_batch["origin"], queued_batch["state"]) == (person_three, "batch",
                                                                                       "pending")
    assert (queued_resend["person"], queued_resend["origin"], queued_resend["state"]) == (person_one,
                                                                                          "coordination",
                                                                                          "pending")

    clock.advance(STEP)
    t2 = clock.now()
    closed = _close(client, offering.ref)
    assert closed == offering.body(state="closed", activated_at=t0, closed_at=t2)
    pending = _invites(sql, offering)
    assert [(i["role"], i["state"]) for i in pending] == [("team", "pending"), ("student", "pending"),
                                                          ("student", "pending"), ("team", "pending")]
    assert [pending[0]["person"], pending[3]["person"]] == [team_one["pseudo_id"], team_two["pseudo_id"]]
    assert [i["invite_send_id"] for i in pending] == [None, batch_a["id"], batch_b["id"], None]
    assert [i["person"] for i in pending[1:3]] == [batch_a["person"], batch_b["person"]]
    summary = _prepare_archive(client, offering.ref)
    assert summary == {"offering": closed, "team_members_active": 0, "pending_links": 4,
                       "confirmation_code": summary["confirmation_code"], "expires_at": iso(t2 + CONFIRMATION)}
    cursor = _last_seq(sql)

    archived = _archive(client, offering.ref, summary["confirmation_code"])
    assert archived == offering.body(state="archived", activated_at=t0, closed_at=t2, archived_at=t2)
    assert _events_after(client, cursor, ALL_EVENT_TYPES) == [
        *(_revoked_event(offering, invite, t2, "archived", "bootstrap") for invite in pending),
        _event("offering.state_changed", t2, {"offering_id": offering.created["offering_id"],
                                              "from_state": "closed", "to_state": "archived"}, "bootstrap"),
    ]
    assert _invites(sql, offering) == [
        _invite(pending[0], state="revoked", created_at=t0, revoked_at=t2),
        _invite(pending[1], state="revoked", created_at=t0, revoked_at=t2),
        _invite(pending[2], state="revoked", created_at=t0, revoked_at=t2),
        _invite(pending[3], state="revoked", created_at=t1, revoked_at=t2),
    ]
    # Os envios da fila não mudam no arquivamento.
    assert _sends(sql, offering) == [batch_a, batch_b, queued_batch, queued_resend]

    clock.advance(STEP)
    t3 = clock.now()
    cursor = _last_seq(sql)
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=mail_channel)
        _wait(lambda: [s["state"] for s in _sends(sql, offering)][2:] == ["cancelled", "cancelled"]
              and len(_invite_email_records(cap)) == 2, "os envios da fila não foram cancelados")
        # O tratador registra o `invite.email` antes de concluir a tarefa: espera nenhuma tarefa em posse antes de ler.
        _wait(lambda: all(row[1] != "leased" for row in _invite_tasks(sql)), "as tarefas invite.email não terminaram")

    assert _sends(sql, offering) == [
        _send(batch_a, state="sent", attempts=1, finished_at=t0),
        _send(batch_b, state="sent", attempts=1, finished_at=t0),
        _send(queued_batch, state="cancelled", attempts=0, finished_at=t3, cancel_reason="offering_inactive"),
        _send(queued_resend, state="cancelled", attempts=0, finished_at=t3, cancel_reason="offering_inactive"),
    ]
    assert (queued_batch["created_at"], queued_resend["created_at"]) == (t1, t1)
    assert [i["state"] for i in _invites(sql, offering)] == ["revoked"] * 4
    assert _invite_tasks(sql) == [({"invite_send_id": s["id"]}, "done", 1, None)
                                  for s in (batch_a, batch_b, queued_batch, queued_resend)]
    oid = offering.created["offering_id"]
    assert _events_after(client, cursor, STUDENT_EVENT_TYPES + ("task.failed",)) == [
        _event("student.invite_cancelled", t3, {"offering_id": oid, "person": person_three, "origin": "batch",
                                                "cancel_reason": "offering_inactive"}),
        _event("student.invite_cancelled", t3, {"offering_id": oid, "person": person_one,
                                                "origin": "coordination", "cancel_reason": "offering_inactive"}),
    ]
    assert _invite_email_records(cap) == [(queued_batch["id"], "cancelled"), (queued_resend["id"], "cancelled")]
    # Nenhum e-mail novo: só os dois do lote (na ordem dos envios), exatos, com os códigos dos convites
    # que o arquivamento revogou.
    messages, links = smtp.messages(), smtp.invite_links()
    assert [m.rcpt_to for m in messages] == [(address_of[batch_a["person"]],), (address_of[batch_b["person"]],)]
    assert [link.message_seq for link in links] == [m.seq for m in messages]
    for message, link in zip(messages, links):
        assert (link.bot_username, link.rcpt_to, link.acknowledged) == (offering.username, message.rcpt_to, True)
        assert link.url == f"https://t.me/{offering.username}?start={link.code}"
        _assert_email(message, address=message.rcpt_to[0],
                      text=_invite_body(link.url, _validity(t0 + timedelta(hours=DEFAULT_HOURS))),
                      not_before=not_before, not_after=not_after)
    assert [invite_code_digest(link.code) for link in links] == [pending[1]["code_sha256"],
                                                                  pending[2]["code_sha256"]]


# ---------------------------------------------------------------------------------------------
# Código de aluno exposto (colado num grupo ou dirigido a outro bot)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["grupo", "outro-bot"])
def test_exposed_student_code_is_revoked_without_reply(
        where: str, client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any,
        mail_channel: Any, settings: Any, bot_worker_thread: Any) -> None:
    """Código de aluno exposto (um código de aluno colado num grupo ou dirigido a outro bot é
    revogado, com o evento próprio de aluno; o `/start
    <código>` fora do chat privado ou dirigido a outro bot segue a revogação por exposição:
    `not_handled`, sem resposta, e o convite `pending` de aluno revogado com `student.invite_revoked
    {refused}`) e o arquivamento: o código lido do e-mail (exato), colado num grupo ou
    mandado como `/start@<outro bot>` num chat privado, deixa o update `not_handled`, nenhuma mensagem
    na fila, o convite `revoked` e um único `student.invite_revoked {refused}` (origem `batch`), sem
    nenhum evento de equipe; depois, o mesmo código no privado cai no `/start` de aluno com o convite revogado:
    `LINK_INVALID` ao chat do update, `student_refused_invalid`, sem evento; a matrícula continua
    `pending`, sem endereço."""
    offering = _active_offering(client, bot_api, f"exposto-{where}", f"cq_exposto_{where.replace('-', '_')}_bot")
    _worker(bot_worker_thread, settings, verification=mail_channel)
    _wait_ready(client, bot_api, offering)
    t0 = clock.now()
    address = "aluna.codigo.exposto@turma.example.org"
    _import(client, offering, [_row("Pessoa Aluna Codigo Exposto", "5600000221", address)])
    not_before = _real_now()
    _prepare_and_confirm(client, clock, offering, eligible=1)
    [link] = smtp.wait_for(lambda: smtp.invite_links(), timeout=PROCESS_TIMEOUT)
    not_after = _real_now()
    code = link.code
    person = _pseudo(sql, "5600000221")
    _wait(lambda: [s["state"] for s in _sends(sql, offering)] == ["sent"], "o convite não foi registrado")
    [message] = smtp.messages()
    assert (link.message_seq, link.bot_username, link.rcpt_to, link.acknowledged) == (
        message.seq, offering.username, (address,), True)
    assert link.url == f"https://t.me/{offering.username}?start={code}"
    _assert_email(message, address=address,
                  text=_invite_body(link.url, _validity(t0 + timedelta(hours=DEFAULT_HOURS))),
                  not_before=not_before, not_after=not_after)
    [send] = _sends(sql, offering)
    [invite] = _invites(sql, offering)
    assert invite == _invite({**invite, "role": "student", "person": person, "invite_send_id": send["id"]},
                             state="pending", created_at=t0)
    assert invite["code_sha256"] == invite_code_digest(code)

    clock.advance(STEP)
    at = clock.now()
    sender = make_user(521)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    if where == "grupo":
        uid = bot_api.push_start(offering.bot, sender, code, chat=GROUP_CHAT)
        expected = ("message", GROUP_CHAT["id"], "group", sender.user_id, f"/start {code}", "/start", "processed",
                    "not_handled", at)
    else:
        uid = bot_api.push_start(offering.bot, sender, code, username=OTHER_BOT_USERNAME)
        expected = ("message", sender.user_id, "private", sender.user_id, f"/start@{OTHER_BOT_USERNAME} {code}",
                    f"/start@{OTHER_BOT_USERNAME}", "processed", "not_handled", at)
    row = _processed(bot_api, sql, offering.bot, uid)
    assert row[1:] == expected
    assert _outbox_after(sql, out_cursor) == []
    assert _invites(sql, offering) == [_invite(invite, state="revoked", created_at=t0, revoked_at=at)]
    revoked = [_event("student.invite_revoked", at, {"offering_id": offering.created["offering_id"],
                                                     "person": person, "origin": "batch",
                                                     "revoke_reason": "refused"})]
    assert _events_after(client, cursor, EXPOSURE_EVENT_TYPES) == revoked

    # O mesmo código no privado: convite de aluno revogado, sem evento.
    uid = bot_api.push_start(offering.bot, sender, code)
    row = _processed(bot_api, sql, offering.bot, uid)
    assert row[1:] == ("message", sender.user_id, "private", sender.user_id, f"/start {code}", "/start",
                       "processed", "student_refused_invalid", at)
    rows = bot_api.wait_for(lambda: [r for r in _outbox_after(sql, out_cursor) if r[2] in ("sent", "failed")],
                            timeout=PROCESS_TIMEOUT)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, offering.bot, None, uid)]
    assert [m.text for m in bot_api.sent_messages(offering.bot, chat_id=sender.user_id)] == [LINK_INVALID]
    assert bot_api.sent_messages(offering.bot, chat_id=GROUP_CHAT["id"]) == []
    assert _events_after(client, cursor, EXPOSURE_EVENT_TYPES) == revoked
    assert _invites(sql, offering) == [_invite(invite, state="revoked", created_at=t0, revoked_at=at)]
    assert sql.execute(
        "SELECT e.role, e.state, e.activated_at, e.terms_version, e.terms_accepted_at FROM enrollments e "
        "JOIN people p ON p.id = e.person_id WHERE p.pseudo_id = %s",
        (uuid.UUID(person),)).fetchall() == [("student", "pending", None, None, None)]
    assert sql.execute("SELECT count(*) FROM delivery_endpoints").fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------
# Envio em voo durante o arquivamento
# ---------------------------------------------------------------------------------------------


def test_send_in_flight_during_archive_is_sent_with_the_invite_already_revoked(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any) -> None:
    """Envio em voo (um envio já em voo, com o código gerado ainda com o oferecimento ativo, pode sair, com o
    convite já revogado; como o bot de um oferecimento arquivado não é mais consultado, nenhum `/start` é
    processado depois do arquivamento, e o link inválido se confere pelo estado gravado);
    o arquivamento e o envio (segunda transação, sem conferir o oferecimento);
    (montagem pelo gancho `on_send` do `ScriptedVerificationChannel`, que delega ao adaptador
    real): com a primeira transação do envio feita e o oferecimento `active`, o envio para no gancho; o teste encerra, prepara
    (`pending_links = 1`) e confirma o arquivamento, que solta a posse do bot (`bot.lease_changed
    {released, archived}` do worker) e revoga o convite (`student.invite_revoked {archived}`); solto o
    envio, o e-mail exato sai e a segunda transação grava `sent` com `student.invite_sent` (registro `invite.email`
    `sent`), com o convite já `revoked`; o bot arquivado não é mais consultado: um `/start` com o código
    enviado depois fica pendente na Bot API falsa, sem `getUpdates` desse bot depois da 1ª consulta do
    bot de outro oferecimento no mesmo worker (consultado duas vezes), e nada é gravado."""
    offering = _active_offering(client, bot_api, "arquivo-em-voo", "cq_arquivo_em_voo_bot")
    witness = _active_offering(client, bot_api, "arquivo-testemunha", "cq_arquivo_testemunha_bot")
    entered = threading.Event()
    release = threading.Event()

    def hold(message: VerificationMessage) -> None:
        entered.set()
        release.wait(PROCESS_TIMEOUT * 2)

    channel = ScriptedVerificationChannel(delegate=mail_channel, on_send=hold)
    address = "aluna.em.voo@turma.example.org"
    t0 = clock.now()
    try:
        with capture_logs() as cap:
            _worker(bot_worker_thread, settings, verification=channel)
            _wait_ready(client, bot_api, offering)
            _wait_ready(client, bot_api, witness)
            _import(client, offering, [_row("Pessoa Aluna Em Voo", "5600000231", address)])
            _prepare_and_confirm(client, clock, offering, eligible=1)
            person = _pseudo(sql, "5600000231")
            assert entered.wait(PROCESS_TIMEOUT), "o envio não chegou ao canal"

            # A primeira transação do envio já foi feita com o oferecimento ativo; o envio está parado no gancho.
            [send] = _sends(sql, offering)
            assert _send(send, state="pending", attempts=1, finished_at=None) == send
            assert send["origin"] == "batch" and send["person"] == person and send["created_at"] == t0
            [invite] = _invites(sql, offering)
            assert invite == _invite({**invite, "role": "student", "person": person, "invite_send_id": send["id"]},
                                     state="pending", created_at=t0)
            assert smtp.messages() == []

            clock.advance(5)
            t1 = clock.now()
            closed = _close(client, offering.ref)
            assert closed == offering.body(state="closed", activated_at=t0, closed_at=t1)
            summary = _prepare_archive(client, offering.ref)
            assert summary == {"offering": closed, "team_members_active": 0, "pending_links": 1,
                               "confirmation_code": summary["confirmation_code"],
                               "expires_at": iso(t1 + CONFIRMATION)}
            cursor = _last_seq(sql)
            archived = _archive(client, offering.ref, summary["confirmation_code"])
            assert archived == offering.body(state="archived", activated_at=t0, closed_at=t1, archived_at=t1)
            assert _invites(sql, offering) == [_invite(invite, state="revoked", created_at=t0, revoked_at=t1)]

            not_before = _real_now()
            release.set()
            _wait(lambda: _sends(sql, offering)[0]["state"] == "sent" and _invite_email_records(cap),
                  "o envio em voo não foi registrado")
            not_after = _real_now()
    finally:
        release.set()

    oid = offering.created["offering_id"]
    expires_at = t0 + timedelta(hours=DEFAULT_HOURS)
    assert _sends(sql, offering) == [_send(send, state="sent", attempts=1, finished_at=t1)]
    assert _invites(sql, offering) == [_invite(invite, state="revoked", created_at=t0, revoked_at=t1)]
    archive_events = [
        _event("bot.lease_changed", t1, {"offering_id": oid, "worker_id": WORKER, "change": "released",
                                         "reason": "archived"}, "bootstrap"),
        _event("student.invite_revoked", t1, {"offering_id": oid, "person": person, "origin": "batch",
                                              "revoke_reason": "archived"}, "bootstrap"),
        _event("offering.state_changed", t1, {"offering_id": oid, "from_state": "closed",
                                              "to_state": "archived"}, "bootstrap"),
        _event("student.invite_sent", t1, {"offering_id": oid, "person": person, "origin": "batch",
                                           "expires_at": iso(expires_at)}),
    ]
    assert _events_after(client, cursor, ARCHIVE_EVENT_TYPES) == archive_events
    assert _invite_email_records(cap) == [(send["id"], "sent")]

    # O e-mail saiu, com o código do convite que o arquivamento já revogou.
    [message] = smtp.messages()
    [link] = smtp.invite_links()
    assert (link.bot_username, link.rcpt_to, link.acknowledged) == (offering.username, (address,), True)
    assert link.url == f"https://t.me/{offering.username}?start={link.code}"
    assert invite_code_digest(link.code) == invite["code_sha256"]
    body = _invite_body(link.url, _validity(expires_at))
    _assert_email(message, address=address, text=body, not_before=not_before, not_after=not_after)
    assert channel.calls == [VerificationMessage(address=address, subject=SUBJECT, body=body)]
    assert [type(result) for result in channel.delegated] == [Delivered]

    # O bot arquivado não é mais consultado (no molde de test_offerings_lifecycle.py): depois de
    # a consulta pendente dele terminar, nenhum `getUpdates` dele começa depois do fim da 1ª consulta
    # do bot testemunha, que continua; um `/start` com o código fica na Bot API falsa, sem gravação.
    bot_api.wait_for(lambda: not bot_api.pending_polls(offering.bot), timeout=PROCESS_TIMEOUT)
    quiet = monotonic()
    uid = bot_api.push_start(offering.bot, make_user(531), link.code)
    assert uid is not None, "o dublê descartou o update"
    bot_api.wait_for(lambda: len(_polls_after(bot_api, witness.bot, quiet)) >= 2, timeout=PROCESS_TIMEOUT)
    first_witness_poll = _polls_after(bot_api, witness.bot, quiet)[0]
    assert first_witness_poll.ended is not None
    assert [c.seq for c in _polls_after(bot_api, offering.bot, first_witness_poll.ended)] == []
    assert uid in bot_api.state(offering.bot).pending_update_ids
    assert _updates(sql, offering.bot, uid) == []
    assert _invites(sql, offering) == [_invite(invite, state="revoked", created_at=t0, revoked_at=t1)]
    assert _events_after(client, cursor, ARCHIVE_EVENT_TYPES) == archive_events
