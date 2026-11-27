"""Banco de questões local: as regras do pacote, a leitura do `.tar.gz`, a versão pelo conteúdo, a
gravação, a listagem, a carga do pacote guardado e o roteador da obtenção.

O operador envia uma pasta de curso do PrairieLearn (ou um commit de um git local, lido pelo cliente) como
um pacote `.tar.gz`; o servidor o guarda, imutável, na tabela `question_banks`, identificado pelo nome e
pela versão (o SHA-256 do manifesto do conteúdo). Uma referência de questão `bank:<nome>` com a versão no
papel do commit é publicada pelo worker no mesmo lugar em que o motor procura qualquer referência.

Partes:
- os tipos (`BankEntry`, `ArchiveSummary`, `BankVersion`, `BanksPage`, `BankMissing`, `BankArchiveError`) e as
  constantes: o padrão do nome (`BANK_NAME`), os tetos fixos, as chaves aceitas do cabeçalho estendido
  (`PAX_KEYS`), o prefixo do manifesto (`VERSION_PREFIX`) e o conjunto fechado das recusas
  (`ARCHIVE_ERRORS`, pares campo × motivo do envelope de erro);
- `read_archive(data, *, max_files, max_bytes, open_member=None)`: a leitura do pacote em fluxo, por um leitor
  tar mínimo próprio (ustar e pax, sem o `tarfile`) sobre o `zlib`, que para na primeira recusa e só levanta
  `BankArchiveError`, sem `__cause__` nem `__context__`;
- `bank_version(entries)`: a versão, `sha256(VERSION_PREFIX + Σ caminho \\0 tamanho \\0 sha256 \\n)` em ordem
  crescente dos bytes UTF-8 do caminho;
- `ensure_bank`, `list_banks`, `load_archive`: a tabela `question_banks`, na transação do chamador;
- `RoutingRepoFetcher(git, bank)`: manda as referências `bank:` ao lado do banco e as demais ao do git.

Núcleo: sem `git`, `subprocess`, `socket`, HTTP, `tarfile` nem os adaptadores.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, BinaryIO, Literal

from cq_server.events import EventType, append_event, load_write_identities
from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure, RepoFetcher
from cq_server.redaction import redact_operator_text

if TYPE_CHECKING:
    import psycopg

    from cq_server.redaction import KnownIdentity

# O nome do banco: o mesmo padrão do código do oferecimento (texto inteiro).
BANK_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]{2,39}\Z")

# O prefixo da referência de questão de um banco enviado (`bank:<nome>`).
BANK_REPO_PREFIX = "bank:"

# Os tetos fixos da api (o worker confere de novo os arquivos e o conteúdo com os tetos dele).
MAX_FILES = 20000
MAX_CONTENT_BYTES = 104857600  # 100 MiB: a soma dos tamanhos dos arquivos
MAX_ARCHIVE_BYTES = 134217728  # 128 MiB: o pacote `.tar.gz`, depois do base64
MAX_BODY_BYTES = 188743680  # 180 MiB: o corpo JSON do pedido de envio
MAX_TAR_STREAM_BYTES = 309667840  # o conteúdo mais 10 KiB por membro (cabeçalhos e preenchimento)
MAX_PATH_BYTES = 4096  # o caminho inteiro, em bytes UTF-8
MAX_COMPONENT_BYTES = 255  # cada componente do caminho, em bytes UTF-8
MAX_EXTENDED_HEADER_BYTES = 8192  # cada cabeçalho estendido pax (`x`), conferido antes de lê-lo

# As únicas chaves aceitas num cabeçalho estendido pax (só `path` e `size` são usadas).
PAX_KEYS = frozenset({"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname"})

# O prefixo do manifesto da versão.
VERSION_PREFIX = b"cq-bank-v1\n"

# As origens de um banco enviado.
SOURCES = ("folder", "git")

# As recusas do envio, como `(campo, motivo)` de um `invalid_request` com um único campo.
ARCHIVE_ERRORS = frozenset({
    ("body", "too_long"),  # o corpo acima do teto
    ("archive", "invalid_format"),  # o base64 inválido, ou o pacote malformado
    ("archive", "too_long"),  # o pacote decodificado acima do teto
    ("archive", "too_large"),  # o fluxo descomprimido, um cabeçalho estendido ou o conteúdo acima do teto
    ("archive", "too_many"),  # arquivos demais
    ("archive", "entry_not_allowed"),  # membro que não é arquivo regular, ou chave pax fora da lista
    ("archive", "path_not_allowed"),  # caminho fora da regra
    ("archive", "duplicate"),  # caminho repetido
    ("archive", "not_a_course"),  # sem `infoCourse.json` na raiz
    ("version", "mismatch"),  # a versão recalculada difere da declarada
})


@dataclass(frozen=True)
class BankEntry:
    """Um arquivo regular do banco: o caminho relativo (UTF-8, `/`), o tamanho e o SHA-256 hex do conteúdo."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArchiveSummary:
    """O pacote lido: as entradas, na ordem do pacote, e as contagens (arquivos, bytes do conteúdo e questões)."""

    entries: tuple[BankEntry, ...]
    file_count: int
    byte_count: int
    question_count: int


@dataclass(frozen=True)
class BankVersion:
    """Uma versão guardada de um banco, como sai na resposta (o nome e o `repo` já redigidos, se for o caso)."""

    name: str
    version: str
    repo: str
    source: Literal["folder", "git"]
    git_commit: str | None
    file_count: int
    byte_count: int
    question_count: int
    created_at: datetime


@dataclass(frozen=True)
class BanksPage:
    """Uma página da listagem; `next_cursor` é o `id` do último item, ou o `after` pedido sem itens."""

    items: tuple[BankVersion, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class BankMissing:
    """O banco (`repository`) ou a versão dele (`commit`) não existe."""

    what: Literal["repository", "commit"]


class BankArchiveError(Exception):
    """A recusa do pacote: `(field, reason)` em `ARCHIVE_ERRORS`. Nunca leva bytes, caminho nem texto do pacote."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(field, reason)
        self.field = field
        self.reason = reason

    def __repr__(self) -> str:
        return f"BankArchiveError(field={self.field!r}, reason={self.reason!r})"


# --------------------------------------------------------------------------------------------------
# A leitura do pacote
# --------------------------------------------------------------------------------------------------

# O bloco do tar, o pedaço da leitura (a saída do zlib e o conteúdo entregue ao `open_member`) e a folga por
# membro do teto do fluxo descomprimido (cabeçalhos e preenchimento).
_BLOCK = 512
_CHUNK = 65536
_STREAM_ALLOWANCE_PER_MEMBER = 10240

# O `magic` POSIX do ustar com a versão `00`; os tipos de arquivo regular; o tipo do cabeçalho estendido pax.
_POSIX_MAGIC = b"ustar\x0000"
_REGULAR_TYPES = (b"0", b"\0")
_EXTENDED_TYPE = b"x"
_PAX_KEY_BYTES = frozenset(key.encode("ascii") for key in PAX_KEYS)
# O maior comprimento de registro pax cabe em 5 dígitos (o cabeçalho estendido tem no máximo 8 KiB).
_PAX_LENGTH_DIGITS = 5

# Os caracteres recusados num caminho: o nulo, os controles C0 e C1, o DEL e a barra invertida.
_FORBIDDEN_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\\]")

# A marca de arquivo na árvore dos caminhos já vistos (os diretórios são dicionários).
_FILE = object()


class _Refusal(Exception):
    """A recusa dentro da leitura; `read_archive` a troca por uma `BankArchiveError` nova, fora do `except`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _TarStream:
    """O fluxo tar descomprimido de um único membro gzip, sob demanda e com teto.

    O pacote em memória entra no `zlib` em fatias de até 64 KiB, e a saída sai em pedaços de até 64 KiB: nunca se
    descomprime mais do que a leitura pede. Passar do teto do fluxo é `too_large`; o gzip que acaba antes da hora, um
    deflate corrompido (o `zlib.error` sobe e vira `invalid_format` em `read_archive`) ou bytes depois do fim do gzip
    são `invalid_format`."""

    def __init__(self, data: bytes, ceiling: int) -> None:
        self._view = memoryview(data)
        self._pos = 0
        self._inflate = zlib.decompressobj(wbits=31)
        self._buf = b""
        self._off = 0
        self._full = False
        self._produced = 0
        self.ceiling = ceiling
        self.consumed = 0

    def _fill(self) -> bool:
        """Traz o próximo pedaço descomprimido para o buffer; `False` no fim do membro gzip."""
        inflate = self._inflate
        while not inflate.eof:
            tail = inflate.unconsumed_tail
            if tail:
                out = inflate.decompress(tail, _CHUNK)
            elif self._full:
                # A saída anterior encheu o limite: o zlib pode ter saída pendente sem pedir mais entrada.
                out = inflate.decompress(b"", _CHUNK)
            elif self._pos < len(self._view):
                piece = self._view[self._pos:self._pos + _CHUNK]
                self._pos += len(piece)
                out = inflate.decompress(piece, _CHUNK)
            else:
                raise _Refusal("invalid_format")
            self._full = len(out) == _CHUNK
            if out:
                self._produced += len(out)
                if self._produced > self.ceiling:
                    raise _Refusal("too_large")
                self._buf = out
                self._off = 0
                return True
        return False

    def pieces(self, size: int) -> Iterator[bytes]:
        """Exatamente `size` bytes do fluxo, em pedaços de até 64 KiB; o fluxo que acaba antes é `invalid_format`."""
        while size:
            if self._off >= len(self._buf) and not self._fill():
                raise _Refusal("invalid_format")
            take = min(size, len(self._buf) - self._off)
            piece = self._buf[self._off:self._off + take]
            self._off += take
            self.consumed += take
            size -= take
            yield piece

    def read(self, size: int) -> bytes:
        return b"".join(self.pieces(size))

    def skip(self, size: int) -> None:
        for _ in self.pieces(size):
            pass

    def fits(self, size: int) -> bool:
        """Se os próximos `size` bytes, com o preenchimento até o bloco, ainda cabem no teto do fluxo."""
        return self.consumed + size + (-size % _BLOCK) <= self.ceiling

    def finish(self) -> None:
        """Depois dos dois blocos de zeros: só zeros até o fim do fluxo, o fim do gzip e nada depois dele."""
        rest = self._buf[self._off:]
        if rest.count(0) != len(rest):
            raise _Refusal("invalid_format")
        self._off = len(self._buf)
        while self._fill():
            if self._buf.count(0) != len(self._buf):
                raise _Refusal("invalid_format")
            self._off = len(self._buf)
        if self._inflate.unused_data or self._pos < len(self._view):
            raise _Refusal("invalid_format")


def _octal(field: bytes, digits: int) -> int:
    """Um campo numérico do ustar em octal (até `digits` dígitos, terminado por nulo ou espaço); a codificação
    base 256, o campo vazio e qualquer outro caractere são `invalid_format`."""
    if field[:1] and field[0] & 0x80:
        raise _Refusal("invalid_format")
    end = field.find(b"\0")
    text = (field if end < 0 else field[:end]).strip(b" ")
    if not text or len(text) > digits or text.strip(b"01234567"):
        raise _Refusal("invalid_format")
    return int(text, 8)


def _header(block: bytes) -> tuple[bytes, int]:
    """O tipo e o tamanho declarado de um cabeçalho, com a soma de conferência e o `magic` POSIX conferidos."""
    stored = _octal(block[148:156], 8)
    if stored != sum(block) - sum(block[148:156]) + 8 * 0x20:
        raise _Refusal("invalid_format")
    if block[257:265] != _POSIX_MAGIC:
        raise _Refusal("invalid_format")
    return block[156:157], _octal(block[124:136], 11)


def _until_nul(field: bytes) -> bytes:
    end = field.find(b"\0")
    return field if end < 0 else field[:end]


def _ustar_name(block: bytes) -> bytes:
    """O nome do membro no ustar: o `prefix`, uma barra e o `name`, ou só o `name` sem `prefix`."""
    name = _until_nul(block[0:100])
    prefix = _until_nul(block[345:500])
    return prefix + b"/" + name if prefix else name


def _pax(raw: bytes) -> tuple[dict[bytes, bytes], bool]:
    """Os registros `"<n> <chave>=<valor>\\n"` de um cabeçalho estendido e se algum tem chave fora da lista aceita.
    Registro malformado, chave repetida e `size` que não é só dígitos são `invalid_format`."""
    records: dict[bytes, bytes] = {}
    disallowed = False
    pos = 0
    while pos < len(raw):
        space = raw.find(b" ", pos, pos + _PAX_LENGTH_DIGITS + 1)
        digits = raw[pos:space] if space > pos else b""
        if not digits or not digits.isdigit():
            raise _Refusal("invalid_format")
        end = pos + int(digits)
        if end <= space + 1 or end > len(raw) or raw[end - 1:end] != b"\n":
            raise _Refusal("invalid_format")
        body = raw[space + 1:end - 1]
        equals = body.find(b"=")
        if equals <= 0:
            raise _Refusal("invalid_format")
        key, value = body[:equals], body[equals + 1:]
        if key in records:
            raise _Refusal("invalid_format")
        records[key] = value
        if key not in _PAX_KEY_BYTES:
            disallowed = True
        pos = end
    size = records.get(b"size")
    if size is not None and not (size and size.isdigit()):
        raise _Refusal("invalid_format")
    return records, disallowed


def _checked_path(raw: bytes) -> tuple[str, list[str]]:
    """O caminho decodificado e os componentes dele, ou `path_not_allowed` fora da regra (UTF-8 estrito, nada de
    nulo, controle ou barra invertida, componentes não vazios, sem `.`, `..` nem `.git` em qualquer caixa, até 255
    bytes por componente e 4096 no caminho inteiro)."""
    if not raw or len(raw) > MAX_PATH_BYTES:
        raise _Refusal("path_not_allowed")
    try:
        text: str | None = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or _FORBIDDEN_PATH_CHARS.search(text):
        raise _Refusal("path_not_allowed")
    parts = text.split("/")
    for part in parts:
        if (part in ("", ".", "..") or (part.isascii() and part.lower() == ".git")
                or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES):
            raise _Refusal("path_not_allowed")
    return text, parts


def _place(tree: dict[str, object], parts: list[str]) -> None:
    """Põe o caminho na árvore dos já vistos: um componente que já é arquivo, ou o caminho que já é diretório, é
    `path_not_allowed` (`a` e `a/b` juntos, em qualquer ordem); o caminho já visto é `duplicate`."""
    node = tree
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif child is _FILE:
            raise _Refusal("path_not_allowed")
        node = child  # type: ignore[assignment]
    existing = node.get(parts[-1])
    if existing is None:
        node[parts[-1]] = _FILE
    elif existing is _FILE:
        raise _Refusal("duplicate")
    else:
        raise _Refusal("path_not_allowed")


def _question_count(tree: dict[str, object], questions: Iterable[str]) -> int:
    """Os `questions/<…>/info.json`, menos os que ficam dentro do diretório de outra questão.

    Cada questão desce a árvore dos caminhos já vistos (`tree`, a de `_place`) pelos diretórios acima do dela, a
    partir de `questions/`, e procura um arquivo `info.json` em cada um. A descida anda um componente por nível,
    sem montar de novo o prefixo do caminho: o custo é linear no tamanho total dos caminhos."""
    count = 0
    for path in questions:
        parts = path.split("/")
        node = tree["questions"]
        for part in parts[1:-2]:
            node = node[part]  # type: ignore[index]
            if node.get("info.json") is _FILE:  # type: ignore[attr-defined]
                break
        else:
            count += 1
    return count


def _read_members(data: bytes, max_files: int, max_bytes: int,
                  open_member: Callable[[str], BinaryIO] | None) -> ArchiveSummary:
    """A leitura membro a membro, na ordem das regras: o bloco e os registros pax bem formados; os tetos do
    cabeçalho estendido, do conteúdo e do fluxo, no cabeçalho; o número de membros; o tipo; o caminho e o conflito;
    o repetido. Depois do último membro: o fim do tar e do gzip, e o `infoCourse.json`."""
    stream = _TarStream(data, max_bytes + (max_files + 1) * _STREAM_ALLOWANCE_PER_MEMBER)
    entries: list[BankEntry] = []
    tree: dict[str, object] = {}
    questions: list[str] = []
    members = 0
    content = 0
    pending: tuple[dict[bytes, bytes], bool] | None = None
    while True:
        block = stream.read(_BLOCK)
        if block.count(0) == _BLOCK:
            if pending is not None or stream.read(_BLOCK).count(0) != _BLOCK:
                raise _Refusal("invalid_format")
            stream.finish()
            break
        kind, size = _header(block)
        if kind == _EXTENDED_TYPE:
            if pending is not None:
                raise _Refusal("invalid_format")
            if size > MAX_EXTENDED_HEADER_BYTES or not stream.fits(size):
                raise _Refusal("too_large")
            raw = stream.read(size)
            stream.skip(-size % _BLOCK)
            pending = _pax(raw)
            continue
        records, disallowed = pending if pending is not None else ({}, False)
        pending = None
        if b"size" in records:
            size = int(records[b"size"])
        content += size
        if content > max_bytes or not stream.fits(size):
            raise _Refusal("too_large")
        members += 1
        if members > max_files:
            raise _Refusal("too_many")
        if kind not in _REGULAR_TYPES or disallowed:
            raise _Refusal("entry_not_allowed")
        raw_path = records.get(b"path")
        path, parts = _checked_path(_ustar_name(block) if raw_path is None else raw_path)
        _place(tree, parts)
        digest = hashlib.sha256()
        if open_member is None:
            for piece in stream.pieces(size):
                digest.update(piece)
        else:
            sink = open_member(path)
            try:
                for piece in stream.pieces(size):
                    digest.update(piece)
                    sink.write(piece)
            finally:
                sink.close()
        stream.skip(-size % _BLOCK)
        entries.append(BankEntry(path=path, size=size, sha256=digest.hexdigest()))
        if len(parts) >= 3 and parts[0] == "questions" and parts[-1] == "info.json":
            questions.append(path)
    if tree.get("infoCourse.json") is not _FILE:
        raise _Refusal("not_a_course")
    return ArchiveSummary(entries=tuple(entries), file_count=len(entries), byte_count=content,
                          question_count=_question_count(tree, questions))


def read_archive(data: bytes, *, max_files: int, max_bytes: int,
                 open_member: Callable[[str], BinaryIO] | None = None) -> ArchiveSummary:
    """Lê e confere o pacote inteiro (um membro gzip, um tar POSIX só com arquivos regulares), em fluxo, com os
    tetos dados de arquivos e de bytes do conteúdo. Com `open_member`, o conteúdo de cada membro é escrito, em
    pedaços, no arquivo que ela abre para o caminho dele (e que é fechado depois do último pedaço). Só levanta
    `BankArchiveError`: toda outra exceção da leitura (inclusive a de `open_member`) vira `invalid_format`, levantada
    fora do `except`, sem `__cause__` nem `__context__` e sem nada do texto original."""
    try:
        return _read_members(data, max_files, max_bytes, open_member)
    except _Refusal as refusal:
        reason = refusal.reason
    except Exception:
        # O texto da exceção pode trazer bytes do pacote: nada dele sai daqui.
        reason = "invalid_format"
    raise BankArchiveError("archive", reason)


def bank_version(entries: Iterable[BankEntry]) -> str:
    """A versão do banco: o SHA-256 hex (minúsculo) do manifesto das entradas, em ordem dos bytes do caminho.

    O manifesto é o prefixo `VERSION_PREFIX` seguido, para cada entrada, do caminho em UTF-8, um nulo, o tamanho
    em decimal, um nulo, o SHA-256 hex do conteúdo e uma quebra de linha; nada mais (modo, horários, dono,
    compressão e origem) entra."""
    items = sorted(((entry.path.encode("utf-8"), entry) for entry in entries), key=lambda item: item[0])
    digest = hashlib.sha256(VERSION_PREFIX)
    for path, entry in items:
        digest.update(path + b"\0" + str(entry.size).encode("ascii") + b"\0" + entry.sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------------
# A tabela `question_banks`
# --------------------------------------------------------------------------------------------------

# O ator do token inicial nos eventos; na tabela, o token inicial fica como `NULL`.
_BOOTSTRAP_ACTOR = "bootstrap"

def _version_of(row: Sequence[object], name: str, repo: str) -> BankVersion:
    _, version, source, git_commit, file_count, byte_count, question_count, created_at = row
    return BankVersion(name=name, version=str(version), repo=repo,
                       source=source,  # type: ignore[arg-type]
                       git_commit=git_commit,  # type: ignore[arg-type]
                       file_count=int(file_count), byte_count=int(byte_count),  # type: ignore[call-overload]
                       question_count=int(question_count),  # type: ignore[call-overload]
                       created_at=created_at)  # type: ignore[arg-type]


def ensure_bank(conn: psycopg.Connection, *, name: str, version: str, source: str, git_commit: str | None,
                archive: bytes, summary: ArchiveSummary, now: datetime,
                actor_token_id: str | None) -> tuple[BankVersion, bool]:
    """Guarda a versão do banco, se ainda não existe (`INSERT … ON CONFLICT DO NOTHING` + `SELECT`), e grava o
    evento do envio só quando criou; devolve a versão guardada (a do primeiro envio) e se este pedido a criou.

    Roda num bloco de transação (um ponto de salvamento dentro da transação do chamador). Um envio simultâneo do
    mesmo nome e da mesma versão espera o bloqueio da linha e devolve a do primeiro, sem evento. O nome do evento é
    redigido na gravação, com as identidades lidas depois do lock dos eventos; a versão devolvida leva o nome
    literal (quem responde redige). O token inicial fica `NULL` na tabela e `bootstrap` no evento."""
    stored_actor = None if actor_token_id in (None, _BOOTSTRAP_ACTOR) else actor_token_id
    with conn.transaction():
        inserted = conn.execute(
            "INSERT INTO question_banks (bank_uuid, name, version, source, git_commit, archive, archive_bytes, "
            "file_count, byte_count, question_count, actor_token_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (name, version) DO NOTHING RETURNING id",
            (uuid.uuid4(), name, version, source, git_commit, archive, len(archive), summary.file_count,
             summary.byte_count, summary.question_count, stored_actor, now),
        ).fetchone()
        created = inserted is not None
        if created:
            identities = load_write_identities(conn)
            append_event(conn, EventType.BANK_UPLOADED, {
                "bank": redact_operator_text(name, identities),
                "version": version,
                "source": source,
                "git_commit": git_commit,
                "file_count": summary.file_count,
                "byte_count": summary.byte_count,
                "question_count": summary.question_count,
            }, occurred_at=now, actor_token_id=actor_token_id, identities=identities)
        row = conn.execute(
            "SELECT name, version, source, git_commit, file_count, byte_count, question_count, created_at "
            "FROM question_banks WHERE name = %s AND version = %s",
            (name, version),
        ).fetchone()
    assert row is not None
    return _version_of(row, str(row[0]), BANK_REPO_PREFIX + str(row[0])), created


def list_banks(conn: psycopg.Connection, *, name: str | None, after: int, limit: int,
               identities: Sequence[KnownIdentity], secrets: Sequence[str | None]) -> BanksPage:
    """As versões guardadas, em ordem do primeiro envio, depois de `after`, com o filtro opcional do nome exato;
    o nome e o `repo` saem redigidos. `next_cursor` é o `id` do último item, ou `after` sem itens."""
    rows = conn.execute(
        "SELECT id, name, version, source, git_commit, file_count, byte_count, question_count, created_at "
        "FROM question_banks "
        "WHERE id > %s AND (%s::text IS NULL OR name = %s) ORDER BY id LIMIT %s",
        (after, name, name, limit),
    ).fetchall()
    items = tuple(
        _version_of(row[1:], redact_operator_text(str(row[1]), identities, secrets=secrets),
                    redact_operator_text(BANK_REPO_PREFIX + str(row[1]), identities, secrets=secrets))
        for row in rows)
    return BanksPage(items=items, next_cursor=int(rows[-1][0]) if rows else after)


def load_archive(conn: psycopg.Connection, name: str, version: str) -> bytes | BankMissing:
    """O pacote guardado de `(name, version)`, ou `BankMissing` (o banco ou a versão inexistente)."""
    row = conn.execute("SELECT archive FROM question_banks WHERE name = %s AND version = %s",
                       (name, version)).fetchone()
    if row is not None:
        return bytes(row[0])
    known = conn.execute("SELECT EXISTS (SELECT 1 FROM question_banks WHERE name = %s)", (name,)).fetchone()
    return BankMissing("commit" if known is not None and known[0] else "repository")


# --------------------------------------------------------------------------------------------------
# O roteador da obtenção
# --------------------------------------------------------------------------------------------------


class RoutingRepoFetcher:
    """A obtenção com dois lados: as referências `bank:` vão ao `bank`, as demais ao `git`. Um lado `None`
    responde `FetchTemporaryFailure("repos_unavailable")`."""

    def __init__(self, git: RepoFetcher | None, bank: RepoFetcher | None) -> None:
        self.git = git
        self.bank = bank

    def ensure(self, repo: str, commit: str) -> Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure:
        side = self.bank if repo.startswith(BANK_REPO_PREFIX) else self.git
        if side is None:
            return FetchTemporaryFailure("repos_unavailable")
        return side.ensure(repo, commit)
