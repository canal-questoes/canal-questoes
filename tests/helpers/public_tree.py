"""A árvore pública que o teste de contrato dos arquivos públicos julga, e as leituras que ele faz dela.

Código de teste, real desde o esqueleto. Quem o usa: `tests/contract/test_public_tree.py`.

- `exported_root(tmp_path_factory) -> Path`: a árvore julgada. Na árvore exportada (`is_exported_tree()`), a
  própria raiz do repositório; no repositório de origem, a exportação do último commit (`export_public.export`) num
  destino temporário, exigindo o código 0. A exportação roda uma vez por processo (cada processo do xdist faz a sua):
  o resultado fica guardado no módulo.
- `pending_exported_changes(repo) -> bool`: há alteração não commitada (modificado, apagado, novo não ignorado) num
  arquivo que a lista positiva leva ou na pasta da sobreposição; a exportação lê só o último commit, então o teste
  não julgaria o que está no disco.
- `tracked(root) -> list[str]`: os arquivos rastreados de `root` (`git ls-files`), com `/`.
- `markdown_links(text) -> list[str]`: os alvos dos links `[..](alvo)` e `![..](alvo)` fora dos blocos cercados e
  dos trechos em crase, sem os de esquema (`https:`, `mailto:`) e sem os que são só uma âncora (`#...`). O alvo vem
  como escrito, sem os sinais `<>` e sem o título.
- `broken_links(root) -> list[tuple[str, str]]`: os pares (arquivo, alvo) de todo `.md` rastreado de `root` cujo
  alvo, sem a âncora e sem a consulta, não resolve por `posixpath.normpath` para um arquivo ou um diretório rastreado
  de `root` (com caixa: o Windows e o Linux dão o mesmo resultado). As âncoras não são conferidas.
- `repository_slugs(root) -> set[tuple[str, str]]`: os pares (dono, repositório) dos badges do README e dos campos
  `repository-code:` e `url:` do CITATION.cff.
- `contact_emails(root) -> dict[str, list[str]]`: os e-mails de cada arquivo de contato presente, na ordem do texto.

Os caminhos privados são montados em tempo de execução, para este módulo não conter os textos que o detector de
códigos internos acusa.

**[I]** Escolhas deste módulo:
- em `pending_exported_changes`, um arquivo novo não ignorado que casa a lista positiva também conta: o último commit
  não o tem, e o teste não o julgaria; o `git status` roda com a configuração do desenvolvedor (a dele decide o fim de
  linha da árvore de trabalho);
- em `markdown_links`, os trechos em crase também ficam fora (o GitHub não os mostra como link); os links de
  referência (`[x]: alvo`) e os de HTML (`<a href>`, `<img src>`) não são lidos; o alvo com `%xx` é decodificado em
  `broken_links`; o alvo que começa com `/` é relativo à raiz, como no GitHub;
- em `repository_slugs`, do README entram só as URLs dos badges (as imagens e o link que envolve uma imagem), para o
  link a outro repositório no texto (o do PrairieLearn, nos agradecimentos) não contar como dono; os padrões são os
  de `github.com/<d>/<r>`, `codecov.io/gh/<d>/<r>`, `api.scorecard.dev/projects/github.com/<d>/<r>` e
  `img.shields.io/github/<rota>/<d>/<r>`, com o sufixo `.git` tirado;
- em `contact_emails`, o padrão é o do detector `email` da varredura da exportação.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from tests.helpers.export_tree import is_exported_tree
from tests.helpers.ops import REPO, load_ops

EXPORT_AUTHOR_NAME = "Pessoa de Teste"
EXPORT_AUTHOR_EMAIL = "teste@exportacao.test"
GIT_TIMEOUT_SECONDS = 120

# Os caminhos privados: nunca saem na árvore exportada (montados em tempo de execução).
PRIVATE_PATHS: tuple[str, ...] = (
    "docs/" + "base-de-conhecimento/",
    "docs/" + "execucao/",
    "docs/" + "monografia/",
    "docs/" + "arquitetura/",
    "spec" + "s/",
    "." + "specify/",
    "." + "claude/",
    "spi" + "kes/",
)

# Os arquivos de contato (os e-mails deles são conferidos).
CONTACT_FILES: tuple[str, ...] = ("README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md",
                                  "CITATION.cff")

FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
# `](alvo)` ou `](alvo "título")`; o alvo pode vir entre `<>`. Casa também o link que envolve uma imagem.
_LINK = re.compile(r"\]\(\s*(<[^>\n]*>|[^)\s]+)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(\s*<?([^)\s>]+)")
_WRAPPED_IMAGE = re.compile(r"\[!\[[^\]\n]*\]\([^)\n]*\)\]\(\s*<?([^)\s>]+)")
_CITATION_URL = re.compile(r"^\s*(?:repository-code|url)\s*:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)

_SEGMENT = r"([A-Za-z0-9_.-]+)"
_SLUG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w.-])github\.com/" + _SEGMENT + "/" + _SEGMENT),
    re.compile(r"codecov\.io/gh/" + _SEGMENT + "/" + _SEGMENT),
    re.compile(r"api\.scorecard\.dev/projects/github\.com/" + _SEGMENT + "/" + _SEGMENT),
)
_SHIELDS_GITHUB = re.compile(r"img\.shields\.io/github/([^?#)\s\"']+)")
# As rotas do shields.io com mais de um segmento antes do dono (as outras têm um só, como `license`).
_SHIELDS_ROUTE_SEGMENTS: dict[str, int] = {"actions": 3, "v": 2, "commit-activity": 2}

_EXPORTED: dict[str, Path] = {}


# --------------------------------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    assert completed.returncode == 0, f"git {args[0]} falhou (código {completed.returncode})"
    return completed.stdout


def tracked(root: Path) -> list[str]:
    """Os arquivos rastreados de `root` (`git ls-files -z`), com `/`, em ordem."""
    return sorted(name for name in _git(Path(root), "ls-files", "-z").decode("utf-8").split("\0") if name)


def _tracked_dirs(files: list[str]) -> set[str]:
    dirs: set[str] = {"."}
    for name in files:
        parts = name.split("/")[:-1]
        for end in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:end]))
    return dirs


# --------------------------------------------------------------------------------------------------
# A árvore julgada
# --------------------------------------------------------------------------------------------------


def exported_root(tmp_path_factory: Any) -> Path:
    """A árvore julgada (ver o docstring do módulo)."""
    if is_exported_tree():
        return REPO
    cached = _EXPORTED.get("root")
    if cached is not None:
        return cached
    export_public = load_ops("ops/export/export_public.py")
    dest = Path(tmp_path_factory.mktemp("arvore-publica"))
    code = export_public.export(REPO, dest, EXPORT_AUTHOR_NAME, EXPORT_AUTHOR_EMAIL)
    assert code == 0, f"a exportação do último commit saiu com o código {code}"
    _EXPORTED["root"] = dest
    return dest


def pending_exported_changes(repo: Path) -> bool:
    """Há alteração não commitada num arquivo que a exportação leva (ver o docstring do módulo)."""
    repo = Path(repo)
    export_public = load_ops("ops/export/export_public.py")
    repo_scan = load_ops("ops/export/repo_scan.py")
    allowlist = repo_scan.Allowlist.load(repo.joinpath(*export_public.ALLOWLIST_PATH.split("/")))
    status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    for entry in status.decode("utf-8").split("\0"):
        if len(entry) < 4:
            continue
        path = entry[3:]
        if allowlist.matches(path) or path.startswith(export_public.OVERLAY_DIR):
            return True
    return False


# --------------------------------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------------------------------


def markdown_links(text: str) -> list[str]:
    """Os alvos dos links relativos (ver o docstring do módulo), na ordem do texto."""
    targets: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if inside:
            continue
        for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
            target = match.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if not target or target.startswith("#") or _SCHEME.match(target):
                continue
            targets.append(target)
    return targets


def _resolve(md_path: str, target: str) -> str | None:
    """O caminho de `target` relativo à raiz, ou `None` se sai dela."""
    bare = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
    if bare.startswith("/"):
        joined = bare.lstrip("/") or "."
    else:
        joined = posixpath.join(posixpath.dirname(md_path), bare)
    resolved = posixpath.normpath(joined) if joined else "."
    if resolved == ".." or resolved.startswith("../"):
        return None
    return resolved


def broken_links(root: Path) -> list[tuple[str, str]]:
    """Os links relativos quebrados de todo `.md` rastreado de `root` (ver o docstring do módulo)."""
    root = Path(root)
    files = tracked(root)
    file_set = set(files)
    dirs = _tracked_dirs(files)
    broken: list[tuple[str, str]] = []
    for md in files:
        if not md.lower().endswith(".md"):
            continue
        text = root.joinpath(*md.split("/")).read_bytes().decode("utf-8", errors="replace")
        for target in markdown_links(text):
            resolved = _resolve(md, target)
            if resolved is None or (resolved not in file_set and resolved not in dirs):
                broken.append((md, target))
    return broken


# --------------------------------------------------------------------------------------------------
# Dono do repositório e contatos
# --------------------------------------------------------------------------------------------------


def _read(root: Path, rel: str) -> str | None:
    path = Path(root).joinpath(*rel.split("/"))
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _strip_git(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def _slugs_of(url: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for pattern in _SLUG_PATTERNS:
        for match in pattern.finditer(url):
            found.add((match.group(1), _strip_git(match.group(2))))
    for match in _SHIELDS_GITHUB.finditer(url):
        segments = [segment for segment in match.group(1).split("/") if segment]
        skip = _SHIELDS_ROUTE_SEGMENTS.get(segments[0], 1) if segments else 1
        if len(segments) >= skip + 2:
            found.add((segments[skip], _strip_git(segments[skip + 1])))
    return found


def badge_urls(text: str) -> list[str]:
    """As URLs dos badges de um Markdown: as das imagens e as dos links que envolvem uma imagem."""
    urls: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if inside:
            continue
        urls.extend(match.group(1) for match in _IMAGE.finditer(line))
        urls.extend(match.group(1) for match in _WRAPPED_IMAGE.finditer(line))
    return urls


def repository_slugs(root: Path) -> set[tuple[str, str]]:
    """Os pares (dono, repositório) dos badges do README e do CITATION.cff (ver o docstring do módulo)."""
    found: set[tuple[str, str]] = set()
    readme = _read(root, "README.md")
    if readme is not None:
        for url in badge_urls(readme):
            found |= _slugs_of(url)
    citation = _read(root, "CITATION.cff")
    if citation is not None:
        for match in _CITATION_URL.finditer(citation):
            found |= _slugs_of(match.group(1))
    return found


def contact_emails(root: Path) -> dict[str, list[str]]:
    """Os e-mails de cada arquivo de contato presente em `root`, na ordem do texto."""
    pattern = load_ops("ops/export/repo_scan.py").DETECTORS["email"].pattern
    found: dict[str, list[str]] = {}
    for rel in CONTACT_FILES:
        text = _read(root, rel)
        if text is not None:
            found[rel] = [match.group(0) for match in pattern.finditer(text)]
    return found
