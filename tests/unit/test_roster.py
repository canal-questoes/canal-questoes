"""Leitor do arquivo da lista da turma.


`cq_client.roster.read_roster(path)` lê o arquivo em bytes (até `MAX_BYTES` = 4 MiB), decodifica em
`utf-8-sig` estrito (um BOM tolerado), escolhe o separador (`,` ou `;`) pelo cabeçalho da linha 1
(exatamente `ra`, `nome` e `email`, em qualquer ordem, sem diferenciar maiúsculas, sem aparar, sem colunas
extras) e devolve `Roster(rows, lines)`: cada linha de aluno como `{"ra", "name", "email"}`, com os campos
**sem aparar** (as regras de validação ficam na API), e `lines[i]`, a linha física do arquivo em que `rows[i]`
**começa** (o cabeçalho é a linha 1). Depois do cabeçalho, só os registros com todos os campos vazios são
ignorados (`,,` com `,`, `;;` com `;` e `""` nos dois); `;;` num arquivo com `,`, e `,,` num arquivo com
`;`, são um campo só, não vazio. Fora do formato: `RosterFileError(reason)`,
com uma das mensagens fixas do cliente, sem caminho nem conteúdo. `row_line(field, lines)` traduz um
`rows[i]` de um erro da API para a linha do arquivo.

Os arquivos são escritos em bytes em `tmp_path`. O caminho de cada arquivo tem o marcador
`SentinelaCaminho`, e o conteúdo de cada arquivo recusado tem o marcador `SentinelaConteudo` numa linha que
não decide o erro: nenhum dos dois aparece na mensagem, no `repr`, nos `args` nem no traceback formatado.

No esqueleto, `read_roster` e `row_line` lançam `NotImplementedError`; `MAX_ROWS`, `MAX_BYTES`, `Roster` e
`RosterFileError` são reais, e as asserções sobre eles ficam em funções que passam por `read_roster`.

"""

from __future__ import annotations

import csv
import pathlib
import traceback

import pytest

from cq_client.roster import MAX_BYTES, MAX_ROWS, Roster, RosterFileError, read_roster, row_line

# --- mensagens exatas ---------------------------------------------------------------

READ_REASON = "não foi possível ler o arquivo da lista."
SIZE_REASON = "o arquivo da lista passa de 4 MiB."
HEADER_REASON = ("a primeira linha do arquivo da lista deve ter exatamente as colunas ra, nome e email, "
                 "separadas por vírgula ou por ponto e vírgula.")
EMPTY_REASON = "o arquivo da lista não tem nenhuma linha de aluno."
TOO_MANY_REASON = "o arquivo da lista tem mais de 1.000 linhas de alunos."


def utf8_reason(line: int) -> str:
    return f"o arquivo da lista não está em UTF-8 (linha {line})."


def csv_reason(line: int) -> str:
    return f"o arquivo da lista não é um CSV válido na linha {line}."


def columns_reason(line: int) -> str:
    return f"a linha {line} do arquivo da lista não tem as 3 colunas do cabeçalho."


# --- arquivos --------------------------------------------------------------------------------------

BOM = b"\xef\xbb\xbf"
PATH_SENTINEL = "SentinelaCaminho"
CONTENT_SENTINEL = "SentinelaConteudo"
FILE_NAME = f"lista-{PATH_SENTINEL}.csv"
# Uma linha de aluno válida (separador `,`) com o marcador de conteúdo, para os arquivos recusados.
SENTINEL_ROW = f"100009,Aluna {CONTENT_SENTINEL},aluna.sentinela@example.org\n".encode()

# Limite de campo do módulo `csv` (padrão da biblioteca): campo acima de
# 131.072 caracteres.
CSV_FIELD_LIMIT = 131_072


def _write(tmp_path: pathlib.Path, data: bytes, name: str = FILE_NAME) -> str:
    """Grava `data` em bytes num arquivo de `tmp_path` e devolve o caminho como texto."""
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _row(ra: str, name: str, email: str) -> dict[str, str]:
    return {"ra": ra, "name": name, "email": email}


def _assert_roster_error(path: str, reason: str) -> None:
    """`read_roster(path)` lança `RosterFileError` com `reason` exata (também em `str`); nem a mensagem,
    nem o `repr`, nem os `args`, nem o traceback formatado (com as exceções encadeadas) contêm o caminho,
    o marcador do caminho ou o marcador do conteúdo (sem caminho nem conteúdo)."""
    with pytest.raises(RosterFileError) as excinfo:
        read_roster(path)
    exc = excinfo.value
    assert exc.reason == reason
    assert str(exc) == reason
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        assert path not in text
        assert PATH_SENTINEL not in text
        assert CONTENT_SENTINEL not in text


# Três alunos neutros; o terceiro com acentos (prova a decodificação UTF-8).
ANA = _row("100001", "Ana Souza", "ana.souza@example.org")
BRUNO = _row("100002", "Bruno Lima", "Bruno.Lima@Example.ORG")
JOAO = _row("100003", "João Conceição", "joao.conceicao@example.org")
CSV_BODY = ("100001,Ana Souza,ana.souza@example.org{nl}100002,Bruno Lima,Bruno.Lima@Example.ORG{nl}"
            "100003,João Conceição,joao.conceicao@example.org")


def _comma_file(newline: str, *, final_newline: bool = True) -> bytes:
    text = "ra,nome,email" + newline + CSV_BODY.format(nl=newline)
    return (text + (newline if final_newline else "")).encode()


# --- leitura básica --------------------------------------------------------------------------------

BASIC_FILES = [
    ("lf", _comma_file("\n")),
    ("crlf", _comma_file("\r\n")),
    ("cr-sozinho", _comma_file("\r")),
    ("sem-quebra-no-fim", _comma_file("\n", final_newline=False)),
    ("bom-lf", BOM + _comma_file("\n")),
    ("bom-crlf", BOM + _comma_file("\r\n")),
]


@pytest.mark.parametrize("data", [d for _, d in BASIC_FILES], ids=[i for i, _ in BASIC_FILES])
def test_reads_rows_and_lines(tmp_path: pathlib.Path, data: bytes) -> None:
    """UTF-8, BOM tolerado, cabeçalho na primeira linha (`utf-8-sig` estrito,
    `StringIO(newline="")`; `\\r` sozinho, `\\r\\n` e `\\n` como fim de
    linha; `lines[i]` = linha física em que `rows[i]` começa, cabeçalho = 1): o `Roster` exato, com as
    chaves `ra`, `name` e `email` e os valores como estão no arquivo."""
    roster = read_roster(_write(tmp_path, data))
    assert isinstance(roster, Roster)
    assert roster == Roster(rows=[ANA, BRUNO, JOAO], lines=[2, 3, 4])


SEPARATOR_FILES = [
    ("ponto-e-virgula",
     b"ra;nome;email\n100001;Ana Souza;ana.souza@example.org\n100002;Bruno Lima;Bruno.Lima@Example.ORG\n",
     [ANA, BRUNO]),
    ("ponto-e-virgula-com-virgula-no-campo",
     b"ra;nome;email\r\n100001;Souza, Ana;ana.souza@example.org\r\n",
     [_row("100001", "Souza, Ana", "ana.souza@example.org")]),
    ("virgula-com-ponto-e-virgula-no-campo",
     b"ra,nome,email\n100001,Souza; Ana,ana.souza@example.org\n",
     [_row("100001", "Souza; Ana", "ana.souza@example.org")]),
    ("bom-ponto-e-virgula",
     BOM + b"ra;nome;email\n100001;Ana Souza;ana.souza@example.org\n",
     [ANA]),
]


@pytest.mark.parametrize(("data", "rows"), [(d, r) for _, d, r in SEPARATOR_FILES],
                         ids=[i for i, *_ in SEPARATOR_FILES])
def test_separator_comes_from_the_header(tmp_path: pathlib.Path, data: bytes,
                                         rows: list[dict[str, str]]) -> None:
    """Separador vírgula ou ponto e vírgula (o do cabeçalho vale para o arquivo inteiro), escolhido
    (separador pelo cabeçalho, sem `csv.Sniffer`): o outro separador, sem aspas, é conteúdo do campo."""
    roster = read_roster(_write(tmp_path, data))
    assert roster == Roster(rows=rows, lines=list(range(2, 2 + len(rows))))


HEADER_ORDERS = [
    ("email-ra-nome", b"email,ra,nome\nana.souza@example.org,100001,Ana Souza\n"),
    ("RA-Nome-Email-ponto-e-virgula", b"RA;Nome;Email\n100001;Ana Souza;ana.souza@example.org\n"),
    ("Nome-EMAIL-rA-crlf", b"Nome;EMAIL;rA\r\nAna Souza;ana.souza@example.org;100001\r\n"),
    ("NOME-RA-EMAIL-bom", BOM + b"NOME,RA,EMAIL\nAna Souza,100001,ana.souza@example.org\n"),
]


@pytest.mark.parametrize("data", [d for _, d in HEADER_ORDERS], ids=[i for i, _ in HEADER_ORDERS])
def test_header_in_any_order_and_case(tmp_path: pathlib.Path, data: bytes) -> None:
    """Colunas `ra`, `nome` e `email` em qualquer ordem, sem diferenciar maiúsculas
    (células comparadas com `.lower()`): cada campo vai para a sua chave, pela posição no cabeçalho."""
    assert read_roster(_write(tmp_path, data)) == Roster(rows=[ANA], lines=[2])


def test_quoted_fields_with_separator_quotes_and_line_breaks(tmp_path: pathlib.Path) -> None:
    """Aspas do CSV padrão (`csv.reader(strict=True)`, dialeto `excel`); e
    (`lines[i]` é a linha em que o registro **começa**): o separador e as aspas duplicadas dentro de aspas
    são conteúdo; a quebra de linha dentro de aspas (`\\n` ou `\\r\\n`) fica no campo, e o registro
    seguinte tem a linha física certa."""
    comma = (b'ra,nome,email\n'
             b'100001,"Souza, Ana",ana.souza@example.org\n'
             b'100002,"Lima ""Bruno""",Bruno.Lima@Example.ORG\n'
             b'100003,"Ana\nSouza",ana@example.org\n'
             b'100004,"Bruno\r\nLima",bruno@example.org\n'
             b'100005,Caio Prado,caio@example.org\n')
    assert read_roster(_write(tmp_path, comma)) == Roster(
        rows=[_row("100001", "Souza, Ana", "ana.souza@example.org"),
              _row("100002", 'Lima "Bruno"', "Bruno.Lima@Example.ORG"),
              _row("100003", "Ana\nSouza", "ana@example.org"),
              _row("100004", "Bruno\r\nLima", "bruno@example.org"),
              _row("100005", "Caio Prado", "caio@example.org")],
        lines=[2, 3, 4, 6, 8],
    )
    semicolon = (b'ra;nome;email\n'
                 b'100001;"Souza; Ana";ana.souza@example.org\n'
                 b'100002;"Bruno\nde\nLima";bruno@example.org\n'
                 b'100003;Caio Prado;caio@example.org\n')
    assert read_roster(_write(tmp_path, semicolon, "lista-2.csv")) == Roster(
        rows=[_row("100001", "Souza; Ana", "ana.souza@example.org"),
              _row("100002", "Bruno\nde\nLima", "bruno@example.org"),
              _row("100003", "Caio Prado", "caio@example.org")],
        lines=[2, 3, 6],
    )


# --- registros vazios, por separador ----------------------------------

IGNORED_EMPTY = [
    ("virgulas-em-arquivo-com-virgula", ",", b",,"),
    ("tres-virgulas-em-arquivo-com-virgula", ",", b",,,"),
    ("pontos-e-virgulas-em-arquivo-com-ponto-e-virgula", ";", b";;"),
    ("aspas-vazias-em-arquivo-com-virgula", ",", b'""'),
    ("aspas-vazias-em-arquivo-com-ponto-e-virgula", ";", b'""'),
    ("tres-aspas-vazias-em-arquivo-com-virgula", ",", b'"","",""'),
]


@pytest.mark.parametrize(("sep", "empty"), [(s, e) for _, s, e in IGNORED_EMPTY],
                         ids=[i for i, *_ in IGNORED_EMPTY])
def test_records_with_only_empty_fields_are_ignored(tmp_path: pathlib.Path, sep: str,
                                                    empty: bytes) -> None:
    """Registros vazios (depois do cabeçalho, os registros com todos
    os campos vazios são ignorados: `,,` num arquivo com vírgula, `;;` num arquivo com ponto e vírgula e
    `""` nos dois): o registro vazio não entra em `rows`, e as linhas seguintes mantêm a linha
    física."""
    s = sep.encode()
    data = (b"ra" + s + b"nome" + s + b"email\n"
            + b"100001" + s + b"Ana Souza" + s + b"ana.souza@example.org\n"
            + empty + b"\n"
            + b"100002" + s + b"Bruno Lima" + s + b"Bruno.Lima@Example.ORG\n"
            + empty + b"\n")
    assert read_roster(_write(tmp_path, data)) == Roster(rows=[ANA, BRUNO], lines=[2, 4])


OTHER_SEPARATOR_EMPTY = [
    ("pontos-e-virgulas-em-arquivo-com-virgula", b"ra,nome,email\n" + SENTINEL_ROW + b";;\n", 3),
    ("virgulas-em-arquivo-com-ponto-e-virgula",
     b"ra;nome;email\n100009;Aluna " + CONTENT_SENTINEL.encode() + b";a@example.org\n,,\n", 3),
    ("pontos-e-virgulas-depois-de-linhas-vazias",
     b"ra,nome,email\n" + SENTINEL_ROW + b"\n\n;;\n", 5),
]


@pytest.mark.parametrize(("data", "line"), [(d, n) for _, d, n in OTHER_SEPARATOR_EMPTY],
                         ids=[i for i, *_ in OTHER_SEPARATOR_EMPTY])
def test_empty_record_of_the_other_separator_is_a_column_error(tmp_path: pathlib.Path, data: bytes,
                                                               line: int) -> None:
    """Registro vazio do outro separador (`;;` num arquivo com vírgula, e `,,` num
    arquivo com ponto e vírgula, são um campo só, não vazio): `a linha N
    do arquivo da lista não tem as 3 colunas do cabeçalho.`, com a linha física do registro."""
    _assert_roster_error(_write(tmp_path, data), columns_reason(line))


def test_blank_lines_are_skipped_and_lines_stay_physical(tmp_path: pathlib.Path) -> None:
    """Linhas vazias depois do cabeçalho ignoradas (`lines[i]` conta as
    linhas vazias e os registros vazios pulados, também com `\\r\\n` e com `\\r` sozinho)."""
    lf = (b"ra,nome,email\n\n"
          b"100001,Ana Souza,ana.souza@example.org\n\n\n"
          b"100002,Bruno Lima,Bruno.Lima@Example.ORG\n,,\n"
          b"100003,Jo\xc3\xa3o Concei\xc3\xa7\xc3\xa3o,joao.conceicao@example.org\n\n")
    assert read_roster(_write(tmp_path, lf)) == Roster(rows=[ANA, BRUNO, JOAO], lines=[3, 6, 8])
    crlf = lf.replace(b"\n", b"\r\n")
    assert read_roster(_write(tmp_path, crlf, "lista-crlf.csv")) == Roster(rows=[ANA, BRUNO, JOAO],
                                                                           lines=[3, 6, 8])
    cr = lf.replace(b"\n", b"\r")
    assert read_roster(_write(tmp_path, cr, "lista-cr.csv")) == Roster(rows=[ANA, BRUNO, JOAO],
                                                                       lines=[3, 6, 8])


def test_fields_are_passed_through_untrimmed(tmp_path: pathlib.Path) -> None:
    """A API apara o nome e o e-mail, e o RA não é aparado (células sem aparar; só os
    registros inteiramente vazios são ignorados): espaços, tabulações, zeros à esquerda e caixa chegam como
    estão; um registro só de espaços em 3 campos não é vazio e vira uma linha."""
    data = (b"ra,nome,email\n"
            b" 100001 , Ana  Souza ,\tAna.Souza@Example.org \n"
            b" , , \n"
            b"000123,\"  Bruno Lima  \",bruno@example.org\n")
    assert read_roster(_write(tmp_path, data)) == Roster(
        rows=[_row(" 100001 ", " Ana  Souza ", "\tAna.Souza@Example.org "),
              _row(" ", " ", " "),
              _row("000123", "  Bruno Lima  ", "bruno@example.org")],
        lines=[2, 3, 4],
    )


# --- tetos (arquivo grande) ------------------------------------------------------

def _students(count: int, *, blank_between: bool = False) -> bytes:
    rows = []
    for i in range(count):
        rows.append(b"%d,Aluno %d,aluno%d@example.org\n" % (200000 + i, i, i))
        if blank_between:
            rows.append(b"\n,,\n")
    return b"".join(rows)


def test_row_limit_accepts_1000_and_refuses_1001(tmp_path: pathlib.Path) -> None:
    """A mensagem `o arquivo da lista tem mais de 1.000 linhas de alunos.`; arquivo
    grande (`MAX_ROWS == 1000`, real no esqueleto, afirmado nesta função): 1.000 linhas de
    alunos são aceitas (também com linhas vazias e registros vazios intercalados, que não contam); 1.001
    são recusadas, sem conteúdo nem caminho na mensagem."""
    assert MAX_ROWS == 1000
    exact = read_roster(_write(tmp_path, b"ra,nome,email\n" + _students(1000), "lista-1000.csv"))
    assert len(exact.rows) == 1000
    assert exact.rows[0] == _row("200000", "Aluno 0", "aluno0@example.org")
    assert exact.rows[999] == _row("200999", "Aluno 999", "aluno999@example.org")
    assert exact.lines == list(range(2, 1002))
    spaced = read_roster(_write(tmp_path, b"ra,nome,email\n" + _students(1000, blank_between=True),
                                "lista-1000-espacada.csv"))
    assert spaced.rows == exact.rows
    assert spaced.lines == [2 + 3 * i for i in range(1000)]
    over = b"ra,nome,email\n" + SENTINEL_ROW + _students(1000)
    _assert_roster_error(_write(tmp_path, over), TOO_MANY_REASON)


def _sized_file(total: int) -> bytes:
    """Um arquivo válido de exatamente `total` bytes, com 1.000 linhas de alunos (nomes longos, cada campo
    abaixo do limite do `csv`)."""
    head = b"ra,nome,email\n"
    lines = [b"%d,%s,aluno%d@example.org\n" % (300000 + i, b"n" * 4150, i) for i in range(1000)]
    extra = total - len(head) - sum(map(len, lines))
    assert 0 <= extra < CSV_FIELD_LIMIT - 4150
    lines[-1] = b"%d,%s,aluno%d@example.org\n" % (300999, b"n" * (4150 + extra), 999)
    data = head + b"".join(lines)
    assert len(data) == total
    return data


def test_size_limit_is_4_mib(tmp_path: pathlib.Path) -> None:
    """A mensagem `o arquivo da lista passa de 4 MiB.`; leitura em bytes até `MAX_BYTES`
    (`MAX_BYTES == 4 * 1024 * 1024`, real no esqueleto, afirmado nesta função): um arquivo válido de
    exatamente 4 MiB é aceito; com 1 byte a mais, é recusado pelo tamanho, sem caminho na mensagem."""
    assert MAX_BYTES == 4 * 1024 * 1024
    exact = read_roster(_write(tmp_path, _sized_file(MAX_BYTES), "lista-4mib.csv"))
    assert len(exact.rows) == 1000
    assert exact.rows[0] == _row("300000", "n" * 4150, "aluno0@example.org")
    assert exact.lines == list(range(2, 1002))
    _assert_roster_error(_write(tmp_path, _sized_file(MAX_BYTES + 1)), SIZE_REASON)


# --- arquivos que não se leem ----------------------------------------------------------------------

def test_missing_file_and_directory_cannot_be_read(tmp_path: pathlib.Path) -> None:
    """Não abre ou não lê → `não foi possível ler o arquivo da lista.`
    (inexistente, diretório): a mesma mensagem, sem o caminho."""
    missing = tmp_path / f"nao-existe-{PATH_SENTINEL}.csv"
    _assert_roster_error(str(missing), READ_REASON)
    directory = tmp_path / f"pasta-{PATH_SENTINEL}"
    directory.mkdir()
    _assert_roster_error(str(directory), READ_REASON)


# --- codificação -----------------------------------------------------------------------------------

NOT_UTF8 = [
    ("cp1252-linha-3", ("ra,nome,email\n1,Aluna " + CONTENT_SENTINEL + ",a@example.org\n"
                        "2,José,jose@example.org\n").encode("cp1252"), 3),
    ("cp1252-crlf-linha-2", ("ra,nome,email\r\n2,José " + CONTENT_SENTINEL + ",jose@example.org\r\n"
                             ).encode("cp1252"), 2),
    ("cp1252-cr-sozinho-linha-3", ("ra,nome,email\r1,Aluna " + CONTENT_SENTINEL + ",a@example.org\r"
                                   "2,José,jose@example.org\r").encode("cp1252"), 3),
    ("cp1252-depois-de-linha-vazia-linha-4", ("ra,nome,email\n1,Aluna " + CONTENT_SENTINEL
                                              + ",a@example.org\n\n4,José,jose@example.org\n"
                                              ).encode("cp1252"), 4),
    ("cp1252-depois-de-registro-multilinha-linha-4",
     ('ra,nome,email\n1,"Aluna\n' + CONTENT_SENTINEL + '",a@example.org\n2,José,jose@example.org\n'
      ).encode("cp1252"), 4),
    ("cp1252-com-bom-linha-3", BOM + ("ra,nome,email\n1,Aluna " + CONTENT_SENTINEL
                                      + ",a@example.org\n2,José,jose@example.org\n").encode("cp1252"), 3),
    ("utf-16-linha-1", ("ra,nome,email\n1,Aluna " + CONTENT_SENTINEL + ",a@example.org\n"
                        ).encode("utf-16"), 1),
]


@pytest.mark.parametrize(("data", "line"), [(d, n) for _, d, n in NOT_UTF8],
                         ids=[i for i, *_ in NOT_UTF8])
def test_not_utf8_is_refused_with_the_line(tmp_path: pathlib.Path, data: bytes, line: int) -> None:
    """UTF-8 (`utf-8-sig` estrito), com a mensagem (`o arquivo da lista não está em UTF-8
    (linha N).`): N é a linha física do primeiro byte inválido (`\\n`, `\\r\\n` e `\\r` sozinho contam uma
    quebra cada; o BOM não conta); UTF-16 (com o BOM `FF FE`) falha no primeiro byte; sem conteúdo nem
    caminho na mensagem."""
    _assert_roster_error(_write(tmp_path, data), utf8_reason(line))


# --- cabeçalho -------------------------------------------------------------------------------------

HEADER_ERRORS = [
    ("espacos-depois-da-virgula", b"ra, nome, email\n" + SENTINEL_ROW),
    ("espaco-no-fim", b"ra,nome,email \n" + SENTINEL_ROW),
    ("ra-repetido", b"ra,ra,email\n" + SENTINEL_ROW),
    ("coluna-extra-vazia", b"ra,nome,email,\n" + SENTINEL_ROW),
    ("coluna-extra", b"ra,nome,email,turma\n" + SENTINEL_ROW),
    ("coluna-a-menos", b"ra,nome\n" + SENTINEL_ROW),
    ("e-mail-com-hifen", b"E-mail;ra;nome\n" + SENTINEL_ROW),
    ("separador-tabulacao", b"ra\tnome\temail\n" + SENTINEL_ROW),
    ("separadores-misturados", b"ra,nome;email\n" + SENTINEL_ROW),
    ("depois-de-linha-vazia", b"\nra,nome,email\n" + SENTINEL_ROW),
    ("depois-de-linha-vazia-crlf", b"\r\nra,nome,email\r\n" + SENTINEL_ROW),
    ("sem-cabecalho", SENTINEL_ROW + b"100002,Bruno Lima,bruno@example.org\n"),
    ("bom-duplo", BOM + BOM + b"ra,nome,email\n" + SENTINEL_ROW),
    ("arquivo-vazio", b""),
    ("so-o-bom", BOM),
]


@pytest.mark.parametrize("data", [d for _, d in HEADER_ERRORS], ids=[i for i, _ in HEADER_ERRORS])
def test_header_must_be_exactly_ra_nome_email_on_line_1(tmp_path: pathlib.Path, data: bytes) -> None:
    """Cabeçalho na primeira linha (com exatamente `ra`, `nome` e `email`, sem espaços e sem
    colunas extras; o cabeçalho com espaço ou precedido de linha vazia é erro de uso; sem aparar;
    sem colunas repetidas nem extras; um único BOM tolerado; cabeçalho ausente ou errado,
    ou arquivo vazio): a mensagem exata do cabeçalho, sem conteúdo nem caminho."""
    _assert_roster_error(_write(tmp_path, data), HEADER_REASON)


# --- CSV malformado --------------------------------------------------------------------------------

MALFORMED = [
    ("aspas-sem-fechamento", b"ra,nome,email\n" + SENTINEL_ROW + b'100002,"Bruno Lima,bruno@example.org\n',
     3),
    ("aspas-seguidas-de-texto", b"ra,nome,email\n" + SENTINEL_ROW + b'100002,"A"x,bruno@example.org\n', 3),
    ("aspas-seguidas-de-texto-ponto-e-virgula",
     b'ra;nome;email\n100009;"Aluna ' + CONTENT_SENTINEL.encode() + b'";a@example.org\n'
     b'100002;"A"x;bruno@example.org\n', 3),
    ("campo-acima-do-limite", b"ra,nome,email\n" + SENTINEL_ROW
     + b"100002," + b"n" * (CSV_FIELD_LIMIT + 1) + b",bruno@example.org\n", 3),
    ("aspas-sem-fechamento-depois-de-linhas-vazias",
     b"ra,nome,email\n" + SENTINEL_ROW + b'\n\n100002,"Bruno', 5),
]


@pytest.mark.parametrize(("data", "line"), [(d, n) for _, d, n in MALFORMED],
                         ids=[i for i, *_ in MALFORMED])
def test_malformed_csv_is_refused_with_the_line(tmp_path: pathlib.Path, data: bytes, line: int) -> None:
    """Aspas do CSV padrão, com `csv.reader(strict=True)` (CSV malformado:
    aspas, campo acima de 131.072 caracteres → `o arquivo da lista não é um CSV válido na linha N.`), com a
    linha física do registro, sem conteúdo nem caminho."""
    _assert_roster_error(_write(tmp_path, data), csv_reason(line))


def test_field_at_the_csv_limit_is_accepted(tmp_path: pathlib.Path) -> None:
    """Só o campo **acima** de 131.072 caracteres é CSV malformado: um nome de exatamente
    131.072 caracteres é lido inteiro."""
    data = b"ra,nome,email\n100001," + b"n" * CSV_FIELD_LIMIT + b",ana@example.org\n"
    assert read_roster(_write(tmp_path, data)) == Roster(
        rows=[_row("100001", "n" * CSV_FIELD_LIMIT, "ana@example.org")], lines=[2])


# --- colunas e ausência de alunos ------------------------------------------------------------------

COLUMN_ERRORS = [
    ("quatro-colunas", b"ra,nome,email\n100009,Aluna " + CONTENT_SENTINEL.encode()
     + b",a@example.org,extra\n", 2),
    ("quatro-colunas-ponto-e-virgula", b"ra;nome;email\n100009;Aluna " + CONTENT_SENTINEL.encode()
     + b";a@example.org;\n", 2),
    ("duas-colunas", b"ra,nome,email\n" + SENTINEL_ROW + b"100002,Bruno Lima\n", 3),
    ("linha-de-espacos", b"ra,nome,email\n" + SENTINEL_ROW + b"   \n", 3),
    ("depois-de-registro-multilinha",
     b'ra,nome,email\n100009,"Aluna\n' + CONTENT_SENTINEL.encode() + b'",a@example.org\n100002,Bruno\n',
     4),
]


@pytest.mark.parametrize(("data", "line"), [(d, n) for _, d, n in COLUMN_ERRORS],
                         ids=[i for i, *_ in COLUMN_ERRORS])
def test_record_without_3_columns_is_refused_with_the_line(tmp_path: pathlib.Path, data: bytes,
                                                           line: int) -> None:
    """Só registros inteiramente vazios são ignorados (uma linha de espaços é um campo não
    vazio); a mensagem `a linha N do arquivo da lista não tem as 3 colunas do cabeçalho.`, com N a
    linha física em que o registro começa, sem conteúdo nem caminho."""
    _assert_roster_error(_write(tmp_path, data), columns_reason(line))


NO_STUDENTS = [
    ("so-cabecalho", b"ra,nome,email\n"),
    ("so-cabecalho-sem-quebra", b"ra,nome,email"),
    ("cabecalho-e-linhas-vazias", b"ra;nome;email\r\n\r\n\r\n"),
    ("cabecalho-e-registros-vazios", b"ra,nome,email\n\n,,\n\"\"\n,,,\n\n"),
]


@pytest.mark.parametrize("data", [d for _, d in NO_STUDENTS], ids=[i for i, _ in NO_STUDENTS])
def test_header_only_is_refused(tmp_path: pathlib.Path, data: bytes) -> None:
    """Só o cabeçalho e linhas vazias (→ `o arquivo da lista não tem nenhuma linha de
    aluno.`), sem caminho na mensagem."""
    _assert_roster_error(_write(tmp_path, data), EMPTY_REASON)


# --- tradução de `rows[i]` e estado global do `csv` ------------------------------------------------

def test_row_line_translates_rows_index_to_file_line(tmp_path: pathlib.Path) -> None:
    """`row_line(field, lines)`: `rows[i]` → linha do arquivo (cada
    item `rows[i]` de um erro da API sai como `  - linha <N>: <reason>`): `rows[3]` → `lines[3]`,
    `rows[0]` → `lines[0]`; `rows` (o campo da lista inteira) → `None`; com as linhas de um arquivo real,
    com linhas vazias e um registro vazio no meio."""
    lines = [2, 4, 5, 9]
    assert row_line("rows[3]", lines) == 9
    assert row_line("rows[0]", lines) == 2
    assert row_line("rows", lines) is None
    data = (b"ra,nome,email\n100001,A,a@example.org\n\n100002,B,b@example.org\n100003,C,c@example.org\n"
            b"\n\n,,\n100004,D,d@example.org\n")
    roster = read_roster(_write(tmp_path, data))
    assert roster.lines == lines
    assert row_line("rows[3]", roster.lines) == 9
    assert row_line("rows", roster.lines) is None


def test_csv_module_globals_are_untouched(tmp_path: pathlib.Path) -> None:
    """Módulo puro, reaproveitado pelo MCP (nenhuma variável global do `csv`
    alterada): o `field_size_limit` (o padrão da biblioteca, 131.072) e os dialetos registrados são os
    mesmos antes e depois de uma leitura aceita e de uma recusada pelo limite do campo."""
    limit = csv.field_size_limit()
    dialects = sorted(csv.list_dialects())
    assert limit == CSV_FIELD_LIMIT
    assert read_roster(_write(tmp_path, _comma_file("\n"), "lista-ok.csv")) == Roster(
        rows=[ANA, BRUNO, JOAO], lines=[2, 3, 4])
    too_long = b"ra,nome,email\n100002," + b"n" * (CSV_FIELD_LIMIT + 1) + b",bruno@example.org\n"
    _assert_roster_error(_write(tmp_path, too_long), csv_reason(2))
    assert csv.field_size_limit() == limit
    assert sorted(csv.list_dialects()) == dialects
