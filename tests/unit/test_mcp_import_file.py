"""Leitura do arquivo da lista pelo servidor MCP: só por caminho absoluto, com a leitura segura e a linha
inválida da lista recusada antes da API, pelas funções internas que os
testes chamam.

Duas partes:

1. **Sem API** (`read_import_file` e `roster.parse_roster`). A ordem: `Path(path).is_absolute()` →
   `ImportFileError("not_absolute")`; `os.stat` → `not_found`; `stat.S_ISREG` → `not_regular`; `os.open` (falha →
   `RosterFileError(READ_REASON)`); `os.fstat` do descritor (regular, mesmo `st_dev`/`st_ino`) → `not_regular`;
   leitura de até `MAX_BYTES + 1` (acima → `SIZE_REASON`). O ilegível é provocado nos dois sistemas trocando o
   `os.open` do módulo `os` por um que levanta `PermissionError` só para o arquivo do teste (**[F]** no Windows,
   `os.chmod(p, 0)` não impede a leitura); o caso a mais sem dublê roda nos dois sistemas, sem `skip` (o `chmod` real
   no POSIX; no Windows, o arquivo aberto por outro descritor sem compartilhamento), e o arquivo especial é uma FIFO
   no POSIX e o dispositivo `NUL` no Windows. A troca entre a checagem e
   a abertura é provocada pelo mesmo meio: o `os.open` do caminho abre outro arquivo, e o `fstat` do descritor dá
   outro `st_ino`. `parse_roster(bytes)` é comparada com `read_roster(path)` sobre as tabelas de casos do vínculo do aluno
   (**[F]** `tests/unit/test_roster.py`, importadas sem edição), inclusive os motivos com o número da linha.
2. **Pela ferramenta** (`import_students`, com a API dos testes como `ServerDeps.http`): cada falha acima vira erro de
   execução com o texto fixo `"[<chave>] <mensagem>"` (`file.roster` com a `reason` literal da leitura da lista) e
   **nenhum** pedido à API (o gancho de pedido do `TestClient` sem registro); a linha **estruturalmente** inválida
   (a linha 3 com 2 colunas) falha localmente com o número da linha (sem chegar
   à API); o sucesso devolve só a resposta agregada da API, sem o caminho nem o conteúdo.

O caminho de cada arquivo tem o nome de um aluno canário (`STUDENT_A` no diretório, `STUDENT_B` no arquivo), e o
conteúdo dos arquivos recusados tem a linha de um aluno canário: nenhuma mensagem, `repr`, `args`, atributo ou
traceback formatado tem o caminho, o nome do diretório ou um alvo canário.

No esqueleto, `read_import_file`, `parse_roster` e `build_server` lançam
`NotImplementedError`: cada função falha por ele (os tipos, `MCP_MESSAGES` e as constantes do `roster` são reais).
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import stat
import sys
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import anyio.from_thread
import pytest

from cq_client import roster
from cq_client.mcp_server import MCP_MESSAGES, ImportFileError, read_import_file
from tests.helpers.api import ADMIN_TOKEN, create_offering
from tests.helpers.canary import STUDENT_A, STUDENT_B, STUDENT_PREFIXES, find_canary
from tests.helpers.contract import assert_matches
from tests.helpers.mcp_client import MISSING_PROFILE_FILE, MODES, SCAN_PREFIXES, call, in_process, make_deps, scan
from tests.helpers.secrets import all_secrets, bot_secrets
from tests.unit import test_roster as roster_tests

BASE_URL = "http://testserver"

# os textos exatos dos erros de execução da ferramenta (literais, conferidos contra a tabela).
NOT_ABSOLUTE_TEXT = "[file.not_absolute] Informe o caminho absoluto do arquivo da lista."
NOT_FOUND_TEXT = "[file.not_found] O arquivo da lista não foi encontrado."
NOT_REGULAR_TEXT = "[file.not_regular] O caminho não é um arquivo comum."
READ_TEXT = "[file.roster] não foi possível ler o arquivo da lista."
SIZE_TEXT = "[file.roster] o arquivo da lista passa de 4 MiB."
LINE_3_COLUMNS_TEXT = "[file.roster] a linha 3 do arquivo da lista não tem as 3 colunas do cabeçalho."
HEADER_TEXT = ("[file.roster] a primeira linha do arquivo da lista deve ter exatamente as colunas ra, nome e email, "
               "separadas por vírgula ou por ponto e vírgula.")
NOT_UTF8_TEXT = "[file.roster] o arquivo da lista não está em UTF-8 (linha 3)."
# O teto de 1.000 linhas pela ferramenta, com a
# `reason` literal da leitura da lista.
TOO_MANY_TEXT = "[file.roster] o arquivo da lista tem mais de 1.000 linhas de alunos."

# O diretório e o arquivo com nomes de alunos canário (o caminho nunca sai em mensagem).
CANARY_DIR = STUDENT_A.name
CANARY_FILE = f"lista {STUDENT_B.name}.csv"

# Uma linha de aluno canário (conteúdo que nunca sai em mensagem) e uma neutra.
CANARY_ROW = f"{STUDENT_A.ra},{STUDENT_A.name},{STUDENT_A.email}\n".encode()
NEUTRAL_ROW = b"7700000001,Aluno Neutro Leitura,aluno.neutro.leitura@turma.example.org\n"
HEADER = b"ra,nome,email\n"

# **[F]** o que a abertura não pode pedir (só leitura).
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


# --------------------------------------------------------------------------------------------------
# Apoio


def _canary_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / CANARY_DIR
    directory.mkdir(exist_ok=True)
    return directory


def _write(tmp_path: pathlib.Path, data: bytes, name: str = CANARY_FILE) -> pathlib.Path:
    path = _canary_dir(tmp_path) / name
    path.write_bytes(data)
    return path


def _rendered(exc: BaseException) -> list[str]:
    """Tudo o que uma exceção mostra: mensagem, `repr`, `args`, atributos e o traceback formatado (com as
    encadeadas)."""
    texts = [str(exc), repr(exc), *map(repr, exc.args), "".join(traceback.format_exception(exc))]
    texts.extend(repr(value) for value in vars(exc).values())
    return texts


def _assert_no_path(exc: BaseException, path: Any) -> None:
    for text in _rendered(exc):
        assert str(path) not in text
        assert CANARY_DIR not in text and STUDENT_B.name not in text
        assert find_canary(text, prefixes=STUDENT_PREFIXES) == [], text


def _assert_import_error(path: Any, key: str) -> None:
    """`read_import_file(path)` levanta `ImportFileError(key)` com a mensagem de `MCP_MESSAGES`, sem o caminho."""
    with pytest.raises(ImportFileError) as excinfo:
        read_import_file(str(path))
    exc = excinfo.value
    assert exc.key == key
    assert str(exc) == MCP_MESSAGES[f"file.{key}"]
    _assert_no_path(exc, path)


def _assert_roster_error(path: Any, reason: str) -> None:
    """`read_import_file(path)` levanta `roster.RosterFileError(reason)`, sem o caminho."""
    with pytest.raises(roster.RosterFileError) as excinfo:
        read_import_file(str(path))
    exc = excinfo.value
    assert exc.reason == reason
    assert str(exc) == reason
    _assert_no_path(exc, path)


def _same_file(a: Any, b: Any) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(a))) == os.path.normcase(os.path.abspath(os.fspath(b)))


def _patch_os_open(monkeypatch: pytest.MonkeyPatch, target: pathlib.Path,
                   replacement: Callable[[Any, int], int]) -> list[int]:
    """Troca `os.open` só para `target` (os demais caminhos abrem de verdade) até o fim do teste; devolve a lista
    das flags pedidas para ele."""
    real_open = os.open
    flags_seen: list[int] = []

    def fake_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if _same_file(path, target):
            flags_seen.append(flags)
            return replacement(path, flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    return flags_seen


def _refuse_permission(path: Any, flags: int) -> int:
    raise PermissionError(13, "Permission denied (dublê)")


# --------------------------------------------------------------------------------------------------
# read_import_file: forma do caminho


RELATIVE_PATHS = [
    pytest.param("lista.csv", id="nome-solto"),
    pytest.param("./lista.csv", id="ponto-barra"),
    pytest.param("sub/lista.csv", id="subdiretorio"),
    pytest.param("..\\lista.csv", id="ponto-ponto-contrabarra"),
    pytest.param("\\lista.csv", id="contrabarra-raiz-do-disco-atual"),
    pytest.param("C:lista.csv", id="disco-sem-barra"),
]


@pytest.mark.parametrize("relative", RELATIVE_PATHS)
def test_relative_path_is_not_absolute(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
                                       relative: str) -> None:
    """Caminho relativo (`Path(path).is_absolute()` primeiro → `file.not_absolute`, nos dois
    sistemas: **[F]** no Windows, `\\x` e `C:x` não são absolutos): o caminho relativo é recusado mesmo quando o
    arquivo existe a partir do diretório atual, com a mensagem fixa e sem o caminho."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    for name in ("lista.csv", "sub/lista.csv"):
        (tmp_path / name).write_bytes(HEADER + NEUTRAL_ROW)
    assert not pathlib.Path(relative).is_absolute()
    _assert_import_error(relative, "not_absolute")
    assert MCP_MESSAGES["file.not_absolute"] == "Informe o caminho absoluto do arquivo da lista."


def test_missing_file_is_not_found(tmp_path: pathlib.Path) -> None:
    """Arquivo inexistente (`os.stat` falha → `file.not_found`): o arquivo inexistente e o arquivo num
    diretório inexistente, sem o caminho."""
    directory = _canary_dir(tmp_path)
    _assert_import_error(directory / CANARY_FILE, "not_found")
    _assert_import_error(directory / "nao-existe" / CANARY_FILE, "not_found")
    assert MCP_MESSAGES["file.not_found"] == "O arquivo da lista não foi encontrado."


def test_directory_is_not_regular(tmp_path: pathlib.Path) -> None:
    """Diretório (`stat.S_ISREG` → `file.not_regular`): um diretório (com o nome de um aluno
    canário), sem o caminho."""
    _assert_import_error(_canary_dir(tmp_path), "not_regular")
    assert MCP_MESSAGES["file.not_regular"] == "O caminho não é um arquivo comum."


def _refuse_opening(path: Any, flags: int) -> int:
    raise AssertionError("o arquivo especial foi aberto (o stat deveria recusá-lo antes)")


def test_special_file_is_not_regular(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Arquivo especial (o `stat` decide antes da abertura; o arquivo especial nunca é aberto, então a leitura
    não bloqueia): um arquivo especial no diretório canário → `file.not_regular`, sem o caminho e sem nenhum `os.open`
    nele. Roda nos dois sistemas, sem `skip`: no POSIX, uma FIFO (`os.mkfifo`); no Windows,
    que não tem FIFO no sistema de arquivos, o dispositivo `NUL` dentro do diretório canário (**[F]** conferido no
    Windows 11 com Python 3.12: `os.stat(<diretório>\\NUL)` dá `S_IFCHR`, e o caminho é absoluto)."""
    directory = _canary_dir(tmp_path)
    if sys.platform == "win32":
        special = directory / "NUL"
        assert stat.S_ISCHR(os.stat(special).st_mode)
    else:
        special = directory / CANARY_FILE
        os.mkfifo(special)
        assert stat.S_ISFIFO(os.stat(special).st_mode)
    opened = _patch_os_open(monkeypatch, special, _refuse_opening)
    _assert_import_error(special, "not_regular")
    assert opened == []


# --------------------------------------------------------------------------------------------------
# read_import_file: abertura, troca e leitura


def test_unreadable_file_gives_the_read_reason(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Arquivo ilegível (`os.open` falha → a `READ_REASON` do `roster`): nos dois sistemas, o `os.open`
    do arquivo levanta `PermissionError` → `RosterFileError(READ_REASON)`, sem o caminho. A abertura é única e só
    de leitura, com `O_BINARY` e `O_NONBLOCK` quando o sistema os tem (**[F]** sem `O_BINARY`, no Windows o CRLF
    vira LF e a leitura para no `0x1A`)."""
    path = _write(tmp_path, HEADER + CANARY_ROW)
    flags_seen = _patch_os_open(monkeypatch, path, _refuse_permission)
    _assert_roster_error(path, roster.READ_REASON)
    assert len(flags_seen) == 1
    flags = flags_seen[0]
    assert flags & WRITE_FLAGS == 0
    for name in ("O_BINARY", "O_NONBLOCK"):
        bit = getattr(os, name, 0)
        assert flags & bit == bit, f"a abertura não pediu {name}"


@contextlib.contextmanager
def _really_unreadable(path: pathlib.Path) -> Iterator[None]:
    """Tira de verdade a leitura do arquivo enquanto dura o bloco, sem dublê no `os.open`.

    - Windows: um descritor do próprio sistema aberto com `dwShareMode = 0` (nenhum compartilhamento). **[F]**
      conferido no Windows 11 com Python 3.12: enquanto ele está aberto, `os.open` do arquivo levanta
      `PermissionError` (errno 13) e `os.stat` continua funcionando, então a recusa vem da abertura. O `chmod` não
      serve aqui: **[F]** no Windows, `os.chmod(p, 0)` não impede a leitura.
    - POSIX: `os.chmod(path, 0)`. **[I]** o superusuário lê o arquivo mesmo assim; nesse caso a pré-condição do teste
      falha de forma explícita (nada de `skip`).
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, no_sharing, open_existing, attribute_normal = 0x80000000, 0, 3, 0x80
        handle = kernel32.CreateFileW(str(path), generic_read, no_sharing, None, open_existing, attribute_normal,
                                      None)
        assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
        try:
            yield
        finally:
            kernel32.CloseHandle(handle)
    else:
        os.chmod(path, 0)
        try:
            yield
        finally:
            os.chmod(path, 0o600)


def test_unreadable_file_for_real(tmp_path: pathlib.Path) -> None:
    """Ilegível de verdade (`os.open` falha → a `READ_REASON` do `roster`): o mesmo caso do ilegível, mas sem
    dublê, com a leitura tirada de verdade pelo sistema (o `chmod` no POSIX; no Windows, o arquivo aberto por outro
    descritor sem compartilhamento) → `RosterFileError(READ_REASON)`, sem o caminho. Roda nos dois sistemas, sem
    `skip`; a pré-condição confere que o arquivo continua um arquivo comum para o `stat` e que a abertura de fato
    falha."""
    path = _write(tmp_path, HEADER + CANARY_ROW)
    with _really_unreadable(path):
        assert stat.S_ISREG(os.stat(path).st_mode)
        with pytest.raises(PermissionError):
            with open(path, "rb"):
                pass
        _assert_roster_error(path, roster.READ_REASON)


def test_swap_between_check_and_open_is_not_regular(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Troca entre a checagem e a abertura (o `fstat` do descritor confere o mesmo `st_dev`/`st_ino` do `stat`): o caminho
    checado é um arquivo, mas a abertura dá outro (a troca entre a checagem e a abertura) → `file.not_regular`, sem
    o caminho e sem ler o outro arquivo."""
    path = _write(tmp_path, HEADER + NEUTRAL_ROW)
    other = _write(tmp_path, HEADER + CANARY_ROW, "outra-lista.csv")
    assert (os.stat(path).st_dev, os.stat(path).st_ino) != (os.stat(other).st_dev, os.stat(other).st_ino)
    real_open = os.open
    flags_seen = _patch_os_open(monkeypatch, path, lambda _path, flags: real_open(other, flags))
    _assert_import_error(path, "not_regular")
    assert len(flags_seen) == 1


def test_bytes_are_returned_unchanged_with_crlf_and_ctrl_z(tmp_path: pathlib.Path) -> None:
    """Bytes intactos (`O_BINARY`; **[F]** em modo texto, no Windows, o CRLF vira LF e a leitura para no
    `0x1A`): os bytes devolvidos são iguais aos de `open(p, "rb").read()`, com CRLF, `0x1A` no meio de um campo e
    numa linha sozinha, e o BOM."""
    data = (roster_tests.BOM + b"ra,nome,email\r\n7700000002,Aluno\x1aNeutro,a@turma.example.org\r\n\x1a\r\n"
            + NEUTRAL_ROW.replace(b"\n", b"\r\n") + b"\x1a")
    path = _write(tmp_path, data)
    with open(path, "rb") as file:
        raw = file.read()
    assert raw == data
    assert read_import_file(str(path)) == raw


def test_size_limit_is_the_roster_limit(tmp_path: pathlib.Path) -> None:
    """Teto de tamanho (leitura de até `MAX_BYTES + 1`): exatamente `roster.MAX_BYTES` bytes são
    devolvidos inteiros; com 1 byte a mais → a razão de tamanho da leitura da lista (`SIZE_REASON`), sem o caminho."""
    assert roster.MAX_BYTES == 4 * 1024 * 1024
    exact = _write(tmp_path, b"a" * roster.MAX_BYTES, "lista-4mib.csv")
    assert read_import_file(str(exact)) == b"a" * roster.MAX_BYTES
    over = _write(tmp_path, b"a" * (roster.MAX_BYTES + 1))
    _assert_roster_error(over, roster.SIZE_REASON)


def test_import_file_error_carries_only_the_key(tmp_path: pathlib.Path) -> None:
    """Só a chave (nenhum atributo guarda o caminho; `repr` sem caminho): a exceção de cada recusa só tem a
    chave, e o `repr` é o do esqueleto."""
    directory = _canary_dir(tmp_path)
    with pytest.raises(ImportFileError) as excinfo:
        read_import_file(str(directory / CANARY_FILE))
    exc = excinfo.value
    assert vars(exc) == {"key": "not_found"}
    assert repr(exc) == "ImportFileError(key='not_found')"
    assert exc.args == (MCP_MESSAGES["file.not_found"],)


# --------------------------------------------------------------------------------------------------
# parse_roster(bytes) == read_roster(path) sobre os casos da leitura da lista


def _ignored_empty(sep: str, empty: bytes) -> bytes:
    """O arquivo do caso `test_records_with_only_empty_fields_are_ignored` de `tests/unit/test_roster.py`."""
    s = sep.encode()
    return (b"ra" + s + b"nome" + s + b"email\n"
            + b"100001" + s + b"Ana Souza" + s + b"ana.souza@example.org\n"
            + empty + b"\n"
            + b"100002" + s + b"Bruno Lima" + s + b"Bruno.Lima@Example.ORG\n"
            + empty + b"\n")


def _roster_cases() -> list[Any]:
    """Os arquivos das tabelas da leitura da lista (`tests/unit/test_roster.py`), mais os dos casos montados nas funções de lá
    (aspas, linhas vazias, sem aparar, teto de linhas e de bytes)."""
    cases: list[Any] = []

    def add(group: str, case_id: str, data: bytes) -> None:
        cases.append(pytest.param(data, id=f"{group}-{case_id}"))

    for group, table in (("basico", roster_tests.BASIC_FILES), ("separador", roster_tests.SEPARATOR_FILES),
                         ("ordem", roster_tests.HEADER_ORDERS), ("outro-separador", roster_tests.OTHER_SEPARATOR_EMPTY),
                         ("nao-utf8", roster_tests.NOT_UTF8), ("cabecalho", roster_tests.HEADER_ERRORS), ("malformado", roster_tests.MALFORMED),
                         ("colunas", roster_tests.COLUMN_ERRORS), ("sem-alunos", roster_tests.NO_STUDENTS)):
        for entry in table:
            add(group, entry[0], entry[1])
    for case_id, sep, empty in roster_tests.IGNORED_EMPTY:
        add("vazio", case_id, _ignored_empty(sep, empty))
    add("aspas", "virgula", b'ra,nome,email\n100001,"Souza, Ana",ana.souza@example.org\n'
                            b'100002,"Lima ""Bruno""",Bruno.Lima@Example.ORG\n100003,"Ana\nSouza",ana@example.org\n'
                            b'100004,"Bruno\r\nLima",bruno@example.org\n100005,Caio Prado,caio@example.org\n')
    add("aspas", "ponto-e-virgula", b'ra;nome;email\n100001;"Souza; Ana";ana.souza@example.org\n'
                                    b'100002;"Bruno\nde\nLima";bruno@example.org\n100003;Caio Prado;caio@example.org\n')
    lf = (b"ra,nome,email\n\n100001,Ana Souza,ana.souza@example.org\n\n\n100002,Bruno Lima,Bruno.Lima@Example.ORG\n"
          b",,\n100003,Jo\xc3\xa3o Concei\xc3\xa7\xc3\xa3o,joao.conceicao@example.org\n\n")
    add("linhas-vazias", "lf", lf)
    add("linhas-vazias", "crlf", lf.replace(b"\n", b"\r\n"))
    add("linhas-vazias", "cr", lf.replace(b"\n", b"\r"))
    add("sem-aparar", "espacos", b"ra,nome,email\n 100001 , Ana  Souza ,\tAna.Souza@Example.org \n , , \n"
                                 b"000123,\"  Bruno Lima  \",bruno@example.org\n")
    add("teto", "1000-linhas", b"ra,nome,email\n" + roster_tests._students(1000))
    add("teto", "1000-linhas-espacadas", b"ra,nome,email\n" + roster_tests._students(1000, blank_between=True))
    add("teto", "1001-linhas", b"ra,nome,email\n" + roster_tests.SENTINEL_ROW + roster_tests._students(1000))
    add("teto", "4-mib", roster_tests._sized_file(roster.MAX_BYTES))
    add("teto", "4-mib-mais-1", roster_tests._sized_file(roster.MAX_BYTES + 1))
    add("teto", "campo-no-limite-do-csv",
        b"ra,nome,email\n100001," + b"n" * roster_tests.CSV_FIELD_LIMIT + b",ana@example.org\n")
    return cases


def _outcome(read: Callable[[Any], roster.Roster], arg: Any) -> tuple[str, Any]:
    try:
        return "ok", read(arg)
    except roster.RosterFileError as exc:
        for text in _rendered(exc):
            assert roster_tests.CONTENT_SENTINEL not in text and roster_tests.PATH_SENTINEL not in text
        return "error", exc.reason


@pytest.mark.parametrize("data", _roster_cases())
def test_parse_roster_matches_read_roster(tmp_path: pathlib.Path, data: bytes) -> None:
    """Mesmo resultado (`parse_roster(raw)` com os mesmos motivos fixos de `read_roster`): sobre
    cada caso da leitura da lista (**[F]** as tabelas de `tests/unit/test_roster.py`), o `Roster` ou o motivo de
    `parse_roster(bytes)` é igual ao de `read_roster(path)`, inclusive os motivos com o número da linha e os tetos;
    nenhum motivo leva o conteúdo nem o caminho."""
    path = tmp_path / f"lista-{roster_tests.PATH_SENTINEL}.csv"
    path.write_bytes(data)
    expected = _outcome(roster.read_roster, str(path))
    assert _outcome(roster.parse_roster, data) == expected


def test_parse_roster_keeps_the_csv_globals(tmp_path: pathlib.Path) -> None:
    """Leitura do CSV (o MCP é um processo longo; nenhuma variável global do `csv` alterada): o `field_size_limit` e os
    dialetos são os mesmos antes e depois de uma análise aceita e de uma recusada pelo limite do campo."""
    import csv

    limit, dialects = csv.field_size_limit(), sorted(csv.list_dialects())
    assert roster.parse_roster(HEADER + NEUTRAL_ROW) == roster.Roster(
        rows=[{"ra": "7700000001", "name": "Aluno Neutro Leitura", "email": "aluno.neutro.leitura@turma.example.org"}],
        lines=[2])
    too_long = b"ra,nome,email\n100002," + b"n" * (roster_tests.CSV_FIELD_LIMIT + 1) + b",b@example.org\n"
    assert _outcome(roster.parse_roster, too_long) == ("error", "o arquivo da lista não é um CSV válido na linha 2.")
    assert (csv.field_size_limit(), sorted(csv.list_dialects())) == (limit, dialects)


# --------------------------------------------------------------------------------------------------
# Pela ferramenta `import_students` (API dos testes)


def _deps(tmp_path: pathlib.Path, client: Any) -> Any:
    env = {"CQ_API_URL": BASE_URL, "CQ_TOKEN": ADMIN_TOKEN, "CQ_CONFIG": str(tmp_path / MISSING_PROFILE_FILE)}
    return make_deps(tmp_path, env=env, http=client)


@contextlib.contextmanager
def _recorded(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _import(tmp_path: pathlib.Path, client: Any, mode: str, offering: str, path: Any) -> tuple[Any, list[Any]]:
    """Uma chamada da `import_students` numa conexão em processo; devolve o resultado e os pedidos à API."""
    deps = _deps(tmp_path, client)
    with anyio.from_thread.start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(in_process(deps, mode)) as mcp_client:
            with _recorded(client) as seen:
                outcome = portal.call(call, mcp_client, "import_students",
                                      {"offering": offering, "path": str(path)})
    return outcome, list(seen)


def _secrets() -> list[str]:
    return all_secrets() + bot_secrets()


def _assert_clean(outcome: Any, *paths: Any) -> None:
    """Nenhum alvo canário, segredo ou caminho no texto, no estruturado e no resultado inteiro."""
    assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []
    rendered = json.dumps(outcome.raw, ensure_ascii=False)
    for path in paths:
        for form in (str(path), json.dumps(str(path))[1:-1], pathlib.Path(path).name):
            assert form not in rendered and form not in (outcome.text or "")
    assert CANARY_DIR not in rendered and STUDENT_B.name not in rendered


def _failure(case: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """O caminho de cada falha da ferramenta e o texto exato do erro de execução."""
    if case == "relativo":
        monkeypatch.chdir(_canary_dir(tmp_path))
        _write(tmp_path, HEADER + CANARY_ROW)
        return CANARY_FILE, NOT_ABSOLUTE_TEXT
    if case == "inexistente":
        return str(_canary_dir(tmp_path) / CANARY_FILE), NOT_FOUND_TEXT
    if case == "diretorio":
        return str(_canary_dir(tmp_path)), NOT_REGULAR_TEXT
    if case == "ilegivel":
        path = _write(tmp_path, HEADER + CANARY_ROW)
        _patch_os_open(monkeypatch, path, _refuse_permission)
        return str(path), READ_TEXT
    if case == "acima-do-limite":
        return str(_write(tmp_path, HEADER + CANARY_ROW + b"a" * roster.MAX_BYTES)), SIZE_TEXT
    if case == "mais-de-1000-linhas":
        # 1.001 linhas de alunos (a do canário e mais 1.000), abaixo de 4 MiB: só o teto de linhas recusa.
        return str(_write(tmp_path, HEADER + CANARY_ROW + roster_tests._students(1000))), TOO_MANY_TEXT
    if case == "linha-3-com-2-colunas":
        path = _write(tmp_path, HEADER + CANARY_ROW + b"7700000003,Aluno Neutro\n" + NEUTRAL_ROW)
        return str(path), LINE_3_COLUMNS_TEXT
    if case == "cabecalho-errado":
        return str(_write(tmp_path, b"ra;nome\n" + CANARY_ROW)), HEADER_TEXT
    if case == "nao-utf8":
        cp1252_row = "7700000004,José,jose@turma.example.org\n".encode("cp1252")
        return str(_write(tmp_path, HEADER + CANARY_ROW + cp1252_row)), NOT_UTF8_TEXT
    raise AssertionError(case)


TOOL_FAILURES = ["relativo", "inexistente", "diretorio", "ilegivel", "acima-do-limite", "mais-de-1000-linhas",
                 "linha-3-com-2-colunas", "cabecalho-errado", "nao-utf8"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", TOOL_FAILURES)
def test_import_tool_failures_are_fixed_and_never_reach_the_api(case: str, mode: str, client: Any,
                                                                tmp_path: pathlib.Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Falhas pela ferramenta (a linha **estruturalmente** inválida falha localmente, com o
    número da linha): cada falha da leitura vira erro de execução (`isError`,
    sem `structuredContent`) com o texto exato `"[<chave>] <mensagem>"` (`file.roster` com a `reason` da leitura da lista
    literal), **nenhum** pedido à API, e nem o caminho, nem o nome do diretório, nem o conteúdo (a linha do aluno
    canário) aparecem no resultado."""
    offering = create_offering(client, ADMIN_TOKEN)
    path, expected = _failure(case, tmp_path, monkeypatch)
    outcome, seen = _import(tmp_path, client, mode, offering["code"], path)
    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is True
    assert outcome.structured is None
    assert outcome.text == expected
    assert seen == []
    _assert_clean(outcome, path)


@pytest.mark.parametrize("mode", MODES)
def test_import_tool_success_returns_only_the_api_counts(mode: str, client: Any, tmp_path: pathlib.Path) -> None:
    """com um arquivo válido de 3 alunos (dois canário e um neutro,
    CRLF e BOM) no caminho absoluto, a ferramenta faz **um** pedido `POST /v1/offerings/{código}/students` com
    `X-CQ-Client: mcp` e as linhas do arquivo, na ordem, sem aparar; o resultado é só a resposta agregada da API
    (as contagens), validada pelo esquema `students.import`, no estruturado e o mesmo JSON no texto; nem o caminho
    nem o conteúdo aparecem."""
    offering = create_offering(client, ADMIN_TOKEN)
    rows = [{"ra": STUDENT_A.ra, "name": STUDENT_A.name, "email": STUDENT_A.email},
            {"ra": "7700000005", "name": " Aluno Neutro Importado ",
             "email": "aluno.neutro.importado@turma.example.org"},
            {"ra": STUDENT_B.ra, "name": STUDENT_B.name, "email": STUDENT_B.email}]
    text = "ra,nome,email\r\n" + "".join(f"{r['ra']},{r['name']},{r['email']}\r\n" for r in rows)
    path = _write(tmp_path, roster_tests.BOM + text.encode("utf-8"))

    outcome, seen = _import(tmp_path, client, mode, offering["code"], path)

    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is False, outcome.text
    expected = {"offering": {"offering_id": offering["offering_id"], "code": offering["code"]}, "received": 3,
                "people_created": 3, "people_reused": 0, "enrollments_created": 3, "already_enrolled": 0}
    assert outcome.structured == expected
    assert_matches("students.import", 200, outcome.structured)
    assert outcome.text == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", f"/v1/offerings/{offering['code']}/students")
    assert request.headers.get("X-CQ-Client") == "mcp"
    assert json.loads(request.content) == {"rows": rows}
    _assert_clean(outcome, path)
