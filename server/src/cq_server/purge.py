"""Expurgo das identidades de um oferecimento arquivado.


Núcleo sem HTTP e sem adaptadores. O expurgo tem duas fases, como o arquivamento do oferecimento:

- `prepare_purge` confere o estado (só `archived` e ainda não expurgado) e o trabalho pendente,
  calcula o impacto (só contagens) e grava o resumo de um código de confirmação válido por
  `PURGE_CODE_TTL`, com o evento `offering.purge_prepared`;
- `confirm_purge` confere o código, recalcula as contagens (diferentes → `PurgeChanged`) e, numa só
  transação, apaga e redige por tabela, na ordem de `FAULT_STEPS`, gravando
  `offering.purged`. Repetida num oferecimento já expurgado, devolve o resultado gravado, sem evento.

O SQL sobre `identities` fica em `cq_server.people` (`purge_scope_rows`, `lock_identities`,
`delete_identities`): este módulo nunca cita a tabela em SQL. As regras voltam como resultados
sentinela (`PurgeBlocked`, `PurgeChanged`, `offerings.InvalidState`, `offerings.InvalidConfirmation`) e
`None` quer dizer "oferecimento inexistente".

O código de confirmação sai de `secrets.token_urlsafe(16)`, fica no banco só como SHA-256 e é comparado por
`hmac.compare_digest`; o vencimento é avaliado pelo `now` do chamador (o `Clock` da API).
"""

from __future__ import annotations

import dataclasses
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from cq_server import events, offerings, people, redaction, scheduler
from cq_server.deliverytexts import CONTACT_HOWTO
from cq_server.offerings import InvalidConfirmation, InvalidState

# Validade do código de confirmação e o texto que troca o contato do professor na
# fila de saída do bot.
PURGE_CODE_TTL = timedelta(minutes=10)
PURGE_CONTACT_PLACEHOLDER = "[contato expurgado]"

# Os passos da confirmação, na ordem; `_fault(step)` é chamado depois de cada um.
FAULT_STEPS = ("endpoints", "updates", "free_messages", "outbox", "identities", "contact", "mark")


# --------------------------------------------------------------------------------------------------
# Tipos de contrato
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeImpact:
    """O impacto mostrado na preparação: só contagens, nunca pessoas."""

    identities_to_delete: int
    identities_kept: int
    endpoints: int
    updates: int
    contact_email_set: bool


@dataclass(frozen=True)
class PurgePending:
    """O trabalho pendente que bloqueia o expurgo."""

    outbox: int
    updates: int
    tasks: int
    freeze: int
    due: int

    @property
    def total(self) -> int:
        return self.outbox + self.updates + self.tasks + self.freeze + self.due


@dataclass(frozen=True)
class PurgeOutcome:
    """O resultado gravado da confirmação."""

    identities_deleted: int
    identities_kept: int
    endpoints: int
    updates: int
    free_messages_redacted: int
    outbox_redacted: int
    contact_email_deleted: bool


@dataclass(frozen=True)
class PurgePreparation:
    """A preparação bem-sucedida. O `repr` nunca mostra o código de confirmação."""

    offering_id: uuid.UUID
    code: str
    impact: PurgeImpact
    confirmation_code: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (f"PurgePreparation(offering_id={self.offering_id!r}, code={self.code!r}, "
                f"impact={self.impact!r}, confirmation_code=<oculto>, "
                f"expires_at={self.expires_at!r})")


@dataclass(frozen=True)
class PurgeResult:
    """O oferecimento expurgado: `code` é o código do oferecimento."""

    offering_id: uuid.UUID
    code: str
    purged_at: datetime
    outcome: PurgeOutcome


@dataclass(frozen=True)
class PurgeBlocked:
    """Expurgo recusado por trabalho pendente (409 `purge_pending`, com `pending`)."""

    pending: PurgePending


@dataclass(frozen=True)
class PurgeChanged:
    """Confirmação recusada: as contagens mudaram desde a preparação (409 `purge_changed`)."""


def _fault(step: str) -> None:
    """Ponto de injeção de falha depois de cada passo de `FAULT_STEPS` (sem efeito; os testes o
    substituem por um que levanta `Crash`, para conferir que uma queda no meio não muda nada)."""


# --------------------------------------------------------------------------------------------------
# Leituras
# --------------------------------------------------------------------------------------------------


def purge_scope(conn: psycopg.Connection, offering_row_id: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(a apagar, mantidas), em ordem de `person_id` (delega a
    `people.purge_scope_rows`)."""
    rows = people.purge_scope_rows(conn, offering_row_id)
    to_delete = tuple(person_id for person_id, kept in rows if not kept)
    kept_ids = tuple(person_id for person_id, kept in rows if kept)
    return to_delete, kept_ids


# As contagens do bot (de todos os bots do oferecimento) e do contato, numa instrução.
_IMPACT_SQL = (
    "SELECT "
    "(SELECT count(*) FROM delivery_endpoints x JOIN bots b ON b.id = x.bot_id "
    " WHERE b.offering_id = %(o)s AND x.chat_id IS NOT NULL), "
    "(SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
    " WHERE b.offering_id = %(o)s "
    " AND (u.chat_id IS NOT NULL OR u.sender_id IS NOT NULL OR u.text IS NOT NULL)), "
    "EXISTS (SELECT 1 FROM offering_contacts c WHERE c.offering_id = %(o)s)"
)


def purge_impact(conn: psycopg.Connection, offering_row_id: int) -> PurgeImpact:
    """As contagens do impacto do expurgo (leitura sem trava)."""
    to_delete, kept = purge_scope(conn, offering_row_id)
    return _impact_of_scope(conn, offering_row_id, to_delete, kept)


def _impact_of_scope(conn: psycopg.Connection, offering_row_id: int, to_delete: tuple[int, ...],
                     kept: tuple[int, ...]) -> PurgeImpact:
    """`purge_impact` com o alcance já lido (a recontagem trava e apaga com o mesmo alcance que contou)."""
    row = conn.execute(_IMPACT_SQL, {"o": offering_row_id}).fetchone()
    assert row is not None
    endpoints, updates, contact_email_set = row
    return PurgeImpact(identities_to_delete=len(to_delete), identities_kept=len(kept),
                       endpoints=int(endpoints), updates=int(updates),
                       contact_email_set=bool(contact_email_set))


# O trabalho pendente: as tarefas vivas cujo assunto é do oferecimento, por chave do `payload`, cada
# uma com o `JOIN` até `offering_id`. `{v}` é o id do `payload` como `bigint`, só quando é número (nunca um
# cast que falhe numa chave de outro formato). Fora o `question.refetch` (`question_refs` é compartilhada e
# a tarefa não toca identidade).
_TASK_SUBJECTS: tuple[tuple[str, str], ...] = (
    ("dispatch_row_id", "JOIN dispatches s ON s.id = {v} WHERE s.offering_id = %(o)s"),
    ("delivery_row_id", "JOIN deliveries d ON d.id = {v} JOIN dispatches s ON s.id = d.dispatch_id "
                        "WHERE s.offering_id = %(o)s"),
    ("submission_row_id", "JOIN submissions g ON g.id = {v} JOIN deliveries d ON d.id = g.delivery_id "
                          "JOIN dispatches s ON s.id = d.dispatch_id WHERE s.offering_id = %(o)s"),
    ("update_row_id", "JOIN telegram_updates u ON u.id = {v} JOIN bots b ON b.id = u.bot_id "
                      "WHERE b.offering_id = %(o)s"),
    ("notice_row_id", "JOIN team_notices n ON n.id = {v} WHERE n.offering_id = %(o)s"),
    ("batch_row_id", "JOIN invite_batches x ON x.id = {v} WHERE x.offering_id = %(o)s"),
    ("preview_row_id", "JOIN dispatch_previews w ON w.id = {v} JOIN dispatches s ON s.id = w.dispatch_id "
                       "WHERE s.offering_id = %(o)s"),
    ("digest_row_id", "JOIN offering_digests g ON g.id = {v} WHERE g.offering_id = %(o)s"),
    ("offering_row_id", "WHERE {v} = %(o)s"),
    ("invite_send_id", "JOIN invite_sends x ON x.id = {v} JOIN enrollments e ON e.id = x.enrollment_id "
                       "WHERE e.offering_id = %(o)s"),
    ("bot_id", "JOIN bots b ON b.id = {v} WHERE b.offering_id = %(o)s"),
)
_REFETCH_TASK_TYPE = "question.refetch"


def _payload_id(key: str) -> str:
    """O id do assunto no `payload` como `bigint`, só quando é número (`key` é constante deste módulo)."""
    return f"(CASE WHEN jsonb_typeof(t.payload->'{key}') = 'number' THEN (t.payload->>'{key}')::bigint END)"


def _live_tasks_sql() -> str:
    """A contagem das tarefas vivas do oferecimento: uma `UNION ALL` por chave, cada tarefa contada uma vez
    (`DISTINCT`) mesmo com mais de uma chave do oferecimento no `payload`."""
    branches = " UNION ALL ".join(
        f"SELECT t.id FROM tasks t {template.format(v=_payload_id(key))} "
        f"AND t.state IN ('pending', 'leased') AND t.task_type <> '{_REFETCH_TASK_TYPE}'"
        for key, template in _TASK_SUBJECTS
    )
    return f"(SELECT count(DISTINCT k.id) FROM ({branches}) AS k)"


# O trabalho que a varredura ainda criaria: as condições do varredor,
# reusadas das constantes do `scheduler` (sem duplicar o SQL), cada uma com a tabela e os apelidos da sua
# regra e o filtro do oferecimento. "Sem o prazo" sai de `now = 'infinity'` (todo `… <= %(now)s` vale).
_FREEZE_SOURCE = ("dispatches s", "s.offering_id", scheduler._FREEZE_DUE)
_DUE_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("deliveries s JOIN dispatches p ON p.id = s.dispatch_id", "p.offering_id", scheduler._REVEAL_DUE),
    # A regra `notice.send` (`REPORT_RULES`): o aviso `pending`.
    ("team_notices s", "s.offering_id", "s.state = 'pending'"),
    ("dispatches s", "s.offering_id", scheduler._NOTICE_REJECTED_DUE),
    ("dispatches s", "s.offering_id", scheduler._NOTICE_CANCELLED_DUE),
    ("dispatches s", "s.offering_id", scheduler._NOTICE_QUESTION_FAILED_DUE),
    ("offerings s", "s.id", scheduler._NOTICE_ACCESS_EPISODE_DUE),
    ("invite_batches s", "s.offering_id", scheduler._NOTICE_INVITE_BATCH_DUE),
    ("offerings s", "s.id", scheduler._NOTICE_INVITE_WINDOW_DUE),
)


def _subject_count(source: str, column: str, condition: str) -> str:
    return f"(SELECT count(*) FROM {source} WHERE {column} = %(o)s AND ({condition}))"


_PENDING_SQL = (
    "SELECT "
    "(SELECT count(*) FROM outbox_messages m JOIN bots b ON b.id = m.bot_id "
    " WHERE b.offering_id = %(o)s AND m.state IN ('pending', 'leased')), "
    "(SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
    " WHERE b.offering_id = %(o)s AND u.state = 'pending'), "
    f"{_live_tasks_sql()}, "
    f"{_subject_count(*_FREEZE_SOURCE)}, "
    f"({' + '.join(_subject_count(*source) for source in _DUE_SOURCES)})"
)


def purge_pending(conn: psycopg.Connection, offering_row_id: int) -> PurgePending:
    """O trabalho pendente do oferecimento, numa só instrução (leitura sem trava)."""
    row = conn.execute(_PENDING_SQL, {"o": offering_row_id, "now": "infinity"}).fetchone()
    assert row is not None
    outbox, updates, tasks, freeze, due = (int(value) for value in row)
    return PurgePending(outbox=outbox, updates=updates, tasks=tasks, freeze=freeze, due=due)


# --------------------------------------------------------------------------------------------------
# Preparação e confirmação
# --------------------------------------------------------------------------------------------------

# A redação do contato: o prefixo fixo da frase do contato (`deliverytexts.CONTACT_HOWTO`, sempre
# a última do texto) e a frase com o contato trocado pelo marcador.
_CONTACT_PREFIX = CONTACT_HOWTO.split("{email}", 1)[0]
_CONTACT_REPLACEMENT = CONTACT_HOWTO.format(email=PURGE_CONTACT_PLACEHOLDER)


@dataclass(frozen=True)
class _PurgeColumns:
    """As colunas do expurgo da linha já travada do oferecimento (`lock_offering` não as lê)."""

    purged_at: datetime | None
    purge_result: dict | None
    code_sha256: str | None
    code_expires_at: datetime | None
    prepared_counts: dict | None


def _purge_columns(conn: psycopg.Connection, offering_row_id: int) -> _PurgeColumns:
    row = conn.execute(
        "SELECT purged_at, purge_result, purge_code_sha256, purge_code_expires_at, purge_prepared_counts "
        "FROM offerings WHERE id = %s",
        (offering_row_id,),
    ).fetchone()
    assert row is not None
    return _PurgeColumns(purged_at=row[0], purge_result=row[1], code_sha256=row[2], code_expires_at=row[3],
                         prepared_counts=row[4])


def _stored_result(row: offerings.OfferingRow, columns: _PurgeColumns) -> PurgeResult:
    """O `PurgeResult` gravado."""
    assert columns.purged_at is not None and columns.purge_result is not None
    return PurgeResult(offering_id=row.offering_id, code=row.code, purged_at=columns.purged_at,
                       outcome=PurgeOutcome(**columns.purge_result))


def _code_matches(columns: _PurgeColumns, confirmation_code: str, now: datetime) -> bool:
    """O código confere com o resumo pendente e ainda vale (`agora < expires_at`)."""
    if columns.code_sha256 is None or columns.code_expires_at is None:
        return False
    if not now < columns.code_expires_at:
        return False
    return hmac.compare_digest(offerings.code_digest(confirmation_code), columns.code_sha256)


def _blocked(conn: psycopg.Connection, offering_row_id: int) -> PurgeBlocked | None:
    """`PurgeBlocked` com as contagens por classe quando há trabalho pendente."""
    pending = purge_pending(conn, offering_row_id)
    return PurgeBlocked(pending) if pending.total > 0 else None


def prepare_purge(conn: psycopg.Connection, ref: str, *, now: datetime,
                  actor_token_id: str | None) -> PurgePreparation | InvalidState | PurgeBlocked | None:
    """Prepara o expurgo; grava `offering.purge_prepared`.

    Travas: o oferecimento (`FOR NO KEY UPDATE`) → leituras sem trava → `UPDATE offerings` → lock dos
    eventos (no `append_event`). Preparar de novo substitui o código anterior. O código em claro só sai no
    retorno: no banco fica o SHA-256, e o evento não leva nem um nem outro."""
    with conn.transaction():
        row = offerings.lock_offering(conn, ref)
        if row is None:
            return None
        columns = _purge_columns(conn, row.id)
        if row.state != "archived" or columns.purged_at is not None:
            return InvalidState(row.state)
        blocked = _blocked(conn, row.id)
        if blocked is not None:
            return blocked
        impact = purge_impact(conn, row.id)
        confirmation_code = secrets.token_urlsafe(16)
        expires_at = now + PURGE_CODE_TTL
        conn.execute(
            "UPDATE offerings SET purge_code_sha256 = %s, purge_code_expires_at = %s, purge_prepared_counts = %s "
            "WHERE id = %s",
            (offerings.code_digest(confirmation_code), expires_at, Jsonb(dataclasses.asdict(impact)), row.id),
        )
        events.append_event(conn, events.EventType.OFFERING_PURGE_PREPARED,
                            {"offering_id": row.offering_id, "expires_at": expires_at,
                             **dataclasses.asdict(impact)},
                            occurred_at=now, actor_token_id=actor_token_id)
    return PurgePreparation(offering_id=row.offering_id, code=row.code, impact=impact,
                            confirmation_code=confirmation_code, expires_at=expires_at)


def _redact_free_messages(conn: psycopg.Connection, identities: list[redaction.KnownIdentity]) -> int:
    """Passo 3 (`free_messages`): as mensagens livres de **todos** os oferecimentos, em ordem de `id`, redigidas de novo
    com todas as identidades de antes do apagamento; grava só as que mudam e devolve quantas."""
    changed = 0
    for message_id, text in conn.execute("SELECT id, text FROM free_messages ORDER BY id").fetchall():
        redacted = redaction.redact(text, identities)
        if redacted != text:
            conn.execute("UPDATE free_messages SET text = %s WHERE id = %s", (redacted, int(message_id)))
            changed += 1
    return changed


def _without_contact(text: str, email: str | None) -> str:
    """Passo 4 (`outbox`): (a) o e-mail vigente vira o marcador em qualquer posição; (b) da frase do contato (o
    prefixo fixo de `CONTACT_HOWTO`) ao fim do texto, a frase com o marcador."""
    out = text.replace(email, PURGE_CONTACT_PLACEHOLDER) if email else text
    at = out.find(_CONTACT_PREFIX)
    if at >= 0:
        out = out[:at] + _CONTACT_REPLACEMENT
    return out


def _redact_outbox(conn: psycopg.Connection, bot_ids: list[int], email: str | None) -> int:
    """Passo 4 (`outbox`): as linhas da fila dos bots do oferecimento que citam o contato, em ordem de `id`; grava só as
    que mudam e devolve quantas."""
    if not bot_ids:
        return 0
    rows = conn.execute(
        "SELECT id, text FROM outbox_messages WHERE bot_id = ANY(%(bots)s) "
        "AND (strpos(text, %(prefix)s) > 0 OR (%(email)s::text IS NOT NULL AND strpos(text, %(email)s) > 0)) "
        "ORDER BY id",
        {"bots": bot_ids, "prefix": _CONTACT_PREFIX, "email": email},
    ).fetchall()
    changed = 0
    for message_id, text in rows:
        rewritten = _without_contact(text, email)
        if rewritten != text:
            conn.execute("UPDATE outbox_messages SET text = %s WHERE id = %s", (rewritten, int(message_id)))
            changed += 1
    return changed


def confirm_purge(conn: psycopg.Connection, ref: str, *, confirmation_code: str, now: datetime,
                  actor_token_id: str | None,
                  ) -> PurgeResult | InvalidState | PurgeBlocked | InvalidConfirmation | PurgeChanged | None:
    """Confirma o expurgo; grava `offering.purged` uma vez. Já expurgado →
    o `PurgeResult` gravado, sem evento.

    Tudo numa transação (uma queda no meio não muda nada). Travas: o oferecimento → o contato →
    os bots → o lock dos eventos (`load_write_identities`, que traz as identidades da nova redação) →
    recontagem → as identidades afetadas (`people.lock_identities`) → os apagamentos por tabela, com `_fault`
    depois de cada passo de `FAULT_STEPS`. Recusas (pendente, código, recontagem) não mudam nada, e o código
    guardado continua valendo até vencer."""
    with conn.transaction():
        row = offerings.lock_offering(conn, ref)
        if row is None:
            return None
        columns = _purge_columns(conn, row.id)
        if columns.purged_at is not None:
            return _stored_result(row, columns)
        if row.state != "archived":
            return InvalidState(row.state)
        blocked = _blocked(conn, row.id)
        if blocked is not None:
            return blocked
        if not _code_matches(columns, confirmation_code, now):
            return InvalidConfirmation()

        # As travas de linha antes do lock dos eventos. `offering_contacts` não é referenciada por FK (`FOR
        # UPDATE` não barra nenhuma checagem de FK); os bots, sim (`FOR NO KEY UPDATE`).
        contact = conn.execute(
            "SELECT email FROM offering_contacts WHERE offering_id = %s FOR UPDATE", (row.id,)).fetchone()
        email = None if contact is None else str(contact[0])
        bot_ids = [int(bot[0]) for bot in conn.execute(
            "SELECT id FROM bots WHERE offering_id = %s ORDER BY id FOR NO KEY UPDATE", (row.id,)).fetchall()]
        identities = events.load_write_identities(conn)

        # Recontagem sob o lock dos eventos: qualquer diferença recusa, sem mudança.
        to_delete, kept = purge_scope(conn, row.id)
        impact = _impact_of_scope(conn, row.id, to_delete, kept)
        if dataclasses.asdict(impact) != columns.prepared_counts:
            return PurgeChanged()
        people.lock_identities(conn, sorted(to_delete + kept))

        endpoints = updates = 0
        if bot_ids:
            endpoints = conn.execute(
                "UPDATE delivery_endpoints SET chat_id = NULL WHERE bot_id = ANY(%s) AND chat_id IS NOT NULL",
                (bot_ids,)).rowcount
        _fault("endpoints")
        if bot_ids:
            updates = conn.execute(
                "UPDATE telegram_updates SET chat_id = NULL, sender_id = NULL, text = NULL WHERE bot_id = ANY(%s) "
                "AND (chat_id IS NOT NULL OR sender_id IS NOT NULL OR text IS NOT NULL)",
                (bot_ids,)).rowcount
        _fault("updates")
        free_messages_redacted = _redact_free_messages(conn, identities)
        _fault("free_messages")
        outbox_redacted = _redact_outbox(conn, bot_ids, email)
        _fault("outbox")
        identities_deleted = people.delete_identities(conn, to_delete)
        _fault("identities")
        contact_email_deleted = conn.execute(
            "DELETE FROM offering_contacts WHERE offering_id = %s", (row.id,)).rowcount > 0
        _fault("contact")

        outcome = PurgeOutcome(identities_deleted=identities_deleted, identities_kept=len(kept),
                               endpoints=endpoints, updates=updates,
                               free_messages_redacted=free_messages_redacted, outbox_redacted=outbox_redacted,
                               contact_email_deleted=contact_email_deleted)
        conn.execute(
            "UPDATE offerings SET purged_at = %s, purge_result = %s, purge_code_sha256 = NULL, "
            "purge_code_expires_at = NULL, purge_prepared_counts = NULL WHERE id = %s",
            (now, Jsonb(dataclasses.asdict(outcome)), row.id),
        )
        _fault("mark")
        events.append_event(conn, events.EventType.OFFERING_PURGED,
                            {"offering_id": row.offering_id, **dataclasses.asdict(outcome)},
                            occurred_at=now, actor_token_id=actor_token_id, identities=identities)
    return PurgeResult(offering_id=row.offering_id, code=row.code, purged_at=now, outcome=outcome)
