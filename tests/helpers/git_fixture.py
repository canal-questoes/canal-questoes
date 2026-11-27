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


# --------------------------------------------------------------------------------------------------
# Repositórios sintéticos de um banco de questões (a leitura de um commit de um git local pelo cliente)
# --------------------------------------------------------------------------------------------------
#
# Criados com o mesmo ambiente isolado de `git_env` (sem nenhuma `GIT_*` do desenvolvedor, as configurações
# global e do sistema no nulo, autor, committer e datas fixos) e com `-c core.autocrlf=false` e
# `-c core.hooksPath=` em todo comando: um `commit.gpgsign`, um gancho global ou o `autocrlf` do Git for Windows
# mudariam ou travariam o commit. Os bytes dados entram no commit como estão.
#
# - `make_bank_repo(tmp_path, files, *, name="banco", object_format="sha1") -> Path`: `git init` (ramo
#   `main`, com o formato de objeto dado: `sha1` dá commits de 40 hex, `sha256`, de 64), grava `files`
#   (caminho POSIX relativo → bytes; uma subpasta é só um prefixo, como `sub/banco/infoCourse.json`) e faz o
#   primeiro commit;
# - `commit_files(repo, files, message) -> str`: grava, faz `git add -A` e o commit; devolve o hash;
# - `add_symlink(repo, path, target) -> str` e `add_submodule(repo, path, commit) -> str`: um link simbólico
#   (modo 120000) ou um submódulo (modo 160000) só no índice, sem nada na cópia de trabalho (funciona no
#   Windows sem privilégio de link), e o commit do índice; devolvem o hash. Um `commit_files` depois deles
#   apagaria a entrada (o `git add -A` vê a falta na cópia de trabalho): faça-os por último;
# - `rev_parse(repo, rev) -> str`, `create_branch(repo, name, rev="HEAD")`, `create_tag(repo, name, rev="HEAD",
#   *, annotated=False)`, `blob_oid(repo, rev, path) -> str`;
# - `refresh_worktree(repo)`: regrava a cópia de trabalho a partir do índice, com os atributos e filtros (com
#   `* text eol=crlf` no `.gitattributes`, a cópia ganha CRLF, e o objeto continua com LF);
# - `install_sentinel_filter(repo, sentinel) -> str`: configura no `.git/config` do repositório um filtro
#   (`filter.sentinel.clean` e `smudge`) e um `diff.sentinel.textconv` que, se algum dia rodarem,
#   criam o arquivo `sentinel`; o filtro é instalado **depois** dos commits, para não rodar na criação; devolve a
#   linha que os liga no `.gitattributes` (`SENTINEL_FILTER_ATTRIBUTES`, a pôr nos `files` do commit);
# - `add_replace(repo, original, data) -> str`: grava um blob com `data` e um `refs/replace/<original>` que o
#   troca; devolve o oid do substituto (o `git` sem `GIT_NO_REPLACE_OBJECTS` leria o substituto);
# - `isolate_git_home(monkeypatch, tmp_path) -> Path`: o executor do produto remove as `GIT_*` herdadas, então
#   o teste aponta `HOME`, `USERPROFILE` e `XDG_CONFIG_HOME` para uma pasta vazia do `tmp_path` (sem a
#   configuração global do desenvolvedor);
# - `require_git()`: o bloqueio de ambiente nomeado; sem o `git` no `PATH`, `pytest.fail` com a mensagem fixa
#   `GIT_MISSING_MESSAGE`;
# - os dublês do executor (o protocolo `run(args, *, repo, input, timeout) -> RunResult`), só para o que o `git`
#   real não reproduz: `MissingGitRunner` (levanta `GitUnavailable`), `FailingGitRunner(returncode, stderr)` (a
#   falha de processo) e `RecordingGitRunner(inner=None)` (registra cada chamada; sem `inner`, toda chamada é
#   um erro do teste).

SENTINEL_FILTER_ATTRIBUTES = "* filter=sentinel diff=sentinel\n"
GIT_MISSING_MESSAGE = "ambiente: git ausente no PATH"
_BANK_GIT_OPTIONS = ("-c", "core.autocrlf=false", "-c", "core.hooksPath=")


def _bank_git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    repo = Path(repo)
    env = git_env(repo.parent)
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
    extra = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    completed = subprocess.run(["git", *_BANK_GIT_OPTIONS, *args], cwd=repo, env=env, capture_output=True,
                               timeout=GIT_TIMEOUT_SECONDS, check=False, **extra)
    if completed.returncode != 0:
        raise AssertionError(f"git {args[0]} falhou na fixture (código {completed.returncode})")
    return completed.stdout


def make_bank_repo(tmp_path: Path, files: Mapping[str, bytes], *, name: str = "banco",
                   object_format: str = "sha1") -> Path:
    """Cria `tmp_path/<name>` como repositório com um commit de `files` (ver o comentário da seção)."""
    if object_format not in ("sha1", "sha256"):
        raise ValueError("formato de objeto desconhecido")
    repo = Path(tmp_path) / name
    if repo.exists():
        raise FileExistsError(f"o repositório de fixture já existe: {name}")
    repo.mkdir(parents=True)
    _bank_git(repo, "init", "-q", "-b", FIXTURE_BRANCH, f"--object-format={object_format}")
    commit_files(repo, files, FIXTURE_MESSAGE)
    return repo


def commit_files(repo: Path, files: Mapping[str, bytes], message: str = FIXTURE_MESSAGE) -> str:
    """Grava `files`, `git add -A` e o commit (aceita commit vazio); devolve o hash do novo `HEAD`."""
    for path, data in files.items():
        _write(repo, path, data)
    _bank_git(repo, "add", "-A")
    return _commit_index(repo, message)


def _commit_index(repo: Path, message: str) -> str:
    _bank_git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return rev_parse(repo, "HEAD")


def _index_entry(repo: Path, mode: str, oid: str, path: str, message: str) -> str:
    _bank_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")
    return _commit_index(repo, message)


def add_symlink(repo: Path, path: str, target: str, message: str = "link") -> str:
    """Um link simbólico `path` → `target` (modo 120000) só no índice, e o commit; devolve o hash."""
    oid = _bank_git(repo, "hash-object", "-w", "--stdin", input=target.encode("utf-8")).decode("ascii").strip()
    return _index_entry(repo, "120000", oid, path, message)


def add_submodule(repo: Path, path: str, commit: str, message: str = "submodulo") -> str:
    """Um submódulo `path` no commit `commit` (modo 160000) só no índice, e o commit; devolve o hash."""
    return _index_entry(repo, "160000", commit, path, message)


def rev_parse(repo: Path, rev: str) -> str:
    """O oid de `rev` (`git rev-parse --verify`)."""
    return _bank_git(repo, "rev-parse", "--verify", "--quiet", rev).decode("ascii").strip()


def create_branch(repo: Path, name: str, rev: str = "HEAD") -> None:
    """Um ramo `name` em `rev`, sem trocar o ramo atual."""
    _bank_git(repo, "branch", name, rev)


def create_tag(repo: Path, name: str, rev: str = "HEAD", *, annotated: bool = False) -> None:
    """Uma etiqueta `name` em `rev` (anotada, com mensagem fixa, se `annotated`)."""
    if annotated:
        _bank_git(repo, "tag", "-a", "-m", FIXTURE_MESSAGE, name, rev)
    else:
        _bank_git(repo, "tag", name, rev)


def blob_oid(repo: Path, rev: str, path: str) -> str:
    """O oid do objeto de `path` em `rev`."""
    return rev_parse(repo, f"{rev}:{path}")


def refresh_worktree(repo: Path) -> None:
    """Regrava a cópia de trabalho a partir do índice, com os atributos e filtros do repositório (apaga antes os
    arquivos rastreados, porque o `checkout-index` não regrava o que o índice dá por atualizado)."""
    listed = _bank_git(repo, "ls-files", "-z", "--stage").split(b"\0")
    for entry in listed:
        if not entry:
            continue
        mode, _, path = entry.partition(b"\t")
        if mode.startswith(b"100"):
            target = Path(repo).joinpath(*PurePosixPath(path.decode("utf-8")).parts)
            if target.is_file():
                target.unlink()
    _bank_git(repo, "checkout-index", "-f", "-a", "-q")


def install_sentinel_filter(repo: Path, sentinel: Path) -> str:
    """Configura o filtro e o `textconv` sentinela no repositório (ver o comentário da seção)."""
    import sys

    python = Path(sys.executable).as_posix()
    touch = "import pathlib; pathlib.Path(" + repr(Path(sentinel).as_posix()) + ").touch()"
    echo = touch + "; import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    commands = {
        "filter.sentinel.clean": echo,
        "filter.sentinel.smudge": echo,
        "diff.sentinel.textconv": touch,
    }
    for key, code in commands.items():
        _bank_git(repo, "config", key, f"'{python}' -c \"{code}\"")
    return SENTINEL_FILTER_ATTRIBUTES


def add_replace(repo: Path, original: str, data: bytes) -> str:
    """Um blob com `data` e o `refs/replace/<original>` que o troca; devolve o oid do substituto."""
    replacement = _bank_git(repo, "hash-object", "-w", "--stdin", input=data).decode("ascii").strip()
    _bank_git(repo, "replace", "-f", original, replacement)
    return replacement


def isolate_git_home(monkeypatch, tmp_path: Path) -> Path:
    """Aponta `HOME`, `USERPROFILE` e `XDG_CONFIG_HOME` para uma pasta vazia do `tmp_path`; devolve a pasta."""
    home = Path(tmp_path) / "home-git"
    home.mkdir(parents=True, exist_ok=True)
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(key, str(home))
    return home


def require_git() -> None:
    """Sem o `git` no `PATH`, o teste falha com a mensagem fixa de ambiente (não é vermelho do produto)."""
    import shutil

    import pytest

    if shutil.which("git") is None:
        pytest.fail(GIT_MISSING_MESSAGE)


class MissingGitRunner:
    """O executor sem o `git`: toda chamada levanta `GitUnavailable` (do produto)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, repo: str, input: bytes | None, timeout: float | None):
        from cq_client.stack_compose import GitUnavailable

        self.calls.append(tuple(args))
        raise GitUnavailable()


class FailingGitRunner:
    """O executor cujo processo falha: toda chamada devolve `RunResult(returncode, b"", stderr)`."""

    def __init__(self, returncode: int = 128, stderr: bytes = b"fatal: falha simulada\n") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, *, repo: str, input: bytes | None, timeout: float | None):
        from cq_client.stack import RunResult

        self.calls.append(tuple(args))
        return RunResult(self.returncode, b"", self.stderr)


class RecordingGitRunner:
    """Registra cada chamada (`calls`: os argumentos, o `repo`, se houve entrada e o prazo) e a repassa ao
    `inner`; sem `inner`, toda chamada é um erro do teste."""

    def __init__(self, inner=None) -> None:
        self.inner = inner
        self.calls: list[tuple[tuple[str, ...], str, bool, float | None]] = []

    def run(self, args, *, repo: str, input: bytes | None, timeout: float | None):
        self.calls.append((tuple(args), repo, input is not None, timeout))
        if self.inner is None:
            raise AssertionError("o executor do git não devia ser chamado")
        return self.inner.run(args, repo=repo, input=input, timeout=timeout)
