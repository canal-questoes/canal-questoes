"""Pessoas: cadastro, listagem, consulta e pseudônimos.

Cobre o cadastro, a listagem, a consulta e os pseudônimos; pessoa
repetida, normalização, colisão de apelido e entrada estrita.


Dados só fictícios (tests/helpers/canary.py e nomes inventados).
"""

from __future__ import annotations

import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cq_server import people as people_core
from tests.helpers.api import ADMIN_TOKEN, auth, create_token, register_person, uuid4_with_prefix
from tests.helpers.canary import OLIMPIO, URSULA, ZEFIRO, find_canary
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo

ADMIN = auth(ADMIN_TOKEN)

# Formatos do apelido e do UUID (os mesmos padrões dos esquemas congelados).
ALIAS_RE = re.compile(r"^(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# Nomes e e-mails nos limites.
NAME_200 = ("Nomeficticio" * 17)[:200]
NAME_201 = NAME_200 + "x"
WORDS_200 = " ".join(["Nome"] * 40) + "Z"
NAME_RAW_TO_200 = "   " + "  \t ".join(["Nome"] * 40) + "Z  "
NAME_RAW_TO_201 = "   " + "  \t ".join(["Nome"] * 40) + "Zy  "
EMAIL_254 = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 49 + ".example.com"
EMAIL_255 = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 50 + ".example.com"
EMAIL_255_NO_AT = "e" * 255

assert len(NAME_200) == 200 and len(NAME_201) == 201
assert len(WORDS_200) == 200 and len(NAME_RAW_TO_200) > 200
assert len(" ".join(NAME_RAW_TO_201.split())) == 201
assert len(EMAIL_254) == 254 and len(EMAIL_255) == 255

VALID_NAME = "Pessoa Ficticia"


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _expected_alias(pseudo_id: str, taken: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Primeiro prefixo do hex (sem hífens) com 4+ caracteres, com letra e livre."""
    hexed = uuid.UUID(pseudo_id).hex
    for size in range(4, 33):
        candidate = hexed[:size]
        if re.search("[a-f]", candidate) and candidate not in taken:
            return candidate
    return None


def _post_person(client: Any, body: Any, headers: dict[str, str] = ADMIN) -> Any:
    return client.post("/v1/people", json=body, headers=headers)


def _get(client: Any, path: str, params: Any = None, headers: dict[str, str] = ADMIN) -> Any:
    return client.get(path, params=params, headers=headers)


def _list_ok(client: Any, params: Any = None) -> dict:
    resp = _get(client, "/v1/people", params=params)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("people.list", 200, body)
    assert set(body) == {"items", "next_cursor"}
    return body


def _get_ok(client: Any, ref: str) -> dict:
    resp = _get(client, f"/v1/people/{ref}")
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("people.get", 200, body)
    return body


def _reveal(client: Any, refs: list[str], reason: str = "conferencia de teste") -> dict:
    resp = client.post("/v1/identity/reveal", json={"pseudonyms": refs, "reason": reason}, headers=ADMIN)
    assert resp.status_code == 200, f"revelação falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("identity.reveal", 200, body)
    return body


def _revealed(person: dict, name: str, ra: str | None, email: str | None) -> dict:
    return {"pseudo_id": person["pseudo_id"], "alias": person["alias"], "name": name, "ra": ra,
            "email": email}


def _counts(sql: psycopg.Connection) -> dict[str, int]:
    return {table: sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("people", "identities", "events")}


def _response_text(resp: Any) -> str:
    return resp.text + "\n" + "\n".join(f"{k}: {v}" for k, v in resp.headers.items())


def _sentinels(value: Any) -> list[str]:
    """Valores (e chaves) enviados com 8+ caracteres, para `assert_no_echo`."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _sentinels(key) + _sentinels(item)
    elif isinstance(value, list):
        for item in value:
            found += _sentinels(item)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        for candidate in dict.fromkeys((text, text.strip())):
            if len(candidate) >= 8:
                found.append(candidate)
    return found


class _SequenceGenerator:
    """Substituto de `people.generate_pseudo_id`: devolve os valores dados, em ordem, e conta."""

    def __init__(self, values: list[uuid.UUID]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> uuid.UUID:
        assert self._values, "o gerador substituto foi chamado mais vezes que o previsto"
        self.calls += 1
        return self._values.pop(0)

    @property
    def remaining(self) -> int:
        return len(self._values)


def _use_generator(monkeypatch: pytest.MonkeyPatch, values: list[uuid.UUID]) -> _SequenceGenerator:
    gen = _SequenceGenerator(values)
    monkeypatch.setattr(people_core, "generate_pseudo_id", gen)
    return gen


def _client_backends(sql: psycopg.Connection) -> list[tuple[int, str]]:
    return sql.execute(
        "SELECT pid, state FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'").fetchall()


def _wait_backends_gone(sql: psycopg.Connection, timeout: float = 10.0) -> None:
    """Espera as outras conexões ao banco do teste terminarem.

    O Postgres só publica em `pg_stat_database` as contagens de uma conexão quando ela termina (ou
    depois de segundos ociosa). Conexões **ociosas** que sobrarem depois da metade do prazo são
    encerradas: sem transação aberta, o encerramento não conta rollback. Espera real, com prazo,
    exigida pelo mecanismo de estatísticas (não é lógica de tempo da aplicação).
    """
    deadline = time.monotonic() + timeout
    terminated = False
    while True:
        rows = _client_backends(sql)
        if not rows:
            return
        now = time.monotonic()
        assert now < deadline, f"conexões ao banco do teste não terminaram em {timeout} s: {rows}"
        if not terminated and now > deadline - timeout / 2:
            for pid, state in rows:
                if state == "idle":
                    sql.execute("SELECT pg_terminate_backend(%s)", (pid,))
            terminated = True
        time.sleep(0.05)


def _xact_rollback(sql: psycopg.Connection) -> int:
    sql.execute("SELECT pg_stat_clear_snapshot()")
    return sql.execute(
        "SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()").fetchone()[0]


# ---------------------------------------------------------------------------------------------
# Cadastro
# ---------------------------------------------------------------------------------------------


def test_register_returns_only_the_pseudonym(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """201 com as chaves exatas, UUID v4 canônico, apelido = primeiro
    prefixo válido do hex, `created_at == iso(clock)` (também com fração), sem identidade no corpo nem
    nos cabeçalhos; cada cadastro grava um `person.registered` com os pseudônimos."""
    t0 = clock.now()
    resp = _post_person(client, ZEFIRO.payload())
    assert resp.status_code == 201, resp.text[:300]
    first = resp.json()
    assert_matches("people.register", 201, first)
    assert set(first) == {"pseudo_id", "alias", "created_at"}
    pid = uuid.UUID(first["pseudo_id"])
    assert pid.version == 4 and pid.variant == uuid.RFC_4122
    assert first["pseudo_id"] == str(pid) and UUID_RE.match(first["pseudo_id"])
    assert ALIAS_RE.match(first["alias"]) and pid.hex.startswith(first["alias"])
    assert first == {"pseudo_id": str(pid), "alias": _expected_alias(str(pid)), "created_at": iso(t0)}
    assert find_canary(_response_text(resp)) == []

    clock.advance(1.5)
    t1 = clock.now()
    resp = _post_person(client, URSULA.payload())
    assert resp.status_code == 201, resp.text[:300]
    second = resp.json()
    assert_matches("people.register", 201, second)
    assert second["pseudo_id"] != first["pseudo_id"]
    assert second == {"pseudo_id": second["pseudo_id"],
                      "alias": _expected_alias(second["pseudo_id"], {first["alias"]}),
                      "created_at": iso(t1)}
    assert find_canary(_response_text(resp)) == []

    events = sql.execute("SELECT type, actor_token_id, occurred_at, data FROM events ORDER BY seq").fetchall()
    assert events == [
        ("person.registered", "bootstrap", t0, {"pseudo_id": first["pseudo_id"], "alias": first["alias"]}),
        ("person.registered", "bootstrap", t1, {"pseudo_id": second["pseudo_id"], "alias": second["alias"]}),
    ]


def test_register_stores_identity_apart_and_normalized(client: Any, clock: Any,
                                                       sql: psycopg.Connection) -> None:
    """`people` só com o pseudônimo; `identities` com o
    nome normalizado, o e-mail aparado (caixa mantida), `email_normalized` casefold e NULLs."""
    zefiro = register_person(client, ADMIN_TOKEN, name="  Zéfiro   Canário\tQuintela ", ra=ZEFIRO.ra,
                             email="  Zefiro.Canario@Canario.Example.COM  ")
    ursula = register_person(client, ADMIN_TOKEN, name=URSULA.name)

    rows = sql.execute("SELECT id, pseudo_id::text, alias, created_at FROM people ORDER BY id").fetchall()
    assert [(r[1], r[2], r[3]) for r in rows] == [
        (zefiro["pseudo_id"], zefiro["alias"], clock.now()),
        (ursula["pseudo_id"], ursula["alias"], clock.now()),
    ]
    assert find_canary(repr(rows)) == []
    identities = sql.execute(
        "SELECT person_id, full_name, ra, email, email_normalized FROM identities ORDER BY person_id").fetchall()
    assert identities == [
        (rows[0][0], "Zéfiro Canário Quintela", ZEFIRO.ra, "Zefiro.Canario@Canario.Example.COM",
         "zefiro.canario@canario.example.com"),
        (rows[1][0], URSULA.name, None, None, None),
    ]


def test_normalization_is_visible_in_the_reveal(client: Any) -> None:
    """Normalização: o nome sai aparado e com os espaços
    colapsados; o e-mail mantém a caixa; `ra`/`email` ausentes ou `null` saem `null`."""
    zefiro = register_person(client, ADMIN_TOKEN, name="  Zéfiro   Canário\tQuintela ", ra=ZEFIRO.ra,
                             email="  Zefiro.Canario@Canario.Example.COM  ")
    ursula = register_person(client, ADMIN_TOKEN, name="Úrsula\n Pintassilgo \r\n Bethânia", ra=URSULA.ra)
    olimpio = register_person(client, ADMIN_TOKEN, name=OLIMPIO.name, ra=None, email=None)
    only_email = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Com Email", email=OLIMPIO.email)

    body = _reveal(client, [zefiro["alias"], ursula["pseudo_id"], olimpio["alias"], only_email["pseudo_id"]])
    assert body == {
        "people": [
            _revealed(zefiro, "Zéfiro Canário Quintela", ZEFIRO.ra, "Zefiro.Canario@Canario.Example.COM"),
            _revealed(ursula, "Úrsula Pintassilgo Bethânia", URSULA.ra, None),
            _revealed(olimpio, OLIMPIO.name, None, None),
            _revealed(only_email, "Pessoa Ficticia Com Email", None, OLIMPIO.email),
        ],
        "not_found": [],
    }


# ---------------------------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------------------------

_SPECIAL_USE = ("invalid", "test", "local", "localhost", "onion", "arpa")

INVALID_REGISTER = [
    # Uma linha por caso do contrato de cadastro
    pytest.param({"ra": "1029384756"}, [("name", "missing")], id="3.2-name-absent"),
    pytest.param({"name": ""}, [("name", "too_short")], id="3.2-name-empty"),
    pytest.param({"name": " \t\n  "}, [("name", "too_short")], id="3.2-name-only-spaces"),
    pytest.param({"name": NAME_201}, [("name", "too_long")], id="3.2-name-201"),
    pytest.param({"name": NAME_RAW_TO_201}, [("name", "too_long")], id="3.2-name-201-after-normalization"),
    pytest.param({"name": VALID_NAME, "ra": "98765abc"}, [("ra", "invalid_format")], id="3.2-ra-non-digit"),
    pytest.param({"name": VALID_NAME, "ra": "99887766554"}, [("ra", "too_long")], id="3.2-ra-11-digits"),
    pytest.param({"name": VALID_NAME, "ra": ""}, [("ra", "too_short")], id="3.2-ra-empty"),
    pytest.param({"name": VALID_NAME, "email": "email-sem-arroba-sentinela"}, [("email", "invalid_format")],
                 id="3.2-email-no-at"),
    pytest.param({"name": VALID_NAME, "email": "dois@@arrobas-sentinela.example.com"},
                 [("email", "invalid_format")], id="3.2-email-double-at"),
    pytest.param({"name": VALID_NAME, "email": "espaco sentinela@example.com"}, [("email", "invalid_format")],
                 id="3.2-email-inner-space"),
    pytest.param({"name": VALID_NAME, "email": EMAIL_255}, [("email", "too_long")], id="3.2-email-255"),
    pytest.param({"name": VALID_NAME, "campo_extra_sentinela": "valor-extra-sentinela"},
                 [("body", "unknown_field")], id="3.2-extra-field"),
    pytest.param({"name": VALID_NAME, "email": "Fulano Exibido <fulano.exibido@exemplo.example.com>"},
                 [("email", "invalid_format")], id="3.2-email-display-name"),
    *[pytest.param({"name": VALID_NAME, "email": f"pessoa.sentinela@exemplo.{tld}"},
                   [("email", "invalid_format")], id=f"3.2-email-special-use-{tld}") for tld in _SPECIAL_USE],
    # Precedência por campo
    pytest.param({"name": 123}, [("name", "invalid_format")], id="prec1-name-int"),
    pytest.param({"name": ["Nome Sentinela Em Lista"]}, [("name", "invalid_format")], id="prec1-name-list"),
    pytest.param({"name": VALID_NAME, "ra": 1029384756}, [("ra", "invalid_format")], id="prec1-ra-int"),
    pytest.param({"name": VALID_NAME, "email": {"endereco": "objeto.sentinela@exemplo.example.com"}},
                 [("email", "invalid_format")], id="prec1-email-object"),
    pytest.param({"name": None}, [("name", "missing")], id="prec2-name-null"),
    pytest.param({"name": VALID_NAME, "ra": "   "}, [("ra", "too_short")], id="prec3-ra-only-spaces"),
    pytest.param({"name": VALID_NAME, "email": ""}, [("email", "too_short")], id="prec3-email-empty"),
    pytest.param({"name": VALID_NAME, "email": "   "}, [("email", "too_short")], id="prec3-email-only-spaces"),
    pytest.param({"name": VALID_NAME, "ra": "abcdefghijk"}, [("ra", "too_long")], id="prec3-before-4-ra"),
    pytest.param({"name": VALID_NAME, "email": EMAIL_255_NO_AT}, [("email", "too_long")],
                 id="prec3-before-4-email"),
    pytest.param({"name": VALID_NAME, "ra": " 123 "}, [("ra", "invalid_format")], id="prec4-ra-padded"),
    pytest.param({"name": VALID_NAME, "ra": " 12345678 "}, [("ra", "invalid_format")],
                 id="prec4-ra-padded-sentinel"),
    # vários erros: na ordem do esquema (name, ra, email), o campo desconhecido no fim
    pytest.param({"email": "sem-arroba-sentinela", "name": ""}, [("name", "too_short"), ("email", "invalid_format")],
                 id="two-errors-schema-order"),
    pytest.param({"email": "outro-sem-arroba-sentinela", "ra": "12ab", "name": 123},
                 [("name", "invalid_format"), ("ra", "invalid_format"), ("email", "invalid_format")],
                 id="three-errors-schema-order"),
    pytest.param({"campo_extra_sentinela": 1, "ra": "", "name": None},
                 [("name", "missing"), ("ra", "too_short"), ("body", "unknown_field")], id="extra-field-last"),
    pytest.param({"name": VALID_NAME, "extra_um_sentinela": 1, "extra_dois_sentinela": "valor-dois-sentinela"},
                 [("body", "unknown_field")], id="two-extra-fields-one-entry"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_REGISTER)
def test_register_rejects_invalid_body_without_echo(client: Any, sql: psycopg.Connection, body: dict,
                                                    fields: list[tuple[str, str]]) -> None:
    """Uma linha por caso; precedência, ordem do esquema,
    campo desconhecido no fim: 400 com `fields` exatos, sem eco e sem nada gravado (nem evento)."""
    resp = _post_person(client, body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    extra = ["fulano.exibido@exemplo.example.com"] if "Fulano" in str(body) else []
    assert_no_echo(resp, *_sentinels(body), *extra)
    assert _counts(sql) == {"people": 0, "identities": 0, "events": 0}


RAW_BODIES = [
    pytest.param(None, None, [("body", "missing")], [], id="no-body"),
    pytest.param(b"", "application/json", [("body", "missing")], [], id="empty-json-body"),
    pytest.param(b"isto nao e json sentinela", "application/json", [("body", "invalid_format")],
                 ["isto nao e json sentinela"], id="not-json"),
    pytest.param(b'{"name": "Nome Sentinela Truncado"', "application/json", [("body", "invalid_format")],
                 ["Nome Sentinela Truncado"], id="truncated-json"),
    pytest.param(b'[{"name": "Nome Sentinela Em Lista"}]', "application/json", [("body", "invalid_format")],
                 ["Nome Sentinela Em Lista"], id="json-list"),
    pytest.param(b'"texto-sentinela-solto"', "application/json", [("body", "invalid_format")],
                 ["texto-sentinela-solto"], id="json-string"),
    pytest.param(b"12345678", "application/json", [("body", "invalid_format")], ["12345678"], id="json-number"),
    # JSON que não é UTF-8: o de um cliente em latin-1/cp1252 e uma sequência inválida.
    pytest.param('{"name": "Zéfiro Sentinela Latina"}'.encode("latin-1"), "application/json",
                 [("body", "invalid_format")], ["Zéfiro Sentinela Latina", "Sentinela Latina"],
                 id="json-latin1"),
    pytest.param(b"\xc3\x28", "application/json", [("body", "invalid_format")], [], id="utf8-invalido"),
    pytest.param(b'{"name": "\xff\xfe Nome Sentinela Bytes"}', "application/json", [("body", "invalid_format")],
                 ["Nome Sentinela Bytes"], id="utf8-invalido-no-valor"),
]


@pytest.mark.parametrize(("raw", "content_type", "fields", "sentinels"), RAW_BODIES)
def test_register_rejects_empty_or_non_object_body(client: Any, sql: psycopg.Connection, raw: bytes | None,
                                                   content_type: str | None, fields: list[tuple[str, str]],
                                                   sentinels: list[str]) -> None:
    """Corpo em JSON UTF-8 (casos de corpo inválido): corpo vazio → `[{body,
    missing}]`; corpo que não é JSON, que não é UTF-8 válido (inclusive o JSON de um cliente em
    latin-1) ou que não é objeto → `[{body, invalid_format}]`; sem eco e sem nada gravado."""
    headers = dict(ADMIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    if raw is None:
        resp = client.post("/v1/people", headers=headers)
    else:
        resp = client.post("/v1/people", content=raw, headers=headers)
    assert_error(resp, 400, "invalid_request", fields=fields)
    if sentinels:
        assert_no_echo(resp, *sentinels)
    assert _counts(sql) == {"people": 0, "identities": 0, "events": 0}


ACCEPTED_LIMITS = [
    pytest.param({"name": "Z"}, "Z", None, None, id="name-1"),
    pytest.param({"name": NAME_200}, NAME_200, None, None, id="name-200"),
    pytest.param({"name": NAME_RAW_TO_200}, WORDS_200, None, None, id="name-raw-over-200-normalized-200"),
    pytest.param({"name": VALID_NAME, "ra": "7"}, VALID_NAME, "7", None, id="ra-1-digit"),
    pytest.param({"name": VALID_NAME, "ra": "1029384756"}, VALID_NAME, "1029384756", None, id="ra-10-digits"),
    pytest.param({"name": VALID_NAME, "email": EMAIL_254}, VALID_NAME, None, EMAIL_254, id="email-254"),
    pytest.param({"name": VALID_NAME, "ra": None, "email": None}, VALID_NAME, None, None, id="ra-email-null"),
]


@pytest.mark.parametrize(("body", "name", "ra", "email"), ACCEPTED_LIMITS)
def test_register_accepts_values_at_the_limits(client: Any, sql: psycopg.Connection, body: dict, name: str,
                                               ra: str | None, email: str | None) -> None:
    """os limites aceitos (nome 1 e 200 depois da
    normalização, inclusive vindo de mais de 200 crus; RA 1 e 10; e-mail 254; `null`) → 201, com o
    valor gravado conferido pela revelação."""
    person = register_person(client, ADMIN_TOKEN, **body)
    assert _reveal(client, [person["pseudo_id"]]) == {"people": [_revealed(person, name, ra, email)],
                                                      "not_found": []}
    assert _counts(sql)["identities"] == 1


def test_people_routes_reject_unknown_query_parameter(client: Any, sql: psycopg.Connection) -> None:
    """Entrada estrita: parâmetro de consulta desconhecido em
    cadastro, listagem e consulta → `[{query, unknown_field}]`, sem eco do nome nem do valor."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Existente")
    calls = [
        ("POST", "/v1/people", {"json": {"name": "Pessoa Ficticia Nova"}}),
        ("GET", "/v1/people", {}),
        ("GET", f"/v1/people/{person['alias']}", {}),
    ]
    for method, path, kwargs in calls:
        resp = client.request(method, path, params={"parametro_sentinela": "valor_sentinela"}, headers=ADMIN,
                              **kwargs)
        assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
        assert_no_echo(resp, "parametro_sentinela", "valor_sentinela", person["pseudo_id"])
    assert _counts(sql) == {"people": 1, "identities": 1, "events": 1}


# ---------------------------------------------------------------------------------------------
# Conflito (pessoa repetida)
# ---------------------------------------------------------------------------------------------


def test_register_conflict_is_409_without_pseudonym_nor_db_error(app_factory: Any, sql: psycopg.Connection,
                                                                 db_url: str) -> None:
    """Pessoa repetida: RA repetido, e-mail repetido
    com outra caixa e espaços nas pontas, e os dois → 409 com `fields` exatos, sem o pseudônimo
    existente e sem eco; nada gravado; o 400 vem antes do 409; nome repetido sem RA/e-mail é aceito;
    `pg_stat_database.xact_rollback` não muda (conflito sem erro no banco), com controle positivo."""
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        zefiro = register_person(c, ADMIN_TOKEN, **ZEFIRO.payload())
        ursula = register_person(c, ADMIN_TOKEN, **URSULA.payload())
        register_person(c, ADMIN_TOKEN, name=ZEFIRO.name)  # nome não é único
        resp = _post_person(c, {"name": "", "ra": ZEFIRO.ra})
        assert_error(resp, 400, "invalid_request", fields=[("name", "too_short")])
        assert_no_echo(resp, ZEFIRO.ra)
    _wait_backends_gone(sql)
    before = _counts(sql)
    assert before == {"people": 3, "identities": 3, "events": 3}
    rollbacks_before = _xact_rollback(sql)

    other = "Outra Pessoa Ficticia"
    cases = [
        ({"name": other, "ra": ZEFIRO.ra}, [("ra", "duplicate")]),
        ({"name": other, "ra": ZEFIRO.ra, "email": "email.novo.ficticio@exemplo.example.com"},
         [("ra", "duplicate")]),
        ({"name": other, "email": "  ZEFIRO.Canario@CANARIO.Example.com  "}, [("email", "duplicate")]),
        ({"name": other, "ra": ZEFIRO.ra, "email": ZEFIRO.email}, [("ra", "duplicate"), ("email", "duplicate")]),
        ({"name": other, "ra": URSULA.ra, "email": ZEFIRO.email.upper()},
         [("ra", "duplicate"), ("email", "duplicate")]),
    ]
    existing = [zefiro["pseudo_id"], uuid.UUID(zefiro["pseudo_id"]).hex,
                ursula["pseudo_id"], uuid.UUID(ursula["pseudo_id"]).hex]
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        for body, fields in cases:
            resp = _post_person(c, body)
            assert_error(resp, 409, "conflict", fields=fields)
            assert_no_echo(resp, *existing, *_sentinels(body))
            assert find_canary(_response_text(resp)) == []
    _wait_backends_gone(sql)
    assert _counts(sql) == before
    assert _xact_rollback(sql) == rollbacks_before, "o conflito provocou rollback no banco"

    # Controle positivo: um rollback de outra conexão aparece na mesma medição.
    with psycopg.connect(db_url, connect_timeout=5) as probe:
        probe.execute("SELECT 1")
        probe.rollback()
    _wait_backends_gone(sql)
    assert _xact_rollback(sql) >= rollbacks_before + 1


# ---------------------------------------------------------------------------------------------
# Aleatoriedade do pseudônimo
# ---------------------------------------------------------------------------------------------


def test_same_identity_in_two_databases_gets_different_pseudo_ids(client: Any, app_factory: Any,
                                                                  db_factory: Any, clock: Any) -> None:
    """a mesma identidade, no mesmo instante, em dois bancos vazios → `pseudo_id`s
    diferentes (o pseudônimo não deriva da identidade nem do horário)."""
    first = register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
    with TestClient(app_factory(database_url=db_factory()), raise_server_exceptions=False) as other:
        second = register_person(other, ADMIN_TOKEN, **ZEFIRO.payload())
    assert first["created_at"] == second["created_at"] == iso(clock.now())
    assert first["pseudo_id"] != second["pseudo_id"]
    for person in (first, second):
        assert person["alias"] == _expected_alias(person["pseudo_id"])


def test_generator_is_not_seeded_by_the_random_module() -> None:
    """`random.seed(1)` antes de duas chamadas de `generate_pseudo_id()` →
    valores diferentes (a fonte não é semeável)."""
    state = random.getstate()
    try:
        random.seed(1)
        a = people_core.generate_pseudo_id()
        random.seed(1)
        b = people_core.generate_pseudo_id()
    finally:
        random.setstate(state)
    assert isinstance(a, uuid.UUID) and isinstance(b, uuid.UUID)
    assert a.version == 4 and b.version == 4
    assert a != b


def test_generator_yields_distinct_uuid4() -> None:
    """1.000 chamadas de `generate_pseudo_id()` → 1.000 UUIDs v4 (variante RFC 4122) distintos."""
    values = [people_core.generate_pseudo_id() for _ in range(1000)]
    assert all(isinstance(v, uuid.UUID) for v in values)
    assert all(v.version == 4 and v.variant == uuid.RFC_4122 for v in values)
    assert len(set(values)) == 1000


def test_registration_returns_the_value_of_the_replaced_generator(client: Any, clock: Any,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """`people.generate_pseudo_id` é o gerador de cada cadastro: o `pseudo_id`
    devolvido é o valor do gerador substituído, chamado uma vez."""
    fixed = uuid4_with_prefix("abcd", fill="1")
    gen = _use_generator(monkeypatch, [fixed])
    resp = _post_person(client, ZEFIRO.payload())
    assert resp.status_code == 201, resp.text[:300]
    assert resp.json() == {"pseudo_id": str(fixed), "alias": "abcd", "created_at": iso(clock.now())}
    assert gen.calls == 1


def test_existing_pseudo_id_is_discarded_and_the_next_one_is_used(client: Any, clock: Any,
                                                                  sql: psycopg.Connection,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """um `pseudo_id` sorteado igual a um existente é descartado e vale o
    próximo, com o gerador chamado exatamente 2 vezes; o existente fica intacto."""
    a_id = uuid4_with_prefix("abcd", fill="1")
    b_id = uuid4_with_prefix("bcde", fill="5")
    _use_generator(monkeypatch, [a_id])
    a = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Primeira")
    assert a == {"pseudo_id": str(a_id), "alias": "abcd", "created_at": iso(clock.now())}

    gen = _use_generator(monkeypatch, [a_id, b_id])
    b = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Segunda")
    assert b == {"pseudo_id": str(b_id), "alias": "bcde", "created_at": iso(clock.now())}
    assert gen.calls == 2 and gen.remaining == 0
    assert sql.execute("SELECT pseudo_id::text, alias FROM people ORDER BY id").fetchall() == [
        (str(a_id), "abcd"), (str(b_id), "bcde")]
    assert _counts(sql) == {"people": 2, "identities": 2, "events": 2}


# ---------------------------------------------------------------------------------------------
# Apelido (colisão de apelido)
# ---------------------------------------------------------------------------------------------


def test_alias_extends_on_collision_and_never_changes(client: Any, clock: Any, sql: psycopg.Connection,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Colisão de apelido: colisões 4 → 5 e 5 → 6; prefixo só
    de dígitos → até a primeira letra (e a colisão depois dela); UUID sem letras → sorteia o próximo;
    os apelidos anteriores ficam intactos (API e banco)."""
    a = uuid4_with_prefix("abcd", fill="1")
    b = uuid4_with_prefix("abcd", fill="2")
    c = uuid4_with_prefix("abcd2", fill="3")
    d = uuid4_with_prefix("12345", fill="e")
    f = uuid4_with_prefix("12345e", fill="f")
    no_letters = uuid4_with_prefix("", fill="1")
    e = uuid4_with_prefix("9876", fill="c")
    assert not re.search("[a-f]", no_letters.hex)
    gen = _use_generator(monkeypatch, [a, b, c, d, f, no_letters, e])

    expected = [(a, "abcd", 1), (b, "abcd2", 2), (c, "abcd23", 3), (d, "12345e", 4), (f, "12345ef", 5),
                (e, "9876c", 7)]
    registered = []
    for index, (pid, alias, calls) in enumerate(expected):
        person = register_person(client, ADMIN_TOKEN, name=f"Pessoa Ficticia Apelido {index}")
        assert person == {"pseudo_id": str(pid), "alias": alias, "created_at": iso(clock.now())}
        assert gen.calls == calls
        registered.append(person)
    assert gen.remaining == 0

    for person in registered:
        assert _get_ok(client, person["pseudo_id"]) == person
        assert _get_ok(client, person["alias"]) == person
    assert _list_ok(client)["items"] == registered
    assert sql.execute("SELECT pseudo_id::text, alias FROM people ORDER BY id").fetchall() == [
        (str(pid), alias) for pid, alias, _ in expected]
    assert sql.execute("SELECT count(*) FROM people WHERE pseudo_id = %s", (no_letters,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------------------------


def test_list_follows_registration_order_when_the_clock_goes_back(client: Any, clock: Any) -> None:
    """Com o relógio voltando entre cadastros, a listagem segue a
    ordem de cadastro, com chaves exatas e os `created_at` como gravados."""
    t0 = clock.now()
    p1 = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Um")
    clock.advance(-3600)
    p2 = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Dois")
    clock.advance(-86400)
    p3 = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Tres")
    clock.set(t0 + timedelta(days=30))
    p4 = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Quatro")
    assert [p["created_at"] for p in (p1, p2, p3, p4)] == [
        iso(t0), iso(t0 - timedelta(hours=1)), iso(t0 - timedelta(hours=1, days=1)), iso(t0 + timedelta(days=30))]

    body = _list_ok(client)
    assert body["items"] == [p1, p2, p3, p4]
    assert all(set(item) == {"pseudo_id", "alias", "created_at"} for item in body["items"])


def test_list_cursor_chain_covers_everything_once(client: Any) -> None:
    """encadear `next_cursor` percorre tudo, em ordem, sem perda nem
    repetição, com cursores crescentes; página vazia → `next_cursor == after` (também além do fim e
    no banco vazio, com o `after` padrão 0)."""
    assert _list_ok(client) == {"items": [], "next_cursor": 0}
    assert _list_ok(client, {"after": 5}) == {"items": [], "next_cursor": 5}

    registered = [register_person(client, ADMIN_TOKEN, name=f"Pessoa Ficticia Cursor {i}") for i in range(7)]
    seen: list[dict] = []
    sizes: list[int] = []
    after = 0
    while True:
        body = _list_ok(client, {"after": after, "limit": 3})
        if not body["items"]:
            assert body == {"items": [], "next_cursor": after}
            break
        assert body["next_cursor"] > after
        seen.extend(body["items"])
        sizes.append(len(body["items"]))
        after = body["next_cursor"]
        assert len(sizes) <= 3, "a paginação não terminou"
    assert sizes == [3, 3, 1]
    assert seen == registered
    assert _list_ok(client, {"after": after + 1000}) == {"items": [], "next_cursor": after + 1000}


def test_list_default_limit_is_100_and_limits_1_and_500_are_accepted(client: Any) -> None:
    """com 101 pessoas, sem `limit` → as 100 primeiras e o cursor leva à 101ª;
    `limit=1` → só a primeira; `limit=500` → todas, com o mesmo cursor final."""
    registered = [register_person(client, ADMIN_TOKEN, name=f"Pessoa Ficticia Numero {i:03d}")
                  for i in range(101)]
    default = _list_ok(client)
    assert default["items"] == registered[:100]
    rest = _list_ok(client, {"after": default["next_cursor"]})
    assert rest["items"] == registered[100:]
    one = _list_ok(client, {"limit": 1})
    assert one["items"] == registered[:1]
    everything = _list_ok(client, {"limit": 500})
    assert everything["items"] == registered
    assert everything["next_cursor"] == rest["next_cursor"]
    assert one["next_cursor"] < default["next_cursor"] < rest["next_cursor"]


INVALID_LIST_QUERY = [
    pytest.param({"limit": "0"}, [("limit", "out_of_range")], id="limit-0"),
    pytest.param({"limit": "501"}, [("limit", "out_of_range")], id="limit-501"),
    pytest.param({"limit": "-5"}, [("limit", "out_of_range")], id="limit-negative"),
    pytest.param({"limit": "abcdefgh"}, [("limit", "invalid_format")], id="limit-text"),
    pytest.param({"limit": "1.5"}, [("limit", "invalid_format")], id="limit-fraction"),
    pytest.param({"after": "abcdefgh"}, [("after", "invalid_format")], id="after-text"),
    pytest.param({"after": "-1"}, [("after", "out_of_range")], id="after-negative"),
    pytest.param({"parametro_sentinela": "valor_sentinela"}, [("query", "unknown_field")], id="unknown"),
    pytest.param({"limit": "5", "parametro_sentinela": "1"}, [("query", "unknown_field")],
                 id="unknown-with-valid"),
    pytest.param({"limit": "0", "parametro_sentinela": "1"}, [("limit", "out_of_range"), ("query", "unknown_field")],
                 id="unknown-last"),
]


@pytest.mark.parametrize(("params", "fields"), INVALID_LIST_QUERY)
def test_list_rejects_invalid_query_without_echo(client: Any, params: dict, fields: list[tuple[str, str]]) -> None:
    """`limit` 0/501/negativo → `out_of_range`; não inteiro →
    `invalid_format`; `after` → `invalid_format`/`out_of_range`; desconhecido → `[{query,
    unknown_field}]`, no fim; sem eco."""
    register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Listada")
    resp = _get(client, "/v1/people", params=params)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *_sentinels(params))


# ---------------------------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------------------------

TARGET = uuid4_with_prefix("abcd", fill="1")
OTHER = uuid4_with_prefix("bcde", fill="2")


def test_get_by_alias_or_uuid_returns_the_same_person(client: Any, clock: Any,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """a consulta pelo apelido e pelo UUID devolve o mesmo `Person` exato."""
    _use_generator(monkeypatch, [OTHER, TARGET])
    other = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Outra")
    target = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Alvo")
    assert target == {"pseudo_id": str(TARGET), "alias": "abcd", "created_at": iso(clock.now())}
    assert _get_ok(client, "abcd") == target
    assert _get_ok(client, str(TARGET)) == target
    assert _get_ok(client, "bcde") == other
    assert _get_ok(client, str(OTHER)) == other


def test_get_unknown_or_longer_prefix_is_404(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Igualdade exata: pseudônimo inexistente, ou prefixo mais longo que o
    apelido (inclusive o hex inteiro), → 404 `not_found`."""
    _use_generator(monkeypatch, [TARGET])
    register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Alvo")
    for ref in ("abcd1", "abcd11", TARGET.hex, "fedc", str(uuid4_with_prefix("fedc", fill="9"))):
        resp = _get(client, f"/v1/people/{ref}")
        assert_error(resp, 404, "not_found")


INVALID_REFS = [
    pytest.param("123456", id="digits-only"),
    pytest.param("ABCD", id="uppercase-alias"),
    pytest.param("abc", id="three-chars"),
    pytest.param(str(TARGET).upper(), id="uppercase-uuid-of-existing"),
    pytest.param("Zefiro", id="name-like"),
    pytest.param("ABCDEF12", id="uppercase-sentinel"),
    pytest.param("12345678", id="digits-sentinel"),
    pytest.param("abcd" + "1" * 29, id="hex-33-chars"),
    pytest.param("abcd1111-1111-1111-8111-111111111111", id="uuid-not-v4"),
    pytest.param("abcd-1234", id="hyphenated-alias"),
]


@pytest.mark.parametrize("ref", INVALID_REFS)
def test_get_rejects_malformed_ref_without_echo(client: Any, monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    """`ref` fora do formato de pseudônimo (inclusive
    o UUID maiúsculo de uma pessoa existente) → `[{ref, invalid_format}]`, sem eco."""
    _use_generator(monkeypatch, [TARGET])
    register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Alvo")
    resp = _get(client, f"/v1/people/{ref}")
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    if len(ref) >= 8:
        assert_no_echo(resp, ref)


# ---------------------------------------------------------------------------------------------
# Horários
# ---------------------------------------------------------------------------------------------


def test_times_leave_in_utc_even_with_another_database_timezone(app_factory: Any, sql: psycopg.Connection,
                                                                db_url: str, clock: Any) -> None:
    """Horários: com o banco em `America/Sao_Paulo` e o relógio num horário
    −03:00 com fração, `created_at` sai em UTC com `Z` (forma canônica de `iso`) no cadastro, na
    listagem e na consulta, e o banco guarda o mesmo instante."""
    dbname = sql.execute("SELECT current_database()").fetchone()[0]
    sql.execute(f"ALTER DATABASE \"{dbname}\" SET timezone = 'America/Sao_Paulo'")
    with psycopg.connect(db_url, connect_timeout=5) as probe:
        assert probe.execute("SHOW timezone").fetchone()[0] == "America/Sao_Paulo"
    local = datetime(2031, 6, 15, 21, 30, 15, 250000, tzinfo=timezone(timedelta(hours=-3)))
    clock.set(local)
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        person = register_person(c, ADMIN_TOKEN, name="Pessoa Ficticia Fuso")
        assert person["created_at"] == iso(local)
        assert _list_ok(c)["items"] == [person]
        assert _get_ok(c, person["pseudo_id"]) == person
    assert sql.execute("SELECT created_at FROM people").fetchone()[0] == local


# ---------------------------------------------------------------------------------------------
# Permissões
# ---------------------------------------------------------------------------------------------


def test_people_routes_require_their_permissions(client: Any, sql: psycopg.Connection) -> None:
    """Ordem: validação → permissão → regras; `people:write` para
    cadastrar; `people:read` para listar e consultar; o 400 vem antes do 403 e o 403 antes do 409/404;
    o cadastro registra o `token_id` de quem cadastrou."""
    reader, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="leitor de pessoas")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["people:write"], label="escritor de pessoas")
    existing = register_person(client, ADMIN_TOKEN, name=ZEFIRO.name, ra=ZEFIRO.ra)

    # people:read não cadastra (nem revela conflito), mas lista e consulta
    resp = _post_person(client, {"name": "Pessoa Ficticia Leitora"}, headers=auth(reader))
    assert_error(resp, 403, "forbidden", permission="people:write")
    resp = _post_person(client, {"name": "Pessoa Ficticia Leitora", "ra": ZEFIRO.ra}, headers=auth(reader))
    assert_error(resp, 403, "forbidden", permission="people:write")
    assert_no_echo(resp, ZEFIRO.ra)
    resp = _post_person(client, {"name": ""}, headers=auth(reader))
    assert_error(resp, 400, "invalid_request", fields=[("name", "too_short")])
    resp = _get(client, "/v1/people", headers=auth(reader))
    assert resp.status_code == 200 and resp.json()["items"] == [existing]
    resp = _get(client, f"/v1/people/{existing['alias']}", headers=auth(reader))
    assert resp.status_code == 200 and resp.json() == existing

    # people:write cadastra, mas não lista nem consulta
    created = register_person(client, writer, name="Pessoa Ficticia Escrita")
    assert_error(_get(client, "/v1/people", headers=auth(writer)), 403, "forbidden", permission="people:read")
    assert_error(_get(client, f"/v1/people/{existing['alias']}", headers=auth(writer)), 403, "forbidden",
                 permission="people:read")
    assert_error(_get(client, f"/v1/people/{uuid.uuid4()}", headers=auth(writer)), 403, "forbidden",
                 permission="people:read")
    assert_error(_get(client, "/v1/people/ABCD", headers=auth(writer)), 400, "invalid_request",
                 fields=[("ref", "invalid_format")])
    assert_error(_get(client, "/v1/people", params={"limit": "0"}, headers=auth(writer)), 400,
                 "invalid_request", fields=[("limit", "out_of_range")])

    events = sql.execute("SELECT type, actor_token_id, data FROM events ORDER BY seq").fetchall()
    assert [(t, actor) for t, actor, _ in events] == [
        ("token.created", "bootstrap"), ("token.created", "bootstrap"),
        ("person.registered", "bootstrap"), ("person.registered", writer_body["token_id"])]
    assert events[-1][2] == {"pseudo_id": created["pseudo_id"], "alias": created["alias"]}
    assert _counts(sql)["people"] == 2
