"""A configuração da cobertura e do lint no `pyproject.toml` da raiz, as dependências fixadas e o lint do código.

A árvore julgada é a fixture `public_root` (`tests/helpers/public_tree.py`): na árvore exportada, a própria raiz; no
repositório de origem, a exportação do último commit num destino temporário. O `pyproject.toml` e o `uv.lock` são
lidos dela, e o lint roda sobre ela: no repositório de origem, os scripts que não saem na exportação não entram.

Três regras, uma função cada:
- as três dependências de desenvolvimento (`pytest-cov` 7.1.0, `coverage` 7.17.2 e `ruff` 0.17.3) fixadas com `==`
  no grupo `dev` do `pyproject.toml` e no `uv.lock`; o `[tool.ruff]` e o `[tool.coverage]` exatos;
- o ruff do ambiente, com a configuração do `pyproject.toml` público, não acha nada em
  `server/src client/src ops scripts`;
- com a mesma configuração, uma violação plantada num arquivo do `tmp_path` sai com código diferente de 0, e um
  arquivo limpo com uma linha de 120 colunas sai com 0.

**Bloqueio de ambiente (não é vermelho):** sem as três dependências no `uv.lock`, ou sem o ruff 0.17.3 no ambiente,
cada função falha com uma mensagem fixa (`LOCK_ENV_MESSAGE` ou `RUFF_ENV_MESSAGE`), nunca com `skip`.

**Vermelho no esqueleto:** as três funções ficam vermelhas enquanto o `pyproject.toml` não tem a configuração do
ruff e da cobertura (arquivo sem a seção); as dependências fixadas já estão no lock e ficam na primeira função.

**[I]** Escolhas deste teste:
- o ruff do ambiente é o módulo `ruff` do interpretador dos testes (`python -m ruff`), conferido pela versão;
- o ruff roda com `--config` no `pyproject.toml` público (a configuração não depende do diretório nem da do
  desenvolvedor), com `--no-cache` (nada é gravado na árvore julgada) e `--no-fix`;
- as violações plantadas são três, uma por família (`E501` com 121 colunas, `W291` e `F401`); as duas primeiras só
  são achadas com a configuração fixada neste teste, não com a padrão do ruff.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.ops import clean_env
from tests.helpers.public_tree import exported_root

DEV_PINS: dict[str, str] = {"pytest-cov": "7.1.0", "coverage": "7.17.2", "ruff": "0.17.3"}
RUFF_VERSION = "ruff 0.17.3"

RUFF_CONFIG: dict[str, Any] = {"line-length": 120, "target-version": "py312", "lint": {"select": ["E", "F", "W"]}}
COVERAGE_CONFIG: dict[str, Any] = {
    "run": {"source": ["server/src/cq_server", "client/src/cq_client"], "relative_files": True,
            "patch": ["subprocess"]},
    "xml": {"output": "coverage.xml"},
}
LINT_PATHS = ("server/src", "client/src", "ops", "scripts")
RUFF_TIMEOUT_SECONDS = 300

LOCK_ENV_MESSAGE = (
    "Ambiente de teste incompleto: o uv.lock não tem as três dependências de desenvolvimento da cobertura e do lint "
    "fixadas (pytest-cov 7.1.0, coverage 7.17.2 e ruff 0.17.3). Resolva o lock com a rede autorizada e rode de novo."
)
RUFF_ENV_MESSAGE = (
    "Ambiente de teste incompleto: o ruff 0.17.3 do lock não está no ambiente dos testes. Rode uv sync e de novo."
)

# As violações plantadas: (nome do arquivo, conteúdo).
PLANTED: tuple[tuple[str, str], ...] = (
    ("linha_longa.py", "VALUE = '" + "x" * 111 + "'\n"),
    ("espaco_no_fim.py", "VALUE = 1 \n"),
    ("import_sem_uso.py", "import os\n"),
)
CLEAN = ("limpo.py", "VALUE = '" + "x" * 110 + "'\n")


@pytest.fixture(scope="session")
def public_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A árvore julgada (uma exportação por processo, no repositório de origem)."""
    return exported_root(tmp_path_factory)


# --------------------------------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------------------------------


def _toml(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    assert path.is_file(), f"arquivo ausente na árvore pública: {name}"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _require_lock(root: Path) -> None:
    """As três dependências no `uv.lock`, na versão fixada e no grupo `dev` do manifesto; senão, o bloqueio."""
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        pytest.fail(LOCK_ENV_MESSAGE, pytrace=False)
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    versions = {str(package.get("name")): str(package.get("version")) for package in lock.get("package", [])}
    manifest = lock.get("manifest", {}).get("dependency-groups", {}).get("dev", [])
    for name, version in DEV_PINS.items():
        if versions.get(name) != version or {"name": name, "specifier": f"=={version}"} not in manifest:
            pytest.fail(LOCK_ENV_MESSAGE, pytrace=False)


def _ruff(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, "-m", "ruff", *args], cwd=cwd, env=clean_env(), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=RUFF_TIMEOUT_SECONDS, check=False)


def _require_ruff(cwd: Path) -> None:
    """O ruff 0.17.3 no ambiente dos testes; senão, o bloqueio."""
    try:
        probe = _ruff("--version", cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail(RUFF_ENV_MESSAGE, pytrace=False)
    if probe.returncode != 0 or probe.stdout.decode("utf-8", "replace").strip() != RUFF_VERSION:
        pytest.fail(RUFF_ENV_MESSAGE, pytrace=False)


def _quality_config(root: Path) -> Path:
    """O `[tool.ruff]` e o `[tool.coverage]` exatos no `pyproject.toml` público; devolve o caminho dele."""
    tool = _toml(root, "pyproject.toml").get("tool", {})
    assert tool.get("ruff") == RUFF_CONFIG, f"pyproject.toml: [tool.ruff] diferente do esperado: {tool.get('ruff')!r}"
    assert tool.get("coverage") == COVERAGE_CONFIG, (
        f"pyproject.toml: [tool.coverage] diferente do esperado: {tool.get('coverage')!r}")
    return root / "pyproject.toml"


def _check(config: Path, cwd: Path, *paths: str) -> subprocess.CompletedProcess[bytes]:
    return _ruff("check", "--no-cache", "--no-fix", "--output-format=concise", "--config", str(config), *paths,
                 cwd=cwd)


def _findings(completed: subprocess.CompletedProcess[bytes]) -> str:
    lines = (completed.stdout + completed.stderr).decode("utf-8", "replace").splitlines()
    return "\n".join(lines[:40])


# --------------------------------------------------------------------------------------------------
# Testes
# --------------------------------------------------------------------------------------------------


def test_dev_dependencies_pinned_and_ruff_and_coverage_config_exact(public_root: Path) -> None:
    """As três dependências de desenvolvimento fixadas com `==` no grupo `dev` do `pyproject.toml` e no `uv.lock`
    (sem elas no lock, bloqueio de ambiente); o `[tool.ruff]` e o `[tool.coverage]` exatos. Vermelho enquanto o
    `pyproject.toml` não tem a configuração do ruff e da cobertura; as dependências já estão fixadas."""
    _require_lock(public_root)
    dev = _toml(public_root, "pyproject.toml").get("dependency-groups", {}).get("dev", [])
    for name, version in DEV_PINS.items():
        assert f"{name}=={version}" in dev, f"pyproject.toml: {name}=={version} fora do grupo dev: {dev}"
    _quality_config(public_root)


def test_ruff_reports_nothing_in_the_public_code(public_root: Path) -> None:
    """O ruff do ambiente, com a configuração do `pyproject.toml` público, sem achado em `server/src`, `client/src`,
    `ops` e `scripts` da árvore pública (sem o lock ou sem o ruff no ambiente, bloqueio de ambiente). Vermelho
    enquanto o `pyproject.toml` não tem a configuração do ruff e da cobertura e, depois, até as linhas longas do
    código público serem quebradas."""
    _require_lock(public_root)
    _require_ruff(public_root)
    config = _quality_config(public_root)
    paths = [path for path in LINT_PATHS if (public_root / path).exists()]
    missing = sorted(set(LINT_PATHS) - set(paths))
    assert not missing, f"pastas do lint ausentes na árvore pública: {missing}"
    completed = _check(config, public_root, *paths)
    assert completed.returncode == 0, f"o ruff achou problemas no código público:\n{_findings(completed)}"


def test_planted_violation_fails_with_the_same_config(public_root: Path, tmp_path: Path) -> None:
    """Com a configuração do `pyproject.toml` público, cada violação plantada num arquivo do `tmp_path` (uma linha de
    121 colunas, um espaço no fim da linha, um import sem uso) faz o ruff sair com código diferente de 0, e um arquivo
    limpo com uma linha de 120 colunas sai com 0 (sem o lock ou sem o ruff no ambiente, bloqueio de ambiente).
    Vermelho enquanto o `pyproject.toml` não tem a configuração do ruff e da cobertura."""
    _require_lock(public_root)
    _require_ruff(public_root)
    config = _quality_config(public_root)
    clean_name, clean_text = CLEAN
    assert len(clean_text.rstrip("\n")) == 120
    assert len(PLANTED[0][1].rstrip("\n")) == 121
    (tmp_path / clean_name).write_text(clean_text, encoding="utf-8", newline="\n")
    clean = _check(config, tmp_path, clean_name)
    assert clean.returncode == 0, f"o arquivo limpo foi recusado:\n{_findings(clean)}"
    for name, text in PLANTED:
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
        planted = _check(config, tmp_path, name)
        assert planted.returncode != 0, f"a violação plantada em {name} passou pelo ruff"
