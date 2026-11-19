"""Conferência das referências das questões do piloto contra o GitHub.
Passo do Pedro, com rede.

Uso:

    python ops/pilot/check_question_refs.py --manifest docs/operacao/piloto/questoes.lock.json

Para cada repositório do manifesto: clone raso do commit num temporário (`git clone --filter=blob:none
--no-checkout`, `git fetch --depth 1 origin <commit>`), confere `questions/<qid>/info.json` e o SHA-256 do
manifesto; apaga o temporário. Mensagens fixas, sem caminho nem saída do `git`.

Só biblioteca padrão. Importável sem efeito colateral. Nos testes, só com o `runner` falso (nenhuma rede).

Comportamento:
- o manifesto inteiro é validado antes de qualquer `git`: uma lista JSON não vazia de objetos com exatamente
  `{repo, commit, qid, field_type, info_json_sha256}`, todos texto; `repo` só `https://`, com host e sem credencial
  na URL; `commit` completo (40 hexadecimais); `qid` um caminho relativo simples (sem `..`, sem `/`
  no início); `info_json_sha256` com 64 hexadecimais. Qualquer falha → `invalid_arguments`, sem `git` nem temporário;
- um clone por repositório (na ordem da primeira aparição), num temporário próprio sob `tempfile.gettempdir()`, um
  `fetch` por commit distinto e a leitura de cada `info.json` por
  `git cat-file blob <commit>:questions/<qid>/info.json`, no clone;
- para na primeira falha; o temporário é apagado no `finally` em todo caso.

[I] Classificação das falhas (escolha da implementação; só as mensagens são fixas): o `clone` que falha, o tempo
esgotado e o `git` que não executa → `network_unavailable`; o `fetch` do commit que falha depois de um `clone` bem
sucedido → `missing_ref` (o remoto respondeu; o motivo provável é o commit ausente, e a saída do `git` não é lida para
não ser ecoada); a leitura do `info.json` que falha → `missing_ref`; os bytes com outro SHA-256 → `sha256_mismatch`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# Chave → (código de saída, mensagem). [I] Chaves, códigos e textos da implementação (com as
# mensagens fixas): tudo confere; referência ausente; SHA-256 divergente; rede indisponível; argumentos
# inválidos; interno.
CHECK_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Todas as referências das questões conferem."),
    "missing_ref": (66, "Uma referência do manifesto não foi encontrada no repositório."),
    "sha256_mismatch": (65, "O info.json de uma questão não confere com o SHA-256 do manifesto."),
    "network_unavailable": (69, "O repositório das questões não respondeu."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "internal": (1, "Erro interno da conferência."),
}

# A forma do `subprocess.run` (`runner(argv, *, cwd, env, capture_output, timeout, check)`).
Runner = Callable[..., Any]

MANIFEST_FIELDS = frozenset({"repo", "commit", "qid", "field_type", "info_json_sha256"})
# [I] Limite de cada chamada do `git` (minha escolha): o clone sem blobs e o fetch raso de um commit são rápidos.
GIT_TIMEOUT_SECONDS = 300
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_QID_PART_RE = re.compile(r"[A-Za-z0-9_.-]+")
_CLONE_DIR = "repo"


class _CheckFailure(Exception):
    """Falha com a chave de `CHECK_MESSAGES` (o texto da exceção nunca é exibido)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _emit(key: str) -> int:
    """Escreve a mensagem fixa de `key` (o sucesso no stdout, as falhas no stderr) e devolve o código."""
    code, text = CHECK_MESSAGES[key]
    target = sys.stdout if code == 0 else sys.stderr
    target.write(text + "\n")
    target.flush()
    return code


def _valid_repo(value: str) -> bool:
    """Só `https://`, com host, sem credencial, sem espaço, consulta nem fragmento."""
    if any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port  # porta inválida levanta ValueError
    except ValueError:
        return False
    return (parsed.scheme == "https" and bool(parsed.hostname) and "@" not in parsed.netloc
            and not parsed.query and not parsed.fragment)


def _valid_qid(value: str) -> bool:
    """Um caminho POSIX relativo simples: partes com letras, dígitos, `_`, `.` e `-`, sem `.` nem `..`."""
    parts = value.split("/")
    return bool(value) and all(part not in ("", ".", "..") and _QID_PART_RE.fullmatch(part) for part in parts)


def _load_manifest(manifest: Path) -> list[dict[str, str]]:
    """Lê e valida o manifesto inteiro (ver o docstring do módulo); `_CheckFailure("invalid_arguments")` se falhar."""
    try:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _CheckFailure("invalid_arguments") from None
    if not isinstance(data, list) or not data:
        raise _CheckFailure("invalid_arguments")
    entries: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or set(item) != MANIFEST_FIELDS:
            raise _CheckFailure("invalid_arguments")
        if not all(isinstance(item[name], str) for name in MANIFEST_FIELDS):
            raise _CheckFailure("invalid_arguments")
        if not (_valid_repo(item["repo"]) and _COMMIT_RE.fullmatch(item["commit"])
                and _valid_qid(item["qid"]) and _SHA256_RE.fullmatch(item["info_json_sha256"])
                and item["field_type"].strip()):
            raise _CheckFailure("invalid_arguments")
        entries.append(dict(item))
    return entries


def _git_env() -> dict[str, str]:
    """O ambiente do `git`: o do processo, sem prompt de credencial no terminal."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(runner: Runner, argv: list[str], cwd: Path, env: dict[str, str], *, failure: str) -> Any:
    """Roda o `git`; devolve o resultado com código 0 ou levanta `_CheckFailure(failure)` (a saída nunca sai)."""
    try:
        completed = runner(argv, cwd=str(cwd), env=env, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
                           check=False)
    except (subprocess.TimeoutExpired, OSError):
        raise _CheckFailure("network_unavailable") from None
    if getattr(completed, "returncode", 1) != 0:
        raise _CheckFailure(failure)
    return completed


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise _CheckFailure("internal")


def _make_writable_and_retry(function: Callable[..., Any], path: str, _exc: Any) -> None:
    """No Windows, o `git` grava os pacotes só para leitura: libera a escrita e tenta de novo."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover - Python anterior ao 3.12
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _check_repository(repo: str, entries: list[dict[str, str]], *, git: str, runner: Runner, workdir: Path,
                      env: dict[str, str]) -> None:
    """Clona `repo` em `workdir`, busca cada commit e confere cada `info.json`; levanta `_CheckFailure` na falha."""
    clone = workdir / _CLONE_DIR
    _run(runner, [git, "clone", "--filter=blob:none", "--no-checkout", repo, str(clone)], workdir, env,
         failure="network_unavailable")
    fetched: set[str] = set()
    for entry in entries:
        commit = entry["commit"]
        if commit not in fetched:
            _run(runner, [git, "fetch", "--depth", "1", "origin", commit], clone, env, failure="missing_ref")
            fetched.add(commit)
        spec = f"{commit}:questions/{entry['qid']}/info.json"
        completed = _run(runner, [git, "cat-file", "blob", spec], clone, env, failure="missing_ref")
        digest = hashlib.sha256(_as_bytes(completed.stdout)).hexdigest()
        if digest != entry["info_json_sha256"].lower():
            raise _CheckFailure("sha256_mismatch")


def check(manifest: Path, *, git: str = "git", runner: Runner = subprocess.run) -> int:
    """Confere cada entrada do manifesto; devolve o código de `CHECK_MESSAGES`."""
    try:
        entries = _load_manifest(manifest)
        by_repo: dict[str, list[dict[str, str]]] = {}
        for entry in entries:
            by_repo.setdefault(entry["repo"], []).append(entry)
        env = _git_env()
        for repo, repo_entries in by_repo.items():
            workdir = Path(tempfile.mkdtemp(prefix="cq-refs-"))
            try:
                _check_repository(repo, repo_entries, git=git, runner=runner, workdir=workdir, env=env)
            finally:
                _remove_tree(workdir)
    except _CheckFailure as failure:
        return _emit(failure.key)
    except Exception:  # noqa: BLE001 - qualquer outra falha sai pela mensagem fixa, sem detalhe
        return _emit("internal")
    return _emit("ok")


class _ArgumentError(Exception):
    """Linha de comando inválida (sem o `usage` do `argparse` nem eco da entrada)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # noqa: ARG002 - a mensagem do argparse ecoa a entrada; descartada
        raise _ArgumentError()

    def exit(self, status: int = 0, message: str | None = None) -> Any:  # noqa: ARG002
        raise _ArgumentError()


def _parser() -> _Parser:
    parser = _Parser(prog="check_question_refs.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        return _emit("invalid_arguments")
    if not args.manifest:
        return _emit("invalid_arguments")
    return check(Path(args.manifest))


if __name__ == "__main__":
    sys.exit(main())
