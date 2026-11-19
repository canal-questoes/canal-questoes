"""Revelação de identidades pelo servidor MCP, só por lista de pseudônimos, inclusive a de pessoa expurgada e o
roteiro de revelação.

Com a API dos testes (o `client` da API base) como `ServerDeps.http`, o perfil pelas variáveis (`CQ_API_URL`, `CQ_TOKEN` e
o `CQ_CONFIG` inexistente do `tmp_path`) e o cliente MCP em processo nas duas eras (`MODES`):

- o **roteiro de revelação**: dois alunos canário (`STUDENT_A`, `STUDENT_B`, importados pela API) revelados
  com o motivo → as identidades no esquema estrito da `identity.reveal.response`, iguais às gravadas, no estruturado
  e no texto (controle positivo do canário); a auditoria `identity.revealed` com os pseudônimos, o motivo e
  `client: mcp`, sem nomes;
- um pseudônimo inexistente e um de pessoa expurgada (pela semente do expurgo) em `not_found`, sem erro;
- token sem `identity:reveal` → `[forbidden]` com `Permissão exigida: identity:reveal.` e o `identity.reveal_denied`
  com `client: mcp`;
- motivo vazio, 0 ou 101 pseudônimos, campo a mais e as demais formas fora do esquema → `-32602` sem pedido;
- um item de `pseudonyms` com um RA canário, um e-mail canário, um identificador numérico do Telegram ou um
  `@usuário` → `-32602` com a `message` exata, sem eco e sem pedido;
- o motivo com o nome de uma pessoa canário não volta em resultado nem erro.

As conexões usam o portal síncrono do `anyio` (`start_blocking_portal` + `wrap_async_context_manager`), para que a
semente e o banco fiquem no código síncrono do teste. No esqueleto,
`build_server` lança `NotImplementedError` ao abrir a conexão: cada função falha por ele.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
from collections.abc import Iterator
from typing import Any

import anyio.from_thread
import psycopg
import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    all_events,
    create_offering,
    create_token,
    import_students,
    prepare_purge,
    purge_offering,
)
from tests.helpers.canary import (
    ALL_PREFIXES,
    PURGE_PREFIXES,
    STUDENT_A,
    STUDENT_B,
    STUDENT_PREFIXES,
    TG_STUDENT_A1,
    ZEFIRO,
    find_canary,
    find_secrets,
)
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.mcp_client import MISSING_PROFILE_FILE, MODES, SCAN_PREFIXES, call, in_process, make_deps, scan
from tests.helpers.purge import _person_by_ra, seed_purge_scenario
from tests.helpers.secrets import all_secrets, bot_secrets

BASE_URL = "http://testserver"
REVEAL_PATH = "/v1/identity/reveal"
IDENTITY_TYPES = ("identity.revealed", "identity.reveal_denied")

# os textos exatos.
INVALID_ARGUMENTS = "Argumentos inválidos para a ferramenta."
FORBIDDEN_TEXT = "[forbidden] Este token não tem a permissão necessária.\nPermissão exigida: identity:reveal."

# Todas as permissões da API base menos `identity:reveal` (a revelação com este token é negada pela API).
NO_REVEAL = ["events:read", "people:read", "people:write", "tokens:manage"]

REASON = "conferencia da turma pedida pelo operador"
# O motivo com o nome de uma pessoa canário da API base (que nunca é alvo da revelação destes testes).
CANARY_REASON = f"conferencia pedida por {ZEFIRO.name}"

# Apelidos de 4 caracteres usados como "inexistentes" (o primeiro que não estiver em uso).
_FREE_ALIAS_CANDIDATES = ("beef", "cafe", "dead", "face", "fade", "bead")

# O cenário mínimo do expurgo: um oferecimento arquivado com dois alunos, sem bot, equipe nem pendências.
MINIMAL_PURGE = {"bot": False, "team": False, "shared": False, "cross_offering": False, "frozen_report": False,
                 "free_message_before_register": False, "contact": None}

_counter = itertools.count(1)


# --------------------------------------------------------------------------------------------------
# Apoio


@dataclasses.dataclass
class _H:
    client: Any
    sql: psycopg.Connection
    clock: Any
    tmp_path: Any


@pytest.fixture
def h(client: Any, sql: psycopg.Connection, clock: Any, tmp_path: Any) -> _H:
    return _H(client=client, sql=sql, clock=clock, tmp_path=tmp_path)


@dataclasses.dataclass(frozen=True)
class _Person:
    pseudo_id: str
    alias: str
    revealed: dict  # o item esperado em `people` (as identidades gravadas)


@contextlib.contextmanager
def _recorded(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


class _Session:
    """Uma conexão MCP em processo, chamada do código síncrono; cada chamada devolve o `Outcome` e os pedidos que
    ela fez à API."""

    def __init__(self, portal: Any, mcp_client: Any, http: Any) -> None:
        self._portal = portal
        self._client = mcp_client
        self._http = http

    def reveal(self, arguments: dict[str, Any]) -> tuple[Any, list[Any]]:
        with _recorded(self._http) as seen:
            outcome = self._portal.call(call, self._client, "reveal_identities", arguments)
        return outcome, list(seen)


@contextlib.contextmanager
def _session(h: _H, mode: str, token: str = ADMIN_TOKEN) -> Iterator[_Session]:
    env = {"CQ_API_URL": BASE_URL, "CQ_TOKEN": token, "CQ_CONFIG": str(h.tmp_path / MISSING_PROFILE_FILE)}
    deps = make_deps(h.tmp_path, env=env, http=h.client)
    with anyio.from_thread.start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(in_process(deps, mode)) as mcp_client:
            yield _Session(portal, mcp_client, h.client)


def _secrets() -> list[str]:
    return all_secrets() + bot_secrets()


def _students(h: _H, *students: Any) -> list[_Person]:
    """Importa os alunos canário num oferecimento novo, pela API, e devolve os pseudônimos (lidos do banco pelo RA)
    e o item esperado da revelação de cada um (**[F]** a identidade é gravada como veio na lista:
    `tests/integration/test_students_import_api.py`, nome e e-mail sem mudança de caixa)."""
    offering = create_offering(h.client, ADMIN_TOKEN, code=f"revelacao-mcp-{next(_counter)}")
    import_students(h.client, ADMIN_TOKEN, offering["code"], [student.row() for student in students])
    people = []
    for student in students:
        _, pseudo_id, alias = _person_by_ra(h.sql, student.ra)
        people.append(_Person(pseudo_id, alias, {"pseudo_id": pseudo_id, "alias": alias, "name": student.name,
                                                 "ra": student.ra, "email": student.email}))
    return people


def _free_alias(h: _H) -> str:
    taken = {row[0] for row in h.sql.execute("SELECT alias FROM people").fetchall()}
    taken |= {row[0] for row in h.sql.execute("SELECT pseudo_id::text FROM people").fetchall()}
    return next(alias for alias in _FREE_ALIAS_CANDIDATES if alias not in taken)


def _cursor(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])


def _identity_events_after(h: _H, cursor: int) -> list[dict]:
    """Os eventos de revelação depois do cursor, pela API, sem o `seq`; sem nome de aluno nem segredo."""
    events = [{k: v for k, v in e.items() if k != "seq"}
              for e in all_events(h.client, ADMIN_TOKEN, *IDENTITY_TYPES) if e["seq"] > cursor]
    assert scan(events, _secrets(), prefixes=SCAN_PREFIXES) == []
    return events


def _identity_event_count(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM events WHERE type IN ('identity.revealed', "
                            "'identity.reveal_denied')").fetchone()[0])


def _assert_success(outcome: Any, seen: list[Any], arguments: dict[str, Any]) -> dict:
    """Sucesso: o corpo da API no estruturado, no esquema estrito da revelação, e o mesmo JSON no
    texto; um pedido `POST /v1/identity/reveal` com `X-CQ-Client: mcp` e os argumentos como corpo; nenhum segredo."""
    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is False, outcome.text
    body = outcome.structured
    assert_matches("identity.reveal", 200, body)
    assert outcome.text == json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", REVEAL_PATH)
    assert request.headers.get("X-CQ-Client") == "mcp"
    assert json.loads(request.content) == arguments
    for text in (outcome.text, json.dumps(outcome.raw, ensure_ascii=False), json.dumps(outcome.raw)):
        assert find_secrets(text, _secrets()) == []
    return body


def _assert_protocol_refusal(outcome: Any, seen: list[Any], *echo: str) -> None:
    """`-32602` com a `message` exata, sem `data`, sem pedido à API, sem canário, segredo nem eco."""
    assert (outcome.protocol_code, outcome.protocol_message) == (-32602, INVALID_ARGUMENTS), outcome.raw
    assert "data" not in outcome.raw
    assert seen == []
    assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []
    rendered = json.dumps(outcome.raw, ensure_ascii=False)
    for value in echo:
        assert value not in rendered


def _names_found(text: str) -> set[str]:
    return {label for label in find_canary(text, prefixes=STUDENT_PREFIXES) if label.startswith("aluno-nome:")}


# --------------------------------------------------------------------------------------------------
# Roteiro de revelação


@pytest.mark.parametrize("mode", MODES)
def test_reveal_walkthrough(h: _H, mode: str) -> None:
    """Roteiro de revelação: dois alunos canário revelados, um pelo `pseudo_id` e
    o outro pelo apelido, com o motivo → `people` com as identidades gravadas, na ordem da primeira referência, e
    `not_found` vazio, no esquema estrito da revelação, no estruturado e no mesmo JSON no texto; os nomes dos dois
    aparecem no estruturado e no texto (controle positivo); um único `identity.revealed` com `client: mcp`, os
    `pseudo_id` revelados, a contagem e o motivo, sem nome de aluno."""
    a, b = _students(h, STUDENT_A, STUDENT_B)
    cursor = _cursor(h.sql)
    arguments = {"pseudonyms": [a.pseudo_id, b.alias], "reason": REASON}

    with _session(h, mode) as s:
        outcome, seen = s.reveal(arguments)

    body = _assert_success(outcome, seen, arguments)
    assert body == {"people": [a.revealed, b.revealed], "not_found": []}
    expected_names = {f"aluno-nome:{STUDENT_A.name}", f"aluno-nome:{STUDENT_B.name}"}
    assert _names_found(outcome.text) == expected_names
    assert _names_found(json.dumps(outcome.structured, ensure_ascii=False)) == expected_names
    assert _identity_events_after(h, cursor) == [
        {"type": "identity.revealed", "occurred_at": iso(h.clock.now()), "actor_token_id": "bootstrap",
         "data": {"client": "mcp", "revealed": [a.pseudo_id, b.pseudo_id], "not_found_count": 0, "reason": REASON}}]


@pytest.mark.parametrize("mode", MODES)
def test_missing_and_purged_pseudonyms_are_not_found(h: _H, mode: str) -> None:
    """Revelação de pessoa expurgada: um apelido inexistente e o `pseudo_id` de uma pessoa expurgada (a semente do expurgo,
    expurgada pelas rotas) voltam em `not_found`, na ordem do pedido, **sem** erro, ao lado de um aluno revelado; o
    `identity.revealed` conta os dois não encontrados; nenhuma identidade da semente do expurgo sai."""
    scenario = seed_purge_scenario(h.client, h.sql, **MINIMAL_PURGE)
    purge_offering(h.client, scenario.code, prepare_purge(h.client, scenario.code)["confirmation_code"])
    purged = next(person for person in scenario.people.values() if person.deleted)
    (live,) = _students(h, STUDENT_A)
    missing = _free_alias(h)
    cursor = _cursor(h.sql)
    arguments = {"pseudonyms": [purged.pseudo_id, missing, live.pseudo_id], "reason": REASON}

    with _session(h, mode) as s:
        outcome, seen = s.reveal(arguments)

    body = _assert_success(outcome, seen, arguments)
    assert body == {"people": [live.revealed], "not_found": [purged.pseudo_id, missing]}
    assert scan(outcome, _secrets(), prefixes=PURGE_PREFIXES) == []
    assert _identity_events_after(h, cursor) == [
        {"type": "identity.revealed", "occurred_at": iso(h.clock.now()), "actor_token_id": "bootstrap",
         "data": {"client": "mcp", "revealed": [live.pseudo_id], "not_found_count": 2, "reason": REASON}}]


@pytest.mark.parametrize("mode", MODES)
def test_token_without_reveal_permission_is_forbidden(h: _H, mode: str) -> None:
    """Erro `[forbidden] <mensagem>` e a linha `Permissão exigida: identity:reveal.`: com um token
    sem `identity:reveal`, a ferramenta falha com o texto exato (sem estruturado), depois de um pedido com
    `X-CQ-Client: mcp`; a API grava o `identity.reveal_denied` com `client: mcp` e o ator do token; nenhuma
    identidade, nenhum segredo (nem o token do perfil) e nenhum pseudônimo pedido no texto."""
    (a,) = _students(h, STUDENT_A)
    secret, token = create_token(h.client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")
    cursor = _cursor(h.sql)
    arguments = {"pseudonyms": [a.pseudo_id], "reason": REASON}

    with _session(h, mode, token=secret) as s:
        outcome, seen = s.reveal(arguments)

    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is True
    assert outcome.structured is None
    assert outcome.text == FORBIDDEN_TEXT
    assert len(seen) == 1 and seen[0].headers.get("X-CQ-Client") == "mcp"
    assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []
    assert a.pseudo_id not in json.dumps(outcome.raw) and a.alias not in (outcome.text or "")
    assert _identity_events_after(h, cursor) == [
        {"type": "identity.reveal_denied", "occurred_at": iso(h.clock.now()), "actor_token_id": token["token_id"],
         "data": {"client": "mcp", "requested": [a.pseudo_id], "not_found_count": 0}}]


# --------------------------------------------------------------------------------------------------
# Esquema de entrada


def _invalid_cases(pseudo_id: str) -> list[tuple[str, dict[str, Any], tuple[str, ...]]]:
    """(id, argumentos, valores que não podem ecoar)."""
    hundred_and_one = [f"a{i:04x}" for i in range(101)]
    return [
        ("motivo-vazio", {"pseudonyms": [pseudo_id], "reason": ""}, ()),
        ("motivo-ausente", {"pseudonyms": [pseudo_id]}, ()),
        ("motivo-nao-texto", {"pseudonyms": [pseudo_id], "reason": 7}, ()),
        ("motivo-nulo", {"pseudonyms": [pseudo_id], "reason": None}, ()),
        ("zero-pseudonimos", {"pseudonyms": [], "reason": REASON}, (REASON,)),
        ("101-pseudonimos", {"pseudonyms": hundred_and_one, "reason": REASON}, (REASON, hundred_and_one[-1])),
        ("pseudonimos-ausentes", {"reason": REASON}, (REASON,)),
        ("pseudonimos-texto", {"pseudonyms": pseudo_id, "reason": REASON}, (REASON,)),
        ("pseudonimo-nao-texto", {"pseudonyms": [1234], "reason": REASON}, (REASON,)),
        ("campo-a-mais", {"pseudonyms": [pseudo_id], "reason": REASON, "campo_extra_sentinela": 1},
         (REASON, "campo_extra_sentinela")),
    ]


@pytest.mark.parametrize("mode", MODES)
def test_arguments_outside_the_schema_are_refused_without_request(h: _H, mode: str) -> None:
    """Esquema de entrada (`reason` com `minLength: 1`; de 1 a 100 itens; nenhum campo a mais) e recusa de protocolo
    (`-32602` com `"Argumentos inválidos para a ferramenta."`, sem `data`): motivo vazio, ausente, de outro tipo ou
    nulo; 0 e 101 pseudônimos; a lista ausente ou de outro tipo; um item que não é texto; um campo a mais → recusa
    de protocolo, sem pedido à API, sem evento de revelação e sem eco do pseudônimo, do motivo nem do campo.
    Controles: 1 e 100 pseudônimos (de forma válida) são aceitos."""
    (a,) = _students(h, STUDENT_A)
    count = _identity_event_count(h.sql)

    with _session(h, mode) as s:
        for case_id, arguments, echo in _invalid_cases(a.pseudo_id):
            outcome, seen = s.reveal(arguments)
            _assert_protocol_refusal(outcome, seen, a.pseudo_id, *echo)
            assert _identity_event_count(h.sql) == count, case_id

        one = {"pseudonyms": [a.alias], "reason": REASON}
        assert _assert_success(*s.reveal(one), one) == {"people": [a.revealed], "not_found": []}
        hundred = {"pseudonyms": [f"f{i:05x}" for i in range(100)], "reason": REASON}
        body = _assert_success(*s.reveal(hundred), hundred)
        assert len(body["people"]) + len(body["not_found"]) == 100


IDENTITY_ITEMS = [
    pytest.param(STUDENT_A.ra, id="ra-canario"),
    pytest.param(STUDENT_A.email, id="email-canario"),
    pytest.param(str(TG_STUDENT_A1.user_id), id="id-numerico-do-telegram"),
    pytest.param(f"@{TG_STUDENT_A1.username}", id="arroba-usuario"),
]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("item", IDENTITY_ITEMS)
def test_identity_shaped_pseudonym_is_refused_without_echo(h: _H, item: str, mode: str) -> None:
    """Entrada por pessoa só por pseudônimo (identidade só pela revelação; `PERSON_REF`): um item de
    `pseudonyms` com um RA canário (só dígitos), um e-mail canário, um identificador numérico do Telegram ou um
    `@usuário`, ao lado de um apelido de forma válida → `-32602` com a `message` exata, sem eco do item, sem
    canário e com o gancho de pedido vazio; nenhum evento de revelação."""
    count = _identity_event_count(h.sql)
    arguments = {"pseudonyms": ["cafe", item], "reason": REASON}

    with _session(h, mode) as s:
        outcome, seen = s.reveal(arguments)

    _assert_protocol_refusal(outcome, seen, item, item.lstrip("@"))
    assert _identity_event_count(h.sql) == count


# --------------------------------------------------------------------------------------------------
# O motivo não volta


@pytest.mark.parametrize("mode", MODES)
def test_reason_with_canary_name_never_comes_back(h: _H, mode: str) -> None:
    """Identidade só pela revelação (sentinela de entrada): o motivo com o nome de uma pessoa canário da API base (que não é
    alvo da revelação) não volta no resultado do sucesso (que só tem as identidades reveladas: controle positivo
    com o aluno revelado), no erro de execução do token sem a permissão nem na recusa de protocolo."""
    (a,) = _students(h, STUDENT_A)
    secret, _ = create_token(h.client, ADMIN_TOKEN, NO_REVEAL, label="token sem revelacao")
    arguments = {"pseudonyms": [a.pseudo_id], "reason": CANARY_REASON}
    assert find_canary(CANARY_REASON, prefixes=ALL_PREFIXES) != []

    with _session(h, mode) as s:
        outcome, seen = s.reveal(arguments)
    body = _assert_success(outcome, seen, arguments)
    assert body == {"people": [a.revealed], "not_found": []}
    assert scan(outcome, _secrets(), prefixes=ALL_PREFIXES) == []
    assert _names_found(outcome.text) == {f"aluno-nome:{STUDENT_A.name}"}

    with _session(h, mode, token=secret) as s:
        denied, seen = s.reveal(arguments)
        refused, refused_seen = s.reveal({**arguments, "campo_extra_sentinela": True})
    assert (denied.is_error, denied.text) == (True, FORBIDDEN_TEXT)
    assert len(seen) == 1
    assert scan(denied, _secrets(), prefixes=SCAN_PREFIXES) == []
    _assert_protocol_refusal(refused, refused_seen, "campo_extra_sentinela")
