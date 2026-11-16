"""Contratos dos relatórios e do acompanhamento campo a campo: os arquivos congelados e o que a
implementação publica.

Confere os arquivos de contrato congelados dos relatórios (`contracts/api/v1/`), que existem desde o
esqueleto, **e**, na mesma função, o mesmo recorte no que a implementação publica (cada função falha no esqueleto:
nenhuma função deste arquivo lê só os arquivos congelados):
- a forma exata do `Report`, propriedade a propriedade: `report_schema_version` `const "1"`,
  os enums, os anuláveis, o `Pair` nunca anulável e o `RatePair` anulável, o par `state`/`partial`; e a dos
  outros 5 esquemas novos (as versões, o reenvio, o resumo diário e o pedido da alteração);
- por tipo de campo, as combinações de `choices`/`values`/`ratios`/`unrated`/`texts` aceitas e recusadas,
  com a indisponibilidade `purged`;
- nenhum nome de gabarito no relatório e nenhum `correct` em `distribution`, com controle
  positivo sintético;
- `x-untrusted` sempre com `x-free-text`, exatamente nos caminhos do contrato, e na raiz da mídia
  `text/csv`; só `x-free-text` no `qid`; nenhum `x-identity` nem `x-secret` novo (continuam 11 e 3);
- nenhum campo novo chamado `code` (fora de `offering.code`), `link`, `recipient`, `secret`, `until`,
  `username` ou `token_id`, nos 6 arquivos e nas 3 variantes de evento;
- as 3 variantes de evento (`report.frozen`, `notice.recorded`, `offering.digest_changed`) com os campos e os
  valores esperados, com conjuntos de campos distintos dos 43 anteriores;
- as 5 operações no catálogo, no registro de `cq_server.api` e no OpenAPI, expostas ao MCP com
  `confirm: false` e não destrutivas, só `offerings.update_digest` com corpo, o 200 de `reports.get` com as
  duas mídias;
- as respostas de erro das 5 operações só com códigos existentes (nenhum novo; `error.schema.json` com os 19).

O publicado: o OpenAPI de `create_app(...).openapi()` (sem banco) e o registro `cq_server.api.operations.BY_ID`
para as 5 operações (ausentes até a implementação delas: vermelho); `EventType` e as variantes
de `cq_server.api.schemas` publicadas no `events.list` para as 3 variantes (ausentes até a implementação).
Escrito para aceitar extensão: nenhum total de operações, de eventos ou de erros do sistema é afirmado,
salvo as contagens de marcas que o contrato fixa (11 e 3) e os 19 códigos (nenhum novo nos relatórios).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from jsonschema import Draft202012Validator

from tests.contract.test_contract_dispatch import (
    _deref,
    _enum_at,
    _expand,
    _nodes,
    _paths,
    _property_names,
    _published,
    _published_operation,
    _request_schema,
    _success_schema,
    _untrusted_without_free_text,
)
from tests.helpers.contract import CONTRACTS, MESSAGES, SCHEMAS, flatten, load_catalog, load_json

# padrões do contrato, da API base aos relatórios.
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{2,39}$"
CONTENT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
COMMIT_SHORT_PATTERN = r"^[0-9a-f]{12}$"
LOCAL_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

REPORT_OPERATIONS = ("reports.get", "reports.list_versions", "reports.resend_summary", "offerings.get_digest",
                 "offerings.update_digest")
REPORT_RESPONSE_FILES = tuple(f"{op}.response.schema.json" for op in REPORT_OPERATIONS)
REPORT_SCHEMA_FILES = (*REPORT_RESPONSE_FILES, "offerings.update_digest.request.schema.json")
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")

# na ordem do contrato.
DISPATCH_STATES = ["open", "closed"]
ORIGINS = ["cli", "api", "mcp", "integration"]
TRIGGERS = ["closing", "resend"]
FIELD_TYPES = ["number", "integer", "text", "single_choice", "multiple_choice"]
NOT_DELIVERED_REASONS = ["endpoint_blocked", "channel_refused", "engine_unavailable", "bot_no_access",
                         "channel_unavailable"]
QUESTION_FAILED_STAGES = ["preparation", "grading"]
COUNTS = ["answered", "correct", "partially_correct", "incorrect", "dont_know"]
RATES = ["correct_rate", "partially_correct_rate", "incorrect_rate", "response_rate", "mean_score"]
# os 10 tipos de aviso e os estados finais (o `pending` nunca vai ao evento).
NOTICE_TYPES = ["dispatch_preview", "dispatch_rejected", "dispatch_opened", "dispatch_closed", "summary_resent",
                "dispatch_cancelled", "question_failed", "bot_access_restored", "invite_failures", "daily_digest"]
NOTICE_STATES = ["queued", "no_recipient", "no_activity", "missed", "not_sent", "gave_up"]

_UUID = {"type": "string", "pattern": UUID4_PATTERN}
_TIME = {"type": "string", "pattern": TIME_PATTERN}
_COUNT = {"type": "integer", "minimum": 0}
_UNTRUSTED = {"x-free-text": True, "x-untrusted": True}
_NULLABLE_NUMBER = {"type": ["number", "null"]}

OFFERING_REF = {
    "type": "object", "additionalProperties": False, "required": ["offering_id", "code"],
    "properties": {"offering_id": _UUID, "code": {"type": "string", "pattern": CODE_PATTERN}},
}
TIMEZONE = {"type": "string", "minLength": 1, "maxLength": 64}
PAIR = {"type": "object", "additionalProperties": False, "required": ["on_time", "with_late"],
        "properties": {"on_time": _COUNT, "with_late": _COUNT}}
RATE_PAIR = {"type": "object", "additionalProperties": False, "required": ["on_time", "with_late"],
             "properties": {"on_time": _NULLABLE_NUMBER, "with_late": _NULLABLE_NUMBER}}
FROZEN_REF_PROPERTIES = {"version": {"type": "integer", "minimum": 1}, "trigger": {"enum": TRIGGERS},
                         "frozen_at": _TIME}
FROZEN_REF = {"type": "object", "additionalProperties": False, "required": ["version", "trigger", "frozen_at"],
              "properties": FROZEN_REF_PROPERTIES}


def _top_list(nullable: bool) -> dict:
    return {
        "type": ["object", "null"] if nullable else "object", "additionalProperties": False,
        "required": ["items", "others"],
        "properties": {
            "items": {"type": "array", "maxItems": 5, "items": {
                "type": "object", "additionalProperties": False, "required": ["value", "count"],
                "properties": {"value": {"type": "string", **_UNTRUSTED}, "count": PAIR}}},
            "others": PAIR,
        },
    }


_NULL = {"const": None}
_NOT_NULL = {"not": {"const": None}}
FIELD_DISTRIBUTION = {
    "type": "object", "additionalProperties": False,
    "required": ["index", "type", "label", "unavailable", "choices", "values", "ratios", "unrated", "texts"],
    "properties": {
        "index": _COUNT,
        "type": {"enum": FIELD_TYPES},
        "label": {"type": ["string", "null"], **_UNTRUSTED},
        "unavailable": {"type": ["string", "null"], "enum": ["purged", None]},
        "choices": {"type": ["array", "null"], "items": {
            "type": "object", "additionalProperties": False, "required": ["content_hash", "text", "count"],
            "properties": {"content_hash": {"type": "string", "pattern": CONTENT_HASH_PATTERN},
                           "text": {"type": "string", **_UNTRUSTED}, "count": PAIR}}},
        "values": _top_list(True),
        "ratios": _top_list(True),
        "unrated": {**PAIR, "type": ["object", "null"]},
        "texts": _top_list(True),
    },
    # escolhas → `choices`; número e inteiro → `values`, `ratios`, `unrated`; texto →
    # `texts`; com `purged`, as listas de número e texto nulas.
    "oneOf": [
        {"properties": {"type": {"enum": ["single_choice", "multiple_choice"]}, "choices": _NOT_NULL,
                        "values": _NULL, "ratios": _NULL, "unrated": _NULL, "texts": _NULL}},
        {"properties": {"type": {"enum": ["number", "integer"]}, "unavailable": _NULL, "choices": _NULL,
                        "values": _NOT_NULL, "ratios": _NOT_NULL, "unrated": _NOT_NULL, "texts": _NULL}},
        {"properties": {"type": {"const": "text"}, "unavailable": _NULL, "choices": _NULL, "values": _NULL,
                        "ratios": _NULL, "unrated": _NULL, "texts": _NOT_NULL}},
        {"properties": {"type": {"enum": ["number", "integer", "text"]}, "unavailable": {"const": "purged"},
                        "choices": _NULL, "values": _NULL, "ratios": _NULL, "unrated": _NULL, "texts": _NULL}},
    ],
}
REPORT_PROPERTIES: dict[str, Any] = {
    "report_schema_version": {"const": "1"},
    "offering": OFFERING_REF,
    "timezone": TIMEZONE,
    "dispatch": {
        "type": "object", "additionalProperties": False,
        "required": ["dispatch_id", "question", "title", "state", "partial", "origin", "opens_at", "closes_at"],
        "properties": {
            "dispatch_id": _UUID,
            "question": {"type": "object", "additionalProperties": False, "required": ["qid", "commit_short"],
                         "properties": {"qid": {"type": "string", "minLength": 1, "x-free-text": True},
                                        "commit_short": {"type": "string", "pattern": COMMIT_SHORT_PATTERN}}},
            "title": {"type": ["string", "null"], **_UNTRUSTED},
            "state": {"enum": DISPATCH_STATES},
            "partial": {"type": "boolean"},
            "origin": {"enum": ORIGINS},
            "opens_at": _TIME,
            "closes_at": _TIME,
        },
        # `partial` verdadeiro se e só se `open`.
        "oneOf": [{"properties": {"state": {"const": "open"}, "partial": {"const": True}}},
                  {"properties": {"state": {"const": "closed"}, "partial": {"const": False}}}],
    },
    "computed_at": _TIME,
    "frozen": {**FROZEN_REF, "type": ["object", "null"]},
    "grading_pending": _COUNT,
    "reach": {
        "type": "object", "additionalProperties": False,
        "required": ["target", "delivered", "not_delivered", "not_delivered_by_reason", "question_failed",
                     "question_failed_by_stage", "pending"],
        "properties": {
            "target": _COUNT,
            "delivered": _COUNT,
            "not_delivered": _COUNT,
            "not_delivered_by_reason": {"type": "object", "additionalProperties": False,
                                        "required": NOT_DELIVERED_REASONS,
                                        "properties": {name: _COUNT for name in NOT_DELIVERED_REASONS}},
            "question_failed": _COUNT,
            "question_failed_by_stage": {"type": "object", "additionalProperties": False,
                                         "required": QUESTION_FAILED_STAGES,
                                         "properties": {name: _COUNT for name in QUESTION_FAILED_STAGES}},
            "pending": _COUNT,
        },
    },
    "performance": {
        "type": "object", "additionalProperties": False, "required": [*COUNTS, *RATES],
        "properties": {**{name: PAIR for name in COUNTS}, **{name: RATE_PAIR for name in RATES}},
    },
    "response_time": {
        "type": "object", "additionalProperties": False,
        "required": ["observations", "negative", "median_ms", "q1_ms", "q3_ms"],
        "properties": {"observations": _COUNT, "negative": _COUNT, "median_ms": _NULLABLE_NUMBER,
                       "q1_ms": _NULLABLE_NUMBER, "q3_ms": _NULLABLE_NUMBER},
    },
    "distribution": {"type": "object", "additionalProperties": False, "required": ["fields"],
                     "properties": {"fields": {"type": "array", "items": FIELD_DISTRIBUTION}}},
    "friction": {
        "type": "object", "additionalProperties": False, "required": ["fields", "unmapped_format_errors"],
        "properties": {
            "fields": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["index", "format_errors", "decimal_comma"],
                "properties": {"index": _COUNT, "format_errors": PAIR, "decimal_comma": PAIR}}},
            "unmapped_format_errors": PAIR,
        },
    },
}
DIGEST_PROPERTIES: dict[str, Any] = {
    "offering": OFFERING_REF,
    "timezone": TIMEZONE,
    "enabled": {"type": "boolean"},
    "local_time": {"type": "string", "pattern": LOCAL_TIME_PATTERN},
}
OTHER_RESPONSE_PROPERTIES: dict[str, dict[str, Any]] = {
    "reports.list_versions.response.schema.json": {
        "offering": OFFERING_REF,
        "dispatch_id": _UUID,
        "items": {"type": "array", "items": FROZEN_REF},
        "next_cursor": {"type": ["integer", "null"], "minimum": 0},
    },
    "reports.resend_summary.response.schema.json": {"offering": OFFERING_REF, "dispatch_id": _UUID,
                                                    "frozen": FROZEN_REF},
    "offerings.get_digest.response.schema.json": DIGEST_PROPERTIES,
    "offerings.update_digest.response.schema.json": DIGEST_PROPERTIES,
}
DIGEST_REQUEST_PROPERTIES = {"enabled": {"type": "boolean"},
                             "local_time": {"type": "string", "pattern": LOCAL_TIME_PATTERN}}

# os dados dos eventos novos (o assunto do aviso, sem chave interna).
EVENT_DATA: dict[str, dict[str, Any]] = {
    "report.frozen": {"offering_id": _UUID, "dispatch_id": _UUID, "version": {"type": "integer", "minimum": 1},
                      "trigger": {"enum": TRIGGERS}},
    "notice.recorded": {
        "offering_id": _UUID,
        "notice_type": {"enum": NOTICE_TYPES},
        "dispatch_id": {"type": ["string", "null"], "pattern": UUID4_PATTERN},
        "report_version": {"type": ["integer", "null"], "minimum": 1},
        "episode_seq": {"type": ["integer", "null"], "minimum": 1},
        "batch_id": {"type": ["string", "null"], "pattern": UUID4_PATTERN},
        "window_started_at": {"type": ["string", "null"], "pattern": TIME_PATTERN},
        "local_date": {"type": ["string", "null"], "pattern": DATE_PATTERN},
        "state": {"enum": NOTICE_STATES},
        "recipient_count": _COUNT,
    },
    "offering.digest_changed": {"offering_id": _UUID, "enabled": {"type": "boolean"},
                                "local_time": {"type": "string", "pattern": LOCAL_TIME_PATTERN}},
}

# `x-untrusted` (sempre com `x-free-text`) e só `x-free-text`, por arquivo.
_REPORT_UNTRUSTED = {"dispatch.title", "distribution.fields[].label", "distribution.fields[].choices[].text",
                     "distribution.fields[].values.items[].value", "distribution.fields[].ratios.items[].value",
                     "distribution.fields[].texts.items[].value"}
EXPECTED_UNTRUSTED: dict[str, set[str]] = {name: set() for name in REPORT_SCHEMA_FILES} | {
    "reports.get.response.schema.json": _REPORT_UNTRUSTED}
EXPECTED_FREE_TEXT_ONLY: dict[str, set[str]] = {name: set() for name in REPORT_SCHEMA_FILES} | {
    "reports.get.response.schema.json": {"dispatch.question.qid"}}
REPORT_CSV_SCHEMA = {"type": "string", "x-free-text": True, "x-untrusted": True}

# nomes que nenhum campo novo pode ter (e `code`, só em `offering.code`).
RESERVED_NAMES = {"link", "recipient", "secret", "until", "username", "token_id"}
# nenhum campo do relatório com nome de gabarito; nenhum `correct` na distribuição.
ANSWER_KEY_NAMES = {"correct_answer", "answer_key", "is_correct", "correct_value", "explanation"}

# os erros de cada operação, todos já existentes (os 4 genéricos da API base e os da
# regra, na ordem das verificações).
_V1_ERRORS = {"invalid_request", "unauthenticated", "forbidden", "internal"}
EXPECTED_ERRORS: dict[str, set[str]] = {
    "reports.get": _V1_ERRORS | {"not_found", "invalid_dispatch_state"},
    "reports.list_versions": _V1_ERRORS | {"not_found", "invalid_dispatch_state"},
    "reports.resend_summary": _V1_ERRORS | {"not_found", "invalid_state", "invalid_dispatch_state"},
    "offerings.get_digest": _V1_ERRORS | {"not_found"},
    "offerings.update_digest": _V1_ERRORS | {"not_found", "invalid_state"},
}

# Amostras.
UUID_SAMPLE = "3f2b8c1e-9a4d-4e7f-8b2a-1c0d9e8f7a6b"
HASH_SAMPLE = "sha256:" + "a" * 64
_PAIR = {"on_time": 1, "with_late": 2}
_RATE = {"on_time": 0.5, "with_late": None}
_TOP = {"items": [{"value": "42", "count": {"on_time": 2, "with_late": 3}}], "others": {"on_time": 0, "with_late": 1}}
CHOICE_FIELD = {"index": 0, "type": "single_choice", "label": "Alternativas", "unavailable": None,
                "choices": [{"content_hash": HASH_SAMPLE, "text": "Opção A", "count": _PAIR}], "values": None,
                "ratios": None, "unrated": None, "texts": None}
NUMBER_FIELD = {"index": 1, "type": "number", "label": None, "unavailable": None, "choices": None, "values": _TOP,
                "ratios": _TOP, "unrated": _PAIR, "texts": None}
TEXT_FIELD = {"index": 2, "type": "text", "label": "Resposta", "unavailable": None, "choices": None,
              "values": None, "ratios": None, "unrated": None, "texts": _TOP}
REPORT_SAMPLE: dict[str, Any] = {
    "report_schema_version": "1",
    "offering": {"offering_id": UUID_SAMPLE, "code": "mc102-2031s1"},
    "timezone": "America/Sao_Paulo",
    "dispatch": {"dispatch_id": UUID_SAMPLE, "question": {"qid": "questoes/q1", "commit_short": "a" * 12},
                 "title": "Questão neutra", "state": "closed", "partial": False, "origin": "cli",
                 "opens_at": "2031-01-01T10:00:00Z", "closes_at": "2031-01-01T11:00:00Z"},
    "computed_at": "2031-01-01T11:10:00.123456Z",
    "frozen": {"version": 1, "trigger": "closing", "frozen_at": "2031-01-01T11:10:00Z"},
    "grading_pending": 0,
    "reach": {"target": 3, "delivered": 2, "not_delivered": 1,
              "not_delivered_by_reason": {name: 0 for name in NOT_DELIVERED_REASONS} | {"endpoint_blocked": 1},
              "question_failed": 0, "question_failed_by_stage": {"preparation": 0, "grading": 0}, "pending": 0},
    "performance": {**{name: _PAIR for name in COUNTS}, **{name: _RATE for name in RATES}},
    "response_time": {"observations": 2, "negative": 0, "median_ms": 1500.0, "q1_ms": -20.5, "q3_ms": 3000},
    "distribution": {"fields": [CHOICE_FIELD, NUMBER_FIELD, TEXT_FIELD]},
    "friction": {"fields": [{"index": 1, "format_errors": _PAIR, "decimal_comma": _PAIR}],
                 "unmapped_format_errors": {"on_time": 0, "with_late": 0}},
}


def _load(name: str) -> Any:
    return load_json(str(SCHEMAS / name))


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def _with(sample: dict, path: str, value: Any) -> dict:
    """Cópia de `sample` com `value` no caminho pontuado `path` (índices de lista como números)."""
    out = copy.deepcopy(sample)
    target: Any = out
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    last = parts[-1]
    if isinstance(target, list):
        target[int(last)] = value
    elif value is _DELETE:
        del target[last]
    else:
        target[last] = value
    return out


_DELETE = object()


def _report_with_field(field: dict) -> dict:
    return _with(REPORT_SAMPLE, "distribution.fields", [field])


def _event_variants() -> dict[str, dict]:
    schema = _load("events.list.response.schema.json")
    return {v["properties"]["type"]["const"]: v for v in schema["properties"]["items"]["items"]["oneOf"]}


def _event_page(event_type: str, data: dict) -> dict:
    return {"items": [{"seq": 1, "type": event_type, "occurred_at": "2031-01-01T00:00:00Z",
                       "actor_token_id": None, "data": data}], "next_cursor": 1}


def _answer_key_names(schema: dict) -> list[str]:
    """Os caminhos com nome de gabarito, em qualquer lugar, e os `correct` dentro de `distribution`."""
    return sorted(path for path, name in _property_names(schema)
                  if name in ANSWER_KEY_NAMES or (name == "correct" and path.startswith("distribution.")))


def _published_answer_key_names(paths: set[str]) -> list[str]:
    found = []
    for path in paths:
        name = path.replace("[]", "").rsplit(".", 1)[-1]
        if name in ANSWER_KEY_NAMES or (name == "correct" and path.startswith("distribution.")):
            found.append(path)
    return sorted(found)


def _forbidden_names(schema: dict) -> list[str]:
    """Campos chamados `code` fora de `offering.code` e campos com um nome de `RESERVED_NAMES`."""
    return sorted(path for path, name in _property_names(schema)
                  if (name == "code" and path != "offering.code") or name in RESERVED_NAMES)


def _published_forbidden(paths: set[str]) -> list[str]:
    found = []
    for path in paths:
        name = path.replace("[]", "").rsplit(".", 1)[-1]
        if (name == "code" and path != "offering.code") or name in RESERVED_NAMES:
            found.append(path)
    return sorted(found)


def _mark_points(node: Any, key: str) -> int:
    """Quantas vezes a palavra-chave `key` (`x-identity`, `x-secret`) aparece no documento."""
    if isinstance(node, dict):
        return int(key in node) + sum(_mark_points(value, key) for value in node.values())
    if isinstance(node, list):
        return sum(_mark_points(item, key) for item in node)
    return 0


def _published_event_variants(doc: dict) -> dict[str, dict]:
    """Tipo → o subesquema `data` (resolvido) das variantes do `events.list` publicado."""
    page = _deref(_success_schema(doc, "events.list"), doc)
    items = _deref(page["properties"]["items"], doc)
    out: dict[str, dict] = {}
    for node in _expand([items.get("items", {})], doc):
        props = node.get("properties", {})
        if "type" not in props or "data" not in props:
            continue
        kind = _deref(props["type"], doc)
        values = [kind["const"]] if "const" in kind else list(kind.get("enum") or [])
        if len(values) == 1:
            out[values[0]] = _deref(props["data"], doc)
    return out


def _data_properties(data: dict, doc: dict) -> set[str]:
    names: set[str] = set()
    for node in _expand([data], doc):
        names |= set(node.get("properties", {}))
    return names


# ---------------------------------------------------------------------------------------------
# Forma exata dos esquemas novos
# ---------------------------------------------------------------------------------------------


def test_report_schema_matches_contract_property_by_property() -> None:
    """A resposta JSON de
    `reports.get` é o `Report` com exatamente as propriedades do contrato, todas `required`:
    `report_schema_version` `const "1"` (não `schema_version`), o `OfferingRef` e o fuso, o cabeçalho do
    disparo (`state` só `open`/`closed`, `partial` verdadeiro se e só se `open`, as 4 origens, o `qid` e o commit
    curto), `computed_at`, `frozen` (`FrozenRef` anulável), o alcance com os 5 motivos e os 2 estágios, o
    desempenho com os 5 `Pair` (nunca anuláveis) e as 5 taxas `RatePair` (anuláveis), o tempo até responder
    (só a mediana e os quartis anuláveis), a distribuição e a fricção. As formas válidas e inválidas são
    aceitas e recusadas.

    O publicado (vermelho no esqueleto): o 200 de `reports.get` tem a mesma forma achatada e os mesmos
    valores enumerados (estado, origem, gatilho, tipo de campo e indisponibilidade)."""
    schema = _load("reports.get.response.schema.json")
    assert (schema["type"], schema["additionalProperties"]) == ("object", False)
    assert schema["properties"] == REPORT_PROPERTIES
    assert schema["required"] == list(REPORT_PROPERTIES)
    assert "schema_version" not in schema["properties"]

    assert _is_valid(schema, REPORT_SAMPLE)
    for path, value in (
        ("frozen", None),
        ("dispatch.title", None),
        ("dispatch.state", "open"),
        ("response_time.median_ms", None),
        ("performance.mean_score", {"on_time": None, "with_late": None}),
        ("performance.correct_rate", {"on_time": 1, "with_late": 0.25}),
        ("frozen.trigger", "resend"),
        ("distribution.fields", []),
        ("friction.fields", []),
    ):
        sample = _with(REPORT_SAMPLE, path, value)
        if path == "dispatch.state":
            sample = _with(sample, "dispatch.partial", True)
        assert _is_valid(schema, sample), (path, value)
    for origin in ORIGINS:
        assert _is_valid(schema, _with(REPORT_SAMPLE, "dispatch.origin", origin)), origin
    for label, sample in (
        ("versão do esquema 2", _with(REPORT_SAMPLE, "report_schema_version", "2")),
        ("versão do esquema inteira", _with(REPORT_SAMPLE, "report_schema_version", 1)),
        ("sem frozen", _with(REPORT_SAMPLE, "frozen", _DELETE)),
        ("Pair nulo", _with(REPORT_SAMPLE, "performance.answered", None)),
        ("Pair com lado nulo", _with(REPORT_SAMPLE, "performance.correct", {"on_time": None, "with_late": 1})),
        ("Pair negativo", _with(REPORT_SAMPLE, "performance.dont_know", {"on_time": -1, "with_late": 0})),
        ("RatePair nulo", _with(REPORT_SAMPLE, "performance.response_rate", None)),
        ("RatePair sem lado", _with(REPORT_SAMPLE, "performance.incorrect_rate", {"on_time": 0.5})),
        ("aberto não parcial", _with(REPORT_SAMPLE, "dispatch.state", "open")),
        ("fechado parcial", _with(REPORT_SAMPLE, "dispatch.partial", True)),
        ("estado agendado", _with(_with(REPORT_SAMPLE, "dispatch.state", "scheduled"), "dispatch.partial", False)),
        ("origem desconhecida", _with(REPORT_SAMPLE, "dispatch.origin", "web")),
        ("gatilho desconhecido", _with(REPORT_SAMPLE, "frozen.trigger", "archive")),
        ("versão 0", _with(REPORT_SAMPLE, "frozen.version", 0)),
        ("commit curto com 11", _with(REPORT_SAMPLE, "dispatch.question.commit_short", "a" * 11)),
        ("commit curto maiúsculo", _with(REPORT_SAMPLE, "dispatch.question.commit_short", "A" * 12)),
        ("horário com deslocamento", _with(REPORT_SAMPLE, "computed_at", "2031-01-01T11:10:00+00:00")),
        ("fuso vazio", _with(REPORT_SAMPLE, "timezone", "")),
        ("motivo a mais", _with(REPORT_SAMPLE, "reach.not_delivered_by_reason.blocked", 0)),
        ("estágio a menos", _with(REPORT_SAMPLE, "reach.question_failed_by_stage.grading", _DELETE)),
        ("observações negativas", _with(REPORT_SAMPLE, "response_time.observations", -1)),
        ("campo a mais", {**REPORT_SAMPLE, "schema_version": "0005"}),
        ("correções pendentes nulas", _with(REPORT_SAMPLE, "grading_pending", None)),
    ):
        assert not _is_valid(schema, sample), label

    # O publicado: mesma forma achatada e os mesmos valores enumerados.
    doc = _published()
    published = _success_schema(doc, "reports.get")
    assert _paths(flatten(published, doc)) == _paths(flatten(schema, schema))
    assert _enum_at(doc, published, "dispatch.state") == set(DISPATCH_STATES)
    assert _enum_at(doc, published, "dispatch.origin") == set(ORIGINS)
    assert _enum_at(doc, published, "frozen.trigger") == set(TRIGGERS)
    assert _enum_at(doc, published, "distribution.fields[].type") == set(FIELD_TYPES)
    assert _enum_at(doc, published, "distribution.fields[].unavailable") == {"purged"}
    assert _enum_at(doc, published, "report_schema_version") == {"1"}


def test_other_report_schemas_match_contract() -> None:
    """`reports.list_versions` é
    `{offering, dispatch_id, items: [FrozenRef], next_cursor: integer | null}`; `reports.resend_summary` é
    `{offering, dispatch_id, frozen: FrozenRef}` (não anulável); o resumo diário é `{offering, timezone,
    enabled, local_time}` nas duas respostas; o pedido do PATCH é fechado, com `minProperties: 1`, só
    `enabled` (booleano) e `local_time` (`HH:MM`, de `00:00` a `23:59`). As formas válidas e inválidas são
    aceitas e recusadas.

    O publicado (vermelho no esqueleto): as mesmas formas achatadas nas 4 respostas e no
    pedido, e o gatilho das versões com os mesmos valores."""
    for name, properties in OTHER_RESPONSE_PROPERTIES.items():
        schema = _load(name)
        assert (schema["type"], schema["additionalProperties"]) == ("object", False), name
        assert schema["properties"] == properties, name
        assert schema["required"] == list(properties), name

    request = _load("offerings.update_digest.request.schema.json")
    assert (request["type"], request["additionalProperties"], request["minProperties"]) == ("object", False, 1)
    assert request["properties"] == DIGEST_REQUEST_PROPERTIES
    assert "required" not in request or request["required"] == []
    for body in ({"enabled": True}, {"enabled": False}, {"local_time": "00:00"}, {"local_time": "23:59"},
                 {"enabled": True, "local_time": "07:05"}):
        assert _is_valid(request, body), body
    for body in ({}, {"enabled": None}, {"enabled": "on"}, {"local_time": "24:00"}, {"local_time": "7h"},
                 {"local_time": "7:05"}, {"local_time": "07:60"}, {"local_time": None}, {"timezone": "UTC"},
                 {"enabled": True, "extra": 1}):
        assert not _is_valid(request, body), body

    digest = _load("offerings.get_digest.response.schema.json")
    base = {"offering": {"offering_id": UUID_SAMPLE, "code": "mc102-2031s1"}, "timezone": "America/Sao_Paulo",
            "enabled": False, "local_time": "20:00"}
    assert _is_valid(digest, base)
    for bad in ({**base, "enabled": None}, {**base, "local_time": "20h"}, {**base, "local_time": None},
                {k: v for k, v in base.items() if k != "timezone"}):
        assert not _is_valid(digest, bad), bad
    versions = _load("reports.list_versions.response.schema.json")
    page = {"offering": base["offering"], "dispatch_id": UUID_SAMPLE,
            "items": [{"version": 1, "trigger": "closing", "frozen_at": "2031-01-01T11:10:00Z"},
                      {"version": 2, "trigger": "resend", "frozen_at": "2031-01-01T12:00:00Z"}], "next_cursor": 2}
    assert _is_valid(versions, page)
    assert _is_valid(versions, {**page, "items": [], "next_cursor": None})
    for bad in ({**page, "next_cursor": -1}, {**page, "items": [{"version": 1, "trigger": "manual",
                                                                  "frozen_at": "2031-01-01T11:10:00Z"}]}):
        assert not _is_valid(versions, bad), bad
    resend = _load("reports.resend_summary.response.schema.json")
    result = {"offering": base["offering"], "dispatch_id": UUID_SAMPLE,
              "frozen": {"version": 2, "trigger": "resend", "frozen_at": "2031-01-01T12:00:00Z"}}
    assert _is_valid(resend, result)
    assert not _is_valid(resend, {**result, "frozen": None})

    # O publicado: as mesmas formas achatadas e os mesmos valores.
    doc = _published()
    for name in OTHER_RESPONSE_PROPERTIES:
        op = name.removesuffix(".response.schema.json")
        frozen = _load(name)
        assert _paths(flatten(_success_schema(doc, op), doc)) == _paths(flatten(frozen, frozen)), op
    assert _enum_at(doc, _success_schema(doc, "reports.list_versions"), "items[].trigger") == set(TRIGGERS)
    assert _enum_at(doc, _success_schema(doc, "reports.resend_summary"), "frozen.trigger") == set(TRIGGERS)
    assert _paths(flatten(_request_schema(doc, "offerings.update_digest"), doc)) == set(DIGEST_REQUEST_PROPERTIES)


def test_distribution_combinations_by_field_type() -> None:
    """Por tipo de campo: escolhas →
    `choices` preenchido e os demais `null`; número e inteiro → `values`, `ratios` e `unrated` preenchidos;
    texto → `texts`; com `unavailable = "purged"` (número ou texto), as quatro listas `null`. Cada lista tem
    no máximo 5 itens, o valor redigido é texto e o `others` é um `Pair`. As combinações fora da tabela são
    recusadas.

    O publicado (vermelho no esqueleto): o campo da distribuição publicado tem os mesmos caminhos e os
    mesmos tipos de campo."""
    schema = _load("reports.get.response.schema.json")
    accepted = {
        "escolha única": CHOICE_FIELD,
        "escolha múltipla sem alternativa contada": {**CHOICE_FIELD, "type": "multiple_choice", "choices": []},
        "número": NUMBER_FIELD,
        "inteiro": {**NUMBER_FIELD, "type": "integer"},
        "número com listas vazias": {**NUMBER_FIELD, "values": {"items": [], "others": _PAIR},
                                     "ratios": {"items": [], "others": _PAIR}},
        "texto": TEXT_FIELD,
        "texto sem rótulo": {**TEXT_FIELD, "label": None},
        "número apagado": {**NUMBER_FIELD, "unavailable": "purged", "values": None, "ratios": None,
                           "unrated": None},
        "texto apagado": {**TEXT_FIELD, "unavailable": "purged", "texts": None},
        "lista com 5 itens": {**TEXT_FIELD, "texts": {"items": [_TOP["items"][0]] * 5, "others": _PAIR}},
        "valor numérico redigido com sinal": {**NUMBER_FIELD, "values": {
            "items": [{"value": "-3", "count": _PAIR}], "others": _PAIR}},
    }
    refused = {
        "escolha sem choices": {**CHOICE_FIELD, "choices": None},
        "escolha com values": {**CHOICE_FIELD, "values": _TOP},
        "escolha com texts": {**CHOICE_FIELD, "texts": _TOP},
        "escolha com unrated": {**CHOICE_FIELD, "unrated": _PAIR},
        "número sem values": {**NUMBER_FIELD, "values": None},
        "número sem ratios": {**NUMBER_FIELD, "ratios": None},
        "inteiro sem unrated": {**NUMBER_FIELD, "type": "integer", "unrated": None},
        "número com choices": {**NUMBER_FIELD, "choices": []},
        "número com texts": {**NUMBER_FIELD, "texts": _TOP},
        "texto sem texts": {**TEXT_FIELD, "texts": None},
        "texto com values": {**TEXT_FIELD, "values": _TOP},
        "texto com choices": {**TEXT_FIELD, "choices": []},
        "número apagado com values": {**NUMBER_FIELD, "unavailable": "purged"},
        "texto apagado com texts": {**TEXT_FIELD, "unavailable": "purged"},
        "indisponibilidade desconhecida": {**TEXT_FIELD, "unavailable": "deleted"},
        "tipo desconhecido": {**TEXT_FIELD, "type": "matrix"},
        "lista com 6 itens": {**TEXT_FIELD, "texts": {"items": [_TOP["items"][0]] * 6, "others": _PAIR}},
        "valor não textual": {**NUMBER_FIELD, "values": {"items": [{"value": 3, "count": _PAIR}], "others": _PAIR}},
        "lista sem others": {**TEXT_FIELD, "texts": {"items": []}},
        "alternativa com correct": {**CHOICE_FIELD, "choices": [{**CHOICE_FIELD["choices"][0], "correct": True}]},
        "alternativa com hash curto": {**CHOICE_FIELD, "choices": [{**CHOICE_FIELD["choices"][0],
                                                                    "content_hash": "sha256:abc"}]},
        "índice negativo": {**TEXT_FIELD, "index": -1},
        "campo com o nome do PL": {**TEXT_FIELD, "name": "resposta"},
    }
    for label, field in accepted.items():
        assert _is_valid(schema, _report_with_field(field)), label
    for label, field in refused.items():
        assert not _is_valid(schema, _report_with_field(field)), label

    # O publicado: os mesmos caminhos do campo da distribuição e os mesmos tipos de campo.
    doc = _published()
    published = _success_schema(doc, "reports.get")
    prefix = "distribution.fields[]"
    published_paths = {p for p in _paths(flatten(published, doc)) if p.startswith(prefix)}
    frozen_paths = {p for p in _paths(flatten(schema, schema)) if p.startswith(prefix)}
    assert published_paths == frozen_paths
    assert _enum_at(doc, published, f"{prefix}.type") == set(FIELD_TYPES)


# ---------------------------------------------------------------------------------------------
# Gabarito, marcas e nomes
# ---------------------------------------------------------------------------------------------


def test_report_has_no_answer_key_field() -> None:
    """Nenhum campo dos 6 arquivos novos nem das 3 variantes de evento
    se chama `correct_answer`, `answer_key`, `is_correct`, `correct_value` ou `explanation`, e nenhum item de
    `distribution` tem `correct` (as contagens `performance.correct` e `correct_rate` são desempenho, não
    gabarito). Controle positivo: o mesmo verificador acusa um esquema sintético com `correct` numa
    alternativa e cada nome de gabarito, e não acusa `performance.correct`.

    O publicado (vermelho no esqueleto): nenhum desses nomes nas 5 respostas publicadas
    nem no pedido de `offerings.update_digest`."""
    for name in REPORT_SCHEMA_FILES:
        assert _answer_key_names(_load(name)) == [], name
    variants = _event_variants()
    assert set(REPORT_EVENT_TYPES) <= set(variants)
    for event_type in REPORT_EVENT_TYPES:
        assert _answer_key_names(variants[event_type]["properties"]["data"]) == [], event_type
    report = _load("reports.get.response.schema.json")
    assert ("performance.correct", "correct") in _property_names(report)

    synthetic = copy.deepcopy(report)
    choice = synthetic["properties"]["distribution"]["properties"]["fields"]["items"]["properties"]["choices"]
    choice["items"]["properties"]["correct"] = {"type": "boolean"}
    assert _answer_key_names(synthetic) == ["distribution.fields[].choices[].correct"]
    for field in sorted(ANSWER_KEY_NAMES):
        node = {"type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {field: {"type": "string"}}}}}}
        assert _answer_key_names(node) == [f"items[].{field}"], field
    assert _published_answer_key_names({"distribution.fields[].correct", "performance.correct",
                                        "items[].answer_key", "frozen.version"}) == [
        "distribution.fields[].correct", "items[].answer_key"]

    # O publicado.
    doc = _published()
    for op in REPORT_OPERATIONS:
        paths = _paths(flatten(_success_schema(doc, op), doc))
        assert paths, op
        assert _published_answer_key_names(paths) == [], op
    assert _published_answer_key_names(_paths(flatten(_request_schema(doc, "offerings.update_digest"), doc))) == []


def test_untrusted_marks_exactly_on_contract_paths_and_no_new_identity_or_secret() -> None:
    """Nos 6 arquivos novos, todo `x-untrusted` vem com
    `x-free-text`, exatamente no título, no rótulo, nas alternativas e nos valores das listas do `Report`;
    só `x-free-text` no `qid`; nenhum `x-identity` nem `x-secret` nos arquivos nem nas 3 variantes; em toda a
    árvore de contratos, `x-identity` continua em 11 pontos e `x-secret` em 3.

    O publicado (vermelho no esqueleto): as mesmas marcas nas 5 respostas publicadas, e a
    mídia `text/csv` de `reports.get` com o esquema `{"type": "string", "x-free-text": true,
    "x-untrusted": true}`."""
    untrusted: dict[str, set[str]] = {}
    free_only: dict[str, set[str]] = {}
    for name in REPORT_SCHEMA_FILES:
        schema = _load(name)
        assert _untrusted_without_free_text(schema) == [], name
        flat = flatten(schema, schema)
        untrusted[name] = {path for path, marks in flat if "x-untrusted" in marks}
        free_only[name] = {path for path, marks in flat if marks == frozenset({"x-free-text"})}
        assert not any(mark.startswith("x-identity") or mark == "x-secret"
                       for _, marks in flat for mark in marks), name
        assert (_mark_points(schema, "x-identity"), _mark_points(schema, "x-secret")) == (0, 0), name
    assert untrusted == EXPECTED_UNTRUSTED
    assert free_only == EXPECTED_FREE_TEXT_ONLY
    variants = _event_variants()
    for event_type in REPORT_EVENT_TYPES:
        data = variants[event_type]["properties"]["data"]
        assert flatten(data, data) == {(p, frozenset()) for p in _paths(flatten(data, data))}, event_type

    files = [*sorted(SCHEMAS.glob("*.json")), CONTRACTS / "catalog.schema.json"]
    assert sum(_mark_points(load_json(str(p)), "x-identity") for p in files) == 11
    assert sum(_mark_points(load_json(str(p)), "x-secret") for p in files) == 3
    assert _mark_points({"a": {"x-identity": "name", "b": [{"x-identity": "ra"}]}}, "x-identity") == 2

    # O publicado.
    doc = _published()
    for op in REPORT_OPERATIONS:
        name = f"{op}.response.schema.json"
        flat = flatten(_success_schema(doc, op), doc)
        assert {path for path, marks in flat if "x-untrusted" in marks} == EXPECTED_UNTRUSTED[name], op
        assert all("x-free-text" in marks for _, marks in flat if "x-untrusted" in marks), op
        assert {path for path, marks in flat if marks == frozenset({"x-free-text"})} == (
            EXPECTED_FREE_TEXT_ONLY[name]), op
        assert not any(m.startswith("x-identity") or m == "x-secret" for _, marks in flat for m in marks), op
    content = _deref(_published_operation(doc, "reports.get")["responses"]["200"], doc).get("content", {})
    assert set(content) == {"application/json", "text/csv"}, sorted(content)
    assert _deref(content["text/csv"].get("schema", {}), doc) == REPORT_CSV_SCHEMA


def test_no_new_field_has_a_reserved_name() -> None:
    """Nenhum campo novo se chama `code` (fora de `offering.code`), `link`,
    `recipient`, `secret`, `until`, `username` ou `token_id`; contagem de destinatários é
    `recipient_count`: nos 6 arquivos novos e nos `data` das 3 variantes, nenhum campo tem esses nomes;
    `offering.code` existe (contra o vazio) nas 5 respostas e `recipient_count` em `notice.recorded`.
    Controle positivo: a checagem acusa cada nome proibido e um `code` fora de `offering`.

    O publicado (vermelho no esqueleto): nenhum campo proibido nas 5 respostas nem no
    pedido; `offering.code` existe."""
    for name in REPORT_SCHEMA_FILES:
        assert _forbidden_names(_load(name)) == [], name
    for name in REPORT_RESPONSE_FILES:
        assert ("offering.code", "code") in _property_names(_load(name)), name
    variants = _event_variants()
    for event_type in REPORT_EVENT_TYPES:
        assert _forbidden_names(variants[event_type]["properties"]["data"]) == [], event_type
    assert "recipient_count" in variants["notice.recorded"]["properties"]["data"]["properties"]
    for field in sorted(RESERVED_NAMES | {"code"}):
        synthetic = {"type": "object", "properties": {"frozen": {"type": "object", "properties": {
            field: {"type": "string"}}}}}
        assert _forbidden_names(synthetic) == [f"frozen.{field}"], field
    assert _forbidden_names({"type": "object", "properties": {"recipient_count": {"type": "integer"}}}) == []
    assert _published_forbidden({"offering.code", "items[].recipient", "dispatch.code", "recipient_count"}) == [
        "dispatch.code", "items[].recipient"]

    # O publicado.
    doc = _published()
    for op in REPORT_OPERATIONS:
        paths = _paths(flatten(_success_schema(doc, op), doc))
        assert "offering.code" in paths, op
        assert _published_forbidden(paths) == [], op
    assert _published_forbidden(_paths(flatten(_request_schema(doc, "offerings.update_digest"), doc))) == []


# ---------------------------------------------------------------------------------------------
# Eventos novos
# ---------------------------------------------------------------------------------------------


def test_report_event_variants_match_the_event_contract() -> None:
    """As 3 variantes novas de
    `events.list` têm o envelope da API base e o `data` fechado com exatamente estes campos e valores:
    `report.frozen {offering_id, dispatch_id, version ≥ 1, trigger}`, `notice.recorded`
    (os 10 tipos de aviso, o assunto sem chave interna, os campos anuláveis do assunto, os 6 estados finais,
    sem `pending`, e `recipient_count ≥ 0`), `offering.digest_changed {offering_id, enabled, local_time}`;
    sem texto livre nem identidade; cada conjunto de campos distinto dos demais tipos. As amostras
    válidas e inválidas são aceitas e recusadas.

    O publicado (vermelho no esqueleto): `EventType` tem os 3 tipos, e o `events.list` publicado (as
    variantes de `cq_server.api.schemas`) tem as 3 variantes com os mesmos campos e os mesmos valores."""
    schema = _load("events.list.response.schema.json")
    variants = _event_variants()
    assert set(REPORT_EVENT_TYPES) <= set(variants)
    for event_type, properties in EVENT_DATA.items():
        variant = variants[event_type]
        assert set(variant["properties"]) == {"seq", "type", "occurred_at", "actor_token_id", "data"}, event_type
        data = variant["properties"]["data"]
        assert (data["type"], data["additionalProperties"]) == ("object", False), event_type
        assert data["properties"] == properties, event_type
        assert data["required"] == list(properties), event_type
    keys = {event_type: frozenset(v["properties"]["data"]["properties"]) for event_type, v in variants.items()}
    for event_type in REPORT_EVENT_TYPES:
        others = {t for t, k in keys.items() if k == keys[event_type] and t != event_type}
        assert others == set(), (event_type, others)

    frozen = {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "version": 2, "trigger": "resend"}
    notice = {"offering_id": UUID_SAMPLE, "notice_type": "dispatch_closed", "dispatch_id": UUID_SAMPLE,
              "report_version": 1, "episode_seq": None, "batch_id": None, "window_started_at": None,
              "local_date": None, "state": "queued", "recipient_count": 2}
    digest = {"offering_id": UUID_SAMPLE, "enabled": False, "local_time": "23:59"}
    valid = [("report.frozen", frozen), ("report.frozen", {**frozen, "version": 1, "trigger": "closing"}),
             ("offering.digest_changed", digest), ("offering.digest_changed", {**digest, "enabled": True})]
    valid += [("notice.recorded", {**notice, "notice_type": kind}) for kind in NOTICE_TYPES]
    valid += [("notice.recorded", {**notice, "state": state, "recipient_count": 0}) for state in NOTICE_STATES]
    valid += [
        ("notice.recorded", {**notice, "notice_type": "daily_digest", "dispatch_id": None, "report_version": None,
                             "local_date": "2031-01-01", "state": "no_activity", "recipient_count": 0}),
        ("notice.recorded", {**notice, "notice_type": "bot_access_restored", "dispatch_id": None,
                             "report_version": None, "episode_seq": 17}),
        ("notice.recorded", {**notice, "notice_type": "invite_failures", "dispatch_id": None,
                             "report_version": None, "batch_id": UUID_SAMPLE}),
        ("notice.recorded", {**notice, "notice_type": "invite_failures", "dispatch_id": None,
                             "report_version": None, "window_started_at": "2031-01-01T10:00:00Z"}),
    ]
    invalid = [
        ("report.frozen", {**frozen, "version": 0}),
        ("report.frozen", {**frozen, "trigger": "archive"}),
        ("report.frozen", {**frozen, "content": {}}),
        ("notice.recorded", {**notice, "state": "pending"}),
        ("notice.recorded", {**notice, "notice_type": "summary"}),
        ("notice.recorded", {**notice, "recipient_count": -1}),
        ("notice.recorded", {**notice, "local_date": "2031-1-1"}),
        ("notice.recorded", {**notice, "window_started_at": "2031-01-01T10:00:00+00:00"}),
        ("notice.recorded", {**notice, "episode_seq": 0}),
        ("notice.recorded", {**notice, "subject_key": "report:1"}),
        ("notice.recorded", {**notice, "notice_id": 1}),
        ("notice.recorded", {**notice, "recipients": [UUID_SAMPLE]}),
        ("notice.recorded", {k: v for k, v in notice.items() if k != "batch_id"}),
        ("offering.digest_changed", {**digest, "local_time": "24:00"}),
        ("offering.digest_changed", {**digest, "enabled": "true"}),
        ("offering.digest_changed", {**digest, "offering_id": "mc102-2031s1"}),
    ]
    for event_type, data in valid:
        assert _is_valid(schema, _event_page(event_type, data)), (event_type, data)
    for event_type, data in invalid:
        assert not _is_valid(schema, _event_page(event_type, data)), (event_type, data)

    # O publicado: `EventType` e as variantes publicadas no `events.list`.
    from cq_server.events import EventType

    assert sorted(set(REPORT_EVENT_TYPES) - {e.value for e in EventType}) == [], "tipos novos ausentes de EventType"
    doc = _published()
    published = _published_event_variants(doc)
    assert sorted(set(REPORT_EVENT_TYPES) - set(published)) == [], "variantes novas ausentes do events.list publicado"
    for event_type, properties in EVENT_DATA.items():
        assert _data_properties(published[event_type], doc) == set(properties), event_type
    page = _success_schema(doc, "events.list")
    assert _enum_at(doc, page, "items[].data.trigger") >= set(TRIGGERS)
    assert _enum_at(doc, page, "items[].data.notice_type") == set(NOTICE_TYPES)
    assert _enum_at(doc, page, "items[].data.state") == set(NOTICE_STATES)


# ---------------------------------------------------------------------------------------------
# Catálogo, registro e erros
# ---------------------------------------------------------------------------------------------


def test_report_operations_registered_published_and_exposed_without_confirmation() -> None:
    """As 5 operações novas estão no catálogo com as
    ferramentas do contrato, expostas ao MCP com `confirm: false` e não destrutivas, sem permissão nova
    (`offerings:read` nas leituras, `offerings:write` no reenvio e na alteração); só
    `offerings.update_digest` recebe corpo (o reenvio é sem corpo).

    A implementação (vermelho no esqueleto): as 5 operações no registro
    (`cq_server.api.operations.BY_ID`) com o método, o caminho e a permissão do catálogo e modelo de pedido só
    na alteração do resumo diário; no OpenAPI publicado, com o `operationId`, corpo só na alteração e o 200 de
    `reports.get` com as duas mídias."""
    operations = {op["id"]: op for op in load_catalog()["operations"]}
    assert set(REPORT_OPERATIONS) <= set(operations)
    tools = {op_id: operations[op_id]["mcp"] for op_id in REPORT_OPERATIONS}
    assert tools == {
        "reports.get": {"exposed": True, "tool": "get_dispatch_report", "confirm": False},
        "reports.list_versions": {"exposed": True, "tool": "list_report_versions", "confirm": False},
        "reports.resend_summary": {"exposed": True, "tool": "resend_report_summary", "confirm": False},
        "offerings.get_digest": {"exposed": True, "tool": "get_offering_digest", "confirm": False},
        "offerings.update_digest": {"exposed": True, "tool": "update_offering_digest", "confirm": False},
    }
    assert [op_id for op_id in REPORT_OPERATIONS if operations[op_id]["destructive"]] == []
    assert {op_id: operations[op_id]["permission"] for op_id in REPORT_OPERATIONS} == {
        "reports.get": "offerings:read", "reports.list_versions": "offerings:read",
        "reports.resend_summary": "offerings:write", "offerings.get_digest": "offerings:read",
        "offerings.update_digest": "offerings:write"}

    # A implementação.
    from cq_server.api.operations import BY_ID

    assert [op_id for op_id in REPORT_OPERATIONS if op_id not in BY_ID] == [], "operações novas fora do registro"
    doc = _published()
    for op_id in REPORT_OPERATIONS:
        entry, op = operations[op_id], BY_ID[op_id]
        assert (op.method, op.path, op.permission.value if op.permission else None) == (
            entry["method"], entry["path"], entry["permission"]), op_id
        assert (op.request_model is not None) is (op_id == "offerings.update_digest"), op_id
        published = _published_operation(doc, op_id)
        assert published.get("operationId") == op_id
        assert ("requestBody" in published) is (op_id == "offerings.update_digest"), op_id
        success = [status for status in published.get("responses", {}) if status.startswith("2")]
        assert success == ["200"], (op_id, success)
    content = _deref(_published_operation(doc, "reports.get")["responses"]["200"], doc).get("content", {})
    assert set(content) == {"application/json", "text/csv"}, sorted(content)


def test_report_error_responses_use_only_existing_codes() -> None:
    """No contrato, nenhum código novo (`CONTRACT_ERRORS` continua com 19): o envelope de erro
    congelado tem os mesmos 19 códigos, e as 5 operações novas usam só eles: `not_found` e
    `invalid_dispatch_state` nas leituras do relatório, mais `invalid_state` no reenvio (oferecimento
    arquivado), `not_found` na leitura do resumo diário e `not_found` e `invalid_state` na alteração, além dos
    4 genéricos da API base.

    A implementação (vermelho no esqueleto): os erros de cada operação no registro
    (`Operation.errors`) são exatamente esses, nenhum fora de `cq_server.api.errors`, e as descrições de erro
    publicadas por operação citam exatamente esses códigos, com a mensagem do contrato."""
    schema = _load("error.schema.json")
    codes = set(schema["properties"]["error"]["properties"]["code"]["enum"])
    assert codes == set(MESSAGES)
    assert len(codes) == 21  # o CLI soma purge_pending e purge_changed
    for op_id, expected in EXPECTED_ERRORS.items():
        assert expected <= codes, op_id

    # A implementação.
    from cq_server.api import errors
    from cq_server.api.operations import BY_ID

    assert set(errors.MESSAGES) == codes
    assert [op_id for op_id in REPORT_OPERATIONS if op_id not in BY_ID] == [], "operações novas fora do registro"
    doc = _published()
    pattern = re.compile(r"`([a-z_]+)`: ")
    for op_id, expected in EXPECTED_ERRORS.items():
        assert set(BY_ID[op_id].errors) == expected, op_id
        responses = _published_operation(doc, op_id).get("responses", {})
        cited: set[str] = set()
        for status, response in responses.items():
            if status.startswith(("4", "5")):
                description = _deref(response, doc).get("description", "")
                cited |= set(pattern.findall(description))
                for code in pattern.findall(description):
                    assert f"`{code}`: {MESSAGES[code]}" in description, (op_id, code)
        assert cited == expected, (op_id, sorted(cited))
