"""OpenAPI publicado.

Tudo é conferido sobre o documento **publicado** em `GET /openapi.json` (sem token), comparado com
os arquivos congelados de `contracts/api/v1/` pela forma achatada do contrato (`flatten`).

Oferecimentos e bots:
201 também em `offerings.create` e
`team.link`; os 4 pedidos novos; `ref` no caminho das 8 operações com `{ref}` e `after`/`limit` em
`offerings.list`; `x-free-text` em `course`/`term` e `x-secret` no `link` de `team.link`; segredo em
pedido exatamente no `token` de `bots.register`. No esqueleto, as rotas novas
não existem, e os testes que as consultam falham por isso.

Vínculo do aluno (lista e convites):
200 nas 5 operações novas; os 4 pedidos novos (`students.import`,
`invites.prepare_send`, `invites.send`, `invites.resend`; `students.summary` é GET sem corpo); `ref` no
caminho das 5; nenhuma marca nas respostas novas e nenhum `x-secret` novo; e o pedido de
`students.import` publicado com as 3 marcas `x-identity` em `rows[].name`, `rows[].ra` e
`rows[].email`. No esqueleto, as rotas do vínculo do aluno não existem, e os testes que as
consultam falham por isso.

Disparo e resposta:
200 nas 6 operações novas; o pedido novo de `dispatches.prepare` (os demais
são sem corpo); `ref` no caminho das 6, `dispatch_id` no caminho de `dispatches.get`, `.confirm` e
`.cancel`, e `after`/`limit` na consulta de `dispatches.list` e `messages.list`; nas respostas, a marca
nova `x-untrusted` sempre com `x-free-text` no conteúdo de terceiros (prévia, motivos do motor) e no
texto livre de aluno (`items[].text` de `messages.list`), e só `x-free-text` em `question.repo` e
`question.qid`; nenhum `x-secret` nem `x-identity` novo. No esqueleto, as rotas do disparo e da resposta não existem,
e os testes que as consultam falham por isso.

Mensagens livres opcionais por oferecimento, desligadas por padrão (no disparo e na resposta):
a operação nova `offerings.update_settings`
(`PATCH`, 200) com pedido (`REQUEST_OPS`), `ref` no caminho e `x-free-text` em `course`/`term` da resposta
(o `Offering`); o pedido de `offerings.create` e o `Offering` de toda resposta com os campos novos (a forma
achatada vem dos arquivos congelados). Vermelho no esqueleto: a rota nova não existe no OpenAPI
publicado, e o `Offering` e o pedido de cadastro publicados não têm os campos novos.

Relatórios e acompanhamento:
200 nas 5 operações novas; o pedido novo de `offerings.update_digest`
(`REQUEST_OPS`; as demais são sem corpo); `ref` no caminho das 5, `dispatch_id` no caminho das 3 de
relatórios, `format` e `version` na consulta de `reports.get` e `after`/`limit` na de `reports.list_versions`;
nas respostas, `x-untrusted` sempre com `x-free-text` no título e nos textos da distribuição do `Report` e só
`x-free-text` em `dispatch.question.qid`; nenhum `x-secret` nem `x-identity` novo; o 200 de `reports.get` com
as duas mídias, `application/json` (o `Report`) e `text/csv` (`{"type": "string", "x-free-text": true,
"x-untrusted": true}`). No esqueleto, as rotas dos relatórios não existem, e os
testes que as consultam falham por isso.

CLI:
200 nas 3 operações novas (`system.doctor`, `offerings.prepare_purge`, `offerings.purge`); o
pedido novo de `offerings.purge` (`REQUEST_OPS`; o `doctor` e a preparação são sem corpo); `ref` no caminho das
duas do expurgo e nenhum parâmetro no `doctor`; o parâmetro novo `person` na consulta de `events.list`; nenhuma
marca nas 3 respostas novas (nenhum `x-identity` nem `x-secret` novo; o oferecimento sai como `OfferingRef`); o
`Offering` de toda resposta com `purged_at` (a forma achatada vem dos arquivos congelados). No esqueleto, as rotas
do CLI não existem, o `person` não está publicado, e as respostas
publicadas que embutem o `Offering` ainda não têm `purged_at`, e os testes que os consultam falham
por isso.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers.contract import flatten, iter_objects, load_catalog, load_schema

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
CATALOG = load_catalog()["operations"]
OP_IDS = [op["id"] for op in CATALOG]

# Status de sucesso de cada operação.
SUCCESS_STATUS = {op_id: "200" for op_id in OP_IDS} | {"people.register": "201", "tokens.create": "201"}
SUCCESS_STATUS |= {"offerings.create": "201", "team.link": "201"}  # oferecimentos e bots

# Operações que recebem corpo, com esquema de pedido congelado.
REQUEST_OPS = ("people.register", "identity.reveal", "tokens.create")
# Oferecimentos e bots.
REQUEST_OPS += ("offerings.create", "offerings.archive", "bots.register", "team.link")
# Vínculo do aluno: `students.summary` é GET, sem corpo.
REQUEST_OPS += ("students.import", "invites.prepare_send", "invites.send", "invites.resend")
# Disparo e resposta: só o preparo tem corpo; a confirmação e o cancelamento são sem corpo
# (pelo identificador do disparo).
REQUEST_OPS += ("dispatches.prepare",)
# Mensagens livres por oferecimento: a alteração da configuração tem corpo (`minProperties: 1`).
REQUEST_OPS += ("offerings.update_settings",)
# Relatórios: só a alteração do resumo diário tem corpo (`minProperties: 1`); o reenvio do
# resumo é sem corpo (`reject_body`, como `dispatches.confirm`).
REQUEST_OPS += ("offerings.update_digest",)
# CLI: só a confirmação do expurgo tem corpo (`{confirmation_code}`); o `doctor` e a
# preparação do expurgo são sem corpo.
REQUEST_OPS += ("offerings.purge",)

# Parâmetros de caminho e de consulta de cada operação. Cabeçalhos ficam de fora.
EXPECTED_PARAMETERS: dict[str, set[tuple[str, str]]] = {op_id: set() for op_id in OP_IDS} | {
    "people.list": {("query", "after"), ("query", "limit")},
    "people.get": {("path", "ref")},
    "tokens.revoke": {("path", "token_id")},
    "events.list": {("query", "types"), ("query", "after"), ("query", "limit")},
    # Oferecimentos e bots.
    "offerings.list": {("query", "after"), ("query", "limit")},
    "offerings.get": {("path", "ref")},
    "offerings.close": {("path", "ref")},
    "offerings.prepare_archive": {("path", "ref")},
    "offerings.archive": {("path", "ref")},
    "bots.register": {("path", "ref")},
    "bots.activate": {("path", "ref")},
    "bots.get": {("path", "ref")},
    "team.link": {("path", "ref")},
    # Vínculo do aluno: `ref` no caminho das 5 operações; nenhuma consulta.
    "students.import": {("path", "ref")},
    "students.summary": {("path", "ref")},
    "invites.prepare_send": {("path", "ref")},
    "invites.send": {("path", "ref")},
    "invites.resend": {("path", "ref")},
    # Disparo e resposta: `ref` no caminho das 6; `dispatch_id` no caminho das 3 operações
    # sobre um disparo; paginação (`after`, `limit`) nas duas listas.
    "dispatches.prepare": {("path", "ref")},
    "dispatches.get": {("path", "ref"), ("path", "dispatch_id")},
    "dispatches.list": {("path", "ref"), ("query", "after"), ("query", "limit")},
    "dispatches.confirm": {("path", "ref"), ("path", "dispatch_id")},
    "dispatches.cancel": {("path", "ref"), ("path", "dispatch_id")},
    "messages.list": {("path", "ref"), ("query", "after"), ("query", "limit")},
    # Mensagens livres por oferecimento: `ref` no caminho, sem consulta.
    "offerings.update_settings": {("path", "ref")},
    # Relatórios: `ref` no caminho das 5; `dispatch_id` no caminho das 3 de relatórios;
    # `format` e `version` na consulta de `reports.get`; paginação nas versões.
    "reports.get": {("path", "ref"), ("path", "dispatch_id"), ("query", "format"), ("query", "version")},
    "reports.list_versions": {("path", "ref"), ("path", "dispatch_id"), ("query", "after"), ("query", "limit")},
    "reports.resend_summary": {("path", "ref"), ("path", "dispatch_id")},
    "offerings.get_digest": {("path", "ref")},
    "offerings.update_digest": {("path", "ref")},
    # CLI: o `doctor` sem parâmetro; `ref` no caminho das duas do expurgo.
    "system.doctor": set(),
    "offerings.prepare_purge": {("path", "ref")},
    "offerings.purge": {("path", "ref")},
}
# CLI: `events.list` tem o parâmetro de consulta `person` (opcional).
EXPECTED_PARAMETERS["events.list"] = EXPECTED_PARAMETERS["events.list"] | {("query", "person")}

# Marcas nas respostas, exatamente como no contrato: (operação, status, caminho, marca).
EXPECTED_RESPONSE_MARKS = {
    ("identity.reveal", "200", "people[].name", "x-identity:name"),
    ("identity.reveal", "200", "people[].ra", "x-identity:ra"),
    ("identity.reveal", "200", "people[].email", "x-identity:email"),
    ("tokens.create", "201", "secret", "x-secret"),
    ("tokens.create", "201", "label", "x-free-text"),
    ("tokens.list", "200", "items[].label", "x-free-text"),
    ("tokens.revoke", "200", "label", "x-free-text"),
    ("events.list", "200", "items[].data.label", "x-free-text"),
    ("events.list", "200", "items[].data.reason", "x-free-text"),
    ("events.list", "200", "items[].data.error", "x-free-text"),
    # Oferecimentos e bots: `course` e `term` em toda resposta com `Offering`; `link` secreto.
    ("offerings.create", "201", "course", "x-free-text"),
    ("offerings.create", "201", "term", "x-free-text"),
    ("offerings.list", "200", "items[].course", "x-free-text"),
    ("offerings.list", "200", "items[].term", "x-free-text"),
    ("offerings.get", "200", "course", "x-free-text"),
    ("offerings.get", "200", "term", "x-free-text"),
    ("offerings.close", "200", "course", "x-free-text"),
    ("offerings.close", "200", "term", "x-free-text"),
    ("offerings.prepare_archive", "200", "offering.course", "x-free-text"),
    ("offerings.prepare_archive", "200", "offering.term", "x-free-text"),
    ("offerings.archive", "200", "course", "x-free-text"),
    ("offerings.archive", "200", "term", "x-free-text"),
    ("bots.activate", "200", "course", "x-free-text"),
    ("bots.activate", "200", "term", "x-free-text"),
    ("team.link", "201", "link", "x-secret"),
}
# Disparo e resposta: no `Dispatch` completo (`dispatches.prepare`, `.get`,
# `.confirm` e `.cancel`), `x-free-text` em `question.repo` e `question.qid` (texto do operador) e
# `x-free-text` + `x-untrusted` no conteúdo de terceiros (a prévia e os motivos do motor); na lista, o
# `DispatchSummary` sem a prévia; em `messages.list`, o texto livre de aluno.
_DISPATCH_OPERATOR_TEXT = ("question.repo", "question.qid")
_DISPATCH_UNTRUSTED = ("rejection.details[]", "preview.title", "preview.statement", "preview.fields[].label",
                 "preview.fields[].suffix", "preview.fields[].hint", "preview.warnings[].message")
for _op in ("dispatches.prepare", "dispatches.get", "dispatches.confirm", "dispatches.cancel"):
    EXPECTED_RESPONSE_MARKS |= {(_op, "200", path, "x-free-text") for path in _DISPATCH_OPERATOR_TEXT + _DISPATCH_UNTRUSTED}
    EXPECTED_RESPONSE_MARKS |= {(_op, "200", path, "x-untrusted") for path in _DISPATCH_UNTRUSTED}
EXPECTED_RESPONSE_MARKS |= {("dispatches.list", "200", f"items[].{path}", "x-free-text")
                            for path in (*_DISPATCH_OPERATOR_TEXT, "rejection.details[]")}
EXPECTED_RESPONSE_MARKS |= {("dispatches.list", "200", "items[].rejection.details[]", "x-untrusted"),
                            ("messages.list", "200", "items[].text", "x-free-text"),
                            ("messages.list", "200", "items[].text", "x-untrusted")}
# Mensagens livres por oferecimento: a resposta de `offerings.update_settings` é o `Offering`, com `course`
# e `term` marcados como em toda resposta com `Offering`; os dois booleanos novos não têm marca.
EXPECTED_RESPONSE_MARKS |= {("offerings.update_settings", "200", "course", "x-free-text"),
                            ("offerings.update_settings", "200", "term", "x-free-text")}
# Relatórios: no `Report`, `x-free-text` + `x-untrusted` no título e nos textos das
# distribuições e só `x-free-text` no `qid`; as outras 4 respostas sem marca. A raiz da mídia `text/csv` (marcada,
# declarativa) não tem caminho na forma achatada e é conferida em `test_response_markers_exactly_as_contract`.
_REPORT_UNTRUSTED = ("dispatch.title", "distribution.fields[].label", "distribution.fields[].choices[].text",
                 "distribution.fields[].values.items[].value", "distribution.fields[].ratios.items[].value",
                 "distribution.fields[].texts.items[].value")
EXPECTED_RESPONSE_MARKS |= {("reports.get", "200", path, "x-free-text")
                            for path in ("dispatch.question.qid", *_REPORT_UNTRUSTED)}
EXPECTED_RESPONSE_MARKS |= {("reports.get", "200", path, "x-untrusted") for path in _REPORT_UNTRUSTED}
# CLI: nenhuma marca nas 3 respostas novas (as mensagens do `doctor` são texto fixo; o
# oferecimento sai como `OfferingRef`; o `confirmation_code` do expurgo não é segredo), e o `purged_at` do
# `Offering` não tem marca: o conjunto não ganha nenhuma tupla, e as 3 operações entram na comparação exata de
# `test_response_markers_exactly_as_contract` pelo laço do catálogo.
# Relatórios: o esquema da mídia CSV do 200 de `reports.get`.
REPORT_CSV_SCHEMA = {"type": "string", "x-free-text": True, "x-untrusted": True}

# Palavras-chave que dão forma a um subesquema; um subesquema sem nenhuma delas aceita qualquer
# valor, inclusive um dicionário livre.
_SHAPING_KEYS = ("type", "$ref", "const", "enum", "allOf", "anyOf", "oneOf", "not")


def _published(client: Any) -> dict:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200, f"/openapi.json sem token: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def _deref(node: Any, doc: dict) -> Any:
    for _ in range(50):
        if not (isinstance(node, dict) and "$ref" in node):
            return node
        ref = node["$ref"]
        assert ref.startswith("#/"), f"$ref externo no OpenAPI publicado: {ref}"
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    raise AssertionError("ciclo de $ref no OpenAPI publicado")


def _operations(doc: dict) -> dict[tuple[str, str], tuple[dict, list]]:
    """(MÉTODO, caminho) → (operação, parâmetros do item de caminho)."""
    ops: dict[tuple[str, str], tuple[dict, list]] = {}
    for path, item in doc.get("paths", {}).items():
        item = _deref(item, doc)
        for method in HTTP_METHODS:
            if method in item:
                ops[(method.upper(), path)] = (item[method], item.get("parameters", []))
    return ops


def _operation(doc: dict, op_id: str) -> tuple[dict, list]:
    entry = next(op for op in CATALOG if op["id"] == op_id)
    ops = _operations(doc)
    key = (entry["method"], entry["path"])
    assert key in ops, f"{op_id}: {key} ausente do OpenAPI publicado"
    return ops[key]


def _parameters(doc: dict, op: dict, path_params: list) -> list[dict]:
    return [_deref(p, doc) for p in [*path_params, *op.get("parameters", [])]]


def _json_schema(doc: dict, container: dict, label: str) -> dict:
    content = _deref(container, doc).get("content", {})
    assert "application/json" in content, f"{label}: sem conteúdo application/json ({sorted(content)})"
    return content["application/json"]["schema"]


def _responses(doc: dict, op: dict) -> dict[str, dict]:
    return {status: _deref(resp, doc) for status, resp in op.get("responses", {}).items()}


def _roots(doc: dict) -> list[tuple[str, str, Any]]:
    """Todos os esquemas alcançáveis, como (tipo, onde, esquema). Tipos: `parameter`, `request`,
    `response`, `header` e `component`."""
    roots: list[tuple[str, str, Any]] = []
    for (method, path), (op, path_params) in _operations(doc).items():
        where = f"{method} {path}"
        for param in _parameters(doc, op, path_params):
            if "schema" in param:
                roots.append(("parameter", f"{where} parâmetro {param.get('name')}", param["schema"]))
            for media, body in param.get("content", {}).items():
                roots.append(("parameter", f"{where} parâmetro {param.get('name')} {media}",
                              body.get("schema", {})))
        if "requestBody" in op:
            for media, body in _deref(op["requestBody"], doc).get("content", {}).items():
                roots.append(("request", f"{where} pedido {media}", body.get("schema", {})))
        for status, resp in _responses(doc, op).items():
            for media, body in resp.get("content", {}).items():
                roots.append(("response", f"{where} {status} {media}", body.get("schema", {})))
            for name, header in resp.get("headers", {}).items():
                header = _deref(header, doc)
                if "schema" in header:
                    roots.append(("header", f"{where} {status} cabeçalho {name}", header["schema"]))
    for name, schema in doc.get("components", {}).get("schemas", {}).items():
        roots.append(("component", f"components.schemas.{name}", schema))
    return roots


def _free_nodes(schema: Any, doc: dict, where: str) -> list[str]:
    """Subesquemas sem forma (aceitam qualquer valor, inclusive um dicionário livre). Ramos de
    combinadores de um pai com `type` só refinam o pai e ficam isentos."""
    found: list[str] = []

    def walk(node: Any, path: str, refines_typed_parent: bool, depth: int) -> None:
        assert depth < 64, f"{where}: esquema profundo demais (ciclo de $ref?)"
        node = _deref(node, doc)
        if not isinstance(node, dict):
            return
        if not refines_typed_parent and not any(k in node for k in _SHAPING_KEYS):
            found.append(f"{where} {path or '<raiz>'}")
        typed = "type" in node or refines_typed_parent
        for key in ("allOf", "anyOf", "oneOf"):
            for branch in node.get(key, []) or []:
                walk(branch, path, typed, depth + 1)
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}" if path else name, False, depth + 1)
        for i, child in enumerate(node.get("prefixItems", []) or []):
            walk(child, f"{path}[{i}]", False, depth + 1)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]", False, depth + 1)
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], f"{path}{{*}}", False, depth + 1)

    walk(schema, "", False, 0)
    return found


def _all_refs(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                refs.append(value)
            else:
                refs.extend(_all_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_all_refs(item))
    return refs


def _paths_only(flat: set[tuple[str, frozenset[str]]]) -> set[str]:
    return {path for path, _ in flat}


# --- OpenAPI 3.1.0 ------------------------------------------------------------------------------

def test_openapi_is_3_1_0_and_public(client: Any) -> None:
    """`/openapi.json` sem token, OpenAPI 3.1.0."""
    doc = _published(client)
    assert doc["openapi"] == "3.1.0"


# --- todo objeto alcançável fechado --------------------------------------------------------------

def test_every_reachable_object_is_closed(client: Any) -> None:
    """todo objeto de pedidos, respostas e parâmetros (e todo
    componente) tem `additionalProperties: false`, e nenhum subesquema fica sem forma (dicionário
    livre).

    Oferecimentos e bots: os 7 pedidos (`REQUEST_OPS`, com os 4 novos) e as
    respostas das 19 operações entram na checagem."""
    doc = _published(client)
    roots = _roots(doc)
    kinds = [kind for kind, _, _ in roots]
    assert kinds.count("request") >= len(REQUEST_OPS), f"pedidos no OpenAPI: {kinds.count('request')}"
    assert kinds.count("response") >= len(OP_IDS), f"respostas no OpenAPI: {kinds.count('response')}"
    open_objects: list[str] = []
    free: list[str] = []
    for _, where, schema in roots:
        for obj in iter_objects(schema, doc):
            if obj.get("additionalProperties") is not False:
                open_objects.append(f"{where}: {obj.get('title') or sorted(obj.get('properties', {}))}")
        free.extend(_free_nodes(schema, doc, where))
    assert open_objects == [], f"objetos abertos: {open_objects}"
    assert free == [], f"subesquemas sem forma (dicionário livre): {free}"


# --- marcas nas respostas exatamente como no contrato --------------------------------------------

def test_response_markers_exactly_as_contract(client: Any) -> None:
    """`x-identity` só na resposta da revelação, `x-secret` só no
    `secret` de `tokens.create` e `x-free-text` exatamente em `label`, `reason` e `error`.

    Oferecimentos e bots: nas respostas, também
    `x-free-text` em `course`/`term` e `x-secret` no `link` de `team.link`; em pedidos e parâmetros,
    `x-secret` exatamente no `token` do pedido de `bots.register` (pares operação × campo).

    Relatórios: as marcas do `Report` (`EXPECTED_RESPONSE_MARKS`) e o 200
    de `reports.get` com exatamente as duas mídias, `application/json` e `text/csv`, esta com o esquema
    `{"type": "string", "x-free-text": true, "x-untrusted": true}` (a marca na raiz, que a forma achatada
    não alcança). Vermelho: a rota não existe no OpenAPI publicado."""
    doc = _published(client)
    found: set[tuple[str, str, str, str]] = set()
    secret_in_requests: list[tuple[str, str]] = []
    for entry in CATALOG:
        op, path_params = _operation(doc, entry["id"])
        for status, resp in _responses(doc, op).items():
            for body in resp.get("content", {}).values():
                for path, marks in flatten(body.get("schema", {}), doc):
                    for mark in marks:
                        found.add((entry["id"], status, path, mark))
        params = _parameters(doc, op, path_params)
        secret_in_requests += [(entry["id"], str(p.get("name"))) for p in params if p.get("x-secret") is True]
        inputs = [p.get("schema", {}) for p in params]
        if "requestBody" in op:
            inputs += [b.get("schema", {}) for b in _deref(op["requestBody"], doc).get("content", {}).values()]
        for schema in inputs:
            secret_in_requests += [(entry["id"], p) for p, marks in flatten(schema, doc) if "x-secret" in marks]
    assert found == EXPECTED_RESPONSE_MARKS
    assert secret_in_requests == [("bots.register", "token")]
    # as duas mídias do 200 de `reports.get` e o esquema marcado da mídia CSV.
    report_op, _ = _operation(doc, "reports.get")
    content = _responses(doc, report_op)["200"].get("content", {})
    assert set(content) == {"application/json", "text/csv"}, sorted(content)
    assert _deref(content["text/csv"].get("schema", {}), doc) == REPORT_CSV_SCHEMA


# --- sem 422 nem componentes de validação ------------------------------------------------------

def test_no_422_and_no_validation_error_components(client: Any) -> None:
    """os erros de validação usam o envelope 400; o OpenAPI não anuncia 422 nem
    os componentes `ValidationError`/`HTTPValidationError`."""
    doc = _published(client)
    ops = _operations(doc)
    assert ops, "OpenAPI sem operações"
    with_422 = [f"{m} {p}" for (m, p), (op, _) in ops.items() if "422" in op.get("responses", {})]
    assert with_422 == []
    components = set(doc.get("components", {}).get("schemas", {}))
    assert components.isdisjoint({"ValidationError", "HTTPValidationError"}), components
    bad_refs = [r for r in _all_refs(doc) if r.rsplit("/", 1)[-1] in ("ValidationError", "HTTPValidationError")]
    assert bad_refs == []


# --- paths × métodos igual ao catálogo ---------------------------------------------------------

def test_paths_and_methods_match_catalog(client: Any) -> None:
    """o conjunto (método, rota) publicado, tirando `/openapi.json`,
    é igual ao do catálogo."""
    doc = _published(client)
    published = set(_operations(doc)) - {("GET", "/openapi.json")}
    assert published == {(op["method"], op["path"]) for op in CATALOG}


# --- forma achatada das respostas de sucesso e do pedido de people.register ------------------

@pytest.mark.parametrize("op_id", OP_IDS)
def test_success_response_flatten_matches_frozen(client: Any, op_id: str) -> None:
    """a resposta de sucesso publicada coincide, na forma achatada, com o
    arquivo congelado; é o único status 2xx anunciado (a saúde 503 usa o mesmo esquema).

    Oferecimentos e bots: as 10 operações novas, com 201 em `offerings.create` e
    `team.link` e 200 nas demais; `events.list` com os 14 tipos de evento novos."""
    doc = _published(client)
    op, _ = _operation(doc, op_id)
    responses = _responses(doc, op)
    success = SUCCESS_STATUS[op_id]
    assert {s for s in responses if s.startswith("2")} == {success}
    expected = flatten(load_schema(op_id, "response"))
    assert expected, f"forma achatada vazia no arquivo congelado de {op_id}"
    assert flatten(_json_schema(doc, responses[success], f"{op_id} {success}"), doc) == expected
    if op_id == "system.health" and "503" in responses:
        assert flatten(_json_schema(doc, responses["503"], "system.health 503"), doc) == expected


def test_people_register_request_flatten_matches_frozen(client: Any) -> None:
    """o pedido de `people.register` publicado coincide com o
    congelado na forma achatada, com `x-identity` em `name`/`ra`/`email` (indício do tipo
    compartilhado de identidade)."""
    doc = _published(client)
    op, _ = _operation(doc, "people.register")
    assert "requestBody" in op, "people.register sem requestBody no OpenAPI"
    schema = _json_schema(doc, op["requestBody"], "people.register pedido")
    expected = flatten(load_schema("people.register", "request"))
    assert expected == {("name", frozenset({"x-identity:name"})), ("ra", frozenset({"x-identity:ra"})),
                        ("email", frozenset({"x-identity:email"}))}
    assert flatten(schema, doc) == expected


def test_students_import_request_flatten_matches_frozen(client: Any) -> None:
    """Vínculo do aluno: o pedido de `students.import`
    publicado coincide com o congelado na forma achatada, com `x-identity` em `rows[].name`,
    `rows[].ra` e `rows[].email` (as 3 marcas por tipo, como no pedido de `people.register`) e sem
    nenhuma outra marca. O arquivo congelado é conferido antes (já vale no esqueleto); a
    comparação com o publicado fica vermelha no esqueleto porque a rota
    não existe."""
    expected = flatten(load_schema("students.import", "request"))
    assert expected == {
        ("rows", frozenset()),
        ("rows[]", frozenset()),
        ("rows[].name", frozenset({"x-identity:name"})),
        ("rows[].ra", frozenset({"x-identity:ra"})),
        ("rows[].email", frozenset({"x-identity:email"})),
    }
    doc = _published(client)
    op, _ = _operation(doc, "students.import")
    assert "requestBody" in op, "students.import sem requestBody no OpenAPI"
    schema = _json_schema(doc, op["requestBody"], "students.import pedido")
    assert flatten(schema, doc) == expected


def test_request_bodies_match_frozen_fields(client: Any) -> None:
    """Só `people.register`, `identity.reveal` e `tokens.create` anunciam
    corpo, com exatamente os campos dos pedidos congelados (as marcas de pedido não são
    comparadas). O canário deriva deste OpenAPI as superfícies de corpo.

    Oferecimentos e bots: também
    `offerings.create`, `offerings.archive`, `bots.register` e `team.link`, com os campos exatos;
    as demais operações novas (inclusive `offerings.close`, `offerings.prepare_archive` e
    `bots.activate`, que são POST) não anunciam corpo."""
    doc = _published(client)
    for op_id in OP_IDS:
        op, _ = _operation(doc, op_id)
        if op_id not in REQUEST_OPS:
            assert "requestBody" not in op, f"{op_id} anuncia corpo"
            continue
        assert "requestBody" in op, f"{op_id} sem requestBody no OpenAPI"
        schema = _json_schema(doc, op["requestBody"], f"{op_id} pedido")
        assert _paths_only(flatten(schema, doc)) == _paths_only(flatten(load_schema(op_id, "request"))), op_id


def test_parameters_match_contract(client: Any) -> None:
    """Parâmetros de caminho e de consulta publicados de cada operação (`after`,
    `limit`, `types`, `ref`, `token_id`). O canário deriva deles as superfícies de consulta
    e de caminho.

    Oferecimentos e bots: `ref` no caminho das 8 operações com `{ref}`
    (código ou `offering_id`) e `after`/`limit` na consulta de `offerings.list`."""
    doc = _published(client)
    found: dict[str, set[tuple[str, str]]] = {}
    for op_id in OP_IDS:
        op, path_params = _operation(doc, op_id)
        found[op_id] = {(p["in"], p["name"]) for p in _parameters(doc, op, path_params)
                        if p.get("in") in ("path", "query")}
    assert found == EXPECTED_PARAMETERS
