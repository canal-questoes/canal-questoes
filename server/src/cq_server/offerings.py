"""Oferecimentos: cadastro, consulta, encerramento e arquivamento.


Núcleo sem HTTP. O ciclo de vida é `draft` → `active` → `closed` → `archived`, com o arquivamento em
duas fases (preparar → confirmar). A ativação (`bots.activate`) fica com os bots. As funções que
mudam o banco abrem **uma** transação (ou um SAVEPOINT, se o chamador já tiver uma) com a mudança e
os eventos dela juntos; as regras de negócio voltam como resultados sentinela
(`CodeTaken`, `InvalidState`…), e `None` quer dizer "oferecimento inexistente".

- **Código**: identificador escolhido pelo operador, não texto livre (não é
  redigido na saída). No cadastro, depois do lock consultivo dos eventos, um código que a redação
  alteraria é recusado (`CodeRejected`), e o repetido também (`CodeTaken`), por consulta antes do
  `INSERT`, sem erro no banco. Um código no formato de UUID nunca é aceito.
- **Referência** (`ref`): o `offering_id` (UUID v4 canônico minúsculo) ou o código.
- **Disciplina e período**: texto livre, gravado **já redigido** (identidades
  lidas depois do lock dos eventos) e redigido de novo na saída, com as identidades do momento.
- **Arquivamento**: a preparação grava só o SHA-256 do código de confirmação
  (`secrets.token_urlsafe(16)`, válido por 10 minutos); a confirmação confere o resumo e a validade
  (`agora < expires_at`) e, na mesma transação, limpa a posse do bot, revoga todos os convites
  pendentes (o evento separado pelo papel) e agenda a descrição arquivada do perfil
  (`bot.sync_profile`).
- **Encerramento**: limpa, no mesmo UPDATE, o preparo do envio em lote dos convites de aluno.
- **Mensagens livres**: o booleano `offerings.free_messages`
  (desligado por padrão) e o contato do professor em `offering_contacts` (no máximo um por
  oferecimento). O e-mail é identidade: nenhuma saída o lê (o `Offering` leva só `contact_email_set`,
  por `EXISTS`), nenhum evento o leva, e o contato que a redação alteraria (identidade de pessoa
  cadastrada) é recusado no cadastro e na alteração (`ContactRejected`) e tratado como ausente na
  classificação (`free_message_settings`). O núcleo recebe o e-mail já validado pela rota
  (`people.validate_email`, aparado).

Ordem das travas: `offerings → offering_contacts → bots → enrollments → invites`, e o lock consultivo
dos eventos sempre por último. Todo horário vem do `Clock` do chamador (`now`); a ordem é a de `id`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets as _secrets
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from cq_server import tasks
from cq_server.events import EventType, append_event, load_known_identities, load_write_identities
from cq_server.notices import notify_config_change
from cq_server.redaction import KnownIdentity, redact
from cq_server.timezones import validate_timezone

# Formatos e limites.
CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
CODE_MIN_LENGTH = 3
CODE_MAX_LENGTH = 40
# Qualquer UUID (de qualquer versão): um código assim se confundiria com o identificador.
UUID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OFFERING_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
COURSE_MAX_LENGTH = 200
TERM_MAX_LENGTH = 40
RESPONSE_WINDOW_MIN = 5
RESPONSE_WINDOW_MAX = 43200
DEFAULT_RESPONSE_WINDOW = 1440

STATES = ("draft", "active", "closed", "archived")
# Estados em que o bot é consultado (os elegíveis).
POLLED_STATES = ("active", "closed")

# Código de confirmação do arquivamento.
ARCHIVE_CODE_TTL = timedelta(minutes=10)

# Tarefa do perfil do bot.
PROFILE_TASK_TYPE = "bot.sync_profile"

# Tópico do aviso aos workers.
BOT_NOTICE_TOPIC = "bot"

_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


class InvalidOffering(ValueError):
    """Valor de entrada fora das regras; `reason` é o motivo do contrato."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------------------------------
# Formatos e normalização
# --------------------------------------------------------------------------------------------------


def is_offering_id(value: object) -> bool:
    """UUID v4 canônico minúsculo."""
    return isinstance(value, str) and OFFERING_ID_PATTERN.fullmatch(value) is not None


def is_code(value: object) -> bool:
    """Código no formato do contrato e fora do formato de UUID."""
    return (isinstance(value, str) and CODE_PATTERN.fullmatch(value) is not None
            and UUID_SHAPE.fullmatch(value) is None)


def is_ref(value: object) -> bool:
    """Referência `{ref}` das rotas: o identificador ou o código; qualquer outra forma não é."""
    return is_offering_id(value) or is_code(value)


def validate_code(value: str) -> str:
    """O código sem mudança; `InvalidOffering` com `too_short`, `too_long` ou `invalid_format`."""
    if len(value) < CODE_MIN_LENGTH:
        raise InvalidOffering("too_short")
    if len(value) > CODE_MAX_LENGTH:
        raise InvalidOffering("too_long")
    if not is_code(value):
        raise InvalidOffering("invalid_format")
    return value


def _normalize_text(value: str, max_length: int) -> str:
    text = value.strip()
    if not text:
        raise InvalidOffering("too_short")
    if len(text) > max_length:
        raise InvalidOffering("too_long")
    if _UNSTORABLE.search(text):
        raise InvalidOffering("invalid_format")
    return text


def normalize_course(value: str) -> str:
    """Disciplina sem espaços nas pontas, de 1 a 200 caracteres, sem U+0000 nem substituto isolado."""
    return _normalize_text(value, COURSE_MAX_LENGTH)


def normalize_term(value: str) -> str:
    """Período sem espaços nas pontas, de 1 a 40 caracteres, sem U+0000 nem substituto isolado."""
    return _normalize_text(value, TERM_MAX_LENGTH)


def validate_response_window(value: object) -> int:
    """Inteiro de 5 a 43.200 (booleano não é inteiro): `invalid_format` ou `out_of_range`."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidOffering("invalid_format")
    if not RESPONSE_WINDOW_MIN <= value <= RESPONSE_WINDOW_MAX:
        raise InvalidOffering("out_of_range")
    return value


def code_digest(code: str) -> str:
    """SHA-256 (hex minúsculo) dos bytes UTF-8 de um código (confirmação e convite)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------
# Registros e resultados
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotSummary:
    username: str
    telegram_bot_id: int
    state: str
    profile_state: str


@dataclass(frozen=True)
class Offering:
    offering_id: uuid.UUID
    code: str
    course: str
    term: str
    timezone: str
    response_window_minutes: int
    state: str
    created_at: datetime
    activated_at: datetime | None
    closed_at: datetime | None
    archived_at: datetime | None
    bot: BotSummary | None
    # A configuração de mensagens livres e se há contato do
    # professor; o e-mail nunca entra aqui.
    free_messages: bool = False
    contact_email_set: bool = False
    # O horário do expurgo confirmado; `None` = não expurgado.
    purged_at: datetime | None = None


@dataclass(frozen=True)
class FreeMessageSettings:
    """A configuração de mensagens livres lida na classificação: `enabled` é
    `offerings.free_messages`; `contact_email` é o e-mail do contato do professor, ou `None` sem contato
    (ou com um contato que a redação alteraria). Uso interno: nunca sai pela API, evento ou log."""

    enabled: bool
    contact_email: str | None


@dataclass(frozen=True)
class OfferingPage:
    items: list[Offering]
    next_cursor: int  # cursor interno do último item, ou `after` se não houver itens


@dataclass(frozen=True)
class ArchivePreparation:
    offering: Offering
    team_members_active: int
    pending_links: int
    confirmation_code: str
    expires_at: datetime


@dataclass(frozen=True)
class CodeTaken:
    """Cadastro recusado: o código já existe (409 `already_exists`)."""


@dataclass(frozen=True)
class CodeRejected:
    """Cadastro recusado: a redação alteraria o código (400 `[{code, invalid_value}]`)."""


@dataclass(frozen=True)
class ContactRejected:
    """O contato do professor que a redação alteraria (identidade de pessoa
    cadastrada), recusado no cadastro e na alteração (400 `[{contact_email, invalid_value}]`, sem eco)."""


@dataclass(frozen=True)
class InvalidState:
    """Operação fora do estado (409 `invalid_state`); `state` é o estado atual."""

    state: str


@dataclass(frozen=True)
class InvalidConfirmation:
    """Código de confirmação vencido, substituído, de outro oferecimento ou desconhecido."""


@dataclass(frozen=True)
class BotRequired:
    """O oferecimento não tem bot (ou, na ativação, tem um bot sem acesso)."""


@dataclass(frozen=True)
class GradingPending:
    """O arquivamento recusado (409 `grading_pending`)
    enquanto há, num oferecimento com disparo `open` ou `closed`, correção pendente, update do bot não
    processado ou (só na confirmação) o leitor do bot sem consulta bem-sucedida desde o preparo. A
    contagem não sai no envelope (sai em `dispatches.get`)."""

    count: int


def generate_offering_id() -> uuid.UUID:
    """Sorteia um `offering_id` (UUID v4 de `os.urandom`, nunca derivado dos dados)."""
    return uuid.uuid4()


# --------------------------------------------------------------------------------------------------
# Leitura (uso comum com `bots` e `team`)
# --------------------------------------------------------------------------------------------------

# O `_SELECT` traz `free_messages` e a existência do contato (`EXISTS`), nunca o e-mail;
# e o `purged_at`.
_SELECT = (
    "SELECT o.id, o.offering_id, o.code, o.course, o.term, o.timezone, o.response_window_minutes, "
    "o.state, o.created_at, o.activated_at, o.closed_at, o.archived_at, "
    "b.username, b.telegram_bot_id, b.state, b.profile_state, o.free_messages, "
    "EXISTS (SELECT 1 FROM offering_contacts c WHERE c.offering_id = o.id), o.purged_at "
    "FROM offerings o LEFT JOIN bots b ON b.offering_id = o.id "
)


def _ref_clause(ref: str) -> tuple[str, Any]:
    """A condição SQL (sobre `o`) e o parâmetro da referência; `ValueError` fora do formato."""
    if is_offering_id(ref):
        return "o.offering_id = %s", uuid.UUID(ref)
    if is_code(ref):
        return "o.code = %s", ref
    raise ValueError("referência de oferecimento fora do formato")


def _offering(row: Sequence[Any], identities: Sequence[KnownIdentity],
              secrets: Sequence[str | None]) -> Offering:
    bot = None
    if row[12] is not None:
        bot = BotSummary(username=row[12], telegram_bot_id=int(row[13]), state=row[14],
                         profile_state=row[15])
    return Offering(
        offering_id=row[1], code=row[2],
        course=redact(row[3], identities, secrets=secrets),
        term=redact(row[4], identities, secrets=secrets),
        timezone=row[5], response_window_minutes=int(row[6]), state=row[7], created_at=row[8],
        activated_at=row[9], closed_at=row[10], archived_at=row[11], bot=bot,
        free_messages=bool(row[16]), contact_email_set=bool(row[17]), purged_at=row[18],
    )


def load_offering(conn: psycopg.Connection, offering_row: int, *,
                  secrets: Iterable[str | None] = (),
                  identities: Sequence[KnownIdentity] | None = None) -> Offering:
    """O `Offering` pelo id interno, com a disciplina e o período redigidos na saída."""
    row = conn.execute(_SELECT + "WHERE o.id = %s", (offering_row,)).fetchone()
    if row is None:
        raise LookupError("oferecimento inexistente")
    known = load_known_identities(conn) if identities is None else identities
    return _offering(row, known, list(secrets))


@dataclass(frozen=True)
class OfferingRow:
    """A linha travada de um oferecimento (uso interno de `offerings`, `bots` e `team`)."""

    id: int
    offering_id: uuid.UUID
    code: str
    timezone: str
    state: str
    archive_code_sha256: str | None
    archive_code_expires_at: datetime | None


def lock_offering(conn: psycopg.Connection, ref: str) -> OfferingRow | None:
    """Trava (`FOR NO KEY UPDATE`) e devolve a linha do oferecimento da referência, ou `None`.

    Precisa de uma transação aberta em `conn` (a trava vale até o COMMIT)."""
    clause, param = _ref_clause(ref)
    # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
    # continua a valer).
    row = conn.execute(
        "SELECT o.id, o.offering_id, o.code, o.timezone, o.state, o.archive_code_sha256, "
        f"o.archive_code_expires_at FROM offerings o WHERE {clause} FOR NO KEY UPDATE",
        (param,),
    ).fetchone()
    if row is None:
        return None
    return OfferingRow(id=int(row[0]), offering_id=row[1], code=row[2], timezone=row[3], state=row[4],
                       archive_code_sha256=row[5], archive_code_expires_at=row[6])


def find_offering_row(conn: psycopg.Connection, ref: str) -> OfferingRow | None:
    """Como `lock_offering`, sem travar (leitura antes de uma chamada externa)."""
    clause, param = _ref_clause(ref)
    row = conn.execute(
        "SELECT o.id, o.offering_id, o.code, o.timezone, o.state, o.archive_code_sha256, "
        f"o.archive_code_expires_at FROM offerings o WHERE {clause}",
        (param,),
    ).fetchone()
    if row is None:
        return None
    return OfferingRow(id=int(row[0]), offering_id=row[1], code=row[2], timezone=row[3], state=row[4],
                       archive_code_sha256=row[5], archive_code_expires_at=row[6])


def notify_bot_change(conn: psycopg.Connection, offering_id: uuid.UUID) -> None:
    """Aviso `bot` aos workers, entregue no COMMIT (chave = `offering_id` em texto canônico)."""
    notify_config_change(conn, BOT_NOTICE_TOPIC, str(offering_id))


def schedule_profile_sync(conn: psycopg.Connection, *, bot_id: int, token_version: int, purpose: str,
                          now: datetime) -> int:
    """Agenda `bot.sync_profile` na transação de `conn`: payload
    `{"bot_id": <id>}` e chave `bot.sync_profile:<id>:<token_version>:<purpose>`."""
    if purpose not in ("active", "archived"):
        raise ValueError("purpose deve ser active ou archived")
    return tasks.enqueue_in_transaction(
        conn, PROFILE_TASK_TYPE, {"bot_id": int(bot_id)}, now=now,
        idempotency_key=f"{PROFILE_TASK_TYPE}:{int(bot_id)}:{int(token_version)}:{purpose}")


def _state_changed(conn: psycopg.Connection, row: OfferingRow, to_state: str, *, now: datetime,
                   actor_token_id: str | None) -> None:
    append_event(conn, EventType.OFFERING_STATE_CHANGED,
                 {"offering_id": row.offering_id, "from_state": row.state, "to_state": to_state},
                 occurred_at=now, actor_token_id=actor_token_id)


def append_revocation_event(conn: psycopg.Connection, *, offering_id: uuid.UUID, person: uuid.UUID,
                            role: str, origin: str | None, reason: str, now: datetime,
                            actor_token_id: str | None) -> None:
    """O evento da revogação de um convite pelo papel da matrícula (uso comum
    com `team`): equipe → `team.link_revoked {revoke_reason}`; aluno → `student.invite_revoked
    {origin, revoke_reason}`, com a `origin` do envio que criou o convite (todo convite de aluno nasce
    de um envio)."""
    if role == "student":
        if origin is None:  # pragma: no cover - convite de aluno sempre tem envio
            raise ValueError("convite de aluno sem o envio que o criou")
        append_event(conn, EventType.STUDENT_INVITE_REVOKED,
                     {"offering_id": offering_id, "person": person, "origin": origin,
                      "revoke_reason": reason},
                     occurred_at=now, actor_token_id=actor_token_id)
    else:
        append_event(conn, EventType.TEAM_LINK_REVOKED,
                     {"offering_id": offering_id, "person": person, "revoke_reason": reason},
                     occurred_at=now, actor_token_id=actor_token_id)


# --------------------------------------------------------------------------------------------------
# Cadastro, listagem e consulta
# --------------------------------------------------------------------------------------------------


def create_offering(conn: psycopg.Connection, *, code: str, course: str, term: str, timezone: str,
                    response_window_minutes: int = DEFAULT_RESPONSE_WINDOW, now: datetime,
                    actor_token_id: str | None, secrets: Iterable[str | None] = (),
                    free_messages: bool = False, contact_email: str | None = None,
                    ) -> Offering | CodeTaken | CodeRejected | ContactRejected:
    """Cadastra um oferecimento em rascunho e grava `offering.created`, na mesma transação.

    A entrada passa de novo pelas regras (`InvalidOffering`/`TimezoneError` antes de qualquer SQL).
    Depois do lock dos eventos: código que a redação alteraria → `CodeRejected`; código repetido →
    `CodeTaken`; contato que a redação alteraria → `ContactRejected` (as
    três sem erro no banco: a transação só leu e termina em COMMIT).

    `free_messages` (desligado por padrão) entra no `INSERT` do oferecimento, e o
    `contact_email` (já validado e aparado pela rota, `people.validate_email`; `None` = sem contato), na
    mesma transação, em `offering_contacts`. O único evento é o `offering.created`.
    """
    if not isinstance(free_messages, bool):
        raise InvalidOffering("invalid_format")
    validate_code(code)
    clean_course = normalize_course(course)
    clean_term = normalize_term(term)
    validate_timezone(timezone)
    validate_response_window(response_window_minutes)
    secret_list = list(secrets)
    with conn.transaction():
        # O lock dos eventos vem antes das identidades: um cadastro de pessoa confirmado antes do
        # `offering.created` entra na checagem do código e na redação.
        identities = load_write_identities(conn)
        if redact(code, identities, secrets=secret_list) != code:
            return CodeRejected()
        if conn.execute("SELECT 1 FROM offerings WHERE code = %s", (code,)).fetchone() is not None:
            return CodeTaken()
        if contact_email is not None and _contact_rejected(contact_email, identities, secret_list):
            return ContactRejected()
        stored_course = redact(clean_course, identities, secrets=secret_list)
        stored_term = redact(clean_term, identities, secrets=secret_list)
        offering_id = generate_offering_id()
        row = conn.execute(
            "INSERT INTO offerings (offering_id, code, course, term, timezone, response_window_minutes, "
            "state, created_at, free_messages) VALUES (%s, %s, %s, %s, %s, %s, 'draft', %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (offering_id, code, stored_course, stored_term, timezone, response_window_minutes, now,
             free_messages),
        ).fetchone()
        if row is None:
            # Só com um INSERT concorrente sem o lock (ex.: SQL cru): sem erro no banco.
            return CodeTaken()
        if contact_email is not None:
            conn.execute(
                "INSERT INTO offering_contacts (offering_id, email, updated_at) VALUES (%s, %s, %s)",
                (int(row[0]), contact_email, now))
        append_event(conn, EventType.OFFERING_CREATED, {"offering_id": offering_id, "code": code},
                     occurred_at=now, actor_token_id=actor_token_id)
    return Offering(offering_id=offering_id, code=code, course=stored_course, term=stored_term,
                    timezone=timezone, response_window_minutes=response_window_minutes, state="draft",
                    created_at=now, activated_at=None, closed_at=None, archived_at=None, bot=None,
                    free_messages=free_messages, contact_email_set=contact_email is not None)


def list_offerings(conn: psycopg.Connection, *, after: int, limit: int,
                   secrets: Iterable[str | None] = ()) -> OfferingPage:
    """Oferecimentos com cursor interno maior que `after`, em ordem de cadastro, no máximo `limit`."""
    secret_list = list(secrets)
    with conn.transaction():
        rows = conn.execute(_SELECT + "WHERE o.id > %s ORDER BY o.id LIMIT %s", (after, limit)).fetchall()
        identities = load_known_identities(conn)
    items = [_offering(row, identities, secret_list) for row in rows]
    next_cursor = int(rows[-1][0]) if rows else after
    return OfferingPage(items=items, next_cursor=next_cursor)


def get_offering(conn: psycopg.Connection, ref: str, *,
                 secrets: Iterable[str | None] = ()) -> Offering | None:
    """O oferecimento da referência (identificador ou código), ou `None`."""
    clause, param = _ref_clause(ref)
    with conn.transaction():
        row = conn.execute(_SELECT + f"WHERE {clause}", (param,)).fetchone()
        if row is None:
            return None
        identities = load_known_identities(conn)
    return _offering(row, identities, list(secrets))


# --------------------------------------------------------------------------------------------------
# Configuração de mensagens livres
# --------------------------------------------------------------------------------------------------

# Marca de "campo não enviado" na alteração (o `None` do contato quer dizer "remover").
UNSET: Any = object()


def _contact_rejected(address: str, identities: Sequence[KnownIdentity],
                      secrets: Sequence[str | None]) -> bool:
    """O contato que a redação alteraria (identidade de pessoa cadastrada)."""
    return redact(address, identities, secrets=secrets) != address


def update_settings(conn: psycopg.Connection, ref: str, *, free_messages: Any = UNSET,
                    contact_email: Any = UNSET, now: datetime, actor_token_id: str | None,
                    secrets: Iterable[str | None] = (),
                    ) -> Offering | InvalidState | ContactRejected | None:
    """Altera a configuração de mensagens livres (`offerings.update_settings`).

    `free_messages`: booleano; `contact_email`: o e-mail já validado e aparado pela rota (define ou
    troca) ou `None` (remove); um campo omitido (`UNSET`) não muda. Trava o oferecimento e depois a
    linha do contato, com o lock dos eventos por último. Ordem das regras: inexistente →
    `None`; `archived` → `InvalidState`; contato que a redação alteraria → `ContactRejected`. Sem
    nenhum valor mudado (o mesmo booleano; o mesmo endereço, comparado exatamente; remover um contato
    inexistente), devolve o `Offering` sem gravar nem gerar evento. Com mudança, grava e gera
    `offering.settings_changed {offering_id, free_messages, contact_email_set}` (sem o e-mail), com o
    token de quem pediu.
    """
    if free_messages is UNSET and contact_email is UNSET:
        raise ValueError("a alteração precisa de pelo menos um campo")
    if free_messages is not UNSET and not isinstance(free_messages, bool):
        raise InvalidOffering("invalid_format")
    if contact_email is not UNSET and contact_email is not None and not isinstance(contact_email, str):
        raise InvalidOffering("invalid_format")
    secret_list = list(secrets)
    with conn.transaction():
        row = lock_offering(conn, ref)
        if row is None:
            return None
        if row.state == "archived":
            return InvalidState(row.state)
        current_flag = conn.execute("SELECT free_messages FROM offerings WHERE id = %s",
                                    (row.id,)).fetchone()
        assert current_flag is not None
        contact = conn.execute("SELECT email FROM offering_contacts WHERE offering_id = %s FOR UPDATE",
                               (row.id,)).fetchone()
        current_email = None if contact is None else contact[0]
        identities = load_write_identities(conn) if isinstance(contact_email, str) else None
        if isinstance(contact_email, str):
            assert identities is not None
            if _contact_rejected(contact_email, identities, secret_list):
                return ContactRejected()
        new_flag = bool(current_flag[0]) if free_messages is UNSET else free_messages
        new_email = current_email if contact_email is UNSET else contact_email
        flag_changed = new_flag != bool(current_flag[0])
        email_changed = new_email != current_email
        if not flag_changed and not email_changed:
            return load_offering(conn, row.id, secrets=secret_list, identities=identities)
        if flag_changed:
            conn.execute("UPDATE offerings SET free_messages = %s WHERE id = %s", (new_flag, row.id))
        if email_changed:
            if new_email is None:
                conn.execute("DELETE FROM offering_contacts WHERE offering_id = %s", (row.id,))
            else:
                conn.execute(
                    "INSERT INTO offering_contacts (offering_id, email, updated_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (offering_id) DO UPDATE SET email = EXCLUDED.email, "
                    "updated_at = EXCLUDED.updated_at",
                    (row.id, new_email, now))
        append_event(conn, EventType.OFFERING_SETTINGS_CHANGED,
                     {"offering_id": row.offering_id, "free_messages": new_flag,
                      "contact_email_set": new_email is not None},
                     occurred_at=now, actor_token_id=actor_token_id, identities=identities)
        return load_offering(conn, row.id, secrets=secret_list, identities=identities)


def free_message_settings(conn: psycopg.Connection, offering_row_id: int, *,
                          secrets: Iterable[str | None] = (),
                          identities: Sequence[KnownIdentity] | None = None) -> FreeMessageSettings:
    """A configuração de mensagens livres do oferecimento (id interno) no momento do processamento,
    sem trava nas linhas. Desligada e com contato, confere o contato contra
    as identidades da gravação (`load_write_identities`, ou as que o chamador já carregou na mesma
    transação) e devolve `contact_email = None` se a redação o alteraria (a identidade só sai pela revelação). Ligada, o
    contato não é usado e volta `None`."""
    row = conn.execute(
        "SELECT o.free_messages, c.email FROM offerings o "
        "LEFT JOIN offering_contacts c ON c.offering_id = o.id WHERE o.id = %s",
        (offering_row_id,),
    ).fetchone()
    if row is None:
        raise LookupError("oferecimento inexistente")
    enabled = bool(row[0])
    email = row[1]
    if enabled or email is None:
        return FreeMessageSettings(enabled=enabled, contact_email=None)
    known = load_write_identities(conn) if identities is None else identities
    if _contact_rejected(email, known, list(secrets)):
        return FreeMessageSettings(enabled=False, contact_email=None)
    return FreeMessageSettings(enabled=False, contact_email=email)


# --------------------------------------------------------------------------------------------------
# Encerramento e arquivamento
# --------------------------------------------------------------------------------------------------


def close_offering(conn: psycopg.Connection, ref: str, *, now: datetime, actor_token_id: str | None,
                   secrets: Iterable[str | None] = ()) -> Offering | InvalidState | None:
    """`active` → `closed`, com `closed_at` e `offering.state_changed`; já `closed`: sem mudança;
    `draft`/`archived`: `InvalidState`. A consulta do bot continua (encerrado é elegível). O mesmo
    UPDATE limpa o preparo do envio em lote dos convites de aluno (as
    quatro colunas só valem em `active`)."""
    with conn.transaction():
        row = lock_offering(conn, ref)
        if row is None:
            return None
        if row.state == "active":
            from cq_server import dispatches  # importação tardia (sem ciclo no carregamento)

            conn.execute(
                "UPDATE offerings SET state = 'closed', closed_at = %s, invite_code_sha256 = NULL, "
                "invite_code_expires_at = NULL, invite_valid_hours = NULL, invite_eligible_count = NULL "
                "WHERE id = %s",
                (now, row.id))
            # Os disparos `validating` e `scheduled` são cancelados
            # (`offering_closed`, um evento por disparo com o token do encerramento); as travas das
            # linhas deles vêm antes do lock dos eventos.
            dispatches.cancel_for_offering_close(conn, row.id, now=now, actor_token_id=actor_token_id)
            _state_changed(conn, row, "closed", now=now, actor_token_id=actor_token_id)
        elif row.state != "closed":
            return InvalidState(row.state)
        return load_offering(conn, row.id, secrets=secrets)


def _archive_summary(conn: psycopg.Connection, offering_row: int, now: datetime) -> tuple[int, int]:
    """(`team_members_active`, `pending_links`) do resumo do arquivamento."""
    row = conn.execute(
        "SELECT "
        "(SELECT count(*) FROM enrollments e WHERE e.offering_id = %(o)s AND e.role = 'team' "
        "   AND e.state = 'active'), "
        "(SELECT count(*) FROM invites i JOIN enrollments e ON e.id = i.enrollment_id "
        "   WHERE e.offering_id = %(o)s AND i.state = 'pending' AND %(now)s < i.expires_at)",
        {"o": offering_row, "now": now},
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def prepare_archive(conn: psycopg.Connection, ref: str, *, now: datetime, actor_token_id: str | None,
                    secrets: Iterable[str | None] = (),
                    ) -> ArchivePreparation | InvalidState | GradingPending | None:
    """Gera o código de confirmação (substitui o anterior) de um `draft` ou `closed`, com o resumo do
    que o arquivamento afeta e `offering.archive_prepared`; outro estado: `InvalidState`.

    Com disparo `open` ou `closed`, `GradingPending` enquanto houver correção
    pendente ou update do bot não processado (a barreira do leitor só vale na confirmação)."""
    with conn.transaction():
        row = lock_offering(conn, ref)
        if row is None:
            return None
        if row.state not in ("draft", "closed"):
            return InvalidState(row.state)
        blockers = _archive_blockers(conn, row.id, prepared_at=None)
        if blockers:
            return GradingPending(blockers)
        confirmation_code = _secrets.token_urlsafe(16)
        expires_at = now + ARCHIVE_CODE_TTL
        conn.execute(
            "UPDATE offerings SET archive_code_sha256 = %s, archive_code_expires_at = %s WHERE id = %s",
            (code_digest(confirmation_code), expires_at, row.id),
        )
        team_members_active, pending_links = _archive_summary(conn, row.id, now)
        append_event(conn, EventType.OFFERING_ARCHIVE_PREPARED,
                     {"offering_id": row.offering_id, "expires_at": expires_at},
                     occurred_at=now, actor_token_id=actor_token_id)
        offering = load_offering(conn, row.id, secrets=secrets)
    return ArchivePreparation(offering=offering, team_members_active=team_members_active,
                              pending_links=pending_links, confirmation_code=confirmation_code,
                              expires_at=expires_at)


def _archive_blockers(conn: psycopg.Connection, offering_row: int, *, prepared_at: datetime | None) -> int:
    """`dispatches.archive_blockers` (importação tardia, sem ciclo no carregamento)."""
    from cq_server import dispatches

    return dispatches.archive_blockers(conn, offering_row, prepared_at=prepared_at)


def _confirmation_matches(row: OfferingRow, confirmation_code: str, now: datetime) -> bool:
    """O código confere com o resumo pendente e ainda vale (`agora < expires_at`)."""
    if row.archive_code_sha256 is None or row.archive_code_expires_at is None:
        return False
    if not now < row.archive_code_expires_at:
        return False
    return hmac.compare_digest(code_digest(confirmation_code), row.archive_code_sha256)


def archive_offering(conn: psycopg.Connection, ref: str, *, confirmation_code: str, now: datetime,
                     actor_token_id: str | None, secrets: Iterable[str | None] = (),
                     ) -> Offering | InvalidState | GradingPending | InvalidConfirmation | None:
    """Confirma o arquivamento: `draft`/`closed` → `archived`.

    Já `archived`: devolve o estado atual, sem conferir o código. `active`: `InvalidState`. Código
    que não confere ou vencido: `InvalidConfirmation`, sem mudança. Na mesma transação, nesta ordem
    de eventos: posse do bot limpa (`bot.lease_changed {released, archived}`, se havia dono); todo
    convite `pending` revogado, vencido ou não, um evento por convite, em ordem de `invites.id`,
    separado pelo papel da matrícula: `team.link_revoked {archived}` para os de
    equipe e `student.invite_revoked {archived}` (com a `origin` do envio que criou o convite) para
    os de aluno; a tarefa `bot.sync_profile` (descrição arquivada); e `offering.state_changed` por
    último; aviso `bot`. Os envios de convite `pending` não mudam: a tarefa deles os cancela
    (`offering_inactive`).

    Depois do `invalid_state`, `GradingPending` com disparo `open`
    ou `closed` e correção pendente, update do bot não processado ou o leitor do bot vivo sem consulta
    bem-sucedida desde o preparo (`prepared_at = archive_code_expires_at − ARCHIVE_CODE_TTL`, lido sob a
    trava do oferecimento). Aceito, os disparos abertos fecham como no horário (`trigger: archive`),
    com as travas deles antes do lock dos eventos.
    """
    with conn.transaction():
        row = lock_offering(conn, ref)
        if row is None:
            return None
        if row.state == "archived":
            return load_offering(conn, row.id, secrets=secrets)
        if row.state not in ("draft", "closed"):
            return InvalidState(row.state)
        prepared_at = (None if row.archive_code_expires_at is None
                       else row.archive_code_expires_at - ARCHIVE_CODE_TTL)
        # Todas as travas de linha antes do lock dos eventos (na ordem fixa das travas). A do
        # bot vem antes da contagem dos bloqueios: um lote do leitor em
        # gravação (`reception.store_updates`, `FOR NO KEY UPDATE`) termina antes e entra na contagem, e
        # um que chegue depois encontra a posse limpa e é descartado.
        # Bot em FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das
        # travas continua a valer).
        bot = conn.execute(
            "SELECT id, token_version, lease_owner FROM bots WHERE offering_id = %s FOR NO KEY UPDATE",
            (row.id,),
        ).fetchone()
        blockers = _archive_blockers(conn, row.id, prepared_at=prepared_at)
        if blockers:
            return GradingPending(blockers)
        if not _confirmation_matches(row, confirmation_code, now):
            return InvalidConfirmation()

        invites = conn.execute(
            "SELECT i.id, p.pseudo_id, e.role, s.origin FROM invites i "
            "JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id "
            "LEFT JOIN invite_sends s ON s.id = i.invite_send_id "
            "WHERE e.offering_id = %s AND i.state = 'pending' ORDER BY i.id FOR UPDATE OF i",
            (row.id,),
        ).fetchall()
        # Os disparos abertos fecham como no horário, com as travas deles (e das
        # entregas) antes do lock dos eventos; o `dispatch.closed` sai com `trigger: archive`.
        from cq_server import dispatches

        dispatches.close_for_archive(conn, row.id, now=now)

        conn.execute(
            "UPDATE offerings SET state = 'archived', archived_at = %s, archive_code_sha256 = NULL, "
            "archive_code_expires_at = NULL WHERE id = %s",
            (now, row.id),
        )
        if bot is not None and bot[2] is not None:
            conn.execute("UPDATE bots SET lease_owner = NULL, lease_until = NULL WHERE id = %s", (bot[0],))
            append_event(conn, EventType.BOT_LEASE_CHANGED,
                         {"offering_id": row.offering_id, "worker_id": bot[2], "change": "released",
                          "reason": "archived"},
                         occurred_at=now, actor_token_id=actor_token_id)
        if invites:
            conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = ANY(%s)",
                         (now, [int(invite[0]) for invite in invites]))
            for _, pseudo_id, role, origin in invites:
                append_revocation_event(conn, offering_id=row.offering_id, person=pseudo_id, role=role,
                                        origin=origin, reason="archived", now=now,
                                        actor_token_id=actor_token_id)
        if bot is not None:
            schedule_profile_sync(conn, bot_id=int(bot[0]), token_version=int(bot[1]),
                                  purpose="archived", now=now)
        _state_changed(conn, row, "archived", now=now, actor_token_id=actor_token_id)
        notify_bot_change(conn, row.offering_id)
        return load_offering(conn, row.id, secrets=secrets)
