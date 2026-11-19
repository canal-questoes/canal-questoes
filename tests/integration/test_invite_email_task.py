"""Tarefa `invite.email`: gerar o código e mandar o convite por e-mail,
inclusive no envio
repetido por falha incerta.

Contratos: o convite e o envio, a confirmação, as etapas de cada execução (T1, envio, T2, fim de
cada execução e quedas), o termo e o vínculo (no caso do aluno vinculado entre a confirmação e o
envio), o autosserviço (nas sequências em que convites concorrem pelo mesmo aluno), a adesão, os
eventos, os textos do bot e do e-mail, a validade no fuso do oferecimento e o próprio e-mail; os
registros de log `invite.email` e `invite.email_superseded`; as interfaces internas (porta de
verificação, tratador) e, por fim, a injeção de falhas nos testes.

Tudo passa pelo caminho real: a API importa a lista, prepara e confirma; um worker com `bots` numa
thread (`bot_worker_thread`, `FakeClock` do teste) recebe o canal de verificação por `verification=`
(o adaptador real, `mail_channel`, sobre o SMTP falso; ou o `ScriptedVerificationChannel`) e processa
as tarefas em FIFO por `id`; o `/start`, o `/aceito` e o `/convite` chegam pela Bot API falsa. Cada
caso confere o envio (`invite_sends`), os convites (o banco só guarda o resumo do código), a tarefa, os
e-mails capturados (envelope, cabeçalhos e texto decodificado exatos; `Date` e `Message-ID` por
forma), os eventos exatos e na ordem e os registros de log exatos.

Os textos esperados (assunto e corpo do e-mail, textos do bot) são escritos aqui
por extenso (não vêm do código); a disciplina e o período são neutros. O corpo decodificado termina
com a quebra de linha que fecha a parte `text/plain` (efeito do `set_content`).
Horários pelo `FakeClock`, em minutos inteiros sempre que o e-mail os mostra (a validade sai em
`HH:MM`); o `Date` do e-mail e a meta de 5 s em tempo real. Ids internos, códigos e pseudônimos nunca como
literal: são lidos do banco e do SMTP falso e comparados por relação. Com o worker no ar, o relógio
anda 60 s por vez: passa as esperas de 2 s e de 4 s das repetições e a posse de 30 s das tarefas, em
minutos inteiros; a posse dos bots é renovada mesmo vencida, sem evento (`bots.renew_leases`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.ports.verification import Delivered, TemporaryFailure, VerificationCapabilities, VerificationMessage
from cq_server.students import INVITE_EMAIL_TASK_TYPE, invite_email_task_handler
from cq_server.tasks import TaskQueue, TaskRecord
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
    students_summary,
)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import DELEGATE, Crash, Delegate, ScriptedVerificationChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import find_invite_codes, remember_secret
from tests.helpers.smtp import SENDER, monotonic

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
DEFAULT_HOURS = 168
CONFIRMATION = timedelta(minutes=10)  # validade do código de confirmação
TASK_LEASE = timedelta(seconds=30)  # posse das tarefas no `settings` dos testes
FIRST_RETRY_WAIT = timedelta(seconds=2)  # espera da fila depois da 1ª tentativa (base 2 s)
SECOND_RETRY_WAIT = timedelta(seconds=4)  # espera da fila depois da 2ª tentativa (2 · 2¹ s)
STEP = 60  # salto do relógio com o worker no ar (ver o docstring do módulo)
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
DELIVERY_DEADLINE_SECONDS = 5.0
WORKER_ID = "worker-convites"
RESUME_WORKER_ID = "worker-convites-retomada"
OTHER_WORKER_ID = "worker-convites-concorrente"
MISSING_SEND_ID = 2_000_000_017  # envio que não existe (entrada do teste, não resultado)

CONFIRMATION_CODE = re.compile(r"[A-Za-z0-9_-]{22}")
INVITE_CODE = re.compile(r"[A-Za-z0-9_-]{22}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MESSAGE_ID = re.compile(r"<[^<>@\s]+@cq\.example\.org>")  # domínio do remetente (SENDER)
SIZE_PARAM = re.compile(r"SIZE=[0-9]+", re.IGNORECASE)
LINK_IN_TEXT = re.compile(r"https://t\.me/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])")

STUDENT_EVENT_TYPES = (
    "student.list_imported", "student.invites_prepared", "student.invites_queued", "student.invite_sent",
    "student.invite_failed", "student.invite_cancelled", "student.invite_revoked", "student.joined",
)


def _fill(template: str, **values: str) -> str:
    """Troca `{nome}` pelos valores dados (os valores deste arquivo não têm chaves)."""
    for name, value in values.items():
        template = template.replace("{" + name + "}", value)
    return template


# Assunto e corpo do e-mail de convite, por extenso.
SUBJECT = _fill("Seu link para o bot do oferecimento {course} ({term})", course=COURSE, term=TERM)
BODY_TEMPLATE = _fill(
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da disciplina chegam por "
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
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.",
    course=COURSE, term=TERM)
FROM_HEADER = f"Canal de Questões <{SENDER}>"

# Textos do bot, por extenso, versão 1 do termo.
TERMS = _fill(
    "Termo de uso do bot do oferecimento {course} ({term}), versão {version}\n"
    "\n"
    "Leia até o fim: as opções para aceitar ou recusar estão no final.\n"
    "\n"
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a elas. As respostas "
    "são registradas para o ensino na disciplina e para o piloto de um Trabalho de Conclusão de Curso "
    "(TCC) de Ciência da Computação da Unicamp, que avalia este sistema.\n"
    "\n"
    "Quais dados são guardados:\n"
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;\n"
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do perfil e o "
    "telefone não são guardados);\n"
    "• as suas respostas e as mensagens que você mandar a este bot.\n"
    "\n"
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno aparece por um "
    "código. A ligação entre o código e o seu nome só é consultada quando necessário, e cada consulta fica "
    "registrada. No TCC, os resultados aparecem só de forma agregada, sem identificar ninguém.\n"
    "\n"
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste oferecimento, "
    "definida pela coordenação. Depois desse prazo, os seus dados de identificação (nome, RA, e-mail e "
    "conta do Telegram) são apagados, e as respostas continuam guardadas sem eles.\n"
    "\n"
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste piloto, estas "
    "questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail continua valendo até "
    "vencer.\n"
    "\n"
    "Para aceitar, toque em /aceito\n"
    "Para recusar, toque em /recuso",
    course=COURSE, term=TERM, version="1")
WELCOME_STUDENT = _fill(
    "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {course} ({term}). As questões "
    "da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação.",
    course=COURSE, term=TERM)
INVITE_REQUESTED = _fill(
    "Pedido recebido. Se o e-mail informado for de um aluno da lista do oferecimento {course} ({term}) que "
    "ainda não se vinculou, um novo link de convite vai para esse e-mail, e o link anterior deixa de "
    "valer. O e-mail pode levar alguns minutos para chegar.\n"
    "\n"
    "Para pedir, mande /convite seguido de um espaço e do seu e-mail institucional, o mesmo da lista da "
    "turma. Há um limite de pedidos por dia. Se o link não chegar, fale com a coordenação.",
    course=COURSE, term=TERM)

# Alunos neutros (fora dos prefixos canário). O e-mail vai à lista como está; o envelope e o `To` levam
# o `ascii_email` do `email_validator` (parte local com a caixa gravada, domínio em minúsculas;
# conforme o `email_validator`), escrito aqui por extenso.
ROW_A = {"name": "Aluna Primeira do Lote", "ra": "4400000101", "email": "Aluna.Primeira@Turma.Example.org"}
ROW_B = {"name": "Aluno Segundo do Lote", "ra": "4400000102", "email": "aluno.segundo@turma.example.org"}
ROW_C = {"name": "Aluna Terceira do Lote", "ra": "4400000103", "email": "ALUNA.Terceira@TURMA.example.ORG"}
ENVELOPE = {
    "4400000101": "Aluna.Primeira@turma.example.org",
    "4400000102": "aluno.segundo@turma.example.org",
    "4400000103": "ALUNA.Terceira@turma.example.org",
}


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


class Batch(NamedTuple):
    """O lote confirmado: cursor dos eventos (logo depois da importação) e os horários do preparo e
    da confirmação."""

    cursor: int
    t_prep: datetime
    t_conf: datetime


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _offering(client: Any, bot_api: Any, code: str, username: str, *, zone: str = ZONE) -> tuple[dict, int]:
    """Oferecimento ativo com bot registrado na Bot API falsa."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=zone)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _import(client: Any, offering: dict, rows: list[dict]) -> None:
    """`students.import` aceito, com as contagens exatas de uma lista só de pessoas novas."""
    body = import_students(client, ADMIN_TOKEN, offering["code"], rows)
    n = len(rows)
    assert body == {"offering": _ref(offering), "received": n, "people_created": n, "people_reused": 0,
                    "enrollments_created": n, "already_enrolled": 0}


def _prepare(client: Any, offering: dict, eligible: int, at: datetime, *, valid_hours: int | None = None) -> str:
    """`invites.prepare_send` com a resposta exata; devolve o código de confirmação."""
    body = prepare_invites(client, ADMIN_TOKEN, offering["code"], valid_hours=valid_hours)
    code = body["confirmation_code"]
    assert CONFIRMATION_CODE.fullmatch(code), "código de confirmação fora do formato"
    assert body == {"offering": _ref(offering), "eligible": eligible,
                    "valid_hours": DEFAULT_HOURS if valid_hours is None else valid_hours,
                    "confirmation_code": code, "expires_at": iso(at + CONFIRMATION)}
    return code


def _confirm(client: Any, offering: dict, code: str, queued: int, valid_hours: int = DEFAULT_HOURS) -> None:
    body = send_invites(client, ADMIN_TOKEN, offering["code"], code)
    assert body == {"offering": _ref(offering), "queued": queued, "valid_hours": valid_hours}


def _batch(client: Any, clock: Any, sql: Any, offering: dict, rows: list[dict], *,
           valid_hours: int | None = None) -> Batch:
    """Importa `rows`, prepara (60 s depois) e confirma (mais 60 s) o lote de todos os alunos."""
    _import(client, offering, rows)
    cursor = _last_seq(sql)
    t_prep = clock.advance(STEP)
    code = _prepare(client, offering, len(rows), t_prep, valid_hours=valid_hours)
    t_conf = clock.advance(STEP)
    _confirm(client, offering, code, len(rows), DEFAULT_HOURS if valid_hours is None else valid_hours)
    return Batch(cursor, t_prep, t_conf)


def _resend(client: Any, offering: dict, pseudonyms: list[str]) -> None:
    """`invites.resend` com a resposta exata (todos os pseudônimos são de alunos distintos)."""
    body = resend_invites(client, ADMIN_TOKEN, offering["code"], pseudonyms)
    assert body == {"offering": _ref(offering), "queued": len(pseudonyms), "valid_hours": DEFAULT_HOURS,
                    "not_students": [], "not_found_count": 0}


def _start_worker(bot_worker_thread: Any, settings: Any, verification: Any,
                  worker_id: str = WORKER_ID) -> tuple[Any, _DefaultChannel]:
    channel = _DefaultChannel()
    thread = bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=worker_id),
                               verification=verification)
    return thread, channel


def _wait(smtp: Any, predicate: Callable[[], Any]) -> Any:
    return smtp.wait_for(predicate, timeout=PROCESS_TIMEOUT)


def _settle(smtp: Any, sql: Any) -> None:
    """Antes de andar o relógio com o worker no ar: nenhuma tarefa em posse e nenhuma mensagem da fila de
    saída por entregar. Um salto de `STEP` (maior que a posse de 30 s) venceria a posse de um trabalho em
    curso (o `complete` de uma tarefa ou o registro de uma entrega), que seria refeito."""

    def idle() -> bool:
        leased = sql.execute("SELECT count(*) FROM tasks WHERE state = 'leased'").fetchone()[0]
        waiting = sql.execute(
            "SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0]
        return leased == 0 and waiting == 0

    _wait(smtp, idle)


def _students(sql: Any, offering: dict) -> list[tuple[int, str, str]]:
    """(id da matrícula, pseudônimo, RA) dos alunos do oferecimento, em ordem de `enrollments.id`."""
    return sql.execute(
        """
        SELECT e.id, p.pseudo_id::text, i.ra
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN identities i ON i.person_id = p.id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s AND e.role = 'student'
         ORDER BY e.id
        """, (offering["offering_id"],)).fetchall()


def _sends(sql: Any) -> list[tuple]:
    """(id, id da matrícula, pseudônimo, origin, valid_hours, state, failure, cancel_reason, error_type,
    attempts, created_at, finished_at) de cada envio, em ordem de id."""
    return sql.execute(
        """
        SELECT s.id, s.enrollment_id, p.pseudo_id::text, s.origin, s.valid_hours, s.state, s.failure,
               s.cancel_reason, s.error_type, s.attempts, s.created_at, s.finished_at
          FROM invite_sends s
          JOIN enrollments e ON e.id = s.enrollment_id
          JOIN people p ON p.id = e.person_id
         ORDER BY s.id
        """).fetchall()


def _send_view(rows: list[tuple]) -> list[tuple]:
    """As linhas de `_sends` sem os ids internos."""
    return [row[2:] for row in rows]


def _wait_send_states(smtp: Any, sql: Any, states: list[str]) -> list[tuple]:
    def ready() -> list[tuple] | None:
        rows = _sends(sql)
        return rows if [row[5] for row in rows] == states else None

    return _wait(smtp, ready)


def _invites(sql: Any) -> list[tuple]:
    """(id, invite_send_id, pseudônimo, code_sha256, state, expires_at, created_at, used_at, revoked_at,
    matrícula do convite = matrícula do envio) dos convites de aluno, em ordem de id."""
    return sql.execute(
        """
        SELECT i.id, i.invite_send_id, p.pseudo_id::text, i.code_sha256, i.state, i.expires_at,
               i.created_at, i.used_at, i.revoked_at, s.enrollment_id = i.enrollment_id
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          LEFT JOIN invite_sends s ON s.id = i.invite_send_id
         WHERE e.role = 'student'
         ORDER BY i.id
        """).fetchall()


def _invite_view(rows: list[tuple]) -> list[tuple]:
    """As linhas de `_invites` sem o id do convite (o id do envio fica: é a relação conferida)."""
    return [row[1:] for row in rows]


def _invite(send_id: int, pseudo: str, digest: str, state: str, created: datetime, *,
            hours: int = DEFAULT_HOURS, used: datetime | None = None,
            revoked: datetime | None = None) -> tuple:
    return (send_id, pseudo, digest, state, created + timedelta(hours=hours), created, used, revoked, True)


def _digest_of_unknown(rows: list[tuple], index: int) -> str:
    """O resumo gravado de um código que nunca saiu num e-mail (ex.: RCPT recusado): só a forma."""
    digest = rows[index][3]
    assert HEX64.fullmatch(digest), "resumo do código fora da forma SHA-256 hex"
    return digest


def _invite_tasks(sql: Any) -> list[tuple]:
    """(id, payload, idempotency_key, state, attempts, available_at, lease_owner, lease_until,
    last_error, finished_at) das tarefas `invite.email`, em ordem de id."""
    return sql.execute(
        """
        SELECT id, payload, idempotency_key, state, attempts, available_at, lease_owner, lease_until,
               last_error, finished_at
          FROM tasks WHERE task_type = 'invite.email' ORDER BY id
        """).fetchall()


def _task(send_id: int, state: str, attempts: int, available_at: datetime, *, owner: str | None = None,
          until: datetime | None = None, last_error: str | None = None,
          finished_at: datetime | None = None) -> tuple:
    """Linha esperada de `_invite_tasks` (sem o id): payload e chave só com o id do envio."""
    return ({"invite_send_id": send_id}, f"invite.email:{send_id}", state, attempts, available_at, owner,
            until, last_error, finished_at)


def _wait_task(smtp: Any, sql: Any, send_id: int, state: str, attempts: int) -> list[tuple]:
    """Espera a tarefa do envio no estado e com as tentativas dadas; devolve todas as tarefas."""

    def ready() -> list[tuple] | None:
        rows = _invite_tasks(sql)
        mine = [row for row in rows if row[1] == {"invite_send_id": send_id}]
        return rows if mine and mine[0][3] == state and mine[0][4] == attempts else None

    return _wait(smtp, ready)


def _wait_task_states(smtp: Any, sql: Any, states: list[str]) -> list[tuple]:
    """Espera as tarefas `invite.email`, em ordem de id, nos estados dados; devolve todas. O registro
    `invite.email` de uma execução sai antes de o worker concluir a tarefa, então esperar o
    estado final da tarefa garante o registro na captura."""

    def ready() -> list[tuple] | None:
        rows = _invite_tasks(sql)
        return rows if [row[3] for row in rows] == states else None

    return _wait(smtp, ready)


def _events_after(client: Any, cursor: int, *extra: str) -> list[dict]:
    """Os eventos de aluno (e os tipos `extra`) depois de `cursor`, sem o `seq` (só conferido)."""
    items = list_events(client, ADMIN_TOKEN, *STUDENT_EVENT_TYPES, *extra, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _ev(type_: str, at: datetime, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _batch_events(offering: dict, batch: Batch, eligible: int, hours: int = DEFAULT_HOURS) -> list[dict]:
    """`student.invites_prepared` e `student.invites_queued {batch}` do lote (ator: token inicial)."""
    return [_ev("student.invites_prepared", batch.t_prep, "bootstrap", offering_id=offering["offering_id"],
                eligible=eligible, valid_hours=hours, expires_at=iso(batch.t_prep + CONFIRMATION)),
            _ev("student.invites_queued", batch.t_conf, "bootstrap", offering_id=offering["offering_id"],
                origin="batch", queued=eligible)]


def _queued(offering: dict, at: datetime, origin: str, queued: int) -> dict:
    return _ev("student.invites_queued", at, "bootstrap", offering_id=offering["offering_id"], origin=origin,
               queued=queued)


def _sent(offering: dict, pseudo: str, origin: str, at: datetime, hours: int = DEFAULT_HOURS) -> dict:
    return _ev("student.invite_sent", at, offering_id=offering["offering_id"], person=pseudo, origin=origin,
               expires_at=iso(at + timedelta(hours=hours)))


def _cancelled(offering: dict, pseudo: str, origin: str, reason: str, at: datetime) -> dict:
    return _ev("student.invite_cancelled", at, offering_id=offering["offering_id"], person=pseudo,
               origin=origin, cancel_reason=reason)


def _revoked(offering: dict, pseudo: str, origin: str, reason: str, at: datetime) -> dict:
    return _ev("student.invite_revoked", at, offering_id=offering["offering_id"], person=pseudo,
               origin=origin, revoke_reason=reason)


def _failed(offering: dict, pseudo: str, origin: str, failure: str, at: datetime) -> dict:
    return _ev("student.invite_failed", at, offering_id=offering["offering_id"], person=pseudo,
               origin=origin, failure=failure)


def _task_failed(task_id: int, at: datetime, attempts: int = 1) -> dict:
    return _ev("task.failed", at, task_id=task_id, task_type="invite.email", attempts=attempts,
               error="PermanentTaskError")


def _log(send_id: int, result: str) -> dict:
    """o registro `invite.email` (INFO, no logger `cq_server.worker`), sem o `time`."""
    return {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email", "invite_send_id": send_id,
            "result": result}


def _log_superseded(send_id: int) -> dict:
    return {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email_superseded",
            "invite_send_id": send_id}


def _invite_logs(cap: Any) -> list[dict]:
    """Os registros `invite.email` e `invite.email_superseded`, na ordem, sem o `time`."""
    return [{k: v for k, v in record.items() if k != "time"} for record in cap.records
            if record.get("event") in ("invite.email", "invite.email_superseded")]


def _assert_logs_clean(cap: Any, sql: Any, addresses: list[str], positive: str) -> None:
    """nenhum registro leva código de convite (nem os nunca entregues), link ou e-mail.
    O detector de código é conferido antes num texto que tem um código (`positive`)."""
    digests = [row[0] for row in sql.execute("SELECT code_sha256 FROM invites").fetchall()]
    assert find_invite_codes(positive, digests), "controle positivo: o detector acha o código"
    assert find_invite_codes(cap.text, digests) == []
    assert "t.me/" not in cap.text
    lowered = cap.text.lower()
    for address in addresses:
        assert address, "controle: e-mail vazio"
        assert address.lower() not in lowered
        assert address.split("@")[0].lower() not in lowered


def _validity(expires_at: datetime, zone: str = ZONE) -> str:
    """`AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = expires_at.astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({zone})"


def _body(link: str, validity: str) -> str:
    return _fill(BODY_TEMPLATE, link=link, validity=validity)


def _link(bot_api: Any, bot: int, code: str) -> str:
    return f"https://t.me/{bot_api.state(bot).username}?start={code}"


def _code_of(message: Any, bot_api: Any, bot: int) -> str:
    """O código do único link do texto decodificado (com o nome do bot do oferecimento); entra em
    `remember_secret`."""
    found = LINK_IN_TEXT.findall(message.text or "")
    assert len(found) == 1, "o e-mail precisa ter exatamente um link de convite"
    username, code = found[0]
    assert username == bot_api.state(bot).username
    remember_secret(code)
    return code


def _assert_email(message: Any, *, to: str, link: str, validity: str, before: datetime, after: datetime,
                  acknowledged: bool = True) -> None:
    """envelope, cabeçalhos e texto decodificado exatos; `Date` (UTC, entre o antes e
    o depois do envio, em tempo real) e `Message-ID` (domínio do remetente) por forma; bruto só ASCII
    (quoted-printable); sem o parâmetro `SMTPUTF8` no MAIL."""
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated, message.acknowledged) == (
        SENDER, (to,), False, True, acknowledged)
    assert [p for p in message.mail_params if not SIZE_PARAM.fullmatch(p)] == []
    names = [name.lower() for name, _ in message.headers]
    assert len(names) == len(set(names)), f"cabeçalho repetido: {names}"
    headers = {name.lower(): value for name, value in message.headers}
    date, message_id = headers.pop("date"), headers.pop("message-id")
    assert headers == {"from": FROM_HEADER, "to": to, "subject": SUBJECT, "mime-version": "1.0",
                       "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    sent_at = parsedate_to_datetime(date)
    assert sent_at.utcoffset() == timedelta(0), f"Date fora de UTC: {date}"
    assert before.replace(microsecond=0) <= sent_at <= after
    assert MESSAGE_ID.fullmatch(message_id), f"Message-ID fora da forma: {message_id}"
    assert message.text == _body(link, validity) + "\n"
    assert message.raw.isascii()


def _updates(sql: Any, bot: int, update_id: int) -> list[tuple]:
    """(id, kind, chat_id, chat_type, sender_id, text, command, state, outcome, processed_at)."""
    return sql.execute(
        """
        SELECT u.id, u.kind, u.chat_id, u.chat_type, u.sender_id, u.text, u.command, u.state, u.outcome,
               u.processed_at
          FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
         WHERE b.telegram_bot_id = %s AND u.update_id = %s
         ORDER BY u.id
        """, (bot, update_id)).fetchall()


def _processed(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update"

    def ready() -> list[tuple] | None:
        rows = _updates(sql, bot, update_id)
        return rows if rows and all(row[7] == "processed" for row in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _private(sender: Any, text: str, command: str, outcome: str, at: datetime) -> tuple:
    """A linha esperada (sem o id) de uma mensagem do chat privado de `sender`."""
    return ("message", sender.user_id, "private", sender.user_id, text, command, "processed", outcome, at)


def _sent_texts(bot_api: Any, bot: int, sender: Any, count: int) -> list[str]:
    """Espera `count` mensagens aceitas pelo `sendMessage` no chat de `sender`; devolve os textos."""
    bot_api.wait_for(lambda: len(bot_api.sent_messages(bot, chat_id=sender.user_id)) >= count,
                     timeout=PROCESS_TIMEOUT)
    return [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)]


def _no_code_in_database(db_url: str, digests: list[str], positive: str) -> list[tuple[str, str]]:
    """(tabela, coluna) de todo valor do banco com um código de convite em claro; o detector é
    conferido antes num texto que tem o código."""
    assert find_invite_codes(positive, digests), "controle positivo: o detector acha o código"
    return sorted({(table, column) for table, column, text in scan_all_tables(db_url)
                   if find_invite_codes(text, digests)})


# ---------------------------------------------------------------------------------------------
# Envio do lote


def test_batch_sends_one_exact_email_per_student_and_stores_only_the_digest(
        client: Any, clock: Any, sql: Any, db_url: str, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any, mail_channel: Any, queue: Any) -> None:
    """Envio em lote, com o e-mail no SMTP falso: a
    confirmação de 3 alunos gera 3 envios `batch` e 3 tarefas `invite.email` (payload
    `{"invite_send_id": <id>}` e chave `invite.email:<id>`, só ids) em ordem de `enrollments.id`; o
    worker manda um e-mail por aluno, uma conexão por envio, no endereço da lista (envelope e `To` com
    o `ascii_email`), com assunto e corpo exatos, o link
    `https://t.me/<bot>?start=<22>` e a validade no fuso do oferecimento, contada do envio (não do
    preparo nem da confirmação); o banco guarda só o resumo (`invites.code_sha256` = SHA-256 do código
    do e-mail; nenhum código em claro em coluna nenhuma), com o convite ligado ao envio da mesma
    matrícula; `invite_sends` `sent` com `attempts = 1`; `student.invite_sent` exato; um `invite.email`
    `sent` por execução, sem e-mail, código nem link nos logs. Uma tarefa reexecutada depois de
    concluída (payload do 1º envio, sem chave) termina `done`, com `invite.email` `skipped`, e nada
    muda: nenhum e-mail, evento, convite ou envio novo."""
    offering, bot = _offering(client, bot_api, "mc102-convites-lote", "cq_convites_lote_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A, ROW_B, ROW_C])
    t_send = clock.advance(STEP)
    assert _validity(t_send + timedelta(hours=DEFAULT_HOURS)) == "2031-01-07 21:03 -03 (America/Sao_Paulo)"

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        sends = _wait_send_states(smtp, sql, ["sent", "sent", "sent"])
        _wait(smtp, lambda: len(smtp.messages()) == 3)
        after = _utcnow()
        students = _students(sql, offering)
        assert [row[1] for row in sends] == [row[0] for row in students]
        send_ids = [row[0] for row in sends]
        tasks = _wait(smtp, lambda: (lambda rows: rows if all(r[3] == "done" for r in rows) else None)(
            _invite_tasks(sql)))

        assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 1, batch.t_conf,
                                      t_send) for _, pseudo, _ in students]
        # A constante do tipo (real no esqueleto) junto de um caso que falha nele.
        assert INVITE_EMAIL_TASK_TYPE == "invite.email"
        assert [row[1:] for row in tasks] == [_task(sid, "done", 1, batch.t_conf, finished_at=t_send)
                                              for sid in send_ids]
        messages = smtp.messages()
        assert [c.outcome for c in smtp.connections()] == ["quit", "quit", "quit"]
        codes = []
        for message, (_, _, ra) in zip(messages, students, strict=True):
            code = _code_of(message, bot_api, bot)
            codes.append(code)
            assert INVITE_CODE.fullmatch(code)
            _assert_email(message, to=ENVELOPE[ra], link=_link(bot_api, bot, code),
                          validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
        assert len(set(codes)) == 3
        assert _invite_view(_invites(sql)) == [
            _invite(sid, pseudo, _sha256(code), "pending", t_send)
            for sid, (_, pseudo, _), code in zip(send_ids, students, codes, strict=True)]
        assert _events_after(client, batch.cursor) == [
            *_batch_events(offering, batch, 3),
            *(_sent(offering, pseudo, "batch", t_send) for _, pseudo, _ in students)]
        digests = [_sha256(code) for code in codes]
        assert _no_code_in_database(db_url, digests, messages[0].text) == []
        assert _invite_logs(cap) == [_log(sid, "sent") for sid in send_ids]

        # Reexecução depois de concluída: envio não `pending` → retorna sem efeito (`skipped`).
        cursor = _last_seq(sql)
        rerun_id = queue.enqueue("invite.email", {"invite_send_id": send_ids[0]})
        record = _wait(smtp, lambda: (lambda r: r if r.state in ("done", "failed") else None)(queue.get(rerun_id)))
        assert record == TaskRecord(id=rerun_id, task_type="invite.email", state="done", attempts=1,
                                    available_at=t_send, lease_owner=None, lease_until=None, last_error=None,
                                    finished_at=t_send)
        assert _sends(sql) == sends
        assert _invite_view(_invites(sql)) == [
            _invite(sid, pseudo, _sha256(code), "pending", t_send)
            for sid, (_, pseudo, _), code in zip(send_ids, students, codes, strict=True)]
        assert len(smtp.messages()) == 3 and len(smtp.connections()) == 3
        assert _events_after(client, cursor, "task.failed") == []
        assert _invite_logs(cap) == [*(_log(sid, "sent") for sid in send_ids), _log(send_ids[0], "skipped")]
    _assert_logs_clean(cap, sql, [row["email"] for row in (ROW_A, ROW_B, ROW_C)], messages[0].text)
    assert default.calls == []


def test_validity_counts_from_the_send_in_the_offering_timezone(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """A validade conta do momento em que a tarefa gera o código, e o e-mail mostra a
    validade no fuso do oferecimento, e só ele: lote com `valid_hours = 1` num
    oferecimento em `Asia/Kolkata`; o worker só roda 2 h depois da confirmação (o convite já teria
    vencido se contasse do preparo); o convite vence 1 h depois do envio, o `student.invite_sent` e o
    e-mail trazem essa validade (`2031-01-01 08:32 IST (Asia/Kolkata)`), e o `invite_sends` guarda
    `valid_hours = 1`."""
    zone = "Asia/Kolkata"
    offering, bot = _offering(client, bot_api, "mc102-convites-fuso", "cq_convites_fuso_bot", zone=zone)
    batch = _batch(client, clock, sql, offering, [ROW_B], valid_hours=1)
    t_send = clock.advance(2 * 3600)
    assert _validity(t_send + timedelta(hours=1), zone) == "2031-01-01 08:32 IST (Asia/Kolkata)"

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        sends = _wait_send_states(smtp, sql, ["sent"])
        message = _wait(smtp, lambda: smtp.messages())[0]
        after = _utcnow()
        tasks = _wait_task_states(smtp, sql, ["done"])
    ((sid, _, pseudo, *_),) = sends
    code = _code_of(message, bot_api, bot)
    _assert_email(message, to=ENVELOPE[ROW_B["ra"]], link=_link(bot_api, bot, code),
                  validity="2031-01-01 08:32 IST (Asia/Kolkata)", before=before, after=after)
    assert _send_view(sends) == [(pseudo, "batch", 1, "sent", None, None, None, 1, batch.t_conf, t_send)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 1, batch.t_conf, finished_at=t_send)]
    assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, _sha256(code), "pending", t_send, hours=1)]
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1, hours=1),
                                                   _sent(offering, pseudo, "batch", t_send, hours=1)]
    assert _invite_logs(cap) == [_log(sid, "sent")]
    assert default.calls == []


# ---------------------------------------------------------------------------------------------
# Cancelamentos na T1


def test_student_linked_between_confirmation_and_send_is_cancelled_on_retry(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O lote não reenvia a aluno já vinculado, e o convite é de uso único (do e-mail
    ao vínculo, com o termo e as boas-vindas uma vez): a 1ª execução do envio
    falha depois de o SMTP falso aceitar a mensagem (`after_accept` no fim do DATA): temporária
    (`RetryTaskLater`, espera de 2 s), e-mail capturado sem confirmação; com o código desse e-mail, o
    `/start` apresenta o `TERMS` e o `/aceito` vincula (`WELCOME_STUDENT`, convite `used`, matrícula
    `active` com a versão 1, endereço `active`, `student.joined`); na repetição, a T1 cancela o envio
    (`cancelled`/`already_linked`, `attempts = 1`, os códigos gerados antes), com
    `student.invite_cancelled` exato, sem outro e-mail; registros `invite.email` `retry` e `cancelled`.
    Depende do vínculo pelo bot."""
    offering, bot = _offering(client, bot_api, "mc102-convites-vinculado", "cq_convites_vinculado_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    t_send = clock.advance(STEP)
    smtp.fail_network("data_end", "reset", after_accept=True)
    sender = make_user(3101)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["pending"])
        tasks = _wait_task(smtp, sql, sid, "pending", 1)
        assert [row[1:] for row in tasks] == [_task(sid, "pending", 1, t_send + FIRST_RETRY_WAIT,
                                                    last_error="RetryTaskLater")]
        (message,) = smtp.messages()
        after = _utcnow()
        code = _code_of(message, bot_api, bot)
        _assert_email(message, to=ENVELOPE[ROW_A["ra"]], link=_link(bot_api, bot, code),
                      validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after,
                      acknowledged=False)
        assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, _sha256(code), "pending", t_send)]

        start = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code))
        assert start[1:] == _private(sender, f"/start {code}", "/start", "student_terms_presented", t_send)
        accept = _processed(bot_api, sql, bot, bot_api.push_text(bot, sender, "/aceito", bot_command=True))
        assert accept[1:] == _private(sender, "/aceito", "/aceito", "student_joined", t_send)
        assert _sent_texts(bot_api, bot, sender, 2) == [TERMS, WELCOME_STUDENT]
        assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, _sha256(code), "used", t_send, used=t_send)]

        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["cancelled"])
        tasks = _wait_task(smtp, sql, sid, "done", 2)
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "already_linked", None, 1,
                                  batch.t_conf, t_retry)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_send + FIRST_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_retry)]
    assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, _sha256(code), "used", t_send, used=t_send)]
    assert sql.execute(
        "SELECT e.role, e.state, e.activated_at, e.terms_version, e.terms_accepted_at FROM enrollments e "
        "JOIN people p ON p.id = e.person_id WHERE p.pseudo_id::text = %s", (pseudo,)).fetchall() == [
        ("student", "active", t_send, 1, t_send)]
    assert sql.execute(
        "SELECT p.pseudo_id::text, b.telegram_bot_id, d.channel, d.chat_id, d.state, d.created_at, d.changed_at "
        "FROM delivery_endpoints d JOIN people p ON p.id = d.person_id JOIN bots b ON b.id = d.bot_id "
        "ORDER BY d.id").fetchall() == [(pseudo, bot, "telegram", sender.user_id, "active", t_send, t_send)]
    assert len(smtp.messages()) == 1
    assert [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)] == [TERMS, WELCOME_STUDENT]
    assert _events_after(client, batch.cursor) == [
        *_batch_events(offering, batch, 1),
        _ev("student.joined", t_send, offering_id=offering["offering_id"], person=pseudo, terms_version=1,
            endpoint_replaced=False),
        _cancelled(offering, pseudo, "batch", "already_linked", t_retry)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log(sid, "cancelled")]
    _assert_logs_clean(cap, sql, [ROW_A["email"]], message.text)
    assert default.calls == []


def test_self_service_send_to_a_student_linked_before_its_retry_is_cancelled(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O autosserviço, como o lote, nunca envia a aluno vinculado, e isso é
    conferido de novo na hora do envio; na T1, o aluno já vinculado (origem
    `self_service` com a matrícula `active` → `cancelled`/`already_linked`): aluno
    importado, sem lote; o `/convite` com o e-mail da lista cria o envio S (`self_service`,
    `invite_requested`, `INVITE_REQUESTED` ao chat, texto guardado mascarado); a 1ª execução de S gera
    o código e cai depois de o SMTP falso aceitar a mensagem (`after_accept` no fim do DATA:
    temporária, `RetryTaskLater`, espera de 2 s, e-mail capturado sem confirmação); com o código desse
    e-mail, o `/start` apresenta o `TERMS` e o `/aceito` vincula no mesmo chat (`WELCOME_STUDENT`,
    convite `used`, matrícula `active` com a versão 1, `student.joined`); na repetição, a T1 cancela S
    (`cancelled`/`already_linked`, `attempts = 1`, o código gerado antes), com
    `student.invite_cancelled {origin: self_service, cancel_reason: already_linked}` exato, sem revogar
    nem gerar código (o convite usado fica intacto) e sem conectar ao SMTP (um só e-mail, uma só
    conexão); registros `invite.email` `retry` e `cancelled`; a adesão dá `linked`. Depende do
    vínculo pelo bot e do autosserviço."""
    offering, bot = _offering(client, bot_api, "mc102-convites-auto-vinculado", "cq_convites_auto_vinc_bot")
    _import(client, offering, [ROW_B])
    cursor = _last_seq(sql)
    ((_, pseudo, _),) = _students(sql, offering)
    t_request = clock.advance(STEP)
    smtp.fail_network("data_end", "reset", after_accept=True)
    sender = make_user(3105)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        request = _processed(bot_api, sql, bot,
                             bot_api.push_text(bot, sender, f"/convite {ROW_B['email']}", bot_command=True))
        assert request[1:] == _private(sender, "/convite [e-mail]", "/convite", "invite_requested", t_request)
        assert _sent_texts(bot_api, bot, sender, 1) == [INVITE_REQUESTED]
        ((sid, *_),) = _wait_send_states(smtp, sql, ["pending"])
        tasks = _wait_task(smtp, sql, sid, "pending", 1)
        assert [row[1:] for row in tasks] == [_task(sid, "pending", 1, t_request + FIRST_RETRY_WAIT,
                                                    last_error="RetryTaskLater")]
        (message,) = smtp.messages()
        after = _utcnow()
        code = _code_of(message, bot_api, bot)
        _assert_email(message, to=ENVELOPE[ROW_B["ra"]], link=_link(bot_api, bot, code),
                      validity=_validity(t_request + timedelta(hours=DEFAULT_HOURS)), before=before, after=after,
                      acknowledged=False)
        assert _send_view(_sends(sql)) == [(pseudo, "self_service", DEFAULT_HOURS, "pending", None, None, None, 1,
                                            t_request, None)]
        assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, _sha256(code), "pending", t_request)]

        start = _processed(bot_api, sql, bot, bot_api.push_start(bot, sender, code))
        assert start[1:] == _private(sender, f"/start {code}", "/start", "student_terms_presented", t_request)
        accept = _processed(bot_api, sql, bot, bot_api.push_text(bot, sender, "/aceito", bot_command=True))
        assert accept[1:] == _private(sender, "/aceito", "/aceito", "student_joined", t_request)
        assert _sent_texts(bot_api, bot, sender, 3) == [INVITE_REQUESTED, TERMS, WELCOME_STUDENT]
        used = _invites(sql)
        assert _invite_view(used) == [_invite(sid, pseudo, _sha256(code), "used", t_request, used=t_request)]

        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["cancelled"])
        tasks = _wait_task(smtp, sql, sid, "done", 2)
    assert _send_view(sends) == [(pseudo, "self_service", DEFAULT_HOURS, "cancelled", None, "already_linked", None,
                                  1, t_request, t_retry)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_request + FIRST_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_retry)]
    assert _invites(sql) == used  # o convite usado fica intacto: nada revogado, nenhum código novo
    assert sql.execute(
        "SELECT e.role, e.state, e.activated_at, e.terms_version, e.terms_accepted_at FROM enrollments e "
        "JOIN people p ON p.id = e.person_id WHERE p.pseudo_id::text = %s", (pseudo,)).fetchall() == [
        ("student", "active", t_request, 1, t_request)]
    assert sql.execute(
        "SELECT p.pseudo_id::text, b.telegram_bot_id, d.channel, d.chat_id, d.state, d.created_at, d.changed_at "
        "FROM delivery_endpoints d JOIN people p ON p.id = d.person_id JOIN bots b ON b.id = d.bot_id "
        "ORDER BY d.id").fetchall() == [(pseudo, bot, "telegram", sender.user_id, "active", t_request, t_request)]
    assert len(smtp.messages()) == 1
    assert len(smtp.connections()) == 1  # só a 1ª execução conectou; a repetição cancelou antes
    assert [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)] == [
        INVITE_REQUESTED, TERMS, WELCOME_STUDENT]
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == {
        "offering": _ref(offering), "enrolled": 1, "no_invite": 0, "sending": 0, "invite_valid": 0,
        "terms_presented": 0, "invite_lapsed": 0, "send_failed": 0, "linked": 1}
    assert _events_after(client, cursor, "task.failed") == [
        _ev("student.joined", t_request, offering_id=offering["offering_id"], person=pseudo, terms_version=1,
            endpoint_replaced=False),
        _cancelled(offering, pseudo, "self_service", "already_linked", t_retry)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log(sid, "cancelled")]
    _assert_logs_clean(cap, sql, [ROW_B["email"]], message.text)
    assert default.calls == []


@pytest.mark.parametrize("final_state", ["closed", "archived"])
def test_inactive_offering_cancels_the_pending_send_without_email(
        final_state: str, client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any,
        settings: Any, smtp: Any, mail_channel: Any) -> None:
    """T1 com o oferecimento inativo: lote confirmado com o worker
    parado; o oferecimento é encerrado (ou encerrado e arquivado) antes da T1; ao subir o worker, o
    envio vira `cancelled`/`offering_inactive` com `attempts = 0` (o cancelamento vem na 1ª execução),
    nenhum convite de aluno é criado, nenhuma conexão chega ao SMTP falso, `student.invite_cancelled`
    exato e um `invite.email` `cancelled`; a tarefa termina `done`."""
    offering, _ = _offering(client, bot_api, f"mc102-convites-{final_state}", f"cq_convites_{final_state}_bot")
    batch = _batch(client, clock, sql, offering, [ROW_C])
    ref = offering["code"]
    current = _ok(client.get(f"/v1/offerings/{ref}", headers=auth(ADMIN_TOKEN)), "offerings.get")
    t_close = clock.advance(STEP)
    closed = _ok(client.post(f"/v1/offerings/{ref}/close", headers=auth(ADMIN_TOKEN)), "offerings.close")
    assert closed == {**current, "state": "closed", "closed_at": iso(t_close)}
    if final_state == "archived":
        t_archive = clock.advance(STEP)
        prepared = _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=auth(ADMIN_TOKEN)),
                       "offerings.prepare_archive")
        assert prepared == {"offering": closed, "team_members_active": 0, "pending_links": 0,
                            "confirmation_code": prepared["confirmation_code"],
                            "expires_at": iso(t_archive + CONFIRMATION)}
        archived = _ok(client.post(f"/v1/offerings/{ref}/archive",
                                   json={"confirmation_code": prepared["confirmation_code"]},
                                   headers=auth(ADMIN_TOKEN)), "offerings.archive")
        assert archived == {**closed, "state": "archived", "archived_at": iso(t_archive)}
    t_run = clock.advance(STEP)

    with capture_logs() as cap:
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        sends = _wait_send_states(smtp, sql, ["cancelled"])
        ((sid, _, pseudo, *_),) = sends
        tasks = _wait_task(smtp, sql, sid, "done", 1)
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "offering_inactive", None, 0,
                                  batch.t_conf, t_run)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 1, batch.t_conf, finished_at=t_run)]
    assert _invites(sql) == []
    assert smtp.messages() == [] and smtp.connections() == []
    assert _events_after(client, batch.cursor) == [
        *_batch_events(offering, batch, 1), _cancelled(offering, pseudo, "batch", "offering_inactive", t_run)]
    assert _invite_logs(cap) == [_log(sid, "cancelled")]
    assert default.calls == []


def test_offering_closed_after_a_temporary_failure_cancels_with_the_codes_generated(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O `attempts` conta os códigos gerados antes do cancelamento: a 1ª execução
    gera o código e recebe RCPT 451 (temporária, `RetryTaskLater`); o oferecimento é encerrado; a
    repetição cancela na T1 (`cancelled`/`offering_inactive`, `attempts = 1`), sem revogar nem gerar
    código (o convite da 1ª execução fica como estava, e o encerramento não o revoga) e sem e-mail;
    `student.invite_cancelled` exato; registros `retry` e `cancelled`."""
    offering, _ = _offering(client, bot_api, "mc102-convites-encerrado-depois", "cq_convites_enc_depois_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    t_send = clock.advance(STEP)
    smtp.fail("rcpt", 451)

    with capture_logs() as cap:
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["pending"])
        _wait_task(smtp, sql, sid, "pending", 1)
        invites = _invites(sql)
        digest = _digest_of_unknown(invites, 0)
        assert _invite_view(invites) == [_invite(sid, pseudo, digest, "pending", t_send)]
        _settle(smtp, sql)
        t_close = clock.advance(STEP)
        ref = offering["code"]
        current = _ok(client.get(f"/v1/offerings/{ref}", headers=auth(ADMIN_TOKEN)), "offerings.get")
        closed = _ok(client.post(f"/v1/offerings/{ref}/close", headers=auth(ADMIN_TOKEN)), "offerings.close")
        assert closed == {**current, "state": "closed", "closed_at": iso(t_close)}
        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["cancelled"])
        tasks = _wait_task(smtp, sql, sid, "done", 2)
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "offering_inactive", None, 1,
                                  batch.t_conf, t_retry)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_send + FIRST_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_retry)]
    assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, digest, "pending", t_send)]
    assert smtp.messages() == []
    assert len(smtp.connections()) == 1  # só a 1ª execução conectou; a repetição cancelou antes
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1),
                                                   _cancelled(offering, pseudo, "batch", "offering_inactive",
                                                              t_retry)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log(sid, "cancelled")]
    assert default.calls == []


def test_bot_without_access_after_confirmation_does_not_stop_the_send(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """A tarefa não confere o bot: depois da confirmação, o token do bot é revogado na
    Bot API falsa e o bot fica `no_access` (`token_rejected`); a 1ª execução do envio recebe RCPT 451
    (temporária); com o bot já `no_access`, a repetição revoga o convite anterior (`replaced`), gera
    outro código e manda o e-mail: `invite_sends` `sent` com `attempts = 2`, `student.invite_revoked` e
    `student.invite_sent` exatos e nenhum `student.invite_cancelled`."""
    offering, bot = _offering(client, bot_api, "mc102-convites-sem-acesso", "cq_convites_sem_acesso_bot")
    batch = _batch(client, clock, sql, offering, [ROW_B])
    t_send = clock.advance(STEP)
    smtp.fail("rcpt", 451)
    bot_api.revoke(bot)

    def bot_row() -> tuple:
        return sql.execute("SELECT state, access_problem FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()

    with capture_logs() as cap:
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["pending"])
        _wait_task(smtp, sql, sid, "pending", 1)
        _wait(smtp, lambda: bot_row() == ("no_access", "token_rejected"))
        first = _invites(sql)
        digest1 = _digest_of_unknown(first, 0)
        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        before = _utcnow()
        sends = _wait_send_states(smtp, sql, ["sent"])
        message = _wait(smtp, lambda: smtp.messages())[0]
        after = _utcnow()
        tasks = _wait_task(smtp, sql, sid, "done", 2)
    assert bot_row() == ("no_access", "token_rejected")
    code = _code_of(message, bot_api, bot)
    _assert_email(message, to=ENVELOPE[ROW_B["ra"]], link=_link(bot_api, bot, code),
                  validity=_validity(t_retry + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert len(smtp.messages()) == 1
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 2, batch.t_conf,
                                  t_retry)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_send + FIRST_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_retry)]
    assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, digest1, "revoked", t_send, revoked=t_retry),
                                           _invite(sid, pseudo, _sha256(code), "pending", t_retry)]
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1),
                                                   _revoked(offering, pseudo, "batch", "replaced", t_retry),
                                                   _sent(offering, pseudo, "batch", t_retry)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log(sid, "sent")]
    assert default.calls == []


def test_unknown_send_fails_permanently_and_logs_skipped(
        client: Any, clock: Any, sql: Any, queue: Any, worker_thread: Any, smtp: Any, mail_channel: Any) -> None:
    """Envio inexistente: uma tarefa `invite.email` com
    `{"invite_send_id": <id inexistente>}` termina `failed` na 1ª tentativa, com `last_error =
    "PermanentTaskError"`, o `task.failed` da fila com o mesmo nome, um registro `invite.email` com
    `result = skipped` e o id do payload, e nenhuma conexão ao SMTP falso. O tratador entra também no
    `run_worker` sem bots (registrado nos dois caminhos do worker)."""
    t0 = clock.now()
    cursor = _last_seq(sql)
    with capture_logs() as cap:
        worker_thread(channel=_DefaultChannel(), verification=mail_channel)
        task_id = queue.enqueue("invite.email", {"invite_send_id": MISSING_SEND_ID})
        record = _wait(smtp, lambda: (lambda r: r if r.state in ("done", "failed") else None)(queue.get(task_id)))
    assert record == TaskRecord(id=task_id, task_type="invite.email", state="failed", attempts=1,
                                available_at=t0, lease_owner=None, lease_until=None,
                                last_error="PermanentTaskError", finished_at=t0)
    assert _events_after(client, cursor, "task.failed") == [_task_failed(task_id, t0)]
    assert _invite_logs(cap) == [_log(MISSING_SEND_ID, "skipped")]
    assert smtp.connections() == []


# ---------------------------------------------------------------------------------------------
# Repetição e execução superada


@pytest.mark.parametrize("mode", ["temporary", "after_accept", "crash"])
def test_repetition_generates_a_new_code_and_only_the_last_one_is_pending(
        mode: str, client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any, mail_channel: Any) -> None:
    """Envio repetido por falha incerta: a 1ª execução
    gera o código 1 e não chega ao `sent` por (a) RCPT 451 (temporária programada, sem e-mail), (b)
    queda da conexão depois de o SMTP falso aceitar a mensagem (`after_accept`: e-mail capturado sem
    confirmação) ou (c) queda do worker entre a T1 e a T2 (`Crash` do `ScriptedVerificationChannel`
    depois de o SMTP falso aceitar; a tarefa fica em posse, e outro worker a retoma depois de a posse
    vencer no `FakeClock`); a repetição revoga o convite 1 com `student.invite_revoked {replaced}`,
    gera o código 2 e manda o e-mail: `sent` com `attempts = 2`, no máximo um convite pendente, e só o
    do último código gerado (o aluno pode ter recebido dois e-mails). Registros: `retry` e `sent` em
    (a) e (b); só `sent` em (c) (a execução que caiu não chega ao fim da T2)."""
    offering, bot = _offering(client, bot_api, f"mc102-repeticao-{mode.replace('_', '-')}",
                              f"cq_repeticao_{mode}_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    t_send = clock.advance(STEP)
    verification: Any = mail_channel
    if mode == "temporary":
        smtp.fail("rcpt", 451)
    elif mode == "after_accept":
        smtp.fail_network("data_end", "reset", after_accept=True)
    else:
        verification = ScriptedVerificationChannel([Delegate(then=Crash()), DELEGATE], delegate=mail_channel)

    with capture_logs() as cap:
        before = _utcnow()
        worker, default = _start_worker(bot_worker_thread, settings, verification)
        defaults = [default]
        ((sid, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["pending"])
        if mode == "crash":
            worker.join(PROCESS_TIMEOUT)
            assert not worker.is_alive(), "o worker devia ter caído com o Crash"
            assert type(worker.error) is Crash
            assert [row[1:] for row in _invite_tasks(sql)] == [
                _task(sid, "leased", 1, batch.t_conf, owner=WORKER_ID, until=t_send + TASK_LEASE)]
        else:
            tasks = _wait_task(smtp, sql, sid, "pending", 1)
            assert [row[1:] for row in tasks] == [_task(sid, "pending", 1, t_send + FIRST_RETRY_WAIT,
                                                        last_error="RetryTaskLater")]
        first = _invites(sql)
        assert [row[4] for row in first] == ["pending"]
        if mode != "crash":  # na queda, a tarefa fica em posse do worker que caiu, de propósito
            _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        if mode == "crash":
            _, default = _start_worker(bot_worker_thread, settings, verification, worker_id=RESUME_WORKER_ID)
            defaults.append(default)
        sends = _wait_send_states(smtp, sql, ["sent"])
        count = 1 if mode == "temporary" else 2
        messages = _wait(smtp, lambda: (lambda m: m if len(m) == count else None)(smtp.messages()))
        after = _utcnow()
        tasks = _wait_task(smtp, sql, sid, "done", 2)

    codes = [_code_of(message, bot_api, bot) for message in messages]
    envelope = ENVELOPE[ROW_A["ra"]]
    if mode == "temporary":
        digest1 = _digest_of_unknown(first, 0)
    else:
        digest1 = _sha256(codes[0])
        _assert_email(messages[0], to=envelope, link=_link(bot_api, bot, codes[0]),
                      validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after,
                      acknowledged=mode == "crash")
    _assert_email(messages[-1], to=envelope, link=_link(bot_api, bot, codes[-1]),
                  validity=_validity(t_retry + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert _invite_view(first) == [_invite(sid, pseudo, digest1, "pending", t_send)]
    assert _invite_view(_invites(sql)) == [_invite(sid, pseudo, digest1, "revoked", t_send, revoked=t_retry),
                                           _invite(sid, pseudo, _sha256(codes[-1]), "pending", t_retry)]
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 2, batch.t_conf,
                                  t_retry)]
    if mode == "crash":
        assert [row[1:] for row in tasks] == [_task(sid, "done", 2, batch.t_conf, finished_at=t_retry)]
        assert [(m.address, m.subject) for m in verification.calls] == [(ROW_A["email"], SUBJECT)] * 2
        assert [m.body for m in verification.calls] == [
            _body(_link(bot_api, bot, codes[0]), _validity(t_send + timedelta(hours=DEFAULT_HOURS))),
            _body(_link(bot_api, bot, codes[1]), _validity(t_retry + timedelta(hours=DEFAULT_HOURS)))]
        assert [type(result) for result in verification.delegated] == [Delivered, Delivered]
    else:
        assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_send + FIRST_RETRY_WAIT,
                                                    last_error="RetryTaskLater", finished_at=t_retry)]
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1),
                                                   _revoked(offering, pseudo, "batch", "replaced", t_retry),
                                                   _sent(offering, pseudo, "batch", t_retry)]
    expected_logs = [_log(sid, "sent")] if mode == "crash" else [_log(sid, "retry"), _log(sid, "sent")]
    assert _invite_logs(cap) == expected_logs
    _assert_logs_clean(cap, sql, [ROW_A["email"]], messages[-1].text)
    assert [d.calls for d in defaults] == [[] for _ in defaults]


def test_superseded_t2_writes_nothing_and_keeps_the_state_of_the_other_execution(
        client: Any, clock: Any, sql: Any, db_url: str, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any, mail_channel: Any) -> None:
    """T2 superada, com a montagem pelo gancho `on_send`: durante o `send` da 1ª
    execução (código 1 já gerado), o gancho do `ScriptedVerificationChannel` avança o `FakeClock` além
    da posse e roda outra execução da mesma tarefa pela fila (`TaskQueue.work_once` com o tratador de
    `students.invite_email_task_handler`), que revoga o convite 1 (`replaced`), gera o código 2 (convite
    de `id` maior), manda o e-mail e grava `sent`; a execução antiga, ao voltar do envio (o e-mail do
    código 1 também sai), acha o envio fora de `pending` e retorna sem gravar: um só
    `student.invite_sent`, o convite 1 continua revogado, e a tarefa fica no estado deixado pela outra
    execução (`done`, `attempts = 2`, `finished_at` dela; o `complete` da antiga não acha a posse e o
    `task.processed` dela lê `done`). Registros: `invite.email` `sent` da outra execução e só
    `invite.email_superseded` da antiga (sem `invite.email`)."""
    offering, bot = _offering(client, bot_api, "mc102-convites-superada", "cq_convites_superada_bot")
    batch = _batch(client, clock, sql, offering, [ROW_C])
    t_send = clock.advance(STEP)
    nested: list[tuple[datetime, int | None]] = []
    with contextlib.ExitStack() as resources:
        other_queue = TaskQueue(db_url, clock=clock, settings=settings)
        resources.callback(other_queue.close)
        other_handler = invite_email_task_handler(db_url, clock=clock, settings=settings, channel=mail_channel)
        resources.callback(other_handler.close)

        def run_other_execution(message: VerificationMessage) -> None:
            if nested:
                return
            at = clock.advance(STEP)
            nested.append((at, other_queue.work_once(OTHER_WORKER_ID, {"invite.email": other_handler})))

        verification = ScriptedVerificationChannel([DELEGATE], delegate=mail_channel,
                                                   on_send=run_other_execution)
        with capture_logs() as cap:
            before = _utcnow()
            _, default = _start_worker(bot_worker_thread, settings, verification)
            messages = _wait(smtp, lambda: (lambda m: m if len(m) == 2 else None)(smtp.messages()))
            sends = _wait_send_states(smtp, sql, ["sent"])
            ((sid, _, pseudo, *_),) = sends
            tasks = _wait_task(smtp, sql, sid, "done", 2)
            ((task_id, *_),) = tasks

            def processed() -> list[tuple[int, str]]:
                return [(r["id"], r["state"]) for r in cap.with_event("task.processed") if r.get("id") == task_id]

            # O `task.processed` da execução antiga sai depois do `complete` dela (que não acha a posse).
            _wait(smtp, lambda: len(processed()) == 2)
            after = _utcnow()

    ((t_other, nested_task),) = nested
    assert nested_task == task_id
    code2, code1 = (_code_of(message, bot_api, bot) for message in messages)
    envelope = ENVELOPE[ROW_C["ra"]]
    _assert_email(messages[0], to=envelope, link=_link(bot_api, bot, code2),
                  validity=_validity(t_other + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    _assert_email(messages[1], to=envelope, link=_link(bot_api, bot, code1),
                  validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert _invite_view(_invites(sql)) == [
        _invite(sid, pseudo, _sha256(code1), "revoked", t_send, revoked=t_other),
        _invite(sid, pseudo, _sha256(code2), "pending", t_other)]
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 2, batch.t_conf,
                                  t_other)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, batch.t_conf, finished_at=t_other)]
    assert [(m.address, m.subject) for m in verification.calls] == [(ROW_C["email"], SUBJECT)]
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1),
                                                   _revoked(offering, pseudo, "batch", "replaced", t_other),
                                                   _sent(offering, pseudo, "batch", t_other)]
    assert _invite_logs(cap) == [_log(sid, "sent"), _log_superseded(sid)]
    assert processed() == [(task_id, "done"), (task_id, "done")]
    _assert_logs_clean(cap, sql, [ROW_C["email"]], messages[0].text)
    assert default.calls == []


def test_superseded_t2_with_the_send_still_pending_writes_nothing(
        client: Any, clock: Any, sql: Any, db_url: str, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any, mail_channel: Any) -> None:
    """Na T2, no ramo em que o envio ainda está `pending`, mas o convite
    desta execução não é o de maior `id` do envio; T2 superada (montagem pelo gancho
    `on_send`): durante o `send` da 1ª execução (código 1 já gerado), o gancho programa RCPT 451 só para
    a conexão seguinte, avança o `FakeClock` além da posse e roda outra execução da mesma tarefa pela
    fila (`TaskQueue.work_once` com o tratador de `students.invite_email_task_handler` sobre o adaptador
    real), que revoga o convite 1 (`student.invite_revoked {replaced}`), gera o código 2 (convite de `id`
    maior) e recebe a falha temporária: nada gravado no envio, `RetryTaskLater`, a tarefa `pending` com a
    espera da fila de 4 s (2ª tentativa) e o registro `invite.email` `retry`. A execução antiga volta com
    `Delivered` (o e-mail do código 1 sai), acha o envio ainda `pending` com um convite mais novo e
    retorna sem gravar: o envio segue `pending` com `attempts = 2`, sem `student.invite_sent` e sem
    `sent`, o convite 1 continua revogado e o 2 pendente; a tarefa fica no estado deixado pela outra
    execução (o `complete` da antiga não acha a posse, e o `task.processed` dela lê `pending`); só
    `invite.email_superseded` da antiga. Depois da espera, a 3ª tentativa revoga o convite 2
    (`replaced`), gera o código 3 e grava `sent` com `attempts = 3`: no fim, um só convite pendente, o do
    último código gerado, e um só `student.invite_sent`."""
    offering, bot = _offering(client, bot_api, "mc102-convites-superada-pendente", "cq_convites_sup_pend_bot")
    batch = _batch(client, clock, sql, offering, [ROW_B])
    ((task_id, *_),) = _invite_tasks(sql)
    ((sid, _, pseudo, *_),) = _sends(sql)
    t_send = clock.advance(STEP)
    nested: list[tuple[datetime, int | None]] = []
    with contextlib.ExitStack() as resources:
        other_queue = TaskQueue(db_url, clock=clock, settings=settings)
        resources.callback(other_queue.close)
        other_handler = invite_email_task_handler(db_url, clock=clock, settings=settings, channel=mail_channel)
        resources.callback(other_handler.close)

        def run_other_execution(message: VerificationMessage) -> None:
            if nested:
                return
            smtp.fail("rcpt", 451)  # uma vez: a próxima RCPT é a da execução aninhada
            at = clock.advance(STEP)
            nested.append((at, other_queue.work_once(OTHER_WORKER_ID, {"invite.email": other_handler})))

        verification = ScriptedVerificationChannel([DELEGATE, DELEGATE], delegate=mail_channel,
                                                   on_send=run_other_execution)
        with capture_logs() as cap:
            before = _utcnow()
            _, default = _start_worker(bot_worker_thread, settings, verification)

            def processed() -> list[tuple[int, str]]:
                return [(r["id"], r["state"]) for r in cap.with_event("task.processed") if r.get("id") == task_id]

            # A aninhada e a antiga terminaram: o `task.processed` da antiga sai depois do `complete` dela.
            _wait(smtp, lambda: len(processed()) == 2)
            (first,) = _wait(smtp, lambda: (lambda m: m if len(m) == 1 else None)(smtp.messages()))
            ((t_other, nested_task),) = nested
            assert nested_task == task_id
            assert processed() == [(task_id, "pending"), (task_id, "pending")]
            code1 = _code_of(first, bot_api, bot)
            middle = _invites(sql)
            digest2 = _digest_of_unknown(middle, 1)
            assert _invite_view(middle) == [
                _invite(sid, pseudo, _sha256(code1), "revoked", t_send, revoked=t_other),
                _invite(sid, pseudo, digest2, "pending", t_other)]
            assert _send_view(_sends(sql)) == [(pseudo, "batch", DEFAULT_HOURS, "pending", None, None, None, 2,
                                                batch.t_conf, None)]
            assert [row[1:] for row in _invite_tasks(sql)] == [
                _task(sid, "pending", 2, t_other + SECOND_RETRY_WAIT, last_error="RetryTaskLater")]
            assert _events_after(client, batch.cursor, "task.failed") == [
                *_batch_events(offering, batch, 1), _revoked(offering, pseudo, "batch", "replaced", t_other)]
            assert _invite_logs(cap) == [_log(sid, "retry"), _log_superseded(sid)]
            assert len(smtp.connections()) == 2  # a aninhada (RCPT 451) e a antiga (entregue)

            _settle(smtp, sql)
            t_final = clock.advance(STEP)
            sends = _wait_send_states(smtp, sql, ["sent"])
            tasks = _wait_task(smtp, sql, sid, "done", 3)
            messages = _wait(smtp, lambda: (lambda m: m if len(m) == 2 else None)(smtp.messages()))
            _wait(smtp, lambda: len(processed()) == 3)
            after = _utcnow()

    assert messages[0] == first
    code3 = _code_of(messages[1], bot_api, bot)
    envelope = ENVELOPE[ROW_B["ra"]]
    _assert_email(messages[0], to=envelope, link=_link(bot_api, bot, code1),
                  validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    _assert_email(messages[1], to=envelope, link=_link(bot_api, bot, code3),
                  validity=_validity(t_final + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert _invite_view(_invites(sql)) == [
        _invite(sid, pseudo, _sha256(code1), "revoked", t_send, revoked=t_other),
        _invite(sid, pseudo, digest2, "revoked", t_other, revoked=t_final),
        _invite(sid, pseudo, _sha256(code3), "pending", t_final)]
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 3, batch.t_conf,
                                  t_final)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 3, t_other + SECOND_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_final)]
    assert len(smtp.connections()) == 3
    assert [(m.address, m.subject) for m in verification.calls] == [(ROW_B["email"], SUBJECT)] * 2
    assert [type(result) for result in verification.delegated] == [Delivered, Delivered]
    assert _events_after(client, batch.cursor, "task.failed") == [
        *_batch_events(offering, batch, 1),
        _revoked(offering, pseudo, "batch", "replaced", t_other),
        _revoked(offering, pseudo, "batch", "replaced", t_final),
        _sent(offering, pseudo, "batch", t_final)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log_superseded(sid), _log(sid, "sent")]
    assert processed() == [(task_id, "pending"), (task_id, "pending"), (task_id, "done")]
    _assert_logs_clean(cap, sql, [ROW_B["email"]], messages[1].text)
    assert default.calls == []


def test_handler_works_with_a_channel_that_is_not_smtp(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any) -> None:
    """A porta isola o transporte; o tratador é verificado com um canal que não é SMTP:
    com o `ScriptedVerificationChannel` sem canal real (resultados programados: `TemporaryFailure` e
    depois `Delivered`), o tratador entrega ao canal a `VerificationMessage` exata (o e-mail da lista
    como está, o assunto e o corpo fixos), repete com outro código na falha temporária
    (`RetryTaskLater`; o convite anterior revogado com `replaced`) e grava `sent` no `Delivered`, sem
    nenhuma conexão ao SMTP falso; `capabilities` do canal = `VerificationCapabilities("email")`."""
    offering, bot = _offering(client, bot_api, "mc102-convites-outro-canal", "cq_convites_outro_canal_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    t_send = clock.advance(STEP)
    channel = ScriptedVerificationChannel([TemporaryFailure("FalhaDoCanal"), Delivered("ref-canal-2")])
    assert channel.capabilities == VerificationCapabilities("email")

    with capture_logs() as cap:
        _, default = _start_worker(bot_worker_thread, settings, channel)
        ((sid, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["pending"])
        _wait_task(smtp, sql, sid, "pending", 1)
        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["sent"])
        tasks = _wait_task(smtp, sql, sid, "done", 2)

    codes = []
    for call in channel.calls:
        found = LINK_IN_TEXT.findall(call.body)
        assert len(found) == 1 and found[0][0] == bot_api.state(bot).username
        remember_secret(found[0][1])
        codes.append(found[0][1])
    assert channel.calls == [
        VerificationMessage(address=ROW_A["email"], subject=SUBJECT,
                            body=_body(_link(bot_api, bot, codes[0]),
                                       _validity(t_send + timedelta(hours=DEFAULT_HOURS)))),
        VerificationMessage(address=ROW_A["email"], subject=SUBJECT,
                            body=_body(_link(bot_api, bot, codes[1]),
                                       _validity(t_retry + timedelta(hours=DEFAULT_HOURS))))]
    assert smtp.connections() == [] and smtp.messages() == []
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 2, batch.t_conf,
                                  t_retry)]
    assert [row[1:] for row in tasks] == [_task(sid, "done", 2, t_send + FIRST_RETRY_WAIT,
                                                last_error="RetryTaskLater", finished_at=t_retry)]
    assert _invite_view(_invites(sql)) == [
        _invite(sid, pseudo, _sha256(codes[0]), "revoked", t_send, revoked=t_retry),
        _invite(sid, pseudo, _sha256(codes[1]), "pending", t_retry)]
    assert _events_after(client, batch.cursor) == [*_batch_events(offering, batch, 1),
                                                   _revoked(offering, pseudo, "batch", "replaced", t_retry),
                                                   _sent(offering, pseudo, "batch", t_retry)]
    assert _invite_logs(cap) == [_log(sid, "retry"), _log(sid, "sent")]
    _assert_logs_clean(cap, sql, [ROW_A["email"]], channel.calls[-1].body)
    assert default.calls == []


# ---------------------------------------------------------------------------------------------
# Convites concorrentes: no máximo um convite pendente, e um envio mais antigo nunca revoga o
# convite de um envio mais novo.


def test_concurrent_invites_1_pending_batch_superseded_by_a_resend_created_before_its_t1(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Convites concorrentes, sequência (1): com o worker parado, o lote é
    confirmado (envio A `pending`) e a coordenação reenvia ao mesmo aluno (envio B, criado antes da T1
    de A); ao subir o worker, a T1 de A acha o envio mais novo e cancela (`superseded`, `attempts = 0`,
    sem revogar nem gerar código), e B gera o código e manda: um só e-mail, um só convite (o de B,
    pendente); eventos e registros (`cancelled`, `sent`) exatos. Depende do reenvio pela
    coordenação."""
    offering, bot = _offering(client, bot_api, "mc102-lote-um", "cq_lote_um_bot")
    batch = _batch(client, clock, sql, offering, [ROW_B])
    ((_, pseudo, _),) = _students(sql, offering)
    t_resend = clock.advance(STEP)
    _resend(client, offering, [pseudo])
    t_run = clock.advance(STEP)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        sends = _wait_send_states(smtp, sql, ["cancelled", "sent"])
        message = _wait(smtp, lambda: smtp.messages())[0]
        after = _utcnow()
        tasks = _wait_task_states(smtp, sql, ["done", "done"])
    (sid_a, *_), (sid_b, *_) = sends
    assert [row[1:] for row in tasks] == [_task(sid_a, "done", 1, batch.t_conf, finished_at=t_run),
                                          _task(sid_b, "done", 1, t_resend, finished_at=t_run)]
    code = _code_of(message, bot_api, bot)
    _assert_email(message, to=ENVELOPE[ROW_B["ra"]], link=_link(bot_api, bot, code),
                  validity=_validity(t_run + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert len(smtp.messages()) == 1
    assert _send_view(sends) == [
        (pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "superseded", None, 0, batch.t_conf, t_run),
        (pseudo, "coordination", DEFAULT_HOURS, "sent", None, None, None, 1, t_resend, t_run)]
    assert _invite_view(_invites(sql)) == [_invite(sid_b, pseudo, _sha256(code), "pending", t_run)]
    assert _events_after(client, batch.cursor) == [
        *_batch_events(offering, batch, 1),
        _queued(offering, t_resend, "coordination", 1),
        _cancelled(offering, pseudo, "batch", "superseded", t_run),
        _sent(offering, pseudo, "coordination", t_run)]
    assert _invite_logs(cap) == [_log(sid_a, "cancelled"), _log(sid_b, "sent")]
    assert default.calls == []


def test_concurrent_invites_2_sent_batch_then_resend_with_temporary_failure_then_success(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Convites concorrentes, sequência (2): lote enviado (convite 1); reenvio da
    coordenação com RCPT 451 na 1ª tentativa e sucesso na 2ª: a 1ª T1 do reenvio revoga o convite do
    lote (`replaced`, `origin: batch`), a 2ª revoga o da 1ª tentativa (`replaced`, `origin:
    coordination`), e só o último código fica pendente; dois e-mails (o do lote e o do último código).
    Depende do reenvio pela coordenação."""
    offering, bot = _offering(client, bot_api, "mc102-lote-dois", "cq_lote_dois_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    t_send = clock.advance(STEP)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid_a, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["sent"])
        first_message = _wait(smtp, lambda: smtp.messages())[0]
        _settle(smtp, sql)
        t_resend = clock.advance(STEP)
        smtp.fail("rcpt", 451)
        _resend(client, offering, [pseudo])
        (_, (sid_b, *_)) = _wait_send_states(smtp, sql, ["sent", "pending"])
        _wait_task(smtp, sql, sid_b, "pending", 1)
        middle = _invites(sql)
        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["sent", "sent"])
        messages = _wait(smtp, lambda: (lambda m: m if len(m) == 2 else None)(smtp.messages()))
        after = _utcnow()
        tasks = _wait_task_states(smtp, sql, ["done", "done"])
    assert [row[1:] for row in tasks] == [
        _task(sid_a, "done", 1, batch.t_conf, finished_at=t_send),
        _task(sid_b, "done", 2, t_resend + FIRST_RETRY_WAIT, last_error="RetryTaskLater", finished_at=t_retry)]
    assert messages[0] == first_message
    code1, code3 = (_code_of(message, bot_api, bot) for message in messages)
    envelope = ENVELOPE[ROW_A["ra"]]
    _assert_email(messages[0], to=envelope, link=_link(bot_api, bot, code1),
                  validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    _assert_email(messages[1], to=envelope, link=_link(bot_api, bot, code3),
                  validity=_validity(t_retry + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    digest2 = _digest_of_unknown(middle, 1)
    assert _invite_view(middle) == [
        _invite(sid_a, pseudo, _sha256(code1), "revoked", t_send, revoked=t_resend),
        _invite(sid_b, pseudo, digest2, "pending", t_resend)]
    assert _invite_view(_invites(sql)) == [
        _invite(sid_a, pseudo, _sha256(code1), "revoked", t_send, revoked=t_resend),
        _invite(sid_b, pseudo, digest2, "revoked", t_resend, revoked=t_retry),
        _invite(sid_b, pseudo, _sha256(code3), "pending", t_retry)]
    assert _send_view(sends) == [
        (pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 1, batch.t_conf, t_send),
        (pseudo, "coordination", DEFAULT_HOURS, "sent", None, None, None, 2, t_resend, t_retry)]
    assert _events_after(client, batch.cursor) == [
        *_batch_events(offering, batch, 1),
        _sent(offering, pseudo, "batch", t_send),
        _queued(offering, t_resend, "coordination", 1),
        _revoked(offering, pseudo, "batch", "replaced", t_resend),
        _revoked(offering, pseudo, "coordination", "replaced", t_retry),
        _sent(offering, pseudo, "coordination", t_retry)]
    assert _invite_logs(cap) == [_log(sid_a, "sent"), _log(sid_b, "retry"), _log(sid_b, "sent")]
    _assert_logs_clean(cap, sql, [ROW_A["email"]], messages[1].text)
    assert default.calls == []


def test_concurrent_invites_3_sent_batch_then_self_service_with_permanent_failure(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Convites concorrentes, sequência (3): lote enviado
    (convite 1); o aluno pede um convite pelo `/convite` com o e-mail da lista (`invite_requested`,
    `INVITE_REQUESTED` ao chat, texto guardado mascarado); o envio de autosserviço S revoga o convite do
    lote na T1 (`replaced`) e recebe RCPT 550: `failed`/`recipient_refused` com o tipo
    `SMTPRecipientsRefused`, o convite da tentativa revogado sem `student.invite_revoked` (coberto por
    `student.invite_failed`), tarefa `failed` com `PermanentTaskError` e o `task.failed` da fila; nenhum
    link vale (nenhum convite pendente) e a adesão dá `send_failed`. Depende do autosserviço e da
    rota da adesão."""
    offering, bot = _offering(client, bot_api, "mc102-lote-tres", "cq_lote_tres_bot")
    batch = _batch(client, clock, sql, offering, [ROW_C])
    t_send = clock.advance(STEP)
    sender = make_user(3103)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid_a, _, pseudo, *_),) = _wait_send_states(smtp, sql, ["sent"])
        message = _wait(smtp, lambda: smtp.messages())[0]
        after = _utcnow()
        _settle(smtp, sql)
        t_request = clock.advance(STEP)
        smtp.fail("rcpt", 550)
        uid = bot_api.push_text(bot, sender, f"/convite {ROW_C['email']}", bot_command=True)
        row = _processed(bot_api, sql, bot, uid)
        sends = _wait_send_states(smtp, sql, ["sent", "failed"])
        (_, (sid_s, *_)) = sends
        tasks = _wait_task(smtp, sql, sid_s, "failed", 1)
    assert row[1:] == _private(sender, "/convite [e-mail]", "/convite", "invite_requested", t_request)
    assert _sent_texts(bot_api, bot, sender, 1) == [INVITE_REQUESTED]
    code1 = _code_of(message, bot_api, bot)
    _assert_email(message, to=ENVELOPE[ROW_C["ra"]], link=_link(bot_api, bot, code1),
                  validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert len(smtp.messages()) == 1
    invites = _invites(sql)
    assert _invite_view(invites) == [
        _invite(sid_a, pseudo, _sha256(code1), "revoked", t_send, revoked=t_request),
        _invite(sid_s, pseudo, _digest_of_unknown(invites, 1), "revoked", t_request, revoked=t_request)]
    assert _send_view(sends) == [
        (pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 1, batch.t_conf, t_send),
        (pseudo, "self_service", DEFAULT_HOURS, "failed", "recipient_refused", None, "SMTPRecipientsRefused", 1,
         t_request, t_request)]
    task_s = tasks[-1]
    assert task_s[1:] == _task(sid_s, "failed", 1, t_request, last_error="PermanentTaskError",
                               finished_at=t_request)
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == {
        "offering": _ref(offering), "enrolled": 1, "no_invite": 0, "sending": 0, "invite_valid": 0,
        "terms_presented": 0, "invite_lapsed": 0, "send_failed": 1, "linked": 0}
    assert _events_after(client, batch.cursor, "task.failed") == [
        *_batch_events(offering, batch, 1),
        _sent(offering, pseudo, "batch", t_send),
        _revoked(offering, pseudo, "batch", "replaced", t_request),
        _failed(offering, pseudo, "self_service", "recipient_refused", t_request),
        _task_failed(task_s[0], t_request)]
    assert _invite_logs(cap) == [_log(sid_a, "sent"), _log(sid_s, "failed")]
    _assert_logs_clean(cap, sql, [ROW_C["email"]], message.text)
    assert default.calls == []


def test_concurrent_invites_4_fifo_batch_resend_and_self_service_with_the_worker_stopped(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Convites concorrentes, sequência (4): com o worker parado, o lote
    é confirmado (envio A `pending`), o `/convite` com o e-mail do aluno chega à Bot API falsa e a
    coordenação reenvia (envio B). Ao subir o worker, a ordem é pelo FIFO: A e B antes do update (só o
    worker grava o update, na consulta do bot, e o `claim` pega o menor `id`). A T1 de A cancela
    (`superseded`, B existe); B gera o código e manda; o update cria o envio S (`invite_requested`);
    a T1 de S revoga o convite de B (`student.invite_revoked {replaced}`, `origin: coordination`) e
    manda: 1 `student.invite_cancelled`, 1 `student.invite_revoked {replaced}`, 2 e-mails, e só o link
    de S vale. Depende do autosserviço e do reenvio."""
    offering, bot = _offering(client, bot_api, "mc102-lote-quatro", "cq_lote_quatro_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    ((_, pseudo, _),) = _students(sql, offering)
    sender = make_user(3104)
    uid = bot_api.push_text(bot, sender, f"/convite {ROW_A['email']}", bot_command=True)
    t_resend = clock.advance(STEP)
    _resend(client, offering, [pseudo])
    t_run = clock.advance(STEP)

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        row = _processed(bot_api, sql, bot, uid)
        sends = _wait_send_states(smtp, sql, ["cancelled", "sent", "sent"])
        messages = _wait(smtp, lambda: (lambda m: m if len(m) == 2 else None)(smtp.messages()))
        after = _utcnow()
        _wait_task_states(smtp, sql, ["done", "done", "done"])
    (sid_a, *_), (sid_b, *_), (sid_s, *_) = sends
    assert row[1:] == _private(sender, "/convite [e-mail]", "/convite", "invite_requested", t_run)
    assert _sent_texts(bot_api, bot, sender, 1) == [INVITE_REQUESTED]
    code_b, code_s = (_code_of(message, bot_api, bot) for message in messages)
    envelope = ENVELOPE[ROW_A["ra"]]
    for message, code in ((messages[0], code_b), (messages[1], code_s)):
        _assert_email(message, to=envelope, link=_link(bot_api, bot, code),
                      validity=_validity(t_run + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
    assert _send_view(sends) == [
        (pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "superseded", None, 0, batch.t_conf, t_run),
        (pseudo, "coordination", DEFAULT_HOURS, "sent", None, None, None, 1, t_resend, t_run),
        (pseudo, "self_service", DEFAULT_HOURS, "sent", None, None, None, 1, t_run, t_run)]
    assert _invite_view(_invites(sql)) == [
        _invite(sid_b, pseudo, _sha256(code_b), "revoked", t_run, revoked=t_run),
        _invite(sid_s, pseudo, _sha256(code_s), "pending", t_run)]
    # FIFO: as tarefas de A e B vêm antes da do update, e a de S depois dela (ordem de criação = `id`).
    tasks = sql.execute(
        "SELECT task_type, payload, state FROM tasks WHERE task_type IN ('invite.email', 'telegram.update') "
        "ORDER BY id").fetchall()
    assert tasks == [("invite.email", {"invite_send_id": sid_a}, "done"),
                     ("invite.email", {"invite_send_id": sid_b}, "done"),
                     ("telegram.update", {"update_row_id": row[0]}, "done"),
                     ("invite.email", {"invite_send_id": sid_s}, "done")]
    assert _events_after(client, batch.cursor) == [
        *_batch_events(offering, batch, 1),
        _queued(offering, t_resend, "coordination", 1),
        _cancelled(offering, pseudo, "batch", "superseded", t_run),
        _sent(offering, pseudo, "coordination", t_run),
        _revoked(offering, pseudo, "coordination", "replaced", t_run),
        _sent(offering, pseudo, "self_service", t_run)]
    assert _invite_logs(cap) == [_log(sid_a, "cancelled"), _log(sid_b, "sent"), _log(sid_s, "sent")]
    _assert_logs_clean(cap, sql, [ROW_A["email"]], messages[1].text)
    assert default.calls == []


@pytest.mark.parametrize("resend_outcome", ["delivered", "refused"])
def test_concurrent_invites_5_batch_waiting_to_retry_is_superseded_by_a_resend_that_already_ran(
        resend_outcome: str, client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any,
        settings: Any, smtp: Any, mail_channel: Any) -> None:
    """Convites concorrentes da mesma matrícula: um envio mais antigo
    nunca revoga o convite de um envio mais novo, o que a T1 confere de novo.
    Aqui o envio mais novo **já passou pela T1** quando o mais antigo volta: com o
    worker no ar, a 1ª execução do lote A recebe RCPT 451 (A `pending`, com o código A1 gerado, o
    convite A1 `pending`, a tarefa `pending` com `available_at` = envio + 2 s e o registro `retry`); sem
    andar o relógio, a coordenação reenvia (envio B), e B roda antes, porque a tarefa de A ainda não
    está disponível: a T1 de B revoga A1 (`student.invite_revoked {replaced, origin: batch}`) e gera B1,
    e B (a) é entregue (`sent`, `attempts = 1`, um e-mail com o link de B1) ou (b) recebe RCPT 550
    (`failed`/`recipient_refused`, `SMTPRecipientsRefused`, B1 revogado sem outro
    `student.invite_revoked`, tarefa `failed` com o `task.failed` da fila). Depois da espera, a T1 de A
    dá `cancelled`/`superseded` com `attempts = 1` (o código gerado antes) e o
    `student.invite_cancelled {batch, superseded}` exato, **sem** revogar B1, sem gerar código e sem
    conectar ao SMTP: os convites ficam exatamente como B os deixou (em (a), só o link de B1 vale e a
    adesão dá `invite_valid`; em (b), nenhum link vale e a adesão dá `send_failed`); um único
    `student.invite_revoked`; registros `invite.email` `retry` (A), `sent` ou `failed` (B) e `cancelled`
    (A). Depende do reenvio e da adesão."""
    delivered = resend_outcome == "delivered"
    offering, bot = _offering(client, bot_api, f"mc102-lote-cinco-{resend_outcome}",
                              f"cq_lote_cinco_{resend_outcome}_bot")
    batch = _batch(client, clock, sql, offering, [ROW_A])
    ((_, pseudo, _),) = _students(sql, offering)
    t_send = clock.advance(STEP)
    smtp.fail("rcpt", 451)  # só a 1ª execução de A

    with capture_logs() as cap:
        before = _utcnow()
        _, default = _start_worker(bot_worker_thread, settings, mail_channel)
        ((sid_a, *_),) = _wait_send_states(smtp, sql, ["pending"])
        tasks = _wait_task(smtp, sql, sid_a, "pending", 1)
        waiting_a = _task(sid_a, "pending", 1, t_send + FIRST_RETRY_WAIT, last_error="RetryTaskLater")
        assert [row[1:] for row in tasks] == [waiting_a]
        first = _invites(sql)
        digest_a1 = _digest_of_unknown(first, 0)
        assert _invite_view(first) == [_invite(sid_a, pseudo, digest_a1, "pending", t_send)]
        assert smtp.messages() == [] and len(smtp.connections()) == 1

        # Sem andar o relógio: a tarefa de A só fica disponível em t_send + 2 s, e B roda antes.
        if not delivered:
            smtp.fail("rcpt", 550)
        _resend(client, offering, [pseudo])
        state_b = "sent" if delivered else "failed"
        (_, (sid_b, *_)) = _wait_send_states(smtp, sql, ["pending", state_b])
        tasks = _wait_task(smtp, sql, sid_b, "done" if delivered else "failed", 1)
        (task_b_id, *_) = tasks[1]
        if delivered:
            (message,) = _wait(smtp, lambda: smtp.messages())
            after = _utcnow()
            code_b = _code_of(message, bot_api, bot)
            _assert_email(message, to=ENVELOPE[ROW_A["ra"]], link=_link(bot_api, bot, code_b),
                          validity=_validity(t_send + timedelta(hours=DEFAULT_HOURS)), before=before, after=after)
        done_b = (_task(sid_b, "done", 1, t_send, finished_at=t_send) if delivered else
                  _task(sid_b, "failed", 1, t_send, last_error="PermanentTaskError", finished_at=t_send))
        assert [row[1:] for row in tasks] == [waiting_a, done_b]
        middle = _invites(sql)
        digest_b1 = _sha256(code_b) if delivered else _digest_of_unknown(middle, 1)
        expected_invites = [
            _invite(sid_a, pseudo, digest_a1, "revoked", t_send, revoked=t_send),
            _invite(sid_b, pseudo, digest_b1, "pending", t_send) if delivered else
            _invite(sid_b, pseudo, digest_b1, "revoked", t_send, revoked=t_send)]
        assert _invite_view(middle) == expected_invites
        assert len(smtp.connections()) == 2

        _settle(smtp, sql)
        t_retry = clock.advance(STEP)
        sends = _wait_send_states(smtp, sql, ["cancelled", state_b])
        tasks = _wait_task(smtp, sql, sid_a, "done", 2)

    b_row = ((pseudo, "coordination", DEFAULT_HOURS, "sent", None, None, None, 1, t_send, t_send) if delivered else
             (pseudo, "coordination", DEFAULT_HOURS, "failed", "recipient_refused", None, "SMTPRecipientsRefused", 1,
              t_send, t_send))
    assert _send_view(sends) == [
        (pseudo, "batch", DEFAULT_HOURS, "cancelled", None, "superseded", None, 1, batch.t_conf, t_retry), b_row]
    assert [row[1:] for row in tasks] == [
        _task(sid_a, "done", 2, t_send + FIRST_RETRY_WAIT, last_error="RetryTaskLater", finished_at=t_retry),
        done_b]
    # A T1 de A não mexeu nos convites (nem revogou B1, nem gerou código) e não conectou ao SMTP.
    assert _invites(sql) == middle
    assert _invite_view(_invites(sql)) == expected_invites
    assert len(smtp.connections()) == 2
    assert len(smtp.messages()) == (1 if delivered else 0)
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == {
        "offering": _ref(offering), "enrolled": 1, "no_invite": 0, "sending": 0,
        "invite_valid": 1 if delivered else 0, "terms_presented": 0, "invite_lapsed": 0,
        "send_failed": 0 if delivered else 1, "linked": 0}
    b_events = ([_sent(offering, pseudo, "coordination", t_send)] if delivered else
                [_failed(offering, pseudo, "coordination", "recipient_refused", t_send),
                 _task_failed(task_b_id, t_send)])
    assert _events_after(client, batch.cursor, "task.failed") == [
        *_batch_events(offering, batch, 1),
        _queued(offering, t_send, "coordination", 1),
        _revoked(offering, pseudo, "batch", "replaced", t_send),
        *b_events,
        _cancelled(offering, pseudo, "batch", "superseded", t_retry)]
    assert _invite_logs(cap) == [_log(sid_a, "retry"), _log(sid_b, "sent" if delivered else "failed"),
                                 _log(sid_a, "cancelled")]
    if delivered:
        _assert_logs_clean(cap, sql, [ROW_A["email"]], message.text)
    assert default.calls == []


# ---------------------------------------------------------------------------------------------
# Tempo de entrega de um lote de cinco


def test_batch_of_five_reaches_the_fake_smtp_within_five_seconds(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Tempo de entrega: com 105 identidades cadastradas (100 numa lista de carga de outro oferecimento e
    os 5 alunos), o worker já no ar e consultando o bot, da resposta 200 da confirmação de um lote de 5
    alunos até o 5º e-mail capturado pelo SMTP falso passam no máximo 5 s em tempo real; os 5 envios
    ficam `sent`, um e-mail por aluno, na ordem de `enrollments.id`."""
    load = create_offering(client, ADMIN_TOKEN, code="mc102-carga-identidades", course=COURSE, term=TERM,
                           timezone=ZONE)
    _import(client, load, [{"name": f"Pessoa de Carga {i:03d}", "ra": f"55000{i:05d}",
                            "email": f"carga.{i:03d}@carga.example.org"} for i in range(100)])
    offering, bot = _offering(client, bot_api, "mc102-convites-prazo", "cq_convites_prazo_bot")
    rows = [{"name": f"Aluno de Tempo {i}", "ra": f"66000{i:05d}", "email": f"tempo.{i}@turma.example.org"}
            for i in range(5)]
    _import(client, offering, rows)
    assert sql.execute("SELECT count(*) FROM identities").fetchone()[0] == 105
    t_prep = clock.advance(STEP)
    code = _prepare(client, offering, 5, t_prep)

    _, default = _start_worker(bot_worker_thread, settings, mail_channel)
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)
    # O worker já terminou as tarefas anteriores (perfil do bot): a medida começa com a fila vazia.
    _wait(smtp, lambda: sql.execute(
        "SELECT count(*) FROM tasks WHERE state IN ('pending', 'leased')").fetchone()[0] == 0)
    # A medida começa na chegada da resposta, antes das asserções sobre ela (o relógio do SMTP falso).
    resp = client.post(f"/v1/offerings/{offering['code']}/invites", json={"confirmation_code": code},
                       headers=auth(ADMIN_TOKEN))
    answered_at = monotonic()
    assert _ok(resp, "invites.send") == {"offering": _ref(offering), "queued": 5, "valid_hours": DEFAULT_HOURS}
    messages = _wait(smtp, lambda: (lambda m: m if len(m) == 5 else None)(smtp.messages()))
    assert messages[4].ended - answered_at <= DELIVERY_DEADLINE_SECONDS, (
        f"5º e-mail {messages[4].ended - answered_at:.2f} s depois da resposta da confirmação")
    sends = _wait_send_states(smtp, sql, ["sent"] * 5)
    students = _students(sql, offering)
    assert [row[1] for row in sends] == [row[0] for row in students]
    assert _send_view(sends) == [(pseudo, "batch", DEFAULT_HOURS, "sent", None, None, None, 1, t_prep, t_prep)
                                 for _, pseudo, _ in students]
    by_ra = {row["ra"]: row["email"] for row in rows}
    assert [m.rcpt_to for m in messages] == [(by_ra[ra],) for _, _, ra in students]
    assert default.calls == []
