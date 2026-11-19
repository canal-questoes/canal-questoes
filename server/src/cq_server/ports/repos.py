"""Porta de obtenção de `repo@commit`: a árvore de um commit de um repositório de questões
(o núcleo não depende do `git`).

O núcleo não chama `git`: pede a árvore de um commit por esta porta, e o único módulo que chama o
`git` é `cq_server.adapters.git` (`GitRepoFetcher`). `ensure` é idempotente: a árvore
`<repos>/<repo_key>/<commit>/` aparece inteira ou não aparece.

Desfechos (valores, não exceções):
- `Fetched`: a árvore existe (`reused=True`: já existia, sem rede);
- `FetchRefused(rule)`: violou a política de endereços → classe `repository_refused`;
- `FetchNotFound(what)`: `repository` ou `commit` (definitivo);
- `FetchTemporaryFailure(error_type)`: repetível. O `timeout` esgotado na última tentativa é
  rejeitado pelo núcleo como `repository_refused`; o adaptador não muda.

Só tipos e o `Protocol`; nada de `subprocess`, `socket` nem HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Conjuntos fechados.
REFUSED_RULES = frozenset({"repo_syntax", "host_not_allowed", "address_not_global", "redirect", "max_bytes",
                           "max_files", "path_invalid", "symlink_absolute", "symlink_escape", "submodule"})
NOT_FOUND_WHAT = frozenset({"repository", "commit"})
# Além destes, o nome de um tipo de exceção (`type(exc).__name__`) quando o `ensure` levanta.
TEMPORARY_ERROR_TYPES = frozenset({"dns", "connect", "timeout", "http_5xx", "git_error",
                                   "repos_unavailable"})


@dataclass(frozen=True)
class Fetched:
    """A árvore do commit existe; `tree_oid`, `file_count` e `byte_count` são `None` quando já estava lá."""

    tree_oid: str | None
    file_count: int | None
    byte_count: int | None
    reused: bool


@dataclass(frozen=True)
class FetchRefused:
    """Recusado pela política de endereços; `rule` em `REFUSED_RULES`."""

    rule: str


@dataclass(frozen=True)
class FetchNotFound:
    """Repositório ou commit inexistente (definitivo); `what` em `NOT_FOUND_WHAT`."""

    what: str


@dataclass(frozen=True)
class FetchTemporaryFailure:
    """Falha repetível; `error_type` em `TEMPORARY_ERROR_TYPES` ou o nome de um tipo de exceção."""

    error_type: str


class RepoFetcher(Protocol):
    """Obtenção de `repo@commit`."""

    def ensure(self, repo: str, commit: str,
               ) -> Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure: ...
