"""Repositórios git temporários para os testes da exportação e do pacote.


Os testes da exportação e do pacote criam um repositório de fixture no `tmp_path` e **nunca** rodam a
exportação nem o pacote contra a árvore real. Nada aqui toca a árvore real: todo comando roda com
`cwd=<repositório da fixture>` e com `GIT_CEILING_DIRECTORIES` no diretório pai dela.

- `git_env(tmp_path) -> dict[str, str]`: o ambiente do `git` dos testes, a partir do `clean_env` de
  `tests/helpers/ops.py` (sem `CQ_*`, `COMPOSE_*`, `DOCKER_HOST`, `DOCKER_CONTEXT`) e **sem nenhuma `GIT_*`
  do desenvolvedor**; `GIT_CONFIG_GLOBAL` e `GIT_CONFIG_SYSTEM` no nulo do sistema (`os.devnull`;
  `GIT_CONFIG_NOSYSTEM=1` também), autor e committer fixos (`FIXTURE_AUTHOR_*`, `FIXTURE_DATE`),
  `HOME` no `tmp_path`, sem prompt de terminal. Sem a configuração do sistema, o `core.autocrlf` do Git
  for Windows não vale, e os bytes dados ficam como estão.
- `make_repo(tmp_path, files, *, name="origem") -> Path`: `git init` (ramo `main`), grava `files`
  (caminho POSIX relativo → bytes), `git add -A` e `git commit`.
- `modify(repo, path, data)`: reescreve um arquivo (rastreado) sem commit;
  `add_untracked(repo, path, data)`: cria um arquivo não rastreado;
  `add_ignored(repo, path, data)`: cria um arquivo e o ignora por `.git/info/exclude` (**[I]**: pelo
  `exclude`, para não criar um `.gitignore` não rastreado);
  `commit_all(repo, message)`: `git add -A` e `git commit` (conveniência).
- `fingerprint(repo) -> tuple[str, ...]`: o estado da origem no contrato de `origin_fingerprint`
  : a saída de `git status --porcelain=v2`, a de `git rev-parse HEAD`, a de `git
  for-each-ref` e o SHA-256 de `.git/index`, nessa ordem. **[I]** O `status` roda com a opção global
  `--no-optional-locks` (não muda a saída): sem ela, o `git status` pode regravar o índice (atualização
  oportunista do `stat`) e mudar o SHA-256 que ele mesmo confere.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from tests.helpers.ops import clean_env

FIXTURE_AUTHOR_NAME = "Autor da Fixture"
FIXTURE_AUTHOR_EMAIL = "autor@fixture.test"
# 2026-01-01T00:00:00Z, no formato bruto do git (`<segundos> <fuso>`).
FIXTURE_DATE = "1767225600 +0000"
FIXTURE_BRANCH = "main"
FIXTURE_MESSAGE = "fixture"
GIT_TIMEOUT_SECONDS = 60


def git_env(tmp_path: Path) -> dict[str, str]:
    """O ambiente do `git` dos testes (ver o docstring do módulo)."""
    env = {name: value for name, value in clean_env().items() if not name.upper().startswith("GIT_")}
    home = str(Path(tmp_path))
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": FIXTURE_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": FIXTURE_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": FIXTURE_DATE,
        "GIT_COMMITTER_NAME": FIXTURE_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": FIXTURE_AUTHOR_EMAIL,
        "GIT_COMMITTER_DATE": FIXTURE_DATE,
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": home,
    })
    return env


def _git(repo: Path, *args: str) -> bytes:
    repo = Path(repo)
    env = git_env(repo.parent)
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
    completed = subprocess.run(["git", *args], cwd=repo, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {args[0]} falhou na fixture (código {completed.returncode})")
    return completed.stdout


def _write(repo: Path, path: str, data: bytes) -> Path:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] == (".git",):
        raise ValueError(f"caminho de fixture inválido: {path}")
    target = Path(repo).joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def make_repo(tmp_path: Path, files: Mapping[str, bytes], *, name: str = "origem") -> Path:
    """Cria `tmp_path/<name>` como repositório com um commit de `files` (ver o docstring do módulo)."""
    repo = Path(tmp_path) / name
    if repo.exists():
        raise FileExistsError(f"o repositório de fixture já existe: {name}")
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", FIXTURE_BRANCH)
    for path, data in files.items():
        _write(repo, path, data)
    commit_all(repo, FIXTURE_MESSAGE)
    return repo


def commit_all(repo: Path, message: str = FIXTURE_MESSAGE) -> str:
    """`git add -A` e `git commit` (aceita commit vazio); devolve o SHA do novo `HEAD`."""
    _git(repo, "add", "-A")
    _git(repo, "-c", "core.hooksPath=", "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def modify(repo: Path, path: str, data: bytes) -> Path:
    """Reescreve um arquivo rastreado, sem commit."""
    target = Path(repo).joinpath(*PurePosixPath(path).parts)
    if not target.is_file():
        raise FileNotFoundError(f"arquivo da fixture ausente: {path}")
    return _write(repo, path, data)


def add_untracked(repo: Path, path: str, data: bytes) -> Path:
    """Cria um arquivo não rastreado (não ignorado)."""
    target = Path(repo).joinpath(*PurePosixPath(path).parts)
    if target.exists():
        raise FileExistsError(f"arquivo da fixture já existe: {path}")
    return _write(repo, path, data)


def add_ignored(repo: Path, path: str, data: bytes) -> Path:
    """Cria um arquivo e o ignora pelo `.git/info/exclude`."""
    target = add_untracked(repo, path, data)
    exclude = Path(repo) / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "ab") as handle:
        handle.write(b"/" + PurePosixPath(path).as_posix().encode("utf-8") + b"\n")
    return target


def fingerprint(repo: Path) -> tuple[str, ...]:
    """O estado da origem (ver o docstring do módulo)."""
    status = _git(repo, "--no-optional-locks", "status", "--porcelain=v2").decode("utf-8")
    head = _git(repo, "rev-parse", "HEAD").decode("ascii")
    refs = _git(repo, "for-each-ref").decode("utf-8")
    index = Path(repo) / ".git" / "index"
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else ""
    return status, head, refs, index_sha
