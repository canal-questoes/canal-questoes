"""A leitura de um banco de questões numa pasta local pelo cliente e o pacote `.tar.gz` que ele gera.

O que fica fixado aqui, sobre `cq_client.banks` (`read_folder`, `bank_version`, `build_archive`, `BankReadError`):
- **a leitura:** todo arquivo regular entra, inclusive os ocultos, o `__pycache__` e o lixo de sistema; todo
  componente `.git`, em qualquer nível e em qualquer caixa, fica fora (arquivo ou pasta), e o `.gitignore` e o
  `.github` entram; diretórios vazios não entram; as entradas saem em ordem crescente dos bytes UTF-8 do caminho,
  com o tamanho, o SHA-256 e o conteúdo de cada arquivo, e sem commit de origem; a versão é a de referência
  (`tests/helpers/banks.reference_version`) e a dos vetores fixos;
- **o pacote:** os mesmos bytes duas vezes e de duas cópias da mesma árvore (outra pasta, outros horários, outra
  ordem de criação); um único membro gzip, sem nome de arquivo, sem comentário e com horário zero; um tar POSIX
  (ustar com `magic` `ustar\\0` e versão `00` em todo cabeçalho; os nomes fora do ustar num cabeçalho estendido
  pax, nunca num membro de nome longo do GNU), só com arquivos regulares em ordem dos bytes do caminho, horário
  zero, donos zerados, sem nome de dono, modo `0644`, sem entradas de diretório, terminado em blocos de zeros;
  o nome e o caminho local da pasta nunca no pacote;
- **as recusas locais**, cada uma como `BankReadError` com o motivo do conjunto fechado e, quando houver, o caminho
  relativo **dentro** do banco (nunca o caminho local, nem no texto, nem no `repr`, nem nos argumentos da
  exceção): link simbólico; junção (só no Windows); arquivo especial (só fora do Windows); arquivo ilegível;
  caminho de rede ou de dispositivo (no Windows, recusado pelo texto, antes de qualquer acesso); caminho relativo;
  inexistente; que não é pasta; a raiz que é link ou junção (recusada antes de listar a pasta); nome fora de
  UTF-8; nome com caractere de controle; sem `infoCourse.json` na raiz; acima dos tetos de arquivos, de conteúdo
  e de pacote (o teto do pacote é lido do módulo na hora da chamada, o que deixa o teste baixá-lo).

Os testes que dependem da plataforma não são pulados: cada um tem um ramo para o Windows e outro para os demais
sistemas. Sem o privilégio de criar link simbólico no Windows (o modo de desenvolvedor desligado), o link do teste
vira uma junção, a forma de link que o Windows cria sem privilégio, e o motivo esperado passa a ser o da junção.

No esqueleto, `read_folder` e `build_archive` lançam `NotImplementedError`: todo caso daqui fica vermelho por isso
até a implementação da leitura da pasta e do pacote.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import tarfile
import zlib
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import banks as bank_fixtures

WINDOWS = os.name == "nt"
ERROR_PRIVILEGE_NOT_HELD = 1314

INFO_COURSE = b'{"uuid": "00000000-0000-4000-8000-0000000000aa", "name": "Curso sintetico"}\n'
BASE_FILES: dict[str, bytes] = {
    "infoCourse.json": INFO_COURSE,
    "questions/q1/info.json": b'{"title": "Q1"}\n',
    "questions/q1/question.html": b"<pl-question-panel>1 + 1</pl-question-panel>\n",
}

# Gzip (RFC 1952): os bits de nome de arquivo e de comentário no byte de opções.
GZIP_FNAME = 0x08
GZIP_FCOMMENT = 0x10
BLOCK = 512
POSIX_MAGIC = b"ustar\x00"
POSIX_VERSION = b"00"
# As chaves pax aceitas num pacote (as mesmas do servidor).
PAX_KEYS = frozenset({"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname"})


# --------------------------------------------------------------------------------------------------
# Apoio


def _bank_root(tmp_path: Path, label: str = "banco") -> Path:
    """Uma pasta nova com nome único (montado em tempo de execução), para procurar o nome nas saídas."""
    return tmp_path / f"{label}-{bank_fixtures.unique_marker()}"


def _write(root: Path, files: dict[str, bytes]) -> Path:
    return bank_fixtures.write_tree(root, files)


def _read(root: Path) -> Any:
    from cq_client.banks import read_folder

    return read_folder(str(root))


def _refusal(path: Any) -> Any:
    """A recusa da leitura de `path` (texto ou `Path`)."""
    from cq_client.banks import BankReadError, read_folder

    with pytest.raises(BankReadError) as info:
        read_folder(str(path))
    return info.value


def _by_bytes(paths: Any) -> list[str]:
    return sorted(paths, key=lambda p: p.encode("utf-8"))


def _assert_entries(bank: Any, files: dict[str, bytes]) -> None:
    """As entradas lidas são exatamente `files`, em ordem dos bytes do caminho, com tamanho, SHA-256 e conteúdo."""
    from cq_client.banks import BankEntry

    assert isinstance(bank.entries, tuple)
    assert all(isinstance(entry, BankEntry) for entry in bank.entries)
    assert [entry.path for entry in bank.entries] == _by_bytes(files)
    for entry in bank.entries:
        data = files[entry.path]
        assert (entry.size, entry.sha256, entry.data) == (len(data), hashlib.sha256(data).hexdigest(), data), entry.path
    assert bank.git_commit is None


def _local_texts(root: Path, tmp_path: Path) -> list[str]:
    """Os textos locais que nenhuma recusa pode conter: o caminho da pasta (nas duas grafias), o nome dela e o
    diretório do teste."""
    return [str(root), root.as_posix(), root.name, str(tmp_path), tmp_path.as_posix()]


def _assert_clean(exc: Any, root: Path, tmp_path: Path, *, reason: str, relative: Any = ...) -> None:
    """A recusa tem o motivo dado, do conjunto fechado; o caminho relativo dado (quando `relative` vem); e nenhum
    texto local na mensagem, no `repr`, nos argumentos nem no caminho relativo."""
    from cq_client.banks import LOCAL_MESSAGES, LOCAL_REASONS

    assert exc.reason == reason, f"motivo {exc.reason!r}, esperado {reason!r}"
    assert exc.reason in LOCAL_REASONS
    if relative is not ...:
        assert exc.relative_path == relative, f"caminho relativo {exc.relative_path!r}"
    texts = [str(exc), repr(exc), *(str(arg) for arg in exc.args), LOCAL_MESSAGES[exc.reason]]
    if exc.relative_path is not None:
        texts.append(exc.relative_path)
    for text in texts:
        for local in _local_texts(root, tmp_path):
            assert local not in text, "a recusa leva o caminho local da pasta"


def _make_link(target: Path, link: Path) -> str:
    """Um link de diretório `link` → `target`; devolve o motivo esperado: `symlink` com o link simbólico e, no
    Windows sem o privilégio de criá-lo, `junction` com a junção no lugar (ver o docstring do módulo)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError as exc:
        if not (WINDOWS and getattr(exc, "winerror", None) == ERROR_PRIVILEGE_NOT_HELD):
            raise
    _make_junction(target, link)
    return "junction"


def _make_junction(target: Path, link: Path) -> None:
    import _winapi  # só no Windows

    _winapi.CreateJunction(str(target), str(link))


def _gunzip_single(archive: bytes) -> bytes:
    """O fluxo tar de um pacote que é um único membro gzip, sem bytes depois (senão, falha)."""
    assert archive[:3] == b"\x1f\x8b\x08", "o pacote não é gzip com deflate"
    inflater = zlib.decompressobj(wbits=31)
    tar = inflater.decompress(archive) + inflater.flush()
    assert inflater.eof, "o membro gzip não terminou"
    assert inflater.unused_data == b"", "há bytes depois do primeiro membro gzip"
    return tar


def _octal(field: bytes) -> int:
    digits = field.replace(b"\0", b" ").strip()
    return int(digits, 8) if digits else 0


def _text(field: bytes) -> bytes:
    return field.split(b"\0", 1)[0]


def _pax_records(data: bytes) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    position = 0
    while position < len(data):
        space = data.index(b" ", position)
        length = int(data[position:space])
        record = data[space + 1:position + length]
        assert record.endswith(b"\n"), "registro pax sem quebra de linha final"
        key, _, value = record[:-1].partition(b"=")
        records.append((key.decode("utf-8"), value.decode("utf-8")))
        position += length
    return records


def _tar_members(tar: bytes) -> list[dict[str, Any]]:
    """Os membros do fluxo tar, lidos bloco a bloco (sem o `tarfile`): para cada membro regular, o caminho
    efetivo (o do pax, quando houver), os campos do cabeçalho e o conteúdo; os registros pax do `x` anterior
    em `pax`. Confere o `magic` POSIX de todo cabeçalho e os blocos de zeros no fim."""
    members: list[dict[str, Any]] = []
    pending_pax: list[tuple[str, str]] | None = None
    position = 0
    while True:
        assert position + BLOCK <= len(tar), "tar truncado"
        header = tar[position:position + BLOCK]
        if header == b"\0" * BLOCK:
            break
        assert header[257:263] == POSIX_MAGIC and header[263:265] == POSIX_VERSION, (
            "cabeçalho sem o magic POSIX (ustar e versão 00)")
        size = _octal(header[124:136])
        kind = header[156:157]
        data = tar[position + BLOCK:position + BLOCK + size]
        position += BLOCK + -(-size // BLOCK) * BLOCK
        if kind == b"x":
            assert pending_pax is None, "dois cabeçalhos pax seguidos"
            pending_pax = _pax_records(data)
            continue
        assert kind in (b"0", b"\0"), f"membro que não é arquivo regular (tipo {kind!r})"
        name = _text(header[0:100])
        prefix = _text(header[345:500])
        path = (prefix + b"/" + name if prefix else name).decode("utf-8")
        pax = dict(pending_pax or [])
        if "path" in pax:
            path = pax["path"]
        members.append({
            "path": path, "mode": _octal(header[100:108]), "uid": _octal(header[108:116]),
            "gid": _octal(header[116:124]), "mtime": _octal(header[136:148]), "uname": _text(header[265:297]),
            "gname": _text(header[297:329]), "data": data, "pax": pending_pax or [],
        })
        pending_pax = None
    assert pending_pax is None, "cabeçalho pax sem membro depois"
    assert len(tar) - position >= 2 * BLOCK, "o tar não termina em dois blocos de zeros"
    assert tar[position:] == b"\0" * (len(tar) - position), "bytes não zero depois do fim do tar"
    return members


def _assert_normalized_archive(archive: bytes, files: dict[str, bytes]) -> list[dict[str, Any]]:
    """O pacote gerado pelo `cq` (ver o docstring do módulo); devolve os membros lidos."""
    flags = archive[3]
    assert flags & GZIP_FNAME == 0, "o gzip leva nome de arquivo"
    assert flags & GZIP_FCOMMENT == 0, "o gzip leva comentário"
    assert int.from_bytes(archive[4:8], "little") == 0, "o gzip leva horário"
    members = _tar_members(_gunzip_single(archive))
    assert [m["path"] for m in members] == _by_bytes(files)
    for member in members:
        assert member["data"] == files[member["path"]], member["path"]
        assert (member["mode"], member["uid"], member["gid"], member["mtime"]) == (0o644, 0, 0, 0), member["path"]
        assert (member["uname"], member["gname"]) == (b"", b""), member["path"]
        keys = [key for key, _ in member["pax"]]
        assert set(keys) <= PAX_KEYS and len(keys) == len(set(keys)), member["pax"]
        pax = dict(member["pax"])
        for key in ("mtime", "uid", "gid"):
            assert pax.get(key, "0") == "0", (member["path"], key)
        for key in ("uname", "gname"):
            assert pax.get(key, "") == "", (member["path"], key)
        if not member["path"].isascii():
            assert pax.get("path") == member["path"], "nome não ASCII sem o registro pax do caminho"
    # Um leitor padrão lê o mesmo pacote, só com arquivos regulares.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz", encoding="utf-8") as tar:
        infos = tar.getmembers()
        assert [info.name for info in infos] == _by_bytes(files)
        assert all(info.isreg() for info in infos)
    return members


@pytest.fixture
def no_listing(monkeypatch) -> list[tuple[str, str]]:
    """Registra as listagens de pasta (`os.scandir`, `os.listdir`), repassando à função original."""
    calls: list[tuple[str, str]] = []
    real_scandir, real_listdir = os.scandir, os.listdir

    def scandir(path: Any = ".") -> Any:
        calls.append(("scandir", os.fspath(path) if not isinstance(path, int) else str(path)))
        return real_scandir(path)

    def listdir(path: Any = ".") -> Any:
        calls.append(("listdir", os.fspath(path) if not isinstance(path, int) else str(path)))
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return calls


# --------------------------------------------------------------------------------------------------
# A leitura da pasta


def test_folder_reads_every_regular_file_including_hidden_and_pycache(tmp_path) -> None:
    """Todo arquivo regular entra: os ocultos, o `__pycache__`, o lixo de sistema, os arquivos vazios e os nomes
    acentuados, em ordem dos bytes do caminho, com tamanho, SHA-256 e conteúdo; a versão é a de referência.
    Vermelho no esqueleto: `read_folder` e `bank_version` lançam `NotImplementedError`."""
    from cq_client.banks import bank_version

    files = {
        **BASE_FILES,
        ".oculto": b"arquivo oculto\n",
        "questions/q1/.rascunho.html": b"<p>rascunho</p>\n",
        "questions/q1/__pycache__/server.cpython-312.pyc": bytes(range(256)),
        ".DS_Store": b"\x00\x00\x00\x01Bud1",
        "Thumbs.db": b"\xd0\xcf\x11\xe0",
        "vazio.txt": b"",
        "questões/ação.txt": "conteúdo acentuado\n".encode("utf-8"),
        "B.txt": b"B\n",
        "a.txt": b"a\n",
    }
    root = _write(_bank_root(tmp_path), files)
    bank = _read(root)
    _assert_entries(bank, files)
    assert bank_version(bank.entries) == bank_fixtures.reference_version(files)


def test_folder_skips_git_components_at_any_level_and_case(tmp_path) -> None:
    """Todo componente `.git` (pasta ou arquivo), em qualquer nível e caixa, fica fora; `.gitignore`,
    `.gitattributes`, `.github` e `x.git` entram. Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    kept = {
        **BASE_FILES,
        ".gitignore": b"*.pyc\n",
        ".gitattributes": b"* text\n",
        ".github/workflows/ci.yml": b"on: push\n",
        "questions/q1/x.git": b"nao e componente .git\n",
    }
    skipped = {
        ".git/config": b"[core]\n",
        ".git/objects/ab/cdef": b"\x78\x01",
        "questions/.GIT/HEAD": b"ref: refs/heads/main\n",
        "a/.Git/b/c.txt": b"dentro de um .Git\n",
        "sub/.git": b"gitdir: ../.git/modules/sub\n",
    }
    root = _write(_bank_root(tmp_path), {**kept, **skipped})
    bank = _read(root)
    _assert_entries(bank, kept)


def test_folder_skips_empty_directories(tmp_path) -> None:
    """Diretórios vazios, inclusive os que só têm diretórios vazios, não entram (só arquivos contam).
    Vermelho no esqueleto: `read_folder` lança `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    (root / "vazia").mkdir()
    (root / "questions" / "q2" / "vazia" / "mais-funda").mkdir(parents=True)
    bank = _read(root)
    _assert_entries(bank, BASE_FILES)


def test_folder_version_matches_the_fixed_vectors(tmp_path) -> None:
    """Cada vetor fixo gravado numa pasta dá a versão do vetor, e as entradas saem em ordem dos bytes do caminho
    (`B.txt` antes de `a.txt`, `a-b/x.txt` antes de `a/b.txt`). Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    from cq_client.banks import bank_version

    vectors = bank_fixtures.load_vectors()
    for case in vectors["cases"]:
        files = bank_fixtures.vector_files(case)
        root = _write(_bank_root(tmp_path, case["id"]), files)
        bank = _read(root)
        _assert_entries(bank, files)
        assert bank_version(bank.entries) == case["version"] == bank_fixtures.reference_version(files), case["id"]


# --------------------------------------------------------------------------------------------------
# O pacote


def test_archive_is_reproducible_and_normalized(tmp_path) -> None:
    """O pacote sai com os mesmos bytes duas vezes e de duas cópias da mesma árvore (outra pasta, outros
    horários, outra ordem de criação), na forma normalizada do docstring do módulo. Vermelho no esqueleto:
    `read_folder` e `build_archive` lançam `NotImplementedError`."""
    from cq_client.banks import build_archive

    files = {
        **BASE_FILES,
        "questions/ação/info.json": b'{"title": "Acao"}\n',
        "questions/" + "n" * 120 + "/info.json": b'{"title": "Longo"}\n',
        "vazio.txt": b"",
        "binario.bin": secrets.token_bytes(3000),
    }
    first = _write(_bank_root(tmp_path, "primeira"), files)
    second = _bank_root(tmp_path, "segunda")
    _write(second, dict(reversed(list(files.items()))))
    for index, path in enumerate(sorted(second.rglob("*"))):
        os.utime(path, (1_000_000 + index, 2_000_000 + index))
    bank = _read(first)
    archive = build_archive(bank.entries)
    assert build_archive(bank.entries) == archive, "o mesmo banco deu dois pacotes diferentes"
    assert build_archive(_read(second).entries) == archive, "a mesma árvore em outra pasta deu outro pacote"
    _assert_normalized_archive(archive, files)


def test_folder_name_and_local_path_never_in_archive(tmp_path) -> None:
    """O nome da pasta e o caminho local não aparecem no pacote, nem no gzip nem no tar. Vermelho no esqueleto:
    `read_folder` e `build_archive` lançam `NotImplementedError`."""
    from cq_client.banks import build_archive

    root = _write(_bank_root(tmp_path), BASE_FILES)
    archive = build_archive(_read(root).entries)
    tar = _gunzip_single(archive)
    for local in _local_texts(root, tmp_path):
        for blob in (archive, tar):
            assert local.encode("utf-8") not in blob, "o pacote leva o nome ou o caminho local da pasta"
    _assert_normalized_archive(archive, BASE_FILES)


# --------------------------------------------------------------------------------------------------
# As recusas locais


def test_link_inside_the_bank_is_refused(tmp_path) -> None:
    """Um link de diretório dentro do banco é recusado com o caminho relativo dele (`symlink`; no Windows sem o
    privilégio de criar link simbólico, a junção no lugar, `junction`). Vermelho no esqueleto: `read_folder`
    lança `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    reason = _make_link(root / "questions" / "q1", root / "questions" / "atalho")
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason=reason, relative="questions/atalho")


def test_platform_special_entry_is_refused(tmp_path) -> None:
    """No Windows, uma junção dentro do banco → `junction`; nos demais sistemas, uma fila nomeada (arquivo
    especial) → `special_file`; as duas com o caminho relativo. Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    if WINDOWS:
        outside = _write(tmp_path / "fora", {"segredo.txt": b"fora do banco\n"})
        _make_junction(outside, root / "questions" / "juncao")
        expected, relative = "junction", "questions/juncao"
    else:
        os.mkfifo(root / "questions" / "fila")
        expected, relative = "special_file", "questions/fila"
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason=expected, relative=relative)


def test_unreadable_file_is_refused(tmp_path) -> None:
    """Um arquivo que não abre para leitura → `unreadable`, com o caminho relativo (no Windows, preso por outro
    processo sem compartilhamento; nos demais sistemas, sem permissão de leitura). Vermelho no esqueleto:
    `read_folder` lança `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    target = root / "questions" / "q1" / "question.html"
    if WINDOWS:
        import _winapi

        handle = _winapi.CreateFile(str(target), _winapi.GENERIC_READ, 0, 0, _winapi.OPEN_EXISTING, 0, 0)
        try:
            exc = _refusal(root)
        finally:
            _winapi.CloseHandle(handle)
    else:
        target.chmod(0)
        try:
            exc = _refusal(root)
        finally:
            target.chmod(0o644)
    _assert_clean(exc, root, tmp_path, reason="unreadable", relative="questions/q1/question.html")


def test_network_and_device_paths_are_refused_before_any_access(tmp_path, monkeypatch) -> None:
    """No Windows, o caminho de rede (UNC) e os de dispositivo são recusados pelo texto (`unc_path`, sem caminho
    relativo), sem nenhum `stat`, `lstat`, abertura ou listagem dele (o acesso a um UNC abriria uma conexão de
    rede); nos demais sistemas, a mesma forma é só um caminho que não existe (`not_found`). Vermelho no
    esqueleto: `read_folder` lança `NotImplementedError`."""
    accessed: list[str] = []
    if WINDOWS:
        paths = ["\\\\servidor.invalid\\compartilhamento\\banco", "//servidor.invalid/compartilhamento/banco",
                 "\\\\?\\UNC\\servidor.invalid\\compartilhamento\\banco", "\\\\?\\" + str(tmp_path),
                 "\\\\.\\" + str(tmp_path)]
        expected = "unc_path"
        for name in ("stat", "lstat", "scandir", "listdir", "open"):
            real = getattr(os, name)

            def spy(path: Any, *args: Any, _real: Any = real, **kwargs: Any) -> Any:
                if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).startswith(("\\\\", "//")):
                    accessed.append(os.fsdecode(path))
                return _real(path, *args, **kwargs)

            monkeypatch.setattr(os, name, spy)
    else:
        paths = ["//servidor.invalid/compartilhamento/banco"]
        expected = "not_found"
    for path in paths:
        exc = _refusal(path)
        _assert_clean(exc, tmp_path, tmp_path, reason=expected, relative=None)
        assert "servidor.invalid" not in str(exc) + repr(exc)
    assert accessed == [], "o caminho de rede ou de dispositivo foi acessado antes da recusa"


@pytest.mark.parametrize("kind", ["relativo", "inexistente", "arquivo"])
def test_relative_missing_and_non_directory_roots_are_refused(tmp_path, monkeypatch, kind) -> None:
    """A raiz relativa → `not_absolute` (mesmo existindo a partir do diretório atual), inexistente → `not_found`,
    um arquivo → `not_a_directory`, todas sem caminho relativo. Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    if kind == "relativo":
        monkeypatch.chdir(tmp_path)
        path, expected = root.name, "not_absolute"
    elif kind == "inexistente":
        path, expected = str(root / "nao-existe"), "not_found"
    else:
        path, expected = str(root / "infoCourse.json"), "not_a_directory"
    exc = _refusal(path)
    _assert_clean(exc, root, tmp_path, reason=expected, relative=None)


@pytest.mark.parametrize("kind", ["link", "juncao-ou-link"])
def test_root_that_is_a_link_is_refused_before_listing(tmp_path, no_listing, kind) -> None:
    """A raiz que é link simbólico (`symlink`) ou, no Windows, junção (`junction`) é recusada sem caminho
    relativo antes de qualquer listagem de pasta (nem a raiz nem o alvo são listados). Fora do Windows, o
    segundo caso é um link para um link. No Windows sem o privilégio de criar link simbólico, o primeiro caso
    também usa a junção (ver o docstring do módulo). Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    target = _write(_bank_root(tmp_path, "alvo"), BASE_FILES)
    root = _bank_root(tmp_path, "raiz")
    if kind == "link":
        expected = _make_link(target, root)
    elif WINDOWS:
        _make_junction(target, root)
        expected = "junction"
    else:
        middle = _bank_root(tmp_path, "meio")
        os.symlink(target, middle, target_is_directory=True)
        os.symlink(middle, root, target_is_directory=True)
        expected = "symlink"
    no_listing.clear()
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason=expected, relative=None)
    assert no_listing == [], "a pasta foi listada antes da recusa da raiz"


def test_name_outside_utf8_is_refused(tmp_path) -> None:
    """Um nome que não codifica em UTF-8 estrito (no Windows, um substituto solto; nos demais sistemas, um byte
    inválido) → `path_not_allowed`, sem texto local na recusa. Vermelho no esqueleto: `read_folder` lança
    `NotImplementedError`."""
    root = _write(_bank_root(tmp_path), BASE_FILES)
    if WINDOWS:
        (root / "questions" / "q1" / "nome\udc80.txt").write_bytes(b"x\n")
    else:
        with open(os.path.join(os.fsencode(root / "questions" / "q1"), b"nome\xff.txt"), "wb") as handle:
            handle.write(b"x\n")
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason="path_not_allowed")


def test_name_with_control_character_is_refused(tmp_path) -> None:
    """Um nome com caractere de controle (no Windows, que não aceita os de C0 em nomes, o CSI de 8 bits e o
    DEL; nos demais sistemas, o ESC) → `path_not_allowed`, com o caminho relativo cru (o CLI é que o
    neutraliza). Vermelho no esqueleto: `read_folder` lança `NotImplementedError`."""
    names = ["a\x9bb.txt", "c\x7fd.txt"] if WINDOWS else ["a\x1bb.txt"]
    for name in names:
        root = _write(_bank_root(tmp_path), {**BASE_FILES, f"questions/{name}": b"x\n"})
        exc = _refusal(root)
        _assert_clean(exc, root, tmp_path, reason="path_not_allowed", relative=f"questions/{name}")


@pytest.mark.parametrize("variant", ["ausente", "so-na-subpasta", "outra-caixa"])
def test_folder_without_info_course_at_root_is_refused(tmp_path, variant) -> None:
    """Sem `infoCourse.json` na raiz (ausente, só numa subpasta, ou com outra caixa no nome) → `not_a_course`.
    Vermelho no esqueleto: `read_folder` lança `NotImplementedError`."""
    files = {path: data for path, data in BASE_FILES.items() if path != "infoCourse.json"}
    if variant == "so-na-subpasta":
        files["questions/infoCourse.json"] = INFO_COURSE
    elif variant == "outra-caixa":
        files["InfoCourse.json"] = INFO_COURSE
    root = _write(_bank_root(tmp_path), files)
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason="not_a_course")


def _many(root: Path, count: int) -> None:
    folder = root / "muitos"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (folder / f"f{index:06d}.txt").write_bytes(b"")


def test_file_count_limit(tmp_path) -> None:
    """O teto de arquivos é o mesmo da api (20000): com 20000 arquivos o banco é lido; com 20001, →
    `too_many_files`. Vermelho no esqueleto: `read_folder` lança `NotImplementedError`."""
    from cq_client import banks

    assert banks.MAX_FILES == bank_fixtures.REFERENCE_MAX_FILES
    root = _write(_bank_root(tmp_path), {"infoCourse.json": INFO_COURSE})
    _many(root, bank_fixtures.REFERENCE_MAX_FILES - 1)
    assert len(_read(root).entries) == bank_fixtures.REFERENCE_MAX_FILES
    (root / "um-a-mais.txt").write_bytes(b"")
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason="too_many_files")


def test_content_size_limit(tmp_path) -> None:
    """O teto de conteúdo é o mesmo da api (100 MiB, a soma dos tamanhos): no teto, o banco é lido; um byte
    acima, → `too_large`. Vermelho no esqueleto: `read_folder` lança `NotImplementedError`."""
    from cq_client import banks

    assert banks.MAX_CONTENT_BYTES == bank_fixtures.REFERENCE_MAX_CONTENT_BYTES
    root = _write(_bank_root(tmp_path), {"infoCourse.json": INFO_COURSE})
    big = root / "questions" / "grande.bin"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as handle:
        handle.truncate(bank_fixtures.REFERENCE_MAX_CONTENT_BYTES - len(INFO_COURSE))
    bank = _read(root)
    assert sum(entry.size for entry in bank.entries) == bank_fixtures.REFERENCE_MAX_CONTENT_BYTES
    del bank
    with open(big, "ab") as handle:
        handle.write(b"\0")
    exc = _refusal(root)
    _assert_clean(exc, root, tmp_path, reason="too_large")


def test_archive_size_limit(tmp_path, monkeypatch) -> None:
    """O teto do pacote é o mesmo da api (128 MiB) e é conferido por `build_archive`, lido do módulo na hora da
    chamada: com o teto baixado para logo abaixo do pacote de conteúdo pouco compressível, →
    `archive_too_large`; com o teto no tamanho exato, o pacote sai. Vermelho no esqueleto: `read_folder` e
    `build_archive` lançam `NotImplementedError`."""
    from cq_client import banks
    from cq_client.banks import BankReadError

    assert banks.MAX_ARCHIVE_BYTES == bank_fixtures.REFERENCE_MAX_ARCHIVE_BYTES
    files = {**BASE_FILES, "questions/q1/ruido.bin": secrets.token_bytes(64 * 1024)}
    root = _write(_bank_root(tmp_path), files)
    entries = _read(root).entries
    size = len(banks.build_archive(entries))
    monkeypatch.setattr(banks, "MAX_ARCHIVE_BYTES", size)
    assert len(banks.build_archive(entries)) == size
    monkeypatch.setattr(banks, "MAX_ARCHIVE_BYTES", size - 1)
    with pytest.raises(BankReadError) as info:
        banks.build_archive(entries)
    _assert_clean(info.value, root, tmp_path, reason="archive_too_large", relative=None)
