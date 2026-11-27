"""O banco de questões local pela API: o envio (`banks.upload`, `POST /v1/question-banks`) e a listagem
(`banks.list`, `GET /v1/question-banks`).

Cobre:
- o envio aceito: 200 com a versão guardada e `created`, a linha de `question_banks` e o evento `bank.uploaded`
  com os campos do contrato; o reenvio idempotente (o primeiro envio vence, também com outra origem ou outro
  empacotamento do mesmo conteúdo); dois envios simultâneos; o mesmo conteúdo com dois nomes; a permissão
  `offerings:write`; o horário pelo relógio injetado; um envio por vez;
- as recusas: o teto do corpo pelo `Content-Length` (sem ler) e pelo contador (corpo em pedaços), o
  `Content-Length` não numérico, a desconexão do cliente no meio da leitura, a ordem de validação do contrato
  (cabeçalho, consulta, permissão antes de ler o corpo, teto, JSON, campos, pacote, versão, gravação), cada motivo
  de campo e cada regra do pacote com o envelope inteiro, sem eco, sem nada guardado e sem o texto da exceção da
  leitura no registro da api;
- a listagem: a ordem do primeiro envio, `after`, `limit` (1 a 500, padrão 100) e `next_cursor`, o filtro pelo
  nome exato, os campos sem `created`, o corpo recusado, a permissão `offerings:read` e o nome e o `repo`
  redigidos (também no evento, com a parte local do e-mail de uma pessoa como alvo).

Arnês: o `client` da API em processo (o `TestClient`, com o `FakeClock` do teste). Os casos que precisam do
controle das mensagens do corpo (o `Content-Length` sem corpo, o corpo em pedaços, a desconexão e a vaga de
envio) chamam o app ASGI direto, no laço de eventos do próprio `TestClient` (`client.portal`), com um `receive`
do teste que conta as leituras. O banco sintético é o curso de fixture (`tests/fixtures/repos/course`) ou uma
árvore mínima; os pacotes e a versão de referência vêm de `tests/helpers/banks.py`, montados à mão, sem o `cq` e
sem o produto. Os tetos e os motivos são os do contrato, escritos aqui (e não importados), para o teste não
depender da implementação.

Vermelho esperado no esqueleto: as duas rotas lançam `NotImplementedError` logo depois do middleware, e toda
chamada autenticada responde 500 `internal` (o núcleo `ensure_bank` também lança); fica assim até as rotas, a
leitura do pacote e a gravação existirem.

Escolhas deste arquivo [I]:
- num pedido com vários campos inválidos, a recusa traz um motivo por campo, na ordem do esquema (como as outras
  rotas com corpo); o pacote e a versão só são conferidos com os campos válidos;
- na desconexão do cliente no meio da leitura, a resposta não é fixada: só não pode ser 500;
- a vaga de envio só é solta depois da gravação: quando o segundo envio lê o corpo, o primeiro já está guardado.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from tests.helpers import banks as bk
from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_token, marker, register_person
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.logs import Captured, capture_logs

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "server" / "src" / "cq_server"

BANKS_PATH = "/v1/question-banks"
BANK_EVENT = "bank.uploaded"
REPO_PREFIX = "bank:"

# Os tetos do contrato (escritos aqui, e não importados).
MAX_BODY_BYTES = 188743680  # 180 MiB: o corpo JSON do envio
MAX_ARCHIVE_BYTES = 134217728  # 128 MiB: o pacote decodificado
CHUNK = 1 << 20  # 1 MiB por mensagem do corpo em pedaços

ALL_PERMISSIONS = ["bots:manage", "events:read", "identity:reveal", "offerings:read", "offerings:write",
                   "people:read", "people:write", "tokens:manage"]

GIT_COMMIT_40 = "a1b2c3d4" * 5
GIT_COMMIT_64 = "c0ffee00" * 8
OTHER_GIT_COMMIT_40 = "0badc0de" * 5

# O tempo real em que o segundo envio fica esperando a vaga, com o primeiro preso na leitura.
HOLD_SECONDS = 0.5

# Trechos do texto das exceções da leitura do pacote que nunca podem chegar ao registro da api (zlib, int(),
# decodificação UTF-8, struct, o tipo das exceções).
EXCEPTION_FRAGMENTS = ("Traceback", "Error -3", "invalid block type", "invalid literal", "codec", "0xff",
                       "can't decode", "zlib", "UnicodeDecodeError", "ValueError", "struct", "EOFError",
                       "BadGzipFile", "incorrect header check")

# A árvore mínima de um curso: o `infoCourse.json` e uma questão.
SMALL = {"infoCourse.json": b'{"name": "curso-minimo"}\n',
         "questions/sentinela-questao/info.json": b'{"title": "Q"}\n',
         "questions/sentinela-questao/question.html": b"<p>Q</p>\n"}
SMALL_VERSION = bk.reference_version(SMALL)
INFO = bk.regular("infoCourse.json", b'{"name": "curso-minimo"}\n')


# --------------------------------------------------------------------------------------------------
# Ajudantes: o banco, o corpo, a resposta esperada e o estado guardado
# --------------------------------------------------------------------------------------------------


def _course() -> dict[str, bytes]:
    return bk.course_files()


def _body(name: str, files: dict[str, bytes], *, source: str = "folder", git_commit: str | None = None,
          archive: bytes | None = None, version: str | None = None) -> dict[str, Any]:
    return bk.upload_body(name, version or bk.reference_version(files), source, git_commit,
                          archive if archive is not None else bk.archive_of(files))


def _post(client: Any, body: Any, *, token: str = ADMIN_TOKEN, params: Any = None,
          headers: dict[str, str] | None = None) -> Any:
    return client.post(BANKS_PATH, json=body, params=params, headers={**auth(token), **(headers or {})})


def _post_raw(client: Any, content: bytes, *, token: str = ADMIN_TOKEN) -> Any:
    return client.post(BANKS_PATH, content=content,
                       headers={**auth(token), "Content-Type": "application/json"})


def _ok(resp: Any) -> dict:
    assert resp.status_code == 200, f"envio: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("banks.upload", 200, body)
    return body


def _list(client: Any, *, token: str = ADMIN_TOKEN, params: Any = None) -> Any:
    return client.get(BANKS_PATH, params=params, headers=auth(token))


def _list_ok(client: Any, **params: Any) -> dict:
    resp = _list(client, params=params or None)
    assert resp.status_code == 200, f"listagem: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("banks.list", 200, body)
    return body


def _version_body(name: str, files: dict[str, bytes], *, source: str, git_commit: str | None,
                  created_at: Any, shown_name: str | None = None) -> dict[str, Any]:
    """O `BankVersion` esperado (sem `created`)."""
    shown = name if shown_name is None else shown_name
    return {"name": shown, "version": bk.reference_version(files), "repo": REPO_PREFIX + shown,
            "source": source, "git_commit": git_commit, "file_count": len(files),
            "byte_count": sum(len(data) for data in files.values()),
            "question_count": bk.reference_question_count(files), "created_at": iso(created_at)}


def _event_data(name: str, files: dict[str, bytes], *, source: str, git_commit: str | None) -> dict[str, Any]:
    return {"bank": name, "version": bk.reference_version(files), "source": source, "git_commit": git_commit,
            "file_count": len(files), "byte_count": sum(len(data) for data in files.values()),
            "question_count": bk.reference_question_count(files)}


def _rows(sql: psycopg.Connection) -> list[dict[str, Any]]:
    """As linhas de `question_banks`, em ordem de `id`, com o pacote pelo SHA-256 (nunca os bytes)."""
    cur = sql.execute(
        "SELECT id, bank_uuid IS NOT NULL AS has_uuid, name, version, source, git_commit, "
        "encode(sha256(archive), 'hex') AS archive_sha256, archive_bytes, file_count, byte_count, question_count, "
        "actor_token_id::text AS actor_token_id, created_at FROM question_banks ORDER BY id")
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _row_of(name: str, files: dict[str, bytes], archive: bytes, *, source: str, git_commit: str | None,
            created_at: Any, actor: str | None = None) -> dict[str, Any]:
    """A linha esperada de `question_banks` (sem o `id`)."""
    return {"has_uuid": True, "name": name, "version": bk.reference_version(files), "source": source,
            "git_commit": git_commit, "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "archive_bytes": len(archive), "file_count": len(files),
            "byte_count": sum(len(data) for data in files.values()),
            "question_count": bk.reference_question_count(files), "actor_token_id": actor, "created_at": created_at}


def _without_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]


def _bank_events(client: Any) -> list[dict[str, Any]]:
    """Os eventos `bank.uploaded`, pela API, sem o `seq`."""
    return [{k: v for k, v in e.items() if k != "seq"} for e in all_events(client, ADMIN_TOKEN, BANK_EVENT)]


def _assert_nothing_stored(sql: psycopg.Connection) -> None:
    assert sql.execute("SELECT count(*) FROM question_banks").fetchone()[0] == 0, "a recusa guardou um banco"
    assert sql.execute("SELECT count(*) FROM events WHERE type = %s", (BANK_EVENT,)).fetchone()[0] == 0, (
        "a recusa gravou um evento do envio")


def _assert_clean_refusal_logs(captured: Captured, *texts: str) -> None:
    """Um único registro `request` com 400 na rota do envio; nenhum registro de exceção; nenhum trecho do texto
    de uma exceção da leitura nem os valores dados."""
    requests = [r for r in captured.records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]
    assert len(requests) == 1, f"esperado 1 registro request, vieram {len(requests)}"
    assert (requests[0].get("method"), requests[0].get("route"), requests[0].get("status")) == (
        "POST", BANKS_PATH, 400), requests[0]
    assert captured.with_event("exception") == [], "registro de exceção na recusa do pacote"
    assert all("exc_type" not in r for r in captured.records), "tipo de exceção no registro"
    for fragment in (*EXCEPTION_FRAGMENTS, *texts):
        assert fragment not in captured.text, f"{fragment!r} no registro da api"


# --------------------------------------------------------------------------------------------------
# O app ASGI direto (o corpo controlado pelo teste)
# --------------------------------------------------------------------------------------------------


@dataclass
class _Reply:
    """A resposta de uma chamada ASGI direta, com a forma que `assert_error` e `assert_no_echo` leem."""

    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    error: BaseException | None = None
    receives: int = 0

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content)


def _headers(token: str | None = ADMIN_TOKEN, *, length: str | None = None,
             client_kind: str | None = None) -> list[tuple[bytes, bytes]]:
    out = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    if token is not None:
        out.append((b"authorization", f"Bearer {token}".encode("ascii")))
    if length is not None:
        out.append((b"content-length", length.encode("ascii")))
    if client_kind is not None:
        out.append((b"x-cq-client", client_kind.encode("ascii")))
    return out


def _scope(client: Any, headers: list[tuple[bytes, bytes]], *, query: bytes = b"") -> dict[str, Any]:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": BANKS_PATH, "raw_path": BANKS_PATH.encode("ascii"),
            "root_path": "", "query_string": query, "headers": headers, "client": ("testclient", 50000),
            "server": ("testserver", 80), "extensions": {}, "state": dict(client.app_state)}


async def _drive(app: Any, scope: dict[str, Any], receive: Callable[[], Any]) -> _Reply:
    reply = _Reply()
    body = bytearray()

    async def counted() -> dict[str, Any]:
        reply.receives += 1
        return await receive()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            reply.status_code = message["status"]
            reply.headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    try:
        await app(scope, counted, send)
    except BaseException as exc:  # noqa: BLE001 - o teste afirma que nada escapa
        reply.error = exc
    reply.content = bytes(body)
    return reply


def _call(client: Any, headers: list[tuple[bytes, bytes]], receive: Callable[[], Any], *,
          query: bytes = b"") -> _Reply:
    """Uma chamada ASGI direta ao envio, no laço de eventos do `TestClient`."""
    return client.portal.call(_drive, client.app, _scope(client, headers, query=query), receive)


def _messages(chunks: Iterable[bytes], *, then_disconnect: bool = False) -> Callable[[], Any]:
    """O `receive` que entrega os pedaços, o último com `more_body` falso (ou, com `then_disconnect`, todos com
    `more_body` verdadeiro, seguidos da desconexão do cliente)."""
    items = list(chunks)
    state = {"next": 0}

    async def receive() -> dict[str, Any]:
        index = state["next"]
        state["next"] += 1
        if index < len(items):
            last = index == len(items) - 1 and not then_disconnect
            return {"type": "http.request", "body": items[index], "more_body": not last}
        return {"type": "http.disconnect"}

    return receive


def _repeated(chunk: bytes, count: int, *, tail: bytes = b"") -> Callable[[], Any]:
    """O `receive` de `count` mensagens iguais (`more_body` verdadeiro) e, depois, `tail` como a última."""
    state = {"next": 0}

    async def receive() -> dict[str, Any]:
        index = state["next"]
        state["next"] += 1
        if index < count:
            return {"type": "http.request", "body": chunk, "more_body": True}
        if index == count:
            return {"type": "http.request", "body": tail, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _no_body() -> dict[str, Any]:
    """O `receive` que não devia ser chamado (a recusa vem antes de ler o corpo)."""
    return {"type": "http.disconnect"}


def _json_bytes(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


# --------------------------------------------------------------------------------------------------
# O envio aceito
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "git_commit"), [("folder", None), ("git", GIT_COMMIT_40), ("git", GIT_COMMIT_64)],
                         ids=["pasta", "git-sha1", "git-sha256"])
def test_upload_answers_200_created_and_stores_the_bank_with_one_event(
        client: Any, sql: psycopg.Connection, clock: Any, source: str, git_commit: str | None) -> None:
    """O envio do curso de fixture responde 200 com o `BankVersion` exato e `created: true`: o nome, a versão (a de
    referência), o `repo` `bank:<nome>`, a origem e o commit git, as contagens (arquivos, bytes do conteúdo e
    questões) e o horário do relógio injetado. A linha de `question_banks` guarda o pacote recebido, as contagens,
    o token nulo do token inicial e o mesmo horário; um único `bank.uploaded` com os campos do contrato, o ator do
    token inicial e o mesmo horário. Vermelho no esqueleto: a rota lança `NotImplementedError` (500)."""
    files = _course()
    archive = bk.archive_of(files)
    clock.advance(1.5)
    now = clock.now()

    body = _ok(_post(client, _body("banco-fisica", files, source=source, git_commit=git_commit, archive=archive)))

    assert body == {**_version_body("banco-fisica", files, source=source, git_commit=git_commit, created_at=now),
                    "created": True}
    assert _without_id(_rows(sql)) == [
        _row_of("banco-fisica", files, archive, source=source, git_commit=git_commit, created_at=now)]
    assert _bank_events(client) == [{"type": BANK_EVENT, "occurred_at": iso(now), "actor_token_id": "bootstrap",
                                     "data": _event_data("banco-fisica", files, source=source,
                                                         git_commit=git_commit)}]


def test_ten_reuploads_keep_one_row_and_one_event(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Dez envios do mesmo nome e da mesma versão, com o relógio andando entre eles: o primeiro responde
    `created: true`; os outros nove, o mesmo corpo com `created: false` e o horário do primeiro. Uma linha e um
    evento. Vermelho no esqueleto: 500 no primeiro envio."""
    files = _course()
    first_at = clock.now()
    bodies = []
    for _ in range(10):
        bodies.append(_ok(_post(client, _body("banco-repetido", files))))
        clock.advance(60)

    expected = _version_body("banco-repetido", files, source="folder", git_commit=None, created_at=first_at)
    assert bodies == [{**expected, "created": True}] + [{**expected, "created": False}] * 9
    assert len(_rows(sql)) == 1
    assert [e["occurred_at"] for e in _bank_events(client)] == [iso(first_at)]


def test_two_simultaneous_uploads_create_the_version_once(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Dois envios iguais ao mesmo tempo (duas threads, soltas juntas): um responde `created: true` e o outro
    `created: false`, com o mesmo `BankVersion`; uma linha e um evento. Vermelho no esqueleto: 500 nos dois."""
    files = _course()
    payload = _body("banco-simultaneo", files)
    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]

    def upload(index: int) -> None:
        barrier.wait(10)
        results[index] = _post(client, payload)

    threads = [threading.Thread(target=upload, args=(i,), daemon=True) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads), "um envio não terminou em 60 s"

    bodies = [_ok(resp) for resp in results]
    assert sorted(b["created"] for b in bodies) == [False, True]
    expected = _version_body("banco-simultaneo", files, source="folder", git_commit=None, created_at=clock.now())
    assert [{k: v for k, v in b.items() if k != "created"} for b in bodies] == [expected, expected]
    assert len(_rows(sql)) == 1
    assert len(_bank_events(client)) == 1


def test_concurrent_ensure_waits_for_the_first_transaction_and_returns_its_version(
        db_url: str, sql: psycopg.Connection, clock: Any) -> None:
    """A gravação no núcleo (`banks.ensure_bank`) com duas conexões: a primeira grava e segura a transação; a
    segunda, com o mesmo nome e a mesma versão, espera o bloqueio da linha e, depois do COMMIT da primeira,
    devolve a versão guardada por ela com `created` falso, sem evento próprio. Vermelho no esqueleto: `ensure_bank`
    lança `NotImplementedError`."""
    from cq_server.banks import ArchiveSummary, BankEntry, ensure_bank

    files = _course()
    archive = bk.archive_of(files)
    entries = tuple(BankEntry(path, len(data), hashlib.sha256(data).hexdigest()) for path, data in files.items())
    summary = ArchiveSummary(entries=entries, file_count=len(files),
                             byte_count=sum(len(d) for d in files.values()),
                             question_count=bk.reference_question_count(files))
    version = bk.reference_version(files)
    kwargs = {"name": "banco-concorrente", "version": version, "source": "folder", "git_commit": None,
              "archive": archive, "summary": summary, "now": clock.now(), "actor_token_id": None}
    second: dict[str, Any] = {}

    with psycopg.connect(db_url) as first, psycopg.connect(db_url) as other:
        other_pid = other.info.backend_pid

        def run_second() -> None:
            try:
                second["result"] = ensure_bank(other, **kwargs)
                other.commit()
            except BaseException as exc:  # noqa: BLE001 - o teste afirma o resultado
                second["error"] = exc

        thread = threading.Thread(target=run_second, daemon=True)
        # O teste é dono da transação da primeira conexão: um bloco de transação interno da gravação vira
        # savepoint e só confirma no COMMIT da saída deste bloco, com a segunda gravação já à espera.
        with first.transaction():
            stored, created = ensure_bank(first, **kwargs)
            thread.start()
            deadline = time.monotonic() + 10
            while not sql.execute("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s",
                                  (other_pid,)).fetchone()[0]:
                assert time.monotonic() < deadline and thread.is_alive(), (
                    "a segunda gravação não esperou o bloqueio")
                time.sleep(0.02)
        thread.join(30)

    assert "error" not in second, f"a segunda gravação falhou: {type(second.get('error')).__name__}"
    assert created is True
    assert second["result"] == (stored, False)
    assert (stored.name, stored.version, stored.repo, stored.source, stored.git_commit) == (
        "banco-concorrente", version, "bank:banco-concorrente", "folder", None)
    assert len(_rows(sql)) == 1
    assert sql.execute("SELECT count(*) FROM events WHERE type = %s", (BANK_EVENT,)).fetchone()[0] == 1


@pytest.mark.parametrize(("first", "second"), [
    (("folder", None), ("git", GIT_COMMIT_40)),
    (("git", GIT_COMMIT_40), ("folder", None)),
    (("git", GIT_COMMIT_40), ("git", OTHER_GIT_COMMIT_40)),
], ids=["pasta-depois-git", "git-depois-pasta", "git-outro-commit"])
def test_same_content_from_another_source_returns_the_first_upload(
        client: Any, sql: psycopg.Connection, clock: Any, first: tuple[str, str | None],
        second: tuple[str, str | None]) -> None:
    """O mesmo nome e o mesmo conteúdo enviados de novo com outra origem ou outro commit git: 200 com o primeiro
    envio (a origem, o commit e o horário dele) e `created: false`; a linha e o evento não mudam. Vermelho no
    esqueleto: 500."""
    files = _course()
    first_at = clock.now()
    _ok(_post(client, _body("banco-origem", files, source=first[0], git_commit=first[1])))
    rows = _rows(sql)
    events = _bank_events(client)
    clock.advance(30)

    again = _ok(_post(client, _body("banco-origem", files, source=second[0], git_commit=second[1])))

    assert again == {**_version_body("banco-origem", files, source=first[0], git_commit=first[1],
                                     created_at=first_at), "created": False}
    assert _rows(sql) == rows
    assert _bank_events(client) == events


def test_same_content_under_two_names_is_two_versions(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """O mesmo conteúdo com dois nomes: duas linhas e dois eventos, os dois envios com `created: true`, a mesma
    versão e o `repo` de cada nome. Vermelho no esqueleto: 500."""
    files = _course()
    one = _ok(_post(client, _body("banco-um", files)))
    two = _ok(_post(client, _body("banco-dois", files)))

    assert (one["created"], two["created"]) == (True, True)
    assert one["version"] == two["version"] == bk.reference_version(files)
    assert (one["repo"], two["repo"]) == ("bank:banco-um", "bank:banco-dois")
    assert [r["name"] for r in _rows(sql)] == ["banco-um", "banco-dois"]
    assert [e["data"]["bank"] for e in _bank_events(client)] == ["banco-um", "banco-dois"]


def test_first_upload_wins_and_the_stored_bank_never_changes(client: Any, sql: psycopg.Connection,
                                                             clock: Any) -> None:
    """A versão guardada é imutável: o mesmo conteúdo empacotado de outro jeito (outra ordem dos membros, outros
    horários, donos e modos, outra compressão, sem o preenchimento do registro) dá a mesma versão e devolve o
    primeiro envio com `created: false`, sem trocar o pacote guardado. E nenhum código do servidor altera ou apaga
    linhas de `question_banks` (sem `UPDATE`, `DELETE` nem `TRUNCATE` da tabela). Vermelho no esqueleto: 500 no
    primeiro envio."""
    files = _course()
    archive = bk.archive_of(files)
    first_at = clock.now()
    _ok(_post(client, _body("banco-imutavel", files, archive=archive)))
    stored = _rows(sql)

    repacked = bk.make_archive(
        [bk.regular(path, files[path], mtime=1234567, uid=1000, gid=1000, mode=0o600, uname=b"autor",
                    gname=b"grupo") for path in sorted(files, reverse=True)],
        compresslevel=1, pad_record=False)
    assert repacked != archive
    clock.advance(30)
    again = _ok(_post(client, _body("banco-imutavel", files, archive=repacked)))

    assert again == {**_version_body("banco-imutavel", files, source="folder", git_commit=None,
                                     created_at=first_at), "created": False}
    assert _rows(sql) == stored
    assert stored[0]["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    assert len(_bank_events(client)) == 1

    writes = re.compile(r"\b(UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+(ONLY\s+)?question_banks\b", re.IGNORECASE)
    offenders = [str(path.relative_to(REPO_ROOT)) for path in sorted(SERVER_SRC.rglob("*"))
                 if path.suffix in (".py", ".sql") and writes.search(path.read_text(encoding="utf-8"))]
    assert offenders == [], f"código que altera ou apaga bancos guardados: {offenders}"


def test_upload_requires_offerings_write_and_records_the_actor(client: Any, sql: psycopg.Connection,
                                                               clock: Any) -> None:
    """Sem `offerings:write` (um token com todas as outras permissões, e um só com `offerings:read`): 403 nomeando
    `offerings:write`, sem linha e sem evento. Com só `offerings:write`: 200, e a linha e o evento levam o token
    que enviou. Vermelho no esqueleto: 500 (a rota lança antes da permissão)."""
    files = _course()
    others, _ = create_token(client, ADMIN_TOKEN, [p for p in ALL_PERMISSIONS if p != "offerings:write"],
                             label="todas menos a escrita")
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="so leitura")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="so escrita")

    for token in (others, reader):
        assert_error(_post(client, _body("banco-permissao", files), token=token), 403, "forbidden",
                     permission="offerings:write")
    _assert_nothing_stored(sql)

    now = clock.now()
    archive = bk.archive_of(files)
    _ok(_post(client, _body("banco-permissao", files, archive=archive), token=writer))
    assert _without_id(_rows(sql)) == [_row_of("banco-permissao", files, archive, source="folder", git_commit=None,
                                               created_at=now, actor=writer_body["token_id"])]
    assert [(e["actor_token_id"], e["data"]["bank"]) for e in _bank_events(client)] == [
        (writer_body["token_id"], "banco-permissao")]


def test_uploads_are_processed_one_at_a_time(client: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Um envio por vez: com o primeiro preso no meio da leitura do corpo, o segundo não lê nada (meio segundo
    real de espera); solto o primeiro, o segundo só começa a ler depois que o primeiro já está guardado, e os dois
    respondem 200 com `created: true`. Vermelho no esqueleto: os dois respondem 500 sem ler o corpo."""
    import anyio

    files = _course()
    first_body = _json_bytes(_body("banco-primeiro", files))
    second_body = _json_bytes(_body("banco-segundo", files))
    half = len(first_body) // 2

    def stored(name: str) -> int:
        with psycopg.connect(db_url, autocommit=True) as conn:
            return conn.execute("SELECT count(*) FROM question_banks WHERE name = %s", (name,)).fetchone()[0]

    async def scenario(app: Any, first_scope: dict, second_scope: dict) -> tuple[dict, dict]:
        gate = anyio.Event()
        log: dict[str, Any] = {"first": 0, "second": 0, "second_during_hold": None, "first_stored": None}
        replies: dict[str, _Reply] = {}

        async def receive_first() -> dict[str, Any]:
            log["first"] += 1
            if log["first"] == 1:
                return {"type": "http.request", "body": first_body[:half], "more_body": True}
            if log["first"] == 2:
                await gate.wait()
                return {"type": "http.request", "body": first_body[half:], "more_body": False}
            return {"type": "http.disconnect"}

        async def receive_second() -> dict[str, Any]:
            log["second"] += 1
            if log["second"] == 1:
                log["first_stored"] = await anyio.to_thread.run_sync(stored, "banco-primeiro")
                return {"type": "http.request", "body": second_body, "more_body": False}
            return {"type": "http.disconnect"}

        async def run(key: str, scope: dict, receive: Callable[[], Any]) -> None:
            replies[key] = await _drive(app, scope, receive)

        async with anyio.create_task_group() as group:
            group.start_soon(run, "first", first_scope, receive_first)
            deadline = anyio.current_time() + 10
            while log["first"] < 2 and "first" not in replies and anyio.current_time() < deadline:
                await anyio.sleep(0.01)
            group.start_soon(run, "second", second_scope, receive_second)
            await anyio.sleep(HOLD_SECONDS)
            log["second_during_hold"] = log["second"]
            gate.set()
        return log, replies

    log, replies = client.portal.call(scenario, client.app,
                                      _scope(client, _headers(length=str(len(first_body)))),
                                      _scope(client, _headers(length=str(len(second_body)))))

    assert replies["first"].error is None and replies["second"].error is None
    assert log["first"] >= 2, f"o primeiro envio não chegou a ler o corpo: status {replies['first'].status_code}"
    assert log["second_during_hold"] == 0, "o segundo envio leu o corpo com o primeiro em andamento"
    assert log["first_stored"] == 1, "o segundo envio leu o corpo antes de o primeiro ser guardado"
    for key in ("first", "second"):
        assert replies[key].status_code == 200, (key, replies[key].status_code, replies[key].text[:300])
        assert_matches("banks.upload", 200, replies[key].json())
        assert replies[key].json()["created"] is True
    assert [r["name"] for r in _rows(sql)] == ["banco-primeiro", "banco-segundo"]


# --------------------------------------------------------------------------------------------------
# As recusas do corpo: o teto, o Content-Length e a desconexão
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("length", "reason"), [
    (str(MAX_BODY_BYTES + 1), "too_long"),
    (str(10 * MAX_BODY_BYTES), "too_long"),
    ("abc", "invalid_format"),
    ("1e9", "invalid_format"),
], ids=["um-byte-acima", "dez-vezes-o-teto", "nao-numerico", "notacao-cientifica"])
def test_content_length_is_checked_before_reading_the_body(client: Any, sql: psycopg.Connection, length: str,
                                                           reason: str) -> None:
    """O `Content-Length` acima do teto do corpo (180 MiB) → 400 `[{body, too_long}]`; não numérico → 400
    `[{body, invalid_format}]`; nos dois, sem ler nenhuma mensagem do corpo, sem nada guardado. Vermelho no
    esqueleto: 500."""
    reply = _call(client, _headers(length=length), _no_body)

    assert reply.error is None
    assert_error(reply, 400, "invalid_request", fields=[("body", reason)])
    assert reply.receives == 0, "a api leu o corpo antes de recusar o Content-Length"
    _assert_nothing_stored(sql)


@pytest.mark.parametrize(("chunks", "tail", "reason", "max_receives"), [
    (MAX_BODY_BYTES // CHUNK, b" ", "too_long", MAX_BODY_BYTES // CHUNK + 1),
    (MAX_BODY_BYTES // CHUNK + 20, b"", "too_long", MAX_BODY_BYTES // CHUNK + 1),
    (MAX_BODY_BYTES // CHUNK, b"", "invalid_format", MAX_BODY_BYTES // CHUNK + 1),
], ids=["um-byte-acima", "muito-acima", "exatamente-o-teto"])
def test_streamed_body_is_capped_by_the_counter(client: Any, sql: psycopg.Connection, chunks: int, tail: bytes,
                                                reason: str, max_receives: int) -> None:
    """O corpo em pedaços de 1 MiB, sem `Content-Length`: um byte acima do teto, ou muito acima → 400
    `[{body, too_long}]`, parando de ler no primeiro pedaço que passa do teto; exatamente o teto (só espaços) é
    lido inteiro e recusado como JSON inválido, `[{body, invalid_format}]`. Nada guardado. Vermelho no esqueleto:
    500."""
    reply = _call(client, _headers(), _repeated(b" " * CHUNK, chunks, tail=tail))

    assert reply.error is None
    assert_error(reply, 400, "invalid_request", fields=[("body", reason)])
    assert reply.receives <= max_receives, f"a api leu {reply.receives} mensagens depois do teto"
    _assert_nothing_stored(sql)


def test_client_disconnect_mid_body_answers_no_500_and_logs_no_exception(client: Any,
                                                                          sql: psycopg.Connection) -> None:
    """A desconexão do cliente no meio da leitura do corpo: nenhuma exceção escapa do app, nenhuma resposta 500,
    nenhum registro de exceção no registro da api, nada guardado e nenhum evento. Vermelho no esqueleto: a rota
    lança `NotImplementedError` e o middleware responde 500 com o registro da exceção."""
    body = _json_bytes(_body("banco-desconexao", _course()))
    with capture_logs() as captured:
        reply = _call(client, _headers(length=str(len(body))),
                      _messages([body[: len(body) // 3]], then_disconnect=True))

    assert reply.error is None, f"exceção escapou do app: {type(reply.error).__name__}"
    assert reply.status_code != 500, "a desconexão do cliente virou 500"
    assert reply.receives >= 2, "a api não chegou a ler o corpo"
    assert captured.with_event("exception") == [], "registro de exceção na desconexão do cliente"
    assert all("exc_type" not in r for r in captured.records)
    _assert_nothing_stored(sql)


# --------------------------------------------------------------------------------------------------
# A ordem de validação
# --------------------------------------------------------------------------------------------------


def test_validation_follows_the_contract_order(client: Any, sql: psycopg.Connection) -> None:
    """A ordem do contrato: o cabeçalho `X-CQ-Client` e a consulta antes da permissão; a permissão (403) antes de
    ler o corpo, mesmo com o corpo acima do teto; o teto antes do JSON; o JSON antes dos campos; os campos antes do
    pacote; o pacote antes da versão; e, com tudo certo, a gravação (200). Nada guardado antes do último passo.
    Vermelho no esqueleto: 500 desde o primeiro pedido."""
    files = _course()
    no_write, _ = create_token(client, ADMIN_TOKEN, [p for p in ALL_PERMISSIONS if p != "offerings:write"],
                               label="sem escrita")
    broken = b'{"name": "sentinela-json-quebrado"'

    resp = client.post(BANKS_PATH, content=broken,
                       headers={**auth(no_write), "X-CQ-Client": "CLI", "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=[("X-CQ-Client", "invalid_value")])

    resp = client.post(BANKS_PATH, content=broken, params={"parametro_sentinela": "1"},
                       headers={**auth(no_write), "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert_no_echo(resp, "parametro_sentinela")

    reply = _call(client, _headers(no_write, length=str(MAX_BODY_BYTES + 1)), _no_body)
    assert_error(reply, 403, "forbidden", permission="offerings:write")
    assert reply.receives == 0, "a api leu o corpo antes de conferir a permissão"

    reply = _call(client, _headers(length=str(MAX_BODY_BYTES + 1)), _no_body)
    assert_error(reply, 400, "invalid_request", fields=[("body", "too_long")])

    assert_error(_post_raw(client, broken), 400, "invalid_request", fields=[("body", "invalid_format")])

    not_gzip = base64.b64encode(b"sentinela-nao-e-gzip").decode("ascii")
    assert_error(_post(client, {**_body("ab", files), "archive": not_gzip}), 400, "invalid_request",
                 fields=[("name", "too_short")])

    wrong_version = bk.reference_version(SMALL)
    assert_error(_post(client, {**_body("banco-ordem", files, version=wrong_version), "archive": "@@@@"}), 400,
                 "invalid_request", fields=[("archive", "invalid_format")])
    assert_error(_post(client, _body("banco-ordem", files, version=wrong_version,
                                     archive=bk.archive_of(files, gzip=False))), 400,
                 "invalid_request", fields=[("archive", "invalid_format")])
    assert_error(_post(client, _body("banco-ordem", files, version=wrong_version)), 400, "invalid_request",
                 fields=[("version", "mismatch")])
    _assert_nothing_stored(sql)

    assert _ok(_post(client, _body("banco-ordem", files)))["created"] is True


# --------------------------------------------------------------------------------------------------
# Os campos
# --------------------------------------------------------------------------------------------------


def _drop(key: str) -> Callable[[dict], dict]:
    return lambda body: {k: v for k, v in body.items() if k != key}


def _set(**changes: Any) -> Callable[[dict], dict]:
    return lambda body: {**body, **changes}


FIELD_CASES = [
    pytest.param(_drop("name"), [("name", "missing")], (), id="nome-ausente"),
    pytest.param(_set(name=12345678), [("name", "invalid_format")], (), id="nome-numero"),
    pytest.param(_set(name=None), [("name", "invalid_format")], (), id="nome-nulo"),
    pytest.param(_set(name="ab"), [("name", "too_short")], (), id="nome-curto"),
    pytest.param(_set(name="b" * 41), [("name", "too_long")], ("b" * 41,), id="nome-longo"),
    pytest.param(_set(name="Sentinela-Maiuscula"), [("name", "invalid_format")], ("Sentinela-Maiuscula",),
                 id="nome-maiuscula"),
    pytest.param(_set(name="-sentinela-hifen"), [("name", "invalid_format")], ("-sentinela-hifen",),
                 id="nome-comeca-com-hifen"),
    pytest.param(_set(name="sentinela_sublinhado"), [("name", "invalid_format")], ("sentinela_sublinhado",),
                 id="nome-sublinhado"),
    pytest.param(_drop("version"), [("version", "missing")], (), id="versao-ausente"),
    pytest.param(_set(version="f" * 63), [("version", "invalid_format")], (), id="versao-63"),
    pytest.param(_set(version="F" * 64), [("version", "invalid_format")], (), id="versao-maiuscula"),
    pytest.param(_set(version="sentinela-versao"), [("version", "invalid_format")], ("sentinela-versao",),
                 id="versao-texto"),
    pytest.param(_set(version=7), [("version", "invalid_format")], (), id="versao-numero"),
    pytest.param(_drop("source"), [("source", "missing")], (), id="origem-ausente"),
    pytest.param(_set(source="sentinela-origem"), [("source", "invalid_value")], ("sentinela-origem",),
                 id="origem-desconhecida"),
    pytest.param(lambda b: {**_drop("git_commit")(b), "source": "git"}, [("git_commit", "missing")], (),
                 id="git-sem-commit"),
    pytest.param(_set(source="git", git_commit="sentinela-commit"), [("git_commit", "invalid_format")],
                 ("sentinela-commit",), id="git-commit-texto"),
    pytest.param(_set(source="git", git_commit="e" * 39), [("git_commit", "invalid_format")], (),
                 id="git-commit-39"),
    pytest.param(_set(source="git", git_commit="E" * 40), [("git_commit", "invalid_format")], (),
                 id="git-commit-maiuscula"),
    pytest.param(_set(git_commit=GIT_COMMIT_40), [("git_commit", "invalid_value")], (), id="pasta-com-commit"),
    pytest.param(_drop("archive"), [("archive", "missing")], (), id="pacote-ausente"),
    pytest.param(_set(archive=12345), [("archive", "invalid_format")], (), id="pacote-numero"),
    pytest.param(_set(archive="@@@@sentinela"), [("archive", "invalid_format")], ("@@@@sentinela",),
                 id="pacote-nao-base64"),
    pytest.param(_set(archive="QUJD\nREVG"), [("archive", "invalid_format")], (), id="pacote-quebra-de-linha"),
    pytest.param(_set(archive="QUJDREVGRw"), [("archive", "invalid_format")], (), id="pacote-sem-preenchimento"),
    pytest.param(_set(archive="QQ==QUJD"), [("archive", "invalid_format")], (), id="pacote-preenchimento-no-meio"),
    pytest.param(_set(archive="-_-_-_-_"), [("archive", "invalid_format")], (), id="pacote-base64-de-url"),
    pytest.param(_set(campo_sentinela="valor-sentinela"), [("body", "unknown_field")],
                 ("campo_sentinela", "valor-sentinela"), id="campo-desconhecido"),
]


@pytest.mark.parametrize(("change", "fields", "echo"), FIELD_CASES)
def test_each_field_rule_is_refused_with_its_reason_and_no_echo(
        client: Any, sql: psycopg.Connection, change: Callable[[dict], dict], fields: list[tuple[str, str]],
        echo: tuple[str, ...]) -> None:
    """Cada campo do pedido fora da regra do contrato → 400 com o par `(campo, motivo)` exato, sem eco do valor e
    sem nada guardado; o corpo válido de controle passa. Vermelho no esqueleto: 500."""
    files = _course()
    resp = _post(client, change(_body("banco-campos", files)))
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *echo)
    _assert_nothing_stored(sql)

    assert _ok(_post(client, _body("banco-campos", files)))["created"] is True


@pytest.mark.parametrize("content", [b"sentinela-nao-json", b'["sentinela-lista"]', b'"sentinela-texto"',
                                     b"null", b"   "],
                         ids=["texto", "lista", "texto-json", "nulo", "espacos"])
def test_body_that_is_not_a_json_object_is_refused(client: Any, sql: psycopg.Connection, content: bytes) -> None:
    """O corpo que não é um objeto JSON → 400 `[{body, invalid_format}]`, sem eco e sem nada guardado. Vermelho
    no esqueleto: 500."""
    resp = _post_raw(client, content)
    assert_error(resp, 400, "invalid_request", fields=[("body", "invalid_format")])
    assert_no_echo(resp, "sentinela-nao-json", "sentinela-lista", "sentinela-texto")
    _assert_nothing_stored(sql)


def test_several_invalid_fields_come_in_schema_order(client: Any, sql: psycopg.Connection) -> None:
    """Vários campos inválidos no mesmo pedido: um motivo por campo, na ordem do esquema (nome, versão, origem,
    commit git, pacote). Vermelho no esqueleto: 500."""
    body = {"archive": "@@@@", "git_commit": GIT_COMMIT_40, "source": "svn", "name": "ab"}
    resp = _post(client, body)
    assert_error(resp, 400, "invalid_request", fields=[("name", "too_short"), ("version", "missing"),
                                                       ("source", "invalid_value"), ("archive", "invalid_format")])
    _assert_nothing_stored(sql)


def test_decoded_archive_above_the_cap_is_too_long(client: Any, sql: psycopg.Connection) -> None:
    """O pacote que, decodificado, passa um byte do teto (128 MiB), dentro do teto do corpo → 400
    `[{archive, too_long}]`, sem nada guardado. Vermelho no esqueleto: 500."""
    groups = (MAX_ARCHIVE_BYTES + 1) // 3
    assert groups * 3 == MAX_ARCHIVE_BYTES + 1
    prefix = json.dumps({"name": "banco-grande", "version": SMALL_VERSION, "source": "folder",
                         "git_commit": None})[:-1].encode("ascii")
    content = prefix + b', "archive": "' + b"AAAA" * groups + b'"}'
    assert len(content) < MAX_BODY_BYTES
    resp = _post_raw(client, content)
    del content
    assert_error(resp, 400, "invalid_request", fields=[("archive", "too_long")])
    _assert_nothing_stored(sql)


# --------------------------------------------------------------------------------------------------
# As regras do pacote
# --------------------------------------------------------------------------------------------------


def _with_info(*members: bk.Member, **options: Any) -> bytes:
    return bk.make_archive([INFO, *members], **options)


def _type_member(kind: bytes) -> bk.Member:
    if kind in (b"1", b"2"):
        return bk.Member("sentinela-ligacao", type=kind, linkname="infoCourse.json")
    if kind == b"g":
        return bk.Member("sentinela-global", bk.pax_records([("comment", "x")]), type=kind)
    if kind in (b"L", b"K"):
        return bk.Member(bk.GNU_LONG_NAME, b"sentinela-nome-longo\0", type=kind)
    if kind == b"5":
        return bk.Member("sentinela-diretorio/", type=kind, mode=0o755)
    return bk.Member("sentinela-especial", type=kind)


def _long_path(total: int) -> str:
    parts, length = [], 0
    while length < total:
        size = min(255, total - length - (1 if parts else 0))
        if size <= 0:
            break
        parts.append("d" * size)
        length = len("/".join(parts))
    assert len("/".join(parts).encode("utf-8")) == total
    return "/".join(parts)


ARCHIVE_CASES: list[Any] = [
    # o formato (gzip, tar e pax)
    pytest.param(lambda: bk.archive_of(SMALL, gzip=False), "invalid_format", id="nao-gzip"),
    pytest.param(lambda: bk.corrupt_deflate(bk.archive_of(SMALL)), "invalid_format", id="deflate-corrompido"),
    pytest.param(lambda: bk.archive_of(SMALL, extra_gzip_member=b"sentinela-segundo"), "invalid_format",
                 id="dois-membros-gzip"),
    pytest.param(lambda: bk.archive_of(SMALL, after_gzip=b"\0\0\0\0"), "invalid_format", id="bytes-depois-do-gzip"),
    pytest.param(lambda: bk.archive_of(SMALL, truncate=600), "invalid_format", id="tar-truncado"),
    pytest.param(lambda: bk.archive_of(SMALL, end_blocks=0, pad_record=False), "invalid_format",
                 id="sem-blocos-finais"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-soma.txt", b"x", checksum_delta=1)), "invalid_format",
                 id="soma-errada"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-gnu.txt", b"x", magic=bk.GNU_MAGIC)), "invalid_format",
                 id="magic-nao-posix"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-b256.txt", b"xy", size_field=bk.base256(2))),
                 "invalid_format", id="tamanho-base-256"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-size.txt", b"xy", pax=(("size", "xyz"),))),
                 "invalid_format", id="pax-size-xyz"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-pax.txt", b"x", pax_raw=b"99 path=x\n")),
                 "invalid_format", id="pax-registro-malformado"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-a.txt", b"x",
                                               pax=(("path", "sentinela-a.txt"), ("path", "sentinela-b.txt")))),
                 "invalid_format", id="pax-chave-repetida"),
    pytest.param(lambda: _with_info(bk.Member(bk.PAX_HEADER_NAME, bk.pax_records([("path", "sentinela-a.txt")]),
                                              type=b"x"),
                                    bk.regular("sentinela-b.txt", b"x", pax=(("path", "sentinela-b.txt"),))),
                 "invalid_format", id="dois-x-seguidos"),
    pytest.param(lambda: _with_info(bk.Member(bk.PAX_HEADER_NAME, bk.pax_records([("path", "sentinela-a.txt")]),
                                              type=b"x")),
                 "invalid_format", id="x-sem-membro"),
    pytest.param(lambda: bk.archive_of(SMALL, trailer=b"\x01"), "invalid_format", id="byte-depois-do-fim"),
    # os tetos
    pytest.param(lambda: bk.content_bomb_archive(), "too_large", id="bomba-no-conteudo"),
    pytest.param(lambda: bk.header_bomb_archive(), "too_large", id="bomba-nos-cabecalhos"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-x.txt", b"x", pax=(("path", "sentinela-x.txt"),
                                                                             ("mtime", "0" * 8200)))),
                 "too_large", id="x-acima-de-8-kib"),
    pytest.param(lambda: bk.make_archive([INFO, *bk.many_files(20000)]), "too_many", id="20001-arquivos"),
    # os tipos de membro e as chaves do pax
    *[pytest.param(lambda kind=kind: _with_info(_type_member(kind)), "entry_not_allowed",
                   id=f"tipo-{kind.decode('ascii')}")
      for kind in (b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"S", b"g", b"L", b"K", b"Z")],
    *[pytest.param(lambda pair=pair: _with_info(bk.regular("sentinela-chave.txt", b"x", pax=(pair,))),
                   "entry_not_allowed", id=f"pax-{pair[0]}")
      for pair in (("GNU.sparse.map", "0,1"), ("GNU.sparse.size", "1"), ("GNU.sparse.major", "1"),
                   ("hdrcharset", "BINARY"), ("comment", "sentinela-comentario"))],
    # os caminhos
    *[pytest.param(lambda name=name: _with_info(bk.regular(name, b"x")), "path_not_allowed", id=label)
      for label, name in (("absoluto", "/sentinela-absoluto.txt"), ("pai", "../sentinela-fora.txt"),
                          ("pai-no-meio", "a/../sentinela-meio.txt"), ("ponto", "./sentinela-ponto.txt"),
                          ("componente-vazio", "a//sentinela-vazio.txt"), ("git", ".git/sentinela-config"),
                          ("git-caixa", "sub/.GIT/sentinela-config"), ("barra-invertida", "a\\sentinela.txt"),
                          ("controle-c0", "sentinela\x01controle.txt"), ("controle-del", "sentinela\x7fdel.txt"),
                          ("controle-c1", "sentinela\u0085c1.txt"), ("componente-256", "a/" + "d" * 256),
                          ("caminho-4097", _long_path(4097)))],
    pytest.param(lambda: bk.make_archive([INFO, bk.regular(b"sentinela-\xff.txt", b"x")], fmt=bk.USTAR_FORMAT),
                 "path_not_allowed", id="ustar-utf8-invalido"),
    pytest.param(lambda: _with_info(bk.regular("p.txt", b"x", pax=((b"path", b"sentinela-\xff.txt"),))),
                 "path_not_allowed", id="pax-utf8-invalido"),
    pytest.param(lambda: _with_info(bk.regular("p.txt", b"x", pax=((b"path", b"sentinela\x00nulo.txt"),))),
                 "path_not_allowed", id="pax-nulo"),
    pytest.param(lambda: _with_info(bk.regular("p.txt", b"x", pax=(("path", "sentinela-dir/"),))),
                 "path_not_allowed", id="pax-barra-final"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-a", b"x"), bk.regular("sentinela-a/b.txt", b"y")),
                 "path_not_allowed", id="arquivo-e-diretorio"),
    pytest.param(lambda: _with_info(bk.regular("sentinela-a/b.txt", b"y"), bk.regular("sentinela-a", b"x")),
                 "path_not_allowed", id="diretorio-e-arquivo"),
    # o repetido e o curso
    pytest.param(lambda: _with_info(bk.regular("sentinela-dup.txt", b"x"), bk.regular("sentinela-dup.txt", b"y")),
                 "duplicate", id="repetido"),
    pytest.param(lambda: _with_info(INFO), "duplicate", id="info-repetido"),
    pytest.param(lambda: bk.make_archive([bk.regular("questions/q/info.json", b"{}")]), "not_a_course",
                 id="sem-info-course"),
    pytest.param(lambda: bk.make_archive([bk.regular("sentinela-sub/infoCourse.json", b"{}")]), "not_a_course",
                 id="info-course-fora-da-raiz"),
    # a ordem operacional: o repetido e o fim do fluxo valem antes da falta do curso
    pytest.param(lambda: bk.make_archive([bk.regular("sentinela-dup.txt", b"x"),
                                          bk.regular("sentinela-dup.txt", b"y")]), "duplicate",
                 id="sem-curso-e-repetido"),
    pytest.param(lambda: bk.make_archive([bk.regular("questions/q/info.json", b"{}")], trailer=b"\x01"),
                 "invalid_format", id="sem-curso-e-byte-depois-do-fim"),
]


@pytest.mark.parametrize(("build", "reason"), ARCHIVE_CASES)
def test_each_archive_rule_is_refused_with_one_field_no_echo_and_clean_logs(
        client: Any, sql: psycopg.Connection, build: Callable[[], bytes], reason: str) -> None:
    """Cada regra do pacote pela API: 400 com o envelope inteiro e um único par `(archive, motivo)`, sem eco do
    nome do membro, nada guardado, nenhum evento, e o registro da api com um único `request` 400, sem registro de
    exceção e sem o texto de nenhuma exceção da leitura (o deflate corrompido, o `size=xyz` do pax e o nome ustar
    com `0xff` incluídos). Vermelho no esqueleto: 500."""
    archive = build()
    with capture_logs() as captured:
        resp = _post(client, bk.upload_body("banco-regras", SMALL_VERSION, "folder", None, archive))

    assert_error(resp, 400, "invalid_request", fields=[("archive", reason)])
    assert_no_echo(resp, "sentinela-", "sentinela-questao")
    _assert_nothing_stored(sql)
    _assert_clean_refusal_logs(captured, "sentinela-")


def test_valid_archive_with_another_version_is_a_version_mismatch(client: Any, sql: psycopg.Connection) -> None:
    """Um pacote válido com a versão de outro conteúdo → 400 `[{version, mismatch}]`, sem eco da versão declarada
    nem da recalculada, e nada guardado. Vermelho no esqueleto: 500."""
    files = _course()
    declared = SMALL_VERSION
    with capture_logs() as captured:
        resp = _post(client, _body("banco-versao", files, version=declared))
    assert_error(resp, 400, "invalid_request", fields=[("version", "mismatch")])
    assert_no_echo(resp, declared, bk.reference_version(files))
    _assert_nothing_stored(sql)
    _assert_clean_refusal_logs(captured, declared, bk.reference_version(files))


# --------------------------------------------------------------------------------------------------
# A listagem
# --------------------------------------------------------------------------------------------------


def _tree(seed: str) -> dict[str, bytes]:
    """Uma árvore mínima de curso, diferente para cada `seed`."""
    return {"infoCourse.json": b'{"name": "curso"}\n', f"questions/{seed}/info.json": seed.encode("ascii") + b"\n"}


def _ids(sql: psycopg.Connection) -> list[int]:
    return [row[0] for row in sql.execute("SELECT id FROM question_banks ORDER BY id").fetchall()]


def test_list_returns_the_versions_in_first_upload_order_without_created(
        client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """A listagem devolve as versões em ordem do primeiro envio (o `id` interno), com os campos do `BankVersion`
    sem `created`, e `next_cursor` igual ao `id` do último item; um reenvio não muda a ordem. Vermelho no
    esqueleto: 500."""
    uploads = [("banco-b", _tree("b1"), "folder", None), ("banco-a", _tree("a1"), "git", GIT_COMMIT_40),
               ("banco-b", _tree("b2"), "git", GIT_COMMIT_64), ("banco-c", _course(), "folder", None)]
    expected = []
    for name, files, source, git_commit in uploads:
        _ok(_post(client, _body(name, files, source=source, git_commit=git_commit)))
        expected.append(_version_body(name, files, source=source, git_commit=git_commit, created_at=clock.now()))
        clock.advance(10)
    _ok(_post(client, _body("banco-b", _tree("b1"))))

    page = _list_ok(client)
    assert page == {"items": expected, "next_cursor": _ids(sql)[-1]}
    assert all("created" not in item for item in page["items"])


def test_list_pages_with_after_and_limit(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """`after` e `limit` por `id`, com `next_cursor`: cinco versões em páginas de 2, 2 e 1, com cursores
    crescentes iguais ao `id` do último item; a página vazia devolve `next_cursor == after`; `limit=1` e
    `limit=500` valem. Vermelho no esqueleto: 500."""
    for index in range(5):
        _ok(_post(client, _body(f"banco-pagina-{index}", _tree(f"p{index}"))))
    ids = _ids(sql)
    all_items = _list_ok(client)["items"]
    assert [item["name"] for item in all_items] == [f"banco-pagina-{i}" for i in range(5)]

    after, pages = 0, []
    while True:
        page = _list_ok(client, after=after, limit=2)
        if not page["items"]:
            assert page == {"items": [], "next_cursor": after}
            break
        pages.append(page)
        after = page["next_cursor"]
    assert [[i["name"] for i in p["items"]] for p in pages] == [
        ["banco-pagina-0", "banco-pagina-1"], ["banco-pagina-2", "banco-pagina-3"], ["banco-pagina-4"]]
    assert [p["next_cursor"] for p in pages] == [ids[1], ids[3], ids[4]]
    assert _list_ok(client, limit=1) == {"items": all_items[:1], "next_cursor": ids[0]}
    assert _list_ok(client, after=ids[2], limit=500) == {"items": all_items[3:], "next_cursor": ids[4]}
    assert _list_ok(client, after=2**63 - 1) == {"items": [], "next_cursor": 2**63 - 1}


def test_list_default_limit_is_100(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Sem `limit`, a página tem 100 itens (de 101 versões guardadas) e `next_cursor` no 100º; com `limit=500`,
    vêm as 101. As linhas são gravadas direto na tabela (a forma da listagem não depende do pacote). Vermelho no
    esqueleto: 500."""
    now = clock.now()
    for index in range(101):
        sql.execute(
            "INSERT INTO question_banks (bank_uuid, name, version, source, git_commit, archive, archive_bytes, "
            "file_count, byte_count, question_count, actor_token_id, created_at) "
            "VALUES (gen_random_uuid(), %s, %s, 'folder', NULL, %s, 2, 1, 0, 0, NULL, %s)",
            (f"banco-limite-{index:03d}", f"{index:064x}", b"\x1f\x8b", now))
    ids = _ids(sql)

    page = _list_ok(client)
    assert len(page["items"]) == 100
    assert page["next_cursor"] == ids[99]
    assert [item["name"] for item in page["items"]] == [f"banco-limite-{i:03d}" for i in range(100)]
    assert len(_list_ok(client, limit=500)["items"]) == 101


def test_list_filters_by_the_exact_name(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """O filtro `name` traz só as versões do nome exato (nem prefixo, nem sufixo, nem caixa); um nome sem
    versões dá a página vazia com `next_cursor == after`. Vermelho no esqueleto: 500."""
    for name, seed in (("abc", "x1"), ("abcd", "x2"), ("xabc", "x3"), ("abc", "x4")):
        _ok(_post(client, _body(name, _tree(seed))))

    page = _list_ok(client, name="abc")
    assert [(i["name"], i["version"]) for i in page["items"]] == [
        ("abc", bk.reference_version(_tree("x1"))), ("abc", bk.reference_version(_tree("x4")))]
    assert _list_ok(client, name="sem-versoes") == {"items": [], "next_cursor": 0}


@pytest.mark.parametrize(("params", "fields", "echo"), [
    pytest.param({"name": "AB"}, [("name", "invalid_format")], (), id="nome-curto-maiusculo"),
    pytest.param({"name": "ab"}, [("name", "invalid_format")], (), id="nome-curto"),
    pytest.param({"name": "c" * 41}, [("name", "invalid_format")], ("c" * 41,), id="nome-longo"),
    pytest.param({"name": "Sentinela-Filtro"}, [("name", "invalid_format")], ("Sentinela-Filtro",),
                 id="nome-maiuscula"),
    pytest.param({"name": "bank:sentinela"}, [("name", "invalid_format")], ("bank:sentinela",), id="nome-repo"),
    pytest.param({"limit": "0"}, [("limit", "out_of_range")], (), id="limit-0"),
    pytest.param({"limit": "501"}, [("limit", "out_of_range")], (), id="limit-501"),
    pytest.param({"limit": "limite-sentinela"}, [("limit", "invalid_format")], ("limite-sentinela",),
                 id="limit-texto"),
    pytest.param({"after": "-1"}, [("after", "out_of_range")], (), id="after-negativo"),
    pytest.param({"after": str(2**63)}, [("after", "out_of_range")], (), id="after-acima"),
    pytest.param({"after": "cursor-sentinela"}, [("after", "invalid_format")], ("cursor-sentinela",),
                 id="after-texto"),
    pytest.param({"parametro_sentinela": "1"}, [("query", "unknown_field")], ("parametro_sentinela",),
                 id="parametro-desconhecido"),
])
def test_list_query_errors(client: Any, params: dict[str, str], fields: list[tuple[str, str]],
                           echo: tuple[str, ...]) -> None:
    """A consulta da listagem fora da regra → 400 com o par exato e sem eco: o nome fora do padrão (qualquer
    forma) é `invalid_format`; `limit` fora de 1 a 500 e `after` fora de 0 a 2⁶³−1 são `out_of_range`; texto é
    `invalid_format`. Vermelho no esqueleto: 500."""
    resp = _list(client, params=params)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *echo)


def test_list_with_a_body_is_refused(client: Any) -> None:
    """A listagem não recebe corpo: JSON, `{}` ou texto cru → 400 `[{body, unknown_field}]`, sem eco; sem corpo, a
    mesma listagem passa. Vermelho no esqueleto: 500."""
    for kwargs in ({"json": {"campo_sentinela": "valor-sentinela"}}, {"json": {}},
                   {"content": b"texto-sentinela-cru"}):
        resp = client.request("GET", BANKS_PATH, headers=auth(ADMIN_TOKEN), **kwargs)
        assert_error(resp, 400, "invalid_request", fields=[("body", "unknown_field")])
        assert_no_echo(resp, "campo_sentinela", "valor-sentinela", "texto-sentinela-cru")
    assert _list_ok(client) == {"items": [], "next_cursor": 0}


def test_list_requires_offerings_read(client: Any) -> None:
    """Sem `offerings:read` (um token com todas as outras permissões): 403 nomeando `offerings:read`; com só
    `offerings:read`, 200. Vermelho no esqueleto: 500."""
    others, _ = create_token(client, ADMIN_TOKEN, [p for p in ALL_PERMISSIONS if p != "offerings:read"],
                             label="todas menos a leitura")
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="so leitura")
    assert_error(_list(client, token=others), 403, "forbidden", permission="offerings:read")
    resp = _list(client, token=reader)
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("banks.list", 200, resp.json())


def test_bank_name_and_repo_are_redacted_in_upload_list_and_event(client: Any, sql: psycopg.Connection,
                                                                  clock: Any) -> None:
    """O nome do banco é texto do operador e sai redigido como o `repo` de um disparo: um nome igual à parte local
    do e-mail de uma pessoa cadastrada e um nome igual ao RA dela saem como a marca da pessoa no envio (`name` e
    `repo`), na listagem e no `bank` do evento `bank.uploaded`; um nome neutro sai literal (controle). A tabela
    guarda o nome literal. Vermelho no esqueleto: 500."""
    person = register_person(client, ADMIN_TOKEN, name="Zuleica Prado Teixeira", ra="1234567",
                             email="banco-zuleica@example.org")
    shown = marker(person["alias"])
    cases = [("banco-zuleica", _tree("r1"), shown), ("1234567", _tree("r2"), shown),
             ("banco-neutro", _tree("r3"), "banco-neutro")]

    expected = []
    for name, files, display in cases:
        body = _ok(_post(client, _body(name, files)))
        version = _version_body(name, files, source="folder", git_commit=None, created_at=clock.now(),
                                shown_name=display)
        assert body == {**version, "created": True}, name
        expected.append(version)

    assert _list_ok(client)["items"] == expected
    assert [e["data"]["bank"] for e in _bank_events(client)] == [shown, shown, "banco-neutro"]
    assert [r["name"] for r in _rows(sql)] == ["banco-zuleica", "1234567", "banco-neutro"]
    listed = json.dumps(_list_ok(client), ensure_ascii=False) + json.dumps(_bank_events(client), ensure_ascii=False)
    for value in ("banco-zuleica", "1234567", "Zuleica"):
        assert value not in listed, f"{value!r} sem redação"
