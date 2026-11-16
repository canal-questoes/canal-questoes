"""Equipe do oferecimento: link de equipe, matrícula, convite e endereço de entrega.


Núcleo sem HTTP. O link de equipe é credencial ao portador: o código do convite
(`secrets.token_urlsafe(16)`, 128 bits) só é guardado pelo resumo (SHA-256) e nunca sai em log nem
em evento; só a resposta de `create_link` o contém.

- `create_link` (a operação `team.link`): cria a matrícula `team` `pending` (ou reusa a
  existente), revoga o convite pendente dela (`team.link_revoked {replaced}`) e cria o convite novo
  (`team.link_created`), numa transação.
- `process_start` (chamado pelo tratador `telegram.update` num `/start <código>` do chat privado,
  com o comando deste bot): aplica as regras do `/start` de equipe na transação do chamador e devolve o
  `outcome`; as respostas saem pela fila de saída (`Outbox.enqueue`), na mesma transação.
- `chat_endpoint`, `person_endpoint`, `reactivate_endpoint` e `link_endpoint`: o endereço de entrega
  vigente e a troca dele, como no `/start` de equipe; os fluxos do aluno os reusam no `/aceito`
  (`cq_server.students`, que pode importar este módulo; este nunca importa aquele).
- `revoke_exposed_codes` (para o mesmo tratador, no primeiro caso de `reception._apply`): revoga os convites
  pendentes do oferecimento cujo código aparece num texto que não será tratado (grupo, outro bot),
  para que nenhum código utilizável fique no texto gravado; também os de
  aluno, com o evento de aluno (`student.invite_revoked {refused}`).

Ordem das travas: `offerings → bots → enrollments → invites → delivery_endpoints`,
e o lock consultivo dos eventos por último. O tratador `telegram.update` trava antes a própria linha
de `telegram_updates` (a exceção da ordem). Por causa do expurgo, `create_link` toma o
lock dos eventos antes do `INSERT` de uma matrícula nova (sem linha de matrícula a travar, a ordem não se
inverte); com a matrícula existente, a ordem é a geral, com o lock dos eventos por último.
"""

from __future__ import annotations

import re
import secrets as _secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from cq_server import botmessages, people
from cq_server.bots import offering_text
from cq_server.events import EVENTS_LOCK_KEY, EventType, append_event, load_write_identities
from cq_server.offerings import (
    BotRequired,
    InvalidState,
    append_revocation_event,
    code_digest,
    lock_offering,
)

# Validade do link, em horas.
MIN_VALID_HOURS = 1
MAX_VALID_HOURS = 720
DEFAULT_VALID_HOURS = 168

LINK_PREFIX = "https://t.me/"
TELEGRAM_CHANNEL = "telegram"

# Os `outcome` de `process_start`.
TEAM_JOINED = "team_joined"
TEAM_ENDPOINT_REPLACED = "team_endpoint_replaced"
TEAM_ALREADY_LINKED = "team_already_linked"
TEAM_REFUSED_INVALID = "team_refused_invalid"
TEAM_REFUSED_OTHER_PERSON = "team_refused_other_person"

# Um código de convite tem 22 caracteres base64url; num texto, qualquer trecho assim é candidato.
CODE_LENGTH = 22
_CODE_RUN = re.compile(r"[A-Za-z0-9_-]{22,}")

# Sorteios de código por convite antes de desistir (a colisão de 128 bits é só teórica).
MAX_CODE_DRAWS = 8


def generate_invite_code() -> str:
    """Sorteia um código de convite (`secrets.token_urlsafe(16)`, 22 caracteres)."""
    return _secrets.token_urlsafe(16)


def validate_valid_hours(value: object) -> int:
    """Inteiro de 1 a 720 (booleano não é inteiro): `ValueError("invalid_format")` ou
    `ValueError("out_of_range")`, sem o valor."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_format")
    if not MIN_VALID_HOURS <= value <= MAX_VALID_HOURS:
        raise ValueError("out_of_range")
    return value


# --------------------------------------------------------------------------------------------------
# Registros e resultados
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamLink:
    """O link criado (credencial ao portador) e o contexto da resposta de `team.link`."""

    link: str
    expires_at: datetime
    pseudo_id: uuid.UUID
    alias: str
    offering_id: uuid.UUID
    code: str
    timezone: str


@dataclass(frozen=True)
class PersonNotFound:
    """A pessoa do pedido não existe (404, depois do oferecimento)."""


@dataclass(frozen=True)
class RoleConflict:
    """A pessoa já é aluna do oferecimento e não pode ser da equipe (409 `role_conflict`,
    depois de `bot_required`)."""


# --------------------------------------------------------------------------------------------------
# Gerar o link
# --------------------------------------------------------------------------------------------------


def _find_person(conn: psycopg.Connection, ref: str) -> tuple[int, uuid.UUID, str] | None:
    if people.is_pseudo_id(ref):
        row = conn.execute("SELECT id, pseudo_id, alias FROM people WHERE pseudo_id = %s",
                           (uuid.UUID(ref),)).fetchone()
    elif people.is_alias(ref):
        row = conn.execute("SELECT id, pseudo_id, alias FROM people WHERE alias = %s", (ref,)).fetchone()
    else:
        raise ValueError("pseudônimo fora do formato")
    return None if row is None else (int(row[0]), row[1], row[2])


def _team_enrollment(conn: psycopg.Connection, person_id: int, offering_row: int,
                     now: datetime) -> int | None:
    """A matrícula da pessoa no oferecimento, travada; criada `team` `pending` se não existir.
    `None` se a pessoa já é aluna do oferecimento (conflito de papel, sem nada gravado)."""
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    row = conn.execute("SELECT id, role FROM enrollments WHERE person_id = %s AND offering_id = %s "
                       "FOR NO KEY UPDATE", (person_id, offering_row)).fetchone()
    if row is None:
        # Expurgo: o lock consultivo dos eventos antes do `INSERT` da matrícula nova,
        # como `students.import_roster`; assim toda criação de matrícula serializa com a recontagem do
        # expurgo, e o lock fica de pé até o COMMIT. Só neste caminho: com a
        # matrícula existente, tomá-lo antes da trava dela inverteria a ordem dos tratadores
        # `telegram.update` (`process_start`, `revoke_exposed_codes`, `students.start_student`, que
        # travam matrícula e convite e só depois o lock dos eventos) e abriria um impasse. Aqui não há
        # linha a travar, e nenhum outro caminho cria a matrícula (todos os criadores seguram a trava do
        # oferecimento).
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK_KEY,))
        inserted = conn.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at) "
            "VALUES (%s, %s, 'team', 'pending', %s) ON CONFLICT DO NOTHING RETURNING id",
            (person_id, offering_row, now),
        ).fetchone()
        if inserted is not None:
            return int(inserted[0])
        # Corrida: outra transação criou a matrícula entre a consulta e o INSERT.
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        row = conn.execute("SELECT id, role FROM enrollments WHERE person_id = %s AND offering_id = %s "
                           "FOR NO KEY UPDATE", (person_id, offering_row)).fetchone()
        assert row is not None
    return None if row[1] == "student" else int(row[0])


def create_link(conn: psycopg.Connection, ref: str, *, person: str, valid_hours: int = DEFAULT_VALID_HOURS,
                now: datetime, actor_token_id: str | None,
                ) -> TeamLink | PersonNotFound | InvalidState | BotRequired | RoleConflict | None:
    """Gera o link de equipe de `person` (pseudônimo) no oferecimento `ref`, nesta ordem
    de checagem: oferecimento (`None`) → pessoa (`PersonNotFound`) → arquivado
    (`InvalidState`) → sem bot (`BotRequired`; um bot `no_access` é aceito) → pessoa já aluna do
    oferecimento, em qualquer estado da matrícula (`RoleConflict`) → gravação, numa
    transação: matrícula `team` `pending` (ou a existente), o convite pendente dela revogado
    (`team.link_revoked {replaced}`), o convite novo e `team.link_created`. As recusas não gravam
    nada."""
    validate_valid_hours(valid_hours)
    if not people.is_pseudonym(person):
        raise ValueError("pseudônimo fora do formato")
    with conn.transaction():
        offering = lock_offering(conn, ref)
        if offering is None:
            return None
        found = _find_person(conn, person)
        if found is None:
            return PersonNotFound()
        if offering.state == "archived":
            return InvalidState("archived")
        bot = conn.execute("SELECT username FROM bots WHERE offering_id = %s", (offering.id,)).fetchone()
        if bot is None:
            return BotRequired()
        person_id, pseudo_id, alias = found
        enrollment_id = _team_enrollment(conn, person_id, offering.id, now)
        if enrollment_id is None:
            return RoleConflict()
        previous = conn.execute(
            "SELECT id FROM invites WHERE enrollment_id = %s AND state = 'pending' FOR UPDATE",
            (enrollment_id,),
        ).fetchone()

        expires_at = now + timedelta(hours=valid_hours)
        if previous is not None:
            conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = %s",
                         (now, previous[0]))
            append_event(conn, EventType.TEAM_LINK_REVOKED,
                         {"offering_id": offering.offering_id, "person": pseudo_id,
                          "revoke_reason": "replaced"},
                         occurred_at=now, actor_token_id=actor_token_id)
        for _ in range(MAX_CODE_DRAWS):
            # Nome global resolvido a cada sorteio (os testes podem substituir o gerador).
            invite_code = generate_invite_code()
            inserted = conn.execute(
                "INSERT INTO invites (enrollment_id, code_sha256, state, expires_at, created_at) "
                "VALUES (%s, %s, 'pending', %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (enrollment_id, code_digest(invite_code), expires_at, now),
            ).fetchone()
            if inserted is not None:
                break
        else:  # pragma: no cover - só com um gerador defeituoso
            raise RuntimeError("não foi possível sortear um código de convite livre")
        append_event(conn, EventType.TEAM_LINK_CREATED,
                     {"offering_id": offering.offering_id, "person": pseudo_id, "expires_at": expires_at},
                     occurred_at=now, actor_token_id=actor_token_id)
    return TeamLink(link=f"{LINK_PREFIX}{bot[0]}?start={invite_code}", expires_at=expires_at,
                    pseudo_id=pseudo_id, alias=alias, offering_id=offering.offering_id,
                    code=offering.code, timezone=offering.timezone)


# --------------------------------------------------------------------------------------------------
# Usar o link
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """Um endereço de entrega vigente (`active` ou `blocked`) no Telegram."""

    id: int
    person_id: int
    # Anulável no banco; só o expurgo grava `NULL`, num oferecimento
    # arquivado cujo bot não é mais consultado.
    chat_id: int | None
    state: str


def chat_endpoint(conn: psycopg.Connection, *, bot_id: int, chat_id: int) -> Endpoint | None:
    """O endereço vigente do chat `chat_id` no bot (de qualquer pessoa), travado."""
    return _current_endpoint(conn, "bot_id = %s AND chat_id = %s", (bot_id, chat_id))


def person_endpoint(conn: psycopg.Connection, *, bot_id: int, person_id: int) -> Endpoint | None:
    """O endereço vigente da pessoa no bot, travado."""
    return _current_endpoint(conn, "bot_id = %s AND person_id = %s", (bot_id, person_id))


def _current_endpoint(conn: psycopg.Connection, where: str, params: tuple[Any, ...]) -> Endpoint | None:
    """O endereço vigente (`active` ou `blocked`) que casa `where`, travado."""
    # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs das tabelas que
    # referenciam o endereço.
    row = conn.execute(
        "SELECT id, person_id, chat_id, state FROM delivery_endpoints "
        f"WHERE {where} AND channel = 'telegram' AND state IN ('active', 'blocked') FOR NO KEY UPDATE",
        params,
    ).fetchone()
    if row is None:
        return None
    return Endpoint(int(row[0]), int(row[1]), None if row[2] is None else int(row[2]), row[3])


def reactivate_endpoint(conn: psycopg.Connection, endpoint: Endpoint, now: datetime) -> None:
    """`blocked → active` (o `/start` do próprio chat prova o desbloqueio), sem evento."""
    if endpoint.state == "blocked":
        conn.execute("UPDATE delivery_endpoints SET state = 'active', changed_at = %s WHERE id = %s",
                     (now, endpoint.id))


def link_endpoint(conn: psycopg.Connection, *, person_id: int, bot_id: int, chat_id: int,
                  current: Endpoint | None, now: datetime) -> bool:
    """O endereço da pessoa no bot passa a ser o chat `chat_id` (o uso do link): se o
    endereço vigente `current` já é desse chat, só a reativação (`blocked → active`); senão o antigo, se
    houver, fica `inactive`, e entra um novo `active`. Devolve se o endereço foi trocado (havia outro)."""
    if current is not None and current.chat_id == chat_id:
        reactivate_endpoint(conn, current, now)
        return False
    if current is not None:
        conn.execute("UPDATE delivery_endpoints SET state = 'inactive', changed_at = %s WHERE id = %s",
                     (now, current.id))
    conn.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
        "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
        (person_id, bot_id, chat_id, now, now),
    )
    return current is not None


def process_start(conn: psycopg.Connection, *, bot_id: int, update_row_id: int, sender_chat_id: int,
                  code: str, now: datetime, outbox: Any) -> str:
    """Aplica um `/start <código>` do chat privado `sender_chat_id` ao bot `bot_id`
    e devolve o `outcome`: `team_joined`, `team_endpoint_replaced`, `team_already_linked`,
    `team_refused_invalid` ou `team_refused_other_person`.

    Roda na transação do chamador (o tratador `telegram.update`, que já travou a linha do update
    `update_row_id` e grava o `outcome`), com as mudanças, os eventos e as respostas juntos: se algo
    falha, nada vale. As respostas saem por `outbox.enqueue`: `WELCOME_TEAM` e
    `ALREADY_LINKED` ao endereço `active` da pessoa (pessoa + bot); `LINK_INVALID` e `OTHER_PERSON`
    ao chat do update (`reply_to_update_id`). O vencimento é avaliado por `now`
    (`agora < expires_at`)."""
    with conn.transaction():
        bot = conn.execute(
            "SELECT o.id, o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id "
            "WHERE b.id = %s FOR NO KEY UPDATE OF b",
            (bot_id,),
        ).fetchone()
        if bot is None:
            raise LookupError("bot inexistente")
        offering_row, offering_id = int(bot[0]), bot[1]

        def refuse_invalid() -> str:
            outbox.enqueue(conn, text=botmessages.LINK_INVALID, bot_id=bot_id,
                           reply_to_update_id=update_row_id)
            append_event(conn, EventType.TEAM_JOIN_REFUSED,
                         {"offering_id": offering_id, "refusal": "invalid"},
                         occurred_at=now, actor_token_id=None)
            return TEAM_REFUSED_INVALID

        found = conn.execute(
            "SELECT i.id, e.id, e.person_id, e.offering_id, p.pseudo_id FROM invites i "
            "JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id "
            "WHERE i.code_sha256 = %s",
            (code_digest(code),),
        ).fetchone()
        if found is None or int(found[3]) != offering_row:
            return refuse_invalid()
        invite_id, enrollment_id, person_id = int(found[0]), int(found[1]), int(found[2])
        pseudo_id = found[4]
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        conn.execute("SELECT 1 FROM enrollments WHERE id = %s FOR NO KEY UPDATE", (enrollment_id,))
        invite = conn.execute("SELECT state, expires_at FROM invites WHERE id = %s FOR UPDATE",
                              (invite_id,)).fetchone()
        assert invite is not None
        invite_state, expires_at = invite
        in_chat = chat_endpoint(conn, bot_id=bot_id, chat_id=sender_chat_id)
        own_endpoint = person_endpoint(conn, bot_id=bot_id, person_id=person_id)

        if invite_state == "revoked":
            return refuse_invalid()
        if invite_state == "used":
            # Um convite usado nunca é tratado como vencido: só a própria conta vinculada o reusa.
            if in_chat is None or in_chat.person_id != person_id:
                return refuse_invalid()
            reactivate_endpoint(conn, in_chat, now)
            outbox.enqueue(conn, recipient=pseudo_id, text=botmessages.ALREADY_LINKED, bot_id=bot_id)
            return TEAM_ALREADY_LINKED
        if not now < expires_at:
            return refuse_invalid()

        if in_chat is not None and in_chat.person_id != person_id:
            conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = %s",
                         (now, invite_id))
            outbox.enqueue(conn, text=botmessages.OTHER_PERSON, bot_id=bot_id,
                           reply_to_update_id=update_row_id)
            append_event(conn, EventType.TEAM_LINK_REVOKED,
                         {"offering_id": offering_id, "person": pseudo_id, "revoke_reason": "refused"},
                         occurred_at=now, actor_token_id=None)
            append_event(conn, EventType.TEAM_JOIN_REFUSED,
                         {"offering_id": offering_id, "refusal": "other_person"},
                         occurred_at=now, actor_token_id=None)
            return TEAM_REFUSED_OTHER_PERSON

        replaced = link_endpoint(conn, person_id=person_id, bot_id=bot_id, chat_id=sender_chat_id,
                                 current=own_endpoint, now=now)
        conn.execute("UPDATE invites SET state = 'used', used_at = %s WHERE id = %s", (now, invite_id))
        conn.execute("UPDATE enrollments SET state = 'active', activated_at = %s "
                     "WHERE id = %s AND state = 'pending'", (now, enrollment_id))
        # O lock dos eventos vem depois de todas as travas de linha; a disciplina e o período da
        # boas-vindas são redigidos com as identidades lidas depois dele.
        identities = load_write_identities(conn)
        welcome = offering_text(conn, "WELCOME_TEAM", bot_id=bot_id, identities=identities)
        outbox.enqueue(conn, recipient=pseudo_id, text=welcome, bot_id=bot_id)
        append_event(conn, EventType.TEAM_JOINED,
                     {"offering_id": offering_id, "person": pseudo_id, "endpoint_replaced": replaced},
                     occurred_at=now, actor_token_id=None, identities=identities)
        return TEAM_ENDPOINT_REPLACED if replaced else TEAM_JOINED


def _candidate_codes(text: str) -> set[str]:
    """Todo trecho de 22 caracteres do alfabeto base64url do texto (candidatos a código)."""
    found: set[str] = set()
    for run in _CODE_RUN.finditer(text):
        chunk = run.group()
        for start in range(len(chunk) - CODE_LENGTH + 1):
            found.add(chunk[start:start + CODE_LENGTH])
    return found


def revoke_exposed_codes(conn: psycopg.Connection, *, bot_id: int, text: str | None,
                         now: datetime) -> int:
    """Revoga os convites `pending` do oferecimento do bot cujo código aparece em `text` (o primeiro
    caso de `reception._apply`), com um evento por convite, em ordem de `invites.id`, separado pelo papel
    da matrícula: `team.link_revoked {refused}` para os de equipe e
    `student.invite_revoked {refused}` (com a `origin` do envio que criou o convite) para os de aluno;
    na transação do chamador; devolve quantos foram revogados."""
    if not text:
        return 0
    digests = sorted({code_digest(candidate) for candidate in _candidate_codes(text)})
    if not digests:
        return 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT i.id, o.offering_id, p.pseudo_id, e.role, s.origin FROM invites i "
            "JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id "
            "JOIN offerings o ON o.id = e.offering_id JOIN bots b ON b.offering_id = o.id "
            "LEFT JOIN invite_sends s ON s.id = i.invite_send_id "
            "WHERE b.id = %s AND i.state = 'pending' AND i.code_sha256 = ANY(%s) "
            "ORDER BY i.id FOR UPDATE OF i",
            (bot_id, digests),
        ).fetchall()
        if not rows:
            return 0
        conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = ANY(%s)",
                     (now, [int(row[0]) for row in rows]))
        for _, offering_id, pseudo_id, role, origin in rows:
            append_revocation_event(conn, offering_id=offering_id, person=pseudo_id, role=role,
                                    origin=origin, reason="refused", now=now, actor_token_id=None)
    return len(rows)
