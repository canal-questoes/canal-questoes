"""Adaptadores da porta `ReportRenderer`: o relatório em JSON e em CSV para a API, e o resumo do
relatório no chat da equipe, com o formatador do canal.

Adaptadores puros, no núcleo:
- `JsonReportRenderer()` → `application/json`: o corpo de `reports.get` (o `reports.report_to_dict`);
- `CsvReportRenderer()` → `text/csv; charset=utf-8`: UTF-8 com BOM, RFC 4180, `\\r\\n`,
  `csv.QUOTE_MINIMAL`, as colunas `CSV_COLUMNS`, uma linha por métrica na ordem do JSON; `csv_cell` só
  nas células de texto (`section`, `field`, `key`, `metric`); `on_time` e `with_late` numéricos;
- `ChannelSummaryRenderer(formatter, *, timezone, offering_code, resend=False)` → o resumo de
  fechamento (`resend=False`) ou de reenvio (`resend=True`) no markup do dialeto do formatador, com os
  textos de `noticetexts` (`CLOSED_SUMMARY`, `RESENT_SUMMARY`, `GRADING_PENDING_LINE`); só o título é
  truncado; nenhuma distribuição.

Ligação: as rotas embrulham `JsonReportRenderer().render(report)` e
`CsvReportRenderer().render(report)`; o `teamnotices.deliver` compõe os resumos por
`ChannelSummaryRenderer(channel.formatter, …).render(report_from_dict(content))`. Nenhum outro caminho
compõe o relatório.

**Disposição das linhas do CSV** (fixada por `tests/unit/test_report_render.py`): uma linha por número do
JSON, na ordem do JSON (`header`: `frozen.version` só com `frozen`, e `grading_pending`; `reach`: os
escalares, um `not_delivered_by_reason` e um `question_failed_by_stage` por chave em `key`;
`performance`: os pares e as taxas; `response_time`; `distribution`: por campo, `unavailable` (o motivo em
`key`, colunas numéricas vazias; só no oferecimento expurgado), `choices`, `values`,
`values.others`, `ratios`, `ratios.others`, `unrated`, `texts` e `texts.others`; `friction`: por campo e,
no fim, `unmapped_format_errors`); um objeto ou lista `null` não gera linha.

Importa `noticetexts`, `timezones` e a porta; os tipos de `reports` só sob `TYPE_CHECKING` (sem ciclo), e
`reports.report_to_dict` dentro do `render` do JSON (a forma única do JSON).
"""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING, Any

from cq_server import noticetexts
from cq_server.ports.delivery import Formatted, MessageFormatter
from cq_server.ports.report import RenderedReport
from cq_server.timezones import format_local

if TYPE_CHECKING:
    from cq_server.reports import Pair, RatePair, Report, TopList

# As mídias de `reports.get` e as colunas do CSV.
JSON_MEDIA_TYPE = "application/json"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
CSV_COLUMNS = ("section", "field", "key", "metric", "on_time", "with_late")
CSV_BOM = "﻿"
CSV_LINE_TERMINATOR = "\r\n"
# Os começos de célula de texto que ganham `'` na frente (o próprio `'` incluso,
# para que a releitura seja inversível).
CSV_GUARDED_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "'")

# O limite do markup de uma mensagem à equipe, em unidades UTF-16 (a fila guarda até 4096 caracteres; o
# mesmo de `teamnotices.MAX_MARKUP_UNITS`), e a marca do título cortado para caber.
SUMMARY_MAX_MARKUP_UNITS = 4096
TITLE_ELLIPSIS = "…"


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _escape_controls(text: str) -> str:
    return "".join(f"\\u{ord(ch):04x}" if _is_control(ch) else ch for ch in text)


def csv_cell(text: str) -> str:
    """Neutraliza uma célula de texto do CSV: `'` antes de `=`, `+`, `-`, `@`,
    tabulação, retorno e `'` iniciais; os controles C0/C1 restantes viram `\\u00XX`."""
    if text[:1] in CSV_GUARDED_PREFIXES:
        return "'" + text[0] + _escape_controls(text[1:])
    return _escape_controls(text)


def _number(value: int | float | None) -> str:
    """Uma célula numérica: como no JSON (`str` do inteiro, `repr` do `float`), vazia no `null`."""
    if value is None:
        return ""
    return repr(value) if isinstance(value, float) else str(value)


class JsonReportRenderer:
    """O relatório em JSON (`application/json`)."""

    def __init__(self) -> None:
        pass

    def render(self, report: Report) -> RenderedReport:
        from cq_server.reports import report_to_dict  # só aqui (sem ciclo)

        body = json.dumps(report_to_dict(report), ensure_ascii=False, allow_nan=False)
        return RenderedReport(media_type=JSON_MEDIA_TYPE, body=body)


class CsvReportRenderer:
    """O relatório em CSV (`text/csv; charset=utf-8`)."""

    def __init__(self) -> None:
        pass

    def render(self, report: Report) -> RenderedReport:
        rows: list[tuple[str, str, str, str, Any, Any]] = []

        def scalar(section: str, metric: str, value: int | float | None, *, key: str = "") -> None:
            rows.append((section, "", key, metric, value, None))

        def pair(section: str, metric: str, count: Pair | RatePair, *, field: str = "", key: str = "") -> None:
            rows.append((section, field, key, metric, count.on_time, count.with_late))

        def top(field: str, metric: str, found: TopList | None) -> None:
            if found is None:
                return
            for item in found.items:
                pair("distribution", metric, item.count, field=field, key=item.value)
            pair("distribution", f"{metric}.others", found.others, field=field)

        if report.frozen is not None:
            scalar("header", "frozen.version", report.frozen.version)
        scalar("header", "grading_pending", report.grading_pending)
        reach = report.reach
        for name in ("target", "delivered", "not_delivered"):
            scalar("reach", name, getattr(reach, name))
        for reason, value in vars(reach.not_delivered_by_reason).items():
            scalar("reach", "not_delivered_by_reason", value, key=reason)
        scalar("reach", "question_failed", reach.question_failed)
        for stage, value in vars(reach.question_failed_by_stage).items():
            scalar("reach", "question_failed_by_stage", value, key=stage)
        scalar("reach", "pending", reach.pending)
        for name, count in vars(report.performance).items():
            pair("performance", name, count)
        for name, value in vars(report.response_time).items():
            scalar("response_time", name, value)
        for item in report.distribution.fields:
            index = str(item.index)
            if item.unavailable is not None:
                rows.append(("distribution", index, item.unavailable, "unavailable", None, None))
            for choice in item.choices or ():
                pair("distribution", "choices", choice.count, field=index, key=choice.content_hash)
            top(index, "values", item.values)
            top(index, "ratios", item.ratios)
            if item.unrated is not None:
                pair("distribution", "unrated", item.unrated, field=index)
            top(index, "texts", item.texts)
        for friction in report.friction.fields:
            pair("friction", "format_errors", friction.format_errors, field=str(friction.index))
            pair("friction", "decimal_comma", friction.decimal_comma, field=str(friction.index))
        pair("friction", "unmapped_format_errors", report.friction.unmapped_format_errors)

        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator=CSV_LINE_TERMINATOR, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for section, field, key, metric, on_time, with_late in rows:
            writer.writerow([csv_cell(section), csv_cell(field), csv_cell(key), csv_cell(metric), _number(on_time),
                             _number(with_late)])
        return RenderedReport(media_type=CSV_MEDIA_TYPE, body=CSV_BOM + out.getvalue())


def _utf16_units(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


class ChannelSummaryRenderer:
    """O resumo de fechamento ou de reenvio no dialeto do formatador do canal.

    O modelo é `CLOSED_SUMMARY` (ou `RESENT_SUMMARY`, com a versão de `report.frozen`), com
    `GRADING_PENDING_LINE` antes da última linha só com correções pendentes, composto por
    `noticetexts.render`; os horários no fuso pelo `timezones.format_local`; os pares, as taxas e os
    tempos pelos auxiliares de `noticetexts`. Só o título é cortado (um prefixo seguido de "…") até o
    texto caber no `fits` do formatador e em `SUMMARY_MAX_MARKUP_UNITS`; nenhuma distribuição.
    O `media_type` do resultado é o dialeto do formatador (`rich`, `html` ou `plain`)."""

    def __init__(self, formatter: MessageFormatter, *, timezone: str, offering_code: str,
                 resend: bool = False) -> None:
        self.formatter = formatter
        self.timezone = timezone
        self.offering_code = offering_code
        self.resend = resend

    def render(self, report: Report) -> RenderedReport:
        template = self._template(report)
        values = self._values(report)
        composed = noticetexts.render(template, self.formatter, **values)
        if not self._fits(composed):
            composed = self._truncated(template, values)
        return RenderedReport(media_type=self.formatter.text_format, body=composed.markup)

    def _template(self, report: Report) -> str:
        lines = (noticetexts.RESENT_SUMMARY if self.resend else noticetexts.CLOSED_SUMMARY).split("\n")
        if report.grading_pending > 0:
            lines.insert(len(lines) - 1, noticetexts.GRADING_PENDING_LINE)
        return "\n".join(lines)

    def _values(self, report: Report) -> dict[str, str | int]:
        reach, perf, times = report.reach, report.performance, report.response_time
        values: dict[str, str | int] = {
            "title": report.dispatch.title if report.dispatch.title is not None else noticetexts.UNTITLED,
            "opens_at": format_local(report.dispatch.opens_at, self.timezone),
            "closes_at": format_local(report.dispatch.closes_at, self.timezone),
            "target": reach.target, "delivered": reach.delivered, "not_delivered": reach.not_delivered,
            "question_failed": reach.question_failed,
            "answered": noticetexts.pair(perf.answered.on_time, perf.answered.with_late),
            "correct": noticetexts.pair(perf.correct.on_time, perf.correct.with_late),
            "partially_correct": noticetexts.pair(perf.partially_correct.on_time, perf.partially_correct.with_late),
            "dont_know": noticetexts.pair(perf.dont_know.on_time, perf.dont_know.with_late),
            "response_rate": (f"{noticetexts.rate(perf.response_rate.on_time)} no prazo · "
                              f"{noticetexts.rate(perf.response_rate.with_late)} com atrasadas"),
            "median": noticetexts.duration(times.median_ms),
            "q1": noticetexts.duration(times.q1_ms),
            "q3": noticetexts.duration(times.q3_ms),
            "code": self.offering_code,
            "dispatch_id": str(report.dispatch.dispatch_id),
        }
        if self.resend:
            if report.frozen is None:
                raise ValueError("o resumo de reenvio precisa da versão congelada (report.frozen)")
            values["version"] = report.frozen.version
        if report.grading_pending > 0:
            values["grading_pending"] = report.grading_pending
        return values

    @staticmethod
    def _fits(formatted: Formatted) -> bool:
        return formatted.fits and _utf16_units(formatted.markup) <= SUMMARY_MAX_MARKUP_UNITS

    def _truncated(self, template: str, values: dict[str, str | int]) -> Formatted:
        """O maior prefixo do título (seguido de "…") que faz o texto caber; os números nunca cortados."""
        title = str(values["title"])

        def attempt(size: int) -> Formatted:
            return noticetexts.render(template, self.formatter, **{**values, "title": title[:size] + TITLE_ELLIPSIS})

        best = attempt(0)
        if not self._fits(best):
            raise ValueError("o resumo não cabe na mensagem nem com o título vazio")
        low, high = 0, len(title) - 1
        while low < high:
            middle = (low + high + 1) // 2
            candidate = attempt(middle)
            if self._fits(candidate):
                low, best = middle, candidate
            else:
                high = middle - 1
        return best
