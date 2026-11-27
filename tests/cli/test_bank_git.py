"""A leitura de um banco de questões num commit de um repositório git local (`cq banks upload --git`).

Com o `git` real da máquina e repositórios sintéticos no `tmp_path` (`tests/helpers/git_fixture.py`), pelo
executor real do produto (`cq_client.stack_compose.GitRunner`) e por `cq_client.banks.read_git`; os dublês do
executor só para o que o `git` real não reproduz (o `git` ausente e o executor que não pode ser chamado). Como o
executor do produto remove as variáveis `GIT_*` herdadas, cada teste aponta `HOME`, `USERPROFILE` e
`XDG_CONFIG_HOME` para uma pasta vazia (nenhuma configuração global do desenvolvedor vale). Sem o `git` no
`PATH`, cada teste falha com a mensagem fixa de ambiente do auxiliar (`require_git`), que não é vermelho do
produto.

O que fica fixado:
- a revisão (`HEAD`, ramo, etiqueta leve ou anotada, hash completo ou curto, `HEAD~1`) resolve para o hash do
  commit, que vem em `git_commit` (40 hexadecimais, ou 64 num repositório SHA-256); as entradas são as da árvore
  desse commit, nunca as da pasta de trabalho (mudanças, arquivos novos e apagados ficam fora);
- a subpasta da pasta de trabalho dá a árvore dela no commit, com os caminhos relativos a ela; a subpasta que não
  existe no commit, ou que é arquivo nele → `not_in_revision`; as regras da pasta valem (`not_a_course`);
- os bytes são os do objeto, sem filtros: um `* text eol=crlf` não muda o conteúdo, um filtro e um `textconv`
  sentinela configurados no repositório nunca rodam, um `refs/replace` não troca o conteúdo; um `GIT_DIR` e um
  `GIT_WORK_TREE` herdados apontando para outro repositório são ignorados;
- um `git` sentinela (`git.bat` e `git.cmd` no Windows, um `git` executável nos demais sistemas) no diretório
  atual, numa entrada relativa do `PATH` e dentro da pasta do banco nunca roda; o `git` achado só dentro da pasta
  do banco é recusado como ausente (`git_missing`), e sem nenhum `git` no `PATH` também;
- a revisão fora da forma aceita (começo com `-`, espaço, `:`, vazia, longa demais, fora do ASCII) → `revision_invalid`
  antes de qualquer chamada ao `git`; a mesma revisão é aceita ou recusada igual por `REVISION_PATTERN` (com o
  teto de tamanho), pelo `cq` e pelo esquema de entrada da ferramenta do MCP; a revisão que não existe →
  `revision_not_found`; a pasta fora de repositório → `not_a_repository`;
- os modos `120000` e `160000` → `git_symlink` e `git_submodule`, com o caminho relativo dentro do banco;
- as regras de caminho e os tetos da pasta valem no commit: um caminho com barra invertida, com o ESC, com um byte
  fora de UTF-8, com um componente de 256 bytes ou com 4097 bytes dentro do banco → `path_not_allowed`, a recusa
  da pasta, com o caminho relativo dentro do banco (também com o banco numa subpasta); no limite (um componente de
  255 bytes e um caminho de 4096 bytes dentro do banco), o caminho é lido; o commit com 20000 arquivos e o com
  100 MiB de conteúdo são lidos, e um arquivo ou um byte a mais → `too_many_files` e `too_large`. Esses caminhos
  entram só no índice, pela `update-index -z --index-info` com os bytes crus na entrada padrão, com a proteção de
  nomes do NTFS do git desligada só nesse comando (com ela, o Git for Windows ignora em silêncio a barra invertida
  e os controles); o auxiliar confere que cada caminho entrou;
- o mesmo conteúdo como pasta e como commit dá as mesmas entradas, a mesma versão e o mesmo pacote, também com o
  modo `100755` (o bit de execução) em arquivos do commit, que não entra na versão nem no pacote (todo membro no
  modo `0644`);
- `cq banks upload --git` envia a origem `git` e o hash e imprime `Origem: git, commit <hash>.` (as linhas do
  envio fixadas em `tests/cli/test_cli_banks.py`); também a partir da própria pasta (`cq banks upload .`); as
  recusas saem como as da pasta (`Uso incorreto: <mensagem fixa>`, código 2), sem pedido à API.

No esqueleto, `read_git` e `GitRunner.run` lançam `NotImplementedError`, e a folha do CLI também: todo caso daqui
fica vermelho por isso até a implementação do executor do `git`, da leitura do commit e do `--git` no CLI.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path
from typing import Any

import httpx2
import pytest
from jsonschema import Draft202012Validator

from tests.helpers import banks as bank_fixtures
from tests.helpers import git_fixture
from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import invoke_cli
from tests.helpers.contract import REPO, assert_matches

WINDOWS = os.name == "nt"
LOCAL_URL = "http://127.0.0.1:8123"
BANKS_PATH = "/v1/question-banks"
BANK_NAME = "banco-git"
CREATED_AT = "2031-01-01T00:00:00Z"
MCP_UPLOAD_SCHEMA = REPO / "contracts" / "mcp" / "v1" / "schemas" / "upload_question_bank.input.schema.json"

INFO_COURSE = b'{"uuid": "00000000-0000-4000-8000-0000000000ac", "name": "Curso sintetico git"}\n'
BASE_FILES: dict[str, bytes] = {
    "infoCourse.json": INFO_COURSE,
    "questions/q1/info.json": b'{"title": "Q1"}\n',
    "questions/q1/question.html": b"<pl-question-panel>1 + 1</pl-question-panel>\n",
}


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path) -> None:
    git_fixture.require_git()
    git_fixture.isolate_git_home(monkeypatch, tmp_path)


# --------------------------------------------------------------------------------------------------
# Apoio


def _runner() -> Any:
    from cq_client.stack_compose import GitRunner

    return GitRunner()


def _read_git(path: Path, revision: str, runner: Any = None) -> Any:
    from cq_client.banks import read_git

    return read_git(str(path), revision, runner if runner is not None else _runner())


def _git_refusal(path: Path, revision: str, runner: Any = None) -> Any:
    from cq_client.banks import BankReadError, read_git

    with pytest.raises(BankReadError) as info:
        read_git(str(path), revision, runner if runner is not None else _runner())
    return info.value


def _by_bytes(paths: Any) -> list[str]:
    return sorted(paths, key=lambda p: p.encode("utf-8"))


def _entries(bank: Any) -> dict[str, bytes]:
    """As entradas como `{caminho: conteúdo}`, conferindo a ordem dos bytes, o tamanho e o SHA-256 de cada uma."""
    import hashlib

    paths = [entry.path for entry in bank.entries]
    assert paths == _by_bytes(paths)
    for entry in bank.entries:
        assert (entry.size, entry.sha256) == (len(entry.data), hashlib.sha256(entry.data).hexdigest()), entry.path
    return {entry.path: entry.data for entry in bank.entries}


def _assert_reason(exc: Any, reason: str, relative: Any = ..., *, local: tuple[Path, ...] = ()) -> None:
    from cq_client.banks import LOCAL_REASONS

    assert exc.reason == reason, f"motivo {exc.reason!r}, esperado {reason!r}"
    assert exc.reason in LOCAL_REASONS
    if relative is not ...:
        assert exc.relative_path == relative, f"caminho relativo {exc.relative_path!r}"
    for path in local:
        for text in (str(exc), repr(exc), *(str(arg) for arg in exc.args)):
            assert str(path) not in text and Path(path).as_posix() not in text, "a recusa leva o caminho local"


def _remove(repo: Path, path: str) -> None:
    (repo / path).unlink()


def _index_commit(repo: Path, entries: list[tuple[str, bytes, bytes]], message: str) -> str:
    """Grava `entries` (modo, caminho em bytes crus, conteúdo) só no índice, por `update-index -z --index-info` com
    os registros na entrada padrão, e faz o commit do índice; devolve o hash. Cada conteúdo vira um objeto uma vez
    só. A proteção de nomes do NTFS do git fica desligada só nesse comando (ver o docstring do módulo), e cada
    caminho é conferido no índice antes do commit."""
    oids: dict[bytes, str] = {}
    records: list[bytes] = []
    for mode, path, data in entries:
        if data not in oids:
            oids[data] = git_fixture._bank_git(repo, "hash-object", "-w", "--stdin", input=data).decode("ascii").strip()
        records.append(f"{mode} blob {oids[data]}\t".encode("ascii") + path + b"\0")
    git_fixture._bank_git(repo, "-c", "core.protectNTFS=false", "update-index", "-z", "--index-info",
                          input=b"".join(records))
    listed = set(git_fixture._bank_git(repo, "ls-files", "-z").split(b"\0"))
    assert all(path in listed for _, path, _ in entries), "o git não gravou um caminho no índice"
    return git_fixture._commit_index(repo, message)


def _path_of_bytes(total: int) -> str:
    """Um caminho de `total` bytes em UTF-8 sob `questions/`, com componentes de 1 a 255 bytes feitos sobretudo de
    `ç` (2 bytes, 1 caractere): o limite é em bytes, não em caracteres."""
    parts, length = ["questions"], len("questions")
    while total - length > 256:
        parts.append("ç" * 127)
        length += 255
    rest = total - length - 1
    parts.append("ç" * (rest // 2) + "f" * (rest % 2))
    path = "/".join(parts)
    assert len(path.encode("utf-8")) == total
    assert all(1 <= len(part.encode("utf-8")) <= 255 for part in path.split("/"))
    return path


def _sentinel_scripts(folder: Path, sentinel: Path) -> None:
    """Grava em `folder` um `git` que, se rodar, cria `sentinel`: `git.bat` e `git.cmd` no Windows (e um `git.exe`
    que não é executável), um `git` executável nos demais sistemas."""
    folder.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        script = f'@echo off\r\necho rodou > "{sentinel}"\r\n'.encode("utf-8")
        (folder / "git.bat").write_bytes(script)
        (folder / "git.cmd").write_bytes(script)
        (folder / "git.exe").write_bytes(b"nao e um executavel\n")
    else:
        target = folder / "git"
        target.write_bytes(f'#!/bin/sh\necho rodou > "{sentinel}"\n'.encode("utf-8"))
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------------------------------
# O dublê da API (o envio pelo CLI)


class FakeBanksApi:
    """A API do envio: registra os pedidos e responde "novo" na forma do contrato."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        sent = json.loads(request.content)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(sent["archive"])), mode="r:gz",
                          encoding="utf-8") as tar:
            files = {i.name: tar.extractfile(i).read() for i in tar.getmembers() if i.isreg()}
        body = {"name": sent["name"], "version": sent["version"], "repo": f"bank:{sent['name']}",
                "source": sent["source"], "git_commit": sent["git_commit"], "file_count": len(files),
                "byte_count": sum(map(len, files.values())),
                "question_count": bank_fixtures.reference_question_count(files), "created_at": CREATED_AT,
                "created": True}
        assert_matches("banks.upload", 200, body)
        return httpx2.Response(200, json=body)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler),
                             timeout=httpx2.Timeout(30.0, connect=5.0))


def _cq(args: list[str], fake: FakeBanksApi, tmp_path: Path) -> Any:
    return invoke_cli(args, obj={"http": fake.client()}, env={"CQ_API_URL": LOCAL_URL, "CQ_TOKEN": ADMIN_TOKEN},
                      tmp_path=tmp_path)


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _git_upload_lines(name: str, version: str, commit: str, files: dict[str, bytes]) -> str:
    return _out(
        f"Banco enviado: {name}, versão {version} (nova).",
        f"Origem: git, commit {commit}.",
        f"Arquivos: {len(files)}; bytes: {sum(map(len, files.values()))}; "
        f"questões: {bank_fixtures.reference_question_count(files)}.",
        f"Para disparar: cq dispatches prepare <oferecimento> --repo bank:{name} --commit {version} --qid <qid>",
    )


def _assert_cli_refusal(result: Any, fake: FakeBanksApi, reason: str) -> None:
    from cq_client.banks import LOCAL_MESSAGES

    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert (result.exit_code, result.stdout, result.stderr) == (2, "", _out(f"Uso incorreto: {LOCAL_MESSAGES[reason]}"))
    assert fake.requests == [], "a recusa local fez pedido à API"


# --------------------------------------------------------------------------------------------------
# A revisão e a árvore do commit


def test_revisions_resolve_to_the_commit_tree(tmp_path) -> None:
    """`HEAD`, o ramo, a etiqueta leve e a anotada, o hash completo e o curto e `HEAD~1` resolvem para o hash do
    commit (nunca o da etiqueta anotada), com a árvore dele. Vermelho no esqueleto: `read_git` lança
    `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    first = git_fixture.rev_parse(repo, "HEAD")
    git_fixture.create_branch(repo, "ramo-antigo")
    git_fixture.create_tag(repo, "v1")
    git_fixture.create_tag(repo, "v1-anotada", annotated=True)
    second_files = {**BASE_FILES, "questions/q1/question.html": b"<pl-question-panel>2 + 2</pl-question-panel>\n"}
    second = git_fixture.commit_files(repo, second_files, "segundo")
    assert first != second and len(first) == len(second) == 40
    cases = {"HEAD": (second, second_files), "main": (second, second_files), second: (second, second_files),
             "ramo-antigo": (first, BASE_FILES), "v1": (first, BASE_FILES), "v1-anotada": (first, BASE_FILES),
             first[:12]: (first, BASE_FILES), "HEAD~1": (first, BASE_FILES), "main^": (first, BASE_FILES)}
    for revision, (commit, files) in cases.items():
        bank = _read_git(repo, revision)
        assert bank.git_commit == commit, revision
        assert _entries(bank) == files, revision


def test_sha256_repository_gives_a_64_hex_commit(tmp_path) -> None:
    """Num repositório com objetos SHA-256, o commit sai com 64 hexadecimais. Vermelho no esqueleto: `read_git`
    lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES, object_format="sha256")
    commit = git_fixture.rev_parse(repo, "HEAD")
    assert len(commit) == 64
    bank = _read_git(repo, "HEAD")
    assert bank.git_commit == commit
    assert _entries(bank) == BASE_FILES


def test_dirty_worktree_stays_out_of_the_package(tmp_path) -> None:
    """Mudanças não commitadas (um arquivo alterado, um novo e um apagado, inclusive no índice) ficam fora: as
    entradas são as do commit. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    commit = git_fixture.rev_parse(repo, "HEAD")
    (repo / "questions" / "q1" / "question.html").write_bytes(b"<p>mudanca nao commitada</p>\n")
    bank_fixtures.write_tree(repo, {"questions/q2/info.json": b'{"title": "nova"}\n'})
    _remove(repo, "questions/q1/info.json")
    bank = _read_git(repo, "HEAD")
    assert bank.git_commit == commit
    assert _entries(bank) == BASE_FILES


def test_subfolder_reads_its_tree_in_the_commit(tmp_path) -> None:
    """A subpasta dá a árvore dela no commit, com os caminhos relativos a ela (o prefixo vem com a barra final),
    também com a etiqueta como revisão; a pasta acima dela, sem `infoCourse.json`, → `not_a_course`. Vermelho no
    esqueleto: `read_git` lança `NotImplementedError`."""
    inner = {f"sub/banco/{path}": data for path, data in BASE_FILES.items()}
    repo = git_fixture.make_bank_repo(tmp_path, {"README.md": b"raiz\n", "sub/LEIA.txt": b"fora\n", **inner})
    git_fixture.create_tag(repo, "v1")
    for revision in ("HEAD", "v1"):
        bank = _read_git(repo / "sub" / "banco", revision)
        assert bank.git_commit == git_fixture.rev_parse(repo, "HEAD")
        assert _entries(bank) == BASE_FILES
    exc = _git_refusal(repo / "sub", "HEAD")
    _assert_reason(exc, "not_a_course", local=(repo,))


def test_subfolder_missing_or_file_in_the_revision_is_refused(tmp_path) -> None:
    """A subpasta que não existe no commit (só na pasta de trabalho) e a que é arquivo no commit pedido →
    `not_in_revision`; a mesma subpasta no commit em que ela é pasta é lida. Vermelho no esqueleto: `read_git`
    lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, {**BASE_FILES, "x": b"era um arquivo\n"})
    old = git_fixture.rev_parse(repo, "HEAD")
    _remove(repo, "x")
    inner = {f"x/{path}": data for path, data in BASE_FILES.items()}
    new = git_fixture.commit_files(repo, inner, "x vira pasta")
    bank_fixtures.write_tree(repo / "novo", BASE_FILES)
    _assert_reason(_git_refusal(repo / "x", old), "not_in_revision", local=(repo,))
    _assert_reason(_git_refusal(repo / "novo", "HEAD"), "not_in_revision", local=(repo,))
    bank = _read_git(repo / "x", "HEAD")
    assert (bank.git_commit, _entries(bank)) == (new, BASE_FILES)


def test_commit_bytes_are_read_without_filters_or_replacements(tmp_path) -> None:
    """Os bytes são os dos objetos do commit: com `* text eol=crlf` no `.gitattributes` (a pasta de trabalho com
    CRLF), o conteúdo continua com LF; o filtro e o `textconv` sentinela do repositório nunca rodam; o
    `refs/replace` de um objeto não troca o conteúdo. Vermelho no esqueleto: `read_git` lança
    `NotImplementedError`."""
    attributes = b"* text eol=crlf\n" + git_fixture.SENTINEL_FILTER_ATTRIBUTES.encode("ascii")
    files = {**BASE_FILES, ".gitattributes": attributes}
    repo = git_fixture.make_bank_repo(tmp_path, files)
    git_fixture.refresh_worktree(repo)
    assert (repo / "questions" / "q1" / "info.json").read_bytes() == b'{"title": "Q1"}\r\n'
    sentinel = tmp_path / "filtro-rodou"
    git_fixture.install_sentinel_filter(repo, sentinel)
    original = git_fixture.blob_oid(repo, "HEAD", "questions/q1/question.html")
    git_fixture.add_replace(repo, original, b"<p>substituto</p>\n")
    assert not sentinel.exists()
    bank = _read_git(repo, "HEAD")
    assert _entries(bank) == files
    assert not sentinel.exists(), "um filtro ou textconv do repositório rodou na leitura"


def test_inherited_git_environment_is_ignored(tmp_path, monkeypatch) -> None:
    """Um `GIT_DIR`, um `GIT_WORK_TREE` e um `GIT_INDEX_FILE` herdados apontando para outro repositório não
    mudam o repositório lido. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    other = git_fixture.make_bank_repo(tmp_path, {"infoCourse.json": b'{"outro": true}\n'}, name="outro")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    bank = _read_git(repo, "HEAD")
    assert bank.git_commit == git_fixture.rev_parse(repo, "HEAD") != git_fixture.rev_parse(other, "HEAD")
    assert _entries(bank) == BASE_FILES


# --------------------------------------------------------------------------------------------------
# O executável do git


def test_sentinel_git_in_cwd_relative_path_and_bank_never_runs(tmp_path, monkeypatch) -> None:
    """Com o diretório atual na pasta do banco, um `git` sentinela nela, numa subpasta dela citada por uma
    entrada relativa do `PATH` e no diretório atual, o `git` real (de uma entrada absoluta) é o que roda, e o
    sentinela nunca. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    sentinel = tmp_path / "sentinela-rodou"
    _sentinel_scripts(repo, sentinel)
    _sentinel_scripts(repo / "ferramentas", sentinel)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "ferramentas", os.environ.get("PATH", "")]))
    bank = _read_git(repo, "HEAD")
    assert _entries(bank) == BASE_FILES
    assert not sentinel.exists(), "um git do diretório atual ou de uma entrada relativa do PATH rodou"


def test_git_only_inside_the_bank_or_absent_is_missing(tmp_path, monkeypatch) -> None:
    """O `git` achado só numa entrada absoluta do `PATH` dentro da pasta do banco é recusado como ausente
    (`git_missing`) e nunca roda; sem nenhum `git` no `PATH`, também `git_missing`; o executor dublê sem `git`
    dá o mesmo motivo. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    sentinel = tmp_path / "sentinela-rodou"
    _sentinel_scripts(repo / "ferramentas", sentinel)
    monkeypatch.setenv("PATH", str(repo / "ferramentas"))
    _assert_reason(_git_refusal(repo, "HEAD"), "git_missing", None, local=(repo,))
    assert not sentinel.exists(), "o git de dentro da pasta do banco rodou"
    empty = tmp_path / "sem-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    _assert_reason(_git_refusal(repo, "HEAD"), "git_missing", None, local=(repo,))
    missing = git_fixture.MissingGitRunner()
    _assert_reason(_git_refusal(repo, "HEAD", missing), "git_missing", None, local=(repo,))


# --------------------------------------------------------------------------------------------------
# As recusas


# Revisões aceitas e recusadas pela forma (o `cq`, o esquema do MCP e o padrão do cliente decidem igual).
ACCEPTED_FORMS = ["HEAD", "main", "v1", "v1-anotada", "HEAD~1", "HEAD^", "main@{0}", "refs/heads/main",
                  "ramo/com.barra_e-hifen", "a" * 256, "nao-existe"]
REFUSED_FORMS = ["-HEAD", "--output=arquivo", "HE AD", "HEAD:infoCourse.json", "", "a" * 257, "ação", "a\tb",
                 "x;y", "HEAD$", "*", "a\\b"]


def test_revision_form_is_checked_before_git_and_matches_the_mcp_schema(tmp_path) -> None:
    """Cada revisão fora da forma → `revision_invalid` sem nenhuma chamada ao executor; cada uma dentro da forma
    passa da conferência (é lida, ou → `revision_not_found` quando não existe); o veredito é o mesmo de
    `REVISION_PATTERN` com o teto de tamanho e do esquema do `git_rev` da ferramenta do MCP. Vermelho no
    esqueleto: `read_git` lança `NotImplementedError`."""
    from cq_client.banks import REVISION_MAX_LENGTH, REVISION_PATTERN

    schema = json.loads(MCP_UPLOAD_SCHEMA.read_text(encoding="utf-8"))["properties"]["git_rev"]
    validator = Draft202012Validator(schema)
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    git_fixture.commit_files(repo, {"extra.txt": b"x\n"}, "segundo")
    git_fixture.create_tag(repo, "v1")
    git_fixture.create_tag(repo, "v1-anotada", annotated=True)
    git_fixture.create_branch(repo, "ramo/com.barra_e-hifen")
    for revision in ACCEPTED_FORMS + REFUSED_FORMS:
        by_pattern = (REVISION_PATTERN.fullmatch(revision) is not None
                      and 1 <= len(revision) <= REVISION_MAX_LENGTH)
        assert validator.is_valid(revision) == by_pattern == (revision in ACCEPTED_FORMS), repr(revision)
        if revision in REFUSED_FORMS:
            recording = git_fixture.RecordingGitRunner()
            _assert_reason(_git_refusal(repo, revision, recording), "revision_invalid", None)
            assert recording.calls == [], f"o git foi chamado com a revisão recusada {revision!r}"
            continue
        if revision == "a" * 256 or revision == "nao-existe":
            _assert_reason(_git_refusal(repo, revision), "revision_not_found", None, local=(repo,))
        else:
            assert len(_read_git(repo, revision).git_commit) == 40, revision


def test_unknown_revision_and_folder_outside_a_repository(tmp_path) -> None:
    """A revisão que não existe (um nome ou um hash ausente) → `revision_not_found`; a pasta fora de repositório
    → `not_a_repository`. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    for revision in ("nao-existe", "deadbeef" * 5, "HEAD~5"):
        _assert_reason(_git_refusal(repo, revision), "revision_not_found", None, local=(repo,))
    loose = bank_fixtures.write_tree(tmp_path / "solta", BASE_FILES)
    _assert_reason(_git_refusal(loose, "HEAD"), "not_a_repository", None, local=(loose,))


def test_symlink_and_submodule_in_the_revision_are_refused(tmp_path) -> None:
    """O modo `120000` (link) → `git_symlink` e o `160000` (submódulo) → `git_submodule`, com o caminho relativo
    dentro do banco (também numa subpasta). Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    linked = git_fixture.make_bank_repo(tmp_path, BASE_FILES, name="com-link")
    git_fixture.add_symlink(linked, "questions/q1/atalho", "../../infoCourse.json")
    _assert_reason(_git_refusal(linked, "HEAD"), "git_symlink", "questions/q1/atalho", local=(linked,))
    nested = git_fixture.make_bank_repo(tmp_path, {f"sub/banco/{p}": d for p, d in BASE_FILES.items()},
                                        name="com-link-na-subpasta")
    git_fixture.add_symlink(nested, "sub/banco/questions/atalho", "q1")
    _assert_reason(_git_refusal(nested / "sub" / "banco", "HEAD"), "git_symlink", "questions/atalho",
                   local=(nested,))
    with_module = git_fixture.make_bank_repo(tmp_path, BASE_FILES, name="com-submodulo")
    commit = git_fixture.rev_parse(with_module, "HEAD")
    git_fixture.add_submodule(with_module, "vendor/externo", commit)
    _assert_reason(_git_refusal(with_module, "HEAD"), "git_submodule", "vendor/externo", local=(with_module,))


# --------------------------------------------------------------------------------------------------
# As regras de caminho e os tetos da pasta, no commit


# O banco na raiz do repositório e numa subpasta: o caminho relativo e os limites são os de dentro do banco.
BANK_PLACES = (("na-raiz", ""), ("na-subpasta", "sub/banco/"))


@pytest.mark.parametrize("raw", [
    pytest.param("questions/a\\b.txt".encode("utf-8"), id="barra-invertida"),
    pytest.param(b"questions/a\x1bb.txt", id="controle-esc"),
    pytest.param(b"questions/nome\xff.txt", id="byte-fora-de-utf8"),
    pytest.param(("questions/q1/" + "ç" * 128).encode("utf-8"), id="componente-de-256-bytes"),
    pytest.param(_path_of_bytes(4097).encode("utf-8"), id="caminho-de-4097-bytes"),
])
def test_path_rule_in_the_commit_is_refused_like_the_folder(tmp_path, raw) -> None:
    """Um caminho fora da regra no commit (só no índice): a barra invertida, o ESC, um byte fora de UTF-8, um
    componente de 256 bytes (128 caracteres de 2 bytes) e um caminho de 4097 bytes dentro do banco →
    `path_not_allowed`, a mesma recusa da pasta, com o caminho relativo dentro do banco, cru (o do nome fora de
    UTF-8 fica sem forma fixada, como na pasta), e sem o caminho local; o mesmo com o banco numa subpasta.
    Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    try:
        relative: Any = raw.decode("utf-8")
    except UnicodeDecodeError:
        relative = ...
    for name, prefix in BANK_PLACES:
        repo = git_fixture.make_bank_repo(tmp_path, {prefix + p: d for p, d in BASE_FILES.items()}, name=name)
        _index_commit(repo, [("100644", prefix.encode("ascii") + raw, b"x\n")], "fora da regra")
        exc = _git_refusal(repo / prefix, "HEAD")
        _assert_reason(exc, "path_not_allowed", relative, local=(repo,))
        if exc.relative_path is not None:
            assert str(repo) not in exc.relative_path and repo.as_posix() not in exc.relative_path, name


def test_paths_at_the_limits_are_read_from_the_commit(tmp_path) -> None:
    """No limite, o caminho entra: um componente de 255 bytes (127 caracteres de 2 bytes e um de 1) e um caminho de
    4096 bytes dentro do banco são lidos com o conteúdo; com o banco numa subpasta, o caminho no repositório passa
    de 4096 bytes, e o banco é lido do mesmo jeito (o limite é o do caminho dentro do banco). Vermelho no
    esqueleto: `read_git` lança `NotImplementedError`."""
    limits = {"questions/q1/" + "ç" * 127 + "a": b"componente no limite\n",
              _path_of_bytes(4096): b"caminho no limite\n"}
    for name, prefix in BANK_PLACES:
        repo = git_fixture.make_bank_repo(tmp_path, {prefix + p: d for p, d in BASE_FILES.items()}, name=name)
        commit = _index_commit(repo, [("100644", (prefix + p).encode("utf-8"), d) for p, d in limits.items()],
                               "no limite")
        bank = _read_git(repo / prefix, "HEAD")
        assert (bank.git_commit, _entries(bank)) == (commit, {**BASE_FILES, **limits}), name


def test_file_count_limit_in_the_commit(tmp_path) -> None:
    """O teto de arquivos da pasta vale no commit (20000, o da api): o commit com 20000 arquivos é lido; o seguinte,
    com 20001, → `too_many_files`. Vermelho no esqueleto: `read_git` lança `NotImplementedError`."""
    from cq_client import banks

    assert banks.MAX_FILES == bank_fixtures.REFERENCE_MAX_FILES
    repo = git_fixture.make_bank_repo(tmp_path, {"infoCourse.json": INFO_COURSE})
    many = [("100644", f"muitos/f{index:06d}.txt".encode("ascii"), b"")
            for index in range(bank_fixtures.REFERENCE_MAX_FILES - 1)]
    at_limit = _index_commit(repo, many, "no teto")
    _index_commit(repo, [("100644", b"um-a-mais.txt", b"")], "um a mais")
    assert len(_read_git(repo, at_limit).entries) == bank_fixtures.REFERENCE_MAX_FILES
    _assert_reason(_git_refusal(repo, "HEAD"), "too_many_files", local=(repo,))


def test_content_size_limit_in_the_commit(tmp_path) -> None:
    """O teto de conteúdo da pasta vale no commit (100 MiB, a soma dos tamanhos): o commit no teto é lido; o
    seguinte, com um byte a mais no mesmo arquivo, → `too_large`. Vermelho no esqueleto: `read_git` lança
    `NotImplementedError`."""
    from cq_client import banks

    assert banks.MAX_CONTENT_BYTES == bank_fixtures.REFERENCE_MAX_CONTENT_BYTES
    repo = git_fixture.make_bank_repo(tmp_path, {"infoCourse.json": INFO_COURSE})
    big = repo / "questions" / "grande.bin"
    big.parent.mkdir(parents=True)
    with open(big, "wb") as handle:
        handle.truncate(bank_fixtures.REFERENCE_MAX_CONTENT_BYTES - len(INFO_COURSE))
    at_limit = git_fixture.commit_files(repo, {}, "no teto")
    with open(big, "ab") as handle:
        handle.write(b"\0")
    git_fixture.commit_files(repo, {}, "um byte acima")
    bank = _read_git(repo, at_limit)
    assert sum(entry.size for entry in bank.entries) == bank_fixtures.REFERENCE_MAX_CONTENT_BYTES
    del bank
    _assert_reason(_git_refusal(repo, "HEAD"), "too_large", local=(repo,))


def test_folder_and_commit_of_the_same_content_match(tmp_path) -> None:
    """O curso de fixture commitado: a pasta de trabalho lida como pasta (o `.git` fica fora) e o commit lido
    pelo git dão as mesmas entradas, a versão de referência e o mesmo pacote; só o commit de origem difere.
    Vermelho no esqueleto: `read_folder`, `read_git` e `build_archive` lançam `NotImplementedError`."""
    from cq_client.banks import bank_version, build_archive, read_folder

    files = bank_fixtures.course_files()
    repo = git_fixture.make_bank_repo(tmp_path, files)
    from_folder = read_folder(str(repo))
    from_git = _read_git(repo, "HEAD")
    assert from_folder.entries == from_git.entries
    assert _entries(from_git) == files
    assert bank_version(from_folder.entries) == bank_version(from_git.entries) == bank_fixtures.reference_version(files)
    assert build_archive(from_folder.entries) == build_archive(from_git.entries)
    assert (from_folder.git_commit, from_git.git_commit) == (None, git_fixture.rev_parse(repo, "HEAD"))


def test_executable_mode_in_the_commit_stays_out_like_in_the_folder(tmp_path) -> None:
    """Os `server.py` do curso de fixture com o modo `100755` no commit (o bit de execução; fora do Windows, também
    na pasta de trabalho): o commit é aceito e dá as mesmas entradas que a pasta do mesmo conteúdo, a versão de
    referência (que não leva o modo) e o mesmo pacote, com todo membro no modo `0644`. Vermelho no esqueleto:
    `read_folder`, `read_git` e `build_archive` lançam `NotImplementedError`."""
    from cq_client.banks import bank_version, build_archive, read_folder

    files = bank_fixtures.course_files()
    executable = sorted(path for path in files if path.endswith("/server.py"))
    assert executable, "o curso de fixture não tem server.py"
    repo = git_fixture.make_bank_repo(tmp_path, files)
    for path in executable:
        if not WINDOWS:
            target = repo / path
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        git_fixture._bank_git(repo, "update-index", "--chmod=+x", "--", path)
    commit = git_fixture._commit_index(repo, "modo executavel")
    listed = [line for line in git_fixture._bank_git(repo, "ls-tree", "-r", "-z", commit).split(b"\0") if line]
    modes = {line.partition(b"\t")[2].decode("utf-8"): line.split(b" ", 1)[0].decode("ascii") for line in listed}
    assert sorted(path for path, mode in modes.items() if mode == "100755") == executable
    from_folder = read_folder(str(repo))
    from_git = _read_git(repo, "HEAD")
    assert from_git.git_commit == commit
    assert from_folder.entries == from_git.entries
    assert _entries(from_git) == files
    assert bank_version(from_git.entries) == bank_version(from_folder.entries) == bank_fixtures.reference_version(files)
    archive = build_archive(from_git.entries)
    assert build_archive(from_folder.entries) == archive
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz", encoding="utf-8") as tar:
        assert {info.name: info.mode for info in tar.getmembers()} == dict.fromkeys(files, 0o644)


# --------------------------------------------------------------------------------------------------
# O CLI


def test_cli_upload_git_sends_and_prints_the_git_origin(tmp_path, monkeypatch) -> None:
    """`cq banks upload <pasta> --git HEAD` envia o conteúdo do commit (a pasta de trabalho suja não entra), a
    origem `git` e o hash, e imprime a origem com o hash; a partir da própria pasta (`cq banks upload .`), com
    sentinelas do `git` nela, o mesmo envio, e o sentinela nunca roda. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    files = bank_fixtures.course_files()
    repo = git_fixture.make_bank_repo(tmp_path, files)
    commit = git_fixture.rev_parse(repo, "HEAD")
    version = bank_fixtures.reference_version(files)
    (repo / "infoCourse.json").write_bytes(b'{"mudanca": "nao commitada"}\n')
    sentinel = tmp_path / "sentinela-rodou"
    _sentinel_scripts(repo, sentinel)
    fake = FakeBanksApi()
    result = _cq(["banks", "upload", str(repo), "--name", BANK_NAME, "--git", "HEAD"], fake, tmp_path)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert (result.exit_code, result.stderr) == (0, ""), result.stderr
    assert result.stdout == _git_upload_lines(BANK_NAME, version, commit, files)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join([".", os.environ.get("PATH", "")]))
    again = _cq(["banks", "upload", ".", "--name", BANK_NAME, "--git", "main"], fake, tmp_path)
    assert (again.exit_code, again.stderr, again.stdout) == (0, "", result.stdout)
    assert not sentinel.exists(), "um git da pasta do banco rodou"
    bodies = [json.loads(request.content) for request in fake.requests]
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert {k: v for k, v in bodies[0].items() if k != "archive"} == {
        "name": BANK_NAME, "version": version, "source": "git", "git_commit": commit}


def test_cli_git_refusals_never_reach_the_api(tmp_path, monkeypatch) -> None:
    """Pelo `cq`, a revisão fora da forma, a que não existe, a pasta fora de repositório e o `git` ausente saem
    com `Uso incorreto: <mensagem fixa>`, o código 2 e nenhum pedido à API. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    repo = git_fixture.make_bank_repo(tmp_path, BASE_FILES)
    loose = bank_fixtures.write_tree(tmp_path / "solta", BASE_FILES)
    fake = FakeBanksApi()
    for revision in REFUSED_FORMS:
        result = _cq(["banks", "upload", str(repo), "--name", BANK_NAME, f"--git={revision}"], fake, tmp_path)
        _assert_cli_refusal(result, fake, "revision_invalid")
    _assert_cli_refusal(_cq(["banks", "upload", str(repo), "--name", BANK_NAME, "--git", "nao-existe"], fake,
                            tmp_path), fake, "revision_not_found")
    _assert_cli_refusal(_cq(["banks", "upload", str(loose), "--name", BANK_NAME, "--git", "HEAD"], fake, tmp_path),
                        fake, "not_a_repository")
    empty = tmp_path / "sem-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("git") is None
    _assert_cli_refusal(_cq(["banks", "upload", str(repo), "--name", BANK_NAME, "--git", "HEAD"], fake, tmp_path),
                        fake, "git_missing")
