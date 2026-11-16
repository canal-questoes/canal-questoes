"""Pacote de implantação para a VM.


Uso:

    python ops/deploy/make_bundle.py --out <arquivo .tar.gz fora do repositório>

Gera `cq-bundle-<commit curto>.tar.gz` com os arquivos rastreados que casam
`ops/export/allowlist.txt`, com os bytes do commit (recusa alteração pendente nesses
arquivos), mais `SOURCE_COMMIT` (40 hex + `\\n`) e `BUNDLE_SHA256SUMS` (formato `sha256sum`,
todos os arquivos menos ele mesmo); passa pela mesma varredura da exportação. Sem `.git/`, sem
`.env*` (menos `.env.example`) e sem nenhum arquivo fora da lista. Nada é escrito na origem.

`source="worktree"` (só o teste de sistema): arquivos rastreados e não rastreados não ignorados que
casam a lista, com os bytes da árvore, e `SOURCE_COMMIT` = `<40 hex>+worktree`. O `main` só usa `head`.

Só biblioteca padrão; carrega `ops/export/repo_scan.py` pelo caminho relativo a este arquivo
(`load_repo_scan`). Mensagens: as de `ops/export/export_public.py` (`EXPORT_MESSAGES`), carregado
também pelo caminho (`load_export_public`). Importável sem efeito colateral.

**[I]** Escolhas da implementação, não fixadas de antemão:
- `out` é o caminho do arquivo `.tar.gz` (ex.: `--out %TEMP%\\cq-bundle.tar.gz`); se `out`
  for um diretório existente, o arquivo vai para dentro dele com o nome `cq-bundle-<commit curto>.tar.gz`
  (`BUNDLE_NAME_TEMPLATE`). Saída dentro da origem, diretório pai inexistente ou `out` que existe e não é arquivo
  comum → `out_invalid` (64); um arquivo comum já existente é substituído (só depois do sucesso);
- as listas (`allowlist.txt`, `fictitious.txt`, `binary-allowlist.txt`) são lidas de `<repo>/ops/export/` na
  mesma origem dos arquivos: no modo `head`, do commit; no `worktree`, da árvore. A `allowlist.txt` ausente ou
  inválida é erro interno; as outras duas ausentes valem como vazias. Linha da `fictitious.txt` fora do formato
  `<tipo>\\t<valor>` (ou de tipo desconhecido) também é erro interno, sem eco;
- a alteração pendente (modo `head`) é a do `git status --porcelain=v1 -z --untracked-files=no --no-renames`
  (com `--no-optional-locks`, que não regrava o índice): qualquer entrada, no índice ou na árvore, cujo caminho
  casa a lista. Não rastreado nunca é pendente: no modo `head` ele só fica de fora;
- os arquivos são materializados num diretório temporário ao lado de `out` (fora da origem), varridos lá
  (`repo_scan.scan`) e só então empacotados; o `.tar.gz` é escrito num temporário no mesmo diretório e renomeado
  para `out` só sem achado; os temporários saem no `finally`;
- o `.tar.gz` é determinístico: membros em ordem, na raiz do arquivo, só arquivos comuns (sem entradas de
  diretório), dono 0:0 sem nome, modo 0644 (0755 para o modo `100755` do git), `mtime` = a data do commit e o
  `mtime` do gzip zerado;
- as mensagens fixas: o sucesso no stdout; recusas, achados (`arquivo:linha: tipo`, ordenados) e erros no stderr;
  o erro inesperado vira `internal` (1), sem o texto da exceção; o `git` ausente vira `git_missing` (69).

A lista de contatos públicos (`ops/export/public-contacts.txt`, o mesmo caminho da exportação) é
lida da mesma origem das outras listas e interpretada pela mesma função da exportação,
`repo_scan.parse_public_contacts`; o resultado vai à varredura (`public_contacts`). Ausente vale
como texto vazio, como a `fictitious.txt`; com a lista vazia, nenhum endereço é dispensado.
Lista inválida (`repo_scan.ListInvalid`, um `ValueError`) vira `internal` (1), sem eco da linha.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

# Os nomes fixos dentro do pacote.
SOURCE_COMMIT_FILE = "SOURCE_COMMIT"
BUNDLE_SUMS_FILE = "BUNDLE_SHA256SUMS"
BUNDLE_NAME_TEMPLATE = "cq-bundle-{commit}.tar.gz"
WORKTREE_SUFFIX = "+worktree"

# O mesmo nome de `ops/export/export_public.py`, para que os dois usuários compartilhem o módulo.
REPO_SCAN_MODULE = "cq_ops_export_repo_scan"
REPO_SCAN_PATH = Path(__file__).resolve().parent.parent / "export" / "repo_scan.py"

# As mensagens (`EXPORT_MESSAGES`) vêm da exportação; o nome é o que `tests/helpers/ops.py` dá ao arquivo.
EXPORT_PUBLIC_MODULE = "cq_ops_export_export_public"
EXPORT_PUBLIC_PATH = Path(__file__).resolve().parent.parent / "export" / "export_public.py"

# O repositório deste arquivo (o `main` empacota o próprio repositório).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# As listas, relativas à raiz da origem.
ALLOWLIST_PATH = "ops/export/allowlist.txt"
FICTITIOUS_PATH = "ops/export/fictitious.txt"
BINARY_ALLOWLIST_PATH = "ops/export/binary-allowlist.txt"
# O mesmo caminho de `export_public.PUBLIC_CONTACTS_PATH`.
PUBLIC_CONTACTS_PATH = "ops/export/public-contacts.txt"

# O tamanho do commit curto no nome do pacote e o tempo máximo de cada comando do `git` (segundos).
SHORT_COMMIT_LENGTH = 12
GIT_TIMEOUT_SECONDS = 300

_STAGING_PREFIX = ".cq-bundle-"
_EXECUTABLE_GIT_MODE = "100755"


def _load_by_path(name: str, path: Path) -> ModuleType:
    """O módulo `path`, registrado em `sys.modules` como `name` antes do `exec_module` (removido em caso de
    erro); reusa o já carregado do mesmo arquivo."""
    existing = sys.modules.get(name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_repo_scan() -> ModuleType:
    """O `ops/export/repo_scan.py`, carregado pelo caminho e registrado em `sys.modules` antes do
    `exec_module` (removido em caso de erro); reusa o já carregado do mesmo arquivo."""
    return _load_by_path(REPO_SCAN_MODULE, REPO_SCAN_PATH)


def load_export_public() -> ModuleType:
    """O `ops/export/export_public.py` (dono de `EXPORT_MESSAGES`), carregado como o `repo_scan`."""
    return _load_by_path(EXPORT_PUBLIC_MODULE, EXPORT_PUBLIC_PATH)


class _Refusal(Exception):
    """Fim com uma chave de `EXPORT_MESSAGES` (sem texto variável)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _emit(key: str, lines: Iterable[str] = ()) -> int:
    """Escreve a mensagem fixa de `key` (e as linhas dos achados) e devolve o código."""
    code, text = load_export_public().EXPORT_MESSAGES[key]
    target = sys.stdout if code == 0 else sys.stderr
    target.write(text + "\n")
    for line in lines:
        target.write(line + "\n")
    target.flush()
    return code


# ---------------------------------------------------------------------------------------------------- git


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Roda o `git` em `repo` sem travas opcionais; erro → `GitError` do `repo_scan` (mensagem fixa); o `git`
    ausente → `_Refusal("git_missing")`."""
    try:
        completed = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args],
                                   input=stdin if stdin is not None else b"",
                                   capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        raise _Refusal("git_missing") from None
    if completed.returncode != 0:
        raise load_repo_scan().GitError()
    return completed.stdout


def _split_z(output: bytes) -> list[str]:
    return [entry for entry in output.decode("utf-8").split("\0") if entry]


def _head_commit(repo: Path) -> str:
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise load_repo_scan().GitError()
    return head


def _commit_time(repo: Path, commit: str) -> int:
    """A data do committer de `commit` (segundos desde a época), lida do objeto do commit."""
    for line in _git(repo, "cat-file", "commit", commit).split(b"\n"):
        if not line:
            break
        if line.startswith(b"committer "):
            parts = line.rsplit(b" ", 2)
            if len(parts) == 3 and parts[1].isdigit():
                return int(parts[1])
    return 0


def _read_blobs(repo: Path, specs: Sequence[str]) -> list[bytes | None]:
    """O conteúdo de cada objeto (`<oid>` ou `<commit>:<caminho>`) por um só `git cat-file --batch`; o ausente
    vira `None`."""
    if not specs:
        return []
    if any("\n" in spec for spec in specs):
        raise ValueError("especificação de objeto inválida")
    output = _git(repo, "cat-file", "--batch", stdin="".join(spec + "\n" for spec in specs).encode("utf-8"))
    results: list[bytes | None] = []
    pos = 0
    for _ in specs:
        end = output.index(b"\n", pos)
        header = output[pos:end].split(b" ")
        pos = end + 1
        if len(header) != 3 or header[1] != b"blob":
            # `missing`, `ambiguous` ou um objeto que não é blob: o conteúdo (se houver) é pulado.
            if len(header) == 3 and header[2].isdigit():
                pos += int(header[2]) + 1
            results.append(None)
            continue
        size = int(header[2])
        results.append(output[pos:pos + size])
        pos += size + 1
    return results


def _head_tree(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Caminho → (modo, oid) dos blobs de `commit` (`git ls-tree -r -z`)."""
    entries: dict[str, tuple[str, str]] = {}
    for entry in _split_z(_git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)):
        meta, _, path = entry.partition("\t")
        mode, kind, oid = (meta.split(" ") + ["", "", ""])[:3]
        if kind == "blob":
            entries[path] = (mode, oid)
    return entries


def _index_modes(repo: Path) -> dict[str, str]:
    """Caminho → modo dos rastreados (`git ls-files -s -z`)."""
    modes: dict[str, str] = {}
    for entry in _split_z(_git(repo, "ls-files", "-s", "-z")):
        meta, _, path = entry.partition("\t")
        modes[path] = meta.split(" ", 1)[0]
    return modes


def _pending_paths(repo: Path) -> list[str]:
    """Os rastreados com alteração pendente, no índice ou na árvore (ver o docstring do módulo)."""
    output = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames")
    return [entry[3:] for entry in _split_z(output) if len(entry) > 3]


# ------------------------------------------------------------------------------------------------ listas


def _parse_fictitious(data: bytes, kinds: Sequence[str]) -> dict[str, frozenset[str]]:
    """`fictitious.txt` (`<tipo>\\t<valor>`; `#` comentário; vazias ignoradas) → tipo → valores."""
    accepted: dict[str, set[str]] = {}
    for raw in data.decode("utf-8").split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip() or line.startswith("#"):
            continue
        kind, sep, value = line.partition("\t")
        if not sep or kind not in kinds or not value:
            raise ValueError("linha inválida na lista de valores fictícios")
        accepted.setdefault(kind, set()).add(value)
    return {kind: frozenset(values) for kind, values in accepted.items()}


def _parse_paths(data: bytes) -> frozenset[str]:
    """Uma lista de caminhos exatos (`#` comentário; vazias ignoradas)."""
    paths: set[str] = set()
    for raw in data.decode("utf-8").split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.strip() and not line.startswith("#"):
            paths.add(line)
    return frozenset(paths)


# ------------------------------------------------------------------------------------------------ saída


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    child, parent = _normalized(path), _normalized(root)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # unidades diferentes no Windows
        return False


def _resolve_out(repo: Path, out: Path, commit: str | None) -> Path:
    """O destino final do `.tar.gz` (ver o docstring do módulo); recusa → `_Refusal("out_invalid")`."""
    target = Path(out).resolve()
    if target.is_dir():
        if commit is None:
            raise _Refusal("out_invalid")
        target = target / BUNDLE_NAME_TEMPLATE.format(commit=commit[:SHORT_COMMIT_LENGTH])
    if _inside(target, repo) or _inside(target.parent, repo):
        raise _Refusal("out_invalid")
    if not target.parent.is_dir():
        raise _Refusal("out_invalid")
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise _Refusal("out_invalid")
    return target


def _safe_parts(path: str) -> list[str]:
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\\" in path:
        raise ValueError("caminho inválido na origem")
    return parts


def _sums(files: Mapping[str, bytes]) -> bytes:
    """`BUNDLE_SHA256SUMS` no formato `sha256sum` (modo texto: dois espaços), em ordem de caminho."""
    lines = []
    for path in sorted(files):
        if "\n" in path or "\r" in path:
            raise ValueError("caminho inválido na origem")
        lines.append(f"{hashlib.sha256(files[path]).hexdigest()}  {path}\n")
    return "".join(lines).encode("utf-8")


def _write_tar(target: Path, members: Sequence[tuple[str, bytes, int]], mtime: int) -> None:
    """Escreve o `.tar.gz` determinístico em `target` (o temporário criado pelo chamador)."""
    with open(target, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------------------------- pacote


def _load_lists(read: Any, scan_module: ModuleType,
                scratch: Path) -> tuple[Any, dict[str, frozenset[str]], frozenset[str], frozenset[str]]:
    """As quatro listas, lidas por `read(caminho) -> bytes | None` (do commit ou da árvore); a lista positiva passa
    por um arquivo em `scratch` (o temporário do pacote) para o `Allowlist.load`. A de contatos passa pela
    `parse_public_contacts` do `repo_scan` (a mesma da exportação), uma chamada por pacote; ausente → texto vazio."""
    allow_bytes = read(ALLOWLIST_PATH)
    if allow_bytes is None:
        raise ValueError("lista positiva ausente")
    fictitious_bytes = read(FICTITIOUS_PATH) or b""
    binary_bytes = read(BINARY_ALLOWLIST_PATH) or b""
    contacts_bytes = read(PUBLIC_CONTACTS_PATH) or b""
    allow_file = scratch / "allowlist.txt"
    allow_file.write_bytes(allow_bytes)
    allowlist = scan_module.Allowlist.load(allow_file)
    # `ListInvalid` e `UnicodeDecodeError` são `ValueError`: o `build_bundle` os mapeia para `internal`, sem eco.
    contacts = scan_module.parse_public_contacts(contacts_bytes.decode("utf-8"))
    return (allowlist, _parse_fictitious(fictitious_bytes, scan_module.FINDING_KINDS), _parse_paths(binary_bytes),
            contacts)


def _build(repo: Path, out: Path, source: str) -> int:
    scan_module = load_repo_scan()
    if source not in ("head", "worktree"):
        raise _Refusal("invalid_arguments")
    # A saída é conferida antes de qualquer `git`: dentro da origem, nada roda (o nome no diretório depende do
    # commit, então esse caso volta aqui depois).
    if not Path(out).resolve().is_dir():
        _resolve_out(repo, out, None)
    commit = _head_commit(repo)
    target = _resolve_out(repo, out, commit)
    mtime = _commit_time(repo, commit)

    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=target.parent))
    try:
        return _stage_and_pack(repo, target, source, commit, mtime, staging, scan_module)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_and_pack(repo: Path, target: Path, source: str, commit: str, mtime: int, staging: Path,
                    scan_module: ModuleType) -> int:
    """Lê as listas, seleciona, confere as pendências, materializa em `staging`, varre e empacota (o `.tar.gz`
    num temporário ao lado de `target`, renomeado só no fim e apagado em erro); devolve o código."""
    tree: dict[str, tuple[str, str]] = {}
    if source == "head":
        tree = _head_tree(repo, commit)

        def read_list(path: str) -> bytes | None:
            entry = tree.get(path)
            return None if entry is None else _read_blobs(repo, [entry[1]])[0]
    else:
        def read_list(path: str) -> bytes | None:
            file = repo.joinpath(*path.split("/"))
            return file.read_bytes() if file.is_file() else None

    allowlist, fictitious, binary_allow, public_contacts = _load_lists(read_list, scan_module, staging)
    selected = sorted(scan_module.select(repo, allowlist, source=source))
    if {SOURCE_COMMIT_FILE, BUNDLE_SUMS_FILE} & set(selected):
        raise ValueError("nome reservado do pacote na origem")
    for path in selected:
        _safe_parts(path)

    if source == "head":
        if any(allowlist.matches(path) for path in _pending_paths(repo)):
            raise _Refusal("pending_changes")
        if any(path not in tree for path in selected):  # no índice e fora do commit: o `status` já acusaria
            raise _Refusal("pending_changes")
        modes = {path: tree[path][0] for path in selected}
        blobs = _read_blobs(repo, [tree[path][1] for path in selected])
    else:
        index_modes = _index_modes(repo)
        modes = {path: index_modes.get(path, "100644") for path in selected}
        blobs = [repo.joinpath(*path.split("/")).read_bytes() for path in selected]

    root = staging / "arvore"
    root.mkdir()
    contents: dict[str, bytes] = {}
    for path, data in zip(selected, blobs, strict=True):
        if data is None:
            raise ValueError("objeto ausente na origem")
        file = root.joinpath(*_safe_parts(path))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        contents[path] = data
    del blobs

    findings = scan_module.scan(root, selected, fictitious=fictitious, binary_allow=binary_allow,
                                public_contacts=public_contacts)
    if findings:
        template = load_export_public().FINDING_LINE_TEMPLATE
        ordered = sorted(findings, key=lambda found: (found.path, found.line, found.kind))
        return _emit("findings", (template.format(path=found.path, line=found.line, kind=found.kind)
                                  for found in ordered))

    source_commit = (commit + (WORKTREE_SUFFIX if source == "worktree" else "") + "\n").encode("ascii")
    contents[SOURCE_COMMIT_FILE] = source_commit
    sums = _sums(contents)
    members: list[tuple[str, bytes, int]] = []
    for path in sorted([*selected, SOURCE_COMMIT_FILE, BUNDLE_SUMS_FILE]):
        if path == BUNDLE_SUMS_FILE:
            members.append((path, sums, 0o644))
        else:
            mode = 0o755 if modes.get(path) == _EXECUTABLE_GIT_MODE else 0o644
            members.append((path, contents[path], mode))

    handle, partial_name = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".tmp", dir=target.parent)
    os.close(handle)
    partial = Path(partial_name)
    try:
        _write_tar(partial, members, mtime)
        os.replace(partial, target)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return _emit("bundle_ok")


def build_bundle(repo: Path, out: Path, *, source: Literal["head", "worktree"] = "head") -> int:
    """Gera o pacote em `out`; devolve o código de `EXPORT_MESSAGES`."""
    try:
        return _build(Path(repo).resolve(), Path(out), source)
    except _Refusal as refusal:
        return _emit(refusal.key)
    except Exception:  # noqa: BLE001 - mensagem fixa; o texto da exceção pode trazer caminho ou valor.
        return _emit("internal")


# ------------------------------------------------------------------------------------------------- main


class _ArgumentError(Exception):
    """Linha de comando inválida (sem o `usage` do `argparse` nem eco da entrada)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # noqa: ARG002 - a mensagem do argparse ecoa a entrada; descartada
        raise _ArgumentError()

    def exit(self, status: int = 0, message: str | None = None) -> Any:  # noqa: ARG002
        raise _ArgumentError()


def _parser() -> _Parser:
    parser = _Parser(prog="make_bundle.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--out", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        return _emit("invalid_arguments")
    if not args.out:
        return _emit("invalid_arguments")
    return build_bundle(REPO_ROOT, Path(args.out), source="head")


if __name__ == "__main__":
    sys.exit(main())
