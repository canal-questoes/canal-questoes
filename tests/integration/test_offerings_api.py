"""Oferecimentos: cadastro, listagem e consulta.

Cobre o cadastro, a listagem e a consulta, inclusive as regras do código do oferecimento
e a entrada estrita. Contratos: os de oferecimentos e bots e o esquema do banco; e as convenções
da API base que continuam valendo (ordem
das verificações, entrada estrita, horários em UTC, cursores, sem eco).

Corpo inteiro por `==` em toda resposta de sucesso, com `assert_matches`; `offering_id` conferido
como UUID v4 canônico e nunca por valor literal; horários pelo `FakeClock` e `iso(dt)`. Dados só
fictícios (tests/helpers/canary.py e nomes inventados).

Mensagens livres e contato do professor:
o `Offering` traz `free_messages` e `contact_email_set`; o `_draft` esperado (um rascunho cadastrado
sem os campos opcionais) traz os dois, falsos (o padrão). `SCHEMA_FIELDS`, os nomes de campo do
pedido de cadastro que o envelope pode devolver em `fields`, inclui os dois campos opcionais do pedido
(`free_messages`, `contact_email`). Os casos da configuração ficam em `test_offering_settings.py`. Sem as
chaves na resposta, os testes que comparam o corpo inteiro falham.
"""

from __future__ import annotations

import itertools
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_offering, create_token, marker, register_person
from tests.helpers.canary import URSULA, ZEFIRO, find_canary
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo

ADMIN = auth(ADMIN_TOKEN)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"
VALID = {"code": "mc404-2031s1", "course": COURSE, "term": TERM, "timezone": TZ}

# Limites do contrato de cadastro (depois de tirar os espaços das pontas).
COURSE_200 = ("Disciplina " * 20)[:199] + "Z"
TERM_40 = ("Periodo " * 5)[:39] + "Z"
assert len(COURSE_200) == 200 and len(TERM_40) == 40
assert COURSE_200 == COURSE_200.strip() and TERM_40 == TERM_40.strip()

# Pessoa de nome de uma palavra só (o código com o nome completo dela é recusado).
ONE_WORD_NAME = "Quitéria"

_DROP: Any = object()


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _with(**changes: Any) -> dict:
    """Corpo válido com as trocas dadas (`_DROP` tira o campo)."""
    body = dict(VALID)
    for key, value in changes.items():
        if value is _DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def _post(client: Any, body: Any, headers: dict[str, str] = ADMIN) -> Any:
    return client.post("/v1/offerings", json=body, headers=headers)


def _post_raw(client: Any, body: Any, headers: dict[str, str] = ADMIN) -> Any:
    """Envia o JSON com escapes ASCII (`\\u0000`, `\\ud800`), para caracteres que o httpx2 não
    codifica em UTF-8 (substituto isolado)."""
    return client.post("/v1/offerings", content=json.dumps(body).encode("ascii"),
                       headers={**headers, "Content-Type": "application/json"})


def _created(resp: Any) -> dict:
    assert resp.status_code == 201, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.create", 201, body)
    offering_id = body["offering_id"]
    assert UUID_RE.fullmatch(offering_id), offering_id
    parsed = uuid.UUID(offering_id)
    assert parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == offering_id
    return body


def _get(client: Any, path: str, params: Any = None, headers: dict[str, str] = ADMIN) -> Any:
    return client.get(path, params=params, headers=headers)


def _get_ok(client: Any, ref: str) -> dict:
    resp = _get(client, f"/v1/offerings/{ref}")
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


def _list_ok(client: Any, params: Any = None) -> dict:
    resp = _get(client, "/v1/offerings", params=params)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.list", 200, body)
    return body


def _draft(offering_id: str, *, code: str, created_at: str, course: str = COURSE, term: str = TERM,
           tz: str = TZ, window: int = 1440) -> dict:
    """`Offering` esperado de um rascunho."""
    return {"offering_id": offering_id, "code": code, "course": course, "term": term, "timezone": tz,
            "response_window_minutes": window, "state": "draft", "created_at": created_at,
            "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
            # As mensagens livres desligadas por padrão e nenhum contato do professor.
            "free_messages": False, "contact_email_set": False,
            # `purged_at` fica nulo sem expurgo.
            "purged_at": None}


def _created_event(body: dict, at: str, actor: str = "bootstrap") -> dict:
    return {"type": "offering.created", "occurred_at": at, "actor_token_id": actor,
            "data": {"offering_id": body["offering_id"], "code": body["code"]}}


def _events(client: Any) -> list[dict]:
    """Os `offering.created` pela API, em ordem de `seq` (crescente), sem o `seq`."""
    events = all_events(client, ADMIN_TOKEN, "offering.created")
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _rows(sql: psycopg.Connection) -> list[tuple]:
    return sql.execute(
        "SELECT offering_id::text, code, course, term, timezone, response_window_minutes, state, "
        "created_at, activated_at, closed_at, archived_at, archive_code_sha256, "
        "archive_code_expires_at FROM offerings ORDER BY id").fetchall()


def _stored(sql: psycopg.Connection, code: str) -> tuple[str, str]:
    return sql.execute("SELECT course, term FROM offerings WHERE code = %s", (code,)).fetchone()


def _counts(sql: psycopg.Connection) -> dict[str, int]:
    return {
        "offerings": sql.execute("SELECT count(*) FROM offerings").fetchone()[0],
        "offering.created": sql.execute(
            "SELECT count(*) FROM events WHERE type = 'offering.created'").fetchone()[0],
    }


SCHEMA_FIELDS = frozenset({"code", "course", "term", "timezone", "response_window_minutes",
                           # Os campos novos do pedido (mensagens livres e contato do professor).
                           "free_messages", "contact_email"})


def _sentinels(value: Any) -> list[str]:
    """Textos enviados com 8+ caracteres (e as partes em volta de NUL/substitutos), para o eco. As
    chaves que são nomes de campo do esquema ficam de fora: o envelope as devolve em `fields`
    (como na API base); as desconhecidas continuam sentinelas."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += ([] if key in SCHEMA_FIELDS else _sentinels(key)) + _sentinels(item)
    elif isinstance(value, list):
        for item in value:
            found += _sentinels(item)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        for part in re.split("[\x00\ud800-\udfff]", text):
            for candidate in dict.fromkeys((part, part.strip())):
                if len(candidate) >= 8:
                    found.append(candidate)
    return found


def _client_backends(sql: psycopg.Connection) -> list[tuple[int, str]]:
    return sql.execute(
        "SELECT pid, state FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'").fetchall()


def _wait_backends_gone(sql: psycopg.Connection, timeout: float = 10.0) -> None:
    """Espera as outras conexões ao banco do teste terminarem (as estatísticas de uma conexão só
    entram em `pg_stat_database` quando ela termina); encerra as ociosas na metade do prazo (sem
    transação aberta, o encerramento não conta rollback). Espera real exigida pelo mecanismo de
    estatísticas do Postgres, como em test_people_api.py."""
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


def test_create_returns_exact_draft_with_event(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """201 com o corpo inteiro exato (estado `draft`,
    `bot: null`, horários de transição nulos, `created_at` do `FakeClock`, identificador UUID v4 novo
    por cadastro); sem janela, 1440; com janela, a enviada; o banco guarda o mesmo, sem código de
    arquivamento; a consulta por código e por identificador devolve o mesmo corpo; um
    `offering.created` exato por cadastro."""
    t0 = clock.now()
    first = _created(_post(client, VALID))
    assert first == _draft(first["offering_id"], code="mc404-2031s1", created_at=iso(t0))

    clock.advance(1.5)
    t1 = clock.now()
    second = _created(_post(client, {"code": "mc404-2031s2", "course": "Estruturas de Dados",
                                     "term": "2031s2", "timezone": "UTC",
                                     "response_window_minutes": 60}))
    assert second == _draft(second["offering_id"], code="mc404-2031s2", course="Estruturas de Dados",
                            term="2031s2", tz="UTC", window=60, created_at=iso(t1))
    assert second["offering_id"] != first["offering_id"]

    assert _rows(sql) == [
        (first["offering_id"], "mc404-2031s1", COURSE, TERM, TZ, 1440, "draft", t0, None, None, None,
         None, None),
        (second["offering_id"], "mc404-2031s2", "Estruturas de Dados", "2031s2", "UTC", 60, "draft", t1,
         None, None, None, None, None),
    ]
    for body in (first, second):
        assert _get_ok(client, body["code"]) == body
        assert _get_ok(client, body["offering_id"]) == body
    assert _events(client) == [_created_event(first, iso(t0)), _created_event(second, iso(t1))]


@pytest.mark.parametrize("window", [5, 43200], ids=["janela-5", "janela-43200"])
def test_response_window_limits_are_accepted(client: Any, clock: Any, window: int) -> None:
    """a janela de 5 e de 43.200 minutos é aceita e devolvida."""
    body = _created(_post(client, _with(response_window_minutes=window)))
    assert body == _draft(body["offering_id"], code="mc404-2031s1", window=window,
                          created_at=iso(clock.now()))
    assert _get_ok(client, "mc404-2031s1") == body


@pytest.mark.parametrize("code", ["abc", "a" * 40], ids=["code-3-caracteres", "code-40-caracteres"])
def test_code_length_limits_are_accepted(client: Any, clock: Any, sql: psycopg.Connection, code: str) -> None:
    """Código de 3 a 40 caracteres (`^[a-z0-9][a-z0-9-]{2,39}$`; a contraparte
    aceita dos casos `code-2-caracteres` e `code-41-caracteres` de `INVALID_CREATE`): os códigos de 3 e
    de 40 caracteres → 201 com o corpo inteiro exato; o banco guarda o código; a consulta pelo código
    devolve o mesmo corpo; um `offering.created` exato."""
    assert len(code) in (3, 40)
    t0 = clock.now()
    body = _created(_post(client, _with(code=code)))
    assert body == _draft(body["offering_id"], code=code, created_at=iso(t0))
    assert _rows(sql) == [(body["offering_id"], code, COURSE, TERM, TZ, 1440, "draft", t0, None, None, None,
                           None, None)]
    assert _get_ok(client, code) == body
    assert _events(client) == [_created_event(body, iso(t0))]


ACCEPTED_TEXT = [
    pytest.param({"course": "  Cálculo   Numérico  ", "term": "  2031  s1  "}, "Cálculo   Numérico",
                 "2031  s1", id="espacos-nas-pontas"),
    pytest.param({"course": COURSE_200, "term": TERM_40}, COURSE_200, TERM_40, id="limites-200-e-40"),
    pytest.param({"course": "C", "term": "T"}, "C", "T", id="um-caractere"),
]


@pytest.mark.parametrize(("changes", "course", "term"), ACCEPTED_TEXT)
def test_course_and_term_are_trimmed_and_limits_accepted(client: Any, clock: Any, sql: psycopg.Connection,
                                                         changes: dict, course: str, term: str) -> None:
    """`course` e `term` saem e ficam gravados sem os espaços
    das pontas (o miolo não muda); 1 caractere e os limites de 200 (disciplina) e 40 (período) são
    aceitos."""
    body = _created(_post(client, _with(**changes)))
    assert body == _draft(body["offering_id"], code="mc404-2031s1", course=course, term=term,
                          created_at=iso(clock.now()))
    assert _stored(sql, "mc404-2031s1") == (course, term)
    assert _get_ok(client, body["offering_id"]) == body


# ---------------------------------------------------------------------------------------------
# Entrada inválida (uma linha por caso; entrada estrita)
# ---------------------------------------------------------------------------------------------

UUID_V4_CODE = "4f0e1c2a-8b3d-4e5f-9a6b-7c8d9e0f1a2b"
UUID_V1_CODE = "abcd1111-1111-1111-8111-111111111111"

INVALID_CREATE = [
    # obrigatórios
    pytest.param(_with(code=_DROP), [("code", "missing")], id="code-ausente"),
    pytest.param(_with(course=_DROP), [("course", "missing")], id="course-ausente"),
    pytest.param(_with(term=_DROP), [("term", "missing")], id="term-ausente"),
    pytest.param(_with(timezone=_DROP), [("timezone", "missing")], id="timezone-ausente"),
    pytest.param({}, [("code", "missing"), ("course", "missing"), ("term", "missing"),
                      ("timezone", "missing")], id="todos-ausentes-na-ordem-do-esquema"),
    pytest.param(_with(code=None), [("code", "missing")], id="code-null"),
    # code
    pytest.param(_with(code=""), [("code", "too_short")], id="code-vazio"),
    pytest.param(_with(code="ab"), [("code", "too_short")], id="code-2-caracteres"),
    pytest.param(_with(code="c" * 41), [("code", "too_long")], id="code-41-caracteres"),
    pytest.param(_with(code="Mc404-sentinela"), [("code", "invalid_format")], id="code-maiuscula"),
    pytest.param(_with(code="-mc404-sentinela"), [("code", "invalid_format")], id="code-comeca-com-hifen"),
    pytest.param(_with(code="mc404_sentinela"), [("code", "invalid_format")], id="code-sublinhado"),
    pytest.param(_with(code="mc404 sentinela"), [("code", "invalid_format")], id="code-espaco"),
    pytest.param(_with(code="  mc404-sentinela  "), [("code", "invalid_format")], id="code-espacos-nas-pontas"),
    pytest.param(_with(code="matemática-sentinela"), [("code", "invalid_format")], id="code-acento"),
    pytest.param(_with(code=12345678), [("code", "invalid_format")], id="code-inteiro"),
    pytest.param(_with(code=UUID_V4_CODE), [("code", "invalid_format")], id="code-formato-uuid-v4"),
    pytest.param(_with(code=UUID_V1_CODE), [("code", "invalid_format")], id="code-formato-uuid-outra-versao"),
    # course
    pytest.param(_with(course=""), [("course", "too_short")], id="course-vazio"),
    pytest.param(_with(course="   \t  "), [("course", "too_short")], id="course-so-espacos"),
    pytest.param(_with(course="d" * 201), [("course", "too_long")], id="course-201"),
    pytest.param(_with(course="DisciplinaSentinela\x00NulNoMeio"), [("course", "invalid_format")],
                 id="course-nul"),
    pytest.param(_with(course="DisciplinaSentinela\ud800SubstitutoIsolado"), [("course", "invalid_format")],
                 id="course-substituto-isolado"),
    pytest.param(_with(course=12345678), [("course", "invalid_format")], id="course-inteiro"),
    # term
    pytest.param(_with(term=""), [("term", "too_short")], id="term-vazio"),
    pytest.param(_with(term="    "), [("term", "too_short")], id="term-so-espacos"),
    pytest.param(_with(term="p" * 41), [("term", "too_long")], id="term-41"),
    pytest.param(_with(term="PeriodoSentinela\x00NulNoMeio"), [("term", "invalid_format")], id="term-nul"),
    pytest.param(_with(term="PeriodoSentinela\udc00SubstitutoIsolado"), [("term", "invalid_format")],
                 id="term-substituto-isolado"),
    # timezone
    pytest.param(_with(timezone=12345678), [("timezone", "invalid_format")], id="timezone-inteiro"),
    pytest.param(_with(timezone=""), [("timezone", "too_short")], id="timezone-vazio"),
    pytest.param(_with(timezone="Z" * 65), [("timezone", "too_long")], id="timezone-65"),
    pytest.param(_with(timezone="America/Campinas"), [("timezone", "invalid_value")], id="timezone-inexistente"),
    pytest.param(_with(timezone="america/sao_paulo"), [("timezone", "invalid_value")], id="timezone-caixa"),
    pytest.param(_with(timezone="Factory"), [("timezone", "invalid_value")], id="timezone-factory"),
    # response_window_minutes
    pytest.param(_with(response_window_minutes=4), [("response_window_minutes", "out_of_range")], id="janela-4"),
    pytest.param(_with(response_window_minutes=43201), [("response_window_minutes", "out_of_range")],
                 id="janela-43201"),
    pytest.param(_with(response_window_minutes=0), [("response_window_minutes", "out_of_range")], id="janela-0"),
    pytest.param(_with(response_window_minutes=-60), [("response_window_minutes", "out_of_range")],
                 id="janela-negativa"),
    pytest.param(_with(response_window_minutes=1.5), [("response_window_minutes", "invalid_format")],
                 id="janela-fracao"),
    pytest.param(_with(response_window_minutes="60"), [("response_window_minutes", "invalid_format")],
                 id="janela-texto"),
    pytest.param(_with(response_window_minutes=True), [("response_window_minutes", "invalid_format")],
                 id="janela-booleano"),
    # entrada estrita e vários erros
    pytest.param(_with(campo_extra_sentinela="valor-extra-sentinela"), [("body", "unknown_field")],
                 id="campo-extra"),
    pytest.param({"timezone": "America/Campinas", "term": "", "code": "ab", "course": COURSE,
                  "campo_extra_sentinela": 1},
                 [("code", "too_short"), ("term", "too_short"), ("timezone", "invalid_value"),
                  ("body", "unknown_field")], id="varios-erros-na-ordem-do-esquema"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_CREATE)
def test_create_rejects_invalid_body_without_echo(client: Any, sql: psycopg.Connection, body: dict,
                                                  fields: list[tuple[str, str]]) -> None:
    """Entrada estrita; uma linha por caso, inclusive
    U+0000 e substituto isolado em `course` e `term`, código no formato de UUID; na ordem do
    esquema, campo desconhecido no fim: 400 com `fields` exatos, sem eco dos valores e sem nada
    gravado (nem oferecimento nem evento)."""
    resp = _post_raw(client, body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *_sentinels(body))
    assert _counts(sql) == {"offerings": 0, "offering.created": 0}


RAW_BODIES = [
    pytest.param(None, [("body", "missing")], id="sem-corpo"),
    pytest.param(b"isto nao e json sentinela", [("body", "invalid_format")], id="nao-json"),
    pytest.param(b'[{"code": "mc404-sentinela"}]', [("body", "invalid_format")], id="lista-json"),
]


@pytest.mark.parametrize(("raw", "fields"), RAW_BODIES)
def test_create_rejects_missing_or_non_object_body(client: Any, sql: psycopg.Connection, raw: bytes | None,
                                                   fields: list[tuple[str, str]]) -> None:
    """Entrada estrita, como na API base: sem corpo → `[{body, missing}]`;
    corpo que não é JSON ou não é objeto → `[{body, invalid_format}]`; sem eco e sem nada gravado."""
    if raw is None:
        resp = client.post("/v1/offerings", headers=ADMIN)
    else:
        resp = client.post("/v1/offerings", content=raw,
                           headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    if raw is not None:
        assert_no_echo(resp, "isto nao e json sentinela", "mc404-sentinela")
    assert _counts(sql) == {"offerings": 0, "offering.created": 0}


# ---------------------------------------------------------------------------------------------
# Código com identidade
# ---------------------------------------------------------------------------------------------


def test_code_with_registered_identity_is_rejected_without_echo(client: Any, clock: Any,
                                                                sql: psycopg.Connection) -> None:
    """O código não contém identidade de pessoa cadastrada: o código com o RA de uma
    pessoa cadastrada (como subcadeia
    de dígitos, também dentro de uma sequência maior) ou com o nome completo de uma pessoa de nome de
    uma palavra só → 400 `[{code, invalid_value}]`, sem eco e sem nada gravado. Controle positivo
    (limitação conhecida): com Zéfiro Canário Quintela cadastrado, `zefiro-2026sb` (uma
    palavra solta de um nome com várias) é aceito."""
    register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
    register_person(client, ADMIN_TOKEN, name=ONE_WORD_NAME)
    cases = [
        (f"turma-{ZEFIRO.ra}", ZEFIRO.ra),
        (f"t{ZEFIRO.ra}x", ZEFIRO.ra),
        (f"mc404-{ZEFIRO.ra}4", ZEFIRO.ra),
        ("quiteria-2031", "quiteria"),
        ("2031-quiteria", "quiteria"),
        ("quiteria2031", "quiteria"),
    ]
    for code, sentinel in cases:
        resp = _post(client, _with(code=code))
        assert_error(resp, 400, "invalid_request", fields=[("code", "invalid_value")])
        assert_no_echo(resp, code, sentinel)
        assert find_canary(resp.text) == []
    assert _counts(sql) == {"offerings": 0, "offering.created": 0}

    accepted = _created(_post(client, _with(code="zefiro-2026sb")))
    assert accepted == _draft(accepted["offering_id"], code="zefiro-2026sb", created_at=iso(clock.now()))
    assert _counts(sql) == {"offerings": 1, "offering.created": 1}


# ---------------------------------------------------------------------------------------------
# Redação de disciplina e período
# ---------------------------------------------------------------------------------------------


def test_term_with_registered_ra_is_redacted_beyond_40(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Texto livre gravado já redigido, que pode passar de
    40: um período de 40 caracteres com o RA de 10 dígitos de uma pessoa cadastrada → 201, gravado e
    devolvido com a marca (mais de 40 caracteres), e o corpo valida no esquema congelado."""
    zefiro = register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
    term = f"turma {ZEFIRO.ra} " + "x" * 23
    assert len(term) == 40
    expected_term = f"turma {marker(zefiro['alias'])} " + "x" * 23
    assert len(expected_term) > 40

    body = _created(_post(client, _with(term=term)))
    assert_matches("offerings.create", 201, body)
    assert body == _draft(body["offering_id"], code="mc404-2031s1", term=expected_term,
                          created_at=iso(clock.now()))
    assert _stored(sql, "mc404-2031s1") == (COURSE, expected_term)
    assert _get_ok(client, "mc404-2031s1") == body
    assert find_canary(json.dumps(body, ensure_ascii=False)) == []


def test_course_and_term_are_redacted_on_write_and_on_output(client: Any, clock: Any,
                                                             sql: psycopg.Connection) -> None:
    """a disciplina e o período com o nome e o RA de uma pessoa
    já cadastrada ficam gravados **já redigidos** e saem com `marker(alias)`; o nome de uma pessoa
    cadastrada **depois** fica cru no banco e só é redigido na saída (consulta e listagem)."""
    zefiro = register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
    mz = marker(zefiro["alias"])
    t0 = clock.now()
    first = _created(_post(client, _with(code="redacao-2031a", course=f"Seminários com {ZEFIRO.name}",
                                         term=f"RA {ZEFIRO.ra}")))
    assert first == _draft(first["offering_id"], code="redacao-2031a", course=f"Seminários com {mz}",
                           term=f"RA {mz}", created_at=iso(t0))
    assert _stored(sql, "redacao-2031a") == (f"Seminários com {mz}", f"RA {mz}")

    later_course = f"Tópicos com {URSULA.name}"
    second = _created(_post(client, _with(code="redacao-2031b", course=later_course)))
    assert second == _draft(second["offering_id"], code="redacao-2031b", course=later_course,
                            created_at=iso(t0))
    assert _stored(sql, "redacao-2031b") == (later_course, TERM)

    ursula = register_person(client, ADMIN_TOKEN, **URSULA.payload())
    mu = marker(ursula["alias"])
    expected_second = _draft(second["offering_id"], code="redacao-2031b", course=f"Tópicos com {mu}",
                             created_at=iso(t0))
    assert _get_ok(client, "redacao-2031b") == expected_second
    assert _get_ok(client, second["offering_id"]) == expected_second
    listed = _list_ok(client)
    assert listed["items"] == [first, expected_second]
    assert _stored(sql, "redacao-2031b") == (later_course, TERM)
    for text in (json.dumps(first, ensure_ascii=False), json.dumps(listed, ensure_ascii=False)):
        assert find_canary(text) == []


# ---------------------------------------------------------------------------------------------
# Código repetido
# ---------------------------------------------------------------------------------------------


def test_duplicate_code_is_409_without_db_error(app_factory: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Sem erro no banco (checagem antes do `INSERT`): o código
    repetido → 409 `already_exists` `[{code, duplicate}]`, sem o identificador existente e sem eco;
    o 400 vem antes do 409; nada é gravado; `pg_stat_database.xact_rollback` não muda (a mesma
    medição do conflito de pessoas da API base), com controle positivo."""
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        existing = create_offering(c, ADMIN_TOKEN, code="dup-2031s1")
    _wait_backends_gone(sql)
    before = _counts(sql)
    assert before == {"offerings": 1, "offering.created": 1}
    rollbacks_before = _xact_rollback(sql)

    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        resp = _post(c, _with(code="dup-2031s1", course="Outra Disciplina Sentinela"))
        assert_error(resp, 409, "already_exists", fields=[("code", "duplicate")])
        assert_no_echo(resp, existing["offering_id"], "Outra Disciplina Sentinela", "dup-2031s1")
        resp = _post(c, _with(code="dup-2031s1", timezone="America/Campinas"))
        assert_error(resp, 400, "invalid_request", fields=[("timezone", "invalid_value")])
        assert_no_echo(resp, "America/Campinas", "dup-2031s1")
    _wait_backends_gone(sql)
    assert _counts(sql) == before
    assert _xact_rollback(sql) == rollbacks_before, "o código repetido provocou rollback no banco"

    # Controle positivo: um rollback de outra conexão aparece na mesma medição.
    with psycopg.connect(db_url, connect_timeout=5) as probe:
        probe.execute("SELECT 1")
        probe.rollback()
    _wait_backends_gone(sql)
    assert _xact_rollback(sql) >= rollbacks_before + 1


# ---------------------------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------------------------


def test_list_follows_creation_order_with_cursor_chain(client: Any, clock: Any) -> None:
    """com o relógio voltando entre cadastros, a listagem segue a ordem de
    cadastro, com os mesmos campos da consulta; encadear `next_cursor` com `limit=3` percorre tudo
    uma vez, com cursores crescentes; página vazia → `next_cursor == after` (também no banco vazio e
    além do fim)."""
    assert _list_ok(client) == {"items": [], "next_cursor": 0}
    assert _list_ok(client, {"after": 5}) == {"items": [], "next_cursor": 5}

    created = []
    for index, shift in enumerate([0, -3600, -86400, 30 * 86400, -60, 5, 1]):
        clock.advance(shift)
        created.append(_created(_post(client, _with(code=f"ordem-{index}-2031"))))
    for body in created:
        assert _get_ok(client, body["code"]) == body

    assert _list_ok(client)["items"] == created
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
    assert seen == created
    assert _list_ok(client, {"after": after + 1000}) == {"items": [], "next_cursor": after + 1000}


def test_list_default_limit_is_100_and_limits_1_and_500_are_accepted(client: Any) -> None:
    """Como `people.list`: com 101 oferecimentos, sem `limit` → os 100
    primeiros e o cursor leva ao 101º; `limit=1` → só o primeiro; `limit=500` → todos."""
    created = [create_offering(client, ADMIN_TOKEN) for _ in range(101)]
    default = _list_ok(client)
    assert default["items"] == created[:100]
    rest = _list_ok(client, {"after": default["next_cursor"]})
    assert rest["items"] == created[100:]
    one = _list_ok(client, {"limit": 1})
    assert one["items"] == created[:1]
    everything = _list_ok(client, {"limit": 500})
    assert everything["items"] == created
    assert everything["next_cursor"] == rest["next_cursor"]
    assert one["next_cursor"] < default["next_cursor"] < rest["next_cursor"]


INVALID_LIST_QUERY = [
    pytest.param({"limit": "0"}, [("limit", "out_of_range")], id="limit-0"),
    pytest.param({"limit": "501"}, [("limit", "out_of_range")], id="limit-501"),
    pytest.param({"limit": "abcdefgh"}, [("limit", "invalid_format")], id="limit-texto"),
    pytest.param({"limit": "1.5"}, [("limit", "invalid_format")], id="limit-fracao"),
    pytest.param({"after": "abcdefgh"}, [("after", "invalid_format")], id="after-texto"),
    pytest.param({"after": "-1"}, [("after", "out_of_range")], id="after-negativo"),
    pytest.param({"parametro_sentinela": "valor_sentinela"}, [("query", "unknown_field")], id="desconhecido"),
    pytest.param({"limit": "0", "parametro_sentinela": "1"}, [("limit", "out_of_range"), ("query", "unknown_field")],
                 id="desconhecido-no-fim"),
]


@pytest.mark.parametrize(("params", "fields"), INVALID_LIST_QUERY)
def test_list_rejects_invalid_query_without_echo(client: Any, params: dict, fields: list[tuple[str, str]]) -> None:
    """Como na API base: `limit` fora de 1..500 → `out_of_range`; não inteiro →
    `invalid_format`; `after` → `invalid_format`/`out_of_range`; parâmetro desconhecido → `[{query,
    unknown_field}]`, no fim; sem eco."""
    create_offering(client, ADMIN_TOKEN)
    resp = _get(client, "/v1/offerings", params=params)
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, *_sentinels(params))


# ---------------------------------------------------------------------------------------------
# Consulta pelo código ou pelo identificador
# ---------------------------------------------------------------------------------------------


def test_get_by_code_or_id_returns_the_same_body(client: Any, clock: Any) -> None:
    """O código do oferecimento (a consulta aceita o código ou o
    identificador): cada oferecimento sai igual pelo código, pelo identificador e na listagem."""
    first = _created(_post(client, VALID))
    clock.advance(10)
    second = _created(_post(client, _with(code="mc202-2031s1", course="Estruturas de Dados")))
    for body in (first, second):
        assert _get_ok(client, body["code"]) == body
        assert _get_ok(client, body["offering_id"]) == body
    assert _list_ok(client)["items"] == [first, second]


def test_get_unknown_offering_is_404(client: Any) -> None:
    """código de formato válido sem oferecimento, e UUID v4 desconhecido →
    404 `not_found`."""
    existing = _created(_post(client, VALID))
    for ref in ("codigo-inexistente", "mc404-2031s2", str(uuid.uuid4())):
        assert_error(_get(client, f"/v1/offerings/{ref}"), 404, "not_found")
    assert _get_ok(client, existing["code"]) == existing


INVALID_REFS = [
    pytest.param("ab", id="dois-caracteres"),
    pytest.param("MC404-2031S1", id="maiusculas"),
    pytest.param("-mc404-2031", id="comeca-com-hifen"),
    pytest.param("mc404_2031s1", id="sublinhado"),
    pytest.param("mc404 2031s1", id="espaco"),
    pytest.param("matemática-2031", id="acento"),
    pytest.param("r" * 41, id="41-caracteres"),
    pytest.param("UUID-EXISTENTE-EM-MAIUSCULAS", id="uuid-existente-maiusculo"),
]


@pytest.mark.parametrize("ref", INVALID_REFS)
def test_get_rejects_malformed_ref_without_echo(client: Any, ref: str) -> None:
    """Formato da referência (outro formato dá `[{ref, invalid_format}]`): `ref`
    que não é UUID v4 canônico minúsculo nem código (inclusive o UUID de um oferecimento existente em
    maiúsculas) → 400 `[{ref, invalid_format}]`, sem eco."""
    existing = _created(_post(client, VALID))
    if ref == "UUID-EXISTENTE-EM-MAIUSCULAS":
        ref = existing["offering_id"].upper()
    resp = _get(client, f"/v1/offerings/{ref}")
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    if len(ref) >= 8:
        assert_no_echo(resp, ref)


# ---------------------------------------------------------------------------------------------
# Autenticação, entrada estrita e permissões
# ---------------------------------------------------------------------------------------------


def test_offering_routes_require_token_and_reject_unknown_query(client: Any, sql: psycopg.Connection) -> None:
    """Entrada estrita, como na API base: sem token ou com token
    desconhecido → 401 nas três operações; parâmetro de consulta desconhecido → `[{query,
    unknown_field}]`, sem eco; nada é gravado pelos pedidos recusados."""
    existing = _created(_post(client, VALID))
    calls = [
        ("POST", "/v1/offerings", {"json": _with(code="nova-2031s1")}),
        ("GET", "/v1/offerings", {}),
        ("GET", f"/v1/offerings/{existing['code']}", {}),
    ]
    for method, path, kwargs in calls:
        assert_error(client.request(method, path, **kwargs), 401, "unauthenticated")
        unknown = auth("cq_tokendesconhecidosentinela_" + "z" * 20)
        assert_error(client.request(method, path, headers=unknown, **kwargs), 401, "unauthenticated")
        resp = client.request(method, path, params={"parametro_sentinela": "valor_sentinela"},
                              headers=ADMIN, **kwargs)
        assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
        assert_no_echo(resp, "parametro_sentinela", "valor_sentinela")
    assert _counts(sql) == {"offerings": 1, "offering.created": 1}


def test_offering_routes_require_their_permissions(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Ordem: validação → permissão → regras; `offerings:write` para
    cadastrar e `offerings:read` para listar e consultar; um token de pessoas não faz nenhum dos dois;
    o 400 vem antes do 403 e o 403 antes do 409 e do 404; o `offering.created` registra o `token_id` de
    quem cadastrou."""
    t0 = clock.now()
    existing = _created(_post(client, _with(code="perm-2031s1")))
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="leitor de oferecimentos")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escritor de oferecimentos")
    other, _ = create_token(client, ADMIN_TOKEN, ["people:read", "people:write"], label="so pessoas")

    # offerings:read lista e consulta, mas não cadastra (nem revela o conflito)
    assert_error(_post(client, _with(code="perm-leitor"), headers=auth(reader)), 403, "forbidden",
                 permission="offerings:write")
    assert_error(_post(client, _with(code="perm-2031s1"), headers=auth(reader)), 403, "forbidden",
                 permission="offerings:write")
    assert_error(_post(client, _with(code="ab"), headers=auth(reader)), 400, "invalid_request",
                 fields=[("code", "too_short")])
    resp = _get(client, "/v1/offerings", headers=auth(reader))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("offerings.list", 200, resp.json())
    admin_list = _list_ok(client)
    assert admin_list["items"] == [existing]
    assert resp.json() == admin_list
    resp = _get(client, f"/v1/offerings/{existing['code']}", headers=auth(reader))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("offerings.get", 200, resp.json())
    assert resp.json() == existing

    # offerings:write cadastra, mas não lista nem consulta
    clock.advance(20)
    created = _created(_post(client, _with(code="perm-escritor"), headers=auth(writer)))
    assert created == _draft(created["offering_id"], code="perm-escritor", created_at=iso(clock.now()))
    for path in ("/v1/offerings", f"/v1/offerings/{existing['code']}", f"/v1/offerings/{uuid.uuid4()}",
                 "/v1/offerings/codigo-inexistente"):
        assert_error(_get(client, path, headers=auth(writer)), 403, "forbidden", permission="offerings:read")
    assert_error(_get(client, "/v1/offerings/MC404", headers=auth(writer)), 400, "invalid_request",
                 fields=[("ref", "invalid_format")])
    assert_error(_get(client, "/v1/offerings", params={"limit": "0"}, headers=auth(writer)), 400,
                 "invalid_request", fields=[("limit", "out_of_range")])

    # um token só de pessoas não faz nada com oferecimentos
    assert_error(_post(client, _with(code="perm-outro"), headers=auth(other)), 403, "forbidden",
                 permission="offerings:write")
    assert_error(_get(client, "/v1/offerings", headers=auth(other)), 403, "forbidden",
                 permission="offerings:read")
    assert_error(_get(client, f"/v1/offerings/{existing['code']}", headers=auth(other)), 403, "forbidden",
                 permission="offerings:read")

    assert _events(client) == [_created_event(existing, iso(t0)),
                               _created_event(created, iso(clock.now()), actor=writer_body["token_id"])]
    assert _counts(sql) == {"offerings": 2, "offering.created": 2}


# ---------------------------------------------------------------------------------------------
# Horários e identificador
# ---------------------------------------------------------------------------------------------


def test_times_leave_in_utc_even_with_another_database_timezone(app_factory: Any, sql: psycopg.Connection,
                                                                db_url: str, clock: Any) -> None:
    """Horários, como na API base: com o banco em `America/Sao_Paulo` e o relógio num
    horário −03:00 com fração, `created_at` sai em UTC com `Z` no cadastro, na listagem e na consulta,
    e o banco guarda o mesmo instante."""
    dbname = sql.execute("SELECT current_database()").fetchone()[0]
    sql.execute(f"ALTER DATABASE \"{dbname}\" SET timezone = 'America/Sao_Paulo'")
    with psycopg.connect(db_url, connect_timeout=5) as probe:
        assert probe.execute("SHOW timezone").fetchone()[0] == "America/Sao_Paulo"
    local = datetime(2031, 6, 15, 21, 30, 15, 250000, tzinfo=timezone(timedelta(hours=-3)))
    clock.set(local)
    with TestClient(app_factory(), raise_server_exceptions=False) as c:
        body = _created(_post(c, VALID))
        assert body["created_at"] == iso(local) == "2031-06-16T00:30:15.250000Z"
        assert body == _draft(body["offering_id"], code="mc404-2031s1", created_at=iso(local))
        assert _list_ok(c)["items"] == [body]
        assert _get_ok(c, body["code"]) == body
    assert sql.execute("SELECT created_at FROM offerings").fetchone()[0] == local


def test_same_data_in_two_databases_gets_different_offering_ids(client: Any, app_factory: Any,
                                                                db_factory: Any, clock: Any) -> None:
    """Um identificador aleatório, nunca derivado dos dados: o mesmo pedido, no mesmo
    instante, em dois bancos vazios → `offering_id` diferentes (UUID v4)."""
    first = _created(_post(client, VALID))
    with TestClient(app_factory(database_url=db_factory()), raise_server_exceptions=False) as other:
        second = _created(_post(other, VALID))
    assert first["created_at"] == second["created_at"] == iso(clock.now())
    assert first["offering_id"] != second["offering_id"]
    assert first == _draft(first["offering_id"], code="mc404-2031s1", created_at=iso(clock.now()))
    assert second == _draft(second["offering_id"], code="mc404-2031s1", created_at=iso(clock.now()))
