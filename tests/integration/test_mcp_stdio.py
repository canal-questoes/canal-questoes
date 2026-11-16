"""O processo `cq mcp` por stdio.


- O `cq mcp` num subprocesso (`stdio_server_params` de `tests/helpers/mcp_client.py`: `sys.executable -c "from
  cq_client.cli import main; main()" mcp`), nas duas eras (`MODES`), com a API inalcançável
  (`http://127.0.0.1:9`, também em `CQ_API_URL`, para que não haja a dica do CLI) e com o perfil inválido (um
  arquivo que não é TOML): o `tools/list` traz os nomes da união calculada (catálogo exposto ∪ locais) e o `run_doctor`
  devolve o erro fixo (`[api_unreachable] …` ou `[profile.InvalidProfileFile] …`), sem `structuredContent`.
- O stdout bruto é gravado por um **intermediário** (`TEE_CODE`, entre o `stdio_client` do SDK e o `cq mcp`) que
  copia cada byte do stdout do servidor para um arquivo: cada linha é uma mensagem JSON-RPC 2.0 válida, e há as
  respostas do `tools/list` e do `tools/call`.
- O stderr (o `errlog` do SDK, num arquivo real) só tem linhas inteiras de `MCP_STDERR_MESSAGES.values()`, com a
  `started`; nenhum segredo de teste nem identidade canário.
- EOF imediato (`run_eof`): código 0, stdout vazio, stderr = `started` e `stopped`, nas duas partidas.
- O erro interno só **em processo**: um `ServerDeps` com um `http` que levanta `RuntimeError` com um detalhe-sentinela,
  sob `quiet_libraries()`: `[mcp_internal] …` sem detalhe, a linha `internal` em `deps.stderr`, e o detalhe em
  nenhuma saída nem registro.
- `quiet_libraries()` silencia os loggers de `MCP_LOGGERS` e os `warnings` e restaura a configuração ao sair.
- Nenhuma variável de injeção no produto: os nomes `CQ_…` citados nos módulos novos são os que o CLI já usa.

**Vermelho no esqueleto:** a folha `mcp` não existe e `serve`, `build_server` e `quiet_libraries` lançam
`NotImplementedError` (até serem implementados). A checagem estática das variáveis (verde no esqueleto) fica na mesma
função do erro interno em processo.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import sys
import warnings
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client.stdio import StdioServerParameters

from cq_client.mcp_server import (
    ERROR_TEXT_TEMPLATE,
    LOCAL_TOOL_BINDINGS,
    MCP_LOGGERS,
    MCP_MESSAGES,
    MCP_STDERR_MESSAGES,
    quiet_libraries,
)
from cq_client.profile import InvalidProfileFile

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import ENV_SENTINEL, ENV_SENTINEL_TOKEN, UNREACHABLE_API_URL, write_profile
from tests.helpers.contract import REPO, load_catalog
from tests.helpers.mcp_client import (
    MODES,
    call,
    in_process,
    list_tools,
    make_deps,
    run,
    run_eof,
    scan,
    stdio_server_params,
    stdio_session,
)
from tests.helpers.secrets import all_secrets, bot_secrets

CLIENT_SRC = REPO / "client" / "src" / "cq_client"
# Os módulos novos que leem o ambiente (o `log_redaction` não o lê).
NEW_MODULES = ("mcp_server.py", "local_ops.py")
CLI_MODULES = ("profile.py", "stack.py", "stack_compose.py", "hint.py", "local_commands.py", "doctor.py", "api.py",
              "roster.py", "cli.py")

INTERNAL_SENTINEL = "detalhe-interno-sentinela-Zq81Wx"
STARTUPS = ("api-inalcancavel", "perfil-invalido")

# O intermediário que grava o stdout bruto do servidor: `argv[1]` = o arquivo da gravação, `argv[2:]` = o comando do
# servidor. Repassa a entrada ao filho (e o EOF), copia o stdout do filho para o próprio stdout e para o arquivo, e
# sai com o código do filho; o stderr do filho é o do intermediário (o `errlog`).
TEE_CODE = """
import os, subprocess, sys, threading
log = open(sys.argv[1], "wb")
child = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def pump_in():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            child.stdin.write(data)
            child.stdin.flush()
    except OSError:
        pass
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
threading.Thread(target=pump_in, daemon=True).start()
out = sys.stdout.buffer
while True:
    data = os.read(child.stdout.fileno(), 65536)
    if not data:
        break
    log.write(data)
    log.flush()
    try:
        out.write(data)
        out.flush()
    except OSError:
        pass
log.close()
sys.exit(child.wait())
"""


def _secrets() -> list[str]:
    return all_secrets(*bot_secrets(), ADMIN_TOKEN, ENV_SENTINEL, ENV_SENTINEL_TOKEN)


def _expected_tool_names() -> list[str]:
    """A união calculada: as do catálogo exposto, na ordem dele, e as locais (sem total fixo)."""
    catalog = [op["mcp"]["tool"] for op in load_catalog()["operations"] if op["mcp"]["exposed"]]
    local = [binding.name for bindings in LOCAL_TOOL_BINDINGS.values() for binding in bindings]
    return catalog + local


def _startup_env(tmp_path: Path, startup: str) -> tuple[dict[str, str], str]:
    """O `env` explícito da partida e o texto esperado do `run_doctor`."""
    if startup == "api-inalcancavel":
        config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
        env = {"CQ_CONFIG": str(config), "CQ_API_URL": UNREACHABLE_API_URL}
        expected = ERROR_TEXT_TEMPLATE.format(code="api_unreachable", message=MCP_MESSAGES["api_unreachable"])
        return env, expected
    config = write_profile(tmp_path, "isto não é [[ um TOML válido\n")
    expected = ERROR_TEXT_TEMPLATE.format(code="profile.InvalidProfileFile",
                                          message=MCP_MESSAGES["profile.InvalidProfileFile"])
    assert MCP_MESSAGES["profile.InvalidProfileFile"] == InvalidProfileFile.reason
    return {"CQ_CONFIG": str(config)}, expected


def _assert_stderr_lines(text: str) -> list[str]:
    """Cada linha do stderr é uma linha inteira de `MCP_STDERR_MESSAGES`; devolve as linhas."""
    lines = text.splitlines()
    allowed = set(MCP_STDERR_MESSAGES.values())
    assert [line for line in lines if line not in allowed] == [], text[-2000:]
    return lines


def _tee_params(params: StdioServerParameters, record: Path) -> StdioServerParameters:
    """Os mesmos parâmetros, com o intermediário `TEE_CODE` na frente do comando do servidor."""
    return params.model_copy(update={"command": sys.executable,
                                     "args": ["-c", TEE_CODE, str(record), params.command, *params.args]})


def _fail_with_server_stderr(exc: BaseException, errlog: Path) -> None:
    """A sessão stdio caiu (por exemplo, `Connection closed` porque o `cq mcp` saiu antes do `initialize`): falha
    citando o stderr do servidor (o `errlog`), que diz por que ele saiu; um segredo de teste nele não é impresso."""
    stderr = errlog.read_text(encoding="utf-8", errors="replace") if errlog.exists() else ""
    if scan(stderr, _secrets()):
        stderr = "<o stderr contém um segredo de teste; omitido>"
    pytest.fail(f"a sessão com o `cq mcp` caiu ({type(exc).__name__}: {exc}); stderr do servidor:\n{stderr[-2000:]}",
                pytrace=False)


@pytest.mark.parametrize("startup", STARTUPS)
@pytest.mark.parametrize("mode", MODES)
def test_stdio_lists_tools_and_calls_doctor_in_both_eras(mode: str, startup: str, tmp_path: Path) -> None:
    """o servidor inicia sem API e com o perfil inválido, nas
    duas eras; o `tools/list` traz a união calculada; o `run_doctor` devolve o erro fixo da partida (`isError`, sem
    estruturado); cada linha do stdout bruto é JSON-RPC 2.0 (com as respostas das duas chamadas); o stderr só tem
    linhas fixas (com a `started`); nada de segredo nem de identidade canário nas saídas."""
    env, expected = _startup_env(tmp_path, startup)
    record = tmp_path / "stdout-bruto.jsonl"
    errlog = tmp_path / "stderr.txt"
    params = _tee_params(stdio_server_params(env, tmp_path), record)

    async def go() -> tuple[list[dict[str, Any]], Any]:
        async with stdio_session(params, mode, errlog) as client:
            tools = await list_tools(client)
            outcome = await call(client, "run_doctor", {})
            return tools, outcome

    try:
        tools, outcome = run(go)
    except Exception as exc:  # noqa: BLE001 — a sessão caiu: a falha mostra o stderr do servidor
        _fail_with_server_stderr(exc, errlog)
    assert [tool["name"] for tool in tools] == _expected_tool_names()
    assert outcome.protocol_code is None
    assert (outcome.is_error, outcome.structured, outcome.text) == (True, None, expected)

    raw = record.read_bytes()
    assert raw.endswith(b"\n")
    messages = []
    for line in raw.split(b"\n")[:-1]:
        message = json.loads(line.decode("utf-8"))
        assert isinstance(message, dict) and message.get("jsonrpc") == "2.0", line[:200]
        assert ("method" in message) or ("id" in message and ("result" in message or "error" in message)), line[:200]
        messages.append(message)
    results = [m["result"] for m in messages if isinstance(m.get("result"), dict)]
    assert any("tools" in r for r in results)
    assert any(r.get("isError") is True for r in results)

    stderr = errlog.read_text(encoding="utf-8", errors="replace")
    assert MCP_STDERR_MESSAGES["started"] in _assert_stderr_lines(stderr)
    assert scan(raw, _secrets()) == []
    assert scan(stderr, _secrets()) == []
    assert scan(outcome, _secrets()) == []


@pytest.mark.parametrize("startup", STARTUPS)
def test_stdio_eof_exits_zero_with_only_fixed_stderr_lines(startup: str, tmp_path: Path) -> None:
    """com a entrada vazia (EOF imediato), o `cq mcp` sai com 0, sem nada no stdout, e o
    stderr tem exatamente `started` e `stopped`, nessa ordem; também com o perfil inválido."""
    env, _expected = _startup_env(tmp_path, startup)
    result = run_eof(env, tmp_path)
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, stderr[-2000:]
    assert result.stdout == b""
    assert _assert_stderr_lines(stderr) == [MCP_STDERR_MESSAGES["started"], MCP_STDERR_MESSAGES["stopped"]]
    assert scan(stderr, _secrets()) == []


def _cq_names(path: Path) -> set[str]:
    """Os nomes `CQ_…` citados nas constantes de texto de um módulo."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.update(re.findall(r"\bCQ_[A-Z0-9_]+", node.value))
    return names


@pytest.mark.parametrize("mode", MODES)
def test_internal_error_in_process_is_fixed_and_leaks_nothing(mode: str, tmp_path: Path,
                                                             caplog: pytest.LogCaptureFixture,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    """um `http` que levanta `RuntimeError` (com um detalhe-sentinela) faz o
    `run_doctor` devolver `[mcp_internal] <mensagem fixa>` (`isError`, sem estruturado), e o `deps.stderr` recebe só a
    linha `internal`; sob `quiet_libraries()`, o detalhe não aparece no resultado, no `deps.stderr`, no stderr do
    processo nem nos registros (`caplog`). Nenhuma variável de injeção no produto: os nomes `CQ_…` nos módulos novos
    estão entre os dos módulos do CLI, e nenhum cita `PYTEST`."""
    caplog.set_level(logging.DEBUG)
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')

    def explode(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError(INTERNAL_SENTINEL)

    stderr = io.StringIO()
    deps = make_deps(tmp_path, env={"CQ_CONFIG": str(config)},
                     http=httpx2.Client(transport=httpx2.MockTransport(explode)), stderr=stderr)

    async def go() -> Any:
        async with in_process(deps, mode) as client:
            return await call(client, "run_doctor", {})

    with quiet_libraries():
        outcome = run(go)
    assert outcome.protocol_code is None
    expected = ERROR_TEXT_TEMPLATE.format(code="mcp_internal", message=MCP_MESSAGES["mcp_internal"])
    assert (outcome.is_error, outcome.structured, outcome.text) == (True, None, expected)
    assert expected != ERROR_TEXT_TEMPLATE.format(code="internal", message=MCP_MESSAGES["api.internal"])
    assert stderr.getvalue().splitlines() == [MCP_STDERR_MESSAGES["internal"]]
    captured = capsys.readouterr()
    for text in (json.dumps(outcome.raw, ensure_ascii=False), stderr.getvalue(), captured.err, captured.out,
                 caplog.text):
        assert INTERNAL_SENTINEL not in text
        assert ADMIN_TOKEN not in text

    known = set().union(*(_cq_names(CLIENT_SRC / name) for name in CLI_MODULES))
    for name in NEW_MODULES:
        source = (CLIENT_SRC / name).read_text(encoding="utf-8")
        assert _cq_names(CLIENT_SRC / name) <= known, name
        assert "PYTEST" not in source.upper(), name


def test_quiet_libraries_silences_and_restores(caplog: pytest.LogCaptureFixture,
                                               capsys: pytest.CaptureFixture[str],
                                               recwarn: pytest.WarningsRecorder) -> None:
    """dentro de `quiet_libraries()`, cada logger de `MCP_LOGGERS` tem `propagate=False`, nível acima de
    `CRITICAL` e um `NullHandler`, um erro num logger filho não chega a lugar nenhum e um `warnings.warn` é ignorado;
    ao sair, os loggers (propagação, nível, tratadores, desligado) e os filtros de `warnings` voltam a ser os de antes,
    e um aviso depois é registrado (controle)."""
    caplog.set_level(logging.DEBUG)

    def snapshot() -> tuple[dict[str, tuple], list]:
        loggers = {}
        for name in MCP_LOGGERS:
            lg = logging.getLogger(name)
            loggers[name] = (lg.propagate, lg.level, list(lg.handlers), lg.disabled)
        return loggers, list(warnings.filters)

    before = snapshot()
    with quiet_libraries():
        for name in MCP_LOGGERS:
            lg = logging.getLogger(name)
            assert lg.propagate is False, name
            assert lg.level > logging.CRITICAL, name
            assert any(isinstance(h, logging.NullHandler) for h in lg.handlers), name
        logging.getLogger("mcp.server.lowlevel.sentinela").error("registro-sentinela-quiet")
        logging.getLogger("httpx").critical("registro-sentinela-quiet")
        warnings.warn("aviso-sentinela-quiet", UserWarning, stacklevel=1)
    assert snapshot() == before
    warnings.warn("aviso-controle-quiet", UserWarning, stacklevel=1)

    captured = capsys.readouterr()
    assert "registro-sentinela-quiet" not in caplog.text + captured.err + captured.out
    messages = [str(w.message) for w in recwarn if "-quiet" in str(w.message)]
    assert messages == ["aviso-controle-quiet"]
