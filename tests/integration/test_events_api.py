"""Consulta do log de eventos pela API.

Cobre a consulta, o limite, a ordem, os horários e a permissão; os limites de tipos e de cursor; os
contratos de horários e cursores, de erros e de `events.list`, e os tipos de evento.

Convenções: `seq` e `next_cursor` nunca são afirmados por valor literal, só por
crescimento e encadeamento; horários por `iso(dt)`; toda resposta de sucesso passa por
`assert_matches` (o `list_events` do arnês já valida o corpo contra o esquema congelado).

Oferecimentos e bots: `ALL_TYPES` tem os 14 tipos
de oferecimentos e bots (a consulta com os 21 tipos é aceita e devolve só os eventos que
existem) e `ALL_PERMISSIONS` as 3 permissões próprias (o token "todas menos `events:read`" tem
7).

Vínculo do aluno: `ALL_TYPES` tem os 8 tipos
de aluno (a consulta com os 29 tipos é aceita e devolve só os eventos que existem; no esqueleto,
sem esses tipos em `EventType`, ela é recusada com 400 nos itens `types[21]` a `types[28]`).
Nenhuma permissão própria.

Disparo e resposta: `ALL_TYPES` tem os
13 tipos de disparo, entrega e submissão (a consulta com os 42 tipos é aceita e devolve só os
eventos que existem; no esqueleto, sem esses tipos em `EventType`, ela é recusada com 400 nos itens
`types[29]` a `types[41]`). Nenhuma permissão própria.

Disparo e resposta, com as mensagens livres opcionais por oferecimento:
`ALL_TYPES` tem o 43º tipo, `offering.settings_changed` (a alteração da configuração de mensagens livres do
oferecimento); a consulta com os 43 tipos é aceita e devolve só os eventos que existem. Sem o tipo em
`EventType`, ela é recusada com 400 no item `types[42]`. O `data` exato do evento fica em
`test_offering_settings.py`.

Relatórios e acompanhamento: `ALL_TYPES` tem os 3
tipos (`report.frozen`, `notice.recorded` e `offering.digest_changed`); a consulta com os 46 tipos é
aceita e devolve só os eventos que existem. No esqueleto, sem esses tipos em `EventType`, ela é recusada
com 400 nos itens `types[43]` a `types[45]`. Nenhuma permissão própria.

CLI e expurgo: `ALL_TYPES` tem os 2
tipos (`offering.purge_prepared` e `offering.purged`); a consulta com os 48 tipos é aceita e devolve só os
eventos que existem. No esqueleto, sem esses tipos em `EventType`, ela é recusada com 400 nos itens `types[46]`
e `types[47]`. Nenhuma permissão própria; o parâmetro `person` fica em `test_events_person.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo as pg_conninfo

from cq_server.ports.delivery import Undeliverable
from tests.helpers.api import (
    ADMIN_TOKEN,
    all_events,
    auth,
    create_token,
    list_events,
    register_person,
    uuid4_with_prefix,
)
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo

# Os 7 tipos de evento da API base, escritos aqui (e não importados de `EventType`) para que o teste não
# dependa da implementação.
ALL_TYPES = (
    "person.registered",
    "token.created",
    "token.revoked",
    "identity.revealed",
    "identity.reveal_denied",
    "task.failed",
    "outbox.failed",
    # Oferecimentos e bots: os 14 tipos.
    # O `data` exato de cada um, pelo seu fluxo, fica em `test_events_offerings_bots.py`.
    "offering.created",
    "offering.state_changed",
    "offering.archive_prepared",
    "bot.registered",
    "bot.token_replaced",
    "bot.profile_updated",
    "bot.lease_changed",
    "bot.access_changed",
    "team.link_created",
    "team.link_revoked",
    "team.joined",
    "team.join_refused",
    "endpoint.blocked",
    "outbox.reply_failed",
    # Vínculo do aluno: os 8 tipos. O
    # `data` exato de cada um, pelo seu fluxo, fica nos testes do vínculo (importação, envio, termo) e nos
    # casos fechados em falha de `test_api_errors.py`.
    "student.list_imported",
    "student.invites_prepared",
    "student.invites_queued",
    "student.invite_sent",
    "student.invite_failed",
    "student.invite_cancelled",
    "student.invite_revoked",
    "student.joined",
    # Disparo e resposta: os 13 tipos. O
    # `data` exato de cada um, pelo seu fluxo, fica nos testes do disparo e nos casos fechados em falha de
    # `test_api_errors.py`.
    "dispatch.prepared",
    "dispatch.validated",
    "dispatch.rejected",
    "dispatch.confirmed",
    "dispatch.cancelled",
    "dispatch.opened",
    "dispatch.closed",
    "delivery.delivered",
    "delivery.not_delivered",
    "delivery.question_failed",
    "delivery.dont_know",
    "submission.graded",
    "submission.internal_failure",
    # Mensagens livres opcionais: a configuração de mensagens livres do oferecimento alterada.
    "offering.settings_changed",
    # Relatórios e acompanhamento: os 3 tipos.
    # O `data` exato de cada um, pelo seu fluxo, fica nos testes dos relatórios e nos casos fechados em falha de
    # `test_api_errors.py`.
    "report.frozen",
    "notice.recorded",
    "offering.digest_changed",
    # CLI e expurgo: os 2 tipos do expurgo.
    # O `data` exato de cada um, pelo seu fluxo, fica em `test_purge_api.py` e nos casos fechados em falha de
    # `test_api_errors.py`.
    "offering.purge_prepared",
    "offering.purged",
)

# As 5 permissões da API base.
ALL_PERMISSIONS = ("events:read", "identity:reveal", "people:read", "people:write", "tokens:manage",
                   # Oferecimentos e bots: as 3 permissões.
                   "bots:manage", "offerings:read", "offerings:write")

# Um pseudônimo que nunca corresponde a ninguém: um apelido de 32 caracteres é o hex inteiro de um
# `pseudo_id` v4, que tem `4` na posição 12; este só tem `f`, então não casa com nenhum apelido, e
# também não tem o formato de identificador (sem hífens).
NEVER_AN_ALIAS = "f" * 32


def _without_seq(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if k != "seq"} for item in items]


def _assert_strictly_increasing(seqs: list[int]) -> None:
    assert all(isinstance(s, int) and not isinstance(s, bool) for s in seqs), seqs
    assert all(a < b for a, b in zip(seqs, seqs[1:])), f"seq não cresce estritamente: {seqs}"


def _get_events(client: TestClient, token: str | None, params: list[tuple[str, str]]):
    return client.get("/v1/events", params=params, headers=auth(token))


# ---------------------------------------------------------------------------------------------------
# um evento de cada tipo, na ordem das operações
# ---------------------------------------------------------------------------------------------------


def test_lifecycle_produces_exact_events_in_order(client, clock):
    """Ciclo de vida (com os dados da auditoria): cadastro → token T sem
    `identity:reveal` → revelação negada com T → revelação com o token inicial → revogação de T geram
    exatamente estes 5 eventos, nessa ordem, com `seq` crescente e `data`, `actor_token_id` e
    `occurred_at` exatos. A consulta pede também os 14 tipos de oferecimentos e bots, que aqui não têm
    evento, e os 8 tipos de aluno, que também não têm evento aqui."""
    person = register_person(client, ADMIN_TOKEN, name="Tercílio Fictício Evento", ra="5500000001")
    at_register = iso(clock.now())
    assert person["created_at"] == at_register

    clock.advance(60)
    secret, token = create_token(client, ADMIN_TOKEN, ["people:read", "events:read"],
                                 label="servico de feedback")
    at_create = iso(clock.now())

    clock.advance(60)
    unknown_uuid = str(uuid4_with_prefix("eeee", fill="e"))
    denied = client.post(
        "/v1/identity/reveal",
        json={"pseudonyms": [person["alias"], unknown_uuid], "reason": "conferencia de rotina"},
        headers=auth(secret, "mcp"))
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    at_denied = iso(clock.now())

    clock.advance(60)
    revealed = client.post(
        "/v1/identity/reveal",
        json={"pseudonyms": [person["pseudo_id"]], "reason": "conferencia de rotina"},
        headers=auth(ADMIN_TOKEN, "cli"))
    assert revealed.status_code == 200, revealed.text[:300]
    assert_matches("identity.reveal", 200, revealed.json())
    assert revealed.json() == {
        "people": [{"pseudo_id": person["pseudo_id"], "alias": person["alias"],
                    "name": "Tercílio Fictício Evento", "ra": "5500000001", "email": None}],
        "not_found": [],
    }
    at_revealed = iso(clock.now())

    clock.advance(60)
    revoked = client.post(f"/v1/tokens/{token['token_id']}/revoke", headers=auth(ADMIN_TOKEN))
    assert revoked.status_code == 200, revoked.text[:300]
    assert_matches("tokens.revoke", 200, revoked.json())
    at_revoked = iso(clock.now())
    assert revoked.json()["revoked_at"] == at_revoked

    # A consulta pede os 7 tipos: só estes 5 eventos existem, e ler eventos não gera evento.
    items = all_events(client, ADMIN_TOKEN, *ALL_TYPES)
    _assert_strictly_increasing([e["seq"] for e in items])
    assert _without_seq(items) == [
        {"type": "person.registered", "occurred_at": at_register, "actor_token_id": "bootstrap",
         "data": {"pseudo_id": person["pseudo_id"], "alias": person["alias"]}},
        {"type": "token.created", "occurred_at": at_create, "actor_token_id": "bootstrap",
         "data": {"token_id": token["token_id"], "label": "servico de feedback",
                  "permissions": ["events:read", "people:read"]}},
        {"type": "identity.reveal_denied", "occurred_at": at_denied,
         "actor_token_id": token["token_id"],
         "data": {"client": "mcp", "requested": [person["pseudo_id"]], "not_found_count": 1}},
        {"type": "identity.revealed", "occurred_at": at_revealed, "actor_token_id": "bootstrap",
         "data": {"client": "cli", "revealed": [person["pseudo_id"]], "not_found_count": 0,
                  "reason": "conferencia de rotina"}},
        {"type": "token.revoked", "occurred_at": at_revoked, "actor_token_id": "bootstrap",
         "data": {"token_id": token["token_id"]}},
    ]
    # `all_events` lê de 500 em 500; uma leitura só, com o limite padrão, devolve o mesmo.
    single = list_events(client, ADMIN_TOKEN, *ALL_TYPES)
    assert single["items"] == items
    assert single["next_cursor"] == items[-1]["seq"]


# ---------------------------------------------------------------------------------------------------
# tipos, cursor, limite e continuação
# ---------------------------------------------------------------------------------------------------


def _seed_interleaved(client) -> tuple[list[dict], list[dict]]:
    """Cadastra 3 pessoas e cria 2 tokens, intercalados: P1, K1, P2, K2, P3."""
    people: list[dict] = []
    tokens: list[dict] = []
    people.append(register_person(client, ADMIN_TOKEN, name="Pessoa Intercalada Um"))
    tokens.append(create_token(client, ADMIN_TOKEN, ["people:read"], label="token intercalado um")[1])
    people.append(register_person(client, ADMIN_TOKEN, name="Pessoa Intercalada Dois"))
    tokens.append(create_token(client, ADMIN_TOKEN, ["events:read"], label="token intercalado dois")[1])
    people.append(register_person(client, ADMIN_TOKEN, name="Pessoa Intercalada Tres"))
    return people, tokens


def _ref(event: dict) -> str:
    data = event["data"]
    return data["pseudo_id"] if event["type"] == "person.registered" else data["token_id"]


def test_filters_by_one_and_several_types(client):
    """só os tipos pedidos, em ordem de `seq`; um tipo ou vários; a ordem dos
    `types` na consulta não muda o resultado."""
    people, tokens = _seed_interleaved(client)

    both = list_events(client, ADMIN_TOKEN, "person.registered", "token.created")
    items = both["items"]
    assert [e["type"] for e in items] == [
        "person.registered", "token.created", "person.registered", "token.created",
        "person.registered"]
    assert [_ref(e) for e in items] == [
        people[0]["pseudo_id"], tokens[0]["token_id"], people[1]["pseudo_id"],
        tokens[1]["token_id"], people[2]["pseudo_id"]]
    _assert_strictly_increasing([e["seq"] for e in items])
    assert both["next_cursor"] == items[-1]["seq"]

    only_people = list_events(client, ADMIN_TOKEN, "person.registered")
    assert only_people == {"items": [items[0], items[2], items[4]], "next_cursor": items[4]["seq"]}

    only_tokens = list_events(client, ADMIN_TOKEN, "token.created")
    assert only_tokens == {"items": [items[1], items[3]], "next_cursor": items[3]["seq"]}

    assert list_events(client, ADMIN_TOKEN, "token.created", "person.registered") == both

    # Um tipo sem eventos: vazio, com `next_cursor == after`.
    assert list_events(client, ADMIN_TOKEN, "token.revoked") == {"items": [], "next_cursor": 0}


def test_after_limit_and_cursor_chaining(client):
    """só `seq > after`; no máximo `limit`; `next_cursor` = `seq`
    do último item; encadear os cursores percorre tudo sem perda nem repetição; repetir a partir do
    último cursor → `[]` e `next_cursor == after`."""
    _seed_interleaved(client)
    types = ("person.registered", "token.created")
    items = list_events(client, ADMIN_TOKEN, *types)["items"]
    assert len(items) == 5

    # `after` é exclusivo: a partir do 2º item, vêm o 3º em diante.
    page = list_events(client, ADMIN_TOKEN, *types, after=items[1]["seq"])
    assert page == {"items": items[2:], "next_cursor": items[-1]["seq"]}
    # Qualquer cursor entre o 1º e o 2º (inclusive o `seq` do 2º − 1) devolve do 2º em diante.
    page = list_events(client, ADMIN_TOKEN, *types, after=items[1]["seq"] - 1)
    assert page == {"items": items[1:], "next_cursor": items[-1]["seq"]}
    # `after` com um tipo só.
    page = list_events(client, ADMIN_TOKEN, "person.registered", after=items[1]["seq"])
    assert page == {"items": [items[2], items[4]], "next_cursor": items[4]["seq"]}

    # Encadeamento com `limit=2`: páginas de 2, 2 e 1, depois vazia.
    collected: list[dict] = []
    sizes: list[int] = []
    after = 0
    for _ in range(10):
        page = list_events(client, ADMIN_TOKEN, *types, after=after, limit=2)
        if not page["items"]:
            assert page["next_cursor"] == after
            break
        assert page["next_cursor"] == page["items"][-1]["seq"]
        assert page["items"][0]["seq"] > after
        sizes.append(len(page["items"]))
        collected.extend(page["items"])
        after = page["next_cursor"]
    else:
        pytest.fail("o encadeamento de cursores não terminou")
    assert sizes == [2, 2, 1]
    assert collected == items

    # Repetir a partir do último cursor: nada novo.
    again = list_events(client, ADMIN_TOKEN, *types, after=items[-1]["seq"])
    assert again == {"items": [], "next_cursor": items[-1]["seq"]}

    # Um evento novo aparece a partir do mesmo cursor, e só ele.
    newcomer = register_person(client, ADMIN_TOKEN, name="Pessoa Que Chegou Depois")
    later = list_events(client, ADMIN_TOKEN, *types, after=items[-1]["seq"])
    assert [_ref(e) for e in later["items"]] == [newcomer["pseudo_id"]]
    assert later["items"][0]["seq"] > items[-1]["seq"]
    assert later["next_cursor"] == later["items"][0]["seq"]


def test_limit_counts_only_requested_types(client):
    """com eventos de outro tipo intercalados (P1, K1, P2, K2, P3),
    o `limit` conta só os eventos dos tipos pedidos; `next_cursor` = `seq` do último item devolvido
    (e não o do último evento varrido); encadear os cursores percorre cada tipo sem perda nem
    repetição; a página vazia devolve `next_cursor == after`, mesmo com eventos de outro tipo depois
    do cursor."""
    people, tokens = _seed_interleaved(client)
    both = list_events(client, ADMIN_TOKEN, "person.registered", "token.created")["items"]
    p1, k1, p2, k2, p3 = both
    assert [_ref(e) for e in both] == [
        people[0]["pseudo_id"], tokens[0]["token_id"], people[1]["pseudo_id"],
        tokens[1]["token_id"], people[2]["pseudo_id"]]

    # Pessoas, de 2 em 2: P1 e P2 (o K1 entre eles não conta); depois P3 (o K2 não conta); depois nada.
    page = list_events(client, ADMIN_TOKEN, "person.registered", limit=2)
    assert page == {"items": [p1, p2], "next_cursor": p2["seq"]}
    page = list_events(client, ADMIN_TOKEN, "person.registered", after=p2["seq"], limit=2)
    assert page == {"items": [p3], "next_cursor": p3["seq"]}
    page = list_events(client, ADMIN_TOKEN, "person.registered", after=p3["seq"], limit=2)
    assert page == {"items": [], "next_cursor": p3["seq"]}

    # Um cursor que aponta para um evento de outro tipo: vem o próximo do tipo pedido.
    page = list_events(client, ADMIN_TOKEN, "person.registered", after=k1["seq"], limit=1)
    assert page == {"items": [p2], "next_cursor": p2["seq"]}

    # Tokens, de 1 em 1: K1; depois K2 (o P2 entre eles não conta); depois nada, e o cursor fica no
    # K2, embora o P3 venha depois dele.
    page = list_events(client, ADMIN_TOKEN, "token.created", limit=1)
    assert page == {"items": [k1], "next_cursor": k1["seq"]}
    page = list_events(client, ADMIN_TOKEN, "token.created", after=k1["seq"], limit=1)
    assert page == {"items": [k2], "next_cursor": k2["seq"]}
    page = list_events(client, ADMIN_TOKEN, "token.created", after=k2["seq"], limit=1)
    assert page == {"items": [], "next_cursor": k2["seq"]}


def test_default_limit_is_100(client):
    """Padrão "até o limite" (limite de 1 a 500, padrão 100; `after` padrão 0): 101
    eventos sem `limit` → os 100 primeiros, e a continuação traz o 101º; com `limit` acima de 100
    não há teto escondido: `limit=500` e `limit=101` → os 101 numa página só; `limit=100` → os 100
    primeiros."""
    people = [register_person(client, ADMIN_TOKEN, name=f"Pessoa Ficticia Numero {i:03d}")
              for i in range(101)]

    resp = _get_events(client, ADMIN_TOKEN, [("types", "person.registered")])
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("events.list", 200, body)
    assert [e["data"]["pseudo_id"] for e in body["items"]] == [p["pseudo_id"] for p in people[:100]]
    _assert_strictly_increasing([e["seq"] for e in body["items"]])
    assert body["next_cursor"] == body["items"][-1]["seq"]

    rest = list_events(client, ADMIN_TOKEN, "person.registered", after=body["next_cursor"])
    assert [e["data"]["pseudo_id"] for e in rest["items"]] == [people[100]["pseudo_id"]]
    assert rest["next_cursor"] == rest["items"][0]["seq"]

    # Os 101 eventos, lidos acima em duas páginas.
    every = body["items"] + rest["items"]
    assert len(every) == 101
    _assert_strictly_increasing([e["seq"] for e in every])

    # Um `limit` maior que 100 não é cortado em silêncio no padrão nem num teto interno.
    assert list_events(client, ADMIN_TOKEN, "person.registered", limit=500) == {
        "items": every, "next_cursor": every[-1]["seq"]}
    assert list_events(client, ADMIN_TOKEN, "person.registered", limit=101) == {
        "items": every, "next_cursor": every[-1]["seq"]}
    assert list_events(client, ADMIN_TOKEN, "person.registered", limit=100) == {
        "items": every[:100], "next_cursor": every[99]["seq"]}


def test_limit_bounds_1_and_500_are_accepted(client):
    """Limite de 1 a 500: os dois extremos são aceitos;
    `limit=1` devolve só o primeiro; `limit=500` devolve todos (aqui, 3). Que `limit=500` devolve
    mais de 100 eventos numa página, sem teto escondido, é conferido em
    `test_default_limit_is_100`, que já tem 101 eventos."""
    people = [register_person(client, ADMIN_TOKEN, name=f"Pessoa Do Limite {i}") for i in range(3)]
    everything = list_events(client, ADMIN_TOKEN, "person.registered")["items"]
    assert [e["data"]["pseudo_id"] for e in everything] == [p["pseudo_id"] for p in people]

    one = list_events(client, ADMIN_TOKEN, "person.registered", limit=1)
    assert one == {"items": everything[:1], "next_cursor": everything[0]["seq"]}

    big = list_events(client, ADMIN_TOKEN, "person.registered", limit=500)
    assert big == {"items": everything, "next_cursor": everything[-1]["seq"]}


def test_cursor_beyond_last_event_returns_empty(client):
    """Um cursor além do último evento devolve lista vazia, com
    `next_cursor == after`."""
    register_person(client, ADMIN_TOKEN, name="Pessoa Do Cursor Distante")
    last = all_events(client, ADMIN_TOKEN, "person.registered")[-1]["seq"]

    beyond = last + 1000
    assert list_events(client, ADMIN_TOKEN, "person.registered", after=beyond) == {
        "items": [], "next_cursor": beyond}
    assert list_events(client, ADMIN_TOKEN, "person.registered", after=last + 1) == {
        "items": [], "next_cursor": last + 1}


# ---------------------------------------------------------------------------------------------------
# Consultas inválidas de tipos e de cursor
# ---------------------------------------------------------------------------------------------------

_PR = ("types", "person.registered")

INVALID_QUERIES = [
    pytest.param([_PR, ("types", "tipo.inexistente")], [("types[1]", "invalid_value")],
                 ("tipo.inexistente",), id="unknown-type-second"),
    pytest.param([("types", "tipo.inexistente")], [("types[0]", "invalid_value")],
                 ("tipo.inexistente",), id="unknown-type-only"),
    pytest.param([_PR, _PR], [("types[1]", "duplicate")], (), id="duplicate-type"),
    pytest.param([("types", "token.created"), _PR, ("types", "token.created")],
                 [("types[2]", "duplicate")], (), id="duplicate-type-third"),
    pytest.param([], [("types", "missing")], (), id="missing-types"),
    pytest.param([("after", "0"), ("limit", "10")], [("types", "missing")], (),
                 id="missing-types-with-cursor"),
    pytest.param([_PR, ("limit", "0")], [("limit", "out_of_range")], (), id="limit-0"),
    pytest.param([_PR, ("limit", "501")], [("limit", "out_of_range")], (), id="limit-501"),
    pytest.param([_PR, ("limit", "x")], [("limit", "invalid_format")], (), id="limit-x"),
    pytest.param([_PR, ("limit", "1.5")], [("limit", "invalid_format")], (), id="limit-fraction"),
    pytest.param([_PR, ("limit", "abcdefgh")], [("limit", "invalid_format")], ("abcdefgh",),
                 id="limit-letters-no-echo"),
    pytest.param([_PR, ("after", "x")], [("after", "invalid_format")], (), id="after-x"),
    pytest.param([_PR, ("after", "zyxwvuts")], [("after", "invalid_format")], ("zyxwvuts",),
                 id="after-letters-no-echo"),
    pytest.param([_PR, ("after", "-1")], [("after", "out_of_range")], (), id="after-negative"),
    pytest.param([_PR, ("parametro_estranho", "valor_estranho")], [("query", "unknown_field")],
                 ("parametro_estranho", "valor_estranho"), id="unknown-param"),
    pytest.param([("types", "tipo.inexistente"), ("parametro_estranho", "1")],
                 [("types[0]", "invalid_value"), ("query", "unknown_field")],
                 ("tipo.inexistente", "parametro_estranho"), id="unknown-type-and-unknown-param"),
]


@pytest.mark.parametrize(("params", "fields", "sentinels"), INVALID_QUERIES)
def test_invalid_event_queries_are_rejected_without_echo(client, params, fields, sentinels):
    """Tipos e cursor inválidos, com os erros de `events.list`: tipo desconhecido →
    `invalid_value`; repetido → `duplicate` (a segunda ocorrência); sem `types` → `missing`; `limit`
    0/501 → `out_of_range`; não inteiro → `invalid_format`; `after=-1` → `out_of_range`; parâmetro
    desconhecido → `[{query, unknown_field}]`, no fim; nada ecoado."""
    register_person(client, ADMIN_TOKEN, name="Pessoa Da Consulta Invalida")
    resp = _get_events(client, ADMIN_TOKEN, params)
    assert_error(resp, 400, "invalid_request", fields=fields)
    if sentinels:
        assert_no_echo(resp, *sentinels)


# ---------------------------------------------------------------------------------------------------
# ordem por `seq`, horários em UTC
# ---------------------------------------------------------------------------------------------------


def test_clock_going_back_keeps_seq_order_and_recorded_times(client, clock):
    """Com o relógio voltando 1 h entre eventos, a ordem segue `seq` (a de
    gravação), e cada `occurred_at` sai como foi gravado."""
    t0 = clock.now()
    first = register_person(client, ADMIN_TOKEN, name="Pessoa Antes Do Recuo")
    clock.advance(-3600)
    t1 = clock.now()
    second = register_person(client, ADMIN_TOKEN, name="Pessoa Depois Do Recuo")
    clock.advance(-60)
    t2 = clock.now()
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token depois do recuo")
    assert t2 < t1 < t0
    assert (first["created_at"], second["created_at"], token["created_at"]) == (
        iso(t0), iso(t1), iso(t2))

    body = list_events(client, ADMIN_TOKEN, "person.registered", "token.created")
    items = body["items"]
    _assert_strictly_increasing([e["seq"] for e in items])
    assert _without_seq(items) == [
        {"type": "person.registered", "occurred_at": iso(t0), "actor_token_id": "bootstrap",
         "data": {"pseudo_id": first["pseudo_id"], "alias": first["alias"]}},
        {"type": "person.registered", "occurred_at": iso(t1), "actor_token_id": "bootstrap",
         "data": {"pseudo_id": second["pseudo_id"], "alias": second["alias"]}},
        {"type": "token.created", "occurred_at": iso(t2), "actor_token_id": "bootstrap",
         "data": {"token_id": token["token_id"], "label": "token depois do recuo",
                  "permissions": ["people:read"]}},
    ]
    # O cursor também segue `seq`: depois do primeiro vêm os dois mais "antigos" no relógio.
    after_first = list_events(client, ADMIN_TOKEN, "person.registered", "token.created",
                              after=items[0]["seq"])
    assert after_first == {"items": items[1:], "next_cursor": items[-1]["seq"]}


def test_times_leave_api_in_utc_with_z_under_database_timezone_sao_paulo(sql, db_url, app_factory,
                                                                          clock):
    """Horários na forma canônica: com
    `ALTER DATABASE … SET timezone = 'America/Sao_Paulo'` e o `FakeClock` em horários com fuso
    −03:00, todos os horários (respostas diretas e lidas do banco, inclusive `occurred_at`) saem
    iguais a `iso(dt)`, em UTC com `Z`."""
    dbname = pg_conninfo.conninfo_to_dict(db_url)["dbname"]
    sql.execute(f"ALTER DATABASE \"{dbname}\" SET timezone = 'America/Sao_Paulo'")
    with psycopg.connect(db_url, autocommit=True) as probe:
        assert probe.execute("SHOW timezone").fetchone()[0] == "America/Sao_Paulo"

    brt = timezone(timedelta(hours=-3))
    t_person = datetime(2031, 3, 10, 21, 30, 15, 123456, tzinfo=brt)
    t_token = datetime(2031, 3, 10, 22, 0, 0, tzinfo=brt)
    t_revoke = datetime(2031, 3, 10, 23, 59, 59, 5, tzinfo=brt)
    # Os horários cruzam a meia-noite em UTC e cobrem as duas formas canônicas (com e sem fração).
    assert (iso(t_person), iso(t_token), iso(t_revoke)) == (
        "2031-03-11T00:30:15.123456Z", "2031-03-11T01:00:00Z", "2031-03-11T02:59:59.000005Z")

    # O app só é criado depois do ALTER, para que toda conexão dele nasça com o fuso alterado.
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        clock.set(t_person)
        person = register_person(c, ADMIN_TOKEN, name="Pessoa Do Fuso Horario")
        clock.set(t_token)
        _, token = create_token(c, ADMIN_TOKEN, ["people:read"], label="token do fuso")
        clock.set(t_revoke)
        revoked = c.post(f"/v1/tokens/{token['token_id']}/revoke", headers=auth(ADMIN_TOKEN))
        assert revoked.status_code == 200, revoked.text[:300]
        assert_matches("tokens.revoke", 200, revoked.json())

        assert person["created_at"] == iso(t_person)
        assert token["created_at"] == iso(t_token)
        expected_token = {"token_id": token["token_id"], "label": "token do fuso",
                          "permissions": ["people:read"], "created_at": iso(t_token),
                          "revoked_at": iso(t_revoke)}
        assert revoked.json() == expected_token

        got = c.get(f"/v1/people/{person['alias']}", headers=auth(ADMIN_TOKEN))
        assert got.status_code == 200, got.text[:300]
        assert_matches("people.get", 200, got.json())
        assert got.json() == person

        listed = c.get("/v1/people", headers=auth(ADMIN_TOKEN))
        assert listed.status_code == 200, listed.text[:300]
        assert_matches("people.list", 200, listed.json())
        assert listed.json()["items"] == [person]

        tokens = c.get("/v1/tokens", headers=auth(ADMIN_TOKEN))
        assert tokens.status_code == 200, tokens.text[:300]
        assert_matches("tokens.list", 200, tokens.json())
        assert tokens.json() == {"items": [expected_token]}

        events = list_events(c, ADMIN_TOKEN, "person.registered", "token.created", "token.revoked")
        assert [(e["type"], e["occurred_at"]) for e in events["items"]] == [
            ("person.registered", iso(t_person)),
            ("token.created", iso(t_token)),
            ("token.revoked", iso(t_revoke)),
        ]


# ---------------------------------------------------------------------------------------------------
# Permissão e esquema
# ---------------------------------------------------------------------------------------------------


def test_events_list_requires_events_read(client):
    """`events.list` exige `events:read` (ordem: autenticação, validação, permissão): um token com
    todas as outras permissões → 403 `events:read`, mas um pedido inválido dele → 400; sem token →
    401; um token só com `events:read` lê o mesmo que o token inicial. Todas as
    outras permissões incluem `bots:manage`, `offerings:read` e `offerings:write`."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Da Permissao")
    others = [p for p in ALL_PERMISSIONS if p != "events:read"]
    no_read, _ = create_token(client, ADMIN_TOKEN, others, label="sem leitura de eventos")
    reader, _ = create_token(client, ADMIN_TOKEN, ["events:read"], label="leitor de eventos")

    resp = _get_events(client, no_read, [_PR])
    assert_error(resp, 403, "forbidden", permission="events:read")

    resp = _get_events(client, no_read, [])
    assert_error(resp, 400, "invalid_request", fields=[("types", "missing")])

    resp = _get_events(client, None, [_PR])
    assert_error(resp, 401, "unauthenticated")
    resp = _get_events(client, None, [("types", "tipo.inexistente")])
    assert_error(resp, 401, "unauthenticated")

    as_reader = list_events(client, reader, "person.registered")
    assert [e["data"]["pseudo_id"] for e in as_reader["items"]] == [person["pseudo_id"]]
    assert as_reader == list_events(client, ADMIN_TOKEN, "person.registered")


def test_every_event_type_matches_frozen_schema(client, clock, queue, outbox, db_url):
    """Os 7 tipos, com o esquema com `oneOf` por tipo: um evento
    de cada tipo sai pela API, cada um valida contra o esquema congelado, e os de tarefa e de
    mensagem saem exatos, com `actor_token_id: null`. A consulta pede também os 14 tipos
    de oferecimentos e bots (cada um sai no seu fluxo em `test_events_offerings_bots.py`), e só os 7 da API base existem aqui. A
    consulta pede também os 8 tipos de aluno (cada um sai no seu
    fluxo nos testes do vínculo do aluno), e a lista devolvida continua só com os 7 da API base."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Do Esquema De Eventos")
    clock.advance(1)
    secret, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="rotulo do esquema")
    clock.advance(1)
    denied = client.post("/v1/identity/reveal",
                         json={"pseudonyms": [person["alias"]], "reason": "motivo do esquema"},
                         headers=auth(secret))
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    clock.advance(1)
    revealed = client.post("/v1/identity/reveal",
                           json={"pseudonyms": [person["alias"]], "reason": "motivo do esquema"},
                           headers=auth(ADMIN_TOKEN))
    assert revealed.status_code == 200, revealed.text[:300]
    clock.advance(1)
    revoked = client.post(f"/v1/tokens/{token['token_id']}/revoke", headers=auth(ADMIN_TOKEN))
    assert revoked.status_code == 200, revoked.text[:300]

    clock.advance(1)
    task_id = queue.enqueue("demo.esquema", {"n": 1})
    claimed = queue.claim("worker-esquema")
    assert claimed is not None and claimed.id == task_id
    assert queue.fail(task_id, "worker-esquema", "falha definitiva de teste", permanent=True) is True
    at_task = iso(clock.now())

    clock.advance(1)
    with psycopg.connect(db_url) as conn:
        message_id = outbox.enqueue(conn, recipient=uuid.UUID(person["pseudo_id"]),
                                    text="mensagem do esquema")
    channel = ScriptedChannel([Undeliverable("destino recusou a entrega")])
    assert outbox.dispatch_once(channel, "worker-esquema") == message_id
    at_outbox = iso(clock.now())

    body = list_events(client, ADMIN_TOKEN, *ALL_TYPES)
    items = body["items"]
    assert [e["type"] for e in items] == [
        "person.registered", "token.created", "identity.reveal_denied", "identity.revealed",
        "token.revoked", "task.failed", "outbox.failed"]
    _assert_strictly_increasing([e["seq"] for e in items])
    for item in items:
        assert_matches("events.list", 200, {"items": [item], "next_cursor": item["seq"]})
    assert _without_seq(items[5:]) == [
        {"type": "task.failed", "occurred_at": at_task, "actor_token_id": None,
         "data": {"task_id": task_id, "task_type": "demo.esquema", "attempts": 1,
                  "error": "falha definitiva de teste"}},
        {"type": "outbox.failed", "occurred_at": at_outbox, "actor_token_id": None,
         "data": {"message_id": message_id, "recipient": person["pseudo_id"], "attempts": 1,
                  "error": "destino recusou a entrega"}},
    ]
