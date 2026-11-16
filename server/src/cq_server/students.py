"""Alunos do oferecimento: importação da lista, adesão, envio dos convites em duas fases, reenvio, a
tarefa `invite.email` e os fluxos do aluno no bot (da importação ao vínculo do aluno com o bot, pelo
convite por e-mail ou pelo autosserviço).

Núcleo sem HTTP e sem SMTP: o e-mail sai pela porta `ports.verification`, e o adaptador SMTP fica em
`cq_server.adapters.smtp` (o núcleo não o importa). Pode importar `team`; nunca importa `reception` nem `worker`
(a `reception` despacha para cá, e `team` nunca importa este módulo).

- `check_rows` valida os itens da lista (forma, campos, repetição) e `import_roster` grava a
  importação numa transação, embaralhando as linhas com `shuffle` (os `person.registered` saem na ordem
  embaralhada). `shuffle` é `secrets.SystemRandom().shuffle`, substituível nos testes.
- `adhesion` conta a adesão por estado.
- `prepare_invites` e `confirm_invites` são as duas fases do envio em lote;
  `resend_invites` é o reenvio pela coordenação.
- `invite_email_task_handler` monta o tratador da tarefa `invite.email`, para o
  `run_worker`; a conexão ao banco abre na primeira chamada, nunca na construção.
- `invite_request_mask` é a máscara do texto do `/convite <e-mail>`, e
  `invite_request_argument`, o argumento dele.
- `start_student`, `accept_terms`, `refuse_terms` e `request_invite` são os fluxos do aluno no bot
  (`/start <código>`, `/aceito`, `/recuso` e `/convite`), chamados pelo
  tratador `telegram.update` da `reception` na transação dele; `student_command` reconhece os
  comandos do aluno, e os desfechos (`STUDENT_*`, `INVITE_REQUESTED`) ficam aqui.

`InvalidState`, `BotRequired` e `InvalidConfirmation` são os tipos de resultado dos oferecimentos (`offerings`),
reaproveitados; `None` quer dizer "oferecimento inexistente". Todo horário vem do `Clock` do chamador
(`now`), em UTC.

As funções que mudam o banco abrem **uma** transação (ou um SAVEPOINT, se o chamador já tiver uma) com
a mudança e os eventos dela juntos. Ordem das travas: `offerings` → `bots`
→ `enrollments` → `invite_sends` → `invites`, com o lock consultivo dos eventos por último, exceto na
importação, que o toma logo depois de travar o oferecimento (como o cadastro de pessoas). Nenhuma ordenação
por horário: a ordem é sempre a de `id`.

**Identidade**: a importação só procura a pessoa por `identities.ra` e
`identities.email_normalized`, por parâmetro, como o cadastro de pessoas, e não devolve nem grava fora de
`identities` o que compara; as respostas só têm contagens (e, no reenvio, os pseudônimos que o pedido
já trazia).

**Tarefa `invite.email`**: a T1 (numa transação) gera o código e
monta o e-mail, o envio sai pela porta **fora** de transação, e a T2 (noutra transação) registra o
resultado. Registros `invite.email` e `invite.email_superseded` no logger `cq_server.worker`, só com o id
do envio e o resultado, depois do COMMIT.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets as _secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from cq_server import botmessages, people, tasks, team, timezones
from cq_server._leasing import LazyConnection
from cq_server.bots import offering_text
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.events import (
    EVENTS_LOCK_KEY,
    EventType,
    append_event,
    load_known_identities,
    load_write_identities,
)
from cq_server.offerings import (
    BotRequired,
    InvalidConfirmation,
    InvalidState,
    append_revocation_event,
    code_digest,
    is_code,
    is_offering_id,
    lock_offering,
)
from cq_server.ports.verification import (
    Delivered,
    PermanentFailure,
    TemporaryFailure,
    VerificationChannel,
    VerificationMessage,
)
from cq_server.redaction import redact
from cq_server.tasks import ClaimedTask, PermanentTaskError, RetryTaskLater

# Payload `{"invite_send_id": <id>}`, chave `invite.email:<id>`.
INVITE_EMAIL_TASK_TYPE = "invite.email"

# Embaralha as linhas da importação no lugar; os testes podem substituí-lo.
shuffle = _secrets.SystemRandom().shuffle

# Os campos de cada item da lista, nesta ordem de precedência.
ROW_FIELDS = ("name", "ra", "email")

# Validade do código de confirmação do envio em lote (como no arquivamento).
INVITE_CODE_TTL = timedelta(minutes=10)

# Origens de um envio.
ORIGIN_BATCH = "batch"
ORIGIN_COORDINATION = "coordination"
ORIGIN_SELF_SERVICE = "self_service"

# Os estados da adesão, na ordem de avaliação (o primeiro verdadeiro vale), e os que
# tornam o aluno elegível ao envio em lote.
ADHESION_STATES = ("linked", "sending", "terms_presented", "invite_valid", "send_failed", "invite_lapsed",
                   "no_invite")
ELIGIBLE_STATES = ("no_invite", "invite_lapsed", "send_failed")


# --------------------------------------------------------------------------------------------------
# Registros e resultados
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterRow:
    """Uma linha da lista, já normalizada pelas funções do cadastro de pessoas (nome, RA e e-mail)."""

    name: str
    ra: str
    email: str


@dataclass(frozen=True)
class ImportResult:
    """Importação aceita: as contagens da resposta de `students.import`."""

    offering_id: uuid.UUID
    code: str
    received: int
    people_created: int
    people_reused: int
    enrollments_created: int
    already_enrolled: int


@dataclass(frozen=True)
class ListConflict:
    """Importação recusada (409 `list_conflict`): `(índice da linha, motivo)`, em ordem de índice."""

    fields: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class Adhesion:
    """A adesão por estado; `enrolled` é a soma dos outros sete."""

    offering_id: uuid.UUID
    code: str
    enrolled: int
    no_invite: int
    sending: int
    invite_valid: int
    terms_presented: int
    invite_lapsed: int
    send_failed: int
    linked: int


@dataclass(frozen=True)
class InvitePreparation:
    """Preparo do envio em lote; o código de confirmação não é segredo."""

    offering_id: uuid.UUID
    code: str
    eligible: int
    valid_hours: int
    confirmation_code: str
    expires_at: datetime


@dataclass(frozen=True)
class InvitesQueued:
    """Confirmação do envio em lote: quantos envios foram enfileirados."""

    offering_id: uuid.UUID
    code: str
    queued: int
    valid_hours: int


@dataclass(frozen=True)
class ResendResult:
    """Reenvio pela coordenação: `not_students` como `(pseudo_id, apelido)`, na ordem
    da primeira referência; `not_found_count` conta os itens distintos sem pessoa (nunca ecoados)."""

    offering_id: uuid.UUID
    code: str
    queued: int
    valid_hours: int
    not_students: tuple[tuple[uuid.UUID, str], ...]
    not_found_count: int


# --------------------------------------------------------------------------------------------------
# Apoio comum
# --------------------------------------------------------------------------------------------------

# A chave da tarefa de um envio `s`, como expressão SQL: `invite.email:<id>`, que o
# índice único de `tasks.idempotency_key` acha.
_TASK_KEY_SQL = f"'{INVITE_EMAIL_TASK_TYPE}:' || s.id"

# O estado da adesão de uma matrícula de aluno `e`, como expressão SQL: o
# primeiro estado verdadeiro, na ordem de `ADHESION_STATES`, com `%(now)s` = o horário do chamador. O convite só
# vale (`terms_presented`, `invite_valid`) se o envio que o criou está `sent`; `send_failed` olha o
# último envio (maior `id`). O estado do oferecimento é ignorado. O mesmo predicado dá a
# elegibilidade do envio em lote, para a adesão, o preparo e a confirmação.
_ADHESION_STATE_SQL = f"""
    CASE
      WHEN e.state = 'active' THEN 'linked'
      WHEN EXISTS (
          SELECT 1 FROM invite_sends s JOIN tasks t ON t.idempotency_key = {_TASK_KEY_SQL}
           WHERE s.enrollment_id = e.id AND s.state = 'pending' AND t.state IN ('pending', 'leased'))
        THEN 'sending'
      WHEN EXISTS (
          SELECT 1 FROM invites i JOIN invite_sends s ON s.id = i.invite_send_id
           WHERE i.enrollment_id = e.id AND i.state = 'pending' AND %(now)s < i.expires_at
             AND s.state = 'sent' AND i.presented_update_id IS NOT NULL)
        THEN 'terms_presented'
      WHEN EXISTS (
          SELECT 1 FROM invites i JOIN invite_sends s ON s.id = i.invite_send_id
           WHERE i.enrollment_id = e.id AND i.state = 'pending' AND %(now)s < i.expires_at
             AND s.state = 'sent' AND i.presented_update_id IS NULL)
        THEN 'invite_valid'
      WHEN coalesce((
          SELECT s.state = 'failed'
                 OR (s.state = 'pending' AND EXISTS (
                     SELECT 1 FROM tasks t WHERE t.idempotency_key = {_TASK_KEY_SQL} AND t.state = 'failed'))
            FROM invite_sends s WHERE s.enrollment_id = e.id ORDER BY s.id DESC LIMIT 1), false)
        THEN 'send_failed'
      WHEN EXISTS (SELECT 1 FROM invites i WHERE i.enrollment_id = e.id) THEN 'invite_lapsed'
      ELSE 'no_invite'
    END"""

# As matrículas de aluno elegíveis do oferecimento `%(o)s`: pendentes e num dos
# estados de `ELIGIBLE_STATES`.
_ELIGIBLE_WHERE_SQL = (
    "e.offering_id = %(o)s AND e.role = 'student' AND e.state = 'pending' AND ("
    + _ADHESION_STATE_SQL + ") IN (" + ", ".join(f"'{state}'" for state in ELIGIBLE_STATES) + ")")

# Limpa as quatro colunas do preparo do envio (importação aceita e confirmação).
_CLEAR_PREPARATION_SQL = (
    "UPDATE offerings SET invite_code_sha256 = NULL, invite_code_expires_at = NULL, "
    "invite_valid_hours = NULL, invite_eligible_count = NULL WHERE id = %s")


def _ref_condition(ref: str) -> tuple[str, Any]:
    """A condição SQL (sobre `o`, com o parâmetro nomeado `ref`) e o valor da referência do
    oferecimento; `ValueError` fora do formato."""
    if is_offering_id(ref):
        return "o.offering_id = %(ref)s", uuid.UUID(ref)
    if is_code(ref):
        return "o.code = %(ref)s", ref
    raise ValueError("referência de oferecimento fora do formato")


def _bot_ready(conn: psycopg.Connection, offering_row: int) -> bool:
    """O oferecimento tem bot, e o bot não perdeu o acesso (`no_access` → `BotRequired`)."""
    row = conn.execute("SELECT state FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()
    return row is not None and row[0] != "no_access"


def _queue_send(conn: psycopg.Connection, enrollment_id: int, *, origin: str, valid_hours: int,
                now: datetime, batch_id: int | None = None) -> int:
    """Cria um envio `pending` da matrícula e a tarefa `invite.email` dele (payload só com o id, chave
    `invite.email:<id>`), na transação de `conn`; devolve o id do envio.

    Lotes: `batch_id`, o lote (`invite_batches.id`) do envio `batch` criado
    por `confirm_invites`; nulo nas demais origens (o CHECK `invite_sends_batch_check`). O
    argumento tem padrão, e os chamadores das outras origens o omitem."""
    row = conn.execute(
        "INSERT INTO invite_sends (enrollment_id, origin, valid_hours, state, created_at, batch_id) "
        "VALUES (%s, %s, %s, 'pending', %s, %s) RETURNING id",
        (enrollment_id, origin, valid_hours, now, batch_id),
    ).fetchone()
    assert row is not None
    send_id = int(row[0])
    tasks.enqueue_in_transaction(conn, INVITE_EMAIL_TASK_TYPE, {"invite_send_id": send_id}, now=now,
                                 idempotency_key=f"{INVITE_EMAIL_TASK_TYPE}:{send_id}")
    return send_id


# --------------------------------------------------------------------------------------------------
# Importação e adesão
# --------------------------------------------------------------------------------------------------


def _row_field(item: Mapping[str, Any], name: str, rule: Callable[[str], Any]) -> tuple[Any, str | None]:
    """O valor de um campo obrigatório do item, já pela regra do cadastro, e o motivo da recusa (tipo →
    `missing` → tamanho → formato, a mesma precedência do cadastro); o motivo nunca traz o valor."""
    value = item.get(name)
    if value is None:
        return None, "missing"
    if not isinstance(value, str):
        return None, "invalid_format"
    try:
        return rule(value), None
    except people.InvalidIdentity as exc:
        return None, exc.reason


def check_rows(items: Sequence[object]) -> tuple[list[RosterRow], list[tuple[int, str]]]:
    """Valida os itens da lista: as linhas normalizadas e `[]`, ou
    `[]` e a lista `(índice, motivo)` em ordem de índice, com **um** motivo por item.

    Precedência em cada item: não é objeto → `invalid_format`; `name`, `ra` e `email`, nesta ordem,
    com as regras do cadastro (os três obrigatórios); chave desconhecida → `unknown_field`; RA ou e-mail
    normalizado (`casefold`) igual ao de um item anterior → `duplicate`. A repetição compara com
    **todos** os itens anteriores cujo campo passou nas regras do cadastro, qualquer que seja a validade dos
    outros campos desses itens. O limite de 1 a 1.000 itens é da rota. Nenhum motivo traz o
    valor recebido."""
    rows: list[RosterRow] = []
    errors: list[tuple[int, str]] = []
    seen_ras: set[str] = set()
    seen_emails: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append((index, "invalid_format"))
            continue
        name, name_reason = _row_field(item, "name", people.normalize_full_name)
        ra, ra_reason = _row_field(item, "ra", people.validate_ra)
        email, email_reason = _row_field(item, "email", people.validate_email)
        reason = name_reason or ra_reason or email_reason
        if reason is None and any(key not in ROW_FIELDS for key in item):
            reason = "unknown_field"
        if reason is None and (ra in seen_ras or email.normalized in seen_emails):
            reason = "duplicate"
        # O RA e o e-mail que passaram nas regras do cadastro contam para os itens seguintes, mesmo
        # que outro campo deste item seja inválido ou que o item seja ele mesmo uma repetição.
        if ra is not None:
            seen_ras.add(ra)
        if email is not None:
            seen_emails.add(email.normalized)
        if reason is not None:
            errors.append((index, reason))
        else:
            rows.append(RosterRow(name=name, ra=ra, email=email.address))
    return ([], errors) if errors else (rows, [])


def _registered_people(conn: psycopg.Connection, offering_row: int, rows: Sequence[RosterRow],
                       ) -> tuple[dict[str, int], dict[str, int], dict[int, str]]:
    """As pessoas cadastradas com um RA ou um e-mail normalizado da lista (só para comparar, por
    parâmetro): `{ra: person_id}`, `{email_normalized: person_id}` e o
    papel da matrícula de cada uma no oferecimento (`{person_id: role}`, só as matriculadas)."""
    found = conn.execute(
        "SELECT i.person_id, i.ra, i.email_normalized, e.role FROM identities i "
        "LEFT JOIN enrollments e ON e.person_id = i.person_id AND e.offering_id = %s "
        "WHERE i.ra = ANY(%s::text[]) OR i.email_normalized = ANY(%s::text[])",
        (offering_row, sorted({row.ra for row in rows}), sorted({row.email.casefold() for row in rows})),
    ).fetchall()
    by_ra: dict[str, int] = {}
    by_email: dict[str, int] = {}
    roles: dict[int, str] = {}
    for person_id, ra, email_normalized, role in found:
        if ra is not None:
            by_ra[ra] = int(person_id)
        if email_normalized is not None:
            by_email[email_normalized] = int(person_id)
        if role is not None:
            roles[int(person_id)] = role
    return by_ra, by_email, roles


def import_roster(conn: psycopg.Connection, ref: str, rows: Sequence[RosterRow], *, now: datetime,
                  actor_token_id: str | None) -> ImportResult | InvalidState | ListConflict | None:
    """Grava a lista `rows` (já conferida por `check_rows`) no oferecimento `ref`
    por inteiro, numa transação: trava o oferecimento (`None` se não existe; fora de `draft` e
    `active`, `InvalidState`) e toma o lock dos eventos; cada linha, na ordem do pedido, é pessoa nova
    (nem o RA nem o e-mail existem), reaproveitada (os dois apontam a mesma pessoa, cuja identidade e
    nome não mudam) ou conflito `duplicate` (qualquer outro caso, inclusive a pessoa já cadastrada com o RA
    da lista e sem e-mail); a reaproveitada da equipe do oferecimento é conflito
    `invalid_value`, e a já aluna conta como "já matriculada".

    Com conflito, `ListConflict` (um item por linha, em ordem de índice), sem nada gravado (a
    transação só leu). Senão, `shuffle` embaralha a lista das pessoas novas e, à parte, a das
    matrículas novas: as pessoas são cadastradas pelo `people.register_person`,
    com um `person.registered` cada, na ordem embaralhada, e as matrículas `student`/`pending`
    entram na ordem embaralhada; limpa o preparo do envio e grava `student.list_imported` com
    as contagens."""
    with conn.transaction():
        offering = lock_offering(conn, ref)
        if offering is None:
            return None
        if offering.state not in ("draft", "active"):
            return InvalidState(offering.state)
        # O lock dos eventos logo depois do oferecimento, antes de ler as identidades e de alocar os
        # `people.id` (como o cadastro de pessoas: a ordem de `people.id` é a de confirmação, e a consulta
        # abaixo vê todo cadastro confirmado antes).
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK_KEY,))
        by_ra, by_email, roles = _registered_people(conn, offering.id, rows)

        conflicts: list[tuple[int, str]] = []
        new_people: list[int] = []  # índices das linhas de pessoas novas, na ordem do pedido
        # Matrículas novas, na ordem do pedido: `(índice da linha nova, None)` ou `(None, person_id)`.
        new_enrollments: list[tuple[int | None, int | None]] = []
        people_reused = already_enrolled = 0
        for index, row in enumerate(rows):
            ra_person = by_ra.get(row.ra)
            email_person = by_email.get(row.email.casefold())
            if ra_person is None and email_person is None:
                new_people.append(index)
                new_enrollments.append((index, None))
            elif ra_person is not None and ra_person == email_person:
                role = roles.get(ra_person)
                if role == "team":
                    conflicts.append((index, "invalid_value"))
                elif role == "student":
                    people_reused += 1
                    already_enrolled += 1
                else:
                    people_reused += 1
                    new_enrollments.append((None, ra_person))
            else:
                conflicts.append((index, "duplicate"))
        if conflicts:
            return ListConflict(tuple(conflicts))

        # Nome global resolvido na hora (os testes substituem `students.shuffle`).
        shuffle(new_people)
        shuffle(new_enrollments)
        created: dict[int, uuid.UUID] = {}
        for index in new_people:
            row = rows[index]
            person = people.register_person(conn, full_name=row.name, ra=row.ra, email=row.email, now=now,
                                            actor_token_id=actor_token_id)
            if isinstance(person, people.Duplicate):
                # Não acontece: o lock dos eventos serializa os cadastros, e a consulta acima já viu
                # todo RA e e-mail confirmado. Levantar desfaz a importação inteira.
                raise RuntimeError("pessoa da lista cadastrada durante a importação")
            created[index] = person.pseudo_id
        person_ids: dict[uuid.UUID, int] = {}
        if created:
            person_ids = {pseudo_id: int(person_id) for pseudo_id, person_id in conn.execute(
                "SELECT pseudo_id, id FROM people WHERE pseudo_id = ANY(%s::uuid[])",
                (list(created.values()),)).fetchall()}
        enrollment_people = [person_ids[created[index]] if index is not None else person_id
                             for index, person_id in new_enrollments]
        if enrollment_people:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO enrollments (person_id, offering_id, role, state, created_at) "
                    "VALUES (%s, %s, 'student', 'pending', %s)",
                    [(person_id, offering.id, now) for person_id in enrollment_people])
        conn.execute(_CLEAR_PREPARATION_SQL, (offering.id,))
        result = ImportResult(offering_id=offering.offering_id, code=offering.code, received=len(rows),
                              people_created=len(created), people_reused=people_reused,
                              enrollments_created=len(enrollment_people),
                              already_enrolled=already_enrolled)
        append_event(conn, EventType.STUDENT_LIST_IMPORTED,
                     {"offering_id": offering.offering_id, "received": result.received,
                      "people_created": result.people_created, "people_reused": result.people_reused,
                      "enrollments_created": result.enrollments_created,
                      "already_enrolled": result.already_enrolled},
                     occurred_at=now, actor_token_id=actor_token_id)
    return result


def adhesion(conn: psycopg.Connection, ref: str, *, now: datetime) -> Adhesion | None:
    """A adesão por estado das matrículas de aluno do oferecimento `ref`, ou
    `None` se ele não existe, numa instrução só (uma foto). O estado do oferecimento é ignorado; a
    equipe não entra; `enrolled` é a soma dos outros sete."""
    condition, param = _ref_condition(ref)
    counts = ", ".join(f"count(*) FILTER (WHERE a.state = '{state}')" for state in ADHESION_STATES)
    row = conn.execute(
        f"SELECT o.offering_id, o.code, {counts} FROM offerings o "
        f"LEFT JOIN LATERAL (SELECT {_ADHESION_STATE_SQL} AS state FROM enrollments e "
        "WHERE e.offering_id = o.id AND e.role = 'student') a ON true "
        f"WHERE {condition} GROUP BY o.id",
        {"ref": param, "now": now},
    ).fetchone()
    if row is None:
        return None
    by_state = {state: int(count) for state, count in zip(ADHESION_STATES, row[2:], strict=True)}
    return Adhesion(offering_id=row[0], code=row[1], enrolled=sum(by_state.values()), **by_state)


# --------------------------------------------------------------------------------------------------
# Envio em duas fases e reenvio
# --------------------------------------------------------------------------------------------------


def prepare_invites(conn: psycopg.Connection, ref: str, *, valid_hours: int, now: datetime,
                    actor_token_id: str | None) -> InvitePreparation | InvalidState | BotRequired | None:
    """Prepara o envio em lote (a primeira fase), numa transação: trava o oferecimento
    (`None` se não existe; fora de `active`, `InvalidState`; sem bot ou com o bot `no_access`,
    `BotRequired`); conta os elegíveis; sorteia o código de confirmação
    (`secrets.token_urlsafe(16)`, que não é segredo) e grava o resumo, `agora + 10 min`, a validade
    dos convites (`valid_hours`, 1 a 720, já conferida pela rota) e a contagem, substituindo o preparo
    anterior; grava `student.invites_prepared`. Com 0 elegíveis, o preparo vale do mesmo jeito."""
    team.validate_valid_hours(valid_hours)
    with conn.transaction():
        offering = lock_offering(conn, ref)
        if offering is None:
            return None
        if offering.state != "active":
            return InvalidState(offering.state)
        if not _bot_ready(conn, offering.id):
            return BotRequired()
        counted = conn.execute(f"SELECT count(*) FROM enrollments e WHERE {_ELIGIBLE_WHERE_SQL}",
                               {"o": offering.id, "now": now}).fetchone()
        assert counted is not None
        eligible = int(counted[0])
        confirmation_code = _secrets.token_urlsafe(16)
        expires_at = now + INVITE_CODE_TTL
        conn.execute(
            "UPDATE offerings SET invite_code_sha256 = %s, invite_code_expires_at = %s, "
            "invite_valid_hours = %s, invite_eligible_count = %s WHERE id = %s",
            (code_digest(confirmation_code), expires_at, valid_hours, eligible, offering.id),
        )
        append_event(conn, EventType.STUDENT_INVITES_PREPARED,
                     {"offering_id": offering.offering_id, "eligible": eligible, "valid_hours": valid_hours,
                      "expires_at": expires_at},
                     occurred_at=now, actor_token_id=actor_token_id)
    return InvitePreparation(offering_id=offering.offering_id, code=offering.code, eligible=eligible,
                             valid_hours=valid_hours, confirmation_code=confirmation_code,
                             expires_at=expires_at)


def confirm_invites(conn: psycopg.Connection, ref: str, *, confirmation_code: str, now: datetime,
                    actor_token_id: str | None,
                    ) -> InvitesQueued | InvalidState | BotRequired | InvalidConfirmation | None:
    """Confirma o envio em lote (a segunda fase), numa transação, nesta ordem:
    oferecimento (`None`) → fora de `active` (`InvalidState`) → sem bot ou bot `no_access`
    (`BotRequired`) → o código confere com o resumo guardado (`hmac.compare_digest`) e
    `agora < invite_code_expires_at`, senão `InvalidConfirmation`, sem mudança. Então limpa as quatro
    colunas do preparo (uso único), trava as matrículas de aluno pendentes em ordem de `id` e, numa
    instrução nova, enfileira os elegíveis do momento, em ordem de `enrollments.id`, até o número
    contado no preparo (limite de contagem, não lista): um `invite_sends` `batch` e uma tarefa
    `invite.email` para cada um; grava `student.invites_queued {origin: batch}` (mesmo com 0). Também
    cria uma `invite_batches` por confirmação e grava o `batch_id` dela em
    cada envio do lote."""
    with conn.transaction():
        offering = lock_offering(conn, ref)
        if offering is None:
            return None
        if offering.state != "active":
            return InvalidState(offering.state)
        if not _bot_ready(conn, offering.id):
            return BotRequired()
        prepared = conn.execute(
            "SELECT invite_code_sha256, invite_code_expires_at, invite_valid_hours, invite_eligible_count "
            "FROM offerings WHERE id = %s", (offering.id,),
        ).fetchone()
        assert prepared is not None
        digest, expires_at, valid_hours, limit = prepared
        if (digest is None or expires_at is None or not now < expires_at
                or not hmac.compare_digest(code_digest(confirmation_code), digest)):
            return InvalidConfirmation()
        conn.execute(_CLEAR_PREPARATION_SQL, (offering.id,))
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        conn.execute(
            "SELECT id FROM enrollments WHERE offering_id = %s AND role = 'student' AND state = 'pending' "
            "ORDER BY id FOR NO KEY UPDATE", (offering.id,),
        )
        chosen = [int(row[0]) for row in conn.execute(
            f"SELECT e.id FROM enrollments e WHERE {_ELIGIBLE_WHERE_SQL} ORDER BY e.id LIMIT %(limit)s",
            {"o": offering.id, "now": now, "limit": int(limit)},
        ).fetchall()]
        # Um lote por confirmação (mesmo com 0 elegíveis), com o
        # `batch_uuid` que o identifica no `notice.recorded`; o `batch_id` vai a cada envio do lote.
        batch = conn.execute(
            "INSERT INTO invite_batches (batch_uuid, offering_id, created_at) VALUES (%s, %s, %s) RETURNING id",
            (uuid.uuid4(), offering.id, now),
        ).fetchone()
        assert batch is not None
        batch_id = int(batch[0])
        for enrollment_id in chosen:
            _queue_send(conn, enrollment_id, origin=ORIGIN_BATCH, valid_hours=int(valid_hours), now=now,
                        batch_id=batch_id)
        append_event(conn, EventType.STUDENT_INVITES_QUEUED,
                     {"offering_id": offering.offering_id, "origin": ORIGIN_BATCH, "queued": len(chosen)},
                     occurred_at=now, actor_token_id=actor_token_id)
    return InvitesQueued(offering_id=offering.offering_id, code=offering.code, queued=len(chosen),
                         valid_hours=int(valid_hours))


def resend_invites(conn: psycopg.Connection, ref: str, *, pseudonyms: Sequence[str], valid_hours: int,
                   now: datetime, actor_token_id: str | None,
                   ) -> ResendResult | InvalidState | BotRequired | None:
    """Reenvio pela coordenação, numa transação: oferecimento
    (`None`) → fora de `active` (`InvalidState`) → sem bot ou bot `no_access` (`BotRequired`) → os
    pseudônimos (válidos, com repetição; já conferidos pela rota), cada pessoa uma vez, citada pelo
    apelido ou pelo identificador: sem pessoa → só contados (`not_found_count`, itens distintos,
    nunca ecoados); sem matrícula de aluno no oferecimento → `not_students` (pseudônimo e apelido
    lidos do banco), na ordem da primeira referência; alunos, vinculados ou não → um `invite_sends`
    `coordination` (com `valid_hours`) e a tarefa para cada um, em ordem de `enrollments.id`. Grava
    `student.invites_queued {origin: coordination}` (mesmo com 0). Não é idempotente: cada pedido
    cria envios novos."""
    team.validate_valid_hours(valid_hours)
    refs = list(pseudonyms)
    if not refs or any(not people.is_pseudonym(item) for item in refs):
        raise ValueError("lista de pseudônimos vazia ou fora do formato")
    with conn.transaction():
        offering = lock_offering(conn, ref)
        if offering is None:
            return None
        if offering.state != "active":
            return InvalidState(offering.state)
        if not _bot_ready(conn, offering.id):
            return BotRequired()
        found = conn.execute(
            "SELECT p.id, p.pseudo_id, p.alias, e.id FROM people p "
            "LEFT JOIN enrollments e ON e.person_id = p.id AND e.offering_id = %s AND e.role = 'student' "
            "WHERE p.alias = ANY(%s::text[]) OR p.pseudo_id = ANY(%s::uuid[])",
            (offering.id, sorted({item for item in refs if people.is_alias(item)}),
             sorted({uuid.UUID(item) for item in refs if people.is_pseudo_id(item)})),
        ).fetchall()
        by_alias = {row[2]: row for row in found}
        by_pseudo_id = {str(row[1]): row for row in found}
        seen: set[int] = set()
        missing: set[str] = set()
        not_students: list[tuple[uuid.UUID, str]] = []
        enrollment_ids: set[int] = set()
        for item in refs:
            row = by_alias.get(item) if people.is_alias(item) else by_pseudo_id.get(item)
            if row is None:
                missing.add(item)
                continue
            if int(row[0]) in seen:
                continue
            seen.add(int(row[0]))
            if row[3] is None:
                not_students.append((row[1], row[2]))
            else:
                enrollment_ids.add(int(row[3]))
        # As matrículas travadas em ordem de `id` (a mesma ordem da confirmação e das travas da T1).
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        locked = [int(row[0]) for row in conn.execute(
            "SELECT id FROM enrollments WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE",
            (sorted(enrollment_ids),)).fetchall()]
        for enrollment_id in locked:
            _queue_send(conn, enrollment_id, origin=ORIGIN_COORDINATION, valid_hours=valid_hours, now=now)
        append_event(conn, EventType.STUDENT_INVITES_QUEUED,
                     {"offering_id": offering.offering_id, "origin": ORIGIN_COORDINATION,
                      "queued": len(locked)},
                     occurred_at=now, actor_token_id=actor_token_id)
    return ResendResult(offering_id=offering.offering_id, code=offering.code, queued=len(locked),
                        valid_hours=valid_hours, not_students=tuple(not_students),
                        not_found_count=len(missing))


# --------------------------------------------------------------------------------------------------
# Tarefa `invite.email`
# --------------------------------------------------------------------------------------------------


# Os registros da tarefa saem no logger do worker, como o `telegram.update`.
_worker_logger = logging.getLogger("cq_server.worker")

# O `result` do registro `invite.email`.
RESULT_SKIPPED = "skipped"
RESULT_CANCELLED = "cancelled"
RESULT_FAILED = "failed"
RESULT_SENT = "sent"
RESULT_RETRY = "retry"
# Resultado interno da T2 descartada (só o registro `invite.email_superseded`, nunca `invite.email`).
_SUPERSEDED = "superseded"

# Na segunda transação (T2), a categoria do `PermanentFailure` vira o `failure` do envio. Uma categoria fora
# da porta (canal defeituoso) conta como recusa do servidor, para o CHECK de `failure` nunca falhar.
_FAILURE_BY_CATEGORY = {"recipient": "recipient_refused", "auth": "auth_refused", "server": "server_refused"}
FAILURE_NOT_CONFIGURED = "not_configured"
FAILURE_ATTEMPTS_EXHAUSTED = "attempts_exhausted"

# `error_type` é o nome de um tipo; outro texto (canal defeituoso) vira o nome genérico.
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_GENERIC_ERROR_TYPE = "Exception"

# Maior `bigint`: um id maior no payload não existe (e não vai ao banco, que o recusaria).
_MAX_BIGINT = 2**63 - 1


@dataclass(frozen=True)
class _Finished:
    """A T1 terminou sem envio: o `result` do registro e, se a tarefa falha de vez, o motivo."""

    result: str
    failure: str | None = None


@dataclass(frozen=True)
class _Attempt:
    """O que a T1 deixa para o envio e a T2: o convite gerado nesta execução, os dados dos eventos e a
    mensagem (o endereço e o corpo, com o código, só em memória e fora do `repr`)."""

    invite_id: int
    offering_id: uuid.UUID
    person: uuid.UUID
    origin: str
    expires_at: datetime
    message: VerificationMessage = field(repr=False)


def _invite_send_id(task: ClaimedTask) -> int:
    """O `invite_send_id` do payload; sem ele (ou fora de inteiro), falha definitiva, sem registro."""
    payload = task.payload
    send_id = payload.get("invite_send_id") if isinstance(payload, dict) else None
    if isinstance(send_id, bool) or not isinstance(send_id, int):
        raise PermanentTaskError("payload sem invite_send_id")
    return send_id


def _error_type(name: object) -> str:
    """O nome de tipo que vai para `invite_sends.error_type`."""
    return name if isinstance(name, str) and _ERROR_TYPE.fullmatch(name) else _GENERIC_ERROR_TYPE


def _log_result(send_id: int, result: str) -> None:
    """O registro `invite.email`, depois do COMMIT: só o id do envio e o resultado."""
    _worker_logger.info("invite.email", extra={"event": "invite.email", "invite_send_id": send_id,
                                               "result": result})


def _cancel_send(conn: psycopg.Connection, send_id: int, reason: str, *, offering_id: uuid.UUID,
                 person: uuid.UUID, origin: str, now: datetime) -> _Finished:
    """T1, nos cancelamentos: o envio `cancelled` com o motivo e `student.invite_cancelled`, sem revogar nem
    gerar código (os `attempts` ficam os dos códigos gerados antes)."""
    conn.execute("UPDATE invite_sends SET state = 'cancelled', cancel_reason = %s, finished_at = %s "
                 "WHERE id = %s", (reason, now, send_id))
    append_event(conn, EventType.STUDENT_INVITE_CANCELLED,
                 {"offering_id": offering_id, "person": person, "origin": origin, "cancel_reason": reason},
                 occurred_at=now, actor_token_id=None)
    return _Finished(RESULT_CANCELLED)


def _generate(conn: psycopg.Connection, send_id: int, *, now: datetime,
              configured: bool) -> _Finished | _Attempt:
    """A T1 da tarefa `invite.email`, numa transação, nesta ordem: lê o envio
    (inexistente → `skipped` e falha definitiva; fora de `pending` → `skipped`); trava o oferecimento
    (`FOR SHARE`), a matrícula e o envio (`FOR NO KEY UPDATE`) e confere o estado de novo; oferecimento fora
    de `active` → `offering_inactive`; origem `batch` ou `self_service` com a matrícula `active` →
    `already_linked`; envio mais novo da mesma matrícula → `superseded`; sem canal →
    `failed`/`not_configured` (`error_type` nulo) com `student.invite_failed`, sem revogar nem gerar
    código. Senão revoga o convite `pending` da matrícula (`student.invite_revoked {replaced}`,
    com a `origin` do envio que o criou), cria o convite novo deste envio, conta o código em
    `attempts` e monta a mensagem do convite (o e-mail da lista é lido só aqui). O bot
    não é conferido."""
    if not 0 < send_id <= _MAX_BIGINT:
        return _Finished(RESULT_SKIPPED, failure="envio inexistente")
    with conn.transaction():
        found = conn.execute(
            "SELECT s.state, s.enrollment_id, e.offering_id FROM invite_sends s "
            "JOIN enrollments e ON e.id = s.enrollment_id WHERE s.id = %s",
            (send_id,),
        ).fetchone()
        if found is None:
            return _Finished(RESULT_SKIPPED, failure="envio inexistente")
        if found[0] != "pending":
            return _Finished(RESULT_SKIPPED)
        enrollment_id, offering_row = int(found[1]), int(found[2])
        # Ordem das travas: oferecimento → matrícula → envio.
        offering = conn.execute(
            "SELECT offering_id, state, course, term, timezone FROM offerings WHERE id = %s FOR SHARE",
            (offering_row,),
        ).fetchone()
        # Matrícula e envio em FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs das tabelas que os
        # referenciam.
        enrollment = conn.execute(
            "SELECT e.state, p.pseudo_id FROM enrollments e JOIN people p ON p.id = e.person_id "
            "WHERE e.id = %s FOR NO KEY UPDATE OF e",
            (enrollment_id,),
        ).fetchone()
        send = conn.execute("SELECT state, origin, valid_hours FROM invite_sends WHERE id = %s FOR NO KEY UPDATE",
                            (send_id,)).fetchone()
        assert offering is not None and enrollment is not None and send is not None
        if send[0] != "pending":  # outra execução terminou o envio antes das travas
            return _Finished(RESULT_SKIPPED)
        offering_id, offering_state, course, term, zone = offering
        enrollment_state, person = enrollment
        origin, valid_hours = send[1], int(send[2])
        cancel = {"offering_id": offering_id, "person": person, "origin": origin, "now": now}

        if offering_state != "active":
            return _cancel_send(conn, send_id, "offering_inactive", **cancel)
        if origin in (ORIGIN_BATCH, ORIGIN_SELF_SERVICE) and enrollment_state == "active":
            return _cancel_send(conn, send_id, "already_linked", **cancel)
        newer = conn.execute("SELECT EXISTS (SELECT 1 FROM invite_sends WHERE enrollment_id = %s AND id > %s)",
                             (enrollment_id, send_id)).fetchone()
        assert newer is not None
        if newer[0]:
            return _cancel_send(conn, send_id, "superseded", **cancel)
        if not configured:
            conn.execute("UPDATE invite_sends SET state = 'failed', failure = %s, finished_at = %s WHERE id = %s",
                         (FAILURE_NOT_CONFIGURED, now, send_id))
            append_event(conn, EventType.STUDENT_INVITE_FAILED,
                         {"offering_id": offering_id, "person": person, "origin": origin,
                          "failure": FAILURE_NOT_CONFIGURED},
                         occurred_at=now, actor_token_id=None)
            return _Finished(RESULT_FAILED, failure="e-mail não configurado")

        previous = conn.execute(
            "SELECT i.id, s.origin FROM invites i LEFT JOIN invite_sends s ON s.id = i.invite_send_id "
            "WHERE i.enrollment_id = %s AND i.state = 'pending' FOR UPDATE OF i",
            (enrollment_id,),
        ).fetchone()
        if previous is not None:
            conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = %s", (now, previous[0]))
            append_revocation_event(conn, offering_id=offering_id, person=person, role="student",
                                    origin=previous[1], reason="replaced", now=now, actor_token_id=None)

        expires_at = now + timedelta(hours=valid_hours)
        for _ in range(team.MAX_CODE_DRAWS):
            # Nome global resolvido a cada sorteio (os testes podem substituir o gerador dos códigos).
            invite_code = team.generate_invite_code()
            inserted = conn.execute(
                "INSERT INTO invites (enrollment_id, code_sha256, state, expires_at, created_at, invite_send_id) "
                "VALUES (%s, %s, 'pending', %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (enrollment_id, code_digest(invite_code), expires_at, now, send_id),
            ).fetchone()
            if inserted is not None:
                break
        else:  # pragma: no cover - só com um gerador defeituoso
            raise RuntimeError("não foi possível sortear um código de convite livre")
        conn.execute("UPDATE invite_sends SET attempts = attempts + 1 WHERE id = %s", (send_id,))

        # A mensagem: o e-mail da lista (uso interno, só para o envelope e o `To`), o bot,
        # a disciplina e o período redigidos de novo na saída e a validade no fuso do oferecimento.
        address = conn.execute(
            "SELECT i.email FROM identities i JOIN enrollments e ON e.person_id = i.person_id WHERE e.id = %s",
            (enrollment_id,),
        ).fetchone()
        bot = conn.execute("SELECT username FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()
        if bot is None:  # pragma: no cover - todo envio nasce num oferecimento com bot
            raise RuntimeError("oferecimento sem bot")
        known = load_known_identities(conn)
        subject, body = botmessages.compose_invite_email(
            course=redact(course, known), term=redact(term, known),
            link=f"{team.LINK_PREFIX}{bot[0]}?start={invite_code}",
            validity=timezones.format_local(expires_at, zone))
        # Sem e-mail na identidade (não acontece: a lista exige o e-mail), o canal recusa o endereço
        # vazio como destinatário.
        email = address[0] if address is not None and address[0] is not None else ""
        message = VerificationMessage(address=email, subject=subject, body=body)
    return _Attempt(invite_id=int(inserted[0]), offering_id=offering_id, person=person, origin=origin,
                    expires_at=expires_at, message=message)


def _record(conn: psycopg.Connection, send_id: int, attempt: _Attempt,
            result: Delivered | TemporaryFailure | PermanentFailure, *, last_attempt: bool,
            now: datetime) -> str:
    """A T2 da tarefa `invite.email`, numa transação: trava o envio; se ele saiu de
    `pending` ou se o convite desta execução não é o de maior `id` do envio, a execução foi superada, e
    nada é gravado. `Delivered` → `sent` e `student.invite_sent`; `PermanentFailure`, ou
    `TemporaryFailure` na última tentativa (`attempts_exhausted`) → o convite desta tentativa revogado
    se ainda `pending` (coberto pelo evento de falha), `failed` com `failure` e `error_type` e
    `student.invite_failed`; `TemporaryFailure` antes da última → nada gravado. Devolve o resultado do
    registro (ou `_SUPERSEDED`)."""
    with conn.transaction():
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        send = conn.execute("SELECT state FROM invite_sends WHERE id = %s FOR NO KEY UPDATE", (send_id,)).fetchone()
        latest = conn.execute("SELECT max(id) FROM invites WHERE invite_send_id = %s", (send_id,)).fetchone()
        if send is None or send[0] != "pending" or latest is None or latest[0] != attempt.invite_id:
            return _SUPERSEDED
        if isinstance(result, Delivered):
            conn.execute("UPDATE invite_sends SET state = 'sent', finished_at = %s WHERE id = %s", (now, send_id))
            append_event(conn, EventType.STUDENT_INVITE_SENT,
                         {"offering_id": attempt.offering_id, "person": attempt.person, "origin": attempt.origin,
                          "expires_at": attempt.expires_at},
                         occurred_at=now, actor_token_id=None)
            return RESULT_SENT
        if isinstance(result, PermanentFailure):
            failure = _FAILURE_BY_CATEGORY.get(result.category, "server_refused")
        elif last_attempt:
            failure = FAILURE_ATTEMPTS_EXHAUSTED
        else:
            return RESULT_RETRY
        conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = %s AND state = 'pending'",
                     (now, attempt.invite_id))
        conn.execute("UPDATE invite_sends SET state = 'failed', failure = %s, error_type = %s, finished_at = %s "
                     "WHERE id = %s", (failure, _error_type(result.error_type), now, send_id))
        append_event(conn, EventType.STUDENT_INVITE_FAILED,
                     {"offering_id": attempt.offering_id, "person": attempt.person, "origin": attempt.origin,
                      "failure": failure},
                     occurred_at=now, actor_token_id=None)
    return RESULT_FAILED


class InviteEmailTaskHandler:
    """O tratador da tarefa `invite.email`, para `run_worker`: chamável
    (`handler(task)`), com `close()`.

    Usado só pela thread do laço do worker: tem a sua conexão (`_leasing.LazyConnection`), aberta na
    primeira chamada (nunca na construção) e fechada em `close()`. Cada execução faz a T1 numa
    transação, o envio **fora** de transação pela porta de verificação e a T2 noutra transação, e
    termina, em todo caso, com exatamente um registro `invite.email` depois
    do COMMIT (ou só `invite.email_superseded`, na T2 superada). Uma `Exception` que escape do `send`
    vale como `TemporaryFailure` com o nome do tipo; uma `BaseException` passa. Uma falha ao
    gravar (ex.: o COMMIT do evento) escapa sem registro: a tarefa repete pela regra da fila."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings,
                 channel: VerificationChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel

    def __call__(self, task: ClaimedTask) -> None:
        send_id = _invite_send_id(task)
        conn = self._db.get()
        step = _generate(conn, send_id, now=self._clock.now(), configured=self._channel is not None)
        if isinstance(step, _Finished):
            _log_result(send_id, step.result)
            if step.failure is not None:
                raise PermanentTaskError(step.failure)
            return
        result = self._send(step.message)
        outcome = _record(conn, send_id, step, result, last_attempt=task.attempts >= self._settings.max_attempts,
                          now=self._clock.now())
        if outcome == _SUPERSEDED:
            _worker_logger.info("invite.email_superseded", extra={"event": "invite.email_superseded",
                                                                  "invite_send_id": send_id})
            return
        _log_result(send_id, outcome)
        if outcome == RESULT_FAILED:
            raise PermanentTaskError("falha registrada no envio do convite")
        if outcome == RESULT_RETRY:
            raise RetryTaskLater(0)

    def _send(self, message: VerificationMessage) -> Delivered | TemporaryFailure | PermanentFailure:
        """O envio pela porta, fora de transação; só uma `Exception` vira `TemporaryFailure` (com o nome
        do tipo, nunca o texto), e um resultado fora da porta vale como `TypeError`, como no
        `outbox.dispatch_once` da fila de saída."""
        assert self._channel is not None
        try:
            result = self._channel.send(message)
        except Exception as exc:  # noqa: BLE001 - falha temporária; só o nome do tipo é guardado
            return TemporaryFailure(_error_type(type(exc).__name__))
        if isinstance(result, (Delivered, TemporaryFailure, PermanentFailure)):
            return result
        return TemporaryFailure(TypeError.__name__)

    def close(self) -> None:
        """Fecha a conexão do tratador (idempotente)."""
        self._db.close()


def invite_email_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                              channel: VerificationChannel | None) -> InviteEmailTaskHandler:
    """O tratador da tarefa `invite.email`, para `run_worker`; `channel=None` quer
    dizer e-mail não configurado (os envios falham de vez com `not_configured`). A conexão ao
    banco abre na primeira chamada, nunca aqui."""
    return InviteEmailTaskHandler(conninfo, clock=clock, settings=settings, channel=channel)


# --------------------------------------------------------------------------------------------------
# Autosserviço `/convite <e-mail>`
# --------------------------------------------------------------------------------------------------


# Nome do comando do autosserviço, sem diferenciar maiúsculas.
INVITE_REQUEST_COMMAND = "/convite"
# O `/convite` sem a entidade `bot_command`: no início do texto, com o
# `@bot` opcional, seguido de espaço em branco ou do fim do texto. O trecho casado, como foi escrito
# e sem o espaço, é o prefixo da máscara.
_INVITE_REQUEST_TEXT = re.compile(r"^/convite(?:@[A-Za-z0-9_]+)?(?=\s|$)", re.IGNORECASE)
INVITE_REQUEST_MASK = "[e-mail]"


def invite_request_prefix(text: str | None, command: str | None) -> str | None:
    """O prefixo do `/convite` no texto de um update, ou `None` se o update não é
    um `/convite`: o `command` gravado (ex.: `/convite`, `/convite@Bot`), cujo nome antes do `@` é
    `/convite` sem diferenciar maiúsculas; sem `command`, o trecho casado por
    `^/convite(@[A-Za-z0-9_]+)?(\\s|$)`, sem diferenciar maiúsculas."""
    if text is None:
        return None
    if command is not None:
        if command.split("@", 1)[0].lower() != INVITE_REQUEST_COMMAND:
            return None
        return command
    match = _INVITE_REQUEST_TEXT.match(text)
    return match.group(0) if match is not None else None


def invite_request_mask(text: str | None, command: str | None) -> str | None:
    """O texto que fica guardado em `telegram_updates.text` depois do processamento de um
    `/convite`, ou `None` quando o texto não muda.

    Função pura. Com o prefixo de `invite_request_prefix` e o argumento
    `text[len(prefixo):].strip()` (o resto do texto, inclusive depois de quebra de linha) não vazio,
    devolve `f"{prefixo} [e-mail]"`, em qualquer chat e para qualquer bot de destino; o
    argumento nunca entra na máscara. Sem argumento, com outro comando ou sem texto, `None`.
    """
    prefix = invite_request_prefix(text, command)
    if prefix is None or text is None:
        return None
    if not text[len(prefix):].strip():
        return None
    return f"{prefix} {INVITE_REQUEST_MASK}"


def invite_request_argument(text: str | None, command: str | None) -> str | None:
    """O argumento do `/convite`: `text[len(prefixo):].strip()`, com o prefixo de
    `invite_request_prefix` (o resto do texto, inclusive depois de quebra de linha; vazio se não há
    argumento), ou `None` se o update não é um `/convite`. O valor fica só em memória: nunca vai para
    log, evento, tarefa nem outra coluna."""
    prefix = invite_request_prefix(text, command)
    if prefix is None or text is None:
        return None
    return text[len(prefix):].strip()


# --------------------------------------------------------------------------------------------------
# Termo, vínculo e autosserviço no bot
# --------------------------------------------------------------------------------------------------
#
# Chamados pelo tratador `telegram.update` (`cq_server.reception`), na transação dele, que já travou a
# linha do update e grava o `outcome` que estas funções devolvem; as respostas saem pela fila de saída
# (`outbox.enqueue`) na mesma transação. Este módulo nunca importa a `reception`: os nomes que as duas
# compartilham (os comandos do aluno e os desfechos) ficam aqui. Todos travam o bot (`FOR NO KEY
# UPDATE`, como o `/start` da equipe) e **leem** o estado do oferecimento sem travá-lo;
# depois, as matrículas (`FOR NO KEY UPDATE`) e os convites (`FOR UPDATE`), em ordem de `id`, e os
# endereços.

# Os comandos do aluno, comparados sem diferenciar maiúsculas; `/start` e
# `/ajuda` seguem o fluxo da equipe.
ACCEPT_COMMAND = "/aceito"
REFUSE_COMMAND = "/recuso"
STUDENT_COMMANDS = (ACCEPT_COMMAND, REFUSE_COMMAND, INVITE_REQUEST_COMMAND)

# Os `outcome` dos fluxos do aluno. `/aceito` e `/recuso` sem candidato usam o `help` do
# fluxo da equipe, que a recepção grava (as funções devolvem `None`).
STUDENT_TERMS_PRESENTED = "student_terms_presented"
STUDENT_JOINED = "student_joined"
STUDENT_ENDPOINT_REPLACED = "student_endpoint_replaced"
STUDENT_ALREADY_LINKED = "student_already_linked"
STUDENT_REFUSED_INVALID = "student_refused_invalid"
STUDENT_REFUSED_OTHER_PERSON = "student_refused_other_person"
STUDENT_TERMS_REFUSED = "student_terms_refused"
INVITE_REQUESTED = "invite_requested"

# O envio por autosserviço usa sempre a validade padrão; limites de
# 3 pedidos por chat e bot em 24 h e de 1 envio por autosserviço por matrícula em 1 h.
SELF_SERVICE_VALID_HOURS = team.DEFAULT_VALID_HOURS
MAX_REQUESTS_PER_CHAT = 3
REQUEST_WINDOW = timedelta(hours=24)
MAX_SELF_SERVICE_SENDS = 1
SELF_SERVICE_WINDOW = timedelta(hours=1)


def student_command(name: str | None) -> str | None:
    """O comando do aluno (`/aceito`, `/recuso` ou `/convite`) do nome de um comando já sem o `@bot`,
    sem diferenciar maiúsculas; `None` para qualquer outro."""
    if name is None:
        return None
    folded = name.lower()
    return folded if folded in STUDENT_COMMANDS else None


@dataclass(frozen=True)
class _BotOffering:
    """O oferecimento do bot travado, com o estado lido sem trava."""

    row: int
    offering_id: uuid.UUID
    state: str


@dataclass(frozen=True)
class _StudentInvite:
    """Um convite de aluno com a matrícula, a pessoa, a origem do envio e o chat da apresentação."""

    id: int
    enrollment_id: int
    enrollment_state: str
    person_id: int
    person: uuid.UUID
    origin: str
    state: str
    expires_at: datetime
    presented_update_id: int | None
    presented_terms_version: int | None
    presented_bot_id: int | None
    presented_chat_id: int | None


def _lock_bot(conn: psycopg.Connection, bot_id: int) -> _BotOffering:
    """Trava o bot (`FOR NO KEY UPDATE`, como no fluxo da equipe) e lê o oferecimento dele sem travá-lo."""
    row = conn.execute(
        "SELECT o.id, o.offering_id, o.state FROM bots b JOIN offerings o ON o.id = b.offering_id "
        "WHERE b.id = %s FOR NO KEY UPDATE OF b",
        (bot_id,),
    ).fetchone()
    if row is None:
        raise LookupError("bot inexistente")
    return _BotOffering(row=int(row[0]), offering_id=row[1], state=row[2])


def _load_invites(conn: psycopg.Connection, invite_ids: Sequence[int], enrollment_ids: Sequence[int], *,
                  lock: bool) -> list[_StudentInvite]:
    """Os convites de aluno `invite_ids`, em ordem de `id`. Com `lock`, trava antes as matrículas
    `enrollment_ids` (`FOR NO KEY UPDATE`) e depois os convites (`FOR UPDATE`), em ordem de `id`
    em cada tabela, e o estado lido é o de depois da trava."""
    if lock:
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        conn.execute("SELECT 1 FROM enrollments WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE",
                     (sorted(set(enrollment_ids)),))
    rows = conn.execute(
        "SELECT i.id, i.enrollment_id, e.state, e.person_id, p.pseudo_id, s.origin, i.state, i.expires_at, "
        "i.presented_update_id, i.presented_terms_version, u.bot_id, u.chat_id FROM invites i "
        "JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id "
        "JOIN invite_sends s ON s.id = i.invite_send_id "
        "LEFT JOIN telegram_updates u ON u.id = i.presented_update_id "
        "WHERE i.id = ANY(%s) ORDER BY i.id" + (" FOR UPDATE OF i" if lock else ""),
        (list(invite_ids),),
    ).fetchall()
    return [_StudentInvite(id=int(r[0]), enrollment_id=int(r[1]), enrollment_state=r[2], person_id=int(r[3]),
                           person=r[4], origin=r[5], state=r[6], expires_at=r[7],
                           presented_update_id=None if r[8] is None else int(r[8]),
                           presented_terms_version=None if r[9] is None else int(r[9]),
                           presented_bot_id=None if r[10] is None else int(r[10]),
                           presented_chat_id=None if r[11] is None else int(r[11]))
            for r in rows]


def _presented_invites(conn: psycopg.Connection, *, bot_id: int, chat_id: int,
                       lock: bool) -> list[_StudentInvite]:
    """Os convites de aluno `pending` (pelo estado gravado, inclusive os vencidos) cujo
    `presented_update_id` aponta um update deste bot e deste chat, do de maior `presented_update_id` (o
    candidato de `/aceito` e `/recuso`) para o de menor. Com a trava do bot, o conjunto
    só pode diminuir (só o `/start` e o `/aceito` deste bot apresentam o termo), e por isso a leitura
    depois de travar as matrículas e os convites é a vigente."""
    found = conn.execute(
        "SELECT i.id, i.enrollment_id FROM invites i JOIN telegram_updates u ON u.id = i.presented_update_id "
        "WHERE u.bot_id = %s AND u.chat_id = %s AND i.state = 'pending'",
        (bot_id, chat_id),
    ).fetchall()
    if not found:
        return []
    invites = _load_invites(conn, [int(r[0]) for r in found], [int(r[1]) for r in found], lock=lock)
    pending = [invite for invite in invites if invite.state == "pending"]
    return sorted(pending, key=lambda invite: invite.presented_update_id or 0, reverse=True)


def _reply(conn: psycopg.Connection, outbox: Any, text: str, *, bot_id: int, update_row_id: int) -> None:
    """Uma resposta ao chat do update (`reply_to_update_id`), pela fila de saída."""
    outbox.enqueue(conn, text=text, bot_id=bot_id, reply_to_update_id=update_row_id)


def _refuse_invalid(conn: psycopg.Connection, outbox: Any, *, bot_id: int, update_row_id: int) -> str:
    """`LINK_INVALID` ao chat do update, sem mudança nem evento (as recusas de link de aluno não geram
    evento)."""
    _reply(conn, outbox, botmessages.LINK_INVALID, bot_id=bot_id, update_row_id=update_row_id)
    return STUDENT_REFUSED_INVALID


def _present_terms(conn: psycopg.Connection, outbox: Any, invite: _StudentInvite, *, bot_id: int,
                   update_row_id: int) -> str:
    """`TERMS` da versão vigente ao chat do update, e a apresentação gravada no convite: este update e a
    versão (a mesma do texto, `TERMS_VERSION` lido na hora); sem evento."""
    version = botmessages.TERMS_VERSION
    text = offering_text(conn, "TERMS", bot_id=bot_id)
    conn.execute("UPDATE invites SET presented_update_id = %s, presented_terms_version = %s WHERE id = %s",
                 (update_row_id, version, invite.id))
    _reply(conn, outbox, text, bot_id=bot_id, update_row_id=update_row_id)
    return STUDENT_TERMS_PRESENTED


def _refuse_other_person(conn: psycopg.Connection, outbox: Any, invite: _StudentInvite, bot: _BotOffering, *,
                         bot_id: int, update_row_id: int, now: datetime) -> str:
    """O chat é o endereço vigente de outra pessoa no bot: o convite revogado, `OTHER_PERSON` ao chat
    do update e `student.invite_revoked {refused}`."""
    conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = %s", (now, invite.id))
    _reply(conn, outbox, botmessages.OTHER_PERSON, bot_id=bot_id, update_row_id=update_row_id)
    append_revocation_event(conn, offering_id=bot.offering_id, person=invite.person, role="student",
                            origin=invite.origin, reason="refused", now=now, actor_token_id=None)
    return STUDENT_REFUSED_OTHER_PERSON


def start_student(conn: psycopg.Connection, *, bot_id: int, update_row_id: int, chat_id: int, code: str,
                  now: datetime, outbox: Any) -> str | None:
    """Um `/start <código>` do chat privado `chat_id`, dirigido ao bot `bot_id`: se o
    código é de um convite **do oferecimento deste bot** e a matrícula é de aluno, aplica o
    fluxo do `/start` do aluno e devolve o `outcome`; senão devolve `None`, sem nada gravado, e a recepção segue
    no `team.process_start` (código desconhecido, de outro oferecimento ou de equipe).

    Na ordem: oferecimento fora de `active` → `LINK_INVALID`; convite `revoked` → `LINK_INVALID`;
    convite `used` e o chat é o endereço vigente da pessoa → reativa o endereço `blocked` e
    `ALREADY_LINKED` ao endereço; `used` e o chat não é o endereço vigente dela → `LINK_INVALID`;
    vencido (`agora >= expires_at`) → `LINK_INVALID`; amarrado a outro chat (o do `presented_update_id`)
    → `LINK_INVALID`; o chat é o endereço vigente de outra pessoa → revoga, `OTHER_PERSON` e
    `student.invite_revoked {refused}`; senão `TERMS` ao chat do update, e o convite fica amarrado a
    este update e à versão vigente."""
    with conn.transaction():
        bot = _lock_bot(conn, bot_id)
        found = conn.execute(
            "SELECT i.id, i.enrollment_id FROM invites i JOIN enrollments e ON e.id = i.enrollment_id "
            "WHERE i.code_sha256 = %s AND e.offering_id = %s AND e.role = 'student'",
            (code_digest(code), bot.row),
        ).fetchone()
        if found is None:
            return None
        (invite,) = _load_invites(conn, [int(found[0])], [int(found[1])], lock=True)
        reply = {"bot_id": bot_id, "update_row_id": update_row_id}

        if bot.state != "active" or invite.state == "revoked":
            return _refuse_invalid(conn, outbox, **reply)
        in_chat = team.chat_endpoint(conn, bot_id=bot_id, chat_id=chat_id)
        if invite.state == "used":
            # Um convite usado nunca é tratado como vencido: só o endereço vigente da pessoa o reusa.
            if in_chat is None or in_chat.person_id != invite.person_id:
                return _refuse_invalid(conn, outbox, **reply)
            team.reactivate_endpoint(conn, in_chat, now)
            outbox.enqueue(conn, recipient=invite.person, text=botmessages.ALREADY_LINKED, bot_id=bot_id)
            return STUDENT_ALREADY_LINKED
        if not now < invite.expires_at:
            return _refuse_invalid(conn, outbox, **reply)
        if invite.presented_update_id is not None and (invite.presented_bot_id,
                                                       invite.presented_chat_id) != (bot_id, chat_id):
            return _refuse_invalid(conn, outbox, **reply)
        if in_chat is not None and in_chat.person_id != invite.person_id:
            return _refuse_other_person(conn, outbox, invite, bot, now=now, **reply)
        return _present_terms(conn, outbox, invite, **reply)


def accept_terms(conn: psycopg.Connection, *, bot_id: int, update_row_id: int, chat_id: int, now: datetime,
                 outbox: Any) -> str | None:
    """Um `/aceito` do chat privado `chat_id`, dirigido ao bot `bot_id` (o fluxo do
    `/aceito`), depois de travar o bot, as matrículas e os convites apresentados no chat; devolve o
    `outcome`, ou `None` sem candidato (a recepção responde `HELP`, com o `help` da equipe).

    Com o candidato (o de maior `presented_update_id`), na ordem: oferecimento fora de `active` ou
    convite vencido → `LINK_INVALID`; versão apresentada diferente da vigente → `TERMS` de novo (este
    update e a versão vigente); o chat é o endereço vigente de outra pessoa → revoga, `OTHER_PERSON` e
    `student.invite_revoked {refused}`; senão o vínculo: o endereço como no `/start` da equipe (reativação no mesmo
    chat; noutro, o antigo fica `inactive` e entra um novo `active`), o convite `used`, a matrícula
    `active` com o termo aceito (no primeiro aceite também `activated_at`; num aceite seguinte,
    só `terms_version` e `terms_accepted_at`), `WELCOME_STUDENT` ao endereço, `student.joined` e os
    demais convites apresentados neste chat e neste bot (de outras pessoas) revogados, em ordem de
    `invites.id`, com `student.invite_revoked {refused}`."""
    with conn.transaction():
        bot = _lock_bot(conn, bot_id)
        presented = _presented_invites(conn, bot_id=bot_id, chat_id=chat_id, lock=True)
        if not presented:
            return None
        candidate, others = presented[0], sorted(presented[1:], key=lambda invite: invite.id)
        reply = {"bot_id": bot_id, "update_row_id": update_row_id}

        if bot.state != "active" or not now < candidate.expires_at:
            return _refuse_invalid(conn, outbox, **reply)
        if candidate.presented_terms_version != botmessages.TERMS_VERSION:
            return _present_terms(conn, outbox, candidate, **reply)
        in_chat = team.chat_endpoint(conn, bot_id=bot_id, chat_id=chat_id)
        if in_chat is not None and in_chat.person_id != candidate.person_id:
            return _refuse_other_person(conn, outbox, candidate, bot, now=now, **reply)

        own = team.person_endpoint(conn, bot_id=bot_id, person_id=candidate.person_id)
        replaced = team.link_endpoint(conn, person_id=candidate.person_id, bot_id=bot_id, chat_id=chat_id,
                                      current=own, now=now)
        conn.execute("UPDATE invites SET state = 'used', used_at = %s WHERE id = %s", (now, candidate.id))
        version = candidate.presented_terms_version
        if candidate.enrollment_state == "pending":
            conn.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = %s, "
                         "terms_accepted_at = %s WHERE id = %s", (now, version, now, candidate.enrollment_id))
        else:
            conn.execute("UPDATE enrollments SET terms_version = %s, terms_accepted_at = %s WHERE id = %s",
                         (version, now, candidate.enrollment_id))
        if others:
            conn.execute("UPDATE invites SET state = 'revoked', revoked_at = %s WHERE id = ANY(%s)",
                         (now, [invite.id for invite in others]))
        # O lock dos eventos vem depois de todas as travas de linha; a disciplina e o período da
        # boas-vindas são redigidos com as identidades lidas depois dele (como no `/start` da equipe).
        identities = load_write_identities(conn)
        welcome = offering_text(conn, "WELCOME_STUDENT", bot_id=bot_id, identities=identities)
        outbox.enqueue(conn, recipient=candidate.person, text=welcome, bot_id=bot_id)
        append_event(conn, EventType.STUDENT_JOINED,
                     {"offering_id": bot.offering_id, "person": candidate.person, "terms_version": version,
                      "endpoint_replaced": replaced},
                     occurred_at=now, actor_token_id=None, identities=identities)
        for invite in others:
            append_revocation_event(conn, offering_id=bot.offering_id, person=invite.person, role="student",
                                    origin=invite.origin, reason="refused", now=now, actor_token_id=None)
        return STUDENT_ENDPOINT_REPLACED if replaced else STUDENT_JOINED


def refuse_terms(conn: psycopg.Connection, *, bot_id: int, update_row_id: int, chat_id: int, now: datetime,
                 outbox: Any) -> str | None:
    """Um `/recuso` do chat privado `chat_id`, dirigido ao bot `bot_id`:
    devolve `None` sem candidato (a recepção responde `HELP`); com o oferecimento fora de `active` ou o
    candidato vencido, `LINK_INVALID`; senão `TERMS_REFUSED` ao chat do update. Nenhuma mudança e
    nenhum evento: o mesmo link continua valendo, sem prorrogar a validade."""
    with conn.transaction():
        bot = _lock_bot(conn, bot_id)
        presented = _presented_invites(conn, bot_id=bot_id, chat_id=chat_id, lock=False)
        if not presented:
            return None
        reply = {"bot_id": bot_id, "update_row_id": update_row_id}
        if bot.state != "active" or not now < presented[0].expires_at:
            return _refuse_invalid(conn, outbox, **reply)
        _reply(conn, outbox, offering_text(conn, "TERMS_REFUSED", bot_id=bot_id), **reply)
        return STUDENT_TERMS_REFUSED


def request_invite(conn: psycopg.Connection, *, bot_id: int, update_row_id: int, chat_id: int, argument: str,
                   now: datetime, outbox: Any) -> str:
    """Um `/convite <e-mail>` do chat privado `chat_id`, dirigido ao bot `bot_id`:
    a resposta `INVITE_REQUESTED` ao chat do update e o `outcome` `invite_requested`,
    **sempre**, qualquer que seja o caso. O envio `self_service` (validade padrão de 168 h) e a
    tarefa `invite.email` só são criados se todas as condições de `_self_service_enrollment` valem;
    nenhum evento no pedido (os eventos saem na tarefa). A trava do bot vem antes das consultas de
    limite e serializa os pedidos do bot. `argument` (o e-mail digitado) nunca é gravado nem
    registrado: só vai à consulta, por parâmetro."""
    with conn.transaction():
        bot = _lock_bot(conn, bot_id)
        enrollment_id = _self_service_enrollment(conn, bot, bot_id=bot_id, update_row_id=update_row_id,
                                                 chat_id=chat_id, argument=argument, now=now)
        if enrollment_id is not None:
            _queue_send(conn, enrollment_id, origin=ORIGIN_SELF_SERVICE, valid_hours=SELF_SERVICE_VALID_HOURS,
                        now=now)
        _reply(conn, outbox, offering_text(conn, "INVITE_REQUESTED", bot_id=bot_id), bot_id=bot_id,
               update_row_id=update_row_id)
    return INVITE_REQUESTED


def _self_service_enrollment(conn: psycopg.Connection, bot: _BotOffering, *, bot_id: int, update_row_id: int,
                             chat_id: int, argument: str, now: datetime) -> int | None:
    """A matrícula que recebe o envio por autosserviço (travada), ou `None` se alguma condição de
    autosserviço falha, nesta ordem: (1) o argumento é um e-mail válido pelas regras do cadastro; (2) os
    pedidos anteriores deste chat neste bot (`processed`, `outcome = invite_requested`, `received_at >
    agora − 24 h`, sem o atual) são menos de 3; (3) o chat não é endereço vigente de ninguém neste bot;
    (4) o oferecimento está `active`; (5) o e-mail normalizado é o de uma pessoa com matrícula de aluno
    `pending` neste oferecimento (leitura interna da identidade); (6) essa matrícula tem
    menos de 1 envio `self_service` com `created_at > agora − 1 h`. Os cortes de tempo vêm do `Clock`
    (`now`); contar numa janela não é ordenar."""
    try:
        email = people.validate_email(argument)
    except people.InvalidIdentity:
        return None
    previous = conn.execute(
        "SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND chat_id = %s AND id <> %s "
        "AND state = 'processed' AND outcome = %s AND received_at > %s",
        (bot_id, chat_id, update_row_id, INVITE_REQUESTED, now - REQUEST_WINDOW),
    ).fetchone()
    assert previous is not None
    if int(previous[0]) >= MAX_REQUESTS_PER_CHAT:
        return None
    # `delivery_endpoints.chat_id` é anulável (o expurgo o apaga); um endereço expurgado
    # (`NULL`) nunca casa com o chat do pedido.
    linked_chat = conn.execute(
        "SELECT 1 FROM delivery_endpoints WHERE bot_id = %s AND chat_id = %s AND channel = 'telegram' "
        "AND state IN ('active', 'blocked')",
        (bot_id, chat_id),
    ).fetchone()
    if linked_chat is not None or bot.state != "active":
        return None
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    found = conn.execute(
        "SELECT e.id FROM identities i JOIN enrollments e ON e.person_id = i.person_id "
        "WHERE i.email_normalized = %s AND e.offering_id = %s AND e.role = 'student' AND e.state = 'pending' "
        "FOR NO KEY UPDATE OF e",
        (email.normalized, bot.row),
    ).fetchone()
    if found is None:
        return None
    enrollment_id = int(found[0])
    recent = conn.execute(
        "SELECT count(*) FROM invite_sends WHERE enrollment_id = %s AND origin = %s AND created_at > %s",
        (enrollment_id, ORIGIN_SELF_SERVICE, now - SELF_SERVICE_WINDOW),
    ).fetchone()
    assert recent is not None
    return enrollment_id if int(recent[0]) < MAX_SELF_SERVICE_SENDS else None
