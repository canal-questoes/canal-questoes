"""Fronteiras da operação por agente (princípio "Portas e contratos";
a folha `cq mcp`).

Arquivo à parte: `tests/contract/test_boundaries_cli.py` e `test_catalog.py`
ficam congelados e sem edição, e os verificadores deles são reusados aqui. Cobre os três módulos do cliente
(`cq_client.mcp_server`, `cq_client.local_ops` e `cq_client.log_redaction`):
- sem `cq_server`: estático (AST, o verificador de `test_catalog.py`) e na sonda de `sys.modules` num subprocesso;
- **só por AST** (como o `with_subprocess` do CLI): sem `click` no nível do módulo **nem** preguiçoso, e sem
  `subprocess` (**[F]** o `import mcp` carrega `subprocess` transitivamente, `mcp/os/win32/utilities.py:5`; a sonda
  transitiva não serve para ele);
- as conferências do `CLIENT_MODULES` do CLI repetidas para os três (sem `cq_server`; `click` só onde `LAZY_CLICK`
  permite, isto é, em nenhum deles);
- `local_ops` e `log_redaction` não importam `mcp` (estático e na sonda); `mcp_server` não importa `cq_client.cli`;
- importar `cq_client.cli` num subprocesso não carrega `mcp` nem `cq_client.mcp_server` (o `cli` importa o servidor
  só dentro da função do comando).

Falha esperada no esqueleto: as checagens estáticas e a sonda do `cli` já passam no
esqueleto, e por isso ficam na **mesma função** do controle positivo: a folha `cq mcp`, invocada num
subprocesso com o stdin vazio, carrega `cq_client.mcp_server` e `mcp`. Vermelho: a folha `mcp` está ausente
da árvore do `cq` até a implementação.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from mcp.client.stdio import get_default_environment

from tests.contract.test_boundaries import _imported_names
from tests.contract.test_boundaries_cli import CLIENT_MODULES, LAZY_CLICK, _click_imports, _client_sources, _loaded_by
from tests.contract.test_catalog import _imports_server
from tests.helpers.mcp_client import HOME_VARIABLES, MISSING_PROFILE_FILE

# Os três módulos do cliente da operação por agente.
AGENT_CLIENT_MODULES = ("mcp_server", "local_ops", "log_redaction")
# Os que não podem importar o SDK `mcp` (as peças locais e a redação servem também ao CLI).
WITHOUT_MCP = ("cq_client.local_ops", "cq_client.log_redaction")
MCP_TOP_LEVEL = frozenset({"mcp", "mcp_types"})

# A folha `cq mcp` num processo novo (**[F]** `main()` não recebe argumentos e o `_RootGroup.main` termina em
# `sys.exit`): o resultado sai pelo stderr, nunca pelo stdout do protocolo, na última linha.
LEAF_PROBE = (
    "import json, sys\n"
    'sys.argv = ["cq", "mcp"]\n'
    "from cq_client.cli import main\n"
    "try:\n"
    "    main()\n"
    "finally:\n"
    '    print(json.dumps({"mcp_server": "cq_client.mcp_server" in sys.modules, "mcp": "mcp" in sys.modules}),\n'
    "          file=sys.stderr)\n"
)
LEAF_PROBE_TIMEOUT_SECONDS = 120


def _top_level(name: str) -> str:
    return name.split(".")[0]


def _names(source: str, modname: str) -> list[str]:
    return sorted({name for _, name in _imported_names(source, modname, False)})


def _sdk_loaded(modules: list[str]) -> list[str]:
    return [m for m in modules if _top_level(m) in MCP_TOP_LEVEL]


def _server_loaded(modules: list[str]) -> list[str]:
    return [m for m in modules if _top_level(m) == "cq_server"]


def _run_leaf(tmp_path: Path) -> subprocess.CompletedProcess:
    """`cq mcp` num processo novo, com o stdin vazio e o ambiente explícito (o herdado seguro do SDK, o diretório do
    usuário no `tmp_path` e o `CQ_CONFIG` num arquivo inexistente do `tmp_path`; nenhuma `CQ_*` do desenvolvedor)."""
    env = get_default_environment() | {name: str(tmp_path) for name in HOME_VARIABLES}
    env["CQ_CONFIG"] = str(tmp_path / MISSING_PROFILE_FILE)
    return subprocess.run([sys.executable, "-c", LEAF_PROBE], input=b"", env=env, cwd=str(tmp_path),
                          capture_output=True, timeout=LEAF_PROBE_TIMEOUT_SECONDS, check=False)


def test_agent_client_modules_respect_boundaries_and_mcp_leaf_loads_the_server(tmp_path: Path) -> None:
    """Portas e contratos; a folha `cq mcp`.

    Estrutura (já vale no esqueleto): `mcp_server`, `local_ops` e `log_redaction` existem no pacote e, por AST, não
    importam `cq_server`, não importam `click` em nível nenhum (nem preguiçoso: `LAZY_CLICK` não os cita) e não
    importam `subprocess`; as conferências de `CLIENT_MODULES` do CLI continuam valendo; `local_ops` e
    `log_redaction` não importam `mcp` nem `mcp_types`; `mcp_server` não importa `cq_client.cli`; controles
    positivos dos verificadores sobre trechos sintéticos. Num subprocesso, importar os três não carrega `cq_server`,
    importar `local_ops` e `log_redaction` não carrega o SDK, e importar `cq_client.cli` não carrega `mcp` nem
    `cq_client.mcp_server`.

    Controle positivo (a ligação): a folha `cq mcp`, num processo novo com o stdin vazio (EOF imediato), carrega
    `cq_client.mcp_server` e `mcp`, e sai com 0.

    Vermelho no esqueleto: a folha `mcp` está ausente da árvore do `cq`."""
    sources = _client_sources()
    new_modules = [f"cq_client.{module}" for module in AGENT_CLIENT_MODULES]
    assert set(new_modules) <= set(sources), "módulos novos ausentes do pacote cliente"
    assert set(AGENT_CLIENT_MODULES).isdisjoint(CLIENT_MODULES)
    assert set(new_modules).isdisjoint(LAZY_CLICK)

    for modname in new_modules:
        source = sources[modname]
        names = _names(source, modname)
        assert _imports_server(ast.parse(source)) == [], f"{modname} importa cq_server"
        assert _click_imports(source) == [], f"{modname} importa click: {_click_imports(source)}"
        assert [name for name in names if _top_level(name) == "subprocess"] == [], f"{modname} importa subprocess"
    # As conferências do CLI (`CLIENT_MODULES`) continuam valendo, com os três módulos ao lado.
    for module in CLIENT_MODULES:
        modname = f"cq_client.{module}"
        assert _imports_server(ast.parse(sources[modname])) == [], modname
        assert _click_imports(sources[modname]) == LAZY_CLICK.get(modname, []), modname
    for modname in WITHOUT_MCP:
        assert _sdk_loaded(_names(sources[modname], modname)) == [], f"{modname} importa o SDK mcp"
    server_names = _names(sources["cq_client.mcp_server"], "cq_client.mcp_server")
    assert [n for n in server_names if n == "cq_client.cli" or n.startswith("cq_client.cli.")] == [], (
        "cq_client.mcp_server importa cq_client.cli")

    # Controles positivos dos verificadores (trechos sintéticos).
    assert _sdk_loaded(_names("import mcp.types as types\n", "cq_client.local_ops")) == ["mcp.types"]
    assert "cq_client.cli" in _names("from .cli import main\n", "cq_client.mcp_server")
    assert "subprocess" in _names("from subprocess import run\n", "cq_client.log_redaction")
    assert _click_imports("def f():\n    import click\n    return click\n") == ["f"]
    assert _imports_server(ast.parse("from cq_server.api import create_app\n")) == ["cq_server.api"]

    # A sonda transitiva (`sys.modules` num processo novo).
    loaded_new = _loaded_by(new_modules)
    assert set(new_modules) <= set(loaded_new)
    assert _server_loaded(loaded_new) == [], "os módulos novos carregaram cq_server"
    loaded_local = _loaded_by(list(WITHOUT_MCP))
    assert set(WITHOUT_MCP) <= set(loaded_local)
    assert _sdk_loaded(loaded_local) == [], "local_ops e log_redaction carregaram o SDK mcp"
    loaded_cli = _loaded_by(["cq_client.cli"])
    assert "cq_client.cli" in loaded_cli
    assert "mcp" not in loaded_cli and "cq_client.mcp_server" not in loaded_cli, (
        "importar cq_client.cli carregou o servidor MCP")
    assert "mcp" in loaded_new, "controle positivo da sonda: importar cq_client.mcp_server não carregou mcp"

    # O controle positivo da ligação: a folha `cq mcp` carrega o servidor e o SDK.
    result = _run_leaf(tmp_path)
    stderr = result.stderr.decode("utf-8", errors="replace")
    lines = stderr.strip().splitlines()
    assert lines, "a sonda da folha não escreveu no stderr"
    loaded_by_leaf = json.loads(lines[-1])
    assert loaded_by_leaf == {"mcp_server": True, "mcp": True}, (
        f"cq mcp não carregou o servidor MCP (folha mcp ausente?); código {result.returncode}")
    assert result.returncode == 0, (result.returncode, stderr[-500:])
