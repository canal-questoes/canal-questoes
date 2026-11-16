"""Exportação pública e higienizada do repositório.

Uso:

    python ops/export/export_public.py --dest <diretório vazio fora do repositório> --author-name <nome>
           --author-email <e-mail>

Copia só os arquivos rastreados que casam `ops/export/allowlist.txt` para `--dest`, grava
`ops/ci/known-flaky.txt` filtrado **antes** da varredura (que o cobre), varre (`repo_scan.scan`) e, sem
achado, cria um repositório novo (`git init -q`, `git add -A`, `git commit -q` com a mensagem fixa
`COMMIT_MESSAGE`, sem linha de coautoria, com `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` no nulo do sistema e
`core.hooksPath` vazio). Nada é escrito na origem.

Só biblioteca padrão; carrega `repo_scan.py` do próprio diretório (`load_repo_scan`). Importável
sem efeito colateral (o `repo_scan` só é carregado quando chamado).

**[I]** Escolhas da implementação, não fixadas de antemão:
- os bytes exportados são os do commit `HEAD` da origem (`git cat-file --batch`), para os rastreados
  (`git ls-files -z`) presentes nele que casam a lista; a alteração pendente não é recusada (a recusa
  é só do pacote), mas fica de fora: a exportação leva o que está no commit. As listas (`allowlist.txt`,
  `fictitious.txt`, `binary-allowlist.txt`, em `<repo>/ops/export/`) e a lista de instáveis
  (o arquivo de `FLAKY_SOURCE_PATH`) também são lidas do commit. A `allowlist.txt` ausente ou
  inválida é erro interno; as outras ausentes valem como vazias. Os modos `100755` saem executáveis no índice do
  destino (`git update-index --chmod=+x`); os links simbólicos e os submódulos do commit ficam de fora;
- o destino tem de ser um diretório **existente** e vazio, fora da origem (comparação pelo caminho resolvido); o
  inexistente também é `dest_invalid`. As recusas do destino vêm antes de qualquer `git`; o `git` ausente
  (`FileNotFoundError`) vira `git_missing` (69) antes de escrever;
- o `ops/ci/known-flaky.txt` derivado leva um cabeçalho de comentário e as linhas por nó da fonte; saem os
  comentários, as vazias e as entradas `re:` (a linha leva só o nó, sem o TAB e o motivo; ver abaixo);
- em achado ou em erro depois da primeira escrita, o conteúdo do destino é apagado e o diretório fica vazio;
- todo `git` (na origem e no destino) roda sem nenhuma variável `GIT_*` herdada, com `GIT_CONFIG_GLOBAL` e
  `GIT_CONFIG_SYSTEM` no nulo do sistema, `GIT_CONFIG_NOSYSTEM=1`, sem prompt e sem entrada; no destino, também
  `core.hooksPath` vazio, `init --template=` (nenhum modelo, logo nenhum gancho copiado), `commit --no-verify`, sem
  assinatura, o ramo `main`, `core.autocrlf=false` (os bytes ficam como estão) e o autor e o committer dados;
- as mensagens fixas: o sucesso no stdout; recusas, achados (`arquivo:linha: tipo`, ordenados) e erros no stderr;
  o erro inesperado vira `internal` (1), sem o texto da exceção. Nome ou e-mail do autor vazio ou com quebra de
  linha, NUL, `<` ou `>` é `invalid_arguments` (64).

Além disso:
- a sobreposição pública `OVERLAY_DIR` do commit tem de ter exatamente `OVERLAY_FILES` como blobs `100644`
  (`overlay_entries`); senão, a recusa `overlay_invalid` (65), depois das listas e antes da primeira escrita. Os três
  arquivos saem na raiz do destino, byte a byte, e nenhum caminho sob a pasta sai. **[I]** Um submódulo sob a pasta
  também a torna inválida, e os três nomes da raiz da origem ficam fora da seleção mesmo que a lista os case;
- a lista de contatos públicos (`PUBLIC_CONTACTS_PATH`, do commit, pela `repo_scan.parse_public_contacts`) vai à
  varredura; ausente vale como vazia; inválida é `internal`;
- o `ops/ci/known-flaky.txt` derivado leva o `KNOWN_FLAKY_HEADER` e só o nó de cada entrada, sem o motivo.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

# Chave → (código de saída, mensagem). [I] Os textos e as chaves são da implementação (fixados de antemão
# são só os códigos 64, 65 e 69 e a linha `arquivo:linha: tipo`); valem também para o pacote
# (`ops/deploy/make_bundle.py`). Nenhuma mensagem traz caminho nem valor.
EXPORT_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Exportação concluída."),
    "bundle_ok": (0, "Pacote de implantação criado."),
    "dest_invalid": (64, "O destino deve ser um diretório vazio fora do repositório."),
    "out_invalid": (64, "O arquivo de saída deve ficar fora do repositório."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "pending_changes": (65, "Há alterações pendentes em arquivos da lista; registre-as num commit antes."),
    "findings": (65, "A varredura encontrou possíveis segredos ou dados pessoais:"),
    "git_missing": (69, "O git não foi encontrado."),
    "internal": (1, "Erro interno da exportação."),
    # A sobreposição pública inválida, antes da primeira escrita.
    "overlay_invalid": (65, "A sobreposição pública deve ter exatamente AGENTS.md, CLAUDE.md e ROADMAP.md."),
}

# A linha de cada achado, depois da mensagem `findings` (sem o valor).
FINDING_LINE_TEMPLATE = "{path}:{line}: {kind}"

# A mensagem fixa do commit da exportação, sem linha de coautoria.
COMMIT_MESSAGE = "Versão inicial do canal-questoes"

# O nome único com que o `repo_scan.py` é registrado em `sys.modules` (o mesmo em `make_bundle.py`).
REPO_SCAN_MODULE = "cq_ops_export_repo_scan"
REPO_SCAN_PATH = Path(__file__).resolve().parent / "repo_scan.py"


def load_repo_scan() -> ModuleType:
    """O `repo_scan.py` deste diretório, carregado pelo caminho e registrado em `sys.modules` antes do
    `exec_module` (removido em caso de erro), como na receita "Importing a source file directly"; reusa o
    já carregado do mesmo arquivo."""
    existing = sys.modules.get(REPO_SCAN_MODULE)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == REPO_SCAN_PATH:
        return existing
    spec = importlib.util.spec_from_file_location(REPO_SCAN_MODULE, REPO_SCAN_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(REPO_SCAN_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[REPO_SCAN_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(REPO_SCAN_MODULE, None)
        raise
    return module


# As listas, relativas à raiz da origem, e o arquivo derivado.
ALLOWLIST_PATH = "ops/export/allowlist.txt"
FICTITIOUS_PATH = "ops/export/fictitious.txt"
BINARY_ALLOWLIST_PATH = "ops/export/binary-allowlist.txt"
FLAKY_SOURCE_PATH = "docs/execucao/instaveis-conhecidos.txt"
# A lista de contatos públicos, lida do commit; ausente vale como vazia.
PUBLIC_CONTACTS_PATH = "ops/export/public-contacts.txt"
KNOWN_FLAKY_PATH = "ops/ci/known-flaky.txt"
# O cabeçalho fixo, só de comentários; abaixo dele, um nó por linha.
KNOWN_FLAKY_HEADER = (
    "# Testes instáveis conhecidos, um nó do pytest por linha, para a repetição única do CI (ops/ci/ci_suite.py).\n"
    "# Arquivo gerado pela exportação pública (ops/export/export_public.py). Não edite à mão.\n"
)

# O repositório deste arquivo (o `main` exporta o próprio repositório).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# O ramo do repositório novo e o tempo máximo de cada comando do `git` (segundos).
DEFAULT_BRANCH = "main"
GIT_TIMEOUT_SECONDS = 300

_EXECUTABLE_GIT_MODE = "100755"
_REGULAR_GIT_MODES = frozenset({"100644", _EXECUTABLE_GIT_MODE})
_FORBIDDEN_AUTHOR_CHARS = frozenset("\n\r\0<>")


# A pasta da sobreposição pública na origem e os três arquivos que ela leva à
# raiz da árvore exportada; nenhum caminho sob a pasta é exportado como está.
OVERLAY_DIR = "ops/export/publico/"
OVERLAY_FILES = ("AGENTS.md", "CLAUDE.md", "ROADMAP.md")
_REGULAR_OVERLAY_MODE = "100644"

# Uma entrada da árvore do commit, (modo, id do blob), como em `_Origin.head_tree`.
TreeEntry = tuple[str, str]


class OverlayInvalid(Exception):
    """A sobreposição pública do commit não tem exatamente os três arquivos como blobs regulares
    (mapeada à chave `overlay_invalid`)."""


def overlay_entries(tree: Mapping[str, TreeEntry]) -> dict[str, str]:
    """Nome → id do blob de cada arquivo de `OVERLAY_FILES` sob `OVERLAY_DIR` em `tree` (caminho → (modo,
    id)); levanta `OverlayInvalid` se faltar, sobrar ou não for blob regular.

    Conta todo caminho que começa com `OVERLAY_DIR` (inclusive os de uma subpasta, que sobram); os nomes comparam
    com caixa; o modo tem de ser `100644`. A exceção não traz caminho nem valor."""
    found = {path[len(OVERLAY_DIR):]: entry for path, entry in tree.items() if path.startswith(OVERLAY_DIR)}
    if set(found) != set(OVERLAY_FILES) or any(mode != _REGULAR_OVERLAY_MODE for mode, _ in found.values()):
        raise OverlayInvalid()
    return {name: found[name][1] for name in OVERLAY_FILES}


class _Refusal(Exception):
    """Fim com uma chave de `EXPORT_MESSAGES` (sem texto variável)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _GitFailed(Exception):
    """O `git` saiu com erro (mensagem fixa; nunca a saída do `git`)."""

    def __init__(self) -> None:
        super().__init__("O git saiu com erro.")


def _emit(key: str, lines: Iterable[str] = ()) -> int:
    """Escreve a mensagem fixa de `key` (e as linhas dos achados) e devolve o código."""
    code, text = EXPORT_MESSAGES[key]
    target = sys.stdout if code == 0 else sys.stderr
    target.write(text + "\n")
    for line in lines:
        target.write(line + "\n")
    target.flush()
    return code


# ---------------------------------------------------------------------------------------------------- git


def _git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """O ambiente do `git` limpo (ver o docstring do módulo)."""
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if extra:
        env.update(extra)
    return env


def _run_git(git: str, args: Sequence[str], *, env: Mapping[str, str], stdin: bytes = b"") -> bytes:
    """Roda o `git` sem travas opcionais; erro → `_GitFailed`; o `git` ausente → `_Refusal("git_missing")`."""
    try:
        completed = subprocess.run([git, "--no-optional-locks", *args], input=stdin, capture_output=True,
                                   env=dict(env), timeout=GIT_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        raise _Refusal("git_missing") from None
    if completed.returncode != 0:
        raise _GitFailed()
    return completed.stdout


def _split_z(output: bytes) -> list[str]:
    return [entry for entry in output.decode("utf-8").split("\0") if entry]


class _Origin:
    """As leituras da origem (só leitura: `ls-files`, `rev-parse`, `ls-tree`, `cat-file`)."""

    def __init__(self, repo: Path, git: str) -> None:
        self.repo = repo
        self.git = git
        self.env = _git_env()
        # Os caminhos de `HEAD` que não são blobs (submódulos), preenchidos por `head_tree`; a sobreposição
        # não pode ter nenhum (só os três blobs regulares).
        self.non_blob_paths: frozenset[str] = frozenset()

    def _git(self, *args: str, stdin: bytes = b"") -> bytes:
        return _run_git(self.git, ["-C", str(self.repo), *args], env=self.env, stdin=stdin)

    def tracked(self) -> set[str]:
        return set(_split_z(self._git("ls-files", "-z")))

    def head_tree(self) -> dict[str, tuple[str, str]]:
        """Caminho → (modo, oid) dos blobs de `HEAD` (`git ls-tree -r -z`)."""
        head = self._git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        entries: dict[str, tuple[str, str]] = {}
        others: set[str] = set()
        for entry in _split_z(self._git("ls-tree", "-r", "-z", "--full-tree", head)):
            meta, _, path = entry.partition("\t")
            mode, kind, oid = (meta.split(" ") + ["", "", ""])[:3]
            if kind == "blob":
                entries[path] = (mode, oid)
            else:
                others.add(path)
        self.non_blob_paths = frozenset(others)
        return entries

    def blobs(self, oids: Sequence[str]) -> list[bytes]:
        """O conteúdo de cada blob, por um só `git cat-file --batch`."""
        if not oids:
            return []
        output = self._git("cat-file", "--batch", stdin="".join(oid + "\n" for oid in oids).encode("ascii"))
        results: list[bytes] = []
        pos = 0
        for _ in oids:
            end = output.index(b"\n", pos)
            header = output[pos:end].split(b" ")
            pos = end + 1
            if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
                raise ValueError("objeto ausente na origem")
            size = int(header[2])
            results.append(output[pos:pos + size])
            pos += size + 1
        return results


# ------------------------------------------------------------------------------------------------ listas


def _lines(data: bytes) -> list[str]:
    return [raw[:-1] if raw.endswith("\r") else raw for raw in data.decode("utf-8").split("\n")]


def _parse_fictitious(data: bytes, kinds: Sequence[str]) -> dict[str, frozenset[str]]:
    """`fictitious.txt` (`<tipo>\\t<valor>`; `#` comentário; vazias ignoradas) → tipo → valores."""
    accepted: dict[str, set[str]] = {}
    for line in _lines(data):
        if not line.strip() or line.startswith("#"):
            continue
        kind, sep, value = line.partition("\t")
        if not sep or kind not in kinds or not value:
            raise ValueError("linha inválida na lista de valores fictícios")
        accepted.setdefault(kind, set()).add(value)
    return {kind: frozenset(values) for kind, values in accepted.items()}


def _parse_paths(data: bytes) -> frozenset[str]:
    """Uma lista de caminhos exatos (`#` comentário; vazias ignoradas)."""
    return frozenset(line for line in _lines(data) if line.strip() and not line.startswith("#"))


def known_flaky_text(source: bytes) -> bytes:
    """O `ops/ci/known-flaky.txt` derivado da lista de instáveis: o cabeçalho fixo e, na ordem
    da fonte, só o nó de cada entrada por nó (sem o TAB e o motivo); saem os comentários, as vazias e as `re:`."""
    nodes = [line.partition("\t")[0] for line in _lines(source)
             if line.strip() and not line.lstrip().startswith(("#", "re:"))]
    return (KNOWN_FLAKY_HEADER + "".join(node + "\n" for node in nodes)).encode("utf-8")


# ------------------------------------------------------------------------------------------------ destino


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    child, parent = _normalized(path), _normalized(root)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # unidades diferentes no Windows
        return False


def _check_dest(repo: Path, dest: Path) -> Path:
    """O destino resolvido; recusa (`dest_invalid`) sem escrever nada."""
    target = Path(dest).resolve()
    if _inside(target, repo) or _inside(repo, target):
        raise _Refusal("dest_invalid")
    if Path(dest).is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise _Refusal("dest_invalid")
    return target


def _safe_parts(path: str) -> list[str]:
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or any(part in ("", ".", "..", ".git") for part in parts):
        raise ValueError("caminho inválido na origem")
    return parts


def _writable_retry(function: Callable[..., Any], path: str, _excinfo: Any) -> None:
    """No Windows, os objetos do `.git` são somente leitura: tira o atributo e tenta de novo."""
    os.chmod(path, 0o700)
    function(path)


def _empty_dir(target: Path) -> None:
    """Apaga o conteúdo de `target` (o diretório fica, vazio)."""
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            if sys.version_info >= (3, 12):
                shutil.rmtree(child, onexc=_writable_retry)
            else:  # pragma: no cover - Python anterior ao 3.12
                shutil.rmtree(child, onerror=_writable_retry)
        else:
            try:
                child.unlink()
            except PermissionError:
                os.chmod(child, 0o600)
                child.unlink()


# ------------------------------------------------------------------------------------------------ exportação


def _valid_author(value: str) -> bool:
    return bool(value.strip()) and not (_FORBIDDEN_AUTHOR_CHARS & set(value))


def _load_lists(scan_module: ModuleType, read: Callable[[str], bytes | None]
                ) -> tuple[Any, dict[str, frozenset[str]], frozenset[str], frozenset[str]]:
    """As quatro listas, lidas do commit; a lista positiva passa por um arquivo num temporário do sistema (fora da
    origem e do destino) para o `Allowlist.load`. A lista de contatos públicos, pela
    `parse_public_contacts` (a mesma do pacote); ausente vale como vazia; inválida (`ListInvalid`, ou bytes fora do
    UTF-8) é um `ValueError`, que vira `internal`."""
    allow_bytes = read(ALLOWLIST_PATH)
    if allow_bytes is None:
        raise ValueError("lista positiva ausente")
    scratch = Path(tempfile.mkdtemp(prefix="cq-export-"))
    try:
        allow_file = scratch / "allowlist.txt"
        allow_file.write_bytes(allow_bytes)
        allowlist = scan_module.Allowlist.load(allow_file)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    fictitious = _parse_fictitious(read(FICTITIOUS_PATH) or b"", scan_module.FINDING_KINDS)
    binary_allow = _parse_paths(read(BINARY_ALLOWLIST_PATH) or b"")
    contacts = scan_module.parse_public_contacts((read(PUBLIC_CONTACTS_PATH) or b"").decode("utf-8"))
    return allowlist, fictitious, binary_allow, contacts


def _materialize(target: Path, files: Mapping[str, bytes]) -> None:
    for path, data in files.items():
        file = target.joinpath(*_safe_parts(path))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)


def _commit(target: Path, author_name: str, author_email: str, executables: Sequence[str], git: str) -> None:
    """`git init -q`, `git add -A` e `git commit -q` no destino, com o ambiente limpo."""
    env = _git_env({
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        "GIT_CEILING_DIRECTORIES": str(target.parent),
    })
    # core.longpaths: no Windows, um destino fundo somado aos caminhos das fixtures do motor passa de 260
    # caracteres, e a configuração global do operador (que costuma ligar a opção) fica desligada aqui; [F] o
    # `git add -A` falhou com "Filename too long" na validação local de 2026-11-03. Nos outros sistemas, a opção
    # não tem efeito.
    options = ["-c", "core.hooksPath=", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
               "-c", "core.longpaths=true", "-c", "commit.gpgSign=false", "-c", f"init.defaultBranch={DEFAULT_BRANCH}"]
    base = [*options, "-C", str(target)]
    _run_git(git, [*options, "init", "-q", "--template=", str(target)], env=env)
    _run_git(git, [*base, "add", "-A"], env=env)
    if executables:
        listing = "".join(path + "\0" for path in executables).encode("utf-8")
        _run_git(git, [*base, "update-index", "--chmod=+x", "-z", "--stdin"], env=env, stdin=listing)
    _run_git(git, [*base, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", COMMIT_MESSAGE], env=env)


def _export(repo: Path, dest: Path, author_name: str, author_email: str, git: str) -> int:
    if not (_valid_author(author_name) and _valid_author(author_email)):
        raise _Refusal("invalid_arguments")
    target = _check_dest(repo, dest)
    scan_module = load_repo_scan()

    origin = _Origin(repo, git)
    tracked = origin.tracked()
    tree = origin.head_tree()

    def read(path: str) -> bytes | None:
        entry = tree.get(path)
        return None if entry is None or entry[0] not in _REGULAR_GIT_MODES else origin.blobs([entry[1]])[0]

    allowlist, fictitious, binary_allow, contacts = _load_lists(scan_module, read)
    # A sobreposição, antes da primeira escrita (o destino fica vazio, sem limpeza).
    if any(path.startswith(OVERLAY_DIR) for path in origin.non_blob_paths):
        raise _Refusal("overlay_invalid")
    try:
        overlay = overlay_entries(tree)
    except OverlayInvalid:
        raise _Refusal("overlay_invalid") from None
    # Fora da seleção, todo caminho sob a pasta da sobreposição e, [I] por garantia, os três nomes
    # da raiz (as versões privadas, que a lista positiva não deve casar): na raiz saem só os da sobreposição.
    selected = sorted(path for path, (mode, _) in tree.items()
                      if mode in _REGULAR_GIT_MODES and path in tracked and allowlist.matches(path)
                      and path != KNOWN_FLAKY_PATH and not path.startswith(OVERLAY_DIR)
                      and path not in OVERLAY_FILES)
    for path in selected:
        _safe_parts(path)
    contents = origin.blobs([tree[path][1] for path in selected] + [overlay[name] for name in OVERLAY_FILES])
    files = dict(zip([*selected, *OVERLAY_FILES], contents, strict=True))
    files[KNOWN_FLAKY_PATH] = known_flaky_text(read(FLAKY_SOURCE_PATH) or b"")
    executables = [path for path in selected if tree[path][0] == _EXECUTABLE_GIT_MODE]
    paths = sorted(files)

    try:
        _materialize(target, files)
        del files, contents
        findings = scan_module.scan(target, paths, fictitious=fictitious, binary_allow=binary_allow,
                                    public_contacts=contacts)
        if findings:
            _empty_dir(target)
            ordered = sorted(findings, key=lambda found: (found.path, found.line, found.kind))
            return _emit("findings", (FINDING_LINE_TEMPLATE.format(path=found.path, line=found.line,
                                                                     kind=found.kind) for found in ordered))
        _commit(target, author_name, author_email, executables, git)
    except BaseException:
        _empty_dir(target)
        raise
    return _emit("ok")


def export(repo: Path, dest: Path, author_name: str, author_email: str, *, git: str = "git") -> int:
    """A exportação; devolve o código de `EXPORT_MESSAGES`."""
    try:
        return _export(Path(repo).resolve(), Path(dest), author_name, author_email, git)
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
    parser = _Parser(prog="export_public.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--author-name", required=True)
    parser.add_argument("--author-email", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        return _emit("invalid_arguments")
    if not args.dest:
        return _emit("invalid_arguments")
    return export(REPO_ROOT, Path(args.dest), args.author_name, args.author_email)


if __name__ == "__main__":
    sys.exit(main())
