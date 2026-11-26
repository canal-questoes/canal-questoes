"""Contratos do banco de questões local: os arquivos de contrato das duas operações novas (`banks.upload`,
`POST /v1/question-banks`, e `banks.list`, `GET /v1/question-banks`) e o que a implementação publica e serve.

Os arquivos de contrato já são os reais desde o esqueleto. Por isso este arquivo tem dois tipos de caso:
- **casos de higiene**, que passam desde o esqueleto e conferem só a forma dos arquivos: os três esquemas novos
  da API e a variante `bank.uploaded` do `events.list`, campo a campo (fechados, com os obrigatórios, os padrões e
  os limites, mais amostras aceitas e recusadas); as duas entradas de `tools.json` (as anotações, a frase de
  conteúdo não confiável e os esquemas de entrada); as cópias byte a byte do pacote do cliente; os 5 motivos novos
  no enum de `fields[].reason` do envelope de erro, com o `Reason` do servidor e o `_ENVELOPE_REASONS` do MCP
  iguais ao enum (nenhum outro teste confere essa igualdade); a forma do arquivo de vetores da versão;
- **casos vermelhos**, um por operação: a operação servida de verdade pelo `TestClient` (o envio de um banco
  válido e a listagem) confere com o esquema de resposta do contrato. Na mesma função ficam as conferências
  estáticas sobre o que o esqueleto já registra: a entrada do catálogo e a ordem no fim, o registro
  `cq_server.api.operations.BY_ID`, o OpenAPI publicado (o corpo JSON só no envio, os parâmetros da listagem e os
  erros publicados `invalid_request`, `unauthenticated`, `forbidden` e `internal`), a árvore do CLI, as tabelas
  do MCP e, na listagem, o `maxItems` do `types` do `list_events` igual ao número de tipos de evento.

Vermelho no esqueleto: as duas rotas lançam `NotImplementedError` logo depois da camada comum, e a API responde
500 `internal` a toda chamada autenticada.

**[I]** Escolhas deste arquivo:
- o banco servido é o curso de fixture `tests/fixtures/repos/course`, empacotado pelo auxiliar de teste (sem o
  `cq`), com a versão e a contagem de questões calculadas pelo próprio auxiliar;
- as duas cópias de contrato que já mudavam antes (a resposta do `events.list` e a entrada do `list_events`)
  entram na conferência byte a byte junto das cinco cópias novas;
- a forma exata dos esquemas é comparada sem `$schema`, `$id` e `title`, conferidos à parte.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, get_args

import click
from jsonschema import Draft202012Validator

from tests.contract.test_catalog import _param_shape
from tests.contract.test_contract_dispatch import _deref, _published_operation
from tests.contract.test_contract_reports import _event_variants
from tests.helpers.api import ADMIN_TOKEN, auth
from tests.helpers.banks import (
    archive_of,
    course_files,
    load_vectors,
    reference_question_count,
    reference_version,
    upload_body,
    vector_files,
)
from tests.helpers.contract import REPO, SCHEMAS, assert_matches, load_catalog, load_json

CONTRACTS = REPO / "contracts"
MCP_V1 = CONTRACTS / "mcp" / "v1"
COPIES_DIR = REPO / "client" / "src" / "cq_client" / "mcp_contract"

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
ID_PREFIX = "https://canal-questoes.invalid/contracts/api/v1/schemas/"

# Os padrões do contrato (o nome é o do código do oferecimento).
NAME_PATTERN = "^[a-z0-9][a-z0-9-]{2,39}$"
VERSION_PATTERN = "^[0-9a-f]{64}$"
COMMIT_PATTERN = "^([0-9a-f]{40}|[0-9a-f]{64})$"
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
BASE64_PATTERN = "^[A-Za-z0-9+/]*={0,2}$"
REVISION_TEXT = "^[A-Za-z0-9_./@{}~^][A-Za-z0-9_./@{}~^-]*$"

# Os tetos fixos: arquivos, conteúdo (100 MiB) e o base64 do pacote de 128 MiB.
MAX_FILES = 20000
MAX_CONTENT_BYTES = 104857600
MAX_ARCHIVE_BASE64 = 178956972

BANK_OPERATIONS = ("banks.upload", "banks.list")
BANK_PATH = "/v1/question-banks"
# As entradas do catálogo, exatas, no fim, depois de `offerings.purge`.
CATALOG_ENTRIES = [
    {"id": "banks.upload", "method": "POST", "path": BANK_PATH, "permission": "offerings:write",
     "cli": "banks upload", "mcp": {"exposed": True, "tool": "upload_question_bank", "confirm": False},
     "destructive": False},
    {"id": "banks.list", "method": "GET", "path": BANK_PATH, "permission": "offerings:read",
     "cli": "banks list", "mcp": {"exposed": True, "tool": "list_question_banks", "confirm": False},
     "destructive": False},
]
PREVIOUS_LAST_OPERATION = "offerings.purge"
# Os erros publicados de toda operação de `/v1` (nenhum código novo).
PUBLISHED_ERRORS = {"400": "invalid_request", "401": "unauthenticated", "403": "forbidden", "500": "internal"}

# Os motivos por campo: os 10 de antes e os 5 novos, no fim e nesta ordem.
PREVIOUS_REASONS = ("missing", "unknown_field", "invalid_format", "too_short", "too_long", "too_few", "too_many",
                    "out_of_range", "invalid_value", "duplicate")
NEW_REASONS = ("too_large", "path_not_allowed", "entry_not_allowed", "not_a_course", "mismatch")

# Os subesquemas comuns.
_NAME_REQUEST = {"type": "string", "minLength": 3, "maxLength": 40, "pattern": NAME_PATTERN}
_FREE_TEXT = {"type": "string", "minLength": 1, "x-free-text": True}
_VERSION = {"type": "string", "pattern": VERSION_PATTERN}
_SOURCE = {"enum": ["folder", "git"]}
_COMMIT = {"type": ["string", "null"], "pattern": COMMIT_PATTERN}
_TIME = {"type": "string", "pattern": TIME_PATTERN}

BANK_VERSION_FIELDS = ("name", "version", "repo", "source", "git_commit", "file_count", "byte_count",
                       "question_count", "created_at")
BANK_VERSION_PROPERTIES = {
    "name": _FREE_TEXT,
    "version": _VERSION,
    "repo": _FREE_TEXT,
    "source": _SOURCE,
    "git_commit": _COMMIT,
    "file_count": {"type": "integer", "minimum": 1, "maximum": MAX_FILES},
    "byte_count": {"type": "integer", "minimum": 0, "maximum": MAX_CONTENT_BYTES},
    "question_count": {"type": "integer", "minimum": 0, "maximum": MAX_FILES},
    "created_at": _TIME,
}

UPLOAD_REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "source", "archive"],
    "properties": {
        "name": _NAME_REQUEST,
        "version": _VERSION,
        "source": _SOURCE,
        "git_commit": _COMMIT,
        "archive": {"type": "string", "minLength": 4, "maxLength": MAX_ARCHIVE_BASE64, "pattern": BASE64_PATTERN,
                    "contentEncoding": "base64", "contentMediaType": "application/gzip"},
    },
    "if": {"properties": {"source": {"const": "git"}}},
    "then": {"required": ["git_commit"], "properties": {"git_commit": {"type": "string"}}},
    "else": {"properties": {"git_commit": {"type": "null"}}},
}
UPLOAD_RESPONSE = {
    "type": "object",
    "additionalProperties": False,
    "required": [*BANK_VERSION_FIELDS, "created"],
    "properties": {**BANK_VERSION_PROPERTIES, "created": {"type": "boolean"}},
}
LIST_RESPONSE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "next_cursor"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": list(BANK_VERSION_FIELDS),
                                             "properties": BANK_VERSION_PROPERTIES}},
        "next_cursor": {"type": "integer", "minimum": 0},
    },
}
# Os dados do evento novo: o nome do banco no campo `bank`; sem caminho local, nome de arquivo nem conteúdo.
BANK_UPLOADED_DATA = ("bank", "version", "source", "git_commit", "file_count", "byte_count", "question_count")

# As ferramentas do MCP, no fim das ferramentas do catálogo (antes das locais).
TOOL_ENTRIES = {
    "upload_question_bank": {
        "source": {"kind": "catalog", "operation": "banks.upload"},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "requires_user_interaction": False,
        "input_schema": "schemas/upload_question_bank.input.schema.json",
        "output_schema": "../../api/v1/schemas/banks.upload.response.schema.json",
    },
    "list_question_banks": {
        "source": {"kind": "catalog", "operation": "banks.list"},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": False},
        "requires_user_interaction": False,
        "input_schema": "schemas/list_question_banks.input.schema.json",
        "output_schema": "../../api/v1/schemas/banks.list.response.schema.json",
    },
}
UPLOAD_TOOL_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "name"],
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "name": _NAME_REQUEST,
        "git_rev": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": REVISION_TEXT},
    },
}
LIST_TOOL_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": _NAME_REQUEST,
        "after": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
}

# As cópias do pacote do cliente (relativas a `contracts/`): as cinco novas e as duas que mudaram junto.
NEW_COPIES = (
    "mcp/v1/tools.json",
    "mcp/v1/schemas/upload_question_bank.input.schema.json",
    "mcp/v1/schemas/list_question_banks.input.schema.json",
    "api/v1/schemas/banks.upload.response.schema.json",
    "api/v1/schemas/banks.list.response.schema.json",
)
UPDATED_COPIES = ("api/v1/schemas/events.list.response.schema.json", "mcp/v1/schemas/list_events.input.schema.json")

# Os vetores da versão: os quatro casos e as chaves de cada um.
VECTOR_CASES = ("minimo", "ordem_por_bytes", "um_byte", "aninhada")
VECTOR_CASE_KEYS = {"id", "description", "files", "question_count", "version"}
VECTOR_FILE_KEYS = {"path", "content_base64"}

# As formas das folhas novas do CLI (o formato de `_param_shape`, em ordem de `repr`).
CLI_LEAVES = {
    "banks upload": [("argument", "PASTA", True, 1), ("option", ("--git",), (), False, False, False, 1),
                     ("option", ("--name",), (), True, False, False, 1)],
    "banks list": [("option", ("--after",), (), False, False, False, 1),
                   ("option", ("--limit",), (), False, False, False, 1),
                   ("option", ("--name",), (), False, False, False, 1)],
}

BANK_NAME = "banco-contrato"
OTHER_BANK_NAME = "outro-banco-contrato"


# --------------------------------------------------------------------------------------------------
# Ajudantes
# --------------------------------------------------------------------------------------------------


def _load(name: str) -> Any:
    return load_json(str(SCHEMAS / name))


def _without_ids(schema: dict) -> dict:
    return {key: value for key, value in schema.items() if key not in ("$schema", "$id", "title")}


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def _assert_header(schema: dict, name: str) -> None:
    assert schema["$schema"] == DRAFT_2020_12, name
    assert schema["$id"] == ID_PREFIX + name, name
    Draft202012Validator.check_schema(schema)


def _sample_version(**fields: Any) -> dict:
    sample = {"name": BANK_NAME, "version": "a" * 64, "repo": f"bank:{BANK_NAME}", "source": "folder",
              "git_commit": None, "file_count": 1, "byte_count": 0, "question_count": 0,
              "created_at": "2031-01-01T00:00:00Z"}
    sample.update(fields)
    return sample


def _tools() -> list[dict]:
    return json.loads((MCP_V1 / "tools.json").read_text(encoding="utf-8"))["tools"]


def _leaf(path: str) -> click.Command:
    from cq_client.cli import cli

    command: click.Command = cli
    for name in path.split():
        assert isinstance(command, click.Group), path
        child = command.get_command(click.Context(command), name)
        assert child is not None, f"folha ausente da árvore do cq: {path}"
        command = child
    assert not isinstance(command, click.Group), f"{path} não é folha"
    return command


def _published(client: Any) -> dict:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200, resp.status_code
    return resp.json()


def _published_errors(doc: dict, op_id: str) -> dict[str, str]:
    """Status de erro publicado → o código citado no começo da descrição (`` `código`: ``)."""
    responses = _published_operation(doc, op_id).get("responses", {})
    out: dict[str, str] = {}
    for status, response in responses.items():
        if status.startswith("2"):
            continue
        match = re.match(r"`([a-z_]+)`", _deref(response, doc).get("description", ""))
        out[status] = match.group(1) if match else ""
    return out


def _assert_static_operation(doc: dict, op_id: str) -> None:
    """A entrada do catálogo, o registro e o OpenAPI publicado de `op_id` (o que o esqueleto já tem)."""
    from cq_server.api.operations import BY_ID

    operations = load_catalog()["operations"]
    ids = [op["id"] for op in operations]
    assert ids[-2:] == list(BANK_OPERATIONS)
    assert ids[-3] == PREVIOUS_LAST_OPERATION
    entry = next(op for op in operations if op["id"] == op_id)
    assert entry == next(e for e in CATALOG_ENTRIES if e["id"] == op_id)

    registered = BY_ID[op_id]
    assert (registered.method, registered.path) == (entry["method"], entry["path"])
    assert registered.permission.value == entry["permission"]

    assert _published_errors(doc, op_id) == PUBLISHED_ERRORS
    success = [s for s in _published_operation(doc, op_id).get("responses", {}) if s.startswith("2")]
    assert success == ["200"], (op_id, success)


def _upload(client: Any, name: str, files: dict[str, bytes], *, source: str = "folder",
            git_commit: str | None = None) -> Any:
    body = upload_body(name, reference_version(files), source, git_commit, archive_of(files))
    return client.post(BANK_PATH, json=body, headers=auth(ADMIN_TOKEN))


def _assert_bank(body: dict, name: str, files: dict[str, bytes], *, source: str, git_commit: str | None) -> None:
    assert body["name"] == name
    assert body["repo"] == f"bank:{name}"
    assert body["version"] == reference_version(files)
    assert body["source"] == source
    assert body["git_commit"] == git_commit
    assert body["file_count"] == len(files)
    assert body["byte_count"] == sum(len(data) for data in files.values())
    assert body["question_count"] == reference_question_count(files)


# --------------------------------------------------------------------------------------------------
# Casos de higiene: a forma dos arquivos de contrato
# --------------------------------------------------------------------------------------------------


def test_bank_request_schema_matches_contract() -> None:
    """O pedido de `banks.upload`, plano e fechado: `name` no padrão do código do oferecimento, `version` com 64
    dígitos hexadecimais minúsculos, `source` `folder` ou `git`, `git_commit` com 40 ou 64 dígitos (obrigatório e não
    nulo com `git`; ausente ou nulo com `folder`) e `archive` em base64 padrão, até o base64 de 128 MiB."""
    name = "banks.upload.request.schema.json"
    schema = _load(name)
    _assert_header(schema, name)
    assert _without_ids(schema) == UPLOAD_REQUEST

    archive = "H4sIAAAAAAAA"
    folder = {"name": BANK_NAME, "version": "0" * 64, "source": "folder", "archive": archive}
    accepted = [
        folder,
        {**folder, "git_commit": None},
        {**folder, "source": "git", "git_commit": "1" * 40},
        {**folder, "source": "git", "git_commit": "f" * 64},
    ]
    refused = [
        {**folder, "source": "git"},
        {**folder, "source": "git", "git_commit": None},
        {**folder, "git_commit": "1" * 40},
        {**folder, "extra": 1},
        {**folder, "name": "ab"},
        {**folder, "name": "-abc"},
        {**folder, "name": "Abc"},
        {**folder, "version": "A" * 64},
        {**folder, "version": "0" * 63},
        {**folder, "source": "zip"},
        {**folder, "source": "git", "git_commit": "1" * 41},
        {**folder, "archive": "abc"},
        {**folder, "archive": "não é base64"},
        {key: value for key, value in folder.items() if key != "archive"},
    ]
    assert [body for body in accepted if not _is_valid(schema, body)] == []
    assert [body for body in refused if _is_valid(schema, body)] == []


def test_bank_response_schemas_and_event_variant_match_contract() -> None:
    """As respostas de `banks.upload` (o `BankVersion` mais `created`) e de `banks.list` (`items` de `BankVersion`
    sem `created` e `next_cursor`), fechadas e com todo campo obrigatório; `name` e `repo` com `x-free-text`, sem
    `pattern`; nenhuma marca de identidade nem de segredo. A variante `bank.uploaded` do `events.list`, fechada,
    com exatamente os 7 dados do contrato, todos obrigatórios, e o `bank` (o nome do banco) com `x-free-text`."""
    upload_name = "banks.upload.response.schema.json"
    list_name = "banks.list.response.schema.json"
    upload = _load(upload_name)
    listing = _load(list_name)
    _assert_header(upload, upload_name)
    _assert_header(listing, list_name)
    assert _without_ids(upload) == UPLOAD_RESPONSE
    assert _without_ids(listing) == LIST_RESPONSE
    for text in (json.dumps(upload), json.dumps(listing)):
        assert "x-identity" not in text and "x-secret" not in text

    version = _sample_version()
    assert _is_valid(upload, {**version, "created": True})
    assert _is_valid(upload, {**version, "source": "git", "git_commit": "c" * 40, "created": False})
    assert not _is_valid(upload, version)
    assert not _is_valid(upload, {**version, "created": True, "extra": 1})
    assert not _is_valid(upload, {**version, "file_count": 0, "created": True})
    assert not _is_valid(upload, {**version, "created_at": "2031-01-01T00:00:00+00:00", "created": True})
    assert _is_valid(listing, {"items": [version], "next_cursor": 7})
    assert _is_valid(listing, {"items": [], "next_cursor": 0})
    assert not _is_valid(listing, {"items": [{**version, "created": True}], "next_cursor": 7})
    assert not _is_valid(listing, {"items": [], "next_cursor": None})

    variant = _event_variants()["bank.uploaded"]
    data = variant["properties"]["data"]
    assert data.get("additionalProperties") is False
    assert sorted(data["required"]) == sorted(BANK_UPLOADED_DATA)
    assert set(data["properties"]) == set(BANK_UPLOADED_DATA)
    assert data["properties"]["bank"].get("x-free-text") is True
    assert "x-identity" not in json.dumps(data) and "x-secret" not in json.dumps(data)


def test_bank_error_reasons_match_server_and_mcp_envelope() -> None:
    """O enum de `fields[].reason` do envelope de erro: os 10 motivos de antes e os 5 novos no fim, nesta ordem; o
    `Reason` de `cq_server.api.schemas` e o `_ENVELOPE_REASONS` de `cq_client.mcp_server` iguais a ele (o servidor
    não publica um motivo que o MCP recusaria ao ler o envelope)."""
    from cq_client.mcp_server import _ENVELOPE_REASONS
    from cq_server.api.schemas import Reason

    schema = _load("error.schema.json")
    enum = schema["properties"]["error"]["properties"]["fields"]["items"]["properties"]["reason"]["enum"]
    assert enum == [*PREVIOUS_REASONS, *NEW_REASONS]
    assert set(get_args(Reason)) == set(enum)
    assert set(_ENVELOPE_REASONS) == set(enum)


def test_bank_tools_and_input_schemas_match_contract() -> None:
    """As duas ferramentas de `tools.json`, logo depois de `purge_offering` e antes das ferramentas locais: a fonte,
    as anotações (o envio não é só leitura, não é destrutivo, é idempotente e não fala com o mundo; a listagem só
    lê), sem interação do operador, os esquemas de entrada e de saída; as duas descrições terminam com a frase do
    conteúdo não confiável (as saídas têm `x-free-text`). Os esquemas de entrada, exatos: o `name` igual ao do
    pedido da API; o `path` de até 4096 caracteres; o `git_rev` no padrão único da revisão."""
    from cq_client.mcp_server import TOOL_UNTRUSTED_SENTENCE

    tools = _tools()
    names = [tool["name"] for tool in tools]
    start = names.index("purge_offering") + 1
    assert names[start:start + 2] == list(TOOL_ENTRIES)
    assert all(tool["source"]["kind"] == "local" for tool in tools[start + 2:])
    for tool in tools[start:start + 2]:
        expected = TOOL_ENTRIES[tool["name"]]
        assert {key: tool[key] for key in expected} == expected, tool["name"]
        assert tool["title"].strip(), tool["name"]
        assert tool["description"].endswith(TOOL_UNTRUSTED_SENTENCE), tool["name"]

    upload = load_json(str(MCP_V1 / "schemas" / "upload_question_bank.input.schema.json"))
    listing = load_json(str(MCP_V1 / "schemas" / "list_question_banks.input.schema.json"))
    for schema in (upload, listing):
        assert schema["$schema"] == DRAFT_2020_12
        Draft202012Validator.check_schema(schema)
    assert _without_ids(upload) == UPLOAD_TOOL_INPUT
    assert _without_ids(listing) == LIST_TOOL_INPUT
    request_name = _load("banks.upload.request.schema.json")["properties"]["name"]
    assert upload["properties"]["name"] == request_name == listing["properties"]["name"]

    assert _is_valid(upload, {"path": "C:/bancos/curso", "name": BANK_NAME, "git_rev": "HEAD~1"})
    assert _is_valid(upload, {"path": "/srv/curso", "name": BANK_NAME, "git_rev": "v1.0^{commit}"})
    assert not _is_valid(upload, {"path": "/srv/curso", "name": BANK_NAME, "git_rev": "-c"})
    assert not _is_valid(upload, {"path": "/srv/curso", "name": BANK_NAME, "git_rev": "a:b"})
    assert not _is_valid(upload, {"path": "/srv/curso", "name": BANK_NAME, "git_rev": "r" * 257})
    assert not _is_valid(upload, {"name": BANK_NAME})
    assert _is_valid(listing, {})
    assert not _is_valid(listing, {"limit": 0})


def test_bank_contract_copies_in_client_package_match_byte_for_byte() -> None:
    """As cópias do pacote do cliente, que o wheel leva: `tools.json`, os dois esquemas de entrada novos e as duas
    respostas novas, mais as duas cópias que mudaram junto (a resposta do `events.list`, com o tipo novo, e a
    entrada do `list_events`), byte a byte iguais a `contracts/`."""
    different = []
    for rel in (*NEW_COPIES, *UPDATED_COPIES):
        source = CONTRACTS.joinpath(*rel.split("/"))
        copy = COPIES_DIR.joinpath(*rel.split("/"))
        assert source.is_file(), rel
        assert copy.is_file(), f"cópia ausente: {rel}"
        if copy.read_bytes() != source.read_bytes():
            different.append(rel)
    assert different == []


def test_version_vectors_file_shape() -> None:
    """O arquivo de vetores da versão: o formato `cq-bank-v1`, a definição, os quatro casos na ordem, cada um com o
    identificador, a descrição, os arquivos (caminho e conteúdo em base64), a contagem de questões e a versão de 64
    dígitos; a versão e a contagem de cada caso são as que o auxiliar de teste calcula por conta própria, a versão
    de cada caso é única, e o caso `um_byte` difere do `minimo` em exatamente um byte do mesmo arquivo."""
    vectors = load_vectors()
    assert set(vectors) == {"format", "definition", "cases"}
    assert vectors["format"] == "cq-bank-v1"
    assert isinstance(vectors["definition"], str) and vectors["definition"].strip()
    assert [case["id"] for case in vectors["cases"]] == list(VECTOR_CASES)
    for case in vectors["cases"]:
        assert set(case) == VECTOR_CASE_KEYS, case["id"]
        assert case["description"].strip(), case["id"]
        assert case["files"] and all(set(item) == VECTOR_FILE_KEYS for item in case["files"]), case["id"]
        paths = [item["path"] for item in case["files"]]
        assert len(paths) == len(set(paths)), case["id"]
        assert "infoCourse.json" in paths, case["id"]
        assert re.fullmatch(VERSION_PATTERN, case["version"]), case["id"]
        files = vector_files(case)
        assert case["version"] == reference_version(files), case["id"]
        assert case["question_count"] == reference_question_count(files), case["id"]
    versions = [case["version"] for case in vectors["cases"]]
    assert len(set(versions)) == len(versions)

    cases = {case["id"]: vector_files(case) for case in vectors["cases"]}
    minimal, one_byte = cases["minimo"], cases["um_byte"]
    assert set(minimal) == set(one_byte) == {"infoCourse.json"}
    before, after = minimal["infoCourse.json"], one_byte["infoCourse.json"]
    assert len(before) == len(after)
    assert sum(1 for a, b in zip(before, after) if a != b) == 1
    assert hashlib.sha256(before).hexdigest() != hashlib.sha256(after).hexdigest()


# --------------------------------------------------------------------------------------------------
# Casos vermelhos: as operações servidas de verdade
# --------------------------------------------------------------------------------------------------


def test_bank_upload_is_registered_and_served_as_the_contract_says(client: Any) -> None:
    """O envio de um banco: o que o esqueleto já registra e, na mesma função, o envio servido de verdade.

    Estático: a entrada do catálogo, exata, nas duas últimas posições (depois de `offerings.purge`); o registro com o
    mesmo método, caminho e permissão; no OpenAPI publicado, o corpo JSON obrigatório (só `application/json`), nenhum
    parâmetro de consulta nem de caminho (só o cabeçalho `X-CQ-Client`), o 200 como único sucesso e os erros
    publicados `invalid_request`, `unauthenticated`, `forbidden` e `internal`; a folha `cq banks upload PASTA --name
    NOME [--git REVISÃO]`; as tabelas do MCP (o envio idempotente, nem só leitura, nem destrutivo, nem confirmado, nem
    de mundo aberto); o `REVISION_PATTERN` e o `REVISION_MAX_LENGTH` do cliente iguais ao `git_rev` do contrato.

    Servido: o envio do curso de fixture como pasta responde 200 com o esquema do contrato, `created: true`, o nome,
    a referência `bank:<nome>`, a versão e as contagens do conteúdo; o reenvio do mesmo conteúdo responde 200 com
    `created: false` e o mesmo primeiro envio; um envio pela origem `git` de outro nome guarda o commit dado.

    Vermelho no esqueleto: a rota lança `NotImplementedError` (500 `internal`)."""
    from cq_client.banks import REVISION_MAX_LENGTH, REVISION_PATTERN
    from cq_client.mcp_server import (
        CONFIRM_TOOLS,
        DESTRUCTIVE_TOOLS,
        IDEMPOTENT_TOOLS,
        OPEN_WORLD_TOOLS,
        READ_ONLY_TOOLS,
    )

    doc = _published(client)
    _assert_static_operation(doc, "banks.upload")
    operation = _published_operation(doc, "banks.upload")
    assert "requestBody" in operation, "banks.upload sem corpo no OpenAPI publicado"
    request_body = _deref(operation["requestBody"], doc)
    assert request_body.get("required") is True
    assert list(request_body["content"]) == ["application/json"]
    parameters = [_deref(p, doc) for p in operation.get("parameters", [])]
    assert [(p["in"], p["name"]) for p in parameters] == [("header", "X-CQ-Client")]

    assert sorted((_param_shape(p) for p in _leaf("banks upload").params), key=repr) == CLI_LEAVES["banks upload"]

    tool = "upload_question_bank"
    assert tool in IDEMPOTENT_TOOLS
    assert tool not in READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS | CONFIRM_TOOLS | OPEN_WORLD_TOOLS
    git_rev = UPLOAD_TOOL_INPUT["properties"]["git_rev"]
    assert REVISION_PATTERN.pattern == git_rev["pattern"]
    assert REVISION_MAX_LENGTH == git_rev["maxLength"]

    files = course_files()
    first = _upload(client, BANK_NAME, files)
    assert first.status_code == 200, (first.status_code, first.text[:300])
    assert_matches("banks.upload", 200, first.json())
    created = first.json()
    assert created["created"] is True
    _assert_bank(created, BANK_NAME, files, source="folder", git_commit=None)

    again = _upload(client, BANK_NAME, files)
    assert again.status_code == 200, (again.status_code, again.text[:300])
    assert_matches("banks.upload", 200, again.json())
    assert again.json() == {**created, "created": False}

    commit = "1234567890abcdef1234567890abcdef12345678"
    from_git = _upload(client, OTHER_BANK_NAME, files, source="git", git_commit=commit)
    assert from_git.status_code == 200, (from_git.status_code, from_git.text[:300])
    assert_matches("banks.upload", 200, from_git.json())
    assert from_git.json()["created"] is True
    _assert_bank(from_git.json(), OTHER_BANK_NAME, files, source="git", git_commit=commit)


def test_bank_list_is_registered_and_served_as_the_contract_says(client: Any) -> None:
    """A listagem dos bancos: o que o esqueleto já registra e, na mesma função, a listagem servida de verdade.

    Estático: a entrada do catálogo, exata; o registro; no OpenAPI publicado, nenhum corpo, os parâmetros de consulta
    `after` (0 a 2⁶³−1, padrão 0), `limit` (1 a 500, padrão 100) e `name` (no padrão do nome), todos opcionais, o
    200 como único sucesso e os erros publicados `invalid_request`, `unauthenticated`, `forbidden` e `internal`; a
    folha `cq banks list [--name] [--after] [--limit]`; a ferramenta da listagem só de leitura; o `maxItems` do
    `types` da entrada do `list_events` (e da cópia do pacote do cliente) igual ao número de variantes do
    `events.list` e ao de tipos de `cq_server.events.EventType` (o agente pede todos os tipos numa chamada só).

    Servido: sem banco enviado, a listagem responde 200 com `items` vazio e `next_cursor` 0; depois de dois envios,
    os dois itens, em ordem do primeiro envio, com o esquema do contrato (sem `created`); o filtro pelo nome exato
    devolve só o dele; a página depois do cursor vem vazia, com o mesmo cursor.

    Vermelho no esqueleto: a rota lança `NotImplementedError` (500 `internal`)."""
    from cq_client.mcp_server import READ_ONLY_TOOLS
    from cq_server.events import EventType

    doc = _published(client)
    _assert_static_operation(doc, "banks.list")
    operation = _published_operation(doc, "banks.list")
    assert "requestBody" not in operation, "banks.list anuncia corpo"
    parameters = {(p["in"], p["name"]): p for p in (_deref(q, doc) for q in operation.get("parameters", []))}
    assert set(parameters) == {("query", "after"), ("query", "limit"), ("query", "name"), ("header", "X-CQ-Client")}
    assert all(parameters[("query", n)].get("required", False) is False for n in ("after", "limit", "name"))
    after = _deref(parameters[("query", "after")]["schema"], doc)
    limit = _deref(parameters[("query", "limit")]["schema"], doc)
    name = _deref(parameters[("query", "name")]["schema"], doc)
    assert (after["type"], after["minimum"], after["maximum"], after.get("default")) == ("integer", 0, 2**63 - 1, 0)
    assert (limit["type"], limit["minimum"], limit["maximum"], limit.get("default")) == ("integer", 1, 500, 100)
    assert (name["type"], name["pattern"]) == ("string", NAME_PATTERN)

    assert sorted((_param_shape(p) for p in _leaf("banks list").params), key=repr) == CLI_LEAVES["banks list"]
    assert "list_question_banks" in READ_ONLY_TOOLS

    variants = len(_event_variants())
    assert variants == len(list(EventType))
    for base in (CONTRACTS, COPIES_DIR):
        types = load_json(str(base / "mcp" / "v1" / "schemas" / "list_events.input.schema.json"))["properties"]["types"]
        assert types["maxItems"] == variants, base

    empty = client.get(BANK_PATH, headers=auth(ADMIN_TOKEN))
    assert empty.status_code == 200, (empty.status_code, empty.text[:300])
    assert_matches("banks.list", 200, empty.json())
    assert empty.json() == {"items": [], "next_cursor": 0}

    files = course_files()
    other = {**files, "extra/leia.txt": b"conteudo de outro banco\n"}
    for bank, content in ((BANK_NAME, files), (OTHER_BANK_NAME, other)):
        resp = _upload(client, bank, content)
        assert resp.status_code == 200, (resp.status_code, resp.text[:300])

    listed = client.get(BANK_PATH, headers=auth(ADMIN_TOKEN))
    assert listed.status_code == 200, (listed.status_code, listed.text[:300])
    page = listed.json()
    assert_matches("banks.list", 200, page)
    assert [item["name"] for item in page["items"]] == [BANK_NAME, OTHER_BANK_NAME]
    _assert_bank(page["items"][0], BANK_NAME, files, source="folder", git_commit=None)
    _assert_bank(page["items"][1], OTHER_BANK_NAME, other, source="folder", git_commit=None)
    assert all("created" not in item for item in page["items"])

    only = client.get(BANK_PATH, params={"name": OTHER_BANK_NAME}, headers=auth(ADMIN_TOKEN))
    assert only.status_code == 200, (only.status_code, only.text[:300])
    assert_matches("banks.list", 200, only.json())
    assert [item["name"] for item in only.json()["items"]] == [OTHER_BANK_NAME]

    cursor = page["next_cursor"]
    rest = client.get(BANK_PATH, params={"after": cursor}, headers=auth(ADMIN_TOKEN))
    assert rest.status_code == 200, (rest.status_code, rest.text[:300])
    assert rest.json() == {"items": [], "next_cursor": cursor}
