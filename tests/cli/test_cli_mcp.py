"""A folha `cq mcp`.

- A folha `mcp` sem parâmetros próprios, com a opção global `--profile` na cadeia, nenhum parâmetro de token ou
  segredo; a ajuda (`cq mcp --help`) com o texto do contrato e sem `--token`; a linha da folha na ajuda da raiz
  sem citar token.
- Uso incorreto (`cq mcp extra`, `cq mcp --x`) → 2, `Uso incorreto:`, stdout vazio e sem eco.
- `cq mcp` em processo (o `CliRunner`) com a entrada vazia → 0, stdout vazio e o stderr só com as linhas fixas
  `started` e `stopped` de `MCP_STDERR_MESSAGES`.
- `cq --profile <nome> mcp` repassa o nome a `mcp_server.serve` (um dublê no atributo do módulo registra o
  argumento) e sai com o código que ele devolve; sem `--profile`, o seletor é `None`.
- Os demais comandos do CLI não carregam `mcp` nem `cq_client.mcp_server` (num subprocesso, pela `sys.modules`).

Ambiente limpo pelo `invoke_cli` (`CQ_CONFIG` sempre num arquivo do `tmp_path`); nenhum Docker, nenhuma rede (a
API inalcançável é `http://127.0.0.1:9`).

**Vermelho no esqueleto:** a folha `mcp` não existe (só a constante `MCP_COMMAND`) até a implementação.
Que os demais comandos do CLI não carregam `mcp` já vale no esqueleto e fica na mesma função da invocação de `cq mcp`;
a forma da cadeia e os casos de uso incorreto ficam na mesma função da ajuda da folha.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest

from cq_client.cli import MCP_COMMAND

from tests.contract.test_catalog import FORBIDDEN_OPTS, FORBIDDEN_PARAM_NAMES, _cli_command, _cli_leaves, _param_shape
from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import UNREACHABLE_API_URL, invoke_cli, write_profile

# a ajuda da folha.
MCP_HELP = "Inicia o servidor MCP (stdio) para agentes, com o perfil escolhido."
PROFILE_SHAPE = ("option", ("--profile",), (), False, False, False, 1)
USAGE_SENTINEL = "argumentosentinelamcp"

# Os comandos do CLI rodados no subprocesso (nenhum chama o Docker; a API inalcançável por `CQ_API_URL`, sem a dica).
OTHER_COMMANDS: tuple[tuple[str, ...], ...] = (("--help",), ("people", "list"), ("offerings", "list"), ("doctor",),
                                               ("events", "list", "--person", "pseudonimo-inexistente"))
PROBE_TIMEOUT = 120

_PROBE = """
import json, sys
from click.testing import CliRunner
from cq_client import cli
runner = CliRunner()
codes = []
for args in json.loads(sys.argv[1]):
    codes.append(runner.invoke(cli.cli, args).exit_code)
loaded = sorted(m for m in sys.modules
                if m == "mcp" or m.startswith("mcp.") or m == "cq_client.mcp_server")
print(json.dumps({"codes": codes, "loaded": loaded}))
"""


def _no_traceback(result: Any) -> None:
    assert "Traceback" not in result.stderr, result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)


def _probe_env(tmp_path: Path) -> dict[str, str]:
    """O ambiente do subprocesso: o do processo sem `CQ_*`, `COMPOSE_*`, `DOCKER_HOST` e `DOCKER_CONTEXT`; o perfil
    inexistente do `tmp_path`, a API inalcançável e o diretório do usuário no `tmp_path`."""
    import os

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("CQ_", "COMPOSE_")) and k.upper() not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
    env.update({"CQ_CONFIG": str(tmp_path / "cq-config-inexistente.toml"), "CQ_API_URL": UNREACHABLE_API_URL,
                "PYTHONUTF8": "1"})
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME"):
        env[name] = str(tmp_path)
    return env


def test_mcp_leaf_shape_help_and_usage_errors(tmp_path: Path) -> None:
    """a folha `mcp` existe e não tem parâmetros próprios; na cadeia, só a opção global
    `--profile` (opcional, com valor) e as demais da raiz, nenhuma de token ou segredo; `cq mcp --help` → 0 com o
    texto do contrato e sem `--token`; a linha `mcp` da ajuda da raiz sem "token". Uso incorreto (`cq mcp
    <argumento>`, `cq mcp --x`, `cq mcp --token x`) → 2, stdout vazio, `Uso incorreto:` e sem eco."""
    from cq_client.cli import cli

    help_result = invoke_cli([MCP_COMMAND, "--help"], tmp_path=tmp_path)
    _no_traceback(help_result)
    assert help_result.exit_code == 0, help_result.stderr
    assert MCP_HELP in " ".join(help_result.stdout.split())
    assert "--token" not in help_result.stdout

    leaves = _cli_leaves(cli)
    assert MCP_COMMAND in leaves
    assert _cli_command(cli, MCP_COMMAND).params == []
    chain = leaves[MCP_COMMAND]
    assert [_param_shape(p) for p in chain if "--profile" in getattr(p, "opts", [])] == [PROFILE_SHAPE]
    assert [p.name for p in chain if isinstance(p, click.Argument)] == []
    offending = [p.name for p in chain if p.name in FORBIDDEN_PARAM_NAMES
                 or FORBIDDEN_OPTS & {*getattr(p, "opts", []), *getattr(p, "secondary_opts", [])}]
    assert offending == []

    root_help = invoke_cli(["--help"], tmp_path=tmp_path)
    _no_traceback(root_help)
    assert root_help.exit_code == 0
    lines = [line for line in root_help.stdout.splitlines() if line.split()[:1] == [MCP_COMMAND]]
    assert len(lines) == 1, root_help.stdout
    assert "token" not in lines[0].casefold()

    for args in ([MCP_COMMAND, USAGE_SENTINEL], [MCP_COMMAND, f"--{USAGE_SENTINEL}"],
                 [MCP_COMMAND, "--x"], [MCP_COMMAND, "--token", ADMIN_TOKEN], [MCP_COMMAND, f"--token={ADMIN_TOKEN}"]):
        result = invoke_cli(args, tmp_path=tmp_path, answer="")
        _no_traceback(result)
        assert result.exit_code == 2, (args, result.stderr)
        assert result.stdout == ""
        assert result.stderr.startswith("Uso incorreto:"), result.stderr
        assert USAGE_SENTINEL not in result.stderr.casefold()
        assert ADMIN_TOKEN not in result.stderr and "--token" not in result.stderr


def test_mcp_runs_in_process_with_empty_stdin_and_other_commands_do_not_load_mcp(tmp_path: Path) -> None:
    """`cq mcp` em processo com a entrada vazia (EOF) e o perfil inexistente →
    0, stdout vazio, stderr = as linhas `started` e `stopped`, inteiras e nessa ordem. Num subprocesso, os comandos
    do CLI de `OTHER_COMMANDS` rodam sem carregar `mcp` nem `cq_client.mcp_server` (o import do servidor fica
    dentro da folha)."""
    from cq_client.mcp_server import MCP_STDERR_MESSAGES

    result = invoke_cli([MCP_COMMAND], tmp_path=tmp_path, answer="")
    _no_traceback(result)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr.splitlines() == [MCP_STDERR_MESSAGES["started"], MCP_STDERR_MESSAGES["stopped"]]

    out = subprocess.run([sys.executable, "-c", _PROBE, json.dumps([list(a) for a in OTHER_COMMANDS])],
                         cwd=str(tmp_path), env=_probe_env(tmp_path),
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT)
    assert out.returncode == 0, out.stderr[-2000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(found["codes"]) == len(OTHER_COMMANDS)
    assert found["codes"][0] == 0
    assert found["loaded"] == []


@pytest.mark.parametrize("returned", [0, 1], ids=["serve-0", "serve-1"])
def test_profile_selector_is_passed_to_serve(returned: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cq --profile teste mcp` chama `mcp_server.serve` uma vez com o seletor `"teste"`
    (posicional ou por nome) e sai com o código devolvido; `cq mcp` sem `--profile` chama com `None`. O nome do perfil
    não é ecoado na saída."""
    from cq_client import mcp_server

    signature = inspect.signature(mcp_server.serve)
    calls: list[dict[str, Any]] = []

    def fake_serve(*args: Any, **kwargs: Any) -> int:
        calls.append(dict(signature.bind(*args, **kwargs).arguments))
        return returned

    monkeypatch.setattr(mcp_server, "serve", fake_serve)
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n\n[profiles.teste]\nkind = "local"\n'
                                     f'api_url = "{UNREACHABLE_API_URL}"\n')

    named = invoke_cli(["--profile", "teste", MCP_COMMAND], config=config, answer="")
    _no_traceback(named)
    assert named.exit_code == returned, named.stderr
    assert named.stdout == "" and "teste" not in named.stderr
    assert [call["selector"] for call in calls] == ["teste"]

    implicit = invoke_cli([MCP_COMMAND], config=config, answer="")
    _no_traceback(implicit)
    assert implicit.exit_code == returned, implicit.stderr
    assert [call["selector"] for call in calls] == ["teste", None]
