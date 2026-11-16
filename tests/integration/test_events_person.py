"""Filtro do log de eventos por pessoa.

Cobre o filtro por pessoa (cursor, limite, tipos, pessoa inexistente, formato, permissão, saída sem identidade) e o roteiro do expurgo. Contratos: o
filtro por pessoa de `events.list`, os erros e a tabela do campo de pessoa por tipo.

O filtro é conferido contra uma conta **própria** do teste: a tabela do campo de pessoa por tipo escrita aqui por extenso
(`PERSON_FIELD_TABLE`: o campo de pessoa de cada um dos 19 tipos e se é lista) aplicada a todos os eventos lidos
pela consulta da API base (`events.list` sem `person`, todos os tipos de `EventType`). Na mesma função de um caso do
filtro, a tabela do núcleo (`events.PERSON_FIELDS`, real no esqueleto) é conferida contra essa tabela,
contra `EVENT_FIELDS` e contra os modelos `*Data` de `cq_server.api.schemas` (todo campo `PseudoId` ou
`list[PseudoId]` está nela, e vice-versa).

Montagem: pessoas cadastradas pela API (`people.register`, que grava `person.registered`) e, para cada tipo com
campo de pessoa, um evento gravado pelo `events.append_event` da API base (real; o gravador único do log) com os
campos exatos do tipo (amostras válidas pelos esquemas congelados, como as de `test_contract_files.py`) e o
pseudônimo da pessoa no campo de pessoa. `seq` e cursores nunca como literal: só as relações.

No esqueleto, a consulta não aceita `person` (400 `[{query,
unknown_field}]` no lugar do 200) e `events.resolve_person` e `query_events(..., person=…)` lançam: cada função
falha no primeiro filtro por pessoa.
"""

from __future__ import annotations

import dataclasses
import itertools
import typing
import uuid
from typing import Any

import psycopg
import pytest

from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_token, list_events, register_person
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.purge import seed_purge_scenario

ADMIN = auth(ADMIN_TOKEN)

# A tabela do campo de pessoa, por extenso: o campo de pessoa de cada tipo e se ele guarda uma lista.
_PERSON_TYPES = ("team.link_created", "team.link_revoked", "team.joined", "endpoint.blocked", "student.invite_sent",
                 "student.invite_failed", "student.invite_cancelled", "student.invite_revoked", "student.joined",
                 "delivery.delivered", "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                 "submission.graded", "submission.internal_failure")
PERSON_FIELD_TABLE: dict[str, tuple[str, bool]] = {
    "person.registered": ("pseudo_id", False),
    "identity.revealed": ("revealed", True),
    "identity.reveal_denied": ("requested", True),
    "outbox.failed": ("recipient", False),
    **{event_type: ("person", False) for event_type in _PERSON_TYPES},
}
assert len(PERSON_FIELD_TABLE) == 19


def _uuid() -> str:
    return str(uuid.uuid4())


def _sample(event_type: str, person: str) -> dict:
    """Os campos exatos de `event_type` (amostras válidas pelos esquemas congelados), com `person` no campo de
    pessoa (numa lista com outro pseudônimo, nos campos de lista)."""
    other = _uuid()
    base: dict[str, dict] = {
        "person.registered": {"pseudo_id": person, "alias": "a3f9" + uuid.uuid4().hex[:6]},
        "identity.revealed": {"client": "cli", "revealed": [other, person], "not_found_count": 0,
                              "reason": "conferencia"},
        "identity.reveal_denied": {"client": "other", "requested": [person, other], "not_found_count": 1},
        "outbox.failed": {"message_id": 1, "recipient": person, "attempts": 1, "error": "bloqueado"},
        "team.link_created": {"offering_id": _uuid(), "person": person, "expires_at": "2031-01-08T00:00:00Z"},
        "team.link_revoked": {"offering_id": _uuid(), "person": person, "revoke_reason": "replaced"},
        "team.joined": {"offering_id": _uuid(), "person": person, "endpoint_replaced": False},
        "endpoint.blocked": {"offering_id": _uuid(), "person": person, "channel": "telegram"},
        "student.invite_sent": {"offering_id": _uuid(), "person": person, "origin": "batch",
                                "expires_at": "2031-01-08T00:00:00Z"},
        "student.invite_failed": {"offering_id": _uuid(), "person": person, "origin": "coordination",
                                  "failure": "recipient_refused"},
        "student.invite_cancelled": {"offering_id": _uuid(), "person": person, "origin": "batch",
                                     "cancel_reason": "superseded"},
        "student.invite_revoked": {"offering_id": _uuid(), "person": person, "origin": "self_service",
                                   "revoke_reason": "replaced"},
        "student.joined": {"offering_id": _uuid(), "person": person, "terms_version": 1, "endpoint_replaced": False},
        "delivery.delivered": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                               "person": person},
        "delivery.not_delivered": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                                   "person": person, "undelivered_reason": "endpoint_blocked"},
        "delivery.question_failed": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                                     "person": person, "failure": "variant_broken"},
        "delivery.dont_know": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                               "submission_id": _uuid(), "person": person},
        "submission.graded": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                              "submission_id": _uuid(), "question_ref_id": _uuid(), "person": person, "valid": True,
                              "late": False, "normalized": False, "score": 1.0,
                              "parts": [{"index": 0, "score": 1.0, "choices": ["sha256:" + "a" * 64], "ratio": None,
                                         "residual": None}]},
        "submission.internal_failure": {"offering_id": _uuid(), "dispatch_id": _uuid(), "delivery_id": _uuid(),
                                        "submission_id": _uuid(), "person": person,
                                        "engine_error": "invalid_request"},
    }
    return base[event_type]


def _decoy() -> tuple[str, dict]:
    """Um evento sem campo de pessoa (não sai em nenhum filtro)."""
    return "offering.created", {"offering_id": _uuid(), "code": f"decoy-{uuid.uuid4().hex[:8]}"}


def _append(sql: psycopg.Connection, clock: Any, event_type: str, data: dict) -> int:
    from cq_server.events import append_event

    with sql.transaction():
        return append_event(sql, event_type, data, occurred_at=clock.now(), actor_token_id=None)


def _all_types() -> tuple[str, ...]:
    from cq_server.events import EventType

    return tuple(t.value for t in EventType)


def _log(client: Any) -> list[dict]:
    """Todo o log pela consulta da API base (sem `person`), em ordem de `seq`."""
    return all_events(client, ADMIN_TOKEN, *_all_types())


def _cites(event: dict, pseudo_id: str) -> bool:
    entry = PERSON_FIELD_TABLE.get(event["type"])
    if entry is None:
        return False
    name, is_list = entry
    value = event["data"].get(name)
    return pseudo_id in value if is_list else value == pseudo_id


def _expected(client: Any, pseudo_id: str, types: tuple[str, ...] | None = None) -> list[dict]:
    return [e for e in _log(client) if _cites(e, pseudo_id) and (types is None or e["type"] in types)]


def _query(client: Any, params: list[tuple[str, Any]], token: str = ADMIN_TOKEN) -> Any:
    return client.get("/v1/events", params=params, headers=auth(token))


def _page(client: Any, person: str, *types: str, after: int = 0, limit: int | None = None) -> dict:
    return list_events(client, ADMIN_TOKEN, *types, after=after, limit=limit, person=person)


# --------------------------------------------------------------------------------------------------
# Cada campo de pessoa


@pytest.mark.parametrize("event_type", sorted(PERSON_FIELD_TABLE))
def test_each_person_field_by_pseudonym_and_alias(event_type: str, client: Any, sql: psycopg.Connection,
                                                  clock: Any) -> None:
    """para cada um dos 19 tipos com campo de pessoa (inclusive
    `pseudo_id`, `revealed`, `requested`, `recipient` e `person`), um evento que cita a pessoa P e outro que cita
    Q, mais um evento sem pessoa; o filtro por P, pelo pseudônimo e pelo apelido, devolve exatamente os eventos
    que citam P (o cadastro dela e o do tipo), em ordem de `seq`, com `next_cursor` no último; nenhum de Q."""
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Filtrada")
    q = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Vizinha")
    _append(sql, clock, *_decoy())
    _append(sql, clock, event_type, _sample(event_type, p["pseudo_id"]))
    _append(sql, clock, event_type, _sample(event_type, q["pseudo_id"]))
    expected = _expected(client, p["pseudo_id"])
    # Com `person.registered`, os dois cadastros de P: o da API e o gravado aqui.
    assert [e["type"] for e in expected] == ["person.registered", event_type]

    for ref in (p["pseudo_id"], p["alias"]):
        page = _page(client, ref)
        assert page == {"items": expected, "next_cursor": expected[-1]["seq"]}
        assert all(q["pseudo_id"] not in str(e["data"]) for e in page["items"])


def test_person_fields_table_matches_the_core_and_the_schemas(client: Any, sql: psycopg.Connection,
                                                              clock: Any) -> None:
    """A tabela do campo de pessoa (um teste confere `PERSON_FIELDS` contra `EVENT_FIELDS` e contra os `*Data` de
    `schemas.py`): a tabela do núcleo tem os 19 tipos, cada campo existe em `EVENT_FIELDS`, os
    dois tipos do expurgo não têm campo de pessoa, e todo campo `PseudoId` ou `list[PseudoId]` dos modelos de evento da
    API está nela com o mesmo `is_list`, e vice-versa. Na mesma função, o caso vermelho: o filtro pelo apelido
    de uma pessoa recém-cadastrada devolve só o cadastro dela."""
    from cq_server.api import schemas
    from cq_server.events import EVENT_FIELDS, PERSON_FIELDS, EventType, PersonField

    core = {etype.value: tuple((f.name, f.is_list) for f in fields) for etype, fields in PERSON_FIELDS.items()}
    assert core == {event_type: (entry,) for event_type, entry in PERSON_FIELD_TABLE.items()}
    for etype, fields in PERSON_FIELDS.items():
        assert all(isinstance(f, PersonField) and f.name in EVENT_FIELDS[EventType(etype)] for f in fields)
    assert "offering.purge_prepared" not in core and "offering.purged" not in core

    from_schemas: dict[str, tuple[tuple[str, bool], ...]] = {}
    for model in vars(schemas).values():
        if not (isinstance(model, type) and issubclass(model, schemas._EventBase) and model is not schemas._EventBase):
            continue
        hints = typing.get_type_hints(model, include_extras=True)
        (literal,) = typing.get_args(hints["type"])
        data_hints = typing.get_type_hints(hints["data"], include_extras=True)
        found = tuple((name, hint == list[schemas.PseudoId]) for name, hint in data_hints.items()
                      if hint in (schemas.PseudoId, list[schemas.PseudoId]))
        if found:
            from_schemas[literal] = found
    assert from_schemas == core

    person = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Do Controle")
    page = _page(client, person["alias"])
    assert [(e["type"], e["data"]) for e in page["items"]] == [
        ("person.registered", {"pseudo_id": person["pseudo_id"], "alias": person["alias"]})]


# --------------------------------------------------------------------------------------------------
# Ordem, cursor, limite e tipos


def test_order_cursor_and_limit(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """O filtro por pessoa não muda cursor nem limite (100/500): com 104 eventos que citam P intercalados com
    eventos de Q e sem pessoa, o padrão devolve os 100 primeiros (`next_cursor` no último); `after` encadeia o
    resto; `limit = 3` pagina de 3 em 3 sem repetir nem pular; `limit = 500` devolve todos; `limit = 501` e
    `limit = 0` → 400 `[{limit, out_of_range}]` como na API base."""
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Paginada")
    q = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Intercalada")
    types = itertools.cycle(sorted(set(PERSON_FIELD_TABLE) - {"person.registered"}))
    for _ in range(103):
        event_type = next(types)
        _append(sql, clock, event_type, _sample(event_type, p["pseudo_id"]))
        _append(sql, clock, event_type, _sample(event_type, q["pseudo_id"]))
        _append(sql, clock, *_decoy())
    expected = _expected(client, p["pseudo_id"])
    assert len(expected) == 104

    first = _page(client, p["alias"])
    assert first == {"items": expected[:100], "next_cursor": expected[99]["seq"]}
    rest = _page(client, p["alias"], after=first["next_cursor"])
    assert rest == {"items": expected[100:], "next_cursor": expected[-1]["seq"]}
    assert _page(client, p["alias"], after=rest["next_cursor"]) == {"items": [], "next_cursor": rest["next_cursor"]}
    collected: list[dict] = []
    after = 0
    while True:
        page = _page(client, p["pseudo_id"], after=after, limit=3)
        assert len(page["items"]) <= 3
        if not page["items"]:
            assert page["next_cursor"] == after
            break
        collected.extend(page["items"])
        after = page["next_cursor"]
        assert after == page["items"][-1]["seq"]
    assert collected == expected
    assert _page(client, p["pseudo_id"], limit=500) == {"items": expected, "next_cursor": expected[-1]["seq"]}
    for limit in ("501", "0"):
        assert_error(_query(client, [("person", p["alias"]), ("limit", limit)]), 400, "invalid_request",
                     fields=[("limit", "out_of_range")])


def test_types_intersect_and_no_person_is_the_base_query(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Com `person` e `types`, a interseção; sem `person`, igual à consulta da API base: com `types` e
    `person`, só os eventos dos tipos pedidos que citam a pessoa; um tipo sem campo de pessoa com `person` →
    página vazia; sem `person`, a consulta por tipos é a da API base (todos os eventos dos tipos, de todas as pessoas),
    e sem `types` e sem `person`, 400 `[{types, missing}]`."""
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Dos Tipos")
    q = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Sem Filtro")
    for event_type in ("team.link_created", "student.joined", "delivery.delivered"):
        _append(sql, clock, event_type, _sample(event_type, p["pseudo_id"]))
        _append(sql, clock, event_type, _sample(event_type, q["pseudo_id"]))
    _append(sql, clock, *_decoy())

    wanted = ("team.link_created", "delivery.delivered")
    expected = _expected(client, p["pseudo_id"], wanted)
    assert [e["type"] for e in expected] == list(wanted)
    assert _page(client, p["alias"], *wanted) == {"items": expected, "next_cursor": expected[-1]["seq"]}
    assert _page(client, p["alias"], "offering.created") == {"items": [], "next_cursor": 0}

    r2 = list_events(client, ADMIN_TOKEN, *wanted, limit=500)
    assert r2["items"] == [e for e in _log(client) if e["type"] in wanted]
    assert {e["data"]["person"] for e in r2["items"]} == {p["pseudo_id"], q["pseudo_id"]}
    assert_error(_query(client, [("after", "0")]), 400, "invalid_request", fields=[("types", "missing")])


# --------------------------------------------------------------------------------------------------
# Pessoa inexistente, malformada ou repetida; permissão


def test_unknown_person_is_an_empty_page(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Pessoa inexistente → `{"items": [], "next_cursor": <after ou 0>}`, igual a uma página sem
    eventos: um pseudônimo válido sem pessoa e um apelido sem pessoa dão a página vazia com `next_cursor` 0
    (ou o `after` pedido), com e sem `types`; igual à página de uma pessoa sem eventos do tipo pedido."""
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Existente")
    _append(sql, clock, "team.joined", _sample("team.joined", p["pseudo_id"]))
    ghost_pseudo = _uuid()
    ghost_alias = "f" + uuid.uuid4().hex[:11]
    assert ghost_pseudo != p["pseudo_id"] and ghost_alias != p["alias"]
    for ref in (ghost_pseudo, ghost_alias):
        assert _page(client, ref) == {"items": [], "next_cursor": 0}
        assert _page(client, ref, after=7) == {"items": [], "next_cursor": 7}
        assert _page(client, ref, "team.joined") == {"items": [], "next_cursor": 0}
    assert _page(client, p["alias"], "student.joined", after=7) == {"items": [], "next_cursor": 7}


@pytest.mark.parametrize("params", [
    pytest.param([("types", "team.joined"), ("person", "NAO_E_PSEUDONIMO_SENTINELA")], id="malformado"),
    pytest.param([("types", "team.joined"), ("person", "pseudonimo-com-hifens-sentinela")], id="hifens"),
    pytest.param([("types", "team.joined"), ("person", "ab")], id="curto"),
    pytest.param([("types", "team.joined"), ("person", "a3f9"), ("person", "b4e8")], id="repetido"),
    pytest.param([("types", "team.joined"), ("person", "")], id="vazio"),
])
def test_malformed_or_repeated_person_is_400_without_echo(params: list[tuple[str, str]], client: Any) -> None:
    """`events.list` com `person` repetido ou fora do padrão de pseudônimo/apelido → 400
    com `[{person, invalid_format}]`, sem eco: o valor fora do padrão `Pseudonym` (o mesmo de `people.get`),
    curto, vazio ou repetido → 400 exato, sem eco dos valores."""
    resp = _query(client, params)
    assert_error(resp, 400, "invalid_request", fields=[("person", "invalid_format")])
    assert_no_echo(resp, *[value for name, value in params if name == "person" and len(value) >= 8])


def test_events_read_permission(client: Any) -> None:
    """A permissão `events:read` não muda com o filtro: um token sem ela → 403 `forbidden` com `permission =
    events:read`; controle: um token só com ela filtra por pessoa."""
    p = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Da Permissao")
    weak, _ = create_token(client, ADMIN_TOKEN, ["people:read"])
    strong, _ = create_token(client, ADMIN_TOKEN, ["events:read"])
    assert_error(_query(client, [("person", p["alias"])], token=weak), 403, "forbidden", permission="events:read")
    resp = _query(client, [("person", p["alias"])], token=strong)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("events.list", 200, body)
    assert [e["type"] for e in body["items"]] == ["person.registered"]


def test_no_identity_in_the_filtered_output(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Saída sem identidade (a redação de saída não muda): o filtro por uma pessoa com
    nome, RA e e-mail cadastrados devolve os eventos sem nenhum deles (o texto livre de `outbox.failed` que cita a
    pessoa sai redigido, como na API base)."""
    name, ra, email = "Iracema Sentinela Filtrada", "4460000001", "iracema.sentinela@turma.example.org"
    p = register_person(client, ADMIN_TOKEN, name=name, ra=ra, email=email)
    data = {**_sample("outbox.failed", p["pseudo_id"]), "error": f"falhou para {name} ({email}, RA {ra})"}
    _append(sql, clock, "outbox.failed", data)
    resp = _query(client, [("person", p["alias"])])
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("events.list", 200, body)
    assert [e["type"] for e in body["items"]] == ["person.registered", "outbox.failed"]
    assert_no_echo(resp, name, ra, email, "Sentinela Filtrada")


# --------------------------------------------------------------------------------------------------
# O roteiro de ponta a ponta (eventos), com o expurgo


def test_events_with_the_purge_walkthrough(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Roteiro de ponta a ponta (eventos): no roteiro do expurgo (a semente do cenário, a preparação e a confirmação
    pelas rotas), o filtro por cada pessoa do cenário (a apagada e a mantida, pelo pseudônimo e pelo apelido)
    devolve exatamente os eventos que a citam pela conta própria, depois do expurgo como antes; os dois tipos
    do expurgo (`offering.purge_prepared`, `offering.purged`) existem no log e não saem em nenhum filtro."""
    scenario = seed_purge_scenario(client, sql)
    before = {label: _page(client, person.alias, limit=500) for label, person in scenario.people.items()}
    prepared = client.post(f"/v1/offerings/{scenario.code}/purge/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text[:300]
    confirmed = client.post(f"/v1/offerings/{scenario.code}/purge",
                            json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert confirmed.status_code == 200, confirmed.text[:300]

    log = _log(client)
    purge_events = [e for e in log if e["type"] in ("offering.purge_prepared", "offering.purged")]
    assert [e["type"] for e in purge_events] == ["offering.purge_prepared", "offering.purged"]
    for label, person in scenario.people.items():
        expected = [e for e in log if _cites(e, person.pseudo_id)]
        assert expected, f"nenhum evento cita {label}"
        for ref in (person.pseudo_id, person.alias):
            page = _page(client, ref, limit=500)
            assert page == {"items": expected, "next_cursor": expected[-1]["seq"]}
            assert not any(e["type"].startswith("offering.purge") for e in page["items"])
        assert before[label]["items"] == expected
    assert dataclasses.asdict(scenario.impact)  # as contagens da semente existem (controle do cenário)
