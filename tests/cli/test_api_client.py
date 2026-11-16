"""Cliente da API do pacote cliente.

O `ApiClient` recebe o `TestClient` da API em `http=`. Os pedidos que ele faz são observados por um
gancho de pedido do próprio cliente HTTP (`event_hooks["request"]`), que vê o pedido final
(método, caminho, consulta, cabeçalhos e corpo) qualquer que seja o método do `httpx2.Client` usado.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import cq_server
from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_server.migrate import EXPECTED_VERSION
from tests.helpers.api import (
    ADMIN_TOKEN,
    all_events,
    auth,
    create_token,
    register_person,
    uuid4_with_prefix,
)
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_matches
from tests.helpers.secrets import remember_secret

BASE_URL = "http://testserver"
ADMIN = auth(ADMIN_TOKEN)
NOT_FOUND_ALIAS = "ffff0000ffff"  # formato válido; nunca é apelido de alguém (o apelido seria `ffff`)


def _api(http: Any, token: str | None = ADMIN_TOKEN, **kwargs: Any) -> ApiClient:
    return ApiClient(BASE_URL, token, http=http, **kwargs)


@contextlib.contextmanager
def _recorded(http: Any):
    """Registra cada pedido que passa pelo cliente HTTP injetado."""
    seen: list[Any] = []
    hooks = http.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _routes(requests: list[Any]) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in requests]


def _query(request: Any) -> list[tuple[str, str]]:
    return sorted(request.url.params.multi_items())


def _sent_json(request: Any) -> Any:
    return json.loads(request.content)


def _op_of(path: str) -> str:
    """Operação do catálogo de um caminho `GET` usado nos testes."""
    if path == "/health":
        return "system.health"
    if path.startswith("/v1/people/"):
        return "people.get"
    return {"/v1/people": "people.list", "/v1/tokens": "tokens.list", "/v1/events": "events.list"}[path]


def _get(client: Any, path: str, params: Any = None) -> dict:
    """`GET` direto na API com o token inicial; afirma 200 e o esquema congelado."""
    resp = client.get(path, params=params, headers=ADMIN)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(_op_of(path), 200, body)
    return body


def _fix_pseudo_ids(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> list[uuid.UUID]:
    """Fixa os `pseudo_id` dos próximos cadastros (`people.generate_pseudo_id`)."""
    from cq_server import people

    ids = [uuid4_with_prefix(p, fill=str(i + 1)) for i, p in enumerate(prefixes)]
    pending = list(ids)
    monkeypatch.setattr(people, "generate_pseudo_id", lambda: pending.pop(0))
    return ids


def _error(code: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": MESSAGES[code], **extra}}


# --------------------------------------------------------------------------------------------------
# Cada método devolve exatamente o corpo da API; um argumento `None` não é enviado


def test_health_returns_api_body(client):
    """`health()`: devolve o corpo de `GET /health`."""
    api = _api(client)
    with _recorded(client) as seen:
        body = api.health()
    assert body == {"status": "ok", "version": cq_server.__version__, "database": "ok",
                    "schema_version": EXPECTED_VERSION, "expected_schema_version": EXPECTED_VERSION}
    assert body == _get(client, "/health")
    assert _routes(seen) == [("GET", "/health")]
    assert _query(seen[0]) == []


def test_register_person_returns_api_body_and_omits_none(client, clock, monkeypatch):
    """`register_person(*, name, ra=None, email=None)`: `None` não vai no corpo."""
    ids = _fix_pseudo_ids(monkeypatch, "abc1", "bcd2", "cde3")
    start = clock.now()
    api = _api(client)
    with _recorded(client) as seen:
        full = api.register_person(name="Helena Fictícia Moraes", ra="1122334455",
                                   email="helena@exemplo.example.com")
        clock.advance(5)
        bare = api.register_person(name="Bruno Inventado Siqueira")
        clock.advance(5)
        explicit_none = api.register_person(name="Clara Imaginária Viana", ra=None,
                                            email="clara@exemplo.example.com")
    assert full == {"pseudo_id": str(ids[0]), "alias": "abc1", "created_at": iso(start)}
    assert bare == {"pseudo_id": str(ids[1]), "alias": "bcd2", "created_at": iso(start + timedelta(seconds=5))}
    assert explicit_none == {"pseudo_id": str(ids[2]), "alias": "cde3",
                             "created_at": iso(start + timedelta(seconds=10))}
    for body in (full, bare, explicit_none):
        assert_matches("people.register", 201, body)
        assert _get(client, f"/v1/people/{body['alias']}") == body
    assert _routes(seen) == [("POST", "/v1/people")] * 3
    assert [_sent_json(r) for r in seen] == [
        {"name": "Helena Fictícia Moraes", "ra": "1122334455", "email": "helena@exemplo.example.com"},
        {"name": "Bruno Inventado Siqueira"},
        {"name": "Clara Imaginária Viana", "email": "clara@exemplo.example.com"},
    ]


def test_list_people_returns_api_body_and_omits_none(client):
    """`list_people(*, after=None, limit=None)`: só os argumentos dados vão na consulta."""
    for name in ("Ana Inventada Prado", "Bento Fictício Lage", "Clara Imaginária Viana"):
        register_person(client, ADMIN_TOKEN, name=name)
    full_api = _get(client, "/v1/people")
    first_api = _get(client, "/v1/people", {"limit": 1})
    cursor = first_api["next_cursor"]
    second_api = _get(client, "/v1/people", {"after": cursor, "limit": 1})
    rest_api = _get(client, "/v1/people", {"after": cursor})
    assert len(full_api["items"]) == 3
    assert second_api["items"] == [full_api["items"][1]]
    assert rest_api["items"] == full_api["items"][1:]

    api = _api(client)
    with _recorded(client) as seen:
        assert api.list_people() == full_api
        assert api.list_people(limit=1) == first_api
        assert api.list_people(after=cursor, limit=1) == second_api
        assert api.list_people(after=cursor) == rest_api
    assert _routes(seen) == [("GET", "/v1/people")] * 4
    assert [_query(r) for r in seen] == [
        [], [("limit", "1")], [("after", str(cursor)), ("limit", "1")], [("after", str(cursor))]]


def test_get_person_returns_api_body(client):
    """`get_person(ref)`: pelo apelido e pelo identificador."""
    person = register_person(client, ADMIN_TOKEN, name="Davi Inventado Nunes")
    api = _api(client)
    with _recorded(client) as seen:
        by_alias = api.get_person(person["alias"])
        by_id = api.get_person(person["pseudo_id"])
    assert by_alias == by_id == person == _get(client, f"/v1/people/{person['alias']}")
    assert _routes(seen) == [("GET", f"/v1/people/{person['alias']}"), ("GET", f"/v1/people/{person['pseudo_id']}")]


def test_reveal_returns_api_body_and_declares_cli(client, clock):
    """`reveal(pseudonyms, *, reason)`: corpo exato; `X-CQ-Client: cli`; evento `client: cli`."""
    a = register_person(client, ADMIN_TOKEN, name="Helena Fictícia Moraes", ra="1122334455",
                        email="Helena@Exemplo.example.com")
    b = register_person(client, ADMIN_TOKEN, name="Bruno Inventado Siqueira")
    refs = [a["alias"], b["pseudo_id"], NOT_FOUND_ALIAS, a["pseudo_id"], NOT_FOUND_ALIAS]
    expected = {
        "people": [
            {"pseudo_id": a["pseudo_id"], "alias": a["alias"], "name": "Helena Fictícia Moraes",
             "ra": "1122334455", "email": "Helena@Exemplo.example.com"},
            {"pseudo_id": b["pseudo_id"], "alias": b["alias"], "name": "Bruno Inventado Siqueira",
             "ra": None, "email": None},
        ],
        "not_found": [NOT_FOUND_ALIAS],
    }
    api = _api(client)
    with _recorded(client) as seen:
        body = api.reveal(refs, reason="conferencia pelo cliente")
    assert body == expected
    assert _routes(seen) == [("POST", "/v1/identity/reveal")]
    assert _sent_json(seen[0]) == {"pseudonyms": refs, "reason": "conferencia pelo cliente"}
    assert seen[0].headers.get("x-cq-client") == "cli"
    assert seen[0].headers.get("authorization") == f"Bearer {ADMIN_TOKEN}"
    events = all_events(client, ADMIN_TOKEN, "identity.revealed")
    assert events == [{"seq": events[0]["seq"], "type": "identity.revealed", "occurred_at": iso(clock.now()),
                       "actor_token_id": "bootstrap",
                       "data": {"client": "cli", "revealed": [a["pseudo_id"], b["pseudo_id"]],
                                "not_found_count": 1, "reason": "conferencia pelo cliente"}}]
    direct = client.post("/v1/identity/reveal", json={"pseudonyms": refs, "reason": "conferencia pelo cliente"},
                         headers=auth(ADMIN_TOKEN, "cli"))
    assert direct.status_code == 200
    assert_matches("identity.reveal", 200, direct.json())
    assert direct.json() == expected


@pytest.mark.parametrize("kind", ["mcp", "other"])
def test_client_kind_is_declared(kind, client, clock):
    """`client_kind`: com `client_kind="mcp"` a auditoria registra `client: mcp`."""
    person = register_person(client, ADMIN_TOKEN, name="Carla Imaginária Rocha")
    api = _api(client, client_kind=kind)
    with _recorded(client) as seen:
        api.reveal([person["alias"]], reason="conferencia declarada")
        api.list_people()
    assert [r.headers.get("x-cq-client") for r in seen] == [kind, kind]
    events = all_events(client, ADMIN_TOKEN, "identity.revealed")
    assert events == [{"seq": events[0]["seq"], "type": "identity.revealed", "occurred_at": iso(clock.now()),
                       "actor_token_id": "bootstrap",
                       "data": {"client": kind, "revealed": [person["pseudo_id"]], "not_found_count": 0,
                                "reason": "conferencia declarada"}}]


def test_create_token_returns_api_body(client, clock, sql):
    """`create_token(*, label, permissions)`: o corpo 201, com o segredo."""
    api = _api(client)
    with _recorded(client) as seen:
        body = api.create_token(label="rotulo do cliente", permissions=["people:read", "events:read"])
    assert_matches("tokens.create", 201, body)
    remember_secret(body["secret"])
    assert body == {"token_id": body["token_id"], "label": "rotulo do cliente",
                    "permissions": ["events:read", "people:read"], "created_at": iso(clock.now()),
                    "revoked_at": None, "secret": body["secret"]}
    assert _routes(seen) == [("POST", "/v1/tokens")]
    assert _sent_json(seen[0]) == {"label": "rotulo do cliente", "permissions": ["people:read", "events:read"]}
    assert _get(client, "/v1/tokens") == {"items": [{k: v for k, v in body.items() if k != "secret"}]}
    row = sql.execute("SELECT secret_sha256 FROM api_tokens WHERE token_id::text = %s",
                      (body["token_id"],)).fetchone()
    assert row == (hashlib.sha256(body["secret"].encode("utf-8")).hexdigest(),)


def test_list_tokens_returns_api_body(client, clock):
    """`list_tokens()`."""
    _, first = create_token(client, ADMIN_TOKEN, ["people:read"], label="primeiro token")
    clock.advance(10)
    _, second = create_token(client, ADMIN_TOKEN, ["events:read"], label="segundo token")
    api = _api(client)
    with _recorded(client) as seen:
        body = api.list_tokens()
    assert body == _get(client, "/v1/tokens")
    assert [i["token_id"] for i in body["items"]] == [first["token_id"], second["token_id"]]
    assert _routes(seen) == [("GET", "/v1/tokens")]
    assert _query(seen[0]) == []


def test_revoke_token_returns_api_body(client, clock):
    """`revoke_token(token_id)`: o `Token` revogado."""
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token a revogar")
    clock.advance(30)
    api = _api(client)
    with _recorded(client) as seen:
        body = api.revoke_token(token["token_id"])
    expected = {"token_id": token["token_id"], "label": "token a revogar", "permissions": ["people:read"],
                "created_at": token["created_at"], "revoked_at": iso(clock.now())}
    assert body == expected
    assert _routes(seen) == [("POST", f"/v1/tokens/{token['token_id']}/revoke")]
    again = client.post(f"/v1/tokens/{token['token_id']}/revoke", headers=ADMIN)
    assert again.status_code == 200
    assert_matches("tokens.revoke", 200, again.json())
    assert again.json() == expected


def test_list_events_returns_api_body_and_omits_none(client):
    """`list_events(types, *, after=None, limit=None)`: só os argumentos dados vão."""
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    register_person(client, ADMIN_TOKEN, name="Bento Fictício Lage")
    create_token(client, ADMIN_TOKEN, ["people:read"], label="token dos eventos")
    types = ["person.registered", "token.created"]
    type_params = [("types", t) for t in types]
    full_api = _get(client, "/v1/events", type_params)
    cursor = full_api["items"][0]["seq"]
    page_api = _get(client, "/v1/events", [*type_params, ("after", cursor), ("limit", 1)])
    limited_api = _get(client, "/v1/events", [*type_params, ("limit", 2)])
    assert [e["type"] for e in full_api["items"]] == ["person.registered", "person.registered", "token.created"]
    assert page_api["items"] == [full_api["items"][1]]
    assert limited_api["items"] == full_api["items"][:2]

    api = _api(client)
    with _recorded(client) as seen:
        assert api.list_events(types) == full_api
        assert api.list_events(types, after=cursor, limit=1) == page_api
        assert api.list_events(types, limit=2) == limited_api
    assert _routes(seen) == [("GET", "/v1/events")] * 3
    assert [sorted(r.url.params.get_list("types")) for r in seen] == [sorted(types)] * 3
    assert [[(k, v) for k, v in _query(r) if k != "types"] for r in seen] == [
        [], [("after", str(cursor)), ("limit", "1")], [("limit", "2")]]


# --------------------------------------------------------------------------------------------------
# `ApiError` com status e corpo exatos


@pytest.mark.parametrize("token", [None, "cq_tokendesconhecidosentinela_" + "z" * 20],
                         ids=["sem-token", "desconhecido"])
def test_api_error_unauthenticated(token, client):
    """`ApiError(status, body)`: 401 com o envelope exato; sem token, sem `Authorization`."""
    api = _api(client, token=token)
    with _recorded(client) as seen, pytest.raises(ApiError) as info:
        api.list_people()
    assert (info.value.status, info.value.body) == (401, _error("unauthenticated"))
    assert _routes(seen) == [("GET", "/v1/people")]
    if token is None:
        assert "authorization" not in seen[0].headers


def test_api_error_forbidden(client):
    """`ApiError`: 403 com `permission`; nada é cadastrado."""
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="so leitura")
    with pytest.raises(ApiError) as info:
        _api(client, token=secret).register_person(name="Nome Qualquer Teste")
    assert (info.value.status, info.value.body) == (403, _error("forbidden", permission="people:write"))
    assert _get(client, "/v1/people") == {"items": [], "next_cursor": 0}


def test_api_error_invalid_request(client):
    """`ApiError`: 400 com os `fields` exatos."""
    api = _api(client)
    with pytest.raises(ApiError) as info:
        api.register_person(name="   ", ra="98765x4321")
    assert (info.value.status, info.value.body) == (400, _error("invalid_request", fields=[
        {"field": "name", "reason": "too_short"}, {"field": "ra", "reason": "invalid_format"}]))
    with pytest.raises(ApiError) as info:
        api.list_people(limit=0)
    assert (info.value.status, info.value.body) == (400, _error("invalid_request", fields=[
        {"field": "limit", "reason": "out_of_range"}]))
    with pytest.raises(ApiError) as info:
        api.reveal([], reason="conferir nada")
    assert (info.value.status, info.value.body) == (400, _error("invalid_request", fields=[
        {"field": "pseudonyms", "reason": "too_few"}]))


def test_api_error_not_found(client):
    """`ApiError`: 404 em `get_person` e `revoke_token`."""
    api = _api(client)
    with pytest.raises(ApiError) as info:
        api.get_person("abcd")
    assert (info.value.status, info.value.body) == (404, _error("not_found"))
    with pytest.raises(ApiError) as info:
        api.revoke_token(str(uuid4_with_prefix("dddd", fill="7")))
    assert (info.value.status, info.value.body) == (404, _error("not_found"))


def test_api_error_conflict(client):
    """`ApiError`: 409 com os `fields` exatos."""
    register_person(client, ADMIN_TOKEN, name="Elisa Inventada Porto", ra="4455667788")
    with pytest.raises(ApiError) as info:
        _api(client).register_person(name="Outra Pessoa Qualquer", ra="4455667788")
    assert (info.value.status, info.value.body) == (409, _error("conflict", fields=[
        {"field": "ra", "reason": "duplicate"}]))


def test_api_error_internal(app_factory, unreachable_db_url):
    """`ApiError` em 5xx: banco inalcançável → 500 `internal`."""
    with TestClient(app_factory(database_url=unreachable_db_url), raise_server_exceptions=False) as http:
        with pytest.raises(ApiError) as info:
            _api(http).list_people()
    assert (info.value.status, info.value.body) == (500, _error("internal"))


# --------------------------------------------------------------------------------------------------
# `health` com 503 devolve o corpo


@pytest.mark.parametrize("case", ["banco-vazio", "banco-inalcancavel"])
def test_health_503_returns_body(case, app_factory, empty_db_url, unreachable_db_url):
    """`health` devolve o corpo também no 503, sem levantar `ApiError`."""
    url = empty_db_url if case == "banco-vazio" else unreachable_db_url
    with TestClient(app_factory(database_url=url), raise_server_exceptions=False) as http:
        body = _api(http).health()
        direct = http.get("/health")
    assert direct.status_code == 503
    assert_matches("system.health", 503, body)
    assert body == direct.json() == {
        "status": "error", "version": cq_server.__version__,
        "database": "ok" if case == "banco-vazio" else "unreachable",
        "schema_version": None, "expected_schema_version": EXPECTED_VERSION}


# --------------------------------------------------------------------------------------------------
# `ApiUnreachable` com a URL limpa


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:9",
    "http://127.0.0.1:9/",
    "http://usuariosentinela:senhasentinela@127.0.0.1:9/",
], ids=["simples", "barra-final", "credenciais"])
def test_unreachable_api_url_is_clean(base_url):
    """`ApiUnreachable.url` é o `base_url` sem barra final e sem credenciais."""
    api = ApiClient(base_url, ADMIN_TOKEN)
    with pytest.raises(ApiUnreachable) as info:
        api.health()
    assert info.value.url == "http://127.0.0.1:9"
    for text in (str(info.value), repr(info.value)):
        assert "usuariosentinela" not in text and "senhasentinela" not in text


@pytest.mark.parametrize("call", ["list_people", "register_person", "reveal"])
def test_unreachable_api_raises_for_operations(call):
    """Sem conexão, as operações levantam `ApiUnreachable` (não `ApiError`)."""
    api = ApiClient("http://usuariosentinela:senhasentinela@127.0.0.1:9", ADMIN_TOKEN)
    calls = {
        "list_people": lambda: api.list_people(),
        "register_person": lambda: api.register_person(name="Nome Inventado Qualquer"),
        "reveal": lambda: api.reveal(["abcd"], reason="motivo qualquer"),
    }
    with pytest.raises(ApiUnreachable) as info:
        calls[call]()
    assert info.value.url == "http://127.0.0.1:9"
