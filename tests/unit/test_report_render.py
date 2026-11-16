"""Adaptadores da porta `ReportRenderer`: o corpo JSON, o CSV e o resumo no canal.



Sobre dois `Report` sintéticos (o cálculo no momento, `frozen = None`, e uma versão congelada de reenvio):

- `JsonReportRenderer().render(report)` → `RenderedReport("application/json", corpo)`: o corpo é o objeto
  do esquema do relatório, igual ao dicionário escrito à mão (`EXPECTED_DICT`) e validado contra
  `reports.get.response.schema.json`; `report_to_dict`/`report_from_dict` fazem a ida e volta sem perda.
- `CsvReportRenderer().render(report)` → `RenderedReport("text/csv; charset=utf-8", corpo)`: UTF-8 com BOM,
  RFC 4180, `\\r\\n`, `csv.QUOTE_MINIMAL`, colunas `section,field,key,metric,on_time,with_late`, comparado
  **byte a byte** com o literal escrito à mão (`EXPECTED_CSV`) e relido igual ao JSON.
- `csv_cell(text)`: `'` antes de `=`, `+`, `-`, `@`, tabulação, retorno e `'` iniciais; os controles C0/C1
  restantes viram o escape visível `\\u00XX`; só nas células de texto.
- `ChannelSummaryRenderer(formatter, *, timezone, offering_code, resend)` → o resumo de fechamento ou de
  reenvio no dialeto do formatador (Rich, HTML, texto simples), com os textos de `noticetexts`.

**Disposição das linhas do CSV** ([I] fixada por este teste; a regra: uma linha por métrica, na
ordem do JSON, `key` = o motivo, o estágio, o `content_hash`, o valor redigido ou vazio, os escalares
sem par com o valor em `on_time` e `with_late` vazio, vazio no lugar de `null`; o ajudante congelado
`tests/helpers/contract.py::assert_report_csv` exige `on_time`/`with_late` numéricos ou vazios, então
nenhum texto do cabeçalho vai para essas colunas). Uma linha por **número** do JSON (ou posição numérica
anulável), na ordem do JSON; um objeto ou lista `null` não gera linha:
- `header`: `frozen.version` (só com `frozen` preenchido) e `grading_pending`, `field` e `key` vazios;
- `reach`: `target`, `delivered`, `not_delivered`, um `not_delivered_by_reason` por motivo (o motivo em
  `key`, na ordem do objeto), `question_failed`, um `question_failed_by_stage` por estágio (o estágio em
  `key`) e `pending`; o valor em `on_time`, `with_late` vazio;
- `performance`: uma linha por par (`answered` … `dont_know`) e por taxa (`correct_rate` … `mean_score`),
  com os dois lados; `null` → célula vazia;
- `response_time`: `observations`, `negative`, `median_ms`, `q1_ms`, `q3_ms`, sem par;
- `distribution`: por campo (`field` = o índice), na ordem `choices` (`key` = o `content_hash`), `values`
  (`key` = o valor), `values.others`, `ratios` (`key` = a razão), `ratios.others`, `unrated`, `texts`
  (`key` = o texto) e `texts.others` (com `key` vazio);
- `friction`: por campo, `format_errors` e `decimal_comma`; no fim, `unmapped_format_errors` sem `field`.
Os números saem como no JSON (`str` do inteiro, `repr` do `float`, sem `'`), inclusive negativos.

**Composição do resumo** ([I] fixada por este teste): o modelo é
`CLOSED_SUMMARY` (ou `RESENT_SUMMARY`, com `version` = `report.frozen.version`), com `GRADING_PENDING_LINE`
antes da última linha só com `grading_pending > 0`, composto por `noticetexts.render(modelo, formatter,
**valores)`; os valores: `title` (o do relatório, ou `UNTITLED`), `opens_at`/`closes_at` pelo
`timezones.format_local` no fuso do adaptador (o formato do disparo), as contagens do
alcance, os pares pelo `noticetexts.pair`, a taxa de resposta como `"<rate> no prazo · <rate> com
atrasadas"` pelo `noticetexts.rate`, os tempos pelo `noticetexts.duration`, `code` = o `offering_code` do
adaptador e `dispatch_id`. O teste usa os auxiliares de `noticetexts` como oráculo da formatação (eles
são testados em `test_notice_texts.py`).

No esqueleto, `render` dos três adaptadores, `csv_cell`, `report_to_dict`, `report_from_dict` e os
auxiliares de `noticetexts` lançam `NotImplementedError`: todo caso falha por esse motivo. As asserções
sobre o que o esqueleto já tem (os construtores reais, as constantes de `reportrender` e o `plain` dos
formatadores do disparo, `telegramrender.py:661`) ficam na mesma função de um caso de `render`.

Linha de indisponibilidade no CSV: a distribuição indisponível (`unavailable` preenchido, na forma do
esquema do relatório) ganha uma linha na posição do campo, antes de qualquer outra linha dele:
`distribution,<índice>,<unavailable>,unavailable,,`, com o motivo (`purged`) em `key` e as colunas numéricas
vazias. O CSV esperado do `REPORT`, que já tem o campo 4 com `unavailable="purged"`, traz a linha
`distribution,4,purged,unavailable,,` depois das linhas do campo 3; no esqueleto ela ainda não é emitida.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from cq_server import noticetexts, reportrender, reports
from cq_server.ports.report import RenderedReport
from cq_server.reportrender import ChannelSummaryRenderer, CsvReportRenderer, JsonReportRenderer
from cq_server.reports import (
    ChoiceItem,
    DispatchHeader,
    Distribution,
    FieldDistribution,
    FieldFriction,
    Friction,
    FrozenRef,
    NotDeliveredByReason,
    OfferingRef,
    Pair,
    Performance,
    QuestionFailedByStage,
    QuestionHeader,
    RatePair,
    Reach,
    Report,
    ResponseTime,
    TopItem,
    TopList,
)
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter
from cq_server.timezones import format_local
from tests.helpers.bot_api_dispatch import RICH_TEXT_MAX, TEXT_MAX, DispatchExtension, parse_markup
from tests.helpers.canary import ZEFIRO
from tests.helpers.contract import assert_matches, assert_report_csv
from tests.helpers.engine import FakeOption, build_variant, multiple_choice_field, preset, single_choice_field

UTC = timezone.utc
ZONE = "America/Sao_Paulo"
CODE = "mc102-2026s2"
OFFERING_ID = uuid.UUID("3f2a9c1e-5b7d-4e8a-9f10-2b3c4d5e6f70")
DISPATCH_ID = uuid.UUID("7c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5")
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
OPENS_AT = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)
COMPUTED_AT = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
FROZEN_AT = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)

# --- os relatórios sintéticos -----------------------------------------------------------------------

REPORT = Report(
    report_schema_version="1",
    offering=OfferingRef(offering_id=OFFERING_ID, code=CODE),
    timezone=ZONE,
    dispatch=DispatchHeader(
        dispatch_id=DISPATCH_ID, question=QuestionHeader(qid="aula03/velocidade", commit_short="a193f7204853"),
        title="Velocidade média", state="closed", partial=False, origin="cli", opens_at=OPENS_AT,
        closes_at=CLOSES_AT),
    computed_at=COMPUTED_AT,
    frozen=None,
    grading_pending=0,
    reach=Reach(target=31, delivered=28, not_delivered=2,
                not_delivered_by_reason=NotDeliveredByReason(endpoint_blocked=2, channel_refused=0,
                                                             engine_unavailable=0, bot_no_access=0,
                                                             channel_unavailable=0),
                question_failed=1, question_failed_by_stage=QuestionFailedByStage(preparation=0, grading=1),
                pending=0),
    performance=Performance(
        answered=Pair(18, 23), correct=Pair(10, 13), partially_correct=Pair(5, 6), incorrect=Pair(3, 4),
        dont_know=Pair(2, 3), correct_rate=RatePair(0.375, 0.5), partially_correct_rate=RatePair(0.125, 0.25),
        incorrect_rate=RatePair(0.0625, 0.125), response_rate=RatePair(0.5, 0.75),
        mean_score=RatePair(0.75, None)),
    response_time=ResponseTime(observations=18, negative=2, median_ms=-250.5, q1_ms=-1200.0, q3_ms=3000.25),
    distribution=Distribution(fields=(
        FieldDistribution(index=0, type="single_choice", label="Cor do céu", unavailable=None,
                          choices=(ChoiceItem(HASH_A, "Azul", Pair(9, 11)),
                                   ChoiceItem(HASH_B, '=HYPERLINK("x")', Pair(1, 2))),
                          values=None, ratios=None, unrated=None, texts=None),
        FieldDistribution(index=1, type="number", label="Velocidade (m/s)", unavailable=None, choices=None,
                          values=TopList((TopItem("-3", Pair(2, 3)), TopItem("6.5", Pair(4, 4))), Pair(1, 2)),
                          ratios=TopList((TopItem("1", Pair(4, 4)), TopItem("-0.462", Pair(2, 3))), Pair(0, 1)),
                          unrated=Pair(1, 1), texts=None),
        FieldDistribution(index=2, type="text", label=None, unavailable=None, choices=None, values=None,
                          ratios=None, unrated=None,
                          texts=TopList((TopItem("=1+1", Pair(2, 2)), TopItem("+55 19", Pair(1, 2)),
                                         TopItem("@mencao", Pair(2, 2)), TopItem("\ttab", Pair(2, 2)),
                                         TopItem("'citado", Pair(0, 2))), Pair(3, 4))),
        FieldDistribution(index=3, type="text", label="Comentário", unavailable=None, choices=None, values=None,
                          ratios=None, unrated=None,
                          texts=TopList((TopItem("a\x10b", Pair(2, 2)), TopItem("\rretorno", Pair(2, 2)),
                                         TopItem("\x85c1", Pair(2, 2)), TopItem("-", Pair(2, 2)),
                                         TopItem('vírgula, "aspas"', Pair(2, 2))), Pair(0, 0))),
        FieldDistribution(index=4, type="integer", label="Contagem", unavailable="purged", choices=None,
                          values=None, ratios=None, unrated=None, texts=None),
    )),
    friction=Friction(fields=(
        FieldFriction(0, Pair(1, 2), Pair(0, 0)),
        FieldFriction(1, Pair(3, 4), Pair(2, 3)),
        FieldFriction(2, Pair(0, 0), Pair(0, 0)),
        FieldFriction(3, Pair(0, 0), Pair(0, 0)),
        FieldFriction(4, Pair(0, 0), Pair(0, 0)),
    ), unmapped_format_errors=Pair(1, 1)),
)

# A versão congelada de reenvio (versão 2), com correções pendentes.
FROZEN_REPORT = replace(REPORT, frozen=FrozenRef(version=2, trigger="resend", frozen_at=FROZEN_AT),
                        grading_pending=3)


def _pair(on_time: int, with_late: int) -> dict:
    return {"on_time": on_time, "with_late": with_late}


def _rate(on_time: float | None, with_late: float | None) -> dict:
    return {"on_time": on_time, "with_late": with_late}


def _top(items: list[tuple[str, int, int]], others: tuple[int, int]) -> dict:
    return {"items": [{"value": v, "count": _pair(a, b)} for v, a, b in items], "others": _pair(*others)}


# O corpo JSON de `REPORT`, escrito à mão (o esquema do relatório; horários em ISO 8601 UTC com `Z`).
EXPECTED_DICT: dict[str, Any] = {
    "report_schema_version": "1",
    "offering": {"offering_id": "3f2a9c1e-5b7d-4e8a-9f10-2b3c4d5e6f70", "code": "mc102-2026s2"},
    "timezone": "America/Sao_Paulo",
    "dispatch": {
        "dispatch_id": "7c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5",
        "question": {"qid": "aula03/velocidade", "commit_short": "a193f7204853"},
        "title": "Velocidade média",
        "state": "closed",
        "partial": False,
        "origin": "cli",
        "opens_at": "2026-10-14T12:00:00Z",
        "closes_at": "2026-10-14T13:00:00Z",
    },
    "computed_at": "2026-10-14T13:30:00Z",
    "frozen": None,
    "grading_pending": 0,
    "reach": {
        "target": 31, "delivered": 28, "not_delivered": 2,
        "not_delivered_by_reason": {"endpoint_blocked": 2, "channel_refused": 0, "engine_unavailable": 0,
                                    "bot_no_access": 0, "channel_unavailable": 0},
        "question_failed": 1,
        "question_failed_by_stage": {"preparation": 0, "grading": 1},
        "pending": 0,
    },
    "performance": {
        "answered": _pair(18, 23), "correct": _pair(10, 13), "partially_correct": _pair(5, 6),
        "incorrect": _pair(3, 4), "dont_know": _pair(2, 3),
        "correct_rate": _rate(0.375, 0.5), "partially_correct_rate": _rate(0.125, 0.25),
        "incorrect_rate": _rate(0.0625, 0.125), "response_rate": _rate(0.5, 0.75),
        "mean_score": _rate(0.75, None),
    },
    "response_time": {"observations": 18, "negative": 2, "median_ms": -250.5, "q1_ms": -1200.0,
                      "q3_ms": 3000.25},
    "distribution": {"fields": [
        {"index": 0, "type": "single_choice", "label": "Cor do céu", "unavailable": None,
         "choices": [{"content_hash": HASH_A, "text": "Azul", "count": _pair(9, 11)},
                     {"content_hash": HASH_B, "text": '=HYPERLINK("x")', "count": _pair(1, 2)}],
         "values": None, "ratios": None, "unrated": None, "texts": None},
        {"index": 1, "type": "number", "label": "Velocidade (m/s)", "unavailable": None, "choices": None,
         "values": _top([("-3", 2, 3), ("6.5", 4, 4)], (1, 2)),
         "ratios": _top([("1", 4, 4), ("-0.462", 2, 3)], (0, 1)),
         "unrated": _pair(1, 1), "texts": None},
        {"index": 2, "type": "text", "label": None, "unavailable": None, "choices": None, "values": None,
         "ratios": None, "unrated": None,
         "texts": _top([("=1+1", 2, 2), ("+55 19", 1, 2), ("@mencao", 2, 2), ("\ttab", 2, 2),
                        ("'citado", 0, 2)], (3, 4))},
        {"index": 3, "type": "text", "label": "Comentário", "unavailable": None, "choices": None,
         "values": None, "ratios": None, "unrated": None,
         "texts": _top([("a\x10b", 2, 2), ("\rretorno", 2, 2), ("\x85c1", 2, 2), ("-", 2, 2),
                        ('vírgula, "aspas"', 2, 2)], (0, 0))},
        {"index": 4, "type": "integer", "label": "Contagem", "unavailable": "purged", "choices": None,
         "values": None, "ratios": None, "unrated": None, "texts": None},
    ]},
    "friction": {
        "fields": [
            {"index": 0, "format_errors": _pair(1, 2), "decimal_comma": _pair(0, 0)},
            {"index": 1, "format_errors": _pair(3, 4), "decimal_comma": _pair(2, 3)},
            {"index": 2, "format_errors": _pair(0, 0), "decimal_comma": _pair(0, 0)},
            {"index": 3, "format_errors": _pair(0, 0), "decimal_comma": _pair(0, 0)},
            {"index": 4, "format_errors": _pair(0, 0), "decimal_comma": _pair(0, 0)},
        ],
        "unmapped_format_errors": _pair(1, 1),
    },
}

EXPECTED_FROZEN_DICT: dict[str, Any] = {
    **EXPECTED_DICT,
    "frozen": {"version": 2, "trigger": "resend", "frozen_at": "2026-10-14T14:00:00Z"},
    "grading_pending": 3,
}

# As linhas do CSV de `REPORT` (sem o BOM e sem o fim de linha), escritas à mão pela disposição do
# docstring. As células de texto neutralizadas: `'-3`, `'-0.462`, `'=1+1`, `'+55 19`, `'@mencao`, `'<TAB>tab`,
# `''citado`, `'-`; o controle C0 `\x10` e o C1 `\x85` viram `\u0010` e `\u0085` (escapes só com dígitos,
# para não depender da caixa do hexadecimal); `'<CR>retorno` e `vírgula, "aspas"` saem entre aspas (RFC 4180).
_CSV_BODY_LINES = [
    "section,field,key,metric,on_time,with_late",
    "header,,,grading_pending,0,",
    "reach,,,target,31,",
    "reach,,,delivered,28,",
    "reach,,,not_delivered,2,",
    "reach,,endpoint_blocked,not_delivered_by_reason,2,",
    "reach,,channel_refused,not_delivered_by_reason,0,",
    "reach,,engine_unavailable,not_delivered_by_reason,0,",
    "reach,,bot_no_access,not_delivered_by_reason,0,",
    "reach,,channel_unavailable,not_delivered_by_reason,0,",
    "reach,,,question_failed,1,",
    "reach,,preparation,question_failed_by_stage,0,",
    "reach,,grading,question_failed_by_stage,1,",
    "reach,,,pending,0,",
    "performance,,,answered,18,23",
    "performance,,,correct,10,13",
    "performance,,,partially_correct,5,6",
    "performance,,,incorrect,3,4",
    "performance,,,dont_know,2,3",
    "performance,,,correct_rate,0.375,0.5",
    "performance,,,partially_correct_rate,0.125,0.25",
    "performance,,,incorrect_rate,0.0625,0.125",
    "performance,,,response_rate,0.5,0.75",
    "performance,,,mean_score,0.75,",
    "response_time,,,observations,18,",
    "response_time,,,negative,2,",
    "response_time,,,median_ms,-250.5,",
    "response_time,,,q1_ms,-1200.0,",
    "response_time,,,q3_ms,3000.25,",
    f"distribution,0,{HASH_A},choices,9,11",
    f"distribution,0,{HASH_B},choices,1,2",
    "distribution,1,'-3,values,2,3",
    "distribution,1,6.5,values,4,4",
    "distribution,1,,values.others,1,2",
    "distribution,1,1,ratios,4,4",
    "distribution,1,'-0.462,ratios,2,3",
    "distribution,1,,ratios.others,0,1",
    "distribution,1,,unrated,1,1",
    "distribution,2,'=1+1,texts,2,2",
    "distribution,2,'+55 19,texts,1,2",
    "distribution,2,'@mencao,texts,2,2",
    "distribution,2,'\ttab,texts,2,2",
    "distribution,2,''citado,texts,0,2",
    "distribution,2,,texts.others,3,4",
    "distribution,3,a\\u0010b,texts,2,2",
    "distribution,3,\"'\rretorno\",texts,2,2",
    "distribution,3,\\u0085c1,texts,2,2",
    "distribution,3,'-,texts,2,2",
    "distribution,3,\"vírgula, \"\"aspas\"\"\",texts,2,2",
    "distribution,3,,texts.others,0,0",
    "distribution,4,purged,unavailable,,",
    "friction,0,,format_errors,1,2",
    "friction,0,,decimal_comma,0,0",
    "friction,1,,format_errors,3,4",
    "friction,1,,decimal_comma,2,3",
    "friction,2,,format_errors,0,0",
    "friction,2,,decimal_comma,0,0",
    "friction,3,,format_errors,0,0",
    "friction,3,,decimal_comma,0,0",
    "friction,4,,format_errors,0,0",
    "friction,4,,decimal_comma,0,0",
    "friction,,,unmapped_format_errors,1,1",
]
EXPECTED_CSV = "﻿" + "\r\n".join(_CSV_BODY_LINES) + "\r\n"
# A versão congelada: a linha `frozen.version` antes de `grading_pending` (que passa a 3).
EXPECTED_FROZEN_CSV = ("﻿" + "\r\n".join(
    [_CSV_BODY_LINES[0], "header,,,frozen.version,2,", "header,,,grading_pending,3,", *_CSV_BODY_LINES[2:]])
    + "\r\n")

# Os textos da distribuição: nenhum sai no resumo.
DISTRIBUTION_TEXTS = ("Cor do céu", "Azul", "HYPERLINK", HASH_A, "Velocidade (m/s)", "6.5", "-0.462", "=1+1",
                      "@mencao", "citado", "retorno", "Comentário", "Contagem", "aspas")

FORMATTERS = {
    "rich": TelegramRichFormatter,
    "html": TelegramHtmlFormatter,
    "plain": PlainFormatter,
}

# --- auxiliares --------------------------------------------------------------------------------------

_ESCAPE = re.compile(r"\\u00([0-9A-Fa-f]{2})")


def _escapes_lower(text: str) -> str:
    """Os escapes `\\u00XX` com o hexadecimal em minúscula (a caixa do escape não é contrato)."""
    return _ESCAPE.sub(lambda m: "\\u00" + m.group(1).lower(), text)


def _fake_csv_response(rendered: RenderedReport) -> Any:
    """Uma resposta HTTP mínima com o CSV renderizado, para o ajudante congelado `assert_report_csv`."""
    return SimpleNamespace(status_code=200, headers={"content-type": rendered.media_type},
                           content=rendered.body.encode("utf-8"), text=rendered.body)


def _reread(body: str) -> list[list[str]]:
    """O CSV relido (sem o BOM), tirando um `'` inicial de cada célula de texto."""
    assert body.startswith("﻿")
    rows = list(csv.reader(io.StringIO(body[1:], newline=""), strict=True))
    out = []
    for row in rows[1:]:
        out.append([cell[1:] if cell.startswith("'") else cell for cell in row[:4]] + row[4:])
    return out


def _number_cell(value: Any) -> str:
    return "" if value is None else (repr(value) if isinstance(value, float) else str(value))


def _rows_from_json(body: dict) -> list[list[str]]:
    """As linhas esperadas do CSV relido, derivadas do JSON pela disposição do docstring.

    Linha de indisponibilidade no CSV: a distribuição indisponível rende a linha `unavailable` (o motivo em
    `key`, as colunas numéricas vazias) antes das demais linhas do campo."""
    rows: list[list[str]] = []
    if body["frozen"] is not None:
        rows.append(["header", "", "", "frozen.version", _number_cell(body["frozen"]["version"]), ""])
    rows.append(["header", "", "", "grading_pending", _number_cell(body["grading_pending"]), ""])
    reach = body["reach"]
    for name in ("target", "delivered", "not_delivered"):
        rows.append(["reach", "", "", name, _number_cell(reach[name]), ""])
    for reason, value in reach["not_delivered_by_reason"].items():
        rows.append(["reach", "", reason, "not_delivered_by_reason", _number_cell(value), ""])
    rows.append(["reach", "", "", "question_failed", _number_cell(reach["question_failed"]), ""])
    for stage, value in reach["question_failed_by_stage"].items():
        rows.append(["reach", "", stage, "question_failed_by_stage", _number_cell(value), ""])
    rows.append(["reach", "", "", "pending", _number_cell(reach["pending"]), ""])
    for name, pair in body["performance"].items():
        rows.append(["performance", "", "", name, _number_cell(pair["on_time"]), _number_cell(pair["with_late"])])
    for name, value in body["response_time"].items():
        rows.append(["response_time", "", "", name, _number_cell(value), ""])
    for item in body["distribution"]["fields"]:
        index = str(item["index"])
        if item["unavailable"] is not None:
            rows.append(["distribution", index, item["unavailable"], "unavailable", "", ""])
        for choice in item["choices"] or ():
            rows.append(["distribution", index, choice["content_hash"], "choices",
                         _number_cell(choice["count"]["on_time"]), _number_cell(choice["count"]["with_late"])])
        for name in ("values", "ratios"):
            top = item[name]
            if top is None:
                continue
            for entry in top["items"]:
                rows.append(["distribution", index, entry["value"], name, _number_cell(entry["count"]["on_time"]),
                             _number_cell(entry["count"]["with_late"])])
            rows.append(["distribution", index, "", f"{name}.others", _number_cell(top["others"]["on_time"]),
                         _number_cell(top["others"]["with_late"])])
            if name == "ratios" and item["unrated"] is not None:
                rows.append(["distribution", index, "", "unrated", _number_cell(item["unrated"]["on_time"]),
                             _number_cell(item["unrated"]["with_late"])])
        if item["texts"] is not None:
            for entry in item["texts"]["items"]:
                rows.append(["distribution", index, entry["value"], "texts", _number_cell(entry["count"]["on_time"]),
                             _number_cell(entry["count"]["with_late"])])
            rows.append(["distribution", index, "", "texts.others", _number_cell(item["texts"]["others"]["on_time"]),
                         _number_cell(item["texts"]["others"]["with_late"])])
    for item in body["friction"]["fields"]:
        for name in ("format_errors", "decimal_comma"):
            rows.append(["friction", str(item["index"]), "", name, _number_cell(item[name]["on_time"]),
                         _number_cell(item[name]["with_late"])])
    unmapped = body["friction"]["unmapped_format_errors"]
    rows.append(["friction", "", "", "unmapped_format_errors", _number_cell(unmapped["on_time"]),
                 _number_cell(unmapped["with_late"])])
    return rows


def _escaped_controls(text: str) -> str:
    """O texto como a célula relida o mostra: os controles C0/C1 (fora da tabulação e do retorno iniciais,
    que só ganham o `'`) como o escape visível de seis caracteres."""
    head = text[:1] if text[:1] in ("\t", "\r") else ""
    rest = text[len(head):]
    return head + "".join(f"\\u{ord(ch):04x}" if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F else ch for ch in rest)


def _json_numbers(node: Any) -> list[float]:
    """Todo número do JSON (sem os booleanos e sem os índices dos campos), em ordem de travessia."""
    if isinstance(node, bool) or node is None or isinstance(node, str):
        return []
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, dict):
        return [n for key, value in node.items() if key != "index" for n in _json_numbers(value)]
    return [n for value in node for n in _json_numbers(value)]


def _summary_template(*, resend: bool, grading_pending: int) -> str:
    lines = (noticetexts.RESENT_SUMMARY if resend else noticetexts.CLOSED_SUMMARY).split("\n")
    if grading_pending > 0:
        lines.insert(len(lines) - 1, noticetexts.GRADING_PENDING_LINE)
    return "\n".join(lines)


def _summary_values(report: Report, *, resend: bool) -> dict[str, Any]:
    reach, perf, rt = report.reach, report.performance, report.response_time
    values: dict[str, Any] = {
        "title": report.dispatch.title if report.dispatch.title is not None else noticetexts.UNTITLED,
        "opens_at": format_local(report.dispatch.opens_at, ZONE),
        "closes_at": format_local(report.dispatch.closes_at, ZONE),
        "target": reach.target, "delivered": reach.delivered, "not_delivered": reach.not_delivered,
        "question_failed": reach.question_failed,
        "answered": noticetexts.pair(perf.answered.on_time, perf.answered.with_late),
        "correct": noticetexts.pair(perf.correct.on_time, perf.correct.with_late),
        "partially_correct": noticetexts.pair(perf.partially_correct.on_time, perf.partially_correct.with_late),
        "dont_know": noticetexts.pair(perf.dont_know.on_time, perf.dont_know.with_late),
        "response_rate": (f"{noticetexts.rate(perf.response_rate.on_time)} no prazo · "
                          f"{noticetexts.rate(perf.response_rate.with_late)} com atrasadas"),
        "median": noticetexts.duration(rt.median_ms),
        "q1": noticetexts.duration(rt.q1_ms),
        "q3": noticetexts.duration(rt.q3_ms),
        "code": CODE,
        "dispatch_id": str(report.dispatch.dispatch_id),
    }
    if resend:
        assert report.frozen is not None
        values["version"] = report.frozen.version
    if report.grading_pending > 0:
        values["grading_pending"] = report.grading_pending
    return values


def _expected_summary(report: Report, formatter: Any, *, resend: bool) -> str:
    template = _summary_template(resend=resend, grading_pending=report.grading_pending)
    return noticetexts.render(template, formatter, **_summary_values(report, resend=resend)).markup


def _summary_plain_lines(report: Report, *, resend: bool) -> list[str]:
    """As linhas do resumo em texto puro (o modelo com os valores, sem escape), para as conferências de
    conteúdo que não dependem do dialeto."""
    template = _summary_template(resend=resend, grading_pending=report.grading_pending)
    values = {k: str(v) for k, v in _summary_values(report, resend=resend).items()}
    return template.format(**values).split("\n")


def _channel_problems(formatter: Any, body: str) -> list[str]:
    """O texto reinjetado no validador da `DispatchExtension` como o canal o enviaria (o método e os parâmetros do
    dialeto): os problemas de protocolo e o 400, se o Telegram o recusaria."""
    ext = DispatchExtension()
    if formatter.text_format == "rich":
        result = ext.check_params("sendRichMessage", {"chat_id": 1, "rich_message": {
            "html": body, "skip_entity_detection": True}})
    elif formatter.text_format == "html":
        result = ext.check_params("sendMessage", {"chat_id": 1, "text": body, "parse_mode": "HTML",
                                                  "link_preview_options": {"is_disabled": True}})
    else:
        result = ext.check_params("sendMessage", {"chat_id": 1, "text": body})
    assert result is not None
    problems, bad = result
    return list(problems) + ([bad] if bad else [])


def _plain_of(formatter: Any, body: str) -> str:
    """O texto como o Telegram o mostraria (tags fora, entidades decodificadas); o texto simples é o próprio."""
    if formatter.text_format == "plain":
        return body
    markup = parse_markup(body, formatter.text_format)
    assert markup.error is None, markup.error
    return markup.plain


# --- JSON --------------------------------------------------------------------------------------------


def test_json_renderer_body_is_the_hand_written_report_and_matches_schema() -> None:
    """O corpo JSON é o objeto do esquema do relatório, igual ao escrito à mão e válido contra o esquema
    congelado. Os construtores e as constantes reais do esqueleto na mesma função."""
    rendered = JsonReportRenderer().render(REPORT)
    assert isinstance(rendered, RenderedReport)
    assert rendered.media_type == "application/json"
    body = json.loads(rendered.body)
    assert body == EXPECTED_DICT
    assert_matches("reports.get", 200, body)
    frozen = JsonReportRenderer().render(FROZEN_REPORT)
    assert frozen.media_type == "application/json"
    assert json.loads(frozen.body) == EXPECTED_FROZEN_DICT
    assert_matches("reports.get", 200, json.loads(frozen.body))
    # Real no esqueleto: as constantes de `reportrender` e os construtores (só guardam os argumentos).
    assert reportrender.JSON_MEDIA_TYPE == "application/json"
    assert reportrender.CSV_MEDIA_TYPE == "text/csv; charset=utf-8"
    assert reportrender.CSV_COLUMNS == ("section", "field", "key", "metric", "on_time", "with_late")
    assert reportrender.CSV_BOM == "﻿"
    assert reportrender.CSV_LINE_TERMINATOR == "\r\n"
    assert reportrender.CSV_GUARDED_PREFIXES == ("=", "+", "-", "@", "\t", "\r", "'")
    assert isinstance(JsonReportRenderer(), JsonReportRenderer)
    assert isinstance(CsvReportRenderer(), CsvReportRenderer)


def test_report_dict_round_trip_without_loss() -> None:
    """`report_to_dict` dá o dicionário escrito à mão e `report_from_dict` o inverte sem perda (tuplas,
    `UUID`, horários com fuso, `None`), inclusive com `frozen` preenchido e o campo `purged`."""
    assert reports.report_to_dict(REPORT) == EXPECTED_DICT
    assert reports.report_to_dict(FROZEN_REPORT) == EXPECTED_FROZEN_DICT
    assert reports.report_from_dict(EXPECTED_DICT) == REPORT
    assert reports.report_from_dict(EXPECTED_FROZEN_DICT) == FROZEN_REPORT
    for report in (REPORT, FROZEN_REPORT):
        back = reports.report_from_dict(json.loads(json.dumps(reports.report_to_dict(report))))
        assert back == report
        assert isinstance(back.dispatch.dispatch_id, uuid.UUID)
        assert back.dispatch.opens_at.utcoffset() is not None
        assert isinstance(back.distribution.fields, tuple)


# --- CSV ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("report", "expected"), [(REPORT, EXPECTED_CSV), (FROZEN_REPORT, EXPECTED_FROZEN_CSV)],
                         ids=["computed", "frozen"])
def test_csv_renderer_is_byte_exact(report: Report, expected: str) -> None:
    """O CSV é igual, byte a byte, ao literal escrito à mão: BOM, RFC 4180, `\\r\\n`, aspas mínimas, as 6
    colunas, uma linha por número na ordem do JSON, vazio no `null` e as células de texto neutralizadas."""
    rendered = CsvReportRenderer().render(report)
    assert isinstance(rendered, RenderedReport)
    assert rendered.media_type == "text/csv; charset=utf-8"
    assert rendered.body == expected
    assert rendered.body.encode("utf-8") == expected.encode("utf-8")
    assert rendered.body.encode("utf-8").startswith(b"\xef\xbb\xbf")
    # A forma conferida pelo ajudante congelado da API (o mesmo que os testes de rota usam).
    assert assert_report_csv(_fake_csv_response(rendered)) == expected


def test_csv_reread_equals_json_in_every_number_and_key() -> None:
    """O CSV relido (tirando um `'` inicial das células de texto) é igual ao JSON da mesma `Report` em toda
    seção, campo, `key`, métrica e número."""
    for report in (REPORT, FROZEN_REPORT):
        body = json.loads(JsonReportRenderer().render(report).body)
        rows = _reread(CsvReportRenderer().render(report).body)
        expected = [[*row[:2], _escaped_controls(row[2]), *row[3:]] for row in _rows_from_json(body)]
        assert [[_escapes_lower(c) for c in row] for row in rows] == expected
        # Todo número do JSON (fora os índices dos campos) aparece uma vez nas colunas numéricas, com o mesmo
        # valor; os índices aparecem em `field`.
        csv_numbers = sorted(float(cell) for row in rows for cell in row[4:6] if cell != "")
        assert csv_numbers == sorted(_json_numbers(body))
        indices = {str(f["index"]) for part in ("distribution", "friction") for f in body[part]["fields"]}
        assert {row[1] for row in rows} - {""} <= indices


def test_csv_numbers_are_never_neutralized_and_negative_key_is() -> None:
    """A mediana e os quartis negativos saem como números, sem `'`; o valor submetido negativo na coluna
    `key` sai como `'-3`, e a releitura devolve `-3`."""
    rendered = CsvReportRenderer().render(REPORT)
    rows = list(csv.reader(io.StringIO(rendered.body[1:], newline=""), strict=True))
    by_metric = {(r[0], r[3]): r for r in rows if r[0] == "response_time"}
    assert by_metric[("response_time", "median_ms")][4:] == ["-250.5", ""]
    assert by_metric[("response_time", "q1_ms")][4:] == ["-1200.0", ""]
    values = [r for r in rows if r[0] == "distribution" and r[3] == "values"]
    assert [r[2] for r in values] == ["'-3", "6.5"]
    assert all(not cell.startswith("'") for r in rows[1:] for cell in r[4:])
    assert [r[2] for r in _reread(rendered.body) if r[0] == "distribution" and r[3] == "values"] == ["-3", "6.5"]


# (identificador, entrada, saída esperada); os escapes comparados sem a caixa do hexadecimal.
CSV_CELL_CASES = [
    ("equals", "=1+1", "'=1+1"),
    ("plus", "+5", "'+5"),
    ("minus", "-3", "'-3"),
    ("minus-ratio", "-0.462", "'-0.462"),
    ("at", "@x", "'@x"),
    ("tab", "\tx", "'\tx"),
    ("carriage-return", "\rx", "'\rx"),
    ("apostrophe", "'x", "''x"),
    ("only-minus", "-", "'-"),
    ("plain", "abc", "abc"),
    ("empty", "", ""),
    ("digits", "6.5", "6.5"),
    ("inner-equals", "a=b", "a=b"),
    ("inner-minus", "a-b", "a-b"),
    ("inner-apostrophe", "a'b", "a'b"),
    ("comma-and-quotes", 'a,"b"', 'a,"b"'),
    ("unicode", "vírgula · ç", "vírgula · ç"),
    ("space-then-equals", " =1", " =1"),
    ("line-feed", "a\nb", "a\\u000ab"),
    ("leading-line-feed", "\nx", "\\u000ax"),
    ("inner-tab", "a\tb", "a\\u0009b"),
    ("inner-carriage-return", "a\rb", "a\\u000db"),
    ("nul", "a\x00b", "a\\u0000b"),
    ("escape", "\x1b[31m", "\\u001b[31m"),
    ("c1-next-line", "a\x85b", "a\\u0085b"),
    ("c1-leading", "\x9fz", "\\u009fz"),
    ("delete", "a\x7fb", "a\\u007fb"),
    ("tab-then-tab", "\t\tx", "'\t\\u0009x"),
    ("cr-then-lf", "\r\nx", "'\r\\u000ax"),
]


@pytest.mark.parametrize(("text", "expected"), [c[1:] for c in CSV_CELL_CASES], ids=[c[0] for c in CSV_CELL_CASES])
def test_csv_cell_neutralizes_formulas_and_escapes_controls(text: str, expected: str) -> None:
    assert _escapes_lower(reportrender.csv_cell(text)) == expected


# --- `plain` dos formatadores e o resumo -------------------------------------------------------------


def _canary_ir_html() -> list[str]:
    """Os trechos de HTML canônico da IR canário (o enunciado, o rótulo e as alternativas com a identidade
    e com marcação), como o motor falso os produz."""
    identity = f"{ZEFIRO.name} <{ZEFIRO.email}> & RA {ZEFIRO.ra}"
    _, canary_ir, _ = build_variant(preset("canary", identity=identity), "1a")
    options = (FakeOption("a", f"<b>{ZEFIRO.name}</b> &amp; x<sup>2</sup>"),
               FakeOption("b", "<code>a&lt;b</code> e <i>itálico</i>"),
               FakeOption("c", "Azul"))
    choice_q = preset("single_choice", fields=(single_choice_field(label=f"Quem é <b>{ZEFIRO.name}</b>?",
                                                                   options=options, correct="c"),
                                               multiple_choice_field("cb", options=options, correct=("a",))))
    _, choice_ir, _ = build_variant(choice_q, "2b")
    pieces = [canary_ir["statement"]["html"], choice_ir["statement"]["html"]]
    for ir in (canary_ir, choice_ir):
        for field_ir in ir["fields"]:
            if field_ir["label_html"]:
                pieces.append(field_ir["label_html"])
            for option in field_ir["options"] or ():
                pieces.append(option["html"])
    return pieces


def test_plain_is_the_same_in_the_three_formatters_and_summary_renders() -> None:
    """O `plain` dos três formatadores é igual sobre a IR canário (o conteúdo do relatório não depende
    do canal; **[F]** `_Formatter.plain` é código do disparo, `telegramrender.py:661`), na mesma função de um caso
    de `ChannelSummaryRenderer.render` sobre a mesma IR: o resumo de um relatório cujo título é o
    `plain` do enunciado canário é igual nos três dialetos ao composto pelos textos de `noticetexts`."""
    pieces = _canary_ir_html()
    assert len(pieces) >= 8
    for html in pieces:
        rich, html_plain, plain = (TelegramRichFormatter().plain(html), TelegramHtmlFormatter().plain(html),
                                   PlainFormatter().plain(html))
        assert rich == html_plain == plain, html
    title = PlainFormatter().plain(pieces[0])
    report = replace(REPORT, dispatch=replace(REPORT.dispatch, title=title))
    for name, factory in FORMATTERS.items():
        formatter = factory()
        rendered = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE).render(report)
        assert rendered.body == _expected_summary(report, formatter, resend=False), name


@pytest.mark.parametrize("resend", [False, True], ids=["closing", "resend"])
@pytest.mark.parametrize("dialect", list(FORMATTERS))
def test_channel_summary_renders_head_numbers_in_dialect(dialect: str, resend: bool) -> None:
    """O resumo de fechamento (`resend=False`) e o de reenvio (`resend=True`) no Rich, no HTML e no texto
    simples: igual ao modelo de `noticetexts` composto no dialeto, com o título, a janela no fuso, o
    alcance, os pares, a taxa de resposta em par, os tempos, as correções pendentes só quando há, e o
    comando `cq reports show <código> <dispatch_id>`; aceito pelo validador do canal. Os construtores reais
    na mesma função."""
    formatter = FORMATTERS[dialect]()
    report = FROZEN_REPORT if resend else REPORT
    renderer = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE, resend=resend)
    rendered = renderer.render(report)
    assert isinstance(rendered, RenderedReport)
    assert rendered.body == _expected_summary(report, formatter, resend=resend)
    assert _channel_problems(formatter, rendered.body) == []
    shown = _plain_of(formatter, rendered.body)
    for line in _summary_plain_lines(report, resend=resend):
        assert line in shown, line
    assert f"cq reports show {CODE} {DISPATCH_ID}" in shown
    head = "Resumo reenviado (versão 2): Velocidade média" if resend else "Disparo fechado: Velocidade média"
    assert head in shown
    assert "Respostas: " + noticetexts.pair(18, 23) in shown
    if resend:
        assert "Correções pendentes: 3" in shown
        assert shown.index("Correções pendentes: 3") < shown.index("Relatório completo:")
    else:
        assert "Correções pendentes" not in shown
    for text in DISTRIBUTION_TEXTS:
        assert text not in shown, text
    # Real no esqueleto: o construtor só guarda os argumentos.
    assert (renderer.formatter, renderer.timezone, renderer.offering_code, renderer.resend) == (
        formatter, ZONE, CODE, resend)
    default = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE)
    assert default.resend is False


@pytest.mark.parametrize("dialect", list(FORMATTERS))
def test_channel_summary_shows_dash_on_empty_values(dialect: str) -> None:
    """Sem respostas e sem título: "questão sem título", "—" nas taxas e nos tempos, pares com zero."""
    formatter = FORMATTERS[dialect]()
    empty = replace(
        REPORT, dispatch=replace(REPORT.dispatch, title=None),
        reach=Reach(0, 0, 0, NotDeliveredByReason(0, 0, 0, 0, 0), 0, QuestionFailedByStage(0, 0), 0),
        performance=Performance(Pair(0, 0), Pair(0, 0), Pair(0, 0), Pair(0, 0), Pair(0, 0), RatePair(None, None),
                                RatePair(None, None), RatePair(None, None), RatePair(None, None),
                                RatePair(None, None)),
        response_time=ResponseTime(0, 0, None, None, None),
        distribution=Distribution(fields=()), friction=Friction(fields=(), unmapped_format_errors=Pair(0, 0)))
    rendered = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE).render(empty)
    assert rendered.body == _expected_summary(empty, formatter, resend=False)
    shown = _plain_of(formatter, rendered.body)
    assert "Disparo fechado: questão sem título" in shown
    assert "Taxa de resposta: — no prazo · — com atrasadas" in shown
    assert "Tempo até responder, no prazo: mediana —, primeiro quartil —, terceiro quartil —" in shown
    assert "Respostas: 0 no prazo · 0 com atrasadas" in shown
    assert "Alcance: 0 alunos-alvo, 0 entregues, 0 não entregues, 0 em falha de questão" in shown
    assert _channel_problems(formatter, rendered.body) == []


@pytest.mark.parametrize("dialect", list(FORMATTERS))
def test_channel_summary_escapes_title(dialect: str) -> None:
    """O título com `<`, `&`, tags, URL e `/comando` é escapado no dialeto (nenhuma tag do título chega ao
    canal) e o texto mostrado é o título literal."""
    formatter = FORMATTERS[dialect]()
    title = '<b>Prova</b> & "aspas" <script>x</script> https://exemplo.invalid/a?b=1&c=2 /start'
    report = replace(REPORT, dispatch=replace(REPORT.dispatch, title=title))
    rendered = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE).render(report)
    assert rendered.body == _expected_summary(report, formatter, resend=False)
    assert _channel_problems(formatter, rendered.body) == []
    assert f"Disparo fechado: {title}" in _plain_of(formatter, rendered.body)
    if dialect != "plain":
        assert "<b>Prova" not in rendered.body
        assert "<script>" not in rendered.body
        assert "&lt;b&gt;Prova&lt;/b&gt; &amp;" in rendered.body


@pytest.mark.parametrize("dialect", list(FORMATTERS))
def test_channel_summary_truncates_only_the_title(dialect: str) -> None:
    """Um título maior que o limite do canal é truncado para caber (o texto reinjetado no validador da
    `DispatchExtension` é aceito); os números, as linhas fixas e o comando nunca são cortados."""
    formatter = FORMATTERS[dialect]()
    title = "Título muito longo & <marcado> " * 1500
    report = replace(REPORT, dispatch=replace(REPORT.dispatch, title=title), grading_pending=2)
    rendered = ChannelSummaryRenderer(formatter, timezone=ZONE, offering_code=CODE).render(report)
    assert _channel_problems(formatter, rendered.body) == []
    shown = _plain_of(formatter, rendered.body)
    limit = RICH_TEXT_MAX if dialect == "rich" else TEXT_MAX
    assert len(shown.encode("utf-16-le")) // 2 <= limit
    assert title not in shown
    assert "Disparo fechado: Título muito longo & <marcado> Título muito longo" in shown
    for line in _summary_plain_lines(report, resend=False)[1:]:
        assert line in shown, line
    assert "Correções pendentes: 2" in shown
    assert shown.rstrip().endswith(f"cq reports show {CODE} {DISPATCH_ID}")
