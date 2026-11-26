"""Banco de questões local, do lado do cliente: a leitura da pasta ou de um commit de um git local, as regras
do pacote, a versão pelo conteúdo e o `.tar.gz` determinístico que o `cq` e o servidor MCP enviam.

Partes:
- os tipos (`BankEntry`, `LocalBank`, `BankReadError`, `GitRun`) e as constantes: os tetos (os mesmos da api),
  o prefixo do manifesto (`VERSION_PREFIX`), o padrão único da revisão do git (`REVISION_PATTERN`, o mesmo do
  `git_rev` da ferramenta do MCP), o conjunto fechado dos motivos locais (`LOCAL_REASONS`) e a mensagem fixa
  de cada um (`LOCAL_MESSAGES`);
- `read_folder(path)`: a pasta, sem seguir links (links, junções, pontos de reanálise e arquivos especiais
  recusados, também na raiz; `.git` fora em qualquer nível e caixa; caminho UNC recusado; diretórios vazios
  fora). Contra a troca no meio da leitura, cada pasta só é listada conferida contra a identidade (dispositivo e
  nó) da classificação, por descritor onde o `scandir` aceita um e, no Windows, pelo `lstat` antes e depois da
  listagem; cada arquivo só é lido com a identidade da varredura e, no Windows, com a localização provada pelo
  próprio handle aberto (o caminho final dele é o da raiz, também obtido por handle, seguido do caminho relativo);
- `read_git(path, revision, runner)`: a árvore de um commit, pelo executor do `git` dado (`rev-parse`,
  `ls-tree` e um único `cat-file --batch`, sem filtros), só a subpasta de `path`;
- `bank_version(entries)`: a versão, `sha256(VERSION_PREFIX + Σ caminho \\0 tamanho \\0 sha256 \\n)` em ordem
  crescente dos bytes UTF-8 do caminho (a mesma definição do servidor, implementada de novo aqui: o cliente
  não importa o servidor);
- `build_archive(entries)`: o pacote reprodutível (pax em ordem dos bytes do caminho, horários e donos zerados,
  modo 0644, sem entradas de diretório; gzip sem horário nem nome de arquivo).

O caminho local da pasta nunca entra numa mensagem nem num pedido à API. As mensagens fixas não levam o
caminho: o CLI acrescenta o caminho relativo **dentro** do banco, neutralizado; o servidor MCP, nunca.

Sem `subprocess` nem `click`: o `git` roda pelo executor injetado (`cq_client.stack_compose.GitRunner`). O
`ctypes` e o `msvcrt` só são importados no Windows, na leitura da pasta.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import stat
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from cq_client.stack import RunResult

# Os tetos (os mesmos que a api aplica; o `cq` confere antes de enviar).
MAX_FILES = 20000
MAX_CONTENT_BYTES = 104857600  # 100 MiB: a soma dos tamanhos dos arquivos
MAX_ARCHIVE_BYTES = 134217728  # 128 MiB: o pacote `.tar.gz`

# O prefixo do manifesto da versão.
VERSION_PREFIX = b"cq-bank-v1\n"

# A revisão do `--git` (e do `git_rev` do MCP): o mesmo padrão do esquema da ferramenta, conferido com
# `fullmatch`, e de 1 a `REVISION_MAX_LENGTH` caracteres. Nunca vira opção do `git` nem caminho (sem `:`).
REVISION_PATTERN = re.compile("^[A-Za-z0-9_./@{}~^][A-Za-z0-9_./@{}~^-]*$")
REVISION_MAX_LENGTH = 256

# Os motivos locais de recusa, um conjunto fechado, cada um com a mensagem fixa em português.
LOCAL_MESSAGES: Mapping[str, str] = {
    "not_absolute": "Informe o caminho absoluto da pasta do banco.",
    "not_found": "A pasta do banco não foi encontrada.",
    "not_a_directory": "O caminho do banco não é uma pasta.",
    "unc_path": "Caminho de rede (UNC) ou de dispositivo não é aceito como pasta do banco.",
    "symlink": "O banco tem um link simbólico; links não são aceitos.",
    "junction": "O banco tem uma junção ou outro ponto de reanálise do Windows; não são aceitos.",
    "special_file": "O banco tem um arquivo especial (dispositivo, fila ou soquete); só arquivos comuns são aceitos.",
    "unreadable": "Um arquivo do banco não pôde ser lido.",
    "path_not_allowed": ("Um caminho do banco está fora da regra (componente vazio, \".\" ou \"..\", barra invertida, "
                         "caractere de controle, nome fora de UTF-8 ou longo demais, ou um arquivo com o nome de "
                         "uma pasta)."),
    "not_a_course": "A pasta não é um curso do PrairieLearn: falta o infoCourse.json na raiz.",
    "too_many_files": "O banco passa de 20000 arquivos.",
    "too_large": "O conteúdo do banco passa de 100 MiB.",
    "archive_too_large": "O pacote do banco passa de 128 MiB.",
    "git_missing": "O git não foi encontrado no PATH (só nas entradas absolutas, fora da pasta do banco).",
    "not_a_repository": "A pasta não está dentro de um repositório git.",
    "revision_not_found": "A revisão não existe no repositório.",
    "revision_invalid": ("A revisão está fora da forma aceita: letras, dígitos e os sinais _ . / @ { } ~ ^ -, "
                         "sem começar por hífen, até 256 caracteres."),
    "not_in_revision": "A pasta não existe nessa revisão, ou é um arquivo nela.",
    "git_symlink": "A revisão tem um link simbólico; links não são aceitos.",
    "git_submodule": "A revisão tem um submódulo; submódulos não são aceitos.",
    "git_failed": "O git falhou ao ler o repositório.",
}
LOCAL_REASONS = frozenset(LOCAL_MESSAGES)


@dataclass(frozen=True)
class BankEntry:
    """Um arquivo regular do banco: o caminho relativo (UTF-8, `/`), o tamanho, o SHA-256 hex e o conteúdo
    (fora do `repr`)."""

    path: str
    size: int
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class LocalBank:
    """O banco lido: as entradas, em ordem dos bytes do caminho, e o commit de origem (só na leitura do git)."""

    entries: tuple[BankEntry, ...]
    git_commit: str | None


class BankReadError(Exception):
    """A recusa local: `reason` em `LOCAL_REASONS` e, quando houver, o caminho relativo dentro do banco (fora
    do `repr` e da mensagem da exceção). Nunca leva o caminho local da pasta."""

    def __init__(self, reason: str, relative_path: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.relative_path = relative_path

    def __repr__(self) -> str:
        return f"BankReadError(reason={self.reason!r})"


class GitRun(Protocol):
    """O executor do `git` que `read_git` usa (o real é `cq_client.stack_compose.GitRunner`)."""

    def run(self, args: Sequence[str], *, repo: str, input: bytes | None,
            timeout: float | None) -> RunResult: ...


# --------------------------------------------------------------------------------------------------
# A regra de caminho (a mesma da api): os componentes e o caminho inteiro
# --------------------------------------------------------------------------------------------------

MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 4096

# O arquivo que faz da raiz um curso do PrairieLearn (a caixa exata).
INFO_COURSE = "infoCourse.json"

# **[I]** Os prazos das chamadas ao `git`: as consultas respondem em segundos; o `cat-file --batch` entrega
# até o teto do conteúdo de uma vez.
GIT_QUERY_TIMEOUT = 60.0
GIT_BATCH_TIMEOUT = 600.0

# A abertura só de leitura, em binário (sem `O_BINARY`, no Windows o CRLF vira LF e a leitura para no `0x1A`),
# sem bloquear (uma fila nomeada trocada no meio do caminho não trava a leitura) e sem seguir link onde existe.
_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_NOFOLLOW", 0))
_READ_CHUNK = 1024 * 1024
_WINDOWS = os.name == "nt"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HEX_COMMIT = re.compile("^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEX_OID = _HEX_COMMIT

# Os modos do `ls-tree`: os arquivos regulares, o link e o submódulo.
_GIT_REGULAR_MODES = frozenset({b"100644", b"100755"})
_GIT_SYMLINK_MODE = b"120000"
_GIT_SUBMODULE_MODE = b"160000"


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _path_allowed(path: str) -> bool:
    """O caminho relativo (já em UTF-8 estrito) segue a regra: componentes separados por `/`, não vazios,
    diferentes de `.` e `..`, com até 255 bytes, sem `\\` nem caractere de controle; até 4096 bytes no todo."""
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return False
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return False
        if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES:
            return False
        if "\\" in part or any(_is_control(char) for char in part):
            return False
    return True


def _is_git_component(part: str) -> bool:
    return part.lower() == ".git"


def _utf8(text: str) -> bytes | None:
    """Os bytes UTF-8 estritos do texto, ou `None` (um substituto solto, vindo do sistema de arquivos)."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _shown(text: str) -> str:
    """O caminho relativo que não codifica em UTF-8, com os caracteres inválidos trocados por `?` (só para a
    mensagem do CLI)."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _entry(path: str, data: bytes) -> BankEntry:
    return BankEntry(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data)


def _sorted(entries: Iterable[BankEntry]) -> tuple[BankEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: entry.path.encode("utf-8")))


# --------------------------------------------------------------------------------------------------
# A raiz
# --------------------------------------------------------------------------------------------------


def _check_root(path: str) -> os.stat_result:
    """A raiz do banco: um texto de caminho absoluto, local (no Windows, nem rede nem dispositivo, recusados só
    pelo texto, antes de qualquer acesso) e de uma pasta que não é link, junção nem ponto de reanálise. Devolve o
    `lstat` da raiz: a identidade dela, conferida de novo antes da listagem."""
    if not isinstance(path, str):
        raise BankReadError("not_absolute")
    if _WINDOWS and len(path) >= 2 and path[0] in "\\/" and path[1] in "\\/":
        # Duas barras no início: UNC (`\\host\...`) ou dispositivo (`\\?\...`, `\\.\...`). O acesso a um UNC
        # abriria uma conexão de rede, que pode entregar a credencial do operador.
        raise BankReadError("unc_path")
    if not Path(path).is_absolute():
        raise BankReadError("not_absolute")
    info = _lstat(path)
    if info is None:
        raise BankReadError("not_found")
    reason = _link_reason(info)
    if reason is not None:
        raise BankReadError(reason)
    if not stat.S_ISDIR(info.st_mode):
        raise BankReadError("not_a_directory")
    return info


def _lstat(path: str) -> os.stat_result | None:
    """O `lstat`, ou `None` na falha (a exceção, cujo `filename` guarda o caminho local, é descartada)."""
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def _link_reason(info: os.stat_result) -> str | None:
    """`symlink` para o link simbólico, `junction` para a junção e qualquer outro ponto de reanálise do
    Windows; `None` para o resto."""
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return "junction"
    return None


# --------------------------------------------------------------------------------------------------
# A pasta
# --------------------------------------------------------------------------------------------------


# A identidade de um objeto do sistema de arquivos: o dispositivo e o número do nó (no Windows, o número de série
# do volume e o índice do arquivo).
_Identity = tuple[int, int]

# Uma entrada classificada: o nome, o caminho local, o tipo e, para `dir` e `file`, o `stat` com a identidade.
_Child = tuple[str, str, str, os.stat_result | None]

# A listagem de uma pasta por descritor, onde o `scandir` aceita um (POSIX): a abertura só de leitura, só de pasta e
# sem seguir link; o `fstat` do descritor confere a pasta, e a listagem é a do objeto conferido. Onde não aceita
# (Windows), a pasta é conferida pelo `lstat` antes e depois da listagem, e a localização de cada arquivo é provada
# na leitura, pelo handle aberto (ver `_located`).
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_LIST_BY_FD = os.scandir in os.supports_fd and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


def _identity(info: os.stat_result) -> _Identity:
    return (info.st_dev, info.st_ino)


def _same_folder(info: os.stat_result | None, expected: _Identity) -> bool:
    """`info` é o da pasta da classificação: uma pasta, sem link nem ponto de reanálise, com a mesma identidade."""
    return (info is not None and _link_reason(info) is None and stat.S_ISDIR(info.st_mode)
            and _identity(info) == expected)


def _folder_refusal(folder: str, relative: str | None) -> BankReadError:
    """A recusa da pasta que não pôde ser listada como o objeto da classificação: `symlink` ou `junction` se agora
    é link, junção ou outro ponto de reanálise; `unreadable` no resto (outro objeto no lugar, ou a abertura ou a
    listagem falhou)."""
    info = _lstat(folder)
    reason = _link_reason(info) if info is not None else None
    return BankReadError(reason or "unreadable", relative)


def _classify(child: os.DirEntry[str], local: str) -> tuple[str, os.stat_result | None]:
    """O tipo da entrada, sem seguir links (`dir`, `file`, `symlink`, `junction`, também outro ponto de
    reanálise, `special_file` ou `unreadable`) e, para `dir` e `file`, o `stat` com a identidade. Na listagem por
    descritor, o `stat` da entrada já é relativo ao descritor da pasta conferida. Na outra (no Windows, o `stat` da
    entrada vem com a identidade zerada), é o `lstat` do caminho, feito antes da segunda conferência da pasta e só
    aceito sem link e com o mesmo tipo."""
    try:
        if child.is_symlink():
            return "symlink", None
        if child.is_junction():
            return "junction", None
        info = child.stat(follow_symlinks=False)
    except OSError:
        return "unreadable", None
    reason = _link_reason(info)
    if reason is not None:
        return reason, None
    if stat.S_ISDIR(info.st_mode):
        kind = "dir"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        return "special_file", None
    if _LIST_BY_FD:
        return kind, info
    named = _lstat(local)
    if named is None:
        return "unreadable", None
    reason = _link_reason(named)
    if reason is not None:
        return reason, None
    if stat.S_IFMT(named.st_mode) != stat.S_IFMT(info.st_mode):
        return "unreadable", None
    return kind, named


def _classified(folder: str, children: list[os.DirEntry[str]]) -> list[_Child]:
    """As entradas da listagem, sem os componentes `.git`, em ordem dos nomes, cada uma classificada."""
    kept = [child for child in children if not _is_git_component(child.name)]
    kept.sort(key=lambda child: child.name.encode("utf-8", errors="surrogatepass"))
    result: list[_Child] = []
    for child in kept:
        # O caminho montado aqui: na listagem por descritor, o `path` da entrada é só o nome.
        local = os.path.join(folder, child.name)
        kind, info = _classify(child, local)
        result.append((child.name, local, kind, info))
    return result


def _list_by_descriptor(folder: str, expected: _Identity) -> list[_Child] | None:
    fd: int | None
    try:
        fd = os.open(folder, _DIR_FLAGS)
    except (OSError, ValueError):
        fd = None
    if fd is None:
        return None
    try:
        opened: os.stat_result | None
        try:
            opened = os.fstat(fd)
        except OSError:
            opened = None
        if not _same_folder(opened, expected):
            return None
        children: list[os.DirEntry[str]] | None
        try:
            with os.scandir(fd) as listing:
                children = list(listing)
        except OSError:
            children = None
        if children is None:
            return None
        # Classificadas com o descritor ainda aberto: o `stat` de cada entrada é relativo a ele.
        return _classified(folder, children)
    finally:
        os.close(fd)


def _list_by_path(folder: str, expected: _Identity) -> list[_Child] | None:
    if not _same_folder(_lstat(folder), expected):
        return None
    children: list[os.DirEntry[str]] | None
    try:
        with os.scandir(folder) as listing:
            children = list(listing)
    except (OSError, ValueError):
        children = None
    if children is None:
        return None
    classified = _classified(folder, children)
    # A segunda conferência, depois da listagem e dos `lstat` das entradas: a pasta trocada no meio é recusada.
    if not _same_folder(_lstat(folder), expected):
        return None
    return classified


def _list_folder(folder: str, expected: _Identity) -> list[_Child] | None:
    """As entradas classificadas da pasta (ver `_classified`), ou `None` se ela não é mais o objeto da
    classificação (a mesma identidade, sem link nem ponto de reanálise) ou não pôde ser listada."""
    if _LIST_BY_FD:
        return _list_by_descriptor(folder, expected)
    return _list_by_path(folder, expected)


def _scan(root: str, root_info: os.stat_result) -> list[tuple[str, str, int, _Identity]]:
    """A varredura sem seguir links: os arquivos regulares como `(caminho relativo, caminho local, tamanho,
    identidade)`, com cada recusa de tipo, de nome e de teto na ordem em que aparece (cada pasta em ordem dos
    nomes). Cada pasta só é listada conferida contra a identidade da classificação (a raiz, contra o `lstat` de
    `_check_root`): a pasta trocada por um link, uma junção ou outra pasta no meio da leitura é recusada."""
    found: list[tuple[str, str, int, _Identity]] = []
    total = 0
    pending: list[tuple[str, str, _Identity]] = [(root, "", _identity(root_info))]
    while pending:
        folder, prefix, expected = pending.pop()
        children = _list_folder(folder, expected)
        if children is None:
            raise _folder_refusal(folder, _shown(prefix.rstrip("/")) or None)
        subfolders: list[tuple[str, str, _Identity]] = []
        for name, local, kind, info in children:
            relative = prefix + name
            if info is None:  # `symlink`, `junction`, `special_file` ou `unreadable`
                raise BankReadError(kind, _shown(relative))
            if kind == "dir":
                subfolders.append((local, relative + "/", _identity(info)))
                continue
            if _utf8(relative) is None:
                raise BankReadError("path_not_allowed", _shown(relative))
            if not _path_allowed(relative):
                raise BankReadError("path_not_allowed", relative)
            if len(found) + 1 > MAX_FILES:
                raise BankReadError("too_many_files")
            total += info.st_size
            if total > MAX_CONTENT_BYTES:
                raise BankReadError("too_large")
            found.append((relative, local, info.st_size, _identity(info)))
        # A pilha em ordem inversa: a primeira subpasta em ordem dos nomes sai primeiro.
        pending.extend(reversed(subfolders))
    return found


# --------------------------------------------------------------------------------------------------
# A localização pelo objeto aberto (Windows)
# --------------------------------------------------------------------------------------------------

# No Windows, a pasta não é listada por descritor, e o `lstat` de uma entrada, pelo caminho, atravessa uma junção
# posta num componente acima dela. A identidade da varredura só prova que o mesmo objeto foi visto duas vezes pelo
# mesmo caminho, não que ele está dentro da raiz: uma pasta trocada por uma junção e devolvida dentro da janela da
# listagem passa pelas duas conferências dela. Por isso, cada arquivo aberto tem a localização provada pelo próprio
# handle: o caminho final dele (`GetFinalPathNameByHandleW`, que resolve junções e links) tem de ser o caminho
# final da raiz, obtido também por um handle aberto nela, seguido do caminho relativo. Qualquer falha da API recusa.
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004  # leitura, escrita e remoção: o handle da raiz não trava ninguém
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # o que permite abrir uma pasta
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # abre o próprio ponto de reanálise, sem segui-lo
_FILE_NAME_NORMALIZED = 0x0
_VOLUME_NAME_DOS = 0x0
_FINAL_PATH_FLAGS = _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS
# O tamanho inicial do buffer do caminho final e o teto, em caracteres, com o nulo final (o maior caminho do Windows
# tem 32767 caracteres); um caminho final maior que o teto é recusado.
_FINAL_PATH_START = 512
_FINAL_PATH_MAX = 32768


@dataclass(frozen=True)
class _Anchor:
    """A raiz no Windows: o caminho final dela, sem a barra final (`\\\\?\\C:\\...` ou `\\\\?\\UNC\\...`), e o
    volume (`st_dev`)."""

    final: str
    device: int


@cache
def _kernel32() -> Any:
    """O `kernel32`, numa instância só deste módulo, com as assinaturas das três chamadas usadas (só no Windows)."""
    import ctypes
    from ctypes import wintypes

    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    library.CreateFileW.restype = wintypes.HANDLE
    library.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    library.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    return library


def _final_path(handle: int) -> str | None:
    """O caminho final do objeto aberto em `handle`, normalizado e com o nome DOS do volume (o prefixo `\\\\?\\`,
    ou `\\\\?\\UNC\\` num volume de rede), ou `None` na falha."""
    import ctypes

    library = _kernel32()
    size = _FINAL_PATH_START
    for _ in range(2):  # a segunda volta só com o tamanho que a primeira pediu
        buffer = ctypes.create_unicode_buffer(size)
        length = library.GetFinalPathNameByHandleW(handle, buffer, size, _FINAL_PATH_FLAGS)
        if length == 0:
            return None
        if length < size:
            return buffer[:length]
        if length > _FINAL_PATH_MAX:
            return None
        size = length  # o tamanho pedido já conta o nulo final
    return None


def _root_anchor(path: str, info: os.stat_result) -> _Anchor | None:
    """O caminho final da raiz, por um handle aberto nela sem seguir ponto de reanálise e conferido contra o `lstat`
    de `_check_root` (a mesma pasta, sem link nem ponto de reanálise), e o volume; `None` em qualquer falha. O
    handle é fechado antes da volta."""
    import ctypes
    import msvcrt

    if "\0" in path:
        return None
    library = _kernel32()
    handle = library.CreateFileW(path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                                 _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        library.CloseHandle(handle)
        return None
    # Daqui em diante, o descritor é o dono do handle: o `os.close` fecha os dois.
    try:
        opened: os.stat_result | None
        try:
            opened = os.fstat(fd)
        except OSError:
            opened = None
        final = _final_path(handle) if _same_folder(opened, _identity(info)) else None
    finally:
        os.close(fd)
    if final is None:
        return None
    return _Anchor(final=final.rstrip("\\"), device=info.st_dev)


def _located(fd: int, relative: str, anchor: _Anchor) -> bool:
    """O arquivo aberto em `fd` está onde a varredura o viu: o caminho final do handle é o da raiz seguido de
    `relative`. O prefixo da raiz é comparado exatamente (vem da mesma chamada, para o mesmo volume); o resto, sem
    caixa (`normcase`) e com os separadores do Windows, fica todo abaixo da raiz."""
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return False
    final = _final_path(handle)
    if final is None:
        return False
    head = anchor.final + "\\"
    return final.startswith(head) and os.path.normcase(final[len(head):]) == os.path.normcase(relative)


def _read_file(local: str, relative: str, budget: int, expected: _Identity, anchor: _Anchor | None = None) -> bytes:
    """O conteúdo do arquivo: o `lstat`, a abertura única só de leitura, o `fstat` do descritor e a leitura de até
    `budget + 1` bytes. O `lstat` e o `fstat` têm de dar a identidade `expected`, a da varredura: outro objeto no
    caminho (o arquivo trocado, ou uma pasta acima dele trocada por um link depois da varredura) é recusado sem
    leitura, e sem abertura quando o `lstat` já mostra a troca. Com a `anchor` (no Windows), o arquivo aberto
    também tem de estar no volume da raiz e no lugar esperado (ver `_located`), conferido antes de ler qualquer byte.
    As exceções do sistema (cujo `filename` guarda o caminho local) são descartadas, e as recusas saem fora do
    `except`."""
    before = _lstat(local)
    if before is None:
        raise BankReadError("unreadable", relative)
    reason = _link_reason(before)
    if reason is not None:
        raise BankReadError(reason, relative)
    if not stat.S_ISREG(before.st_mode):
        raise BankReadError("special_file", relative)
    if _identity(before) != expected:
        raise BankReadError("unreadable", relative)
    fd: int | None
    try:
        fd = os.open(local, _OPEN_FLAGS)
    except (OSError, ValueError):
        fd = None
    if fd is None:
        raise BankReadError("unreadable", relative)
    failed = False
    chunks: list[bytes] = []
    total = 0
    try:
        opened: os.stat_result | None
        try:
            opened = os.fstat(fd)
        except OSError:
            opened = None
        if opened is None or not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            failed = True
        elif anchor is not None and (opened.st_dev != anchor.device or not _located(fd, relative, anchor)):
            failed = True
        else:
            limit = budget + 1
            try:
                while total < limit:
                    chunk = os.read(fd, min(_READ_CHUNK, limit - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            except OSError:
                failed = True
    finally:
        os.close(fd)
    if failed:
        raise BankReadError("unreadable", relative)
    if total > budget:
        raise BankReadError("too_large")
    return b"".join(chunks)


def read_folder(path: str) -> LocalBank:
    """Lê a pasta de caminho absoluto `path` (ver o docstring do módulo); `BankReadError` na primeira recusa."""
    root_info = _check_root(path)
    anchor: _Anchor | None = None
    if _WINDOWS:
        anchor = _root_anchor(path, root_info)
        if anchor is None:
            raise _folder_refusal(path, None)
    found = _scan(path, root_info)
    if not any(relative == INFO_COURSE for relative, _, _, _ in found):
        raise BankReadError("not_a_course")
    entries: list[BankEntry] = []
    used = 0
    for relative, local, _, identity in found:
        data = _read_file(local, relative, MAX_CONTENT_BYTES - used, identity, anchor)
        used += len(data)
        entries.append(_entry(relative, data))
    return LocalBank(entries=_sorted(entries), git_commit=None)


# --------------------------------------------------------------------------------------------------
# O commit de um git local
# --------------------------------------------------------------------------------------------------


def _git(runner: GitRun, args: Sequence[str], repo: str, *, input: bytes | None = None,
         timeout: float = GIT_QUERY_TIMEOUT) -> RunResult:
    """Uma chamada ao executor. Sem o `git`, `git_missing`; qualquer outra falha do executor (o prazo esgotado,
    um erro do sistema), `git_failed`. A exceção do executor (que pode citar o argv, com o caminho local) é
    descartada, e a recusa sai fora do `except`."""
    from cq_client.stack_compose import GitUnavailable  # o executor real vive lá; importado só aqui

    reason: str | None = None
    result: RunResult | None = None
    try:
        result = runner.run(list(args), repo=repo, input=input, timeout=timeout)
    except GitUnavailable:
        reason = "git_missing"
    except Exception:  # noqa: BLE001 - o texto da exceção nunca sai
        reason = "git_failed"
    if reason is not None:
        raise BankReadError(reason)
    assert result is not None
    return result


def _line(stdout: bytes) -> str | None:
    """A única linha da saída, sem o fim de linha; `None` fora do ASCII."""
    text = stdout[:-1] if stdout.endswith(b"\n") else stdout
    try:
        return text.decode("ascii")
    except UnicodeDecodeError:
        return None


def _tree_listing(stdout: bytes) -> list[tuple[bytes, bytes, bytes, str, bytes]]:
    """Os registros do `ls-tree -r -z -l`: `(modo, tipo, oid, tamanho, caminho em bytes)`."""
    records: list[tuple[bytes, bytes, bytes, str, bytes]] = []
    for record in stdout.split(b"\0"):
        if not record:
            continue
        meta, tab, raw = record.partition(b"\t")
        fields = meta.split()
        if not tab or len(fields) != 4 or not raw:
            raise BankReadError("git_failed")
        mode, kind, oid, size = fields
        records.append((mode, kind, oid, size.decode("ascii", errors="replace"), raw))
    return records


def _batch_contents(stdout: bytes, oids: Sequence[str]) -> dict[str, bytes]:
    """O conteúdo de cada blob da saída do `cat-file --batch`, na ordem pedida (`<oid> blob <tamanho>\\n` e os
    bytes, seguidos de `\\n`)."""
    contents: dict[str, bytes] = {}
    position = 0
    for oid in oids:
        end = stdout.find(b"\n", position)
        if end < 0:
            raise BankReadError("git_failed")
        header = stdout[position:end].split(b" ")
        if len(header) != 3 or header[0].decode("ascii", errors="replace") != oid or header[1] != b"blob":
            raise BankReadError("git_failed")
        if not header[2].isdigit():
            raise BankReadError("git_failed")
        size = int(header[2])
        start = end + 1
        stop = start + size
        if stop + 1 > len(stdout) or stdout[stop:stop + 1] != b"\n":
            raise BankReadError("git_failed")
        contents[oid] = stdout[start:stop]
        position = stop + 1
    if position != len(stdout):
        raise BankReadError("git_failed")
    return contents


def read_git(path: str, revision: str, runner: GitRun) -> LocalBank:
    """Lê, no commit de `revision`, a árvore da subpasta `path` de um repositório git local, pelo `runner`;
    `BankReadError` na primeira recusa (a revisão fora de `REVISION_PATTERN` antes de qualquer chamada)."""
    if (not isinstance(revision, str) or not 1 <= len(revision) <= REVISION_MAX_LENGTH
            or REVISION_PATTERN.fullmatch(revision) is None):
        raise BankReadError("revision_invalid")
    _check_root(path)

    # O prefixo da pasta dentro da árvore de trabalho (vazio na raiz, com a barra final numa subpasta); fora de um
    # repositório, o `git` sai com código diferente de zero.
    shown = _git(runner, ["rev-parse", "--show-prefix"], path)
    if shown.returncode != 0:
        raise BankReadError("not_a_repository")
    prefix_raw = shown.stdout[:-1] if shown.stdout.endswith(b"\n") else shown.stdout
    try:
        prefix = prefix_raw.decode("utf-8")
    except UnicodeDecodeError:
        prefix = None
    if prefix is None or "\n" in prefix:
        raise BankReadError("git_failed")

    # A revisão resolvida para um commit (uma etiqueta anotada dá o commit dela, nunca o objeto da etiqueta).
    resolved = _git(runner, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], path)
    commit = _line(resolved.stdout) if resolved.returncode == 0 else None
    if resolved.returncode != 0:
        raise BankReadError("revision_not_found")
    if commit is None or _HEX_COMMIT.fullmatch(commit) is None:
        raise BankReadError("git_failed")

    # A árvore da subpasta no commit: ausente, ou arquivo nele, é a mesma recusa.
    located = _git(runner, ["rev-parse", "--verify", "--quiet", f"{commit}:{prefix}"], path)
    tree = _line(located.stdout) if located.returncode == 0 else None
    if tree is None or _HEX_OID.fullmatch(tree) is None:
        raise BankReadError("not_in_revision")
    typed = _git(runner, ["cat-file", "-t", tree], path)
    if typed.returncode != 0 or _line(typed.stdout) != "tree":
        raise BankReadError("not_in_revision")

    # A listagem da árvore inteira dessa subpasta (`--full-tree`: rodado de uma subpasta, o `ls-tree` filtraria
    # pelo prefixo dela e não listaria nada da árvore pedida).
    listed = _git(runner, ["ls-tree", "-r", "-z", "-l", "--full-tree", tree], path)
    if listed.returncode != 0:
        raise BankReadError("git_failed")
    files: list[tuple[str, str]] = []
    total = 0
    for mode, kind, oid_raw, size_text, raw in _tree_listing(listed.stdout):
        try:
            relative: str | None = raw.decode("utf-8")
        except UnicodeDecodeError:
            relative = None
        shown_path = relative if relative is not None else raw.decode("utf-8", errors="replace")
        if any(_is_git_component(part) for part in shown_path.split("/")):
            continue
        if mode == _GIT_SYMLINK_MODE:
            raise BankReadError("git_symlink", shown_path)
        if mode == _GIT_SUBMODULE_MODE:
            raise BankReadError("git_submodule", shown_path)
        if mode not in _GIT_REGULAR_MODES or kind != b"blob":
            raise BankReadError("git_failed")
        if relative is None or not _path_allowed(relative):
            raise BankReadError("path_not_allowed", shown_path)
        oid = oid_raw.decode("ascii", errors="replace")
        if _HEX_OID.fullmatch(oid) is None or not size_text.isdigit():
            raise BankReadError("git_failed")
        if len(files) + 1 > MAX_FILES:
            raise BankReadError("too_many_files")
        total += int(size_text)
        if total > MAX_CONTENT_BYTES:
            raise BankReadError("too_large")
        files.append((relative, oid))
    if not any(relative == INFO_COURSE for relative, _ in files):
        raise BankReadError("not_a_course")

    # Um único `cat-file --batch`, com cada objeto pedido uma vez: os bytes guardados, sem filtros nem atributos.
    unique = list(dict.fromkeys(oid for _, oid in files))
    contents: dict[str, bytes] = {}
    if unique:
        request = "".join(f"{oid}\n" for oid in unique).encode("ascii")
        batch = _git(runner, ["cat-file", "--batch"], path, input=request, timeout=GIT_BATCH_TIMEOUT)
        if batch.returncode != 0:
            raise BankReadError("git_failed")
        contents = _batch_contents(batch.stdout, unique)
    used = sum(len(contents[oid]) for _, oid in files)
    if used > MAX_CONTENT_BYTES:
        raise BankReadError("too_large")
    return LocalBank(entries=_sorted(_entry(relative, contents[oid]) for relative, oid in files), git_commit=commit)


# --------------------------------------------------------------------------------------------------
# A versão e o pacote
# --------------------------------------------------------------------------------------------------


def bank_version(entries: Iterable[BankEntry]) -> str:
    """A versão do banco: o SHA-256 hex (minúsculo) do manifesto das entradas, em ordem dos bytes do caminho."""
    digest = hashlib.sha256(VERSION_PREFIX)
    for entry in sorted(entries, key=lambda item: item.path.encode("utf-8")):
        digest.update(entry.path.encode("utf-8") + b"\0" + str(entry.size).encode("ascii") + b"\0"
                      + entry.sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


# **[I]** O nível de compressão do pacote (um banco real comprime em segundos).
ARCHIVE_COMPRESSLEVEL = 6


def build_archive(entries: Iterable[BankEntry]) -> bytes:
    """O pacote `.tar.gz` reprodutível das entradas (os mesmos bytes para o mesmo conteúdo)."""
    buffer = io.BytesIO()
    # `filename=""`: nenhum nome de arquivo no cabeçalho do gzip; `mtime=0`: nenhum horário.
    with gzip.GzipFile(filename="", mode="wb", compresslevel=ARCHIVE_COMPRESSLEVEL, fileobj=buffer,
                       mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as tar:
            for entry in _sorted(entries):
                info = tarfile.TarInfo(entry.path)
                info.type = tarfile.REGTYPE
                info.size = len(entry.data)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(entry.data))
    archive = buffer.getvalue()
    # O teto é lido do módulo na hora da chamada.
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise BankReadError("archive_too_large")
    return archive
