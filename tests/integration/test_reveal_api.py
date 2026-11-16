"""Revelação de identidade pela API.

Cobre a revelação de identidade e os seus eventos, com o escopo da revelação.
Contratos: o cabeçalho do cliente, os erros, a rota de revelação e os dados dos eventos.
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_token, marker, register_person
from tests.helpers.canary import CANARY_PEOPLE, OLIMPIO, URSULA, ZEFIRO, CanaryPerson, find_canary
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo

REVEAL = "/v1/identity/reveal"
IDENTITY_TYPES = ("identity.revealed", "identity.reveal_denied")
ALL_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
             "identity.reveal_denied", "task.failed", "outbox.failed")

# Todas as permissões menos `identity:reveal`: a revelação com este token é negada.
NO_REVEAL = ["events:read", "people:read", "people:write", "tokens:manage"]

# Pessoa fictícia extra (fora do canário), com nome, RA e e-mail, para o caso "de outra pessoa".
TEODORO = CanaryPerson("Teodoro Jabuticaba Lindqvist", "9900778899",
                       "teodoro.jabuticaba@exemplo.example.com")

# Apelidos de 4 caracteres usados como "inexistentes" (o primeiro que não estiver em uso).
_FREE_ALIAS_CANDIDATES = ("beef", "cafe", "dead", "face", "fade", "bead")


def _register(client: Any, person: CanaryPerson) -> dict:
    return register_person(client, ADMIN_TOKEN, **person.payload())


def _revealed(registered: dict, person: CanaryPerson) -> dict:
    """Item esperado em `people` da revelação."""
    return {"pseudo_id": registered["pseudo_id"], "alias": registered["alias"], "name": person.name,
            "ra": person.ra, "email": person.email}


def _reveal(client: Any, token: str | None, pseudonyms: Any, reason: Any,
            client_kind: str | None = "cli") -> Any:
    return client.post(REVEAL, json={"pseudonyms": pseudonyms, "reason": reason},
                       headers=auth(token, client_kind))


def _taken(registered: list[dict]) -> set[str]:
    return {b["alias"] for b in registered} | {b["pseudo_id"] for b in registered}


def _assert_not_taken(value: str, registered: list[dict]) -> None:
    assert value not in _taken(registered), f"pré-condição: {value!r} não pode ser pseudônimo existente"


def _free_alias(registered: list[dict], skip: tuple[str, ...] = ()) -> str:
    taken = _taken(registered)
    for candidate in _FREE_ALIAS_CANDIDATES:
        if candidate not in taken and candidate not in skip:
            return candidate
    raise AssertionError("nenhum apelido livre entre os candidatos")


def _strip_seq(events: list[dict]) -> list[dict]:
    """Tira o `seq` (valor de sequência, nunca afirmado por valor), conferindo que cresce."""
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _identity_events(client: Any) -> list[dict]:
    return _strip_seq(all_events(client, ADMIN_TOKEN, *IDENTITY_TYPES))


def _identity_event_count(sql: Any) -> int:
    return sql.execute(
        "SELECT count(*) FROM events WHERE type IN ('identity.revealed', 'identity.reveal_denied')"
    ).fetchone()[0]


def _events_text(sql: Any) -> str:
    """Todas as linhas de `events`, cada uma como texto (todas as colunas)."""
    return "\n".join(row[0] for row in sql.execute("SELECT e::text FROM events e ORDER BY seq"))


def _identity_data_text(sql: Any) -> list[str]:
    return [row[0] for row in sql.execute(
        "SELECT data::text FROM events WHERE type IN ('identity.revealed', 'identity.reveal_denied') "
        "ORDER BY seq")]


# ---------------------------------------------------------------------------------------------
# Resposta exata
# ---------------------------------------------------------------------------------------------


def test_mixed_list_returns_each_person_once_in_first_reference_order(client: Any) -> None:
    """Escopo da revelação: lista mista (apelidos, UUIDs,
    inexistentes, texto repetido, a mesma pessoa pelos dois e um prefixo mais longo que o apelido)
    → `people` uma vez por pessoa, na ordem da primeira referência; `not_found` sem repetição, na
    ordem do pedido; `null` nos campos ausentes."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    o = _register(client, OLIMPIO)
    registered = [z, u, o]
    missing_alias = _free_alias(registered)
    missing_uuid = "0badc0de-0000-4000-8000-000000000001"
    longer_prefix = z["pseudo_id"].replace("-", "")[: len(z["alias"]) + 2]
    for value in (missing_uuid, longer_prefix):
        _assert_not_taken(value, registered)

    pseudonyms = [
        o["alias"], missing_alias, z["pseudo_id"], o["pseudo_id"], missing_alias, u["alias"],
        z["alias"], missing_uuid, longer_prefix, u["alias"], missing_uuid,
    ]
    resp = _reveal(client, ADMIN_TOKEN, pseudonyms, "conferencia de cadastro da turma")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_matches("identity.reveal", 200, body)
    assert body == {
        "people": [_revealed(o, OLIMPIO), _revealed(z, ZEFIRO), _revealed(u, URSULA)],
        "not_found": [missing_alias, missing_uuid, longer_prefix],
    }


# ---------------------------------------------------------------------------------------------
# Evento identity.revealed
# ---------------------------------------------------------------------------------------------


def test_revealed_event_is_exact_for_bootstrap_and_created_token(client: Any, clock: Any) -> None:
    """cada revelação gera um `identity.revealed` exato
    (`{client, revealed, not_found_count, reason}`), com `actor_token_id` `bootstrap` ou o
    `token_id` do token criado, `occurred_at` do relógio e a justificativa aparada; a revelação sem
    ninguém encontrado também é registrada."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    missing = _free_alias([z, u])
    secret, token = create_token(client, ADMIN_TOKEN, ["identity:reveal"], label="token revelador")

    clock.advance(60)
    first_at = iso(clock.now())
    r1 = _reveal(client, ADMIN_TOKEN, [u["alias"], missing, z["pseudo_id"], u["pseudo_id"], missing],
                 "  conferencia de notas da turma  ", client_kind="cli")
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"people": [_revealed(u, URSULA), _revealed(z, ZEFIRO)], "not_found": [missing]}

    clock.advance(3600)
    second_at = iso(clock.now())
    r2 = _reveal(client, secret, [z["alias"]], "revisao de frequencia", client_kind="mcp")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"people": [_revealed(z, ZEFIRO)], "not_found": []}

    clock.advance(60)
    third_at = iso(clock.now())
    r3 = _reveal(client, secret, [missing], "busca sem resultado", client_kind=None)
    assert r3.status_code == 200, r3.text
    assert_matches("identity.reveal", 200, r3.json())
    assert r3.json() == {"people": [], "not_found": [missing]}

    assert _identity_events(client) == [
        {"type": "identity.revealed", "occurred_at": first_at, "actor_token_id": "bootstrap",
         "data": {"client": "cli", "revealed": [u["pseudo_id"], z["pseudo_id"]], "not_found_count": 1,
                  "reason": "conferencia de notas da turma"}},
        {"type": "identity.revealed", "occurred_at": second_at, "actor_token_id": token["token_id"],
         "data": {"client": "mcp", "revealed": [z["pseudo_id"]], "not_found_count": 0,
                  "reason": "revisao de frequencia"}},
        {"type": "identity.revealed", "occurred_at": third_at, "actor_token_id": token["token_id"],
         "data": {"client": "other", "revealed": [], "not_found_count": 1,
                  "reason": "busca sem resultado"}},
    ]


# ---------------------------------------------------------------------------------------------
# Itens não encontrados não são guardados
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("owner_registered", [False, True], ids=["ra-sem-dono", "ra-de-pessoa-cadastrada"])
def test_not_found_items_are_never_stored(client: Any, sql: Any, clock: Any,
                                          owner_registered: bool) -> None:
    """Contagem de não encontrados (`not_found_count`): itens com formato de pseudônimo que
    carregam o RA canário (`<RA>a` e um UUID v4 com o RA no último grupo) voltam em `not_found`,
    mas os eventos (concedido e negado) só guardam a contagem, e o RA não aparece em `events`."""
    u = _register(client, URSULA)
    registered = [u]
    if owner_registered:
        z = _register(client, ZEFIRO)
        registered.append(z)
    alias_with_ra = f"{ZEFIRO.ra}a"
    uuid_with_ra = f"c0ffee00-0000-4000-8000-{ZEFIRO.ra}ab"
    assert uuid.UUID(uuid_with_ra).version == 4 and str(uuid.UUID(uuid_with_ra)) == uuid_with_ra
    for value in (alias_with_ra, uuid_with_ra):
        _assert_not_taken(value, registered)
    denied_secret, denied_token = create_token(client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")
    reason = "conferencia de itens digitados"

    granted = _reveal(client, ADMIN_TOKEN, [alias_with_ra, u["alias"], uuid_with_ra], reason)
    assert granted.status_code == 200, granted.text
    assert granted.json() == {"people": [_revealed(u, URSULA)], "not_found": [alias_with_ra, uuid_with_ra]}

    denied = _reveal(client, denied_secret, [uuid_with_ra, u["pseudo_id"], alias_with_ra], reason)
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    assert_no_echo(denied, alias_with_ra, uuid_with_ra, ZEFIRO.ra)

    now = iso(clock.now())
    assert _identity_events(client) == [
        {"type": "identity.revealed", "occurred_at": now, "actor_token_id": "bootstrap",
         "data": {"client": "cli", "revealed": [u["pseudo_id"]], "not_found_count": 2, "reason": reason}},
        {"type": "identity.reveal_denied", "occurred_at": now, "actor_token_id": denied_token["token_id"],
         "data": {"client": "cli", "requested": [u["pseudo_id"]], "not_found_count": 2}},
    ]
    stored = _events_text(sql)
    data_text = "\n".join(_identity_data_text(sql))
    for text in (stored, data_text):
        assert ZEFIRO.ra not in text
        assert alias_with_ra not in text and uuid_with_ra not in text
        assert find_canary(text) == []
    if owner_registered:
        assert marker(z["alias"]) not in stored


# ---------------------------------------------------------------------------------------------
# Cliente declarado
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("header", "recorded"), [("cli", "cli"), ("mcp", "mcp"), ("other", "other"),
                                                  (None, "other")],
                         ids=["cli", "mcp", "other", "ausente"])
def test_declared_client_is_recorded(client: Any, clock: Any, header: str | None, recorded: str) -> None:
    """Contrato (`X-CQ-Client`): o cliente declarado vai para o evento da
    revelação concedida e da negada; ausente vale `other`."""
    z = _register(client, ZEFIRO)
    secret, token = create_token(client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")

    granted = _reveal(client, ADMIN_TOKEN, [z["alias"]], "conferencia de cliente", client_kind=header)
    assert granted.status_code == 200, granted.text
    assert granted.json() == {"people": [_revealed(z, ZEFIRO)], "not_found": []}
    denied = _reveal(client, secret, [z["pseudo_id"]], "conferencia de cliente", client_kind=header)
    assert_error(denied, 403, "forbidden", permission="identity:reveal")

    at = iso(clock.now())
    assert _identity_events(client) == [
        {"type": "identity.revealed", "occurred_at": at, "actor_token_id": "bootstrap",
         "data": {"client": recorded, "revealed": [z["pseudo_id"]], "not_found_count": 0,
                  "reason": "conferencia de cliente"}},
        {"type": "identity.reveal_denied", "occurred_at": at, "actor_token_id": token["token_id"],
         "data": {"client": recorded, "requested": [z["pseudo_id"]], "not_found_count": 0}},
    ]


@pytest.mark.parametrize(("header", "sentinels"), [("xyzxyzxy", ("xyzxyzxy",)), ("CLI", ())],
                         ids=["desconhecido", "maiusculas"])
def test_invalid_declared_client_is_400_without_event(client: Any, sql: Any, header: str,
                                                      sentinels: tuple[str, ...]) -> None:
    """`X-CQ-Client` fora de `cli`/`mcp`/`other` (diferencia
    maiúsculas) → 400 `[{X-CQ-Client, invalid_value}]`, sem eco e sem evento."""
    z = _register(client, ZEFIRO)
    resp = _reveal(client, ADMIN_TOKEN, [z["alias"]], "conferencia de cliente", client_kind=header)
    assert_error(resp, 400, "invalid_request", fields=[("X-CQ-Client", "invalid_value")])
    assert_no_echo(resp, *sentinels)
    assert _identity_event_count(sql) == 0


# ---------------------------------------------------------------------------------------------
# Redação na gravação e auditoria sem identidade
# ---------------------------------------------------------------------------------------------


def test_reason_is_redacted_when_written(client: Any, sql: Any) -> None:
    """a justificativa com nome, RA e e-mail da pessoa
    revelada e de outra pessoa cadastrada é gravada em `events.data` (SQL) e devolvida pela API só
    com `marker(alias)`."""
    z = _register(client, ZEFIRO)
    t = _register(client, TEODORO)
    mz, mt = marker(z["alias"]), marker(t["alias"])
    reason = (f"conferir {ZEFIRO.name}, RA {ZEFIRO.ra}, e-mail {ZEFIRO.email}; comparar com "
              f"{TEODORO.name}, RA {TEODORO.ra}, e-mail {TEODORO.email}")
    expected = f"conferir {mz}, RA {mz}, e-mail {mz}; comparar com {mt}, RA {mt}, e-mail {mt}"

    resp = _reveal(client, ADMIN_TOKEN, [z["alias"]], reason)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"people": [_revealed(z, ZEFIRO)], "not_found": []}

    stored = [row[0] for row in sql.execute(
        "SELECT data FROM events WHERE type = 'identity.revealed' ORDER BY seq")]
    assert stored == [{"client": "cli", "revealed": [z["pseudo_id"]], "not_found_count": 0,
                       "reason": expected}]
    listed = all_events(client, ADMIN_TOKEN, "identity.revealed")
    assert [e["data"] for e in listed] == stored


def test_audit_trail_has_no_identity_of_registered_people(client: Any, sql: Any) -> None:
    """com as três pessoas canário cadastradas, uma revelação
    concedida e uma negada (justificativa cheia de identidades) geram exatamente dois registros de
    auditoria, e nenhuma identidade aparece em `events.data::text` nem nas linhas de `events`."""
    registered = [_register(client, p) for p in CANARY_PEOPLE]
    z, u, o = registered
    mz, mu, mo = (marker(b["alias"]) for b in registered)
    secret, _ = create_token(client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")
    refs = [b["alias"] for b in registered] + [b["pseudo_id"] for b in registered]
    reason = (f"auditar {ZEFIRO.name}, RA {ZEFIRO.ra}, {ZEFIRO.email}, {URSULA.name}, RA {URSULA.ra}, "
              f"{OLIMPIO.name} e {OLIMPIO.email}")

    granted = _reveal(client, ADMIN_TOKEN, refs, reason)
    assert granted.status_code == 200, granted.text
    assert granted.json() == {"people": [_revealed(z, ZEFIRO), _revealed(u, URSULA), _revealed(o, OLIMPIO)],
                              "not_found": []}
    assert find_canary(granted.text) != [], "controle positivo: o detector acha a identidade revelada"
    denied = _reveal(client, secret, refs, reason)
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    assert find_canary(denied.text) == []

    pids = [b["pseudo_id"] for b in registered]
    assert [(e["type"], e["data"]) for e in _identity_events(client)] == [
        ("identity.revealed", {"client": "cli", "revealed": pids, "not_found_count": 0,
                               "reason": f"auditar {mz}, RA {mz}, {mz}, {mu}, RA {mu}, {mo} e {mo}"}),
        ("identity.reveal_denied", {"client": "cli", "requested": pids, "not_found_count": 0}),
    ]
    audit = _identity_data_text(sql)
    assert len(audit) == 2
    for text in [*audit, _events_text(sql)]:
        assert find_canary(text) == []
        for person in CANARY_PEOPLE:
            for value in (person.name, person.ra, person.email):
                if value is not None:
                    assert value not in text


# ---------------------------------------------------------------------------------------------
# Revelação negada
# ---------------------------------------------------------------------------------------------


def test_denied_reveal_records_event_without_reason(client: Any, sql: Any, clock: Any) -> None:
    """token sem `identity:reveal` (com todas as outras permissões) → 403
    nomeando `identity:reveal`, corpo sem identidade, um `identity.reveal_denied` exato
    `{client, requested, not_found_count}` e **nenhum** vestígio da justificativa nos eventos, nem
    as marcas das pessoas citadas nela."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    missing = _free_alias([z, u])
    secret, token = create_token(client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")
    clock.advance(120)
    sentinel = "justificativa-sentinela-negada"
    reason = f"{sentinel}: {ZEFIRO.name} RA {ZEFIRO.ra} e {URSULA.name} RA {URSULA.ra}"

    resp = _reveal(client, secret, [u["pseudo_id"], missing, z["alias"], u["alias"], missing], reason,
                   client_kind="mcp")

    assert_error(resp, 403, "forbidden", permission="identity:reveal")
    assert_no_echo(resp, sentinel, ZEFIRO.name, ZEFIRO.ra, URSULA.name, URSULA.ra)
    assert find_canary(resp.text) == []
    assert _identity_events(client) == [
        {"type": "identity.reveal_denied", "occurred_at": iso(clock.now()),
         "actor_token_id": token["token_id"],
         "data": {"client": "mcp", "requested": [u["pseudo_id"], z["pseudo_id"]], "not_found_count": 1}},
    ]
    stored = _events_text(sql)
    listed = json.dumps(all_events(client, ADMIN_TOKEN, *ALL_TYPES), ensure_ascii=False)
    for text in (stored, listed):
        assert sentinel not in text
        assert marker(z["alias"]) not in text and marker(u["alias"]) not in text
        assert find_canary(text) == []


# ---------------------------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------------------------

_REASON_501 = ("motivo-sentinela-" * 30)[:501]
_UPPER_UUID = "DEADBEEF-CAFE-4BAD-8BAD-0123456789AB"
_UUID_V1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
_HEX_33 = "fedcba9876543210fedcba9876543210f"
_INVALID_ITEMS_SENTINELS = ("ABCDEF12", "12345678", _UPPER_UUID, _UUID_V1, _HEX_33, "Nome Digitado Qualquer")


def _distinct_aliases(n: int) -> list[str]:
    return [f"ab{i:010x}" for i in range(n)]


VALIDATION_CASES = [
    pytest.param(lambda ok: {"json": {"pseudonyms": [], "reason": "motivo valido"}},
                 [("pseudonyms", "too_few")], (), id="lista-vazia"),
    pytest.param(lambda ok: {"json": {"pseudonyms": _distinct_aliases(101), "reason": "motivo valido"}},
                 [("pseudonyms", "too_many")], (), id="101-distintos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok] * 101, "reason": "motivo valido"}},
                 [("pseudonyms", "too_many")], (), id="101-repetidos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": ["NAO-E-PSEUDONIMO"] * 101, "reason": "motivo valido"}},
                 [("pseudonyms", "too_many")], ("NAO-E-PSEUDONIMO",), id="101-invalidos-sem-validar-itens"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [
        ok, "ABCDEF12", ok, "12345678", _UPPER_UUID, "abc", "", 123, _UUID_V1, _HEX_33,
        "Nome Digitado Qualquer", "ABCD"], "reason": "motivo valido"}},
                 [(f"pseudonyms[{i}]", "invalid_format") for i in (1, 3, 4, 5, 6, 7, 8, 9, 10, 11)],
                 _INVALID_ITEMS_SENTINELS, id="itens-invalidos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": "x", "reason": "motivo valido"}},
                 [("pseudonyms", "invalid_format")], (), id="pseudonyms-texto"),
    pytest.param(lambda ok: {"json": {"pseudonyms": None, "reason": "motivo valido"}},
                 [("pseudonyms", "missing")], (), id="pseudonyms-null"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok]}},
                 [("reason", "missing")], (), id="sem-reason"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": None}},
                 [("reason", "missing")], (), id="reason-null"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": ""}},
                 [("reason", "too_short")], (), id="reason-vazia"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": "   "}},
                 [("reason", "too_short")], (), id="reason-so-espacos"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": _REASON_501}},
                 [("reason", "too_long")], (_REASON_501, _REASON_501[:40]), id="reason-501"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": f"  {_REASON_501}  "}},
                 [("reason", "too_long")], (_REASON_501[:40],), id="reason-501-depois-de-aparar"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": 123}},
                 [("reason", "invalid_format")], (), id="reason-numero"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok], "reason": "motivo valido",
                                      "campo_extra_sentinela": "valor-extra-sentinela"}},
                 [("body", "unknown_field")], ("campo_extra_sentinela", "valor-extra-sentinela"),
                 id="campo-extra"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [], "reason": "   ", "outro_campo_sentinela": 1}},
                 [("pseudonyms", "too_few"), ("reason", "too_short"), ("body", "unknown_field")],
                 ("outro_campo_sentinela",), id="varios-erros-campo-extra-no-fim"),
    pytest.param(lambda ok: {"json": {"pseudonyms": [ok, "ABCDEF12"], "reason": "   "}},
                 [("pseudonyms[1]", "invalid_format"), ("reason", "too_short")], ("ABCDEF12",),
                 id="item-e-reason-na-ordem-do-esquema"),
    pytest.param(lambda ok: {"json": {"pseudonyms": "x", "reason": 123}},
                 [("pseudonyms", "invalid_format"), ("reason", "invalid_format")], (),
                 id="dois-tipos-errados"),
    pytest.param(lambda ok: {"json": {}},
                 [("pseudonyms", "missing"), ("reason", "missing")], (), id="objeto-vazio"),
    pytest.param(lambda ok: {"content": b""},
                 [("body", "missing")], (), id="corpo-vazio"),
    pytest.param(lambda ok: {"content": b'{"pseudonyms": ["sentinela-json-quebrado"'},
                 [("body", "invalid_format")], ("sentinela-json-quebrado",), id="corpo-nao-json"),
    pytest.param(lambda ok: {"content": b'["sentinela-lista-json"]'},
                 [("body", "invalid_format")], ("sentinela-lista-json",), id="corpo-lista"),
    # JSON que não é UTF-8: o de um cliente em latin-1/cp1252 e uma sequência inválida.
    pytest.param(lambda ok: {"content": json.dumps({"pseudonyms": [ok], "reason": "razão sentinela-latina"},
                                                   ensure_ascii=False).encode("latin-1")},
                 [("body", "invalid_format")], ("razão sentinela-latina", "sentinela-latina"),
                 id="corpo-latin1"),
    pytest.param(lambda ok: {"content": b"\xc3\x28"},
                 [("body", "invalid_format")], (), id="corpo-utf8-invalido"),
]


@pytest.mark.parametrize(("build", "fields", "sentinels"), VALIDATION_CASES)
def test_reveal_rejects_invalid_request(client: Any, sql: Any, build: Callable[[str], dict],
                                        fields: list[tuple[str, str]], sentinels: tuple[str, ...]) -> None:
    """Escopo da revelação; contrato: cada entrada
    inválida → 400 com os `fields` exatos (um por item inválido; lista fora do limite sem validar os
    itens; na ordem do esquema; campo extra no fim), sem eco e sem evento."""
    z = _register(client, ZEFIRO)
    kwargs = build(z["alias"])
    headers = auth(ADMIN_TOKEN, "cli")
    if "content" in kwargs:
        headers["Content-Type"] = "application/json"
    resp = client.post(REVEAL, headers=headers, **kwargs)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *sentinels)
    assert find_canary(resp.text) == []
    assert _identity_event_count(sql) == 0


def test_reveal_accepts_limits(client: Any, clock: Any) -> None:
    """Escopo da revelação: 100 itens (repetidos ou distintos) e a
    justificativa de 1 caractere ou de 500 depois de aparada são aceitos; o evento traz a
    justificativa aparada e a contagem de não encontrados."""
    z = _register(client, ZEFIRO)
    missing = _distinct_aliases(99)
    for value in missing:
        _assert_not_taken(value, [z])
    reason_500 = ("limite-" * 80)[:500]
    assert len(reason_500) == 500

    r1 = _reveal(client, ADMIN_TOKEN, [z["alias"]] * 100, "x")
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"people": [_revealed(z, ZEFIRO)], "not_found": []}
    r2 = _reveal(client, ADMIN_TOKEN, [z["pseudo_id"], *missing], f"  {reason_500}  ")
    assert r2.status_code == 200, r2.text
    assert_matches("identity.reveal", 200, r2.json())
    assert r2.json() == {"people": [_revealed(z, ZEFIRO)], "not_found": missing}

    at = iso(clock.now())
    assert _identity_events(client) == [
        {"type": "identity.revealed", "occurred_at": at, "actor_token_id": "bootstrap",
         "data": {"client": "cli", "revealed": [z["pseudo_id"]], "not_found_count": 0, "reason": "x"}},
        {"type": "identity.revealed", "occurred_at": at, "actor_token_id": "bootstrap",
         "data": {"client": "cli", "revealed": [z["pseudo_id"]], "not_found_count": 99,
                  "reason": reason_500}},
    ]


# ---------------------------------------------------------------------------------------------
# Ordem das verificações: autenticação → validação → permissão
# ---------------------------------------------------------------------------------------------

ORDER_400_CASES = [
    pytest.param(lambda ok: {"pseudonyms": [], "reason": "motivo valido"}, "cli",
                 [("pseudonyms", "too_few")], id="lista-vazia"),
    pytest.param(lambda ok: {"pseudonyms": [ok, "ABCDEF12"], "reason": "motivo valido"}, "cli",
                 [("pseudonyms[1]", "invalid_format")], id="item-invalido"),
    pytest.param(lambda ok: {"pseudonyms": [ok]}, "cli", [("reason", "missing")], id="sem-reason"),
    pytest.param(lambda ok: {"pseudonyms": [ok], "reason": "motivo valido", "campo_extra_sentinela": 1},
                 "cli", [("body", "unknown_field")], id="campo-extra"),
    pytest.param(lambda ok: {"pseudonyms": [ok], "reason": "motivo valido"}, "xyzxyzxy",
                 [("X-CQ-Client", "invalid_value")], id="cliente-invalido"),
]


@pytest.mark.parametrize(("build", "client_kind", "fields"), ORDER_400_CASES)
def test_invalid_request_without_permission_is_400_without_event(
        client: Any, sql: Any, clock: Any, build: Callable[[str], dict], client_kind: str,
        fields: list[tuple[str, str]]) -> None:
    """Validação antes da permissão: pedido inválido com token sem
    `identity:reveal` → 400 (não 403) e nenhum evento; o mesmo token com pedido válido é negado e
    registrado (controle)."""
    z = _register(client, ZEFIRO)
    secret, token = create_token(client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")

    resp = client.post(REVEAL, json=build(z["alias"]), headers=auth(secret, client_kind))
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert _identity_event_count(sql) == 0

    control = _reveal(client, secret, [z["alias"]], "motivo valido")
    assert_error(control, 403, "forbidden", permission="identity:reveal")
    assert _identity_events(client) == [
        {"type": "identity.reveal_denied", "occurred_at": iso(clock.now()),
         "actor_token_id": token["token_id"],
         "data": {"client": "cli", "requested": [z["pseudo_id"]], "not_found_count": 0}},
    ]


_UNKNOWN_TOKEN = "token-desconhecido-sentinela-0123456789abcdef"

ORDER_401_CASES = [
    pytest.param({"X-CQ-Client": "cli"}, lambda ok: {"pseudonyms": [ok], "reason": "motivo valido"}, (),
                 id="sem-token"),
    pytest.param({"X-CQ-Client": "cli"}, lambda ok: {"pseudonyms": [], "reason": "   "}, (),
                 id="sem-token-corpo-invalido"),
    pytest.param({"X-CQ-Client": "xyzxyzxy"}, lambda ok: {"pseudonyms": [ok], "reason": "motivo valido"},
                 ("xyzxyzxy",), id="sem-token-cliente-invalido"),
    pytest.param({"Authorization": f"Bearer {_UNKNOWN_TOKEN}", "X-CQ-Client": "cli"},
                 lambda ok: {"pseudonyms": [ok], "reason": "motivo valido"}, (_UNKNOWN_TOKEN,),
                 id="token-desconhecido"),
    pytest.param({"Authorization": "Basic dXN1YXJpbzpzZW5oYQ==", "X-CQ-Client": "cli"},
                 lambda ok: {"pseudonyms": [ok], "reason": "motivo valido"}, ("dXN1YXJpbzpzZW5oYQ==",),
                 id="esquema-basic"),
]


@pytest.mark.parametrize(("headers", "build", "sentinels"), ORDER_401_CASES)
def test_unauthenticated_reveal_is_401_without_event(client: Any, sql: Any, headers: dict[str, str],
                                                     build: Callable[[str], dict],
                                                     sentinels: tuple[str, ...]) -> None:
    """Autenticação antes de tudo: sem token, com token desconhecido ou com outro esquema
    → 401 exato, mesmo com corpo ou cliente inválidos, sem eco e sem nenhum evento."""
    z = _register(client, ZEFIRO)
    resp = client.post(REVEAL, json=build(z["alias"]), headers=headers)
    assert_error(resp, 401, "unauthenticated")
    assert_no_echo(resp, *sentinels)
    assert _identity_event_count(sql) == 0
