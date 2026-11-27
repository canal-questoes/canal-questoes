"""Contratos do CLI (operação e expurgo) campo a campo: os arquivos congelados da API e do CLI e o que a
implementação publica: as operações do `doctor` e do expurgo, os eventos novos e a saída `--json` dos
comandos locais.

Confere os arquivos de contrato congelados do CLI (`contracts/api/v1/` e `contracts/cli/v1/`), que
existem desde o esqueleto, **e**, na mesma função, o mesmo recorte no que a implementação publica (cada
função falha no esqueleto: nenhuma função deste arquivo lê só os arquivos congelados):
- as 3 operações (`system.doctor`, `offerings.prepare_purge`, `offerings.purge`) no catálogo (permissões, MCP,
  `confirm`, `destructive`), no registro de `cq_server.api` e no OpenAPI publicado; `system.health` intacto;
- a forma exata dos 4 esquemas novos da API, propriedade a propriedade (enums, anuláveis, mínimos e máximos),
  com amostras aceitas e recusadas;
- `purge_pending` (com `pending`) e `purge_changed` no `error.schema.json` e em `cq_server.api.errors`;
- `Offering.purged_at` em todo esquema que embute o `Offering` e no modelo publicado;
- o parâmetro `person` de `events.list` e `types` com `required: false` no OpenAPI publicado;
- as 2 variantes de evento com conjuntos de chaves distintos dos 46 anteriores;
- nenhum `x-identity`/`x-secret` novo (continuam 11 e 3) e nenhum nome proibido nos campos novos;
- `LOCAL_COMMANDS` disjunto das folhas do catálogo;
- os 5 esquemas de `contracts/cli/v1/` válidos (2020-12), fechados e com toda propriedade obrigatória.

O publicado: o OpenAPI de `create_app(...).openapi()` (sem banco) e o registro `cq_server.api.operations.BY_ID`
para as 3 operações (ausentes até a implementação delas: vermelho); `EventType` e as variantes de
`cq_server.api.schemas`; `cq_server.api.errors` e o `Offering` publicado; o
parâmetro `person`; e, para os esquemas do CLI, a saída `--json` real de `cq status`, `cq up`,
`cq down`, `cq logs` e `cq doctor`, com a pilha pelo dublê de nível 1 (`FakeStack`) e a API dos testes por
`http=`. Escrito para aceitar extensão: nenhum total de operações, de eventos ou de erros
do sistema é afirmado, salvo as contagens de marcas que o contrato fixa (11 e 3) e os 21 códigos de erro.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for

from tests.contract.test_contract_dispatch import (
    _deref,
    _enum_at,
    _paths,
    _published,
    _published_operation,
    _request_schema,
    _success_schema,
)
from tests.contract.test_contract_reports import (
    _data_properties,
    _event_variants,
    _mark_points,
    _property_names,
    _published_event_variants,
)
from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import invoke_cli, make_stack_dir, write_profile
from tests.helpers.clock import FakeClock
from tests.helpers.contract import (
    CLI_SCHEMAS,
    MESSAGES,
    SCHEMAS,
    STATUS_OF,
    assert_cli_json,
    flatten,
    iter_objects,
    load_catalog,
    load_json,
)
from tests.helpers.stack_fake import TEST_API_URL, FakeSleep, FakeStack

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# padrões do contrato, da API base ao CLI.
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{2,39}$"
CONFIRMATION_CODE_PATTERN = r"^[A-Za-z0-9_-]{22}$"
# o endereço de exibição (sem `user:senha@`) e o nome do projeto Compose.
DISPLAY_URL_PATTERN = r"^https?://[^/?#@\s]+([/?#].*)?$"
PROJECT_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

DOCTOR_PURGE_OPERATIONS = ("system.doctor", "offerings.prepare_purge", "offerings.purge")
PURGE_EVENT_TYPES = ("offering.purge_prepared", "offering.purged")
DOCTOR_PURGE_API_FILES = ("system.doctor.response.schema.json", "offerings.prepare_purge.response.schema.json",
                "offerings.purge.request.schema.json", "offerings.purge.response.schema.json")
CLI_COMMANDS = ("status", "up", "down", "logs", "doctor")

# o catálogo do contrato, linha a linha.
CATALOG_ENTRIES = {
    "system.doctor": {"method": "GET", "path": "/v1/system/doctor", "permission": "offerings:read",
                      "cli": "doctor", "mcp": {"exposed": True, "tool": "run_doctor", "confirm": False},
                      "destructive": False},
    "offerings.prepare_purge": {"method": "POST", "path": "/v1/offerings/{ref}/purge/prepare",
                                "permission": "people:write", "cli": "offerings purge prepare",
                                "mcp": {"exposed": True, "tool": "prepare_purge_offering", "confirm": False},
                                "destructive": False},
    "offerings.purge": {"method": "POST", "path": "/v1/offerings/{ref}/purge", "permission": "people:write",
                        "cli": "offerings purge confirm",
                        "mcp": {"exposed": True, "tool": "purge_offering", "confirm": True},
                        "destructive": True},
}
# O `system.health` da API base, que o CLI não muda (inclusive o motivo congelado do `mcp.exposed: false`).
SYSTEM_HEALTH_ENTRY = {"id": "system.health", "method": "GET", "path": "/health", "permission": None,
                       "cli": "health",
                       "mcp": {"exposed": False,
                               "reason": ("Operação de diagnóstico; no MCP, o diagnóstico fica com a "
                                          "run_doctor, e a stack_status mostra a saúde da API da pilha local.")},
                       "destructive": False}

_UUID = {"type": "string", "pattern": UUID4_PATTERN}
_TIME = {"type": "string", "pattern": TIME_PATTERN}
_COUNT = {"type": "integer", "minimum": 0}
_BOOL = {"type": "boolean"}
OFFERING_REF = {
    "type": "object", "additionalProperties": False, "required": ["offering_id", "code"],
    "properties": {"offering_id": _UUID, "code": {"type": "string", "pattern": CODE_PATTERN}},
}

# os identificadores do servidor e os 3 resultados (os 5 valores são só do CLI). Com a implantação
# e o piloto: os 9, os 6 primeiros e os 3
# globais no fim, iguais ao enum do esquema da API e a `cq_server.doctor.SERVER_CHECK_IDS`.
SERVER_CHECK_IDS = ["bots.master_key", "bot.access", "bot.poll", "bot.lease", "tasks.overdue", "clock.skew",
                    "backup.age", "smtp.server", "retention.overdue"]
SERVER_RESULTS = ["ok", "warning", "failure"]
DOCTOR_RESPONSE = {
    "type": "object", "additionalProperties": False, "required": ["checked_at", "checks"],
    "properties": {
        "checked_at": _TIME,
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "result", "message", "offering", "count"],
            "properties": {
                "id": {"enum": SERVER_CHECK_IDS},
                "result": {"enum": SERVER_RESULTS},
                "message": {"type": "string", "minLength": 1},
                "offering": {"type": ["string", "null"], "pattern": CODE_PATTERN},
                "count": {"type": ["integer", "null"], "minimum": 0},
            }}},
    },
}
# a preparação e a confirmação do expurgo.
IMPACT_KEYS = ["identities_to_delete", "identities_kept", "endpoints", "updates", "contact_email_set"]
OUTCOME_KEYS = ["identities_deleted", "identities_kept", "endpoints", "updates", "free_messages_redacted",
                "outbox_redacted", "contact_email_deleted"]
PREPARE_PURGE_RESPONSE = {
    "type": "object", "additionalProperties": False,
    "required": ["offering", "impact", "confirmation_code", "expires_at"],
    "properties": {
        "offering": OFFERING_REF,
        "impact": {"type": "object", "additionalProperties": False, "required": IMPACT_KEYS,
                   "properties": {**{key: _COUNT for key in IMPACT_KEYS[:-1]}, "contact_email_set": _BOOL}},
        "confirmation_code": {"type": "string", "pattern": CONFIRMATION_CODE_PATTERN},
        "expires_at": _TIME,
    },
}
PURGE_REQUEST = {
    "type": "object", "additionalProperties": False, "required": ["confirmation_code"],
    "properties": {"confirmation_code": {"type": "string", "minLength": 1, "maxLength": 64}},
}
PURGE_RESPONSE = {
    "type": "object", "additionalProperties": False, "required": ["offering", "purged_at", "result"],
    "properties": {
        "offering": OFFERING_REF,
        "purged_at": _TIME,
        "result": {"type": "object", "additionalProperties": False, "required": OUTCOME_KEYS,
                   "properties": {**{key: _COUNT for key in OUTCOME_KEYS[:-1]}, "contact_email_deleted": _BOOL}},
    },
}
EXPECTED_API_SCHEMAS = {
    "system.doctor.response.schema.json": DOCTOR_RESPONSE,
    "offerings.prepare_purge.response.schema.json": PREPARE_PURGE_RESPONSE,
    "offerings.purge.request.schema.json": PURGE_REQUEST,
    "offerings.purge.response.schema.json": PURGE_RESPONSE,
}

# o objeto `pending` do erro `purge_pending`.
PENDING_KEYS = ["outbox", "updates", "tasks", "freeze", "due"]
PENDING = {"type": "object", "additionalProperties": False, "required": PENDING_KEYS,
           "properties": {key: _COUNT for key in PENDING_KEYS}}

# as respostas que embutem o `Offering` e o caminho dele em cada uma.
OFFERING_EMBEDS = {
    "offerings.create.response.schema.json": "", "offerings.list.response.schema.json": "items[].",
    "offerings.get.response.schema.json": "", "offerings.close.response.schema.json": "",
    "offerings.prepare_archive.response.schema.json": "offering.",
    "offerings.archive.response.schema.json": "", "bots.activate.response.schema.json": "",
    "offerings.update_settings.response.schema.json": "",
}
PURGED_AT = {"type": ["string", "null"], "pattern": TIME_PATTERN}

# os eventos novos, na ordem do contrato.
EVENT_DATA = {
    "offering.purge_prepared": {"offering_id": _UUID, "expires_at": _TIME,
                                **{key: _COUNT for key in IMPACT_KEYS[:-1]}, "contact_email_set": _BOOL},
    "offering.purged": {"offering_id": _UUID, **{key: _COUNT for key in OUTCOME_KEYS[:-1]},
                        "contact_email_deleted": _BOOL},
}

# nomes que nenhum campo novo pode ter (como nos relatórios).
RESERVED_NAMES = {"link", "recipient", "secret", "until", "username", "token_id", "worker_id", "chat_id",
                  "sender_id"}

# os enums do CLI.
PROFILE_ORIGINS = {
    "name": ["option", "env", "file", "default"], "kind": ["profile", "inferred"],
    "api_url": ["env", "profile", "file", "default"], "token": ["env", "profile", "file", "default"],
}
SERVICES = ["postgres", "api", "worker", "engine"]
SERVICE_STATES = ["running", "stopped", "absent", "unknown"]
SERVICE_HEALTH = ["healthy", "unhealthy", "starting", "none", "unknown"]
STATUS_WARNINGS = ["foreign_api", "port_mismatch", "docker_unavailable", "insecure_transport", "env_file_missing"]
DOCTOR_RESULTS = ["ok", "warning", "failure", "not_verified", "not_applicable"]
DOCTOR_PARTS = ["api", "local", "profile"]
# Com a implantação e o piloto: os 27, na ordem dos grupos (as 4 do cliente, as 9 do
# servidor, as 10 locais, as 2 do host e as 2 do perfil), iguais ao enum do esquema do CLI.
CLIENT_CHECK_IDS = [
    "api.reachable", "database.reachable", "database.schema", "api.access", "bots.master_key", "bot.access",
    "bot.poll", "bot.lease", "tasks.overdue", "clock.skew", "backup.age", "smtp.server", "retention.overdue",
    "docker.reachable", "compose.version", "stack.directory", "stack.env_file",
    "service.postgres", "service.api", "service.worker", "service.engine", "engine.image", "api.owner",
    "postgres.volume", "disk.space", "profile.permissions", "profile.git",
]
SERVICE_VIEW = {
    "type": "object", "additionalProperties": False, "required": ["service", "state", "health"],
    "properties": {"service": {"enum": SERVICES}, "state": {"enum": SERVICE_STATES},
                   "health": {"enum": SERVICE_HEALTH}},
}
PROFILE_VIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "kind", "api_url", "token_set", "compose_project", "origins"],
    "properties": {
        "name": {"type": ["string", "null"], "minLength": 1},
        "kind": {"enum": ["local", "remote"]},
        "api_url": {"type": "string", "pattern": DISPLAY_URL_PATTERN},
        "token_set": _BOOL,
        "compose_project": {"type": ["string", "null"], "pattern": PROJECT_PATTERN},
        "origins": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "kind", "api_url", "token", "compose_project"],
            "properties": {**{key: {"enum": values} for key, values in PROFILE_ORIGINS.items()},
                           "compose_project": {"type": ["string", "null"], "enum": ["profile", "default", None]}},
        },
    },
}
_PORT = {"minimum": 1, "maximum": 65535}

UUID_SAMPLE = "3f2b8c1e-9a4d-4e7f-8b2a-1c0d9e8f7a6b"
TIME_SAMPLE = "2031-01-01T00:10:00Z"
CODE_SAMPLE = "Ab0_-" * 4 + "xy"


def _load(name: str) -> Any:
    return load_json(str(SCHEMAS / name))


def _load_cli(command: str) -> Any:
    return load_json(str(CLI_SCHEMAS / f"{command}.output.schema.json"))


def _body(schema: dict) -> dict:
    """O esquema sem as chaves de identificação do arquivo (`$schema`, `$id`, `title`)."""
    return {k: v for k, v in schema.items() if k not in ("$schema", "$id", "title")}


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def _event_page(event_type: str, data: dict) -> dict:
    return {"items": [{"seq": 1, "type": event_type, "occurred_at": TIME_SAMPLE, "actor_token_id": None,
                       "data": data}], "next_cursor": 1}


def _published_params(doc: dict, op_id: str) -> dict[str, dict]:
    """Nome → parâmetro publicado (resolvido) de uma operação, com os do item de caminho."""
    entry = next(op for op in load_catalog()["operations"] if op["id"] == op_id)
    item = _deref(doc.get("paths", {}).get(entry["path"], {}), doc)
    operation = _published_operation(doc, op_id)
    params = [_deref(p, doc) for p in [*item.get("parameters", []), *operation.get("parameters", [])]]
    return {p["name"]: p for p in params}


def _reserved(paths: set[str]) -> list[str]:
    found = []
    for path in paths:
        name = path.replace("[]", "").rsplit(".", 1)[-1]
        if (name == "code" and path != "offering.code") or name in RESERVED_NAMES:
            found.append(path)
    return sorted(found)


def _extension_keys(node: Any, in_names: bool = False) -> list[str]:
    """Toda palavra-chave `x-*` do documento (dentro de `properties`, as chaves são nomes, não palavras-chave)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if not in_names and key.startswith("x-"):
                found.append(key)
            found += _extension_keys(value, key in ("properties", "$defs") and not in_names)
    elif isinstance(node, list):
        for item in node:
            found += _extension_keys(item)
    return found


def _type_set(node: dict) -> set[str]:
    t = node.get("type")
    if t is None:
        return set()
    return {t} if isinstance(t, str) else set(t)


# ---------------------------------------------------------------------------------------------
# Catálogo, registro e OpenAPI
# ---------------------------------------------------------------------------------------------


def test_doctor_purge_operations_in_catalog_registry_and_openapi() -> None:
    """As 3 operações novas estão no catálogo
    com os valores exatos do contrato (permissões já existentes, `offerings:read` no `doctor` e
    `people:write` no expurgo; as 3 expostas ao MCP; só `offerings.purge` destrutiva e com `confirm: true`),
    no fim (`ids[36:39]`), e `system.health` continua intacto (a entrada do catálogo, o motivo congelado do
    `mcp.exposed: false` e a resposta publicada igual à congelada).

    A implementação (vermelho no esqueleto): as 3 operações no registro (`BY_ID`) com o método, o
    caminho e a permissão do catálogo e modelo de pedido só na confirmação; no OpenAPI publicado, com o
    `operationId`, só o 200 de sucesso, corpo só na confirmação (o pedido congelado, o mesmo do arquivamento) e
    a resposta de sucesso igual, na forma achatada, à congelada."""
    operations = load_catalog()["operations"]
    by_id = {op["id"]: op for op in operations}
    assert {op_id: {k: v for k, v in by_id[op_id].items() if k != "id"} for op_id in DOCTOR_PURGE_OPERATIONS} == \
        CATALOG_ENTRIES
    assert [op["id"] for op in operations][36:39] == list(DOCTOR_PURGE_OPERATIONS)
    assert by_id["system.health"] == SYSTEM_HEALTH_ENTRY
    assert {op_id for op_id in DOCTOR_PURGE_OPERATIONS if by_id[op_id]["mcp"].get("confirm")} == {"offerings.purge"}
    assert _body(_load("offerings.purge.request.schema.json")) == _body(_load("offerings.archive.request.schema.json"))

    # A implementação.
    from cq_server.api.operations import BY_ID

    assert [op_id for op_id in DOCTOR_PURGE_OPERATIONS if op_id not in BY_ID] == [], "operações novas fora do registro"
    doc = _published()
    for op_id in DOCTOR_PURGE_OPERATIONS:
        entry, op = by_id[op_id], BY_ID[op_id]
        assert (op.method, op.path, op.permission.value if op.permission else None) == (
            entry["method"], entry["path"], entry["permission"]), op_id
        assert (op.request_model is not None) is (op_id == "offerings.purge"), op_id
        published = _published_operation(doc, op_id)
        assert published.get("operationId") == op_id
        assert ("requestBody" in published) is (op_id == "offerings.purge"), op_id
        assert [s for s in published.get("responses", {}) if s.startswith("2")] == ["200"], op_id
        assert flatten(_success_schema(doc, op_id), doc) == flatten(_load(f"{op_id}.response.schema.json")), op_id
    assert flatten(_request_schema(doc, "offerings.purge"), doc) == flatten(
        _load("offerings.purge.request.schema.json"))
    # `system.health` intacto também no publicado.
    health = _published_operation(doc, "system.health")
    assert health.get("operationId") == "system.health"
    assert flatten(_success_schema(doc, "system.health"), doc) == flatten(
        _load("system.health.response.schema.json"))


# ---------------------------------------------------------------------------------------------
# Forma exata dos esquemas novos da API
# ---------------------------------------------------------------------------------------------


def test_doctor_response_schema_matches_contract() -> None:
    """`system.doctor.response.schema.json` tem
    exatamente a forma do contrato (`checked_at` horário UTC; `checks[]` fechado com `id` nos
    identificadores do servidor (os 9), `result` em `ok | warning | failure`, `message` não vazia, `offering` código
    ou nulo e `count` inteiro ≥ 0 ou nulo); as amostras válidas são aceitas e as inválidas (resultado do
    cliente, identificador do cliente, contagem negativa, código fora do padrão, campo a mais, horário com
    deslocamento) recusadas; os tipos do núcleo (`SERVER_CHECK_IDS`, `SERVER_CHECK_RESULTS`, reais no
    esqueleto) batem com o esquema.

    O publicado (vermelho no esqueleto): a resposta de sucesso do `system.doctor` no OpenAPI, na forma
    achatada, igual à congelada, com os mesmos enums."""
    schema = _load("system.doctor.response.schema.json")
    assert _body(schema) == DOCTOR_RESPONSE
    check = {"id": "bot.poll", "result": "warning", "message": "Bot sem consulta recente.",
             "offering": "mc102-2031s1", "count": None}
    valid = [
        {"checked_at": TIME_SAMPLE, "checks": []},
        {"checked_at": "2031-01-01T00:10:00.123456Z", "checks": [check]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "id": "tasks.overdue", "result": "ok", "offering": None,
                                                "count": 0}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "id": "bot.access", "result": "failure"}]},
    ]
    invalid = [
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "result": "not_verified"}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "result": "not_applicable"}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "id": "api.reachable"}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "count": -1}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "offering": "MC102"}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "message": ""}]},
        {"checked_at": TIME_SAMPLE, "checks": [{**check, "detail": "x"}]},
        {"checked_at": TIME_SAMPLE, "checks": [{k: v for k, v in check.items() if k != "count"}]},
        {"checked_at": "2031-01-01T00:10:00+00:00", "checks": []},
        {"checked_at": TIME_SAMPLE, "checks": [], "version": "1"},
    ]
    for body in valid:
        assert _is_valid(schema, body), body
    for body in invalid:
        assert not _is_valid(schema, body), body
    from cq_server.doctor import SERVER_CHECK_IDS as CORE_IDS, SERVER_CHECK_RESULTS as CORE_RESULTS

    assert list(CORE_IDS) == SERVER_CHECK_IDS
    assert list(CORE_RESULTS) == SERVER_RESULTS
    # O cliente valida o corpo pela mesma lista do servidor (vermelho no esqueleto).
    from cq_client.doctor import SERVER_CHECK_IDS as CLIENT_SERVER_IDS

    assert list(CLIENT_SERVER_IDS) == SERVER_CHECK_IDS

    # O publicado.
    doc = _published()
    published = _success_schema(doc, "system.doctor")
    assert flatten(published, doc) == flatten(schema)
    assert _enum_at(doc, published, "checks[].id") == set(SERVER_CHECK_IDS)
    assert _enum_at(doc, published, "checks[].result") == set(SERVER_RESULTS)


def test_purge_schemas_match_contract() -> None:
    """Os 3 esquemas do expurgo têm exatamente a forma do
    contrato: a preparação (`OfferingRef`, o impacto só com contagens ≥ 0 e o booleano do
    contato, o `confirmation_code` com o padrão de 22 caracteres do arquivamento, `expires_at`); o pedido da
    confirmação (`confirmation_code` de 1 a 64 caracteres, fechado); o resultado (`OfferingRef`, `purged_at`
    e as 7 contagens). Amostras válidas aceitas; inválidas (contagem negativa ou fracionária, código fora do
    padrão, pedido vazio ou longo, campo a mais, pseudônimo no resultado) recusadas.

    O publicado (vermelho no esqueleto): as respostas das duas operações e o pedido da confirmação no
    OpenAPI, na forma achatada, iguais aos congelados; a preparação sem corpo."""
    for name in ("offerings.prepare_purge.response.schema.json", "offerings.purge.request.schema.json",
                 "offerings.purge.response.schema.json"):
        assert _body(_load(name)) == EXPECTED_API_SCHEMAS[name], name

    prepare = _load("offerings.prepare_purge.response.schema.json")
    impact = {"identities_to_delete": 3, "identities_kept": 1, "endpoints": 4, "updates": 12,
              "contact_email_set": True}
    prepared = {"offering": {"offering_id": UUID_SAMPLE, "code": "mc102-2031s1"}, "impact": impact,
                "confirmation_code": CODE_SAMPLE, "expires_at": TIME_SAMPLE}
    assert _is_valid(prepare, prepared)
    assert _is_valid(prepare, {**prepared, "impact": {**dict.fromkeys(IMPACT_KEYS[:-1], 0),
                                                      "contact_email_set": False}})
    for bad in ({**prepared, "confirmation_code": CODE_SAMPLE[:-1]},
                {**prepared, "confirmation_code": CODE_SAMPLE + "a"},
                {**prepared, "impact": {**impact, "endpoints": -1}},
                {**prepared, "impact": {**impact, "updates": 1.5}},
                {**prepared, "impact": {**impact, "contact_email_set": "sim"}},
                {**prepared, "impact": {**impact, "people": [UUID_SAMPLE]}},
                {**prepared, "offering": {"offering_id": UUID_SAMPLE, "code": "MC102"}},
                {**prepared, "expires_at": "2031-01-01T00:10:00-03:00"},
                {k: v for k, v in prepared.items() if k != "impact"}):
        assert not _is_valid(prepare, bad), bad

    request = _load("offerings.purge.request.schema.json")
    for ok in ({"confirmation_code": "a"}, {"confirmation_code": "x" * 64}, {"confirmation_code": CODE_SAMPLE}):
        assert _is_valid(request, ok), ok
    for bad in ({}, {"confirmation_code": ""}, {"confirmation_code": "x" * 65}, {"confirmation_code": 1},
                {"confirmation_code": CODE_SAMPLE, "force": True}):
        assert not _is_valid(request, bad), bad

    purge = _load("offerings.purge.response.schema.json")
    outcome = {"identities_deleted": 3, "identities_kept": 1, "endpoints": 4, "updates": 12,
               "free_messages_redacted": 2, "outbox_redacted": 1, "contact_email_deleted": True}
    purged = {"offering": {"offering_id": UUID_SAMPLE, "code": "mc102-2031s1"}, "purged_at": TIME_SAMPLE,
              "result": outcome}
    assert _is_valid(purge, purged)
    for bad in ({**purged, "purged_at": None},
                {**purged, "result": {**outcome, "outbox_redacted": -1}},
                {**purged, "result": {**outcome, "contact_email_deleted": None}},
                {**purged, "result": {**outcome, "pseudonyms": [UUID_SAMPLE]}},
                {**purged, "result": {k: v for k, v in outcome.items() if k != "free_messages_redacted"}},
                {**purged, "confirmation_code": CODE_SAMPLE}):
        assert not _is_valid(purge, bad), bad

    # O publicado.
    doc = _published()
    for op_id in ("offerings.prepare_purge", "offerings.purge"):
        assert flatten(_success_schema(doc, op_id), doc) == flatten(_load(f"{op_id}.response.schema.json")), op_id
    assert "requestBody" not in _published_operation(doc, "offerings.prepare_purge")
    assert flatten(_request_schema(doc, "offerings.purge"), doc) == flatten(request)


# ---------------------------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------------------------


def test_purge_error_codes_in_error_schema_and_errors_module() -> None:
    """O `error.schema.json` tem os 21 códigos; o ramo
    de `purge_pending` exige `pending` (o objeto fechado das 5 contagens ≥ 0) e proíbe `fields`, `state` e
    `permission`; o de `purge_changed` proíbe os quatro; os dois com 409 e a mensagem exata, também nos
    ajudantes (`tests/helpers/contract.py`).

    A implementação (vermelho no esqueleto): `cq_server.api.errors` com os 2 códigos (409, a mensagem
    exata), `PENDING_CODES == {"purge_pending"}`, e o envelope de um `ApiError("purge_pending",
    pending=PurgePending(...))` com o `pending` exato (as 5 contagens) e o de `purge_changed` sem ele."""
    schema = _load("error.schema.json")
    error = schema["properties"]["error"]
    codes = error["properties"]["code"]["enum"]
    assert {"purge_pending", "purge_changed"} <= set(codes)
    assert len(codes) == len(set(codes)) == 21
    assert error["properties"]["pending"] == PENDING
    branches = {b["properties"]["code"]["const"]: b for b in error["oneOf"]}
    for code in ("purge_pending", "purge_changed"):
        assert branches[code]["properties"]["message"] == {"const": MESSAGES[code]}, code
        assert STATUS_OF[code] == 409, code
    assert MESSAGES["purge_pending"] == ("Há trabalho pendente neste oferecimento; o expurgo só pode ser feito "
                                         "depois que ele terminar.")
    assert MESSAGES["purge_changed"] == ("As contagens do expurgo mudaram desde a preparação; prepare de novo e "
                                         "confira o impacto.")

    def forbidden(code: str) -> list[str]:
        return [name for alt in branches[code].get("not", {}).get("anyOf", []) for name in alt.get("required", [])]

    assert branches["purge_pending"]["required"] == ["code", "message", "pending"]
    assert sorted(forbidden("purge_pending")) == ["fields", "permission", "state"]
    assert branches["purge_changed"]["required"] == ["code", "message"]
    assert sorted(forbidden("purge_changed")) == ["fields", "pending", "permission", "state"]
    pending = {"outbox": 1, "updates": 2, "tasks": 0, "freeze": 1, "due": 4}
    good = {"error": {"code": "purge_pending", "message": MESSAGES["purge_pending"], "pending": pending}}
    assert Draft202012Validator(schema).is_valid(good)
    changed = {"error": {"code": "purge_changed", "message": MESSAGES["purge_changed"]}}
    assert Draft202012Validator(schema).is_valid(changed)
    assert not Draft202012Validator(schema).is_valid({"error": {**changed["error"], "pending": pending}})
    assert not Draft202012Validator(schema).is_valid({"error": {**good["error"], "state": "archived"}})

    # A implementação.
    from cq_server.api import errors
    from cq_server.purge import PurgePending

    assert sorted({"purge_pending", "purge_changed"} - set(errors.MESSAGES)) == [], (
        "códigos do expurgo ausentes de cq_server.api.errors")
    assert {code: (errors.STATUS[code], errors.MESSAGES[code]) for code in ("purge_pending", "purge_changed")} == {
        code: (409, MESSAGES[code]) for code in ("purge_pending", "purge_changed")}
    assert getattr(errors, "PENDING_CODES", None) == frozenset({"purge_pending"})
    response = errors.from_api_error(errors.ApiError("purge_pending", pending=PurgePending(**pending)))
    assert response.status_code == 409
    assert json.loads(response.body) == good
    response = errors.from_api_error(errors.ApiError("purge_changed"))
    assert (response.status_code, json.loads(response.body)) == (409, changed)


# ---------------------------------------------------------------------------------------------
# `Offering.purged_at` e o `person` de `events.list`
# ---------------------------------------------------------------------------------------------


def test_offering_purged_at_in_every_embedding_schema_and_published_model() -> None:
    """Todo objeto `Offering` (o que tem `free_messages` e
    `contact_email_set`) de todo esquema de resposta congelado tem `purged_at` obrigatório, horário UTC ou
    nulo, e eles são exatamente os das 8 respostas do contrato (no caminho do `Offering` em cada uma);
    um `Offering` com `purged_at` horário ou nulo é aceito, sem a chave ou com outro formato, recusado.

    O publicado (vermelho no esqueleto): a resposta de sucesso das 8 operações no OpenAPI tem `purged_at`
    no caminho do `Offering`."""
    found: dict[str, list[dict]] = {}
    for path in sorted(SCHEMAS.glob("*.response.schema.json")):
        schema = load_json(str(path))
        for obj in iter_objects(schema, schema):
            props = obj.get("properties") or {}
            if {"free_messages", "contact_email_set", "state", "course"} <= set(props):
                found.setdefault(path.name, []).append(obj)
    assert sorted(found) == sorted(OFFERING_EMBEDS)
    for name, objects in found.items():
        for obj in objects:
            assert obj["properties"].get("purged_at") == PURGED_AT, name
            assert "purged_at" in obj["required"], name

    get = _load("offerings.get.response.schema.json")
    sample = {"offering_id": UUID_SAMPLE, "code": "mc102-2031s1", "course": "Disciplina", "term": "2031s1",
              "timezone": "America/Sao_Paulo", "response_window_minutes": 1440, "state": "archived",
              "created_at": TIME_SAMPLE, "activated_at": None, "closed_at": None, "archived_at": TIME_SAMPLE,
              "bot": None, "free_messages": False, "contact_email_set": False, "purged_at": TIME_SAMPLE}
    assert _is_valid(get, sample)
    assert _is_valid(get, {**sample, "purged_at": None})
    assert not _is_valid(get, {k: v for k, v in sample.items() if k != "purged_at"})
    assert not _is_valid(get, {**sample, "purged_at": "2031-01-01T00:10:00+00:00"})

    # O publicado.
    doc = _published()
    missing = []
    for name, prefix in OFFERING_EMBEDS.items():
        op_id = name.removesuffix(".response.schema.json")
        if f"{prefix}purged_at" not in _paths(flatten(_success_schema(doc, op_id), doc)):
            missing.append(op_id)
    assert missing == [], f"purged_at ausente do Offering publicado em {missing}"


def test_events_list_person_parameter_and_optional_types() -> None:
    """A entrada congelada de `events.list` não muda
    (`GET /v1/events`, `events:read`, `events list`; o catálogo não descreve parâmetros).

    O publicado (vermelho no esqueleto): no OpenAPI, `events.list` tem o parâmetro de consulta `person`,
    opcional, um valor, com o padrão do pseudônimo de `people.get` (apelido ou UUID v4 canônico), e `types`
    tem `required: false` (a regra "obrigatório sem `person`" é da rota); `after` e `limit` também são de consulta."""
    entry = next(op for op in load_catalog()["operations"] if op["id"] == "events.list")
    assert (entry["method"], entry["path"], entry["permission"], entry["cli"]) == (
        "GET", "/v1/events", "events:read", "events list")

    doc = _published()
    params = _published_params(doc, "events.list")
    assert "person" in params, "parâmetro person ausente do OpenAPI publicado de events.list"
    person = params["person"]
    assert (person["in"], person.get("required", False)) == ("query", False)
    person_schema = _deref(person["schema"], doc)
    assert _type_set(person_schema) == {"string"}
    ref = _deref(_published_params(doc, "people.get")["ref"]["schema"], doc)
    assert person_schema.get("pattern") == ref["pattern"]
    assert (params["types"]["in"], params["types"].get("required", False)) == ("query", False)
    assert {name: p["in"] for name, p in params.items() if p["in"] == "query"} == {
        "types": "query", "after": "query", "limit": "query", "person": "query"}


# ---------------------------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------------------------


def test_purge_event_variants_match_the_event_contract() -> None:
    """As 2 variantes novas de `events.list` têm o
    envelope da API base e o `data` fechado com exatamente os campos esperados (só o identificador do
    oferecimento, o vencimento, as contagens ≥ 0 e os booleanos do contato; nenhum pseudônimo, nome ou texto
    livre), cada conjunto de campos distinto dos demais tipos; amostras válidas aceitas e inválidas
    recusadas.

    O publicado (vermelho no esqueleto): `EventType` tem os 2 tipos, e o `events.list` publicado (as
    variantes de `cq_server.api.schemas`) tem as 2 variantes com os mesmos campos."""
    schema = _load("events.list.response.schema.json")
    variants = _event_variants()
    assert set(PURGE_EVENT_TYPES) <= set(variants)
    for event_type, properties in EVENT_DATA.items():
        variant = variants[event_type]
        assert set(variant["properties"]) == {"seq", "type", "occurred_at", "actor_token_id", "data"}, event_type
        data = variant["properties"]["data"]
        assert (data["type"], data["additionalProperties"]) == ("object", False), event_type
        assert data["properties"] == properties, event_type
        assert data["required"] == list(properties), event_type
        assert _extension_keys(variant) == [], event_type
    keys = {event_type: frozenset(v["properties"]["data"]["properties"]) for event_type, v in variants.items()}
    for event_type in PURGE_EVENT_TYPES:
        others = {t for t, k in keys.items() if k == keys[event_type] and t != event_type}
        assert others == set(), (event_type, others)

    prepared = {"offering_id": UUID_SAMPLE, "expires_at": TIME_SAMPLE, "identities_to_delete": 2,
                "identities_kept": 0, "endpoints": 3, "updates": 9, "contact_email_set": False}
    purged = {"offering_id": UUID_SAMPLE, "identities_deleted": 2, "identities_kept": 0, "endpoints": 3,
              "updates": 9, "free_messages_redacted": 0, "outbox_redacted": 0, "contact_email_deleted": False}
    valid = [("offering.purge_prepared", prepared), ("offering.purged", purged),
             ("offering.purged", {**purged, "contact_email_deleted": True, "free_messages_redacted": 5})]
    invalid = [
        ("offering.purge_prepared", {**prepared, "identities_kept": -1}),
        ("offering.purge_prepared", {**prepared, "confirmation_code": CODE_SAMPLE}),
        ("offering.purge_prepared", {**prepared, "expires_at": "2031-01-01T00:10:00+00:00"}),
        ("offering.purge_prepared", {**prepared, "offering_id": "mc102-2031s1"}),
        ("offering.purge_prepared", purged),
        ("offering.purged", {**purged, "outbox_redacted": 1.5}),
        ("offering.purged", {**purged, "person": UUID_SAMPLE}),
        ("offering.purged", {k: v for k, v in purged.items() if k != "contact_email_deleted"}),
        ("offering.purged", prepared),
    ]
    for event_type, data in valid:
        assert _is_valid(schema, _event_page(event_type, data)), (event_type, data)
    for event_type, data in invalid:
        assert not _is_valid(schema, _event_page(event_type, data)), (event_type, data)

    # O publicado.
    from cq_server.events import EventType

    assert sorted(set(PURGE_EVENT_TYPES) - {e.value for e in EventType}) == [], "tipos novos ausentes de EventType"
    doc = _published()
    published = _published_event_variants(doc)
    assert sorted(set(PURGE_EVENT_TYPES) - set(published)) == [], "variantes novas ausentes do events.list publicado"
    for event_type, properties in EVENT_DATA.items():
        assert _data_properties(published[event_type], doc) == set(properties), event_type


# ---------------------------------------------------------------------------------------------
# Marcas e nomes
# ---------------------------------------------------------------------------------------------


def test_no_new_identity_secret_or_reserved_name() -> None:
    """Nenhum `x-identity` nem `x-secret` novo (continuam 11
    e 3 em `contracts/api/v1/schemas/`); nenhuma marca nos 4 arquivos novos da API, nas 2 variantes novas, no
    objeto `pending` do envelope e nos 5 esquemas do CLI; nenhum campo novo chamado `code` (fora de
    `offering.code`), `link`, `recipient`, `secret`, `until`, `username`, `token_id`, `worker_id`, `chat_id`
    ou `sender_id`.

    O publicado (vermelho no esqueleto): as respostas das 3 operações no OpenAPI, na forma
    achatada, sem marca e sem nome proibido."""
    identity = secret = 0
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = load_json(str(path))
        identity += _mark_points(schema, "x-identity")
        secret += _mark_points(schema, "x-secret")
    assert (identity, secret) == (11, 3)
    for name in DOCTOR_PURGE_API_FILES:
        schema = _load(name)
        assert _extension_keys(schema) == [], name
        assert _reserved({path for path, _ in _property_names(schema)}) == [], name
    for event_type in PURGE_EVENT_TYPES:
        data = _event_variants()[event_type]["properties"]["data"]
        assert _reserved(set(data["properties"])) == [], event_type
    assert _extension_keys(_load("error.schema.json")["properties"]["error"]["properties"]["pending"]) == []
    for command in CLI_COMMANDS:
        schema = _load_cli(command)
        assert _extension_keys(schema) == [], command
        assert _reserved({path for path, _ in _property_names(schema)}) == [], command

    # O publicado.
    doc = _published()
    for op_id in DOCTOR_PURGE_OPERATIONS:
        flat = flatten(_success_schema(doc, op_id), doc)
        assert [path for path, marks in flat if marks] == [], op_id
        assert _reserved(_paths(flat)) == [], op_id


# ---------------------------------------------------------------------------------------------
# CLI: comandos locais e esquemas das saídas `--json`
# ---------------------------------------------------------------------------------------------


def test_local_commands_disjoint_from_catalog_leaves() -> None:
    """`LOCAL_COMMANDS` (real no esqueleto) é exatamente
    `{up, down, status, logs}`, disjunto dos `cli` do catálogo e de toda folha das operações (nenhum comando
    local tem endpoint nem MCP).

    A implementação (vermelho no esqueleto): a raiz do `cq` tem os 4 comandos locais, folhas sem
    subcomando."""
    import click

    from cq_client.local_commands import LOCAL_COMMANDS

    catalog_leaves = {op["cli"] for op in load_catalog()["operations"]}
    assert LOCAL_COMMANDS == frozenset({"up", "down", "status", "logs"})
    assert catalog_leaves.isdisjoint(LOCAL_COMMANDS)
    assert {leaf.split()[0] for leaf in catalog_leaves}.isdisjoint(LOCAL_COMMANDS)

    from cq_client.cli import cli

    ctx = click.Context(cli)
    present = {name: cli.get_command(ctx, name) for name in sorted(LOCAL_COMMANDS)}
    assert sorted(name for name, cmd in present.items() if cmd is None) == [], "comandos locais ausentes do cli"
    assert sorted(name for name, cmd in present.items() if isinstance(cmd, click.Group)) == []


def _local_profile(tmp_path: Path) -> Path:
    """O perfil local nomeado dos casos de sucesso: o endereço de loopback com porta explícita
    (`TEST_API_URL`, igual à porta do `FakeStack`), o token inicial dos testes e um diretório de pilha com os
    marcadores e o `.env` de teste."""
    stack_dir = make_stack_dir(tmp_path)
    return write_profile(tmp_path, (
        'default_profile = "local"\n\n'
        "[profiles.local]\n"
        'kind = "local"\n'
        f'api_url = "{TEST_API_URL}"\n'
        f'token = "{ADMIN_TOKEN}"\n'
        f'stack_dir = "{stack_dir.as_posix()}"\n'
        'compose_project = "cq"\n'))


def _real_output(command: str, client: Any, tmp_path: Path) -> Any:
    """A saída `--json` real de `cq <command>` com o perfil local, a pilha pelo `FakeStack` (os quatro
    serviços "ok"; no `up`, parada e depois saudável) e a API dos testes por `http=`."""
    config = _local_profile(tmp_path)
    sleep = FakeSleep(FakeClock())
    obj: dict[str, Any] = {"http": client, "stack": FakeStack(["healthy"]), "clock": sleep.monotonic,
                           "sleep": sleep}
    if command == "up":
        obj["stack"] = FakeStack(["stopped", "healthy"])
    result = invoke_cli([command], json=True, obj=obj, config=config, tmp_path=tmp_path)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    # O `status` e o `doctor` imprimem o relatório no stdout também com o código 1.
    assert result.exit_code in ((0, 1) if command in ("status", "doctor") else (0,)), (
        command, result.exit_code, result.stderr[-500:])
    return assert_cli_json(command, result.stdout)


@pytest.mark.parametrize("command", CLI_COMMANDS)
def test_cli_output_schema_is_valid_closed_and_matches_real_output(command: str, client: Any,
                                                                   tmp_path: Path) -> None:
    """O esquema congelado da saída `--json` de
    `cq <command>` (`contracts/cli/v1/schemas/<command>.output.schema.json`) é JSON Schema 2020-12 válido, com
    `$id` único, todo objeto fechado e toda propriedade obrigatória (anulável como `type: [X, "null"]`), e
    com as formas do contrato: o `ProfileView` (o `api_url` sem `user:senha@`, os rótulos de origem), o
    `ServiceView`, os avisos do `status` (o `health` igual ao `system.health` congelado, anulável), os 26
    identificadores, as 3 partes e os 5 resultados do `doctor` (iguais aos de `cq_client.doctor`, reais no
    esqueleto), o projeto e a porta do `up`, o projeto do `down` e as linhas do `logs`.

    A implementação (vermelho no esqueleto): o comando existe na árvore do `cq`, e a saída
    `--json` real (perfil local, `FakeStack`, a API dos testes) valida contra o esquema."""
    schema = _load_cli(command)
    assert schema.get("$schema") == DRAFT_2020_12
    assert validator_for(schema) is Draft202012Validator
    Draft202012Validator.check_schema(schema)
    ids = [_load_cli(c).get("$id") for c in CLI_COMMANDS]
    assert len(set(ids)) == len(ids)
    assert schema["$id"].endswith(f"/contracts/cli/v1/schemas/{command}.output.schema.json")
    objects = list(iter_objects(schema, schema))
    assert objects and _type_set(schema) == {"object"}
    for obj in objects:
        assert obj.get("additionalProperties") is False, (command, sorted(obj.get("properties", {})))
        assert set(obj.get("required", [])) == set(obj.get("properties", {})), command
    for key in ("allOf", "anyOf", "oneOf"):
        assert key not in json.dumps(schema), command

    props = schema["properties"]
    if command in ("status", "doctor"):
        assert props["profile"] == PROFILE_VIEW
        url = Draft202012Validator(PROFILE_VIEW["properties"]["api_url"])
        assert url.is_valid("http://127.0.0.1:18765") and url.is_valid("https://cq.exemplo.br/base")
        assert not url.is_valid("https://usuario:senha-sentinela@cq.exemplo.br")
    if command == "status":
        assert props["services"] == {"type": ["array", "null"], "items": SERVICE_VIEW}
        assert props["api_port"] == {"type": ["integer", "null"], **_PORT}
        assert props["warnings"] == {"type": "array", "items": {"enum": STATUS_WARNINGS}}
        api = props["api"]
        assert (api["required"], api["properties"]["reachable"], api["properties"]["owned_by_stack"]) == (
            ["reachable", "health", "owned_by_stack"], _BOOL, {"type": ["boolean", "null"]})
        health = api["properties"]["health"]
        frozen_health = _body(_load("system.health.response.schema.json"))
        assert {**health, "type": "object"} == frozen_health and health["type"] == ["object", "null"]
    elif command == "doctor":
        check = props["checks"]["items"]
        assert check["required"] == ["id", "part", "result", "message", "offering", "count", "detail"]
        assert check["properties"] == {
            "id": {"enum": CLIENT_CHECK_IDS}, "part": {"enum": DOCTOR_PARTS}, "result": {"enum": DOCTOR_RESULTS},
            "message": {"type": "string", "minLength": 1},
            "offering": {"type": ["string", "null"], "pattern": CODE_PATTERN},
            "count": {"type": ["integer", "null"], "minimum": 0}, "detail": {"type": ["string", "null"]}}
        assert set(SERVER_CHECK_IDS) <= set(CLIENT_CHECK_IDS)
        from cq_client import doctor as client_doctor

        assert list(client_doctor.CHECK_IDS) == CLIENT_CHECK_IDS
        # A parte de cada id no cliente (volume e disco na `local`).
        assert {i: client_doctor.CHECK_PARTS[i] for i in ("backup.age", "smtp.server", "retention.overdue",
                                                          "postgres.volume", "disk.space")} == {
            "backup.age": "api", "smtp.server": "api", "retention.overdue": "api", "postgres.volume": "local",
            "disk.space": "local"}
    elif command == "up":
        assert props == {"project": {"type": "string", "pattern": PROJECT_PATTERN},
                         "services": {"type": "array", "items": SERVICE_VIEW},
                         "api_port": {"type": "integer", **_PORT}}
    elif command == "down":
        assert props == {"project": {"type": "string", "pattern": PROJECT_PATTERN}}
    else:
        assert props == {"lines": {"type": "array", "items": {"type": "string"}}}

    # A implementação: o comando existe e a saída `--json` real valida contra o esquema.
    import click

    from cq_client.cli import cli

    assert cli.get_command(click.Context(cli), command) is not None, f"comando {command} ausente da árvore do cli"
    _real_output(command, client, tmp_path)
