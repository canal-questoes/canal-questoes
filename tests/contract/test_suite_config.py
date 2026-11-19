"""Configuração da suíte paralela (a suíte em paralelo com o `pytest-xdist`, fora da parte de sistema).


O que se confere, só lendo arquivos da árvore (nada roda):
- `pytest-xdist==3.8.0` no grupo `dev` do `pyproject.toml` e, com o `execnet`, no `uv.lock` (lidos como TOML);
- `scripts/run-suite.sh`: cada parte sem sistema (as linhas `run <nome> … -m "not system"`) com
  `-n "${CQ_SUITE_WORKERS:-<N>}" --dist worksteal` (`<N>` inteiro qualquer, por expressão regular), as partes do
  Windows mantidas (`unit`, `contract`, `cli`, `intA`, `intB`, `privacy`; a divisão por portas) e a
  parte `-m system` **sem** `-n` nem `--dist`; nenhuma referência à suíte do motor;
- `scripts/test-postgres.sh`: `-c max_connections=<inteiro>` e `--shm-size=<valor>` no `docker run`, com a mesma
  imagem fixada por digest;
- a suíte do motor (`scripts/run-engine-suite.sh`) sem `-n` nem `pytest`;
- `pytest.ini` e `tests/conftest.py` iguais aos hashes congelados (copiados como literais em `FROZEN_SUITE_HASHES`),
  sem `-n`, `xdist` nem `addopts` novos; nenhum `conftest.py` na raiz
  (contornaria o congelamento).

**Vermelho no esqueleto**: o `-n` do `run-suite.sh` e o `max_connections` do
`test-postgres.sh` falham por asserção ainda não cumprida até a implementação. As asserções sobre o que o esqueleto já
tem (`pytest.ini`, `tests/conftest.py`, a suíte do motor, nenhum `conftest.py` na raiz, as partes do Windows) ficam na
mesma função de um desses casos vermelhos. O caso do `pyproject.toml` e do `uv.lock` fica numa função própria:
é **higiene declarada**, verde já no esqueleto, que já traz a dependência.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RUN_SUITE = REPO / "scripts" / "run-suite.sh"
TEST_POSTGRES = REPO / "scripts" / "test-postgres.sh"
ENGINE_SUITE = REPO / "scripts" / "run-engine-suite.sh"
# **[F]** Hashes copiados aqui: o do `pytest.ini` é o congelado (lido em 2026-10-29); o do `tests/conftest.py`,
# o do arquivo atual. Os documentos privados não vão para
# a exportação pública (só os arquivos de `ops/export/test-needs.txt`), e o teste tem de rodar também no CI exportado.
FROZEN_SUITE_HASHES = {
    "pytest.ini": "ec48e2026257c787632edff3f9f794dcd7a597bcf292fdc5b0204d443454c024",
    "tests/conftest.py": "bedb81fe811f05c26428e8711de54727333e6d17526e3218d84da57e2d627fb8",
}

XDIST_VERSION = "3.8.0"
# A forma esperada: o número vem da variável, com padrão inteiro.
PARALLEL = re.compile(r'(?<!\S)-n\s+"\$\{CQ_SUITE_WORKERS:-[0-9]+\}"\s+--dist\s+worksteal(?!\S)')
# As partes do Windows: a divisão por diretório continua.
WINDOWS_PARTS = ("unit", "contract", "cli", "intA", "intB", "privacy")
# A imagem do Postgres de teste (o digest não muda; só entram as duas opções novas).
POSTGRES_IMAGE = ("postgres:18.6@sha256:86c951e05bf56c93d95d397747fb8820ac76cc3bedb78f43abd83eedbe3666ae")


def _text(path: Path) -> str:
    assert path.is_file(), f"arquivo ausente: {path.relative_to(REPO).as_posix()}"
    return path.read_text(encoding="utf-8")


def _run_lines(script: str) -> dict[str, str]:
    """As chamadas `run <nome> …` do `run-suite.sh` (uma por parte), sem a definição da função `run()`."""
    parts: dict[str, str] = {}
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped.startswith("run ") or stripped.startswith("run()"):
            continue
        name = stripped.split()[1]
        assert name not in parts, f"parte repetida no run-suite.sh: {name}"
        parts[name] = stripped
    return parts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_xdist_is_pinned_in_the_dev_group_and_in_the_lock() -> None:
    """`pytest-xdist==3.8.0` no grupo `dev` do `pyproject.toml`; no `uv.lock`, o pacote na
    versão 3.8.0 (com o `execnet` como dependência) e o `execnet` presente; o especificador do grupo `dev` no
    manifesto do lock igual. Higiene declarada: verde no esqueleto, que já traz a dependência."""
    project = tomllib.loads(_text(REPO / "pyproject.toml"))
    dev = project["dependency-groups"]["dev"]
    assert f"pytest-xdist=={XDIST_VERSION}" in dev, dev

    lock = tomllib.loads(_text(REPO / "uv.lock"))
    packages = {p["name"]: p for p in lock["package"]}
    xdist = packages.get("pytest-xdist")
    assert xdist is not None, "pytest-xdist ausente do uv.lock"
    assert xdist["version"] == XDIST_VERSION
    assert {"name": "execnet"} in xdist.get("dependencies", []), xdist.get("dependencies")
    assert "execnet" in packages, "execnet ausente do uv.lock"
    manifest_groups = lock.get("manifest", {}).get("dependency-groups", {})
    assert {"name": "pytest-xdist", "specifier": f"=={XDIST_VERSION}"} in manifest_groups.get("dev", []), (
        manifest_groups.get("dev"))


def test_run_suite_is_parallel_only_outside_system_and_the_frozen_config_is_untouched() -> None:
    """no `run-suite.sh`, `-n "${CQ_SUITE_WORKERS:-<N>}" --dist worksteal` em toda
    parte sem sistema e nenhum `-n` na parte `-m system`; as partes do Windows mantidas; a suíte do motor fora.
    Na mesma função: `pytest.ini` e `tests/conftest.py` com os hashes congelados, sem `-n`, `xdist`
    nem `addopts` novos; nenhum `conftest.py` na raiz; o `run-engine-suite.sh` sem `-n` nem `pytest`.
    Vermelho até a implementação (o `-n` ainda não está no `run-suite.sh`)."""
    # O que o esqueleto já cumpre.
    for name, digest in FROZEN_SUITE_HASHES.items():
        assert _sha256(REPO / name) == digest, f"{name} mudou em relação ao congelado"
    ini = _text(REPO / "pytest.ini")
    assert "xdist" not in ini and not re.search(r"(?<!\S)-n\b", ini), "o paralelismo entra só pela linha de comando"
    addopts = [line for line in ini.splitlines() if line.strip().startswith("addopts")]
    assert addopts == ["addopts = --import-mode=importlib -ra --strict-markers"], addopts
    conftest = _text(REPO / "tests" / "conftest.py")
    assert "xdist" not in conftest
    root_names = {path.name for path in REPO.iterdir()}
    assert "conftest.py" not in root_names, "um conftest.py na raiz contornaria o congelamento"

    engine = _text(ENGINE_SUITE)
    assert "pytest" not in engine and not re.search(r"(?<!\S)-n(?!\S)", engine), "a suíte do motor fica fora"

    script = _text(RUN_SUITE)
    assert "run-engine-suite" not in script, "nenhuma outra suíte junto da do motor"
    parts = _run_lines(script)
    for name in WINDOWS_PARTS:
        assert name in parts, f"a parte {name} do Windows sumiu do run-suite.sh"

    # O comportamento novo do `run-suite.sh`.
    system_parts = {name: line for name, line in parts.items() if '-m system' in line and "not system" not in line}
    assert list(system_parts) == ["system"], f"a parte de sistema: {list(system_parts)}"
    system_args = shlex.split(system_parts["system"])
    assert "-n" not in system_args and not any(a.startswith("--dist") for a in system_args), (
        "a parte -m system roda em série")
    for name, line in parts.items():
        if name in system_parts:
            continue
        assert '-m "not system"' in line, f"parte {name} sem o filtro de sistema"
        assert PARALLEL.search(line), f'parte {name} sem -n "${{CQ_SUITE_WORKERS:-N}}" --dist worksteal: {line}'


def test_test_postgres_raises_max_connections_and_shm_with_the_same_image() -> None:
    """O `docker run` do Postgres de teste com `-c max_connections=<inteiro>` e
    `--shm-size=<valor>`, a mesma imagem fixada e o mesmo nome e porta. Vermelho até a implementação."""
    script = _text(TEST_POSTGRES)
    assert f"IMAGE={POSTGRES_IMAGE}" in script, "a imagem do Postgres de teste não muda"
    assert "NAME=cq-test-pg" in script and "PORT=55432" in script
    run_start = script.index("docker run -d")
    run_block = script[run_start:script.index('"${IMAGE}"', run_start)]
    assert re.search(r"--shm-size=[0-9]+[bkmgBKMG]?(?!\S)", run_block), "o docker run sem --shm-size"
    tail = script[run_start:script.index("fi", run_start)]
    assert re.search(r'"\$\{IMAGE\}"[\s\\]+-c[\s\\]+max_connections=[0-9]+', tail), (
        "o docker run sem -c max_connections=<N> depois da imagem")
