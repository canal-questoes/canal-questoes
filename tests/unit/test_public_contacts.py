"""Testes da lista de contatos públicos, `ops/export/public-contacts.txt`, e do seu uso na varredura, na exportação e
no pacote de implantação.

A lista guarda os endereços de e-mail públicos do projeto que a varredura aceita no tipo `email`. O que se confere:
- `repo_scan.parse_public_contacts(text)`: a lista vazia, só com comentários ou ausente vale como vazia; uma linha por
  endereço ASCII, na forma que o detector `email` reconhece; repetição comparada sem caixa, endereço inválido, domínio
  reservado (que a varredura já aceita sem a lista) e fim de linha CRLF levantam `repo_scan.ListInvalid`, com a
  mensagem fixa, sem repetir a linha;
- `repo_scan.scan(..., public_contacts=...)`: no tipo `email`, um endereço da lista, comparado sem caixa, não gera
  achado; outro endereço real continua acusado, e nenhum outro tipo de achado muda;
- a exportação e o pacote leem a lista do commit, pela mesma função `parse_public_contacts`; com a lista vazia ou
  ausente, o endereço continua recusado; com a lista inválida, os dois saem com `internal` (1), sem eco da linha;
- a lista real tem no máximo uma entrada.

Sempre em repositórios de fixture no `tmp_path` (`tests/helpers/git_fixture.py`), nunca contra a árvore real (salvo a
leitura da lista real no último caso). Os endereços e os demais valores que a varredura acusa são montados em tempo
de execução, para este arquivo não ter achado.

**Vermelho esperado:** `parse_public_contacts` e a varredura com a lista não vazia lançam `NotImplementedError` até a
implementação da lista; no esqueleto, a exportação e o pacote ignoram a lista (o endereço da lista é recusado, e a lista
inválida passa). As asserções sobre o que já funciona (a varredura sem a
lista, a recusa com a lista vazia) ficam na mesma função de um caso vermelho.

**[I]** Escolhas deste teste:
- o conjunto devolvido é comparado sem caixa (a função pode ou não normalizar a caixa);
- dois endereços distintos são uma lista válida para a função: o limite de uma entrada vale para a lista real e é
  conferido aqui e no teste de contrato, não pela função;
- a fixture da exportação traz a pasta da sobreposição pública com os três arquivos, para a recusa da sobreposição
  não esconder o caso;
- a função é observada por um espião posto no módulo `repo_scan` compartilhado (o mesmo que a exportação e o pacote
  carregam pelo caminho); cada execução a chama uma vez, com o texto do commit.
"""

from __future__ import annotations

import os
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import pytest

from tests.helpers.git_fixture import fingerprint, git_env, make_repo, modify
from tests.helpers.ops import REPO, load_ops

export_public = load_ops("ops/export/export_public.py")
make_bundle = load_ops("ops/deploy/make_bundle.py")
repo_scan = load_ops("ops/export/repo_scan.py")

MESSAGES = export_public.EXPORT_MESSAGES
LINE = export_public.FINDING_LINE_TEMPLATE
AUTHOR_NAME = "Pessoa Autora da Exportação"
AUTHOR_EMAIL = "autora@exportacao.test"
CONTACTS_PATH = "ops/export/public-contacts.txt"
REAL_CONTACTS = REPO / "ops" / "export" / "public-contacts.txt"


def _join(*parts: str) -> str:
    return "".join(parts)


# Os endereços reais da fixture (domínios fora dos reservados), montados aqui.
CONTACT = _join("Contato.Projeto", "@", "Dominio-Publico.org.br")
OTHER = _join("pessoa.qualquer", "@", "outro-provedor.com.br")
SECOND = _join("segundo.contato", "@", "dominio-publico.org.br")
RA_LINE = _join("RA ", "2468024")
PHONE = _join("(19) ", "99999-0000")

HEADER = "# Contatos públicos (fixture).\n#\n# Um endereço por linha.\n"


def _lowered(values: frozenset[str]) -> set[str]:
    return {value.lower() for value in values}


# --------------------------------------------------------------------------------------------------
# A função de leitura da lista
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("", set()),
    (HEADER, set()),
    ("# só comentário\n\n# outro comentário\n", set()),
    (HEADER + CONTACT + "\n", {CONTACT.lower()}),
    (CONTACT, {CONTACT.lower()}),
    (HEADER + "\n" + CONTACT.upper() + "\n\n", {CONTACT.lower()}),
    (HEADER + CONTACT + "\n" + OTHER + "\n", {CONTACT.lower(), OTHER.lower()}),
], ids=["vazia", "so-cabecalho", "comentario-e-brancas", "um-endereco", "sem-fim-de-linha", "maiusculas",
        "dois-distintos"])
def test_parse_reads_one_address_per_line(text: str, expected: set[str]) -> None:
    """Uma linha por endereço; comentários e linhas vazias ignorados; o resultado é um `frozenset`, comparado aqui sem
    caixa. Vermelho até a implementação da lista (a função lança `NotImplementedError`)."""
    parsed = repo_scan.parse_public_contacts(text)
    assert isinstance(parsed, frozenset)
    assert _lowered(parsed) == expected


@pytest.mark.parametrize("bad", [
    pytest.param(CONTACT + "\n" + CONTACT.upper() + "\n", id="repetido-sem-caixa"),
    pytest.param(CONTACT + "\n" + CONTACT + "\n", id="repetido-igual"),
    pytest.param(HEADER.replace("\n", "\r\n") + CONTACT + "\r\n", id="crlf"),
    pytest.param(CONTACT + "\r\n", id="crlf-so-no-endereco"),
    pytest.param(_join("sem-arroba.", "dominio-publico.org.br") + "\n", id="sem-arroba"),
    pytest.param(_join("pessoa", "@", "semponto") + "\n", id="dominio-sem-ponto"),
    pytest.param(CONTACT + " " + OTHER + "\n", id="dois-na-linha"),
    pytest.param(CONTACT + " extra\n", id="texto-depois"),
    pytest.param(_join("joão", "@", "dominio-publico.org.br") + "\n", id="nao-ascii"),
    pytest.param(_join("fulana", "@", "example.com") + "\n", id="reservado-example-com"),
    pytest.param(_join("fulana", "@", "turma.Example.ORG") + "\n", id="reservado-subdominio"),
    pytest.param(_join("fulana", "@", "example.net") + "\n", id="reservado-example-net"),
    pytest.param(_join("fulana", "@", "servico.test") + "\n", id="reservado-test"),
    pytest.param(_join("fulana", "@", "host.invalid") + "\n", id="reservado-invalid"),
    pytest.param(_join("fulana", "@", "maquina.localhost") + "\n", id="reservado-localhost"),
])
def test_parse_rejects_invalid_lists_without_echo(bad: str) -> None:
    """Repetido (sem caixa), CRLF, endereço fora da forma do detector `email`, não ASCII ou de domínio reservado →
    `ListInvalid`, um `ValueError` com a mensagem fixa e sem nenhum pedaço da linha. Vermelho até a implementação
    da lista (a função lança `NotImplementedError`)."""
    with pytest.raises(repo_scan.ListInvalid) as raised:
        repo_scan.parse_public_contacts(bad)
    error = raised.value
    assert isinstance(error, ValueError)
    assert str(error) == "lista inválida"
    shown = str(error) + repr(error) + repr(error.args)
    for piece in (line.strip() for line in bad.replace("\r", "").split("\n")):
        if piece and not piece.startswith("#"):
            assert piece not in shown and piece.lower() not in shown.lower()


def test_real_list_has_at_most_one_entry() -> None:
    """A lista real é válida e tem no máximo uma entrada. Vermelho até a implementação da lista (a função lança
    `NotImplementedError`)."""
    text = REAL_CONTACTS.read_text(encoding="utf-8")
    assert len(repo_scan.parse_public_contacts(text)) <= 1


# --------------------------------------------------------------------------------------------------
# A varredura com a lista
# --------------------------------------------------------------------------------------------------


def test_scan_accepts_listed_address_in_any_case_and_nothing_else(tmp_path: Path) -> None:
    """No tipo `email`, o endereço da lista (com outra caixa) não gera achado; outro endereço real, o endereço da
    lista com um prefixo ou noutro subdomínio continuam acusados; o RA e o telefone, inclusive na linha do endereço
    aceito, também. Sem a lista, todos os e-mails são acusados. Vermelho até a implementação
    da lista (a varredura com a lista não vazia lança `NotImplementedError`)."""
    lines = [
        "contato: " + CONTACT.lower(),                   # 1: aceito com a lista
        "outro: " + OTHER,                               # 2: acusado
        RA_LINE,                                         # 3: ra
        "telefone: " + PHONE,                            # 4: phone
        CONTACT.upper() + " " + RA_LINE,                 # 5: o e-mail aceito, o ra acusado
        "x" + CONTACT,                                   # 6: outro endereço (prefixo)
        CONTACT.replace("@", "@sub."),                   # 7: outro endereço (subdomínio)
        CONTACT + " e " + OTHER,                         # 8: um aceito e um acusado na mesma linha
        "",
    ]
    (tmp_path / "contatos.md").write_text("\n".join(lines), encoding="utf-8")
    paths = ["contatos.md"]

    # Sem a lista: o comportamento que o esqueleto já tem.
    baseline = repo_scan.scan(tmp_path, paths)
    assert repo_scan.scan(tmp_path, paths, public_contacts=frozenset()) == baseline
    as_pairs = {(found.line, found.kind) for found in baseline}
    assert as_pairs == {(1, "email"), (2, "email"), (3, "ra"), (4, "phone"), (5, "email"), (5, "ra"),
                        (6, "email"), (7, "email"), (8, "email")}

    for contacts in (frozenset({CONTACT}), frozenset({CONTACT.lower()}), frozenset({CONTACT.upper()})):
        findings = repo_scan.scan(tmp_path, paths, public_contacts=contacts)
        assert {(found.line, found.kind) for found in findings} == as_pairs - {(1, "email"), (5, "email")}
        assert all(found.path == "contatos.md" for found in findings)


# --------------------------------------------------------------------------------------------------
# A exportação e o pacote
# --------------------------------------------------------------------------------------------------

ALLOWLIST = (
    b"# lista positiva de teste\n"
    b"README.md\n"
    b"src/**\n"
    b"ops/**\n"
)
OVERLAY = {
    "ops/export/publico/AGENTS.md": b"# Agentes (fixture)\n",
    "ops/export/publico/CLAUDE.md": b"# Claude (fixture)\n",
    "ops/export/publico/ROADMAP.md": b"# Roteiro (fixture)\n",
}


def _files(contacts: str | None, readme_contact: str | None) -> dict[str, bytes]:
    readme = "# Projeto de fixture\n" + ("" if readme_contact is None else "Contato: " + readme_contact + "\n")
    files: dict[str, bytes] = {
        "README.md": readme.encode("utf-8"),
        "src/app.py": b'print("ola")\n',
        "ops/export/allowlist.txt": ALLOWLIST,
        "ops/export/fictitious.txt": b"# valores ficticios de teste\n",
        "ops/export/binary-allowlist.txt": b"",
        **OVERLAY,
    }
    if contacts is not None:
        files[CONTACTS_PATH] = contacts.encode("utf-8")
    return files


@pytest.fixture
def git_environ(tmp_path: Path) -> Iterator[None]:
    """O processo com o ambiente do `git` dos testes (sem a configuração do desenvolvedor) durante a chamada."""
    with mock.patch.dict(os.environ, git_env(tmp_path), clear=True):
        yield


@pytest.fixture
def parse_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Um espião na função de leitura da lista: guarda o texto de cada chamada e devolve o resultado da original."""
    original: Callable[[str], frozenset[str]] = repo_scan.parse_public_contacts
    calls: list[str] = []

    def spy(text: str) -> frozenset[str]:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(repo_scan, "parse_public_contacts", spy)
    return calls


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _empty_dir(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir()
    return target


def _dest_clean(dest: Path) -> bool:
    return not dest.exists() or not any(dest.iterdir())


def _export(tmp_path: Path, name: str, files: dict[str, bytes]) -> tuple[Path, Path, int, tuple[str, ...]]:
    repo = make_repo(tmp_path, files, name="origem-" + name)
    before = fingerprint(repo)
    dest = _empty_dir(tmp_path, "publico-" + name)
    code = export_public.export(repo, dest, AUTHOR_NAME, AUTHOR_EMAIL)
    assert fingerprint(repo) == before
    return repo, dest, code, before


def _bundle(tmp_path: Path, name: str, files: dict[str, bytes]) -> tuple[Path, Path, int]:
    repo = make_repo(tmp_path, files, name="origem-" + name)
    before = fingerprint(repo)
    out = _empty_dir(tmp_path, "saida-" + name) / "cq-bundle-teste.tar.gz"
    code = make_bundle.build_bundle(repo, out)
    assert fingerprint(repo) == before
    return repo, out, code


def _members(out: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(out, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                files[name] = handle.read()
    return files


def _email_line() -> str:
    return LINE.format(path="README.md", line=2, kind="email")


def test_export_reads_the_list_from_the_commit(tmp_path: Path, git_environ: None, parse_calls: list[str],
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """A exportação lê a lista do commit pela função de leitura (uma chamada, com o texto do commit, mesmo com a cópia
    de trabalho alterada sem commit) e aceita o endereço dela, com outra caixa, no README; sem a lista ou com ela
    vazia, o mesmo endereço continua recusado (`findings`, a linha sem o valor, o destino vazio). Vermelho até a
    exportação ler a lista (a do esqueleto a ignora e recusa o endereço)."""
    # Sem a lista e com a lista vazia: a recusa que o esqueleto já tem.
    for name, contacts in (("ausente", None), ("vazia", HEADER)):
        _, dest, code, _ = _export(tmp_path, name, _files(contacts, CONTACT.upper()))
        output = _output(capsys)
        assert code == MESSAGES["findings"][0], output
        assert MESSAGES["findings"][1] in output and _email_line() in output
        assert CONTACT.lower() not in output.lower()
        assert _dest_clean(dest)
    parse_calls.clear()

    listed = HEADER + CONTACT.lower() + "\n"
    repo = make_repo(tmp_path, _files(listed, CONTACT.upper()), name="origem-lista")
    # A cópia de trabalho diferente do commit: a exportação lê o commit.
    modify(repo, CONTACTS_PATH, HEADER.encode("utf-8"))
    before = fingerprint(repo)
    dest = _empty_dir(tmp_path, "publico-lista")

    code = export_public.export(repo, dest, AUTHOR_NAME, AUTHOR_EMAIL)

    output = _output(capsys)
    assert code == 0, output
    assert MESSAGES["ok"][1] in output
    assert parse_calls == [listed]
    assert (dest / "README.md").read_bytes() == _files(listed, CONTACT.upper())["README.md"]
    assert (dest / "ops" / "export" / "public-contacts.txt").read_bytes() == listed.encode("utf-8")
    assert fingerprint(repo) == before


def test_bundle_reads_the_list_from_the_commit(tmp_path: Path, git_environ: None, parse_calls: list[str],
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """O pacote lê a lista do commit pela mesma função de leitura (uma chamada, com o texto do commit) e aceita o
    endereço dela no README; sem a lista ou com ela vazia, o endereço continua recusado. Vermelho até o pacote
    carregar a lista (o do esqueleto a ignora e recusa o endereço)."""
    # Sem a lista e com a lista vazia: a recusa que o esqueleto já tem.
    for name, contacts in (("ausente", None), ("vazia", HEADER)):
        _, out, code = _bundle(tmp_path, name, _files(contacts, CONTACT.upper()))
        output = _output(capsys)
        assert code == MESSAGES["findings"][0], output
        assert MESSAGES["findings"][1] in output and _email_line() in output
        assert CONTACT.lower() not in output.lower()
        assert not out.exists()
    parse_calls.clear()

    listed = HEADER + CONTACT.lower() + "\n"
    files = _files(listed, CONTACT.upper())
    _, out, code = _bundle(tmp_path, "lista", files)

    output = _output(capsys)
    assert code == 0, output
    assert MESSAGES["bundle_ok"][1] in output
    assert parse_calls == [listed]
    members = _members(out)
    assert members["README.md"] == files["README.md"]
    assert members[CONTACTS_PATH] == listed.encode("utf-8")


@pytest.mark.parametrize("bad", [
    pytest.param(HEADER + _join("fulana", "@", "example.com") + "\n", id="dominio-reservado"),
    pytest.param(HEADER + SECOND + "\n" + SECOND.upper() + "\n", id="repetido"),
    pytest.param(HEADER + _join("sem-arroba.", "dominio-publico.org.br") + "\n", id="fora-da-forma"),
])
def test_invalid_list_is_internal_error_in_export_and_bundle(tmp_path: Path, git_environ: None, bad: str,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    """A lista inválida no commit → `internal` (1) na exportação (o destino vazio) e no pacote (nenhum arquivo de
    saída), com a mensagem fixa e sem nenhum pedaço da linha; a origem intacta. Vermelho até a exportação e o
    pacote carregarem a lista (no esqueleto, os dois a ignoram e terminam com 0)."""
    files = _files(bad, None)
    entry = bad.splitlines()[-1]

    _, dest, code, _ = _export(tmp_path, "exportacao", files)
    output = _output(capsys)
    assert code == MESSAGES["internal"][0] == 1, output
    assert MESSAGES["internal"][1] in output
    assert entry not in output and entry.lower() not in output.lower()
    assert _dest_clean(dest)

    _, out, code = _bundle(tmp_path, "pacote", files)
    output = _output(capsys)
    assert code == MESSAGES["internal"][0], output
    assert MESSAGES["internal"][1] in output
    assert entry not in output and entry.lower() not in output.lower()
    assert not out.exists()
