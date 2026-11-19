"""O pacote cliente da operação por agente construído e instalado fora do repositório.


Reusa, sem editá-lo, o congelado `tests/integration/test_client_package.py` (a fixture `installed`,
que constrói o wheel e o instala sem rede num diretório temporário; os ajudantes `_uv`, `_cq_env`, `_assert_package`;
a mensagem `CACHE_FAILURE_MESSAGE`). Nenhum `skip`: sem o cache local do uv, a construção ou a instalação falham
explicitamente.

- O wheel tem `cq_client/local_ops.py`, `cq_client/log_redaction.py`, `cq_client/mcp_server.py` e as cópias dos
  contratos em `cq_client/mcp_contract/`: `mcp/v1/tools.json`, `mcp/v1/tools.schema.json`, todo
  arquivo de `contracts/mcp/v1/schemas/` e cada esquema citado por `tools.json` (resolvido a partir de `mcp/v1/`),
  cada um byte a byte igual ao de `contracts/`, e nenhum outro arquivo de dados; no ambiente instalado, os módulos
  da operação por agente importam sem `cq_server` e o `tools.json` sai do pacote por `importlib.resources`.
- As dependências travadas exportadas trazem o `mcp`; sem o cache, reinstalar o `mcp` sem rede falha com a mensagem
  fixa.
- O `cq mcp` **instalado** (o script `cq` do ambiente novo, num diretório fora do repositório, com a API inalcançável
  em `http://127.0.0.1:9`) responde ao `tools/list` nas duas eras com os mesmos nomes do servidor em processo, iguais à
  união calculada (catálogo exposto ∪ locais; sem total fixo); o stderr só com linhas de `MCP_STDERR_MESSAGES`.

**Vermelho no esqueleto:** as cópias de `mcp_contract/` não existem; o `tools/list` em processo e o `cq mcp`
lançam `NotImplementedError` ou não existem. As asserções do CLI sobre o pacote (`_assert_package`) e a
falha sem o cache, já verdes, ficam na mesma função das cópias.
"""

from __future__ import annotations

import json
import posixpath
import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
from mcp.client.stdio import StdioServerParameters

from cq_client.mcp_server import LOCAL_TOOL_BINDINGS, MCP_CONTRACT_PACKAGE, MCP_STDERR_MESSAGES, TOOLS_FILE

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import UNREACHABLE_API_URL, write_profile
from tests.helpers.contract import REPO, load_catalog
from tests.helpers.mcp_client import HOME_VARIABLES, MODES, in_process, list_tools, make_deps, run, scan, stdio_session
from tests.integration.test_client_package import (  # noqa: F401 - `installed` é a fixture do congelado
    CACHE_FAILURE_MESSAGE,
    CQ_TIMEOUT,
    Installed,
    _assert_package,
    _cq_env,
    _uv,
    installed,
)

CONTRACTS = REPO / "contracts"
NEW_MODULES = ("cq_client/local_ops.py", "cq_client/log_redaction.py", "cq_client/mcp_server.py")
CONTRACT_PREFIX = MCP_CONTRACT_PACKAGE.replace(".", "/") + "/"


def _expected_contract_files() -> set[str]:
    """Os caminhos (relativos a `contracts/`) que o pacote copia."""
    tools = json.loads((CONTRACTS / TOOLS_FILE).read_text(encoding="utf-8"))
    expected = {TOOLS_FILE, "mcp/v1/tools.schema.json"}
    expected |= {p.relative_to(CONTRACTS).as_posix() for p in (CONTRACTS / "mcp" / "v1" / "schemas").rglob("*")
                 if p.is_file()}
    for tool in tools["tools"]:
        for key in ("input_schema", "output_schema"):
            path = posixpath.normpath(posixpath.join(posixpath.dirname(TOOLS_FILE), tool[key]))
            assert not path.startswith(".."), path
            expected.add(path)
    return expected


def _expected_tool_names() -> list[str]:
    catalog = [op["mcp"]["tool"] for op in load_catalog()["operations"] if op["mcp"]["exposed"]]
    local = [binding.name for bindings in LOCAL_TOOL_BINDINGS.values() for binding in bindings]
    return catalog + local


def _locked_version(name: str) -> str:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == name]
    assert len(versions) == 1, versions
    return versions[0]


def test_installed_wheel_has_agent_modules_and_contract_copies(installed: Installed, tmp_path: Path) -> None:
    """o pacote do CLI continua igual (`_assert_package`: um único script `cq`, sem
    `cq_server`); o wheel tem os 3 módulos do agente e, em `cq_client/mcp_contract/`, exatamente as cópias esperadas
    (fora `.py`), byte a byte iguais a `contracts/`; no ambiente instalado, esses módulos importam sem `cq_server`
    e o `tools.json` lido por `importlib.resources` é igual ao do repositório. As dependências exportadas trazem o
    `mcp`, e reinstalá-lo sem rede e sem o cache falha com `CACHE_FAILURE_MESSAGE` (nunca `skip`)."""
    _assert_package(installed)
    expected = _expected_contract_files()
    with zipfile.ZipFile(installed.wheel) as wheel:
        names = set(wheel.namelist())
        for module in NEW_MODULES:
            assert module in names, module
        copies = {n[len(CONTRACT_PREFIX):] for n in names
                  if n.startswith(CONTRACT_PREFIX) and not n.endswith((".py", "/"))}
        assert sorted(expected - copies) == [], "cópias ausentes do wheel"
        assert sorted(copies - expected) == [], "arquivos a mais no wheel"
        different = sorted(p for p in expected if wheel.read(CONTRACT_PREFIX + p) != (CONTRACTS / p).read_bytes())
        assert different == []

    probe = ("import importlib.resources, json, sys\n"
             "import cq_client.local_ops, cq_client.log_redaction, cq_client.mcp_server\n"
             f"root = importlib.resources.files({MCP_CONTRACT_PACKAGE!r})\n"
             f"data = root.joinpath(*{TOOLS_FILE!r}.split('/')).read_bytes()\n"
             "print(json.dumps({'server': sorted(m for m in sys.modules if m.split('.')[0] == 'cq_server'),"
             " 'tools': data.decode('utf-8'), 'location': cq_client.mcp_server.__file__}))\n")
    out = subprocess.run([str(installed.python), "-I", "-c", probe], cwd=installed.root,
                         env=_cq_env(installed.root / "nada.toml"), capture_output=True, text=True, encoding="utf-8",
                         timeout=CQ_TIMEOUT)
    assert out.returncode == 0, out.stderr[-2000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["server"] == []
    assert found["tools"].encode("utf-8") == (CONTRACTS / TOOLS_FILE).read_bytes()
    assert Path(found["location"]).resolve().is_relative_to(installed.root.resolve())

    requirements = (installed.root / "requisitos.txt").read_text(encoding="utf-8")
    version = _locked_version("mcp")
    assert any(line.split(";")[0].strip() == f"mcp=={version}" for line in requirements.splitlines())
    empty_cache = tmp_path / "cache-vazio"
    empty_cache.mkdir()
    with pytest.raises(pytest.fail.Exception) as failed:
        _uv(["pip", "install", "--offline", "--dry-run", "--python", str(installed.python), "--reinstall",
             f"mcp=={version}"], extra_env={"UV_CACHE_DIR": str(empty_cache)})
    assert str(failed.value) == CACHE_FAILURE_MESSAGE


@pytest.mark.parametrize("mode", MODES)
def test_installed_cq_mcp_lists_tools_in_both_eras(mode: str, installed: Installed, tmp_path: Path) -> None:
    """o `cq mcp` instalado, num diretório fora do repositório, com o perfil local de API
    inalcançável (`http://127.0.0.1:9`) e o diretório do usuário no `tmp_path`, responde ao `tools/list` na era pedida
    com os mesmos nomes, na mesma ordem, do servidor em processo, iguais à união calculada; o stderr só com linhas de
    `MCP_STDERR_MESSAGES` (com a `started`) e sem o token do perfil."""
    outside = tmp_path / "fora-do-repositorio"
    outside.mkdir()
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
    env = _cq_env(config) | {name: str(tmp_path) for name in HOME_VARIABLES}
    params = StdioServerParameters(command=str(installed.cq), args=["mcp"], env=env, cwd=str(outside))
    errlog = tmp_path / "stderr.txt"

    async def installed_names() -> list[str]:
        async with stdio_session(params, mode, errlog) as client:
            return [tool["name"] for tool in await list_tools(client)]

    async def in_process_names() -> list[str]:
        async with in_process(make_deps(tmp_path), mode) as client:
            return [tool["name"] for tool in await list_tools(client)]

    local: Any = run(in_process_names)
    names: Any = run(installed_names)
    assert local == _expected_tool_names()
    assert names == local

    lines = errlog.read_text(encoding="utf-8", errors="replace").splitlines()
    assert [line for line in lines if line not in set(MCP_STDERR_MESSAGES.values())] == []
    assert MCP_STDERR_MESSAGES["started"] in lines
    assert scan("\n".join(lines), [ADMIN_TOKEN]) == []
