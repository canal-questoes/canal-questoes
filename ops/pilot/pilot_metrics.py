"""Gerador do relatório de métricas do piloto.


Uso:

    uv run --project client python ops/pilot/pilot_metrics.py --profile piloto --offering <código>
           --plan docs/operacao/piloto/plano-de-disparos.toml [--agent-token-id <uuid>] [--json] [--out <arquivo>]

Lê só pela API (`ApiClient`, só com os métodos de leitura `dispatches.list`, `reports.get`,
`students.summary`, `events.list`); nenhuma escrita. Só agregados: nenhum pseudônimo,
nome, e-mail, `user_id`, texto livre ou título de questão; o `qid` vem do plano de disparos. Taxas com
denominador 0 saem `None` (`null` no JSON).

Único script de `ops/` que importa o `cq_client` (só `api` e `profile`); nunca o `cq_server`. Importável
sem efeito colateral.

Definições (**[I]**, as do docstring do teste `tests/integration/test_pilot_metrics.py`; o formato
do relatório nomeia os campos, mas não a fonte de cada um):
- um disparo casa a vaga do plano pela referência (`repo`, `commit`, `qid`) e pelo `opens_at` (como instantes); o
  `kind`, o `qid` e o `field_type` vêm da vaga; só os disparos `open` ou `closed` contam (os outros nunca entregaram);
  os que não casam nenhuma vaga ficam de fora, e o stderr diz quantos (mensagem fixa); a lista sai na ordem do
  `opens_at`;
- `target`, `delivered` e `blocked` vêm do alcance do relatório (`reach`); as respostas, o "não sei", a falta de
  resposta, a falha de questão e a correção pendente ao fim vêm das contagens do `dispatches.list`;
- os erros de formato e a vírgula decimal: a soma de `friction.fields[]`, com as atrasadas (`with_late`), sob o
  `field_type` da vaga;
- `graded_without_internal_error`: os `submission.graded` válidos do disparo (no prazo e atrasados);
- `invite_to_link_hours`: por pessoa vinculada, as horas do primeiro `student.invite_sent` ao `student.joined`
  (o pseudônimo só na memória do processo); `resends`: os `student.invite_sent` fora do lote (`origin` ≠ `batch`);
- `agent_tasks`: os eventos do `--agent-token-id` (sem oferecimento no `data`, ou deste oferecimento); `failed` =
  as recusas de revelação (`identity.reveal_denied`), `ok` = o resto;
- `terms_refused`: `None` e a métrica manual `not_exposed` (a recusa do termo não gera
  evento e o `students.summary` não a conta).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import sys
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_client.profile import ProfileError, resolve_profile

if TYPE_CHECKING:
    import httpx2

DispatchKind = Literal["daily", "intensive"]
QuestionSource = Literal["rodolfo", "demo"]
ManualReason = Literal["diary", "not_exposed"]


@dataclass(frozen=True)
class PlanSlot:
    """Uma vaga do plano de disparos (`[[slot]]`). `session` vazio nas diárias."""

    kind: DispatchKind
    session: str
    opens_at: datetime
    closes_at: datetime
    repo: str
    commit: str
    qid: str
    field_type: str
    source: QuestionSource


@dataclass(frozen=True)
class DispatchPlan:
    """O plano de disparos: fuso, oferecimento e as vagas na ordem do arquivo."""

    timezone: str
    offering: str
    slots: tuple[PlanSlot, ...]


@dataclass(frozen=True)
class Quartiles:
    """Mediana e quartis de uma amostra."""

    median: float
    q1: float
    q3: float


@dataclass(frozen=True)
class CountedQuartiles:
    """[I] Mediana e quartis com o tamanho da amostra (`invite_to_link_hours: {median, q1, q3, n}`)."""

    median: float
    q1: float
    q3: float
    n: int


@dataclass(frozen=True)
class AgentTasks:
    """[I] As tarefas do agente (`agent_tasks: {ok, failed}`), contadas pelo `--agent-token-id`."""

    ok: int
    failed: int


@dataclass(frozen=True)
class ManualMetric:
    """Métrica que o gerador não mede: vem do diário (`diary`) ou a leitura não a expõe (`not_exposed`)."""

    name: str
    reason: ManualReason


@dataclass(frozen=True)
class DispatchMetrics:
    """As métricas de um disparo."""

    dispatch_id: str
    qid: str
    kind: DispatchKind
    opens_at: datetime
    closes_at: datetime
    target: int
    delivered: int
    blocked: int
    answered_on_time: int
    answered_late: int
    dont_know: int
    no_answer: int
    delivery_rate: float | None
    block_rate: float | None
    response_rate_on_time: float | None
    response_rate_with_late: float | None
    response_time_ms: Quartiles | None
    format_errors_by_field_type: Mapping[str, int]
    decimal_comma: int
    graded_without_internal_error: int
    question_failed: int
    grading_pending_at_end: int


@dataclass(frozen=True)
class TotalMetrics:
    """As somas e taxas do oferecimento. [I] As faixas (mín., máx.) das medianas por disparo
    ficam em `response_time_median_range`."""

    target: int
    delivered: int
    blocked: int
    answered_on_time: int
    answered_late: int
    dont_know: int
    no_answer: int
    delivery_rate: float | None
    block_rate: float | None
    response_rate_on_time: float | None
    response_rate_with_late: float | None
    response_time_median_range: tuple[float, float] | None
    format_errors_by_field_type: Mapping[str, int]
    decimal_comma: int
    graded_without_internal_error: int
    question_failed: int
    grading_pending_at_end: int
    invite_to_link_hours: CountedQuartiles | None
    resends: int
    terms_refused: int | None  # a API não expõe a recusa do termo
    agent_tasks: AgentTasks | None


@dataclass(frozen=True)
class PilotMetrics:
    """O relatório."""

    generated_at: datetime
    offering: str
    dispatches: list[DispatchMetrics]
    total: TotalMetrics
    manual: list[ManualMetric] = field(default_factory=list)


# As métricas que vêm do diário. [I] Os nomes (identificadores) são da implementação: incidentes,
# tempo de recuperação, teste de restauração, confirmações pedidas e zero vazamento.
MANUAL_METRICS: tuple[ManualMetric, ...] = (
    ManualMetric("incidents", "diary"),
    ManualMetric("recovery_time", "diary"),
    ManualMetric("restore_test", "diary"),
    ManualMetric("confirmations_requested", "diary"),
    ManualMetric("zero_leak", "diary"),
)


# As métricas que a leitura não expõe: saem "manual" com o motivo.
NOT_EXPOSED_METRICS: tuple[ManualMetric, ...] = (
    ManualMetric("terms_refused", "not_exposed"),
)

# [F] Os 48 tipos do `events.list` (o `oneOf` de `contracts/api/v1/schemas/events.list.response.schema.json`),
# pedidos todos quando há `--agent-token-id` (as tarefas do agente podem ser de qualquer tipo).
EVENT_TYPES: tuple[str, ...] = (
    "person.registered", "token.created", "token.revoked", "identity.revealed", "identity.reveal_denied",
    "task.failed", "outbox.failed", "offering.created", "offering.state_changed", "offering.archive_prepared",
    "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed", "student.list_imported", "student.invites_prepared", "student.invites_queued",
    "student.invite_sent", "student.invite_failed", "student.invite_cancelled", "student.invite_revoked",
    "student.joined", "dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
    "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered", "delivery.not_delivered",
    "delivery.question_failed", "delivery.dont_know", "submission.graded", "submission.internal_failure",
    "offering.settings_changed", "report.frozen", "notice.recorded", "offering.digest_changed",
    "offering.purge_prepared", "offering.purged",
)
INVITE_SENT = "student.invite_sent"
JOINED = "student.joined"
GRADED = "submission.graded"
REVEAL_DENIED = "identity.reveal_denied"
BASE_EVENT_TYPES: tuple[str, ...] = (INVITE_SENT, JOINED, GRADED)

# Os estados de disparo que entregaram (os outros nunca chegaram aos alunos).
COUNTED_STATES = frozenset({"open", "closed"})
# [F] O máximo de itens por página das listas da API (`server/src/cq_server/events.py`, `MAX_LIMIT`).
PAGE_LIMIT = 500

DISPATCH_KINDS: tuple[str, ...] = ("daily", "intensive")
QUESTION_SOURCES: tuple[str, ...] = ("rodolfo", "demo")
SLOT_FIELDS: tuple[str, ...] = ("kind", "session", "opens_at", "closes_at", "repo", "commit", "qid", "field_type",
                                "source")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

# Mensagens fixas (sem eco de entrada, de endereço nem de token: nenhum dado de identidade sai por elas).
MESSAGES: Mapping[str, str] = {
    "invalid_arguments": "Argumentos inválidos. Uso: pilot_metrics.py --profile <perfil> --offering <código> "
                         "--plan <arquivo> [--agent-token-id <uuid>] [--json] [--out <arquivo>]",
    "invalid_agent_token": "O --agent-token-id não é um identificador de token válido.",
    "plan_unreadable": "Não foi possível ler o plano de disparos.",
    "plan_invalid": "O plano de disparos é inválido.",
    "plan_other_offering": "O plano de disparos é de outro oferecimento.",
    "profile": "Não foi possível resolver o perfil escolhido.",
    "unreachable": "A API não respondeu.",
    "api_error": "A API recusou a leitura (HTTP {status}).",
    "unexpected": "A API respondeu fora do contrato.",
    "out_unwritable": "Não foi possível gravar o arquivo de saída.",
    "written": "Relatório gravado no arquivo de saída.",
    "unplanned": "{count} disparo(s) do oferecimento fora do plano ficaram de fora do relatório.",
}


class PlanError(ValueError):
    """O plano de disparos não pôde ser lido ou é inválido. A mensagem é fixa (`MESSAGES`), sem eco do arquivo."""

    def __init__(self, key: Literal["plan_unreadable", "plan_invalid"]) -> None:
        super().__init__(MESSAGES[key])
        self.key = key


class _UnexpectedResponse(Exception):
    """Uma resposta da API sem um campo que o cálculo usa (fora do contrato)."""


# --- plano ---------------------------------------------------------------------------------------------------------


def _plan_instant(value: object, zone: ZoneInfo) -> datetime:
    """Um horário do plano: o `datetime` do TOML ou o texto ISO 8601; sem fuso, o do plano."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise PlanError("plan_invalid") from None
    if not isinstance(value, datetime):
        raise PlanError("plan_invalid")
    return value.replace(tzinfo=zone) if value.tzinfo is None else value


def _plan_text(fields: Mapping[str, Any], key: str, *, optional: bool = False) -> str:
    """Um campo de texto da vaga; só o `session` pode faltar ou vir vazio (vazio nas diárias)."""
    value = fields.get(key, "" if optional else None)
    if not isinstance(value, str) or (not optional and not value):
        raise PlanError("plan_invalid")
    return value


def load_plan(path: Path) -> DispatchPlan:
    """Lê o plano de disparos (`tomllib`). Levanta `PlanError` (mensagem fixa)."""
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        raise PlanError("plan_invalid") from None
    except (OSError, UnicodeDecodeError):
        raise PlanError("plan_unreadable") from None
    timezone = data.get("timezone")
    offering = data.get("offering")
    raw_slots = data.get("slot", [])
    if not (isinstance(timezone, str) and timezone and isinstance(offering, str) and offering
            and isinstance(raw_slots, list)):
        raise PlanError("plan_invalid")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise PlanError("plan_invalid") from None
    slots: list[PlanSlot] = []
    for raw in raw_slots:
        if not isinstance(raw, dict) or set(raw) - set(SLOT_FIELDS):
            raise PlanError("plan_invalid")
        kind = _plan_text(raw, "kind")
        source = _plan_text(raw, "source")
        if kind not in DISPATCH_KINDS or source not in QUESTION_SOURCES:
            raise PlanError("plan_invalid")
        opens_at = _plan_instant(raw.get("opens_at"), zone)
        closes_at = _plan_instant(raw.get("closes_at"), zone)
        if closes_at <= opens_at:
            raise PlanError("plan_invalid")
        slots.append(PlanSlot(kind=kind, session=_plan_text(raw, "session", optional=True), opens_at=opens_at,
                              closes_at=closes_at, repo=_plan_text(raw, "repo"), commit=_plan_text(raw, "commit"),
                              qid=_plan_text(raw, "qid"), field_type=_plan_text(raw, "field_type"),
                              source=source))  # type: ignore[arg-type]
    return DispatchPlan(timezone=timezone, offering=offering, slots=tuple(slots))


# --- coleta --------------------------------------------------------------------------------------------------------


def _api_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise _UnexpectedResponse()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _UnexpectedResponse() from None
    if parsed.tzinfo is None:
        raise _UnexpectedResponse()
    return parsed


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _UnexpectedResponse()
    return value


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _UnexpectedResponse()
    return float(value)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _quartiles(values: Sequence[float]) -> tuple[float, float, float]:
    """(mediana, q1, q3) pelo método inclusivo (o do relatório por disparo); com um valor só, os três são ele."""
    if len(values) == 1:
        return values[0], values[0], values[0]
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return median, q1, q3


def _dispatch_pages(api: ApiClient, offering: str) -> tuple[dict, list[dict]]:
    """A primeira página (com o `offering` e o `timezone`) e todos os itens do `dispatches.list`."""
    first: dict | None = None
    items: list[dict] = []
    after: int | None = None
    while True:
        page = api.list_dispatches(offering, after=after, limit=PAGE_LIMIT)
        if first is None:
            first = page
        batch = page.get("items")
        if not isinstance(batch, list):
            raise _UnexpectedResponse()
        if not batch:
            break
        items.extend(batch)
        cursor = _int(page.get("next_cursor"))
        if after is not None and cursor <= after:
            break
        after = cursor
    assert first is not None
    return first, items


def _events(api: ApiClient, types: Sequence[str]) -> Iterator[dict]:
    """Todos os eventos dos `types`, página a página, na ordem do `seq`."""
    after: int | None = None
    while True:
        page = api.list_events(types, after=after, limit=PAGE_LIMIT)
        batch = page.get("items")
        if not isinstance(batch, list):
            raise _UnexpectedResponse()
        if not batch:
            return
        yield from batch
        cursor = _int(page.get("next_cursor"))
        if after is not None and cursor <= after:
            return
        after = cursor


def _slot_of(item: dict, plan: DispatchPlan) -> PlanSlot | None:
    question = item.get("question")
    if not isinstance(question, dict) or item.get("opens_at") is None:
        return None
    opens_at = _api_instant(item["opens_at"])
    for slot in plan.slots:
        if ((slot.repo, slot.commit, slot.qid) == (question.get("repo"), question.get("commit"), question.get("qid"))
                and slot.opens_at == opens_at):
            return slot
    return None


def _friction(report: dict, key: str) -> int:
    friction = report.get("friction")
    fields = friction.get("fields") if isinstance(friction, dict) else None
    if not isinstance(fields, list):
        raise _UnexpectedResponse()
    total = 0
    for item in fields:
        pair = item.get(key) if isinstance(item, dict) else None
        if not isinstance(pair, dict):
            raise _UnexpectedResponse()
        total += _int(pair.get("with_late"))
    return total


def _dispatch_metrics(item: dict, slot: PlanSlot, report: dict, graded: int) -> DispatchMetrics:
    counts = item.get("deliveries")
    reach = report.get("reach")
    times = report.get("response_time")
    if not (isinstance(counts, dict) and isinstance(reach, dict) and isinstance(times, dict)):
        raise _UnexpectedResponse()
    by_reason = reach.get("not_delivered_by_reason")
    if not isinstance(by_reason, dict):
        raise _UnexpectedResponse()
    target, delivered = _int(reach.get("target")), _int(reach.get("delivered"))
    blocked = _int(by_reason.get("endpoint_blocked"))
    on_time, late = _int(counts.get("answered")), _int(counts.get("answered_late"))
    median = _number(times.get("median_ms"))
    response_time = None
    if median is not None:
        q1, q3 = _number(times.get("q1_ms")), _number(times.get("q3_ms"))
        response_time = Quartiles(median=median, q1=median if q1 is None else q1, q3=median if q3 is None else q3)
    closes_at = item.get("closes_at")
    return DispatchMetrics(
        dispatch_id=str(item.get("dispatch_id")), qid=slot.qid, kind=slot.kind,
        opens_at=_api_instant(item.get("opens_at")),
        closes_at=slot.closes_at if closes_at is None else _api_instant(closes_at),
        target=target, delivered=delivered, blocked=blocked, answered_on_time=on_time, answered_late=late,
        dont_know=_int(counts.get("dont_know")), no_answer=_int(counts.get("no_answer")),
        delivery_rate=_rate(delivered, target), block_rate=_rate(blocked, target),
        response_rate_on_time=_rate(on_time, delivered), response_rate_with_late=_rate(on_time + late, delivered),
        response_time_ms=response_time,
        format_errors_by_field_type={slot.field_type: _friction(report, "format_errors")},
        decimal_comma=_friction(report, "decimal_comma"), graded_without_internal_error=graded,
        question_failed=_int(counts.get("question_failed")), grading_pending_at_end=_int(counts.get("grading_pending")))


def _of_offering(event: dict, offering_id: str) -> bool:
    data = event.get("data")
    return isinstance(data, dict) and str(data.get("offering_id", "")).lower() == offering_id


def _invite_to_link(events: Sequence[dict], offering_id: str) -> CountedQuartiles | None:
    """As horas do primeiro envio de convite ao vínculo, por pessoa. O pseudônimo só serve de chave aqui, na
    memória do processo; nada dele sai."""
    sent: dict[str, list[datetime]] = {}
    joined: dict[str, datetime] = {}
    for event in events:
        if not _of_offering(event, offering_id):
            continue
        person = event["data"].get("person")
        if not isinstance(person, str):
            continue
        when = _api_instant(event.get("occurred_at"))
        if event.get("type") == INVITE_SENT:
            sent.setdefault(person, []).append(when)
        elif event.get("type") == JOINED and (person not in joined or when < joined[person]):
            joined[person] = when
    hours = sorted((joined_at - min(before)).total_seconds() / 3600
                   for person, joined_at in joined.items()
                   if (before := [t for t in sent.get(person, []) if t <= joined_at]))
    if not hours:
        return None
    median, q1, q3 = _quartiles(hours)
    return CountedQuartiles(median=median, q1=q1, q3=q3, n=len(hours))


def collect(api: ApiClient, offering: str, plan: DispatchPlan, *, agent_token_id: str | None = None) -> PilotMetrics:
    """As métricas do `offering` pela API, só leitura. Levanta `ApiError`, `ApiUnreachable` ou
    `_UnexpectedResponse`."""
    first, items = _dispatch_pages(api, offering)
    header = first.get("offering")
    if not isinstance(header, dict) or not isinstance(header.get("offering_id"), str):
        raise _UnexpectedResponse()
    offering_id = header["offering_id"].lower()
    code = header.get("code") if isinstance(header.get("code"), str) else offering

    matched: list[tuple[dict, PlanSlot]] = []
    unplanned = 0
    for item in items:
        if item.get("state") not in COUNTED_STATES:
            continue
        slot = _slot_of(item, plan)
        if slot is None:
            unplanned += 1
        else:
            matched.append((item, slot))
    matched.sort(key=lambda pair: _api_instant(pair[0].get("opens_at")))
    if unplanned:
        print(MESSAGES["unplanned"].format(count=unplanned), file=sys.stderr)

    agent = None if agent_token_id is None else agent_token_id.lower()
    events = list(_events(api, EVENT_TYPES if agent is not None else BASE_EVENT_TYPES))
    graded: dict[str, int] = {}
    for event in events:
        data = event.get("data")
        if (event.get("type") == GRADED and isinstance(data, dict) and _of_offering(event, offering_id)
                and data.get("valid", True) is not False):
            key = str(data.get("dispatch_id", "")).lower()
            graded[key] = graded.get(key, 0) + 1

    dispatches = [
        _dispatch_metrics(item, slot, api.get_report(code, str(item.get("dispatch_id"))),
                          graded.get(str(item.get("dispatch_id")).lower(), 0))
        for item, slot in matched]

    agent_tasks = None
    if agent is not None:
        own = [e for e in events if str(e.get("actor_token_id") or "").lower() == agent
               and (not isinstance(e.get("data"), dict) or "offering_id" not in e["data"]
                    or _of_offering(e, offering_id))]
        failed = sum(1 for e in own if e.get("type") == REVEAL_DENIED)
        agent_tasks = AgentTasks(ok=len(own) - failed, failed=failed)

    medians = [d.response_time_ms.median for d in dispatches if d.response_time_ms is not None]
    format_errors: dict[str, int] = {}
    for d in dispatches:
        for field_type, count in d.format_errors_by_field_type.items():
            format_errors[field_type] = format_errors.get(field_type, 0) + count

    def total(name: str) -> int:
        return sum(getattr(d, name) for d in dispatches)

    target, delivered, blocked = total("target"), total("delivered"), total("blocked")
    on_time, late = total("answered_on_time"), total("answered_late")
    totals = TotalMetrics(
        target=target, delivered=delivered, blocked=blocked, answered_on_time=on_time, answered_late=late,
        dont_know=total("dont_know"), no_answer=total("no_answer"),
        delivery_rate=_rate(delivered, target), block_rate=_rate(blocked, target),
        response_rate_on_time=_rate(on_time, delivered), response_rate_with_late=_rate(on_time + late, delivered),
        response_time_median_range=(min(medians), max(medians)) if medians else None,
        format_errors_by_field_type=format_errors, decimal_comma=total("decimal_comma"),
        graded_without_internal_error=total("graded_without_internal_error"),
        question_failed=total("question_failed"), grading_pending_at_end=total("grading_pending_at_end"),
        invite_to_link_hours=_invite_to_link(events, offering_id),
        resends=sum(1 for e in events if e.get("type") == INVITE_SENT and _of_offering(e, offering_id)
                    and e["data"].get("origin") != "batch"),
        terms_refused=None, agent_tasks=agent_tasks)
    events.clear()
    return PilotMetrics(generated_at=datetime.now(UTC), offering=code, dispatches=dispatches, total=totals,
                        manual=[*MANUAL_METRICS, *NOT_EXPOSED_METRICS])


# --- saídas --------------------------------------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def to_json(metrics: PilotMetrics) -> str:
    """O relatório em JSON (`--json`): as chaves dos dataclasses; horários em ISO 8601, em UTC."""
    return json.dumps(_plain(dataclasses.asdict(metrics)), ensure_ascii=False, indent=2) + "\n"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


def _ms(value: float) -> str:
    return f"{value / 1000:.1f} s"


def _count_by_type(values: Mapping[str, int]) -> str:
    shown = [f"{field_type}: {count}" for field_type, count in sorted(values.items())]
    return "; ".join(shown) if shown else "—"


def to_markdown(metrics: PilotMetrics) -> str:
    """O relatório em Markdown (padrão): só agregados, com as métricas manuais marcadas."""
    t = metrics.total
    lines = [
        f"# Métricas do piloto: {metrics.offering}",
        "",
        f"Gerado em {_iso(metrics.generated_at)}. Só agregados, sem dado de pessoa.",
        "",
        "## Disparos",
        "",
        "| qid | tipo | abre (UTC) | alvo | entregues | bloqueados | no prazo | atrasadas | não sei | sem resposta "
        "| entrega | resposta no prazo | resposta com atrasadas | mediana (q1–q3) | erros de formato | vírgula "
        "| com nota | falha de questão | correção pendente |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: "
        "| ---: | ---: | ---: |",
    ]
    for d in metrics.dispatches:
        times = ("—" if d.response_time_ms is None else
                 f"{_ms(d.response_time_ms.median)} ({_ms(d.response_time_ms.q1)}–{_ms(d.response_time_ms.q3)})")
        lines.append(
            f"| {d.qid} | {'diário' if d.kind == 'daily' else 'intensivo'} | {_iso(d.opens_at)} | {d.target} "
            f"| {d.delivered} | {d.blocked} | {d.answered_on_time} | {d.answered_late} | {d.dont_know} "
            f"| {d.no_answer} | {_pct(d.delivery_rate)} | {_pct(d.response_rate_on_time)} "
            f"| {_pct(d.response_rate_with_late)} | {times} | {_count_by_type(d.format_errors_by_field_type)} "
            f"| {d.decimal_comma} | {d.graded_without_internal_error} | {d.question_failed} "
            f"| {d.grading_pending_at_end} |")
    if not metrics.dispatches:
        lines.append("| — | — | — | 0 | 0 | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | 0 | 0 | 0 | 0 |")
    median_range = ("—" if t.response_time_median_range is None else
                    f"{_ms(t.response_time_median_range[0])} a {_ms(t.response_time_median_range[1])}")
    invite = ("—" if t.invite_to_link_hours is None else
              f"mediana {t.invite_to_link_hours.median:.1f} h (q1 {t.invite_to_link_hours.q1:.1f} h, "
              f"q3 {t.invite_to_link_hours.q3:.1f} h; n = {t.invite_to_link_hours.n})")
    agent = ("— (sem --agent-token-id)" if t.agent_tasks is None else
             f"{t.agent_tasks.ok} com sucesso, {t.agent_tasks.failed} recusadas")
    lines += [
        "",
        "## Total",
        "",
        f"- Alvo: {t.target}; entregues: {t.delivered} ({_pct(t.delivery_rate)}); bloqueados: {t.blocked} "
        f"({_pct(t.block_rate)}).",
        f"- Respostas no prazo: {t.answered_on_time} ({_pct(t.response_rate_on_time)}); atrasadas: "
        f"{t.answered_late} (com elas, {_pct(t.response_rate_with_late)}); não sei: {t.dont_know}; sem resposta: "
        f"{t.no_answer}.",
        f"- Faixa das medianas do tempo até responder, por disparo: {median_range}.",
        f"- Erros de formato por tipo de campo: {_count_by_type(t.format_errors_by_field_type)}; vírgula decimal: "
        f"{t.decimal_comma}.",
        f"- Respostas com nota, sem erro interno: {t.graded_without_internal_error}; falhas de questão: "
        f"{t.question_failed}; correções pendentes ao fim: {t.grading_pending_at_end}.",
        f"- Tempo do convite ao vínculo: {invite}; reenvios de convite: {t.resends}.",
        "- Recusas do termo: métrica manual (a leitura não expõe).",
        f"- Tarefas do agente: {agent}.",
        "",
        "## Métricas manuais",
        "",
        "Estas o gerador não mede: vêm do diário do piloto (`diary`) ou a leitura da API não as expõe "
        "(`not_exposed`).",
        "",
    ]
    lines += [f"- {m.name}: manual ({m.reason})" for m in metrics.manual]
    return "\n".join(lines) + "\n"


# --- linha de comando ----------------------------------------------------------------------------------------------


class _ArgumentError(Exception):
    """Linha de comando inválida (sem o `usage` do `argparse` nem eco da entrada)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002 - a mensagem do argparse ecoa a entrada; descartada
        raise _ArgumentError()

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:  # noqa: ARG002
        raise _ArgumentError()


def _parser() -> _Parser:
    parser = _Parser(prog="pilot_metrics.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--offering", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--agent-token-id", dest="agent_token_id")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("--out")
    return parser


def _fail(key: str, code: int = EXIT_FAILURE, **values: object) -> int:
    print(MESSAGES[key].format(**values), file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None, *, http: httpx2.Client | None = None,
         env: Mapping[str, str] | None = None) -> int:
    """Linha de comando; `http` injetado é o `TestClient` dos testes; `env=None` → o
    ambiente do processo."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        return _fail("invalid_arguments", EXIT_USAGE)
    if not (args.profile and args.offering and args.plan) or args.out == "":
        return _fail("invalid_arguments", EXIT_USAGE)
    agent_token_id = args.agent_token_id
    if agent_token_id is not None:
        try:
            agent_token_id = str(uuid.UUID(agent_token_id))
        except ValueError:
            return _fail("invalid_agent_token", EXIT_USAGE)
    try:
        plan = load_plan(Path(args.plan))
    except PlanError as error:
        return _fail(error.key)
    if plan.offering != args.offering:
        return _fail("plan_other_offering")
    try:
        profile = resolve_profile(os.environ if env is None else env, args.profile)
    except ProfileError:
        return _fail("profile")

    try:
        with ApiClient(profile.api_url, profile.token, client_kind="other", http=http) as api:
            metrics = collect(api, args.offering, plan, agent_token_id=agent_token_id)
    except ApiUnreachable:
        return _fail("unreachable")
    except ApiError as error:
        return _fail("api_error", status=int(error.status))
    except (_UnexpectedResponse, KeyError, TypeError, ValueError):
        return _fail("unexpected")

    text = to_json(metrics) if args.as_json else to_markdown(metrics)
    if args.out is None:
        sys.stdout.write(text)
        return EXIT_OK
    try:
        with Path(args.out).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        return _fail("out_unwritable")
    print(MESSAGES["written"], file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
