"""O cliente da API no envio e na listagem dos bancos de questões (`ApiClient.upload_bank` e `list_banks`).

A API aqui é um dublê de transporte (`httpx2.MockTransport`): cada pedido que o cliente faz é registrado com o
método, o endereço, os cabeçalhos, o corpo e o prazo efetivo do pedido (a extensão `timeout` que o `httpx2` passa
ao transporte), e a resposta é um corpo na forma do contrato (conferida pelo esquema congelado).

O que fica fixado:
- `upload_bank` faz `POST /v1/question-banks` com o corpo JSON de exatamente cinco campos (`name`, `version`,
  `source`, `git_commit`, nulo na origem `folder`, e `archive`, o pacote em base64 padrão, com preenchimento e sem
  quebras de linha), os cabeçalhos de sempre (`X-CQ-Client`, `Authorization`) e nenhuma consulta; devolve o
  corpo da resposta; o erro da API sai como `ApiError`, e o transporte que falha, como `ApiUnreachable`;
- o prazo do envio é `httpx2.Timeout(300, connect=5)`, só nesse pedido, tanto no cliente HTTP próprio do
  `ApiClient` quanto num injetado; os outros pedidos, antes e depois dele, seguem com 5 s de conexão e 30 s no
  resto;
- o caminho local da pasta (e o nome dela) nunca vai no pedido: nem no endereço, nem nos cabeçalhos, nem no
  corpo, nem dentro do pacote descomprimido;
- `list_banks` faz `GET /v1/question-banks` só com a consulta dada (`name`, `after`, `limit`); sem argumentos,
  no máximo o cursor zero.

No esqueleto, `upload_bank` e `list_banks` lançam `NotImplementedError` (e, no caso do caminho local, também a
leitura da pasta): todo caso daqui fica vermelho por isso até a implementação do cliente.
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from cq_client.api import ApiClient, ApiError, ApiUnreachable
from tests.helpers import banks as bank_fixtures
from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.contract import MESSAGES, assert_matches

BASE_URL = "http://api.cq.test"
BANKS_PATH = "/v1/question-banks"
BANK_NAME = "banco-sintetico"
CREATED_AT = "2031-01-01T00:00:00Z"
UPLOAD_TIMEOUT = {"connect": 5.0, "read": 300.0, "write": 300.0, "pool": 300.0}
DEFAULT_TIMEOUT = {"connect": 5.0, "read": 30.0, "write": 30.0, "pool": 30.0}


class FakeApi:
    """O dublê da API: registra cada pedido e responde pela função `respond(request) -> httpx2.Response`
    (por padrão, o 200 da forma do contrato para cada rota dos bancos e um `GET /health` qualquer)."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._respond = respond or self.default

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        self.requests.append(request)
        return self._respond(request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)

    def client(self) -> httpx2.Client:
        """Um cliente HTTP injetável, com os mesmos prazos padrão do `ApiClient`."""
        return httpx2.Client(transport=self.transport(), timeout=httpx2.Timeout(30.0, connect=5.0))

    @staticmethod
    def default(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == BANKS_PATH and request.method == "POST":
            sent = json.loads(request.content)
            return httpx2.Response(200, json=_bank_version(sent, created=True))
        if request.url.path == BANKS_PATH and request.method == "GET":
            return httpx2.Response(200, json={"items": [], "next_cursor": 0})
        return httpx2.Response(200, json={"status": "ok", "version": "0.0.0", "schema_version": "0008"})


def _bank_version(sent: dict, *, created: bool) -> dict:
    """O corpo de resposta do envio, na forma do contrato, para o pedido `sent`."""
    body = {"name": sent["name"], "version": sent["version"], "repo": f"bank:{sent['name']}",
            "source": sent["source"], "git_commit": sent["git_commit"], "file_count": 3, "byte_count": 120,
            "question_count": 1, "created_at": CREATED_AT, "created": created}
    assert_matches("banks.upload", 200, body)
    return body


def _timeout(request: httpx2.Request) -> dict:
    return {key: float(value) for key, value in request.extensions["timeout"].items()}


def _sample() -> tuple[dict[str, bytes], bytes, str]:
    """Um banco pequeno, o pacote montado à mão (de tamanho que exige preenchimento no base64) e a versão."""
    files = {"infoCourse.json": b'{"uuid": "00000000-0000-4000-8000-0000000000ab", "name": "Sintetico"}\n',
             "questions/q1/info.json": b'{"title": "Q1"}\n',
             "questions/q1/question.html": b"<p>1 + 1</p>\n"}
    archive = bank_fixtures.archive_of(files)
    if len(archive) % 3 == 0:
        archive = bank_fixtures.archive_of({**files, "extra.txt": b"x"})
        files = {**files, "extra.txt": b"x"}
    assert len(archive) % 3 != 0
    return files, archive, bank_fixtures.reference_version(files)


def _upload(api: ApiClient, archive: bytes, version: str, *, source: str = "folder",
            git_commit: str | None = None) -> dict:
    return api.upload_bank(name=BANK_NAME, version=version, source=source, git_commit=git_commit, archive=archive)


@pytest.fixture
def owned_http(monkeypatch) -> FakeApi:
    """O cliente HTTP **próprio** do `ApiClient` (sem `http=`) passa pelo dublê: o `httpx2.Client` criado ganha o
    transporte do dublê e mantém os prazos que o `ApiClient` lhe dá."""
    fake = FakeApi()
    real_client = httpx2.Client

    class WithFakeTransport(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = fake.transport()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx2, "Client", WithFakeTransport)
    return fake


# --------------------------------------------------------------------------------------------------
# O envio


def test_upload_sends_the_contract_body_and_returns_the_response() -> None:
    """`upload_bank` faz um único `POST /v1/question-banks`, sem consulta, com os cabeçalhos de sempre e o corpo
    de cinco campos (o pacote em base64 padrão, com preenchimento e sem quebras de linha; `git_commit` nulo na
    pasta); devolve o corpo da resposta. Vermelho no esqueleto: `upload_bank` lança `NotImplementedError`."""
    fake = FakeApi()
    files, archive, version = _sample()
    with ApiClient(BASE_URL, ADMIN_TOKEN, http=fake.client()) as api:
        body = _upload(api, archive, version)
    assert body == _bank_version(bank_fixtures.upload_body(BANK_NAME, version, "folder", None, archive),
                                 created=True)
    [request] = fake.requests
    assert (request.method, str(request.url)) == ("POST", BASE_URL + BANKS_PATH)
    assert request.headers["X-CQ-Client"] == "cli"
    assert request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert request.headers["content-type"].split(";")[0] == "application/json"
    sent = json.loads(request.content)
    assert sent == bank_fixtures.upload_body(BANK_NAME, version, "folder", None, archive)
    assert sent["archive"].endswith("=") and "\n" not in sent["archive"]
    assert base64.b64decode(sent["archive"], validate=True) == archive


def test_upload_from_git_sends_the_commit() -> None:
    """Na origem `git`, o corpo leva o commit dado (40 ou 64 hexadecimais), e o resto como na pasta. Vermelho no
    esqueleto: `upload_bank` lança `NotImplementedError`."""
    fake = FakeApi()
    files, archive, version = _sample()
    for commit in ("ab" * 20, "cd" * 32):
        with ApiClient(BASE_URL, ADMIN_TOKEN, http=fake.client()) as api:
            body = _upload(api, archive, version, source="git", git_commit=commit)
        assert (body["source"], body["git_commit"]) == ("git", commit)
        assert json.loads(fake.requests[-1].content) == bank_fixtures.upload_body(BANK_NAME, version, "git", commit,
                                                                                  archive)


def test_upload_timeout_applies_only_to_the_upload_with_an_injected_client() -> None:
    """Com o cliente HTTP injetado, o envio vai com 300 s de leitura, escrita e espera e 5 s de conexão; a
    listagem e a saúde, antes e depois dele, seguem com 30 s e 5 s. Vermelho no esqueleto: `upload_bank` lança
    `NotImplementedError`."""
    from cq_client.api import UPLOAD_TIMEOUT_SECONDS

    assert UPLOAD_TIMEOUT_SECONDS == 300.0
    fake = FakeApi()
    _, archive, version = _sample()
    with ApiClient(BASE_URL, ADMIN_TOKEN, http=fake.client()) as api:
        api.health()
        _upload(api, archive, version)
        api.list_banks()
        api.health()
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ("GET", "/health"), ("POST", BANKS_PATH), ("GET", BANKS_PATH), ("GET", "/health")]
    assert [_timeout(r) for r in fake.requests] == [DEFAULT_TIMEOUT, UPLOAD_TIMEOUT, DEFAULT_TIMEOUT,
                                                    DEFAULT_TIMEOUT]


def test_upload_timeout_applies_only_to_the_upload_with_the_owned_client(owned_http) -> None:
    """Com o cliente HTTP próprio do `ApiClient`, o mesmo: 300 s e 5 s só no envio; 30 s e 5 s nos outros pedidos.
    Vermelho no esqueleto: `upload_bank` lança `NotImplementedError`."""
    _, archive, version = _sample()
    with ApiClient(BASE_URL, ADMIN_TOKEN) as api:
        api.health()
        _upload(api, archive, version)
        api.health()
    assert [(r.method, r.url.path) for r in owned_http.requests] == [
        ("GET", "/health"), ("POST", BANKS_PATH), ("GET", "/health")]
    assert [_timeout(r) for r in owned_http.requests] == [DEFAULT_TIMEOUT, UPLOAD_TIMEOUT, DEFAULT_TIMEOUT]


def test_upload_errors_keep_the_client_conventions() -> None:
    """A recusa da API no envio sai como `ApiError` com o corpo intacto; o transporte que falha, como
    `ApiUnreachable` com o endereço base e sem a exceção original. Vermelho no esqueleto: `upload_bank` lança
    `NotImplementedError`."""
    error = {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                       "fields": [{"field": "archive", "reason": "not_a_course"}]}}
    refused = FakeApi(lambda request: httpx2.Response(400, json=error))
    _, archive, version = _sample()
    with ApiClient(BASE_URL, ADMIN_TOKEN, http=refused.client()) as api:
        with pytest.raises(ApiError) as info:
            _upload(api, archive, version)
    assert (info.value.status, info.value.body) == (400, error)

    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("falha simulada", request=request)

    with ApiClient(BASE_URL, ADMIN_TOKEN, http=FakeApi(broken).client()) as api:
        with pytest.raises(ApiUnreachable) as unreachable:
            _upload(api, archive, version)
    assert unreachable.value.url == BASE_URL
    assert unreachable.value.__cause__ is None


def test_local_folder_path_never_in_the_request(tmp_path) -> None:
    """O banco lido de uma pasta com nome único e enviado: o caminho local e o nome da pasta não aparecem no
    endereço, nos cabeçalhos, no corpo nem no pacote descomprimido. Vermelho no esqueleto: `read_folder`,
    `build_archive` e `upload_bank` lançam `NotImplementedError`."""
    from cq_client.banks import bank_version, build_archive, read_folder

    files, _, _ = _sample()
    root = bank_fixtures.write_tree(tmp_path / f"pasta-{bank_fixtures.unique_marker()}", files)
    bank = read_folder(str(root))
    archive = build_archive(bank.entries)
    fake = FakeApi()
    with ApiClient(BASE_URL, ADMIN_TOKEN, http=fake.client()) as api:
        _upload(api, archive, bank_version(bank.entries))
    [request] = fake.requests
    inflater = zlib.decompressobj(wbits=31)
    tar = inflater.decompress(archive) + inflater.flush()
    seen = [str(request.url).encode("utf-8"), *(f"{k}: {v}".encode("utf-8") for k, v in request.headers.items()),
            request.content, archive, tar]
    for local in (str(root), Path(root).as_posix(), root.name, str(tmp_path), Path(tmp_path).as_posix()):
        needles = {local.encode("utf-8"), json.dumps(local)[1:-1].encode("utf-8")}
        for blob in seen:
            assert not any(needle in blob for needle in needles), "o pedido leva o caminho local da pasta"


# --------------------------------------------------------------------------------------------------
# A listagem


def test_list_banks_sends_only_the_given_query() -> None:
    """`list_banks` faz `GET /v1/question-banks`, sem corpo, com a consulta dada (`name`, `after`, `limit`) e
    nada além; sem argumentos, nenhuma consulta ou só o cursor zero; devolve o corpo. Vermelho no esqueleto:
    `list_banks` lança `NotImplementedError`."""
    page = {"items": [{"name": BANK_NAME, "version": "e" * 64, "repo": f"bank:{BANK_NAME}", "source": "folder",
                       "git_commit": None, "file_count": 3, "byte_count": 120, "question_count": 1,
                       "created_at": CREATED_AT}], "next_cursor": 7}
    assert_matches("banks.list", 200, page)
    fake = FakeApi(lambda request: httpx2.Response(200, json=page))
    with ApiClient(BASE_URL, ADMIN_TOKEN, http=fake.client()) as api:
        assert api.list_banks(name=BANK_NAME, after=5, limit=10) == page
        assert api.list_banks(name=BANK_NAME) == page
        assert api.list_banks(after=3) == page
        assert api.list_banks() == page
    assert [(r.method, r.url.path, r.content) for r in fake.requests] == [("GET", BANKS_PATH, b"")] * 4
    queries = [sorted(r.url.params.multi_items()) for r in fake.requests]
    assert queries[0] == [("after", "5"), ("limit", "10"), ("name", BANK_NAME)]
    assert queries[1] in ([("name", BANK_NAME)], [("after", "0"), ("name", BANK_NAME)])
    assert queries[2] == [("after", "3")]
    assert queries[3] in ([], [("after", "0")])
    assert all(r.headers["X-CQ-Client"] == "cli" for r in fake.requests)
    assert [_timeout(r) for r in fake.requests] == [DEFAULT_TIMEOUT] * 4
