"""Ferramentas do catálogo pelo servidor MCP: cada operação pela ferramenta certa, o roteiro de operação só
pelas ferramentas, a validação das entradas, o perfil, os erros de execução e o teto do texto do
resultado.

O Claude Code é sempre o dublê em processo (`tests/helpers/mcp_client.py`), nas duas eras (`MODES`) quando o caso pede.
A API é a dos testes (o `client` da API base, um `TestClient` que atende qualquer host) injetada como `ServerDeps.http`; os
pedidos e as respostas são observados pelos ganchos `event_hooks["request"]` e `event_hooks["response"]` do próprio
cliente HTTP, como nos testes do CLI. Os casos que não precisam do banco usam uma API dublê (`_FakeApi`, um
`httpx2.Client` com transporte falso que anota cada pedido). O perfil é sempre um arquivo do `tmp_path` por
`CQ_CONFIG` (o `make_deps` exige), nunca o do desenvolvedor.

Escolhas deste arquivo ([I]):
- o texto de um erro de execução com as linhas a mais do envelope de erro (permissão, campos, linhas, pendências, a dica
  do CLI) é a primeira linha `"[<código>] <mensagem>"` seguida de cada linha a mais, separadas por `"\n"`, nessa
  ordem (permissão, campos, pendências); o separador é escolha deste teste, e o CLI também põe cada uma numa
  linha (`cli.py:477-490`);
- o motivo de `Campo …: …` e de `Linha N: …` é o `reason` do envelope, literal (o `_field_line` do CLI);
- o evento de auditoria com `client: mcp` existe em três tipos do contrato (**[F]**
  `events.list.response.schema.json`): `identity.revealed` (`client`) e `dispatch.prepared` (`origin`, da origem do
  `X-CQ-Client`), os dois conferidos aqui, e `identity.reveal_denied` (`client`);
- a semente das ferramentas que dependem de estado é a semente `seed_state` (`tests/helpers/calls.py`) com os
  menores totais, e os argumentos de cada ferramenta repetem os do `VALID_CALLS` da operação.

Casos que dependem das ferramentas das duas fases e da revelação: a chamada certa das 8
ferramentas das duas fases (`prepare_archive_offering`, `archive_offering`, `prepare_send_invites`, `send_invites`,
`prepare_dispatch`, `confirm_dispatch`, `prepare_purge_offering`, `purge_offering`), inclusive o roteiro de
operação, que prepara um disparo, e a da `reveal_identities`.

No esqueleto, toda função falha no `build_server` (`NotImplementedError`), dentro de `in_process`.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.memory import create_client_server_memory_streams

from cq_client.mcp_server import (
    INVALID_PARAMS_CODE,
    MAX_RESULT_CHARS,
    MCP_HINT_TEMPLATE,
    MCP_MESSAGES,
    MCP_STDERR_MESSAGES,
    PROFILE_ERROR_CLASSES,
    PROTOCOL_MESSAGES,
    TOOL_NAMES,
    build_server,
)

from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_offering, create_token, register_bot, write_roster
from tests.helpers.calls import SEED_QUESTION, CallState, seed_state
from tests.helpers.canary import TG_ZEFIRO, ZEFIRO
from tests.helpers.cli_runner import write_profile
from tests.helpers.contract import load_catalog, load_schema
from tests.helpers.mcp_client import MODES, Outcome, call, in_process, list_tools, make_deps, run, scan
from tests.helpers.mcp_client import unreachable_http
from tests.helpers.secrets import all_secrets
from tests.helpers.stack_fake import TEST_API_URL, FakeStack

# --------------------------------------------------------------------------------------------------
# O catálogo exposto (nenhum total literal: a lista sai do catálogo congelado)
# --------------------------------------------------------------------------------------------------

_OPERATIONS: tuple[dict, ...] = tuple(load_catalog()["operations"])
OP_BY_TOOL: dict[str, dict] = {op["mcp"]["tool"]: op for op in _OPERATIONS if op["mcp"]["exposed"]}
CATALOG_TOOLS: tuple[str, ...] = tuple(OP_BY_TOOL)

# As 8 ferramentas das duas fases.
TWO_PHASE_TOOLS = frozenset({"prepare_archive_offering", "archive_offering", "prepare_send_invites", "send_invites",
                             "prepare_dispatch", "confirm_dispatch", "prepare_purge_offering", "purge_offering"})

# Endereços e tokens dos perfis de teste (nenhum é a porta 8000; o `TestClient` atende qualquer host).
UNREACHABLE_URL = "http://127.0.0.1:9"
REMOTE_URL = "https://api-remota.cq.test"
PROFILE_ONE_URL = "https://perfil-um.cq.test"
PROFILE_TWO_URL = "https://perfil-dois.cq.test"
ENV_URL = "https://api-ambiente.cq.test"
REFUSED_TOKEN = "cq_tokenrecusadomcp_" + "r" * 28
TOP_TOKEN = "cq_tokendotopomcp_" + "t" * 30

# A razão neutra da revelação (nenhuma identidade nem palavra canário).
REVEAL_REASON = "conferencia de apoio pelo agente"

# O teto do texto de um resultado como literal (50.000 caracteres serializados), e não a
# constante `MAX_RESULT_CHARS` do código de produção, que não é congelado: a semente e as medidas do teste não podem
# crescer junto com a constante.
RESULT_LIMIT_CHARS = 50_000

# Valores estáticos válidos pelo esquema de entrada, para os casos em que a API não é alcançada.
STATIC_OFFERING = "oferta-estatica-mcp"
STATIC_DISPATCH = "0f8fad5b-d9cb-469f-a165-70867728950e"
STATIC_PERSON = "abc123"
STATIC_CODE = "codigo-estatico-mcp"

_counter = itertools.count(1)


def _unique(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def _neutral_row(name: str) -> dict[str, str]:
    """Uma linha neutra da lista (fora dos alvos canário), com RA e e-mail únicos."""
    n = next(_counter)
    return {"name": name, "ra": f"66{n:08d}", "email": f"aluno.mcp.{n}@turma.example.org"}


# --------------------------------------------------------------------------------------------------
# Ajudantes de observação, de perfil e de asserção
# --------------------------------------------------------------------------------------------------


@dataclass
class Seen:
    """Os pedidos e as respostas observados pelos ganchos do cliente HTTP."""

    requests: list[httpx2.Request] = field(default_factory=list)
    responses: list[httpx2.Response] = field(default_factory=list)

    def bodies(self) -> list[Any]:
        return [response.json() for response in self.responses]


@contextlib.contextmanager
def observed(http: Any) -> Iterator[Seen]:
    """Liga os ganchos de pedido e de resposta do cliente HTTP (o `TestClient` da API) enquanto o bloco roda."""
    seen = Seen()

    def on_request(request: httpx2.Request) -> None:
        seen.requests.append(request)

    def on_response(response: httpx2.Response) -> None:
        response.read()
        seen.responses.append(response)

    request_hooks = http.event_hooks["request"]
    response_hooks = http.event_hooks["response"]
    request_hooks.append(on_request)
    response_hooks.append(on_response)
    try:
        yield seen
    finally:
        request_hooks.remove(on_request)
        response_hooks.remove(on_response)


def query_of(request: httpx2.Request) -> dict[str, list[str]]:
    """A consulta exata de um pedido: cada chave com os seus valores, na ordem em que foram enviados."""
    query: dict[str, list[str]] = {}
    for key, value in request.url.params.multi_items():
        query.setdefault(key, []).append(value)
    return query


def json_body(request: httpx2.Request) -> Any:
    """O corpo JSON do pedido, ou `None` sem corpo."""
    content = request.content
    return json.loads(content) if content else None


class FakeApi:
    """A API dublê: um `httpx2.Client` com transporte falso que anota cada pedido e responde por `respond`. Sem
    `respond`, responde 500 com o envelope `internal` (um pedido inesperado nunca vira sucesso por acaso)."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._respond = respond or (lambda request: envelope_response(500, "internal"))
        self.http = httpx2.Client(transport=httpx2.MockTransport(self._handle))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._respond(request)


_API_ERROR_MESSAGES = {
    "internal": "Erro interno. Tente de novo mais tarde.",
    "purge_pending": "Há trabalho pendente neste oferecimento; o expurgo só pode ser feito depois que ele terminar.",
}


def envelope_response(status: int, code: str, **extra: Any) -> httpx2.Response:
    """Uma resposta com o envelope de erro do contrato (conferido contra `error.schema.json`)."""
    body = {"error": {"code": code, "message": _API_ERROR_MESSAGES[code], **extra}}
    Draft202012Validator(load_schema("error", "")).validate(body)
    return httpx2.Response(status, json=body)


def profile_env(tmp_path: Path, *, api_url: str = TEST_API_URL, token: str | None = ADMIN_TOKEN,
                extra: str = "") -> dict[str, str]:
    """O `env` do `ServerDeps` com um arquivo de perfil do `tmp_path`: o perfil implícito (chaves de topo) com o
    endereço e o token dados (com o `TEST_API_URL`, um perfil local, no loopback) e o texto `extra` no fim."""
    lines = [f'api_url = "{api_url}"']
    if token is not None:
        lines.append(f'token = "{token}"')
    path = write_profile(tmp_path, "\n".join(lines) + "\n" + extra)
    return {"CQ_CONFIG": str(path)}


def secrets(*extra: str) -> list[str]:
    """Os segredos conhecidos (token inicial, chave-mestra, os lembrados) e os dados."""
    return all_secrets(*extra)


def assert_clean(outcome: Any, *extra_secrets: str) -> None:
    """Nenhuma identidade canário nem segredo no texto, no estruturado e no bruto."""
    assert scan(outcome, secrets(*extra_secrets)) == []


def compact(body: Any) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def assert_success(outcome: Outcome, body: Any) -> None:
    """o corpo inteiro no `structuredContent` e o mesmo JSON, compacto, no único `TextContent`."""
    assert not outcome.is_protocol_error, (outcome.protocol_code, outcome.protocol_message)
    assert outcome.is_error is False, outcome.text
    assert outcome.structured == body
    assert [item["type"] for item in outcome.raw["content"]] == ["text"]
    assert outcome.text == compact(body)


def error_text(key: str, *lines: str, code: str | None = None) -> str:
    """O texto exato de um erro de execução: `"[<código>] <mensagem>"` e as linhas a mais, por `"\\n"`."""
    shown = code if code is not None else (key[len("api."):] if key.startswith("api.") else key)
    return "\n".join([f"[{shown}] {MCP_MESSAGES[key]}", *lines])


def assert_execution_error(outcome: Outcome, text: str) -> None:
    """`isError` verdadeiro, um `TextContent` com o texto exato e nenhum estruturado."""
    assert not outcome.is_protocol_error, (outcome.protocol_code, outcome.protocol_message)
    assert outcome.is_error is True
    assert outcome.structured is None
    assert [item["type"] for item in outcome.raw["content"]] == ["text"]
    assert outcome.text == text


def assert_invalid_params(outcome: Outcome, key: str = "invalid_arguments") -> None:
    """`-32602`, a `message` exata e nenhum `data`."""
    assert outcome.is_protocol_error, outcome.text
    assert outcome.protocol_code == INVALID_PARAMS_CODE
    assert outcome.protocol_message == PROTOCOL_MESSAGES[key]
    assert "data" not in outcome.raw, outcome.raw


def assert_no_echo(outcome: Outcome, *values: str) -> None:
    dumped = json.dumps(outcome.raw, ensure_ascii=False) + (outcome.text or "") + (outcome.protocol_message or "")
    for value in values:
        assert value not in dumped, f"eco de {value!r}"


def api_get(client: Any, path: str, query: Mapping[str, Sequence[str]] | None = None) -> Any:
    """A mesma leitura, direto na API, com o token inicial (o valor esperado de uma ferramenta de leitura)."""
    params = [(key, value) for key, values in (query or {}).items() for value in values]
    resp = client.get(path, params=params, headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"GET {path}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def route(tool: str, **params: str) -> str:
    """O caminho da operação da ferramenta, montado sobre o modelo do catálogo."""
    return OP_BY_TOOL[tool]["path"].format(**params)


def published(tools: list[dict]) -> dict[str, dict]:
    return {tool["name"]: tool for tool in tools}


# --------------------------------------------------------------------------------------------------
# Os argumentos e o pedido esperado de cada ferramenta do catálogo (como o `VALID_CALLS` da operação)
# --------------------------------------------------------------------------------------------------

_NO_BODY: Any = object()


@dataclass(frozen=True)
class Expected:
    """O que uma chamada válida da ferramenta manda: os argumentos, os parâmetros do caminho do catálogo, a
    consulta exata e o corpo (`_NO_BODY` = pedido sem corpo)."""

    arguments: dict[str, Any]
    path_params: dict[str, str]
    query: dict[str, list[str]] = field(default_factory=dict)
    body: Any = _NO_BODY


def _draft(s: CallState) -> str:
    assert s.draft_offering is not None
    return s.draft_offering["code"]


def _active(s: CallState) -> str:
    assert s.active_offering is not None
    return s.active_offering["code"]


def _invite(s: CallState) -> str:
    assert s.invite_offering is not None
    return s.invite_offering["code"]


def _dispatch(s: CallState) -> str:
    assert s.dispatch_offering is not None
    return s.dispatch_offering["code"]


def _report(s: CallState) -> tuple[str, str]:
    assert s.report_offering is not None and s.report_shown is not None
    return s.report_offering["code"], s.report_shown


def _import_case(s: CallState, tmp_path: Path) -> Expected:
    assert s.import_offering is not None
    row = _neutral_row("Aluno Neutro Ferramenta")
    path = write_roster(tmp_path / "lista-ferramenta.csv", [row])
    code = s.import_offering["code"]
    return Expected({"offering": code, "path": str(path.resolve())}, {"ref": code}, body={"rows": [row]})


def _report_case(tool: str, **extra: Any) -> Callable[[CallState, Path], Expected]:
    def build(s: CallState, tmp_path: Path) -> Expected:
        code, dispatch_id = _report(s)
        return Expected({"offering": code, "dispatch_id": dispatch_id, **extra},
                        {"ref": code, "dispatch_id": dispatch_id})
    return build


def _create_case(s: CallState, tmp_path: Path) -> Expected:
    body = {"code": _unique("oferta-mcp"), "course": "Disciplina Valida Ferramenta", "term": "2031s1",
            "timezone": "America/Sao_Paulo"}
    return Expected(dict(body), {}, body=body)


def _archive_case(s: CallState, tmp_path: Path) -> Expected:
    code, confirmation = s.archive_targets[0]
    return Expected({"offering": code, "confirmation_code": confirmation}, {"ref": code},
                    body={"confirmation_code": confirmation})


def _send_case(s: CallState, tmp_path: Path) -> Expected:
    code, confirmation = s.invite_targets[0]
    return Expected({"offering": code, "confirmation_code": confirmation}, {"ref": code},
                    body={"confirmation_code": confirmation})


def _purge_case(s: CallState, tmp_path: Path) -> Expected:
    code, confirmation = s.purge_targets[0]
    return Expected({"offering": code, "confirmation_code": confirmation}, {"ref": code},
                    body={"confirmation_code": confirmation})


def _resend_case(s: CallState, tmp_path: Path) -> Expected:
    assert s.resend_person is not None
    alias = s.resend_person["alias"]
    return Expected({"offering": _invite(s), "pseudonyms": [alias]}, {"ref": _invite(s)},
                    body={"pseudonyms": [alias]})


def _dispatch_id_case(targets: Callable[[CallState], str]) -> Callable[[CallState, Path], Expected]:
    def build(s: CallState, tmp_path: Path) -> Expected:
        dispatch_id = targets(s)
        return Expected({"offering": _dispatch(s), "dispatch_id": dispatch_id},
                        {"ref": _dispatch(s), "dispatch_id": dispatch_id})
    return build


CASES: dict[str, Callable[[CallState, Path], Expected]] = {
    "list_people": lambda s, t: Expected({}, {}),
    "get_person": lambda s, t: Expected({"person": s.person["pseudo_id"]}, {"ref": s.person["pseudo_id"]}),
    "reveal_identities": lambda s, t: Expected(
        {"pseudonyms": [s.person["pseudo_id"]], "reason": REVEAL_REASON}, {},
        body={"pseudonyms": [s.person["pseudo_id"]], "reason": REVEAL_REASON}),
    "list_events": lambda s, t: Expected({"types": ["person.registered"]}, {},
                                         query={"types": ["person.registered"]}),
    "create_offering": _create_case,
    "list_offerings": lambda s, t: Expected({}, {}),
    "get_offering": lambda s, t: Expected({"offering": _draft(s)}, {"ref": _draft(s)}),
    "close_offering": lambda s, t: Expected({"offering": _active(s)}, {"ref": _active(s)}),
    "prepare_archive_offering": lambda s, t: Expected({"offering": s.archive_targets[0][0]},
                                                      {"ref": s.archive_targets[0][0]}),
    "archive_offering": _archive_case,
    "activate_bot": lambda s, t: Expected({"offering": _draft(s)}, {"ref": _draft(s)}),
    "get_bot": lambda s, t: Expected({"offering": _draft(s)}, {"ref": _draft(s)}),
    "import_students": _import_case,
    "get_students_summary": lambda s, t: Expected({"offering": _invite(s)}, {"ref": _invite(s)}),
    "prepare_send_invites": lambda s, t: Expected({"offering": _invite(s), "valid_hours": 24}, {"ref": _invite(s)},
                                                  body={"valid_hours": 24}),
    "send_invites": _send_case,
    "resend_invites": _resend_case,
    "prepare_dispatch": lambda s, t: Expected({"offering": _dispatch(s), **SEED_QUESTION}, {"ref": _dispatch(s)},
                                              body=dict(SEED_QUESTION)),
    "get_dispatch": _dispatch_id_case(lambda s: s.dispatch_shown or ""),
    "list_dispatches": lambda s, t: Expected({"offering": _dispatch(s)}, {"ref": _dispatch(s)}),
    "confirm_dispatch": _dispatch_id_case(lambda s: s.confirm_targets[0]),
    "cancel_dispatch": _dispatch_id_case(lambda s: s.cancel_targets[0]),
    "update_offering_settings": lambda s, t: Expected({"offering": _draft(s), "free_messages": True},
                                                      {"ref": _draft(s)}, body={"free_messages": True}),
    "get_dispatch_report": _report_case("get_dispatch_report"),
    "list_report_versions": _report_case("list_report_versions"),
    "resend_report_summary": _report_case("resend_report_summary"),
    "get_offering_digest": lambda s, t: Expected({"offering": _report(s)[0]}, {"ref": _report(s)[0]}),
    "update_offering_digest": lambda s, t: Expected({"offering": _report(s)[0], "local_time": "21:00"},
                                                    {"ref": _report(s)[0]}, body={"local_time": "21:00"}),
    "run_doctor": lambda s, t: Expected({}, {}),
    "prepare_purge_offering": lambda s, t: Expected({"offering": s.prepare_targets[0]}, {"ref": s.prepare_targets[0]}),
    "purge_offering": _purge_case,
}


def static_arguments(tool: str, roster_path: Path) -> dict[str, Any]:
    """Argumentos válidos pelo esquema de entrada, sem estado (para os casos em que a API não é alcançada)."""
    offering = {"offering": STATIC_OFFERING}
    dispatch = {**offering, "dispatch_id": STATIC_DISPATCH}
    confirm = {**offering, "confirmation_code": STATIC_CODE}
    table: dict[str, dict[str, Any]] = {
        "list_people": {}, "get_person": {"person": STATIC_PERSON},
        "reveal_identities": {"pseudonyms": [STATIC_PERSON], "reason": REVEAL_REASON},
        "list_events": {"types": ["person.registered"]},
        "create_offering": {"code": STATIC_OFFERING, "course": "Disciplina Estatica", "term": "2031s1",
                            "timezone": "America/Sao_Paulo"},
        "list_offerings": {}, "get_offering": offering, "close_offering": offering,
        "prepare_archive_offering": offering, "archive_offering": confirm, "activate_bot": offering,
        "get_bot": offering, "import_students": {**offering, "path": str(roster_path)},
        "get_students_summary": offering, "prepare_send_invites": offering, "send_invites": confirm,
        "resend_invites": {**offering, "pseudonyms": [STATIC_PERSON]},
        "prepare_dispatch": {**offering, **SEED_QUESTION}, "get_dispatch": dispatch,
        "list_dispatches": offering, "confirm_dispatch": dispatch, "cancel_dispatch": dispatch,
        "update_offering_settings": {**offering, "free_messages": True}, "get_dispatch_report": dispatch,
        "list_report_versions": dispatch, "resend_report_summary": dispatch, "get_offering_digest": offering,
        "update_offering_digest": {**offering, "local_time": "21:00"}, "run_doctor": {},
        "prepare_purge_offering": offering, "purge_offering": confirm,
    }
    assert set(table) == set(CATALOG_TOOLS), "a tabela de argumentos estáticos não cobre o catálogo exposto"
    return table[tool]


def static_roster(tmp_path: Path) -> Path:
    """Um arquivo da lista válido, com uma linha neutra, pelo caminho absoluto."""
    return write_roster(tmp_path / "lista-estatica.csv", [_neutral_row("Aluno Neutro Estatico")]).resolve()


def seed(client: Any, **totals: int) -> CallState:
    """A semente (`seed_state`) com os menores totais (um alvo de cada; nenhum alvo de revogação)."""
    counts = {"revoke_targets": 0, "archive_targets": 1, "invite_targets": 1, "dispatch_targets": 1,
              "report_targets": 1, "prepare_targets": 1, "purge_targets": 1}
    counts.update(totals)
    return seed_state(client, **counts)


# --------------------------------------------------------------------------------------------------
# Cada ferramenta do catálogo: a operação certa, uma vez, com o `X-CQ-Client: mcp` (nas duas eras)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_catalog_cases_cover_the_exposed_catalog(mode: str, tmp_path: Path) -> None:
    """Guarda do próprio arquivo, junto de um caso vermelho: os casos e os argumentos estáticos cobrem
    exatamente as ferramentas do catálogo exposto, e o `tools/list` real (`NotImplementedError` em `build_server`
    até o servidor existir) as lista todas, na ordem do catálogo."""
    assert set(CASES) == set(CATALOG_TOOLS)
    static_arguments("list_people", static_roster(tmp_path))
    assert [name for name in TOOL_NAMES if name in OP_BY_TOOL] == list(CATALOG_TOOLS)
    deps = make_deps(tmp_path, http=FakeApi().http)

    async def scenario() -> list[dict]:
        async with in_process(deps, mode) as mc:
            return await list_tools(mc)

    names = [tool["name"] for tool in run(scenario)]
    assert [name for name in names if name in OP_BY_TOOL] == list(CATALOG_TOOLS)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", CATALOG_TOOLS)
def test_catalog_tool_calls_its_operation_once(tool: str, mode: str, client: Any, tmp_path: Path) -> None:
    """a ferramenta chama exatamente uma vez a operação do catálogo
    (método e rota observados pelo gancho de pedido), com `X-CQ-Client: mcp` e o token do perfil, a consulta e o
    corpo esperados, e devolve o corpo da API inteiro no `structuredContent` e o mesmo JSON no texto, válido contra
    o esquema de saída publicado no `tools/list`. O evento de auditoria da operação, quando existe, tem a origem
    `mcp` (`dispatch.prepared.origin`, `identity.revealed.client`). Nada canário nem segredo na saída."""
    state = seed(client)
    expected = CASES[tool](state, tmp_path)
    op = OP_BY_TOOL[tool]
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[dict, Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            tools = published(await list_tools(mc))
            with observed(client) as seen:
                outcome = await call(mc, tool, expected.arguments)
            return tools[tool], outcome, seen

    wire, outcome, seen = run(scenario)

    assert [(r.method, r.url.path) for r in seen.requests] == [(op["method"], route(tool, **expected.path_params))]
    request = seen.requests[0]
    assert request.headers.get("X-CQ-Client") == "mcp"
    assert request.headers.get("Authorization") == f"Bearer {ADMIN_TOKEN}"
    assert query_of(request) == expected.query
    if expected.body is _NO_BODY:
        assert request.content == b""
    else:
        assert json_body(request) == expected.body
    assert len(seen.responses) == 1 and 200 <= seen.responses[0].status_code < 300, seen.responses
    body = seen.bodies()[0]
    assert_success(outcome, body)
    Draft202012Validator(wire["outputSchema"]).validate(outcome.structured)
    assert_clean(outcome)

    if tool == "prepare_dispatch":
        prepared = [e for e in all_events(client, ADMIN_TOKEN, "dispatch.prepared")
                    if e["data"]["dispatch_id"] == body["dispatch_id"]]
        assert [e["data"]["origin"] for e in prepared] == ["mcp"]
    if tool == "reveal_identities":
        revealed = [e["data"] for e in all_events(client, ADMIN_TOKEN, "identity.revealed")]
        assert revealed == [{"client": "mcp", "revealed": [state.person["pseudo_id"]], "not_found_count": 0,
                             "reason": REVEAL_REASON}]


@pytest.mark.parametrize("mode", MODES)
def test_get_offering_equals_the_api(mode: str, client: Any, tmp_path: Path) -> None:
    """`get_offering`, pelo código e pelo identificador, devolve exatamente o que a API devolve ao token
    inicial para o mesmo oferecimento."""
    offering = create_offering(client, ADMIN_TOKEN, code=_unique("consulta-mcp"), course="Disciplina Consulta",
                               term="2031s1", timezone="America/Sao_Paulo")
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> list[Outcome]:
        async with in_process(deps, mode) as mc:
            return [await call(mc, "get_offering", {"offering": ref})
                    for ref in (offering["code"], offering["offering_id"])]

    for outcome in run(scenario):
        assert_success(outcome, api_get(client, route("get_offering", ref=offering["code"])))
        assert_clean(outcome)


# --------------------------------------------------------------------------------------------------
# O roteiro de operação, numa função
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_operation_walkthrough(mode: str, client: Any, bot_api: Any, tmp_path: Path) -> None:
    """Roteiro de operação: só pelas ferramentas, `create_offering` → (o operador registra o bot no
    terminal dele) `activate_bot` → `import_students` por caminho absoluto de um arquivo fictício com 3 alunos
    (só as contagens voltam) → `get_students_summary` → `prepare_dispatch` (evento com origem `mcp`) e
    `list_dispatches` → `get_dispatch_report` (do disparo fechado da semente) → `list_events` com `person` (o
    pseudônimo de um aluno importado). Cada resultado é o corpo da API; nenhum nome, RA, e-mail ou caminho do
    arquivo sai em nenhum resultado. Depende do `prepare_dispatch`."""
    state = seed(client, archive_targets=0, invite_targets=0, dispatch_targets=0, prepare_targets=0,
                 purge_targets=0)
    report_code, report_dispatch = _report(state)
    rows = [_neutral_row(f"Aluno Roteiro {label}") for label in ("Um", "Dois", "Tres")]
    roster = write_roster(tmp_path / "turma-roteiro.csv", rows).resolve()
    people_before = {p["pseudo_id"] for p in api_get(client, "/v1/people", {"limit": ["500"]})["items"]}
    code = _unique("roteiro-mcp")
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)
    outcomes: list[Outcome] = []

    async def scenario() -> dict[str, Any]:
        results: dict[str, Any] = {}
        async with in_process(deps, mode) as mc:
            async def step(name: str, arguments: dict[str, Any]) -> Outcome:
                with observed(client) as seen:
                    outcome = await call(mc, name, arguments)
                outcomes.append(outcome)
                assert len(seen.requests) == 1, (name, [r.url.path for r in seen.requests])
                assert seen.requests[0].headers.get("X-CQ-Client") == "mcp"
                results[name + ":api"] = seen.bodies()[0] if seen.responses else None
                results[name + ":request"] = seen.requests[0]
                return outcome

            results["create"] = await step("create_offering", {
                "code": code, "course": "Algoritmos Roteiro Agente", "term": "2031s2",
                "timezone": "America/Sao_Paulo"})
            # O passo do operador, no terminal dele (fora do laço de eventos do cliente MCP).
            bot_id = bot_api.add_bot()
            await anyio.to_thread.run_sync(lambda: register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_id)))
            results["activate"] = await step("activate_bot", {"offering": code})
            results["import"] = await step("import_students", {"offering": code, "path": str(roster)})
            results["summary"] = await step("get_students_summary", {"offering": code})
            results["prepare"] = await step("prepare_dispatch", {"offering": code, **SEED_QUESTION})
            results["list"] = await step("list_dispatches", {"offering": code})
            results["report"] = await step("get_dispatch_report",
                                           {"offering": report_code, "dispatch_id": report_dispatch})
            people_after = await anyio.to_thread.run_sync(lambda: api_get(client, "/v1/people", {"limit": ["500"]}))
            new_people = [p["pseudo_id"] for p in people_after["items"] if p["pseudo_id"] not in people_before]
            results["new_people"] = new_people
            results["events"] = await step("list_events", {"person": new_people[0]})
        return results

    results = run(scenario)

    for name, key in (("create_offering", "create"), ("activate_bot", "activate"), ("import_students", "import"),
                      ("get_students_summary", "summary"), ("prepare_dispatch", "prepare"),
                      ("list_dispatches", "list"), ("get_dispatch_report", "report"), ("list_events", "events")):
        assert_success(results[key], results[name + ":api"])
    assert results["create"].structured["code"] == code
    assert results["activate"].structured["state"] == "active"
    imported = results["import"].structured
    assert {k: imported[k] for k in ("received", "people_created", "people_reused", "enrollments_created",
                                     "already_enrolled")} == {"received": 3, "people_created": 3,
                                                              "people_reused": 0, "enrollments_created": 3,
                                                              "already_enrolled": 0}
    assert json_body(results["import_students:request"]) == {"rows": rows}
    assert results["summary"].structured == api_get(client, route("get_students_summary", ref=code))
    dispatch_id = results["prepare"].structured["dispatch_id"]
    assert dispatch_id in [item["dispatch_id"] for item in results["list"].structured["items"]]
    prepared = [e for e in all_events(client, ADMIN_TOKEN, "dispatch.prepared")
                if e["data"]["dispatch_id"] == dispatch_id]
    assert [e["data"]["origin"] for e in prepared] == ["mcp"]
    assert results["report"].structured == api_get(
        client, route("get_dispatch_report", ref=report_code, dispatch_id=report_dispatch))
    assert len(results["new_people"]) == 3
    person = results["new_people"][0]
    assert query_of(results["list_events:request"]) == {"person": [person]}
    assert results["events"].structured == api_get(client, "/v1/events", {"person": [person]})
    for outcome in outcomes:
        assert_clean(outcome)
        assert_no_echo(outcome, str(roster), roster.name, *[value for row in rows for value in row.values()])


# --------------------------------------------------------------------------------------------------
# Filtros e limites das listas
# --------------------------------------------------------------------------------------------------

LIST_TOOLS_WITH_LIMIT = ("list_people", "list_offerings", "list_events", "list_dispatches", "list_report_versions")


@pytest.mark.parametrize("mode", MODES)
def test_list_filters_and_limits_reach_the_api(mode: str, client: Any, tmp_path: Path) -> None:
    """cada filtro e limite aceito das listas (`after`, `limit`; em `list_events`, `types` e `person`,
    juntos e separados; `get_dispatch_report.version` com um número e com `latest`) chega à API como a consulta
    exata, e o resultado é o da mesma consulta feita direto à API; `limit` 500 é aceito e chega à API."""
    state = seed(client, archive_targets=0, invite_targets=0, dispatch_targets=0, prepare_targets=0,
                 purge_targets=0)
    pid = state.person["pseudo_id"]
    dispatch_code = _dispatch(state)
    report_code, report_dispatch = _report(state)
    versions = api_get(client, route("list_report_versions", ref=report_code, dispatch_id=report_dispatch))
    version = str(versions["items"][0]["version"])
    report_ref = {"offering": report_code, "dispatch_id": report_dispatch}
    report_path = {"ref": report_code, "dispatch_id": report_dispatch}
    cases: list[tuple[str, dict[str, Any], dict[str, str], dict[str, list[str]]]] = [
        ("list_people", {"after": 1, "limit": 2}, {}, {"after": ["1"], "limit": ["2"]}),
        ("list_people", {"limit": 500}, {}, {"limit": ["500"]}),
        ("list_offerings", {"after": 1, "limit": 3}, {}, {"after": ["1"], "limit": ["3"]}),
        ("list_offerings", {"limit": 500}, {}, {"limit": ["500"]}),
        ("list_events", {"types": ["person.registered", "offering.created"]}, {},
         {"types": ["person.registered", "offering.created"]}),
        ("list_events", {"person": pid}, {}, {"person": [pid]}),
        ("list_events", {"types": ["person.registered"], "person": pid, "after": 0, "limit": 5}, {},
         {"types": ["person.registered"], "after": ["0"], "limit": ["5"], "person": [pid]}),
        ("list_events", {"types": ["offering.created"], "limit": 500}, {},
         {"types": ["offering.created"], "limit": ["500"]}),
        ("list_dispatches", {"offering": dispatch_code, "after": 0, "limit": 1}, {"ref": dispatch_code},
         {"after": ["0"], "limit": ["1"]}),
        ("list_dispatches", {"offering": dispatch_code, "limit": 500}, {"ref": dispatch_code}, {"limit": ["500"]}),
        ("list_report_versions", {**report_ref, "after": 0, "limit": 2}, report_path,
         {"after": ["0"], "limit": ["2"]}),
        ("list_report_versions", {**report_ref, "limit": 500}, report_path, {"limit": ["500"]}),
        ("get_dispatch_report", {**report_ref, "version": version}, report_path, {"version": [version]}),
        ("get_dispatch_report", {**report_ref, "version": "latest"}, report_path, {"version": ["latest"]}),
    ]
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> list[tuple[Outcome, Seen]]:
        results = []
        async with in_process(deps, mode) as mc:
            for tool, arguments, _, _ in cases:
                with observed(client) as seen:
                    results.append((await call(mc, tool, arguments), seen))
        return results

    for (tool, arguments, params, query), (outcome, seen) in zip(cases, run(scenario), strict=True):
        label = f"{tool} {arguments}"
        assert [(r.method, r.url.path) for r in seen.requests] == [("GET", route(tool, **params))], label
        assert query_of(seen.requests[0]) == query, label
        assert_success(outcome, api_get(client, route(tool, **params), query))
        assert_clean(outcome)


@pytest.mark.parametrize("limit", [0, 501])
@pytest.mark.parametrize("tool", LIST_TOOLS_WITH_LIMIT)
@pytest.mark.parametrize("mode", MODES)
def test_list_limit_out_of_range_is_invalid_params(mode: str, tool: str, limit: int, tmp_path: Path) -> None:
    """`LIMIT` de 1 a 500: `limit` 0 ou 501 → `-32602` com a mensagem exata, sem eco e sem
    nenhum pedido à API."""
    api = FakeApi()
    arguments = {**static_arguments(tool, static_roster(tmp_path)), "limit": limit}
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, tool, arguments)

    outcome = run(scenario)
    assert_invalid_params(outcome)
    assert api.requests == []
    assert_clean(outcome)


# --------------------------------------------------------------------------------------------------
# Entrada recusada pelo esquema e pelas combinações
# --------------------------------------------------------------------------------------------------

# (ferramenta, argumentos, valores que não podem ecoar). Nenhum valor com `\n` final.
INVALID_ARGUMENTS: tuple[tuple[str, str, dict[str, Any], tuple[str, ...]], ...] = (
    ("tipo", "get_offering", {"offering": 4815162342}, ("4815162342",)),
    ("padrao", "get_offering", {"offering": "Oferta Sentinela Invalida!"}, ("Oferta Sentinela Invalida!",)),
    ("campo_desconhecido", "list_people", {"campo_sentinela_x": 1}, ("campo_sentinela_x",)),
    ("ausente", "get_offering", {}, ()),
    ("dispatch_id_fora_do_padrao", "get_dispatch", {"offering": STATIC_OFFERING, "dispatch_id": "disparo-sentinela"},
     ("disparo-sentinela",)),
    ("versao_fora_do_padrao", "get_dispatch_report",
     {"offering": STATIC_OFFERING, "dispatch_id": STATIC_DISPATCH, "version": "versao-sentinela"},
     ("versao-sentinela",)),
    ("format_no_relatorio", "get_dispatch_report",
     {"offering": STATIC_OFFERING, "dispatch_id": STATIC_DISPATCH, "format": "csv"}, ()),
    ("origin_no_disparo", "prepare_dispatch", {"offering": STATIC_OFFERING, **SEED_QUESTION, "origin": "integration"},
     ()),
    ("contact_email_nas_configuracoes", "update_offering_settings",
     {"offering": STATIC_OFFERING, "free_messages": True, "contact_email": "contato.sentinela@docente.example.org"},
     ("contato.sentinela@docente.example.org",)),
    ("free_messages_ausente", "update_offering_settings", {"offering": STATIC_OFFERING}, ()),
    ("eventos_sem_filtro", "list_events", {}, ()),
    ("eventos_so_paginacao", "list_events", {"after": 0, "limit": 5}, ()),
    ("tipo_de_evento_fora_do_padrao", "list_events", {"types": ["TipoSentinela"]}, ("TipoSentinela",)),
    ("resumo_sem_campo", "update_offering_digest", {"offering": STATIC_OFFERING}, ()),
    ("horario_fora_do_padrao", "update_offering_digest", {"offering": STATIC_OFFERING, "local_time": "25:99"},
     ("25:99",)),
    ("valid_hours_fora", "prepare_send_invites", {"offering": STATIC_OFFERING, "valid_hours": 721}, ("721",)),
    ("after_negativo", "list_people", {"after": -1}, ()),
    ("codigo_vazio", "archive_offering", {"offering": STATIC_OFFERING, "confirmation_code": ""}, ()),
)


@pytest.mark.parametrize(("case", "tool", "arguments", "values"), INVALID_ARGUMENTS,
                         ids=[case[0] for case in INVALID_ARGUMENTS])
@pytest.mark.parametrize("mode", MODES)
def test_invalid_arguments_are_rejected_without_echo(mode: str, case: str, tool: str, arguments: dict[str, Any],
                                                     values: tuple[str, ...], tmp_path: Path) -> None:
    """argumentos fora do esquema (tipo, padrão, campo desconhecido, ausente,
    as exceções nomeadas das formas de entrada) ou fora das combinações conferidas no servidor (`list_events` sem `types` nem
    `person`; `update_offering_digest` sem `enabled` nem `local_time`) → `-32602` com a mensagem exata, sem
    `data`, sem citar a ferramenta nem o valor, e nenhum pedido à API."""
    api = FakeApi()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, tool, arguments)

    outcome = run(scenario)
    assert_invalid_params(outcome)
    assert_no_echo(outcome, tool, *values)
    assert api.requests == []
    assert_clean(outcome)


@pytest.mark.parametrize("name", ["ferramenta_sentinela_inexistente", "tokens_create", "register_bot",
                                  "list_messages"])
@pytest.mark.parametrize("mode", MODES)
def test_unknown_tool_is_rejected_without_echo(mode: str, name: str, tmp_path: Path) -> None:
    """uma ferramenta desconhecida (inclusive o nome de uma operação fora do MCP) →
    `-32602` `"Ferramenta desconhecida."`, sem `data`, sem o nome e sem pedido à API."""
    api = FakeApi()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, name, {})

    outcome = run(scenario)
    assert_invalid_params(outcome, "unknown_tool")
    assert_no_echo(outcome, name)
    assert api.requests == []


# Entradas por pessoa (identidade só pela revelação): identidades no lugar do pseudônimo.
PERSON_IDENTITIES: tuple[tuple[str, str], ...] = (
    ("ra_canario", ZEFIRO.ra or ""),
    ("email_canario", ZEFIRO.email or ""),
    ("id_telegram", str(TG_ZEFIRO.user_id)),
    ("usuario_telegram", "@" + (TG_ZEFIRO.username or "")),
)


@pytest.mark.parametrize(("label", "value"), PERSON_IDENTITIES, ids=[label for label, _ in PERSON_IDENTITIES])
@pytest.mark.parametrize(("tool", "key"), [("get_person", "person"), ("list_events", "person")])
@pytest.mark.parametrize("mode", MODES)
def test_person_inputs_refuse_identities(mode: str, tool: str, key: str, label: str, value: str,
                                         tmp_path: Path) -> None:
    """Identidade só pela revelação: `get_person.person` e `list_events.person` só aceitam pseudônimo; um RA canário (só
    dígitos), um e-mail canário, um identificador numérico do Telegram e um `@usuário` → `-32602` com a mensagem
    exata, sem eco (nem o valor, nem o alvo canário) e com o gancho de pedido vazio."""
    assert value and value not in ("@",)
    api = FakeApi()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, tool, {key: value})

    outcome = run(scenario)
    assert_invalid_params(outcome)
    assert_no_echo(outcome, value, value.lstrip("@"))
    assert api.requests == []
    assert_clean(outcome)


@pytest.mark.parametrize("mode", MODES)
def test_create_offering_with_contact_email_creates_nothing(mode: str, client: Any, tmp_path: Path) -> None:
    """`create_offering` com `contact_email` → `-32602` com a mensagem exata, sem eco, nenhum pedido à
    API e nenhum oferecimento criado."""
    before = api_get(client, "/v1/offerings", {"limit": ["500"]})
    email = "contato.sentinela@docente.example.org"
    arguments = {"code": _unique("contato-mcp"), "course": "Disciplina Contato", "term": "2031s1",
                 "timezone": "America/Sao_Paulo", "contact_email": email}
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                return await call(mc, "create_offering", arguments), seen

    outcome, seen = run(scenario)
    assert_invalid_params(outcome)
    assert_no_echo(outcome, email, arguments["code"])
    assert seen.requests == []
    assert api_get(client, "/v1/offerings", {"limit": ["500"]}) == before


# --------------------------------------------------------------------------------------------------
# Erros da API com as linhas fixas
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), [
    ("create_offering", {"code": "permissao-sentinela", "course": "Disciplina Permissao", "term": "2031s1",
                         "timezone": "America/Sao_Paulo"}),
    ("list_events", {"types": ["person.registered"]}),
], ids=["create_offering", "list_events"])
@pytest.mark.parametrize("mode", MODES)
def test_forbidden_shows_the_required_permission(mode: str, tool: str, arguments: dict[str, Any], client: Any,
                                                 tmp_path: Path) -> None:
    """com um token sem a permissão da operação, `[forbidden]` com a mensagem fixa e a linha
    `Permissão exigida: <permissão do catálogo>.` exata; sem eco da entrada nem do token."""
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="token sem permissao mcp")
    deps = make_deps(tmp_path, env=profile_env(tmp_path, token=secret), http=client)

    async def scenario() -> tuple[Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                return await call(mc, tool, arguments), seen

    outcome, seen = run(scenario)
    permission = OP_BY_TOOL[tool]["permission"]
    assert [(r.method, r.url.path) for r in seen.requests] == [(OP_BY_TOOL[tool]["method"], route(tool))]
    assert_execution_error(outcome, error_text("api.forbidden", f"Permissão exigida: {permission}."))
    assert_no_echo(outcome, *[v for v in arguments.values() if isinstance(v, str)], secret)
    assert_clean(outcome, secret)


@pytest.mark.parametrize("mode", MODES)
def test_envelope_fields_become_field_lines(mode: str, client: Any, tmp_path: Path) -> None:
    """um `fields[]` do envelope vira `Campo <campo>: <motivo>.` (fora da importação, nunca
    `Linha`), sem o `message` livre e sem eco do valor."""
    zone = "America/Campinas"
    arguments = {"code": _unique("campo-mcp"), "course": "Disciplina Campo", "term": "2031s1", "timezone": zone}
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                return await call(mc, "create_offering", arguments), seen

    outcome, seen = run(scenario)
    assert len(seen.requests) == 1
    assert seen.bodies()[0]["error"]["fields"] == [{"field": "timezone", "reason": "invalid_value"}]
    assert_execution_error(outcome, error_text("api.invalid_request", "Campo timezone: invalid_value."))
    assert_no_echo(outcome, zone, arguments["code"])
    assert_clean(outcome)


@pytest.mark.parametrize("mode", MODES)
def test_import_row_content_error_becomes_file_line(mode: str, client: Any, tmp_path: Path) -> None:
    """Linha inválida da lista na importação pelo MCP: uma linha com conteúdo inválido (um RA com letras) é
    **enviada** à API, e o `rows[i]` do envelope volta como `Linha N: <motivo>.`, com o N da linha física do arquivo
    (cabeçalho = 1); nada é importado e nenhum valor do arquivo ecoa."""
    offering = create_offering(client, ADMIN_TOKEN, code=_unique("lista-linha-mcp"), course="Disciplina Lista",
                               term="2031s1", timezone="America/Sao_Paulo")
    rows = [_neutral_row("Aluno Linha Um"), {**_neutral_row("Aluno Linha Dois"), "ra": "77ab000001"},
            _neutral_row("Aluno Linha Tres")]
    roster = write_roster(tmp_path / "lista-linha-invalida.csv", rows).resolve()
    summary_path = route("get_students_summary", ref=offering["code"])
    before = api_get(client, summary_path)
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                return await call(mc, "import_students", {"offering": offering["code"], "path": str(roster)}), seen

    outcome, seen = run(scenario)
    assert [(r.method, r.url.path) for r in seen.requests] == [("POST", route("import_students",
                                                                              ref=offering["code"]))]
    assert json_body(seen.requests[0]) == {"rows": rows}
    assert seen.bodies()[0]["error"]["fields"] == [{"field": "rows[1]", "reason": "invalid_format"}]
    assert_execution_error(outcome, error_text("api.invalid_request", "Linha 3: invalid_format."))
    assert_no_echo(outcome, str(roster), roster.name, *[value for row in rows for value in row.values()])
    assert api_get(client, summary_path) == before
    assert_clean(outcome)


@pytest.mark.parametrize("mode", MODES)
def test_pending_counts_become_pending_line(mode: str, tmp_path: Path) -> None:
    """o `pending` do `purge_pending` vira a linha `Pendências: fila …, updates …, tarefas …,
    congelamento …, devidas ….` com as contagens do envelope (uma API dublê com o envelope do contrato)."""
    pending = {"outbox": 1, "updates": 2, "tasks": 3, "freeze": 4, "due": 5}
    api = FakeApi(lambda request: envelope_response(409, "purge_pending", pending=pending))
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, "prepare_purge_offering", {"offering": STATIC_OFFERING})

    outcome = run(scenario)
    assert [(r.method, r.url.path) for r in api.requests] == [
        ("POST", route("prepare_purge_offering", ref=STATIC_OFFERING))]
    assert_execution_error(outcome, error_text(
        "api.purge_pending", "Pendências: fila 1, updates 2, tarefas 3, congelamento 4, devidas 5."))
    assert_clean(outcome)


# --------------------------------------------------------------------------------------------------
# Perfil: sem token, inválido, resolução a cada chamada
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_profile_without_token_fails_every_api_tool(mode: str, tmp_path: Path) -> None:
    """Perfil sem token (`no_token`): com o perfil sem token, cada ferramenta do catálogo falha com
    `[no_token]` e a mensagem fixa (que não cita `CQ_TOKEN` nem manda colar token), sem pedido à API."""
    api = FakeApi()
    roster = static_roster(tmp_path)
    deps = make_deps(tmp_path, env=profile_env(tmp_path, token=None), http=api.http)

    async def scenario() -> dict[str, Outcome]:
        async with in_process(deps, mode) as mc:
            return {tool: await call(mc, tool, static_arguments(tool, roster)) for tool in CATALOG_TOOLS}

    for outcome in run(scenario).values():
        assert_execution_error(outcome, error_text("no_token"))
        assert_clean(outcome)
    assert "CQ_TOKEN" not in MCP_MESSAGES["no_token"]
    assert api.requests == []


# Cada classe concreta de `ProfileError` e o arquivo que a provoca (`selector`, conteúdo; `None` = um diretório no
# lugar do arquivo). Os textos-sentinela do arquivo não podem ecoar.
PROFILE_SENTINEL = "perfil-sentinela-Qm7Xv2"
PROFILE_FILES: dict[str, tuple[str | None, str | None]] = {
    "InvalidProfileFile": (None, f'api_url = "{PROFILE_SENTINEL}\n[[[ isto não é TOML\n'),
    "UnreadableProfileFile": (None, None),
    "InvalidApiUrl": (None, f'api_url = "ftp://{PROFILE_SENTINEL}.cq.test"\n'),
    "InvalidToken": (None, f'token = "{PROFILE_SENTINEL}-çinválido"\n'),
    "UnknownProfile": (PROFILE_SENTINEL, f'[profiles.outro]\napi_url = "{REMOTE_URL}"\n'),
    "UnknownProfileKey": (None, f'[profiles.p]\napi_url = "{REMOTE_URL}"\nchave_{PROFILE_SENTINEL.replace("-", "_")} '
                                f'= "x"\n'),
    "InvalidProfileField": (None, f'[profiles.p]\nkind = "remote"\napi_url = "{REMOTE_URL}"\n'
                                  f'stack_dir = "/{PROFILE_SENTINEL}"\n'),
    "InvalidProfileKind": (None, f'[profiles.p]\nkind = "{PROFILE_SENTINEL}"\n'),
    "InvalidComposeProject": (None, '[profiles.p]\nkind = "local"\napi_url = "http://127.0.0.1:9"\n'
                                    'compose_project = "cq-systest"\n'),
}


@pytest.mark.parametrize("error", [cls.__name__ for cls in PROFILE_ERROR_CLASSES])
@pytest.mark.parametrize("mode", MODES)
def test_invalid_profile_lists_tools_and_fails_each_call(mode: str, error: str, tmp_path: Path) -> None:
    """com o perfil inválido ou o arquivo de configuração quebrado (cada classe concreta de
    `ProfileError`), o servidor lista as ferramentas mesmo assim e cada chamada do catálogo falha com
    `[profile.<Classe>] <reason do CLI>` exato, sem eco do arquivo e sem pedido à API."""
    assert set(PROFILE_FILES) == {cls.__name__ for cls in PROFILE_ERROR_CLASSES}
    selector, content = PROFILE_FILES[error]
    if content is None:
        config = tmp_path / "config-diretorio.toml"
        config.mkdir()
    else:
        config = write_profile(tmp_path, content)
    api = FakeApi()
    roster = static_roster(tmp_path)
    deps = make_deps(tmp_path, env={"CQ_CONFIG": str(config)}, http=api.http, selector=selector)

    async def scenario() -> tuple[list[dict], dict[str, Outcome]]:
        async with in_process(deps, mode) as mc:
            tools = await list_tools(mc)
            return tools, {tool: await call(mc, tool, static_arguments(tool, roster)) for tool in CATALOG_TOOLS}

    tools, outcomes = run(scenario)
    assert [tool["name"] for tool in tools] == list(TOOL_NAMES)
    cls = next(c for c in PROFILE_ERROR_CLASSES if c.__name__ == error)
    for outcome in outcomes.values():
        assert_execution_error(outcome, f"[profile.{error}] {cls.reason}")
        assert_no_echo(outcome, PROFILE_SENTINEL, str(config))
        assert_clean(outcome)
    assert api.requests == []


TWO_PROFILES = f"""\
api_url = "{UNREACHABLE_URL}"
token = "{TOP_TOKEN}"

[profiles.um]
kind = "remote"
api_url = "{PROFILE_ONE_URL}"
token = "{REFUSED_TOKEN}"

[profiles.dois]
kind = "remote"
api_url = "{PROFILE_TWO_URL}"
token = "{ADMIN_TOKEN}"
"""


def _target(request: httpx2.Request) -> tuple[str, str, str | None]:
    return request.url.scheme, request.url.host, request.headers.get("Authorization")


@pytest.mark.parametrize(("selector", "env", "url", "token"), [
    ("dois", {}, PROFILE_TWO_URL, ADMIN_TOKEN),
    ("dois", {"CQ_PROFILE": "um"}, PROFILE_TWO_URL, ADMIN_TOKEN),
    (None, {"CQ_PROFILE": "dois"}, PROFILE_TWO_URL, ADMIN_TOKEN),
    (None, {"CQ_PROFILE": "um", "CQ_TOKEN": ADMIN_TOKEN}, PROFILE_ONE_URL, ADMIN_TOKEN),
    (None, {"CQ_PROFILE": "um", "CQ_API_URL": ENV_URL, "CQ_TOKEN": ADMIN_TOKEN}, ENV_URL, ADMIN_TOKEN),
    (None, {"CQ_API_URL": ENV_URL, "CQ_TOKEN": ADMIN_TOKEN}, ENV_URL, ADMIN_TOKEN),
], ids=["opcao", "opcao_vence_variavel", "variavel", "variavel_e_token", "variavel_endereco_e_token",
        "implicito_com_variaveis"])
@pytest.mark.parametrize("mode", MODES)
def test_profile_resolution_picks_url_and_token(mode: str, selector: str | None, env: dict[str, str], url: str,
                                                token: str, client: Any, tmp_path: Path) -> None:
    """(a) `--profile` (o `selector`) com dois perfis no `CQ_CONFIG` → o pedido vai ao `api_url` e
    com o token do perfil escolhido; (b) `CQ_PROFILE`, `CQ_API_URL` e `CQ_TOKEN` do `env` do `ServerDeps` vencem
    como no CLI, campo a campo (o gancho de pedido mostra o endereço e o token usados); o resultado é o da API."""
    config = write_profile(tmp_path, TWO_PROFILES)
    deps = make_deps(tmp_path, env={"CQ_CONFIG": str(config), **env}, http=client, selector=selector)

    async def scenario() -> tuple[Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                return await call(mc, "list_people", {}), seen

    outcome, seen = run(scenario)
    expected_url = httpx2.URL(url)
    assert [_target(r) for r in seen.requests] == [(expected_url.scheme, expected_url.host, f"Bearer {token}")]
    assert seen.requests[0].url.path == route("list_people")
    assert_success(outcome, api_get(client, route("list_people")))
    assert_clean(outcome, REFUSED_TOKEN, TOP_TOKEN)


@pytest.mark.parametrize("mode", MODES)
def test_profile_is_resolved_on_every_call(mode: str, client: Any, tmp_path: Path) -> None:
    """O perfil é lido a cada chamada: no **mesmo** servidor, a primeira chamada com o arquivo quebrado falha com
    `[profile.InvalidProfileFile]`; o arquivo corrigido, a segunda chamada vai à API e devolve o corpo dela."""
    config = write_profile(tmp_path, "[[[ arquivo quebrado\n")
    deps = make_deps(tmp_path, env={"CQ_CONFIG": str(config)}, http=client)

    async def scenario() -> tuple[Outcome, Outcome, Seen, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as first_seen:
                first = await call(mc, "list_people", {})
            write_profile(tmp_path, f'api_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
            with observed(client) as second_seen:
                second = await call(mc, "list_people", {})
            return first, second, first_seen, second_seen

    first, second, first_seen, second_seen = run(scenario)
    from cq_client.profile import InvalidProfileFile

    assert_execution_error(first, f"[profile.InvalidProfileFile] {InvalidProfileFile.reason}")
    assert first_seen.requests == []
    assert [r.url.path for r in second_seen.requests] == [route("list_people")]
    assert_success(second, api_get(client, route("list_people")))


# --------------------------------------------------------------------------------------------------
# API inalcançável, sem `up` implícito
# --------------------------------------------------------------------------------------------------

LOCAL_NAMED = f"""\
[profiles.local]
kind = "local"
api_url = "{UNREACHABLE_URL}"
token = "{ADMIN_TOKEN}"
compose_project = "cq-mcp-teste"

[profiles.remoto]
kind = "remote"
api_url = "{REMOTE_URL}"
token = "{ADMIN_TOKEN}"
"""


@pytest.mark.parametrize(("profile", "selector", "env", "project"), [
    ("implicito_local", None, {}, "cq"),
    ("nomeado_local", "local", {}, "cq-mcp-teste"),
    ("nomeado_remoto", "remoto", {}, None),
    ("local_com_cq_api_url", "local", {"CQ_API_URL": UNREACHABLE_URL}, None),
], ids=["implicito_local", "nomeado_local", "nomeado_remoto", "local_com_cq_api_url"])
@pytest.mark.parametrize("mode", MODES)
def test_unreachable_api_hint_only_for_local_profile(mode: str, profile: str, selector: str | None,
                                                    env: dict[str, str], project: str | None,
                                                    tmp_path: Path) -> None:
    """API inalcançável (`api_unreachable`): com a API inalcançável, o perfil local sem
    `CQ_API_URL` dá a mensagem fixa e a linha de `MCP_HINT_TEMPLATE` com o projeto Compose do perfil; o perfil remoto
    ou com `CQ_API_URL` dá só a mensagem. Em nenhum caso a pilha é tocada (nenhuma chamada ao `FakeStack`), e o
    endereço não sai no texto."""
    if profile == "implicito_local":
        config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
    else:
        config = write_profile(tmp_path, LOCAL_NAMED)
    stack = FakeStack()
    deps = make_deps(tmp_path, env={"CQ_CONFIG": str(config), **env}, http=unreachable_http(), stack=stack,
                     selector=selector)

    async def scenario() -> list[Outcome]:
        async with in_process(deps, mode) as mc:
            return [await call(mc, "list_people", {}),
                    await call(mc, "create_offering", static_arguments("create_offering", tmp_path))]

    hint = () if project is None else (MCP_HINT_TEMPLATE.format(project=project),)
    for outcome in run(scenario):
        assert_execution_error(outcome, error_text("api_unreachable", *hint))
        assert_no_echo(outcome, "127.0.0.1", REMOTE_URL)
        assert_clean(outcome)
    assert stack.calls == []


# --------------------------------------------------------------------------------------------------
# Limite do resultado e respostas fora do contrato
# --------------------------------------------------------------------------------------------------


def _first_limit_over(client: Any, ceiling: int) -> int:
    """O menor `limit` da listagem de oferecimentos cujo JSON compacto passa de `RESULT_LIMIT_CHARS` (busca binária
    entre 1 e `ceiling`, cujo JSON já passa; o JSON cresce com o `limit`)."""
    low, high = 1, ceiling
    while low < high:
        middle = (low + high) // 2
        if len(compact(api_get(client, "/v1/offerings", {"limit": [str(middle)]}))) > RESULT_LIMIT_CHARS:
            high = middle
        else:
            low = middle + 1
    return high


@pytest.mark.parametrize("mode", MODES)
def test_result_over_the_limit_is_a_fixed_error(mode: str, client: Any, tmp_path: Path) -> None:
    """o teto é o literal do teste (`RESULT_LIMIT_CHARS`, 50.000 caracteres do
    texto), conferido dos dois lados numa listagem grande semeada (oferecimentos com textos longos e neutros): com o
    menor `limit` cujo JSON compacto passa do teto → `[result_too_large]` com a mensagem fixa, sem nenhum pedaço do
    JSON; com um oferecimento a menos (o JSON ainda dentro do teto) → o corpo da API inteiro, então o servidor não
    recusa abaixo do teto; um limite pequeno também devolve o corpo (controle). A constante `MAX_RESULT_CHARS`
    é igual a ele."""
    assert MAX_RESULT_CHARS == RESULT_LIMIT_CHARS, "o teto é de 50.000 caracteres serializados"
    course = ("Disciplina Longa Neutra " * 9)[:200]
    term = ("Periodo Longo " * 3)[:40]
    created = 0
    while len(compact(api_get(client, "/v1/offerings", {"limit": ["500"]}))) <= RESULT_LIMIT_CHARS:
        for _ in range(10):
            create_offering(client, ADMIN_TOKEN, code=_unique("lista-grande-mcp"), course=course, term=term,
                            timezone="America/Sao_Paulo")
        created += 10
        assert created <= 400, "a semente da listagem grande não passou do limite"
    over = _first_limit_over(client, 500)
    assert over > 1, "um oferecimento sozinho passa do teto"
    fits = api_get(client, "/v1/offerings", {"limit": [str(over - 1)]})
    assert len(compact(fits)) <= RESULT_LIMIT_CHARS, "controle: com um a menos, o JSON cabe no teto"
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[Outcome, Seen, Outcome, Outcome]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                big = await call(mc, "list_offerings", {"limit": over})
            near = await call(mc, "list_offerings", {"limit": over - 1})
            small = await call(mc, "list_offerings", {"limit": 2})
            return big, seen, near, small

    big, seen, near, small = run(scenario)
    assert len(seen.requests) == 1 and query_of(seen.requests[0]) == {"limit": [str(over)]}
    assert len(compact(seen.bodies()[0])) > RESULT_LIMIT_CHARS
    assert_execution_error(big, error_text("result_too_large"))
    assert_no_echo(big, course, term)
    assert_success(near, fits)
    assert_success(small, api_get(client, "/v1/offerings", {"limit": ["2"]}))


def _respond_with(status: int, *, content: bytes | None = None, json_value: Any = None) -> Callable[..., Any]:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if content is not None:
            return httpx2.Response(status, content=content)
        return httpx2.Response(status, json=json_value)
    return respond


RESPONSE_SENTINEL = "corpo-sentinela-Lp4Rt8"
OUT_OF_CONTRACT: tuple[tuple[str, Callable[..., Any], str], ...] = (
    ("200_objeto_fora_do_esquema",
     _respond_with(200, json_value={"items": RESPONSE_SENTINEL, "next_cursor": 0, "extra": RESPONSE_SENTINEL}),
     "unexpected_response"),
    ("200_sem_json", _respond_with(200, content=f"texto {RESPONSE_SENTINEL}".encode()), "api_error"),
    ("200_lista_json", _respond_with(200, json_value=[RESPONSE_SENTINEL]), "api_error"),
    ("502_sem_envelope", _respond_with(502, json_value={"detalhe": RESPONSE_SENTINEL}), "api_error"),
    ("502_sem_json", _respond_with(502, content=f"<html>{RESPONSE_SENTINEL}</html>".encode()), "api_error"),
    ("400_codigo_fora_do_contrato",
     _respond_with(400, json_value={"error": {"code": "codigo_sentinela", "message": RESPONSE_SENTINEL}}),
     "api_error"),
)


@pytest.mark.parametrize(("case", "respond", "key"), OUT_OF_CONTRACT, ids=[c[0] for c in OUT_OF_CONTRACT])
@pytest.mark.parametrize("mode", MODES)
def test_out_of_contract_response_is_a_fixed_error(mode: str, case: str, respond: Callable[..., Any], key: str,
                                                   tmp_path: Path) -> None:
    """(1) um 200 com objeto JSON fora do esquema de saída → `[unexpected_response]`;
    (2) um 200 sem JSON (ou com JSON que não é objeto), um 502 sem envelope e um código fora dos 21 do contrato →
    `[api_error]` (o código sintético do `ApiClient` não vira `api.unexpected_response` nem `mcp_internal`); nos
    dois, nada do corpo recebido, e uma chamada só à API."""
    api = FakeApi(respond)
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> Outcome:
        async with in_process(deps, mode) as mc:
            return await call(mc, "list_people", {})

    outcome = run(scenario)
    assert [(r.method, r.url.path) for r in api.requests] == [("GET", route("list_people"))]
    assert_execution_error(outcome, error_text(key))
    assert_no_echo(outcome, RESPONSE_SENTINEL, "codigo_sentinela")
    assert_clean(outcome)


@pytest.mark.parametrize("mode", MODES)
def test_api_internal_and_mcp_internal_are_distinct(mode: str, tmp_path: Path) -> None:
    """o 500 da API (`api.internal`) e uma exceção interna do servidor (`mcp_internal`) dão textos
    fixos e distintos; a exceção não deixa detalhe no texto nem no stderr do servidor, que só recebe linhas fixas
    (a `internal`), e o 500 da API não escreve no stderr."""
    api = FakeApi(lambda request: envelope_response(500, "internal"))
    detail = "detalhe-interno-sentinela-Wd3Kc9"

    def explode(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError(detail)

    broken = FakeApi(explode)
    api_stderr, broken_stderr = io.StringIO(), io.StringIO()
    api_deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http, stderr=api_stderr)
    broken_deps = make_deps(tmp_path, env=profile_env(tmp_path), http=broken.http, stderr=broken_stderr)

    async def scenario() -> tuple[Outcome, Outcome]:
        async with in_process(api_deps, mode) as mc:
            from_api = await call(mc, "list_people", {})
        async with in_process(broken_deps, mode) as mc:
            internal = await call(mc, "list_people", {})
        return from_api, internal

    from_api, internal = run(scenario)
    assert_execution_error(from_api, error_text("api.internal"))
    assert_execution_error(internal, error_text("mcp_internal"))
    assert from_api.text != internal.text
    assert_no_echo(internal, detail, "RuntimeError", "Traceback")
    assert api_stderr.getvalue() == ""
    lines = broken_stderr.getvalue().splitlines()
    assert MCP_STDERR_MESSAGES["internal"] in lines
    assert set(lines) <= set(MCP_STDERR_MESSAGES.values())
    assert detail not in broken_stderr.getvalue()
    assert len(broken.requests) == 1


# --------------------------------------------------------------------------------------------------
# Cancelamento
# --------------------------------------------------------------------------------------------------

WAIT_SECONDS = 10


def _wire(item: Any) -> dict[str, Any]:
    """A mensagem JSON-RPC de um `SessionMessage`, como dicionário (o que iria no fio)."""
    return item.message.model_dump(mode="json", by_alias=True, exclude_none=True)


class _RecordingSend:
    """Um lado de escrita que anota cada mensagem **antes** de repassá-la: uma escrita que falhe porque o outro lado
    já fechou continua anotada."""

    def __init__(self, inner: Any, log: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._log = log

    async def send(self, item: Any) -> None:
        self._log.append(_wire(item))
        await self._inner.send(item)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> _RecordingSend:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._inner.__aexit__(*exc_info)


@contextlib.asynccontextmanager
async def recording_transport(server: Any, sent: list[dict[str, Any]], emitted: list[dict[str, Any]]) -> Any:
    """O `Server` em processo sobre streams de memória, como o `InMemoryTransport` do SDK, com o fio gravado nos dois
    sentidos: `sent` = o que o cliente manda ao servidor, `emitted` = tudo o que o servidor escreve.

    **[F]** (`mcp` 2.3.0, `mcp/server/lowlevel/server.py` e `mcp/server/runner.py`, `serve_dual_era_loop`): o
    `Server.run` atende as duas eras sobre streams; a primeira requisição decide a era. Com este transporte, a era
    moderna passa pelo fio JSON-RPC, como no `cq mcp` por stdio, e não pelo despacho direto do `in_process`, onde não
    há fio a observar (`mcp/shared/direct_dispatcher.py`)."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        server_done = anyio.Event()

        async def serve() -> None:
            try:
                await server.run(server_read, _RecordingSend(server_write, emitted),
                                 server.create_initialization_options(), raise_exceptions=False)
            finally:
                server_done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            try:
                yield client_read, _RecordingSend(client_write, sent)
            finally:
                await client_write.aclose()
                with anyio.move_on_after(WAIT_SECONDS):
                    await server_done.wait()
                if not server_done.is_set():
                    group.cancel_scope.cancel()


def _mentions_request(message: Mapping[str, Any], request_id: Any) -> bool:
    """A mensagem do servidor é sobre o pedido `request_id`: a resposta ou o erro dele (`id`), ou uma notificação que o
    cita (`requestId` do cancelamento, `progressToken` do progresso)."""
    if "method" not in message and message.get("id") == request_id:
        return True
    params = message.get("params") or {}
    return request_id in (params.get("requestId"), params.get("progressToken"))


@pytest.mark.parametrize("mode", MODES)
def test_cancelled_call_gets_no_late_response_and_calls_api_once(mode: str, tmp_path: Path) -> None:
    """um `tools/call` cancelado enquanto a API ainda não respondeu não recebe resposta depois, e a
    chamada à API já enviada acontece uma vez (nenhuma repetição); a chamada seguinte, no mesmo servidor, recebe o
    próprio resultado. A API dublê segura o primeiro pedido até o teste soltá-lo (no máximo `WAIT_SECONDS`).

    O que se observa é o fio do servidor (`recording_transport`), não a tarefa do cliente: nenhuma mensagem que o
    servidor escreve, até o fim da sessão, é sobre o pedido cancelado (`_mentions_request`). Controles positivos: o
    cliente de fato mandou o `notifications/cancelled` com o `id` do `tools/call`, e a resposta da chamada seguinte
    aparece no fio com o próprio `id`.

    Ordem ([I] deste arquivo, sobre o **[F]** do SDK `mcp` 2.3.0, `mcp/shared/jsonrpc_dispatcher.py`: o laço de
    leitura aplica o `notifications/cancelled` sem ceder a vez antes de ler a mensagem seguinte): depois do
    cancelamento, um `tools/list` serve de barreira; quando a resposta dele volta, o servidor já leu o cancelamento.
    Só então a API dublê é solta. Sem a barreira, um servidor correto poderia receber a resposta da API antes do
    cancelamento e responder legitimamente."""
    import threading

    arrived, release, finished = threading.Event(), threading.Event(), threading.Event()
    released_in_time: list[bool] = []
    people = {"items": [], "next_cursor": 0}
    offerings = {"items": [], "next_cursor": 0}

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == route("list_people"):
            arrived.set()
            released_in_time.append(release.wait(WAIT_SECONDS))
            finished.set()
            return httpx2.Response(200, json=people)
        return httpx2.Response(200, json=offerings)

    api = FakeApi(respond)
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)
    sent: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []

    async def scenario() -> tuple[bool, Outcome]:
        server = build_server(deps)
        transport = recording_transport(server, sent, emitted)
        async with Client(transport, mode=mode, raise_exceptions=False, cache=None) as mc:
            with anyio.fail_after(WAIT_SECONDS):
                async with anyio.create_task_group() as group:
                    async def blocked() -> None:
                        await call(mc, "list_people", {})

                    group.start_soon(blocked)
                    assert await anyio.to_thread.run_sync(arrived.wait, WAIT_SECONDS)
                    group.cancel_scope.cancel()
            await list_tools(mc)
            release.set()
            done = await anyio.to_thread.run_sync(finished.wait, WAIT_SECONDS)
            following = await call(mc, "list_offerings", {})
        return done, following

    done, following = run(scenario)

    calls = [m for m in sent if m.get("method") == "tools/call"]
    assert [m["params"]["name"] for m in calls] == ["list_people", "list_offerings"]
    cancelled_id, following_id = calls[0]["id"], calls[1]["id"]
    cancels = [m for m in sent if m.get("method") == "notifications/cancelled"]
    assert [m["params"]["requestId"] for m in cancels] == [cancelled_id]
    assert [m for m in emitted if _mentions_request(m, cancelled_id)] == []
    following_wire = [m for m in emitted if "method" not in m and m.get("id") == following_id]
    assert len(following_wire) == 1 and "result" in following_wire[0]

    assert done and released_in_time == [True]
    assert_success(following, offerings)
    assert [r.url.path for r in api.requests] == [route("list_people"), route("list_offerings")]
