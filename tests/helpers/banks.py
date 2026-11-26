"""Bancos de questões e pacotes `.tar.gz` montados à mão, para os testes do banco de questões local.

Nada aqui importa o produto: os bytes do pacote, a versão de referência e a contagem de questões são calculados
por conta própria, para que um erro da implementação apareça no teste em vez de ser repetido por ele. Os pacotes
são montados em tempo de execução (nenhum binário de pacote fica na árvore).

**O banco sintético.** É o curso de fixture `tests/fixtures/repos/course` (só leitura): `course_files()` o lê como
`{caminho POSIX relativo: bytes}`, `copy_course(dst)` o copia para uma pasta nova (a origem nunca muda), e
`write_tree(root, files)` grava qualquer árvore dada.

**A versão de referência.** `reference_version(files)`: o SHA-256 hex (minúsculo) do manifesto
`b"cq-bank-v1\\n" + Σ (caminho_utf8 + b"\\0" + str(tamanho) + b"\\0" + sha256hex(conteúdo) + b"\\n")`, com a soma em
ordem crescente dos **bytes** UTF-8 do caminho (não a ordem alfabética nem a da árvore do git); o modo, os
horários, o dono, a compressão e a origem não entram. `reference_question_count(paths)`: o número de
`questions/<um ou mais componentes>/info.json`, menos os que ficam dentro do diretório de outra questão (um
`questions/a/b/info.json` não conta quando existe `questions/a/info.json`). `load_vectors()` e `vector_files(case)`
leem os vetores fixos de `tests/fixtures/banks/version-vectors.json`.

**Os pacotes.** `make_archive(members, *, gzip=True, fmt=PAX_FORMAT, ...)` monta o fluxo tar bloco a bloco, sem o
`tarfile` (só as constantes de formato vêm dele), e o comprime num membro gzip. Cada `Member` é um cabeçalho de
512 bytes seguido do conteúdo, com o preenchimento até o múltiplo de 512 calculado pelo tamanho **real** do
conteúdo:
- `type`: qualquer byte de tipo (`b"0"`, `b"\\0"`, `b"1"` a `b"7"`, `b"S"`, `b"g"`, `b"x"`, `b"L"`, `b"K"` ou um
  desconhecido); um cabeçalho estendido `x`, global `g` ou de nome longo do GNU (`L`, `K`) também é um `Member`,
  com o conteúdo cru que se quiser (`pax_records` monta os registros pax);
- `name`, `linkname` e `prefix` em texto (codificado em UTF-8) ou em bytes crus (UTF-8 inválido, `\\`, controle,
  nulo); `size` é o tamanho **declarado** (por padrão, o real); `size_field` grava os 12 bytes do campo como dados
  (base 256 por `base256`, dígitos fora do octal); `magic` grava os 8 bytes de `magic` e versão como dados (por
  padrão, os do formato); `checksum_delta` soma ao valor certo da soma de conferência (um valor diferente de zero
  dá a soma errada);
- `pax` (pares chave e valor, em texto ou bytes, na ordem dada, repetidos se repetidos) ou `pax_raw` (os bytes
  crus do conteúdo): um cabeçalho `x` logo antes do membro, com esse conteúdo. Sem nenhum dos dois, no formato
  `PAX_FORMAT`, o `x` só aparece sozinho quando o nome não cabe no campo do ustar (não ASCII ou acima de 100
  bytes, com a chave `path`) ou o tamanho declarado não cabe em 11 dígitos octais (com a chave `size`), como o
  `tarfile` faz; no `USTAR_FORMAT`, o nome longo vai ao `prefix`; no `GNU_FORMAT`, o `magic` é o do GNU (que não
  é o POSIX) e o nome ou o alvo longos vão a um membro `L` ou `K` antes do membro;
- `data`: bytes, ou um `Fill(count, byte)`, gerado em pedaços de 1 MiB (o conteúdo muito compressível de uma bomba
  nunca fica inteiro na memória).

Depois do último membro, `end_blocks` blocos de zeros (2, por padrão) e, com `pad_record`, zeros até o múltiplo
de 10240 bytes (como gravam o `tarfile` e o GNU tar); `trailer` vem depois disso (um byte não zero depois do fim);
`truncate` corta o fluxo tar nos primeiros `truncate` bytes (um tar truncado). Na compressão: `gzip=False` devolve o
fluxo tar cru (não é gzip); `extra_gzip_member` acrescenta um segundo membro gzip com esses bytes; `after_gzip`
acrescenta bytes crus depois do fim do gzip. `corrupt_deflate(archive)` troca o primeiro byte do deflate por um
bloco de tipo reservado (o `zlib` falha logo no começo). Os atalhos `archive_of(files)` (o pacote válido de uma
árvore, em ordem dos bytes do caminho), `many_files(count)` (muitos cabeçalhos pequenos), `header_bomb_archive()`
(um `x` que declara 300 MB, com o conteúdo de verdade, muito compressível) e `content_bomb_archive()` (um arquivo
acima de 100 MiB de conteúdo muito compressível) montam os casos comuns.

**O pedido.** `upload_body(name, version, source, git_commit, archive)`: o corpo JSON do envio, com o pacote em
base64 padrão (com preenchimento).

**O marcador.** `unique_marker()`: um texto único, montado em tempo de execução, para pôr dentro de um arquivo do
banco e procurar nas saídas (ele nunca fica escrito na árvore).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
import tarfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COURSE = FIXTURES / "repos" / "course"
VECTORS_PATH = FIXTURES / "banks" / "version-vectors.json"

# Os formatos do fluxo tar (só as constantes do `tarfile`; a montagem é feita aqui).
PAX_FORMAT = tarfile.PAX_FORMAT
USTAR_FORMAT = tarfile.USTAR_FORMAT
GNU_FORMAT = tarfile.GNU_FORMAT

BLOCK_SIZE = 512
RECORD_SIZE = 10240
# `magic` (6 bytes) e versão (2 bytes) do ustar POSIX e do GNU (que não é POSIX).
POSIX_MAGIC = b"ustar\x00" + b"00"
GNU_MAGIC = b"ustar  \x00"
PAX_HEADER_NAME = b"././@PaxHeader"
GNU_LONG_NAME = b"././@LongLink"

# O prefixo do manifesto da versão (definição própria do auxiliar, independente das implementações).
REFERENCE_VERSION_PREFIX = b"cq-bank-v1\n"

# Os tetos de referência (os mesmos números da regra, escritos aqui de novo).
REFERENCE_MAX_FILES = 20000
REFERENCE_MAX_CONTENT_BYTES = 104857600  # 100 MiB
REFERENCE_MAX_ARCHIVE_BYTES = 134217728  # 128 MiB
HEADER_BOMB_BYTES = 300 * 1024 * 1024  # o cabeçalho `x` que declara 300 MB

_FILL_CHUNK = 1 << 20
_OCTAL_SIZE_LIMIT = 8 ** 11  # o maior tamanho em 11 dígitos octais, mais um


# --------------------------------------------------------------------------------------------------
# O banco sintético
# --------------------------------------------------------------------------------------------------


def course_files() -> dict[str, bytes]:
    """O curso de fixture como `{caminho POSIX relativo: bytes}`, em ordem dos bytes do caminho."""
    files = {path.relative_to(COURSE).as_posix(): path.read_bytes()
             for path in COURSE.rglob("*") if path.is_file()}
    return {path: files[path] for path in sorted(files, key=lambda p: p.encode("utf-8"))}


def copy_course(dst: Path) -> Path:
    """Copia o curso de fixture para `dst` (que não pode existir); a origem não muda. Devolve `dst`."""
    dst = Path(dst)
    shutil.copytree(COURSE, dst)
    return dst


def write_tree(root: Path, files: Mapping[str, bytes]) -> Path:
    """Grava cada `{caminho POSIX relativo: bytes}` sob `root` (criada se preciso). Recusa caminho absoluto e
    componente `..` (o auxiliar nunca escreve fora de `root`). Devolve `root`."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("caminho de árvore inválido")
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


# --------------------------------------------------------------------------------------------------
# A versão e a contagem de referência
# --------------------------------------------------------------------------------------------------


def reference_version(files: Mapping[str, bytes]) -> str:
    """A versão de referência de `{caminho: conteúdo}` (a definição do docstring do módulo)."""
    items = sorted(((path.encode("utf-8"), data) for path, data in files.items()), key=lambda item: item[0])
    manifest = REFERENCE_VERSION_PREFIX + b"".join(
        path + b"\0" + str(len(data)).encode("ascii") + b"\0"
        + hashlib.sha256(data).hexdigest().encode("ascii") + b"\n"
        for path, data in items)
    return hashlib.sha256(manifest).hexdigest()


def reference_question_count(paths: Iterable[str]) -> int:
    """O número de questões de referência entre os caminhos dados (o critério do docstring do módulo)."""
    present = set(paths)
    count = 0
    for path in present:
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "questions" or parts[-1] != "info.json":
            continue
        inner = parts[1:-1]
        nested = any("/".join(["questions", *inner[:depth], "info.json"]) in present
                     for depth in range(1, len(inner)))
        if not nested:
            count += 1
    return count


def load_vectors() -> dict[str, Any]:
    """Os vetores fixos da versão (`tests/fixtures/banks/version-vectors.json`)."""
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def vector_files(case: Mapping[str, Any]) -> dict[str, bytes]:
    """Os arquivos de um caso dos vetores, como `{caminho: bytes}` (o conteúdo vem em base64)."""
    return {item["path"]: base64.b64decode(item["content_base64"], validate=True) for item in case["files"]}


# --------------------------------------------------------------------------------------------------
# Os pacotes montados à mão
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """`count` bytes iguais a `byte`, gerados em pedaços (nunca inteiros na memória)."""

    count: int
    byte: bytes = b"\0"

    def __post_init__(self) -> None:
        if len(self.byte) != 1 or self.count < 0:
            raise ValueError("preenchimento inválido")

    def __len__(self) -> int:
        return self.count

    def chunks(self) -> Iterator[bytes]:
        full, rest = divmod(self.count, _FILL_CHUNK)
        if full:
            block = self.byte * _FILL_CHUNK
            for _ in range(full):
                yield block
        if rest:
            yield self.byte * rest


Text = str | bytes


@dataclass(frozen=True)
class Member:
    """Um membro do fluxo tar (ver o docstring do módulo)."""

    name: Text
    data: bytes | Fill = b""
    type: bytes = b"0"
    size: int | None = None
    size_field: bytes | None = None
    pax: tuple[tuple[Text, Text], ...] | None = None
    pax_raw: bytes | None = None
    linkname: Text = b""
    prefix: Text = b""
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    mtime: int = 0
    uname: bytes = b""
    gname: bytes = b""
    magic: bytes | None = None
    checksum_delta: int = 0

    def __post_init__(self) -> None:
        if len(self.type) != 1:
            raise ValueError("o tipo do membro é um único byte")
        if self.pax is not None and self.pax_raw is not None:
            raise ValueError("use pax ou pax_raw, não os dois")
        if self.size_field is not None and len(self.size_field) != 12:
            raise ValueError("o campo de tamanho tem 12 bytes")
        if self.magic is not None and len(self.magic) != 8:
            raise ValueError("magic e versão têm 8 bytes")


def _bytes(value: Text) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def regular(path: Text, data: bytes | Fill = b"", **fields: Any) -> Member:
    """Um arquivo regular (tipo `0`)."""
    return Member(path, data, **fields)


def base256(value: int, width: int = 12) -> bytes:
    """Um campo numérico na codificação base 256 (o primeiro byte `0x80`), que a regra recusa."""
    return b"\x80" + value.to_bytes(width - 1, "big")


def pax_records(pairs: Iterable[tuple[Text, Text]]) -> bytes:
    """Os registros `"<n> <chave>=<valor>\\n"` de um cabeçalho pax, com o `n` certo, na ordem dada."""
    out = bytearray()
    for key, value in pairs:
        body = b" " + _bytes(key) + b"=" + _bytes(value) + b"\n"
        length = len(body) + 1
        while len(str(length)) + len(body) != length:
            length = len(str(length)) + len(body)
        out += str(length).encode("ascii") + body
    return bytes(out)


def _octal(value: int, width: int) -> bytes:
    digits = width - 1
    if value < 0 or value >= 8 ** digits:
        raise ValueError("número fora do campo octal")
    return f"{value:0{digits}o}".encode("ascii") + b"\0"


def _field(value: bytes, width: int) -> bytes:
    if len(value) > width:
        raise ValueError("valor maior que o campo")
    return value + b"\0" * (width - len(value))


def header_block(*, name: bytes, size: int, type: bytes, size_field: bytes | None = None, linkname: bytes = b"",
                 prefix: bytes = b"", mode: int = 0o644, uid: int = 0, gid: int = 0, mtime: int = 0,
                 uname: bytes = b"", gname: bytes = b"", magic: bytes = POSIX_MAGIC,
                 checksum_delta: int = 0) -> bytes:
    """Um cabeçalho ustar de 512 bytes, campo a campo, com a soma de conferência certa mais `checksum_delta`."""
    parts = [
        _field(name, 100),
        _octal(mode, 8),
        _octal(uid, 8),
        _octal(gid, 8),
        size_field if size_field is not None else _octal(size, 12),
        _octal(mtime, 12),
        b" " * 8,  # a soma de conferência, calculada com o campo em espaços
        type,
        _field(linkname, 100),
        magic,
        _field(uname, 32),
        _field(gname, 32),
        _octal(0, 8),
        _octal(0, 8),
        _field(prefix, 155),
    ]
    block = bytearray(b"".join(parts))
    block += b"\0" * (BLOCK_SIZE - len(block))
    checksum = sum(block) + checksum_delta
    block[148:156] = f"{checksum:06o}".encode("ascii")[-6:] + b"\0 "
    return bytes(block)


def _ascii_name(name: bytes) -> bytes:
    """O nome no campo do ustar quando o `path` do pax o substitui: não ASCII trocado por `?`, até 100 bytes."""
    return bytes(b if b < 0x80 else 0x3F for b in name)[:100]


def _split_ustar(name: bytes) -> tuple[bytes, bytes]:
    if len(name) <= 100:
        return b"", name
    for index in range(len(name) - 1, -1, -1):
        if name[index:index + 1] == b"/" and index <= 155 and len(name) - index - 1 <= 100:
            return name[:index], name[index + 1:]
    raise ValueError("o nome não cabe no ustar")


def _data_chunks(data: bytes | Fill) -> Iterator[bytes]:
    if isinstance(data, Fill):
        yield from data.chunks()
    elif data:
        yield data


def _padding(length: int) -> bytes:
    return b"\0" * (-length % BLOCK_SIZE)


def _member_blocks(member: Member, fmt: int) -> Iterator[bytes]:
    name = _bytes(member.name)
    linkname = _bytes(member.linkname)
    prefix = _bytes(member.prefix)
    real = len(member.data)
    declared = real if member.size is None else member.size
    magic = member.magic if member.magic is not None else (GNU_MAGIC if fmt == GNU_FORMAT else POSIX_MAGIC)
    size_field = member.size_field
    extended: bytes | None = member.pax_raw
    if member.pax is not None:
        extended = pax_records(member.pax)
    field_name = name
    if fmt == PAX_FORMAT and extended is None:
        auto: list[tuple[Text, Text]] = []
        if len(name) > 100 or any(b >= 0x80 for b in name):
            auto.append((b"path", name))
            field_name = _ascii_name(name)
        if size_field is None and declared >= _OCTAL_SIZE_LIMIT:
            auto.append((b"size", str(declared).encode("ascii")))
            size_field = _octal(0, 12)
        if auto:
            extended = pax_records(auto)
    elif fmt == USTAR_FORMAT and not prefix:
        prefix, field_name = _split_ustar(name)
    elif fmt == GNU_FORMAT:
        for flag, value in ((b"L", name), (b"K", linkname)):
            if len(value) > 100:
                long_data = value + b"\0"
                yield header_block(name=GNU_LONG_NAME, size=len(long_data), type=flag, magic=GNU_MAGIC)
                yield long_data + _padding(len(long_data))
        field_name = name[:100]
        linkname = linkname[:100]
    if fmt == PAX_FORMAT and len(field_name) > 100:
        field_name = field_name[:100]
    if extended is not None:
        yield header_block(name=PAX_HEADER_NAME, size=len(extended), type=b"x", magic=magic)
        yield extended + _padding(len(extended))
    yield header_block(name=field_name, size=declared, type=member.type, size_field=size_field,
                       linkname=linkname, prefix=prefix, mode=member.mode, uid=member.uid, gid=member.gid,
                       mtime=member.mtime, uname=member.uname, gname=member.gname, magic=magic,
                       checksum_delta=member.checksum_delta)
    yield from _data_chunks(member.data)
    yield _padding(real)


def tar_stream(members: Iterable[Member], *, fmt: int = PAX_FORMAT, end_blocks: int = 2, pad_record: bool = True,
               trailer: bytes = b"", truncate: int | None = None) -> Iterator[bytes]:
    """O fluxo tar cru, em pedaços (ver `make_archive`)."""
    if fmt not in (PAX_FORMAT, USTAR_FORMAT, GNU_FORMAT):
        raise ValueError("formato desconhecido")

    def chunks() -> Iterator[bytes]:
        total = 0
        for member in members:
            for chunk in _member_blocks(member, fmt):
                total += len(chunk)
                yield chunk
        end = b"\0" * (BLOCK_SIZE * end_blocks)
        total += len(end)
        yield end
        if pad_record:
            yield b"\0" * (-total % RECORD_SIZE)
        yield trailer

    remaining = truncate
    for chunk in chunks():
        if not chunk:
            continue
        if remaining is not None:
            if remaining <= 0:
                return
            chunk = chunk[:remaining]
            remaining -= len(chunk)
        yield chunk


def gzip_bytes(data: bytes) -> bytes:
    """Um único membro gzip de `data` (sem horário nem nome de arquivo)."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    return compressor.compress(data) + compressor.flush()


def make_archive(members: Iterable[Member], *, gzip: bool = True, fmt: int = PAX_FORMAT, end_blocks: int = 2,
                 pad_record: bool = True, trailer: bytes = b"", truncate: int | None = None,
                 compresslevel: int = 6, extra_gzip_member: bytes | None = None,
                 after_gzip: bytes = b"") -> bytes:
    """O pacote dos `members` (ver o docstring do módulo). Com `gzip=False`, o fluxo tar cru."""
    stream = tar_stream(members, fmt=fmt, end_blocks=end_blocks, pad_record=pad_record, trailer=trailer,
                        truncate=truncate)
    if not gzip:
        return b"".join(stream) + after_gzip
    compressor = zlib.compressobj(compresslevel, zlib.DEFLATED, 31)
    out = bytearray()
    for chunk in stream:
        out += compressor.compress(chunk)
    out += compressor.flush()
    if extra_gzip_member is not None:
        out += gzip_bytes(extra_gzip_member)
    out += after_gzip
    return bytes(out)


def corrupt_deflate(archive: bytes) -> bytes:
    """O mesmo pacote com o primeiro byte do deflate trocado por um bloco final de tipo reservado: o `zlib`
    falha logo no começo da leitura."""
    if archive[:4] != b"\x1f\x8b\x08\x00":
        raise ValueError("não é um gzip sem campos opcionais")
    return archive[:10] + b"\x07" + archive[11:]


def members_of(files: Mapping[str, bytes]) -> list[Member]:
    """Os arquivos regulares de `{caminho: conteúdo}`, em ordem dos bytes do caminho (sem entradas de diretório)."""
    return [regular(path, files[path]) for path in sorted(files, key=lambda p: p.encode("utf-8"))]


def archive_of(files: Mapping[str, bytes], **options: Any) -> bytes:
    """O pacote válido de `{caminho: conteúdo}` (as opções vão ao `make_archive`)."""
    return make_archive(members_of(files), **options)


def many_files(count: int, *, directory: str = "muitos", data: bytes = b"") -> Iterator[Member]:
    """`count` arquivos regulares pequenos (`<directory>/f<n>.txt`), gerados um a um."""
    for index in range(count):
        yield regular(f"{directory}/f{index:06d}.txt", data)


def header_bomb_archive(declared: int = HEADER_BOMB_BYTES) -> bytes:
    """Um cabeçalho `x` que declara `declared` bytes, com esse conteúdo de verdade (muito compressível), seguido
    de um `infoCourse.json`: a bomba de compressão nos cabeçalhos."""
    return make_archive([Member(PAX_HEADER_NAME, Fill(declared, b"1"), type=b"x"),
                         regular("infoCourse.json", b"{}\n")])


def content_bomb_archive(size: int = REFERENCE_MAX_CONTENT_BYTES + 1) -> bytes:
    """Um `infoCourse.json` e um arquivo de `size` bytes iguais (muito compressível): a bomba no conteúdo."""
    return make_archive([regular("infoCourse.json", b"{}\n"), regular("questions/grande.bin", Fill(size, b"a"))])


# --------------------------------------------------------------------------------------------------
# O pedido e o marcador
# --------------------------------------------------------------------------------------------------


def upload_body(name: str, version: str, source: str, git_commit: str | None, archive: bytes) -> dict[str, Any]:
    """O corpo JSON do envio, com o pacote em base64 padrão (com preenchimento)."""
    return {"name": name, "version": version, "source": source, "git_commit": git_commit,
            "archive": base64.b64encode(archive).decode("ascii")}


def unique_marker() -> str:
    """Um texto único por chamada, montado em tempo de execução (letras e dígitos ASCII)."""
    return "mrc" + secrets.token_hex(16) + "fim"
