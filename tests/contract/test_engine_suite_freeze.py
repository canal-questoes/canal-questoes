"""O script da suíte do motor pula a conferência do congelamento só na árvore exportada.

A conferência do congelamento dos testes do motor lê uma lista que só existe no repositório de origem. Na árvore
exportada (o marcador `ops/ci/known-flaky.txt` rastreado pelo git, o mesmo de `tests/helpers/export_tree.py`), o
`scripts/run-engine-suite.sh` não roda a conferência e escreve a linha fixa `SKIP_LINE`; no repositório de origem, a
lista ausente continua erro.

O teste copia o `scripts/run-engine-suite.sh` e o `scripts/check-frozen-engine-tests.sh` de hoje para repositórios git
de fixture no `tmp_path` e os roda com `docker`, `node` e `curl` falsos no começo do `PATH` (o `curl` falso devolve
`200`, senão a espera do oráculo no script dorme até 300 s) e o `CQ_SUITE_OUT` no `tmp_path`. Nada fala com o Docker,
com a rede ou com a árvore real. Os falsos registram cada chamada num arquivo, conferido para provar que o `PATH`
falso valeu.

O `bash` é o de `shutil.which("bash")`. **Bloqueio de ambiente (não é vermelho):** sem `bash` no `PATH`, ou no
Windows com um `bash` cujo `uname -s` dá `Linux` (o do WSL, que não enxerga o `tmp_path` nem o `PATH` falso), o teste
falha com a mensagem fixa `BASH_ENV_MESSAGE`, nunca com `skip`.

**Vermelho no esqueleto:** o caso da árvore exportada fica vermelho até o script pular a conferência (senão ele a roda,
ela não acha a lista e o script sai com 1). Os dois casos do repositório de origem (sem o marcador; com o marcador no
disco, mas não rastreado), que o script já cumpre no esqueleto, ficam na mesma função.

**[I]** Escolhas deste teste:
- a linha fixa é conferida como uma linha inteira da saída padrão, sem os espaços das pontas;
- nos dois casos do repositório de origem, a linha fixa também não pode aparecer;
- além do `PATH` falso, a pasta do próprio `bash` entra logo depois dela, para os utilitários do script (`sha256sum`,
  `seq`, `grep`) virem da mesma instalação;
- os falsos são scripts `#!/bin/sh`, que o `bash` do Git for Windows também executa pelo cabeçalho.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers import git_fixture
from tests.helpers.export_tree import EXPORT_MARKER
from tests.helpers.ops import REPO

SCRIPTS: tuple[str, ...] = ("scripts/run-engine-suite.sh", "scripts/check-frozen-engine-tests.sh")
SKIP_LINE = "congelamento: sem lista neste repositório"
MARKER_CONTENT = b"# instaveis conhecidos\n"
SCRIPT_TIMEOUT_SECONDS = 180
PROBE_TIMEOUT_SECONDS = 60

BASH_ENV_MESSAGE = (
    "Ambiente de teste incompleto: este teste roda os scripts da suíte do motor por um bash que enxerga os arquivos e "
    "o PATH deste sistema. Sem bash no PATH, ou com o bash do WSL no Windows, instale o Git for Windows (o bash dele "
    "antes do WSL no PATH) e rode de novo."
)

# Os comandos falsos: registram a chamada no `CQ_FAKE_LOG` e saem com 0; o `curl` responde `200`.
FAKE_COMMANDS: dict[str, str] = {
    "docker": '#!/bin/sh\necho "docker $*" >> "$CQ_FAKE_LOG"\nexit 0\n',
    "node": '#!/bin/sh\necho "node $*" >> "$CQ_FAKE_LOG"\nexit 0\n',
    "curl": '#!/bin/sh\necho "curl $*" >> "$CQ_FAKE_LOG"\nprintf 200\nexit 0\n',
}


def _bash() -> str:
    """O `bash` de `shutil.which`, que enxerga o `tmp_path`; senão, o bloqueio de ambiente."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail(BASH_ENV_MESSAGE, pytrace=False)
    if os.name == "nt":
        try:
            probe = subprocess.run([bash, "-c", "uname -s"], stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=PROBE_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pytest.fail(BASH_ENV_MESSAGE, pytrace=False)
        system = probe.stdout.decode("utf-8", "replace").strip()
        if probe.returncode != 0 or not system or system.startswith("Linux"):
            pytest.fail(BASH_ENV_MESSAGE, pytrace=False)
    return bash


def _fake_bin(tmp_path: Path) -> Path:
    folder = tmp_path / "falsos"
    folder.mkdir()
    for name, body in FAKE_COMMANDS.items():
        target = folder / name
        target.write_bytes(body.encode("utf-8"))
        target.chmod(0o755)
    return folder


def _scripts() -> dict[str, bytes]:
    return {path: REPO.joinpath(*path.split("/")).read_bytes() for path in SCRIPTS}


def _run(bash: str, repo: Path, fake_bin: Path, tmp_path: Path, label: str) -> tuple[int, list[str], str]:
    """Roda a cópia do script da suíte do motor em `repo`; devolve o código, as linhas da saída e o registro."""
    log = tmp_path / f"chamadas-{label}.txt"
    env = git_fixture.git_env(tmp_path)
    for name in ("BASH_ENV", "ENV"):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
    env["PATH"] = os.pathsep.join((str(fake_bin), str(Path(bash).parent), env.get("PATH", "")))
    env["CQ_SUITE_OUT"] = (tmp_path / "saidas").as_posix()
    env["CQ_FAKE_LOG"] = log.as_posix()
    completed = subprocess.run([bash, "scripts/run-engine-suite.sh", label], cwd=repo, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=SCRIPT_TIMEOUT_SECONDS,
                               check=False)
    lines = [line.strip() for line in completed.stdout.decode("utf-8", "replace").splitlines()]
    calls = log.read_text(encoding="utf-8") if log.is_file() else ""
    return completed.returncode, lines, calls


def test_engine_suite_skips_the_freeze_check_only_in_the_exported_tree(tmp_path: Path) -> None:
    """Com o marcador da árvore exportada rastreado e sem a lista do congelamento, o script da suíte do motor escreve a
    linha fixa e sai com 0; sem o marcador, ou com o marcador no disco mas não rastreado, a lista ausente continua
    erro (código diferente de 0, sem a linha fixa). Vermelho até o script pular a conferência na árvore exportada;
    os dois casos do repositório de origem já passam no esqueleto."""
    bash = _bash()
    fake_bin = _fake_bin(tmp_path)

    exported = git_fixture.make_repo(tmp_path, {**_scripts(), EXPORT_MARKER: MARKER_CONTENT}, name="exportada")
    code, lines, calls = _run(bash, exported, fake_bin, tmp_path, "exportada")
    assert "docker build" in calls and "curl " in calls, f"os comandos falsos não foram chamados: {calls!r}"
    assert SKIP_LINE in lines, f"a árvore exportada sem a linha fixa {SKIP_LINE!r}: {lines[-6:]}"
    assert code == 0, f"a árvore exportada saiu com o código {code}: {lines[-6:]}"

    private = git_fixture.make_repo(tmp_path, _scripts(), name="origem")
    code, lines, calls = _run(bash, private, fake_bin, tmp_path, "origem")
    assert "docker build" in calls, f"os comandos falsos não foram chamados: {calls!r}"
    assert code != 0, "sem o marcador e sem a lista, o script saiu com 0"
    assert SKIP_LINE not in lines, "sem o marcador, o script pulou a conferência"

    untracked = git_fixture.make_repo(tmp_path, _scripts(), name="marcador-solto")
    git_fixture.add_untracked(untracked, EXPORT_MARKER, MARKER_CONTENT)
    code, lines, calls = _run(bash, untracked, fake_bin, tmp_path, "marcador-solto")
    assert "docker build" in calls, f"os comandos falsos não foram chamados: {calls!r}"
    assert code != 0, "com o marcador não rastreado e sem a lista, o script saiu com 0"
    assert SKIP_LINE not in lines, "com o marcador não rastreado, o script pulou a conferência"
