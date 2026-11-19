"""Autosserviço `/convite <e-mail>`,
inclusive quando um terceiro pede reenvio para o e-mail de
outro aluno.

Contratos: prefixo, argumento, máscara, condições do envio e limites do autosserviço; o segundo
desvio do texto literal; a trava do bot antes das consultas de limite; a tarefa `invite.email`;
os eventos; o desfecho `invite_requested`; os textos `INVITE_REQUESTED`, `TERMS` e
`WELCOME_STUDENT`; a validade no fuso; o e-mail do convite; onde fica o e-mail
digitado; os registros `telegram.update` e `task.processed`;
e a injeção de falhas nos testes (gatilho no `INSERT` de `invite_sends`).

Tudo passa pelo caminho real: a API importa a lista e confirma o lote; um worker com `bots`
(`bot_worker_thread`, `FakeClock` do teste) e `verification=mail_channel` (o adaptador SMTP real
sobre o SMTP falso) consulta a Bot API falsa, grava e processa o `/convite`, responde pela fila de
saída e manda o e-mail pela tarefa `invite.email`. Cada caso confere a linha do update (texto
guardado já mascarado, `command`, estado, `outcome` e horário), a resposta (a linha da fila de saída
e o `sendMessage` inteiro), as tarefas criadas, os envios (`invite_sends`), os convites, o e-mail
capturado (envelope, cabeçalhos e texto decodificado) e os eventos exatos, na ordem; cada teste
confere os registros de log do sistema na captura (só os esperados, com as chaves exatas), e os
testes do envio e dos limites usam os links para provar que só o do último código vale (o anterior
dá `LINK_INVALID`).

Os textos esperados (bot e e-mail) são escritos aqui por extenso (não vêm do código),
com a disciplina e o período neutros do oferecimento. Os remetentes são neutros (`make_user`), e os
alunos, nomes, RAs e e-mails neutros em `example.*`, fora dos alvos canário. Horários pelo
`FakeClock`, salvo o `Date` do e-mail, conferido por forma em tempo real; ids internos, `seq`,
códigos e pseudônimos só por relação (os pseudônimos são lidos do banco pela identidade da lista).
As esperas reais são sempre dos dublês (`bot_api.wait_for`, `smtp.wait_for`), com prazo.

Fora deste arquivo, sem mecanismo de limpeza (limitações conhecidas do `/convite`): a posse
esgotada na última tentativa, o e-mail mandado fora do `/convite` e a versão morta da linha.
"""

from __future__ import annotations

import dataclasses
import email.utils
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql as pgsql

from cq_server.events import EventType
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.tasks import TaskRecord
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    import_students,
    link_code,
    list_events,
    prepare_invites,
    register_bot,
    register_person,
    send_invites,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.canary import _forms, fold
from tests.helpers.clock import START, iso
from tests.helpers.contract import assert_matches
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import Captured, capture_logs
from tests.helpers.smtp import SENDER

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
BOT_USERNAME = "cq_autosservico_bot"
OTHER_BOT_USERNAME = "outro_bot_da_turma"
SECOND_BOT_USERNAME = "cq_autosservico_segundo_bot"  # bot de um 2º oferecimento, no mesmo worker
WORKER_ID = "worker-autosservico"
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook) e o envio do e-mail
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
DEFAULT_HOURS = 168  # o autosserviço usa sempre a validade padrão
BATCH_HOURS = 24  # validade de um lote diferente do padrão (o autosserviço não a herda)
PREPARED_HOURS = 48  # validade de um preparo pendente, sem confirmar (o autosserviço não a herda)
CONFIRMATION_MINUTES = 10
GROUP_CHAT = {"id": -4001230042, "type": "group", "title": "Turma do Autosservico"}
# Entidade `bot_command` explícita sobre o `/convite` (8 unidades UTF-16), para texto com quebra de linha.
CONVITE_ENTITY = [{"offset": 0, "length": 8, "type": "bot_command"}]
MASK = "[e-mail]"

# Gatilho de teste no `INSERT` de `invite_sends` (injeção de falhas nos testes).
FAIL_TRIGGER = "cq_test_fail_invite_send"
FORCED_FAILURE = "falha simulada na gravacao do envio de convite"

CONFIRMATION_SHAPE = re.compile(r"[A-Za-z0-9_-]{22}")
MESSAGE_ID_SHAPE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
SIZE_PARAM = re.compile(r"size=[1-9][0-9]*", re.IGNORECASE)
SENDER_HEADER = "Canal de Questões <convites@cq.example.org>"  # `"Canal de Questões" <remetente>`, decodificado
MAIL_HEADERS = ("from", "to", "subject", "date", "message-id", "mime-version", "content-type",
                "content-transfer-encoding")

# Textos do bot e do e-mail, por extenso.
TERMS_TEMPLATE = (
    "Termo de uso do bot do oferecimento {course} ({term}), versão {version}\n"
    "\n"
    "Leia até o fim: as opções para aceitar ou recusar estão no final.\n"
    "\n"
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a "
    "elas. As respostas são registradas para o ensino na disciplina e para o piloto de um "
    "Trabalho de Conclusão de Curso (TCC) de Ciência da Computação da Unicamp, que avalia "
    "este sistema.\n"
    "\n"
    "Quais dados são guardados:\n"
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;\n"
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do "
    "perfil e o telefone não são guardados);\n"
    "• as suas respostas e as mensagens que você mandar a este bot.\n"
    "\n"
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno "
    "aparece por um código. A ligação entre o código e o seu nome só é consultada quando "
    "necessário, e cada consulta fica registrada. No TCC, os resultados aparecem só de forma "
    "agregada, sem identificar ninguém.\n"
    "\n"
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste "
    "oferecimento, definida pela coordenação. Depois desse prazo, os seus dados de "
    "identificação (nome, RA, e-mail e conta do Telegram) são apagados, e as respostas "
    "continuam guardadas sem eles.\n"
    "\n"
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste "
    "piloto, estas questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail "
    "continua valendo até vencer.\n"
    "\n"
    "Para aceitar, toque em /aceito\n"
    "Para recusar, toque em /recuso"
)
WELCOME_STUDENT_TEMPLATE = (
    "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {course} ({term}). "
    "As questões da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a "
    "coordenação."
)
INVITE_REQUESTED_TEMPLATE = (
    "Pedido recebido. Se o e-mail informado for de um aluno da lista do oferecimento "
    "{course} ({term}) que ainda não se vinculou, um novo link de convite vai para esse "
    "e-mail, e o link anterior deixa de valer. O e-mail pode levar alguns minutos para "
    "chegar.\n"
    "\n"
    "Para pedir, mande /convite seguido de um espaço e do seu e-mail institucional, o mesmo "
    "da lista da turma. Há um limite de pedidos por dia. Se o link não chegar, fale com a "
    "coordenação."
)
EMAIL_SUBJECT_TEMPLATE = "Seu link para o bot do oferecimento {course} ({term})"
EMAIL_BODY_TEMPLATE = (
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da "
    "disciplina chegam por um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em "
    '"Iniciar". O bot vai mostrar o termo de uso, que você pode aceitar ou recusar.\n'
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e "
    "do seu e-mail institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la."
)


def _offering_text(template: str) -> str:
    return template.replace("{course}", COURSE).replace("{term}", TERM)


TERMS_V1 = _offering_text(TERMS_TEMPLATE).replace("{version}", "1")
WELCOME_STUDENT = _offering_text(WELCOME_STUDENT_TEMPLATE)
INVITE_REQUESTED = _offering_text(INVITE_REQUESTED_TEMPLATE)
EMAIL_SUBJECT = _offering_text(EMAIL_SUBJECT_TEMPLATE)

# o registro `telegram.update` de todo `/convite` em chat privado, a menos de
# `update_row_id` e `time`, e o `task.processed` da tarefa dele, a menos de `id` e `time`.
UPDATE_LOG = {"level": "INFO", "logger": "cq_server.worker", "event": "telegram.update",
              "outcome": "invite_requested"}
TASK_LOG = {"level": "INFO", "logger": "cq_server.worker", "event": "task.processed", "state": "done"}

# Registros por inclusão: os registros do worker que estes fluxos produzem,
# com as chaves além de `level`, `logger`, `event` e `time`, e o campo cujo valor cada teste restringe.
WORKER_LOGS = {
    "telegram.update": (("update_row_id", "outcome"), "outcome"),
    "task.processed": (("id", "state"), "state"),
    "outbox.dispatched": (("id", "state"), "state"),
    "invite.email": (("invite_send_id", "result"), "result"),
}

# Texto do bot para link inválido, sem mudança com o vínculo do aluno, por extenso.
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."


# ---------------------------------------------------------------------------------------------
# Alunos (neutros, fora dos alvos canário)


@dataclasses.dataclass(frozen=True)
class _Student:
    key: str
    name: str
    ra: str
    email: str    # como na lista: maiúsculas na parte local e no domínio
    address: str  # o endereço de envio: parte local da lista, domínio em minúsculas

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


LINKED = _Student("vinculada", "Estudante Vinculada Autosservico", "4410000101",
                  "Vinculada.Autosservico@Turma-Autosservico.Example.ORG",
                  "Vinculada.Autosservico@turma-autosservico.example.org")
ALFA = _Student("alfa", "Estudante Alfa Autosservico", "4410000102",
                "Alfa.Autosservico@Turma-Autosservico.Example.ORG",
                "Alfa.Autosservico@turma-autosservico.example.org")
BRAVO = _Student("bravo", "Estudante Bravo Autosservico", "4410000103",
                 "Bravo.Autosservico@Turma-Autosservico.Example.ORG",
                 "Bravo.Autosservico@turma-autosservico.example.org")
CHARLIE = _Student("charlie", "Estudante Charlie Autosservico", "4410000104",
                   "Charlie.Autosservico@Turma-Autosservico.Example.ORG",
                   "Charlie.Autosservico@turma-autosservico.example.org")
DELTA = _Student("delta", "Estudante Delta Autosservico", "4410000105",
                 "Delta.Autosservico@Turma-Autosservico.Example.ORG",
                 "Delta.Autosservico@turma-autosservico.example.org")

# E-mails digitados com outra caixa (a comparação não diferencia maiúsculas; o envio vai ao da lista).
ALFA_TYPED = "alfa.AUTOSSERVICO@turma-AUTOSSERVICO.example.ORG"
BRAVO_TYPED = "BRAVO.autosservico@TURMA-AUTOSSERVICO.EXAMPLE.ORG"
DELTA_TYPED = "dElTa.aUtOsSeRvIcO@tUrMa-AuToSsErViCo.ExAmPlE.oRg"
OUTSIDE = "Fora.Da.Lista@Turma-Autosservico.Example.ORG"

# Pessoas cadastradas que não são alunas pendentes do oferecimento do bot (condição 5 do autosserviço):
# uma aluna de outro oferecimento, um membro da equipe e uma pessoa sem matrícula. O
# `_Student` serve só pela identidade (`row()` é também o corpo do `people.register`).
NEIGHBOR = _Student("vizinha", "Estudante Vizinha Autosservico", "4410000106",
                    "Vizinha.Autosservico@Turma-Autosservico.Example.ORG",
                    "Vizinha.Autosservico@turma-autosservico.example.org")
TEAMMATE = _Student("equipe", "Pessoa Equipe Autosservico", "4410000107",
                    "Equipe.Autosservico@Turma-Autosservico.Example.ORG",
                    "Equipe.Autosservico@turma-autosservico.example.org")
REGISTERED = _Student("cadastrada", "Pessoa Cadastrada Autosservico", "4410000108",
                      "Cadastrada.Autosservico@Turma-Autosservico.Example.ORG",
                      "Cadastrada.Autosservico@turma-autosservico.example.org")
NEIGHBOR_TYPED = "vIZINHA.autoSSERVICO@turma-AUTOSSERVICO.example.ORG"
TEAMMATE_TYPED = "EQUIPE.autosservico@TURMA-autosservico.EXAMPLE.org"
REGISTERED_TYPED = "cadastrada.AUTOSSERVICO@Turma-Autosservico.EXAMPLE.Org"


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


@dataclasses.dataclass
class _Ctx:
    client: Any
    clock: Any
    sql: psycopg.Connection
    bot_api: Any
    smtp: Any
    offering: dict
    bot: int
    pseudo: dict[str, str]
    batch_codes: dict[str, str]
    channel: _DefaultChannel
    username: str = BOT_USERNAME  # o do bot do oferecimento (o link do e-mail usa este nome)

    @property
    def ref(self) -> dict:
        return {"offering_id": self.offering["offering_id"], "code": self.offering["code"]}


@dataclasses.dataclass(frozen=True)
class _Case:
    label: str
    row_id: int
    task_id: int
    code: str | None = None  # o código do e-mail do envio, nos casos com envio
    send_id: int | None = None  # o `invite_sends.id` criado, nos casos com envio


@dataclasses.dataclass(frozen=True)
class _Marks:
    """O estado antes de um pedido: cursores e instantâneos para comparar depois."""

    seq: int
    outbox: int
    task: int
    call: int
    messages: int
    connections: int
    sends: list[tuple]
    invites: list[tuple]
    real: datetime


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validity(moment: datetime) -> str:
    """A validade no fuso do oferecimento: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = moment.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def _email_body(code: str, expires_at: datetime, username: str = BOT_USERNAME) -> str:
    return (_offering_text(EMAIL_BODY_TEMPLATE)
            .replace("{link}", f"https://t.me/{username}?start={code}")
            .replace("{validity}", _validity(expires_at)))


def _rows(sql: psycopg.Connection, query: str, params: Sequence[Any] = ()) -> list[tuple]:
    return sql.execute(query, params).fetchall()  # type: ignore[arg-type]


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _last_outbox(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _last_task(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM tasks").fetchone()[0]


def _last_call(bot_api: Any) -> int:
    return max((c.seq for c in bot_api.calls()), default=0)


def _updates(sql: psycopg.Connection, bot: int, update_id: int) -> list[tuple]:
    """(id, kind, chat_id, chat_type, sender_id, text, command, state, outcome, processed_at)."""
    return _rows(sql, """
        SELECT u.id, u.kind, u.chat_id, u.chat_type, u.sender_id, u.text, u.command, u.state,
               u.outcome, u.processed_at
          FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
         WHERE b.telegram_bot_id = %s AND u.update_id = %s
         ORDER BY u.id
        """, (bot, update_id))


def _update_by_id(sql: psycopg.Connection, row_id: int) -> tuple:
    """A linha inteira do update `row_id` (todas as colunas, inclusive `received_at` e `details`)."""
    rows = _rows(sql, "SELECT id, bot_id, update_epoch, update_id, kind, chat_id, chat_type, sender_id, text, "
                      "command, details, received_at, state, outcome, processed_at FROM telegram_updates "
                      "WHERE id = %s", (row_id,))
    assert len(rows) == 1
    return rows[0]


def _row(sender: Any, text: str, command: str | None, outcome: str | None, at: datetime | None, *,
         chat: dict | None = None, state: str = "processed") -> tuple:
    """A linha esperada (sem o id) de uma mensagem de `sender`, no chat privado dele ou em `chat`."""
    chat_id, chat_type = (sender.user_id, "private") if chat is None else (chat["id"], chat["type"])
    return ("message", chat_id, chat_type, sender.user_id, text, command, state, outcome, at)


def _processed(ctx: _Ctx, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update (tipo fora do allowed_updates)"

    def ready() -> list[tuple] | None:
        rows = _updates(ctx.sql, ctx.bot, update_id)
        return rows if rows and all(row[7] == "processed" for row in rows) else None

    rows = ctx.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _stored(ctx: _Ctx, update_id: int | None) -> int:
    """Espera o update gravado (ainda sem processar, se for o caso); devolve o id da linha."""
    assert update_id is not None, "o dublê descartou o update (tipo fora do allowed_updates)"
    rows = ctx.bot_api.wait_for(lambda: _updates(ctx.sql, ctx.bot, update_id), timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0][0]


def _outbox_after(sql: psycopg.Connection, after: int) -> list[tuple]:
    """(id, text, state, pseudo_id do destinatário, bot, chat do endereço, update respondido)."""
    return _rows(sql, """
        SELECT o.id, o.text, o.state, p.pseudo_id::text, b.telegram_bot_id, d.chat_id, u.update_id
          FROM outbox_messages o
          LEFT JOIN people p ON p.id = o.recipient_person_id
          LEFT JOIN bots b ON b.id = o.bot_id
          LEFT JOIN delivery_endpoints d ON d.id = o.delivery_endpoint_id
          LEFT JOIN telegram_updates u ON u.id = o.reply_to_update_id
         WHERE o.id > %s
         ORDER BY o.id
        """, (after,))


def _settled_outbox(ctx: _Ctx, after: int, count: int) -> list[tuple]:
    """Espera `count` mensagens novas na fila, todas em estado final (`sent` ou `failed`)."""

    def ready() -> list[tuple] | None:
        rows = _outbox_after(ctx.sql, after)
        return rows if len(rows) >= count and all(r[2] in ("sent", "failed") for r in rows) else None

    rows = ctx.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == count, f"mensagens novas na fila: {[r[1:] for r in rows]}"
    return rows


def _tasks_after(sql: psycopg.Connection, after: int) -> list[tuple]:
    """(id, task_type, state, idempotency_key, payload) das tarefas criadas depois de `after`."""
    return _rows(sql, "SELECT id, task_type, state, idempotency_key, payload FROM tasks "
                      "WHERE id > %s ORDER BY id", (after,))


def _settled_tasks(ctx: _Ctx, after: int, count: int) -> list[tuple]:
    """Espera ao menos `count` tarefas novas, todas em estado final (`done` ou `failed`)."""

    def ready() -> list[tuple] | None:
        rows = _tasks_after(ctx.sql, after)
        return rows if len(rows) >= count and all(r[2] in ("done", "failed") for r in rows) else None

    return ctx.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _update_task(row_id: int, task_id: int, state: str = "done") -> tuple:
    return (task_id, "telegram.update", state, f"telegram.update:{row_id}", {"update_row_id": row_id})


def _task_of_row(sql: psycopg.Connection, row_id: int) -> int:
    rows = _rows(sql, "SELECT id FROM tasks WHERE idempotency_key = %s", (f"telegram.update:{row_id}",))
    assert len(rows) == 1, f"tarefas telegram.update do update: {len(rows)}"
    return rows[0][0]


def _all_sends(sql: psycopg.Connection) -> list[tuple]:
    """Todos os envios, com a matrícula na 2ª posição (para filtrar por aluno)."""
    return _rows(sql, "SELECT id, enrollment_id, origin, valid_hours, state, failure, cancel_reason, "
                      "error_type, attempts, created_at, finished_at FROM invite_sends ORDER BY id")


def _all_invites(sql: psycopg.Connection) -> list[tuple]:
    """Todos os convites, com a matrícula na 2ª posição (para filtrar por aluno)."""
    return _rows(sql, "SELECT id, enrollment_id, code_sha256, state, expires_at, created_at, used_at, "
                      "revoked_at, invite_send_id FROM invites ORDER BY id")


def _all_enrollments(sql: psycopg.Connection) -> list[tuple]:
    """Todas as matrículas, com todas as colunas, em ordem de id (para conferir que nada muda)."""
    return _rows(sql, "SELECT * FROM enrollments ORDER BY id")


def _roles(sql: psycopg.Connection, person: _Student) -> list[tuple]:
    """(código do oferecimento, papel, estado) das matrículas da pessoa do e-mail, em ordem de id."""
    return _rows(sql, """
        SELECT o.code, e.role, e.state
          FROM enrollments e
          JOIN offerings o ON o.id = e.offering_id
          JOIN identities i ON i.person_id = e.person_id
         WHERE i.email_normalized = %s
         ORDER BY e.id
        """, (person.email.casefold(),))


def _others(rows: list[tuple], enrollment: int) -> list[tuple]:
    return [row for row in rows if row[1] != enrollment]


def _enrollment(sql: psycopg.Connection, student: _Student) -> int:
    rows = _rows(sql, "SELECT e.id FROM enrollments e JOIN identities i ON i.person_id = e.person_id "
                      "WHERE i.email_normalized = %s", (student.email.casefold(),))
    assert len(rows) == 1, f"matrículas de {student.key}: {len(rows)}"
    return rows[0][0]


def _sends(sql: psycopg.Connection, student: _Student) -> list[tuple]:
    """(origin, valid_hours, state, failure, cancel_reason, error_type, attempts, created_at,
    finished_at) dos envios da matrícula do aluno, em ordem de id."""
    return _rows(sql, """
        SELECT s.origin, s.valid_hours, s.state, s.failure, s.cancel_reason, s.error_type, s.attempts,
               s.created_at, s.finished_at
          FROM invite_sends s
          JOIN enrollments e ON e.id = s.enrollment_id
          JOIN identities i ON i.person_id = e.person_id
         WHERE i.email_normalized = %s
         ORDER BY s.id
        """, (student.email.casefold(),))


def _last_send_id(sql: psycopg.Connection, student: _Student) -> int:
    rows = _rows(sql, """
        SELECT max(s.id) FROM invite_sends s JOIN enrollments e ON e.id = s.enrollment_id
          JOIN identities i ON i.person_id = e.person_id WHERE i.email_normalized = %s
        """, (student.email.casefold(),))
    assert rows[0][0] is not None, f"{student.key} sem envio"
    return rows[0][0]


def _invites(sql: psycopg.Connection, student: _Student) -> list[tuple]:
    """(code_sha256, state, expires_at, created_at, used_at, revoked_at, origem do envio, o envio é da
    mesma matrícula) dos convites da matrícula do aluno, em ordem de id (o convite nasce na
    tarefa, com o `invite_send_id` de um envio da mesma matrícula)."""
    return _rows(sql, """
        SELECT v.code_sha256, v.state, v.expires_at, v.created_at, v.used_at, v.revoked_at, s.origin,
               s.enrollment_id = v.enrollment_id
          FROM invites v
          JOIN enrollments e ON e.id = v.enrollment_id
          JOIN identities i ON i.person_id = e.person_id
          LEFT JOIN invite_sends s ON s.id = v.invite_send_id
         WHERE i.email_normalized = %s
         ORDER BY v.id
        """, (student.email.casefold(),))


def _endpoints(sql: psycopg.Connection, student: _Student) -> list[tuple]:
    return _rows(sql, """
        SELECT b.telegram_bot_id, d.channel, d.chat_id, d.state
          FROM delivery_endpoints d
          JOIN bots b ON b.id = d.bot_id
          JOIN identities i ON i.person_id = d.person_id
         WHERE i.email_normalized = %s
         ORDER BY d.id
        """, (student.email.casefold(),))


def _pseudo_of(sql: psycopg.Connection, student: _Student) -> str:
    rows = _rows(sql, "SELECT p.pseudo_id::text FROM people p JOIN identities i ON i.person_id = p.id "
                      "WHERE i.email_normalized = %s", (student.email.casefold(),))
    assert len(rows) == 1, f"pessoas com o e-mail de {student.key}: {len(rows)}"
    return rows[0][0]


def _event_types_after(sql: psycopg.Connection, cursor: int) -> list[str]:
    """Os tipos de todos os eventos gravados depois de `cursor` (de qualquer tipo), em ordem de `seq`."""
    return [row[0] for row in _rows(sql, "SELECT type FROM events WHERE seq > %s ORDER BY seq", (cursor,))]


def _events_after(ctx: _Ctx, cursor: int) -> list[dict]:
    """Todos os eventos, de qualquer tipo, depois de `cursor`, pela API, sem o `seq` (só crescente)."""
    items = list_events(ctx.client, ADMIN_TOKEN, *(t.value for t in EventType), after=cursor,
                        limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _ev(type_: str, at: datetime, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": None, "data": data}


def _wait_bot_ready(ctx: _Ctx) -> None:
    """O perfil do bot configurado (o `bot.profile_updated` fica antes dos cursores) e a consulta no ar."""
    ctx.bot_api.wait_for(lambda: ctx.sql.execute(
        "SELECT profile_state FROM bots WHERE telegram_bot_id = %s", (ctx.bot,)).fetchone()[0] == "configured",
        timeout=PROCESS_TIMEOUT)
    ctx.bot_api.wait_for(lambda: ctx.bot_api.pending_polls(ctx.bot), timeout=PROCESS_TIMEOUT)


def _wait_tasks_logged(ctx: _Ctx, boot: Captured) -> None:
    """Toda tarefa do banco (novo a cada teste: só as da preparação, inclusive a `bot.sync_profile` de
    cada bot) em estado final e o `task.processed` desse estado já em `boot`, a captura aberta antes de
    o worker subir. O worker grava o resultado no tratador (o `configured` do perfil, o `sent` do
    envio) e só depois registra `invite.email` e `task.processed`: sem esta espera, esses registros da
    preparação podem cair na captura do cenário."""

    def ready() -> list[tuple] | None:
        tasks = _rows(ctx.sql, "SELECT id, state FROM tasks ORDER BY id")
        logged = {(r.get("id"), r.get("state")) for r in boot.records if r.get("event") == "task.processed"}
        return tasks if tasks and all(t[1] in ("done", "failed") and t in logged for t in tasks) else None

    ctx.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _jump(ctx: _Ctx, moment: datetime, *others: _Ctx) -> None:
    """Leva o `FakeClock` a `moment`, com o worker ocioso, e espera, no bot de `ctx` e no de cada um de
    `others`, uma consulta começada depois do salto (só sai com a posse do bot renovada, como nos
    oferecimentos e bots)."""
    mark = monotonic()
    ctx.clock.set(moment)
    for each in (ctx, *others):
        each.bot_api.wait_for(lambda each=each: [c for c in each.bot_api.calls("getUpdates", bot_id=each.bot)
                                                 if c.started > mark], timeout=PROCESS_TIMEOUT)


def _marks(ctx: _Ctx) -> _Marks:
    return _Marks(seq=_last_seq(ctx.sql), outbox=_last_outbox(ctx.sql), task=_last_task(ctx.sql),
                  call=_last_call(ctx.bot_api), messages=len(ctx.smtp.messages()),
                  connections=len(ctx.smtp.connections()), sends=_all_sends(ctx.sql),
                  invites=_all_invites(ctx.sql), real=_real_now())


def _close(ctx: _Ctx) -> None:
    """Encerra o oferecimento: 200 com o `Offering` exato (`closed`, `closed_at` do relógio)."""
    at = ctx.clock.now()
    code = ctx.offering["code"]
    resp = ctx.client.get(f"/v1/offerings/{code}", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"offerings.get: {resp.status_code} {resp.text[:300]}"
    before = resp.json()
    assert_matches("offerings.get", 200, before)
    resp = ctx.client.post(f"/v1/offerings/{code}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"offerings.close: {resp.status_code} {resp.text[:300]}"
    closed = resp.json()
    assert_matches("offerings.close", 200, closed)
    assert closed == {**before, "state": "closed", "closed_at": iso(at)}


# ---------------------------------------------------------------------------------------------
# Montagem: oferecimento, lista, worker e lote


def _offering_with_bot(client: Any, bot_api: Any, code: str, username: str,
                       students: Sequence[_Student]) -> tuple[dict, int]:
    """Oferecimento ativo com o próprio bot (com acesso) e, se `students` não é vazia, a lista
    importada pela API, com as contagens exatas. Devolve o `Offering` e o bot."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    if students:
        ref = {"offering_id": offering["offering_id"], "code": code}
        count = len(students)
        imported = import_students(client, ADMIN_TOKEN, code, [student.row() for student in students])
        assert imported == {"offering": ref, "received": count, "people_created": count, "people_reused": 0,
                            "enrollments_created": count, "already_enrolled": 0}
    return offering, bot


def _setup(client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, smtp: Any, settings: Any,
           bot_worker_thread: Any, mail_channel: Any, students: Sequence[_Student], *, code: str,
           batch: bool, batch_hours: int = DEFAULT_HOURS) -> _Ctx:
    """Oferecimento ativo com bot, a lista `students` importada, o worker no ar com
    o canal de verificação real e, se `batch`, o lote preparado (com `batch_hours`; no padrão, o
    pedido vai sem `valid_hours`), confirmado e entregue: um e-mail por aluno, no
    endereço da lista, e um convite `pending` por matrícula. Termina com os registros das tarefas da
    preparação já emitidos (`_wait_tasks_logged`)."""
    offering, bot = _offering_with_bot(client, bot_api, code, BOT_USERNAME, students)
    channel = _DefaultChannel()
    ctx = _Ctx(client=client, clock=clock, sql=sql, bot_api=bot_api, smtp=smtp, offering=offering,
               bot=bot, pseudo={s.key: _pseudo_of(sql, s) for s in students}, batch_codes={},
               channel=channel)
    with capture_logs() as boot:
        bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=WORKER_ID),
                          verification=mail_channel)
        _wait_bot_ready(ctx)
        if batch:
            _deliver_batch(ctx, students, batch_hours)
        _wait_tasks_logged(ctx, boot)
    return ctx


def _sibling(ctx: _Ctx, offering: dict, bot: int, username: str, students: Sequence[_Student]) -> _Ctx:
    """O contexto de um segundo oferecimento (criado por `_offering_with_bot` antes do `_setup`), no
    mesmo worker; espera o bot dele pronto (posse, perfil e consulta antes dos cursores)."""
    other = dataclasses.replace(ctx, offering=offering, bot=bot, username=username,
                                pseudo={s.key: _pseudo_of(ctx.sql, s) for s in students}, batch_codes={})
    _wait_bot_ready(other)
    return other


def _deliver_batch(ctx: _Ctx, students: Sequence[_Student], hours: int = DEFAULT_HOURS) -> None:
    t0 = ctx.clock.now()
    count = len(students)
    before = _real_now()
    prepared = prepare_invites(ctx.client, ADMIN_TOKEN, ctx.offering["code"],
                               valid_hours=None if hours == DEFAULT_HOURS else hours)
    assert CONFIRMATION_SHAPE.fullmatch(prepared["confirmation_code"]) is not None
    assert prepared == {"offering": ctx.ref, "eligible": count, "valid_hours": hours,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(t0 + timedelta(minutes=CONFIRMATION_MINUTES))}
    queued = send_invites(ctx.client, ADMIN_TOKEN, ctx.offering["code"], prepared["confirmation_code"])
    assert queued == {"offering": ctx.ref, "queued": count, "valid_hours": hours}
    ctx.bot_api.wait_for(lambda: [s[4] for s in _all_sends(ctx.sql)] == ["sent"] * count,
                         timeout=PROCESS_TIMEOUT)
    after = _real_now()
    messages = ctx.smtp.messages()
    assert sorted(m.rcpt_to for m in messages) == sorted((s.address,) for s in students)
    links = {link.message_seq: link for link in ctx.smtp.invite_links()}
    expires = t0 + timedelta(hours=hours)
    for student in students:
        message = next(m for m in messages if m.rcpt_to == (student.address,))
        link = links[message.seq]
        _assert_invite_email(message, student, link.code, expires, before, after, username=ctx.username)
        ctx.batch_codes[student.key] = link.code
        assert _sends(ctx.sql, student) == [("batch", hours, "sent", None, None, None, 1, t0, t0)]
        assert _invites(ctx.sql, student) == [(_sha256(link.code), "pending", expires, t0, None, None,
                                               "batch", True)]


def _assert_invite_email(message: Any, student: _Student, code: str, expires_at: datetime,
                         before: datetime, after: datetime, *, username: str = BOT_USERNAME) -> None:
    """O e-mail do convite: envelope e cabeçalhos exatos no endereço **da
    lista** (a parte local com a caixa gravada, o domínio em minúsculas), `Date` em UTC entre o antes
    e o depois do envio (tempo real, com a resolução de segundos do cabeçalho), `Message-ID` com o
    domínio do remetente, `text/plain; charset=utf-8` em quoted-printable e o texto decodificado
    exato, com o link do código no bot `username` e a validade no fuso do oferecimento. No `MAIL
    FROM`, só o parâmetro `SIZE` que o `smtplib` acrescenta (o dublê anuncia SIZE), conferido por
    forma: nada de `SMTPUTF8` nem de `BODY=8BITMIME`. A conversa é a do `smtp_settings` (sem
    TLS, autenticada), e o bruto é só ASCII (quoted-printable)."""
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated, message.acknowledged) == (
        SENDER, (student.address,), False, True, True)
    assert message.raw.isascii()
    assert [param for param in message.mail_params if SIZE_PARAM.fullmatch(param) is None] == []
    assert sorted(name.lower() for name in message.header_names()) == sorted(MAIL_HEADERS)
    assert {name: message.header(name) for name in ("From", "To", "Subject", "MIME-Version",
                                                     "Content-Transfer-Encoding")} == {
        "From": SENDER_HEADER, "To": student.address, "Subject": EMAIL_SUBJECT, "MIME-Version": "1.0",
        "Content-Transfer-Encoding": "quoted-printable"}
    assert message.message.get_params() == [("text/plain", ""), ("charset", "utf-8")]
    sent_at = email.utils.parsedate_to_datetime(message.header("Date"))
    assert sent_at.utcoffset() == timedelta(0), f"Date fora de UTC: {message.header('Date')}"
    assert before.replace(microsecond=0) <= sent_at <= after
    assert MESSAGE_ID_SHAPE.fullmatch(message.header("Message-ID")) is not None
    assert message.text == _email_body(code, expires_at, username) + "\n"


def _link_student(ctx: _Ctx, student: _Student, sender: Any) -> None:
    """Vincula o aluno pelo link do lote: `/start` → `TERMS`, `/aceito` →
    `WELCOME_STUDENT` e o endereço `active` no chat de `sender`."""
    code = ctx.batch_codes[student.key]
    at = ctx.clock.now()
    cursor, out_cursor = _last_seq(ctx.sql), _last_outbox(ctx.sql)
    uid = ctx.bot_api.push_start(ctx.bot, sender, code)
    row = _processed(ctx, uid)
    assert row[1:] == _row(sender, f"/start {code}", "/start", "student_terms_presented", at)
    accept = _processed(ctx, ctx.bot_api.push_text(ctx.bot, sender, "/aceito", bot_command=True))
    assert accept[1:] == _row(sender, "/aceito", "/aceito", "student_joined", at)
    rows = _settled_outbox(ctx, out_cursor, 2)
    assert [r[1:] for r in rows] == [(TERMS_V1, "sent", None, ctx.bot, None, uid),
                                     (WELCOME_STUDENT, "sent", ctx.pseudo[student.key], ctx.bot,
                                      sender.user_id, None)]
    assert [m.text for m in ctx.bot_api.sent_messages(ctx.bot, chat_id=sender.user_id)] == [
        TERMS_V1, WELCOME_STUDENT]
    assert _endpoints(ctx.sql, student) == [(ctx.bot, "telegram", sender.user_id, "active")]
    assert _events_after(ctx, cursor) == [
        _ev("student.joined", at, offering_id=ctx.offering["offering_id"], person=ctx.pseudo[student.key],
            terms_version=1, endpoint_replaced=False)]


# ---------------------------------------------------------------------------------------------
# Um pedido `/convite` no chat privado, dirigido a este bot


def _assert_generic_reply(ctx: _Ctx, sender: Any, uid: int, marks: _Marks) -> None:
    """A resposta genérica, idêntica em todos os casos: uma linha na fila de saída, ao chat do
    update (sem pessoa nem endereço), e um `sendMessage` com exatamente `chat_id` (o do update) e o
    texto `INVITE_REQUESTED`."""
    rows = _settled_outbox(ctx, marks.outbox, 1)
    assert [r[1:] for r in rows] == [(INVITE_REQUESTED, "sent", None, ctx.bot, None, uid)]
    calls = [(c.params, c.status) for c in ctx.bot_api.calls("sendMessage", bot_id=ctx.bot)
             if c.seq > marks.call]
    assert calls == [({"chat_id": sender.user_id, "text": INVITE_REQUESTED}, 200)]


def _assert_no_send(ctx: _Ctx, row_id: int, marks: _Marks) -> int:
    """Caso sem envio: exatamente uma tarefa nova, a `telegram.update` do update (concluída),
    nenhuma `invite.email`, nenhum envio nem convite novo ou mudado, nenhum evento de tipo nenhum e
    nenhuma conexão ao SMTP falso. Devolve o id da tarefa."""
    tasks = _settled_tasks(ctx, marks.task, 1)
    assert tasks == [_update_task(row_id, tasks[0][0])]
    assert _all_sends(ctx.sql) == marks.sends
    assert _all_invites(ctx.sql) == marks.invites
    assert _event_types_after(ctx.sql, marks.seq) == []
    assert (len(ctx.smtp.messages()), len(ctx.smtp.connections())) == (marks.messages, marks.connections)
    return tasks[0][0]


def _assert_send(ctx: _Ctx, student: _Student, row_id: int, at: datetime, marks: _Marks, *,
                 revokes: str | None, sends_before: list[tuple],
                 invites_before: list[tuple]) -> tuple[int, int, str]:
    """Caso com envio: as tarefas
    `telegram.update` e `invite.email:<envio>` (só com o id no payload), o envio `self_service` com
    `valid_hours = 168` e `sent`, o convite anterior pendente (da origem `revokes`) revogado, um
    convite novo `pending` com `expires_at` = envio + 168 h e o resumo do código do único e-mail novo,
    no endereço da lista, com o link do bot do `ctx`; no máximo um convite pendente; os eventos
    `student.invite_revoked {replaced}` (se havia convite) e `student.invite_sent {self_service}`,
    nessa ordem; nada muda nos envios e convites das outras matrículas. Devolve o id da tarefa
    `telegram.update`, o id do envio e o código do e-mail (já em `remember_secret`, pelo
    `invite_links`)."""
    tasks = _settled_tasks(ctx, marks.task, 2)
    send_id = _last_send_id(ctx.sql, student)
    assert tasks == [_update_task(row_id, tasks[0][0]),
                     (tasks[1][0], "invite.email", "done", f"invite.email:{send_id}", {"invite_send_id": send_id})]
    expires = at + timedelta(hours=DEFAULT_HOURS)
    assert _sends(ctx.sql, student) == [*sends_before, ("self_service", DEFAULT_HOURS, "sent", None, None,
                                                        None, 1, at, at)]
    pending = [inv for inv in invites_before if inv[1] == "pending"]
    assert [inv[6] for inv in pending] == ([revokes] if revokes is not None else [])

    messages = ctx.smtp.messages()[marks.messages:]
    assert [m.rcpt_to for m in messages] == [(student.address,)]
    links = [link for link in ctx.smtp.invite_links() if link.message_seq == messages[0].seq]
    assert [(link.bot_username, link.acknowledged) for link in links] == [(ctx.username, True)]
    code = links[0].code
    _assert_invite_email(messages[0], student, code, expires, marks.real, _real_now(), username=ctx.username)

    revoked = [(inv[0], "revoked", inv[2], inv[3], inv[4], at, inv[6], inv[7]) if inv[1] == "pending" else inv
               for inv in invites_before]
    assert _invites(ctx.sql, student) == [*revoked, (_sha256(code), "pending", expires, at, None, None,
                                                     "self_service", True)]
    enrollment = _enrollment(ctx.sql, student)
    assert _others(_all_sends(ctx.sql), enrollment) == _others(marks.sends, enrollment)
    assert _others(_all_invites(ctx.sql), enrollment) == _others(marks.invites, enrollment)

    person = ctx.pseudo[student.key]
    offering_id = ctx.offering["offering_id"]
    expected = [_ev("student.invite_revoked", at, offering_id=offering_id, person=person, origin=revokes,
                    revoke_reason="replaced")] if revokes is not None else []
    expected.append(_ev("student.invite_sent", at, offering_id=offering_id, person=person,
                        origin="self_service", expires_at=iso(expires)))
    assert _events_after(ctx, marks.seq) == expected
    return tasks[0][0], send_id, code


def _request(ctx: _Ctx, label: str, sender: Any, text: str, *, masked: str, command: str = "/convite",
             entities: list[dict] | None = None, bot_command: bool = True,
             sends_to: _Student | None = None, revokes: str | None = None) -> _Case:
    """Manda `text` do chat privado de `sender` e confere o caso inteiro: a linha do update com o texto
    guardado `masked`, o `command` e o `outcome = invite_requested`, a resposta genérica e,
    conforme `sends_to`, o envio ao aluno ou nenhum efeito."""
    try:
        at = ctx.clock.now()
        marks = _marks(ctx)
        sends_before = _sends(ctx.sql, sends_to) if sends_to is not None else []
        invites_before = _invites(ctx.sql, sends_to) if sends_to is not None else []
        uid = ctx.bot_api.push_text(ctx.bot, sender, text, entities=entities, bot_command=bot_command)
        row = _processed(ctx, uid)
        assert row[1:] == _row(sender, masked, command, "invite_requested", at)
        _assert_generic_reply(ctx, sender, uid, marks)
        code: str | None = None
        send_id: int | None = None
        if sends_to is None:
            task_id = _assert_no_send(ctx, row[0], marks)
        else:
            task_id, send_id, code = _assert_send(ctx, sends_to, row[0], at, marks, revokes=revokes,
                                                  sends_before=sends_before, invites_before=invites_before)
    except AssertionError as exc:
        raise AssertionError(f"[{label}] {exc}") from exc
    return _Case(label, row[0], task_id, code, send_id)


def _assert_indistinguishable(captured: Captured, cases: Sequence[_Case]) -> None:
    """por caso, exatamente um registro `telegram.update` do update (controle
    positivo: o desfecho `invite_requested` sai em cada caso) e exatamente um `task.processed` da
    tarefa dele; a menos de `update_row_id`/`id` e `time`, todos iguais às constantes (mesmas
    chaves, `level`, `logger`, `event`, `outcome` e `state`). O único registro a mais é o da tarefa
    de envio, quando ele é criado: na captura, os `invite.email` são
    exatamente um `{result: sent}` por envio dos casos com envio (o registro sai antes do fim da
    tarefa, que o caso já esperou)."""
    mail = [{k: v for k, v in r.items() if k != "time"} for r in captured.records
            if r.get("event") == "invite.email"]
    assert sorted(mail, key=lambda r: r.get("invite_send_id", 0)) == [
        {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email", "invite_send_id": send_id,
         "result": "sent"} for send_id in sorted(c.send_id for c in cases if c.send_id is not None)]
    for case in cases:
        updates = [r for r in captured.records
                   if r.get("event") == "telegram.update" and r.get("update_row_id") == case.row_id]
        assert [{k: v for k, v in r.items() if k not in ("update_row_id", "time")} for r in updates] == [
            UPDATE_LOG], case.label
        processed = [r for r in captured.records
                     if r.get("event") == "task.processed" and r.get("id") == case.task_id]
        assert [{k: v for k, v in r.items() if k not in ("id", "time")} for r in processed] == [
            TASK_LOG], case.label


def _assert_system_logs(captured: Captured, *, outcomes: Sequence[str] = ("invite_requested",),
                        task_states: Sequence[str] = ("done",)) -> None:
    """Nenhum log nem evento distingue esses casos, exceto os registros de log da
    tarefa de envio; e registros por inclusão: na captura, todo registro dos
    loggers do sistema (`cq_server*`) é um pedido da API feito pelo próprio teste (`request`, INFO, no
    `cq_server.api`) ou um registro INFO do `cq_server.worker` deste fluxo, com as chaves exatas:
    `telegram.update` (desfecho em `outcomes`), `task.processed` (estado em `task_states`),
    `outbox.dispatched` (`sent`) ou `invite.email` (`sent`). Nenhum outro registro: nenhum que distinga
    os casos sem envio (ex.: um aviso de limite), nenhuma exceção e nenhum aviso."""
    allowed = {"telegram.update": tuple(outcomes), "task.processed": tuple(task_states),
               "outbox.dispatched": ("sent",), "invite.email": ("sent",)}
    unexpected = []
    for record in captured.records:
        logger = str(record.get("logger", ""))
        if not logger.startswith("cq_server"):
            continue
        event, level = record.get("event"), record.get("level")
        if (logger, event, level) == ("cq_server.api", "request", "INFO"):
            continue
        shape = WORKER_LOGS.get(event) if isinstance(event, str) else None
        if (shape is None or logger != "cq_server.worker" or level != "INFO"
                or set(record) != {"level", "logger", "event", "time", *shape[0]}
                or record.get(shape[1]) not in allowed[event]):
            unexpected.append(record)
    assert unexpected == [], f"registros fora do esperado: {unexpected}"


def _presentation(sql: psycopg.Connection, code: str) -> list[tuple]:
    """(state, update do `presented_update_id`, `presented_terms_version`) do convite do código."""
    return _rows(sql, "SELECT v.state, u.update_id, v.presented_terms_version FROM invites v "
                      "LEFT JOIN telegram_updates u ON u.id = v.presented_update_id "
                      "WHERE v.code_sha256 = %s", (_sha256(code),))


def _assert_only_last_link_works(ctx: _Ctx, sender: Any, old_codes: Sequence[str], last_code: str) -> None:
    """O novo convite, que invalida o anterior; se algum link
    vale, é o do último código gerado; no `/start` de cada código: no chat
    privado de `sender`, cada código anterior (revogado) → `LINK_INVALID` ao chat do update,
    `student_refused_invalid`, sem evento e sem mudança em envios e convites; o último código →
    `TERMS` ao chat do update, `student_terms_presented`, sem evento, com o convite ainda `pending` e o
    termo apresentado por este update, na versão 1."""
    for code in (*old_codes, last_code):
        at = ctx.clock.now()
        marks = _marks(ctx)
        last = code == last_code
        uid = ctx.bot_api.push_start(ctx.bot, sender, code)
        row = _processed(ctx, uid)
        outcome = "student_terms_presented" if last else "student_refused_invalid"
        assert row[1:] == _row(sender, f"/start {code}", "/start", outcome, at)
        rows = _settled_outbox(ctx, marks.outbox, 1)
        assert [r[1:] for r in rows] == [(TERMS_V1 if last else LINK_INVALID, "sent", None, ctx.bot, None, uid)]
        assert _event_types_after(ctx.sql, marks.seq) == []
        assert (_all_sends(ctx.sql), _all_invites(ctx.sql)) == (marks.sends, marks.invites)
        assert _presentation(ctx.sql, code) == ([("pending", uid, 1)] if last else [("revoked", None, None)])
    assert [m.text for m in ctx.bot_api.sent_messages(ctx.bot, chat_id=sender.user_id)] == [
        *([LINK_INVALID] * len(old_codes)), TERMS_V1]


# ---------------------------------------------------------------------------------------------
# Varredura do e-mail digitado


def _typed_hits(sources: Iterable[tuple[str, str]], needles: Sequence[str]) -> list[tuple[str, str]]:
    """(origem, alvo) de cada alvo achado num texto, sem diferenciar maiúsculas nem acentos, também
    em JSON escapado e em percent-encoding (as formas do detector canário)."""
    hits: set[tuple[str, str]] = set()
    for origin, text in sources:
        if not text:
            continue
        for form in _forms(text):
            folded = fold(form)
            for needle in needles:
                if needle in folded:
                    hits.add((origin, needle))
    return sorted(hits)


def _needles(emails: Iterable[str]) -> list[str]:
    """O e-mail inteiro e a parte local, normalizados, de cada e-mail."""
    out: set[str] = set()
    for address in emails:
        folded = fold(address.strip())
        out.add(folded)
        out.add(folded.split("@", 1)[0])
    return sorted(out)


def _database_sources(sql: psycopg.Connection) -> list[tuple[str, str]]:
    return [(f"{table}.{column}", value) for table, column, value in scan_all_tables(sql)]


def _assert_typed_emails_absent(ctx: _Ctx, captured: Captured, *, list_emails: Iterable[str],
                                other_emails: Iterable[str] = ()) -> None:
    """O e-mail digitado não aparece em log, evento, tarefa nem outra coluna: em
    todas as colunas de todas as tabelas, nos logs capturados e nos parâmetros de todas as chamadas à
    Bot API, os e-mails da lista (e as suas partes locais) só aparecem em `identities.email` e
    `identities.email_normalized` — o controle positivo da varredura do banco —, e os de fora da lista
    em lugar nenhum. Controle positivo do detector: cada alvo é achado em maiúsculas e em
    percent-encoding."""
    list_needles, other_needles = _needles(list_emails), _needles(other_emails)
    needles = sorted(set(list_needles) | set(other_needles))
    for needle in needles:
        control = "x " + needle.upper().replace("@", "%40") + " y"
        assert _typed_hits([("controle", control)], [needle]) == [("controle", needle)]
    sources = _database_sources(ctx.sql) + [("logs", captured.text)] + [
        (f"bot_api.{c.method}", json.dumps(c.params, ensure_ascii=False)) for c in ctx.bot_api.calls()]
    expected = sorted({(origin, needle) for needle in list_needles
                       for origin in ("identities.email", "identities.email_normalized")})
    assert _typed_hits(sources, needles) == expected


# ---------------------------------------------------------------------------------------------
# Gatilho de teste no `INSERT` de `invite_sends`


def _install_send_failure(sql: psycopg.Connection) -> None:
    """Gatilho de restrição adiado, no molde do `_install_commit_failure` da API base: o COMMIT de toda
    transação que grava um `invite_sends` falha (`RaiseException`), e a transação inteira volta atrás."""
    name = pgsql.Identifier(FAIL_TRIGGER)
    body = f"BEGIN RAISE EXCEPTION '%', '{FORCED_FAILURE}'; RETURN NULL; END"
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}")
                .format(name, pgsql.Literal(body)))
    sql.execute(pgsql.SQL("CREATE CONSTRAINT TRIGGER {} AFTER INSERT ON invite_sends DEFERRABLE INITIALLY "
                          "DEFERRED FOR EACH ROW EXECUTE FUNCTION {}()").format(name, name))
    sql.execute(pgsql.SQL("ALTER TABLE invite_sends ENABLE ALWAYS TRIGGER {}").format(name))


def _remove_send_failure(sql: psycopg.Connection) -> None:
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("DROP TRIGGER {} ON invite_sends").format(name))
    sql.execute(pgsql.SQL("DROP FUNCTION {}()").format(name))


def _attempted(ctx: _Ctx, queue: Any, task_id: int, attempts: int) -> TaskRecord:
    """Espera a tentativa `attempts` da tarefa terminada (fora de posse); devolve o registro."""

    def ready() -> TaskRecord | None:
        record = queue.get(task_id)
        return record if record.attempts == attempts and record.state != "leased" else None

    return ctx.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _task_states(captured: Captured, task_id: int) -> list[str]:
    return [r.get("state") for r in captured.records
            if r.get("event") == "task.processed" and r.get("id") == task_id]


# ---------------------------------------------------------------------------------------------
# Testes


def test_generic_reply_and_records_are_identical_in_every_case(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any, smtp: Any, settings: Any,
                                                               bot_worker_thread: Any,
                                                               mail_channel: Any) -> None:
    """no chat privado,
    todo `/convite` recebe a mesma resposta (`INVITE_REQUESTED` ao chat do update, `sendMessage` só com
    `chat_id` e `text`) e o `outcome = invite_requested`, e o texto guardado vira `/convite [e-mail]`
    quando há argumento (sem argumento, fica `/convite`). Casos: e-mail da lista (com envio, que
    revoga o convite do lote; controle positivo do envio), fora da lista, mal formado (`<…>`,
    `mailto:` e dois e-mails, com o e-mail de um aluno pendente), sem argumento, e-mail de aluno
    vinculado, chat já vinculado (o chat do endereço de um aluno, com o e-mail de outro aluno pendente)
    e oferecimento encerrado (com o e-mail de um aluno pendente). Nos casos sem envio: exatamente uma
    tarefa, a `telegram.update` (concluída), nenhuma `invite.email`, nenhum envio, nenhum evento e
    nenhuma conexão ao SMTP. Em todos, um registro `telegram.update` e um `task.processed` iguais às
    constantes a menos de `update_row_id`/`id` e `time` (os demais casos de resposta igual, limite atingido,
    estão no teste dos limites, com as mesmas constantes), e nenhum outro registro do sistema na
    captura além dos esperados. O e-mail digitado não aparece em log, evento, tarefa
    nem outra coluna."""
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel,
                 (LINKED, ALFA, BRAVO, CHARLIE), code="mc102-autosservico-casos", batch=True)
    clock.advance(STEP)
    linked_chat = make_user(401)
    _link_student(ctx, LINKED, linked_chat)
    clock.advance(STEP)
    masked = f"/convite {MASK}"

    with capture_logs() as captured:
        cases = [_request(ctx, "e-mail da lista", make_user(402), f"/convite {BRAVO_TYPED}", masked=masked,
                          sends_to=BRAVO, revokes="batch")]
        for label, sender, text, stored in (
                ("fora da lista", make_user(403), f"/convite {OUTSIDE}", masked),
                ("mal formado <…>", make_user(404), f"/convite <{ALFA.email}>", masked),
                ("mal formado mailto:", make_user(404), f"/convite mailto:{ALFA.email}", masked),
                ("mal formado dois e-mails", make_user(405), f"/convite {ALFA.email} {CHARLIE.email}", masked),
                ("sem argumento", make_user(406), "/convite", "/convite"),
                ("aluno vinculado", make_user(407), f"/convite {LINKED.email}", masked),
                ("chat já vinculado", linked_chat, f"/convite {ALFA.email}", masked)):
            cases.append(_request(ctx, label, sender, text, masked=stored))
        _close(ctx)
        cases.append(_request(ctx, "oferecimento encerrado", make_user(408), f"/convite {CHARLIE.email}",
                              masked=masked))

    _assert_indistinguishable(captured, cases)
    _assert_system_logs(captured)
    assert [m.text for m in bot_api.sent_messages(ctx.bot, chat_id=linked_chat.user_id)] == [
        TERMS_V1, WELCOME_STUDENT, INVITE_REQUESTED]
    assert _endpoints(sql, LINKED) == [(ctx.bot, "telegram", linked_chat.user_id, "active")]
    _assert_typed_emails_absent(ctx, captured, list_emails=(LINKED.email, ALFA.email, BRAVO.email,
                                                            CHARLIE.email, BRAVO_TYPED),
                                other_emails=(OUTSIDE,))
    assert ctx.channel.calls == []


def test_registered_email_that_is_not_a_pending_student_of_the_bot_offering_sends_nothing(
        client: Any, clock: Any, sql: Any, bot_api: Any, smtp: Any, settings: Any, bot_worker_thread: Any,
        mail_channel: Any) -> None:
    """Compara o e-mail só com os e-mails dos alunos **não vinculados** do oferecimento do
    bot; com um e-mail que não é de nenhum aluno não vinculado do oferecimento, nada é
    enviado; condição 5 do autosserviço (o `normalized` do argumento é o
    `email_normalized` de uma pessoa com matrícula de **aluno** `pending` **neste** oferecimento). Com o
    e-mail, digitado com outra caixa, de uma pessoa cadastrada que não é aluna pendente do oferecimento
    do bot:
    - (a) aluna `pending` de outro oferecimento ativo, com bot com acesso e a lista importada pela API;
    - (b) membro da equipe deste oferecimento, cadastrado com e-mail (`people.register` e `team.link`:
      matrícula `team` `pending` e convite de equipe `pending`);
    - (c) pessoa cadastrada com e-mail e sem matrícula;
    o `/convite` no chat privado deste bot tem os cheques dos demais casos sem envio: a resposta
    genérica idêntica (`INVITE_REQUESTED` ao chat do update, `sendMessage` só com `chat_id` e `text`),
    `outcome = invite_requested`, o texto guardado `/convite [e-mail]`, exatamente uma tarefa (a
    `telegram.update`, concluída), nenhuma `invite.email`, nenhum `invite_sends` nem convite novo ou
    mudado (o de equipe continua `pending`, sem apresentação), nenhum evento, nenhuma conexão nem
    e-mail no SMTP falso; e nenhuma matrícula muda (todas as colunas; a da outra aluna e a da equipe
    ficam como estavam). Registros `telegram.update` e `task.processed` iguais às constantes dos
    demais casos, e nenhum outro registro do sistema além dos esperados. Controle positivo:
    o mesmo e-mail de (a), mandado ao bot do oferecimento dela, envia: a resposta genérica
    daquele bot, um envio `self_service` de 168 h para a matrícula dela, o e-mail no endereço da lista
    com o link do bot dela e `student.invite_sent {self_service}` com o oferecimento dela. O e-mail
    digitado não aparece em log, evento, tarefa nem outra coluna."""
    second, second_bot = _offering_with_bot(client, bot_api, "mc102-autosservico-vizinho",
                                            SECOND_BOT_USERNAME, (NEIGHBOR,))
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel, (ALFA,),
                 code="mc102-autosservico-cadastro", batch=False)
    neighbor = _sibling(ctx, second, second_bot, SECOND_BOT_USERNAME, (NEIGHBOR,))
    teammate = register_person(client, ADMIN_TOKEN, **TEAMMATE.row())
    team_code = link_code(create_team_link(client, ADMIN_TOKEN, ctx.offering["code"], teammate["alias"])["link"])
    register_person(client, ADMIN_TOKEN, **REGISTERED.row())
    assert _roles(sql, ALFA) == [(ctx.offering["code"], "student", "pending")]
    assert _roles(sql, NEIGHBOR) == [(second["code"], "student", "pending")]
    assert _roles(sql, TEAMMATE) == [(ctx.offering["code"], "team", "pending")]
    assert _roles(sql, REGISTERED) == []
    assert _presentation(sql, team_code) == [("pending", None, None)]
    assert (_all_sends(sql), [inv[3] for inv in _all_invites(sql)]) == ([], ["pending"])
    clock.advance(STEP)
    enrollments = _all_enrollments(sql)
    masked = f"/convite {MASK}"
    senders = {"a": make_user(471), "b": make_user(472), "c": make_user(473), "controle": make_user(474)}

    with capture_logs() as captured:
        cases = []
        for key, label, typed in (
                ("a", "aluna pendente de outro oferecimento", NEIGHBOR_TYPED),
                ("b", "membro da equipe deste oferecimento", TEAMMATE_TYPED),
                ("c", "pessoa cadastrada sem matrícula", REGISTERED_TYPED)):
            cases.append(_request(ctx, label, senders[key], f"/convite {typed}", masked=masked))
            assert _all_enrollments(sql) == enrollments, label
        cases.append(_request(neighbor, "controle: o e-mail de (a) no bot do oferecimento dela",
                              senders["controle"], f"/convite {NEIGHBOR_TYPED}", masked=masked,
                              sends_to=NEIGHBOR))

    _assert_indistinguishable(captured, cases)
    _assert_system_logs(captured)
    for key in ("a", "b", "c"):
        assert [m.text for m in bot_api.sent_messages(ctx.bot, chat_id=senders[key].user_id)] == [
            INVITE_REQUESTED], key
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(neighbor.bot)] == [
        (senders["controle"].user_id, INVITE_REQUESTED)]
    assert [m.rcpt_to for m in smtp.messages()] == [(NEIGHBOR.address,)]
    assert [(s[1], s[2]) for s in _all_sends(sql)] == [(_enrollment(sql, NEIGHBOR), "self_service")]
    assert _all_enrollments(sql) == enrollments
    assert _presentation(sql, team_code) == [("pending", None, None)]
    assert (_sends(sql, ALFA), _invites(sql, ALFA)) == ([], [])
    _assert_typed_emails_absent(ctx, captured,
                                list_emails=(ALFA.email, NEIGHBOR.email, TEAMMATE.email, REGISTERED.email,
                                             NEIGHBOR_TYPED, TEAMMATE_TYPED, REGISTERED_TYPED))
    assert ctx.channel.calls == []


def test_request_limits_reply_the_same_and_release_on_time(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                           smtp: Any, settings: Any, bot_worker_thread: Any,
                                                           mail_channel: Any) -> None:
    """Limites de pedidos: no máximo 3 pedidos por chat a cada 24 horas, contando todos os
    pedidos; com um terceiro pedindo reenvio para o e-mail de outro aluno;
    condições do autosserviço (condição 2, "os pedidos anteriores deste chat neste bot", e condição 6; janelas
    `received_at > agora − 24 h` e `created_at > agora − 1 h`, calculadas pelo `FakeClock`): um chat
    com 3 pedidos nas últimas 24 h, e só um deles bem formado (um fora da lista, um mal formado e um
    sem argumento: todos contam), tem o 4º, com o e-mail de um aluno pendente, sem envio (se o mal
    formado ou o sem argumento não contassem, ele enviaria). 1 s antes de 24 h, o pedido com o e-mail
    do aluno continua sem envio; no mesmo instante, o chat faz mais um pedido neste bot (fora da
    lista) e um no bot de outro oferecimento (o mesmo `chat_id`, o do chat privado da pessoa, com a
    resposta genérica daquele bot e nenhum efeito). Às 24 h exatas, com 2 pedidos deste chat neste bot
    na janela, o pedido seguinte envia: o pedido do outro bot não conta (contado, seriam 3 e nada
    sairia), e o limite é 3 (com 2, nada sairia). Uma matrícula com um envio por autosserviço não
    ganha outro na mesma hora, nem pedido por outro chat (o terceiro), nem 1 s antes de 1 h; à 1 h
    exata, o pedido seguinte envia ao endereço da lista e revoga o convite do envio anterior
    (`student.invite_revoked {replaced, self_service}`), com no máximo um convite pendente, e só o
    link do último código vale (o do primeiro envio dá `LINK_INVALID`). Todos os casos
    recebem a resposta idêntica, com os registros `telegram.update` e `task.processed` iguais às
    constantes (as mesmas do teste dos demais casos) e nenhum outro registro do sistema além dos
    esperados; o e-mail digitado não aparece em log, evento, tarefa nem outra coluna."""
    second, second_bot = _offering_with_bot(client, bot_api, "mc102-autosservico-limites-outro",
                                            SECOND_BOT_USERNAME, ())
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel,
                 (ALFA, BRAVO), code="mc102-autosservico-limites", batch=False)
    other = _sibling(ctx, second, second_bot, SECOND_BOT_USERNAME, ())
    t0 = clock.now()
    chat, first, third = make_user(411), make_user(412), make_user(413)
    masked = f"/convite {MASK}"
    outside = [f"Pedido.{n}@Fora-Autosservico.Example.NET" for n in ("Um", "Dois", "Tres")]

    with capture_logs() as captured:
        cases = [
            _request(ctx, "pedido 1 do chat, fora da lista", chat, f"/convite {outside[0]}", masked=masked),
            _request(ctx, "pedido 2 do chat, mal formado", chat, f"/convite <{ALFA.email}>", masked=masked),
            _request(ctx, "pedido 3 do chat, sem argumento", chat, "/convite", masked="/convite"),
            _request(ctx, "4º pedido do chat em 24 h", chat, f"/convite {ALFA.email}", masked=masked),
            _request(ctx, "1º envio da matrícula", first, f"/convite {BRAVO.email}", masked=masked,
                     sends_to=BRAVO),
            _request(ctx, "2º envio da matrícula em 1 h, por outro chat", third, f"/convite {BRAVO.email}",
                     masked=masked),
        ]
        _jump(ctx, t0 + timedelta(hours=1) - timedelta(seconds=1))
        cases.append(_request(ctx, "2º envio da matrícula 1 s antes de 1 h", third, f"/convite {BRAVO.email}",
                              masked=masked))
        _jump(ctx, t0 + timedelta(hours=1))
        cases.append(_request(ctx, "envio da matrícula liberado depois de 1 h", third,
                              f"/convite {BRAVO.email}", masked=masked, sends_to=BRAVO, revokes="self_service"))
        _jump(ctx, t0 + timedelta(hours=24) - timedelta(seconds=1), other)
        cases.append(_request(ctx, "pedido do chat 1 s antes de 24 h", chat, f"/convite {ALFA.email}",
                              masked=masked))
        cases.append(_request(ctx, "outro pedido do chat 1 s antes de 24 h, fora da lista", chat,
                              f"/convite {outside[1]}", masked=masked))
        cases.append(_request(other, "pedido do mesmo chat no bot de outro oferecimento", chat,
                              f"/convite {outside[2]}", masked=masked))
        _jump(ctx, t0 + timedelta(hours=24))
        cases.append(_request(ctx, "pedido do chat liberado depois de 24 h (2 neste bot e 1 no outro)", chat,
                              f"/convite {ALFA.email}", masked=masked, sends_to=ALFA))

    _assert_indistinguishable(captured, cases)
    _assert_system_logs(captured)
    assert [m.text for m in bot_api.sent_messages(ctx.bot, chat_id=chat.user_id)] == [INVITE_REQUESTED] * 7
    assert [m.text for m in bot_api.sent_messages(ctx.bot, chat_id=third.user_id)] == [INVITE_REQUESTED] * 3
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(other.bot)] == [(chat.user_id, INVITE_REQUESTED)]
    assert [m.rcpt_to for m in smtp.messages()] == [(BRAVO.address,), (BRAVO.address,), (ALFA.address,)]
    assert [inv[1] for inv in _invites(sql, BRAVO)] == ["revoked", "pending"]
    codes = {case.label: case.code for case in cases}
    first, last = codes["1º envio da matrícula"], codes["envio da matrícula liberado depois de 1 h"]
    assert first is not None and last is not None
    _assert_only_last_link_works(ctx, make_user(414), [first], last)
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email, BRAVO.email), other_emails=outside)
    assert ctx.channel.calls == []


def test_list_email_in_any_form_sends_a_new_invite_to_the_list_address(client: Any, clock: Any, sql: Any,
                                                                       bot_api: Any, smtp: Any,
                                                                       settings: Any, bot_worker_thread: Any,
                                                                       mail_channel: Any) -> None:
    """Terceiro pedindo reenvio para o
    e-mail de outro aluno; prefixo, argumento e máscara do autosserviço (o envio é criado com
    `valid_hours = 168`) e e-mail do convite. Antes dos pedidos, o lote sai com validade de 24 h (não o padrão) e
    fica gravado um preparo novo, não confirmado, de 48 h (0 elegíveis): o
    autosserviço usa sempre o padrão, sem herdar a validade do envio anterior nem a do
    preparo. O e-mail da lista digitado com outra caixa e espaços nas pontas, depois de uma quebra de
    linha (entidade `bot_command` explícita), com `/convite@<bot>` e com `/CONVITE` → a resposta
    genérica, um envio `self_service` com `valid_hours = 168` e o e-mail no endereço **da lista**
    (nunca o digitado), com o link do código novo e a validade = envio + 168 h no fuso; o convite do
    lote (de 24 h) revogado (`student.invite_revoked {replaced, batch}`), o novo `pending` com
    `expires_at` = envio + 168 h, no máximo um pendente, e só o último código vale (o
    link do lote dá `LINK_INVALID`, e o do e-mail novo apresenta o termo); o texto guardado vira
    `/convite [e-mail]`, `/convite@<bot> [e-mail]` e `/CONVITE [e-mail]` (o prefixo como foi
    escrito). Registros `telegram.update` e `task.processed` iguais aos dos casos sem envio,
    e nenhum outro registro do sistema além dos esperados; o e-mail digitado não aparece em log,
    evento, tarefa nem outra coluna."""
    assert _validity(START + timedelta(hours=DEFAULT_HOURS)) == "2031-01-07 21:00 -03 (America/Sao_Paulo)"
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel,
                 (ALFA, BRAVO, CHARLIE, DELTA), code="mc102-autosservico-envio", batch=True,
                 batch_hours=BATCH_HOURS)
    t0 = clock.now()
    for student in (ALFA, BRAVO, CHARLIE, DELTA):
        assert [(s[0], s[1], s[2]) for s in _sends(sql, student)] == [("batch", BATCH_HOURS, "sent")]
        assert [(inv[1], inv[2]) for inv in _invites(sql, student)] == [
            ("pending", t0 + timedelta(hours=BATCH_HOURS))]
    prepared = prepare_invites(client, ADMIN_TOKEN, ctx.offering["code"], valid_hours=PREPARED_HOURS)
    assert CONFIRMATION_SHAPE.fullmatch(prepared["confirmation_code"]) is not None
    assert prepared == {"offering": ctx.ref, "eligible": 0, "valid_hours": PREPARED_HOURS,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(t0 + timedelta(minutes=CONFIRMATION_MINUTES))}
    clock.advance(STEP)

    with capture_logs() as captured:
        cases = [
            _request(ctx, "caixa e espaços", make_user(421), f"/convite    {ALFA_TYPED}   ",
                     masked=f"/convite {MASK}", sends_to=ALFA, revokes="batch"),
            _request(ctx, "quebra de linha", make_user(422), f"/convite\n{BRAVO_TYPED}", masked=f"/convite {MASK}",
                     entities=CONVITE_ENTITY, bot_command=False, sends_to=BRAVO, revokes="batch"),
            _request(ctx, "com @ deste bot", make_user(423), f"/convite@{BOT_USERNAME} {CHARLIE.email}",
                     masked=f"/convite@{BOT_USERNAME} {MASK}", command=f"/convite@{BOT_USERNAME}",
                     sends_to=CHARLIE, revokes="batch"),
            _request(ctx, "comando em maiúsculas", make_user(424), f"/CONVITE {DELTA_TYPED}",
                     masked=f"/CONVITE {MASK}", command="/CONVITE", sends_to=DELTA, revokes="batch"),
        ]

    _assert_indistinguishable(captured, cases)
    _assert_system_logs(captured)
    for student, case in zip((ALFA, BRAVO, CHARLIE, DELTA), cases, strict=True):
        invites = _invites(sql, student)
        assert [(inv[1], inv[6]) for inv in invites] == [("revoked", "batch"), ("pending", "self_service")]
        assert [inv[0] for inv in invites] == [_sha256(ctx.batch_codes[student.key]), _sha256(case.code or "")]
    _assert_only_last_link_works(ctx, make_user(425), [ctx.batch_codes[ALFA.key]], cases[0].code or "")
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email, BRAVO.email, CHARLIE.email,
                                                            DELTA.email, ALFA_TYPED, BRAVO_TYPED, DELTA_TYPED))
    assert ctx.channel.calls == []


def test_convite_in_a_group_for_another_bot_or_without_entity_is_only_masked(client: Any, clock: Any,
                                                                            sql: Any, bot_api: Any, smtp: Any,
                                                                            settings: Any,
                                                                            bot_worker_thread: Any,
                                                                            mail_channel: Any) -> None:
    """Comandos do aluno fora de lugar, com o controle que envia;
    regras do `/start`
    e máscara (vale em qualquer chat e para qualquer bot de destino, inclusive sem a entidade
    `bot_command`, quando o texto casa `^/convite(@[A-Za-z0-9_]+)?(\\s|$)` sem diferenciar
    maiúsculas): com o e-mail de um aluno pendente, `/convite` num grupo (também `/convite@<este
    bot>`), `/convite@outro_bot` no privado e `/convite` sem a entidade (no privado, em maiúsculas,
    com `@` e depois de quebra de linha, e num grupo) → `not_handled` (o desfecho do bot para mensagem
    fora do lugar ou sem comando), o texto guardado vira `<prefixo como foi escrito> [e-mail]`,
    nenhuma resposta, nenhum `invite_sends` nem convite, exatamente uma tarefa (a `telegram.update`
    do próprio update, concluída), nenhuma `invite.email`, nenhum evento e um registro
    `telegram.update` com o desfecho `not_handled`. Controle negativo da máscara (o
    texto literal só desvia na troca por U+FFFD e na máscara): `/convites <argumento>`, com e sem a entidade, não
    é `/convite` e fica literal. Controle positivo: o mesmo e-mail num `/convite` privado com a
    entidade envia (o e-mail era de um aluno que podia receber)."""
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel, (ALFA,),
                 code="mc102-autosservico-fora", batch=False)
    clock.advance(STEP)
    sender = make_user(431)
    group = GROUP_CHAT
    ignored: list[int] = []

    with capture_logs() as captured:
        for label, text, chat, with_entity, stored, command in (
                ("outro comando", "/convites argumento-qualquer", None, True, "/convites argumento-qualquer",
                 "/convites"),
                ("outro comando, sem entidade", "/convites argumento-qualquer", None, False,
                 "/convites argumento-qualquer", None),
                ("grupo", f"/convite {ALFA.email}", group, True, f"/convite {MASK}", "/convite"),
                ("grupo, com @ deste bot", f"/convite@{BOT_USERNAME} {ALFA.email}", group, True,
                 f"/convite@{BOT_USERNAME} {MASK}", f"/convite@{BOT_USERNAME}"),
                ("outro bot", f"/convite@{OTHER_BOT_USERNAME} {ALFA.email}", None, True,
                 f"/convite@{OTHER_BOT_USERNAME} {MASK}", f"/convite@{OTHER_BOT_USERNAME}"),
                ("sem entidade", f"/convite {ALFA.email}", None, False, f"/convite {MASK}", None),
                ("sem entidade, maiúsculas", f"/CONVITE {ALFA_TYPED}", None, False, f"/CONVITE {MASK}", None),
                ("sem entidade, com @", f"/Convite@Outro_Bot {ALFA.email}", None, False,
                 f"/Convite@Outro_Bot {MASK}", None),
                ("sem entidade, quebra de linha", f"/convite\n{ALFA.email}", None, False, f"/convite {MASK}", None),
                ("sem entidade, em grupo", f"/convite {ALFA.email}", group, False, f"/convite {MASK}", None)):
            try:
                at = clock.now()
                marks = _marks(ctx)
                uid = bot_api.push_text(ctx.bot, sender, text, bot_command=with_entity, chat=chat)
                row = _processed(ctx, uid)
                assert row[1:] == _row(sender, stored, command, "not_handled", at, chat=chat)
                assert _outbox_after(sql, marks.outbox) == []
                _assert_no_send(ctx, row[0], marks)
                assert [c.seq for c in bot_api.calls("sendMessage", bot_id=ctx.bot) if c.seq > marks.call] == []
                assert (_all_sends(sql), _all_invites(sql)) == ([], [])
                ignored.append(row[0])
            except AssertionError as exc:
                raise AssertionError(f"[{label}] {exc}") from exc
        control = _request(ctx, "controle: privado com a entidade", make_user(439), f"/convite {ALFA.email}",
                           masked=f"/convite {MASK}", sends_to=ALFA)

    _assert_indistinguishable(captured, [control])
    for row_id in ignored:
        updates = [r for r in captured.records
                   if r.get("event") == "telegram.update" and r.get("update_row_id") == row_id]
        assert [{k: v for k, v in r.items() if k not in ("update_row_id", "time")} for r in updates] == [
            {**UPDATE_LOG, "outcome": "not_handled"}]
    _assert_system_logs(captured, outcomes=("not_handled", "invite_requested"))
    assert bot_api.sent_messages(ctx.bot, chat_id=group["id"]) == []
    assert bot_api.sent_messages(ctx.bot, chat_id=sender.user_id) == []
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email, ALFA_TYPED))
    assert ctx.channel.calls == []


def test_temporary_failure_keeps_the_literal_text_until_the_update_is_processed(client: Any, clock: Any,
                                                                                sql: Any, bot_api: Any,
                                                                                smtp: Any, settings: Any,
                                                                                bot_worker_thread: Any,
                                                                                mail_channel: Any,
                                                                                queue: Any) -> None:
    """Controle positivo do texto literal (o texto é guardado literalmente antes do
    processamento, para nenhuma resposta se perder, e mascarado na transação que registra o resultado);
    com o gatilho de teste no `INSERT` de `invite_sends`, o processamento de um
    `/convite` válido falha de forma temporária na 1ª tentativa (não é a última: sem máscara); a
    tarefa `telegram.update` fica `pending`, com `last_error = "RaiseException"` e a espera da fila no
    `FakeClock`, e a linha do update fica `pending` com o texto literal, que a varredura do e-mail
    digitado acha em `telegram_updates.text` (além da lista em `identities`); nada é enviado nem
    respondido e não há evento. Removido o gatilho e avançado o relógio, o update é processado
    (`invite_requested`), mascarado, respondido e o convite é enviado ao endereço da lista; depois,
    o e-mail digitado não aparece mais em coluna, log nem tarefa, e a mensagem do gatilho não vai
    para o log."""
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel, (ALFA,),
                 code="mc102-autosservico-literal", batch=False)
    clock.advance(STEP)
    sender = make_user(441)
    text = f"/convite {ALFA_TYPED}"

    with capture_logs() as captured:
        _install_send_failure(sql)
        at = clock.now()
        marks = _marks(ctx)
        uid = bot_api.push_text(ctx.bot, sender, text, bot_command=True)
        row_id = _stored(ctx, uid)
        task_id = _task_of_row(sql, row_id)
        assert _attempted(ctx, queue, task_id, 1) == TaskRecord(
            id=task_id, task_type="telegram.update", state="pending", attempts=1,
            available_at=at + timedelta(seconds=settings.backoff_base_seconds), lease_owner=None,
            lease_until=None, last_error="RaiseException", finished_at=None)
        assert _updates(sql, ctx.bot, uid) == [(row_id, *_row(sender, text, "/convite", None, None,
                                                              state="pending"))]
        needles = _needles((ALFA.email,))
        assert _typed_hits(_database_sources(sql), needles) == sorted(
            (origin, needle) for needle in needles
            for origin in ("identities.email", "identities.email_normalized", "telegram_updates.text"))
        assert (_all_sends(sql), _all_invites(sql), _outbox_after(sql, marks.outbox)) == ([], [], [])
        assert [c.seq for c in bot_api.calls("sendMessage", bot_id=ctx.bot) if c.seq > marks.call] == []
        assert _event_types_after(sql, marks.seq) == []
        assert (len(smtp.messages()), len(smtp.connections())) == (0, 0)

        _remove_send_failure(sql)
        t1 = clock.advance(settings.backoff_base_seconds)
        row = _processed(ctx, uid)
        assert row == (row_id, *_row(sender, f"/convite {MASK}", "/convite", "invite_requested", t1))
        _assert_generic_reply(ctx, sender, uid, marks)
        assert _assert_send(ctx, ALFA, row_id, t1, marks, revokes=None, sends_before=[],
                            invites_before=[])[0] == task_id
        record = queue.get(task_id)
        assert (record.state, record.attempts, record.last_error) == ("done", 2, "RaiseException")

    # A falha temporária não gera `telegram.update`; o registro do processamento é o de todo caso.
    assert _task_states(captured, task_id) == ["pending", "done"]
    updates = [r for r in captured.records
               if r.get("event") == "telegram.update" and r.get("update_row_id") == row_id]
    assert [{k: v for k, v in r.items() if k not in ("update_row_id", "time")} for r in updates] == [UPDATE_LOG]
    _assert_system_logs(captured, task_states=("pending", "done"))
    assert FORCED_FAILURE not in captured.text
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email, ALFA_TYPED))
    assert ctx.channel.calls == []


def test_definitive_failure_masks_the_text_in_a_fallback_transaction(client: Any, clock: Any, sql: Any,
                                                                     bot_api: Any, smtp: Any, settings: Any,
                                                                     bot_worker_thread: Any, mail_channel: Any,
                                                                     queue: Any) -> None:
    """Se o processamento falhar de vez, a mesma máscara é aplicada numa transação de
    reserva: com o gatilho de teste no `INSERT` de
    `invite_sends` (como o `_install_commit_failure` da API base), o processamento de um `/convite` válido
    falha nas 3 tentativas do `settings` dos testes: na 1ª e na 2ª (não são a última), a tarefa volta
    a `pending` com as esperas da fila (2 s e 4 s no `FakeClock`) e o texto continua literal; na 3ª (a
    última), a tarefa fica `failed` (`last_error = "RaiseException"`, `task.failed` da fila com o mesmo
    nome) e o texto guardado vira `/convite [e-mail]`, com a linha `pending` (sem `outcome` nem
    `processed_at`); nenhum envio, convite, resposta nem outro evento; a mensagem do gatilho não vai
    para o log, e o e-mail digitado não aparece em coluna, log, evento nem tarefa."""
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel, (ALFA,),
                 code="mc102-autosservico-reserva", batch=False)
    clock.advance(STEP)
    sender = make_user(451)
    text = f"/convite {ALFA.email}"
    base = settings.backoff_base_seconds
    assert settings.max_attempts == 3

    with capture_logs() as captured:
        _install_send_failure(sql)
        at = clock.now()
        marks = _marks(ctx)
        uid = bot_api.push_text(ctx.bot, sender, text, bot_command=True)
        row_id = _stored(ctx, uid)
        task_id = _task_of_row(sql, row_id)
        literal = [(row_id, *_row(sender, text, "/convite", None, None, state="pending"))]

        assert _attempted(ctx, queue, task_id, 1) == TaskRecord(
            id=task_id, task_type="telegram.update", state="pending", attempts=1,
            available_at=at + timedelta(seconds=base), lease_owner=None, lease_until=None,
            last_error="RaiseException", finished_at=None)
        assert _updates(sql, ctx.bot, uid) == literal
        t1 = clock.advance(base)
        assert _attempted(ctx, queue, task_id, 2) == TaskRecord(
            id=task_id, task_type="telegram.update", state="pending", attempts=2,
            available_at=t1 + timedelta(seconds=2 * base), lease_owner=None, lease_until=None,
            last_error="RaiseException", finished_at=None)
        assert _updates(sql, ctx.bot, uid) == literal
        t2 = clock.advance(2 * base)
        assert _attempted(ctx, queue, task_id, 3) == TaskRecord(
            id=task_id, task_type="telegram.update", state="failed", attempts=3, available_at=t2,
            lease_owner=None, lease_until=None, last_error="RaiseException", finished_at=t2)
        assert _updates(sql, ctx.bot, uid) == [(row_id, *_row(sender, f"/convite {MASK}", "/convite", None, None,
                                                              state="pending"))]
        _remove_send_failure(sql)
        # O `task.processed` da última tentativa sai logo depois do `fail` (mesma thread): espera por ele.
        bot_api.wait_for(lambda: len(_task_states(captured, task_id)) == 3, timeout=PROCESS_TIMEOUT)

    assert _task_states(captured, task_id) == ["pending", "pending", "failed"]
    # Sem desfecho gravado, não há registro `telegram.update`; nenhum outro registro do sistema.
    assert [r for r in captured.records if r.get("event") == "telegram.update"] == []
    _assert_system_logs(captured, outcomes=(), task_states=("pending", "failed"))
    assert _events_after(ctx, marks.seq) == [
        _ev("task.failed", t2, task_id=task_id, task_type="telegram.update", attempts=3, error="RaiseException")]
    assert _tasks_after(sql, marks.task) == [_update_task(row_id, task_id, "failed")]
    assert (_all_sends(sql), _all_invites(sql), _outbox_after(sql, marks.outbox)) == ([], [], [])
    assert [c.seq for c in bot_api.calls("sendMessage", bot_id=ctx.bot) if c.seq > marks.call] == []
    assert (len(smtp.messages()), len(smtp.connections())) == (0, 0)
    assert FORCED_FAILURE not in captured.text
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email,))
    assert ctx.channel.calls == []


def test_rerun_of_the_update_task_after_commit_changes_nothing(client: Any, clock: Any, sql: Any,
                                                               bot_api: Any, smtp: Any, settings: Any,
                                                               bot_worker_thread: Any, mail_channel: Any,
                                                               queue: Any) -> None:
    """A máscara na mesma transação do resultado (tarefas idempotentes);
    o tratador trava a linha e, se ela já está `processed`, nada faz:
    depois de um `/convite` processado, mascarado, respondido e com o envio feito, uma nova tarefa
    `telegram.update` com o mesmo payload, enfileirada pela fila com o relógio 15 s adiante,
    termina `done` na 1ª tentativa e sem efeito: a linha do update (texto mascarado, `outcome`,
    `processed_at`), os envios, os convites e a fila de saída ficam iguais; nenhuma tarefa além dela
    (nenhuma `invite.email`), nenhum `sendMessage`, nenhum e-mail e nenhum evento."""
    ctx = _setup(client, clock, sql, bot_api, smtp, settings, bot_worker_thread, mail_channel, (ALFA,),
                 code="mc102-autosservico-repeticao", batch=True)
    clock.advance(STEP)
    sender = make_user(461)
    with capture_logs() as captured:
        case = _request(ctx, "pedido com envio", sender, f"/convite {ALFA_TYPED}", masked=f"/convite {MASK}",
                        sends_to=ALFA, revokes="batch")
        update = _update_by_id(sql, case.row_id)
        sends, invites, outbox = _all_sends(sql), _all_invites(sql), _outbox_after(sql, 0)
        calls = [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=ctx.bot)]
        messages, connections = len(smtp.messages()), len(smtp.connections())
        cursor, task_mark = _last_seq(sql), _last_task(sql)

        t1 = clock.advance(STEP)
        task_id = queue.enqueue("telegram.update", {"update_row_id": case.row_id})
        record = bot_api.wait_for(lambda: (lambda r: r if r.state in ("done", "failed") else None)(
            queue.get(task_id)), timeout=PROCESS_TIMEOUT)
        # O `task.processed` sai logo depois do `complete` (mesma thread): espera por ele, com prazo.
        bot_api.wait_for(lambda: _task_states(captured, task_id), timeout=PROCESS_TIMEOUT)

    assert record == TaskRecord(id=task_id, task_type="telegram.update", state="done", attempts=1,
                                available_at=t1, lease_owner=None, lease_until=None, last_error=None,
                                finished_at=t1)
    assert _update_by_id(sql, case.row_id) == update
    assert (_all_sends(sql), _all_invites(sql), _outbox_after(sql, 0)) == (sends, invites, outbox)
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=ctx.bot)] == calls
    assert (len(smtp.messages()), len(smtp.connections())) == (messages, connections)
    assert _tasks_after(sql, task_mark) == [(task_id, "telegram.update", "done", None,
                                             {"update_row_id": case.row_id})]
    assert _event_types_after(sql, cursor) == []
    # A execução repetida não registra `telegram.update` (o tratador do bot nada faz com a linha já
    # `processed`): só o do pedido; o `task.processed` dela é `done`.
    assert [r["update_row_id"] for r in captured.records if r.get("event") == "telegram.update"] == [case.row_id]
    assert [{k: v for k, v in r.items() if k != "time"} for r in captured.records
            if r.get("event") == "task.processed" and r.get("id") == task_id] == [{**TASK_LOG, "id": task_id}]
    _assert_system_logs(captured)
    _assert_typed_emails_absent(ctx, captured, list_emails=(ALFA.email, ALFA_TYPED))
    assert ctx.channel.calls == []
