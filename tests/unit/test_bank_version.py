"""A versão de um banco de questões enviado: o SHA-256 do manifesto do conteúdo, calculada duas vezes (no servidor,
`cq_server.banks.bank_version`, e no cliente, `cq_client.banks.bank_version`, cada uma importada do seu pacote, sem
código compartilhado).

O que se confere aqui:
- os vetores fixos de `tests/fixtures/banks/version-vectors.json` nas duas implementações, e a versão de referência
  calculada à parte pelo auxiliar `tests/helpers/banks.py` (que não importa o produto);
- a ordem de leitura, o modo, os horários, o dono, a compressão e a origem fora da versão: as entradas embaralhadas,
  dois pacotes com os mesmos arquivos e cabeçalhos diferentes (lidos pelo servidor) e duas pastas com os mesmos
  arquivos gravados em ordens, modos e horários diferentes (lidas pelo cliente) dão a mesma versão; as duas funções
  só recebem as entradas;
- um único byte trocado muda a versão;
- a ordem é a dos bytes UTF-8 do caminho (`B.txt` antes de `a.txt`, `a-b/x.txt` antes de `a/b.txt`), e não a
  alfabética: o manifesto montado em outra ordem dá outra versão.

No esqueleto, as duas `bank_version`, `read_archive` e `read_folder` lançam `NotImplementedError`: todo caso falha.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import pathlib
import random
from collections.abc import Mapping
from typing import Any

import pytest

from tests.helpers.banks import (
    USTAR_FORMAT,
    archive_of,
    load_vectors,
    make_archive,
    reference_version,
    regular,
    vector_files,
    write_tree,
)

VECTORS = load_vectors()
CASES = {case["id"]: case for case in VECTORS["cases"]}


def _server_entries(files: Mapping[str, bytes]) -> list[Any]:
    from cq_server.banks import BankEntry

    return [BankEntry(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest())
            for path, data in files.items()]


def _client_entries(files: Mapping[str, bytes]) -> list[Any]:
    from cq_client.banks import BankEntry

    return [BankEntry(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data)
            for path, data in files.items()]


def _server_version(files: Mapping[str, bytes]) -> str:
    from cq_server.banks import bank_version

    return bank_version(_server_entries(files))


def _client_version(files: Mapping[str, bytes]) -> str:
    from cq_client.banks import bank_version

    return bank_version(_client_entries(files))


def _manifest_hash(files: Mapping[str, bytes], order: list[str]) -> str:
    """O SHA-256 do manifesto com a soma na ordem dada (para mostrar que outra ordem dá outra versão)."""
    manifest = b"cq-bank-v1\n" + b"".join(
        path.encode("utf-8") + b"\0" + str(len(files[path])).encode("ascii") + b"\0"
        + hashlib.sha256(files[path]).hexdigest().encode("ascii") + b"\n"
        for path in order)
    return hashlib.sha256(manifest).hexdigest()


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_vectors_match_in_server_and_client(case_id: str) -> None:
    """Cada vetor fixo dá a versão gravada no arquivo de vetores nas duas implementações (servidor e cliente) e na
    referência do auxiliar; a versão é hexadecimal minúsculo de 64 caracteres."""
    case = CASES[case_id]
    files = vector_files(case)
    assert reference_version(files) == case["version"]
    server = _server_version(files)
    client = _client_version(files)
    assert server == case["version"]
    assert client == case["version"]
    assert len(server) == 64 and server == server.lower()


def test_reading_order_mode_times_and_origin_are_outside_the_version(tmp_path: pathlib.Path) -> None:
    """A ordem de leitura, o modo, os horários, o dono, a compressão e a origem não entram na versão.

    As entradas embaralhadas dão a mesma versão nos dois lados; as duas funções só recebem as entradas (nenhum
    parâmetro de origem); dois pacotes com os mesmos arquivos (um em ordem dos bytes, modo 0644, horário e dono
    zerados, pax, compressão 6; o outro em ordem inversa, modo 0755, horário e dono não zero, ustar, compressão 9)
    lidos pelo servidor dão a versão de referência; duas pastas com os mesmos arquivos gravados em ordens, horários e
    (fora do Windows) modos diferentes, lidas pelo cliente, também."""
    from cq_client.banks import bank_version as client_bank_version
    from cq_client.banks import read_folder
    from cq_server.banks import bank_version as server_bank_version
    from cq_server.banks import read_archive

    case = CASES["ordem_por_bytes"]
    files = vector_files(case)
    expected = case["version"]

    for bank_version in (server_bank_version, client_bank_version):
        assert list(inspect.signature(bank_version).parameters) == ["entries"]

    server_entries = _server_entries(files)
    client_entries = _client_entries(files)
    rng = random.Random(20261125)
    for _ in range(5):
        rng.shuffle(server_entries)
        rng.shuffle(client_entries)
        assert server_bank_version(server_entries) == expected
        assert client_bank_version(client_entries) == expected
    assert server_bank_version(iter(server_entries)) == expected
    assert client_bank_version(tuple(client_entries)) == expected

    plain = archive_of(files)
    paths = sorted(files, key=lambda p: p.encode("utf-8"), reverse=True)
    varied = make_archive(
        [regular(path, files[path], mode=0o755, mtime=1700000000, uid=1000, gid=1000, uname=b"prof",
                 gname=b"curso") for path in paths],
        fmt=USTAR_FORMAT, compresslevel=9)
    assert plain != varied
    for archive in (plain, varied):
        summary = read_archive(archive, max_files=20000, max_bytes=104857600)
        assert server_bank_version(summary.entries) == expected

    first = write_tree(tmp_path / "primeira", files)
    second = tmp_path / "segunda"
    for path in reversed(list(files)):
        write_tree(second, {path: files[path]})
    for index, path in enumerate(files):
        target = second.joinpath(*path.split("/"))
        os.utime(target, (1600000000 + index, 1600000000 + index))
        if os.name != "nt":
            target.chmod(0o600 if index % 2 else 0o755)
    for root in (first, second):
        bank = read_folder(str(root.resolve()))
        assert client_bank_version(bank.entries) == expected
        assert bank.git_commit is None


def test_one_changed_byte_changes_the_version() -> None:
    """Um único byte trocado muda a versão: os vetores `minimo` e `um_byte` diferem num byte do `infoCourse.json` e
    têm versões diferentes nos dois lados; trocar um byte de qualquer arquivo do vetor maior, ou acrescentar um byte,
    também muda a versão."""
    minimum = vector_files(CASES["minimo"])
    one_byte = vector_files(CASES["um_byte"])
    left, right = minimum["infoCourse.json"], one_byte["infoCourse.json"]
    assert len(left) == len(right) and sum(a != b for a, b in zip(left, right)) == 1
    for version in (_server_version, _client_version):
        assert version(minimum) == CASES["minimo"]["version"]
        assert version(one_byte) == CASES["um_byte"]["version"]
        assert version(minimum) != version(one_byte)

    files = vector_files(CASES["ordem_por_bytes"])
    base = _server_version(files)
    assert _client_version(files) == base
    for path, data in files.items():
        if not data:
            continue
        flipped = dict(files)
        flipped[path] = bytes([data[0] ^ 0x01]) + data[1:]
        assert _server_version(flipped) != base, path
        assert _client_version(flipped) != base, path
        assert _server_version(flipped) == _client_version(flipped) == reference_version(flipped)
    grown = dict(files)
    grown["vazio.txt"] = b"\0"
    assert _server_version(grown) != base
    assert _client_version(grown) != base


def test_order_is_by_utf8_bytes_of_the_path() -> None:
    """A soma do manifesto vai em ordem crescente dos bytes UTF-8 do caminho: `B.txt` antes de `a.txt` e
    `a-b/x.txt` antes de `a/b.txt` (`-` é 0x2D e `/` é 0x2F). O manifesto montado na ordem alfabética sem caixa ou
    na ordem inversa dá outra versão; as duas implementações dão a da ordem dos bytes."""
    files = vector_files(CASES["ordem_por_bytes"])
    by_bytes = sorted(files, key=lambda p: p.encode("utf-8"))
    assert by_bytes.index("B.txt") < by_bytes.index("a.txt")
    assert by_bytes.index("a-b/x.txt") < by_bytes.index("a/b.txt")
    expected = _manifest_hash(files, by_bytes)
    assert expected == CASES["ordem_por_bytes"]["version"]

    casefold = sorted(files, key=lambda p: (p.casefold(), p))
    for other in (casefold, list(reversed(by_bytes))):
        assert other != by_bytes
        assert _manifest_hash(files, other) != expected

    assert _server_version(files) == expected
    assert _client_version(files) == expected
    two = {"a.txt": b"a\n", "B.txt": b"B\n"}
    assert _server_version(two) == _client_version(two) == _manifest_hash(two, ["B.txt", "a.txt"])
    assert _server_version(two) != _manifest_hash(two, ["a.txt", "B.txt"])
