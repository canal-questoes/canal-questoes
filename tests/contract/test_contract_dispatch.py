"""Contratos do disparo e da resposta campo a campo: os arquivos congelados, o OpenAPI publicado e os
registros da implementação.

Confere os arquivos de contrato congelados do disparo (`contracts/api/v1/`), que existem desde
o esqueleto, **e** que a implementação bate com eles (para que
nenhum destes testes passe no esqueleto): o OpenAPI que a app publica (`create_app(...).openapi()`, sem
banco) e o registro de operações (`cq_server.api.operations.BY_ID`) e de erros (`cq_server.api.errors`)
das 6 operações do disparo. As conferências do disparo nos outros testes de contrato ficam nos próprios arquivos
(`test_catalog.py`, `test_contract_files.py`, `test_openapi.py`); as
demais conferências do disparo ficam aqui:
- a forma exata do `Dispatch`: `state` nos 6 estados, `origin` nas 4 origens, `rejection`
  com `reason` nas 9 classes e `message` pareada por `anyOf` de `const` (os textos exatos do contrato),
  `cancel_reason` nas 5 razões, as 9 contagens de `deliveries`, a prévia com `fields[] = {type, label,
  suffix, hint}` (os 5 tipos da IR) e `warnings[] = {kind, message}` (os 6 avisos), sem gabarito; o
  `DispatchSummary` da lista (sem `offering`, `timezone` e `preview`); `messages.list`; o pedido de preparo
  propriedade a propriedade, com o `qid` no padrão do contrato do motor (`contracts/engine/v1/`)
  e os horários com deslocamento explícito;
- toda `x-untrusted` vem com `x-free-text`, em exatamente os caminhos do contrato; `x-free-text`
  sem `x-untrusted` só em `question.repo` e `question.qid`; nenhum `x-identity` nem `x-secret`;
- nenhum campo chamado `code` fora de `offering.code`, nem `link`, `recipient`, `secret`, `until`,
  `username` ou `token_id`, nas respostas, no pedido e nos 13 eventos do disparo;
- os 3 ramos do disparo no envelope de erro sem `fields`, `state` nem `permission`;
- `messages.list` fora do MCP, e `dispatches.confirm` a única operação do disparo com confirmação e
  destrutiva.
Cada checagem estrutural tem controle positivo sintético (contra o vazio). Em cada função, depois da
conferência dos arquivos (que já vale no esqueleto), a mesma regra no publicado: caminhos e valores
enumerados do `Dispatch`, do `DispatchSummary` e do pedido de preparo; marcas `x-untrusted`; nomes
proibidos; os 3 códigos de erro no `cq_server.api.errors` e nas descrições de erro publicadas por
operação; as 6 operações no registro e no OpenAPI, com o método, o caminho, a permissão e o corpo do
catálogo.

Falha esperada no esqueleto: as rotas das 6 operações não existem no OpenAPI nem no registro
e os 3 códigos novos não existem em `cq_server.api.errors`
(contrato não cumprido até a implementação).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from jsonschema import Draft202012Validator

from tests.helpers.contract import CONTRACTS, REPO, SCHEMAS, flatten, load_catalog, load_json

# padrões do contrato, da API base ao disparo.
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
OFFSET_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
ALIAS_PATTERN = r"^(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}$"
CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{2,39}$"
COMMIT_PATTERN = r"^([0-9a-f]{40}|[0-9a-f]{64})$"

DISPATCH_OPERATIONS = ("dispatches.prepare", "dispatches.get", "dispatches.list", "dispatches.confirm",
                 "dispatches.cancel", "messages.list")
FULL_DISPATCH_OPS = ("dispatches.prepare", "dispatches.get", "dispatches.confirm", "dispatches.cancel")
DISPATCH_SCHEMA_FILES = ("dispatches.prepare.request.schema.json",
                   *(f"{op}.response.schema.json" for op in DISPATCH_OPERATIONS))
DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                  "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
                  "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                  "submission.graded", "submission.internal_failure")

# na ordem do contrato.
DISPATCH_STATES = ["validating", "rejected", "scheduled", "open", "closed", "cancelled"]
ORIGINS = ["cli", "api", "mcp", "integration"]
REJECTIONS = {
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
CANCEL_REASONS = ["operator", "expired", "offering_closed", "window_missed", "validation_overran"]
FIELD_TYPES = ["number", "integer", "text", "single_choice", "multiple_choice"]
# `no_teacher_contact` (o aviso de falta de contato do professor) é o 7º aviso, por último.
WARNING_KINDS = ["console_output", "missing_media", "media_error", "empty_target", "too_many_options",
                 "repeated_question", "no_teacher_contact"]
DELIVERY_COUNTS = ["pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                   "answered_late", "question_failed", "grading_pending"]

_UUID = {"type": "string", "pattern": UUID4_PATTERN}
_TIME = {"type": "string", "pattern": TIME_PATTERN}
_NULLABLE_TIME = {"type": ["string", "null"], "pattern": TIME_PATTERN}
_UNTRUSTED = {"x-free-text": True, "x-untrusted": True}

OFFERING_REF = {
    "type": "object", "additionalProperties": False, "required": ["offering_id", "code"],
    "properties": {"offering_id": _UUID, "code": {"type": "string", "pattern": CODE_PATTERN}},
}
QUESTION = {
    "type": "object", "additionalProperties": False, "required": ["question_ref_id", "repo", "commit", "qid"],
    "properties": {
        "question_ref_id": _UUID,
        "repo": {"type": "string", "minLength": 1, "x-free-text": True},
        "commit": {"type": "string", "pattern": COMMIT_PATTERN},
        "qid": {"type": "string", "minLength": 1, "x-free-text": True},
    },
}
REJECTION = {
    "type": ["object", "null"], "additionalProperties": False, "required": ["reason", "message", "details"],
    "properties": {
        "reason": {"enum": list(REJECTIONS)},
        "message": {"enum": list(REJECTIONS.values())},
        "details": {"type": "array", "items": {"type": "string", **_UNTRUSTED}},
    },
    "anyOf": [{"properties": {"reason": {"const": reason}, "message": {"const": message}}}
              for reason, message in REJECTIONS.items()],
}
DELIVERIES = {
    "type": "object", "additionalProperties": False, "required": DELIVERY_COUNTS,
    "properties": {name: {"type": "integer", "minimum": 0} for name in DELIVERY_COUNTS},
}
PREVIEW = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["title", "statement", "fields", "target_count", "warnings"],
    "properties": {
        "title": {"type": ["string", "null"], **_UNTRUSTED},
        "statement": {"type": "string", **_UNTRUSTED},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "label", "suffix", "hint"],
            "properties": {
                "type": {"enum": FIELD_TYPES},
                "label": {"type": ["string", "null"], **_UNTRUSTED},
                "suffix": {"type": ["string", "null"], **_UNTRUSTED},
                "hint": {"type": "string", **_UNTRUSTED},
            }}},
        "target_count": {"type": "integer", "minimum": 0},
        "warnings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "message"],
            "properties": {"kind": {"enum": WARNING_KINDS}, "message": {"type": "string", **_UNTRUSTED}}}},
    },
}
# O `DispatchSummary`: o `Dispatch` sem `offering`, `timezone` e `preview`.
SUMMARY_PROPERTIES: dict[str, Any] = {
    "dispatch_id": _UUID,
    "question": QUESTION,
    "state": {"enum": DISPATCH_STATES},
    "confirmed": {"type": "boolean"},
    "origin": {"enum": ORIGINS},
    "opens_at": _NULLABLE_TIME,
    "closes_at": _NULLABLE_TIME,
    "created_at": _TIME,
    "validated_at": _NULLABLE_TIME,
    "confirmed_at": _NULLABLE_TIME,
    "rejected_at": _NULLABLE_TIME,
    "opened_at": _NULLABLE_TIME,
    "closed_at": _NULLABLE_TIME,
    "cancelled_at": _NULLABLE_TIME,
    "rejection": REJECTION,
    "cancel_reason": {"type": ["string", "null"], "enum": [*CANCEL_REASONS, None]},
    "target_count": {"type": ["integer", "null"], "minimum": 0},
    "deliveries": DELIVERIES,
}
DISPATCH_PROPERTIES: dict[str, Any] = {
    "offering": OFFERING_REF,
    "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
    **SUMMARY_PROPERTIES,
    "preview": PREVIEW,
}
MESSAGES_LIST_PROPERTIES: dict[str, Any] = {
    "offering": OFFERING_REF,
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["pseudo_id", "alias", "text", "created_at"],
        "properties": {
            "pseudo_id": _UUID,
            "alias": {"type": "string", "pattern": ALIAS_PATTERN},
            "text": {"type": "string", **_UNTRUSTED},
            "created_at": _TIME,
        }}},
    "next_cursor": {"type": "integer", "minimum": 0},
}

# os caminhos com `x-untrusted` (sempre com `x-free-text`) e os com só
# `x-free-text`, por arquivo, na forma achatada.
_DISPATCH_UNTRUSTED = {"rejection.details[]", "preview.title", "preview.statement", "preview.fields[].label",
                       "preview.fields[].suffix", "preview.fields[].hint", "preview.warnings[].message"}
EXPECTED_UNTRUSTED: dict[str, set[str]] = {
    **{f"{op}.response.schema.json": _DISPATCH_UNTRUSTED for op in FULL_DISPATCH_OPS},
    "dispatches.list.response.schema.json": {"items[].rejection.details[]"},
    "messages.list.response.schema.json": {"items[].text"},
    "dispatches.prepare.request.schema.json": set(),
    # Relatórios:
    # o título e os textos da distribuição do Report, os mesmos 6
    # caminhos de `_REPORT_UNTRUSTED` do test_openapi.py, aqui por extenso.
    "reports.get.response.schema.json": {
        "dispatch.title",
        "distribution.fields[].label",
        "distribution.fields[].choices[].text",
        "distribution.fields[].values.items[].value",
        "distribution.fields[].ratios.items[].value",
        "distribution.fields[].texts.items[].value",
    },
}
EXPECTED_FREE_TEXT_ONLY: dict[str, set[str]] = {
    **{f"{op}.response.schema.json": {"question.repo", "question.qid"} for op in FULL_DISPATCH_OPS},
    "dispatches.list.response.schema.json": {"items[].question.repo", "items[].question.qid"},
    "messages.list.response.schema.json": set(),
    "dispatches.prepare.request.schema.json": set(),
}
# nomes que nenhum campo novo pode ter (e `code`, só em `offering.code`).
RESERVED_NAMES = {"link", "recipient", "secret", "until", "username", "token_id"}


def _load(name: str) -> Any:
    return load_json(str(SCHEMAS / name))


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def _nodes(node: Any, path: str = "") -> Iterator[tuple[str, dict]]:
    """(caminho achatado, subesquema) de todo subesquema de propriedade ou item, atravessando os
    combinadores (o caminho de um ramo é o do pai)."""
    if not isinstance(node, dict):
        return
    if path:
        yield path, node
    for key in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(key, []) or []:
            yield from _nodes(branch, path)
    for name, child in (node.get("properties") or {}).items():
        yield from _nodes(child, f"{path}.{name}" if path else name)
    if isinstance(node.get("items"), dict):
        yield from _nodes(node["items"], f"{path}[]")


def _untrusted_without_free_text(schema: dict) -> list[str]:
    """Caminhos com `x-untrusted` sem `x-free-text: true`, ou com `x-untrusted` diferente de `true`."""
    return sorted({path for path, node in _nodes(schema) if "x-untrusted" in node
                   and (node["x-untrusted"] is not True or node.get("x-free-text") is not True)})


def _property_names(schema: dict) -> set[tuple[str, str]]:
    """(caminho, nome) de toda propriedade declarada."""
    return {(path, path.rsplit(".", 1)[-1]) for path, _ in _nodes(schema) if not path.endswith("[]")}


def _forbidden_names(schema: dict) -> list[str]:
    """Campos chamados `code` fora de `offering.code` e campos com um nome de `RESERVED_NAMES`."""
    return sorted(path for path, name in _property_names(schema)
                  if (name == "code" and path != "offering.code") or name in RESERVED_NAMES)


def _event_variants() -> dict[str, dict]:
    schema = _load("events.list.response.schema.json")
    return {v["properties"]["type"]["const"]: v for v in schema["properties"]["items"]["items"]["oneOf"]}


# --- o publicado pela implementação ------------------------------------------------------------


def _published() -> dict:
    """O OpenAPI que a app publica (`create_app`; montar o documento não abre conexão com o banco)."""
    from cq_server.api import create_app
    from cq_server.config import Settings

    return create_app(Settings(database_url="postgresql://cq-contrato.invalid/cq")).openapi()


def _deref(node: Any, doc: dict) -> Any:
    for _ in range(50):
        if not (isinstance(node, dict) and "$ref" in node):
            return node
        target: Any = doc
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    raise AssertionError("ciclo de $ref no OpenAPI publicado")


def _published_operation(doc: dict, op_id: str) -> dict:
    """A operação publicada no (método, caminho) do catálogo."""
    entry = next(op for op in load_catalog()["operations"] if op["id"] == op_id)
    item = _deref(doc.get("paths", {}).get(entry["path"], {}), doc)
    operation = item.get(entry["method"].lower())
    assert operation is not None, f"{op_id}: {entry['method']} {entry['path']} ausente do OpenAPI publicado"
    return operation


def _success_schema(doc: dict, op_id: str) -> dict:
    responses = _published_operation(doc, op_id).get("responses", {})
    success = [status for status in responses if status.startswith("2")]
    assert len(success) == 1, (op_id, sorted(responses))
    return _deref(responses[success[0]], doc)["content"]["application/json"]["schema"]


def _request_schema(doc: dict, op_id: str) -> dict:
    operation = _published_operation(doc, op_id)
    assert "requestBody" in operation, f"{op_id} sem requestBody no OpenAPI publicado"
    return _deref(operation["requestBody"], doc)["content"]["application/json"]["schema"]


def _expand(nodes: list[Any], doc: dict) -> list[dict]:
    """Os nós, resolvidos, com os ramos dos combinadores."""
    out: list[dict] = []
    pending = list(nodes)
    while pending:
        node = _deref(pending.pop(0), doc)
        if not isinstance(node, dict):
            continue
        out.append(node)
        for key in ("allOf", "anyOf", "oneOf"):
            pending.extend(node.get(key, []) or [])
    return out


def _enum_at(doc: dict, schema: dict, path: str) -> set[Any]:
    """Os valores enumerados (`enum`/`const`, sem o nulo) do caminho achatado `path` no esquema publicado."""
    nodes: list[Any] = [schema]
    for part in path.split("."):
        name = part[:-2] if part.endswith("[]") else part
        nodes = [n["properties"][name] for n in _expand(nodes, doc) if name in n.get("properties", {})]
        if part.endswith("[]"):
            nodes = [n["items"] for n in _expand(nodes, doc) if isinstance(n.get("items"), dict)]
    values: set[Any] = set()
    for node in _expand(nodes, doc):
        values |= set(node.get("enum") or [])
        if "const" in node:
            values.add(node["const"])
    values.discard(None)
    return values


def _paths(flat: set[tuple[str, frozenset[str]]]) -> set[str]:
    return {path for path, _ in flat}


def _published_forbidden(paths: set[str]) -> list[str]:
    """Os caminhos publicados chamados `code` fora de `offering.code` ou com um nome de `RESERVED_NAMES`."""
    found = []
    for path in paths:
        name = re.sub(r"\[\]", "", path).rsplit(".", 1)[-1]
        if (name == "code" and path != "offering.code") or name in RESERVED_NAMES:
            found.append(path)
    return sorted(found)


def _file_op(name: str) -> tuple[str, str]:
    """`<op>.<request|response>.schema.json` → (op, tipo)."""
    op, kind = name.removesuffix(".schema.json").rsplit(".", 1)
    return op, kind


# ---------------------------------------------------------------------------------------------
# Forma exata dos esquemas novos
# ---------------------------------------------------------------------------------------------


def test_dispatch_states_and_fields_match_contract() -> None:
    """Disparo e resposta, campo a campo:
    as respostas de `dispatches.prepare`, `.get`, `.confirm` e `.cancel` são o `Dispatch` completo
    com exatamente as propriedades do contrato (estado, origem, rejeição com `reason` e `message`
    pareados pelos textos exatos, razão do cancelamento, contagens, prévia sem gabarito nem nome do campo
    do PL), todas `required`; a de `dispatches.list` é o `DispatchSummary` (sem `offering`, `timezone` e
    `preview`) com `next_cursor`; `messages.list` é `{offering, items: [{pseudo_id, alias, text,
    created_at}], next_cursor}`; o pedido de preparo é plano e fechado, com o `qid` no padrão do
    `question_ref.qid` do contrato do motor e os horários com deslocamento explícito; as formas válidas e
    inválidas do contrato são aceitas e recusadas."""
    for op in FULL_DISPATCH_OPS:
        schema = _load(f"{op}.response.schema.json")
        assert (schema["type"], schema["additionalProperties"]) == ("object", False), op
        assert schema["properties"] == DISPATCH_PROPERTIES, op
        assert set(schema["required"]) == set(DISPATCH_PROPERTIES), op

    listing = _load("dispatches.list.response.schema.json")
    assert (listing["type"], listing["additionalProperties"]) == ("object", False)
    assert set(listing["required"]) == set(listing["properties"]) == {"offering", "timezone", "items",
                                                                      "next_cursor"}
    assert listing["properties"]["offering"] == OFFERING_REF
    assert listing["properties"]["timezone"] == DISPATCH_PROPERTIES["timezone"]
    assert listing["properties"]["next_cursor"] == {"type": "integer", "minimum": 0}
    items = listing["properties"]["items"]
    assert items["type"] == "array"
    summary = items["items"]
    assert (summary["type"], summary["additionalProperties"]) == ("object", False)
    assert summary["properties"] == SUMMARY_PROPERTIES
    assert set(summary["required"]) == set(SUMMARY_PROPERTIES)
    assert set(DISPATCH_PROPERTIES) - set(SUMMARY_PROPERTIES) == {"offering", "timezone", "preview"}

    messages = _load("messages.list.response.schema.json")
    assert (messages["type"], messages["additionalProperties"]) == ("object", False)
    assert messages["properties"] == MESSAGES_LIST_PROPERTIES
    assert set(messages["required"]) == set(MESSAGES_LIST_PROPERTIES)

    engine = load_json(str(REPO / "contracts" / "engine" / "v1" / "engine-api.schema.json"))
    engine_qid = engine["$defs"]["question_ref"]["properties"]["qid"]
    request = _load("dispatches.prepare.request.schema.json")
    assert (request["type"], request["additionalProperties"]) == ("object", False)
    assert request["required"] == ["repo", "commit", "qid"]
    assert request["properties"] == {
        "repo": {"type": "string", "minLength": 1, "maxLength": 2048},
        "commit": {"type": "string", "pattern": COMMIT_PATTERN},
        "qid": {"type": "string", "minLength": 1, "maxLength": 512, "pattern": engine_qid["pattern"]},
        "opens_at": {"type": "string", "pattern": OFFSET_TIME_PATTERN},
        "closes_at": {"type": "string", "pattern": OFFSET_TIME_PATTERN},
        "origin": {"enum": ORIGINS},
    }
    assert (engine_qid["minLength"], engine_qid["maxLength"]) == (1, 512)

    base = {"repo": "https://repos.cq.test/curso-neutro.git", "commit": "a" * 40, "qid": "questoes/q1"}
    for extra in ({}, {"commit": "0" * 64}, {"opens_at": "2031-01-01T10:00:00-03:00"},
                  {"closes_at": "2031-01-01T13:00:00Z"}, {"opens_at": "2031-01-01T10:00:00.5+05:30"},
                  {"qid": "q" * 512}, {"repo": "r" * 2048},
                  *({"origin": origin} for origin in ORIGINS)):
        assert _is_valid(request, {**base, **extra}), extra
    for label, body in (
        ("sem repo", {k: v for k, v in base.items() if k != "repo"}),
        ("sem commit", {k: v for k, v in base.items() if k != "commit"}),
        ("sem qid", {k: v for k, v in base.items() if k != "qid"}),
        ("repo vazio", {**base, "repo": ""}),
        ("repo 2049", {**base, "repo": "r" * 2049}),
        ("commit maiúsculo", {**base, "commit": "A" * 40}),
        ("commit 39", {**base, "commit": "a" * 39}),
        ("commit 41", {**base, "commit": "a" * 41}),
        ("qid com ..", {**base, "qid": "../q1"}),
        ("qid com .", {**base, "qid": "questoes/./q1"}),
        ("qid 513", {**base, "qid": "q" * 513}),
        ("qid vazio", {**base, "qid": ""}),
        ("abertura sem fuso", {**base, "opens_at": "2031-01-01T10:00:00"}),
        ("fechamento só data", {**base, "closes_at": "2031-01-01"}),
        ("abertura nula", {**base, "opens_at": None}),
        ("origem desconhecida", {**base, "origin": "web"}),
        ("campo a mais", {**base, "body_key": "x"}),
        ("objeto aninhado", {"question": base}),
    ):
        assert not _is_valid(request, body), label

    rejection = DISPATCH_PROPERTIES["rejection"]
    assert _is_valid(rejection, None)
    for reason, message in REJECTIONS.items():
        assert _is_valid(rejection, {"reason": reason, "message": message, "details": []}), reason
        assert _is_valid(rejection, {"reason": reason, "message": message, "details": ["motivo"]}), reason
        for other in REJECTIONS.values():
            if other != message:
                assert not _is_valid(rejection, {"reason": reason, "message": other, "details": []}), reason
        assert not _is_valid(rejection, {"reason": reason, "message": message}), reason
        assert not _is_valid(rejection, {"reason": reason, "message": message, "details": [],
                                         "technical_detail": "x"}), reason
    schema = _load("dispatches.get.response.schema.json")
    assert schema["properties"]["rejection"] == rejection
    assert not _is_valid(rejection, {"reason": "repo_unreachable", "message": REJECTIONS["repository_unreachable"],
                                     "details": []})

    # O publicado: as mesmas propriedades (forma achatada) e os mesmos valores
    # enumerados do `Dispatch`, do `DispatchSummary` e do pedido de preparo.
    doc = _published()
    for op in (*FULL_DISPATCH_OPS, "dispatches.list", "messages.list"):
        frozen = _load(f"{op}.response.schema.json")
        assert _paths(flatten(_success_schema(doc, op), doc)) == _paths(flatten(frozen, frozen)), op
    for op in FULL_DISPATCH_OPS:
        published = _success_schema(doc, op)
        assert _enum_at(doc, published, "state") == set(DISPATCH_STATES), op
        assert _enum_at(doc, published, "origin") == set(ORIGINS), op
        assert _enum_at(doc, published, "rejection.reason") == set(REJECTIONS), op
        assert _enum_at(doc, published, "cancel_reason") == set(CANCEL_REASONS), op
        assert _enum_at(doc, published, "preview.fields[].type") == set(FIELD_TYPES), op
        assert _enum_at(doc, published, "preview.warnings[].kind") == set(WARNING_KINDS), op
    listing = _success_schema(doc, "dispatches.list")
    assert _enum_at(doc, listing, "items[].state") == set(DISPATCH_STATES)
    published_request = _request_schema(doc, "dispatches.prepare")
    assert _paths(flatten(published_request, doc)) == set(request["properties"])
    assert _enum_at(doc, published_request, "origin") == set(ORIGINS)


# ---------------------------------------------------------------------------------------------
# Marcas: `x-untrusted` com `x-free-text`; nada de identidade nem segredo novo
# ---------------------------------------------------------------------------------------------


def test_every_untrusted_mark_comes_with_free_text() -> None:
    """`x-untrusted: true` sempre junto de `x-free-text: true`: em
    todo arquivo de `contracts/api/v1/`, todo subesquema com `x-untrusted` tem `x-untrusted: true` e
    `x-free-text: true`; `x-untrusted` aparece exatamente nos caminhos do conteúdo de terceiros da prévia
    e dos motivos e no texto de `messages.list`, e em nenhum arquivo anterior nem no pedido; nos
    arquivos do disparo, `x-free-text` sem `x-untrusted` só em `question.repo` e `question.qid`; nenhum
    `x-identity` nem `x-secret` nos arquivos do disparo. Controle positivo: a checagem acusa `x-untrusted` sem
    `x-free-text`, com `x-free-text: false` e com `x-untrusted` diferente de `true`."""
    files = sorted(p.name for p in SCHEMAS.glob("*.json"))
    assert set(DISPATCH_SCHEMA_FILES) <= set(files)
    offenders = {name: bad for name in files if (bad := _untrusted_without_free_text(_load(name)))}
    assert offenders == {}
    catalog_schema = load_json(str(CONTRACTS / "catalog.schema.json"))
    assert _untrusted_without_free_text(catalog_schema) == []

    untrusted: dict[str, set[str]] = {}
    free_only: dict[str, set[str]] = {}
    for name in files:
        flat = flatten(_load(name), _load(name))
        marked = {path for path, marks in flat if "x-untrusted" in marks}
        if marked or name in DISPATCH_SCHEMA_FILES:
            untrusted[name] = marked
        if name in DISPATCH_SCHEMA_FILES:
            free_only[name] = {path for path, marks in flat if marks == frozenset({"x-free-text"})}
            assert not any(mark.startswith("x-identity") or mark == "x-secret"
                           for _, marks in flat for mark in marks), name
    assert untrusted == EXPECTED_UNTRUSTED
    assert free_only == EXPECTED_FREE_TEXT_ONLY

    for node, expected in (
        ({"type": "object", "properties": {"a": {"type": "string", "x-untrusted": True}}}, ["a"]),
        ({"type": "object", "properties": {"a": {"type": "string", "x-untrusted": True,
                                                 "x-free-text": False}}}, ["a"]),
        ({"type": "object", "properties": {"a": {"type": "array", "items": {
            "type": "string", "x-untrusted": "sim", "x-free-text": True}}}}, ["a[]"]),
        ({"type": "object", "properties": {"a": {"anyOf": [{"type": "string", "x-untrusted": True}]}}}, ["a"]),
        ({"type": "object", "properties": {"a": {"type": "string", **_UNTRUSTED}}}, []),
    ):
        assert _untrusted_without_free_text(node) == expected, node

    # O publicado (respostas): `x-untrusted` (sempre com `x-free-text`) exatamente nos mesmos caminhos,
    # e só `x-free-text` em `question.repo` e `question.qid`.
    doc = _published()
    for name in DISPATCH_SCHEMA_FILES:
        op, kind = _file_op(name)
        if kind != "response":
            continue  # as marcas de pedido publicadas não são comparadas
        flat = flatten(_success_schema(doc, op), doc)
        assert {path for path, marks in flat if "x-untrusted" in marks} == EXPECTED_UNTRUSTED[name], name
        assert all("x-free-text" in marks for _, marks in flat if "x-untrusted" in marks), name
        assert {path for path, marks in flat if marks == frozenset({"x-free-text"})} == (
            EXPECTED_FREE_TEXT_ONLY[name]), name


# ---------------------------------------------------------------------------------------------
# Nomes proibidos nos campos novos
# ---------------------------------------------------------------------------------------------


def test_no_new_field_is_named_code_or_a_reserved_name() -> None:
    """Nenhum campo novo se chama `code` (fora de `offering.code`),
    `link`, `recipient`, `secret`, `until`, `username` ou `token_id`: nos 7 arquivos novos e nos
    `data` dos 13 tipos de evento novos, nenhum campo tem esses nomes; `offering.code` existe (contra o
    vazio) nas respostas com `OfferingRef`. Controle positivo: a checagem acusa cada nome proibido e um
    `code` fora de `offering`."""
    for name in DISPATCH_SCHEMA_FILES:
        assert _forbidden_names(_load(name)) == [], name
    for op in (*FULL_DISPATCH_OPS, "dispatches.list", "messages.list"):
        assert ("offering.code", "code") in _property_names(_load(f"{op}.response.schema.json")), op
    variants = _event_variants()
    assert set(DISPATCH_EVENT_TYPES) <= set(variants)
    for event_type in DISPATCH_EVENT_TYPES:
        assert _forbidden_names(variants[event_type]["properties"]["data"]) == [], event_type

    for field in sorted(RESERVED_NAMES | {"code"}):
        synthetic = {"type": "object", "properties": {"question": {"type": "object", "properties": {
            field: {"type": "string"}}}}}
        assert _forbidden_names(synthetic) == [f"question.{field}"], field
    assert _forbidden_names({"type": "object", "properties": {"offering": {"type": "object", "properties": {
        "code": {"type": "string"}}}}}) == []
    assert _forbidden_names({"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"username": {"type": "string"}}}}}}) == ["items[].username"]

    # O publicado: nenhum campo proibido nas respostas novas nem no pedido de preparo; `offering.code`
    # existe (contra o vazio).
    doc = _published()
    assert _published_forbidden({"question.link", "items[].username", "offering.code", "x.code"}) == [
        "items[].username", "question.link", "x.code"]
    for op in (*FULL_DISPATCH_OPS, "dispatches.list", "messages.list"):
        paths = _paths(flatten(_success_schema(doc, op), doc))
        assert "offering.code" in paths, op
        assert _published_forbidden(paths) == [], op
    assert _published_forbidden(_paths(flatten(_request_schema(doc, "dispatches.prepare"), doc))) == []


# ---------------------------------------------------------------------------------------------
# Envelope de erro e catálogo
# ---------------------------------------------------------------------------------------------


def test_dispatch_error_branches_have_no_fields_state_or_permission() -> None:
    """Mesmo envelope da API base, **sem** `fields`, `state` nem
    `permission` nos três: cada código novo tem exatamente um ramo em `oneOf`, com o `code` e a
    `message` exata como `const` e a proibição das três chaves; o envelope com qualquer delas é
    recusado."""
    schema = _load("error.schema.json")
    error = schema["properties"]["error"]
    messages = {
        "invalid_dispatch_state": "A operação não é permitida no estado atual do disparo.",
        "window_expired": "A janela do disparo não é mais válida; prepare um novo disparo.",
        "grading_pending": ("Há respostas aguardando correção ou mensagens ainda não processadas neste "
                            "oferecimento."),
    }
    for code, message in messages.items():
        branches = [b for b in error["oneOf"] if b["properties"]["code"] == {"const": code}]
        assert branches == [{
            "properties": {"code": {"const": code}, "message": {"const": message}},
            "required": ["code", "message"],
            "not": {"anyOf": [{"required": ["fields"]}, {"required": ["permission"]}, {"required": ["state"]}]},
        }], code
        good = {"error": {"code": code, "message": message}}
        assert _is_valid(schema, good), code
        for extra in ({"fields": [{"field": "repo", "reason": "invalid_format"}]}, {"state": "active"},
                      {"permission": "offerings:write"}):
            assert not _is_valid(schema, {"error": {**good["error"], **extra}}), (code, extra)

    # A implementação: os 3 códigos em `cq_server.api.errors` (409, a mensagem exata, sem `fields`,
    # `state` nem `permission`, e o envelope válido no esquema congelado) e nas descrições de erro que o
    # OpenAPI publica por operação.
    from cq_server.api import errors

    assert sorted(set(messages) - set(errors.MESSAGES)) == [], "códigos do disparo ausentes de cq_server.api.errors"
    for code, message in messages.items():
        assert (errors.MESSAGES[code], errors.STATUS[code]) == (message, 409), code
        assert code not in errors.FIELDS_CODES, code
        body = errors.error_body(code)
        assert body == {"error": {"code": code, "message": message}}, code
        assert _is_valid(schema, body), code
    doc = _published()
    for op_id, codes in (("dispatches.confirm", ("invalid_dispatch_state", "window_expired")),
                         ("dispatches.cancel", ("invalid_dispatch_state",)),
                         ("offerings.prepare_archive", ("grading_pending",)),
                         ("offerings.archive", ("grading_pending",))):
        conflict = _deref(_published_operation(doc, op_id).get("responses", {}).get("409", {}), doc)
        for code in codes:
            assert f"`{code}`: {messages[code]}" in conflict.get("description", ""), (op_id, code)


def test_dispatch_catalog_confirmation_and_mcp_exposure() -> None:
    """Entre as 6 operações novas, só `dispatches.confirm` tem
    `confirm: true` e é destrutiva; as 5 de disparos são expostas ao MCP com as ferramentas do contrato;
    `messages.list` não é exposta, com o motivo exato (texto livre de aluno fica para a operação por agente)."""
    operations = {op["id"]: op for op in load_catalog()["operations"]}
    assert set(DISPATCH_OPERATIONS) <= set(operations)
    tools = {op_id: operations[op_id]["mcp"].get("tool") for op_id in DISPATCH_OPERATIONS}
    assert tools == {"dispatches.prepare": "prepare_dispatch", "dispatches.get": "get_dispatch",
                     "dispatches.list": "list_dispatches", "dispatches.confirm": "confirm_dispatch",
                     "dispatches.cancel": "cancel_dispatch", "messages.list": None}
    assert operations["messages.list"]["mcp"] == {
        "exposed": False, "reason": "Texto livre de aluno; fica fora do MCP nesta versão."}
    assert [op_id for op_id in DISPATCH_OPERATIONS if operations[op_id]["mcp"].get("confirm") is True] == [
        "dispatches.confirm"]
    assert [op_id for op_id in DISPATCH_OPERATIONS if operations[op_id]["destructive"]] == ["dispatches.confirm"]

    # A implementação: as 6 operações no registro (`BY_ID`) e no OpenAPI publicado, com o método, o
    # caminho e a permissão do catálogo; só `dispatches.prepare` recebe corpo (a confirmação, destrutiva,
    # é pelo caminho, sem corpo).
    from cq_server.api.operations import BY_ID

    assert [op_id for op_id in DISPATCH_OPERATIONS if op_id not in BY_ID] == [], "operações do disparo fora do registro"
    doc = _published()
    for op_id in DISPATCH_OPERATIONS:
        entry, op = operations[op_id], BY_ID[op_id]
        assert (op.method, op.path, op.permission.value if op.permission else None) == (
            entry["method"], entry["path"], entry["permission"]), op_id
        published = _published_operation(doc, op_id)
        assert published.get("operationId") == op_id
        assert ("requestBody" in published) is (op_id == "dispatches.prepare"), op_id
