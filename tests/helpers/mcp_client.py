"""Cliente MCP dos testes da operação por agente, em processo e por stdio.


Não importa `cq_server`. Usa o `Client` do SDK `mcp` 2.3.x instalado (**[F]** na fonte do SDK:
`Client(server, mode=…, raise_exceptions=…)`, `mcp/client/client.py`):

- `MODES = ("legacy", "2026-07-28")`: as duas eras. `"legacy"` passa pelo `Server.run` real sobre o
  `InMemoryTransport` (com o `initialize`); a era moderna, pelo despacho direto, sem o `initialize`. `"auto"`
  é aceito só por `in_process` e `stdio_session`, para ler as capacidades reais (**[F]** com a versão fixa, o
  cliente adota `_synthesize_discover`, com `ServerCapabilities()` vazio, sem perguntar ao servidor).
- `make_deps(tmp_path, *, env=None, …) -> ServerDeps`: o `env` é **explícito** e nunca parte do `os.environ`
  (as `CQ_*`, `COMPOSE_*`, `DOCKER_HOST` e `DOCKER_CONTEXT` do desenvolvedor não entram); `None` num valor do
  `env` tira a variável. Sem `env`, o `CQ_CONFIG` aponta para um arquivo inexistente no `tmp_path`; com `env`
  sem `CQ_CONFIG`, `ValueError`. Sem `cwd`, o `cwd` é o `tmp_path` (nunca o diretório do processo, que é a raiz
  do repositório, com marcadores de pilha).
- `in_process(deps, mode)`: o `Client` sobre `build_server(deps)`, com `raise_exceptions=False` e sem o cache
  de respostas do cliente (`cache=None`: toda chamada chega ao servidor).
- `call(client, name, arguments) -> Outcome`: o resultado do `tools/call`, ou o erro de protocolo capturado
  como `MCPError` (nunca propagado). Outros erros do cliente propagam: o `RuntimeError` da revalidação do
  `structuredContent` contra o `outputSchema` (**[F]** só quando `isError` é falso, `mcp/client/session.py`).
- `list_tools(client) -> list[dict]`: `t.model_dump(mode="json", by_alias=True, exclude_unset=True)` de cada
  ferramenta, todas as páginas: só as chaves que vieram do servidor.
- `run(async_fn, *args)` = `anyio.run`; `call` e `list_tools` rodam sob `anyio.fail_after(CALL_TIMEOUT_SECONDS)`.
- `unreachable_http()`: o `http=` da API inalcançável em processo (o transporte levanta `httpx2.ConnectError`
  na hora; nenhuma rede, nunca a porta 8000).
- `stdio_server_params(env, cwd, *, command=sys.executable)`, `stdio_session(params, mode, errlog_path)` e
  `run_eof(env, cwd)`: o `cq mcp` num subprocesso, com o `CQ_CONFIG` obrigatório e `APPDATA`, `LOCALAPPDATA`,
  `USERPROFILE`, `HOME` e `XDG_CONFIG_HOME` sobrescritos para o `home` (por padrão, o `cwd`, que é o
  `tmp_path`); o stderr do filho vai a um arquivo real (`errlog_path`).
- `scan(outcome_or_text, secrets, *, prefixes=SCAN_PREFIXES) -> list[str]`: `find_canary` (todos os alvos canário)
  e `find_secrets` sobre o texto, o estruturado e o `raw`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import httpx2
import mcp.types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError

from cq_client.mcp_server import ServerDeps, build_server

from tests.helpers.canary import ALL_PREFIXES, DISPATCH_PREFIXES, PURGE_PREFIXES, STUDENT_PREFIXES, find_canary, find_secrets

MODES: tuple[str, ...] = ("legacy", "2026-07-28")
AUTO_MODE = "auto"

# Teto de cada chamada em memória; nenhum `sleep` real.
CALL_TIMEOUT_SECONDS = 10

# O arquivo de perfil inexistente do `tmp_path` (o mesmo nome de `tests/helpers/cli_runner.py`).
MISSING_PROFILE_FILE = "cq-config-inexistente.toml"

# Variáveis do diretório do usuário que o subprocesso recebe apontadas para o `tmp_path` (**[F]** o SDK
# repassa `APPDATA`, `LOCALAPPDATA`, `USERPROFILE` e `HOME` do processo ao filho, `mcp/client/stdio.py`).
HOME_VARIABLES: tuple[str, ...] = ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME")

# O `cq mcp` sem depender do executável `cq` no PATH.
SERVER_CODE = "from cq_client.cli import main; main()"
SERVER_ARGS: tuple[str, ...] = ("-c", SERVER_CODE, "mcp")

# Todos os alvos canário (`tests/helpers/canary.py`).
SCAN_PREFIXES: tuple[str, ...] = ALL_PREFIXES + STUDENT_PREFIXES + DISPATCH_PREFIXES + PURGE_PREFIXES

# Teto do subprocesso de `run_eof` (a partida do Python e a saída no EOF).
EOF_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Outcome:
    """O resultado de um `tools/call` visto pelo cliente.

    Sucesso ou erro de execução: `structured` = `structuredContent` (ou `None`), `text` = o texto do único
    `TextContent` (com mais de um item, os textos juntados por `\\n`; sem conteúdo, `None`), `is_error` = o
    `isError`, `protocol_code` e `protocol_message` `None`, `raw` = o `CallToolResult` por
    `model_dump(mode="json", by_alias=True)`.

    Erro de protocolo (`MCPError`): `structured`, `text` e `is_error` `None`; `protocol_code` e
    `protocol_message` do erro; `raw` = o `ErrorData` por `model_dump(mode="json", by_alias=True,
    exclude_unset=True)`, sem a chave `data` quando o erro veio sem ela (**[F]** `MCPError` só põe o `data`
    quando ele não é `None`, `mcp/shared/exceptions.py`)."""

    structured: Any
    text: str | None
    is_error: bool | None
    protocol_code: int | None
    protocol_message: str | None
    raw: dict[str, Any]

    @property
    def is_protocol_error(self) -> bool:
        return self.protocol_code is not None


def _check_mode(mode: str) -> None:
    if mode not in MODES and mode != AUTO_MODE:
        raise ValueError(f"modo fora de {MODES + (AUTO_MODE,)}: {mode!r}")


def _explicit_env(tmp_path: Path, env: Mapping[str, str | None] | None) -> dict[str, str]:
    """O ambiente explícito (nunca o `os.environ`): sem `env`, só o `CQ_CONFIG` inexistente do `tmp_path`."""
    if env is None:
        missing = Path(tmp_path) / MISSING_PROFILE_FILE
        assert not missing.exists(), "o arquivo de perfil 'inexistente' existe"
        return {"CQ_CONFIG": str(missing)}
    full = {name: value for name, value in env.items() if value is not None}
    if not full.get("CQ_CONFIG"):
        raise ValueError("o env dos testes do servidor MCP sempre define CQ_CONFIG num arquivo do tmp_path")
    return full


def make_deps(tmp_path: Path, *, env: Mapping[str, str | None] | None = None, http: Any = None,
              stack: Any = None, runner: Any = None, cwd: Path | None = None,
              clock: Callable[[], float] | None = None, wall_clock: Callable[[], datetime] | None = None,
              sleep: Callable[[float], None] | None = None, new_code: Callable[[], str] | None = None,
              selector: str | None = None, stderr: Any = None) -> ServerDeps:
    """O `ServerDeps` dos testes (ver o docstring do módulo). Os relógios, o `sleep` e o `new_code` ausentes
    ficam com o padrão do `ServerDeps`."""
    optional: dict[str, Any] = {}
    if clock is not None:
        optional["clock"] = clock
    if wall_clock is not None:
        optional["wall_clock"] = wall_clock
    if sleep is not None:
        optional["sleep"] = sleep
    if new_code is not None:
        optional["new_code"] = new_code
    if stderr is not None:
        optional["stderr"] = stderr
    return ServerDeps(env=_explicit_env(tmp_path, env), selector=selector, http=http, stack=stack,
                      runner=runner, cwd=Path(cwd) if cwd is not None else Path(tmp_path), **optional)


@asynccontextmanager
async def in_process(deps: ServerDeps, mode: str) -> AsyncIterator[Client]:
    """O `Client` do SDK conectado em processo a `build_server(deps)`, na era pedida."""
    _check_mode(mode)
    server = build_server(deps)
    async with Client(server, mode=mode, raise_exceptions=False, cache=None) as client:
        yield client


def _text_of(content: list[Any]) -> str | None:
    texts = [item.text for item in content if isinstance(item, types.TextContent)]
    if not texts:
        return None
    if len(content) == 1:
        return texts[0]
    return "\n".join(texts)


async def call(client: Client, name: str, arguments: Mapping[str, Any] | None = None) -> Outcome:
    """Um `tools/call`; o erro de protocolo volta no `Outcome` (ver `Outcome`)."""
    with anyio.fail_after(CALL_TIMEOUT_SECONDS):
        try:
            result = await client.call_tool(name, dict(arguments) if arguments is not None else None)
        except MCPError as exc:
            return Outcome(structured=None, text=None, is_error=None, protocol_code=exc.code,
                           protocol_message=exc.message,
                           raw=exc.error.model_dump(mode="json", by_alias=True, exclude_unset=True))
    return Outcome(structured=result.structured_content, text=_text_of(list(result.content)),
                   is_error=result.is_error, protocol_code=None, protocol_message=None,
                   raw=result.model_dump(mode="json", by_alias=True))


async def list_tools(client: Client) -> list[dict[str, Any]]:
    """O `tools/list` inteiro (todas as páginas), cada ferramenta só com as chaves que vieram do servidor."""
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        with anyio.fail_after(CALL_TIMEOUT_SECONDS):
            result = await client.list_tools(cursor=cursor)
        tools.extend(t.model_dump(mode="json", by_alias=True, exclude_unset=True) for t in result.tools)
        cursor = result.next_cursor
        if cursor is None:
            return tools


def run(async_fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    """`anyio.run` (testes síncronos, sem plugin assíncrono)."""
    return anyio.run(async_fn, *args)


def unreachable_http() -> httpx2.Client:
    """O `http=` da API inalcançável em processo: todo pedido levanta `httpx2.ConnectError` na hora."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("conexão recusada (dublê)", request=request)

    return httpx2.Client(transport=httpx2.MockTransport(refuse))


def _subprocess_env(env: Mapping[str, str | None], home: Path) -> dict[str, str]:
    """O ambiente do filho por cima do herdado pelo SDK: o diretório do usuário no `home` e, por último, o
    `env` dado (com o `CQ_CONFIG` obrigatório)."""
    if not env.get("CQ_CONFIG"):
        raise ValueError("o subprocesso do servidor MCP sempre recebe CQ_CONFIG num arquivo do tmp_path")
    full = {name: str(home) for name in HOME_VARIABLES}
    full.update({name: value for name, value in env.items() if value is not None})
    return full


def stdio_server_params(env: Mapping[str, str | None], cwd: Path, *, command: str = sys.executable,
                        home: Path | None = None) -> StdioServerParameters:
    """Os parâmetros do `stdio_client` para o `cq mcp` (`<command> -c "<SERVER_CODE>" mcp`). O SDK junta ao
    `env` só as variáveis seguras do processo (`get_default_environment`), nunca as `CQ_*`."""
    full = _subprocess_env(env, Path(home) if home is not None else Path(cwd))
    return StdioServerParameters(command=command, args=list(SERVER_ARGS), env=full, cwd=str(cwd))


@asynccontextmanager
async def stdio_session(params: StdioServerParameters, mode: str, errlog_path: Path) -> AsyncIterator[Client]:
    """O `Client` sobre o `cq mcp` em subprocesso, na era pedida; o stderr do filho vai a `errlog_path`."""
    _check_mode(mode)
    with open(errlog_path, "w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog), mode=mode, raise_exceptions=False,
                          cache=None) as client:
            yield client


def run_eof(env: Mapping[str, str | None], cwd: Path, *, command: str = sys.executable,
            home: Path | None = None, timeout: float = EOF_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """O `cq mcp` com a entrada vazia (EOF imediato), para o código de saída (o `stdio_client` não o expõe).
    O ambiente é o mesmo do `stdio_server_params`: o herdado seguro do SDK, o `home` e o `env`."""
    full = get_default_environment() | _subprocess_env(env, Path(home) if home is not None else Path(cwd))
    return subprocess.run([command, *SERVER_ARGS], input=b"", env=full, cwd=str(cwd), capture_output=True,
                          timeout=timeout, check=False)


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="replace")]
    if isinstance(value, Outcome):
        return _texts(value.text) + _texts_json(value.structured) + _texts_json(value.raw)
    return _texts_json(value)


def _texts_json(value: Any) -> list[str]:
    if value is None:
        return []
    return [json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=True)]


def scan(outcome_or_text: Any, secrets: Collection[str], *,
         prefixes: Collection[str] = SCAN_PREFIXES) -> list[str]:
    """Os rótulos de canário e de segredo achados (lista vazia se nenhum), no texto, no estruturado e no
    `raw` de um `Outcome`, ou num texto, em bytes ou num objeto JSON."""
    hits: set[str] = set()
    for text in _texts(outcome_or_text):
        hits.update(find_canary(text, prefixes=prefixes))
        hits.update(find_secrets(text, list(secrets)))
    return sorted(hits)
