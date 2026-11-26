"""A leitura do pacote de um banco de questões enviado (`cq_server.banks.read_archive`), com pacotes montados à mão
pelo auxiliar `tests/helpers/banks.py` (sem o `cq`).

O que se confere aqui:
- cada regra do pacote com o par `(campo, motivo)` exato da recusa (o campo é sempre `archive`):
  - o formato (`invalid_format`): não gzip, deflate corrompido, dois membros gzip, bytes depois do fim do gzip, tar
    truncado (no cabeçalho, no conteúdo e nos blocos do fim), sem os dois blocos de zeros, soma de conferência
    errada, `magic` que não é o POSIX, `size` em base 256 ou fora do octal, `size=xyz` no pax, registro pax
    malformado ou com chave repetida, dois cabeçalhos `x` seguidos, um `x` sem membro depois e um byte não zero
    depois do fim;
  - os tetos (`too_large`): conteúdo declarado acima de 100 MiB num membro ou na soma, a bomba no conteúdo, um `x`
    que declara 300 MB, um `x` acima de 8 KiB, zeros depois do fim do tar acima do teto do fluxo descomprimido e o
    teto de bytes passado pelo chamador;
  - os arquivos demais (`too_many`): 20001 arquivos, muitos cabeçalhos pequenos (20001 membros, cada um com um `x`
    de quase 8 KiB) e o teto de arquivos passado pelo chamador;
  - o tipo (`entry_not_allowed`): `1`, `2`, `3`, `4`, `5`, `6`, `7`, `S`, `g`, `L`, `K` e desconhecidos, e o pax com
    `GNU.sparse.map`, `GNU.sparse.size`, `GNU.sparse.major=1`, `hdrcharset` ou chave fora da lista;
  - o caminho (`path_not_allowed`): absoluto, `..`, `.`, vazio, componente vazio, barra final (no ustar e no `path`
    do pax), `.git` em qualquer caixa e nível, componente de 256 bytes, nulo, `\\`, controle (U+0000 a U+001F e
    U+007F a U+009F), UTF-8 inválido no nome ustar e no `path` do pax, caminho de 4097 bytes, `..` no `prefix` do
    ustar, e `a` com `a/b` nas duas ordens; o repetido (`duplicate`); sem `infoCourse.json` na raiz
    (`not_a_course`);
- a ordem das regras: a leitura para na primeira recusa, membro a membro, e as regras do fim valem só depois do
  último membro (um link no primeiro membro seguido de 20001 arquivos → `entry_not_allowed`; 20001 arquivos seguidos
  de um link → `too_many`; sem `infoCourse.json` e com um caminho repetido → `duplicate`; sem `infoCourse.json` e com
  um byte não zero depois do fim → `invalid_format`; e outros pares de regras);
- o pico de memória medido por `tracemalloc` abaixo de 32 MiB além do pacote, em cada bomba e nos casos de muitos
  membros;
- os pacotes aceitos: o gerado pelo `tarfile` em `PAX_FORMAT` (nome acentuado e caminho de 200 bytes); o montado
  com todas as chaves pax aceitas, o tipo `\\0`, o `prefix` do ustar, componente de 255 bytes, caminho de 4096
  bytes, `x` de 8 KiB exatos, `A.txt` e `a.txt`; 20000 arquivos; os tetos do chamador no limite;
- toda recusa é `BankArchiveError`, sem `__cause__` nem `__context__`, com `args == (campo, motivo)`, e sem ecoar o
  nome do arquivo nem bytes do pacote (um marcador único nos nomes e no conteúdo não aparece no texto, no `repr` nem
  no traceback da exceção);
- com `open_member`, o conteúdo de cada membro chega inteiro e em pedaços de até 64 KiB, uma abertura por membro, na
  ordem do pacote;
- a contagem de questões: `questions/<…>/info.json`, menos os que ficam dentro do diretório de outra questão (o
  vetor `aninhada`), e as entradas e contagens do resumo.

No esqueleto, `read_archive` lança `NotImplementedError`: todo caso falha.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import traceback
import tracemalloc
import zlib
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest

from tests.helpers.banks import (
    GNU_FORMAT,
    GNU_LONG_NAME,
    GNU_MAGIC,
    PAX_HEADER_NAME,
    REFERENCE_MAX_CONTENT_BYTES,
    REFERENCE_MAX_FILES,
    USTAR_FORMAT,
    Fill,
    Member,
    archive_of,
    base256,
    content_bomb_archive,
    corrupt_deflate,
    header_bomb_archive,
    load_vectors,
    make_archive,
    many_files,
    pax_records,
    reference_question_count,
    reference_version,
    regular,
    tar_stream,
    unique_marker,
    vector_files,
)

MiB = 1024 * 1024
MEMORY_CEILING = 32 * MiB
# O teto do fluxo tar descomprimido: o conteúdo mais 10 KiB por membro (até 20001 membros).
STREAM_CEILING = REFERENCE_MAX_CONTENT_BYTES + (REFERENCE_MAX_FILES + 1) * 10240
EXTENDED_HEADER_CEILING = 8192
CHUNK_CEILING = 64 * 1024

INFO_COURSE = b'{"uuid": "00000000-0000-4000-8000-000000000001", "name": "SINT 001"}\n'


def _info() -> Member:
    return regular("infoCourse.json", INFO_COURSE)


def _with_info(*members: Member, **options: Any) -> bytes:
    return make_archive([_info(), *members], **options)


def _read(data: bytes, *, max_files: int = REFERENCE_MAX_FILES, max_bytes: int = REFERENCE_MAX_CONTENT_BYTES,
          open_member: Callable[[str], Any] | None = None) -> Any:
    from cq_server.banks import read_archive

    return read_archive(data, max_files=max_files, max_bytes=max_bytes, open_member=open_member)


def _refusal(data: bytes, *, marker: str | None = None, **limits: Any) -> Any:
    """A recusa de `data`, conferida: só `BankArchiveError`, sem causa nem contexto, `args == (campo, motivo)`, o
    par no conjunto fechado e nenhum eco do marcador (no texto, no `repr` e no traceback)."""
    from cq_server.banks import ARCHIVE_ERRORS, BankArchiveError

    with pytest.raises(BankArchiveError) as info:
        _read(data, **limits)
    exc = info.value
    assert type(exc) is BankArchiveError
    assert exc.args == (exc.field, exc.reason)
    assert (exc.field, exc.reason) in ARCHIVE_ERRORS
    assert exc.__cause__ is None
    assert exc.__context__ is None
    if marker is not None:
        text = "".join(traceback.format_exception(exc)) + repr(exc) + str(exc) + repr(exc.args)
        assert marker not in text
        assert marker.encode("ascii").hex() not in text
    return exc


def _assert_refused(data: bytes, reason: str, *, marker: str | None = None, **limits: Any) -> None:
    exc = _refusal(data, marker=marker, **limits)
    assert (exc.field, exc.reason) == ("archive", reason)


def _assert_refused_within_memory(build: Callable[[], bytes], reason: str, **limits: Any) -> None:
    """A recusa com o pico de memória da leitura (medido por `tracemalloc`, o pacote já montado antes da medida)
    abaixo de 32 MiB."""
    data = build()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        exc = _refusal(data, **limits)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (exc.field, exc.reason) == ("archive", reason)
    assert peak - before < MEMORY_CEILING, f"pico de {(peak - before) / MiB:.1f} MiB na leitura"


def _path_of_length(total: int, *, head: str = "") -> str:
    """Um caminho válido de `total` bytes, com componentes de no máximo 255 bytes."""
    parts = [head] if head else []
    used = len(head.encode("utf-8")) + (1 if head else 0)
    remaining = total - used
    while remaining > 255:
        parts.append("d" * 200)
        remaining -= 201
    parts.append("f" * remaining)
    path = "/".join(parts)
    assert len(path.encode("utf-8")) == total
    return path


def _pax_of_size(size: int, path: str) -> tuple[tuple[str, str], ...]:
    """Os registros pax `path` e `uname` com `size` bytes no total (o `uname` completa o tamanho)."""
    for filler in range(size, 0, -1):
        pairs = (("path", path), ("uname", "u" * filler))
        if len(pax_records(pairs)) == size:
            return pairs
    raise AssertionError("tamanho de pax impossível")


def _symlink(name: str) -> Member:
    return regular(name, type=b"2", linkname="infoCourse.json")


def _zeros_after_end(total_zeros: int) -> bytes:
    """Um pacote válido seguido, dentro do mesmo membro gzip, de `total_zeros` zeros (gerados em pedaços)."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    out = bytearray()
    for chunk in tar_stream([_info()]):
        out += compressor.compress(chunk)
    block = b"\0" * MiB
    full, rest = divmod(total_zeros, MiB)
    for _ in range(full):
        out += compressor.compress(block)
    out += compressor.compress(b"\0" * rest)
    out += compressor.flush()
    return bytes(out)


def _many_with_extended_headers(count: int) -> Iterator[Member]:
    """`count` arquivos pequenos, cada um com um cabeçalho `x` de quase 8 KiB (os muitos cabeçalhos pequenos)."""
    pairs = _pax_of_size(EXTENDED_HEADER_CEILING - 64, "muitos/f000000.txt")
    filler = pairs[1][1]
    for index in range(count):
        name = f"muitos/f{index:06d}.txt"
        yield regular(name, b"", pax=(("path", name), ("uname", filler)))


# --------------------------------------------------------------------------------------------------
# As recusas, regra por regra
# --------------------------------------------------------------------------------------------------

FORMAT_CASES: dict[str, Callable[[str], bytes]] = {
    "nao_gzip": lambda m: archive_of({"infoCourse.json": INFO_COURSE, f"{m}.txt": m.encode()}, gzip=False),
    "deflate_corrompido": lambda m: corrupt_deflate(archive_of({"infoCourse.json": INFO_COURSE,
                                                                f"{m}.txt": m.encode()})),
    "dois_membros_gzip": lambda m: archive_of({"infoCourse.json": INFO_COURSE}, extra_gzip_member=m.encode()),
    "membro_gzip_vazio_depois": lambda m: archive_of({"infoCourse.json": INFO_COURSE}, extra_gzip_member=b""),
    "zero_depois_do_gzip": lambda m: archive_of({"infoCourse.json": INFO_COURSE}, after_gzip=b"\0"),
    "texto_depois_do_gzip": lambda m: archive_of({"infoCourse.json": INFO_COURSE}, after_gzip=m.encode()),
    "truncado_no_cabecalho": lambda m: _with_info(regular(f"{m}.txt", b"x" * 2000), truncate=512 + 512 + 300),
    "truncado_no_conteudo": lambda m: _with_info(regular(f"{m}.txt", b"x" * 2000), truncate=512 * 3 + 1000),
    "truncado_no_fim": lambda m: _with_info(regular(f"{m}.txt", b"x"), pad_record=False,
                                            truncate=512 * 4 + 1024 - 1),
    "sem_blocos_do_fim": lambda m: _with_info(regular(f"{m}.txt", b"x"), end_blocks=0, pad_record=False),
    "um_bloco_do_fim": lambda m: _with_info(regular(f"{m}.txt", b"x"), end_blocks=1, pad_record=False),
    "soma_a_mais": lambda m: _with_info(regular(f"{m}.txt", b"x", checksum_delta=1)),
    "soma_a_menos": lambda m: _with_info(regular(f"{m}.txt", b"x", checksum_delta=-1)),
    "magic_gnu": lambda m: _with_info(regular(f"{m}.txt", b"x", magic=GNU_MAGIC)),
    "formato_gnu": lambda m: archive_of({"infoCourse.json": INFO_COURSE, f"{m}.txt": b"x"}, fmt=GNU_FORMAT),
    "magic_zerado": lambda m: _with_info(regular(f"{m}.txt", b"x", magic=b"\0" * 8)),
    "versao_ustar_01": lambda m: _with_info(regular(f"{m}.txt", b"x", magic=b"ustar\x0001")),
    "size_base256": lambda m: _with_info(regular(f"{m}.txt", b"abc", size_field=base256(3))),
    "size_fora_do_octal": lambda m: _with_info(regular(f"{m}.txt", b"", size_field=b"00000000009\0")),
    "pax_size_xyz": lambda m: _with_info(regular(f"{m}.txt", b"abc", pax=(("size", "xyz"),))),
    "pax_size_com_sinal": lambda m: _with_info(regular(f"{m}.txt", b"abc", pax=(("size", "+3"),))),
    "pax_comprimento_errado": lambda m: _with_info(regular("a.txt", b"a", pax_raw=f"5 path={m}.txt\n".encode())),
    "pax_sem_igual": lambda m: _with_info(regular("a.txt", b"a", pax_raw=b"8 pathx\n")),
    "pax_comprimento_nao_numerico": lambda m: _with_info(regular("a.txt", b"a", pax_raw=b"x path=a\n")),
    "pax_sem_fim_de_linha": lambda m: _with_info(regular("a.txt", b"a", pax_raw=b"9 path=ab")),
    "pax_comprimento_zero": lambda m: _with_info(regular("a.txt", b"a", pax_raw=b"0 path=a\n")),
    "pax_lixo_depois": lambda m: _with_info(regular("a.txt", b"a",
                                                    pax_raw=pax_records([("path", f"{m}.txt")]) + b"zz")),
    "pax_chave_repetida": lambda m: _with_info(regular("a.txt", b"a",
                                                       pax=(("path", f"{m}.txt"), ("path", "b.txt")))),
    "pax_mtime_repetido": lambda m: _with_info(regular(f"{m}.txt", b"a", pax=(("mtime", "0"), ("mtime", "0")))),
    "dois_x_seguidos": lambda m: _with_info(Member(PAX_HEADER_NAME, pax_records([("mtime", "0")]), type=b"x"),
                                            regular(f"{m}.txt", b"a", pax=(("mtime", "0"),))),
    "x_sem_membro": lambda m: _with_info(Member(PAX_HEADER_NAME, pax_records([("path", f"{m}.txt")]), type=b"x")),
    "byte_nao_zero_depois_do_fim": lambda m: _with_info(regular(f"{m}.txt", b"x"), trailer=b"\x01"),
    "texto_depois_do_fim": lambda m: _with_info(regular(f"{m}.txt", b"x"), trailer=m.encode()),
}


@pytest.mark.parametrize("case", sorted(FORMAT_CASES))
def test_malformed_archive_is_invalid_format(case: str) -> None:
    """Pacote malformado → `(archive, invalid_format)`, sem causa, sem contexto e sem eco: o gzip (não gzip, deflate
    corrompido com `zlib.error` por baixo, dois membros, bytes depois do fim), o tar (truncado, sem os dois blocos de
    zeros, soma errada, `magic` que não é `ustar\\000`, `size` fora do octal ou em base 256) e o pax (`size` que não
    é só dígitos, registro malformado, chave repetida, dois `x` seguidos, `x` sem membro), e qualquer byte não zero
    depois do fim."""
    marker = unique_marker()
    _assert_refused(FORMAT_CASES[case](marker), "invalid_format", marker=marker)


TYPE_CASES: dict[str, Callable[[str], list[Member]]] = {
    "1_link_fisico": lambda m: [regular(f"{m}.lnk", type=b"1", linkname="infoCourse.json")],
    "2_link_simbolico": lambda m: [_symlink(f"{m}.lnk")],
    "3_dispositivo_de_caractere": lambda m: [regular(f"{m}.chr", type=b"3")],
    "4_dispositivo_de_bloco": lambda m: [regular(f"{m}.blk", type=b"4")],
    "5_diretorio": lambda m: [regular(f"{m}/", type=b"5")],
    "6_fila": lambda m: [regular(f"{m}.fifo", type=b"6")],
    "7_contiguo": lambda m: [regular(f"{m}.bin", b"abc", type=b"7")],
    "S_esparso": lambda m: [regular(f"{m}.bin", b"", type=b"S")],
    "g_pax_global": lambda m: [Member(f"{m}/global", pax_records([("comment", m)]), type=b"g"),
                               regular("a.txt", b"a")],
    "L_nome_longo_gnu": lambda m: [Member(GNU_LONG_NAME, f"{m}/longo.txt\0".encode(), type=b"L"),
                                   regular("a.txt", b"a")],
    "K_alvo_longo_gnu": lambda m: [Member(GNU_LONG_NAME, f"{m}/alvo\0".encode(), type=b"K"),
                                   regular("a.txt", b"a")],
    "Z_desconhecido": lambda m: [regular(f"{m}.bin", b"", type=b"Z")],
    "V_volume_gnu": lambda m: [regular(f"{m}.bin", b"", type=b"V")],
    "pax_gnu_sparse_map": lambda m: [regular(f"{m}.bin", b"a", pax=(("GNU.sparse.map", "0,1"),))],
    "pax_gnu_sparse_size": lambda m: [regular(f"{m}.bin", b"a", pax=(("GNU.sparse.size", "1"),))],
    "pax_gnu_sparse_major": lambda m: [regular(f"{m}.bin", b"a", pax=(("GNU.sparse.major", "1"),))],
    "pax_hdrcharset": lambda m: [regular(f"{m}.bin", b"a", pax=(("hdrcharset", "BINARY"),))],
    "pax_comment": lambda m: [regular(f"{m}.bin", b"a", pax=(("comment", m),))],
    "pax_chave_desconhecida": lambda m: [regular(f"{m}.bin", b"a", pax=(("SCHILY.xattr.user.cq", m),))],
}


@pytest.mark.parametrize("case", sorted(TYPE_CASES))
def test_member_that_is_not_a_regular_file_is_entry_not_allowed(case: str) -> None:
    """Membro que não é arquivo regular (`0` ou `\\0`) → `(archive, entry_not_allowed)`: os tipos `1` a `7`, `S`,
    `g`, `L`, `K` e desconhecidos, e um pax com chave fora da lista aceita (`GNU.sparse.*`, `hdrcharset`, `comment`,
    uma chave estendida qualquer), sempre depois de um `infoCourse.json` válido."""
    marker = unique_marker()
    _assert_refused(_with_info(*TYPE_CASES[case](marker)), "entry_not_allowed", marker=marker)


PATH_CASES: dict[str, Callable[[str], list[Member]]] = {
    "absoluto": lambda m: [regular(f"/{m}.txt", b"a")],
    "ponto_ponto_no_inicio": lambda m: [regular(f"../{m}.txt", b"a")],
    "ponto_ponto_no_meio": lambda m: [regular(f"{m}/../x.txt", b"a")],
    "ponto_ponto_no_fim": lambda m: [regular(f"{m}/..", b"a")],
    "ponto_no_inicio": lambda m: [regular(f"./{m}.txt", b"a")],
    "ponto_no_meio": lambda m: [regular(f"{m}/./x.txt", b"a")],
    "ponto_sozinho": lambda m: [regular(".", m.encode())],
    "vazio": lambda m: [regular("", m.encode())],
    "componente_vazio": lambda m: [regular(f"{m}//x.txt", b"a")],
    "barra_final_no_ustar": lambda m: [regular(f"{m}/x/", b"a")],
    "barra_final_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", f"{m}/x.txt/"),))],
    "git_na_raiz": lambda m: [regular(f".git/{m}", b"a")],
    "git_em_maiusculas": lambda m: [regular(f"{m}/.GIT/HEAD", b"a")],
    "git_misturado": lambda m: [regular(f"{m}/.gIt/config", b"a")],
    "git_como_arquivo": lambda m: [regular(f"{m}/.Git", b"a")],
    "componente_de_256_bytes": lambda m: [regular(f"{m}/" + "c" * 256, b"a")],
    "componente_acentuado_de_256_bytes": lambda m: [regular(f"{m}/" + "ç" * 128, b"a")],
    "nulo_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", f"{m}\0x.txt"),))],
    "barra_invertida": lambda m: [regular(f"{m}\\x.txt", b"a")],
    "controle_u0001": lambda m: [regular(f"{m}\x01.txt", b"a")],
    "controle_u001f": lambda m: [regular(f"{m}\x1f.txt", b"a")],
    "controle_fim_de_linha": lambda m: [regular(f"{m}\n.txt", b"a")],
    "controle_u007f": lambda m: [regular(f"{m}\x7f.txt", b"a")],
    "controle_u0080": lambda m: [regular(f"{m}\x80.txt", b"a")],
    "controle_u009f": lambda m: [regular(f"{m}\x9f.txt", b"a")],
    "controle_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", f"{m}\x05.txt"),))],
    "utf8_invalido_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", m.encode() + b"\xff.txt"),))],
    "utf8_longo_demais_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", m.encode() + b"\xc0\xaf"),))],
    "utf8_substituto_no_pax": lambda m: [regular("a.txt", b"a", pax=(("path", m.encode() + b"\xed\xa0\x80"),))],
    "caminho_de_4097_bytes": lambda m: [regular(_path_of_length(4097, head=m), b"a")],
    "ponto_ponto_no_prefix": lambda m: [regular("x.txt", b"a", prefix=f"{m}/..")],
    "arquivo_e_depois_diretorio": lambda m: [regular(m, b"a"), regular(f"{m}/b.txt", b"b")],
    "diretorio_e_depois_arquivo": lambda m: [regular(f"{m}/b.txt", b"b"), regular(m, b"a")],
    "conflito_em_profundidade": lambda m: [regular(f"q/{m}/b", b"b"), regular(f"q/{m}", b"a")],
}


@pytest.mark.parametrize("case", sorted(PATH_CASES))
def test_path_outside_the_rule_is_path_not_allowed(case: str) -> None:
    """Caminho fora da regra → `(archive, path_not_allowed)`: absoluto; `..` e `.` em qualquer posição; vazio ou com
    componente vazio; barra final (no nome ustar e no `path` do pax); `.git` em qualquer caixa e nível, também como
    arquivo; componente acima de 255 bytes (também contado em bytes UTF-8); nulo; `\\`; controle U+0000 a U+001F e
    U+007F a U+009F; UTF-8 inválido no `path` do pax; caminho acima de 4096 bytes; `..` no `prefix` do ustar; e um
    caminho que é arquivo e prefixo de diretório de outro, nas duas ordens."""
    marker = unique_marker()
    _assert_refused(_with_info(*PATH_CASES[case](marker)), "path_not_allowed", marker=marker)


def test_invalid_utf8_in_the_ustar_name_is_path_not_allowed() -> None:
    """UTF-8 inválido no nome ustar (sem pax: o formato ustar grava os bytes crus) → `path_not_allowed`: um byte
    `0xff`, uma sequência longa demais e um substituto; o nome acentuado válido no mesmo formato é aceito."""
    marker = unique_marker()
    for raw in (b"\xff.txt", b"\xc0\xaf.txt", b"\xed\xa0\x80.txt", b"\x80.txt"):
        archive = make_archive([_info(), regular(marker.encode() + raw, b"a")], fmt=USTAR_FORMAT)
        _assert_refused(archive, "path_not_allowed", marker=marker)
    accepted = _read(make_archive([_info(), regular("questões/ação.txt", b"a")], fmt=USTAR_FORMAT))
    assert [entry.path for entry in accepted.entries] == ["infoCourse.json", "questões/ação.txt"]


def test_repeated_path_is_duplicate() -> None:
    """Caminho repetido → `(archive, duplicate)`: o mesmo arquivo duas vezes, o `infoCourse.json` duas vezes, e o
    mesmo caminho vindo uma vez do nome ustar e outra do `path` do pax. Caixa diferente não é repetição (`A.txt` e
    `a.txt` são aceitos)."""
    marker = unique_marker()
    _assert_refused(_with_info(regular(f"{marker}.txt", b"a"), regular(f"{marker}.txt", b"b")), "duplicate",
                    marker=marker)
    _assert_refused(_with_info(_info()), "duplicate")
    _assert_refused(_with_info(regular(f"{marker}.txt", b"a"),
                               regular("outro.txt", b"b", pax=(("path", f"{marker}.txt"),))),
                    "duplicate", marker=marker)
    summary = _read(_with_info(regular("A.txt", b"A"), regular("a.txt", b"a")))
    assert [entry.path for entry in summary.entries] == ["infoCourse.json", "A.txt", "a.txt"]


def test_missing_info_course_is_not_a_course() -> None:
    """Sem `infoCourse.json` na raiz → `(archive, not_a_course)`: só questões, o arquivo numa subpasta, o nome com
    outra caixa e o pacote sem nenhum membro (só os blocos do fim)."""
    marker = unique_marker()
    for members in ([regular(f"questions/{marker}/info.json", b"{}\n")],
                    [regular(f"{marker}/infoCourse.json", INFO_COURSE)],
                    [regular("InfoCourse.json", INFO_COURSE), regular(f"{marker}.txt", b"a")],
                    []):
        _assert_refused(make_archive(members), "not_a_course", marker=marker)


# --------------------------------------------------------------------------------------------------
# Os tetos e a memória
# --------------------------------------------------------------------------------------------------


def test_declared_content_above_the_ceiling_is_too_large_before_reading_it() -> None:
    """Conteúdo declarado acima de 100 MiB → `(archive, too_large)` no cabeçalho, antes de ler o conteúdo: um membro
    que declara 100 MiB + 1 sem trazer os bytes (lido até o fim, seria um tar truncado) e a soma de dois membros (60
    MiB de verdade e outros 60 MiB só declarados); o pico de memória fica abaixo de 32 MiB além do pacote."""
    _assert_refused_within_memory(
        lambda: _with_info(regular("questions/grande.bin", b"", size=REFERENCE_MAX_CONTENT_BYTES + 1)), "too_large")
    _assert_refused_within_memory(
        lambda: _with_info(regular("questions/a.bin", Fill(60 * MiB, b"a")),
                           regular("questions/b.bin", b"", size=60 * MiB)), "too_large")


def test_content_bomb_is_too_large_within_memory() -> None:
    """A bomba no conteúdo (um arquivo de 100 MiB + 1 muito compressível) → `too_large`, com o pico de memória
    abaixo de 32 MiB além do pacote."""
    _assert_refused_within_memory(content_bomb_archive, "too_large")


def test_header_bomb_is_too_large_within_memory() -> None:
    """Um cabeçalho `x` que declara 300 MB (com o conteúdo de verdade, muito compressível) → `too_large`, conferido
    no cabeçalho do `x`, com o pico de memória abaixo de 32 MiB além do pacote; um `x` de 8 KiB + 1 também é
    `too_large`."""
    _assert_refused_within_memory(header_bomb_archive, "too_large")
    pairs = _pax_of_size(EXTENDED_HEADER_CEILING + 1, "a.txt")
    _assert_refused(_with_info(regular("a.txt", b"a", pax=pairs)), "too_large")


def test_zeros_after_the_end_above_the_stream_ceiling_are_too_large_within_memory() -> None:
    """Zeros depois do fim do tar até passar do teto do fluxo descomprimido (o conteúdo mais 10 KiB por membro) →
    `too_large`, com o pico de memória abaixo de 32 MiB além do pacote."""
    _assert_refused_within_memory(lambda: _zeros_after_end(STREAM_CEILING + MiB), "too_large")


def test_many_small_headers_are_too_many_within_memory() -> None:
    """Muitos cabeçalhos pequenos: 20001 membros, cada um com um `x` de quase 8 KiB (o fluxo abaixo do teto) →
    `too_many`, com o pico de memória abaixo de 32 MiB além do pacote."""
    _assert_refused_within_memory(lambda: make_archive(_many_with_extended_headers(REFERENCE_MAX_FILES + 1)),
                                  "too_many")


def test_too_many_files_and_the_caller_ceilings() -> None:
    """20001 arquivos → `(archive, too_many)`, com o pico de memória abaixo de 32 MiB além do pacote, e 20000 são
    aceitos; os tetos passados pelo chamador valem no limite: 3 arquivos com `max_files=2` → `too_many` e 2
    aceitos; 11 bytes de conteúdo com `max_bytes=10` → `too_large` e 10 aceitos."""
    _assert_refused_within_memory(lambda: make_archive([_info(), *many_files(REFERENCE_MAX_FILES)]), "too_many")
    summary = _read(make_archive([_info(), *many_files(REFERENCE_MAX_FILES - 1)]))
    assert summary.file_count == REFERENCE_MAX_FILES

    small = make_archive([regular("infoCourse.json", b"{}\n"), regular("a.txt", b"a"), regular("b.txt", b"b")])
    _assert_refused(small, "too_many", max_files=2)
    assert _read(make_archive([regular("infoCourse.json", b"{}\n"), regular("a.txt", b"a")]),
                 max_files=2).file_count == 2
    _assert_refused(make_archive([regular("infoCourse.json", b"{}\n"), regular("a.txt", b"12345678")]),
                    "too_large", max_bytes=10)
    assert _read(make_archive([regular("infoCourse.json", b"{}"), regular("a.txt", b"12345678")]),
                 max_bytes=10).byte_count == 10


# --------------------------------------------------------------------------------------------------
# A ordem das regras
# --------------------------------------------------------------------------------------------------


def test_rules_apply_member_by_member_in_arrival_order() -> None:
    """A leitura para na primeira recusa, membro a membro, e as regras do fim (os blocos de zeros, o fim do gzip e o
    `infoCourse.json`) valem só depois do último membro:
    - um link no primeiro membro seguido de 20001 arquivos → `entry_not_allowed`;
    - 20001 arquivos seguidos de um link → `too_many`, e 20000 arquivos seguidos de um link também (o 20001º
      membro, de qualquer tipo);
    - sem `infoCourse.json` e com um caminho repetido → `duplicate`;
    - sem `infoCourse.json` e com um byte não zero depois do fim → `invalid_format`;
    - um `x` acima de 8 KiB antes de um link → `too_large`; um link com caminho fora da regra →
      `entry_not_allowed`; um repetido antes de um caminho fora da regra → `duplicate`, e o contrário →
      `path_not_allowed`; uma soma errada depois de um link → `entry_not_allowed`."""
    _assert_refused(make_archive([_symlink("link"), *many_files(REFERENCE_MAX_FILES + 1)]), "entry_not_allowed")
    _assert_refused(make_archive([*many_files(REFERENCE_MAX_FILES + 1), _symlink("link")]), "too_many")
    _assert_refused(make_archive([*many_files(REFERENCE_MAX_FILES), _symlink("link")]), "too_many")
    _assert_refused(make_archive([regular("a.txt", b"a"), regular("a.txt", b"a")]), "duplicate")
    _assert_refused(make_archive([regular("a.txt", b"a")], trailer=b"\x01"), "invalid_format")

    big_pax = _pax_of_size(EXTENDED_HEADER_CEILING + 1, "link")
    _assert_refused(_with_info(Member("link", type=b"2", linkname="infoCourse.json", pax=big_pax)), "too_large")
    _assert_refused(_with_info(_symlink("../fora")), "entry_not_allowed")
    _assert_refused(_with_info(regular("a.txt", b"a"), regular("a.txt", b"a"), regular("../b.txt", b"b")),
                    "duplicate")
    _assert_refused(_with_info(regular("../b.txt", b"b"), regular("a.txt", b"a"), regular("a.txt", b"a")),
                    "path_not_allowed")
    _assert_refused(_with_info(_symlink("link"), regular("a.txt", b"a", checksum_delta=1)), "entry_not_allowed")


# --------------------------------------------------------------------------------------------------
# Os pacotes aceitos e o resumo
# --------------------------------------------------------------------------------------------------


def _expected_entries(files: Mapping[str, bytes], order: list[str]) -> tuple[Any, ...]:
    from cq_server.banks import BankEntry

    return tuple(BankEntry(path=path, size=len(files[path]), sha256=hashlib.sha256(files[path]).hexdigest())
                 for path in order)


def _assert_summary(summary: Any, files: Mapping[str, bytes], order: list[str]) -> None:
    assert summary.entries == _expected_entries(files, order)
    assert summary.file_count == len(files)
    assert summary.byte_count == sum(len(data) for data in files.values())
    assert summary.question_count == reference_question_count(files)


def test_archive_written_by_tarfile_in_pax_format_is_accepted() -> None:
    """O pacote gerado pelo `tarfile` em `PAX_FORMAT` (como o `cq` grava: só arquivos, em ordem dos bytes do
    caminho, com um nome acentuado e um caminho de 200 bytes, que vão ao `path` do pax) é aceito, com as entradas na
    ordem do pacote, o tamanho, o SHA-256, as contagens e a versão de referência."""
    from cq_server.banks import bank_version

    long_path = f"dados/{'p' * 188}/x.txt"
    assert len(long_path.encode("utf-8")) == 200
    files = {
        "infoCourse.json": INFO_COURSE,
        "questions/ação/info.json": b'{"title": "A"}\n',
        "questions/q1/info.json": b'{"title": "Q1"}\n',
        "questions/q1/question.html": "<p>Quanto é 1 + 1?</p>\n".encode(),
        long_path: b"longo\n",
        "zé.bin": bytes(range(256)),
        "vazio.txt": b"",
    }
    order = sorted(files, key=lambda p: p.encode("utf-8"))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in order:
            info = tarfile.TarInfo(path)
            info.size = len(files[path])
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(files[path]))
    summary = _read(buffer.getvalue())
    _assert_summary(summary, files, order)
    assert summary.question_count == 2
    assert bank_version(summary.entries) == reference_version(files)


def test_archive_with_every_accepted_form_is_accepted() -> None:
    """Um pacote com toda forma aceita: o tipo `\\0`, todas as chaves pax da lista (`path`, `size`, `mtime`,
    `atime`, `ctime`, `uid`, `gid`, `uname`, `gname`), o nome longo no `prefix` do ustar, um componente de 255 bytes
    (também em bytes UTF-8), um caminho de 4096 bytes, um `x` de 8 KiB exatos, `A.txt` e `a.txt`, ocultos e
    `__pycache__`, um arquivo vazio e um conteúdo de 1 MiB; as entradas saem na ordem do pacote (não ordenadas)."""
    from cq_server.banks import bank_version

    component = "c" * 255
    accented = "ç" * 127 + "x"
    path_4096 = _path_of_length(4096, head="fundo")
    ustar_long = f"{'u' * 120}/{'v' * 90}.txt"
    pax_8192 = _pax_of_size(EXTENDED_HEADER_CEILING, "x8k.txt")
    big = hashlib.sha256(b"semente").digest() * (MiB // 32)
    files = {
        "zz.txt": b"ultimo na ordem dos bytes, primeiro no pacote\n",
        "infoCourse.json": INFO_COURSE,
        "antigo.txt": b"tipo nulo\n",
        "todas.txt": b"todas as chaves\n",
        ustar_long: b"no prefix\n",
        f"d/{component}": b"255\n",
        f"d/{accented}": b"255 em UTF-8\n",
        path_4096: b"4096\n",
        "x8k.txt": b"x de 8 KiB\n",
        "A.txt": b"A\n",
        "a.txt": b"a\n",
        ".oculto": b"oculto\n",
        "__pycache__/m.cpython-312.pyc": b"\x00\x01",
        "vazio.txt": b"",
        "questions/q1/info.json": b"{}\n",
        "questions/q1/grande.bin": big,
    }
    members = []
    for path, data in files.items():
        if path == "antigo.txt":
            members.append(regular(path, data, type=b"\0"))
        elif path == "todas.txt":
            members.append(regular("ignorado.txt", data, pax=(
                ("path", path), ("size", str(len(data))), ("mtime", "1700000000.5"), ("atime", "1700000000"),
                ("ctime", "1700000000"), ("uid", "1000"), ("gid", "1000"), ("uname", "prof"), ("gname", "curso"))))
        elif path == ustar_long:
            members.append(regular(path.split("/")[1], data, prefix=path.split("/")[0]))
        elif path == "x8k.txt":
            members.append(regular("x8k.txt", data, pax=pax_8192))
        else:
            members.append(regular(path, data))
    summary = _read(make_archive(members))
    _assert_summary(summary, files, list(files))
    assert summary.question_count == 1
    assert bank_version(summary.entries) == reference_version(files)
    ustar_only = _read(make_archive([_info(), regular(ustar_long, b"no prefix\n")], fmt=USTAR_FORMAT))
    assert [entry.path for entry in ustar_only.entries] == ["infoCourse.json", ustar_long]


class _Sink(io.BytesIO):
    """O arquivo que o `open_member` devolve: guarda cada pedaço escrito (o conteúdo sobrevive ao `close`)."""

    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[bytes] = []

    def write(self, data: Any) -> int:
        piece = bytes(data)
        self.pieces.append(piece)
        return super().write(piece)


def test_open_member_receives_each_member_whole_and_in_pieces() -> None:
    """Com `open_member`, cada membro é aberto uma vez, pelo caminho, na ordem do pacote, e recebe o conteúdo
    inteiro (os mesmos bytes) em pedaços de até 64 KiB; o conteúdo de 1 MiB chega em mais de um pedaço; o resumo
    é o mesmo da leitura sem `open_member`."""
    big = b"".join(hashlib.sha256(index.to_bytes(4, "big")).digest() for index in range(MiB // 32))
    files = {
        "infoCourse.json": INFO_COURSE,
        "questions/q1/info.json": b'{"title": "Q1"}\n',
        "questions/q1/dados.bin": big,
        "vazio.txt": b"",
        "zé.txt": "acentuado\n".encode(),
    }
    archive = archive_of(files)
    opened: list[str] = []
    sinks: dict[str, _Sink] = {}

    def open_member(path: str) -> _Sink:
        assert isinstance(path, str)
        opened.append(path)
        sinks[path] = _Sink()
        return sinks[path]

    summary = _read(archive, open_member=open_member)
    order = sorted(files, key=lambda p: p.encode("utf-8"))
    assert opened == order
    for path in order:
        pieces = sinks[path].pieces
        assert b"".join(pieces) == files[path], path
        assert all(len(piece) <= CHUNK_CEILING for piece in pieces), path
    assert len([piece for piece in sinks["questions/q1/dados.bin"].pieces if piece]) > 1
    _assert_summary(summary, files, order)
    assert _read(archive) == summary


@pytest.mark.parametrize("case", [case["id"] for case in load_vectors()["cases"]])
def test_question_count_skips_questions_inside_another_question(case: str) -> None:
    """A contagem de questões: cada `questions/<um ou mais componentes>/info.json`, menos os que ficam dentro do
    diretório de outra questão (o vetor `aninhada`: `questions/a/b/info.json` não conta porque `questions/a/` é
    questão, e `questions/x/y/info.json` conta). Cada vetor dá a contagem gravada nele e a versão dele."""
    from cq_server.banks import bank_version

    vector = next(item for item in load_vectors()["cases"] if item["id"] == case)
    files = vector_files(vector)
    summary = _read(archive_of(files))
    assert summary.question_count == vector["question_count"] == reference_question_count(files)
    _assert_summary(summary, files, sorted(files, key=lambda p: p.encode("utf-8")))
    assert bank_version(summary.entries) == vector["version"]
