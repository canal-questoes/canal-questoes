"""O pacote cliente construído e instalado fora do repositório.


Sem rede: `uv build --offline` do `client/`, `uv export --frozen` das dependências dele, `uv venv` e
`uv pip install --offline` num diretório temporário (todos com `--offline` e `UV_OFFLINE=1`; nenhum índice de pacotes
é contatado). O ambiente instalado não tem o servidor (`cq_server`) e expõe um único script, `cq`. Os comandos rodam
como subprocessos do `cq` instalado, com o ambiente sem `CQ_*`, `COMPOSE_*`, `DOCKER_HOST` e `DOCKER_CONTEXT` do
desenvolvedor, o perfil de teste por `CQ_CONFIG` (nunca o padrão `127.0.0.1:8000`) e o `docker` falso de
`write_fake_docker` no início do `PATH` (o argv registrado conferido contra a lista fechada do contrato).

Sem o cache local do uv, a construção ou a instalação falham **explicitamente**, com a mensagem fixa
`CACHE_FAILURE_MESSAGE` (nunca `skip`). O repositório não é escrito (a árvore do `client/` fica igual, sem
`dist/` nem `build/`).

**O que já passa no esqueleto:** a construção, o wheel com um único script, a instalação, a sonda sem
`cq_server` e `cq --help` já funcionam no esqueleto; ficam na mesma função de `cq status` com o `docker` falso
(vermelho por comando ausente até a implementação). O comando do catálogo com perfil fica com `cq --profile <nome> status`
(vermelho até a implementação); a falha explícita sem o cache fica com `cq up` fora do repositório (vermelho até a
implementação).
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cq_client.stack import StackDirNotFound

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import UNREACHABLE_API_URL, make_stack_dir, write_profile
from tests.helpers.contract import assert_cli_json
from tests.helpers.docker_fake import assert_allowed, read_fake_docker_calls, write_fake_docker

REPO = Path(__file__).resolve().parents[2]
CLIENT = REPO / "client"
UV_TIMEOUT = 300
CQ_TIMEOUT = 120

CACHE_FAILURE_MESSAGE = ("O pacote cliente não pôde ser construído ou instalado sem rede: o cache local do uv não tem "
                         "o que é preciso (rode `uv sync` com rede antes).")

CLIENT_MODULES = ("cq_client", "cq_client.api", "cq_client.profile", "cq_client.cli", "cq_client.roster",
                  "cq_client.hint", "cq_client.stack", "cq_client.stack_compose", "cq_client.local_commands",
                  "cq_client.doctor")

_CLEARED_PREFIXES = ("CQ_", "COMPOSE_", "UV_", "VIRTUAL_ENV")
_CLEARED_NAMES = ("DOCKER_HOST", "DOCKER_CONTEXT", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")


@dataclass(frozen=True)
class Installed:
    root: Path
    wheel: Path
    python: Path
    cq: Path
    tree_before: tuple
    tree_after: tuple


def _client_tree() -> tuple:
    """Os arquivos do `client/` (sem `__pycache__`), com o tamanho: a árvore que a construção não pode mudar."""
    items = []
    for dirpath, dirnames, filenames in os.walk(CLIENT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            path = Path(dirpath) / name
            items.append((path.relative_to(CLIENT).as_posix(), path.stat().st_size))
    return tuple(sorted(items))


def _uv_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(_CLEARED_PREFIXES) and k.upper() not in _CLEARED_NAMES}
    env.update({"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "PYTHONUTF8": "1"})
    env.update(extra or {})
    return env


def _uv(args: list[str], *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Roda o `uv` sem rede; qualquer falha é a falha explícita do cache ausente (nunca `skip`)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail(CACHE_FAILURE_MESSAGE, pytrace=False)
    result = subprocess.run([uv, *args], cwd=REPO, env=_uv_env(extra_env), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=UV_TIMEOUT)
    if result.returncode != 0:
        pytest.fail(CACHE_FAILURE_MESSAGE, pytrace=False)
    return result


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """Constrói o wheel do `client/`, exporta as dependências travadas e instala os dois num ambiente novo, tudo no
    diretório temporário e sem rede."""
    root = tmp_path_factory.mktemp("pacote-cliente")
    before = _client_tree()
    dist = root / "dist"
    _uv(["build", "--offline", "--wheel", "--out-dir", str(dist), str(CLIENT)])
    wheels = sorted(dist.glob("cq_client-*.whl"))
    assert len(wheels) == 1, wheels
    requirements = root / "requisitos.txt"
    _uv(["export", "--frozen", "--offline", "--no-dev", "--package", "cq-client", "--no-hashes",
         "--no-emit-workspace", "--output-file", str(requirements)])
    venv = root / "venv"
    _uv(["venv", "--offline", "--no-project", "--python", sys.executable, str(venv)])
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    _uv(["pip", "install", "--offline", "--python", str(python), "-r", str(requirements), str(wheels[0])])
    cq = scripts / ("cq.exe" if os.name == "nt" else "cq")
    return Installed(root, wheels[0], python, cq, before, _client_tree())


def _cq_env(config: Path, *, fake_docker_dir: Path | None = None, extra: dict[str, str] | None = None
            ) -> dict[str, str]:
    """O ambiente do `cq` instalado: sem as variáveis do desenvolvedor, com o perfil de teste e o `docker` falso
    no início do `PATH`."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(_CLEARED_PREFIXES) and k.upper() not in _CLEARED_NAMES}
    env["CQ_CONFIG"] = str(config)
    env["PYTHONUTF8"] = "1"
    if fake_docker_dir is not None:
        env["PATH"] = str(fake_docker_dir) + os.pathsep + env.get("PATH", "")
    env.update(extra or {})
    return env


def _cq(pkg: Installed, args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(pkg.cq), *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=CQ_TIMEOUT)


def _assert_package(pkg: Installed) -> None:
    """O wheel sem `__pycache__` nem servidor, com os módulos do cliente e um único script; a árvore do `client/`
    intacta; o ambiente instalado sem `cq_server`."""
    assert pkg.tree_before == pkg.tree_after
    assert not (CLIENT / "dist").exists() and not (CLIENT / "build").exists()
    with zipfile.ZipFile(pkg.wheel) as wheel:
        names = wheel.namelist()
        entry_points = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        parser = configparser.ConfigParser()
        parser.read_string(wheel.read(entry_points).decode("utf-8"))
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(n.startswith("cq_server") for n in names)
    for module in CLIENT_MODULES:
        assert module.replace(".", "/") + (".py" if module != "cq_client" else "/__init__.py") in names, module
    assert parser.sections() == ["console_scripts"]
    assert dict(parser["console_scripts"]) == {"cq": "cq_client.cli:main"}
    probe = ("import importlib.util, json, sys\n"
             f"import {', '.join(CLIENT_MODULES)}\n"
             "print(json.dumps({'server': sorted(m for m in sys.modules if m.split('.')[0] == 'cq_server'),"
             " 'findable': importlib.util.find_spec('cq_server') is not None,"
             " 'location': cq_client.__file__}))\n")
    out = subprocess.run([str(pkg.python), "-I", "-c", probe], cwd=pkg.root, env=_cq_env(pkg.root / "nada.toml"),
                         capture_output=True, text=True, encoding="utf-8", timeout=CQ_TIMEOUT)
    assert out.returncode == 0, out.stderr[-2000:]
    found = json.loads(out.stdout.strip().splitlines()[-1])
    assert found["server"] == [] and found["findable"] is False
    assert Path(found["location"]).resolve().is_relative_to(pkg.root.resolve())


def test_installed_package_help_and_status_with_fake_docker(installed: Installed, tmp_path: Path) -> None:
    """o pacote se constrói e se instala sozinho, sem o servidor e com um único script `cq`
    (`cq --help` → 0); fora do repositório, `cq status` com o perfil local (a API inalcançável em
    `http://127.0.0.1:9`) e o diretório da pilha de teste usa o `docker` falso do `PATH` só com argv da lista
    fechada, e mostra os serviços (código 1 pela API inalcançável; `--json` validado contra `status.output`)."""
    _assert_package(installed)
    outside = tmp_path / "fora-do-repositorio"
    outside.mkdir()
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
    help_result = _cq(installed, ["--help"], _cq_env(config), outside)
    assert help_result.returncode == 0, help_result.stderr
    assert help_result.stdout.startswith("Usage: cq ")
    stack_dir = make_stack_dir(tmp_path)
    docker_dir = tmp_path / "docker-falso"
    write_fake_docker(docker_dir)
    env = _cq_env(config, fake_docker_dir=docker_dir, extra={"CQ_STACK_DIR": str(stack_dir)})
    status = _cq(installed, ["--json", "status"], env, outside)
    assert status.returncode == 1, status.stderr
    body = assert_cli_json("status", status.stdout)
    assert body["services"] == [{"service": "postgres", "state": "running", "health": "healthy"},
                                {"service": "api", "state": "running", "health": "healthy"},
                                {"service": "worker", "state": "running", "health": "none"},
                                {"service": "engine", "state": "running", "health": "healthy"}]
    assert body["api"]["reachable"] is False
    calls = read_fake_docker_calls(docker_dir)
    assert calls, "o cq status não chamou o docker falso"
    assert_allowed(calls, "cq", stack_dir)


def test_installed_catalog_command_with_profile_against_api_subprocess(installed: Installed, tmp_path: Path,
                                                                      api_subprocess: Any) -> None:
    """Pacote instalado: com um perfil nomeado apontando para a API dos testes num subprocesso
    do loopback, uma folha do catálogo pelo `cq` instalado dá 0 com a resposta da API; `cq --profile <nome> status`
    com o `docker` falso (a api publicada na porta do perfil) mostra a pilha saudável e a API saudável (0)."""
    port = int(api_subprocess.url.rsplit(":", 1)[1].split("/")[0])
    outside = tmp_path / "fora-do-repositorio"
    outside.mkdir()
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n\n[profiles.teste]\nkind = "local"\n'
                                     f'api_url = "{api_subprocess.url}"\ntoken = "{ADMIN_TOKEN}"\n')
    stack_dir = make_stack_dir(tmp_path)
    docker_dir = tmp_path / "docker-falso"
    write_fake_docker(docker_dir, samples={"port": port})
    env = _cq_env(config, fake_docker_dir=docker_dir, extra={"CQ_STACK_DIR": str(stack_dir)})
    people = _cq(installed, ["--json", "--profile", "teste", "people", "list"], env, outside)
    assert people.returncode == 0, people.stderr
    assert json.loads(people.stdout)["items"] == []
    status = _cq(installed, ["--json", "--profile", "teste", "status"], env, outside)
    assert status.returncode == 0, status.stderr
    body = assert_cli_json("status", status.stdout)
    assert body["profile"]["name"] == "teste"
    assert body["api"]["reachable"] is True and body["api"]["health"]["status"] == "ok"
    assert body["api_port"] == port
    assert body["warnings"] == []
    assert ADMIN_TOKEN not in people.stdout + people.stderr + status.stdout + status.stderr
    assert_allowed(read_fake_docker_calls(docker_dir), "cq", stack_dir)


def test_installed_up_outside_repository_and_missing_cache(installed: Installed, tmp_path: Path) -> None:
    """`cq up` fora do repositório, sem `stack_dir` nem `CQ_STACK_DIR` → 2, a mensagem fixa de
    `StackDirNotFound`, stdout vazio e nenhuma chamada ao `docker`. Sem o cache local do uv (um cache vazio),
    a instalação sem rede falha explicitamente com a mensagem fixa, sem mudar o ambiente instalado."""
    outside = tmp_path / "fora-do-repositorio"
    outside.mkdir()
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
    docker_dir = tmp_path / "docker-falso"
    write_fake_docker(docker_dir)
    up = _cq(installed, ["up"], _cq_env(config, fake_docker_dir=docker_dir), outside)
    assert (up.returncode, up.stdout) == (2, "")
    assert up.stderr == StackDirNotFound().message + "\n"
    assert read_fake_docker_calls(docker_dir) == []
    empty_cache = tmp_path / "cache-vazio"
    empty_cache.mkdir()
    with pytest.raises(pytest.fail.Exception) as failed:
        _uv(["pip", "install", "--offline", "--dry-run", "--python", str(installed.python), "--reinstall",
             "click==8.5.0"], extra_env={"UV_CACHE_DIR": str(empty_cache)})
    assert str(failed.value) == CACHE_FAILURE_MESSAGE
