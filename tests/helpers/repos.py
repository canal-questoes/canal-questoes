"""Obtenção falsa de `repo@commit`, com injeção de falhas nos testes.


`FakeRepoFetcher` cumpre a porta `cq_server.ports.repos.RepoFetcher` (devolve os tipos reais da porta)
sem rede nem `git`: mapeia `(repo, commit)` para uma árvore local de `tests/fixtures/repos/` e a copia
para a raiz temporária no leiaute do motor, `<raiz>/<repo_key(repo)>/<commit>/` (a mesma raiz que o
motor falso, `tests/helpers/engine.py`, lê). Nenhum teste do núcleo chama `git`.

- **Leiaute e atomicidade**: a árvore é copiada para `<raiz>/.staging/<uuid4>/tree` e movida
  por `os.rename` para o destino; ela aparece inteira ou não aparece, e a sobra do staging sai no
  `finally`.
- **Reuso** (idempotência): se `<raiz>/<repo_key>/<commit>/` já é um diretório, `Fetched(reused=True)`
  sem copiar nada (os três números `None`, como no adaptador).
- **Árvore por par:** `add(repo, commit, tree)` escolhe a árvore; um par sem `add` usa a
  `default_tree` (padrão `course`, com uma questão por preset do motor falso); com `default_tree=None`,
  o par desconhecido dá `FetchNotFound("repository")`.
- **Desfechos programados:** `program(repo, commit, result, times=None)`, com `result` um
  `FetchRefused`, `FetchNotFound`, `FetchTemporaryFailure`, `Fetched` ou uma exceção (levantada, como
  um adaptador que falha); `times=None` vale até `program(…, None)`, `reset()` ou outro `program`.
- **Perda do conteúdo**: `delete_tree(repo, commit)` apaga a árvore; o motor falso passa a
  responder 404, e a nova obtenção a recria.
- **Contagem**: `calls()`/`count(repo, commit)` registram cada `ensure`, com o desfecho.
- **Estrito** (paridade exata com a política real, `questionrefs.validate_repo` com a lista de hosts do
  `RepoSettings`): `ensure` com um `repo` que a
  política recusa vai para `protocol_errors()` com o mesmo motivo (o código de `validate_repo`), na
  ordem `repo_length` (mais de 2048 caracteres) · `repo_syntax` (fora de `questionrefs.REPO_URL`, ou
  host que é IP literal, que nenhuma lista válida contém) · `repo_dot_segment` (algum segmento do
  caminho é `.` ou `..`) · `host_not_allowed` (host fora de `allowed_hosts`, comparação exata; a lista
  vazia recusa tudo); e `commit` fora de `questionrefs.COMMIT` também (o núcleo valida antes de obter).
  `allowed_hosts` tem o padrão `DEFAULT_ALLOWED_HOSTS` = {`repos.cq.test`}, o mesmo do
  `TEST_REPO_SETTINGS` (`tests/helpers/calls.py`), e muda pelo construtor ou por `reset(…,
  allowed_hosts=…)`. O dublê reimplementa a regra (não chama `validate_repo`), para acusar mesmo que o
  núcleo erre.

Seguro para threads (o worker chama `ensure` na própria thread).
"""

from __future__ import annotations

import hashlib
import os
import re
import pathlib
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure
from cq_server.questionrefs import COMMIT, REPO_MAX_LENGTH, REPO_URL, repo_key

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "repos"
DEFAULT_TREE = "course"
STAGING = ".staging"
# O mesmo do `TEST_REPO_SETTINGS` (`tests/helpers/calls.py`): a lista de hosts dos testes do núcleo.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({"repos.cq.test"})
# Host que é IP literal: o último rótulo é número (decimal, ou `0x` hexadecimal), como o "ends in a
# number" do padrão de URL (WHATWG), que o curl também lê como endereço (`1.2.3.4`, `127.1`, `a.0x7f`).
_IP_LAST_LABEL = re.compile(r"\A(?:[0-9]+|0x[0-9a-f]*)\Z")

FetchResult = Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure
_RESULT_TYPES = (Fetched, FetchRefused, FetchNotFound, FetchTemporaryFailure)


@dataclass(frozen=True)
class FetchCall:
    """Um `ensure` recebido: o par, o desfecho (`Fetched`, `FetchRefused`, `FetchNotFound`,
    `FetchTemporaryFailure` ou `raised:<tipo>`), se veio da programação e, no `Fetched`, o `reused`."""

    seq: int
    repo: str
    commit: str
    outcome: str
    programmed: bool
    reused: bool | None


@dataclass
class _Programmed:
    result: Any
    times: int | None


def tree_source(tree: str | os.PathLike[str]) -> pathlib.Path:
    """O diretório de uma árvore: nome em `tests/fixtures/repos/` ou caminho."""
    path = pathlib.Path(tree)
    if not path.is_absolute() and len(path.parts) == 1:
        path = FIXTURES / path
    if not path.is_dir():
        raise ValueError(f"árvore de teste inexistente: {tree}")
    return path


def policy_problem(repo: str, allowed_hosts: frozenset[str]) -> str | None:
    """O código com que a política real (`questionrefs.validate_repo`) recusa `repo` com a lista
    `allowed_hosts` (`None` = aceito), reimplementado aqui: `repo_length` · `repo_syntax` ·
    `repo_dot_segment` · `host_not_allowed`, nessa ordem."""
    if not isinstance(repo, str):
        return "repo_syntax"
    if len(repo) > REPO_MAX_LENGTH:
        return "repo_length"
    match = REPO_URL.fullmatch(repo)
    if match is None or _IP_LAST_LABEL.fullmatch(match.group("host").rsplit(".", 1)[-1]):
        return "repo_syntax"
    if any(segment in (".", "..") for segment in match.group("path").split("/")):
        return "repo_dot_segment"
    if match.group("host") not in allowed_hosts:
        return "host_not_allowed"
    return None


def _tree_oid(repo: str, commit: str) -> str:
    """Um `tree_oid` determinístico com o tamanho do formato do commit (SHA-1 ou SHA-256)."""
    digest = hashlib.sha256(f"{repo}\0{commit}".encode()).hexdigest()
    return digest[: len(commit)]


class FakeRepoFetcher:
    """Obtenção falsa (ver o docstring do módulo). `root` é a raiz da área de repositórios."""

    def __init__(self, root: str | os.PathLike[str], *,
                 default_tree: str | os.PathLike[str] | None = DEFAULT_TREE,
                 allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS) -> None:
        self._lock = threading.RLock()
        self.root = pathlib.Path(root)
        self._default_tree = default_tree
        self.allowed_hosts = frozenset(allowed_hosts)
        self._init_state()

    def _init_state(self) -> None:
        self._trees: dict[tuple[str, str], pathlib.Path] = {}
        self._programmed: dict[tuple[str, str], _Programmed] = {}
        self._calls: list[FetchCall] = []
        self._problems: list[str] = []
        self._seq = 0
        self._hooks: list[Callable[[str, str], None]] = []

    def reset(self, root: str | os.PathLike[str] | None = None, *,
              allowed_hosts: frozenset[str] | None = None) -> None:
        """Esquece árvores, programação, chamadas e desvios (a área em disco fica como está);
        com `root`, passa a usar outra raiz; com `allowed_hosts`, outra lista de hosts."""
        with self._lock:
            if root is not None:
                self.root = pathlib.Path(root)
            if allowed_hosts is not None:
                self.allowed_hosts = frozenset(allowed_hosts)
            self._init_state()

    # -- programação ---------------------------------------------------------------------------

    def add(self, repo: str, commit: str, tree: str | os.PathLike[str] = DEFAULT_TREE) -> None:
        """Associa o par à árvore `tree` (nome em `tests/fixtures/repos/` ou caminho)."""
        source = tree_source(tree)
        with self._lock:
            self._trees[(repo, commit)] = source

    def program(self, repo: str, commit: str, result: Any, *, times: int | None = None) -> None:
        """O desfecho das próximas `times` obtenções do par (`None` = até nova programação);
        `result=None` volta ao comportamento normal (cópia da árvore ou reuso)."""
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
            raise ValueError("times deve ser um inteiro positivo ou None")
        if result is not None and not isinstance(result, (*_RESULT_TYPES, BaseException)):
            raise TypeError("program: use um desfecho da porta (Fetched, FetchRefused, FetchNotFound, "
                            "FetchTemporaryFailure), uma exceção ou None")
        with self._lock:
            if result is None:
                self._programmed.pop((repo, commit), None)
            else:
                self._programmed[(repo, commit)] = _Programmed(result=result, times=times)

    def on_ensure(self, hook: Callable[[str, str], None] | None) -> None:
        """Gancho chamado no início de cada `ensure(repo, commit)` (ex.: para simular concorrência);
        `None` remove todos."""
        with self._lock:
            if hook is None:
                self._hooks.clear()
            else:
                self._hooks.append(hook)

    # -- a porta -------------------------------------------------------------------------------

    def tree_path(self, repo: str, commit: str) -> pathlib.Path:
        """`<raiz>/<repo_key(repo)>/<commit>`, onde o motor procura o curso."""
        return self.root / repo_key(repo) / commit

    def ensure(self, repo: str, commit: str) -> FetchResult:
        """`RepoFetcher.ensure`: reuso, cópia atômica ou o desfecho programado."""
        with self._lock:
            hooks = list(self._hooks)
        for hook in hooks:
            hook(repo, commit)
        with self._lock:
            self._seq += 1
            seq = self._seq
            code = policy_problem(repo, self.allowed_hosts)
            if code is not None:
                self._problems.append(f"ensure: repo recusado pela política (questionrefs.validate_repo: {code})")
            if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
                self._problems.append("ensure: commit fora da forma validada (questionrefs.COMMIT)")
            key = (repo, commit)
            programmed = self._programmed.get(key)
            if programmed is not None:
                if programmed.times is not None:
                    programmed.times -= 1
                    if programmed.times == 0:
                        del self._programmed[key]
                result = programmed.result
                if isinstance(result, BaseException):
                    self._calls.append(FetchCall(seq, repo, commit, f"raised:{type(result).__name__}", True,
                                                 None))
                    raise result
                self._record(seq, repo, commit, result, programmed=True)
                return result
            result = self._materialize(repo, commit)
            self._record(seq, repo, commit, result, programmed=False)
            return result

    def _materialize(self, repo: str, commit: str) -> FetchResult:
        final = self.tree_path(repo, commit)
        if final.is_dir():
            return Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
        source = self._trees.get((repo, commit))
        if source is None:
            if self._default_tree is None:
                return FetchNotFound("repository")
            source = tree_source(self._default_tree)
        staging = self.root / STAGING / str(uuid.uuid4())
        try:
            tree = staging / "tree"
            shutil.copytree(source, tree)
            files = [p for p in tree.rglob("*") if p.is_file()]
            byte_count = sum(p.stat().st_size for p in files)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tree, final)
            return Fetched(tree_oid=_tree_oid(repo, commit), file_count=len(files), byte_count=byte_count,
                           reused=False)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _record(self, seq: int, repo: str, commit: str, result: Any, *, programmed: bool) -> None:
        reused = result.reused if isinstance(result, Fetched) else None
        self._calls.append(FetchCall(seq, repo, commit, type(result).__name__, programmed, reused))

    # -- perda do conteúdo e observação --------------------------------------------------------

    def delete_tree(self, repo: str, commit: str) -> bool:
        """Apaga `<raiz>/<repo_key>/<commit>`; `True` se existia."""
        with self._lock:
            final = self.tree_path(repo, commit)
            if not final.exists():
                return False
            shutil.rmtree(final)
            return True

    def calls(self, repo: str | None = None, commit: str | None = None) -> list[FetchCall]:
        with self._lock:
            return [c for c in self._calls
                    if (repo is None or c.repo == repo) and (commit is None or c.commit == commit)]

    def count(self, repo: str, commit: str) -> int:
        """Quantos `ensure` o par recebeu."""
        return len(self.calls(repo, commit))

    def protocol_errors(self) -> list[str]:
        with self._lock:
            return list(self._problems)


__all__ = ["DEFAULT_ALLOWED_HOSTS", "DEFAULT_TREE", "FIXTURES", "FakeRepoFetcher", "FetchCall", "policy_problem",
           "tree_source"]
