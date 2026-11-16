"""Testes do pacote de implantação, `ops/deploy/make_bundle.py`, montado a partir dos arquivos
rastreados que casam a lista da exportação.

Sempre num repositório de fixture (`tests/helpers/git_fixture.py`) com listas de teste em `ops/export/`; nunca contra
a árvore real. O que se confere:
- o `.tar.gz` tem exatamente os rastreados que casam a lista, com os bytes do commit, mais `SOURCE_COMMIT` (40 hex
  e `\\n`) e `BUNDLE_SHA256SUMS` (formato `sha256sum`, todos os arquivos menos ele mesmo, conferidos); nada de
  `.env*` (menos `.env.example`), `docs/execucao`, `docs/base-de-conhecimento`, `docs/monografia` nem `.git`;
- arquivo da lista alterado → `pending_changes` (65), no modo `head`, sem pacote;
- modo `worktree`: um não rastreado não ignorado que casa a lista entra com os bytes da árvore, um ignorado fica de
  fora, e `SOURCE_COMMIT` = `<40 hex>+worktree`;
- uma `CQ_MASTER_KEY` e um `CQ_SMTP_PASSWORD` com a forma do `.env` plantados num arquivo da lista → recusa
  (`findings`, 65) com `arquivo:linha: env_assignment`, os valores ausentes de toda saída e do destino, nenhum
  `.tar.gz` deixado;
- saída dentro da origem → `out_invalid` (64);
- a origem intacta (`origin_fingerprint` e o `fingerprint` do ajudante, antes e depois, em sucesso e em recusa).

**[I]** Escolhas deste teste:
- o pacote e a exportação leem as listas (`allowlist.txt`, `fictitious.txt`, `binary-allowlist.txt`) de
  `<repo>/ops/export/`, a mesma posição da árvore real, e por isso o repositório de fixture as traz;
- `out` é o caminho do arquivo `.tar.gz` (`--out <arquivo .tar.gz>`), com o diretório pai já
  existente; os membros ficam na raiz do arquivo (o teste de sistema roda `docker compose -f <dir>/compose.yaml`
  sobre a extração), com um `./` inicial tolerado;
- as mensagens (de `EXPORT_MESSAGES`, as mesmas da exportação) e as linhas `arquivo:linha: tipo` saem pelo
  `sys.stdout`/`sys.stderr` do momento da chamada; o teste confere a união dos dois;
- os valores plantados são montados em tempo de execução, para que o próprio arquivo de teste não tenha achado.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tarfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest import mock

import pytest

from tests.helpers import git_fixture
from tests.helpers.git_fixture import add_ignored, add_untracked, fingerprint, git_env, make_repo, modify
from tests.helpers.ops import clean_env, load_ops, run_main

make_bundle = load_ops("ops/deploy/make_bundle.py")
export_public = load_ops("ops/export/export_public.py")
repo_scan = load_ops("ops/export/repo_scan.py")

MESSAGES = export_public.EXPORT_MESSAGES
LINE = export_public.FINDING_LINE_TEMPLATE

ALLOWLIST = (
    b"# lista positiva de teste\n"
    b"src/**\n"
    b"ops/**\n"
    b"deploy/**\n"
    b"docs/operacao/**\n"
    b"compose.yaml\n"
    b".env.example\n"
)
FICTITIOUS = b"# valores ficticios de teste\n"
BINARY_ALLOWLIST = b"src/imagens/logo.png\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"

BASE_FILES: dict[str, bytes] = {
    "compose.yaml": b"services:\n  api:\n    image: exemplo\n",
    "src/app.py": b'print("ola")\n',
    "src/dados/tabela.json": b'{"chave": "valor"}\n',
    "src/imagens/logo.png": PNG,
    "docs/operacao/guia.md": b"# Guia\n\nPasso 1.\n",
    ".env.example": b"CQ_DB_PASSWORD=<senha do banco>\nCQ_ADMIN_TOKEN=<token>\n",
    "ops/export/allowlist.txt": ALLOWLIST,
    "ops/export/fictitious.txt": FICTITIOUS,
    "ops/export/binary-allowlist.txt": BINARY_ALLOWLIST,
    # Fora da lista: nunca entram no pacote.
    ".env": b"CQ_API_PORT=8000\n",
    ".env.local": b"CQ_API_PORT=8001\n",
    "docs/execucao/estado.md": b"estado do loop\n",
    "docs/base-de-conhecimento/fatos.md": b"fatos\n",
    "docs/monografia/cap1.md": b"capitulo\n",
    "notas/privado.md": b"rascunho\n",
}
EXPECTED_SELECTED = frozenset({
    "compose.yaml", "src/app.py", "src/dados/tabela.json", "src/imagens/logo.png", "docs/operacao/guia.md",
    ".env.example", "ops/export/allowlist.txt", "ops/export/fictitious.txt", "ops/export/binary-allowlist.txt",
})
NEVER_IN_BUNDLE = re.compile(r"^(\.env(?!\.example$).*|docs/(execucao|base-de-conhecimento|monografia)/.*)$")
SUMS_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<path>\S(?:.*\S)?)$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture
def git_environ(tmp_path: Path) -> Iterator[None]:
    """O processo com o ambiente do `git` dos testes (sem a configuração do desenvolvedor) durante a chamada."""
    with mock.patch.dict(os.environ, git_env(tmp_path), clear=True):
        yield


def _head(repo: Path) -> str:
    return git_fixture._git(repo, "rev-parse", "HEAD").decode("ascii").strip()


def _states(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return repo_scan.origin_fingerprint(repo), fingerprint(repo)


def _out(tmp_path: Path) -> Path:
    out_dir = tmp_path / "saida"
    out_dir.mkdir()
    return out_dir / "cq-bundle-teste.tar.gz"


def _members(out: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(out, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name[2:] if member.name.startswith("./") else member.name
            assert not name.startswith("/") and ".." not in Path(name).parts, f"membro fora da raiz: {name}"
            assert member.isfile() or member.isdir(), f"membro que não é arquivo nem diretório: {name}"
            if member.isfile():
                handle = archive.extractfile(member)
                assert handle is not None
                assert name not in files, f"membro repetido: {name}"
                files[name] = handle.read()
    return files


def _assert_bundle(members: Mapping[str, bytes], expected: Mapping[str, bytes], source_commit: str) -> None:
    special = {make_bundle.SOURCE_COMMIT_FILE, make_bundle.BUNDLE_SUMS_FILE}
    assert set(members) == set(expected) | special
    for path, data in expected.items():
        assert members[path] == data, f"{path}: bytes diferentes"
    assert [name for name in members if ".git" in Path(name).parts] == []
    assert [name for name in members if NEVER_IN_BUNDLE.match(name)] == []
    assert members[make_bundle.SOURCE_COMMIT_FILE] == source_commit.encode("ascii")

    listed: dict[str, str] = {}
    for line in members[make_bundle.BUNDLE_SUMS_FILE].decode("utf-8").splitlines():
        match = SUMS_LINE.match(line)
        assert match is not None, f"linha fora do formato sha256sum: {line!r}"
        assert match["path"] not in listed, f"arquivo repetido nas somas: {match['path']}"
        listed[match["path"]] = match["digest"]
    assert set(listed) == set(members) - {make_bundle.BUNDLE_SUMS_FILE}
    for path, digest in listed.items():
        assert hashlib.sha256(members[path]).hexdigest() == digest, f"{path}: SHA-256 não confere"


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_bundle_has_listed_files_with_commit_bytes(tmp_path: Path, git_environ: None,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """só os rastreados que casam a lista, com os bytes do commit; `SOURCE_COMMIT` e
    `BUNDLE_SHA256SUMS` conferidos; nada fora da lista; a origem intacta."""
    # As constantes reais do esqueleto (na mesma função do caso vermelho).
    assert (make_bundle.SOURCE_COMMIT_FILE, make_bundle.BUNDLE_SUMS_FILE) == ("SOURCE_COMMIT", "BUNDLE_SHA256SUMS")
    assert make_bundle.WORKTREE_SUFFIX == "+worktree"
    assert make_bundle.load_repo_scan() is repo_scan

    repo = make_repo(tmp_path, BASE_FILES)
    # Um não rastreado que casa a lista não entra no modo `head`.
    add_untracked(repo, "src/nao_rastreado.py", b"x = 1\n")
    before = _states(repo)
    out = _out(tmp_path)

    code = make_bundle.build_bundle(repo, out)

    output = _output(capsys)
    assert code == 0, output
    assert MESSAGES["bundle_ok"][1] in output
    head = _head(repo)
    assert HEX40.match(head)
    expected = {path: BASE_FILES[path] for path in EXPECTED_SELECTED}
    _assert_bundle(_members(out), expected, head + "\n")
    assert _states(repo) == before


def test_pending_change_in_listed_file_is_refused(tmp_path: Path, git_environ: None,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """arquivo da lista com alteração pendente → `pending_changes` (65), no modo `head`, sem pacote."""
    repo = make_repo(tmp_path, BASE_FILES)
    modify(repo, "src/app.py", b'print("alterado")\n')
    before = _states(repo)
    out = _out(tmp_path)

    code = make_bundle.build_bundle(repo, out)

    output = _output(capsys)
    assert code == MESSAGES["pending_changes"][0] == 65
    assert MESSAGES["pending_changes"][1] in output
    assert list(out.parent.iterdir()) == []
    assert _states(repo) == before


def test_worktree_mode_takes_untracked_not_ignored_files(tmp_path: Path, git_environ: None,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """no modo `worktree`, os rastreados e os não rastreados não ignorados que casam a lista, com os bytes da
    árvore; o ignorado e o que não casa ficam de fora; `SOURCE_COMMIT` = `<40 hex>+worktree`."""
    repo = make_repo(tmp_path, BASE_FILES)
    modify(repo, "src/app.py", b'print("da arvore")\n')
    add_untracked(repo, "src/novo.py", b"novo = True\n")
    add_ignored(repo, "src/ignorado.py", b"ignorado = True\n")
    add_untracked(repo, "notas/rascunho.md", b"fora da lista\n")
    before = _states(repo)
    out = _out(tmp_path)

    code = make_bundle.build_bundle(repo, out, source="worktree")

    output = _output(capsys)
    assert code == 0, output
    expected = {path: BASE_FILES[path] for path in EXPECTED_SELECTED}
    expected["src/app.py"] = b'print("da arvore")\n'
    expected["src/novo.py"] = b"novo = True\n"
    _assert_bundle(_members(out), expected, _head(repo) + make_bundle.WORKTREE_SUFFIX + "\n")
    assert _states(repo) == before


def test_env_assignments_planted_in_listed_file_are_refused(tmp_path: Path, git_environ: None,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    """`CQ_MASTER_KEY` e `CQ_SMTP_PASSWORD` com a forma do `.env` num arquivo da lista → `findings`
    (65), uma linha `arquivo:linha: env_assignment` por atribuição, sem os valores, e nenhum `.tar.gz`."""
    master_key = secrets.token_urlsafe(32)
    smtp_password = secrets.token_hex(8)
    planted = ("# configuracao da VM\n"
               + "CQ_MASTER_KEY" + "=" + master_key + "\n"
               + "CQ_SMTP_PASSWORD" + "=" + smtp_password + "\n").encode("ascii")
    repo = make_repo(tmp_path, {**BASE_FILES, "deploy/vm.conf": planted})
    before = _states(repo)
    out = _out(tmp_path)

    code = make_bundle.build_bundle(repo, out)

    output = _output(capsys)
    assert code == MESSAGES["findings"][0] == 65
    assert MESSAGES["findings"][1] in output
    for line in (2, 3):
        assert LINE.format(path="deploy/vm.conf", line=line, kind="env_assignment") in output.splitlines()
    assert master_key not in output and smtp_password not in output
    assert [path.name for path in out.parent.iterdir()] == []
    assert not out.exists()
    assert _states(repo) == before


def test_output_inside_origin_is_refused(tmp_path: Path, git_environ: None,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """saída dentro da origem → `out_invalid` (64), nada escrito."""
    repo = make_repo(tmp_path, BASE_FILES)
    before = _states(repo)
    out = repo / "cq-bundle-dentro.tar.gz"

    code = make_bundle.build_bundle(repo, out)

    output = _output(capsys)
    assert code == MESSAGES["out_invalid"][0] == 64
    assert MESSAGES["out_invalid"][1] in output
    assert not out.exists()
    assert _states(repo) == before


def test_main_rejects_missing_out() -> None:
    """sem `--out` → `invalid_arguments` (64) no stderr, nenhum pacote."""
    result = run_main(make_bundle, [], env=clean_env())
    assert result.code == MESSAGES["invalid_arguments"][0] == 64
    assert MESSAGES["invalid_arguments"][1] in result.stderr.splitlines()
