"""O cliente da API do CLI como biblioteca, sem `click` e sem a camada de comandos (diagnóstico,
expurgo e eventos por pessoa).

No molde de `tests/cli/test_api_client.py`: o `ApiClient` recebe o `TestClient` da API em `http=`, e
os pedidos são observados pelo gancho `event_hooks["request"]` do próprio cliente HTTP (método, caminho, consulta,
cabeçalhos e corpo finais). Os métodos cobertos aqui: `system_doctor()`, `prepare_purge(ref)`, `purge_offering(ref,
confirmation_code=…)` e `list_events(types, *, after, limit, person)` (só os argumentos dados vão); `ApiError.pending`
traz o objeto `pending` do envelope (`None` sem ele).

**O que já passa no esqueleto:** o atributo `pending` já existe; a asserção de `None` nos demais
códigos fica na mesma função do caso `purge_pending` pela rota (vermelho por `NotImplementedError` e pela rota
ausente até a implementação). Verde depois da implementação.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from cq_client.api import ApiClient, ApiError

from tests.helpers.api import (
    ADMIN_TOKEN,
    auth,
    create_offering,
    create_token,
    import_students,
    register_person,
)
from tests.helpers.contract import MESSAGES, assert_error, assert_matches
from tests.helpers.purge import make_pending, seed_purge_scenario

BASE_URL = "http://testserver"

# O cenário mínimo do expurgo: um oferecimento arquivado com dois alunos, sem bot, sem equipe e sem trabalho
# pendente (tests/helpers/purge.py).
_MINIMAL = {"bot": False, "team": False, "shared": False, "cross_offering": False, "frozen_report": False,
            "free_message_before_register": False, "contact": None}


def _api(http: Any, token: str | None = ADMIN_TOKEN) -> ApiClient:
    return ApiClient(BASE_URL, token, http=http)


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


def _query(request: Any) -> list[tuple[str, str]]:
    return sorted(request.url.params.multi_items())


def _assert_request(request: Any, method: str, path: str, *, body: Any = None,
                    token: str | None = ADMIN_TOKEN) -> None:
    """O pedido final: método, caminho, sem consulta, o corpo exato (ou nenhum) e a autenticação do token."""
    assert (request.method, request.url.path) == (method, path)
    assert _query(request) == []
    if body is None:
        assert request.content == b""
    else:
        assert json.loads(request.content) == body
    assert request.headers.get("Authorization") == (f"Bearer {token}" if token else None)
    assert request.headers.get("X-CQ-Client") == "cli"


# --------------------------------------------------------------------------------------------------
# system.doctor


def test_system_doctor_returns_api_body(client) -> None:
    """`system_doctor()` faz `GET /v1/system/doctor`, sem corpo nem consulta,
    com o token, e devolve o corpo da API sem mudança."""
    api = _api(client)
    with _recorded(client) as seen:
        body = api.system_doctor()
    assert len(seen) == 1
    _assert_request(seen[0], "GET", "/v1/system/doctor")
    assert_matches("system.doctor", 200, body)
    direct = client.get("/v1/system/doctor", headers=auth(ADMIN_TOKEN))
    assert direct.status_code == 200
    assert body == direct.json()


# --------------------------------------------------------------------------------------------------
# offerings.prepare_purge e offerings.purge


def test_prepare_purge_and_purge_offering(client, sql) -> None:
    """`prepare_purge(ref)` faz `POST /v1/offerings/{ref}/purge/prepare`
    sem corpo; `purge_offering(ref, confirmation_code=…)` faz `POST /v1/offerings/{ref}/purge` com o corpo
    exatamente `{"confirmation_code": …}`; cada um devolve o corpo da API (o esquema congelado), e o
    `offerings.get` mostra depois o `purged_at` do resultado."""
    scenario = seed_purge_scenario(client, sql, **_MINIMAL)
    api = _api(client)
    with _recorded(client) as seen:
        prepared = api.prepare_purge(scenario.code)
    assert len(seen) == 1
    _assert_request(seen[0], "POST", f"/v1/offerings/{scenario.code}/purge/prepare")
    assert_matches("offerings.prepare_purge", 200, prepared)
    assert prepared["offering"] == {"offering_id": scenario.offering_id, "code": scenario.code}
    code = prepared["confirmation_code"]
    with _recorded(client) as seen:
        purged = api.purge_offering(scenario.code, confirmation_code=code)
    assert len(seen) == 1
    _assert_request(seen[0], "POST", f"/v1/offerings/{scenario.code}/purge", body={"confirmation_code": code})
    assert_matches("offerings.purge", 200, purged)
    assert purged["offering"] == {"offering_id": scenario.offering_id, "code": scenario.code}
    shown = client.get(f"/v1/offerings/{scenario.code}", headers=auth(ADMIN_TOKEN))
    assert shown.status_code == 200
    assert shown.json()["purged_at"] == purged["purged_at"]


# --------------------------------------------------------------------------------------------------
# events.list com person


def test_list_events_with_person_sends_only_given_arguments(client) -> None:
    """`list_events(types, …, person=…)` manda `person` e `types` só quando
    informados, e `after`/`limit` como nas outras listas; o corpo é o da API; sem o argumento, nada de `person`."""
    ana = register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    register_person(client, ADMIN_TOKEN, name="Bento Fictício Lage")
    create_token(client, ADMIN_TOKEN, ["people:read"], label="token dos eventos por pessoa")

    def direct(params: list[tuple[str, Any]]) -> dict:
        resp = client.get("/v1/events", params=params, headers=auth(ADMIN_TOKEN))
        assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert_matches("events.list", 200, body)
        return body

    by_alias = direct([("person", ana["alias"])])
    by_pseudo = direct([("types", "person.registered"), ("after", 0), ("limit", 5), ("person", ana["pseudo_id"])])
    plain = direct([("types", "person.registered")])
    assert [e["type"] for e in by_alias["items"]] == ["person.registered"]
    assert by_pseudo["items"] == by_alias["items"]
    assert len(plain["items"]) == 2

    api = _api(client)
    with _recorded(client) as seen:
        assert api.list_events([], person=ana["alias"]) == by_alias
        assert api.list_events(["person.registered"], after=0, limit=5, person=ana["pseudo_id"]) == by_pseudo
        assert api.list_events(["person.registered"]) == plain
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/events")] * 3
    assert [_query(r) for r in seen] == [
        [("person", ana["alias"])],
        [("after", "0"), ("limit", "5"), ("person", ana["pseudo_id"]), ("types", "person.registered")],
        [("types", "person.registered")],
    ]


# --------------------------------------------------------------------------------------------------
# ApiError.pending


def test_api_error_pending_only_in_purge_pending(client, sql) -> None:
    """no `purge_pending` real da API (uma tarefa pendente com o assunto do oferecimento),
    `ApiError.pending` é o objeto `pending` exato do envelope; no `purge_changed` (as contagens mudaram desde a
    preparação), no `invalid_state`, no `invalid_confirmation` e no 403, `pending` é `None` (o atributo
    real do esqueleto)."""
    scenario = seed_purge_scenario(client, sql, **_MINIMAL)
    api = _api(client)

    # purge_changed: a aluna passa a ter matrícula num oferecimento não expurgado entre as duas fases.
    prepared = api.prepare_purge(scenario.code)
    other = create_offering(client, ADMIN_TOKEN)
    import_students(client, ADMIN_TOKEN, other["code"], [scenario.person("aluno_a").identity.row()])
    try:
        api.purge_offering(scenario.code, confirmation_code=prepared["confirmation_code"])
    except ApiError as exc:
        changed = exc
    else:
        raise AssertionError("a confirmação com as contagens mudadas não foi recusada")
    assert (changed.status, changed.code) == (409, "purge_changed")
    assert changed.body == {"error": {"code": "purge_changed", "message": MESSAGES["purge_changed"]}}
    assert changed.pending is None

    # purge_pending: uma tarefa pendente com o assunto do oferecimento.
    assert make_pending(sql, "task:offering_row_id", scenario) == "tasks"
    expected_pending = {"outbox": 0, "updates": 0, "tasks": 1, "freeze": 0, "due": 0}
    try:
        api.prepare_purge(scenario.code)
    except ApiError as exc:
        blocked = exc
    else:
        raise AssertionError("a preparação com trabalho pendente não foi recusada")
    assert (blocked.status, blocked.code) == (409, "purge_pending")
    assert blocked.pending == expected_pending
    assert blocked.body == {"error": {"code": "purge_pending", "message": MESSAGES["purge_pending"],
                                      "pending": expected_pending}}
    assert_error(client.post(f"/v1/offerings/{scenario.code}/purge/prepare", headers=auth(ADMIN_TOKEN)),
                 409, "purge_pending", pending=expected_pending)

    # invalid_confirmation: sem o pendente, a preparação vale e um código errado é recusado.
    sql.execute("DELETE FROM tasks WHERE task_type = 'notice.access_episode' AND payload->>'offering_row_id' = %s",
                (str(scenario.offering_row),))
    api.prepare_purge(scenario.code)
    try:
        api.purge_offering(scenario.code, confirmation_code="codigo-errado-Xq7")
    except ApiError as exc:
        wrong = exc
    else:
        raise AssertionError("o código errado não foi recusado")
    assert (wrong.status, wrong.code, wrong.pending) == (409, "invalid_confirmation", None)

    # invalid_state: um oferecimento em rascunho não pode ser expurgado.
    draft = create_offering(client, ADMIN_TOKEN)
    try:
        api.prepare_purge(draft["code"])
    except ApiError as exc:
        state = exc
    else:
        raise AssertionError("o rascunho não foi recusado")
    assert (state.status, state.code, state.pending) == (409, "invalid_state", None)
    assert state.body["error"]["state"] == "draft"

    # 403: um token sem `people:write`.
    secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="token sem people:write")
    try:
        _api(client, secret).prepare_purge(scenario.code)
    except ApiError as exc:
        forbidden = exc
    else:
        raise AssertionError("o token sem permissão não foi recusado")
    assert (forbidden.status, forbidden.code, forbidden.pending) == (403, "forbidden", None)
    assert forbidden.body["error"]["permission"] == "people:write"
