"""Saúde e rotas fora de `/v1`.

`GET /health` não exige token, ignora consulta, corpo, `X-CQ-Client` e `Authorization` (a
autenticação só vale em `/v1/*`), e responde 200 só com o banco acessível e o esquema na versão
esperada; senão, 503 com o mesmo esquema.
"""

from __future__ import annotations

import importlib.metadata
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

import cq_server
from cq_server.migrate import EXPECTED_VERSION
from tests.helpers.api import ADMIN_TOKEN, auth, create_token
from tests.helpers.canary import find_secrets
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import all_secrets

# Senha da `unreachable_db_url` do conftest.
SENTINEL_PASSWORD = "senha-que-nao-pode-vazar"

# Token no formato de segredo que não existe em banco nenhum (46 caracteres).
UNKNOWN_BEARER = "cq_saude_token_desconhecido_" + "q" * 18
assert len(UNKNOWN_BEARER) >= 40


def _version() -> str:
    """Versão do sistema: o metadado do pacote `cq-server`, igual a `cq_server.__version__`."""
    version = importlib.metadata.version("cq-server")
    assert version == cq_server.__version__
    return version


def _health(status: str, database: str, schema_version: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "version": _version(),
        "database": database,
        "schema_version": schema_version,
        "expected_schema_version": EXPECTED_VERSION,
    }


def _request_records(captured: Any, **expected: Any) -> list[dict]:
    """Registros `request` do logger `cq_server.api` que incluem o subconjunto `expected`."""
    wanted = {"logger": "cq_server.api", "event": "request", **expected}
    return [record for record in captured.records if wanted.items() <= record.items()]


def test_health_ok_without_token(client: Any) -> None:
    """sem token → 200 e o corpo exato, com `version` = metadado de
    `cq-server` = `cq_server.__version__`; a requisição gera o registro `request`."""
    with capture_logs() as captured:
        resp = client.get("/health")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert_matches("system.health", 200, body)
    assert body == _health("ok", "ok", EXPECTED_VERSION)
    assert len(_request_records(captured, method="GET", route="/health", status=200)) == 1


def test_health_ok_without_admin_token_configured(app_factory: Any) -> None:
    """sem token inicial configurado, a saúde continua respondendo 200 sem token."""
    with TestClient(app_factory(admin_token=None), raise_server_exceptions=False) as c:
        resp = c.get("/health")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json() == _health("ok", "ok", EXPECTED_VERSION)


def test_health_ignores_query_and_client_header(client: Any) -> None:
    """`/health` ignora parâmetros de consulta e `X-CQ-Client` (inválido fora de
    `/v1`), e responde o mesmo 200."""
    expected = _health("ok", "ok", EXPECTED_VERSION)
    cases = [
        ({"extra_param_x": "1"}, {}),
        ({"types": "tipo-desconhecido", "limit": "abc", "after": "-1"}, {}),
        ({}, {"X-CQ-Client": "CLI"}),
        ({}, {"X-CQ-Client": "cliente-desconhecido"}),
        ({"extra_param_x": "1"}, {"X-CQ-Client": "CLI"}),
    ]
    for params, headers in cases:
        resp = client.get("/health", params=params, headers=headers)
        assert resp.status_code == 200, (params, headers, resp.text[:300])
        assert resp.json() == expected, (params, headers)


def test_health_empty_database_is_503(app_factory: Any, empty_db_url: str) -> None:
    """banco acessível sem esquema → 503 com `database: ok` e
    `schema_version: null`."""
    with TestClient(app_factory(database_url=empty_db_url), raise_server_exceptions=False) as c:
        resp = c.get("/health")

    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert_matches("system.health", 503, body)
    assert body == _health("error", "ok", None)


def test_health_unexpected_schema_version_is_503(client: Any, sql: Any) -> None:
    """com a versão `9999` gravada em `schema_migrations`, o esquema não
    está na versão esperada → 503 com `schema_version: "9999"`."""
    sql.execute("INSERT INTO schema_migrations (version, applied_at) VALUES ('9999', now())")

    resp = client.get("/health")

    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert_matches("system.health", 503, body)
    assert body == _health("error", "ok", "9999")


def test_health_unreachable_database_is_503_fast_without_password(
        app_factory: Any, unreachable_db_url: str) -> None:
    """Saúde sem banco (`create_app` sobe sem banco, `connect_timeout` de 3 s): banco
    inalcançável → 503 `{database: unreachable, schema_version: null}` em até 10 s, com a senha
    sentinela ausente do corpo, dos cabeçalhos e dos logs."""
    assert SENTINEL_PASSWORD in unreachable_db_url
    app = app_factory(database_url=unreachable_db_url)

    with capture_logs() as captured:
        started = time.monotonic()
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/health")
            elapsed = time.monotonic() - started

    assert elapsed < 10, f"a saúde levou {elapsed:.1f} s com o banco inalcançável"
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert_matches("system.health", 503, body)
    assert body == _health("error", "unreachable", None)
    assert_no_echo(resp, SENTINEL_PASSWORD)
    assert SENTINEL_PASSWORD not in captured.text
    assert SENTINEL_PASSWORD.casefold() not in captured.text.casefold()
    # Contra o vazio: a captura tem o registro da própria requisição.
    assert len(_request_records(captured, method="GET", route="/health", status=503)) == 1


def test_openapi_json_without_token(client: Any, app: Any) -> None:
    """`/openapi.json` sem token → 200 com o documento OpenAPI 3.1 da
    própria app, ignorando consulta e `X-CQ-Client`."""
    resp = client.get("/openapi.json")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"].startswith("application/json")
    document = resp.json()
    assert document["openapi"] == "3.1.0"
    assert document == json.loads(json.dumps(app.openapi()))

    again = client.get("/openapi.json", params={"extra_param_x": "1"}, headers={"X-CQ-Client": "CLI"})
    assert again.status_code == 200, again.text[:300]
    assert again.json() == document


# --- cabeçalho `Authorization` fora de /v1 -----------------------------------------------------


def _hdr_unknown(client: Any) -> tuple[dict, list[str]]:
    return {"Authorization": f"Bearer {UNKNOWN_BEARER}"}, [UNKNOWN_BEARER]


def _hdr_basic(client: Any) -> tuple[dict, list[str]]:
    return {"Authorization": "Basic eA=="}, []


def _hdr_bearer_empty(client: Any) -> tuple[dict, list[str]]:
    return {"Authorization": "Bearer "}, []


def _hdr_non_ascii(client: Any) -> tuple[dict, list[str]]:
    raw = "cq_" + "ç" * 43
    encoded = raw.encode("utf-8")
    return {"Authorization": b"Bearer " + encoded}, [raw, encoded.decode("latin-1")]


def _hdr_revoked(client: Any) -> tuple[dict, list[str]]:
    secret, body = create_token(client, ADMIN_TOKEN, ["people:read"], label="token revogado da saude")
    resp = client.post(f"/v1/tokens/{body['token_id']}/revoke", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    return auth(secret), [secret]


AUTH_HEADERS: dict[str, Callable[[Any], tuple[dict, list[str]]]] = {
    "bearer_unknown": _hdr_unknown,
    "basic": _hdr_basic,
    "bearer_empty": _hdr_bearer_empty,
    "non_ascii": _hdr_non_ascii,
    "bearer_revoked": _hdr_revoked,
}


@pytest.mark.parametrize("case", list(AUTH_HEADERS))
def test_authorization_header_is_ignored_outside_v1(client: Any, app: Any, case: str) -> None:
    """A saúde e a descrição da API não exigem token: fora de `/v1/`, nada exige token, e a
    autenticação só vale em `/v1/*`; contrato
    da saúde: com um `Authorization` desconhecido, `Basic`, `Bearer` vazio, com byte não ASCII ou de
    um token revogado, `GET /health` → o mesmo 200 exato, sem `WWW-Authenticate`; `GET
    /openapi.json` → o documento da app; `GET /nada` → 404 (não 401); `POST /health` → 405; sem eco
    do token nas respostas nem nos logs."""
    headers, sentinels = AUTH_HEADERS[case](client)

    with capture_logs() as captured:
        health = client.get("/health", headers=headers)
        document = client.get("/openapi.json", headers=headers)
        unknown = client.get("/nada", headers=headers)
        wrong_method = client.post("/health", headers=headers)

    assert health.status_code == 200, health.text[:300]
    assert_matches("system.health", 200, health.json())
    assert health.json() == _health("ok", "ok", EXPECTED_VERSION)
    assert document.status_code == 200, document.text[:300]
    assert document.json() == json.loads(json.dumps(app.openapi()))
    assert_error(unknown, 404, "not_found")
    assert_error(wrong_method, 405, "method_not_allowed")
    for resp in (health, document, unknown, wrong_method):
        assert "www-authenticate" not in resp.headers, (resp.request.method, resp.request.url.path)
        if sentinels:
            assert_no_echo(resp, *sentinels)

    assert len(_request_records(captured, method="GET", route="/health", status=200)) == 1
    assert len(_request_records(captured, method="GET", route=None, status=404)) == 1
    assert len(_request_records(captured, method="POST", route="/health", status=405)) == 1
    assert find_secrets(captured.text, all_secrets(*sentinels)) == [], "token nos logs"
    for value in sentinels:
        assert value not in captured.text


def test_health_unreachable_database_ignores_authorization(
        app_factory: Any, unreachable_db_url: str) -> None:
    """A saúde dá 503 quando o banco não responde: com o banco inalcançável
    e um `Bearer` desconhecido (que só o banco poderia conferir), `GET /health` → 503
    `{database: unreachable}`, e não 500 nem 401; sem eco do token nem da senha."""
    app = app_factory(database_url=unreachable_db_url)

    with capture_logs() as captured:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/health", headers=auth(UNKNOWN_BEARER, "cli"))

    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert_matches("system.health", 503, body)
    assert body == _health("error", "unreachable", None)
    assert "www-authenticate" not in resp.headers
    assert_no_echo(resp, UNKNOWN_BEARER, SENTINEL_PASSWORD)
    assert UNKNOWN_BEARER not in captured.text
    assert SENTINEL_PASSWORD not in captured.text
    assert len(_request_records(captured, method="GET", route="/health", status=503)) == 1


def test_docs_redoc_and_unknown_paths_are_not_found(client: Any) -> None:
    """Não há /docs nem /redoc, e fora de `/v1/` um caminho desconhecido dá 404; e o log
    (`route: null` quando nenhuma rota casa): 404 `not_found` no envelope exato, sem token."""
    paths = ("/docs", "/redoc", "/docs/oauth2-redirect", "/caminho-inexistente-xyz")
    with capture_logs() as captured:
        for path in paths:
            resp = client.get(path)
            assert_error(resp, 404, "not_found")

    assert len(_request_records(captured, method="GET", route=None, status=404)) == len(paths)


def test_health_wrong_method_is_405_and_body_is_ignored(client: Any) -> None:
    """Alcance, erros e log de uma rota com o método errado: `POST /health` (e `POST
    /openapi.json`) → 405 `method_not_allowed`, com `route` do modelo que casou; `GET /health` com
    corpo (JSON ou não) → o mesmo 200."""
    with capture_logs() as captured:
        assert_error(client.post("/health"), 405, "method_not_allowed")
        assert_error(client.post("/health", json={"status": "ok"}), 405, "method_not_allowed")
        assert_error(client.post("/openapi.json"), 405, "method_not_allowed")

    assert len(_request_records(captured, method="POST", route="/health", status=405)) == 2

    expected = _health("ok", "ok", EXPECTED_VERSION)
    with_json = client.request("GET", "/health", json={"extra_param_x": "valor-qualquer"})
    assert with_json.status_code == 200, with_json.text[:300]
    assert with_json.json() == expected
    with_text = client.request("GET", "/health", content=b"isto nao e json",
                               headers={"Content-Type": "text/plain"})
    assert with_text.status_code == 200, with_text.text[:300]
    assert with_text.json() == expected
