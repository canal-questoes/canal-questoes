"""Envio dos convites em duas fases: preparo e confirmação.

Cobre o preparo, a elegibilidade, a confirmação e as recusas do envio dos convites; a elegibilidade pelo predicado da
adesão, com a exigência de envio `sent`; e a injeção de falhas só pelos mecanismos declarados.
Contratos: a API do preparo e da confirmação (`invites.prepare_send` e
`invites.send`); no banco, `invite_sends`, as colunas do preparo em `offerings`,
a elegibilidade, o preparo e a confirmação, a tarefa `invite.email` (só como meio de montar os estados),
os estados da adesão, os eventos `student.invites_prepared` e `student.invites_queued` e
o e-mail do convite; e as convenções da API base e dos bots (ordem das verificações, entrada estrita, sem eco,
horários em UTC).

Convenções: corpo inteiro por `==` e `assert_matches` (pelos atalhos de
`tests/helpers/api.py`) em toda resposta de sucesso; erros por `assert_error` (status, `code`,
`message`, `fields` e `state` exatos); horários pelo `FakeClock` e `iso(dt)`; ids internos, `seq`,
códigos de confirmação e pseudônimos nunca como literal (só relações: a ordem de `enrollments.id`, o
resumo SHA-256 do código, o pseudônimo lido do banco pelo RA da lista). Os alunos da lista são neutros
(fora dos alvos canário), com RAs de 10 dígitos e e-mails em `turma.example.org`.

Os estados da adesão são montados só pelos fluxos e pelos mecanismos declarados
(injeção de falhas nos testes): o envio pelo worker (`bot_worker_thread` com `verification=`)
contra o SMTP falso; a falha definitiva e a temporária pelo SMTP falso (`fail("rcpt", …)`); a queda
depois da primeira transação do envio pelo `Crash` do `ScriptedVerificationChannel`; a posse esgotada por SQL em `tasks`
(`attempts = max_attempts`, `state = 'leased'`, `lease_until` no passado; o `claim` da fila a marca
`failed` sem chamar o tratador); o termo e o vínculo pela Bot API falsa (`/start` e `/aceito`). Nunca
`UPDATE invite_sends` por SQL. Todo e-mail capturado é conferido por inteiro: envelope,
cabeçalhos (o `Date` e o `Message-ID` só por forma) e texto decodificado.

No esqueleto, as rotas `POST /v1/offerings/{ref}/students`, `…/invites/prepare` e `…/invites` não
existem e a migração `0003` não foi aplicada: cada função falha no primeiro pedido do vínculo do aluno (404 no lugar
do 200, 400 ou 409 esperado; rota ausente) ou, nas que tiram o instantâneo do banco antes do pedido, na
leitura das colunas novas de `offerings` (`UndefinedColumn`; coluna ausente). As
asserções sobre o que o esqueleto já tem (401 sem token; 404 de oferecimento inexistente, que no
esqueleto coincide com o 404 da rota ausente) ficam na mesma função de um caso que falha.
"""

from __future__ import annotations

import dataclasses
import email.utils
import hashlib
import itertools
import json
import re
import secrets
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
    create_token,
    import_students,
    list_events,
    prepare_invites,
    register_bot,
    register_person,
    send_invites,
)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import DELEGATE, Crash, ScriptedVerificationChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.db import scan_all_tables
from tests.helpers.smtp import SENDER as SMTP_SENDER

ADMIN = auth(ADMIN_TOKEN)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"
DEFAULT_HOURS = 168
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"[A-Za-z0-9_-]{22}")
PROCESS_TIMEOUT = 15.0
LEASE_EXHAUSTED = "posse vencida sem conclusão"  # `last_error` da posse esgotada

MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"

# Tipos de evento do vínculo do aluno, como literais (o esqueleto ainda não os tem).
STUDENT_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                       "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                       "student.invite_revoked", "student.joined")

# E-mail do convite, por extenso; a disciplina e o período padrão de
# `create_offering` não têm identidade, então a redação não os muda.
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
    return _Student(name=f"Aluno Envio {label}", ra=f"663{n:07d}",
                    email=f"aluno.envio.{label.lower().replace(' ', '.')}.{n}@turma.example.org")


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


def _pseudo(sql: Any, student: _Student) -> str:
    """O `pseudo_id` da pessoa criada pela importação, achado pelo RA (leitura de teste)."""
    row = sql.execute("SELECT p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (student.ra,)).fetchone()
    assert row is not None, "aluno da lista sem pessoa depois da importação"
    return row[0]


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


def _task_of(sql: Any, send_id: int) -> tuple:
    """(state, attempts, last_error) da única tarefa `invite.email:<id>` do envio."""
    rows = sql.execute("SELECT state, attempts, last_error FROM tasks WHERE idempotency_key = %s",
                       (f"invite.email:{send_id}",)).fetchall()
    assert len(rows) == 1, f"tarefas do envio: {rows}"
    return rows[0]


def _pending_send_row(send_id: int, enrollment_id: int, pseudo: str, valid_hours: int, at: datetime) -> tuple:
    """A linha de `invite_sends` de um envio do lote recém-criado."""
    return (send_id, enrollment_id, pseudo, "batch", valid_hours, "pending", None, None, None, 0, at, None)


def _pending_task_row(send_id: int, at: datetime) -> tuple:
    """A tarefa `invite.email` recém-enfileirada do envio (payload só com o id)."""
    return ({"invite_send_id": send_id}, f"invite.email:{send_id}", "pending", 0, at, None, None, None, at, None)


def _prepared_columns(sql: Any, offering: dict) -> tuple:
    """(invite_code_sha256, invite_code_expires_at, invite_valid_hours, invite_eligible_count)."""
    return sql.execute(
        "SELECT invite_code_sha256, invite_code_expires_at, invite_valid_hours, invite_eligible_count "
        "FROM offerings WHERE offering_id = %s", (uuid.UUID(offering["offering_id"]),)).fetchone()


CLEARED = (None, None, None, None)


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _student_events(client: Any, cursor: int) -> list[dict]:
    """Os eventos do vínculo do aluno depois de `cursor`, pela API, em ordem de `seq` (crescente), sem o `seq`."""
    items = list_events(client, ADMIN_TOKEN, *STUDENT_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _prepared_event(offering: dict, at: datetime, eligible: int, valid_hours: int,
                    actor: str = "bootstrap") -> dict:
    return {"type": "student.invites_prepared", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": offering["offering_id"], "eligible": eligible, "valid_hours": valid_hours,
                     "expires_at": iso(at + CONFIRMATION)}}


def _queued_event(offering: dict, at: datetime, queued: int, actor: str = "bootstrap") -> dict:
    return {"type": "student.invites_queued", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": offering["offering_id"], "origin": "batch", "queued": queued}}


def _prepare_body(offering: dict, eligible: int, valid_hours: int, code: str, at: datetime) -> dict:
    """A resposta 200 exata de `invites.prepare_send`, com o código devolvido."""
    return {"offering": _ref(offering), "eligible": eligible, "valid_hours": valid_hours,
            "confirmation_code": code, "expires_at": iso(at + CONFIRMATION)}


def _prepare(client: Any, offering: dict, eligible: int, *, valid_hours: int | None = None,
             token: str = ADMIN_TOKEN, at: datetime) -> str:
    """Prepara pela API, confere a resposta inteira e devolve o código de confirmação."""
    body = prepare_invites(client, token, offering["code"], valid_hours=valid_hours)
    code = body["confirmation_code"]
    assert CONFIRMATION_RE.fullmatch(code), "código de confirmação fora do formato esperado"
    assert body == _prepare_body(offering, eligible, DEFAULT_HOURS if valid_hours is None else valid_hours,
                                 code, at)
    return code


def _confirm(client: Any, offering: dict, code: str, queued: int, valid_hours: int = DEFAULT_HOURS, *,
             token: str = ADMIN_TOKEN) -> None:
    body = send_invites(client, token, offering["code"], code)
    assert body == {"offering": _ref(offering), "queued": queued, "valid_hours": valid_hours}


def _post_prepare(client: Any, ref: str, body: Any = None, *, token: str | None = ADMIN_TOKEN) -> Any:
    return client.post(f"/v1/offerings/{ref}/invites/prepare", json={} if body is None else body,
                       headers=auth(token))


def _post_confirm(client: Any, ref: str, code: Any, *, token: str | None = ADMIN_TOKEN) -> Any:
    return client.post(f"/v1/offerings/{ref}/invites", json={"confirmation_code": code}, headers=auth(token))


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


def _nothing_sent(sql: Any, offering: dict) -> dict:
    """O que um pedido recusado não pode mudar: as colunas do preparo, os envios, as tarefas de envio
    e os eventos."""
    return {"prepared": _prepared_columns(sql, offering), "sends": _sends(sql, offering),
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


def _emails(smtp: Any, username: str, expected: list[tuple[str, datetime]], not_before: datetime) -> dict[str, str]:
    """Confere, na ordem de chegada, todas as mensagens capturadas contra `expected` (endereço e
    vencimento do convite) e devolve o código de cada endereço (os códigos entram em `remember_secret`
    pelo `invite_links()`)."""
    messages = smtp.messages()
    links = smtp.invite_links()
    assert [m.rcpt_to for m in messages] == [(address,) for address, _ in expected]
    assert [link.message_seq for link in links] == [m.seq for m in messages], "um link por mensagem"
    for message, link, (address, expires_at) in zip(messages, links, expected):
        _assert_invite_email(message, address=address, username=username, code=link.code,
                             expires_at=expires_at, not_before=not_before)
    return {address: link.code for link, (address, _) in zip(links, expected)}


def _worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel, name: str,
            verification: Any) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=name),
                             verification=verification)


def _wait(bot_api: Any, predicate: Any) -> Any:
    return bot_api.wait_for(predicate, timeout=PROCESS_TIMEOUT)


def _send_states(sql: Any, offering: dict) -> dict[str, str]:
    """O estado do último envio de cada pseudônimo do oferecimento."""
    return {row[2]: row[5] for row in _sends(sql, offering)}


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


def _exhaust_lease(sql: Any, settings: Any, clock: Any, send_id: int) -> None:
    """Posse esgotada por SQL (injeção de falhas nos testes): a tarefa do envio fica
    `leased`, com `attempts = max_attempts` e a posse vencida; o próximo `claim` da fila a marca `failed`
    sem chamar o tratador. Nunca toca em `invite_sends`."""
    rows = sql.execute(
        "UPDATE tasks SET state = 'leased', attempts = %s, lease_owner = 'worker-sem-posse', lease_until = %s "
        "WHERE idempotency_key = %s AND state IN ('pending', 'leased') RETURNING id",
        (settings.max_attempts, clock.now() - timedelta(seconds=1), f"invite.email:{send_id}")).fetchall()
    assert len(rows) == 1, "a tarefa do envio não estava pendente nem em posse"


# ---------------------------------------------------------------------------------------------
# Preparo
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("valid_hours", "expected_hours"), [
    pytest.param(None, DEFAULT_HOURS, id="padrao-168"),
    pytest.param(1, 1, id="minimo-1h"),
    pytest.param(720, 720, id="maximo-720h"),
])
def test_prepare_returns_exact_body_event_and_stores_only_the_digest(
        client: Any, clock: Any, sql: Any, bot_api: Any, db_url: str, valid_hours: int | None,
        expected_hours: int) -> None:
    """Preparo do envio: código de confirmação guardado só como resumo; contrato do preparo,
    colunas do preparo e eventos: com 3 alunos importados e sem convite, o preparo → 200 com o
    corpo exato (`eligible` 3, `valid_hours` pedido ou 168, código de confirmação de 22 caracteres
    base64url e `expires_at` = agora + 10 min); o oferecimento guarda só o resumo SHA-256 do código, o
    vencimento, a validade e a contagem (o código não está em nenhuma coluna de nenhuma tabela, e o
    resumo só em `offerings.invite_code_sha256`); um `student.invites_prepared` exato, com o ator; nada
    é enviado ainda (nenhum `invite_sends` nem tarefa `invite.email`)."""
    offering, _ = _offering(client, bot_api, "envio-preparo")
    _import(client, offering, [_student("Um"), _student("Dois"), _student("Tres")])
    clock.advance(60)
    at = clock.now()
    cursor = _last_seq(sql)

    code = _prepare(client, offering, 3, valid_hours=valid_hours, at=at)
    assert _prepared_columns(sql, offering) == (_sha256(code), at + CONFIRMATION, expected_hours, 3)
    assert _student_events(client, cursor) == [_prepared_event(offering, at, 3, expected_hours)]
    assert _sends(sql, offering) == []
    assert sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'invite.email'").fetchone()[0] == 0
    scan = scan_all_tables(db_url)
    assert [(t, c) for t, c, text in scan if code in text] == []
    assert [(t, c) for t, c, text in scan if _sha256(code) in text] == [("offerings", "invite_code_sha256")]


INVALID_PREPARE = [
    pytest.param({"valid_hours": 0}, [("valid_hours", "out_of_range")], id="zero"),
    pytest.param({"valid_hours": 721}, [("valid_hours", "out_of_range")], id="721"),
    pytest.param({"valid_hours": -5}, [("valid_hours", "out_of_range")], id="negativo"),
    pytest.param({"valid_hours": True}, [("valid_hours", "invalid_format")], id="booleano"),
    pytest.param({"valid_hours": 1.5}, [("valid_hours", "invalid_format")], id="fracionario"),
    pytest.param({"valid_hours": "24"}, [("valid_hours", "invalid_format")], id="texto"),
    pytest.param({"valid_hours": 24, "campo_extra_sentinela": 1}, [("body", "unknown_field")], id="campo-extra"),
    pytest.param({"valid_hours": 0, "campo_extra_sentinela": "valor-extra-sentinela"},
                 [("valid_hours", "out_of_range"), ("body", "unknown_field")], id="erro-e-campo-extra-no-fim"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_PREPARE)
def test_prepare_rejects_invalid_body_without_change(client: Any, sql: Any, bot_api: Any, body: dict,
                                                     fields: list[tuple[str, str]]) -> None:
    """Validade ajustável de 1 hora a 30 dias (`valid_hours` inteiro
    de 1 a 720; fora: `out_of_range`; não inteiro ou booleano: `invalid_format`) e entrada estrita da API base
    (campo desconhecido → `[{body, unknown_field}]`, no fim): 400 com `fields` exatos, sem eco, e nada
    muda (colunas do preparo, envios, tarefas, eventos)."""
    offering, _ = _offering(client, bot_api, "envio-preparo-invalido")
    before = _nothing_sent(sql, offering)
    resp = _post_prepare(client, offering["code"], body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "campo_extra_sentinela", "valor-extra-sentinela")
    assert _nothing_sent(sql, offering) == before
    assert before["prepared"] == CLEARED


RAW_PREPARE_BODIES = [
    pytest.param(None, [("body", "missing")], id="sem-corpo"),
    pytest.param(b"isto nao e json sentinela", [("body", "invalid_format")], id="nao-json"),
    pytest.param(b'[{"valid_hours": 24}]', [("body", "invalid_format")], id="lista-json"),
    pytest.param(b"\xc3\x28", [("body", "invalid_format")], id="nao-utf8"),
]


@pytest.mark.parametrize(("raw", "fields"), RAW_PREPARE_BODIES)
def test_prepare_requires_a_json_object(client: Any, sql: Any, bot_api: Any, raw: bytes | None,
                                        fields: list[tuple[str, str]]) -> None:
    """Preparo: objeto obrigatório, que pode ser `{}`, e as regras de corpo da API base: sem
    corpo → `[{body, missing}]`; corpo que não é JSON (inclusive bytes fora de UTF-8) ou não é objeto →
    `[{body, invalid_format}]`; sem eco e sem mudança."""
    offering, _ = _offering(client, bot_api, "envio-preparo-corpo")
    before = _nothing_sent(sql, offering)
    path = f"/v1/offerings/{offering['code']}/invites/prepare"
    if raw is None:
        resp = client.post(path, headers=ADMIN)
    else:
        resp = client.post(path, content=raw, headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "isto nao e json sentinela")
    assert _nothing_sent(sql, offering) == before


# ---------------------------------------------------------------------------------------------
# Confirmação (no que é da API)
# ---------------------------------------------------------------------------------------------


def test_confirm_queues_one_send_and_task_per_eligible_in_enrollment_order(
        client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Enfileira os alunos elegíveis no momento da confirmação, em ordem de matrícula;
    tarefa idempotente, sem código nem e-mail no conteúdo; a confirmação e, no banco,
    `invite_sends`, colunas do preparo e eventos: com 5 alunos importados e o preparo feito por um token com
    `offerings:write`, a confirmação por esse token → 200 `{offering, queued: 5, valid_hours: 720}`; um
    `invite_sends` `batch`/`pending` (validade do preparo, `attempts` 0) e uma tarefa `invite.email`
    `pending` com a chave `invite.email:<id>` e o payload só com o id, por aluno, criados em ordem de
    `enrollments.id` (a importação embaralha a ordem); as quatro colunas do preparo limpas (uso
    único); `student.invites_prepared` e `student.invites_queued {origin: batch}` exatos, com o
    `token_id` de quem pediu. Repetir a confirmação com o mesmo código → 409 `invalid_confirmation`,
    sem nada duplicado. Um novo preparo não conta os alunos em envio (`sending`) e dá
    `eligible` 0, e a confirmação dele enfileira 0."""
    offering, _ = _offering(client, bot_api, "envio-confirmacao")
    students = [_student(label) for label in ("Ana", "Bia", "Caio", "Davi", "Eva")]
    _import(client, offering, students)
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    actor = writer_body["token_id"]
    enrollments = _student_enrollments(sql, offering)
    assert sorted(pseudo for _, pseudo in enrollments) == sorted(_pseudo(sql, s) for s in students)
    t0 = clock.now()
    cursor = _last_seq(sql)

    code = _prepare(client, offering, 5, valid_hours=720, token=writer, at=t0)
    clock.advance(60)
    t1 = clock.now()
    _confirm(client, offering, code, 5, 720, token=writer)

    sends = _sends(sql, offering)
    assert [row[1] for row in sends] == [eid for eid, _ in enrollments], "envios fora da ordem de matrícula"
    assert sends == [_pending_send_row(row[0], eid, pseudo, 720, t1)
                     for row, (eid, pseudo) in zip(sends, enrollments, strict=True)]
    send_ids = [row[0] for row in sends]
    assert send_ids == sorted(send_ids)
    assert _tasks(sql, send_ids) == [_pending_task_row(sid, t1) for sid in send_ids]
    assert _prepared_columns(sql, offering) == CLEARED
    assert _student_events(client, cursor) == [_prepared_event(offering, t0, 5, 720, actor),
                                               _queued_event(offering, t1, 5, actor)]

    resp = _post_confirm(client, offering["code"], code, token=writer)
    assert_error(resp, 409, "invalid_confirmation")
    assert_no_echo(resp, code)
    assert _sends(sql, offering) == sends
    assert _tasks(sql, send_ids) == [_pending_task_row(sid, t1) for sid in send_ids]

    clock.advance(60)
    t2 = clock.now()
    again = _prepare(client, offering, 0, at=t2)
    _confirm(client, offering, again, 0)
    assert _sends(sql, offering) == sends
    assert _student_events(client, cursor)[2:] == [_prepared_event(offering, t2, 0, DEFAULT_HOURS),
                                                   _queued_event(offering, t2, 0)]


def test_zero_eligible_prepares_and_confirms_zero(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Com 0 elegíveis, a resposta é 200: o código funciona e a confirmação
    enfileira 0; o evento sai mesmo com 0: num oferecimento ativo sem alunos, o preparo dá
    `eligible` 0 e a confirmação `queued` 0, sem envio nem tarefa, com os dois eventos exatos."""
    offering, _ = _offering(client, bot_api, "envio-zero")
    at = clock.now()
    cursor = _last_seq(sql)
    code = _prepare(client, offering, 0, at=at)
    _confirm(client, offering, code, 0)
    assert _sends(sql, offering) == []
    assert sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'invite.email'").fetchone()[0] == 0
    assert _prepared_columns(sql, offering) == CLEARED
    assert _student_events(client, cursor) == [_prepared_event(offering, at, 0, DEFAULT_HOURS),
                                               _queued_event(offering, at, 0)]


INVALID_CONFIRM_BODIES = [
    pytest.param({}, [("confirmation_code", "missing")], id="ausente"),
    pytest.param({"confirmation_code": None}, [("confirmation_code", "missing")], id="null"),
    pytest.param({"confirmation_code": ""}, [("confirmation_code", "too_short")], id="vazio"),
    pytest.param({"confirmation_code": "c" * 65}, [("confirmation_code", "too_long")], id="65-caracteres"),
    pytest.param({"confirmation_code": 12345678}, [("confirmation_code", "invalid_format")], id="inteiro"),
    pytest.param({"confirmation_code": "CodigoSentinela\x00NulNoMeio"}, [("confirmation_code", "invalid_format")],
                 id="nul"),
    pytest.param({"confirmation_code": "CodigoSentinela\ud800SubstitutoIsolado"},
                 [("confirmation_code", "invalid_format")], id="substituto-isolado"),
    pytest.param({"confirmation_code": "codigo-qualquer", "campo_extra_sentinela": 1},
                 [("body", "unknown_field")], id="campo-extra"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_CONFIRM_BODIES)
def test_confirm_rejects_invalid_body(client: Any, clock: Any, sql: Any, bot_api: Any, body: dict,
                                      fields: list[tuple[str, str]]) -> None:
    """Confirmação com o código do preparo (pedido
    `{confirmation_code}` de 1 a 64 caracteres, como `offerings.archive`; fora: `too_short`/`too_long`;
    não texto, U+0000 ou substituto isolado: `invalid_format`) e entrada estrita: 400 com `fields`
    exatos, sem eco, e o preparo não muda. Controle: 64 caracteres passam da validação e dão 409
    `invalid_confirmation`; o código verdadeiro continua valendo."""
    offering, _ = _offering(client, bot_api, "envio-confirma-invalido")
    _import(client, offering, [_student("Unico")])
    at = clock.now()
    code = _prepare(client, offering, 1, at=at)
    before = _nothing_sent(sql, offering)

    resp = client.post(f"/v1/offerings/{offering['code']}/invites", content=json.dumps(body).encode("ascii"),
                       headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "CodigoSentinela", "NulNoMeio", "SubstitutoIsolado", "campo_extra_sentinela", "c" * 65)
    assert_error(_post_confirm(client, offering["code"], "c" * 64), 409, "invalid_confirmation")
    assert _nothing_sent(sql, offering) == before
    assert before["prepared"] == (_sha256(code), at + CONFIRMATION, DEFAULT_HOURS, 1)
    _confirm(client, offering, code, 1)


# ---------------------------------------------------------------------------------------------
# Substituição, importação e códigos inválidos
# ---------------------------------------------------------------------------------------------


def test_new_preparation_replaces_the_previous_code(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Um novo preparo invalida o código anterior e sobrescreve as colunas:
    o segundo preparo (outra validade) substitui o primeiro nas colunas; a
    confirmação com o primeiro código → 409 `invalid_confirmation`, sem mudança; com o segundo → 200
    com a validade do segundo, e os envios levam essa validade; eventos exatos na ordem."""
    offering, _ = _offering(client, bot_api, "envio-substitui")
    _import(client, offering, [_student("Primeiro"), _student("Segundo")])
    t0 = clock.now()
    cursor = _last_seq(sql)
    first = _prepare(client, offering, 2, valid_hours=24, at=t0)
    clock.advance(60)
    t1 = clock.now()
    second = _prepare(client, offering, 2, valid_hours=48, at=t1)
    assert second != first
    assert _prepared_columns(sql, offering) == (_sha256(second), t1 + CONFIRMATION, 48, 2)

    before = _nothing_sent(sql, offering)
    resp = _post_confirm(client, offering["code"], first)
    assert_error(resp, 409, "invalid_confirmation")
    assert_no_echo(resp, first)
    assert _nothing_sent(sql, offering) == before

    _confirm(client, offering, second, 2, 48)
    assert [(row[3], row[4], row[5]) for row in _sends(sql, offering)] == [("batch", 48, "pending")] * 2
    assert _student_events(client, cursor) == [_prepared_event(offering, t0, 2, 24),
                                               _prepared_event(offering, t1, 2, 48),
                                               _queued_event(offering, t1, 2)]


def test_refused_imports_keep_the_confirmation_code(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Uma importação recusada (400 ou 409) não invalida o código: depois do
    preparo, uma importação recusada com 400 (`rows` vazio → `too_few`) e outra com 409 `list_conflict`
    (RA de uma pessoa cadastrada com outro e-mail → `[{rows[0], duplicate}]`) não mudam as colunas do
    preparo nem os envios; a confirmação com o código continua valendo e enfileira os contados."""
    offering, _ = _offering(client, bot_api, "envio-importa-recusada")
    kept = _student("Mantido")
    _import(client, offering, [kept])
    other = _student("Cadastrado")
    register_person(client, ADMIN_TOKEN, **other.row())
    at = clock.now()
    code = _prepare(client, offering, 1, at=at)
    before = _nothing_sent(sql, offering)

    resp = client.post(f"/v1/offerings/{offering['code']}/students", json={"rows": []}, headers=ADMIN)
    assert_error(resp, 400, "invalid_request", fields=[("rows", "too_few")])
    conflicting = {"name": other.name, "ra": other.ra, "email": "outro.endereco.conflito@turma.example.org"}
    resp = client.post(f"/v1/offerings/{offering['code']}/students", json={"rows": [conflicting]}, headers=ADMIN)
    assert_error(resp, 409, "list_conflict", fields=[("rows[0]", "duplicate")])
    assert_no_echo(resp, other.ra, other.name, "outro.endereco.conflito@turma.example.org")
    assert _nothing_sent(sql, offering) == before
    assert before["prepared"] == (_sha256(code), at + CONFIRMATION, DEFAULT_HOURS, 1)

    t1 = clock.now()
    _confirm(client, offering, code, 1)
    ((eid, pseudo),) = _student_enrollments(sql, offering)
    assert pseudo == _pseudo(sql, kept)
    sends = _sends(sql, offering)
    assert sends == [_pending_send_row(sends[0][0], eid, pseudo, DEFAULT_HOURS, t1)]


def test_accepted_import_clears_the_preparation(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Uma importação aceita no oferecimento depois do preparo invalida o código;
    na importação, ela limpa as colunas do preparo; e o código nunca vale para mais que
    `eligible_count` alunos (o aluno importado depois do preparo não entra por esse código):
    depois do preparo (1 elegível), uma importação aceita de outro aluno limpa as quatro colunas; a
    confirmação com o código → 409 `invalid_confirmation` e nada é enfileirado; um novo preparo conta
    os 2 alunos e a confirmação dele enfileira os 2, em ordem de matrícula."""
    offering, _ = _offering(client, bot_api, "envio-importa-aceita")
    first = _student("Antes")
    _import(client, offering, [first])
    t0 = clock.now()
    cursor = _last_seq(sql)
    code = _prepare(client, offering, 1, at=t0)
    later = _student("Depois")
    _import(client, offering, [later])
    assert _prepared_columns(sql, offering) == CLEARED

    resp = _post_confirm(client, offering["code"], code)
    assert_error(resp, 409, "invalid_confirmation")
    assert _sends(sql, offering) == []

    clock.advance(60)
    t1 = clock.now()
    again = _prepare(client, offering, 2, at=t1)
    _confirm(client, offering, again, 2)
    enrollments = _student_enrollments(sql, offering)
    sends = _sends(sql, offering)
    assert sends == [_pending_send_row(row[0], eid, pseudo, DEFAULT_HOURS, t1)
                     for row, (eid, pseudo) in zip(sends, enrollments, strict=True)]
    assert [pseudo for _, pseudo in enrollments] == [_pseudo(sql, first), _pseudo(sql, later)]
    imported = {"type": "student.list_imported", "occurred_at": iso(t0), "actor_token_id": "bootstrap",
                "data": {"offering_id": offering["offering_id"], "received": 1, "people_created": 1,
                         "people_reused": 0, "enrollments_created": 1, "already_enrolled": 0}}
    assert _student_events(client, cursor) == [_prepared_event(offering, t0, 1, DEFAULT_HOURS), imported,
                                               _prepared_event(offering, t1, 2, DEFAULT_HOURS),
                                               _queued_event(offering, t1, 2)]


def test_invalid_confirmation_codes_change_nothing(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Confirmação do envio: uso único; vencido, substituído, de outro oferecimento ou inventado → recusado,
    nada enviado; fronteira `agora < invite_code_expires_at`: o código de outro
    oferecimento, dois inventados e um de 64 caracteres → 409 `invalid_confirmation`, sem eco e sem
    mudança; 1 µs antes do vencimento, o código ainda confirma; no instante exato do vencimento, o do
    outro oferecimento é recusado e as colunas dele ficam como estavam; o código já usado → recusado."""
    a, _ = _offering(client, bot_api, "envio-codigo-a")
    b, _ = _offering(client, bot_api, "envio-codigo-b")
    _import(client, a, [_student("Codigo A")])
    _import(client, b, [_student("Codigo B")])
    t0 = clock.now()
    code_a = _prepare(client, a, 1, at=t0)
    code_b = _prepare(client, b, 1, at=t0)
    cursor = _last_seq(sql)
    before_a, before_b = _nothing_sent(sql, a), _nothing_sent(sql, b)

    for offering, code in ((a, code_b), (b, code_a), (a, secrets.token_urlsafe(16)), (b, "codigo-inventado"),
                           (a, "k" * 64)):
        resp = _post_confirm(client, offering["code"], code)
        assert_error(resp, 409, "invalid_confirmation")
        assert_no_echo(resp, code)
    assert (_nothing_sent(sql, a), _nothing_sent(sql, b)) == (before_a, before_b)

    clock.set(t0 + CONFIRMATION - timedelta(microseconds=1))
    just_before = clock.now()
    _confirm(client, a, code_a, 1)
    clock.set(t0 + CONFIRMATION)
    assert_error(_post_confirm(client, b["code"], code_b), 409, "invalid_confirmation")
    assert _prepared_columns(sql, b) == (_sha256(code_b), t0 + CONFIRMATION, DEFAULT_HOURS, 1)
    assert _sends(sql, b) == []
    assert_error(_post_confirm(client, a["code"], code_a), 409, "invalid_confirmation")
    assert len(_sends(sql, a)) == 1
    assert _student_events(client, cursor) == [_queued_event(a, just_before, 1)]


# ---------------------------------------------------------------------------------------------
# Estados, recusas e encerramento
# ---------------------------------------------------------------------------------------------


def test_refusals_follow_the_contract_order(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Recusas do envio: preparo e confirmação exigem oferecimento ativo com bot registrado e com acesso;
    nos demais casos, conflito; ordem: 404 do oferecimento → `invalid_state` →
    `bot_required` → `invalid_confirmation`: oferecimento inexistente (por código e por
    identificador) → 404; `ref` fora do formato → 400 `[{ref, invalid_format}]`; rascunho com bot,
    encerrado e arquivado → 409 `invalid_state` com o estado atual (o estado vem antes do código);
    ativo com o bot `no_access` (pré-condição direta no banco, como em oferecimentos e bots) ou sem bot (a API não deixa
    um oferecimento ativo sem bot; a linha do bot é apagada antes de qualquer update ou mensagem) → 409
    `bot_required`; um preparo válido seguido da perda de acesso do bot → a confirmação dá
    `bot_required` (antes do código), e o código continua guardado e vale depois que o acesso volta.
    Nada muda nos pedidos recusados."""
    draft, _ = _offering(client, bot_api, "envio-rascunho", activate=False)
    closed, _ = _offering(client, bot_api, "envio-encerrado")
    _close(client, closed["code"])
    archived, _ = _offering(client, bot_api, "envio-arquivado", activate=False)
    _archive(client, archived["code"])
    no_access, no_access_bot = _offering(client, bot_api, "envio-sem-acesso")
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                "WHERE telegram_bot_id = %s", (no_access_bot,))
    no_bot, _ = _offering(client, bot_api, "envio-sem-bot")
    sql.execute("DELETE FROM bots WHERE offering_id = (SELECT id FROM offerings WHERE code = %s)",
                (no_bot["code"],))
    before = sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall()

    cases = [
        ("mc999-inexistente", 404, "not_found", None),
        (MISSING_OFFERING_ID, 404, "not_found", None),
        (draft["code"], 409, "invalid_state", "draft"),
        (closed["code"], 409, "invalid_state", "closed"),
        (archived["offering_id"], 409, "invalid_state", "archived"),
        (no_access["code"], 409, "bot_required", None),
        (no_bot["code"], 409, "bot_required", None),
    ]
    for ref, status, code, state in cases:
        for resp in (_post_prepare(client, ref), _post_confirm(client, ref, secrets.token_urlsafe(16))):
            if state is None:
                assert_error(resp, status, code)
            else:
                assert_error(resp, status, code, state=state)
    for resp in (_post_prepare(client, "Oferta_Formato_Invalido"),
                 _post_confirm(client, "Oferta_Formato_Invalido", "qualquer-codigo")):
        assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
        assert_no_echo(resp, "Oferta_Formato_Invalido")
    assert sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall() == before
    assert sql.execute("SELECT count(*) FROM invite_sends").fetchone()[0] == 0

    active, active_bot = _offering(client, bot_api, "envio-perde-acesso")
    _import(client, active, [_student("Acesso")])
    at = clock.now()
    code = _prepare(client, active, 1, at=at)
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                "WHERE telegram_bot_id = %s", (active_bot,))
    assert_error(_post_confirm(client, active["code"], code), 409, "bot_required")
    assert_error(_post_prepare(client, active["code"]), 409, "bot_required")
    assert _prepared_columns(sql, active) == (_sha256(code), at + CONFIRMATION, DEFAULT_HOURS, 1)
    assert _sends(sql, active) == []
    sql.execute("UPDATE bots SET state = 'registered', access_problem = NULL WHERE telegram_bot_id = %s",
                (active_bot,))
    _confirm(client, active, code, 1)


def test_close_clears_the_preparation(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """O encerramento limpa as quatro colunas do preparo, no mesmo UPDATE que grava `closed`:
    o encerramento de um oferecimento com preparo pendente limpa as quatro colunas; a
    confirmação com o código → 409 `invalid_state` com `state: closed` (o estado vem antes do código) e
    um novo preparo também; nada é enfileirado."""
    offering, _ = _offering(client, bot_api, "envio-encerra-preparo")
    _import(client, offering, [_student("Encerrado")])
    at = clock.now()
    code = _prepare(client, offering, 1, at=at)
    assert _prepared_columns(sql, offering) == (_sha256(code), at + CONFIRMATION, DEFAULT_HOURS, 1)
    _close(client, offering["code"])
    assert _prepared_columns(sql, offering) == CLEARED
    assert_error(_post_confirm(client, offering["code"], code), 409, "invalid_state", state="closed")
    assert_error(_post_prepare(client, offering["code"]), 409, "invalid_state", state="closed")
    assert _sends(sql, offering) == []


def test_authentication_and_permission(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Permissão `offerings:write` nas duas fases; ordem da API base (401 → 400
    → 403 → regras): sem token ou com token desconhecido → 401; um token só com `offerings:read`,
    `people:write` e `bots:manage` → 403 com `permission = offerings:write`, inclusive num oferecimento
    inexistente (a permissão vem antes das regras), mas o pedido inválido é 400 antes do 403; nada muda.
    Um token com `offerings:write` prepara e confirma, e os eventos levam o `token_id` dele."""
    offering, _ = _offering(client, bot_api, "envio-permissao")
    _import(client, offering, [_student("Permissao")])
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "people:write", "bots:manage"],
                             label="sem escrita de oferecimento")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    before = _nothing_sent(sql, offering)
    ref = offering["code"]

    for token in (None, "cq_" + "z" * 43):
        assert_error(_post_prepare(client, ref, token=token), 401, "unauthenticated")
        assert_error(_post_confirm(client, ref, "qualquer-codigo", token=token), 401, "unauthenticated")
    for target in (ref, "mc999-inexistente"):
        assert_error(_post_prepare(client, target, token=reader), 403, "forbidden", permission="offerings:write")
        assert_error(_post_confirm(client, target, "qualquer-codigo", token=reader), 403, "forbidden",
                     permission="offerings:write")
    assert_error(_post_prepare(client, ref, {"valid_hours": 0}, token=reader), 400, "invalid_request",
                 fields=[("valid_hours", "out_of_range")])
    assert_error(_post_confirm(client, ref, "", token=reader), 400, "invalid_request",
                 fields=[("confirmation_code", "too_short")])
    assert _nothing_sent(sql, offering) == before

    at = clock.now()
    cursor = _last_seq(sql)
    code = _prepare(client, offering, 1, token=writer, at=at)
    _confirm(client, offering, code, 1, token=writer)
    actor = writer_body["token_id"]
    assert _student_events(client, cursor) == [_prepared_event(offering, at, 1, DEFAULT_HOURS, actor),
                                               _queued_event(offering, at, 1, actor)]


# ---------------------------------------------------------------------------------------------
# Elegibilidade pelos estados da adesão
# ---------------------------------------------------------------------------------------------


def test_eligible_students_follow_the_adhesion_states(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                      bot_worker_thread: Any, stop_one: Any, settings: Any,
                                                      smtp: Any, mail_channel: Any) -> None:
    """Elegibilidade do envio: elegíveis sem convite, com convite vencido ou revogado sem substituto e com falha no
    envio; validade contada do envio; predicado único da adesão e estados da adesão;
    um convite `pending` de um envio que não está `sent` não conta como válido; estados
    pelos fluxos e pelos mecanismos declarados, nunca `UPDATE invite_sends`.

    Montagem, num só oferecimento, com o `FakeClock` parado até o fim (exceto onde dito):
    - X: lote 0 com validade de 1 h, entregue (depois vence: `invite_lapsed`);
    - L, T, V: lote A (168 h), entregues; T apresenta o termo (`/start`: `terms_presented`), L apresenta
      e aceita (`/aceito`: `linked`), V fica com o convite (`invite_valid`);
    - F, E1, E2, S: lote B (o preparo conta só os 4: X, V, T e L não são elegíveis). F recebe 550 no
      RCPT (`failed`/`recipient_refused`: `send_failed`); a tarefa de E1 tem a posse esgotada antes da
      primeira transação do envio (sem convite: `send_failed`); E2 cai com o `Crash` do `ScriptedVerificationChannel` depois da
      primeira transação (convite `pending` de um envio que não está `sent`) e a posse dele é esgotada
      (`send_failed`); S recebe 451 no RCPT (falha temporária: a tarefa volta a `pending` e o envio
      continua `pending`: `sending`);
    - com os workers parados, um preparo conta 3 (F, E1, E2: o convite de E2 não o tira do lote);
      depois o relógio passa 1 h (o convite de X vence), N é importado (`no_invite`) e o preparo final
      conta 5 (X, F, E1, E2 e N), e a confirmação enfileira exatamente esses 5, em ordem de matrícula.
    Todos os e-mails entregues são conferidos por inteiro."""
    not_before = datetime.now(timezone.utc)
    username = "cq_envio_elegiveis_bot"
    offering, bot = _offering(client, bot_api, "envio-elegiveis", username)
    x, lnk, trm, val = _student("Vence"), _student("Vinculado"), _student("Termo"), _student("Valido")
    fail, ex1, ex2, snd = _student("Falha"), _student("Posse Antes"), _student("Posse Depois"), _student("Enviando")
    last = _student("Sem Convite")
    t0 = clock.now()
    channel = _DefaultChannel()

    # Lote 0: X, com validade de 1 h.
    _import(client, offering, [x])
    code = _prepare(client, offering, 1, valid_hours=1, at=t0)
    _confirm(client, offering, code, 1, 1)
    w1 = _worker(bot_worker_thread, settings, channel, "worker-envio-1", mail_channel)
    _wait(bot_api, lambda: _send_states(sql, offering) == {_pseudo(sql, x): "sent"})

    # Lote A: L, T e V (X tem convite válido e não entra).
    _import(client, offering, [lnk, trm, val])
    code = _prepare(client, offering, 3, at=t0)
    _confirm(client, offering, code, 3)
    batch_a = [row[2] for row in _sends(sql, offering)][1:]
    _wait(bot_api, lambda: all(state == "sent" for state in _send_states(sql, offering).values())
          and len(_send_states(sql, offering)) == 4)
    by_pseudo = {_pseudo(sql, s): s for s in (x, lnk, trm, val)}
    expected_mail = [(x.email, t0 + timedelta(hours=1))] + [
        (by_pseudo[p].email, t0 + timedelta(hours=DEFAULT_HOURS)) for p in batch_a]
    smtp.wait_for(lambda: len(smtp.messages()) >= 4, timeout=PROCESS_TIMEOUT)
    codes = _emails(smtp, username, expected_mail, not_before)

    t_user, l_user = make_user(3001), make_user(3002)
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, t_user, codes[trm.email])) == "student_terms_presented"
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, l_user, codes[lnk.email])) == "student_terms_presented"
    assert _outcome(bot_api, sql, bot, bot_api.push_text(bot, l_user, "/aceito", bot_command=True)) == "student_joined"
    _outbox_settled(bot_api, sql)
    assert stop_one(w1) != float("inf")

    # Lote B: F, E1, E2 e S (só eles são elegíveis).
    _import(client, offering, [fail, ex1, ex2, snd])
    by_pseudo.update({_pseudo(sql, s): s for s in (fail, ex1, ex2, snd)})
    code = _prepare(client, offering, 4, at=t0)
    _confirm(client, offering, code, 4)
    batch_b = [(row[0], row[2]) for row in _sends(sql, offering)][4:]
    assert sorted(p for _, p in batch_b) == sorted(_pseudo(sql, s) for s in (fail, ex1, ex2, snd))
    send_of = {by_pseudo[p].ra: sid for sid, p in batch_b}
    _exhaust_lease(sql, settings, clock, send_of[ex1.ra])
    smtp.fail("rcpt", 550, when=lambda ctx: ctx.argument == fail.email, times=None)
    smtp.fail("rcpt", 451, when=lambda ctx: ctx.argument == snd.email, times=None)
    steps: list[Any] = []
    for _sid, pseudo in batch_b:
        if by_pseudo[pseudo] is ex1:
            continue
        if by_pseudo[pseudo] is ex2:
            steps.append(Crash())
            break
        steps.append(DELEGATE)
    w2 = _worker(bot_worker_thread, settings, channel, "worker-envio-2",
                 ScriptedVerificationChannel(steps, delegate=mail_channel))
    _wait(bot_api, lambda: not w2.is_alive())
    assert isinstance(w2.error, Crash), f"esperada a queda por Crash, veio {w2.error!r}"
    _exhaust_lease(sql, settings, clock, send_of[ex2.ra])
    w3 = _worker(bot_worker_thread, settings, channel, "worker-envio-3", mail_channel)

    def settled() -> bool:
        return (_task_of(sql, send_of[fail.ra])[0] == "failed"
                and _task_of(sql, send_of[ex1.ra])[0] == "failed"
                and _task_of(sql, send_of[ex2.ra])[0] == "failed"
                and _task_of(sql, send_of[snd.ra]) == ("pending", 1, "RetryTaskLater"))

    _wait(bot_api, settled)
    assert stop_one(w3) != float("inf")
    assert _task_of(sql, send_of[ex1.ra]) == ("failed", settings.max_attempts, LEASE_EXHAUSTED)
    assert _task_of(sql, send_of[ex2.ra]) == ("failed", settings.max_attempts, LEASE_EXHAUSTED)
    sends = {row[2]: row[3:10] for row in _sends(sql, offering)}
    assert sends == {
        _pseudo(sql, x): ("batch", 1, "sent", None, None, None, 1),
        _pseudo(sql, lnk): ("batch", DEFAULT_HOURS, "sent", None, None, None, 1),
        _pseudo(sql, trm): ("batch", DEFAULT_HOURS, "sent", None, None, None, 1),
        _pseudo(sql, val): ("batch", DEFAULT_HOURS, "sent", None, None, None, 1),
        _pseudo(sql, fail): ("batch", DEFAULT_HOURS, "failed", "recipient_refused", None,
                             "SMTPRecipientsRefused", 1),
        _pseudo(sql, ex1): ("batch", DEFAULT_HOURS, "pending", None, None, None, 0),
        _pseudo(sql, ex2): ("batch", DEFAULT_HOURS, "pending", None, None, None, 1),
        _pseudo(sql, snd): ("batch", DEFAULT_HOURS, "pending", None, None, None, 1),
    }
    invites = sql.execute(
        """
        SELECT p.pseudo_id::text, i.state, i.expires_at, i.presented_terms_version, i.code_sha256
          FROM invites i JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id
         ORDER BY i.id
        """).fetchall()
    assert {row[0]: row[1:4] for row in invites} == {
        _pseudo(sql, x): ("pending", t0 + timedelta(hours=1), None),
        _pseudo(sql, lnk): ("used", t0 + timedelta(hours=DEFAULT_HOURS), 1),
        _pseudo(sql, trm): ("pending", t0 + timedelta(hours=DEFAULT_HOURS), 1),
        _pseudo(sql, val): ("pending", t0 + timedelta(hours=DEFAULT_HOURS), None),
        _pseudo(sql, fail): ("revoked", t0 + timedelta(hours=DEFAULT_HOURS), None),
        _pseudo(sql, ex2): ("pending", t0 + timedelta(hours=DEFAULT_HOURS), None),
        _pseudo(sql, snd): ("pending", t0 + timedelta(hours=DEFAULT_HOURS), None),
    }
    assert len(invites) == 7
    assert {row[4] for row in invites if row[0] in {_pseudo(sql, s) for s in (x, lnk, trm, val)}} == {
        _sha256(codes[s.email]) for s in (x, lnk, trm, val)}
    assert len(smtp.messages()) == 4, "nenhum e-mail além dos 4 entregues (F e S recusados, E2 caiu antes)"

    # Com os workers parados: F, E1 e E2 são elegíveis (o convite pendente de E2 não conta).
    code = _prepare(client, offering, 3, at=t0)

    # O convite de X vence; N entra sem convite; o preparo final conta X, F, E1, E2 e N.
    clock.advance(3600 + 60)
    t1 = clock.now()
    _import(client, offering, [last])
    cursor = _last_seq(sql)
    code = _prepare(client, offering, 5, at=t1)
    _confirm(client, offering, code, 5)
    eligible = {_pseudo(sql, s) for s in (x, fail, ex1, ex2, last)}
    enrollments = [(eid, pseudo) for eid, pseudo in _student_enrollments(sql, offering) if pseudo in eligible]
    new_sends = _sends(sql, offering)[8:]
    assert new_sends == [_pending_send_row(row[0], eid, pseudo, DEFAULT_HOURS, t1)
                         for row, (eid, pseudo) in zip(new_sends, enrollments)]
    assert len(new_sends) == 5
    assert _tasks(sql, [row[0] for row in new_sends]) == [_pending_task_row(row[0], t1) for row in new_sends]
    assert _student_events(client, cursor) == [_prepared_event(offering, t1, 5, DEFAULT_HOURS),
                                               _queued_event(offering, t1, 5)]
    assert channel.calls == []


def test_confirm_never_exceeds_the_prepared_count(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                  bot_worker_thread: Any, stop_one: Any, settings: Any,
                                                  smtp: Any, mail_channel: Any) -> None:
    """Nunca mais que o número contado no preparo; um aluno que ficou elegível depois do
    preparo pode entrar no lugar de outro; o `invite_eligible_count` é limite de
    contagem, aos elegíveis no momento da confirmação, em ordem de `enrollments.id`; e (validade
    contada do envio): A recebe um convite de 1 h (entregue e conferido); 55 min depois, B e C são
    importados e o preparo conta 2 (o convite de A ainda vale); 6 min depois, o convite de A venceu e A
    voltou a ser elegível, mas a confirmação enfileira só 2: A (a menor matrícula) e o primeiro de B e C
    em ordem de `enrollments.id`."""
    not_before = datetime.now(timezone.utc)
    username = "cq_envio_limite_bot"
    offering, _ = _offering(client, bot_api, "envio-limite", username)
    first, b, c = _student("Primeira"), _student("Segunda"), _student("Terceira")
    t0 = clock.now()
    _import(client, offering, [first])
    code = _prepare(client, offering, 1, valid_hours=1, at=t0)
    _confirm(client, offering, code, 1, 1)
    channel = _DefaultChannel()
    worker = _worker(bot_worker_thread, settings, channel, "worker-envio-limite", mail_channel)
    _wait(bot_api, lambda: _send_states(sql, offering) == {_pseudo(sql, first): "sent"})
    smtp.wait_for(lambda: smtp.messages(), timeout=PROCESS_TIMEOUT)
    _emails(smtp, username, [(first.email, t0 + timedelta(hours=1))], not_before)
    assert stop_one(worker) != float("inf")

    clock.set(t0 + timedelta(minutes=55))
    t1 = clock.now()
    _import(client, offering, [b, c])
    code = _prepare(client, offering, 2, at=t1)
    clock.set(t0 + timedelta(minutes=61))
    t2 = clock.now()
    cursor = _last_seq(sql)
    _confirm(client, offering, code, 2)

    enrollments = _student_enrollments(sql, offering)
    assert enrollments[0][1] == _pseudo(sql, first)
    new_sends = _sends(sql, offering)[1:]
    assert new_sends == [_pending_send_row(row[0], eid, pseudo, DEFAULT_HOURS, t2)
                         for row, (eid, pseudo) in zip(new_sends, enrollments[:2])]
    assert len(new_sends) == 2
    assert _tasks(sql, [row[0] for row in new_sends]) == [_pending_task_row(row[0], t2) for row in new_sends]
    assert _student_events(client, cursor) == [_queued_event(offering, t2, 2)]
    assert len(smtp.messages()) == 1
    assert channel.calls == []
