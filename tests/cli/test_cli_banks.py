"""CLI `cq banks upload` e `cq banks list` (o envio e a consulta dos bancos de questões locais).

O CLI roda em processo pelo `invoke_cli` (`tests/helpers/cli_runner.py`, ambiente limpo), com o cliente HTTP de
um dublê da API em `obj["http"]` (`httpx2.MockTransport`): o dublê registra cada pedido e responde na forma do
contrato (cada resposta conferida pelo esquema congelado). O envio idempotente fica no dublê: o primeiro envio
de um nome e uma versão é "novo" (`created: true`); o seguinte devolve o primeiro, com a origem e o commit dele
(`created: false`). O banco enviado é a cópia do curso de fixture numa pasta com nome único.

Saídas fixadas aqui (com as convenções dos demais comandos):
- `banks upload`, em quatro linhas:
  `Banco enviado: <name>, versão <version> (nova).` (ou `(já existia).`),
  `Origem: folder.` (ou `Origem: git, commit <git_commit>.`),
  `Arquivos: <file_count>; bytes: <byte_count>; questões: <question_count>.` e
  `Para disparar: cq dispatches prepare <oferecimento> --repo <repo> --commit <version> --qid <qid>`, com os
  marcadores `<oferecimento>` e `<qid>` literais e o `repo` **da resposta** (que sai redigido quando o nome tem
  forma de identidade), nunca o nome digitado; tudo pelos valores da resposta, com o `name` e o `repo` (texto
  livre) passados pelo `_terminal_safe`; com `--json`, o corpo da API;
- as recusas locais da leitura da pasta: no stderr, `Uso incorreto: <mensagem fixa do motivo>` e, quando houver,
  `Caminho no banco: <caminho relativo neutralizado>`; o stdout vazio, o código 2 e nenhum pedido à API; um
  nome com controle (o ESC fora do Windows, o CSI de 8 bits e o DEL no Windows, que não aceita os de C0 em
  nomes) sai como os escapes visíveis do `repr`, sem nenhum controle cru; a recusa de um caminho na leitura de um
  commit (`--git`) sai na mesma forma, com o nome com ESC nas duas plataformas (gravado só no índice de um
  repositório sintético no `tmp_path`, pelo `git` real e por `tests/helpers/git_fixture.py`);
- o erro da API no envio: `Erro: <message>` e uma linha por campo, código 1;
- o perfil remoto (`kind = "remote"`) envia o mesmo corpo pela mesma rota, no endereço dele;
- `banks list`: `<name>  <version>  <repo>  <source>  <git_commit ou ->  <file_count>  <byte_count>
  <question_count>  <created_at>` por versão (o `-` no commit nulo, como o `_dash` do CLI; o `name` e o `repo`
  pelo `_terminal_safe`) e, por último, `Próximo cursor: <next_cursor>`; a consulta leva só as opções dadas
  (`--name`, `--after`, `--limit`); com `--json`, o corpo da API.

No esqueleto, as duas folhas lançam `NotImplementedError`, que o grupo raiz converte em `Erro: falha inesperada no
CLI (NotImplementedError).` com o código 1: todo caso daqui fica vermelho por isso até a implementação do envio
(sem `--git`, menos o caso da recusa de um commit, que espera também a do `--git`) e da listagem pelo CLI.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from tests.helpers import banks as bank_fixtures
from tests.helpers import git_fixture
from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.cli_runner import invoke_cli, write_profile
from tests.helpers.contract import MESSAGES, assert_matches

WINDOWS = os.name == "nt"
LOCAL_URL = "http://127.0.0.1:8123"
VM_URL = "https://vm.cq.test"
BANKS_PATH = "/v1/question-banks"
BANK_NAME = "banco-sintetico"
CREATED_AT = "2031-01-01T00:00:00Z"
USAGE_EXIT = 2
# Controles crus que nunca podem sair no texto do CLI (a quebra de linha só como fim de linha).
RAW_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------------------------------
# O dublê da API


class FakeBanksApi:
    """A API dos bancos: o envio idempotente por nome e versão e a listagem; `respond` troca a resposta."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response | None] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.stored: dict[tuple[str, str], dict] = {}
        self.page: dict = {"items": [], "next_cursor": 0}
        self._respond = respond

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        if self._respond is not None:
            response = self._respond(request)
            if response is not None:
                return response
        if (request.method, request.url.path) == ("POST", BANKS_PATH):
            return httpx2.Response(200, json=self._upload(json.loads(request.content)))
        if (request.method, request.url.path) == ("GET", BANKS_PATH):
            assert_matches("banks.list", 200, self.page)
            return httpx2.Response(200, json=self.page)
        return httpx2.Response(404, json={"error": {"code": "not_found", "message": MESSAGES["not_found"]}})

    def _upload(self, sent: dict) -> dict:
        key = (sent["name"], sent["version"])
        if key in self.stored:
            body = {**self.stored[key], "created": False}
        else:
            files = _archive_files(base64.b64decode(sent["archive"], validate=True))
            body = {"name": sent["name"], "version": sent["version"], "repo": f"bank:{sent['name']}",
                    "source": sent["source"], "git_commit": sent["git_commit"], "file_count": len(files),
                    "byte_count": sum(len(data) for data in files.values()),
                    "question_count": bank_fixtures.reference_question_count(files), "created_at": CREATED_AT,
                    "created": True}
            self.stored[key] = body
        assert_matches("banks.upload", 200, body)
        return body

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler),
                             timeout=httpx2.Timeout(30.0, connect=5.0))


def _archive_files(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz", encoding="utf-8") as tar:
        return {info.name: tar.extractfile(info).read() for info in tar.getmembers() if info.isreg()}


# --------------------------------------------------------------------------------------------------
# Apoio


def _env() -> dict[str, str]:
    return {"CQ_API_URL": LOCAL_URL, "CQ_TOKEN": ADMIN_TOKEN}


def _cq(args: list[str], fake: FakeBanksApi, tmp_path: Path, *, json: bool = False, **kwargs: Any) -> Any:
    kwargs.setdefault("env", _env())
    return invoke_cli(args, json=json, obj={"http": fake.client()}, tmp_path=tmp_path, **kwargs)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _safe(text: str) -> str:
    """O `_terminal_safe` esperado: controles C0/C1 e quebras de linha como escapes do `repr`."""
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return "".join(escapes.get(ch) or (f"\\x{ord(ch):02x}" if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F else ch)
                   for ch in text)


def _upload_lines(body: dict) -> str:
    """As quatro linhas de `banks upload` para o corpo da resposta."""
    state = "nova" if body["created"] else "já existia"
    origin = "Origem: folder." if body["source"] == "folder" else f"Origem: git, commit {body['git_commit']}."
    return _out(
        f"Banco enviado: {_safe(body['name'])}, versão {body['version']} ({state}).",
        origin,
        f"Arquivos: {body['file_count']}; bytes: {body['byte_count']}; questões: {body['question_count']}.",
        f"Para disparar: cq dispatches prepare <oferecimento> --repo {_safe(body['repo'])} "
        f"--commit {body['version']} --qid <qid>",
    )


def _course(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    root = bank_fixtures.copy_course(tmp_path / f"curso-{bank_fixtures.unique_marker()}")
    return root, bank_fixtures.course_files()


def _local_texts(root: Path, tmp_path: Path) -> list[str]:
    return [str(root), root.as_posix(), root.name, str(tmp_path), tmp_path.as_posix()]


def _assert_no_local_path(requests: list[httpx2.Request], root: Path, tmp_path: Path) -> None:
    """Nenhum pedido leva o caminho local nem o nome da pasta: no endereço, nos cabeçalhos, no corpo nem no
    pacote descomprimido."""
    for request in requests:
        blobs = [str(request.url).encode("utf-8"),
                 *(f"{k}: {v}".encode("utf-8") for k, v in request.headers.items()), request.content]
        if request.content:
            archive = base64.b64decode(json.loads(request.content)["archive"], validate=True)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                blobs.append(b"".join(tar.extractfile(i).read() for i in tar.getmembers() if i.isreg()))
                blobs.append("\n".join(tar.getnames()).encode("utf-8"))
            blobs.append(archive)
        for local in _local_texts(root, tmp_path):
            needles = {local.encode("utf-8"), json.dumps(local)[1:-1].encode("utf-8")}
            for blob in blobs:
                assert not any(needle in blob for needle in needles), "o pedido leva o caminho local da pasta"


def _assert_local_refusal(result: Any, fake: FakeBanksApi, reason: str, relative: str | None) -> None:
    """A recusa local: a mensagem fixa e o caminho relativo neutralizado no stderr, o stdout vazio, o código 2
    e nenhum pedido à API."""
    from cq_client.banks import LOCAL_MESSAGES

    _no_traceback(result)
    expected = [f"Uso incorreto: {LOCAL_MESSAGES[reason]}"]
    if relative is not None:
        expected.append(f"Caminho no banco: {_safe(relative)}")
    assert (result.exit_code, result.stdout, result.stderr) == (USAGE_EXIT, "", _out(*expected))
    assert RAW_CONTROL.search(result.stderr.rstrip("\n").replace("\n", "")) is None
    assert fake.requests == [], "a recusa local fez pedido à API"


# --------------------------------------------------------------------------------------------------
# cq banks upload


def test_upload_folder_prints_new_then_already_existed(tmp_path) -> None:
    """O curso de fixture enviado duas vezes: a primeira saída diz "nova", a segunda "já existia", com a mesma
    versão (a de referência), as contagens da resposta, a origem `folder` e a linha pronta do disparo com o `repo`
    da resposta; os dois pedidos são `POST /v1/question-banks` com o mesmo corpo (o pacote tem exatamente os
    arquivos do curso) e nenhum leva o caminho local. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    root, files = _course(tmp_path)
    fake = FakeBanksApi()
    first = _cq(["banks", "upload", str(root), "--name", BANK_NAME], fake, tmp_path)
    second = _cq(["banks", "upload", str(root), "--name", BANK_NAME], fake, tmp_path)
    _ok(first)
    _ok(second)
    version = bank_fixtures.reference_version(files)
    stored = fake.stored[(BANK_NAME, version)]
    assert first.stdout == _upload_lines({**stored, "created": True})
    assert second.stdout == _upload_lines({**stored, "created": False})
    assert (stored["repo"], stored["source"], stored["git_commit"]) == (f"bank:{BANK_NAME}", "folder", None)
    assert [(r.method, str(r.url)) for r in fake.requests] == [("POST", LOCAL_URL + BANKS_PATH)] * 2
    bodies = [json.loads(r.content) for r in fake.requests]
    assert bodies[0] == bodies[1]
    assert {k: v for k, v in bodies[0].items() if k != "archive"} == {
        "name": BANK_NAME, "version": version, "source": "folder", "git_commit": None}
    assert _archive_files(base64.b64decode(bodies[0]["archive"])) == files
    assert all(r.headers["X-CQ-Client"] == "cli" for r in fake.requests)
    assert all(dict(r.extensions["timeout"])["read"] == 300 for r in fake.requests)
    _assert_no_local_path(fake.requests, root, tmp_path)


def test_upload_json_prints_the_api_body(tmp_path) -> None:
    """Com `--json`, o stdout é o corpo da resposta da API, intacto. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    root, files = _course(tmp_path)
    fake = FakeBanksApi()
    result = _cq(["banks", "upload", str(root), "--name", BANK_NAME], fake, tmp_path, json=True)
    _ok(result)
    assert json.loads(result.stdout) == fake.stored[(BANK_NAME, bank_fixtures.reference_version(files))]


def test_upload_ready_line_uses_the_repo_from_the_response(tmp_path) -> None:
    """A linha pronta e o nome saem da resposta, não do nome digitado: com a resposta redigida (o nome com forma
    de identidade), a saída mostra a marca da redação; com controles no nome e no `repo` da resposta, eles saem
    neutralizados. Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    root, files = _course(tmp_path)
    typed = "nome-com-forma-de-pessoa"
    for shown_name in ("[pessoa 1a2b]", "nome\x1b[31m\x9bfim\n"):
        def respond(request: httpx2.Request, _name: str = shown_name) -> httpx2.Response:
            sent = json.loads(request.content)
            body = {"name": _name, "version": sent["version"], "repo": f"bank:{_name}", "source": "folder",
                    "git_commit": None, "file_count": 2, "byte_count": 10, "question_count": 1,
                    "created_at": CREATED_AT, "created": True}
            assert_matches("banks.upload", 200, body)
            return httpx2.Response(200, json=body)

        fake = FakeBanksApi(respond)
        result = _cq(["banks", "upload", str(root), "--name", typed], fake, tmp_path)
        _ok(result)
        version = bank_fixtures.reference_version(files)
        assert result.stdout == _upload_lines({
            "name": shown_name, "version": version, "repo": f"bank:{shown_name}", "source": "folder",
            "git_commit": None, "file_count": 2, "byte_count": 10, "question_count": 1, "created": True})
        assert typed not in result.stdout
        assert RAW_CONTROL.search(result.stdout.replace("\n", "")) is None


def test_upload_already_existing_shows_the_first_origin(tmp_path) -> None:
    """O reenvio de um conteúdo enviado antes de outra origem mostra a origem e o commit do primeiro envio (os da
    resposta) e "já existia". Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    root, files = _course(tmp_path)
    version = bank_fixtures.reference_version(files)
    commit = "ab" * 20
    fake = FakeBanksApi()
    fake.stored[(BANK_NAME, version)] = {
        "name": BANK_NAME, "version": version, "repo": f"bank:{BANK_NAME}", "source": "git", "git_commit": commit,
        "file_count": len(files), "byte_count": sum(map(len, files.values())),
        "question_count": bank_fixtures.reference_question_count(files), "created_at": CREATED_AT, "created": True}
    result = _cq(["banks", "upload", str(root), "--name", BANK_NAME], fake, tmp_path)
    _ok(result)
    assert result.stdout == _upload_lines({**fake.stored[(BANK_NAME, version)], "created": False})
    assert f"Origem: git, commit {commit}." in result.stdout
    assert json.loads(fake.requests[0].content)["source"] == "folder"


def test_upload_api_error_follows_the_cli_convention(tmp_path) -> None:
    """A recusa da API no envio sai como os outros erros da API: `Erro: <message>` e a linha do campo, código 1,
    o stdout vazio. Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    root, _ = _course(tmp_path)
    error = {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                       "fields": [{"field": "archive", "reason": "not_a_course"}]}}
    fake = FakeBanksApi(lambda request: httpx2.Response(400, json=error))
    result = _cq(["banks", "upload", str(root), "--name", BANK_NAME], fake, tmp_path)
    _no_traceback(result)
    assert (result.exit_code, result.stdout) == (1, "")
    assert result.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - archive: not_a_course")
    assert len(fake.requests) == 1


def test_upload_local_refusals_never_reach_the_api(tmp_path) -> None:
    """As recusas da leitura da pasta saem com a mensagem fixa, o caminho relativo dentro do banco (quando há) e
    o código 2, sem pedido à API: a pasta inexistente, um arquivo no lugar da pasta, a pasta sem
    `infoCourse.json` e um arquivo com controle no nome (neutralizado). Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    root, _ = _course(tmp_path)
    fake = FakeBanksApi()
    missing = _cq(["banks", "upload", str(root / "nao-existe"), "--name", BANK_NAME], fake, tmp_path)
    _assert_local_refusal(missing, fake, "not_found", None)
    not_dir = _cq(["banks", "upload", str(root / "infoCourse.json"), "--name", BANK_NAME], fake, tmp_path)
    _assert_local_refusal(not_dir, fake, "not_a_directory", None)
    no_course = bank_fixtures.write_tree(tmp_path / "sem-curso", {"questions/q1/info.json": b"{}\n"})
    refused = _cq(["banks", "upload", str(no_course), "--name", BANK_NAME], fake, tmp_path)
    _assert_local_refusal(refused, fake, "not_a_course", None)
    names = ["a\x9bb.txt", "c\x7fd.txt"] if WINDOWS else ["a\x1bb.txt", "c\x1b[31md.txt"]
    for name in names:
        bad = bank_fixtures.copy_course(tmp_path / f"controle-{bank_fixtures.unique_marker()}")
        (bad / "questions" / name).write_bytes(b"x\n")
        result = _cq(["banks", "upload", str(bad), "--name", BANK_NAME], fake, tmp_path)
        _assert_local_refusal(result, fake, "path_not_allowed", f"questions/{name}")
        for local in _local_texts(bad, tmp_path):
            assert local not in result.stderr


def _commit_index_only(repo: Path, path: bytes, data: bytes) -> str:
    """Um arquivo `path` (bytes crus) só no índice do repositório de fixture, por `update-index -z --index-info` com
    o registro na entrada padrão, e o commit do índice; devolve o hash. A proteção de nomes do NTFS do git fica
    desligada só nesse comando: com ela, o Git for Windows ignora em silêncio um nome com controle. O caminho é
    conferido no índice antes do commit."""
    oid = git_fixture._bank_git(repo, "hash-object", "-w", "--stdin", input=data).decode("ascii").strip()
    git_fixture._bank_git(repo, "-c", "core.protectNTFS=false", "update-index", "-z", "--index-info",
                          input=f"100644 blob {oid}\t".encode("ascii") + path + b"\0")
    assert path in git_fixture._bank_git(repo, "ls-files", "-z").split(b"\0"), "o git não gravou o caminho no índice"
    return git_fixture._commit_index(repo, "caminho fora da regra")


def test_upload_git_path_refusal_has_the_local_refusal_form(tmp_path, monkeypatch) -> None:
    """Pelo `--git`, o commit com um nome com controle (o ESC de uma sequência de cor, só no índice, nas duas
    plataformas) sai na mesma forma da recusa local da pasta: a mensagem fixa e o caminho relativo neutralizado no
    stderr, o código 2, nenhum pedido à API e nenhum caminho local. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    git_fixture.require_git()
    git_fixture.isolate_git_home(monkeypatch, tmp_path)
    repo = git_fixture.make_bank_repo(tmp_path, bank_fixtures.course_files(),
                                      name=f"git-{bank_fixtures.unique_marker()}")
    name = "c\x1b[31md.txt"
    _commit_index_only(repo, f"questions/{name}".encode("utf-8"), b"x\n")
    fake = FakeBanksApi()
    result = _cq(["banks", "upload", str(repo), "--name", BANK_NAME, "--git", "HEAD"], fake, tmp_path)
    _assert_local_refusal(result, fake, "path_not_allowed", f"questions/{name}")
    for local in _local_texts(repo, tmp_path):
        assert local not in result.stderr


def test_upload_with_remote_profile_uses_the_same_route(tmp_path) -> None:
    """Com o perfil remoto, o envio vai pela mesma rota, com o mesmo corpo do perfil local, no endereço do
    perfil, e a saída é a mesma. Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    root, _ = _course(tmp_path)
    config = write_profile(tmp_path, f"""\
default_profile = "local"

[profiles.local]
kind = "local"
api_url = "{LOCAL_URL}"
token = "{ADMIN_TOKEN}"

[profiles.vm]
kind = "remote"
api_url = "{VM_URL}"
token = "{ADMIN_TOKEN}"
""")
    local_fake, remote_fake = FakeBanksApi(), FakeBanksApi()
    local = invoke_cli(["banks", "upload", str(root), "--name", BANK_NAME], obj={"http": local_fake.client()},
                       config=config)
    remote = invoke_cli(["--profile", "vm", "banks", "upload", str(root), "--name", BANK_NAME],
                        obj={"http": remote_fake.client()}, config=config)
    _ok(local)
    _ok(remote)
    assert local.stdout == remote.stdout
    [local_request], [remote_request] = local_fake.requests, remote_fake.requests
    assert (local_request.method, str(local_request.url)) == ("POST", LOCAL_URL + BANKS_PATH)
    assert (remote_request.method, str(remote_request.url)) == ("POST", VM_URL + BANKS_PATH)
    assert json.loads(local_request.content) == json.loads(remote_request.content)
    assert remote_request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"


# --------------------------------------------------------------------------------------------------
# cq banks list


def _item(name: str, version: str, *, source: str = "folder", git_commit: str | None = None,
          counts: tuple[int, int, int] = (3, 120, 1), created_at: str = CREATED_AT) -> dict:
    return {"name": name, "version": version, "repo": f"bank:{name}", "source": source, "git_commit": git_commit,
            "file_count": counts[0], "byte_count": counts[1], "question_count": counts[2], "created_at": created_at}


def _list_line(item: dict) -> str:
    commit = "-" if item["git_commit"] is None else item["git_commit"]
    return (f"{_safe(item['name'])}  {item['version']}  {_safe(item['repo'])}  {item['source']}  {commit}  "
            f"{item['file_count']}  {item['byte_count']}  {item['question_count']}  {item['created_at']}")


def test_list_prints_one_line_per_version_and_the_cursor(tmp_path) -> None:
    """Uma linha por versão, na ordem da resposta (o primeiro envio primeiro), com o commit git ou `-`, e o
    cursor por último; o nome com controle sai neutralizado; sem opções, a consulta não leva filtro nem limite.
    Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    fake = FakeBanksApi()
    fake.page = {"items": [
        _item(BANK_NAME, "1" * 64),
        _item("outro-banco", "2" * 64, source="git", git_commit="cd" * 32, counts=(40, 9000, 12),
              created_at="2031-01-02T03:04:05.123456Z"),
        _item(BANK_NAME, "3" * 64, source="git", git_commit="ef" * 20),
        _item("nome\x1b[2Jfim", "4" * 64),
    ], "next_cursor": 4}
    result = _cq(["banks", "list"], fake, tmp_path)
    _ok(result)
    assert result.stdout == _out(*(_list_line(item) for item in fake.page["items"]), "Próximo cursor: 4")
    assert RAW_CONTROL.search(result.stdout.replace("\n", "")) is None
    [request] = fake.requests
    assert (request.method, request.url.path, request.content) == ("GET", BANKS_PATH, b"")
    assert sorted(request.url.params.multi_items()) in ([], [("after", "0")])


def test_list_passes_name_after_and_limit(tmp_path) -> None:
    """`--name`, `--after` e `--limit` vão à consulta, cada um só quando dado; a página vazia imprime só o
    cursor. Vermelho no esqueleto: a folha lança `NotImplementedError`."""
    fake = FakeBanksApi()
    fake.page = {"items": [], "next_cursor": 0}
    runs = [
        (["--name", BANK_NAME, "--after", "3", "--limit", "2"],
         [[("after", "3"), ("limit", "2"), ("name", BANK_NAME)]]),
        (["--name", BANK_NAME], [[("name", BANK_NAME)], [("after", "0"), ("name", BANK_NAME)]]),
        (["--after", "7"], [[("after", "7")]]),
        (["--limit", "500"], [[("limit", "500")], [("after", "0"), ("limit", "500")]]),
    ]
    for options, accepted in runs:
        result = _cq(["banks", "list", *options], fake, tmp_path)
        _ok(result)
        assert result.stdout == _out("Próximo cursor: 0")
        assert sorted(fake.requests[-1].url.params.multi_items()) in accepted, options
    assert len(fake.requests) == len(runs)


def test_list_json_prints_the_api_body(tmp_path) -> None:
    """Com `--json`, o stdout é o corpo da listagem, intacto. Vermelho no esqueleto: a folha lança
    `NotImplementedError`."""
    fake = FakeBanksApi()
    fake.page = {"items": [_item(BANK_NAME, "5" * 64)], "next_cursor": 9}
    result = _cq(["banks", "list", "--name", BANK_NAME], fake, tmp_path, json=True)
    _ok(result)
    assert json.loads(result.stdout) == fake.page
