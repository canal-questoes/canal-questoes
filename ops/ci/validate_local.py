"""Validação local do CI num container.


Uso:

    python ops/ci/validate_local.py --export-dir <temporário fora do repositório>

Roda, num container `python:3.12-slim-trixie` (o digest do `Dockerfile`) com usuário sem privilégio e a
pasta exportada montada, o mesmo passo do age Linux do workflow (`scripts/fetch_age.py`) e depois o
`ops/ci/ci_suite.py fast`. Recursos Docker com o prefixo `RESOURCE_PREFIX` + id; remove só o que criou;
nunca monta o socket do Docker (`FORBIDDEN_MOUNTS`).

Só biblioteca padrão. Importável sem efeito colateral. Os testes só conferem o plano de comandos
(`plan_commands`), sem Docker.

Como funciona:

1. `plan_commands` recusa (`ValueError`, antes de qualquer comando) o `--export-dir` igual à raiz do
   repositório ou dentro dela, um id de execução fora de `[a-z0-9]`, e caminhos com vírgula (quebrariam o
   `--mount`). As imagens vêm dos arquivos da origem, para não haver uma segunda fonte dos digests: o Python
   e o `uv` do `Dockerfile` (`FROM python:3.12-slim-trixie@sha256:…` e `COPY --from=ghcr.io/astral-sh/uv:…`) e o
   Postgres do `scripts/test-postgres.sh` (`IMAGE=…`).
2. O plano, em três fases (`_phases`), todas com os nomes `RESOURCE_PREFIX + id`:
   - preparação: a rede própria; o Postgres de teste nela (como o `scripts/test-postgres.sh`: `trust`,
     `tmpfs`, `--shm-size=256m`, `max_connections=300`); um container **só criado** (nunca iniciado) da imagem
     fixada do `uv`, de onde o `docker cp` tira o binário para um diretório próprio no temporário do sistema
     (`<temp>/cq-ci-local-<id>-uv`), que o container do Python monta só para leitura;
   - os passos: um único container do Python, com `--user 999:999` (o uid do `nonroot` do `Dockerfile`),
     `--cap-drop ALL` e `no-new-privileges`, a exportação montada só para leitura em `/src`, e um `sh -c` que
     copia a exportação para `/tmp/cq-work` (o dono passa a ser o usuário do container), roda os passos do job
     `fast` do `.github/workflows/ci.yml` na mesma ordem (`uv sync --locked`, o aquecimento do cache do `uv`
     para o `test_client_package.py`, o age Linux com o `CQ_TEST_AGE_DIR`, a espera do Postgres e o
     `ci_suite.py fast`);
   - a limpeza: `docker rm -f` dos três containers e `docker network rm` da rede, sempre depois dos passos.
3. `run` executa a preparação e os passos em ordem, parando no primeiro código diferente de zero; a limpeza
   roda **sempre** (num `finally`), com os erros ignorados (um recurso que não chegou a ser criado não existe,
   e o nome dele é exclusivo desta execução), e o diretório do `uv` no temporário é apagado. Nada de `prune`,
   `rmi` nem `image rm`: as imagens baixadas ficam no cache do Docker.

**[I]** Escolhas da implementação, não fixadas de antemão:
- o `git` do Debian (os testes da exportação e do pacote o chamam; o runner do GitHub já o tem, a imagem slim
  não). Sem root não há `apt-get install`: o pacote `git` é baixado pelo `apt-get download` com listas e cache
  num temporário do container e extraído por `dpkg-deb -x` em `/tmp/cq-git`, que entra no `PATH`. Só o
  binário `git` (os subcomandos usados são internos; ele liga só a `libc`, `zlib` e `pcre2`, que a imagem base
  já tem). Se falhar, o script avisa e segue: os testes que precisam do `git` falham na suíte, com o nome;
- a espera do Postgres é uma conexão TCP: o `docker-entrypoint.sh` da imagem só abre a porta TCP no servidor
  definitivo (o temporário da inicialização escuta só no socket Unix);
- os textos das mensagens e os códigos de saída (`VALIDATE_MESSAGES`).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

RESOURCE_PREFIX = "cq-ci-local-"
FORBIDDEN_MOUNTS = ("/var/run/docker.sock", "//./pipe/docker_engine")

# A forma do `subprocess.run` (`runner(argv, *, env, timeout, check)`).
Runner = Callable[..., Any]

# Chave → (código de saída, mensagem). [I] Chaves, códigos e textos da implementação; nenhum ecoa entrada.
VALIDATE_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Validação local concluída: a suíte rápida passou no container."),
    "suite_failed": (1, "Validação local concluída com falha dentro do container (veja a saída acima)."),
    "setup_failed": (69, "Não foi possível preparar os recursos Docker da validação."),
    "docker_missing": (69, "O docker não foi encontrado no PATH."),
    "invalid_export_dir": (64, "O --export-dir tem de ser um diretório fora do repositório."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "internal": (1, "Erro interno na validação local."),
}

# A raiz deste repositório (o script vive em `ops/ci/`).
REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKER = "docker"
SETUP_TIMEOUT_SECONDS = 15 * 60
SUITE_TIMEOUT_SECONDS = 90 * 60
CLEANUP_TIMEOUT_SECONDS = 2 * 60

# O uid/gid do usuário `nonroot` do `Dockerfile` (sem entrada no /etc/passwd da imagem base: o HOME vem do -e).
CONTAINER_USER = "999:999"

_RUN_ID = re.compile(r"^[a-z0-9]{1,32}$")
_PYTHON_FROM = re.compile(r"^FROM (python:3\.12-slim-trixie@sha256:[0-9a-f]{64})\b", re.MULTILINE)
_UV_FROM = re.compile(r"--from=(ghcr\.io/astral-sh/uv:[0-9.]+@sha256:[0-9a-f]{64})\s")
_PG_IMAGE = re.compile(r"^IMAGE=(postgres:[0-9.]+@sha256:[0-9a-f]{64})\s*$", re.MULTILINE)

# Caminhos dentro do container do Python.
_SRC = "/src"
_UV_MOUNT = "/opt/cq-uv"
_WORK = "/tmp/cq-work"
_BIN = "/tmp/cq-bin"
_AGE_DIR = "/tmp/cq-age"
_GIT_ROOT = "/tmp/cq-git"
_APT = "/tmp/cq-apt"

_Phases = tuple[list[list[str]], list[list[str]], list[list[str]]]


def _emit(key: str) -> int:
    code, text = VALIDATE_MESSAGES[key]
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write(text + "\n")
    stream.flush()
    return code


def _is_inside(path: Path, root: Path) -> bool:
    resolved = os.path.normcase(str(path.resolve()))
    base = os.path.normcase(str(root.resolve()))
    return resolved == base or resolved.startswith(base.rstrip(os.sep) + os.sep)


def _single(pattern: re.Pattern[str], text: str) -> str:
    found = set(pattern.findall(text))
    if len(found) != 1:
        raise ValueError("imagem fixada ausente ou ambígua na origem")
    return found.pop()


def _images(repo_root: Path) -> tuple[str, str, str]:
    """(Python, uv, Postgres), com os digests da origem."""
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    postgres = (repo_root / "scripts" / "test-postgres.sh").read_text(encoding="utf-8")
    return _single(_PYTHON_FROM, dockerfile), _single(_UV_FROM, dockerfile), _single(_PG_IMAGE, postgres)


def uv_host_dir(run_id: str) -> Path:
    """O diretório do binário do `uv` no temporário do sistema, exclusivo da execução."""
    return Path(tempfile.gettempdir()) / f"{RESOURCE_PREFIX}{run_id}-uv"


def _container_script(pg_host: str) -> str:
    """O `sh -c` do container do Python: os passos do job `fast`, na ordem do workflow."""
    apt = (f"-o Dir::State::Lists={_APT}/lists -o Dir::Cache={_APT}/cache -o Debug::NoLocking=1 -q")
    wait_pg = (
        "import socket, sys, time\n"
        "for _ in range(120):\n"
        "    try:\n"
        f"        socket.create_connection(('{pg_host}', 5432), 2).close()\n"
        "        sys.exit(0)\n"
        "    except OSError:\n"
        "        time.sleep(1)\n"
        "sys.exit(1)\n"
    )
    lines = [
        "set -eu",
        f"mkdir -p {_BIN} {_WORK} \"$HOME\" {_APT}/lists/partial {_APT}/cache/archives/partial {_GIT_ROOT}",
        f"cp {_UV_MOUNT}/uv {_BIN}/uv",
        f"chmod 0755 {_BIN}/uv",
        f"export PATH=\"{_BIN}:{_GIT_ROOT}/root/usr/bin:$PATH\"",
        "echo '== git do Debian (sem root)'",
        f"if (cd {_GIT_ROOT} && apt-get {apt} update && apt-get {apt} download git"
        f" && dpkg-deb -x git_*.deb {_GIT_ROOT}/root) > /dev/null; then git --version;"
        " else echo 'aviso: sem o git; os testes que o usam vão falhar' >&2; fi",
        "echo '== cópia da exportação'",
        f"cp -R {_SRC}/. {_WORK}/",
        f"cd {_WORK}",
        "echo '== uv sync --locked'",
        "uv sync --locked",
        "echo '== cache do uv para o pacote cliente'",
        # [I] O teste do pacote cliente instala sem rede (`uv pip install --offline -r <requisitos>`), o que precisa dos
        # metadados do índice no cache; o `uv sync --locked` não os grava. O aquecimento repete, com rede, os passos do
        # teste.
        "uv build --wheel --out-dir /tmp/cq-uv-warm client",
        "uv export --frozen --no-dev --package cq-client --no-hashes --no-emit-workspace "
        "--output-file /tmp/cq-uv-warm/req.txt",
        "uv venv --no-project --python python3 /tmp/cq-uv-warm/venv",
        "uv pip install --python /tmp/cq-uv-warm/venv/bin/python -r /tmp/cq-uv-warm/req.txt "
        "/tmp/cq-uv-warm/cq_client-*.whl",
        "echo '== age v1.3.2 (Linux)'",
        "uv run python scripts/fetch_age.py --platform linux-amd64 --dest \"$CQ_TEST_AGE_DIR\"",
        "echo '== Postgres de teste'",
        f"python3 -c \"{wait_pg}\"",
        "echo '== suíte rápida'",
        "exec uv run python ops/ci/ci_suite.py fast --junit-dir /tmp/cq-junit",
    ]
    return "\n".join(lines) + "\n"


def _phases(export_dir: Path, *, run_id: str, repo_root: Path) -> _Phases:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("id de execução inválido")
    if _is_inside(export_dir, repo_root):
        raise ValueError("o diretório exportado tem de ficar fora do repositório")
    source = str(export_dir.resolve())
    uv_dir = uv_host_dir(run_id)
    for path in (source, str(uv_dir)):
        if "," in path:
            raise ValueError("caminho com vírgula")
    python_image, uv_image, pg_image = _images(repo_root)

    prefix = f"{RESOURCE_PREFIX}{run_id}"
    network, pg, uv, python = prefix, f"{prefix}-pg", f"{prefix}-uv", f"{prefix}-py"

    setup = [
        [DOCKER, "network", "create", network],
        [DOCKER, "run", "-d", "--name", pg, "--network", network,
         "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "--tmpfs", "/var/lib/postgresql", "--shm-size=256m",
         pg_image, "-c", "max_connections=300"],
        [DOCKER, "create", "--name", uv, "--entrypoint", "/uv", uv_image],
        [DOCKER, "cp", f"{uv}:/uv", str(uv_dir / "uv")],
    ]
    steps = [
        [DOCKER, "run", "--name", python, "--network", network, "--user", CONTAINER_USER,
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         # Sem UV_CACHE_DIR: o uv usa o cache padrão ($HOME/.cache/uv), o mesmo que o
         # tests/integration/test_client_package.py encontra, porque ele limpa as variáveis UV_*. [I] Isso sozinho
         # não bastou na validação de 2026-11-03 (os 6 casos do pacote cliente continuaram falhando); ver o
         # aquecimento abaixo.
         "-e", "HOME=/tmp/cq-home", "-e", "UV_PYTHON_DOWNLOADS=never",
         "-e", f"CQ_TEST_PG_DSN=postgresql://postgres@{pg}:5432/postgres", "-e", f"CQ_TEST_AGE_DIR={_AGE_DIR}",
         "--mount", f"type=bind,source={source},target={_SRC},readonly",
         "--mount", f"type=bind,source={uv_dir},target={_UV_MOUNT},readonly",
         "--workdir", "/tmp",
         python_image, "sh", "-c", _container_script(pg)],
    ]
    cleanup = [
        [DOCKER, "rm", "-f", python, pg, uv],
        [DOCKER, "network", "rm", network],
    ]
    for command in setup + steps + cleanup:
        for arg in command:
            normalized = arg.replace("\\", "/")
            if any(forbidden in normalized for forbidden in FORBIDDEN_MOUNTS):
                raise ValueError("montagem proibida")
    return setup, steps, cleanup


def plan_commands(export_dir: Path, *, run_id: str, repo_root: Path) -> list[list[str]]:
    """Os argv, na ordem, da validação sobre `export_dir` (criar, rodar e remover os recursos
    `RESOURCE_PREFIX + run_id`); `repo_root` é a origem, para recusar `export_dir` dentro dela."""
    setup, steps, cleanup = _phases(Path(export_dir), run_id=run_id, repo_root=Path(repo_root))
    return setup + steps + cleanup


def _call(runner: Runner, argv: list[str], timeout: int) -> int:
    try:
        completed = runner(argv, env=dict(os.environ), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 1
    return int(getattr(completed, "returncode", 1))


def run(export_dir: Path, *, run_id: str | None = None, runner: Runner | None = None) -> int:
    """Executa o plano de `plan_commands`; devolve o código de saída."""
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        return _emit("invalid_export_dir")
    run_id = run_id or secrets.token_hex(4)
    try:
        setup, steps, cleanup = _phases(export_dir, run_id=run_id, repo_root=REPO_ROOT)
    except ValueError:
        return _emit("invalid_export_dir")
    if runner is None:
        if shutil.which(DOCKER) is None:
            return _emit("docker_missing")
        runner = subprocess.run

    uv_dir = uv_host_dir(run_id)
    try:
        uv_dir.mkdir()
    except OSError:
        return _emit("setup_failed")
    key = "ok"
    try:
        for command in setup:
            if _call(runner, command, SETUP_TIMEOUT_SECONDS) != 0:
                key = "setup_failed"
                break
        else:
            for command in steps:
                if _call(runner, command, SUITE_TIMEOUT_SECONDS) != 0:
                    key = "suite_failed"
                    break
    finally:
        for command in cleanup:
            try:
                _call(runner, command, CLEANUP_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - a limpeza segue com os outros recursos
                pass
        shutil.rmtree(uv_dir, ignore_errors=True)
    return _emit(key)


def _parse(argv: Sequence[str]) -> Path | None:
    args = list(argv)
    if len(args) == 1 and args[0].startswith("--export-dir="):
        value = args[0].split("=", 1)[1]
    elif len(args) == 2 and args[0] == "--export-dir":
        value = args[1]
    else:
        return None
    return Path(value) if value else None


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        export_dir = _parse(sys.argv[1:] if argv is None else argv)
        if export_dir is None:
            return _emit("invalid_arguments")
        return run(export_dir)
    except Exception:  # noqa: BLE001 - mensagem fixa, sem eco da exceção
        return _emit("internal")


if __name__ == "__main__":
    sys.exit(main())
