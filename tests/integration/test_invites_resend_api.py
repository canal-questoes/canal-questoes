"""Reenvio dos convites pela coordenação.

Cobre o reenvio a alunos vinculados ou não, com as regras do `identity.reveal` da API base para a lista de
pseudônimos e o reenvio não destrutivo e não idempotente. Contratos:
a API do reenvio (`invites.resend`, com a ordem das verificações); a tabela
`invite_sends`; a primeira transação do envio (revoga o convite pendente anterior, com a `origin` do envio que o criou, e
cancela como `superseded` o envio que tem outro mais novo); o reenvio; os eventos; o e-mail
do convite; e as convenções da API base e dos bots (ordem das verificações, entrada estrita, sem eco, horários
em UTC).

Convenções: corpo inteiro por `==` e `assert_matches` (pelos atalhos de
`tests/helpers/api.py`) em toda resposta de sucesso; erros por `assert_error`; horários pelo
`FakeClock` e `iso(dt)`; ids internos, `seq`, códigos e pseudônimos nunca como literal (o pseudônimo e
o apelido de cada pessoa são lidos da resposta do cadastro ou do banco, pelo RA da lista). O worker
roda em thread (`bot_worker_thread` com `verification=` sobre o SMTP falso) só onde o e-mail precisa
sair; os saltos do relógio com o worker no ar são de 15 s (menos que a posse de 30 s). Todo e-mail
capturado é conferido por inteiro (envelope, cabeçalhos, `Date` e `Message-ID` por forma, texto
decodificado).

No esqueleto, as rotas `POST /v1/offerings/{ref}/students`, `…/invites/resend` e as do lote não
existem e a migração `0003` não foi aplicada: cada função falha no primeiro pedido do vínculo do aluno (404 no lugar
do 200 ou do 400/409 esperado; rota ausente) ou, nas que tiram o instantâneo do banco antes do pedido,
na contagem de `invite_sends` (`UndefinedTable`; tabela ausente). As asserções sobre o
que o esqueleto já tem (401 sem token; 404 de oferecimento inexistente) ficam na mesma função de um caso
que falha.
"""

from __future__ import annotations

import dataclasses
import email.utils
import hashlib
import itertools
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    create_token,
    import_students,
    list_events,
    prepare_invites,
    register_bot,
    register_person,
    resend_invites,
    send_invites,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.smtp import SENDER as SMTP_SENDER

ADMIN = auth(ADMIN_TOKEN)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"
DEFAULT_HOURS = 168
PROCESS_TIMEOUT = 15.0
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)

# Pseudônimos de formato válido que nunca pertencem a alguém (o apelido real tem outro tamanho).
MISSING_ALIAS = "ffff0000ffff"
MISSING_PSEUDO_ID = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"

# Tipos de evento do vínculo do aluno, como literais (o esqueleto ainda não os tem).
STUDENT_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                       "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                       "student.invite_revoked", "student.joined")

# E-mail do convite, por extenso.
SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
EMAIL_BODY = (
    "Olá!\n"
    "\n"
    f"Você está na lista da turma do oferecimento {COURSE} ({TERM}). As questões da disciplina chegam por "
    "um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot vai "
    "mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu e-mail "
    "institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.\n")
EMAIL_HEADER_NAMES = ("content-transfer-encoding", "content-type", "date", "from", "message-id",
                      "mime-version", "subject", "to")
MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
UTC_DATE_RE = re.compile(r".+ (?:\+0000|-0000|GMT|UT)")


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot, então nada pode chegar
    aqui (conferido no fim dos testes com worker)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


@dataclasses.dataclass(frozen=True)
class _Student:
    """Uma linha neutra da lista (fora dos alvos canário)."""

    name: str
    ra: str
    email: str

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


_counter = itertools.count(1)


def _student(label: str) -> _Student:
    """Aluno neutro com RA de 10 dígitos e e-mail válido pelas regras da API base (o rótulo, sem espaços,
    entra na parte local)."""
    n = next(_counter)
    return _Student(name=f"Aluno Reenvio {label}", ra=f"664{n:07d}",
                    email=f"aluno.reenvio.{label.lower().replace(' ', '.')}.{n}@turma.example.org")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ref(offering: dict) -> dict:
    """O `OfferingRef` das respostas do vínculo do aluno."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _offering(client: Any, bot_api: Any, code: str, username: str | None = None, *,
              activate: bool = True) -> tuple[dict, int]:
    """Oferecimento (disciplina, período e fuso padrão) com um bot novo da Bot API falsa registrado e,
    salvo `activate=False`, ativado; devolve o corpo do cadastro e o id do bot no Telegram."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=TZ)
    bot = bot_api.add_bot(username) if username is not None else bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    if activate:
        activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _import(client: Any, offering: dict, students: list[_Student]) -> None:
    """Importa alunos novos (`students.import`) e confere a resposta inteira (só contagens)."""
    n = len(students)
    body = import_students(client, ADMIN_TOKEN, offering["code"], [s.row() for s in students])
    assert body == {"offering": _ref(offering), "received": n, "people_created": n, "people_reused": 0,
                    "enrollments_created": n, "already_enrolled": 0}


def _person(sql: Any, student: _Student) -> dict[str, str]:
    """`{pseudo_id, alias}` da pessoa criada pela importação, achada pelo RA (leitura de teste)."""
    row = sql.execute("SELECT p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (student.ra,)).fetchone()
    assert row is not None, "aluno da lista sem pessoa depois da importação"
    return {"pseudo_id": row[0], "alias": row[1]}


def _canonical(person: dict) -> dict[str, str]:
    """O item de `not_students`: o pseudônimo e o apelido canônicos."""
    return {"pseudo_id": person["pseudo_id"], "alias": person["alias"]}


def _student_enrollments(sql: Any, offering: dict) -> list[tuple[int, str]]:
    """(id interno, pseudo_id) das matrículas de aluno do oferecimento, em ordem de `enrollments.id`."""
    return sql.execute(
        """
        SELECT e.id, p.pseudo_id::text
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s AND e.role = 'student'
         ORDER BY e.id
        """, (uuid.UUID(offering["offering_id"]),)).fetchall()


def _enrollments(sql: Any) -> list[tuple]:
    """(pseudo_id, offering_id, role, state, terms_version) de todas as matrículas, em ordem de id."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, e.role, e.state, e.terms_version
          FROM enrollments e JOIN people p ON p.id = e.person_id JOIN offerings o ON o.id = e.offering_id
         ORDER BY e.id
        """).fetchall()


def _sends(sql: Any, offering: dict) -> list[tuple]:
    """(id, enrollment_id, pseudo_id, origin, valid_hours, state, failure, cancel_reason, error_type,
    attempts, created_at, finished_at) dos envios do oferecimento, em ordem de `invite_sends.id`."""
    return sql.execute(
        """
        SELECT s.id, s.enrollment_id, p.pseudo_id::text, s.origin, s.valid_hours, s.state, s.failure,
               s.cancel_reason, s.error_type, s.attempts, s.created_at, s.finished_at
          FROM invite_sends s
          JOIN enrollments e ON e.id = s.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s
         ORDER BY s.id
        """, (uuid.UUID(offering["offering_id"]),)).fetchall()


def _send_row(send_id: int, enrollment_id: int, pseudo: str, valid_hours: int, at: datetime, *,
              state: str = "pending", cancel_reason: str | None = None, attempts: int = 0,
              finished_at: datetime | None = None) -> tuple:
    """A linha de `invite_sends` de um envio da coordenação."""
    return (send_id, enrollment_id, pseudo, "coordination", valid_hours, state, None, cancel_reason, None,
            attempts, at, finished_at)


def _tasks(sql: Any, send_ids: list[int]) -> list[tuple]:
    """(payload, idempotency_key, state, attempts, available_at, lease_owner, lease_until, last_error,
    created_at, finished_at) das tarefas `invite.email` dos envios dados, em ordem de `tasks.id`."""
    return sql.execute(
        """
        SELECT payload, idempotency_key, state, attempts, available_at, lease_owner, lease_until,
               last_error, created_at, finished_at
          FROM tasks
         WHERE task_type = 'invite.email' AND (payload ->> 'invite_send_id')::bigint = ANY(%s)
         ORDER BY id
        """, (send_ids,)).fetchall()


def _pending_task_row(send_id: int, at: datetime) -> tuple:
    """A tarefa `invite.email` recém-enfileirada do envio (payload só com o id)."""
    return ({"invite_send_id": send_id}, f"invite.email:{send_id}", "pending", 0, at, None, None, None, at, None)


def _invites(sql: Any, offering: dict) -> list[tuple]:
    """(pseudo_id, code_sha256, state, expires_at, created_at, used_at, revoked_at, origin do envio) dos
    convites de aluno do oferecimento, em ordem de `invites.id`."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, i.code_sha256, i.state, i.expires_at, i.created_at, i.used_at,
               i.revoked_at, s.origin
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
          JOIN invite_sends s ON s.id = i.invite_send_id
         WHERE o.offering_id = %s AND e.role = 'student'
         ORDER BY i.id
        """, (uuid.UUID(offering["offering_id"]),)).fetchall()


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _student_events(client: Any, cursor: int) -> list[dict]:
    """Os eventos do vínculo do aluno depois de `cursor`, pela API, em ordem de `seq` (crescente), sem o `seq`."""
    items = list_events(client, ADMIN_TOKEN, *STUDENT_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: datetime, data: dict, actor: str | None = None) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _queued(offering: dict, at: datetime, queued: int, actor: str | None = "bootstrap") -> dict:
    return _event("student.invites_queued", at,
                  {"offering_id": offering["offering_id"], "origin": "coordination", "queued": queued}, actor)


def _sent(offering: dict, pseudo: str, at: datetime, expires_at: datetime, origin: str = "coordination") -> dict:
    return _event("student.invite_sent", at, {"offering_id": offering["offering_id"], "person": pseudo,
                                              "origin": origin, "expires_at": iso(expires_at)})


def _revoked(offering: dict, pseudo: str, at: datetime, origin: str, reason: str = "replaced") -> dict:
    return _event("student.invite_revoked", at, {"offering_id": offering["offering_id"], "person": pseudo,
                                                 "origin": origin, "revoke_reason": reason})


def _cancelled(offering: dict, pseudo: str, at: datetime, reason: str, origin: str = "coordination") -> dict:
    return _event("student.invite_cancelled", at, {"offering_id": offering["offering_id"], "person": pseudo,
                                                   "origin": origin, "cancel_reason": reason})


def _resend_body(offering: dict, queued: int, *, valid_hours: int = DEFAULT_HOURS,
                 not_students: list[dict] | None = None, not_found_count: int = 0) -> dict:
    """A resposta 200 exata de `invites.resend`."""
    return {"offering": _ref(offering), "queued": queued, "valid_hours": valid_hours,
            "not_students": not_students or [], "not_found_count": not_found_count}


def _post_resend(client: Any, ref: str, body: Any, *, token: str | None = ADMIN_TOKEN) -> Any:
    return client.post(f"/v1/offerings/{ref}/invites/resend", json=body, headers=auth(token))


def _ok(resp: Any, op_id: str) -> dict:
    """200 com o corpo validado pelo esquema congelado da operação."""
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _close(client: Any, ref: str) -> None:
    assert _ok(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")["state"] == "closed"


def _archive(client: Any, ref: str) -> None:
    prepared = _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN), "offerings.prepare_archive")
    body = _ok(client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": prepared["confirmation_code"]},
                           headers=ADMIN), "offerings.archive")
    assert body["state"] == "archived"


def _unchanged(sql: Any) -> dict:
    """O que um pedido recusado não pode mudar: envios, tarefas de envio e eventos."""
    return {"sends": sql.execute("SELECT count(*) FROM invite_sends").fetchone()[0],
            "tasks": sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'invite.email'").fetchone()[0],
            "events": sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall()}


def _validity(moment: datetime, zone: str = TZ) -> str:
    """A validade no fuso: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = moment.astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({zone})"


def _assert_invite_email(message: Any, *, address: str, username: str, code: str, expires_at: datetime,
                         not_before: datetime) -> None:
    """Uma mensagem capturada pelo SMTP falso, conferida por inteiro (o e-mail do convite):
    envelope, cabeçalhos exatos (o `Date` em UTC entre o início do teste e agora, em tempo real, e o
    `Message-ID` com o domínio do remetente, só por forma) e texto decodificado exato."""
    assert (message.mail_from, message.rcpt_to, message.acknowledged) == (SMTP_SENDER, (address,), True)
    assert sorted(name.lower() for name, _ in message.headers) == sorted(EMAIL_HEADER_NAMES)
    headers = {name.lower(): value for name, value in message.headers}
    date_value = headers.pop("date")
    message_id = headers.pop("message-id")
    assert headers == {"from": f"Canal de Questões <{SMTP_SENDER}>", "to": address, "subject": SUBJECT,
                       "mime-version": "1.0", "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    assert UTC_DATE_RE.fullmatch(date_value), f"Date fora de UTC: {date_value!r}"
    sent = email.utils.parsedate_to_datetime(date_value).replace(tzinfo=timezone.utc)
    assert not_before.replace(microsecond=0) <= sent <= datetime.now(timezone.utc)
    assert MESSAGE_ID_RE.fullmatch(message_id), f"Message-ID fora da forma: {message_id!r}"
    link = f"https://t.me/{username}?start={code}"
    assert message.text == EMAIL_BODY.replace("{link}", link).replace("{validity}", _validity(expires_at))


def _emails(smtp: Any, username: str, expected: list[tuple[str, datetime]], not_before: datetime) -> list[str]:
    """Confere, na ordem de chegada, todas as mensagens capturadas contra `expected` (endereço e
    vencimento do convite) e devolve os códigos, na mesma ordem (os códigos entram em
    `remember_secret` pelo `invite_links()`)."""
    messages = smtp.messages()
    links = smtp.invite_links()
    assert [m.rcpt_to for m in messages] == [(address,) for address, _ in expected]
    assert [link.message_seq for link in links] == [m.seq for m in messages], "um link por mensagem"
    for message, link, (address, expires_at) in zip(messages, links, expected):
        _assert_invite_email(message, address=address, username=username, code=link.code,
                             expires_at=expires_at, not_before=not_before)
    return [link.code for link in links]


def _worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel, name: str, verification: Any) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=name),
                             verification=verification)


def _wait(bot_api: Any, predicate: Any) -> Any:
    return bot_api.wait_for(predicate, timeout=PROCESS_TIMEOUT)


def _all_settled(sql: Any, offering: dict, count: int) -> bool:
    """Os `count` envios do oferecimento existem e nenhum está `pending`."""
    sends = _sends(sql, offering)
    return len(sends) == count and all(row[5] != "pending" for row in sends)


def _outcome(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> str:
    """Espera o update gravado e processado e devolve o `outcome` da única linha dele."""
    assert update_id is not None, "o dublê descartou o update"

    def ready() -> list[tuple] | None:
        rows = sql.execute(
            "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (bot, update_id)).fetchall()
        return rows if rows and all(r[0] == "processed" for r in rows) else None

    rows = _wait(bot_api, ready)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0][1]


def _outbox_settled(bot_api: Any, sql: Any) -> None:
    _wait(bot_api, lambda: sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0] == 0)


# ---------------------------------------------------------------------------------------------
# Reenvio a alunos vinculados e não vinculados
# ---------------------------------------------------------------------------------------------


def test_resend_to_unlinked_and_linked_students_sends_and_revokes(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, stop_one: Any,
        settings: Any, smtp: Any, mail_channel: Any) -> None:
    """Reenvio a alunos vinculados ou não: um envio por aluno distinto, vinculado ou não, em ordem de matrícula;
    `not_students` com o pseudônimo e o apelido canônicos, na ordem da primeira referência, sem
    repetição; só a contagem dos itens distintos sem pessoa, nunca ecoados; validade contada
    do envio; `student.invites_queued {origin: coordination}` com o ator; regras do reenvio; a
    primeira transação do envio revoga o convite pendente anterior, com a `origin` do envio que o criou.

    Montagem: B e C recebem o convite do lote (entregue); B apresenta o termo e aceita (vinculado); A é
    importado depois, sem convite; P é uma pessoa sem matrícula, Q é da equipe do oferecimento e R é
    aluno de outro oferecimento. O pedido cita P pelo apelido, A pelo apelido e pelo identificador, um
    apelido sem pessoa (duas vezes), B pelo identificador e pelo apelido, Q e P pelo identificador, C
    pelo apelido, R pelo apelido e um identificador sem pessoa → 200 `{queued: 3, valid_hours: 168,
    not_students: [P, Q, R], not_found_count: 2}`; um `invite_sends` `coordination`/`pending` e uma
    tarefa por aluno distinto (A, B e C), em ordem de `enrollments.id`, e nada para P, Q e R. Com o
    worker: cada aluno recebe um e-mail (conferido por inteiro) com o link do convite novo; o convite do
    lote de C, ainda pendente, é revogado (`student.invite_revoked {replaced, origin: batch}`), o de B
    continua usado (o vínculo e o endereço de B não mudam até um novo aceite); os eventos saem na ordem
    dos envios."""
    not_before = datetime.now(timezone.utc)
    username = "cq_reenvio_coordenacao_bot"
    offering, bot = _offering(client, bot_api, "reenvio-coordenacao", username)
    other = create_offering(client, ADMIN_TOKEN, code="reenvio-outro", course=COURSE, term=TERM, timezone=TZ)
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Reenvio Sem Matricula", ra="6649000001",
                        email="pessoa.sem.matricula@turma.example.org")
    q = register_person(client, ADMIN_TOKEN, name="Pessoa Reenvio Equipe", ra="6649000002")
    create_team_link(client, ADMIN_TOKEN, offering["code"], q["alias"])
    a, b, c, r = _student("Sem Convite"), _student("Vinculado"), _student("Do Lote"), _student("Outro")
    t0 = clock.now()
    channel = _DefaultChannel()

    _import(client, offering, [b, c])
    prepared = prepare_invites(client, ADMIN_TOKEN, offering["code"])
    assert prepared == {"offering": _ref(offering), "eligible": 2, "valid_hours": DEFAULT_HOURS,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(t0 + timedelta(minutes=10))}
    assert send_invites(client, ADMIN_TOKEN, offering["code"], prepared["confirmation_code"]) == {
        "offering": _ref(offering), "queued": 2, "valid_hours": DEFAULT_HOURS}
    batch = [row[2] for row in _sends(sql, offering)]
    worker = _worker(bot_worker_thread, settings, channel, "worker-reenvio-1", mail_channel)
    _wait(bot_api, lambda: _all_settled(sql, offering, 2))
    address = {_person(sql, s)["pseudo_id"]: s.email for s in (b, c)}
    smtp.wait_for(lambda: len(smtp.messages()) >= 2, timeout=PROCESS_TIMEOUT)
    batch_codes = _emails(smtp, username, [(address[pid], t0 + timedelta(hours=DEFAULT_HOURS)) for pid in batch],
                          not_before)
    b_code = batch_codes[batch.index(_person(sql, b)["pseudo_id"])]
    b_user = make_user(4401)
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, b_user, b_code)) == "student_terms_presented"
    assert _outcome(bot_api, sql, bot, bot_api.push_text(bot, b_user, "/aceito", bot_command=True)) == "student_joined"
    _outbox_settled(bot_api, sql)
    assert stop_one(worker) != float("inf")

    _import(client, offering, [a])
    _import(client, other, [r])
    pa, pb, pc, pr = (_person(sql, s) for s in (a, b, c, r))
    address.update({pa["pseudo_id"]: a.email})
    enrollments_before = _enrollments(sql)
    endpoints_before = sql.execute("SELECT person_id, bot_id, chat_id, state, changed_at FROM delivery_endpoints "
                                   "ORDER BY id").fetchall()
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    clock.advance(60)
    t1 = clock.now()
    cursor = _last_seq(sql)

    pseudonyms = [p["alias"], pa["alias"], MISSING_ALIAS, pb["pseudo_id"], pa["pseudo_id"], q["pseudo_id"],
                  p["pseudo_id"], pc["alias"], MISSING_ALIAS, pr["alias"], MISSING_PSEUDO_ID, pb["alias"]]
    body = resend_invites(client, writer, offering["code"], pseudonyms)
    assert body == _resend_body(offering, 3, not_students=[_canonical(p), _canonical(q), _canonical(pr)],
                                not_found_count=2)

    enrollments = _student_enrollments(sql, offering)
    assert sorted(pid for _, pid in enrollments) == sorted([pa["pseudo_id"], pb["pseudo_id"], pc["pseudo_id"]])
    new_sends = _sends(sql, offering)[2:]
    assert new_sends == [_send_row(row[0], eid, pid, DEFAULT_HOURS, t1)
                         for row, (eid, pid) in zip(new_sends, enrollments)]
    assert len(new_sends) == 3
    send_ids = [row[0] for row in new_sends]
    assert _tasks(sql, send_ids) == [_pending_task_row(sid, t1) for sid in send_ids]
    assert _sends(sql, other) == []
    assert _enrollments(sql) == enrollments_before
    assert _student_events(client, cursor) == [_queued(offering, t1, 3, writer_body["token_id"])]

    worker = _worker(bot_worker_thread, settings, channel, "worker-reenvio-2", mail_channel)
    _wait(bot_api, lambda: _all_settled(sql, offering, 5))
    order = [pid for _, pid in enrollments]
    expires = t1 + timedelta(hours=DEFAULT_HOURS)
    smtp.wait_for(lambda: len(smtp.messages()) >= 5, timeout=PROCESS_TIMEOUT)
    codes = _emails(smtp, username, [(address[pid], t0 + timedelta(hours=DEFAULT_HOURS)) for pid in batch]
                    + [(address[pid], expires) for pid in order], not_before)[2:]
    assert _sends(sql, offering)[2:] == [
        _send_row(row[0], eid, pid, DEFAULT_HOURS, t1, state="sent", attempts=1, finished_at=t1)
        for row, (eid, pid) in zip(new_sends, enrollments)]

    expected_events = [_queued(offering, t1, 3, writer_body["token_id"])]
    for pid in order:
        if pid == pc["pseudo_id"]:
            expected_events.append(_revoked(offering, pid, t1, "batch"))
        expected_events.append(_sent(offering, pid, t1, expires))
    assert _student_events(client, cursor) == expected_events

    invites = _invites(sql, offering)
    batch_rows = {row[0]: row for row in invites[:2]}
    assert [row[0] for row in invites[:2]] == batch
    assert batch_rows[pb["pseudo_id"]][2:] == ("used", t0 + timedelta(hours=DEFAULT_HOURS), t0, t0, None, "batch")
    assert batch_rows[pc["pseudo_id"]][2:] == ("revoked", t0 + timedelta(hours=DEFAULT_HOURS), t0, None, t1, "batch")
    assert invites[2:] == [(pid, _sha256(code), "pending", expires, t1, None, None, "coordination")
                           for pid, code in zip(order, codes)]
    assert _enrollments(sql) == enrollments_before
    assert sql.execute("SELECT person_id, bot_id, chat_id, state, changed_at FROM delivery_endpoints "
                       "ORDER BY id").fetchall() == endpoints_before
    assert stop_one(worker) != float("inf")
    assert channel.calls == []


def test_repeating_the_resend_supersedes_and_revokes_the_links_just_sent(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, stop_one: Any,
        settings: Any, smtp: Any, mail_channel: Any) -> None:
    """O reenvio não é idempotente: repetir o pedido reenvia e revoga os links recém-enviados;
    um envio anterior ainda pendente é cancelado (`superseded`), e o convite já
    enviado é revogado (`replaced`) na primeira transação do envio novo; só o último link vale:
    o mesmo pedido, repetido, dá sempre a mesma resposta 200 `{queued: 1}`; com o worker
    no ar, o segundo envio revoga o convite do primeiro (`replaced`, `origin: coordination`) e manda
    outro e-mail; com o worker parado, dois pedidos seguidos criam dois envios pendentes, e ao subir o
    worker o mais antigo é cancelado (`cancelled`/`superseded`, `attempts` 0, sem e-mail) e o mais novo
    revoga o convite anterior e envia. No fim, só o convite do último e-mail está pendente."""
    not_before = datetime.now(timezone.utc)
    username = "cq_reenvio_repetido_bot"
    offering, _ = _offering(client, bot_api, "reenvio-repetido", username)
    student = _student("Repetido")
    _import(client, offering, [student])
    person = _person(sql, student)
    ((eid, pid),) = _student_enrollments(sql, offering)
    channel = _DefaultChannel()
    worker = _worker(bot_worker_thread, settings, channel, "worker-repetido-1", mail_channel)
    t0 = clock.now()
    cursor = _last_seq(sql)

    assert resend_invites(client, ADMIN_TOKEN, offering["code"], [person["alias"]]) == _resend_body(offering, 1)
    _wait(bot_api, lambda: _all_settled(sql, offering, 1))
    clock.advance(STEP)
    t1 = clock.now()
    assert resend_invites(client, ADMIN_TOKEN, offering["code"], [person["alias"]]) == _resend_body(offering, 1)
    _wait(bot_api, lambda: _all_settled(sql, offering, 2))
    assert stop_one(worker) != float("inf")

    clock.advance(60)
    t2 = clock.now()
    for _ in range(2):
        assert resend_invites(client, ADMIN_TOKEN, offering["code"], [person["pseudo_id"]]) == _resend_body(offering, 1)
    worker = _worker(bot_worker_thread, settings, channel, "worker-repetido-2", mail_channel)
    _wait(bot_api, lambda: _all_settled(sql, offering, 4))

    h = timedelta(hours=DEFAULT_HOURS)
    smtp.wait_for(lambda: len(smtp.messages()) >= 3, timeout=PROCESS_TIMEOUT)
    codes = _emails(smtp, username, [(student.email, t0 + h), (student.email, t1 + h), (student.email, t2 + h)],
                    not_before)
    assert len(set(codes)) == 3
    sends = _sends(sql, offering)
    assert sends == [
        _send_row(sends[0][0], eid, pid, DEFAULT_HOURS, t0, state="sent", attempts=1, finished_at=t0),
        _send_row(sends[1][0], eid, pid, DEFAULT_HOURS, t1, state="sent", attempts=1, finished_at=t1),
        _send_row(sends[2][0], eid, pid, DEFAULT_HOURS, t2, state="cancelled", cancel_reason="superseded",
                  attempts=0, finished_at=t2),
        _send_row(sends[3][0], eid, pid, DEFAULT_HOURS, t2, state="sent", attempts=1, finished_at=t2),
    ]
    assert _invites(sql, offering) == [
        (pid, _sha256(codes[0]), "revoked", t0 + h, t0, None, t1, "coordination"),
        (pid, _sha256(codes[1]), "revoked", t1 + h, t1, None, t2, "coordination"),
        (pid, _sha256(codes[2]), "pending", t2 + h, t2, None, None, "coordination"),
    ]
    assert _student_events(client, cursor) == [
        _queued(offering, t0, 1), _sent(offering, pid, t0, t0 + h),
        _queued(offering, t1, 1), _revoked(offering, pid, t1, "coordination"), _sent(offering, pid, t1, t1 + h),
        _queued(offering, t2, 1), _queued(offering, t2, 1),
        _cancelled(offering, pid, t2, "superseded"),
        _revoked(offering, pid, t2, "coordination"), _sent(offering, pid, t2, t2 + h),
    ]
    assert stop_one(worker) != float("inf")
    assert channel.calls == []


def test_resend_with_only_non_students_and_missing_queues_zero(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any) -> None:
    """Os envios são enfileirados mesmo com 0; regras do reenvio; sem nenhum aluno na
    lista, `queued` é 0 e a resposta continua 200: um pedido só com uma pessoa que não é aluna
    (citada pelo apelido e pelo identificador) e um pseudônimo sem pessoa (repetido) → 200 `{queued: 0,
    not_students: [a pessoa, uma vez], not_found_count: 1}`, sem eco do pseudônimo sem pessoa, nenhum
    envio nem tarefa, e `student.invites_queued {offering_id, origin: coordination, queued: 0}` com o
    `token_id` de quem pediu."""
    offering, _ = _offering(client, bot_api, "reenvio-zero")
    _import(client, offering, [_student("Nao Citado")])
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Reenvio Zero", ra="6649000003")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    at = clock.now()
    cursor = _last_seq(sql)

    resp = _post_resend(client, offering["code"],
                        {"pseudonyms": [MISSING_ALIAS, person["pseudo_id"], MISSING_ALIAS, person["alias"]]},
                        token=writer)
    assert _ok(resp, "invites.resend") == _resend_body(offering, 0, not_students=[_canonical(person)],
                                                        not_found_count=1)
    assert_no_echo(resp, MISSING_ALIAS)
    assert resend_invites(client, writer, offering["code"], [person["alias"]]) == _resend_body(
        offering, 0, not_students=[_canonical(person)])
    assert _sends(sql, offering) == []
    assert sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'invite.email'").fetchone()[0] == 0
    actor = writer_body["token_id"]
    assert _student_events(client, cursor) == [_queued(offering, at, 0, actor), _queued(offering, at, 0, actor)]


@pytest.mark.parametrize(("valid_hours", "expected_hours"), [
    pytest.param(None, DEFAULT_HOURS, id="omitido-168"),
    pytest.param(1, 1, id="minimo-1h"),
    pytest.param(720, 720, id="maximo-720h"),
])
def test_resend_valid_hours_counts_from_the_send(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                 bot_worker_thread: Any, stop_one: Any, settings: Any,
                                                 smtp: Any, mail_channel: Any, valid_hours: int | None,
                                                 expected_hours: int) -> None:
    """O reenvio aceita a validade de 1 hora a 30 dias, padrão de 7 dias, contada do envio;
    `valid_hours` na resposta é o valor usado, 168 quando omitido: a resposta,
    o `invite_sends.valid_hours`, o `expires_at` do convite gerado pelo worker (15 s depois do pedido),
    o do `student.invite_sent` e a validade no fuso do e-mail seguem o valor pedido."""
    not_before = datetime.now(timezone.utc)
    username = "cq_reenvio_validade_bot"
    offering, _ = _offering(client, bot_api, "reenvio-validade", username)
    student = _student("Validade")
    _import(client, offering, [student])
    person = _person(sql, student)
    ((eid, pid),) = _student_enrollments(sql, offering)
    t0 = clock.now()
    cursor = _last_seq(sql)

    body = resend_invites(client, ADMIN_TOKEN, offering["code"], [person["alias"]], valid_hours=valid_hours)
    assert body == _resend_body(offering, 1, valid_hours=expected_hours)
    sends = _sends(sql, offering)
    assert sends == [_send_row(sends[0][0], eid, pid, expected_hours, t0)]

    clock.advance(STEP)
    t1 = clock.now()
    channel = _DefaultChannel()
    worker = _worker(bot_worker_thread, settings, channel, "worker-validade", mail_channel)
    _wait(bot_api, lambda: _all_settled(sql, offering, 1))
    expires = t1 + timedelta(hours=expected_hours)
    smtp.wait_for(lambda: smtp.messages(), timeout=PROCESS_TIMEOUT)
    (code,) = _emails(smtp, username, [(student.email, expires)], not_before)
    assert _sends(sql, offering) == [_send_row(sends[0][0], eid, pid, expected_hours, t0, state="sent", attempts=1,
                                               finished_at=t1)]
    assert _invites(sql, offering) == [(pid, _sha256(code), "pending", expires, t1, None, None, "coordination")]
    assert _student_events(client, cursor) == [_queued(offering, t0, 1), _sent(offering, pid, t1, expires)]
    assert stop_one(worker) != float("inf")
    assert channel.calls == []


def test_resend_list_of_100_with_repeats_is_accepted(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Regras do reenvio (o limite de 100 vale para a lista enviada, com repetidos; cada pessoa conta uma
    vez, citada pelo apelido ou pelo identificador): 100 itens, 50 com o apelido e 50 com o identificador
    do mesmo aluno → 200 `{queued: 1}`, um único envio e uma única tarefa (a contraparte de 101 itens é
    o caso `101-repetidos` de `test_resend_rejects_invalid_request`)."""
    offering, _ = _offering(client, bot_api, "reenvio-cem")
    student = _student("Cem")
    _import(client, offering, [student])
    person = _person(sql, student)
    ((eid, pid),) = _student_enrollments(sql, offering)
    at = clock.now()

    body = resend_invites(client, ADMIN_TOKEN, offering["code"], [person["alias"]] * 50 + [person["pseudo_id"]] * 50)
    assert body == _resend_body(offering, 1)
    sends = _sends(sql, offering)
    assert sends == [_send_row(sends[0][0], eid, pid, DEFAULT_HOURS, at)]
    assert _tasks(sql, [sends[0][0]]) == [_pending_task_row(sends[0][0], at)]


# ---------------------------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------------------------

_UPPER_UUID = "DEADBEEF-CAFE-4BAD-8BAD-0123456789AB"
_UUID_V1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
_HEX_33 = "fedcba9876543210fedcba9876543210f"
_INVALID_ITEMS_SENTINELS = ("ABCDEF12", "12345678", _UPPER_UUID, _UUID_V1, _HEX_33, "Nome Digitado Qualquer")


def _distinct_aliases(n: int) -> list[str]:
    return [f"ab{i:010x}" for i in range(n)]


VALIDATION_CASES = [
    pytest.param(lambda ok: {"json": {"pseudonyms": []}}, [("pseudonyms", "too_few")], (), id="lista-vazia"),
    pytest.param(lambda ok: {"json": {"pseudonyms": _distinct_aliases(101)}}, [("pseudonyms", "too_many")], (),
                 id="101-distintos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok] * 101}}, [("pseudonyms", "too_many")], (),
                 id="101-repetidos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": ["NAO-E-PSEUDONIMO"] * 101}}, [("pseudonyms", "too_many")],
                 ("NAO-E-PSEUDONIMO",), id="101-invalidos-sem-validar-itens"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [
        ok, "ABCDEF12", ok, "12345678", _UPPER_UUID, "abc", "", 123, _UUID_V1, _HEX_33,
        "Nome Digitado Qualquer", "ABCD"]}},
                 [(f"pseudonyms[{i}]", "invalid_format") for i in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11)],
                 _INVALID_ITEMS_SENTINELS, id="itens-invalidos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": "x"}}, [("pseudonyms", "invalid_format")], (),
                 id="pseudonyms-texto"),
    pytest.param(lambda ok: {"json": {"pseudonyms": None}}, [("pseudonyms", "missing")], (), id="pseudonyms-null"),
    pytest.param(lambda ok: {"json": {}}, [("pseudonyms", "missing")], (), id="objeto-vazio"),
    pytest.param(lambda ok: {"json": {"valid_hours": 24}}, [("pseudonyms", "missing")], (), id="sem-pseudonyms"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": 0}}, [("valid_hours", "out_of_range")], (),
                 id="validade-zero"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": 721}}, [("valid_hours", "out_of_range")],
                 (), id="validade-721"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": -5}}, [("valid_hours", "out_of_range")],
                 (), id="validade-negativa"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": True}},
                 [("valid_hours", "invalid_format")], (), id="validade-booleana"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": 1.5}},
                 [("valid_hours", "invalid_format")], (), id="validade-fracionaria"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "valid_hours": "24"}},
                 [("valid_hours", "invalid_format")], (), id="validade-texto"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "campo_extra_sentinela": "valor-extra-sentinela"}},
                 [("body", "unknown_field")], ("campo_extra_sentinela", "valor-extra-sentinela"), id="campo-extra"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [], "valid_hours": 0, "outro_campo_sentinela": 1}},
                 [("pseudonyms", "too_few"), ("valid_hours", "out_of_range"), ("body", "unknown_field")],
                 ("outro_campo_sentinela",), id="varios-erros-campo-extra-no-fim"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok, "ABCDEF12"], "valid_hours": True}},
                 [("pseudonyms[1]", "invalid_format"), ("valid_hours", "invalid_format")], ("ABCDEF12",),
                 id="item-e-validade-na-ordem-do-esquema"),
    pytest.param(lambda ok: {"json": {"pseudonyms": "x", "valid_hours": "24"}},
                 [("pseudonyms", "invalid_format"), ("valid_hours", "invalid_format")], (),
                 id="dois-tipos-errados"),
    pytest.param(lambda ok: {"content": b""}, [("body", "missing")], (), id="corpo-vazio"),
    pytest.param(lambda ok: {"content": b'{"pseudonyms": ["sentinela-json-quebrado"'}, [("body", "invalid_format")],
                 ("sentinela-json-quebrado",), id="corpo-nao-json"),
    pytest.param(lambda ok: {"content": b'["sentinela-lista-json"]'}, [("body", "invalid_format")],
                 ("sentinela-lista-json",), id="corpo-lista"),
    # JSON que não é UTF-8 (contrato da API base): o de um cliente em latin-1/cp1252 e uma sequência inválida.
    pytest.param(lambda ok: {"content": json.dumps({"pseudonyms": [ok], "nota": "razão sentinela-latina"},
                                                   ensure_ascii=False).encode("latin-1")},
                 [("body", "invalid_format")], ("razão sentinela-latina", "sentinela-latina"), id="corpo-latin1"),
    pytest.param(lambda ok: {"content": b"\xc3\x28"}, [("body", "invalid_format")], (), id="corpo-utf8-invalido"),
]


@pytest.mark.parametrize(("build", "fields", "sentinels"), VALIDATION_CASES)
def test_resend_rejects_invalid_request(client: Any, sql: Any, bot_api: Any, build: Any,
                                        fields: list[tuple[str, str]], sentinels: tuple[str, ...]) -> None:
    """As regras do `identity.reveal` da API base: 1 a 100 itens, com os repetidos; lista fora
    do limite sem validar os itens; um `pseudonyms[i]` por item fora do formato; no reenvio,
    (`valid_hours` fora de 1..720 → `out_of_range`; não inteiro ou booleano → `invalid_format`) e
    entrada estrita da API base (ordem do esquema, campo desconhecido no fim; corpo ausente, não JSON, não
    UTF-8 ou não objeto): 400 com `fields` exatos, sem eco, e nada muda (envios, tarefas, eventos)."""
    offering, _ = _offering(client, bot_api, "reenvio-invalido")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Reenvio Valida", ra="6649000004")
    before = _unchanged(sql)
    kwargs = build(person["alias"])
    headers = dict(ADMIN)
    if "content" in kwargs:
        headers["Content-Type"] = "application/json"
    resp = client.post(f"/v1/offerings/{offering['code']}/invites/resend", headers=headers, **kwargs)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *sentinels)
    assert _unchanged(sql) == before


# ---------------------------------------------------------------------------------------------
# Recusas, autenticação e permissão
# ---------------------------------------------------------------------------------------------


def test_resend_refusals_follow_the_contract_order(client: Any, sql: Any, bot_api: Any) -> None:
    """Nas condições do envio dos convites para o oferecimento (ativo, com bot com acesso);
    ordem: 400 → 404 do oferecimento → `invalid_state` → `bot_required`: oferecimento
    inexistente (por código e por identificador) → 404; `ref` fora do formato → 400 `[{ref,
    invalid_format}]`; corpo inválido num oferecimento inexistente → 400 (a validação vem antes);
    rascunho, encerrado e arquivado (cada um com o aluno citado) → 409 `invalid_state` com o estado
    atual; ativo com o bot `no_access` (pré-condição direta no banco, como em oferecimentos e bots) ou sem bot (a API não
    deixa um oferecimento ativo sem bot; a linha do bot é apagada antes de qualquer update ou mensagem)
    → 409 `bot_required`. Nada muda em nenhum caso."""
    student = _student("Recusa")
    draft, _ = _offering(client, bot_api, "reenvio-rascunho", activate=False)
    _import(client, draft, [student])
    alias = _person(sql, student)["alias"]
    closed, _ = _offering(client, bot_api, "reenvio-encerrado")
    _close(client, closed["code"])
    archived, _ = _offering(client, bot_api, "reenvio-arquivado", activate=False)
    _archive(client, archived["code"])
    no_access, no_access_bot = _offering(client, bot_api, "reenvio-sem-acesso")
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                "WHERE telegram_bot_id = %s", (no_access_bot,))
    no_bot, _ = _offering(client, bot_api, "reenvio-sem-bot")
    sql.execute("DELETE FROM bots WHERE offering_id = (SELECT id FROM offerings WHERE code = %s)", (no_bot["code"],))
    before = _unchanged(sql)

    cases = [
        ("reenvio-inexistente", 404, "not_found", None),
        (MISSING_OFFERING_ID, 404, "not_found", None),
        (draft["code"], 409, "invalid_state", "draft"),
        (closed["code"], 409, "invalid_state", "closed"),
        (archived["offering_id"], 409, "invalid_state", "archived"),
        (no_access["code"], 409, "bot_required", None),
        (no_bot["code"], 409, "bot_required", None),
    ]
    for ref, status, code, state in cases:
        resp = _post_resend(client, ref, {"pseudonyms": [alias]})
        if state is None:
            assert_error(resp, status, code)
        else:
            assert_error(resp, status, code, state=state)
    resp = _post_resend(client, "Oferta_Formato_Invalido", {"pseudonyms": [alias]})
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_no_echo(resp, "Oferta_Formato_Invalido")
    assert_error(_post_resend(client, "reenvio-inexistente", {"pseudonyms": []}), 400, "invalid_request",
                 fields=[("pseudonyms", "too_few")])
    assert _unchanged(sql) == before


def test_resend_authentication_and_permission(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Permissão `offerings:write`, sem permissão nova; ordem da API base (401
    → 400 → 403 → regras): sem token ou com token desconhecido → 401; um token com `offerings:read`,
    `people:write`, `bots:manage` e `identity:reveal` (sem `offerings:write`) → 403 com `permission =
    offerings:write`, inclusive num oferecimento inexistente; o pedido inválido é 400 antes do 403; nada
    muda. Um token só com `offerings:write` reenvia, e o evento leva o `token_id` dele."""
    offering, _ = _offering(client, bot_api, "reenvio-permissao")
    student = _student("Permissao")
    _import(client, offering, [student])
    alias = _person(sql, student)["alias"]
    ((eid, pid),) = _student_enrollments(sql, offering)
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "people:write", "bots:manage", "identity:reveal"],
                             label="sem escrita de oferecimento")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    before = _unchanged(sql)

    for token in (None, "cq_" + "z" * 43):
        assert_error(_post_resend(client, offering["code"], {"pseudonyms": [alias]}, token=token), 401,
                     "unauthenticated")
    for ref in (offering["code"], "reenvio-inexistente"):
        assert_error(_post_resend(client, ref, {"pseudonyms": [alias]}, token=reader), 403, "forbidden",
                     permission="offerings:write")
    assert_error(_post_resend(client, offering["code"], {"pseudonyms": [alias], "valid_hours": 0}, token=reader),
                 400, "invalid_request", fields=[("valid_hours", "out_of_range")])
    assert _unchanged(sql) == before

    at = clock.now()
    cursor = _last_seq(sql)
    assert resend_invites(client, writer, offering["code"], [alias]) == _resend_body(offering, 1)
    assert _student_events(client, cursor) == [_queued(offering, at, 1, writer_body["token_id"])]
    sends = _sends(sql, offering)
    assert sends == [_send_row(sends[0][0], eid, pid, DEFAULT_HOURS, at)]
