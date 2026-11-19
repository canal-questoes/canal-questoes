"""Falhas do envio do convite por e-mail e montagem do canal no worker.

Cobre a ordem da T1 (o envio superado é cancelado antes de "não configurado"), a repetição, a classificação
das falhas, o canal sem e-mail, a redação e os eventos; o erro do servidor de e-mail que repete o endereço e
o e-mail da lista com parte local não ASCII; a classificação das falhas do adaptador, os envios concorrentes e
a montagem do canal.

Contratos: a tarefa `invite.email` (T1, envio, T2, fim de cada execução,
casos fechados em falha e a **tabela única** de classificação das falhas do adaptador), a configuração
(`MailSettings` e a montagem do canal, com as mensagens exatas de `ConfigError`), os eventos e
o e-mail; os registros `worker.mail_disabled`, `invite.email` e `config.invalid`;
as interfaces internas (`build_verification(env, settings)`, `run_worker(…, verification=…)`)
e o modo de testar deste arquivo (SMTP falso, tempo, exceção das tentativas no teste da espera crescente).

Tudo passa pelo caminho real: a API importa a lista, prepara e confirma o lote; um worker com bots
(`bot_worker_thread`, `FakeClock` do teste, `worker_id` fixo) roda a tarefa `invite.email` com o
adaptador real (`SmtpVerificationChannel`) contra o SMTP falso (a guarda de rede ligada pela fixture
`smtp`); no caso de outra `Exception` (que vale como falha temporária), o canal é o
`ScriptedVerificationChannel`, que levanta uma `Exception` comum (num dos casos, só na 1ª
execução, e depois delega ao adaptador real). As esperas são com prazo (tempo real, threads do worker),
nunca `sleep` como prova; os horários vêm do `FakeClock` (`iso`); ids internos, `seq` e códigos nunca
como literal. Os eventos são lidos pela
API (`events.list`, esquema congelado) depois de um cursor, por inteiro e na ordem de `seq`.

Os registros de log são conferidos como na API base: por **inclusão** do subconjunto exigido
(campos extras permitidos), com a contagem e a ordem exatas; cada registro é varrido inteiro atrás de
endereço, código e senha. O e-mail capturado é comparado por igualdade no envelope, nos cabeçalhos
(`Date` e `Message-ID` por forma) e no texto decodificado (o corpo fixo do e-mail seguido da quebra
de linha final que o DATA exige).

Os casos da montagem do canal chamam `cq_server.__main__.build_verification` em processo (com
`capture_logs` e `capfd`), e dois casos sobem o `cq-server worker` num subprocesso (`spawn_worker`).
"""

from __future__ import annotations

import dataclasses
import email.utils
import json
import re
import secrets as stdlib_secrets
import time
import types
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql as pgsql

from cq_server.config import ConfigError, Settings
from cq_server.ports.verification import Delivered, VerificationMessage
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    import_students,
    list_events,
    prepare_invites,
    register_bot,
    resend_invites,
    send_invites,
)
from tests.helpers.canary import STUDENT_B, STUDENT_PREFIXES, find_canary, find_secrets
from tests.helpers.channels import DELEGATE, ScriptedVerificationChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import Captured, capture_logs
from tests.helpers.processes import spawn_worker
from tests.helpers.secrets import find_invite_codes, invite_code_digest, mail_secrets, remember_secret
from tests.helpers.smtp import SENDER

# Disciplina, período e fuso dos oferecimentos deste arquivo (valores neutros, sem identidade: a
# redação não os muda).
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"

WORKER = "worker-falhas-email"
PROCESS_TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
SUBPROCESS_TIMEOUT = 60.0  # subida de um processo Python novo com o pacote inteiro (como em oferecimentos e bots)
DEFAULT_HOURS = 168
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
SMTP_TIMEOUT = 2  # `timeout_seconds` do `smtp_settings` do conftest

# Tipos de evento lidos nos casos do envio: os 8 do vínculo do aluno e o `task.failed` da API base.
STUDENT_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                       "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                       "student.invite_revoked", "student.joined")
SEND_EVENT_TYPES = STUDENT_EVENT_TYPES + ("task.failed",)

# remetente, assunto e corpo do e-mail do convite, por extenso.
FROM_HEADER = f"Canal de Questões <{SENDER}>"
SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
EMAIL_HEADER_NAMES = ("from", "to", "subject", "date", "message-id", "mime-version", "content-type",
                      "content-transfer-encoding")

# mensagens exatas de `ConfigError` (sem o valor recebido).
MSG_PORT = "CQ_SMTP_PORT deve ser um número inteiro de 1 a 65535."
MSG_SECURITY = "CQ_SMTP_SECURITY deve ser starttls, tls ou none."
MSG_PLAINTEXT = "CQ_SMTP_SECURITY=none só é aceito com CQ_SMTP_HOST em 127.0.0.1, localhost ou ::1."
MSG_HOST = "CQ_SMTP_HOST deve ser um nome de host ou endereço IP, sem espaços."
MSG_PAIR = "CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD devem vir juntos."
MSG_ASCII = "CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD só aceitam caracteres ASCII visíveis e espaço."
MSG_FROM = "CQ_SMTP_FROM deve ser um endereço de e-mail ASCII válido."
MSG_TIMEOUT = "CQ_SMTP_TIMEOUT_SECONDS deve ser um número finito maior que zero."
MSG_LEASE = "CQ_SMTP_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."

# Senhas fictícias conhecidas: procuradas, com as formas do fio, em logs e em fd 1 e 2.
KNOWN_USER = "cq-envio-montagem"
KNOWN_PASSWORD = "Senha-Ficticia-Montagem-7Q2w"
IGNORED_PASSWORD = "Senha-Ignorada-Sem-Host-3Kp9"
PROCESS_PASSWORD = "Senha-Do-Processo-Worker-8Zx4"
PORT_SENTINEL = "porta-sentinela-invalida"

# O `Settings` dos testes em processo (posse de 30 s, como a fixture `settings`), sem banco.
LEASE_30 = Settings(database_url="postgresql://cq@127.0.0.1:9/cq", admin_token=ADMIN_TOKEN,
                    lease_seconds=30, max_attempts=3, backoff_base_seconds=2, backoff_max_seconds=60,
                    poll_interval_seconds=0.05)


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


def _row(name: str, ra: str, email_address: str) -> dict[str, str]:
    return {"name": name, "ra": ra, "email": email_address}


def _active_offering(client: Any, bot_api: Any, code: str, username: str) -> _Offering:
    """Oferecimento ativo com um bot novo da Bot API falsa (registrado e com acesso)."""
    created = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    return _Offering(created, bot, username)


def _queue_batch(client: Any, clock: Any, offering: _Offering, rows: list[dict[str, str]]) -> None:
    """Importa `rows` (pessoas novas), prepara e confirma o lote, com as três respostas exatas.
    """
    n = len(rows)
    imported = import_students(client, ADMIN_TOKEN, offering.ref, rows)
    assert imported == {"offering": offering.summary, "received": n, "people_created": n, "people_reused": 0,
                        "enrollments_created": n, "already_enrolled": 0}
    now = clock.now()
    prepared = prepare_invites(client, ADMIN_TOKEN, offering.ref)
    code = prepared["confirmation_code"]
    assert CONFIRMATION_RE.fullmatch(code), code
    assert prepared == {"offering": offering.summary, "eligible": n, "valid_hours": DEFAULT_HOURS,
                        "confirmation_code": code, "expires_at": iso(now + CONFIRMATION)}
    queued = send_invites(client, ADMIN_TOKEN, offering.ref, code)
    assert queued == {"offering": offering.summary, "queued": n, "valid_hours": DEFAULT_HOURS}


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


def _send(send_id: int, person: str, *, state: str, created_at: datetime, attempts: int,
          finished_at: datetime | None = None, failure: str | None = None, cancel_reason: str | None = None,
          error_type: str | None = None, origin: str = "batch") -> dict:
    return {"id": send_id, "person": person, "origin": origin, "valid_hours": DEFAULT_HOURS, "state": state,
            "failure": failure, "cancel_reason": cancel_reason, "error_type": error_type, "attempts": attempts,
            "created_at": created_at, "finished_at": finished_at}


INVITE_COLUMNS = ("id", "person", "invite_send_id", "code_sha256", "state", "expires_at", "created_at",
                  "used_at", "revoked_at")


def _invites(sql: psycopg.Connection, offering: _Offering) -> list[dict]:
    """Os convites de aluno do oferecimento, em ordem de `id`."""
    rows = sql.execute(
        """
        SELECT i.id, p.pseudo_id::text, i.invite_send_id, i.code_sha256, i.state, i.expires_at,
               i.created_at, i.used_at, i.revoked_at
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s AND e.role = 'student'
         ORDER BY i.id
        """, (uuid.UUID(offering.created["offering_id"]),)).fetchall()
    return [dict(zip(INVITE_COLUMNS, row)) for row in rows]


def _invite(row: dict, *, state: str, created_at: datetime, revoked_at: datetime | None = None) -> dict:
    """O convite esperado a partir da linha lida (o `id`, o envio e o resumo vêm da própria linha)."""
    return {**row, "state": state, "expires_at": created_at + timedelta(hours=DEFAULT_HOURS),
            "created_at": created_at, "used_at": None, "revoked_at": revoked_at}


TASK_COLUMNS = ("id", "payload", "idempotency_key", "state", "attempts", "available_at", "lease_owner",
                "lease_until", "last_error", "finished_at")


def _invite_tasks(sql: psycopg.Connection) -> list[dict]:
    """As tarefas `invite.email`, em ordem de `id`."""
    rows = sql.execute(
        "SELECT id, payload, idempotency_key, state, attempts, available_at, lease_owner, lease_until, "
        "last_error, finished_at FROM tasks WHERE task_type = 'invite.email' ORDER BY id").fetchall()
    return [dict(zip(TASK_COLUMNS, row)) for row in rows]


def _task(task_id: int, send_id: int, *, state: str, attempts: int, available_at: datetime,
          last_error: str | None, finished_at: datetime | None = None) -> dict:
    """A tarefa esperada: payload e chave só com o id do envio, sem posse."""
    return {"id": task_id, "payload": {"invite_send_id": send_id}, "idempotency_key": f"invite.email:{send_id}",
            "state": state, "attempts": attempts, "available_at": available_at, "lease_owner": None,
            "lease_until": None, "last_error": last_error, "finished_at": finished_at}


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int, types: tuple[str, ...] = SEND_EVENT_TYPES) -> list[dict]:
    """Os eventos dos tipos dados depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: datetime, data: dict, actor: str | None = None) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _student_event(type_: str, offering: _Offering, person: str, at: datetime, **data: Any) -> dict:
    return _event(type_, at, {"offering_id": offering.created["offering_id"], "person": person, **data})


def _task_failed(task_id: int, attempts: int, at: datetime) -> dict:
    return _event("task.failed", at, {"task_id": task_id, "task_type": "invite.email", "attempts": attempts,
                                      "error": "PermanentTaskError"})


def _invite_email_records(cap: Captured) -> list[tuple[Any, Any]]:
    """Os registros `invite.email`, em ordem, como (`invite_send_id`, `result`); cada um,
    sem o `time`, é exatamente `{level: INFO, logger: cq_server.worker, event, invite_send_id, result}`
    (nenhuma chave a mais, como nos registros do envio: nada de `error_type` nem texto do servidor)."""
    records = cap.with_event("invite.email")
    for record in records:
        rest = {key: value for key, value in record.items() if key != "time"}
        expected = {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email",
                    "invite_send_id": record.get("invite_send_id"), "result": record.get("result")}
        assert rest == expected, record
    return [(r.get("invite_send_id"), r.get("result")) for r in records]


def _task_rows_text(sql: psycopg.Connection) -> str:
    """O texto de todas as tarefas `invite.email` (payload, chave e `last_error`), para as varreduras."""
    return json.dumps([[row["payload"], row["idempotency_key"], row["last_error"]] for row in _invite_tasks(sql)],
                      ensure_ascii=False)


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


def _assert_email(message: Any, *, address: str, subject: str, text: str, not_before: datetime,
                  not_after: datetime) -> None:
    """Um e-mail capturado pelo SMTP falso: envelope, cabeçalhos (`Date` em UTC entre
    `not_before` e `not_after`, tempo real, e `Message-ID` com o domínio do remetente, por forma) e
    texto decodificado exatos; nada acima de 127 no bruto (sem caminho SMTPUTF8)."""
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
    assert headers == {"from": FROM_HEADER, "to": address, "subject": subject, "mime-version": "1.0",
                       "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    # O DATA sempre termina em CRLF, que o texto decodificado mostra como a quebra de linha final.
    assert message.text == text + "\n"


def _assert_no_leak(texts: dict[str, str], *, address: str | None = None, digests: list[str] = (),
                    passwords: list[str] = ()) -> None:
    """Nenhum texto de `texts` (superfície → texto) traz o endereço (por subcadeia, sem distinguir
    caixa), um código de convite cujo resumo está em `digests` ou uma senha e as suas formas do fio."""
    for surface, text in texts.items():
        if address is not None:
            assert address.casefold() not in text.casefold(), f"endereço em {surface}"
        if digests:
            assert find_invite_codes(text, digests) == [], f"código de convite em {surface}"
        if passwords:
            assert find_secrets(text, list(passwords)) == [], f"senha em {surface}"


def _synthetic_controls(smtp: Any) -> None:
    """Controles positivos dos detectores, com valores sintéticos: um código de convite colado a letras
    é achado pelo resumo, e a senha do SMTP falso é achada por `mail_secrets`."""
    code = stdlib_secrets.token_urlsafe(16)
    assert find_invite_codes(f"prefixo{code}sufixo", [invite_code_digest(code)]) != []
    assert find_secrets(f"log com {smtp.credentials.password} no meio", mail_secrets(smtp)) != []


def _processed(cap: Captured, task_id: int) -> int:
    """Quantas execuções da tarefa `task_id` já terminaram: os registros `task.processed` da API base com o
    `id` dela (saem depois do tratador e do registro `invite.email` da execução, no mesmo laço)."""
    return [record.get("id") for record in cap.with_event("task.processed")].count(task_id)


def _link(offering: _Offering, code: str) -> str:
    """O link do convite, por extenso."""
    return f"https://t.me/{offering.username}?start={code}"


LINK_IN_TEXT = re.compile(r"https://t\.me/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])")


def _codes_sent_to(channel: ScriptedVerificationChannel, offering: _Offering) -> list[str]:
    """O código de cada mensagem entregue ao canal roteirizado, em ordem (um link do bot do oferecimento
    por mensagem); cada código entra nos segredos conhecidos das varreduras do arnês."""
    codes = []
    for call in channel.calls:
        found = LINK_IN_TEXT.findall(call.body)
        assert len(found) == 1 and found[0][0] == offering.username, "mensagem sem o link do convite"
        remember_secret(found[0][1])
        codes.append(found[0][1])
    return codes


# Sentinela do texto da `Exception` comum levantada pelo canal roteirizado.
EXCEPTION_SENTINEL = "Sentinela-Excecao-Do-Canal-6Hj3"


def _channel_error(address: str, step: int) -> RuntimeError:
    """Uma `Exception` comum (não é o `Crash`, que é `BaseException`) cujo texto traz o endereço do aluno
    e a sentinela: armadilha para quem registra ou grava a mensagem da exceção (as exceções
    saem só com tipo e posição)."""
    error = RuntimeError(f"{EXCEPTION_SENTINEL}-{step}: o servidor recusou <{address}>")
    assert isinstance(error, Exception) and address in str(error) and EXCEPTION_SENTINEL in str(error)
    return error


def _assert_no_exception_text(texts: dict[str, str]) -> None:
    """Nenhum texto de `texts` (superfície → texto) traz a sentinela do texto da exceção do canal."""
    for surface, text in texts.items():
        assert EXCEPTION_SENTINEL.casefold() not in text.casefold(), f"texto da exceção em {surface}"


# ---------------------------------------------------------------------------------------------
# Falha temporária e espera crescente até o esgotamento
# ---------------------------------------------------------------------------------------------


def test_temporary_refusal_retries_and_leaves_a_single_valid_invite(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any, capfd: Any) -> None:
    """4xx → temporária, repetida com a espera crescente; a repetição gera outro
    código e invalida o anterior; só o último vale; a tarefa (T2, `TemporaryFailure`
    antes da última tentativa → `RetryTaskLater(0)`; a T1 seguinte revoga o convite anterior com
    `student.invite_revoked {replaced}`) e o fim de cada execução; fixture `settings` (3
    tentativas): com o RCPT recusado uma vez com 450, a 1ª execução deixa a tarefa `pending` com
    `last_error = "RetryTaskLater"` e a espera de 2 s, o envio `pending` com `attempts = 1`, um convite
    `pending` cujo código nunca foi entregue e nenhum e-mail nem evento; depois da espera, a 2ª execução
    revoga esse convite (`replaced`), gera outro, entrega o e-mail exato e grava `sent`
    com `attempts = 2` e `student.invite_sent`; sobra um único convite válido, o do e-mail; os registros
    `invite.email` são `retry` e `sent`; nenhum log, fd, evento ou tarefa traz o endereço ou algum dos
    dois códigos (redação, com controle positivo no e-mail entregue)."""
    offering = _active_offering(client, bot_api, "falha-temporaria", "cq_falha_temporaria_bot")
    address = "aluna.espera@turma.example.org"
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Espera Curta", "5600000101", address)])
    person = _pseudo(sql, "5600000101")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)
    smtp.fail("rcpt", 450)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=mail_channel)
        _wait(lambda: _invite_tasks(sql)[0]["attempts"] == 1 and _invite_tasks(sql)[0]["state"] == "pending"
              and _invite_email_records(cap) == [(sid, "retry")], "a 1ª execução não terminou em retry")
        assert _invite_tasks(sql) == [_task(task["id"], sid, state="pending", attempts=1,
                                            available_at=t1 + timedelta(seconds=2), last_error="RetryTaskLater")]
        assert _sends(sql, offering) == [_send(sid, person, state="pending", created_at=t_confirm, attempts=1)]
        [first] = _invites(sql, offering)
        assert first == _invite({**first, "person": person, "invite_send_id": sid}, state="pending", created_at=t1)
        assert smtp.messages() == []
        assert len(smtp.connections()) == 1
        assert _events_after(client, cursor) == []

        not_before = _real_now()
        clock.advance(2)
        t2 = clock.now()
        _wait(lambda: _invite_tasks(sql)[0]["state"] == "done"
              and _invite_email_records(cap) == [(sid, "retry"), (sid, "sent")], "a 2ª execução não entregou")
        not_after = _real_now()

    first_after, second = _invites(sql, offering)
    assert first_after == _invite(first, state="revoked", created_at=t1, revoked_at=t2)
    assert second == _invite({**second, "person": person, "invite_send_id": sid}, state="pending", created_at=t2)
    assert _sends(sql, offering) == [_send(sid, person, state="sent", created_at=t_confirm, attempts=2,
                                           finished_at=t2)]
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="done", attempts=2,
                                        available_at=t1 + timedelta(seconds=2), last_error="RetryTaskLater",
                                        finished_at=t2)]

    [message] = smtp.messages()
    [link] = smtp.invite_links()
    assert (link.bot_username, link.rcpt_to, link.acknowledged) == (offering.username, (address,), True)
    assert invite_code_digest(link.code) == second["code_sha256"]
    _assert_email(message, address=address, subject=SUBJECT,
                  text=_invite_body(link.url, _validity(t2 + timedelta(hours=DEFAULT_HOURS))),
                  not_before=not_before, not_after=not_after)
    assert link.url == f"https://t.me/{offering.username}?start={link.code}"
    assert len(smtp.connections()) == 2
    # Um único convite válido, e é o do e-mail entregue.
    assert [row["code_sha256"] for row in _invites(sql, offering) if row["state"] == "pending"] == [
        invite_code_digest(link.code)]

    assert _events_after(client, cursor) == [
        _student_event("student.invite_revoked", offering, person, t2, origin="batch", revoke_reason="replaced"),
        _student_event("student.invite_sent", offering, person, t2, origin="batch",
                       expires_at=iso(t2 + timedelta(hours=DEFAULT_HOURS))),
    ]
    assert _invite_email_records(cap) == [(sid, "retry"), (sid, "sent")]

    out, err = capfd.readouterr()
    digests = [first["code_sha256"], second["code_sha256"]]
    events_text = json.dumps(_events_after(client, cursor), ensure_ascii=False)
    _assert_no_leak({"logs": cap.text, "stdout": out, "stderr": err, "eventos": events_text,
                     "tarefas": _task_rows_text(sql)}, address=address, digests=digests,
                    passwords=mail_secrets(smtp))
    # Controle positivo: o detector acha o código no texto do e-mail entregue.
    assert find_invite_codes(message.text, digests) != []
    _synthetic_controls(smtp)


def test_temporary_refusals_back_off_until_attempts_are_exhausted(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any) -> None:
    """Tentativas esgotadas (5 tentativas, 30 segundos de espera ao todo; esperas de
    2, 4, 8 e 16 s; só este caso usa `dataclasses.replace(settings, max_attempts=5)`, com a
    posse de 30 s e o prazo do SMTP de 2 s); a tarefa (T2: `TemporaryFailure` na última tentativa
    → falha registrada `attempts_exhausted`, com o `error_type` da última `TemporaryFailure`; a revogação
    do convite da tentativa coberta pelo `student.invite_failed`) e seus desfechos: com o RCPT sempre recusado
    com 450, os `available_at` são +2, +4, +8 e +16 s no `FakeClock`; a 5ª execução, 30 s depois da 1ª,
    grava `failed`/`attempts_exhausted`, `error_type = "SMTPRecipientsRefused"`, `attempts = 5` (um
    código por execução), a tarefa `failed` com `last_error = "PermanentTaskError"` e o `task.failed` do
    log com o mesmo nome; cada T1 a partir da 2ª revoga o convite anterior (`replaced`), e o 5º convite é
    revogado sem evento; nenhum e-mail sai; os registros `invite.email` são 4 `retry` e um `failed`."""
    offering = _active_offering(client, bot_api, "falha-esgotada", "cq_falha_esgotada_bot")
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Tentativas", "5600000102",
                                                "aluna.tentativas@turma.example.org")])
    person = _pseudo(sql, "5600000102")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)
    smtp.fail("rcpt", 450, times=None)
    five = dataclasses.replace(settings, max_attempts=5)

    clock.advance(5)
    t1 = clock.now()
    starts = [t1]
    with capture_logs() as cap:
        _worker(bot_worker_thread, five, verification=mail_channel)
        for attempt in range(1, 5):
            _wait(lambda: _invite_tasks(sql)[0]["attempts"] == attempt  # noqa: B023 - espera deste passo
                  and _invite_tasks(sql)[0]["state"] == "pending"
                  and len(_invite_email_records(cap)) == attempt,
                  f"a execução {attempt} não terminou em retry")
            wait = timedelta(seconds=2 ** attempt)
            assert _invite_tasks(sql) == [_task(task["id"], sid, state="pending", attempts=attempt,
                                                available_at=starts[-1] + wait, last_error="RetryTaskLater")]
            assert _sends(sql, offering) == [_send(sid, person, state="pending", created_at=t_confirm,
                                                   attempts=attempt)]
            assert [row["state"] for row in _invites(sql, offering)] == ["revoked"] * (attempt - 1) + ["pending"]
            clock.advance(2 ** attempt)
            starts.append(clock.now())
        t5 = starts[-1]
        _wait(lambda: _invite_tasks(sql)[0]["state"] == "failed" and len(_invite_email_records(cap)) == 5,
              "a 5ª execução não registrou a falha")

    assert [start - t1 for start in starts] == [timedelta(seconds=s) for s in (0, 2, 6, 14, 30)]
    assert _sends(sql, offering) == [_send(sid, person, state="failed", created_at=t_confirm, attempts=5,
                                           finished_at=t5, failure="attempts_exhausted",
                                           error_type="SMTPRecipientsRefused")]
    invites = _invites(sql, offering)
    assert invites == [_invite({**row, "person": person, "invite_send_id": sid}, state="revoked",
                               created_at=starts[k], revoked_at=starts[k + 1] if k < 4 else t5)
                       for k, row in enumerate(invites)]
    assert len(invites) == 5
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="failed", attempts=5,
                                        available_at=starts[3] + timedelta(seconds=16),
                                        last_error="PermanentTaskError", finished_at=t5)]
    assert smtp.messages() == []
    assert len(smtp.connections()) == 5
    revoked = [_student_event("student.invite_revoked", offering, person, at, origin="batch",
                              revoke_reason="replaced") for at in starts[1:]]
    assert _events_after(client, cursor) == revoked + [
        _student_event("student.invite_failed", offering, person, t5, origin="batch", failure="attempts_exhausted"),
        _task_failed(task["id"], 5, t5),
    ]
    assert _invite_email_records(cap) == [(sid, "retry")] * 4 + [(sid, "failed")]
    _assert_no_leak({"logs": cap.text, "tarefas": _task_rows_text(sql)},
                    address="aluna.tentativas@turma.example.org", digests=[row["code_sha256"] for row in invites],
                    passwords=mail_secrets(smtp))


# ---------------------------------------------------------------------------------------------
# `Exception` comum que escapa do `send` (vale como falha temporária com o nome do tipo; injeção de falhas)
# ---------------------------------------------------------------------------------------------


def test_exception_escaping_the_channel_is_a_temporary_failure_and_the_retry_delivers(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any, capfd: Any) -> None:
    """Falha temporária, repetida com a espera crescente; a repetição gera outro código e
    invalida o anterior; redação; eventos; a tarefa (no envio, uma `Exception` que escape do `send`
    vale como `TemporaryFailure(type(exc).__name__)`, e só a `BaseException` passa: outra
    `Exception` vira falha temporária com o nome do tipo; na T2, `TemporaryFailure` antes da
    última tentativa → `RetryTaskLater(0)`; a T1 seguinte revoga o convite anterior com
    `student.invite_revoked {replaced}`) e o fim de cada execução; a porta de
    verificação (só uma `Exception` que escape do `send` vale como `TemporaryFailure` no tratador) e
    a injeção de falhas nos testes; as exceções seguem só com tipo e posição;
    fixture `settings` (3 tentativas): com o `ScriptedVerificationChannel` que levanta um `RuntimeError`
    comum (não o `Crash`), cujo texto traz o endereço do aluno e uma sentinela, e depois delega ao
    adaptador real, a 1ª execução deixa a tarefa `pending` com `last_error = "RetryTaskLater"` (não o nome
    da exceção, que é o que a fila gravaria se ela escapasse do tratador) e a espera de 2 s da fila, o envio
    `pending` com `attempts = 1`, um convite `pending`, o registro `invite.email` `retry`, nenhum evento e
    nenhuma conexão SMTP; depois da espera, a 2ª execução revoga esse convite (`replaced`), gera outro,
    entrega o e-mail exato pelo SMTP falso e grava `sent` com `attempts = 2` e
    `student.invite_sent`; as duas mensagens entregues ao canal são exatas, cada uma com o código do
    convite da sua execução; os registros `invite.email` são `retry` e `sent`; nem o texto da exceção nem
    o endereço nem os códigos aparecem em logs, fd 1 e 2, eventos ou tarefas (`last_error` inclusive)."""
    offering = _active_offering(client, bot_api, "falha-excecao", "cq_falha_excecao_bot")
    address = "aluna.excecao@turma.example.org"
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Excecao Canal", "5600000109", address)])
    person = _pseudo(sql, "5600000109")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)
    channel = ScriptedVerificationChannel([_channel_error(address, 1), DELEGATE], delegate=mail_channel)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=channel)
        _wait(lambda: _processed(cap, task["id"]) == 1, "a 1ª execução não terminou")
        assert _invite_tasks(sql) == [_task(task["id"], sid, state="pending", attempts=1,
                                            available_at=t1 + timedelta(seconds=2), last_error="RetryTaskLater")]
        assert _sends(sql, offering) == [_send(sid, person, state="pending", created_at=t_confirm, attempts=1)]
        [first] = _invites(sql, offering)
        assert first == _invite({**first, "person": person, "invite_send_id": sid}, state="pending", created_at=t1)
        assert _invite_email_records(cap) == [(sid, "retry")]
        assert _events_after(client, cursor) == []
        assert (len(channel.calls), channel.delegated) == (1, [])
        assert smtp.connections() == []
        assert smtp.messages() == []

        not_before = _real_now()
        clock.advance(2)
        t2 = clock.now()
        _wait(lambda: _processed(cap, task["id"]) == 2, "a 2ª execução não terminou")
        not_after = _real_now()

    first_after, second = _invites(sql, offering)
    assert first_after == _invite(first, state="revoked", created_at=t1, revoked_at=t2)
    assert second == _invite({**second, "person": person, "invite_send_id": sid}, state="pending", created_at=t2)
    assert _sends(sql, offering) == [_send(sid, person, state="sent", created_at=t_confirm, attempts=2,
                                           finished_at=t2)]
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="done", attempts=2,
                                        available_at=t1 + timedelta(seconds=2), last_error="RetryTaskLater",
                                        finished_at=t2)]

    codes = _codes_sent_to(channel, offering)
    assert channel.calls == [
        VerificationMessage(address=address, subject=SUBJECT,
                            body=_invite_body(_link(offering, code),
                                              _validity(at + timedelta(hours=DEFAULT_HOURS))))
        for code, at in zip(codes, (t1, t2), strict=True)]
    assert [invite_code_digest(code) for code in codes] == [first["code_sha256"], second["code_sha256"]]
    assert [type(result) for result in channel.delegated] == [Delivered]
    [message] = smtp.messages()
    [link] = smtp.invite_links()
    assert (link.bot_username, link.rcpt_to, link.acknowledged, link.code) == (
        offering.username, (address,), True, codes[1])
    _assert_email(message, address=address, subject=SUBJECT,
                  text=_invite_body(_link(offering, codes[1]), _validity(t2 + timedelta(hours=DEFAULT_HOURS))),
                  not_before=not_before, not_after=not_after)
    assert len(smtp.connections()) == 1

    events = _events_after(client, cursor)
    assert events == [
        _student_event("student.invite_revoked", offering, person, t2, origin="batch", revoke_reason="replaced"),
        _student_event("student.invite_sent", offering, person, t2, origin="batch",
                       expires_at=iso(t2 + timedelta(hours=DEFAULT_HOURS))),
    ]
    assert _invite_email_records(cap) == [(sid, "retry"), (sid, "sent")]

    out, err = capfd.readouterr()
    surfaces = {"logs": cap.text, "stdout": out, "stderr": err, "eventos": json.dumps(events, ensure_ascii=False),
                "tarefas": _task_rows_text(sql)}
    _assert_no_exception_text(surfaces)
    _assert_no_leak(surfaces, address=address, digests=[first["code_sha256"], second["code_sha256"]],
                    passwords=mail_secrets(smtp))
    # Controles positivos: a sentinela é achada no texto da exceção, e o código da 1ª execução, na
    # mensagem entregue ao canal.
    with pytest.raises(AssertionError):
        _assert_no_exception_text({"exceção": str(_channel_error(address, 1))})
    assert find_invite_codes(channel.calls[0].body, [first["code_sha256"]]) != []
    _synthetic_controls(smtp)


def test_exception_in_every_execution_exhausts_the_attempts_with_its_type(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, settings: Any,
        bot_worker_thread: Any, capfd: Any) -> None:
    """Falha temporária repetida até esgotar as tentativas; a repetição, a redação e os eventos; a tarefa
    (no envio, a `Exception` que escapa do `send` vale como `TemporaryFailure(type(exc).__name__)`;
    outra `Exception` vira falha temporária com o nome do tipo; na T2, `TemporaryFailure`
    na última tentativa → o convite da tentativa revogado sem evento próprio, `failed`/
    `attempts_exhausted` com o `error_type` da última `TemporaryFailure`, `student.invite_failed` e
    `PermanentTaskError`; nas anteriores, `RetryTaskLater`) e o fim de cada execução; injeção de falhas (o
    `ScriptedVerificationChannel` com uma `Exception` comum, sem canal real); fixture `settings` (3
    tentativas, esperas de 2 e 4 s da fila): com o `RuntimeError` em toda execução, cujo texto traz o
    endereço do aluno e uma sentinela, as duas primeiras execuções deixam a tarefa `pending` com
    `last_error = "RetryTaskLater"`, o envio `pending` e o registro `invite.email` `retry`; a 3ª, 6 s
    depois da 1ª, grava o envio `failed`/`attempts_exhausted` com `error_type = "RuntimeError"` e
    `attempts = 3` (um código por execução), a tarefa `failed` com `last_error = "PermanentTaskError"` e
    o `task.failed` da fila com o mesmo nome (não o nome da exceção, que é o que a fila gravaria se ela
    escapasse do tratador); cada T1 a partir da 2ª revoga o convite anterior (`replaced`), e o 3º é
    revogado sem evento; as três mensagens entregues ao canal são exatas; nenhuma conexão SMTP; os
    registros `invite.email` são `retry`, `retry` e `failed`; nem o texto das exceções nem o endereço nem
    os códigos aparecem em logs, fd 1 e 2, eventos, tarefas ou `invite_sends.error_type`."""
    offering = _active_offering(client, bot_api, "falha-excecao-esgotada", "cq_falha_excecao_esgotada_bot")
    address = "aluna.excecao.esgotada@turma.example.org"
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Excecao Esgotada", "5600000110", address)])
    person = _pseudo(sql, "5600000110")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)
    assert settings.max_attempts == 3
    channel = ScriptedVerificationChannel([_channel_error(address, step) for step in (1, 2, 3)])

    clock.advance(5)
    t1 = clock.now()
    starts = [t1]
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=channel)
        for attempt in (1, 2):
            _wait(lambda: _processed(cap, task["id"]) == attempt,  # noqa: B023 - espera deste passo
                  f"a execução {attempt} não terminou")
            assert _invite_tasks(sql) == [_task(task["id"], sid, state="pending", attempts=attempt,
                                                available_at=starts[-1] + timedelta(seconds=2 ** attempt),
                                                last_error="RetryTaskLater")]
            assert _sends(sql, offering) == [_send(sid, person, state="pending", created_at=t_confirm,
                                                   attempts=attempt)]
            assert [row["state"] for row in _invites(sql, offering)] == ["revoked"] * (attempt - 1) + ["pending"]
            assert _invite_email_records(cap) == [(sid, "retry")] * attempt
            clock.advance(2 ** attempt)
            starts.append(clock.now())
        t3 = starts[-1]
        _wait(lambda: _processed(cap, task["id"]) == 3, "a 3ª execução não terminou")

    assert [start - t1 for start in starts] == [timedelta(seconds=s) for s in (0, 2, 6)]
    assert _sends(sql, offering) == [_send(sid, person, state="failed", created_at=t_confirm, attempts=3,
                                           finished_at=t3, failure="attempts_exhausted", error_type="RuntimeError")]
    invites = _invites(sql, offering)
    assert len(invites) == 3
    assert invites == [_invite({**row, "person": person, "invite_send_id": sid}, state="revoked",
                               created_at=starts[k], revoked_at=starts[k + 1] if k < 2 else t3)
                       for k, row in enumerate(invites)]
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="failed", attempts=3,
                                        available_at=starts[1] + timedelta(seconds=4),
                                        last_error="PermanentTaskError", finished_at=t3)]

    codes = _codes_sent_to(channel, offering)
    assert channel.calls == [
        VerificationMessage(address=address, subject=SUBJECT,
                            body=_invite_body(_link(offering, code),
                                              _validity(at + timedelta(hours=DEFAULT_HOURS))))
        for code, at in zip(codes, starts, strict=True)]
    assert [invite_code_digest(code) for code in codes] == [row["code_sha256"] for row in invites]
    assert channel.delegated == []
    assert smtp.connections() == []
    assert smtp.messages() == []

    events = _events_after(client, cursor)
    assert events == [
        *(_student_event("student.invite_revoked", offering, person, at, origin="batch", revoke_reason="replaced")
          for at in starts[1:]),
        _student_event("student.invite_failed", offering, person, t3, origin="batch", failure="attempts_exhausted"),
        _task_failed(task["id"], 3, t3),
    ]
    assert _invite_email_records(cap) == [(sid, "retry"), (sid, "retry"), (sid, "failed")]

    out, err = capfd.readouterr()
    error_types = json.dumps([row["error_type"] for row in _sends(sql, offering)], ensure_ascii=False)
    surfaces = {"logs": cap.text, "stdout": out, "stderr": err, "eventos": json.dumps(events, ensure_ascii=False),
                "tarefas": _task_rows_text(sql), "invite_sends.error_type": error_types}
    _assert_no_exception_text(surfaces)
    _assert_no_leak(surfaces, address=address, digests=[row["code_sha256"] for row in invites],
                    passwords=mail_secrets(smtp))
    # Controle positivo: o detector acha cada código na mensagem entregue ao canal na sua execução.
    for call, row in zip(channel.calls, invites, strict=True):
        assert find_invite_codes(call.body, [row["code_sha256"]]) != []


# ---------------------------------------------------------------------------------------------
# Falhas definitivas, pela tabela única de classificação das falhas
# ---------------------------------------------------------------------------------------------


def _same_credentials(smtp: Any) -> tuple[str, str]:
    """As credenciais atuais do SMTP falso, para um `reset` que troca só o modo (o `smtp_settings` da
    fixture continua valendo)."""
    return (smtp.credentials.username, smtp.credentials.password)


def _tls_channel(smtp: Any, mail_settings: Any) -> Any:
    """O adaptador real em `starttls`, com a CA de teste injetada."""
    from cq_server.adapters.smtp import SmtpVerificationChannel

    return SmtpVerificationChannel(dataclasses.replace(mail_settings, security="starttls"),
                                   ssl_context=smtp.client_context())


def _scenario_rcpt_550(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    # O texto padrão do 550 no RCPT repete `<endereço>` (armadilha para quem registra a exceção).
    smtp.fail("rcpt", 550)
    return mail_channel


def _scenario_auth_535(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    smtp.fail("auth", 535)
    return mail_channel


def _scenario_bad_certificate(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    # Certificado da CA de teste, mas emitido para outro nome: a verificação do nome falha.
    smtp.reset(clock=clock, security="starttls", cert="wrong_host", credentials=_same_credentials(smtp))
    return _tls_channel(smtp, mail_settings)


def _scenario_without_starttls(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    smtp.reset(clock=clock, security="starttls", advertise_starttls=False, credentials=_same_credentials(smtp))
    return _tls_channel(smtp, mail_settings)


def _scenario_without_auth_mechanism(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    # Com credenciais no `MailSettings`, e o servidor sem AUTH anunciado.
    smtp.reset(clock=clock, advertise_auth=(), credentials=_same_credentials(smtp))
    return mail_channel


def _scenario_mail_553(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    smtp.fail("mail", 553)
    return mail_channel


def _scenario_data_554(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    smtp.fail("data", 554)
    return mail_channel


def _scenario_greeting_554(smtp: Any, clock: Any, mail_settings: Any, mail_channel: Any) -> Any:
    smtp.fail("connect", 554)
    return mail_channel


DEFINITIVE_FAILURES = [
    pytest.param("rcpt-550-com-eco", _scenario_rcpt_550, "recipient_refused", "SMTPRecipientsRefused",
                 id="rcpt-550-com-eco"),
    pytest.param("auth-535", _scenario_auth_535, "auth_refused", "SMTPAuthenticationError", id="auth-535"),
    pytest.param("certificado-invalido", _scenario_bad_certificate, "server_refused", "SSLCertVerificationError",
                 id="certificado-invalido"),
    pytest.param("sem-starttls", _scenario_without_starttls, "server_refused", "SMTPNotSupportedError",
                 id="sem-starttls"),
    pytest.param("sem-mecanismo-de-auth", _scenario_without_auth_mechanism, "server_refused",
                 "SMTPNotSupportedError", id="sem-mecanismo-de-auth"),
    pytest.param("mail-553", _scenario_mail_553, "server_refused", "SMTPSenderRefused", id="mail-553"),
    pytest.param("data-554", _scenario_data_554, "server_refused", "SMTPDataError", id="data-554"),
    pytest.param("saudacao-554", _scenario_greeting_554, "server_refused", "SMTPConnectError", id="saudacao-554"),
]


@pytest.mark.parametrize(("slug", "scenario", "failure", "error_type"), DEFINITIVE_FAILURES)
def test_definitive_refusal_is_recorded_once_with_its_class(
        slug: str, scenario: Callable[..., Any], failure: str, error_type: str, client: Any, clock: Any,
        sql: psycopg.Connection, bot_api: Any, smtp: Any, smtp_settings: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any, capfd: Any) -> None:
    """Classes definitivas `recipient_refused`, `auth_refused` e `server_refused` (5xx de
    configuração ou da conversa, certificado inválido, STARTTLS ou AUTH ausentes); a tarefa
    (tabela única: RCPT 550 → `recipient`; `SMTPAuthenticationError` 535 → `auth`; certificado
    com o nome errado (CA de teste injetada) → `SSLCertVerificationError`; modo `starttls` sem STARTTLS
    anunciado e servidor sem mecanismo de AUTH com credenciais → `SMTPNotSupportedError`; MAIL 553,
    DATA 554 e saudação 554 → `server`) e T2 (falha definitiva: o convite da tentativa revogado **sem**
    `student.invite_revoked`, coberto pelo `student.invite_failed`); os desfechos da tarefa (em cada falha
    registrada, `last_error = "PermanentTaskError"` e o `task.failed` da fila com o mesmo nome, e
    `attempts` = códigos gerados = 1; registro `invite.email` `failed`); erro do servidor
    de e-mail que repete o endereço: com o aluno canário B e o 550 que repete o endereço,
    nenhum log, fd 1 e 2, evento, tarefa (payload e `last_error`) nem o `error_type` traz alvo do aluno,
    o código nunca entregue ou a senha do SMTP; uma conexão só (sem repetição) e nenhum e-mail."""
    offering = _active_offering(client, bot_api, f"falha-{slug}", f"cq_{slug.replace('-', '_')}_bot")
    channel = scenario(smtp, clock, smtp_settings, mail_channel)
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [STUDENT_B.row()])
    person = _pseudo(sql, STUDENT_B.ra)
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=channel)
        _wait(lambda: _invite_tasks(sql)[0]["state"] == "failed" and _invite_email_records(cap),
              "o envio não terminou em falha registrada")

    assert _sends(sql, offering) == [_send(sid, person, state="failed", created_at=t_confirm, attempts=1,
                                           finished_at=t1, failure=failure, error_type=error_type)]
    [invite] = _invites(sql, offering)
    assert invite == _invite({**invite, "person": person, "invite_send_id": sid}, state="revoked", created_at=t1,
                             revoked_at=t1)
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="failed", attempts=1, available_at=t_confirm,
                                        last_error="PermanentTaskError", finished_at=t1)]
    events = _events_after(client, cursor)
    assert events == [
        _student_event("student.invite_failed", offering, person, t1, origin="batch", failure=failure),
        _task_failed(task["id"], 1, t1),
    ]
    assert _invite_email_records(cap) == [(sid, "failed")]
    assert smtp.messages() == []
    assert len(smtp.connections()) == 1

    out, err = capfd.readouterr()
    error_types = json.dumps([row["error_type"] for row in _sends(sql, offering)], ensure_ascii=False)
    surfaces = {"logs": cap.text, "stdout": out, "stderr": err,
                "eventos": json.dumps(events, ensure_ascii=False), "tarefas": _task_rows_text(sql),
                "invite_sends.error_type": error_types}
    for surface, text in surfaces.items():
        assert find_canary(text, prefixes=STUDENT_PREFIXES) == [], f"identidade do aluno em {surface}"
    _assert_no_leak(surfaces, digests=[invite["code_sha256"]], passwords=mail_secrets(smtp))
    # Controle positivo: a área restrita tem o e-mail do aluno, e o detector o acha ali.
    identity = sql.execute("SELECT row_to_json(i)::text FROM identities i WHERE i.ra = %s",
                           (STUDENT_B.ra,)).fetchone()[0]
    assert f"aluno-email:{STUDENT_B.email}" in find_canary(identity, prefixes=STUDENT_PREFIXES)
    _synthetic_controls(smtp)


def test_non_ascii_local_part_is_refused_without_connecting(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, mail_channel: Any,
        settings: Any, bot_worker_thread: Any) -> None:
    """E-mail da lista com parte local não ASCII (recusa definitiva do
    destinatário, sem conectar: não há caminho SMTPUTF8); a tarefa (linha da revalidação do
    `email_validator` com `allow_smtputf8=False`: `PermanentFailure("recipient", "EmailSyntaxError")`,
    email-validator 2.3.0), de ponta a ponta: a importação aceita o endereço pelas regras do
    cadastro; a T1 gera o código (`attempts = 1`); o adaptador recusa sem conectar (`smtp.connections() ==
    []`); o envio fica `failed`/`recipient_refused` com `error_type = "EmailSyntaxError"`, o convite da
    tentativa revogado sem evento próprio, `student.invite_failed`, a tarefa `failed` com
    `PermanentTaskError` e o `task.failed` da fila; registro `invite.email` `failed`."""
    offering = _active_offering(client, bot_api, "falha-nao-ascii", "cq_falha_nao_ascii_bot")
    address = "joão.aluno@turma.example.org"
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Acentuada", "5600000104", address)])
    person = _pseudo(sql, "5600000104")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings, verification=mail_channel)
        _wait(lambda: _invite_tasks(sql)[0]["state"] == "failed" and _invite_email_records(cap),
              "o envio não terminou em falha registrada")

    assert _sends(sql, offering) == [_send(sid, person, state="failed", created_at=t_confirm, attempts=1,
                                           finished_at=t1, failure="recipient_refused",
                                           error_type="EmailSyntaxError")]
    [invite] = _invites(sql, offering)
    assert invite == _invite({**invite, "person": person, "invite_send_id": sid}, state="revoked", created_at=t1,
                             revoked_at=t1)
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="failed", attempts=1, available_at=t_confirm,
                                        last_error="PermanentTaskError", finished_at=t1)]
    assert _events_after(client, cursor) == [
        _student_event("student.invite_failed", offering, person, t1, origin="batch", failure="recipient_refused"),
        _task_failed(task["id"], 1, t1),
    ]
    assert _invite_email_records(cap) == [(sid, "failed")]
    assert smtp.connections() == []
    assert smtp.messages() == []
    _assert_no_leak({"logs": cap.text, "tarefas": _task_rows_text(sql)}, address=address,
                    digests=[invite["code_sha256"]])


# ---------------------------------------------------------------------------------------------
# Sem servidor de e-mail configurado
# ---------------------------------------------------------------------------------------------


def test_worker_without_mail_fails_uncancelled_sends_as_not_configured(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """Sem e-mail (cada envio não cancelado que chegar ao worker falha de vez como falha no envio, sem gerar
    código); a ordem da T1: o oferecimento inativo cancela antes de "não configurado";
    a tarefa (T1 sem canal: `failed`/`not_configured`, `error_type` nulo, `student.invite_failed` e
    `PermanentTaskError`, sem revogar nem gerar código) e o fim de cada execução: com o
    worker sem `verification`, os dois envios do oferecimento ativo ficam `failed`/`not_configured` com
    `error_type` nulo e `attempts = 0`, sem nenhum convite (nenhum resumo de código gravado), a tarefa
    `failed` com `last_error = "PermanentTaskError"` e o `task.failed` da fila; o envio de um oferecimento
    encerrado depois da confirmação é cancelado (`offering_inactive`, `student.invite_cancelled`), não
    falha; os registros `invite.email` são `failed`, `failed` e `cancelled`, na ordem das tarefas, e
    nem eles nem as tarefas trazem os endereços."""
    active = _active_offering(client, bot_api, "sem-email-ativo", "cq_sem_email_ativo_bot")
    closing = _active_offering(client, bot_api, "sem-email-encerrado", "cq_sem_email_encerrado_bot")
    t_confirm = clock.now()
    _queue_batch(client, clock, active, [_row("Pessoa Aluna Sem Email Um", "5600000105",
                                              "aluna.sem.email.um@turma.example.org"),
                                         _row("Pessoa Aluna Sem Email Dois", "5600000106",
                                              "aluna.sem.email.dois@turma.example.org")])
    _queue_batch(client, clock, closing, [_row("Pessoa Aluna Sem Email Tres", "5600000107",
                                               "aluna.sem.email.tres@turma.example.org")])
    resp = client.post(f"/v1/offerings/{closing.ref}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("offerings.close", 200, resp.json())
    assert resp.json() == {**closing.created, "state": "closed", "activated_at": iso(t_confirm),
                           "closed_at": iso(t_confirm), "archived_at": None,
                           "bot": {"username": closing.username, "telegram_bot_id": closing.bot,
                                   "state": "registered", "profile_state": "pending"}}
    first, second = _sends(sql, active)
    [cancelled] = _sends(sql, closing)
    tasks = _invite_tasks(sql)
    assert [t["payload"] for t in tasks] == [{"invite_send_id": s["id"]} for s in (first, second, cancelled)]
    cursor = _last_seq(sql)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings)
        _wait(lambda: [t["state"] for t in _invite_tasks(sql)] == ["failed", "failed", "done"]
              and len(_invite_email_records(cap)) == 3, "os três envios não terminaram")

    assert _sends(sql, active) == [
        _send(s["id"], s["person"], state="failed", created_at=t_confirm, attempts=0, finished_at=t1,
              failure="not_configured") for s in (first, second)]
    assert _sends(sql, closing) == [_send(cancelled["id"], cancelled["person"], state="cancelled",
                                          created_at=t_confirm, attempts=0, finished_at=t1,
                                          cancel_reason="offering_inactive")]
    assert _invites(sql, active) == []
    assert _invites(sql, closing) == []
    assert sql.execute("SELECT count(*) FROM invites").fetchone()[0] == 0
    assert _invite_tasks(sql) == [
        _task(tasks[0]["id"], first["id"], state="failed", attempts=1, available_at=t_confirm,
              last_error="PermanentTaskError", finished_at=t1),
        _task(tasks[1]["id"], second["id"], state="failed", attempts=1, available_at=t_confirm,
              last_error="PermanentTaskError", finished_at=t1),
        _task(tasks[2]["id"], cancelled["id"], state="done", attempts=1, available_at=t_confirm,
              last_error=None, finished_at=t1),
    ]
    assert _events_after(client, cursor) == [
        _student_event("student.invite_failed", active, first["person"], t1, origin="batch",
                       failure="not_configured"),
        _task_failed(tasks[0]["id"], 1, t1),
        _student_event("student.invite_failed", active, second["person"], t1, origin="batch",
                       failure="not_configured"),
        _task_failed(tasks[1]["id"], 1, t1),
        _student_event("student.invite_cancelled", closing, cancelled["person"], t1, origin="batch",
                       cancel_reason="offering_inactive"),
    ]
    assert _invite_email_records(cap) == [(first["id"], "failed"), (second["id"], "failed"),
                                          (cancelled["id"], "cancelled")]
    # Os registros e as tarefas não trazem nenhum dos três endereços.
    for address in ("aluna.sem.email.um@turma.example.org", "aluna.sem.email.dois@turma.example.org",
                    "aluna.sem.email.tres@turma.example.org"):
        _assert_no_leak({"logs": cap.text, "tarefas": _task_rows_text(sql)}, address=address)


def test_worker_without_mail_cancels_the_superseded_send_before_not_configured(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """A ordem da T1: o envio superado é cancelado antes de "não configurado"; redação e
    eventos; a tarefa (T1 com envio superado: existe envio mais novo da mesma matrícula →
    `cancelled`/`superseded` com `student.invite_cancelled`, sem revogar nem gerar código; T1 sem
    canal → `failed`/`not_configured`, `error_type` nulo, `student.invite_failed` e
    `PermanentTaskError`, sem revogar nem gerar código) e o fim de cada execução; a ordem da T1
    (oferecimento inativo → já vinculado → superado → não configurado):
    com o worker sem `verification`, o envio A do lote e o reenvio B da coordenação pendentes para a mesma
    matrícula, a T1 de A (tarefa de menor `id`, pega primeiro pelo `claim` da fila) acha B e cancela A
    (`superseded`, `attempts = 0`, tarefa `done`, registro `invite.email` `cancelled`), e a T1 de B falha
    de vez (`not_configured`, `attempts = 0`, tarefa `failed` com `last_error = "PermanentTaskError"` e o
    `task.failed` da fila, registro `invite.email` `failed`); nenhum convite; eventos exatos, na ordem; nem
    os registros nem as tarefas trazem o endereço."""
    offering = _active_offering(client, bot_api, "sem-email-superado", "cq_sem_email_superado_bot")
    address = "aluna.sem.email.superada@turma.example.org"
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Sem Email Superada", "5600000111", address)])
    person = _pseudo(sql, "5600000111")
    clock.advance(60)
    t_resend = clock.now()
    resent = resend_invites(client, ADMIN_TOKEN, offering.ref, [person])
    assert resent == {"offering": offering.summary, "queued": 1, "valid_hours": DEFAULT_HOURS, "not_students": [],
                      "not_found_count": 0}
    batch_send, coordination_send = _sends(sql, offering)
    tasks = _invite_tasks(sql)
    assert [t["payload"] for t in tasks] == [{"invite_send_id": s["id"]} for s in (batch_send, coordination_send)]
    cursor = _last_seq(sql)

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings)
        _wait(lambda: [_processed(cap, t["id"]) for t in tasks] == [1, 1], "os dois envios não terminaram")

    assert _sends(sql, offering) == [
        _send(batch_send["id"], person, state="cancelled", created_at=t_confirm, attempts=0, finished_at=t1,
              cancel_reason="superseded"),
        _send(coordination_send["id"], person, state="failed", created_at=t_resend, attempts=0, finished_at=t1,
              failure="not_configured", origin="coordination"),
    ]
    assert _invites(sql, offering) == []
    assert sql.execute("SELECT count(*) FROM invites").fetchone()[0] == 0
    assert _invite_tasks(sql) == [
        _task(tasks[0]["id"], batch_send["id"], state="done", attempts=1, available_at=t_confirm,
              last_error=None, finished_at=t1),
        _task(tasks[1]["id"], coordination_send["id"], state="failed", attempts=1, available_at=t_resend,
              last_error="PermanentTaskError", finished_at=t1),
    ]
    assert _events_after(client, cursor) == [
        _student_event("student.invite_cancelled", offering, person, t1, origin="batch",
                       cancel_reason="superseded"),
        _student_event("student.invite_failed", offering, person, t1, origin="coordination",
                       failure="not_configured"),
        _task_failed(tasks[1]["id"], 1, t1),
    ]
    assert _invite_email_records(cap) == [(batch_send["id"], "cancelled"), (coordination_send["id"], "failed")]
    _assert_no_leak({"logs": cap.text, "tarefas": _task_rows_text(sql)}, address=address)


FAIL_TRIGGER = "cq_teste_falha_convite_nao_configurado"


def _install_commit_failure(sql_conn: psycopg.Connection, event_type: str) -> None:
    """Gatilho de restrição adiado em `events`, no molde de `test_api_errors.py`: o COMMIT de
    toda transação que grava um evento `event_type` falha com `RAISE EXCEPTION`."""
    body = (f"BEGIN IF NEW.type = '{event_type}' THEN RAISE EXCEPTION 'falha forcada do teste'; END IF; "
            "RETURN NULL; END")
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql_conn.execute("SET lock_timeout = '10s'")
    sql_conn.execute(pgsql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}")
                     .format(name, pgsql.Literal(body)))
    sql_conn.execute(pgsql.SQL("CREATE CONSTRAINT TRIGGER {} AFTER INSERT ON events DEFERRABLE INITIALLY "
                               "DEFERRED FOR EACH ROW EXECUTE FUNCTION {}()").format(name, name))
    sql_conn.execute(pgsql.SQL("ALTER TABLE events ENABLE ALWAYS TRIGGER {}").format(name))


def _remove_commit_failure(sql_conn: psycopg.Connection) -> None:
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql_conn.execute("SET lock_timeout = '10s'")
    sql_conn.execute(pgsql.SQL("DROP TRIGGER {} ON events").format(name))
    sql_conn.execute(pgsql.SQL("DROP FUNCTION {}()").format(name))


def test_invite_failed_not_configured_fails_closed(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """O evento na mesma transação da mudança; a falha ao gravar o evento (na
    T1, o `student.invite_failed` de `not_configured` volta atrás e a tarefa repete sem mudança; o
    registro `invite.email` sai depois do COMMIT) e os desfechos da tarefa: o caminho da T1 fechado em falha, fora do
    `FAIL_CLOSED_CASES` dos casos fechados em falha. Com o gatilho de falha no COMMIT do `student.invite_failed`, no molde
    de `test_api_errors.py`, a execução do worker sem `verification` volta atrás: tarefa `pending` com `last_error =
    "RaiseException"` e a espera de 2 s da fila, envio `pending` com `attempts = 0` e sem `finished_at`,
    nenhum convite, nenhum evento, nenhum registro `invite.email` e nenhum `event=exception` (o
    registro `task.processed` com `pending`); sem o gatilho, a repetição grava `failed`/`not_configured`,
    `student.invite_failed`, a tarefa `failed` com `PermanentTaskError` e o `task.failed` da fila, com um
    único registro `invite.email` `failed`."""
    offering = _active_offering(client, bot_api, "sem-email-fechado", "cq_sem_email_fechado_bot")
    t_confirm = clock.now()
    _queue_batch(client, clock, offering, [_row("Pessoa Aluna Falha Fechada", "5600000108",
                                                "aluna.falha.fechada@turma.example.org")])
    person = _pseudo(sql, "5600000108")
    [send] = _sends(sql, offering)
    sid = send["id"]
    [task] = _invite_tasks(sql)
    cursor = _last_seq(sql)
    _install_commit_failure(sql, "student.invite_failed")

    clock.advance(5)
    t1 = clock.now()
    with capture_logs() as cap:
        _worker(bot_worker_thread, settings)
        _wait(lambda: _invite_tasks(sql)[0]["attempts"] == 1 and _invite_tasks(sql)[0]["state"] == "pending"
              and [r.get("id") for r in cap.with_event("task.processed")].count(task["id"]) == 1,
              "a execução com o COMMIT falhando não terminou")
        assert _invite_tasks(sql) == [_task(task["id"], sid, state="pending", attempts=1,
                                            available_at=t1 + timedelta(seconds=2), last_error="RaiseException")]
        assert _sends(sql, offering) == [_send(sid, person, state="pending", created_at=t_confirm, attempts=0)]
        assert _invites(sql, offering) == []
        assert _events_after(client, cursor) == []
        assert _invite_email_records(cap) == []
        processed = [r for r in cap.with_event("task.processed") if r.get("id") == task["id"]]
        assert [r.get("state") for r in processed] == ["pending"], processed
        assert cap.with_event("exception") == []

        _remove_commit_failure(sql)
        clock.advance(2)
        t2 = clock.now()
        _wait(lambda: _invite_tasks(sql)[0]["state"] == "failed" and _invite_email_records(cap),
              "a repetição sem o gatilho não registrou a falha")

    assert _sends(sql, offering) == [_send(sid, person, state="failed", created_at=t_confirm, attempts=0,
                                           finished_at=t2, failure="not_configured")]
    assert _invites(sql, offering) == []
    assert _invite_tasks(sql) == [_task(task["id"], sid, state="failed", attempts=2,
                                        available_at=t1 + timedelta(seconds=2), last_error="PermanentTaskError",
                                        finished_at=t2)]
    assert _events_after(client, cursor) == [
        _student_event("student.invite_failed", offering, person, t2, origin="batch", failure="not_configured"),
        _task_failed(task["id"], 2, t2),
    ]
    assert _invite_email_records(cap) == [(sid, "failed")]
    _assert_no_leak({"logs": cap.text, "tarefas": _task_rows_text(sql)},
                    address="aluna.falha.fechada@turma.example.org")


# ---------------------------------------------------------------------------------------------
# Montagem do canal no worker, em processo
# ---------------------------------------------------------------------------------------------


def _build(env: dict[str, str], settings: Any = LEASE_30) -> Any:
    from cq_server.__main__ import build_verification

    return build_verification(env, settings)


def _wire_secrets(user: str, password: str) -> list[str]:
    """A senha e as suas formas no fio (`mail_secrets` sobre um par qualquer de credenciais)."""
    return mail_secrets(types.SimpleNamespace(credentials=(user, password)))


NO_HOST_ENVS = [
    pytest.param({}, id="sem-variaveis"),
    pytest.param({"CQ_SMTP_HOST": ""}, id="host-vazio"),
    pytest.param({"CQ_SMTP_PORT": "abc", "CQ_SMTP_SECURITY": "desconhecido", "CQ_SMTP_USERNAME": KNOWN_USER,
                  "CQ_SMTP_PASSWORD": IGNORED_PASSWORD, "CQ_SMTP_FROM": "nao-e-um-endereco",
                  "CQ_SMTP_TIMEOUT_SECONDS": "nan"}, id="demais-invalidas-ignoradas"),
]


@pytest.mark.parametrize("env", NO_HOST_ENVS)
def test_build_verification_without_smtp_host_returns_none_and_warns_once(
        env: dict[str, str], network_guard: Any, capfd: Any) -> None:
    """Aviso único (registrar um único aviso funcional ao subir); sem `CQ_SMTP_HOST`, ausente ou
    vazio, as demais `CQ_SMTP_*` são ignoradas, mesmo inválidas; logger `cq_server.worker`;
    montagem do canal (`build_verification(env, settings)` em processo: devolve `None` e registra uma
    única vez `worker.mail_disabled`, WARNING, sem mais nenhum registro); nada em fd 1 e 2, nenhuma
    conexão e nenhuma senha (a das variáveis ignoradas) no log."""
    capfd.readouterr()
    with capture_logs() as cap:
        channel = _build(env)
    assert channel is None
    assert len(cap.records) == 1, cap.records
    expected = {"level": "WARNING", "logger": "cq_server.worker", "event": "worker.mail_disabled"}
    assert expected.items() <= cap.records[0].items(), cap.records[0]
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []
    assert find_secrets(cap.text, _wire_secrets(KNOWN_USER, IGNORED_PASSWORD)) == []
    assert find_secrets(f"log com {IGNORED_PASSWORD}", _wire_secrets(KNOWN_USER, IGNORED_PASSWORD)) != []


BASE_MAIL_ENV = {"CQ_SMTP_HOST": "smtp.example.org", "CQ_SMTP_USERNAME": KNOWN_USER,
                 "CQ_SMTP_PASSWORD": KNOWN_PASSWORD, "CQ_SMTP_FROM": "convites@cq.example.org"}


def _mail_env(**changes: str | None) -> dict[str, str]:
    """`BASE_MAIL_ENV` (válido: `starttls`, porta 587 e prazo de 10 s pelos padrões) com as trocas;
    `None` tira a variável."""
    env = dict(BASE_MAIL_ENV)
    for key, value in changes.items():
        name = f"CQ_SMTP_{key.upper()}"
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


INVALID_MAIL_ENVS = [
    pytest.param(_mail_env(port="0"), MSG_PORT, id="porta-0"),
    pytest.param(_mail_env(port="65536"), MSG_PORT, id="porta-65536"),
    pytest.param(_mail_env(port="abc"), MSG_PORT, id="porta-abc"),
    pytest.param(_mail_env(port="25.5"), MSG_PORT, id="porta-25.5"),
    pytest.param(_mail_env(security="ssl"), MSG_SECURITY, id="modo-desconhecido"),
    pytest.param(_mail_env(security="none"), MSG_PLAINTEXT, id="none-fora-do-loopback"),
    pytest.param(_mail_env(host="smtp exemplo.org"), MSG_HOST, id="host-com-espaco"),
    pytest.param(_mail_env(password=None), MSG_PAIR, id="usuario-sem-senha"),
    pytest.param(_mail_env(username=None), MSG_PAIR, id="senha-sem-usuario"),
    pytest.param(_mail_env(password="Senha-Com-Cedilha-ç-9"), MSG_ASCII, id="senha-nao-ascii"),
    pytest.param(_mail_env(**{"from": None}), MSG_FROM, id="remetente-ausente"),
    pytest.param(_mail_env(**{"from": "nao-e-um-endereco"}), MSG_FROM, id="remetente-invalido"),
    pytest.param(_mail_env(**{"from": "convitês@cq.example.org"}), MSG_FROM, id="remetente-nao-ascii"),
    pytest.param(_mail_env(**{"from": "convites@cq.test"}), MSG_FROM, id="remetente-em-test"),
    pytest.param(_mail_env(timeout_seconds="0"), MSG_TIMEOUT, id="prazo-0"),
    pytest.param(_mail_env(timeout_seconds="-1"), MSG_TIMEOUT, id="prazo-negativo"),
    pytest.param(_mail_env(timeout_seconds="nan"), MSG_TIMEOUT, id="prazo-nan"),
    pytest.param(_mail_env(timeout_seconds="inf"), MSG_TIMEOUT, id="prazo-inf"),
    pytest.param(_mail_env(timeout_seconds="15.5"), MSG_LEASE, id="prazo-mais-15-passa-da-posse"),
]


@pytest.mark.parametrize(("env", "message"), INVALID_MAIL_ENVS)
def test_build_verification_rejects_invalid_mail_configuration(
        env: dict[str, str], message: str, network_guard: Any, capfd: Any) -> None:
    """Configuração inválida (com `CQ_SMTP_HOST`, cada configuração inválida → `ConfigError` com a
    mensagem exata, sem o valor; a invariante `CQ_SMTP_TIMEOUT_SECONDS + 15 > CQ_LEASE_SECONDS`, com a
    posse de 30 s do `Settings` dos testes, → a mensagem da invariante); montagem do canal (`build_verification` em
    processo, que só levanta: a saída 2 e o `config.invalid` são do `cq-server worker`, testados no
    subprocesso); nenhuma conexão, e nem o log nem fd 1 e 2 trazem a senha ou as formas do fio."""
    capfd.readouterr()
    with capture_logs() as cap:
        with pytest.raises(ConfigError) as raised:
            _build(env)
    assert str(raised.value) == message
    assert raised.value.args == (message,)
    out, err = capfd.readouterr()
    assert network_guard.attempts() == []
    user = env.get("CQ_SMTP_USERNAME", KNOWN_USER)
    password = env.get("CQ_SMTP_PASSWORD", KNOWN_PASSWORD)
    for surface, text in (("logs", cap.text), ("stdout", out), ("stderr", err)):
        assert find_secrets(text, _wire_secrets(user, password)) == [], f"senha em {surface}"
    assert find_secrets(f"log com {password}", _wire_secrets(user, password)) != []


@pytest.mark.parametrize("timeout_seconds", ["2", "15"], ids=["prazo-2", "prazo-15-no-limite-da-posse"])
def test_build_verification_with_mail_configured_returns_the_smtp_channel_silently(
        timeout_seconds: str, clock: Any, smtp: Any, network_guard: Any, capfd: Any) -> None:
    """A senha não aparece nos logs nem em fd 1 e 2 da subida do worker; segredos e montagem do canal;
    (com e-mail configurado, ela devolve o `SmtpVerificationChannel` e não registra
    nada); a invariante no limite (`15 + 15 == 30` da posse é aceito): com uma senha
    fictícia conhecida, `build_verification` devolve o adaptador SMTP (capacidades `email`) sem nenhum
    registro, nada em fd 1 e 2 e sem conectar; o canal devolvido usa a configuração do ambiente: um
    envio pelo SMTP falso é entregue (`Delivered`), autenticado, com o envelope, os cabeçalhos e o texto
    exatos; nenhum log do envio traz a senha, as formas do fio ou o endereço (controle positivo do
    detector num valor sintético)."""
    from cq_server.adapters.smtp import SmtpVerificationChannel
    from cq_server.ports.verification import Delivered, VerificationCapabilities, VerificationMessage

    smtp.reset(clock=clock, credentials=(KNOWN_USER, KNOWN_PASSWORD))
    env = {"CQ_SMTP_HOST": "127.0.0.1", "CQ_SMTP_PORT": str(smtp.port), "CQ_SMTP_SECURITY": "none",
           "CQ_SMTP_USERNAME": KNOWN_USER, "CQ_SMTP_PASSWORD": KNOWN_PASSWORD, "CQ_SMTP_FROM": SENDER,
           "CQ_SMTP_TIMEOUT_SECONDS": timeout_seconds}
    known = mail_secrets(smtp)
    assert KNOWN_PASSWORD in known
    capfd.readouterr()
    with capture_logs() as built:
        channel = _build(env)
    assert isinstance(channel, SmtpVerificationChannel)
    assert channel.capabilities == VerificationCapabilities("email")
    assert built.records == []
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []

    address = "aluna.montagem@turma.example.org"
    subject = "Conferência da montagem do canal"
    body = "Mensagem de conferência da montagem do canal de verificação."
    not_before = _real_now()
    with capture_logs() as sent:
        result = channel.send(VerificationMessage(address=address, subject=subject, body=body))
    not_after = _real_now()
    assert type(result) is Delivered
    [message] = smtp.wait_for(lambda: smtp.messages(), timeout=5)
    assert message.authenticated is True
    _assert_email(message, address=address, subject=subject, text=body, not_before=not_before,
                  not_after=not_after)
    out, err = capfd.readouterr()
    for surface, text in (("logs da montagem", built.text), ("logs do envio", sent.text), ("stdout", out),
                          ("stderr", err)):
        assert find_secrets(text, known) == [], f"senha em {surface}"
        assert address not in text, f"endereço em {surface}"
    assert find_secrets(f"log com {KNOWN_PASSWORD}", known) != []


# ---------------------------------------------------------------------------------------------
# O `cq-server worker` em subprocesso
# ---------------------------------------------------------------------------------------------


def _json_records(out: str) -> list[dict]:
    """As linhas JSON da saída padrão do processo (tudo sai em JSON)."""
    records: list[dict] = []
    for line in out.splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def test_worker_process_without_smtp_host_logs_mail_disabled_once(db_url: str, bot_api: Any) -> None:
    """Aviso único (registrar um único aviso funcional ao subir); sem `CQ_SMTP_HOST`, as demais
    `CQ_SMTP_*` são ignoradas, mesmo inválidas: o worker sobe; WARNING, logger
    `cq_server.worker`; o `_cmd_worker` monta o canal com a `build_verification` antes de
    `run_worker`: o `cq-server worker` num subprocesso, sem `CQ_SMTP_HOST` e com uma porta inválida e
    uma senha nas demais variáveis, sobe (`worker.started`) e registra exatamente um
    `worker.mail_disabled`, antes do `worker.started`; toda a saída padrão é JSON, e nem ela nem a de
    erro trazem a senha (controle positivo do detector)."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-sem-email",
                         "CQ_SMTP_PORT": PORT_SENTINEL, "CQ_SMTP_USERNAME": KNOWN_USER,
                         "CQ_SMTP_PASSWORD": IGNORED_PASSWORD})
    try:
        _wait(lambda: '"worker.started"' in proc.output()[0] or proc.poll() is not None,
              "o cq-server worker não subiu", timeout=SUBPROCESS_TIMEOUT)
        assert proc.poll() is None, f"o cq-server worker saiu com {proc.returncode}"
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    disabled = [r for r in records if r.get("event") == "worker.mail_disabled"]
    assert len(disabled) == 1, f"registros worker.mail_disabled: {len(disabled)} (eventos: {events})"
    expected = {"level": "WARNING", "logger": "cq_server.worker", "event": "worker.mail_disabled"}
    assert expected.items() <= disabled[0].items(), disabled[0]
    assert events.index("worker.mail_disabled") < events.index("worker.started"), events
    secrets_ = _wire_secrets(KNOWN_USER, IGNORED_PASSWORD)
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert find_secrets(text, secrets_) == [], f"senha na {surface} do cq-server worker"
        assert PORT_SENTINEL not in text, f"valor da porta na {surface}"
    assert find_secrets(out + IGNORED_PASSWORD, secrets_) != []


def test_worker_process_with_invalid_smtp_port_exits_2_with_config_invalid(db_url: str, bot_api: Any) -> None:
    """Configuração inválida (ou invariante violada → o processo
    sai com código 2 e o registro `config.invalid`, no logger `cq_server.main`, com a mensagem em
    `detail`, sem valor nem senha; o caso por subprocesso): `python -m cq_server worker` com
    `CQ_SMTP_HOST` e uma porta inválida sai com código 2, e a saída padrão é exatamente um registro
    JSON `{level: ERROR, logger: cq_server.main, event: config.invalid, detail: <mensagem exata>}`; nem
    o valor da porta nem a senha aparecem em fd 1 e 2 (controle positivo do detector)."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-porta-invalida",
                         "CQ_SMTP_HOST": "127.0.0.1", "CQ_SMTP_PORT": PORT_SENTINEL,
                         "CQ_SMTP_USERNAME": KNOWN_USER, "CQ_SMTP_PASSWORD": PROCESS_PASSWORD,
                         "CQ_SMTP_FROM": SENDER})
    try:
        # O processo tem de sair antes de subir o laço: um `worker.started` já é falha.
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0],
              f"o cq-server worker não saiu nem subiu em {SUBPROCESS_TIMEOUT} s", timeout=SUBPROCESS_TIMEOUT)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o cq-server worker não saiu com código 2 (saiu: {code}; registros: {events})"
    assert "worker.started" not in events, events
    assert len(records) == 1, events
    expected = {"level": "ERROR", "logger": "cq_server.main", "event": "config.invalid", "detail": MSG_PORT}
    assert expected.items() <= records[0].items(), records[0]
    secrets_ = _wire_secrets(KNOWN_USER, PROCESS_PASSWORD)
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert find_secrets(text, secrets_) == [], f"senha na {surface} do cq-server worker"
        assert PORT_SENTINEL not in text, f"valor da porta na {surface}"
    assert find_secrets(out + PROCESS_PASSWORD, secrets_) != []
