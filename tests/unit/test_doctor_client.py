"""Composição do `doctor` no cliente.


`cq_client.doctor`, sem Click e sem banco:
- `collect_api_checks(api, profile)` com uma API **falsa** (`httpx2.MockTransport` injetado no `ApiClient` por
  `http=`): inalcançável → `api.reachable` falha e as demais da API "não verificado"; `health` 503 por banco e por
  esquema → `database.*` falha conforme o corpo e as demais "não verificado", **sem chamar** `system.doctor`; sem
  token, 401, 403, 5xx e resposta inesperada → `api.access` falha com "sem token", "token recusado", "sem
  permissão" ou "erro da API", e a resposta da API **nunca** repassada; sucesso → as 4 do cliente "ok" e as do
  servidor como vieram;
- `collect_local_checks(stack, profile, …)` com o `FakeStack` (nível 1): Docker indisponível, Compose 2.26.1 →
  aviso, sem diretório, sem `.env`, cada serviço parado ou com falha, o worker rodando sem verificação de saúde →
  ok, imagem do motor ausente → falha e presente → ok com a versão no `detail` (os 12 primeiros
  hexadecimais depois de `sha256:`), API de outro projeto → `api.owner` falha; perfil
  remoto → todas "não se aplica";
- a matriz de isolamento das condições: cada condição muda só a sua verificação (comparando `(id, offering, result)` com o caso
  saudável; sem `clock.skew`, que não entra na parte do cliente);
- `exit_code`: 0 sem `failure`, 1 com ao menos uma.
- os corpos 200 de
  `system.doctor` fora do esquema e das regras → "erro da API", sem repassar nada; os de dentro, nos limites, passam;
  o `/health` 200 fora do esquema → API inalcançável, sem chamar a operação.
- (verificações do host e do backup, no piloto) os grupos
  de verificações: `SERVER_CHECK_IDS` com os 9 na ordem do servidor (as 6 originais e `backup.age`,
  `smtp.server`, `retention.overdue` no fim, como `cq_server.doctor.SERVER_CHECK_IDS`); `FIXED_CHECK_IDS` e
  `fixed_unverified_checks` não existem; `HOST_CHECK_IDS`
  (`postgres.volume`, `disk.space`, parte `local`) logo depois de `LOCAL_CHECK_IDS`, que não muda; `CHECK_IDS` com
  os 27 pela soma dos grupos. Os corpos do servidor levam as 6 globais (as 3 novas com as mensagens de
  `cq_server.doctor.DOCTOR_REASON_MESSAGES`); o validador recusa corpo sem uma das 6 e com uma global repetida. A
  recusa dos corpos fora do contrato tem, na mesma função, o controle positivo do corpo válido de referência
  (sem ele, um validador que recusasse todo corpo passaria no teste). Os casos
  completos das regras novas (`count`, `offering`, mensagens por motivo) ficam em `test_doctor_local_deploy.py`.


Escolhas deste teste ([I]; textos e regras fixados aqui para a implementação):
- as verificações do servidor saem **na ordem em que a API as devolve** (o cliente não reordena), com
  `part = "api"` e `detail = None`; quando não verificadas, sai **uma** linha por id do servidor, na ordem de
  `SERVER_CHECK_IDS`, com `offering = None` e `count = None`;
- o motivo de "não verificado" segue a primeira verificação que falhou: `api_unreachable` (API inalcançável),
  `database` (`health` 503) ou `api_access` (sem acesso à operação), das `NOT_VERIFIED_MESSAGES`;
- `health` 503 com `database = "unreachable"` → `database.reachable` falha e `database.schema` "não verificado"
  (`database`); com o banco no ar e a versão do esquema diferente da esperada → `database.reachable` ok e
  `database.schema` falha;
- na parte local, a dependência é: sem diretório (o `Stack` é `None`) → toda local "não verificado"
  (`stack_directory`), salvo `stack.directory` em falha; sem `.env` → os serviços e `api.owner` "não verificado"
  (`env_file`), sem nenhuma leitura de estado do Compose (nenhum `snapshot`; o prefixo do Compose
  sempre leva `--env-file`), com `docker.reachable`, `compose.version` e `engine.image` verificados (não usam o
  prefixo); Docker indisponível → `docker.reachable` falha e o que depende do Docker "não verificado"
  (`docker_unavailable`), com `stack.directory` e `stack.env_file` verificados; API inalcançável → `api.owner`
  "não verificado" (`api_unreachable`);
- `api.owner` segue a regra de pertencimento da pilha ao projeto: o instantâneo diz que a api é do projeto (`api_owned`), o
  serviço `api` está "ok" e a porta publicada é a do `api_url` do perfil; senão, falha.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from cq_client import doctor
from cq_client.api import ApiClient
from cq_client.doctor import (
    API_ACCESS_FAILURE_MESSAGES,
    API_CLIENT_CHECK_IDS,
    CHECK_IDS,
    CHECK_PARTS,
    CLIENT_MESSAGES,
    DOCTOR_RESULTS,
    HOST_CHECK_IDS,
    LOCAL_CHECK_IDS,
    NOT_APPLICABLE_REMOTE,
    NOT_VERIFIED_MESSAGES,
    PROFILE_CHECK_IDS,
    SERVER_CHECK_IDS,
    Check,
)
from cq_client.profile import EffectiveProfile
from cq_client.stack import DockerUnavailable, ServiceState, StackDirNotFound, StackSnapshot
from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

from tests.helpers.stack_fake import (
    ENGINE_IMAGE_ID,
    TEST_API_PORT,
    TEST_API_URL,
    FakeStack,
    foreign_api,
    healthy,
    port_mismatch,
    snapshot,
)

PROFILE_TOKEN = "cq_token-do-perfil-de-teste-" + "t" * 22
# Marca posta em toda resposta de erro da API falsa: nenhuma mensagem do `doctor` pode repeti-la.
API_SENTINEL = "resposta-da-api-sentinela-Rk42"
ENGINE_DETAIL = ENGINE_IMAGE_ID.removeprefix("sha256:")[:12]

LOCAL = EffectiveProfile(
    name="local", kind="local", api_url=TEST_API_URL, token=PROFILE_TOKEN, stack_dir=None, compose_project="cq",
    origins={"name": "env", "kind": "profile", "api_url": "profile", "token": "profile",
             "compose_project": "default"},
    named=True, config_path=pathlib.Path("perfil-de-teste.toml"))
REMOTE = dataclasses.replace(LOCAL, name="remoto", kind="remote", api_url="https://cq.example.org")

HEALTH_OK = {"status": "ok", "version": "0.1.0", "database": "ok", "schema_version": "0005",
             "expected_schema_version": "0005"}
HEALTH_DB_DOWN = {"status": "error", "version": "0.1.0", "database": "unreachable", "schema_version": None,
                  "expected_schema_version": "0005"}
HEALTH_SCHEMA_BEHIND = {"status": "error", "version": "0.1.0", "database": "ok", "schema_version": "0004",
                        "expected_schema_version": "0005"}

# O modelo da idade do backup (as horas inteiras no `count`).
BACKUP_AGE = DOCTOR_REASON_MESSAGES[("backup.age", "ok", "age")]

# Um corpo de `system.doctor` com cada resultado possível do servidor, em ordem própria (o cliente não reordena).
SERVER_CHECKS = [
    {"id": "bots.master_key", "result": "warning", "message": DOCTOR_MESSAGES[("bots.master_key", "warning")],
     "offering": None, "count": None},
    {"id": "tasks.overdue", "result": "warning", "message": DOCTOR_MESSAGES[("tasks.overdue", "warning")],
     "offering": None, "count": 3},
    {"id": "clock.skew", "result": "ok", "message": DOCTOR_MESSAGES[("clock.skew", "ok")], "offering": None,
     "count": None},
    # as 3 globais novas.
    {"id": "backup.age", "result": "warning", "message": BACKUP_AGE.format(hours=30), "offering": None,
     "count": 30},
    {"id": "smtp.server", "result": "failure", "message": DOCTOR_REASON_MESSAGES[("smtp.server", "failure", "auth")],
     "offering": None, "count": None},
    {"id": "retention.overdue", "result": "warning",
     "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "warning", "overdue")], "offering": "turma-velha",
     "count": 2},
    {"id": "bot.access", "result": "failure", "message": DOCTOR_MESSAGES[("bot.access", "failure")],
     "offering": "turma-a", "count": None},
    {"id": "bot.lease", "result": "warning", "message": DOCTOR_MESSAGES[("bot.lease", "warning")],
     "offering": "turma-b", "count": None},
    {"id": "bot.poll", "result": "ok", "message": DOCTOR_MESSAGES[("bot.poll", "ok")], "offering": "turma-b",
     "count": None},
]
DOCTOR_BODY = {"checked_at": "2031-01-01T00:00:00Z", "checks": SERVER_CHECKS}


# --- API falsa ------------------------------------------------------------------------------------------------


class FakeApi:
    """O transporte falso: `/health` e `/v1/system/doctor` com o status e o corpo dados (ou a falha de conexão);
    registra cada pedido."""

    def __init__(self, *, health: tuple[int, Any] = (200, HEALTH_OK),
                 doctor_response: tuple[int, Any] = (200, DOCTOR_BODY), unreachable: bool = False) -> None:
        self.health = health
        self.doctor_response = doctor_response
        self.unreachable = unreachable
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx2.ConnectError("conexão recusada", request=request)
        if request.url.path == "/health":
            status, body = self.health
        elif request.url.path == "/v1/system/doctor":
            status, body = self.doctor_response
        else:
            status, body = 404, {"error": {"code": "not_found", "message": API_SENTINEL}}
        if isinstance(body, (bytes, str)):
            content = body.encode("utf-8") if isinstance(body, str) else body
            return httpx2.Response(status, content=content, headers={"content-type": "text/html"})
        return httpx2.Response(status, json=body)

    def client(self, profile: EffectiveProfile) -> ApiClient:
        http = httpx2.Client(transport=httpx2.MockTransport(self.handler))
        return ApiClient(profile.api_url, profile.token, http=http)

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": f"{API_SENTINEL} {code}"}}


def _ok(check_id: str, **extra: Any) -> Check:
    return Check(check_id, CHECK_PARTS[check_id], "ok", CLIENT_MESSAGES[(check_id, "ok")], **extra)


def _result(check_id: str, result: str, **extra: Any) -> Check:
    return Check(check_id, CHECK_PARTS[check_id], result, CLIENT_MESSAGES[(check_id, result)], **extra)


def _not_verified(check_id: str, reason: str) -> Check:
    return Check(check_id, CHECK_PARTS[check_id], "not_verified", NOT_VERIFIED_MESSAGES[reason])


def _server_unverified(reason: str) -> list[Check]:
    return [Check(check_id, "api", "not_verified", NOT_VERIFIED_MESSAGES[reason]) for check_id in SERVER_CHECK_IDS]


def _server_passed(checks: list[dict]) -> list[Check]:
    return [Check(c["id"], "api", c["result"], c["message"], c["offering"], c["count"], None) for c in checks]


def _collect(fake: FakeApi, profile: EffectiveProfile = LOCAL) -> list[Check]:
    with fake.client(profile) as api:
        return doctor.collect_api_checks(api, profile)


def _assert_not_repeated(checks: list[Check]) -> None:
    for check in checks:
        for text in (check.message, check.detail or ""):
            assert API_SENTINEL not in text, f"resposta da API repassada em {check.id}: {text!r}"
            assert PROFILE_TOKEN not in text and "cq.example.org" not in text and str(TEST_API_PORT) not in text


# --- parte da API --------------------------------------------------------------------------------------------


def test_api_checks_success_passes_server_checks_through() -> None:
    """Sucesso: as 4 do cliente "ok" e as do servidor como vieram (ordem, `offering`, `count`), com `part =
    "api"`; o `health` vai sem autenticação e a operação com o token. As constantes (os ids na ordem
    fixada, as partes e os resultados) ficam aqui; o módulo não tem as reservadas "não verificado"
    (`FIXED_CHECK_IDS`, `fixed_unverified_checks`)."""
    assert CHECK_IDS == API_CLIENT_CHECK_IDS + SERVER_CHECK_IDS + LOCAL_CHECK_IDS + HOST_CHECK_IDS + PROFILE_CHECK_IDS
    assert API_CLIENT_CHECK_IDS == ("api.reachable", "database.reachable", "database.schema", "api.access")
    assert SERVER_CHECK_IDS == ("bots.master_key", "bot.access", "bot.poll", "bot.lease", "tasks.overdue",
                                "clock.skew", "backup.age", "smtp.server", "retention.overdue")
    assert not hasattr(doctor, "FIXED_CHECK_IDS") and not hasattr(doctor, "fixed_unverified_checks")
    assert LOCAL_CHECK_IDS == ("docker.reachable", "compose.version", "stack.directory", "stack.env_file",
                               "service.postgres", "service.api", "service.worker", "service.engine",
                               "engine.image", "api.owner")
    assert HOST_CHECK_IDS == ("postgres.volume", "disk.space")
    assert len(set(CHECK_IDS)) == len(CHECK_IDS) == 27
    assert DOCTOR_RESULTS == ("ok", "warning", "failure", "not_verified", "not_applicable")
    assert {CHECK_PARTS[i] for i in API_CLIENT_CHECK_IDS + SERVER_CHECK_IDS} == {"api"}
    assert {CHECK_PARTS[i] for i in LOCAL_CHECK_IDS + HOST_CHECK_IDS} == {"local"}
    assert {CHECK_PARTS[i] for i in PROFILE_CHECK_IDS} == {"profile"}
    assert set(CHECK_PARTS) == set(CHECK_IDS)

    fake = FakeApi()
    checks = _collect(fake)
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"), _ok("api.access"),
                      *_server_passed(SERVER_CHECKS)]
    assert fake.paths() == [("GET", "/health"), ("GET", "/v1/system/doctor")]
    health_request, doctor_request = fake.requests
    assert "authorization" not in {k.lower() for k in health_request.headers}
    assert doctor_request.headers["authorization"] == f"Bearer {PROFILE_TOKEN}"
    assert doctor_request.url.params.multi_items() == [] and doctor_request.content == b""
    _assert_not_repeated(checks)


def test_api_checks_success_with_all_server_checks_ok() -> None:
    """A API saudável sem oferecimentos: as 6 globais do servidor "ok", nenhuma `bot.*`."""
    server = [{"id": i, "result": "ok", "message": DOCTOR_MESSAGES[(i, "ok")], "offering": None,
               "count": 0 if i == "tasks.overdue" else None} for i in ("bots.master_key", "tasks.overdue",
                                                                    "clock.skew")]
    server += [{"id": "backup.age", "result": "ok", "message": BACKUP_AGE.format(hours=0), "offering": None,
                "count": 0},
               {"id": "smtp.server", "result": "ok", "message": DOCTOR_REASON_MESSAGES[("smtp.server", "ok", "ok")],
                "offering": None, "count": None},
               {"id": "retention.overdue", "result": "ok",
                "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")], "offering": None, "count": 0}]
    checks = _collect(FakeApi(doctor_response=(200, {"checked_at": "2031-01-01T00:00:00Z", "checks": server})))
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"), _ok("api.access"),
                      *_server_passed(server)]
    assert doctor.exit_code(checks) == 0


def test_api_unreachable_marks_rest_not_verified() -> None:
    """API inalcançável: `api.reachable` falha e as demais da API "não verificado" (`api_unreachable`)."""
    fake = FakeApi(unreachable=True)
    checks = _collect(fake)
    assert checks == [_result("api.reachable", "failure"),
                      _not_verified("database.reachable", "api_unreachable"),
                      _not_verified("database.schema", "api_unreachable"),
                      _not_verified("api.access", "api_unreachable"),
                      *_server_unverified("api_unreachable")]
    assert fake.paths() == [("GET", "/health")]
    assert doctor.exit_code(checks) == 1
    _assert_not_repeated(checks)


@pytest.mark.parametrize(("health", "database_checks"), [
    (HEALTH_DB_DOWN, [_result("database.reachable", "failure"), _not_verified("database.schema", "database")]),
    (HEALTH_SCHEMA_BEHIND, [_ok("database.reachable"), _result("database.schema", "failure")]),
], ids=["banco-fora", "migracao-faltando"])
def test_health_503_fails_database_without_calling_doctor(health: dict, database_checks: list[Check]) -> None:
    """`health` 503: o banco e as migrações pelo corpo do `health`; `api.access` e as do servidor "não
    verificado" (`database`: a autenticação também depende do banco), sem chamar a operação."""
    fake = FakeApi(health=(503, health))
    checks = _collect(fake)
    assert checks == [_ok("api.reachable"), *database_checks, _not_verified("api.access", "database"),
                      *_server_unverified("database")]
    assert fake.paths() == [("GET", "/health")], "o doctor chamou a operação com o health 503"
    assert doctor.exit_code(checks) == 1


def test_api_access_without_token() -> None:
    """Perfil sem token: banco e migrações pelo `health`, `api.access` falha "sem token", as do
    servidor "não verificado" (`api_access`); nunca com o 401 que a API daria."""
    profile = dataclasses.replace(LOCAL, token=None)
    fake = FakeApi(doctor_response=(401, _error("unauthenticated")))
    checks = _collect(fake, profile)
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"),
                      Check("api.access", "api", "failure", API_ACCESS_FAILURE_MESSAGES["no_token"]),
                      *_server_unverified("api_access")]
    assert all("authorization" not in {k.lower() for k in r.headers} for r in fake.requests)
    _assert_not_repeated(checks)


@pytest.mark.parametrize(("response", "reason"), [
    ((401, _error("unauthenticated")), "token_refused"),
    ((403, {"error": {"code": "forbidden", "message": API_SENTINEL, "permission": "offerings:read"}}), "forbidden"),
    ((500, _error("internal")), "api_error"),
    ((503, _error("internal")), "api_error"),
    ((502, f"<html>{API_SENTINEL}</html>"), "api_error"),
    ((200, f"<html>{API_SENTINEL}</html>"), "api_error"),
    ((404, _error("not_found")), "api_error"),
], ids=["401", "403", "500", "503", "502-html", "200-html", "404"])
def test_api_access_failures_have_fixed_reason_and_never_repeat_response(response: tuple[int, Any],
                                                                         reason: str) -> None:
    """401 → "token recusado", 403 → "sem permissão", 5xx e resposta inesperada → "erro da API"; a
    resposta da API (mensagem, permissão, HTML) nunca é repassada; as do servidor "não verificado"."""
    fake = FakeApi(doctor_response=response)
    checks = _collect(fake)
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"),
                      Check("api.access", "api", "failure", API_ACCESS_FAILURE_MESSAGES[reason]),
                      *_server_unverified("api_access")]
    assert fake.paths() == [("GET", "/health"), ("GET", "/v1/system/doctor")]
    assert doctor.exit_code(checks) == 1
    _assert_not_repeated(checks)
    assert "offerings:read" not in "".join(c.message for c in checks)


def test_api_checks_remote_profile_same_composition() -> None:
    """A parte da API vale com o perfil remoto: a mesma composição, sem o endereço nas mensagens."""
    fake = FakeApi()
    assert _collect(fake, REMOTE) == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"),
                                      _ok("api.access"), *_server_passed(SERVER_CHECKS)]
    assert {r.url.host for r in fake.requests} == {"cq.example.org"}


# --- corpo fora do contrato ---------------------------------------------------------------------------------------


def _server_item(check_id: str, result: str = "ok", **extra: Any) -> dict:
    """Um item válido de `system.doctor`: a mensagem fixa do servidor, `count` 0 em
    `tasks.overdue` "ok"; `extra` sobrescreve campos."""
    count = 0 if check_id == "tasks.overdue" and result == "ok" else None
    return {"id": check_id, "result": result, "message": DOCTOR_MESSAGES[(check_id, result)], "offering": None,
            "count": count, **extra}


# as 6 globais (as 3 novas "ok") e uma `bot.*`.
VALID_SERVER = [_server_item("bots.master_key"), _server_item("tasks.overdue"), _server_item("clock.skew"),
                {"id": "backup.age", "result": "ok", "message": BACKUP_AGE.format(hours=5), "offering": None,
                 "count": 5},
                {"id": "smtp.server", "result": "ok", "message": DOCTOR_REASON_MESSAGES[("smtp.server", "ok", "ok")],
                 "offering": None, "count": None},
                {"id": "retention.overdue", "result": "ok",
                 "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")], "offering": None, "count": 0},
                _server_item("bot.access", offering="turma-a")]


def _doctor_body(checks: list[dict], **extra: Any) -> dict:
    return {"checked_at": "2031-01-01T00:00:00Z", "checks": checks, **extra}


def _without(item: dict, key: str) -> dict:
    return {k: v for k, v in item.items() if k != key}


# Dígitos que não são ASCII (árabe-índicos): o esquema só aceita `[0-9]`.
NON_ASCII_YEAR = "".join(chr(0x0660 + digit) for digit in (2, 0, 3, 1))
GLOBAL, OVERDUE, CLOCK, BACKUP, SMTP, RETENTION, ACCESS = VALID_SERVER
# as 3 globais novas, que todo corpo leva (salvo onde o defeito é a falta ou a repetição delas).
NEW = [BACKUP, SMTP, RETENTION]

# Corpos 200 de `system.doctor` fora do que a API pode devolver (esquema `system.doctor.response`, a tabela de
# mensagens fixas e as demais regras). Vários levam a sentinela, que nunca pode sair no relatório.
OUT_OF_CONTRACT_BODIES = {
    "lista-vazia": _doctor_body([]),
    "sem-uma-global": _doctor_body([OVERDUE, CLOCK, *NEW, ACCESS]),
    "global-repetida": _doctor_body([*VALID_SERVER, GLOBAL]),
    "bot-repetido-no-mesmo-oferecimento": _doctor_body([*VALID_SERVER, ACCESS]),
    "mensagem-livre": _doctor_body([dict(GLOBAL, message=API_SENTINEL), OVERDUE, CLOCK, *NEW, ACCESS]),
    "mensagem-nula": _doctor_body([dict(GLOBAL, message=None), OVERDUE, CLOCK, *NEW, ACCESS]),
    "par-fora-da-tabela-com-mensagem-nula": _doctor_body(
        [*VALID_SERVER, {"id": "x", "result": "ok", "message": None, "offering": None, "count": None}]),
    "resultado-fora-da-tabela-com-mensagem-nula": _doctor_body(
        [dict(GLOBAL, result="failure", message=None), OVERDUE, CLOCK, *NEW, ACCESS]),
    "id-do-cliente-com-mensagem-nula": _doctor_body(
        [*VALID_SERVER, {"id": "api.access", "result": "ok", "message": None, "offering": None, "count": None}]),
    "resultado-do-cliente": _doctor_body(
        [dict(GLOBAL, result="not_verified", message=API_SENTINEL), OVERDUE, CLOCK, *NEW, ACCESS]),
    "id-nao-textual": _doctor_body([dict(GLOBAL, id=["bots.master_key"]), OVERDUE, CLOCK, *NEW, ACCESS]),
    "offering-na-global": _doctor_body([dict(GLOBAL, offering="turma-a"), OVERDUE, CLOCK, *NEW, ACCESS]),
    "bot-sem-offering": _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, offering=None)]),
    "offering-fora-do-padrao": _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, offering=API_SENTINEL)]),
    "count-negativo": _doctor_body([GLOBAL, dict(OVERDUE, count=-1), CLOCK, *NEW, ACCESS]),
    "count-booleano": _doctor_body([GLOBAL, dict(OVERDUE, count=False), CLOCK, *NEW, ACCESS]),
    "count-fora-de-tasks-overdue": _doctor_body([dict(GLOBAL, count=1), OVERDUE, CLOCK, *NEW, ACCESS]),
    "count-numa-bot": _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, count=1)]),
    "tasks-overdue-ok-com-count-nulo": _doctor_body([GLOBAL, dict(OVERDUE, count=None), CLOCK, *NEW, ACCESS]),
    "mensagem-fixa-de-outro-par": _doctor_body(
        [GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, message=DOCTOR_MESSAGES[("bot.poll", "ok")])]),
    "par-fora-da-tabela-com-mensagem-da-tabela": _doctor_body(
        [dict(GLOBAL, result="failure"), OVERDUE, CLOCK, *NEW, ACCESS]),
    "offering-curto-demais": _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, offering="tu")]),
    "offering-longo-demais": _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, offering="t" * 41)]),
    "ok-com-tarefas-atrasadas": _doctor_body([GLOBAL, dict(OVERDUE, count=2), CLOCK, *NEW, ACCESS]),
    "aviso-sem-tarefas-atrasadas": _doctor_body(
        [GLOBAL, _server_item("tasks.overdue", "warning", count=0), CLOCK, *NEW, ACCESS]),
    "chave-a-mais-no-item": _doctor_body([dict(GLOBAL, extra=API_SENTINEL), OVERDUE, CLOCK, *NEW, ACCESS]),
    "chave-a-menos-no-item": _doctor_body([_without(GLOBAL, "count"), OVERDUE, CLOCK, *NEW, ACCESS]),
    "chave-a-mais-no-corpo": _doctor_body(VALID_SERVER, extra=API_SENTINEL),
    "sem-checked-at": {"checks": VALID_SERVER},
    "checked-at-com-sete-casas": {"checked_at": "2031-01-01T00:00:00.1234567Z", "checks": VALID_SERVER},
    "checked-at-fora-do-formato": {"checked_at": API_SENTINEL, "checks": VALID_SERVER},
    "checked-at-com-digitos-nao-ascii": {"checked_at": NON_ASCII_YEAR + "-01-01T00:00:00Z", "checks": VALID_SERVER},
    "checks-nao-lista": _doctor_body({"0": GLOBAL}),
    # Cada uma das 3 globais novas exatamente uma vez.
    "sem-backup-age": _doctor_body([GLOBAL, OVERDUE, CLOCK, SMTP, RETENTION, ACCESS]),
    "sem-smtp-server": _doctor_body([GLOBAL, OVERDUE, CLOCK, BACKUP, RETENTION, ACCESS]),
    "sem-retention-overdue": _doctor_body([GLOBAL, OVERDUE, CLOCK, BACKUP, SMTP, ACCESS]),
    "backup-age-repetida": _doctor_body([*VALID_SERVER, BACKUP]),
    "smtp-server-repetida": _doctor_body([*VALID_SERVER, SMTP]),
    "retention-overdue-repetida": _doctor_body([*VALID_SERVER, RETENTION]),
}


@pytest.mark.parametrize("body", list(OUT_OF_CONTRACT_BODIES.values()), ids=list(OUT_OF_CONTRACT_BODIES))
def test_server_body_out_of_contract_is_api_error(body: dict) -> None:
    """Um 200 de `system.doctor` fora do esquema e das regras vira "erro da API",
    com as do servidor "não verificado" e código 1: nenhuma lista incompleta passa por saudável, e nada do corpo
    (mensagem, `offering`, chave a mais) é repassado. Nenhum corpo derruba o `doctor` com exceção.

    O controle positivo, na mesma função, mostra que o corpo válido de referência (as 6 globais
    e uma `bot.*`) passa; assim a recusa vem do defeito de cada corpo, e não de recusar tudo."""
    reference = _collect(FakeApi(doctor_response=(200, _doctor_body(VALID_SERVER))))
    assert reference == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"), _ok("api.access"),
                         *_server_passed(VALID_SERVER)], "controle positivo: o corpo válido de referência foi recusado"
    fake = FakeApi(doctor_response=(200, body))
    checks = _collect(fake)
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"),
                      Check("api.access", "api", "failure", API_ACCESS_FAILURE_MESSAGES["api_error"]),
                      *_server_unverified("api_access")]
    assert doctor.exit_code(checks) == 1
    _assert_not_repeated(checks)


@pytest.mark.parametrize("body", [
    _doctor_body(VALID_SERVER),
    {"checked_at": "2031-01-01T00:00:00.123456Z", "checks": VALID_SERVER},
    _doctor_body([GLOBAL, _server_item("tasks.overdue", "warning", count=2), CLOCK, *NEW, ACCESS]),
    _doctor_body([ACCESS, RETENTION, CLOCK, SMTP, OVERDUE, BACKUP, GLOBAL]),
    _doctor_body([GLOBAL, OVERDUE, CLOCK, *NEW, dict(ACCESS, offering="tur"), dict(ACCESS, offering="t" * 40)]),
], ids=["minimo", "checked-at-com-microssegundos", "aviso-com-tarefas-atrasadas", "ordem-propria",
        "offering-nos-limites"])
def test_server_body_within_contract_passes_through(body: dict) -> None:
    """Controle positivo da validação: corpos dentro do contrato, inclusive nos limites (microssegundos no
    `checked_at`, `count` positivo com "warning", a ordem da API), passam como vieram."""
    fake = FakeApi(doctor_response=(200, body))
    checks = _collect(fake)
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"), _ok("api.access"),
                      *_server_passed(body["checks"])]
    assert fake.paths() == [("GET", "/health"), ("GET", "/v1/system/doctor")]
    assert doctor.exit_code(checks) == 0


OUT_OF_SCHEMA_HEALTH = {
    "chave-a-mais": dict(HEALTH_OK, extra=API_SENTINEL),
    "chaves-a-menos": {"database": "ok", "schema_version": "0005", "expected_schema_version": "0005"},
    "versao-fora-do-formato": dict(HEALTH_OK, version=API_SENTINEL),
    "status-fora-do-enum": dict(HEALTH_OK, status=API_SENTINEL),
}


@pytest.mark.parametrize("health", list(OUT_OF_SCHEMA_HEALTH.values()), ids=list(OUT_OF_SCHEMA_HEALTH))
def test_health_body_out_of_schema_counts_as_unreachable(health: dict) -> None:
    """Um `/health` 200 fora do esquema `system.health.response` não confirma que ali está a API do sistema: conta
    como API inalcançável, como o corpo sem JSON (a mesma validação do `status`), sem chamar
    `system.doctor` e sem relatar o banco "ok"."""
    fake = FakeApi(health=(200, health))
    checks = _collect(fake)
    assert checks == [_result("api.reachable", "failure"),
                      _not_verified("database.reachable", "api_unreachable"),
                      _not_verified("database.schema", "api_unreachable"),
                      _not_verified("api.access", "api_unreachable"),
                      *_server_unverified("api_unreachable")]
    assert fake.paths() == [("GET", "/health")]
    assert doctor.exit_code(checks) == 1
    _assert_not_repeated(checks)


# --- parte local -----------------------------------------------------------------------------------------------


def _local(stack: Any, profile: EffectiveProfile = LOCAL, *, stack_dir_error: Any = None,
           env_file_present: bool | None = True, api_reachable: bool = True) -> list[Check]:
    return doctor.collect_local_checks(stack, profile, stack_dir_error=stack_dir_error,
                                       env_file_present=env_file_present, api_reachable=api_reachable)


HEALTHY_LOCAL = [
    _ok("docker.reachable"), _ok("compose.version"), _ok("stack.directory"), _ok("stack.env_file"),
    _ok("service.postgres"), _ok("service.api"), _ok("service.worker"), _ok("service.engine"),
    _ok("engine.image", detail=ENGINE_DETAIL), _ok("api.owner"),
]


def _with_service(base: StackSnapshot, service: str, state: str, health: str) -> StackSnapshot:
    services = tuple(ServiceState(s.service, state, health) if s.service == service else s for s in base.services)
    return dataclasses.replace(base, services=services)


def test_local_checks_healthy_stack_all_ok() -> None:
    """Pilha saudável: as dez locais "ok"; o worker rodando **sem** verificação de saúde conta como ok;
    a versão da imagem do motor no `detail`."""
    stack = FakeStack([healthy()])
    assert snapshot("healthy").services[2] == ServiceState("worker", "running", "none")
    checks = _local(stack)
    assert checks == HEALTHY_LOCAL
    assert doctor.exit_code(checks) == 0
    assert CLIENT_MESSAGES[("service.worker", "ok")] == "Serviço rodando."


def test_local_checks_remote_profile_not_applicable() -> None:
    """Perfil remoto: as dez locais "não se aplica (perfil remoto)", sem nenhum `Stack`."""
    checks = _local(None, REMOTE, env_file_present=None)
    assert checks == [Check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE) for i in LOCAL_CHECK_IDS]
    assert doctor.exit_code(checks) == 0


def test_local_checks_docker_unavailable() -> None:
    """Docker indisponível: `docker.reachable` falha; o que depende do Docker "não verificado"; o diretório e o
    `.env` (só existência) continuam verificados."""
    checks = _local(FakeStack.unavailable())
    reason = "docker_unavailable"
    assert checks == [_result("docker.reachable", "failure"), _not_verified("compose.version", reason),
                      _ok("stack.directory"), _ok("stack.env_file"),
                      *(_not_verified(f"service.{s}", reason) for s in ("postgres", "api", "worker", "engine")),
                      _not_verified("engine.image", reason), _not_verified("api.owner", reason)]
    assert doctor.exit_code(checks) == 1


def test_local_checks_without_stack_directory() -> None:
    """Sem diretório da pilha (sem `Stack`): `stack.directory` falha e as demais locais "não verificado"."""
    checks = _local(None, stack_dir_error=StackDirNotFound(), env_file_present=None)
    assert checks == [*(_not_verified(i, "stack_directory") for i in LOCAL_CHECK_IDS[:2]),
                      _result("stack.directory", "failure"),
                      *(_not_verified(i, "stack_directory") for i in LOCAL_CHECK_IDS[3:])]
    assert doctor.exit_code(checks) == 1
    assert "docs/operacao" not in "".join(c.message for c in checks)


def test_local_checks_without_env_file_reads_no_compose_state() -> None:
    """Sem `.env`: `stack.env_file` falha; os serviços e `api.owner` "não verificado" (`env_file`), sem
    nenhum `snapshot` (o prefixo do Compose sempre leva `--env-file`); Docker, Compose e imagem verificados."""
    stack = FakeStack([healthy()])
    checks = _local(stack, env_file_present=False)
    assert checks == [_ok("docker.reachable"), _ok("compose.version"), _ok("stack.directory"),
                      _result("stack.env_file", "failure"),
                      *(_not_verified(f"service.{s}", "env_file") for s in ("postgres", "api", "worker", "engine")),
                      _ok("engine.image", detail=ENGINE_DETAIL), _not_verified("api.owner", "env_file")]
    assert "snapshot" not in stack.names() and "up" not in stack.names() and "down" not in stack.names()


def test_local_checks_api_unreachable_owner_not_verified() -> None:
    """API inalcançável: as locais rodam assim mesmo, e só `api.owner` fica "não verificado"."""
    checks = _local(FakeStack([healthy()]), api_reachable=False)
    assert checks == [*HEALTHY_LOCAL[:-1], _not_verified("api.owner", "api_unreachable")]


def test_local_checks_never_change_the_stack() -> None:
    """A parte local só lê: nada de `up`, `down` nem `logs`."""
    stack = FakeStack([healthy()])
    _local(stack)
    assert set(stack.names()) <= {"snapshot", "compose_version", "engine_image_id", "docker_available"}


def test_local_checks_foreign_api() -> None:
    """A API do endereço do perfil de outro projeto (a pilha do projeto ausente): os serviços falham e
    `api.owner` falha."""
    checks = _local(FakeStack([foreign_api()]))
    assert checks == [_ok("docker.reachable"), _ok("compose.version"), _ok("stack.directory"),
                      _ok("stack.env_file"),
                      *(_result(f"service.{s}", "failure") for s in ("postgres", "api", "worker", "engine")),
                      _ok("engine.image", detail=ENGINE_DETAIL), _result("api.owner", "failure")]


# Matriz de isolamento das condições: cada condição e o conjunto exato das verificações que ela muda (o resto igual ao saudável).
def _stack_for(condition: str) -> tuple[FakeStack, dict[str, str]]:
    base = healthy()
    cases: dict[str, tuple[Callable[[], FakeStack], dict[str, str]]] = {
        "compose-2.26.1": (lambda: FakeStack([base], compose_version=(2, 26, 1)), {"compose.version": "warning"}),
        "postgres-parado": (lambda: FakeStack([_with_service(base, "postgres", "stopped", "none")]),
                            {"service.postgres": "failure"}),
        "postgres-com-falha": (lambda: FakeStack([_with_service(base, "postgres", "running", "unhealthy")]),
                               {"service.postgres": "failure"}),
        "worker-parado": (lambda: FakeStack([_with_service(base, "worker", "stopped", "none")]),
                          {"service.worker": "failure"}),
        "worker-com-falha": (lambda: FakeStack([_with_service(base, "worker", "running", "unhealthy")]),
                             {"service.worker": "failure"}),
        "engine-parado": (lambda: FakeStack([_with_service(base, "engine", "stopped", "none")]),
                          {"service.engine": "failure"}),
        "engine-subindo": (lambda: FakeStack([_with_service(base, "engine", "running", "starting")]),
                           {"service.engine": "failure"}),
        "engine-ausente": (lambda: FakeStack([_with_service(base, "engine", "absent", "none")]),
                           {"service.engine": "failure"}),
        # A api parada ou com falha também tira o pertencimento (o pertencimento exige o contêiner `api` rodando e saudável).
        "api-parada": (lambda: FakeStack([_with_service(base, "api", "stopped", "none")]),
                       {"service.api": "failure", "api.owner": "failure"}),
        "api-com-falha": (lambda: FakeStack([_with_service(base, "api", "running", "unhealthy")]),
                          {"service.api": "failure", "api.owner": "failure"}),
        "imagem-do-motor-ausente": (lambda: FakeStack([base], engine_image_id=None), {"engine.image": "failure"}),
        "porta-divergente": (lambda: FakeStack([port_mismatch()]), {"api.owner": "failure"}),
        "api-de-outro-projeto": (lambda: FakeStack([dataclasses.replace(base, api_owned=False)]),
                                 {"api.owner": "failure"}),
    }
    make, changed = cases[condition]
    return make(), changed


MATRIX = ("compose-2.26.1", "postgres-parado", "postgres-com-falha", "worker-parado", "worker-com-falha",
          "engine-parado", "engine-subindo", "engine-ausente", "api-parada", "api-com-falha",
          "imagem-do-motor-ausente", "porta-divergente", "api-de-outro-projeto")


def _key(checks: list[Check]) -> list[tuple[str, str | None, str]]:
    return [(c.id, c.offering, c.result) for c in checks]


@pytest.mark.parametrize("condition", MATRIX)
def test_each_local_condition_changes_only_its_check(condition: str) -> None:
    """cada condição provocada muda só a(s) verificação(ões) correspondente(s); o resultado de
    `exit_code` acompanha (1 com uma falha, 0 só com avisos)."""
    stack, changed = _stack_for(condition)
    checks = _local(stack)
    expected = [(i, o, changed.get(i, r)) for i, o, r in _key(HEALTHY_LOCAL)]
    assert _key(checks) == expected
    for check in checks:
        if check.id in changed:
            assert check.message == CLIENT_MESSAGES[(check.id, check.result)]
    assert doctor.exit_code(checks) == (1 if "failure" in changed.values() else 0)
    if condition == "imagem-do-motor-ausente":
        assert [c.detail for c in checks if c.id == "engine.image"] == [None]


@pytest.mark.parametrize("server_condition", [
    ("bots.master_key", "warning", None, None),
    ("bot.access", "failure", "turma-a", None),
    ("bot.poll", "warning", "turma-a", None),
    ("bot.lease", "warning", "turma-a", None),
    ("tasks.overdue", "warning", None, 2),
    ("clock.skew", "warning", None, None),
], ids=lambda c: c[0])
def test_each_server_condition_changes_only_its_check(server_condition: tuple) -> None:
    """Isolamento das condições na parte da API: com a API saudável, cada condição que o servidor relata muda só a sua linha."""
    check_id, result, offering, count = server_condition
    healthy_server = [
        {"id": "bots.master_key", "result": "ok", "message": DOCTOR_MESSAGES[("bots.master_key", "ok")],
         "offering": None, "count": None},
        {"id": "tasks.overdue", "result": "ok", "message": DOCTOR_MESSAGES[("tasks.overdue", "ok")],
         "offering": None, "count": 0},
        {"id": "clock.skew", "result": "ok", "message": DOCTOR_MESSAGES[("clock.skew", "ok")], "offering": None,
         "count": None},
        # As 3 globais novas "ok"; os casos delas ficam em `test_doctor_local_deploy.py`.
        BACKUP, SMTP, RETENTION,
        *({"id": i, "result": "ok", "message": DOCTOR_MESSAGES[(i, "ok")], "offering": "turma-a", "count": None}
          for i in ("bot.access", "bot.lease", "bot.poll")),
    ]
    changed = [dict(c, result=result, message=DOCTOR_MESSAGES[(check_id, result)],
                    count=count if count is not None else c["count"])
               if c["id"] == check_id and c["offering"] == offering else c for c in healthy_server]
    base = _collect(FakeApi(doctor_response=(200, {"checked_at": "2031-01-01T00:00:00Z", "checks": healthy_server})))
    after = _collect(FakeApi(doctor_response=(200, {"checked_at": "2031-01-01T00:00:00Z", "checks": changed})))
    differing = [(a.id, a.offering, a.result) for a, b in zip(after, base) if (a.id, a.offering, a.result) != (
        b.id, b.offering, b.result)]
    assert differing == [(check_id, offering, result)]
    assert len(after) == len(base) == 4 + len(healthy_server)
    assert doctor.exit_code(after) == (1 if result == "failure" else 0)


# --- código de saída -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("results", "code"), [
    ((), 0),
    (("ok",), 0),
    (("ok", "warning", "not_verified", "not_applicable"), 0),
    (("failure",), 1),
    (("ok", "warning", "failure", "not_verified"), 1),
    (("failure", "failure"), 1),
])
def test_exit_code(results: tuple[str, ...], code: int) -> None:
    """0 sem `failure`; 1 com ao menos uma."""
    checks = [Check(CHECK_IDS[i], CHECK_PARTS[CHECK_IDS[i]], r, "mensagem") for i, r in enumerate(results)]
    assert doctor.exit_code(checks) == code


def test_docker_unavailable_type_is_stack_error() -> None:
    """O erro do Docker indisponível (real no esqueleto) junto de um caso da parte local."""
    assert DockerUnavailable().exit_code == 1
    assert _local(FakeStack.unavailable())[0] == _result("docker.reachable", "failure")
