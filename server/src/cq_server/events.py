"""Log de eventos, com o catálogo fechado dos tipos e dos campos de cada um
(48 tipos).

Funções de núcleo, sem HTTP, que os módulos de domínio usam **na transação do chamador**:

- `append_event(conn, event_type, data, *, occurred_at, actor_token_id=None, secrets=(),
  identities=None) -> int`: grava um evento e devolve o `seq`. O `data` é conferido contra os campos
  do tipo (objeto fechado) e o texto livre dele (`label`, `reason`, `error`) é
  **redigido na gravação**, com as identidades cadastradas naquele momento (as da própria transação
  inclusive). Não confirma nada: o evento entra junto com a mudança, no COMMIT do chamador. A
  ordem de `seq` é garantida pelo gatilho de lock consultivo do banco, com a
  chave `EVENTS_LOCK_KEY`.
- `load_known_identities(conn) -> list[KnownIdentity]`: as identidades de todas as pessoas, em ordem
  crescente de `people.id` (a ordem de desempate da redação).
- `load_write_identities(conn) -> list[KnownIdentity]`: as mesmas, para a redação **na gravação**:
  toma antes o lock consultivo dos eventos, na transação de `conn`, para que nenhum cadastro
  confirme entre a leitura e o COMMIT de quem grava.
- `redact_free_text(conn, text, *, secrets=(), identities=None) -> str`: redige um texto livre que
  não é evento (ex.: `api_tokens.label`, `tasks.last_error`, `outbox_messages.last_error`) com as
  identidades do momento.
- `redact_event_data(event_type, data, identities, *, secrets=()) -> dict`: redige só os campos de
  texto livre de um `data` (usado na gravação e de novo na saída).
- `query_events(conn, types, *, after=0, limit=100, secrets=(), identities=None) -> EventPage`: lê
  eventos por cursor (`seq > after`, ordem de `seq`, no máximo `limit`), com o texto livre redigido
  **de novo na saída**, com as identidades do momento da leitura, e `occurred_at` em UTC.

`secrets` são os valores literais a trocar por `[segredo]` além do padrão `cq_…`; os chamadores
passam `(settings.admin_token,)` (`None` é ignorado). `identities`, quando o chamador já as
carregou na mesma transação (na gravação, com `load_write_identities`), evita outra consulta.

Nenhuma dessas funções devolve identidade: `load_known_identities` só alimenta a redação.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server.redaction import KnownIdentity, redact


class EventType(StrEnum):
    PERSON_REGISTERED = "person.registered"
    TOKEN_CREATED = "token.created"
    TOKEN_REVOKED = "token.revoked"
    IDENTITY_REVEALED = "identity.revealed"
    IDENTITY_REVEAL_DENIED = "identity.reveal_denied"
    TASK_FAILED = "task.failed"
    OUTBOX_FAILED = "outbox.failed"
    # Oferecimentos, bots e equipe.
    OFFERING_CREATED = "offering.created"
    OFFERING_STATE_CHANGED = "offering.state_changed"
    OFFERING_ARCHIVE_PREPARED = "offering.archive_prepared"
    BOT_REGISTERED = "bot.registered"
    BOT_TOKEN_REPLACED = "bot.token_replaced"
    BOT_PROFILE_UPDATED = "bot.profile_updated"
    BOT_LEASE_CHANGED = "bot.lease_changed"
    BOT_ACCESS_CHANGED = "bot.access_changed"
    TEAM_LINK_CREATED = "team.link_created"
    TEAM_LINK_REVOKED = "team.link_revoked"
    TEAM_JOINED = "team.joined"
    TEAM_JOIN_REFUSED = "team.join_refused"
    ENDPOINT_BLOCKED = "endpoint.blocked"
    OUTBOX_REPLY_FAILED = "outbox.reply_failed"
    # Lista de alunos e convites.
    STUDENT_LIST_IMPORTED = "student.list_imported"
    STUDENT_INVITES_PREPARED = "student.invites_prepared"
    STUDENT_INVITES_QUEUED = "student.invites_queued"
    STUDENT_INVITE_SENT = "student.invite_sent"
    STUDENT_INVITE_FAILED = "student.invite_failed"
    STUDENT_INVITE_CANCELLED = "student.invite_cancelled"
    STUDENT_INVITE_REVOKED = "student.invite_revoked"
    STUDENT_JOINED = "student.joined"
    # Disparos, entregas e correção.
    DISPATCH_PREPARED = "dispatch.prepared"
    DISPATCH_VALIDATED = "dispatch.validated"
    DISPATCH_REJECTED = "dispatch.rejected"
    DISPATCH_CONFIRMED = "dispatch.confirmed"
    DISPATCH_CANCELLED = "dispatch.cancelled"
    DISPATCH_OPENED = "dispatch.opened"
    DISPATCH_CLOSED = "dispatch.closed"
    DELIVERY_DELIVERED = "delivery.delivered"
    DELIVERY_NOT_DELIVERED = "delivery.not_delivered"
    DELIVERY_QUESTION_FAILED = "delivery.question_failed"
    DELIVERY_DONT_KNOW = "delivery.dont_know"
    SUBMISSION_GRADED = "submission.graded"
    SUBMISSION_INTERNAL_FAILURE = "submission.internal_failure"
    # A configuração de mensagens
    # livres do oferecimento alterada, sem o e-mail do contato.
    OFFERING_SETTINGS_CHANGED = "offering.settings_changed"
    # Relatórios, avisos e resumo diário.
    REPORT_FROZEN = "report.frozen"
    NOTICE_RECORDED = "notice.recorded"
    OFFERING_DIGEST_CHANGED = "offering.digest_changed"
    # A preparação e a confirmação do
    # expurgo, só com contagens (sem pseudônimo, identidade nem o código).
    OFFERING_PURGE_PREPARED = "offering.purge_prepared"
    OFFERING_PURGED = "offering.purged"


# Os campos de `data` de cada tipo (objetos fechados; dos de oferecimentos em diante, cada tipo
# tem um conjunto de campos distinto dos demais). Os 8 tipos de convites não têm texto
# livre, identidade, código, chat nem e-mail; os 14 de disparos também não têm texto livre, texto digitado,
# URL do repositório nem detalhe técnico (`person` é o pseudônimo; os `*_id` são os `uuid`); o
# `offering.settings_changed` leva só o booleano `contact_email_set`, nunca o e-mail.
EVENT_FIELDS: dict[EventType, frozenset[str]] = {
    EventType.PERSON_REGISTERED: frozenset({"pseudo_id", "alias"}),
    EventType.TOKEN_CREATED: frozenset({"token_id", "label", "permissions"}),
    EventType.TOKEN_REVOKED: frozenset({"token_id"}),
    EventType.IDENTITY_REVEALED: frozenset({"client", "revealed", "not_found_count", "reason"}),
    EventType.IDENTITY_REVEAL_DENIED: frozenset({"client", "requested", "not_found_count"}),
    EventType.TASK_FAILED: frozenset({"task_id", "task_type", "attempts", "error"}),
    EventType.OUTBOX_FAILED: frozenset({"message_id", "recipient", "attempts", "error"}),
    EventType.OFFERING_CREATED: frozenset({"offering_id", "code"}),
    EventType.OFFERING_STATE_CHANGED: frozenset({"offering_id", "from_state", "to_state"}),
    EventType.OFFERING_ARCHIVE_PREPARED: frozenset({"offering_id", "expires_at"}),
    EventType.BOT_REGISTERED: frozenset({"offering_id", "telegram_bot_id", "username"}),
    EventType.BOT_TOKEN_REPLACED: frozenset({"offering_id", "telegram_bot_id", "token_version"}),
    EventType.BOT_PROFILE_UPDATED: frozenset({"offering_id", "profile_state", "purpose"}),
    EventType.BOT_LEASE_CHANGED: frozenset({"offering_id", "worker_id", "change", "reason"}),
    EventType.BOT_ACCESS_CHANGED: frozenset({"offering_id", "access", "problem"}),
    EventType.TEAM_LINK_CREATED: frozenset({"offering_id", "person", "expires_at"}),
    EventType.TEAM_LINK_REVOKED: frozenset({"offering_id", "person", "revoke_reason"}),
    EventType.TEAM_JOINED: frozenset({"offering_id", "person", "endpoint_replaced"}),
    EventType.TEAM_JOIN_REFUSED: frozenset({"offering_id", "refusal"}),
    EventType.ENDPOINT_BLOCKED: frozenset({"offering_id", "person", "channel"}),
    EventType.OUTBOX_REPLY_FAILED: frozenset({"message_id", "reply_to", "attempts", "error"}),
    EventType.STUDENT_LIST_IMPORTED: frozenset({"offering_id", "received", "people_created",
                                                "people_reused", "enrollments_created",
                                                "already_enrolled"}),
    EventType.STUDENT_INVITES_PREPARED: frozenset({"offering_id", "eligible", "valid_hours",
                                                   "expires_at"}),
    EventType.STUDENT_INVITES_QUEUED: frozenset({"offering_id", "origin", "queued"}),
    EventType.STUDENT_INVITE_SENT: frozenset({"offering_id", "person", "origin", "expires_at"}),
    EventType.STUDENT_INVITE_FAILED: frozenset({"offering_id", "person", "origin", "failure"}),
    EventType.STUDENT_INVITE_CANCELLED: frozenset({"offering_id", "person", "origin",
                                                   "cancel_reason"}),
    EventType.STUDENT_INVITE_REVOKED: frozenset({"offering_id", "person", "origin",
                                                 "revoke_reason"}),
    EventType.STUDENT_JOINED: frozenset({"offering_id", "person", "terms_version",
                                         "endpoint_replaced"}),
    EventType.DISPATCH_PREPARED: frozenset({"offering_id", "dispatch_id", "question_ref_id",
                                            "origin"}),
    EventType.DISPATCH_VALIDATED: frozenset({"offering_id", "dispatch_id", "target_count",
                                             "warnings"}),
    EventType.DISPATCH_REJECTED: frozenset({"offering_id", "dispatch_id", "rejection_reason"}),
    EventType.DISPATCH_CONFIRMED: frozenset({"offering_id", "dispatch_id", "target_count", "opens_at",
                                             "closes_at"}),
    EventType.DISPATCH_CANCELLED: frozenset({"offering_id", "dispatch_id", "cancel_reason"}),
    EventType.DISPATCH_OPENED: frozenset({"offering_id", "dispatch_id", "target_count"}),
    EventType.DISPATCH_CLOSED: frozenset({"offering_id", "dispatch_id", "trigger", "counts"}),
    EventType.DELIVERY_DELIVERED: frozenset({"offering_id", "dispatch_id", "delivery_id", "person"}),
    EventType.DELIVERY_NOT_DELIVERED: frozenset({"offering_id", "dispatch_id", "delivery_id", "person",
                                                 "undelivered_reason"}),
    EventType.DELIVERY_QUESTION_FAILED: frozenset({"offering_id", "dispatch_id", "delivery_id",
                                                   "person", "failure"}),
    EventType.DELIVERY_DONT_KNOW: frozenset({"offering_id", "dispatch_id", "delivery_id",
                                             "submission_id", "person"}),
    EventType.SUBMISSION_GRADED: frozenset({"offering_id", "dispatch_id", "delivery_id",
                                            "submission_id", "question_ref_id", "person", "valid",
                                            "late", "normalized", "score", "parts"}),
    EventType.SUBMISSION_INTERNAL_FAILURE: frozenset({"offering_id", "dispatch_id", "delivery_id",
                                                      "submission_id", "person", "engine_error"}),
    EventType.OFFERING_SETTINGS_CHANGED: frozenset({"offering_id", "free_messages",
                                                    "contact_email_set"}),
    # Relatórios e avisos: sem pseudônimo, identidade nem texto livre; sem chaves internas;
    # `recipient_count`, nunca `recipient`. O assunto do aviso vai nos campos próprios de cada tipo
    # (nulos fora dele).
    EventType.REPORT_FROZEN: frozenset({"offering_id", "dispatch_id", "version", "trigger"}),
    EventType.NOTICE_RECORDED: frozenset({"offering_id", "notice_type", "dispatch_id",
                                          "report_version", "episode_seq", "batch_id",
                                          "window_started_at", "local_date", "state",
                                          "recipient_count"}),
    EventType.OFFERING_DIGEST_CHANGED: frozenset({"offering_id", "enabled", "local_time"}),
    # Expurgo: só contagens e o booleano do contato; sem pseudônimo, identidade nem o
    # código de confirmação (nem o resumo dele).
    EventType.OFFERING_PURGE_PREPARED: frozenset({"offering_id", "expires_at", "identities_to_delete",
                                                  "identities_kept", "endpoints", "updates",
                                                  "contact_email_set"}),
    EventType.OFFERING_PURGED: frozenset({"offering_id", "identities_deleted", "identities_kept",
                                          "endpoints", "updates", "free_messages_redacted",
                                          "outbox_redacted", "contact_email_deleted"}),
}

# Os campos de texto livre (`x-free-text`) de cada tipo.
# O `code` de `offering.created` e o `username` de `bot.registered` são identificadores, não texto
# livre. Os tipos de convites, de disparos e de relatórios não têm texto livre.
FREE_TEXT_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.TOKEN_CREATED: ("label",),
    EventType.IDENTITY_REVEALED: ("reason",),
    EventType.TASK_FAILED: ("error",),
    EventType.OUTBOX_FAILED: ("error",),
    EventType.OUTBOX_REPLY_FAILED: ("error",),
}

# Os campos de pessoa
# (pseudônimo) de cada tipo, lista fechada, 19 dos 48 tipos; `is_list` marca os campos que guardam uma
# lista de pseudônimos. É a base do filtro `person` de `query_events`.
@dataclass(frozen=True)
class PersonField:
    name: str
    is_list: bool


_PERSON = (PersonField("person", False),)

PERSON_FIELDS: Mapping[EventType, tuple[PersonField, ...]] = {
    EventType.PERSON_REGISTERED: (PersonField("pseudo_id", False),),
    EventType.IDENTITY_REVEALED: (PersonField("revealed", True),),
    EventType.IDENTITY_REVEAL_DENIED: (PersonField("requested", True),),
    EventType.OUTBOX_FAILED: (PersonField("recipient", False),),
    EventType.TEAM_LINK_CREATED: _PERSON,
    EventType.TEAM_LINK_REVOKED: _PERSON,
    EventType.TEAM_JOINED: _PERSON,
    EventType.ENDPOINT_BLOCKED: _PERSON,
    EventType.STUDENT_INVITE_SENT: _PERSON,
    EventType.STUDENT_INVITE_FAILED: _PERSON,
    EventType.STUDENT_INVITE_CANCELLED: _PERSON,
    EventType.STUDENT_INVITE_REVOKED: _PERSON,
    EventType.STUDENT_JOINED: _PERSON,
    EventType.DELIVERY_DELIVERED: _PERSON,
    EventType.DELIVERY_NOT_DELIVERED: _PERSON,
    EventType.DELIVERY_QUESTION_FAILED: _PERSON,
    EventType.DELIVERY_DONT_KNOW: _PERSON,
    EventType.SUBMISSION_GRADED: _PERSON,
    EventType.SUBMISSION_INTERNAL_FAILURE: _PERSON,
}

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Chave fixa do lock consultivo de transação do log de eventos.
# É a mesma que o gatilho `events_serialize_writers` da migração 0001 toma, escrita lá como literal:
# as duas precisam ser iguais. Quem precisa da ordem de confirmação antes do INSERT do evento (ex.:
# `people.register_person`) toma o lock com ela no início da transação, e quem redige texto livre na
# gravação o toma antes de ler as identidades (`load_write_identities`).
EVENTS_LOCK_KEY = 6371000002


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    type: str
    occurred_at: datetime  # UTC
    actor_token_id: str | None
    data: dict


@dataclass(frozen=True)
class EventPage:
    items: list[StoredEvent]
    next_cursor: int  # `seq` do último item, ou `after` se não houver itens


def load_known_identities(conn: psycopg.Connection) -> list[KnownIdentity]:
    """As identidades cadastradas, em ordem crescente de `people.id` (só para a redação)."""
    rows = conn.execute(
        "SELECT p.alias, i.full_name, i.ra, i.email FROM people p "
        "JOIN identities i ON i.person_id = p.id ORDER BY p.id"
    ).fetchall()
    return [KnownIdentity(alias=alias, full_name=full_name, ra=ra, email=email)
            for alias, full_name, ra, email in rows]


def load_write_identities(conn: psycopg.Connection) -> list[KnownIdentity]:
    """As identidades para a redação **na gravação**: toma o lock dos eventos e só então as carrega.

    O cadastro (`people.register_person`) segura o lock consultivo dos eventos (`EVENTS_LOCK_KEY`)
    do início ao COMMIT. Se quem grava texto livre carregasse as identidades antes do lock, um
    cadastro aberto ficaria de fora da leitura; o escritor esperaria o lock no INSERT do evento (o
    gatilho `events_serialize_writers`) e gravaria o nome sem redação, com `seq` **depois** de
    `person.registered`: a pessoa já estava cadastrada no momento da gravação, e o texto a mostra
    sem redação. Com o lock antes da leitura, em READ COMMITTED ela vê todo cadastro confirmado
    antes, e nenhum outro cadastro confirma até o COMMIT de quem grava (o mesmo argumento de
    `seq` vale para o cadastro). O lock é reentrante na mesma sessão (o gatilho
    o toma de novo sem esperar) e fica até o COMMIT/ROLLBACK; por isso `conn` precisa estar numa
    transação.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK_KEY,))
    return load_known_identities(conn)


def redact_free_text(conn: psycopg.Connection, text: str, *, secrets: Iterable[str | None] = (),
                     identities: Sequence[KnownIdentity] | None = None) -> str:
    """Redige um texto livre com as identidades do momento (carregadas de `conn` se não vierem).

    Na gravação, o chamador passa as de `load_write_identities`, tomadas na mesma transação.
    """
    known = load_known_identities(conn) if identities is None else identities
    return redact(text, known, secrets=secrets)


def redact_event_data(event_type: str, data: Mapping[str, Any], identities: Sequence[KnownIdentity],
                      *, secrets: Iterable[str | None] = ()) -> dict:
    """Cópia de `data` com os campos de texto livre do tipo redigidos (os demais ficam iguais)."""
    out = dict(data)
    fields = FREE_TEXT_FIELDS.get(EventType(event_type), ())
    secret_list = list(secrets)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = redact(value, identities, secrets=secret_list)
    return out


def _timestamp(value: datetime) -> str:
    """Horário RFC 3339 em UTC com `Z`, sem fração quando os microssegundos são zero (a forma dos
    horários da API), para os horários dentro de `data` (ex.: `expires_at`)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("horário sem fuso nos dados do evento")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, date):
        # Data sem horário (ex.: o `local_date` do `notice.recorded`): `AAAA-MM-DD`.
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def append_event(conn: psycopg.Connection, event_type: str, data: Mapping[str, Any], *,
                 occurred_at: datetime, actor_token_id: str | None = None,
                 secrets: Iterable[str | None] = (),
                 identities: Sequence[KnownIdentity] | None = None) -> int:
    """Grava um evento na transação de `conn` e devolve o `seq` (ver o docstring do módulo).

    - `event_type`: um `EventType` (ou o texto dele); outro valor é `ValueError`.
    - `data`: exatamente os campos do tipo; `uuid.UUID` vira texto, e `datetime`
      com fuso, o horário em UTC com `Z` (sem fuso é `ValueError`); `date`, `AAAA-MM-DD`. Campo
      a mais ou a menos é `ValueError`. As validações vêm antes de qualquer SQL: depois do `ValueError`, a
      transação do chamador segue usável.
    - `occurred_at`: horário com fuso, do `Clock` da aplicação (gravado como veio).
    - `actor_token_id`: `token_id` em texto, `"bootstrap"` (token inicial) ou `None` (worker).
    - `identities`: se vierem, o chamador as carregou com `load_write_identities` na mesma
      transação; se não, e o tipo tiver texto livre, são carregadas aqui depois do lock dos eventos.
    """
    etype = EventType(event_type)
    keys = set(data)
    if keys != EVENT_FIELDS[etype]:
        raise ValueError(f"campos de dados inválidos para o evento {etype.value}")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at precisa ter fuso horário")
    payload = _jsonable(data)
    if etype in FREE_TEXT_FIELDS:
        # O lock dos eventos vem antes da leitura das identidades, e não só no INSERT abaixo: um
        # cadastro concorrente confirmado antes do evento precisa estar na redação (ver
        # `load_write_identities`).
        known = load_write_identities(conn) if identities is None else identities
        payload = redact_event_data(etype, payload, known, secrets=secrets)
    row = conn.execute(
        "INSERT INTO events (type, occurred_at, actor_token_id, data) VALUES (%s, %s, %s, %s) "
        "RETURNING seq",
        (etype.value, occurred_at, actor_token_id, Jsonb(payload)),
    ).fetchone()
    assert row is not None
    return int(row[0])


def resolve_person(conn: psycopg.Connection, ref: str) -> str | None:
    """O pseudônimo canônico da pessoa de `ref` (pseudônimo ou apelido), ou `None` se não existe
    no cadastro.

    O formato decide a coluna (o apelido sempre tem uma letra de `a` a `f` e nunca hífen; o pseudônimo é
    o UUID v4 canônico); fora dos dois formatos, `None` sem consulta. Roda na transação do chamador.
    """
    # Importação local: `people` importa este módulo (o gravador único do log).
    from cq_server.people import is_alias, is_pseudo_id

    if is_pseudo_id(ref):
        row = conn.execute("SELECT pseudo_id FROM people WHERE pseudo_id = %s", (uuid.UUID(ref),)).fetchone()
    elif is_alias(ref):
        row = conn.execute("SELECT pseudo_id FROM people WHERE alias = %s", (ref,)).fetchone()
    else:
        return None
    return None if row is None else str(row[0])


def _person_condition(person_types: Sequence[EventType], pseudo_id: str) -> tuple[str, list[Any]]:
    """A condição do filtro por pessoa: uma por grupo de tipos e campo, unidas por `OR`;
    `data->>'campo' = %s` nos escalares e `data->'campo' ? %s` nas listas. Os nomes dos campos vêm da
    lista fechada `PERSON_FIELDS` (nunca do pedido) e vão como parâmetros. Devolve o SQL e os parâmetros."""
    groups: dict[PersonField, list[str]] = {}
    for etype in person_types:
        for person_field in PERSON_FIELDS[etype]:
            groups.setdefault(person_field, []).append(etype.value)
    parts: list[str] = []
    params: list[Any] = []
    for person_field, type_values in groups.items():
        if person_field.is_list:
            parts.append("(type = ANY(%s) AND data->(%s::text) ? %s::text)")
        else:
            parts.append("(type = ANY(%s) AND data->>(%s::text) = %s::text)")
        params += [type_values, person_field.name, pseudo_id]
    return " OR ".join(parts), params


def query_events(conn: psycopg.Connection, types: Sequence[str], *, after: int = 0,
                 limit: int = DEFAULT_LIMIT, secrets: Iterable[str | None] = (),
                 identities: Sequence[KnownIdentity] | None = None,
                 person: str | None = None) -> EventPage:
    """Eventos dos `types` com `seq > after`, em ordem de `seq`, no máximo `limit`.

    A validação da entrada (tipos conhecidos e sem repetição, `after >= 0`, `1 <= limit <= 500`) é
    da camada que recebe o pedido; aqui só se exige que os tipos existam (`ValueError`).

    `person`: filtra pelos campos de pessoa de `PERSON_FIELDS`; com ele,
    `types` pode ser vazio. Com `person=None`, o comportamento é o da consulta sem filtro de pessoa.
    """
    requested = [EventType(t) for t in types]
    if person is None:
        rows = conn.execute(
            "SELECT seq, type, occurred_at, actor_token_id, data FROM events "
            "WHERE type = ANY(%s) AND seq > %s ORDER BY seq LIMIT %s",
            ([t.value for t in requested], after, limit),
        ).fetchall()
    else:
        pseudo_id = resolve_person(conn, person)
        # Com `types`, a interseção com os tipos que têm campo de pessoa; sem, todos eles.
        wanted = set(requested)
        person_types = [t for t in PERSON_FIELDS if not wanted or t in wanted]
        if pseudo_id is None or not person_types:
            # Sem pessoa (ou sem tipo que a cite): a página vazia, sem consultar `events`.
            return EventPage(items=[], next_cursor=after)
        condition, params = _person_condition(person_types, pseudo_id)
        rows = conn.execute(
            "SELECT seq, type, occurred_at, actor_token_id, data FROM events "
            f"WHERE ({condition}) AND seq > %s ORDER BY seq LIMIT %s",
            (*params, after, limit),
        ).fetchall()
    known: Sequence[KnownIdentity] | None = identities
    secret_list = list(secrets)
    items: list[StoredEvent] = []
    for seq, etype, occurred, actor, data in rows:
        if etype in FREE_TEXT_FIELDS:
            if known is None:
                known = load_known_identities(conn)
            data = redact_event_data(etype, data, known, secrets=secret_list)
        items.append(StoredEvent(seq=int(seq), type=etype, occurred_at=occurred.astimezone(timezone.utc),
                                 actor_token_id=actor, data=data))
    next_cursor = items[-1].seq if items else after
    return EventPage(items=items, next_cursor=next_cursor)
