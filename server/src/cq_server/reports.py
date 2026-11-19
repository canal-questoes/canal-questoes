"""Relatório de um disparo: cálculo, congelamento, operações e o tratador `report.freeze`. O relatório é
descritivo, por questão: alcance, desempenho, tempo de resposta, distribuição das respostas e fricção,
calculado do que está gravado e congelado em versões numeradas, com o resumo de fechamento enviado à
equipe e o reenvio sob pedido.

- **Tipos de contrato**: o `Report` e as suas partes, com os nomes Python iguais
  aos do JSON do contrato do relatório, um tipo por objeto do JSON e na mesma ordem (o CSV sai na ordem do
  JSON); `ReportTrigger`; os resultados das operações sobre versões (`ReportVersionsPage`,
  `ResendResult`, no molde de `dispatches.DispatchesPage` e de `students.ResendResult`: a rota monta o
  `OfferingRef` a partir de `offering_id` e `code`); a sentinela `ReportVersionNotFound` (→ 404).
- **Cálculo** (`compute_report`): lê sem trava; redige com as identidades passadas; usa só
  `formatter.plain` no rótulo e nas alternativas.
- **Congelamento** (`freeze_report`, tratador `report.freeze`) e **reenvio** (`resend_summary`): a
  ordem das travas do relatório; o resumo é composto pelo `teamnotices.deliver` pela porta
  `ReportRenderer` (`reportrender.ChannelSummaryRenderer`).
- **Funções puras** (`round_sig`, `normalize_text_answer`, `top_list`): a única definição usada pelo
  cálculo e pelos testes unitários.

Importa `reportrender` e `teamnotices` (o resumo de fechamento); `dispatches`, `offerings` e `answers`
só dentro das funções (sem ciclo); nunca HTTP nem `cq_server.adapters`.

Ordem do arquivo: as funções puras, a forma do JSON (ida e volta) e o cálculo; depois
`compute_report`, `freeze_report`, `get_report`, `list_report_versions` e `resend_summary`; no fim,
o tratador `report.freeze`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from cq_server import reportrender, teamnotices  # noqa: F401 - dependências (o resumo de fechamento)
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import Settings, redaction_secrets
from cq_server.events import EventType, append_event, load_write_identities
from cq_server.ports.delivery import DeliveryChannel, MessageFormatter
from cq_server.redaction import KnownIdentity, redact
from cq_server.tasks import PermanentTaskError

if TYPE_CHECKING:
    from cq_server.dispatches import DispatchStateConflict
    from cq_server.offerings import InvalidState
    from cq_server.tasks import ClaimedTask

# Constantes configuráveis no código, como as cadências da entrega das questões.
FREEZE_TASK_TYPE = "report.freeze"
FREEZE_WAIT_SECONDS = 600
REPORT_SCHEMA_VERSION = "1"
TOP_ITEMS = 5
TOP_MIN_COUNT = 2
RATIO_DIGITS = 3
ITEM_MAX_CHARS = 200

# Os conjuntos fechados de valores do relatório.
DISPATCH_STATES = ("open", "closed")
ORIGINS = ("cli", "api", "mcp", "integration")
FIELD_TYPES = ("number", "integer", "text", "single_choice", "multiple_choice")
CHOICE_FIELD_TYPES = ("single_choice", "multiple_choice")
NUMERIC_FIELD_TYPES = ("number", "integer")
UNAVAILABLE_VALUES = ("purged",)
PURGED = "purged"
# Os tipos de campo cuja distribuição fica indisponível depois do expurgo.
PURGED_FIELD_TYPES = ("text", *NUMERIC_FIELD_TYPES)
# Os motivos de `reach.not_delivered_by_reason` e os estágios de `reach.question_failed_by_stage`.
NOT_DELIVERED_REASONS = ("endpoint_blocked", "channel_refused", "engine_unavailable", "bot_no_access",
                         "channel_unavailable")
QUESTION_FAILED_STAGES = ("preparation", "grading")
# As falhas de questão de cada estágio.
PREPARATION_FAILURES = ("variant_broken", "variant_unsupported", "channel_limits_exceeded", "engine_rejected")
GRADING_FAILURES = ("grading_broken",)


class ReportTrigger(StrEnum):
    """O gatilho de uma versão congelada: o fechamento ou o reenvio."""

    CLOSING = "closing"
    RESEND = "resend"


# --------------------------------------------------------------------------------------------------
# O objeto `Report`, na ordem do JSON
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """Uma contagem em par: só no prazo e com as atrasadas."""

    on_time: int
    with_late: int


@dataclass(frozen=True)
class RatePair:
    """Uma fração de 0 a 1 em par; `None` no denominador zero, em cada lado."""

    on_time: float | None
    with_late: float | None


@dataclass(frozen=True)
class FrozenRef:
    """Uma versão congelada: o número (1, 2, …), o gatilho (`closing` · `resend`) e o horário (UTC)."""

    version: int
    trigger: str
    frozen_at: datetime


@dataclass(frozen=True)
class TopItem:
    """Um item de uma lista dos mais comuns: o valor (redigido e truncado) e a contagem em par."""

    value: str
    count: Pair


@dataclass(frozen=True)
class TopList:
    """Os mais comuns (até `TOP_ITEMS`, com pelo menos `TOP_MIN_COUNT` no lado "com atrasadas") e a
    soma dos demais em `others`."""

    items: tuple[TopItem, ...]
    others: Pair


@dataclass(frozen=True)
class ChoiceItem:
    """Uma alternativa escolhida: o `content_hash` da IR, o texto (redigido e truncado) e a contagem."""

    content_hash: str
    text: str
    count: Pair


@dataclass(frozen=True)
class FieldDistribution:
    """A distribuição de um campo da IR: escolhas → `choices`; número e
    inteiro → `values`, `ratios` e `unrated`; texto → `texts`; os demais `None`."""

    index: int
    type: str
    label: str | None
    unavailable: str | None
    choices: tuple[ChoiceItem, ...] | None
    values: TopList | None
    ratios: TopList | None
    unrated: Pair | None
    texts: TopList | None


@dataclass(frozen=True)
class Distribution:
    """`distribution`: um item por campo da IR, em ordem de índice."""

    fields: tuple[FieldDistribution, ...]


@dataclass(frozen=True)
class FieldFriction:
    """A fricção de um campo da IR: os erros de formato e a vírgula decimal, em par."""

    index: int
    format_errors: Pair
    decimal_comma: Pair


@dataclass(frozen=True)
class Friction:
    """`friction`: um item por campo da IR e os erros de formato fora da IR."""

    fields: tuple[FieldFriction, ...]
    unmapped_format_errors: Pair


@dataclass(frozen=True)
class NotDeliveredByReason:
    """`reach.not_delivered_by_reason` (objeto fechado nos 5 motivos)."""

    endpoint_blocked: int
    channel_refused: int
    engine_unavailable: int
    bot_no_access: int
    channel_unavailable: int


@dataclass(frozen=True)
class QuestionFailedByStage:
    """`reach.question_failed_by_stage`: preparação e correção."""

    preparation: int
    grading: int


@dataclass(frozen=True)
class Reach:
    """O alcance."""

    target: int
    delivered: int
    not_delivered: int
    not_delivered_by_reason: NotDeliveredByReason
    question_failed: int
    question_failed_by_stage: QuestionFailedByStage
    pending: int


@dataclass(frozen=True)
class Performance:
    """O desempenho: as contagens em par e as taxas em `RatePair`."""

    answered: Pair
    correct: Pair
    partially_correct: Pair
    incorrect: Pair
    dont_know: Pair
    correct_rate: RatePair
    partially_correct_rate: RatePair
    incorrect_rate: RatePair
    response_rate: RatePair
    mean_score: RatePair


@dataclass(frozen=True)
class ResponseTime:
    """O tempo até responder, só no prazo: `None` nos três sem observações."""

    observations: int
    negative: int
    median_ms: float | None
    q1_ms: float | None
    q3_ms: float | None


@dataclass(frozen=True)
class QuestionHeader:
    """`dispatch.question`: o QID (texto do operador) e o commit curto (12 dígitos hexadecimais)."""

    qid: str
    commit_short: str


@dataclass(frozen=True)
class DispatchHeader:
    """`dispatch`: `partial` verdadeiro se e só se `state == "open"`; a origem é
    a declarada."""

    dispatch_id: uuid.UUID
    question: QuestionHeader
    title: str | None
    state: str
    partial: bool
    origin: str
    opens_at: datetime
    closes_at: datetime


@dataclass(frozen=True)
class OfferingRef:
    """`offering`: o oferecimento do disparo (`offering_id` e `code`), como o `OfferingRef` da API."""

    offering_id: uuid.UUID
    code: str


@dataclass(frozen=True)
class Report:
    """O relatório de um disparo, já redigido; `frozen` é `None` no cálculo no
    momento e no `content` gravado (a leitura de uma versão o preenche)."""

    report_schema_version: str
    offering: OfferingRef
    timezone: str
    dispatch: DispatchHeader
    computed_at: datetime
    frozen: FrozenRef | None
    grading_pending: int
    reach: Reach
    performance: Performance
    response_time: ResponseTime
    distribution: Distribution
    friction: Friction


# --------------------------------------------------------------------------------------------------
# Resultados das operações sobre versões e sentinela
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportVersionsPage:
    """Uma página de `reports.list_versions`, em ordem de versão;
    `next_cursor` = o número da última versão da página, ou o `after` recebido sem itens."""

    offering_id: uuid.UUID
    code: str
    dispatch_id: uuid.UUID
    items: tuple[FrozenRef, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class ResendResult:
    """O resultado de `reports.resend_summary`: a versão congelada agora, com
    `trigger = "resend"`. Distinto de `cq_server.students.ResendResult`."""

    offering_id: uuid.UUID
    code: str
    dispatch_id: uuid.UUID
    frozen: FrozenRef


@dataclass(frozen=True)
class ReportVersionNotFound:
    """404 `not_found`: a versão pedida não existe, ou `version=latest` sem versão congelada."""


# --------------------------------------------------------------------------------------------------
# Funções puras
# --------------------------------------------------------------------------------------------------

# A marca do item cortado em `ITEM_MAX_CHARS` (a mesma do eco da resposta, `deliveries.py`).
ELLIPSIS = "…"

# Os estados do oferecimento em que o reenvio vale (senão `invalid_state`).
_RESEND_OFFERING_STATES = ("active", "closed")


def round_sig(x: float, digits: int = RATIO_DIGITS) -> str:
    """O número com `digits` algarismos significativos, escrito como `format(x, ".3g")`."""
    return format(x, f".{digits}g")


def normalize_text_answer(text: str) -> str:
    """`" ".join(text.split())`: pontas aparadas e espaços internos colapsados, caixa preservada."""
    return " ".join(text.split())


def top_list(counts: Mapping[str, Pair], *, limit: int = TOP_ITEMS, min_count: int = TOP_MIN_COUNT) -> TopList:
    """Os mais comuns: ordem por contagem com atrasadas decrescente e depois pela cadeia; o mínimo
    vale no lado "com atrasadas"; os demais somados em `others`."""
    items: list[TopItem] = []
    others_on_time = others_with_late = 0
    for value, count in sorted(counts.items(), key=lambda entry: (-entry[1].with_late, entry[0])):
        if len(items) < limit and count.with_late >= min_count:
            items.append(TopItem(value, count))
        else:
            others_on_time += count.on_time
            others_with_late += count.with_late
    return TopList(items=tuple(items), others=Pair(others_on_time, others_with_late))


# --------------------------------------------------------------------------------------------------
# Forma do JSON (ida e volta sem perda)
# --------------------------------------------------------------------------------------------------


def _utc_text(moment: datetime) -> str:
    """O horário em ISO 8601 UTC com `Z` (a forma de `api.schemas.timestamp`)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("horário sem fuso")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    """Um valor do `Report` na forma do JSON: os objetos na ordem dos campos (a do contrato)."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return _utc_text(value)
    return value


def report_to_dict(report: Report) -> dict[str, Any]:
    """O `Report` na forma do JSON do contrato (horários em ISO 8601 UTC, UUIDs como texto)."""
    data = _json_value(report)
    assert isinstance(data, dict)
    return data


def _moment(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("horário sem fuso")
    return moment.astimezone(timezone.utc)


def _real(value: Any) -> float | None:
    return None if value is None else float(value)


def _pair_of(data: Mapping[str, Any]) -> Pair:
    return Pair(int(data["on_time"]), int(data["with_late"]))


def _rate_of(data: Mapping[str, Any]) -> RatePair:
    return RatePair(_real(data["on_time"]), _real(data["with_late"]))


def _top_of(data: Mapping[str, Any] | None) -> TopList | None:
    if data is None:
        return None
    return TopList(items=tuple(TopItem(str(item["value"]), _pair_of(item["count"])) for item in data["items"]),
                   others=_pair_of(data["others"]))


def _field_of(data: Mapping[str, Any]) -> FieldDistribution:
    choices = data["choices"]
    return FieldDistribution(
        index=int(data["index"]), type=str(data["type"]), label=data["label"], unavailable=data["unavailable"],
        choices=None if choices is None else tuple(
            ChoiceItem(str(c["content_hash"]), str(c["text"]), _pair_of(c["count"])) for c in choices),
        values=_top_of(data["values"]), ratios=_top_of(data["ratios"]),
        unrated=None if data["unrated"] is None else _pair_of(data["unrated"]), texts=_top_of(data["texts"]))


def report_from_dict(data: Mapping[str, Any]) -> Report:
    """O inverso de `report_to_dict` (o `content` gravado de uma versão, com `frozen` nulo)."""
    offering, dispatch, frozen = data["offering"], data["dispatch"], data["frozen"]
    reach, perf, times = data["reach"], data["performance"], data["response_time"]
    reasons, stages = reach["not_delivered_by_reason"], reach["question_failed_by_stage"]
    friction = data["friction"]
    return Report(
        report_schema_version=str(data["report_schema_version"]),
        offering=OfferingRef(offering_id=uuid.UUID(str(offering["offering_id"])), code=str(offering["code"])),
        timezone=str(data["timezone"]),
        dispatch=DispatchHeader(
            dispatch_id=uuid.UUID(str(dispatch["dispatch_id"])),
            question=QuestionHeader(qid=str(dispatch["question"]["qid"]),
                                    commit_short=str(dispatch["question"]["commit_short"])),
            title=dispatch["title"], state=str(dispatch["state"]), partial=bool(dispatch["partial"]),
            origin=str(dispatch["origin"]), opens_at=_moment(dispatch["opens_at"]),
            closes_at=_moment(dispatch["closes_at"])),
        computed_at=_moment(data["computed_at"]),
        frozen=None if frozen is None else FrozenRef(version=int(frozen["version"]), trigger=str(frozen["trigger"]),
                                                     frozen_at=_moment(frozen["frozen_at"])),
        grading_pending=int(data["grading_pending"]),
        reach=Reach(
            target=int(reach["target"]), delivered=int(reach["delivered"]), not_delivered=int(reach["not_delivered"]),
            not_delivered_by_reason=NotDeliveredByReason(**{k: int(reasons[k]) for k in NOT_DELIVERED_REASONS}),
            question_failed=int(reach["question_failed"]),
            question_failed_by_stage=QuestionFailedByStage(**{k: int(stages[k]) for k in QUESTION_FAILED_STAGES}),
            pending=int(reach["pending"])),
        performance=Performance(
            answered=_pair_of(perf["answered"]), correct=_pair_of(perf["correct"]),
            partially_correct=_pair_of(perf["partially_correct"]), incorrect=_pair_of(perf["incorrect"]),
            dont_know=_pair_of(perf["dont_know"]), correct_rate=_rate_of(perf["correct_rate"]),
            partially_correct_rate=_rate_of(perf["partially_correct_rate"]),
            incorrect_rate=_rate_of(perf["incorrect_rate"]), response_rate=_rate_of(perf["response_rate"]),
            mean_score=_rate_of(perf["mean_score"])),
        response_time=ResponseTime(observations=int(times["observations"]), negative=int(times["negative"]),
                                   median_ms=_real(times["median_ms"]), q1_ms=_real(times["q1_ms"]),
                                   q3_ms=_real(times["q3_ms"])),
        distribution=Distribution(fields=tuple(_field_of(item) for item in data["distribution"]["fields"])),
        friction=Friction(
            fields=tuple(FieldFriction(int(item["index"]), _pair_of(item["format_errors"]),
                                       _pair_of(item["decimal_comma"])) for item in friction["fields"]),
            unmapped_format_errors=_pair_of(friction["unmapped_format_errors"])),
    )


# --------------------------------------------------------------------------------------------------
# Cálculo
# --------------------------------------------------------------------------------------------------

# Os estados de entrega que passaram pela entrega da questão (`reach.delivered`).
_DELIVERED_STATES = ("delivered", "answered", "dont_know", "no_answer", "answered_late")


class _Counter:
    """Contagens em par por chave (no prazo = `NOT late`; com atrasadas = todas)."""

    def __init__(self) -> None:
        self._counts: dict[str, list[int]] = {}

    def bump(self, key: str, late: bool) -> None:
        count = self._counts.setdefault(key, [0, 0])
        if not late:
            count[0] += 1
        count[1] += 1

    def pairs(self) -> dict[str, Pair]:
        return {key: Pair(on_time, with_late) for key, (on_time, with_late) in self._counts.items()}


class _Side:
    """Uma contagem em par simples."""

    def __init__(self) -> None:
        self.on_time = 0
        self.with_late = 0

    def bump(self, late: bool) -> None:
        if not late:
            self.on_time += 1
        self.with_late += 1

    def pair(self) -> Pair:
        return Pair(self.on_time, self.with_late)


def _ratio(numerator: float, denominator: int) -> float | None:
    """A fração, ou `None` no denominador zero."""
    return None if denominator == 0 else numerator / denominator


def _clip(text: str) -> str:
    """O texto cortado em `ITEM_MAX_CHARS` pontos de código, com "…" (idempotente)."""
    return text if len(text) <= ITEM_MAX_CHARS else text[:ITEM_MAX_CHARS] + ELLIPSIS


def _untrusted(text: str, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> str:
    """Um texto de terceiros ou do aluno na saída: redigido e cortado em `ITEM_MAX_CHARS`."""
    return _clip(redact(text, identities, secrets=secrets))


def _untrusted_title(text: str, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> str:
    """O título do disparo na saída: redigido, sem corte. O corte
    em `ITEM_MAX_CHARS` vale só para os itens da distribuição; o resumo corta o título só para caber na mensagem."""
    return redact(text, identities, secrets=secrets)


def _operator_text(text: str, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> str:
    """O `qid` (texto do operador) redigido como no disparo (`dispatches._redact_operator_text`)."""
    from cq_server import dispatches  # só aqui (sem ciclo)

    return dispatches._redact_operator_text(text, identities, secrets)


@dataclass(frozen=True)
class _Submission:
    delivery_id: int
    outcome: str
    valid: bool | None
    late: bool
    score: float | None
    raw_input: Mapping[str, Any]
    normalized: tuple[str, ...]
    format_error_keys: tuple[str, ...]
    parts: Mapping[int, Mapping[str, Any]]


def _parts_by_index(part_derived: Any) -> dict[int, Mapping[str, Any]]:
    """Os itens de `part_derived` (`grading.py:_parts`) pelo índice do campo da IR."""
    out: dict[int, Mapping[str, Any]] = {}
    if isinstance(part_derived, list):
        for position, item in enumerate(part_derived):
            if isinstance(item, Mapping):
                index = item.get("index")
                out.setdefault(index if isinstance(index, int) else position, item)
    return out


def _compute(conn: psycopg.Connection, dispatch_row_id: int, *, now: datetime, identities: Sequence[KnownIdentity],
             secrets: Sequence[str | None], formatter: MessageFormatter) -> Report | DispatchStateConflict:
    """O cálculo do relatório, sem trava, com a redação pelas identidades e segredos passados."""
    from cq_server import answers  # só aqui: `raw_for_engine`, a única normalização
    from cq_server.dispatches import DispatchStateConflict

    head = conn.execute(
        "SELECT s.dispatch_uuid, s.state, s.origin, s.opens_at, s.closes_at, q.qid, q.commit_sha, q.title, "
        "o.offering_id, o.code, o.timezone, o.purged_at FROM dispatches s "
        "JOIN question_refs q ON q.id = s.question_ref_id "
        "JOIN offerings o ON o.id = s.offering_id WHERE s.id = %s", (dispatch_row_id,)).fetchone()
    if head is None:
        raise ValueError("disparo inexistente")
    (dispatch_uuid, state, origin, opens_at, closes_at, qid, commit_sha, title, offering_id, code,
     zone, purged_at) = head
    if state not in DISPATCH_STATES:
        return DispatchStateConflict(state=state)

    # Alcance.
    reasons = dict.fromkeys(NOT_DELIVERED_REASONS, 0)
    stages = dict.fromkeys(QUESTION_FAILED_STAGES, 0)
    target = delivered = not_delivered = question_failed = pending = 0
    for delivery_state, reason, failure in conn.execute(
            "SELECT state, undelivered_reason, failure FROM deliveries WHERE dispatch_id = %s ORDER BY id",
            (dispatch_row_id,)).fetchall():
        target += 1
        if delivery_state in _DELIVERED_STATES:
            delivered += 1
        elif delivery_state == "not_delivered":
            not_delivered += 1
            if reason in reasons:
                reasons[reason] += 1
        elif delivery_state == "question_failed":
            question_failed += 1
            if failure in PREPARATION_FAILURES:
                stages["preparation"] += 1
            elif failure in GRADING_FAILURES:
                stages["grading"] += 1
        elif delivery_state == "pending":
            pending += 1

    # Os campos das IRs gravadas (só `fields`: tipo, nome, rótulo e alternativas), em ordem de entrega.
    ir_fields: dict[int, list[Mapping[str, Any]]] = {}
    for delivery_id, stored in conn.execute(
            "SELECT c.delivery_id, c.ir -> 'fields' FROM delivery_contents c JOIN deliveries d ON d.id = c.delivery_id "
            "WHERE d.dispatch_id = %s ORDER BY c.delivery_id", (dispatch_row_id,)).fetchall():
        if isinstance(stored, list):
            ir_fields[int(delivery_id)] = [f for f in stored if isinstance(f, Mapping)]
    meta: list[Mapping[str, Any]] = next(iter(ir_fields.values()), [])
    name_index = {str(f.get("name")): i for i, f in enumerate(meta)}
    # O HTML de cada alternativa (por campo e `content_hash`), da entrega de menor `id` que a tem.
    option_html: list[dict[str, str]] = [{} for _ in meta]
    for stored_fields in ir_fields.values():
        for i, field_ir in enumerate(stored_fields[:len(meta)]):
            for option in field_ir.get("options") or ():
                if isinstance(option, Mapping) and isinstance(option.get("content_hash"), str):
                    option_html[i].setdefault(option["content_hash"], str(option.get("html") or ""))

    # Submissões: só as colunas permitidas; de `format_errors`, só as chaves; `raw_input` só das finais.
    submissions = [
        _Submission(delivery_id=int(row[0]), outcome=row[1], valid=row[2], late=bool(row[3]),
                    score=None if row[4] is None else float(row[4]),
                    raw_input=row[5] if isinstance(row[5], Mapping) else {}, normalized=tuple(row[6] or ()),
                    format_error_keys=tuple(row[7] or ()), parts=_parts_by_index(row[8]))
        for row in conn.execute(
            "SELECT s.delivery_id, s.outcome, s.valid, s.late, s.score, "
            "CASE WHEN s.outcome = 'graded' AND s.valid THEN s.raw_input END, s.normalized_fields, "
            "CASE WHEN s.outcome = 'graded' AND NOT s.valid AND jsonb_typeof(s.format_errors) = 'object' "
            "THEN ARRAY(SELECT jsonb_object_keys(s.format_errors)) END, "
            "CASE WHEN s.outcome = 'graded' AND s.valid THEN s.part_derived END "
            "FROM submissions s JOIN deliveries d ON d.id = s.delivery_id WHERE d.dispatch_id = %s ORDER BY s.id",
            (dispatch_row_id,)).fetchall()]

    # Desempenho.
    answered, correct, partial, incorrect, dont_know = _Side(), _Side(), _Side(), _Side(), _Side()
    score_sum = [0.0, 0.0]
    grading_pending = 0
    for sub in submissions:
        if sub.outcome == "dont_know":
            dont_know.bump(sub.late)
        elif sub.outcome == "grading" and not sub.late:
            grading_pending += 1
        elif sub.outcome == "graded" and sub.valid:
            answered.bump(sub.late)
            if sub.score is not None:
                if sub.score >= 1:
                    correct.bump(sub.late)
                elif sub.score > 0:
                    partial.bump(sub.late)
                else:
                    incorrect.bump(sub.late)
                clamped = min(max(sub.score, 0.0), 1.0)
                if not sub.late:
                    score_sum[0] += clamped
                score_sum[1] += clamped

    def rates(side: _Side) -> RatePair:
        return RatePair(_ratio(side.on_time, delivered), _ratio(side.with_late, delivered))

    performance = Performance(
        answered=answered.pair(), correct=correct.pair(), partially_correct=partial.pair(),
        incorrect=incorrect.pair(), dont_know=dont_know.pair(), correct_rate=rates(correct),
        partially_correct_rate=rates(partial), incorrect_rate=rates(incorrect), response_rate=rates(answered),
        mean_score=RatePair(_ratio(score_sum[0], answered.on_time), _ratio(score_sum[1], answered.with_late)))

    # Tempo até responder: `percentile_cont` (inclusivo), só no prazo.
    observations, negative, median_ms, q1_ms, q3_ms = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE s.response_ms < 0), "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY s.response_ms), "
        "percentile_cont(0.25) WITHIN GROUP (ORDER BY s.response_ms), "
        "percentile_cont(0.75) WITHIN GROUP (ORDER BY s.response_ms) "
        "FROM submissions s JOIN deliveries d ON d.id = s.delivery_id WHERE d.dispatch_id = %s "
        "AND s.outcome = 'graded' AND s.valid AND NOT s.late AND s.response_ms IS NOT NULL",
        (dispatch_row_id,)).fetchone()
    response_time = ResponseTime(observations=int(observations), negative=int(negative), median_ms=_real(median_ms),
                                 q1_ms=_real(q1_ms), q3_ms=_real(q3_ms))

    # Distribuição e fricção.
    choice_counts = [_Counter() for _ in meta]
    value_counts = [_Counter() for _ in meta]
    ratio_counts = [_Counter() for _ in meta]
    text_counts = [_Counter() for _ in meta]
    unrated = [_Side() for _ in meta]
    format_errors = [_Side() for _ in meta]
    decimal_comma = [_Side() for _ in meta]
    unmapped = _Side()
    for sub in submissions:
        if sub.outcome != "graded":
            continue
        if not sub.valid:
            for key in sub.format_error_keys:
                index = name_index.get(key)
                (unmapped if index is None else format_errors[index]).bump(sub.late)
        for name in sub.normalized:
            index = name_index.get(name)
            if index is not None:
                decimal_comma[index].bump(sub.late)
        if not sub.valid:
            continue
        engine_raw = answers.raw_for_engine({"fields": ir_fields.get(sub.delivery_id, meta)}, sub.raw_input,
                                            list(sub.normalized))
        for i, field_ir in enumerate(meta):
            field_type, name = field_ir.get("type"), str(field_ir.get("name"))
            part = sub.parts.get(i)
            if field_type in CHOICE_FIELD_TYPES:
                for content_hash in dict.fromkeys((part or {}).get("choices") or ()):
                    choice_counts[i].bump(str(content_hash), sub.late)
            elif field_type in NUMERIC_FIELD_TYPES:
                value = engine_raw.get(name)
                if isinstance(value, str):
                    value_counts[i].bump(value, sub.late)
                ratio = (part or {}).get("ratio")
                if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
                    ratio_counts[i].bump(round_sig(float(ratio)), sub.late)
                else:
                    unrated[i].bump(sub.late)
            elif field_type == "text":
                value = sub.raw_input.get(name)
                if isinstance(value, str):
                    text_counts[i].bump(normalize_text_answer(value), sub.late)

    def text(value: str) -> str:
        return _untrusted(value, identities, secrets)

    def top(counter: _Counter) -> TopList:
        found = top_list(counter.pairs())
        return TopList(items=tuple(TopItem(text(item.value), item.count) for item in found.items),
                       others=found.others)

    distribution: list[FieldDistribution] = []
    for i, field_ir in enumerate(meta):
        field_type = str(field_ir.get("type"))
        label_html = field_ir.get("label_html")
        label = None if not isinstance(label_html, str) else text(formatter.plain(label_html))
        choices = values = ratios = texts = None
        rated: Pair | None = None
        unavailable: str | None = None
        if purged_at is not None and field_type in PURGED_FIELD_TYPES:
            # Depois do expurgo, as respostas de texto e de número ficam indisponíveis.
            unavailable = PURGED
        elif field_type in CHOICE_FIELD_TYPES:
            ordered = sorted(choice_counts[i].pairs().items(), key=lambda entry: (-entry[1].with_late, entry[0]))
            choices = tuple(ChoiceItem(content_hash, text(formatter.plain(option_html[i].get(content_hash, ""))),
                                       count) for content_hash, count in ordered)
        elif field_type in NUMERIC_FIELD_TYPES:
            values, ratios, rated = top(value_counts[i]), top(ratio_counts[i]), unrated[i].pair()
        elif field_type == "text":
            texts = top(text_counts[i])
        distribution.append(FieldDistribution(index=i, type=field_type, label=label, unavailable=unavailable,
                                              choices=choices, values=values, ratios=ratios, unrated=rated,
                                              texts=texts))

    if opens_at is None or closes_at is None:
        raise ValueError("disparo aberto ou fechado sem janela")
    return Report(
        report_schema_version=REPORT_SCHEMA_VERSION,
        offering=OfferingRef(offering_id=offering_id, code=code),
        timezone=zone,
        dispatch=DispatchHeader(
            dispatch_id=dispatch_uuid,
            question=QuestionHeader(qid=_operator_text(str(qid), identities, secrets),
                                    commit_short=str(commit_sha)[:12]),
            title=None if title is None else _untrusted_title(str(title), identities, secrets), state=state,
            partial=state == "open", origin=origin, opens_at=opens_at.astimezone(timezone.utc),
            closes_at=closes_at.astimezone(timezone.utc)),
        computed_at=now.astimezone(timezone.utc),
        frozen=None,
        grading_pending=grading_pending,
        reach=Reach(target=target, delivered=delivered, not_delivered=not_delivered,
                    not_delivered_by_reason=NotDeliveredByReason(**reasons), question_failed=question_failed,
                    question_failed_by_stage=QuestionFailedByStage(**stages), pending=pending),
        performance=performance,
        response_time=response_time,
        distribution=Distribution(fields=tuple(distribution)),
        friction=Friction(fields=tuple(FieldFriction(i, format_errors[i].pair(), decimal_comma[i].pair())
                                       for i in range(len(meta))),
                          unmapped_format_errors=unmapped.pair()),
    )


def _redact_report(report: Report, identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> Report:
    """A versão congelada redigida de novo na saída: todo `x-untrusted` e o
    `qid`, com as identidades e os segredos do momento da leitura."""

    def text(value: str) -> str:
        return _untrusted(value, identities, secrets)

    def top(found: TopList | None) -> TopList | None:
        if found is None:
            return None
        return TopList(items=tuple(TopItem(text(item.value), item.count) for item in found.items),
                       others=found.others)

    distribution = tuple(
        replace(item, label=None if item.label is None else text(item.label),
                choices=None if item.choices is None else tuple(replace(c, text=text(c.text)) for c in item.choices),
                values=top(item.values), ratios=top(item.ratios), texts=top(item.texts))
        for item in report.distribution.fields)
    question = report.dispatch.question
    title = report.dispatch.title
    dispatch = replace(report.dispatch, title=None if title is None else _untrusted_title(title, identities, secrets),
                       question=replace(question, qid=_operator_text(question.qid, identities, secrets)))
    return replace(report, dispatch=dispatch, distribution=Distribution(fields=distribution))


@dataclass(frozen=True)
class _Located:
    """O oferecimento e o disparo de `ref`/`dispatch_id` (o disparo `None` quando não é dele)."""

    offering_id: uuid.UUID
    code: str
    dispatch_row_id: int | None
    state: str | None


def _locate(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID) -> _Located | None:
    """404 do oferecimento → 404 do disparo (inexistente ou de outro oferecimento), sem trava."""
    from cq_server import offerings  # só aqui (sem ciclo)

    if offerings.is_offering_id(ref):
        condition, value = "o.offering_id = %s", uuid.UUID(ref)
    elif offerings.is_code(ref):
        condition, value = "o.code = %s", ref
    else:
        raise ValueError("referência de oferecimento fora do formato")
    row = conn.execute(
        "SELECT o.offering_id, o.code, s.id, s.state FROM offerings o "
        f"LEFT JOIN dispatches s ON s.offering_id = o.id AND s.dispatch_uuid = %s WHERE {condition}",
        (dispatch_id, value)).fetchone()
    if row is None:
        return None
    return _Located(offering_id=row[0], code=row[1], dispatch_row_id=None if row[2] is None else int(row[2]),
                    state=row[3])


# --------------------------------------------------------------------------------------------------
# Cálculo, congelamento e operações
# --------------------------------------------------------------------------------------------------


def compute_report(conn: psycopg.Connection, dispatch_row_id: int, *, now: datetime,
                   identities: Sequence[KnownIdentity],
                   formatter: MessageFormatter, secrets: Sequence[str | None] = ()) -> Report | DispatchStateConflict:
    """O relatório no momento, só para disparos `open` ou `closed`.

    Lê sem trava só `dispatches`, `question_refs` (e o `OfferingRef`), `deliveries`, os `fields` de
    `delivery_contents.ir` e `submissions` (de `format_errors`, só as chaves); nunca o motor, o canal,
    o gabarito, `submitted_answers` nem `free_messages`. O rótulo e as alternativas passam só por
    `formatter.plain`; todo texto de terceiros e do aluno é redigido com `identities` e cortado
    em `ITEM_MAX_CHARS`. `secrets`: os valores literais trocados por `[segredo]` (o token inicial
    configurado; nenhum segredo sai).

    Oferecimento expurgado (`offerings.purged_at` preenchido): os campos `text`, `number`
    e `integer` saem com `unavailable = "purged"` e `values`, `ratios`, `unrated`, `texts` nulos; o resto
    não muda. As versões congeladas não passam por aqui."""
    return _compute(conn, dispatch_row_id, now=now, identities=identities, secrets=secrets, formatter=formatter)


@dataclass(frozen=True)
class _Frozen:
    """A versão gravada agora: o `id` da linha (o `report_id` do aviso) e a referência pública."""

    row_id: int
    ref: FrozenRef


def _freeze(conn: psycopg.Connection, dispatch_row_id: int, *, trigger: ReportTrigger, now: datetime,
            identities: Sequence[KnownIdentity], formatter: MessageFormatter,
            actor_token_id: str | None, secrets: Sequence[str | None] = ()) -> _Frozen | None:
    """Cálculo → `INSERT` da versão `max + 1` → `report.frozen`, na transação do chamador (que já travou o
    disparo e tomou o lock dos eventos com `load_write_identities`). `None` = conflito
    (a versão `closing` já existe: a reexecução do congelamento). O `content` gravado sai redigido
    com as `identities` e os `secrets` configurados (o token inicial; regra de privacidade)."""
    from cq_server.dispatches import DispatchStateConflict

    report = _compute(conn, dispatch_row_id, now=now, identities=identities, secrets=secrets, formatter=formatter)
    if isinstance(report, DispatchStateConflict):
        raise ValueError("congelamento de disparo que não abriu")
    # O `content` guardado tem `frozen` nulo; a leitura o preenche com a própria linha.
    content = report_to_dict(replace(report, frozen=None))
    trigger_value = ReportTrigger(trigger).value
    # `ON CONFLICT DO NOTHING` sem alvo: o índice parcial da versão `closing` (a reexecução) e a unicidade
    # `(dispatch_id, version)`, que o disparo travado já serializa.
    row = conn.execute(
        "INSERT INTO dispatch_reports (report_uuid, dispatch_id, version, trigger, content, frozen_at) "
        "SELECT %s, %s, coalesce(max(version), 0) + 1, %s, %s, %s FROM dispatch_reports WHERE dispatch_id = %s "
        "ON CONFLICT DO NOTHING RETURNING id, version",
        (uuid.uuid4(), dispatch_row_id, trigger_value, Jsonb(content), now, dispatch_row_id),
    ).fetchone()
    if row is None:
        return None
    keys = conn.execute("SELECT s.dispatch_uuid, o.offering_id FROM dispatches s "
                        "JOIN offerings o ON o.id = s.offering_id WHERE s.id = %s", (dispatch_row_id,)).fetchone()
    assert keys is not None
    append_event(conn, EventType.REPORT_FROZEN,
                 {"offering_id": keys[1], "dispatch_id": keys[0], "version": int(row[1]), "trigger": trigger_value},
                 occurred_at=now, actor_token_id=actor_token_id, identities=identities)
    return _Frozen(row_id=int(row[0]), ref=FrozenRef(version=int(row[1]), trigger=trigger_value, frozen_at=now))


def freeze_report(conn: psycopg.Connection, dispatch_row_id: int, *, trigger: ReportTrigger, now: datetime,
                  identities: Sequence[KnownIdentity], formatter: MessageFormatter,
                  secrets: Sequence[str | None] = ()) -> FrozenRef | None:
    """Congela a versão seguinte; `None` = a versão `closing` já existe.
    Grava `report.frozen` (sem ator; o reenvio grava o ator do token).

    Na transação do chamador, com o disparo já travado e as `identities` de `load_write_identities` (a
    ordem de travas do relatório): calcula o relatório com tudo o que está gravado, grava a versão
    `coalesce(max, 0) + 1` com o `content` já redigido (`INSERT … ON CONFLICT DO NOTHING`) e o evento."""
    frozen = _freeze(conn, dispatch_row_id, trigger=trigger, now=now, identities=identities, formatter=formatter,
                     actor_token_id=None, secrets=secrets)
    return None if frozen is None else frozen.ref


def get_report(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *,
               version: int | Literal["latest"] | None, now: datetime, identities: Sequence[KnownIdentity],
               secrets: Sequence[str | None],
               formatter: MessageFormatter) -> Report | DispatchStateConflict | ReportVersionNotFound | None:
    """`reports.get`: `version=None` = o cálculo no momento; `None` = oferecimento ou
    disparo inexistente.

    Ordem das regras: 404 do oferecimento → 404 do disparo → `invalid_dispatch_state`
    (só `open` e `closed` têm relatório) → 404 da versão (`ReportVersionNotFound`). A versão congelada
    sai com `frozen` preenchido pela própria linha e redigida de novo com as identidades do momento."""
    from cq_server.dispatches import DispatchStateConflict

    found = _locate(conn, ref, dispatch_id)
    if found is None or found.dispatch_row_id is None or found.state is None:
        return None
    if found.state not in DISPATCH_STATES:
        return DispatchStateConflict(state=found.state)
    if version is None:
        return _compute(conn, found.dispatch_row_id, now=now, identities=identities, secrets=secrets,
                        formatter=formatter)
    if version == "latest":
        row = conn.execute("SELECT version, trigger, content, frozen_at FROM dispatch_reports WHERE dispatch_id = %s "
                           "ORDER BY version DESC LIMIT 1", (found.dispatch_row_id,)).fetchone()
    else:
        row = conn.execute("SELECT version, trigger, content, frozen_at FROM dispatch_reports WHERE dispatch_id = %s "
                           "AND version = %s", (found.dispatch_row_id, int(version))).fetchone()
    if row is None:
        return ReportVersionNotFound()
    frozen = FrozenRef(version=int(row[0]), trigger=str(row[1]), frozen_at=row[3].astimezone(timezone.utc))
    return _redact_report(replace(report_from_dict(row[2]), frozen=frozen), identities, secrets)


def list_report_versions(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *, after: int | None,
                         limit: int) -> ReportVersionsPage | DispatchStateConflict | None:
    """`reports.list_versions`: as versões com número maior que `after`, em ordem de
    versão, no máximo `limit`; `next_cursor` = o número da última, ou `after` sem itens. Lista
    vazia é válida no aberto e no fechado ainda sem a versão 1; `invalid_dispatch_state` nos que não
    abriram."""
    from cq_server.dispatches import DispatchStateConflict

    found = _locate(conn, ref, dispatch_id)
    if found is None or found.dispatch_row_id is None or found.state is None:
        return None
    if found.state not in DISPATCH_STATES:
        return DispatchStateConflict(state=found.state)
    start = 0 if after is None else int(after)
    rows = conn.execute("SELECT version, trigger, frozen_at FROM dispatch_reports WHERE dispatch_id = %s "
                        "AND version > %s ORDER BY version LIMIT %s",
                        (found.dispatch_row_id, start, limit)).fetchall()
    items = tuple(FrozenRef(version=int(v), trigger=str(t), frozen_at=at.astimezone(timezone.utc))
                  for v, t, at in rows)
    return ReportVersionsPage(offering_id=found.offering_id, code=found.code, dispatch_id=dispatch_id, items=items,
                              next_cursor=items[-1].version if items else start)


def resend_summary(conn: psycopg.Connection, ref: str, dispatch_id: uuid.UUID, *, now: datetime,
                   actor_token_id: str | None, formatter: MessageFormatter,
                   secrets: Sequence[str | None] = ()) -> ResendResult | InvalidState | DispatchStateConflict | None:
    """`reports.resend_summary`; `None` = oferecimento ou disparo
    inexistente (ou de outro oferecimento).

    Numa transação, na ordem de travas do relatório: oferecimento (`FOR NO KEY UPDATE`) → disparo (`FOR NO KEY
    UPDATE`: as duas tabelas são referenciadas por FK; a trava serializa a numeração e a conferência
    da versão `closing` com o congelamento e com outro reenvio) → identidades → cálculo → a versão `max + 1`
    com `trigger = resend` e o `report.frozen` com o ator → o aviso `summary_resent` `pending`
    (`report:<id>`), que a regra `notice.send` entrega (a API não compõe o texto). Ordem das regras
    de recusa: 404 → 404 → `invalid_state` (oferecimento fora de `active`/`closed`) →
    `invalid_dispatch_state` (disparo fora de `closed`, ou fechado ainda sem a versão `closing`). Não é
    idempotente: cada pedido congela uma versão. `secrets`: os valores literais redigidos no `content` (a API
    passa o `ApiContext.secrets`; nenhum segredo sai)."""
    from cq_server import offerings  # só aqui (sem ciclo)
    from cq_server.dispatches import DispatchStateConflict
    from cq_server.offerings import InvalidState

    if offerings.is_offering_id(ref):
        condition, value = "offering_id = %s", uuid.UUID(ref)
    elif offerings.is_code(ref):
        condition, value = "code = %s", ref
    else:
        raise ValueError("referência de oferecimento fora do formato")
    with conn.transaction():
        offering = conn.execute(f"SELECT id, offering_id, code, state FROM offerings WHERE {condition} "
                                "FOR NO KEY UPDATE", (value,)).fetchone()
        if offering is None:
            return None
        offering_row, offering_id, code, offering_state = int(offering[0]), offering[1], str(offering[2]), offering[3]
        dispatch = conn.execute("SELECT id, state FROM dispatches WHERE offering_id = %s AND dispatch_uuid = %s "
                                "FOR NO KEY UPDATE", (offering_row, dispatch_id)).fetchone()
        if dispatch is None:
            return None
        if offering_state not in _RESEND_OFFERING_STATES:
            return InvalidState(state=str(offering_state))
        dispatch_row, dispatch_state = int(dispatch[0]), str(dispatch[1])
        if dispatch_state != "closed":
            return DispatchStateConflict(state=dispatch_state)
        closing = conn.execute("SELECT 1 FROM dispatch_reports WHERE dispatch_id = %s AND trigger = %s",
                               (dispatch_row, ReportTrigger.CLOSING.value)).fetchone()
        if closing is None:
            # Na espera de até 10 min do congelamento: a versão 1 é sempre a do fechamento.
            return DispatchStateConflict(state=dispatch_state)
        identities = load_write_identities(conn)
        frozen = _freeze(conn, dispatch_row, trigger=ReportTrigger.RESEND, now=now, identities=identities,
                         formatter=formatter, actor_token_id=actor_token_id, secrets=secrets)
        assert frozen is not None, "o reenvio não conflita (o disparo travado serializa a numeração)"
        teamnotices.record(conn, offering_row_id=offering_row, kind=teamnotices.NoticeKind.SUMMARY_RESENT,
                           subject_key=f"report:{frozen.row_id}", data={}, now=now, dispatch_row_id=dispatch_row,
                           report_row_id=frozen.row_id)
    return ResendResult(offering_id=offering_id, code=code, dispatch_id=dispatch_id, frozen=frozen.ref)


# --------------------------------------------------------------------------------------------------
# Tratador `report.freeze`
# --------------------------------------------------------------------------------------------------


# A chave do assunto no payload da tarefa `report.freeze`.
FREEZE_SUBJECT_KEY = "dispatch_row_id"
_NO_CHANNEL = "canal sem rota"


def _closed_above_floor(conn: psycopg.Connection, dispatch_uuid: uuid.UUID) -> bool:
    """O piso por fato do resumo de fechamento: o `dispatch.closed`
    do disparo tem `seq > event_floor` (fechou depois do deploy)."""
    floor = teamnotices.floors(conn).event_floor
    row = conn.execute("SELECT 1 FROM events WHERE type = 'dispatch.closed' AND data->>'dispatch_id' = %s "
                       "AND seq > %s LIMIT 1", (str(dispatch_uuid), floor)).fetchone()
    return row is not None


def _freeze_on_close(conn: psycopg.Connection, dispatch_row_id: int, *, now: datetime, channel: DeliveryChannel,
                     formatter: MessageFormatter, secrets: Sequence[str | None] = ()) -> None:
    """O congelamento do fechamento e o resumo à equipe, numa transação, na ordem
    de travas do relatório: disparo (`FOR NO KEY UPDATE`) → avisos `pending` do disparo (`FOR NO KEY UPDATE`, em
    ordem de `id`) → identidades (o lock dos eventos) → cálculo → versão `closing` → `report.frozen` →
    entrega dos `pending` do disparo (o "aberto" antecipado) → registro e entrega do
    `dispatch_closed` (ou `not_sent`, fechado no piso ou abaixo dele). A reexecução (versão `closing` já
    gravada) termina sem efeito. Os `secrets` (o token inicial configurado) redigem o `content` gravado e
    a composição dos avisos (nenhum segredo sai)."""
    with conn.transaction():
        found = conn.execute("SELECT s.state, s.offering_id, s.dispatch_uuid FROM dispatches s WHERE s.id = %s "
                             "FOR NO KEY UPDATE OF s", (dispatch_row_id,)).fetchone()
        if found is None:
            raise PermanentTaskError("disparo inexistente")
        if found[0] != "closed":
            return  # a regra só cria a tarefa de disparo fechado; outro estado não congela
        offering_row, dispatch_uuid = int(found[1]), found[2]
        pending = [int(r[0]) for r in conn.execute(
            "SELECT id FROM team_notices WHERE dispatch_id = %s AND state = 'pending' ORDER BY id FOR NO KEY UPDATE",
            (dispatch_row_id,)).fetchall()]
        identities = load_write_identities(conn)
        frozen = _freeze(conn, dispatch_row_id, trigger=ReportTrigger.CLOSING, now=now, identities=identities,
                         formatter=formatter, actor_token_id=None, secrets=secrets)
        if frozen is None:
            return
        for notice_row_id in pending:
            teamnotices.deliver(conn, notice_row_id, channel=channel, now=now, secrets=secrets)
        notice_row_id = teamnotices.record(conn, offering_row_id=offering_row,
                                           kind=teamnotices.NoticeKind.DISPATCH_CLOSED,
                                           subject_key=f"report:{frozen.row_id}", data={}, now=now,
                                           dispatch_row_id=dispatch_row_id, report_row_id=frozen.row_id)
        if notice_row_id is None:  # pragma: no cover - a versão acabou de nascer; o assunto é novo
            return
        if _closed_above_floor(conn, dispatch_uuid):
            teamnotices.deliver(conn, notice_row_id, channel=channel, now=now, secrets=secrets)
        else:
            teamnotices.finish_without_sending(conn, notice_row_id, state=teamnotices.NoticeState.NOT_SENT, now=now)


class _TaskHandler:
    """O tratador `report.freeze` (chamável, com `close()`): a conexão abre no primeiro uso; o canal (e o
    formatador dele) só é lido na execução."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel
        # Os segredos configurados (o token inicial), redigidos no `content` e nos avisos
        # (nenhum segredo sai), como o `Outbox` e a `TaskQueue`.
        self._secrets = redaction_secrets(settings)

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(FREEZE_SUBJECT_KEY) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {FREEZE_SUBJECT_KEY}")
        if self._channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        # O formatador do canal com rota, lido antes do cálculo (o rótulo e as alternativas só pelo `plain`,
        # e o resumo, pela porta, no `teamnotices.deliver`).
        formatter = self._channel.formatter
        conn = self._db.get()
        _freeze_on_close(conn, value, now=self._clock.now(), channel=self._channel, formatter=formatter,
                         secrets=self._secrets)

    def close(self) -> None:
        self._db.close()


def freeze_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                        channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador `report.freeze`: `channel = None` numa tarefa que precisa compor o
    resumo → `PermanentTaskError("canal sem rota")`. Nada lê o canal na montagem."""
    return _TaskHandler(conninfo, clock=clock, settings=settings, channel=channel)
