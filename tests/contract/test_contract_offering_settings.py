"""Contratos da configuração das mensagens livres do oferecimento (opcionais por oferecimento e
desligadas por padrão), com o e-mail de contato do professor e o aviso de falta desse contato na
prévia do disparo.

Divisão com os demais arquivos de contrato: `test_contract_dispatch.py` fica só com as 6 operações
do disparo (`DISPATCH_OPERATIONS` e `DISPATCH_SCHEMA_FILES`), porque afirma que nenhum arquivo dele tem `x-identity`, e o pedido
daqui tem; as tabelas gerais (as operações do catálogo, os 11 pontos `x-identity`, os tipos de
evento, `REQUEST_OPS` e parâmetros do OpenAPI) ficam nos próprios `test_catalog.py`, `test_contract_files.py` e
`test_openapi.py`. Aqui ficam as conferências próprias das mensagens livres, campo a campo:
- o pedido e a resposta de `offerings.update_settings` (`PATCH /v1/offerings/{ref}/settings`);
- o `Offering` com `free_messages` e `contact_email_set` nas 8 respostas que o têm, e o e-mail fora de toda
  resposta;
- a variante `offering.settings_changed` de `events.list`;
- a entrada do catálogo e o registro da operação;
- a marca `x-identity: email` em `contact_email` dos dois pedidos, publicada;
- nenhum nome proibido nos campos das mensagens livres e nenhuma identidade nas respostas com `Offering`;
- o aviso `no_teacher_contact` no `enum` de `kind` dos avisos da prévia e de `warnings` do
  `dispatch.validated`, o conjunto fixado por `WARNING_KINDS`.

Em cada função, primeiro os arquivos congelados de `contracts/api/v1/`
e depois a mesma regra na implementação: o registro de operações (`cq_server.api.operations.BY_ID`), o tipo de
evento (`cq_server.events`) e o OpenAPI que a app publica (`create_app(...).openapi()`, sem banco). No esqueleto, falha
assim: a operação nova não está no registro nem no OpenAPI publicado, o
`Offering`, o pedido de cadastro e os avisos publicados não têm os campos e o valor novos, e `EventType` não tem
o 43º tipo. Símbolos que só a implementação cria são lidos com `getattr`, para o módulo coletar.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.helpers.contract import SCHEMAS, flatten, load_catalog, load_json

OP_ID = "offerings.update_settings"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_BOOL = {"type": "boolean"}
_CONTACT = {"type": ["string", "null"], "x-identity": "email"}

# O pedido fechado, com pelo menos um dos dois campos, sem `required`.
UPDATE_SETTINGS_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {"free_messages": _BOOL, "contact_email": _CONTACT},
}
# A entrada do catálogo no contrato.
CATALOG_ENTRY = {
    "id": OP_ID, "method": "PATCH", "path": "/v1/offerings/{ref}/settings", "permission": "offerings:write",
    "cli": "offerings settings",
    "mcp": {"exposed": True, "tool": "update_offering_settings", "confirm": False},
    "destructive": False,
}
# O `Offering` com as mensagens livres: operação → (status de sucesso, prefixo do `Offering` na forma
# achatada). Os 8 esquemas de resposta com `Offering`.
OFFERING_RESPONSES: dict[str, tuple[str, str]] = {
    "offerings.create": ("201", ""),
    "offerings.list": ("200", "items[]."),
    "offerings.get": ("200", ""),
    "offerings.close": ("200", ""),
    "offerings.prepare_archive": ("200", "offering."),
    "offerings.archive": ("200", ""),
    OP_ID: ("200", ""),
    "bots.activate": ("200", ""),
}
# Os dados do evento, sem o e-mail.
SETTINGS_CHANGED_FIELDS = {"offering_id", "free_messages", "contact_email_set"}
# Forma achatada dos pedidos com `contact_email` (os demais campos sem marca).
REQUEST_FLATTEN: dict[str, set[tuple[str, frozenset[str]]]] = {
    "offerings.create": {
        ("code", frozenset()), ("course", frozenset()), ("term", frozenset()), ("timezone", frozenset()),
        ("response_window_minutes", frozenset()), ("free_messages", frozenset()),
        ("contact_email", frozenset({"x-identity:email"})),
    },
    OP_ID: {("free_messages", frozenset()), ("contact_email", frozenset({"x-identity:email"}))},
}
# Os avisos da prévia, com `no_teacher_contact` por último.
WARNING_KINDS = ["console_output", "missing_media", "media_error", "empty_target", "too_many_options",
                 "repeated_question", "no_teacher_contact"]
FULL_DISPATCH_OPS = ("dispatches.prepare", "dispatches.get", "dispatches.confirm", "dispatches.cancel")
# O mesmo critério de `test_contract_dispatch.py`: `code` só em `offering.code`, e nenhum destes.
RESERVED_NAMES = {"link", "recipient", "secret", "until", "username", "token_id"}
_HTTP_METHODS = ("get", "put", "post", "delete", "patch")


def _load(name: str) -> Any:
    return load_json(str(SCHEMAS / name))


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def _without_ids(schema: dict) -> dict:
    return {k: v for k, v in schema.items() if k not in ("$schema", "$id", "title")}


def _offering_node(op_id: str, schema: dict) -> dict:
    """O subesquema do `Offering` na resposta de `op_id`."""
    if op_id == "offerings.list":
        return schema["properties"]["items"]["items"]
    if op_id == "offerings.prepare_archive":
        return schema["properties"]["offering"]
    return schema


def _nodes(node: Any, path: str = "") -> list[tuple[str, dict]]:
    """(caminho achatado, subesquema) de toda propriedade e item, atravessando combinadores."""
    out: list[tuple[str, dict]] = []
    if not isinstance(node, dict):
        return out
    if path:
        out.append((path, node))
    for key in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(key, []) or []:
            out += _nodes(branch, path)
    for name, child in (node.get("properties") or {}).items():
        out += _nodes(child, f"{path}.{name}" if path else name)
    if isinstance(node.get("items"), dict):
        out += _nodes(node["items"], f"{path}[]")
    return out


def _forbidden_names(schema: dict) -> list[str]:
    """Campos chamados `code` fora de `offering.code` e campos com um nome de `RESERVED_NAMES`."""
    names = {(path, path.rsplit(".", 1)[-1]) for path, _ in _nodes(schema) if not path.endswith("[]")}
    return sorted(path for path, name in names
                  if (name == "code" and path != "offering.code") or name in RESERVED_NAMES)


def _extension_keys(node: Any, pointer: str = "", in_names: bool = False) -> list[tuple[str, str, Any]]:
    """Toda palavra-chave `x-*` do documento: (ponteiro, chave, valor)."""
    out: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if not in_names and key.startswith("x-"):
                out.append((pointer, key, value))
            out += _extension_keys(value, f"{pointer}/{key}", key in ("properties", "$defs") and not in_names)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            out += _extension_keys(item, f"{pointer}/{i}")
    return out


def _event_variants(schema: dict) -> dict[str, dict]:
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


# ---------------------------------------------------------------------------------------------


def test_update_settings_request_and_response_schemas() -> None:
    """O pedido exato de
    `offerings.update_settings` (fechado, `minProperties: 1`, sem `required`, `free_messages` booleano e
    `contact_email` texto ou `null` com `x-identity: email`), que recusa `{}`, o booleano nulo ou em texto, o
    e-mail não texto e o campo extra; a resposta igual à de `offerings.get` (o `Offering` completo) fora o
    `$id` e o `title`. Na implementação: a operação no registro com modelo de pedido, e o pedido e a resposta
    publicados com a mesma forma achatada dos congelados. Vermelho: a operação não está no registro nem no
    OpenAPI publicado."""
    request = _load(f"{OP_ID}.request.schema.json")
    assert request["$id"].endswith(f"/{OP_ID}.request.schema.json")
    assert _without_ids(request) == UPDATE_SETTINGS_REQUEST
    for ok in ({"free_messages": True}, {"free_messages": False}, {"contact_email": "prof@docentes.example.org"},
               {"contact_email": None}, {"free_messages": False, "contact_email": None}):
        assert _is_valid(request, ok), ok
    for bad in ({}, {"free_messages": None}, {"free_messages": "on"}, {"free_messages": 1},
                {"contact_email": 1}, {"contact_email": ["prof@docentes.example.org"]},
                {"free_messages": True, "contact_email_set": True}, {"email": "prof@docentes.example.org"}):
        assert not _is_valid(request, bad), bad

    response = _load(f"{OP_ID}.response.schema.json")
    assert response["$id"].endswith(f"/{OP_ID}.response.schema.json")
    assert response["$id"] != _load("offerings.get.response.schema.json")["$id"]
    assert _without_ids(response) == _without_ids(_load("offerings.get.response.schema.json"))

    from cq_server.api.operations import BY_ID

    assert OP_ID in BY_ID, f"{OP_ID} fora do registro de operações"
    assert BY_ID[OP_ID].request_model is not None, f"{OP_ID} sem modelo de pedido"
    doc = _published()
    assert _paths(flatten(_request_schema(doc, OP_ID), doc)) == {"free_messages", "contact_email"}
    assert flatten(_success_schema(doc, OP_ID), doc) == flatten(response, response)


def test_offering_has_the_two_booleans_everywhere() -> None:
    """Nos 8 esquemas de
    resposta com `Offering` (`offerings.create`, `.list` em `items[]`, `.get`, `.close`, `.prepare_archive` em
    `offering`, `.archive`, `.update_settings` e `bots.activate`), `free_messages` e `contact_email_set` são
    `{"type": "boolean"}` e obrigatórios; nenhum esquema de resposta tem um campo `contact_email` (o e-mail
    nunca sai). Na implementação: as 8 respostas publicadas têm os dois booleanos, sem marca, e nenhum
    `contact_email`. Vermelho: o `Offering` publicado não tem os campos novos."""
    for op_id in OFFERING_RESPONSES:
        node = _offering_node(op_id, _load(f"{op_id}.response.schema.json"))
        assert {"free_messages", "contact_email_set"} <= set(node["required"]), op_id
        assert node["properties"]["free_messages"] == _BOOL, op_id
        assert node["properties"]["contact_email_set"] == _BOOL, op_id
        assert "contact_email" not in node["properties"], op_id
    responses = sorted(p.name for p in SCHEMAS.glob("*.response.schema.json"))
    assert responses
    with_email = sorted(f"{name}:{path}" for name in responses
                        for path, _ in _nodes(_load(name)) if path.rsplit(".", 1)[-1] == "contact_email")
    assert with_email == []

    doc = _published()
    for op_id, (_, prefix) in OFFERING_RESPONSES.items():
        flat = flatten(_success_schema(doc, op_id), doc)
        assert (f"{prefix}free_messages", frozenset()) in flat, f"{op_id}: `free_messages` ausente do publicado"
        assert (f"{prefix}contact_email_set", frozenset()) in flat, (
            f"{op_id}: `contact_email_set` ausente do publicado")
        assert not any(path.rsplit(".", 1)[-1] == "contact_email" for path, _ in flat), op_id


def test_settings_changed_event_variant() -> None:
    """A variante `offering.settings_changed`
    de `events.list` é exatamente `{seq, type: {const}, occurred_at, actor_token_id, data}`, com o envelope
    das demais variantes e `data` fechado `{offering_id (UUID v4), free_messages (booleano),
    contact_email_set (booleano)}`, todos obrigatórios, sem marca e sem o e-mail. Na implementação:
    `EventType` tem o tipo, `EVENT_FIELDS` dá os mesmos campos, e o `events.list` publicado tem os dois
    booleanos em `data`. Vermelho: `EventType` não tem o 43º tipo."""
    schema = _load("events.list.response.schema.json")
    variants = _event_variants(schema)
    assert "offering.settings_changed" in variants
    reference = variants["offering.created"]
    expected = {
        **reference,
        "properties": {
            **reference["properties"],
            "type": {"const": "offering.settings_changed"},
            "data": {
                "type": "object", "additionalProperties": False,
                "required": ["offering_id", "free_messages", "contact_email_set"],
                "properties": {"offering_id": {"type": "string", "pattern": UUID4_PATTERN},
                               "free_messages": _BOOL, "contact_email_set": _BOOL},
            },
        },
    }
    assert variants["offering.settings_changed"] == expected
    assert _extension_keys(variants["offering.settings_changed"]) == []
    page = {"items": [{"seq": 1, "type": "offering.settings_changed", "occurred_at": "2031-01-01T00:00:00Z",
                       "actor_token_id": None, "data": {"offering_id": "3f2b8c1e-9a4d-4e7f-8b2a-1c0d9e8f7a6b",
                                                        "free_messages": False, "contact_email_set": True}}],
            "next_cursor": 1}
    assert _is_valid(schema, page)
    page["items"][0]["data"]["contact_email"] = "prof@docentes.example.org"
    assert not _is_valid(schema, page)

    from cq_server import events

    member = getattr(events.EventType, "OFFERING_SETTINGS_CHANGED", None)
    assert member is not None, "EventType sem offering.settings_changed"
    assert member.value == "offering.settings_changed"
    assert set(events.EVENT_FIELDS[member]) == SETTINGS_CHANGED_FIELDS
    doc = _published()
    published = _paths(flatten(_success_schema(doc, "events.list"), doc))
    assert {"items[].data.free_messages", "items[].data.contact_email_set"} <= published


def test_update_settings_is_registered_and_published() -> None:
    """A entrada do catálogo é exatamente
    `{offerings.update_settings, PATCH, /v1/offerings/{ref}/settings, offerings:write, offerings settings,
    exposta como update_offering_settings, confirm: false, não destrutiva}`, a 31ª do catálogo. Na
    implementação: o registro (`BY_ID`) com o mesmo método, caminho e permissão e com modelo de pedido; o
    OpenAPI publicado com o `PATCH` no caminho, o `operationId`, o `requestBody`, só o 200 como sucesso e o
    `ref` no caminho. Vermelho: a operação não está no registro nem no OpenAPI publicado.

    Relatórios: a
    31ª entrada (índice 30); as 5 dos relatórios vêm depois, no molde de `ids[30:31]`
    do test_catalog.py."""
    operations = load_catalog()["operations"]
    # a 31ª entrada (índice 30); as 5 dos relatórios vêm depois.
    assert operations[30] == CATALOG_ENTRY
    assert [op["id"] for op in operations].count(OP_ID) == 1

    from cq_server.api.operations import BY_ID

    assert OP_ID in BY_ID, f"{OP_ID} fora do registro de operações"
    op = BY_ID[OP_ID]
    assert (op.method, op.path, op.permission.value if op.permission else None) == (
        CATALOG_ENTRY["method"], CATALOG_ENTRY["path"], CATALOG_ENTRY["permission"])
    assert op.request_model is not None
    doc = _published()
    item = _deref(doc.get("paths", {}).get(CATALOG_ENTRY["path"], {}), doc)
    assert {m for m in _HTTP_METHODS if m in item} == {"patch"}, sorted(item)
    published = _published_operation(doc, OP_ID)
    assert published.get("operationId") == OP_ID
    assert "requestBody" in published
    assert {s for s in published.get("responses", {}) if s.startswith("2")} == {"200"}
    params = [_deref(p, doc) for p in [*item.get("parameters", []), *published.get("parameters", [])]]
    assert {(p["in"], p["name"]) for p in params if p.get("in") in ("path", "query")} == {("path", "ref")}


@pytest.mark.parametrize("op_id", ["offerings.create", OP_ID])
def test_contact_email_identity_mark_is_published(op_id: str) -> None:
    """`x-identity: email` em 2 pontos, ambos em pedido: o pedido
    congelado, na forma achatada, tem `("contact_email", {"x-identity:email"})` e `("free_messages", ∅)`; no
    cadastro, os demais campos ficam sem marca; o pedido **publicado** coincide com o congelado na forma
    achatada, com as marcas (como `test_openapi.py::test_people_register_request_flatten_matches_frozen`).
    Vermelho: o pedido de cadastro publicado não tem `contact_email` nem a marca, e a operação nova não está
    no OpenAPI publicado."""
    frozen = _load(f"{op_id}.request.schema.json")
    expected = flatten(frozen, frozen)
    assert expected == REQUEST_FLATTEN[op_id]
    doc = _published()
    assert flatten(_request_schema(doc, op_id), doc) == expected


def test_new_schemas_have_no_forbidden_names_and_no_identity_in_responses() -> None:
    """Com o critério de `test_contract_dispatch.py` (`code` só em
    `offering.code`; nenhum `link`, `recipient`, `secret`, `until`, `username` nem `token_id`), nenhum nome
    proibido no pedido da operação, nos dados de `offering.settings_changed` e nos dois campos do `Offering`; na
    resposta dela, só os do `Offering` original (`code` na raiz e `bot.username`, os mesmos de `offerings.get`;
    leitura [I]: o critério vale para os campos das mensagens livres, e a resposta repete o `Offering` original).
    Nenhum `x-identity` nem `x-secret` nos 8 esquemas de resposta com `Offering`; o único `x-identity` dos
    esquemas daqui é o `contact_email` do pedido, e a resposta da operação só tem `x-free-text` em `course` e
    `term`. Controle positivo: o critério acusa um campo novo proibido. Na implementação: o mesmo no pedido
    e na resposta publicados da operação, e nenhuma marca de identidade ou segredo nas 8 respostas
    publicadas. Vermelho: a operação nova não está no OpenAPI publicado."""
    request = _load(f"{OP_ID}.request.schema.json")
    response = _load(f"{OP_ID}.response.schema.json")
    assert _forbidden_names(request) == []
    event = _event_variants(_load("events.list.response.schema.json"))["offering.settings_changed"]
    assert _forbidden_names(event["properties"]["data"]) == []
    baseline = _forbidden_names(_load("offerings.get.response.schema.json"))
    assert baseline == ["bot.username", "code"]
    assert _forbidden_names(response) == baseline
    for op_id in OFFERING_RESPONSES:
        node = _offering_node(op_id, _load(f"{op_id}.response.schema.json"))
        new_fields = {"type": "object", "properties": {
            name: node["properties"][name] for name in ("free_messages", "contact_email_set")}}
        assert _forbidden_names(new_fields) == [], op_id
        marks = [(ptr, key) for ptr, key, _ in _extension_keys(_load(f"{op_id}.response.schema.json"))
                 if key in ("x-identity", "x-secret")]
        assert marks == [], op_id
    assert _extension_keys(request) == [("/properties/contact_email", "x-identity", "email")]
    assert sorted(_extension_keys(response)) == [("/properties/course", "x-free-text", True),
                                                 ("/properties/term", "x-free-text", True)]
    assert _forbidden_names({"type": "object", "properties": {"free_messages": _BOOL, "token_id": _BOOL}}) == [
        "token_id"]
    assert _forbidden_names({"type": "object", "properties": {"offering": {"type": "object", "properties": {
        "code": {"type": "string"}, "username": {"type": "string"}}}}}) == ["offering.username"]

    doc = _published()
    published_request = _paths(flatten(_request_schema(doc, OP_ID), doc))
    assert not ({"code", "username", "token_id", "secret", "link", "recipient", "until"} & published_request)
    published_response = flatten(_success_schema(doc, OP_ID), doc)
    assert {path for path, marks in published_response if marks} == {"course", "term"}
    for op_id in OFFERING_RESPONSES:
        flat = flatten(_success_schema(doc, op_id), doc)
        assert not any(m.startswith("x-identity") or m == "x-secret" for _, marks in flat for m in marks), op_id


def test_no_teacher_contact_warning_kind_is_frozen_and_published() -> None:
    """O `enum` de `kind` dos avisos da prévia
    nas 4 respostas com o `Dispatch` completo (`dispatches.prepare`, `.get`, `.confirm` e `.cancel`) e o de
    `warnings` do `dispatch.validated` em `events.list` são exatamente os 7 avisos, com `no_teacher_contact`
    por último. Na implementação: os mesmos valores publicados (`preview.warnings[].kind` e
    `items[].data.warnings[]`). Vermelho: os avisos publicados não têm `no_teacher_contact`."""
    for op_id in FULL_DISPATCH_OPS:
        schema = _load(f"{op_id}.response.schema.json")
        kind = schema["properties"]["preview"]["properties"]["warnings"]["items"]["properties"]["kind"]
        assert kind == {"enum": WARNING_KINDS}, op_id
    validated = _event_variants(_load("events.list.response.schema.json"))["dispatch.validated"]
    assert validated["properties"]["data"]["properties"]["warnings"] == {
        "type": "array", "items": {"enum": WARNING_KINDS}}

    doc = _published()
    for op_id in FULL_DISPATCH_OPS:
        assert _enum_at(doc, _success_schema(doc, op_id), "preview.warnings[].kind") == set(WARNING_KINDS), op_id
    assert _enum_at(doc, _success_schema(doc, "events.list"), "items[].data.warnings[]") == set(WARNING_KINDS)
