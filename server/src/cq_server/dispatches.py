"""Disparos: preparo, validação, confirmação, expiração, cancelamento, abertura, fechamento e consulta
(um disparo põe uma questão para os alunos de um oferecimento, numa janela de horário).
O motor só entra pelos tratadores; as operações nunca o chamam.

- **Operações** (na transação do chamador): `prepare_dispatch` (nunca chama o motor),
  `get_dispatch`, `list_dispatches`, `confirm_dispatch` e `cancel_dispatch`. Os resultados de regra
  voltam como sentinelas: `InvalidState` e `BotRequired` (os de `offerings`),
  `DispatchStateConflict(state)` (409 `invalid_dispatch_state`) e `WindowExpired` (409
  `window_expired`); `None` quer dizer "oferecimento (ou disparo) inexistente".
- **Tratadores** (objetos chamáveis com `close()`, conexão aberta no primeiro uso, como o
  `invite.email`): `dispatch.validate` (um passo externo por execução: obtenção → inspeção →
  variante de amostra; o passo sai do estado gravado), `dispatch.expire`, `dispatch.open` e
  `dispatch.close`. O `channel` é o canal com rota do worker; é lido só ao processar uma tarefa.
- **Oferecimento:** `cancel_for_offering_close` (encerramento), `close_for_archive` e
  `archive_blockers` (arquivamento). `offerings` importa este módulo **dentro** das
  funções (importação tardia), para não formar ciclo.
- `reject_validation_exhausted`: o fim das rodadas da validação pela varredura.

Todo horário vem do `Clock` do chamador (`now`), em UTC; toda transição é `UPDATE … WHERE id = %s AND
state = ANY(%s)`. Importa `questionrefs` e `deliveries`, nunca `answers`, `grading`,
`reception` nem `worker`; `deliveries` dá o tipo da tarefa de preparo da entrega, e a fila de saída
(`outbox`) só entra no fechamento (a revelação e o cancelamento das mensagens da entrega).

Fechamento (compartilhado com o arquivamento): `_close_locked` faz a transição de fechamento
(entregas, revelação pela fila e `dispatch.closed` por último) com o disparo já travado.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server import offerings, questionrefs, tasks
from cq_server._leasing import LazyConnection, utc
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.deliveries import PREPARE_TASK_TYPE, choice_has_buttons
from cq_server.events import EventType, append_event, load_known_identities, load_write_identities
from cq_server.offerings import BotRequired, InvalidState
from cq_server.ports.delivery import ChannelCapabilities, DeliveryChannel, MessageFormatter
from cq_server.ports.engine import (
    Broken,
    EngineNotFound,
    EngineRequestRejected,
    EngineUnavailable,
    QuestionEngine,
    QuestionRef,
    Unsupported,
    Variant,
)
from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure, RepoFetcher
from cq_server.redaction import KnownIdentity, redact, redact_operator_text
from cq_server.tasks import ClaimedTask, PermanentTaskError

__all__ = [
    "BotRequired", "CancelReason", "Dispatch", "DispatchRequest", "DispatchState", "DispatchStateConflict",
    "DispatchSummary", "DispatchesPage", "InvalidState", "Origin", "RejectionClass", "WindowExpired",
]

# Tipos de tarefa (payload `{"dispatch_row_id": n}`).
VALIDATE_TASK_TYPE = "dispatch.validate"
EXPIRE_TASK_TYPE = "dispatch.expire"
OPEN_TASK_TYPE = "dispatch.open"
CLOSE_TASK_TYPE = "dispatch.close"

# Atraso tolerado na abertura antes do `window_missed`.
OPEN_GRACE_SECONDS = 60
# Janela de 5 minutos a 30 dias; abertura até 90 dias no futuro.
MIN_WINDOW_MINUTES = 5
MAX_WINDOW_MINUTES = 43_200
MAX_OPENS_AHEAD_DAYS = 90


class DispatchState(StrEnum):
    """Estado do disparo; o "validado" é `scheduled` sem confirmação."""

    VALIDATING = "validating"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Origin(StrEnum):
    """Origem declarada do disparo."""

    CLI = "cli"
    API = "api"
    MCP = "mcp"
    INTEGRATION = "integration"


class RejectionClass(StrEnum):
    """Classe da rejeição."""

    REPOSITORY_UNREACHABLE = "repository_unreachable"
    REPOSITORY_REFUSED = "repository_refused"
    COMMIT_NOT_FOUND = "commit_not_found"
    QUESTION_NOT_FOUND = "question_not_found"
    QUESTION_UNSUPPORTED = "question_unsupported"
    QUESTION_BROKEN = "question_broken"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    CHANNEL_LIMITS_EXCEEDED = "channel_limits_exceeded"
    INTERNAL_FAILURE = "internal_failure"


class CancelReason(StrEnum):
    """Motivo do cancelamento."""

    OPERATOR = "operator"
    EXPIRED = "expired"
    OFFERING_CLOSED = "offering_closed"
    WINDOW_MISSED = "window_missed"
    VALIDATION_OVERRAN = "validation_overran"


# As transições permitidas (de → para).
DISPATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "validating": frozenset({"rejected", "scheduled", "cancelled"}),
    "scheduled": frozenset({"open", "cancelled"}),
    "open": frozenset({"closed"}),
}
# Os estados em que o disparo ainda anda (`dispatches_live_idx`).
LIVE_STATES = frozenset({"validating", "scheduled", "open"})

# Avisos da prévia (os três primeiros vêm do motor; o último, da configuração de
# mensagens livres do oferecimento).
PREVIEW_WARNINGS = ("console_output", "missing_media", "media_error", "empty_target", "too_many_options",
                    "repeated_question", "no_teacher_contact")


# --------------------------------------------------------------------------------------------------
# Pedido, vista e sentinelas
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchRequest:
    """O pedido de preparo já validado; horários em UTC."""

    repo: str
    commit: str
    qid: str
    opens_at: datetime | None
    closes_at: datetime | None
    origin: str


@dataclass(frozen=True)
class DispatchRejection:
    """A rejeição: a classe e os motivos do motor, redigidos."""

    reason: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryCounts:
    """Contagens das entregas por estado e a marca `grading_pending`."""

    pending: int
    delivered: int
    not_delivered: int
    answered: int
    dont_know: int
    no_answer: int
    answered_late: int
    question_failed: int
    grading_pending: int


@dataclass(frozen=True)
class DispatchSummary:
    """Um item de `dispatches.list` (a vista sem o oferecimento e sem a prévia)."""

    dispatch_id: uuid.UUID
    question_ref_id: uuid.UUID
    repo: str
    commit: str
    qid: str
    state: str
    origin: str
    confirmed_at: datetime | None
    opens_at: datetime | None
    closes_at: datetime | None
    created_at: datetime
    validated_at: datetime | None
    rejected_at: datetime | None
    opened_at: datetime | None
    closed_at: datetime | None
    cancelled_at: datetime | None
    rejection: DispatchRejection | None
    cancel_reason: str | None
    target_count: int | None
    deliveries: DeliveryCounts


@dataclass(frozen=True)
class Dispatch:
    """A vista de um disparo na API: o oferecimento, o fuso, o resumo e a
    prévia (redigida; `None` até a validação e no rejeitado)."""

    offering_id: uuid.UUID
    code: str
    timezone: str
    summary: DispatchSummary
    preview: Mapping[str, Any] | None


@dataclass(frozen=True)
class DispatchesPage:
    """Uma página de `dispatches.list` (paginação por `id`)."""

    offering_id: uuid.UUID
    code: str
    timezone: str
    items: tuple[DispatchSummary, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class DispatchStateConflict:
    """409 `invalid_dispatch_state`: a operação não vale no estado atual do disparo."""

    state: str


@dataclass(frozen=True)
class WindowExpired:
    """409 `window_expired`: a janela efetiva do disparo não é mais válida."""


# --------------------------------------------------------------------------------------------------
# Textos e limites fixos
# --------------------------------------------------------------------------------------------------

# A mensagem pareada de cada classe de rejeição.
REJECTION_MESSAGES: dict[str, str] = {
    "repository_unreachable": "Não foi possível obter o repositório da questão.",
    "repository_refused": "O repositório foi recusado pela política de obtenção.",
    "commit_not_found": "O commit informado não existe no repositório.",
    "question_not_found": "A questão não existe nesse commit.",
    "question_unsupported": "A questão usa recursos que o sistema não suporta.",
    "question_broken": "A questão falhou ao gerar uma variante.",
    "engine_unavailable": "O motor de questões ficou indisponível durante a validação.",
    "channel_limits_exceeded": "A questão não cabe nos limites do canal.",
    "internal_failure": "Falha interna na validação.",
}

# As mensagens dos avisos do núcleo (as do motor vêm dele, redigidas). [I] Os textos
# não estão fixados no contrato; nenhum leva detalhe técnico.
CORE_WARNING_MESSAGES: dict[str, str] = {
    "empty_target": "Nenhum aluno vinculado recebe esta questão agora.",
    "too_many_options": ("As alternativas não cabem nos botões do canal; os alunos vão responder digitando "
                         "a letra da alternativa."),
    "repeated_question": "Esta questão já foi disparada neste oferecimento.",
    # Texto fixo.
    "no_teacher_contact": ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                           "o aluno que escrever fora das questões não saberá a quem se dirigir."),
}
ENGINE_WARNING_KINDS = ("console_output", "missing_media", "media_error")

# O prazo de confirmação do imediato.
IMMEDIATE_CONFIRM = timedelta(hours=24)
MIN_WINDOW = timedelta(minutes=MIN_WINDOW_MINUTES)
MAX_WINDOW = timedelta(minutes=MAX_WINDOW_MINUTES)
OPEN_GRACE = timedelta(seconds=OPEN_GRACE_SECONDS)

# A mensagem fixa do tratador sem canal com rota (sem texto de terceiros).
NO_ROUTE_CHANNEL = "canal sem rota"

# A seed do motor (< 2³²) e o `button_token` (16 bytes em base64url).
SEED_LIMIT = 2**32
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"

# Estados das entregas, na ordem das contagens.
DELIVERY_STATES = ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                   "answered_late", "question_failed")
# As entregas que recebem a revelação no fechamento (o gabarito permitido e
# `revealed_at`) e as que recebem a edição de fechamento (as reveladas e a `no_answer`, sem gabarito).
_REVEALED_STATES = ("answered", "dont_know")
_CLOSE_EDIT_STATES = ("answered", "dont_know", "no_answer")


def base36(value: int) -> str:
    """A seed canônica do PL: base 36 minúscula, sem zeros à esquerda."""
    if value < 0:
        raise ValueError("seed negativa")
    out = ""
    while True:
        value, rest = divmod(value, 36)
        out = _BASE36[rest] + out
        if value == 0:
            return out


# --------------------------------------------------------------------------------------------------
# Leitura (a linha do disparo, o oferecimento, o bot, as contagens)
# --------------------------------------------------------------------------------------------------

_COLUMNS = (
    "d.id, d.dispatch_uuid, d.offering_id, d.origin, d.immediate, d.opens_at, d.closes_at, d.window_minutes, "
    "d.state, d.preview, d.rejection, d.rejection_reasons, d.cancel_reason, d.created_at, d.validated_at, "
    "d.confirm_deadline, d.confirmed_at, d.opened_at, d.closed_at, d.rejected_at, d.cancelled_at, "
    "d.question_ref_id, q.ref_uuid, q.repo, q.commit_sha, q.qid")


@dataclass(frozen=True)
class _Row:
    """A linha de um disparo com a referência (uso interno)."""

    id: int
    dispatch_uuid: uuid.UUID
    offering_row: int
    origin: str
    immediate: bool
    opens_at: datetime | None
    closes_at: datetime | None
    window_minutes: int | None
    state: str
    preview: Mapping[str, Any] | None
    rejection: str | None
    rejection_reasons: list[Any] | None
    cancel_reason: str | None
    created_at: datetime
    validated_at: datetime | None
    confirm_deadline: datetime | None
    confirmed_at: datetime | None
    opened_at: datetime | None
    closed_at: datetime | None
    rejected_at: datetime | None
    cancelled_at: datetime | None
    ref_row: int
    ref_uuid: uuid.UUID
    repo: str
    commit: str
    qid: str


def _as_row(values: Sequence[Any]) -> _Row:
    v = list(values)
    for index in (5, 6, 13, 14, 15, 16, 17, 18, 19, 20):
        v[index] = utc(v[index])
    return _Row(id=int(v[0]), dispatch_uuid=v[1], offering_row=int(v[2]), origin=v[3], immediate=bool(v[4]),
                opens_at=v[5], closes_at=v[6], window_minutes=v[7], state=v[8], preview=v[9], rejection=v[10],
                rejection_reasons=v[11], cancel_reason=v[12], created_at=v[13], validated_at=v[14],
                confirm_deadline=v[15], confirmed_at=v[16], opened_at=v[17], closed_at=v[18], rejected_at=v[19],
                cancelled_at=v[20], ref_row=int(v[21]), ref_uuid=v[22], repo=v[23], commit=v[24], qid=v[25])


def _select(conn: psycopg.Connection, where: str, params: Sequence[Any], *, lock: bool = False) -> list[_Row]:
    sql = (f"SELECT {_COLUMNS} FROM dispatches d JOIN question_refs q ON q.id = d.question_ref_id "
           f"WHERE {where}")
    if lock:
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        sql += " FOR NO KEY UPDATE OF d"
    return [_as_row(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def _one(conn: psycopg.Connection, where: str, params: Sequence[Any], *, lock: bool = False) -> _Row | None:
    rows = _select(conn, where, params, lock=lock)
    return rows[0] if rows else None


@dataclass(frozen=True)
class _Offering:
    """O oferecimento de um disparo: o id interno, o público, o código, o fuso, o estado e a janela."""

    id: int
    offering_id: uuid.UUID
    code: str
    timezone: str
    state: str
    response_window_minutes: int


def _offering_of(conn: psycopg.Connection, where: str, param: Any, *, lock: str = "") -> _Offering | None:
    row = conn.execute(
        "SELECT o.id, o.offering_id, o.code, o.timezone, o.state, o.response_window_minutes FROM offerings o "
        f"WHERE {where} {lock}", (param,)).fetchone()
    if row is None:
        return None
    return _Offering(id=int(row[0]), offering_id=row[1], code=row[2], timezone=row[3], state=row[4],
                     response_window_minutes=int(row[5]))


def _offering_by_ref(conn: psycopg.Connection, ref: str, *, lock: str = "") -> _Offering | None:
    if offerings.is_offering_id(ref):
        return _offering_of(conn, "o.offering_id = %s", uuid.UUID(ref), lock=lock)
    if offerings.is_code(ref):
        return _offering_of(conn, "o.code = %s", ref, lock=lock)
    raise ValueError("referência de oferecimento fora do formato")


def _offering_by_id(conn: psycopg.Connection, offering_row: int, *, lock: str = "") -> _Offering:
    offering = _offering_of(conn, "o.id = %s", offering_row, lock=lock)
    if offering is None:  # pragma: no cover - FK
        raise LookupError("oferecimento inexistente")
    return offering


def _bot_of(conn: psycopg.Connection, offering_row: int, *, lock: str = "") -> tuple[int, str] | None:
    """(id interno, estado) do bot do oferecimento, ou `None`."""
    row = conn.execute(f"SELECT id, state FROM bots WHERE offering_id = %s {lock}", (offering_row,)).fetchone()
    return None if row is None else (int(row[0]), row[1])


def _current_target(conn: psycopg.Connection, offering_row: int) -> int:
    """O alvo **agora**: matrículas `student` `active` do oferecimento com
    endereço vigente (`active` ou `blocked`) no bot dele."""
    row = conn.execute(
        "SELECT count(DISTINCT e.id) FROM enrollments e "
        "JOIN bots b ON b.offering_id = e.offering_id "
        "JOIN delivery_endpoints de ON de.person_id = e.person_id AND de.bot_id = b.id "
        "  AND de.state IN ('active', 'blocked') "
        "WHERE e.offering_id = %s AND e.role = 'student' AND e.state = 'active'",
        (offering_row,)).fetchone()
    return int(row[0])


def _counts(conn: psycopg.Connection, dispatch_row: int) -> DeliveryCounts:
    """As contagens das entregas por estado e das correções pendentes."""
    by_state = dict(conn.execute(
        "SELECT state, count(*) FROM deliveries WHERE dispatch_id = %s GROUP BY state", (dispatch_row,)).fetchall())
    grading = conn.execute(
        "SELECT count(DISTINCT s.delivery_id) FROM submissions s JOIN deliveries x ON x.id = s.delivery_id "
        "WHERE x.dispatch_id = %s AND s.outcome = 'grading'", (dispatch_row,)).fetchone()[0]
    values = {state: int(by_state.get(state, 0)) for state in DELIVERY_STATES}
    return DeliveryCounts(**values, grading_pending=int(grading))


def _state_counts(conn: psycopg.Connection, dispatch_row: int) -> dict[str, int]:
    """As 8 contagens por estado (os `counts` do `dispatch.closed`)."""
    counts = _counts(conn, dispatch_row)
    return {state: getattr(counts, state) for state in DELIVERY_STATES}


def _redact_text(value: Any, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> Any:
    return redact(value, identities, secrets=secrets) if isinstance(value, str) else value


def _redact_operator_text(value: str, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> str:
    """`repo` e `qid` na saída: a redação de sempre e, por serem caminhos
    (`https://host/<RA>/<parte local>.git`), também a parte local do e-mail de cada pessoa como alvo extra,
    com a mesma fronteira por classe do nome (`redaction.redact_operator_text`). Os valores gravados ficam
    literais (a chave da obtenção)."""
    return redact_operator_text(value, identities, secrets=secrets)


def _redact_preview(preview: Mapping[str, Any] | None, identities: Sequence[KnownIdentity],
                    secrets: Sequence[str | None]) -> dict[str, Any] | None:
    """A prévia com o texto de terceiros redigido (título, enunciado, rótulos, sufixos, dicas e as
    mensagens dos avisos); na gravação e de novo na saída."""
    if preview is None:
        return None
    return {
        "title": _redact_text(preview.get("title"), identities, secrets),
        "statement": _redact_text(preview.get("statement", ""), identities, secrets),
        "fields": [{"type": f.get("type"), "label": _redact_text(f.get("label"), identities, secrets),
                    "suffix": _redact_text(f.get("suffix"), identities, secrets),
                    "hint": _redact_text(f.get("hint", ""), identities, secrets)}
                   for f in preview.get("fields", [])],
        "target_count": int(preview.get("target_count", 0)),
        "warnings": [{"kind": w.get("kind"), "message": _redact_text(w.get("message", ""), identities, secrets)}
                     for w in preview.get("warnings", [])],
    }


def _summary(conn: psycopg.Connection, row: _Row, *, identities: Sequence[KnownIdentity],
             secrets: Sequence[str | None]) -> DispatchSummary:
    if row.state == "scheduled":
        target: int | None = _current_target(conn, row.offering_row)
    elif row.state in ("open", "closed"):
        target = int(conn.execute("SELECT count(*) FROM deliveries WHERE dispatch_id = %s",
                                  (row.id,)).fetchone()[0])
    else:
        target = None
    rejection = None
    if row.rejection is not None:
        reasons = row.rejection_reasons if isinstance(row.rejection_reasons, list) else []
        details = tuple(redact(str(item.get("message", "")), identities, secrets=secrets)
                        for item in reasons if isinstance(item, Mapping))
        rejection = DispatchRejection(reason=row.rejection, details=details)
    return DispatchSummary(
        dispatch_id=row.dispatch_uuid, question_ref_id=row.ref_uuid,
        repo=_redact_operator_text(row.repo, identities, secrets), commit=row.commit,
        qid=_redact_operator_text(row.qid, identities, secrets), state=row.state, origin=row.origin,
        confirmed_at=row.confirmed_at, opens_at=row.opens_at, closes_at=row.closes_at, created_at=row.created_at,
        validated_at=row.validated_at, rejected_at=row.rejected_at, opened_at=row.opened_at,
        closed_at=row.closed_at, cancelled_at=row.cancelled_at, rejection=rejection,
        cancel_reason=row.cancel_reason, target_count=target, deliveries=_counts(conn, row.id))


def _view(conn: psycopg.Connection, offering: _Offering, row: _Row, *, identities: Sequence[KnownIdentity],
          secrets: Sequence[str | None]) -> Dispatch:
    return Dispatch(offering_id=offering.offering_id, code=offering.code, timezone=offering.timezone,
                    summary=_summary(conn, row, identities=identities, secrets=secrets),
                    preview=_redact_preview(row.preview, identities, secrets))


def _reload_view(conn: psycopg.Connection, offering: _Offering, dispatch_row: int, *,
                 secrets: Sequence[str | None] = ()) -> Dispatch:
    row = _one(conn, "d.id = %s", (dispatch_row,))
    assert row is not None
    return _view(conn, offering, row, identities=load_known_identities(conn), secrets=list(secrets))


def _event_ref(offering_id: uuid.UUID, row: _Row) -> dict[str, Any]:
    return {"offering_id": offering_id, "dispatch_id": row.dispatch_uuid}


def _offering_uuid(conn: psycopg.Connection, offering_row: int) -> uuid.UUID:
    return conn.execute("SELECT offering_id FROM offerings WHERE id = %s", (offering_row,)).fetchone()[0]


# --------------------------------------------------------------------------------------------------
# Transições comuns (toda transição é condicional ao estado)
# --------------------------------------------------------------------------------------------------


def _cancel(conn: psycopg.Connection, row: _Row, reason: str, *, now: datetime, actor_token_id: str | None,
            from_states: Sequence[str], offering_id: uuid.UUID | None = None) -> bool:
    """`from_states` → `cancelled/<reason>` com `dispatch.cancelled` (a linha já travada)."""
    updated = conn.execute(
        "UPDATE dispatches SET state = 'cancelled', cancelled_at = %s, cancel_reason = %s "
        "WHERE id = %s AND state = ANY(%s)", (now, reason, row.id, list(from_states))).rowcount
    if not updated:
        return False
    oid = offering_id if offering_id is not None else _offering_uuid(conn, row.offering_row)
    append_event(conn, EventType.DISPATCH_CANCELLED, {**_event_ref(oid, row), "cancel_reason": reason},
                 occurred_at=now, actor_token_id=actor_token_id)
    return True


def _reject(conn: psycopg.Connection, row: _Row, rejection: str, reasons: Sequence[Mapping[str, Any]], *,
            now: datetime) -> bool:
    """`validating` → `rejected/<classe>` com os motivos do motor redigidos na gravação (só `code`,
    `message` e `phase`; nunca o `technical_detail`) e `dispatch.rejected`."""
    identities = load_write_identities(conn) if reasons else ()
    stored = []
    for reason in reasons:
        item = {"code": str(reason.get("code", "")),
                "message": redact(str(reason.get("message", "")), identities)}
        if reason.get("phase") is not None:
            item["phase"] = str(reason["phase"])
        stored.append(item)
    updated = conn.execute(
        "UPDATE dispatches SET state = 'rejected', rejected_at = %s, rejection = %s, rejection_reasons = %s "
        "WHERE id = %s AND state = 'validating'", (now, rejection, Jsonb(stored), row.id)).rowcount
    if not updated:
        return False
    append_event(conn, EventType.DISPATCH_REJECTED,
                 {**_event_ref(_offering_uuid(conn, row.offering_row), row), "rejection_reason": rejection},
                 occurred_at=now, actor_token_id=None)
    return True


# --------------------------------------------------------------------------------------------------
# Operações
# --------------------------------------------------------------------------------------------------


def prepare_dispatch(conn: psycopg.Connection, ref: str, request: DispatchRequest, *, now: datetime,
                     actor_token_id: str | None, secrets: Sequence[str | None] = (),
                     ) -> Dispatch | InvalidState | BotRequired | None:
    """Prepara um disparo em `validating`: 404 do oferecimento → `invalid_state` →
    `bot_required`; na transação, a referência (`ensure_ref`), o disparo, a tarefa `dispatch.validate`
    (chave `dispatch.validate:<id>`) e o `dispatch.prepared`. Nunca chama o motor. O pedido já
    foi validado pela API (horários em UTC)."""
    if request.origin not in {o.value for o in Origin}:
        raise ValueError("origem fora do conjunto")
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        offering = _offering_by_ref(conn, ref, lock="FOR NO KEY UPDATE")
        if offering is None:
            return None
        if offering.state != "active":
            return InvalidState(offering.state)
        bot = _bot_of(conn, offering.id, lock="FOR SHARE")
        if bot is None or bot[1] != "registered":
            return BotRequired()
        questionrefs.ensure_ref(conn, repo=request.repo, commit=request.commit, qid=request.qid, now=now)
        found = questionrefs.ref_row(conn, repo=request.repo, commit=request.commit, qid=request.qid)
        assert found is not None
        ref_row_id, ref_uuid = found
        immediate = request.opens_at is None
        opens_at = request.opens_at
        closes_at = request.closes_at
        window_minutes: int | None = None
        if immediate:
            if closes_at is None:
                window_minutes = offering.response_window_minutes
        elif closes_at is None:
            closes_at = opens_at + timedelta(minutes=offering.response_window_minutes)
        dispatch_uuid = uuid.uuid4()
        row_id = int(conn.execute(
            "INSERT INTO dispatches (dispatch_uuid, offering_id, question_ref_id, origin, immediate, opens_at, "
            "closes_at, window_minutes, state, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'validating', %s) "
            "RETURNING id",
            (dispatch_uuid, offering.id, ref_row_id, request.origin, immediate, opens_at, closes_at, window_minutes,
             now)).fetchone()[0])
        tasks.enqueue_in_transaction(conn, VALIDATE_TASK_TYPE, {"dispatch_row_id": row_id}, now=now,
                                     idempotency_key=f"{VALIDATE_TASK_TYPE}:{row_id}")
        append_event(conn, EventType.DISPATCH_PREPARED,
                     {"offering_id": offering.offering_id, "dispatch_id": dispatch_uuid,
                      "question_ref_id": ref_uuid, "origin": request.origin},
                     occurred_at=now, actor_token_id=actor_token_id)
        return _reload_view(conn, offering, row_id, secrets=secrets)


def get_dispatch(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *, now: datetime,
                 identities: Sequence[KnownIdentity], secrets: Sequence[str]) -> Dispatch | None:
    """O disparo do oferecimento (404 do oferecimento → 404 do disparo, inexistente ou de outro
    oferecimento), com a prévia; `repo`, `qid`, a prévia e os motivos redigidos na saída."""
    del now
    offering = _offering_by_ref(conn, ref)
    if offering is None:
        return None
    row = _one(conn, "d.dispatch_uuid = %s AND d.offering_id = %s", (dispatch_id, offering.id))
    if row is None:
        return None
    return _view(conn, offering, row, identities=identities, secrets=list(secrets))


def list_dispatches(conn: psycopg.Connection, ref: str, *, after: int | None, limit: int,
                    identities: Sequence[KnownIdentity], secrets: Sequence[str]) -> DispatchesPage | None:
    """Os disparos do oferecimento (qualquer estado dele) com `id > after`, em ordem de `id`, no máximo
    `limit`; `next_cursor` = o `id` do último, ou `after` sem itens."""
    offering = _offering_by_ref(conn, ref)
    if offering is None:
        return None
    start = 0 if after is None else int(after)
    rows = _select(conn, "d.offering_id = %s AND d.id > %s ORDER BY d.id LIMIT %s", (offering.id, start, limit))
    items = tuple(_summary(conn, row, identities=identities, secrets=list(secrets)) for row in rows)
    return DispatchesPage(offering_id=offering.offering_id, code=offering.code, timezone=offering.timezone,
                          items=items, next_cursor=rows[-1].id if rows else start)


def confirm_dispatch(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *, now: datetime,
                     actor_token_id: str | None, secrets: Sequence[str | None] = (),
                     ) -> Dispatch | InvalidState | BotRequired | DispatchStateConflict | WindowExpired | None:
    """A confirmação: 404 → 404 → `invalid_state` → `bot_required` →
    `invalid_dispatch_state` (não é agendado sem confirmação, ou `now >= confirm_deadline`) →
    `window_expired` (a janela efetiva). Aceita: a marca `confirmed_at`; no imediato, `opens_at = now`,
    `closes_at` efetivo e a tarefa `dispatch.open` (chave `dispatch.open:<id>`); `dispatch.confirmed`
    com o alvo **agora**. Travas: oferecimento → bot → disparo."""
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        offering = _offering_by_ref(conn, ref, lock="FOR NO KEY UPDATE")
        if offering is None:
            return None
        found = _one(conn, "d.dispatch_uuid = %s AND d.offering_id = %s", (dispatch_id, offering.id))
        if found is None:
            return None
        if offering.state != "active":
            return InvalidState(offering.state)
        bot = _bot_of(conn, offering.id, lock="FOR SHARE")
        if bot is None or bot[1] != "registered":
            return BotRequired()
        row = _one(conn, "d.id = %s", (found.id,), lock=True)
        assert row is not None
        if (row.state != "scheduled" or row.confirmed_at is not None or row.confirm_deadline is None
                or now >= row.confirm_deadline):
            return DispatchStateConflict(row.state)
        if row.immediate:
            opens_at = now
            if row.closes_at is not None:
                closes_at = row.closes_at
            else:
                assert row.window_minutes is not None
                closes_at = now + timedelta(minutes=row.window_minutes)
        else:
            assert row.opens_at is not None and row.closes_at is not None
            opens_at, closes_at = row.opens_at, row.closes_at
        if not (closes_at > now and MIN_WINDOW <= closes_at - opens_at <= MAX_WINDOW):
            return WindowExpired()
        conn.execute(
            "UPDATE dispatches SET confirmed_at = %s, opens_at = %s, closes_at = %s "
            "WHERE id = %s AND state = 'scheduled' AND confirmed_at IS NULL", (now, opens_at, closes_at, row.id))
        if row.immediate:
            tasks.enqueue_in_transaction(conn, OPEN_TASK_TYPE, {"dispatch_row_id": row.id}, now=now,
                                         idempotency_key=f"{OPEN_TASK_TYPE}:{row.id}")
        append_event(conn, EventType.DISPATCH_CONFIRMED,
                     {**_event_ref(offering.offering_id, row), "target_count": _current_target(conn, offering.id),
                      "opens_at": opens_at, "closes_at": closes_at},
                     occurred_at=now, actor_token_id=actor_token_id)
        return _reload_view(conn, offering, row.id, secrets=secrets)


def cancel_dispatch(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *, now: datetime,
                    actor_token_id: str | None, secrets: Sequence[str | None] = (),
                    ) -> Dispatch | DispatchStateConflict | None:
    """O cancelamento pelo operador: 404 → 404 → `invalid_dispatch_state`;
    `validating` ou `scheduled` (confirmado ou não) → `cancelled/operator` com `dispatch.cancelled`.
    Uma validação ou abertura que chegue depois não muda o estado (as transições são condicionais)."""
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        offering = _offering_by_ref(conn, ref, lock="FOR NO KEY UPDATE")
        if offering is None:
            return None
        found = _one(conn, "d.dispatch_uuid = %s AND d.offering_id = %s", (dispatch_id, offering.id))
        if found is None:
            return None
        row = _one(conn, "d.id = %s", (found.id,), lock=True)
        assert row is not None
        if not _cancel(conn, row, CancelReason.OPERATOR.value, now=now, actor_token_id=actor_token_id,
                       from_states=("validating", "scheduled"), offering_id=offering.offering_id):
            return DispatchStateConflict(row.state)
        return _reload_view(conn, offering, row.id, secrets=secrets)


def reject_validation_exhausted(conn: psycopg.Connection, dispatch_row_id: int, *, now: datetime) -> bool:
    """Esgotadas as rodadas da validação: `validating` → `rejected/internal_failure`, ou
    `cancelled/validation_overran` se agendado com `now >= opens_at`; `False` se já não estava lá.

    Trabalha só sobre a linha do disparo (a varredura já a travou na transação dela), sem travar o
    oferecimento nem o bot: a ordem fixa das travas fica preservada."""
    with conn.transaction():
        row = _one(conn, "d.id = %s", (dispatch_row_id,), lock=True)
        if row is None or row.state != "validating":
            return False
        if not row.immediate and row.opens_at is not None and now >= row.opens_at:
            return _cancel(conn, row, CancelReason.VALIDATION_OVERRAN.value, now=now, actor_token_id=None,
                           from_states=("validating",))
        return _reject(conn, row, RejectionClass.INTERNAL_FAILURE.value, (), now=now)


# --------------------------------------------------------------------------------------------------
# Oferecimento: encerramento e arquivamento
# --------------------------------------------------------------------------------------------------


def cancel_for_offering_close(conn: psycopg.Connection, offering_row_id: int, *, now: datetime,
                              actor_token_id: str | None) -> int:
    """No encerramento (na transação dele, com o oferecimento travado): cancela os disparos
    `validating` e `scheduled` (confirmados ou não) com `offering_closed`, um `dispatch.cancelled` por
    disparo com o token do encerramento; os abertos seguem. Devolve quantos."""
    rows = _select(conn, "d.offering_id = %s AND d.state IN ('validating', 'scheduled') ORDER BY d.id",
                   (offering_row_id,), lock=True)
    if not rows:
        return 0
    offering_id = _offering_uuid(conn, offering_row_id)
    return sum(1 for row in rows
               if _cancel(conn, row, CancelReason.OFFERING_CLOSED.value, now=now, actor_token_id=actor_token_id,
                          from_states=("validating", "scheduled"), offering_id=offering_id))


def close_for_archive(conn: psycopg.Connection, offering_row_id: int, *, now: datetime) -> int:
    """No arquivamento aceito (na transação dele, com o oferecimento travado): fecha os disparos
    `open` como no horário, com `trigger: archive`. Devolve quantos."""
    bot = _bot_of(conn, offering_row_id, lock="FOR SHARE")
    rows = _select(conn, "d.offering_id = %s AND d.state = 'open' ORDER BY d.id", (offering_row_id,), lock=True)
    if rows:
        # Ordem das travas: as entregas de TODOS os disparos abertos, em ordem de `id`, antes
        # de qualquer evento. Fechar um disparo grava eventos (e toma o lock dos eventos); travar as entregas do
        # seguinte só depois disso inverteria a ordem do worker (entrega → eventos, no registro do resultado de
        # uma mensagem de entrega) e permitiria um impasse.
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        conn.execute("SELECT x.id FROM deliveries x WHERE x.dispatch_id = ANY(%s) ORDER BY x.id "
                     "FOR NO KEY UPDATE OF x",
                     ([row.id for row in rows],))
    closed = 0
    for row in rows:
        if _close_locked(conn, row, now=now, trigger="archive", bot=bot, channel=None):
            closed += 1
    return closed


def archive_blockers(conn: psycopg.Connection, offering_row_id: int, *,
                     prepared_at: datetime | None) -> int:
    """O que impede o arquivamento: 0 sem disparo `open` ou `closed`
    (nada a esperar). Com eles: as submissões `grading` dos disparos do oferecimento, mais os updates
    `pending` do bot, mais 1 se (só na confirmação, `prepared_at` não nulo) o bot está sendo consultado
    (registrado, com posse viva desde o preparo) e a última consulta bem-sucedida é anterior ao preparo
    (a barreira (b) do fechamento, com o preparo no lugar de `closes_at`)."""
    live = conn.execute("SELECT EXISTS (SELECT 1 FROM dispatches WHERE offering_id = %s "
                        "AND state IN ('open', 'closed'))", (offering_row_id,)).fetchone()[0]
    if not live:
        return 0
    # As duas contagens num único comando, com um só instantâneo (READ COMMITTED): a transação que processa a
    # última parte de uma resposta passa o update a `processed` e cria a submissão `grading` de uma vez; contadas
    # em comandos separados, uma confirmação entre eles escaparia das duas, e o arquivamento passaria com
    # trabalho pendente.
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM submissions s JOIN deliveries x ON x.id = s.delivery_id "
        "JOIN dispatches d ON d.id = x.dispatch_id WHERE d.offering_id = %s AND s.outcome = 'grading'), "
        "(SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.offering_id = %s AND u.state = 'pending')",
        (offering_row_id, offering_row_id)).fetchone()
    grading, pending = int(counts[0]), int(counts[1])
    bot = conn.execute("SELECT id, state, lease_owner, lease_until, last_poll_ok_at FROM bots "
                       "WHERE offering_id = %s", (offering_row_id,)).fetchone()
    if bot is None:
        return grading
    stale = 0
    if prepared_at is not None:
        lease_until, last_poll = utc(bot[3]), utc(bot[4])
        polled = bot[1] == "registered" and bot[2] is not None and lease_until is not None and (
            lease_until >= prepared_at)
        if polled and (last_poll is None or last_poll < prepared_at):
            stale = 1
    return grading + pending + stale


# --------------------------------------------------------------------------------------------------
# Fechamento: a transição comum ao horário e ao arquivamento
# --------------------------------------------------------------------------------------------------


def _close_locked(conn: psycopg.Connection, row: _Row, *, now: datetime, trigger: str,
                  bot: tuple[int, str] | None, channel: DeliveryChannel | None) -> bool:
    """`open` → `closed` com o disparo já travado: as entregas em ordem de `id` (`delivered` sem
    correção pendente → `no_answer`; `pending` → `not_delivered` com as mensagens `pending`
    canceladas), a edição de fechamento (versões e a edição com a renderização completa, lida na posse)
    das `answered`, `dont_know` e `no_answer` sem correção pendente, e o `dispatch.closed` por último,
    com as contagens (a entrega com correção pendente conta como `delivered`). A revelação das
    `answered`/`dont_know` (`revealed_at` e o gabarito permitido); a edição de fechamento, sem gabarito
    nem `revealed_at`, das `no_answer`, com os controles e o "Não sei" no horário e, no arquivamento,
    só com "Janela fechada" (a renderização sabe do oferecimento
    arquivado).

    Com o canal (o horário), a revelação grava a renderização por `deliveries.render_state` (que soma o
    `render_version` e grava o `current_render`, no dialeto gravado) e a leva por
    `deliveries.enqueue_state_update` (edição, ou `fallback` sem edição, e o `notice` sem caber). Sem
    canal (o arquivamento, na transação da API), soma o `render_version` e enfileira a edição com essa
    versão: a posse renderiza o estado corrente, no dialeto gravado, porque a `current_render` ficou
    mais velha que a versão pedida."""
    if row.state != "open":
        return False
    from cq_server import deliveries, outbox  # a fila de saída e a renderização só no fechamento

    offering_id = _offering_uuid(conn, row.offering_row)
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    items = conn.execute(
        "SELECT x.id, x.delivery_uuid, x.state, x.variant_at, x.delivery_endpoint_id, x.render_version, "
        "x.controls_version, p.pseudo_id, EXISTS (SELECT 1 FROM submissions s WHERE s.delivery_id = x.id "
        "AND s.outcome = 'grading'), EXISTS (SELECT 1 FROM outbox_messages m WHERE m.delivery_id = x.id "
        "AND m.state IN ('pending', 'leased')) "
        "FROM deliveries x JOIN enrollments e ON e.id = x.enrollment_id JOIN people p ON p.id = e.person_id "
        "WHERE x.dispatch_id = %s ORDER BY x.id FOR NO KEY UPDATE OF x", (row.id,)).fetchall()
    bot_row = bot[0] if bot is not None else None
    no_access = bot is not None and bot[1] == "no_access"
    # Avisos à equipe: com as entregas travadas e antes da passagem a `closed`, o
    # "aberto" antecipado com as contagens do instante (as pendentes ainda pendentes), só a fase 1 (`pending`,
    # sem evento) e só se o `dispatch.opened` está acima do piso e ainda não há aviso. Importação tardia: o
    # domínio dos disparos não carrega os avisos à equipe com o módulo.
    from cq_server import teamnotices

    teamnotices.record_opened_on_close(conn, dispatch_row_id=row.id, offering_row_id=row.offering_row,
                                       counts=_counts(conn, row.id), now=now)
    # O disparo fecha antes das entregas: a renderização da revelação lê o disparo já `closed`.
    conn.execute("UPDATE dispatches SET state = 'closed', closed_at = %s WHERE id = %s AND state = 'open'",
                 (now, row.id))
    undelivered: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for (delivery_id, delivery_uuid, state, variant_at, endpoint_row, render_version, controls_version, person,
         grading, queued) in items:
        delivery_id = int(delivery_id)
        if state == "delivered" and not grading:
            conn.execute("UPDATE deliveries SET state = 'no_answer' WHERE id = %s AND state = 'delivered'",
                         (delivery_id,))
            state = "no_answer"
        elif state == "pending":
            if no_access:
                reason = "bot_no_access"
            elif variant_at is None:
                reason = "engine_unavailable"
            else:
                reason = "channel_unavailable"
            conn.execute("UPDATE deliveries SET state = 'not_delivered', undelivered_reason = %s, "
                         "current_field = NULL WHERE id = %s AND state = 'pending'", (reason, delivery_id))
            if queued:
                outbox.cancel_delivery_messages(conn, delivery_row_id=delivery_id)
            undelivered.append((delivery_uuid, person, reason))
            continue
        if state in _CLOSE_EDIT_STATES and not grading and bot_row is not None:
            revealed = state in _REVEALED_STATES
            if channel is not None:
                conn.execute("UPDATE deliveries SET controls_version = controls_version + 1, "
                             "revealed_at = CASE WHEN %s THEN %s ELSE revealed_at END WHERE id = %s",
                             (revealed, now, delivery_id))
                text_format = conn.execute("SELECT rendered ->> 'format' FROM delivery_contents "
                                           "WHERE delivery_id = %s", (delivery_id,)).fetchone()
                rendered = deliveries.render_state(
                    conn, delivery_id, formatter=channel.formatter_for(str(text_format[0] if text_format else "")),
                    capabilities=channel.capabilities, now=now)
                deliveries.enqueue_state_update(conn, delivery_id, rendered, capabilities=channel.capabilities,
                                                now=now)
                continue
            version = int(render_version) + 1
            conn.execute(
                "UPDATE deliveries SET render_version = %s, controls_version = %s, "
                "revealed_at = CASE WHEN %s THEN %s ELSE revealed_at END WHERE id = %s",
                (version, int(controls_version) + 1, revealed, now, delivery_id))
            outbox.enqueue_delivery_edit(conn, delivery_row_id=delivery_id, recipient=person, bot_id=bot_row,
                                         endpoint_row_id=int(endpoint_row), role="question",
                                         markup_only=False, version=version, now=now)
    for delivery_uuid, person, reason in undelivered:
        append_event(conn, EventType.DELIVERY_NOT_DELIVERED,
                     {**_event_ref(offering_id, row), "delivery_id": delivery_uuid, "person": person,
                      "undelivered_reason": reason}, occurred_at=now, actor_token_id=None)
    append_event(conn, EventType.DISPATCH_CLOSED,
                 {**_event_ref(offering_id, row), "trigger": trigger, "counts": _state_counts(conn, row.id)},
                 occurred_at=now, actor_token_id=None)
    return True



# --------------------------------------------------------------------------------------------------
# Tratadores
# --------------------------------------------------------------------------------------------------


class _TaskHandler:
    """Tratador de tarefa do disparo (chamável, com `close()`): a conexão própria abre no primeiro uso."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings

    def __call__(self, task: ClaimedTask) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _subject(task: ClaimedTask) -> int:
        value = task.payload.get("dispatch_row_id") if isinstance(task.payload, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError("payload sem dispatch_row_id")
        return value


class ValidationRetry(Exception):
    """Falha repetível de um passo externo da validação (a fila de tarefas repete com a espera dela; só o
    nome do tipo vai ao `last_error`, sem URL nem detalhe)."""


@dataclass(frozen=True)
class _ValidationState:
    row: _Row
    inspected: bool
    title: str | None


class _ValidateHandler(_TaskHandler):
    """`dispatch.validate`: um passo externo por execução; o passo sai do estado
    gravado: a obtenção sempre primeiro (o reuso segue sem rede), a inspeção se a referência
    ainda não foi inspecionada, e a variante de amostra por último."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                 fetcher: RepoFetcher | None, channel: DeliveryChannel | None) -> None:
        super().__init__(conninfo, clock=clock, settings=settings)
        self._engine = engine
        self._fetcher = fetcher
        self._channel = channel

    # -- leitura e guardas ------------------------------------------------------------------------

    def _load(self, conn: psycopg.Connection, dispatch_row: int) -> _ValidationState | None:
        row = _one(conn, "d.id = %s", (dispatch_row,))
        if row is None:
            return None
        ref = conn.execute("SELECT inspected_at, title FROM question_refs WHERE id = %s", (row.ref_row,)).fetchone()
        return _ValidationState(row=row, inspected=ref[0] is not None, title=ref[1])

    def _guard(self, conn: psycopg.Connection, dispatch_row: int) -> _ValidationState | None:
        """O início de todo passo: ainda `validating` e, no agendado, `now <
        opens_at`; senão `cancelled/validation_overran`, sem chamada externa. `None`: nada a fazer."""
        state = self._load(conn, dispatch_row)
        if state is None or state.row.state != "validating":
            return None
        row = state.row
        now = self._clock.now()
        if not row.immediate and row.opens_at is not None and now >= row.opens_at:
            with conn.transaction():
                locked = _one(conn, "d.id = %s", (dispatch_row,), lock=True)
                if locked is not None:
                    _cancel(conn, locked, CancelReason.VALIDATION_OVERRAN.value, now=now, actor_token_id=None,
                            from_states=("validating",))
            return None
        return state

    def _finish(self, conn: psycopg.Connection, dispatch_row: int, action: Callable[[_Row], None]) -> None:
        """Numa transação, com o disparo travado e ainda `validating`, aplica `action`."""
        with conn.transaction():
            row = _one(conn, "d.id = %s", (dispatch_row,), lock=True)
            if row is None or row.state != "validating":
                return
            action(row)

    def _reject_now(self, conn: psycopg.Connection, dispatch_row: int, rejection: str,
                    reasons: Sequence[Mapping[str, Any]] = ()) -> None:
        self._finish(conn, dispatch_row, lambda row: _reject(conn, row, rejection, reasons, now=self._clock.now()))

    def _continue(self, conn: psycopg.Connection, dispatch_row: int, task: ClaimedTask,
                  record: Callable[[_Row], None] | None = None) -> None:
        """Grava o resultado do passo e enfileira a continuação `dispatch.validate:<id>:c<task_id>`
        do passo seguinte, na mesma transação."""

        def action(row: _Row) -> None:
            if record is not None:
                record(row)
            tasks.enqueue_in_transaction(conn, VALIDATE_TASK_TYPE, {"dispatch_row_id": dispatch_row},
                                         now=self._clock.now(),
                                         idempotency_key=f"{VALIDATE_TASK_TYPE}:{dispatch_row}:c{task.id}")

        self._finish(conn, dispatch_row, action)

    def _retry_or_reject(self, conn: psycopg.Connection, dispatch_row: int, task: ClaimedTask,
                         rejection: str) -> None:
        """Falha repetível: repete pela fila de tarefas; na última tentativa, rejeita com a classe."""
        if task.attempts >= self._settings.max_attempts:
            self._reject_now(conn, dispatch_row, rejection)
            return
        raise ValidationRetry(rejection)

    # -- a execução -------------------------------------------------------------------------------

    def __call__(self, task: ClaimedTask) -> None:
        dispatch_row = self._subject(task)
        conn = self._db.get()
        state = self._guard(conn, dispatch_row)
        if state is None:
            return
        row = state.row

        # Passo 1: a obtenção (sempre; o reuso não usa a rede e segue).
        fetched = self._fetch(row.repo, row.commit)
        if isinstance(fetched, FetchRefused):
            self._reject_now(conn, dispatch_row, RejectionClass.REPOSITORY_REFUSED.value)
            return
        if isinstance(fetched, FetchNotFound):
            rejection = (RejectionClass.COMMIT_NOT_FOUND if fetched.what == "commit"
                         else RejectionClass.REPOSITORY_UNREACHABLE)
            self._reject_now(conn, dispatch_row, rejection.value)
            return
        if isinstance(fetched, FetchTemporaryFailure):
            rejection = (RejectionClass.REPOSITORY_REFUSED if fetched.error_type == "timeout"
                         else RejectionClass.REPOSITORY_UNREACHABLE)
            self._retry_or_reject(conn, dispatch_row, task, rejection.value)
            return
        if not fetched.reused:
            self._continue(conn, dispatch_row, task)
            return

        ref = QuestionRef(repo=row.repo, commit=row.commit, qid=row.qid)
        if not state.inspected:
            state = self._guard(conn, dispatch_row)
            if state is None:
                return
            self._inspect(conn, dispatch_row, task, ref, state.row)
            return
        state = self._guard(conn, dispatch_row)
        if state is None:
            return
        self._variant(conn, dispatch_row, task, ref, state)

    def _fetch(self, repo: str, commit: str) -> Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure:
        if self._fetcher is None:
            return FetchTemporaryFailure("repos_unavailable")
        try:
            return self._fetcher.ensure(repo, commit)
        except Exception as exc:  # noqa: BLE001 - só o nome do tipo
            return FetchTemporaryFailure(type(exc).__name__)

    def _engine_or_unavailable(self) -> QuestionEngine:
        if self._engine is None:
            raise EngineUnavailable("unconfigured")
        return self._engine

    def _inspect(self, conn: psycopg.Connection, dispatch_row: int, task: ClaimedTask, ref: QuestionRef,
                 row: _Row) -> None:
        try:
            result = self._engine_or_unavailable().inspect(ref)
        except EngineUnavailable:
            self._retry_or_reject(conn, dispatch_row, task, RejectionClass.ENGINE_UNAVAILABLE.value)
            return
        except EngineNotFound:
            self._reject_now(conn, dispatch_row, RejectionClass.QUESTION_NOT_FOUND.value)
            return
        except EngineRequestRejected:
            self._reject_now(conn, dispatch_row, RejectionClass.INTERNAL_FAILURE.value)
            return
        if isinstance(result, Unsupported):
            self._reject_now(conn, dispatch_row, RejectionClass.QUESTION_UNSUPPORTED.value,
                             [{"code": r.code, "message": r.message} for r in result.reasons])
            return
        question = result.question
        now = self._clock.now()

        def record(_row: _Row) -> None:
            # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa
            # das travas continua a valer).
            conn.execute("SELECT id FROM question_refs WHERE id = %s FOR NO KEY UPDATE", (row.ref_row,))
            identities = load_write_identities(conn)
            questionrefs.record_inspection(conn, row.ref_row, question, now=now, identities=identities)

        self._continue(conn, dispatch_row, task, record)

    def _variant(self, conn: psycopg.Connection, dispatch_row: int, task: ClaimedTask, ref: QuestionRef,
                 state: _ValidationState) -> None:
        channel = self._channel
        if channel is None:
            raise PermanentTaskError(NO_ROUTE_CHANNEL)
        seed = base36(secrets.randbelow(SEED_LIMIT))
        try:
            result = self._engine_or_unavailable().create_variant(ref, seed)
        except EngineUnavailable:
            self._retry_or_reject(conn, dispatch_row, task, RejectionClass.ENGINE_UNAVAILABLE.value)
            return
        except EngineNotFound:
            self._reject_now(conn, dispatch_row, RejectionClass.QUESTION_NOT_FOUND.value)
            return
        except EngineRequestRejected:
            self._reject_now(conn, dispatch_row, RejectionClass.INTERNAL_FAILURE.value)
            return
        if isinstance(result, Unsupported):
            self._reject_now(conn, dispatch_row, RejectionClass.QUESTION_UNSUPPORTED.value,
                             [{"code": r.code, "message": r.message} for r in result.reasons])
            return
        if isinstance(result, Broken):
            self._reject_now(conn, dispatch_row, RejectionClass.QUESTION_BROKEN.value,
                             [{"code": result.code, "message": result.message, "phase": result.phase}])
            return
        preview, fits, too_many = _render_preview(result, title=state.title, formatter=channel.formatter,
                                                  capabilities=channel.capabilities)
        if not fits:
            self._reject_now(conn, dispatch_row, RejectionClass.CHANNEL_LIMITS_EXCEEDED.value)
            return

        def action(row: _Row) -> None:
            now = self._clock.now()
            if not row.immediate and row.opens_at is not None and now >= row.opens_at:
                _cancel(conn, row, CancelReason.VALIDATION_OVERRAN.value, now=now, actor_token_id=None,
                        from_states=("validating",))
                return
            target = _current_target(conn, row.offering_row)
            warnings = list(preview["warnings"])
            if target == 0:
                warnings.append({"kind": "empty_target", "message": CORE_WARNING_MESSAGES["empty_target"]})
            if too_many:
                warnings.append({"kind": "too_many_options",
                                 "message": CORE_WARNING_MESSAGES["too_many_options"]})
            repeated = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM dispatches WHERE offering_id = %s AND question_ref_id = %s "
                "AND id <> %s AND validated_at IS NOT NULL)", (row.offering_row, row.ref_row, row.id)).fetchone()[0]
            if repeated:
                warnings.append({"kind": "repeated_question",
                                 "message": CORE_WARNING_MESSAGES["repeated_question"]})
            if _lacks_teacher_contact(conn, row.offering_row):
                # Por último na lista (o aviso é acrescentado depois dos do motor).
                warnings.append({"kind": "no_teacher_contact",
                                 "message": CORE_WARNING_MESSAGES["no_teacher_contact"]})
            identities = load_write_identities(conn)
            stored = _redact_preview({**preview, "target_count": target, "warnings": warnings}, identities, ())
            deadline = now + IMMEDIATE_CONFIRM if row.immediate else row.opens_at
            updated = conn.execute(
                "UPDATE dispatches SET state = 'scheduled', validated_at = %s, confirm_deadline = %s, preview = %s "
                "WHERE id = %s AND state = 'validating'", (now, deadline, Jsonb(stored), row.id)).rowcount
            if updated:
                append_event(conn, EventType.DISPATCH_VALIDATED,
                             {**_event_ref(_offering_uuid(conn, row.offering_row), row), "target_count": target,
                              "warnings": [w["kind"] for w in warnings]},
                             occurred_at=now, actor_token_id=None, identities=identities)
                # A amostra da prévia, na mesma transação, com a seed que
                # foi ao motor e a IR da variante (importação tardia).
                from cq_server import previews

                previews.record_sample(conn, dispatch_row_id=row.id, seed=seed, ir=result.ir, now=now)

        self._finish(conn, dispatch_row, action)


def _lacks_teacher_contact(conn: psycopg.Connection, offering_row: int) -> bool:
    """O oferecimento, na validação, tem as mensagens livres desligadas e nenhum contato em
    `offering_contacts`. Lê só a existência da linha,
    nunca o e-mail."""
    row = conn.execute(
        "SELECT NOT o.free_messages AND NOT EXISTS (SELECT 1 FROM offering_contacts c WHERE c.offering_id = o.id) "
        "FROM offerings o WHERE o.id = %s", (offering_row,)).fetchone()
    return bool(row is not None and row[0])


def _media_labels(ir: Mapping[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for index, media in enumerate(ir.get("media") or [], start=1):
        if isinstance(media, Mapping) and isinstance(media.get("id"), str):
            labels[media["id"]] = f"Figura {index}"
    return labels


def _render_preview(variant: Variant, *, title: str | None, formatter: MessageFormatter,
                    capabilities: ChannelCapabilities) -> tuple[dict[str, Any], bool, bool]:
    """A prévia e se a questão cabe nos limites do canal com rota: o
    enunciado em texto pelo `formatter.plain`, os campos (tipo, rótulo, sufixo e dica, sem o nome do
    campo do PL nem o gabarito) e os avisos do motor; o terceiro valor marca a escolha que degrada
    (alternativas e controles, "Enviar" e "Não sei" nas duas escolhas, acima de `max_buttons`:
    `deliveries.choice_has_buttons`, a mesma regra da abertura)."""
    ir = variant.ir
    statement_html = str(ir.get("statement", {}).get("html", ""))
    labels = _media_labels(ir)
    blocks = [formatter.from_ir(statement_html, media_labels=labels)]
    fields = []
    too_many = False
    for field_ in ir.get("fields") or []:
        label_html = field_.get("label_html")
        suffix_html = field_.get("suffix_html")
        fields.append({"type": field_.get("type"),
                       "label": None if label_html is None else formatter.plain(label_html),
                       "suffix": None if suffix_html is None else formatter.plain(suffix_html),
                       "hint": str(field_.get("hint", ""))})
        if label_html:
            blocks.append(formatter.from_ir(label_html, media_labels=labels))
        options = field_.get("options") or []
        for option in options:
            if isinstance(option, Mapping):
                blocks.append(formatter.from_ir(str(option.get("html", "")), media_labels=labels))
        kind = field_.get("type")
        if kind in ("single_choice", "multiple_choice") \
                and not choice_has_buttons(str(kind), len(options), capabilities):
            too_many = True
    composed = formatter.compose(blocks)
    fits = composed.fits and all(block.fits for block in blocks)
    warnings = [{"kind": w.code, "message": w.message} for w in variant.warnings if w.code in ENGINE_WARNING_KINDS]
    preview: dict[str, Any] = {"title": title, "statement": formatter.plain(statement_html), "fields": fields,
                               "target_count": 0, "warnings": warnings}
    return preview, fits, too_many


class _ExpireHandler(_TaskHandler):
    """`dispatch.expire`: reconfere sob as travas (oferecimento → bot →
    disparo) e cancela o agendado sem confirmação com o prazo vencido (`expired`)."""

    def __call__(self, task: ClaimedTask) -> None:
        dispatch_row = self._subject(task)
        conn = self._db.get()
        with conn.transaction():
            found = _one(conn, "d.id = %s", (dispatch_row,))
            if found is None:
                return
            _offering_by_id(conn, found.offering_row, lock="FOR SHARE")
            _bot_of(conn, found.offering_row, lock="FOR SHARE")
            row = _one(conn, "d.id = %s", (dispatch_row,), lock=True)
            now = self._clock.now()
            if (row is None or row.state != "scheduled" or row.confirmed_at is not None
                    or row.confirm_deadline is None or row.confirm_deadline > now):
                return
            conn.execute("UPDATE dispatches SET state = 'cancelled', cancelled_at = %s, cancel_reason = 'expired' "
                         "WHERE id = %s AND state = 'scheduled' AND confirmed_at IS NULL", (now, row.id))
            append_event(conn, EventType.DISPATCH_CANCELLED,
                         {**_event_ref(_offering_uuid(conn, row.offering_row), row), "cancel_reason": "expired"},
                         occurred_at=now, actor_token_id=None)


class _OpenHandler(_TaskHandler):
    """`dispatch.open`: oferecimento `FOR SHARE` → bot `FOR SHARE` → disparo
    `FOR NO KEY UPDATE`; o alvo lido sem trava; uma entrega por matrícula, com a seed e o `button_token`
    próprios; o endereço `blocked` já `not_delivered/endpoint_blocked`; as demais `pending` com a
    tarefa `delivery.prepare`; `window_missed` com a tolerância `OPEN_GRACE_SECONDS`."""

    def __call__(self, task: ClaimedTask) -> None:
        dispatch_row = self._subject(task)
        conn = self._db.get()
        with conn.transaction():
            found = _one(conn, "d.id = %s", (dispatch_row,))
            if found is None:
                return
            offering = _offering_by_id(conn, found.offering_row, lock="FOR SHARE")
            bot = _bot_of(conn, found.offering_row, lock="FOR SHARE")
            row = _one(conn, "d.id = %s", (dispatch_row,), lock=True)
            now = self._clock.now()
            if (row is None or row.state != "scheduled" or row.confirmed_at is None or row.opens_at is None
                    or row.closes_at is None or row.opens_at > now):
                return
            if now - row.opens_at > OPEN_GRACE and row.closes_at - now < MIN_WINDOW:
                _cancel(conn, row, CancelReason.WINDOW_MISSED.value, now=now, actor_token_id=None,
                        from_states=("scheduled",), offering_id=offering.offering_id)
                return
            targets = [] if bot is None else conn.execute(
                "SELECT e.id, de.id, de.state, p.pseudo_id FROM enrollments e "
                "JOIN delivery_endpoints de ON de.person_id = e.person_id AND de.bot_id = %s "
                "  AND de.state IN ('active', 'blocked') "
                "JOIN people p ON p.id = e.person_id "
                "WHERE e.offering_id = %s AND e.role = 'student' AND e.state = 'active' ORDER BY e.id",
                (bot[0], row.offering_row)).fetchall()
            blocked: list[tuple[uuid.UUID, uuid.UUID]] = []
            for enrollment_row, endpoint_row, endpoint_state, person in targets:
                delivery_uuid = uuid.uuid4()
                is_blocked = endpoint_state == "blocked"
                delivery_id = int(conn.execute(
                    "INSERT INTO deliveries (delivery_uuid, dispatch_id, enrollment_id, delivery_endpoint_id, seed, "
                    "button_token, state, undelivered_reason, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (delivery_uuid, row.id, enrollment_row, endpoint_row, secrets.randbelow(SEED_LIMIT),
                     secrets.token_urlsafe(16), "not_delivered" if is_blocked else "pending",
                     "endpoint_blocked" if is_blocked else None, now)).fetchone()[0])
                if is_blocked:
                    blocked.append((delivery_uuid, person))
                else:
                    tasks.enqueue_in_transaction(conn, PREPARE_TASK_TYPE, {"delivery_row_id": delivery_id},
                                                 now=now, idempotency_key=f"{PREPARE_TASK_TYPE}:{delivery_id}")
            conn.execute("UPDATE dispatches SET state = 'open', opened_at = %s WHERE id = %s AND state = 'scheduled'",
                         (now, row.id))
            append_event(conn, EventType.DISPATCH_OPENED,
                         {**_event_ref(offering.offering_id, row), "target_count": len(targets)},
                         occurred_at=now, actor_token_id=None)
            for delivery_uuid, person in blocked:
                append_event(conn, EventType.DELIVERY_NOT_DELIVERED,
                             {**_event_ref(offering.offering_id, row), "delivery_id": delivery_uuid,
                              "person": person, "undelivered_reason": "endpoint_blocked"},
                             occurred_at=now, actor_token_id=None)


class _CloseHandler(_TaskHandler):
    """`dispatch.close`: bot `FOR SHARE` → disparo `FOR NO KEY UPDATE`; a barreira dos updates
    gravados ((a) nenhum update `pending` do bot com `received_at <= closes_at`; (b) a última consulta
    bem-sucedida em ou depois de `closes_at`, ou o bot fora de consulta); sem ela, termina `done` sem
    efeito (a varredura cria a rodada seguinte); com ela, a transição comum (`trigger: window`)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings,
                 channel: DeliveryChannel | None) -> None:
        super().__init__(conninfo, clock=clock, settings=settings)
        self._channel = channel

    def __call__(self, task: ClaimedTask) -> None:
        dispatch_row = self._subject(task)
        conn = self._db.get()
        with conn.transaction():
            found = _one(conn, "d.id = %s", (dispatch_row,))
            if found is None or found.state != "open":
                return
            bot = conn.execute(
                "SELECT b.id, b.state, b.lease_owner, b.lease_until, b.last_poll_ok_at, o.state FROM bots b "
                "JOIN offerings o ON o.id = b.offering_id WHERE b.offering_id = %s FOR SHARE OF b",
                (found.offering_row,)).fetchone()
            row = _one(conn, "d.id = %s", (dispatch_row,), lock=True)
            now = self._clock.now()
            if row is None or row.state != "open" or row.closes_at is None or row.closes_at > now:
                return
            if bot is not None and not self._barrier(conn, bot, row.closes_at, now):
                return
            has_deliveries = conn.execute("SELECT EXISTS (SELECT 1 FROM deliveries WHERE dispatch_id = %s)",
                                          (row.id,)).fetchone()[0]
            if has_deliveries and self._channel is None:
                raise PermanentTaskError(NO_ROUTE_CHANNEL)
            _close_locked(conn, row, now=now, trigger="window",
                          bot=None if bot is None else (int(bot[0]), bot[1]), channel=self._channel)

    @staticmethod
    def _barrier(conn: psycopg.Connection, bot: Sequence[Any], closes_at: datetime, now: datetime) -> bool:
        bot_row, state, owner, lease_until, last_poll, offering_state = bot
        pending = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM telegram_updates WHERE bot_id = %s AND state = 'pending' "
            "AND received_at <= %s)", (bot_row, closes_at)).fetchone()[0]
        if pending:
            return False
        lease_until, last_poll = utc(lease_until), utc(last_poll)
        polled = (state == "registered" and owner is not None and lease_until is not None and lease_until >= now
                  and offering_state in offerings.POLLED_STATES)
        return not polled or (last_poll is not None and last_poll >= closes_at)


def validate_task_handler(conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                          fetcher: RepoFetcher | None, channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador `dispatch.validate`. O motor ausente vale
    `EngineUnavailable("unconfigured")`; a obtenção ausente, `FetchTemporaryFailure("repos_unavailable")`."""
    return _ValidateHandler(conninfo, clock=clock, settings=settings, engine=engine, fetcher=fetcher,
                            channel=channel)


def expire_task_handler(conninfo: str, *, clock: Clock, settings: Settings) -> _TaskHandler:
    """O tratador `dispatch.expire`."""
    return _ExpireHandler(conninfo, clock=clock, settings=settings)


def open_task_handler(conninfo: str, *, clock: Clock, settings: Settings) -> _TaskHandler:
    """O tratador `dispatch.open`."""
    return _OpenHandler(conninfo, clock=clock, settings=settings)


def close_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                       channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador `dispatch.close`."""
    return _CloseHandler(conninfo, clock=clock, settings=settings, channel=channel)
