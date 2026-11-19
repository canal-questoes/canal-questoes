"""Contrato do `tools/list` do servidor MCP: os nomes e a ordem das ferramentas, as anotações e as
descrições, os esquemas de entrada e de saída, as ferramentas locais, as mensagens fixas e as cópias dos
contratos no pacote.

Toda asserção sobre a implementação é feita sobre o **JSON** do `tools/list` real (o dump de
`tests/helpers/mcp_client.list_tools`, com `exclude_unset`: "presente" = chave no dump), lido em processo pelo
`Client` do SDK nas duas eras (`MODES`), com o `ServerDeps` de `make_deps` (sem API: o `tools/list` não chama a
API). Nenhum teste afirma "36" nem total do catálogo: os números saem da união calculada (catálogo exposto ∪
`LOCAL_TOOL_BINDINGS`).

No esqueleto, falham porque `build_server` e `load_tools` lançam `NotImplementedError`
(até a implementação) e as cópias de `client/src/cq_client/mcp_contract/` não existem (arquivo ausente). As
tabelas reais do esqueleto (`TOOL_NAMES`, `LOCAL_TOOL_BINDINGS`, os conjuntos de ferramentas do contrato, `MCP_MESSAGES`) ficam
na mesma função de uma leitura do `tools/list` real ou da conferência das cópias.

Higiene (verdes desde o início, a única exceção aceita), em funções
próprias, sobre os arquivos de contrato congelados, que já existem no esqueleto:
- `contracts/mcp/v1/tools.json` válido contra `tools.schema.json`, e este válido no 2020-12 (1 caso);
- cada arquivo de `contracts/mcp/v1/schemas/` válido no 2020-12, fechado em todo objeto e sem `$ref` (um caso
  por arquivo);
- os conjuntos de `tools.json` contra o catálogo e contra `LOCAL_COMMANDS` (1 caso).
"""

from __future__ import annotations

import inspect
import json
import posixpath
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from cq_client import profile as cq_profile
from cq_client import stack as cq_stack
from cq_client.local_commands import LOCAL_COMMANDS, PortMismatch, UpTimeout
from cq_client.mcp_server import (
    CONFIRM_TOOLS,
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_TOOLS,
    LOCAL_TOOL_BINDINGS,
    MCP_HINT_TEMPLATE,
    MCP_MESSAGES,
    MCP_STDERR_MESSAGES,
    OPEN_WORLD_TOOLS,
    READ_ONLY_TOOLS,
    REQUIRES_USER_INTERACTION_KEY,
    TOOL_CONFIRM_SENTENCE,
    TOOL_NAMES,
    TOOL_UNTRUSTED_SENTENCE,
)

from tests.helpers.contract import REPO, load_catalog
from tests.helpers.mcp_client import AUTO_MODE, MODES, in_process, list_tools, make_deps, run, scan

CONTRACTS = REPO / "contracts"
MCP_V1 = CONTRACTS / "mcp" / "v1"
MCP_SCHEMAS = MCP_V1 / "schemas"
TOOLS_JSON = MCP_V1 / "tools.json"
TOOLS_SCHEMA_JSON = MCP_V1 / "tools.schema.json"
API_SCHEMAS = CONTRACTS / "api" / "v1" / "schemas"
# as cópias dos contratos no pacote (o pacote espelha a árvore de `contracts/`).
COPIES_DIR = REPO / "client" / "src" / "cq_client" / "mcp_contract"

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
LEGACY_MODE = "legacy"

# as quatro dicas, sempre declaradas.
HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
META_KEY = "_meta"
MAX_RESULT_META_KEY = "anthropic/maxResultSizeChars"
COMBINATORS = ("anyOf", "oneOf", "allOf")
UNTRUSTED_MARKS = ("x-free-text", "x-untrusted")

# a ordem dos comandos locais no `tools/list`, depois das ferramentas do catálogo.
LOCAL_ORDER = ("status", "up", "down", "logs")

# o nome completo da ferramenta no Claude Code e o teto dele.
PLUGIN_TOOL_PREFIX = "mcp__plugin_canal-questoes_cq__"
TOOL_FULL_NAME_MAX = 128

# as formas comuns usadas nas exceções das formas de entrada das ferramentas.
PERSON_REF = {"type": "string",
              "pattern": "^(?:(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
                         "-[0-9a-f]{12})$"}

# Os campos de caminho e de consulta das ferramentas do catálogo: fora da comparação com o
# corpo do pedido da API.
PATH_FIELDS = frozenset({"offering", "dispatch_id"})
PATH_AND_QUERY_FIELDS = frozenset({"offering", "dispatch_id", "person", "after", "limit", "types", "version"})

# As exceções nomeadas das formas de entrada, uma a uma. `removed` sai do corpo; `replaced` troca o esquema do
# campo; `added` entra; `required_added` passa a obrigatório.
INPUT_FORM_EXCEPTIONS: dict[str, dict[str, Any]] = {
    "create_offering": {"removed": {"contact_email"}},
    "update_offering_settings": {"removed": {"contact_email"}, "required_added": {"free_messages"}},
    "prepare_dispatch": {"removed": {"origin"}},
    "reveal_identities": {"replaced": {
        "reason": {"type": "string", "minLength": 1},
        "pseudonyms": {"type": "array", "items": PERSON_REF, "minItems": 1, "maxItems": 100},
    }},
    "import_students": {"removed": {"rows"}, "added": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}},
                        "required_added": {"path"}},
}

# o que cada ferramenta destrutiva exige do preparo (o código, ou o `dispatch_id` do `confirm_dispatch`).
PREPARED_INPUTS = frozenset({"confirmation_code", "dispatch_id"})

# As ferramentas citadas pelas mensagens fixas: as do `UpTimeout`, do `down_code_invalid` e da
# dica da API inalcançável.
TOOLS_CITED_BY_MESSAGES = frozenset({"stack_logs", "run_doctor", "stack_status", "stack_up", "prepare_stack_down"})

# as chaves locais de mensagem fixa, além das `api.*`, `profile.*` e `stack.*`.
FIXED_LOCAL_KEYS = frozenset({"api_unreachable", "api_error", "unexpected_response", "result_too_large", "no_token",
                              "file.roster", "stack_busy", "down_code_invalid", "down_nothing_to_stop",
                              "file.not_absolute", "file.not_found", "file.not_regular", "mcp_internal"})

CLI_COMMAND_RE = re.compile(r"\bcq\s+[a-z]")
PASTE_RE = re.compile(r"\bcol(?:e|ar|ando|em)\b", re.IGNORECASE)


# --------------------------------------------------------------------------------------------------
# Ajudantes
# --------------------------------------------------------------------------------------------------


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tools_file() -> dict[str, Any]:
    return _load(TOOLS_JSON)


def _contract_rel(rel: str) -> str:
    """O caminho de um esquema de `tools.json` relativo a `contracts/`."""
    resolved = posixpath.normpath("mcp/v1/" + rel)
    assert not resolved.startswith(".."), rel
    return resolved


def _schema_file(rel: str) -> Any:
    return _load(CONTRACTS / _contract_rel(rel))


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Todo objeto JSON do esquema, com o caminho."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}/{index}")


def _is_object(node: dict[str, Any]) -> bool:
    kind = node.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def _open_objects(schema: Any) -> list[str]:
    """Os objetos do esquema sem `additionalProperties: false`."""
    return [path or "/" for path, node in _walk(schema)
            if _is_object(node) and node.get("additionalProperties") is not False]


def _with_key(schema: Any, key: str) -> list[str]:
    return [path or "/" for path, node in _walk(schema) if key in node]


def _catalog_ops() -> list[dict[str, Any]]:
    return load_catalog()["operations"]


def _catalog_exposed() -> list[dict[str, Any]]:
    return [op for op in _catalog_ops() if op["mcp"]["exposed"]]


def _local_names() -> list[str]:
    return [binding.name for command in LOCAL_ORDER for binding in LOCAL_TOOL_BINDINGS[command]]


def _expected_names() -> list[str]:
    return [op["mcp"]["tool"] for op in _catalog_exposed()] + _local_names()


def _published(tmp_path: Path, mode: str) -> list[dict[str, Any]]:
    """O `tools/list` real (o JSON) numa conexão em processo."""

    async def main() -> list[dict[str, Any]]:
        async with in_process(make_deps(tmp_path), mode) as client:
            return await list_tools(client)

    return run(main)


def _by_name(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_name = {tool["name"]: tool for tool in tools}
    assert len(by_name) == len(tools), "nomes repetidos no tools/list"
    return by_name


def _untrusted_output(schema: Any) -> bool:
    return any(_with_key(schema, mark) for mark in UNTRUSTED_MARKS)


def _subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


def _hints_dump(annotations: Any) -> dict[str, Any]:
    dumped = annotations.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return {key: dumped.get(key) for key in HINT_KEYS}


# --------------------------------------------------------------------------------------------------
# O `tools/list` real (vermelho até a implementação)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_tools_list_names_follow_catalog_then_local_tools(tmp_path: Path, mode: str) -> None:
    """Os nomes do `tools/list`: os do catálogo e, depois, os das ferramentas locais.

    O `tools/list` real tem exatamente os nomes de `TOOL_NAMES`, na ordem: os `mcp.tool` das operações do catálogo
    com `mcp.exposed: true`, na ordem do catálogo, seguidos dos nomes de `LOCAL_TOOL_BINDINGS` na ordem de
    `LOCAL_ORDER`; os dois conjuntos disjuntos; nenhuma operação com `mcp.exposed: false`; o total é o da união
    calculada. Os conjuntos do contrato conferem com a regra: `CONFIRM_TOOLS` = as `mcp.confirm: true` do catálogo
    ∪ {`stack_down`}; `DESTRUCTIVE_TOOLS` = as `destructive: true` ∪ {`stack_down`}; `READ_ONLY_TOOLS` = as GET
    expostas ∪ {`stack_status`, `stack_logs`}; `IDEMPOTENT_TOOLS` e `OPEN_WORLD_TOOLS` dentro do `tools/list`. O nome
    completo `mcp__plugin_canal-questoes_cq__<nome>` (fora de `[A-Za-z0-9_-]` trocado por `_`) tem no máximo 128
    caracteres.

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    published = [tool["name"] for tool in _published(tmp_path, mode)]

    catalog_names = [op["mcp"]["tool"] for op in _catalog_exposed()]
    local_names = _local_names()
    hidden = {op["mcp"].get("tool") for op in _catalog_ops() if not op["mcp"]["exposed"]} - {None}
    assert set(catalog_names).isdisjoint(local_names)
    assert published == _expected_names() == list(TOOL_NAMES)
    assert len(published) == len(set(catalog_names) | set(local_names))
    assert hidden.isdisjoint(published)
    assert [op["id"] for op in _catalog_ops() if not op["mcp"]["exposed"]
            and op["mcp"].get("tool") in published] == []

    by_tool = {op["mcp"]["tool"]: op for op in _catalog_exposed()}
    assert CONFIRM_TOOLS == {name for name, op in by_tool.items() if op["mcp"]["confirm"]} | {"stack_down"}
    assert DESTRUCTIVE_TOOLS == {name for name, op in by_tool.items() if op["destructive"]} | {"stack_down"}
    assert READ_ONLY_TOOLS == {name for name, op in by_tool.items() if op["method"] == "GET"} | {"stack_status",
                                                                                               "stack_logs"}
    assert IDEMPOTENT_TOOLS | OPEN_WORLD_TOOLS <= set(published)
    assert DESTRUCTIVE_TOOLS <= CONFIRM_TOOLS
    assert READ_ONLY_TOOLS.isdisjoint(DESTRUCTIVE_TOOLS)

    long_names = [name for name in published
                  if len(re.sub(r"[^A-Za-z0-9_-]", "_", PLUGIN_TOOL_PREFIX + name)) > TOOL_FULL_NAME_MAX]
    assert long_names == []


@pytest.mark.parametrize("mode", MODES)
def test_tools_list_annotations_meta_and_descriptions(tmp_path: Path, mode: str) -> None:
    """As anotações, o `_meta` e as descrições do `tools/list`.

    Em toda ferramenta do JSON do `tools/list`: `title` e `description` não vazios e iguais aos de `tools.json`
    (pt-BR); as 4 anotações **presentes**, booleanas e pela regra (`readOnlyHint` ⇔ ∈ `READ_ONLY_TOOLS`;
    `destructiveHint` ⇔ ∈ `DESTRUCTIVE_TOOLS`, com `false` declarado nas demais; `idempotentHint` ⇔ ∈
    `IDEMPOTENT_TOOLS`; `openWorldHint` ⇔ ∈ `OPEN_WORLD_TOOLS`), iguais às de `tools.json`; `_meta` presente **só**
    nas de `CONFIRM_TOOLS`, com exatamente `{"anthropic/requiresUserInteraction": true}` (booleano JSON), igual ao
    `requires_user_interaction` de `tools.json`; nenhuma declara `anthropic/maxResultSizeChars`. A frase
    `TOOL_UNTRUSTED_SENTENCE` está na descrição exatamente das ferramentas cuja saída publicada tem `x-free-text` ou
    `x-untrusted` (conjunto derivado do esquema), e a frase da aprovação, exatamente nas confirmadoras. Nenhum título
    ou descrição cita `cq ` seguido de comando ou `CQ_TOKEN`, e o `tools/list` inteiro não tem identidade canário.

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    tools = _published(tmp_path, mode)
    entries = {entry["name"]: entry for entry in _tools_file()["tools"]}
    assert [tool["name"] for tool in tools] == list(entries)

    for tool in tools:
        name = tool["name"]
        entry = entries[name]
        assert isinstance(tool.get("title"), str) and tool["title"].strip(), name
        assert isinstance(tool.get("description"), str) and tool["description"].strip(), name
        assert (tool["title"], tool["description"]) == (entry["title"], entry["description"]), name
        for text in (tool["title"], tool["description"]):
            assert CLI_COMMAND_RE.search(text) is None and "CQ_TOKEN" not in text, name

        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), f"{name}: sem anotações"
        assert [key for key in HINT_KEYS if key not in annotations] == [], name
        assert all(isinstance(annotations[key], bool) for key in HINT_KEYS), name
        expected = {"readOnlyHint": name in READ_ONLY_TOOLS, "destructiveHint": name in DESTRUCTIVE_TOOLS,
                    "idempotentHint": name in IDEMPOTENT_TOOLS, "openWorldHint": name in OPEN_WORLD_TOOLS}
        assert {key: annotations[key] for key in HINT_KEYS} == expected == entry["annotations"], name

        if name in CONFIRM_TOOLS:
            assert tool.get(META_KEY) == {REQUIRES_USER_INTERACTION_KEY: True}, name
            assert tool[META_KEY][REQUIRES_USER_INTERACTION_KEY] is True, name
        else:
            assert META_KEY not in tool, name
        assert entry["requires_user_interaction"] is (name in CONFIRM_TOOLS), name
        assert MAX_RESULT_META_KEY not in json.dumps(tool), name

        untrusted = _untrusted_output(tool["outputSchema"])
        assert (TOOL_UNTRUSTED_SENTENCE in tool["description"]) is untrusted, name
        assert (TOOL_CONFIRM_SENTENCE in tool["description"]) is (name in CONFIRM_TOOLS), name

    assert scan(tools, []) == []


@pytest.mark.parametrize("mode", MODES)
def test_tools_list_schemas_are_closed_and_equal_to_contract_files(tmp_path: Path, mode: str) -> None:
    """Os esquemas do `tools/list`, fechados e iguais aos arquivos de contrato.

    Em toda ferramenta: o `inputSchema` publicado é igual ao arquivo `input_schema` de `tools.json`, e o
    `outputSchema`, ao `output_schema` (relativo a `mcp/v1/`; os da API e do CLI sem cópia editada); ambos fechados
    em todo objeto, sem `$ref` e sem `anyOf`/`oneOf`/`allOf` na raiz; nenhum `x-identity`/`x-secret` em entrada;
    `x-identity` em saída **só** na `reveal_identities` (e presente nela). Nenhuma ferramenta faz as duas fases: toda
    destrutiva exige na entrada o código ou o `dispatch_id` do preparo. O esquema de entrada publicado é o **mesmo**
    dicionário usado na validação: em `load_tools()`, o validador de cada ferramenta guarda o próprio
    `input_schema`, igual ao publicado, e o `wire` é a ferramenta publicada.

    Vermelho no esqueleto: `build_server` e `load_tools` lançam `NotImplementedError`."""
    tools = _by_name(_published(tmp_path, mode))
    entries = {entry["name"]: entry for entry in _tools_file()["tools"]}
    assert list(tools) == list(entries)

    with_identity_out: list[str] = []
    for name, tool in tools.items():
        entry = entries[name]
        published_in, published_out = tool["inputSchema"], tool["outputSchema"]
        assert published_in == _schema_file(entry["input_schema"]), name
        assert published_out == _schema_file(entry["output_schema"]), name
        for kind, schema in (("entrada", published_in), ("saída", published_out)):
            assert schema.get("type") == "object", (name, kind)
            assert _open_objects(schema) == [], (name, kind)
            assert _with_key(schema, "$ref") == [], (name, kind)
            assert [key for key in COMBINATORS if key in schema] == [], (name, kind)
        assert _with_key(published_in, "x-identity") == [] and _with_key(published_in, "x-secret") == [], name
        if _with_key(published_out, "x-identity"):
            with_identity_out.append(name)
        if name in DESTRUCTIVE_TOOLS:
            assert PREPARED_INPUTS & set(published_in.get("required", [])), f"{name} faria as duas fases"
    assert with_identity_out == ["reveal_identities"]

    from cq_client.mcp_server import load_tools

    specs = load_tools()
    assert [spec.name for spec in specs] == list(tools)
    for spec in specs:
        assert spec.input_validator.schema is spec.input_schema, spec.name
        assert spec.input_schema == tools[spec.name]["inputSchema"], spec.name
        assert spec.output_schema == tools[spec.name]["outputSchema"], spec.name
        assert spec.wire.name == spec.name, spec.name
        assert spec.wire.input_schema == tools[spec.name]["inputSchema"], spec.name
        assert spec.wire.output_schema == tools[spec.name]["outputSchema"], spec.name


@pytest.mark.parametrize("mode", MODES)
def test_tools_list_body_fields_match_api_requests(tmp_path: Path, mode: str) -> None:
    """Os campos de corpo das ferramentas iguais aos dos pedidos da API.

    Para cada ferramenta do catálogo cuja operação tem `*.request.schema.json`, os campos de corpo do `inputSchema`
    publicado (fora `offering` e `dispatch_id`) são os do pedido da API, **com as mesmas restrições**, e os
    obrigatórios também, salvo as exceções nomeadas, uma a uma: `create_offering` e
    `update_offering_settings` sem `contact_email`; `update_offering_settings.free_messages` obrigatório;
    `prepare_dispatch` sem `origin`; `reveal_identities.reason` com `minLength: 1` e `pseudonyms` com itens
    `PERSON_REF` (1 a 100); `import_students` com `path` no lugar de `rows`. As demais ferramentas do catálogo (sem
    corpo) só têm campos de caminho e de consulta.

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    tools = _by_name(_published(tmp_path, mode))
    with_body: list[str] = []
    for op in _catalog_exposed():
        name = op["mcp"]["tool"]
        published = tools[name]["inputSchema"]
        props = published.get("properties", {})
        required = set(published.get("required", []))
        request_path = API_SCHEMAS / f"{op['id']}.request.schema.json"
        if not request_path.exists():
            assert set(props) <= PATH_AND_QUERY_FIELDS, name
            continue
        with_body.append(name)
        request = _load(request_path)
        rule = INPUT_FORM_EXCEPTIONS.get(name, {})
        removed = rule.get("removed", set())
        expected_props = {field: schema for field, schema in request["properties"].items() if field not in removed}
        expected_props.update(rule.get("replaced", {}))
        expected_props.update(rule.get("added", {}))
        assert set(rule.get("replaced", {})) <= set(request["properties"]), name
        assert removed <= set(request["properties"]), name
        body = {field: schema for field, schema in props.items() if field not in PATH_FIELDS}
        assert body == expected_props, name
        expected_required = (set(request.get("required", [])) - removed) | rule.get("required_added", set())
        assert required - PATH_FIELDS == expected_required, name
    assert set(INPUT_FORM_EXCEPTIONS) <= set(with_body)


@pytest.mark.parametrize("mode", MODES)
def test_local_tool_bindings_match_tools_list_and_contract(tmp_path: Path, mode: str) -> None:
    """A tabela das ferramentas locais igual ao `tools/list` e ao contrato.

    `LOCAL_TOOL_BINDINGS`: as chaves são `LOCAL_COMMANDS`, na ordem fixada neste arquivo; cada uma das
    ferramentas locais aparece uma vez; ferramenta a ferramenta, o nome, a fase (`single` ⇔ sem `phase` em
    `tools.json`), as anotações e a confirmação são iguais aos da seção local de `tools.json` e aos do `tools/list`
    publicado (as anotações e o `_meta`).

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    tools = _by_name(_published(tmp_path, mode))
    local_entries = [entry for entry in _tools_file()["tools"] if entry["source"]["kind"] == "local"]

    assert set(LOCAL_TOOL_BINDINGS) == LOCAL_COMMANDS == set(LOCAL_ORDER)
    assert list(LOCAL_TOOL_BINDINGS) == list(LOCAL_ORDER)
    names = _local_names()
    assert len(names) == len(set(names))
    assert names == [entry["name"] for entry in local_entries]

    by_entry = {entry["name"]: entry for entry in local_entries}
    for command in LOCAL_ORDER:
        for binding in LOCAL_TOOL_BINDINGS[command]:
            entry = by_entry[binding.name]
            assert entry["source"]["command"] == command, binding.name
            assert entry["source"].get("phase", "single") == binding.phase, binding.name
            hints = _hints_dump(binding.annotations)
            assert hints == entry["annotations"], binding.name
            assert binding.requires_user_interaction is entry["requires_user_interaction"], binding.name
            published = tools[binding.name]
            assert {key: published["annotations"][key] for key in HINT_KEYS} == hints, binding.name
            assert (META_KEY in published) is binding.requires_user_interaction, binding.name


@pytest.mark.parametrize("mode", MODES)
def test_tools_list_is_the_same_in_two_connections(tmp_path: Path, mode: str) -> None:
    """o `tools/list` é estável: duas conexões seguidas ao mesmo `ServerDeps` devolvem a mesma lista
    (o JSON inteiro), não vazia.

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    first = _published(tmp_path, mode)
    second = _published(tmp_path, mode)
    assert first, "tools/list vazio"
    assert first == second


@pytest.mark.parametrize("mode", MODES)
def test_capabilities_without_resources_or_prompts(tmp_path: Path, mode: str) -> None:
    """o servidor só oferece ferramentas.

    As capacidades anunciadas não têm `resources` nem `prompts` e têm `tools`: na era legada, pelo `initialize`; na
    moderna, pelo modo `"auto"` (o `server/discover`), nunca pela versão fixa, que adota capacidades vazias por
    construção (**[F]**). Nas duas eras, `resources/list` e `prompts/list` são recusados com erro de
    protocolo (`-32601`).

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    from mcp.shared.exceptions import MCPError

    caps_mode = mode if mode == LEGACY_MODE else AUTO_MODE

    async def capabilities() -> tuple[str, Any]:
        async with in_process(make_deps(tmp_path), caps_mode) as client:
            return client.protocol_version, client.server_capabilities

    async def refusals() -> list[int]:
        codes: list[int] = []
        async with in_process(make_deps(tmp_path), mode) as client:
            for method in (client.list_resources, client.list_prompts):
                try:
                    await method()
                except MCPError as exc:
                    codes.append(exc.code)
                else:
                    codes.append(0)
        return codes

    version, caps = run(capabilities)
    if mode == LEGACY_MODE:
        assert version != MODES[1], "a era legada negociou a versão moderna"
    else:
        assert version == mode, "o modo auto não chegou à era moderna"
    assert caps.tools is not None
    assert caps.resources is None and caps.prompts is None
    assert run(refusals) == [-32601, -32601]


def test_mcp_messages_have_closed_keys_and_no_token_instructions(tmp_path: Path) -> None:
    """As mensagens fixas do MCP: chaves fechadas e nenhuma instrução sobre o token.

    As chaves de `MCP_MESSAGES` são exatamente: `api.<código>` para cada código de
    `contracts/api/v1/schemas/error.schema.json` (separada de `mcp_internal`, com texto distinto de `api.internal`);
    `profile.<Classe>` para cada subclasse de `ProfileError`, com a `reason` literal; `stack.<Classe>` para cada
    subclasse de `StackError` de mensagem fixa, mais `PortMismatch` e `UpTimeout`; `stack.StackFailure.<kind>` para
    cada `kind` de `STACK_FAILURE_MESSAGES`; `stack.StackVariableMissing` e `.unnamed`; e as chaves locais fixas. Os
    modelos `{seconds}`, `{services}`, `{name}` e `{reason}` são formatáveis. Nenhuma mensagem (nem a dica nem as
    linhas do stderr) cita `CQ_TOKEN` ou `cq ` seguido de comando, nem manda colar token; a que fala em configurar
    token o deixa com o operador. As ferramentas citadas pelas mensagens existem no `tools/list` real.

    Vermelho no esqueleto: `build_server` lança `NotImplementedError`."""
    error_schema = _load(API_SCHEMAS / "error.schema.json")
    api_codes = error_schema["properties"]["error"]["properties"]["code"]["enum"]
    profile_classes = _subclasses(cq_profile.ProfileError)
    stack_classes = _subclasses(cq_stack.StackError)
    assert profile_classes and stack_classes

    expected: set[str] = {f"api.{code}" for code in api_codes} | set(FIXED_LOCAL_KEYS)
    expected |= {f"profile.{cls.__name__}" for cls in profile_classes}
    for cls in stack_classes:
        if cls is cq_stack.StackFailure:
            expected |= {f"stack.StackFailure.{kind}" for kind in cq_stack.STACK_FAILURE_MESSAGES}
        elif cls is cq_stack.StackVariableMissing:
            expected |= {"stack.StackVariableMissing", "stack.StackVariableMissing.unnamed"}
        else:
            expected.add(f"stack.{cls.__name__}")
    expected |= {f"stack.{PortMismatch.__name__}", f"stack.{UpTimeout.__name__}"}
    assert set(MCP_MESSAGES) == expected
    assert len([key for key in MCP_MESSAGES if key.startswith("api.")]) == len(set(api_codes))
    assert MCP_MESSAGES["api.internal"] != MCP_MESSAGES["mcp_internal"]
    for cls in profile_classes:
        assert not inspect.isabstract(cls)
        assert MCP_MESSAGES[f"profile.{cls.__name__}"] == cls.reason

    up_timeout = MCP_MESSAGES["stack.UpTimeout"].format(seconds=180, services="api, worker")
    assert "180" in up_timeout and "api, worker" in up_timeout
    assert "CQ_DB_PASSWORD" in MCP_MESSAGES["stack.StackVariableMissing"].format(name="CQ_DB_PASSWORD")
    assert MCP_MESSAGES["file.roster"].format(reason="motivo fixo") == "motivo fixo"
    assert "cq-demo" in MCP_HINT_TEMPLATE.format(project="cq-demo")

    texts = [*MCP_MESSAGES.values(), MCP_HINT_TEMPLATE, *MCP_STDERR_MESSAGES.values()]
    assert all(isinstance(text, str) and text.strip() for text in texts)
    assert [text for text in texts if "CQ_TOKEN" in text] == []
    assert [text for text in texts if CLI_COMMAND_RE.search(text)] == []
    assert [text for text in texts if PASTE_RE.search(text)] == []
    assert [text for text in texts
            if re.search(r"configur\w*[^.]*\btoken\b", text, re.IGNORECASE) and "operador" not in text] == []

    published = {tool["name"] for tool in _published(tmp_path, LEGACY_MODE)}
    cited = {name for name in published for text in texts if re.search(rf"\b{name}\b", text)}
    assert cited == TOOLS_CITED_BY_MESSAGES


def test_contract_copies_in_package_match_frozen_contracts() -> None:
    """As cópias dos contratos no pacote iguais aos contratos congelados.

    As cópias em `client/src/cq_client/mcp_contract/<p>` são byte a byte iguais a `contracts/<p>` para todo arquivo
    citado por `tools.json` (as entradas e as saídas, inclusive as da API e do CLI), e para o próprio `tools.json` e
    o `tools.schema.json`; nenhum arquivo a mais, além do `__init__.py` vazio (os `__pycache__` ficam fora). Junta-se a
    conferência das tabelas reais: os nomes de `tools.json` são os de `TOOL_NAMES`, na ordem.

    Vermelho no esqueleto: as cópias não existem (arquivo ausente)."""
    entries = _tools_file()["tools"]
    assert [entry["name"] for entry in entries] == list(TOOL_NAMES)

    expected = {"mcp/v1/tools.json", "mcp/v1/tools.schema.json"}
    for entry in entries:
        expected.add(_contract_rel(entry["input_schema"]))
        expected.add(_contract_rel(entry["output_schema"]))

    assert COPIES_DIR.is_dir(), "cópias dos contratos ausentes em client/src/cq_client/mcp_contract/"
    present = {path.relative_to(COPIES_DIR).as_posix() for path in COPIES_DIR.rglob("*")
               if path.is_file() and "__pycache__" not in path.relative_to(COPIES_DIR).parts}
    assert sorted(expected - present) == [], "cópias ausentes"
    assert sorted(present - expected) == ["__init__.py"], "arquivos a mais nas cópias"
    assert (COPIES_DIR / "__init__.py").read_bytes() == b""
    different = sorted(rel for rel in expected
                       if (COPIES_DIR / rel).read_bytes() != (CONTRACTS / rel).read_bytes())
    assert different == []


# --------------------------------------------------------------------------------------------------
# Higiene dos arquivos de contrato (verdes desde o esqueleto)
# --------------------------------------------------------------------------------------------------


def test_tools_json_is_valid_against_its_schema() -> None:
    """`tools.schema.json` é válido no 2020-12 e `tools.json` é válido contra ele."""
    schema = _load(TOOLS_SCHEMA_JSON)
    assert schema.get("$schema") == DRAFT_2020_12
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(_tools_file()), key=lambda e: list(e.path))
    assert [f"{list(e.path)}: {e.message}" for e in errors] == []


@pytest.mark.parametrize("schema_file", sorted(p.name for p in MCP_SCHEMAS.glob("*.json")))
def test_mcp_schema_file_is_valid_closed_and_without_ref(schema_file: str) -> None:
    """cada arquivo de `contracts/mcp/v1/schemas/` declara e é válido no 2020-12, tem
    raiz `type: object`, `additionalProperties: false` em todo objeto e nenhum `$ref`."""
    schema = _load(MCP_SCHEMAS / schema_file)
    assert schema.get("$schema") == DRAFT_2020_12
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("type") == "object"
    assert _open_objects(schema) == []
    assert _with_key(schema, "$ref") == []


def test_tools_json_sets_match_catalog_and_local_commands() -> None:
    """os nomes da seção do catálogo de `tools.json` são os
    `mcp.tool` das operações com `mcp.exposed: true`, na ordem do catálogo, com a operação de cada uma; nenhuma
    operação com `mcp.exposed: false` aparece; a seção local vem depois e liga exatamente os comandos de
    `LOCAL_COMMANDS`; os nomes das duas seções são disjuntos."""
    entries = _tools_file()["tools"]
    kinds = [entry["source"]["kind"] for entry in entries]
    first_local = kinds.index("local")
    assert set(kinds[:first_local]) == {"catalog"} and set(kinds[first_local:]) == {"local"}

    catalog_entries = entries[:first_local]
    local_entries = entries[first_local:]
    exposed = _catalog_exposed()
    assert [(e["name"], e["source"]["operation"]) for e in catalog_entries] == [
        (op["mcp"]["tool"], op["id"]) for op in exposed]
    hidden_ops = {op["id"] for op in _catalog_ops() if not op["mcp"]["exposed"]}
    assert hidden_ops.isdisjoint(e["source"]["operation"] for e in catalog_entries)
    assert {e["source"]["command"] for e in local_entries} == set(LOCAL_COMMANDS)
    catalog_names = {e["name"] for e in catalog_entries}
    local_names = {e["name"] for e in local_entries}
    assert catalog_names.isdisjoint(local_names)
    assert len(catalog_names) + len(local_names) == len(entries)
