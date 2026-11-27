"""Tokens de API e permissões.

Cobre a criação, a revogação e as permissões dos tokens, inclusive a revogação imediata.
Contratos: os erros, a ordem das verificações, as operações de token e o catálogo de permissões;
o modelo dos tokens e os eventos.

Oferecimentos e bots: as permissões
são 8 (entre elas `bots:manage`, `offerings:read`, `offerings:write`), com os literais das
listas, "todas menos a exigida" com 7 e `too_many` com 9 itens; as matrizes de 401 e 403 e o teste
sem token inicial percorrem também as 10 operações dessa parte (201 em `offerings.create` e `team.link`);
`_db_state` inclui as tabelas dessa parte e as filas (um pedido recusado não grava oferecimento, bot,
matrícula, convite, endereço, update, tarefa nem mensagem).

Vínculo do aluno: sem permissão
própria; as matrizes de 401 e 403 e o teste sem token inicial percorrem também as 5 operações dessa parte (200
em todas), e `_db_state` inclui a tabela `invite_sends` (`STUDENT_LINK_STATE_TABLES`; as colunas de
`enrollments`, `invites` e `offerings` já entram pela linha inteira das tabelas de oferecimentos e bots): um pedido
recusado não importa lista, não prepara nem enfileira envio.

Disparo e resposta: sem permissão
própria; as matrizes de 401 e 403 e o teste sem token inicial percorrem também as 6 operações dessa parte (200 em
todas), e `_db_state` inclui as tabelas dessa parte com `id` (`DISPATCH_STATE_TABLES`; as colunas de
`outbox_messages` e `telegram_updates` já entram pela linha inteira das tabelas de oferecimentos e bots; `delivery_contents`
e `media_blobs` ficam fora, porque o laço ordena por `id` e elas só mudam junto com as entregas): um
pedido recusado não prepara, não confirma nem cancela disparo e não grava entrega, submissão nem mensagem
livre. No esqueleto, sem as tabelas e sem a semente de disparos, esses casos falham.

Mensagens livres opcionais: `_db_state`
inclui a tabela `offering_contacts` (`DISPATCH_STATE_TABLES`; a coluna `offerings.free_messages` já entra
pela linha inteira de `offerings`): um pedido recusado não grava, troca nem remove o contato do professor.
As matrizes percorrem também a operação `offerings.update_settings` pelo `VALID_CALLS`
(`tests/helpers/calls.py`). Sem a tabela, os casos que leem `_db_state` falham.

Relatórios e acompanhamento: sem
permissão própria; as matrizes de 401 e 403 e o teste sem token inicial percorrem também as 5 operações dessa parte
pelo `VALID_CALLS` (200 em todas), com `offerings:read` nas leituras (`reports.get`, `reports.list_versions`,
`offerings.get_digest`) e `offerings:write` no reenvio e na alteração (`reports.resend_summary`,
`offerings.update_digest`; `REPORT_PERMISSIONS`, conferido na matriz de 403); `_db_state` inclui as tabelas dessa parte
com `id` (`REPORT_STATE_TABLES`): um pedido recusado não congela versão, não registra aviso nem grava a
configuração do resumo diário. No esqueleto, sem as tabelas, sem as rotas e sem o congelamento da
semente, esses casos falham.

CLI: sem permissão própria; as matrizes de 401 e 403 e o teste sem token inicial
percorrem também as 3
operações dessa parte pelo `VALID_CALLS` (200 em todas), com `offerings:read` no `doctor` e `people:write` na
preparação e na confirmação do expurgo (`DOCTOR_PURGE_PERMISSIONS`, escrita aqui e não lida do catálogo, conferida na
matriz de 403: um token com todas as outras permissões, inclusive `offerings:write`, recebe 403 no expurgo, e o
estado do banco fica igual, `_db_state`); `_db_state` sem tabela própria (as colunas de `offerings` já entram
pela linha inteira). No esqueleto, a semente prepara os alvos do expurgo pela rota ausente,
e esses casos falham.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets as stdlib_secrets
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers.api import ADMIN_TOKEN, all_events, auth, create_token, list_events, register_person
from tests.helpers.calls import VALID_CALLS, seed_state
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo, load_catalog
from tests.helpers.secrets import all_secrets, remember_secret

# As permissões dos tokens, em ordem alfabética.
# Oferecimentos e bots: as 8 permissões, em ordem alfabética.
ALL_PERMISSIONS = ["bots:manage", "events:read", "identity:reveal", "offerings:read", "offerings:write",
                   "people:read", "people:write", "tokens:manage"]

# Operações do catálogo sob /v1 (todas exigem token).
V1_OPS = [op_id for op_id in VALID_CALLS if op_id != "system.health"]

# Status de sucesso de cada operação /v1.
# 201 também em `offerings.create` e `team.link`.
SUCCESS_STATUS = {op_id: 201 if op_id in ("people.register", "tokens.create", "offerings.create", "team.link")
                  else 200 for op_id in V1_OPS}

CATALOG_OPS = {op["id"]: op for op in load_catalog()["operations"]}
PROTECTED_OPS = [op_id for op_id, op in CATALOG_OPS.items() if op["permission"] is not None]

# Relatórios e acompanhamento: a permissão exigida por cada operação dessa parte, escrita aqui (e não
# lida do catálogo) para a matriz de 403 conferir o catálogo.
REPORT_PERMISSIONS = {"reports.get": "offerings:read", "reports.list_versions": "offerings:read",
                  "reports.resend_summary": "offerings:write", "offerings.get_digest": "offerings:read",
                  "offerings.update_digest": "offerings:write"}

# CLI: a permissão exigida por cada operação dessa parte, escrita aqui (e
# não lida do catálogo) para a matriz de 403 conferir o catálogo.
DOCTOR_PURGE_PERMISSIONS = {"system.doctor": "offerings:read", "offerings.prepare_purge": "people:write",
                  "offerings.purge": "people:write"}

# Eventos que um 403 grava de propósito: só a revelação negada.
DENIAL_EVENTS = {"identity.reveal": ["identity.reveal_denied"]}

SECRET_PATTERN = re.compile(r"cq_[A-Za-z0-9_-]{43}")

LABEL_100 = "rotulo-limite-" + "y" * 86
LABEL_101 = "rotulo-longo-" + "x" * 88
assert len(LABEL_100) == 100 and len(LABEL_101) == 101


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public(created: dict) -> dict:
    """O `Token` de listagem/revogação correspondente a um `CreatedToken` (sem o segredo)."""
    return {k: v for k, v in created.items() if k != "secret"}


def _without_seq(event: dict) -> dict:
    return {k: v for k, v in event.items() if k != "seq"}


# Oferecimentos e bots: as tabelas dessa parte e as filas, linha inteira, em ordem de `id`.
OFFERING_BOT_STATE_TABLES = ("offerings", "bots", "enrollments", "invites", "delivery_endpoints", "telegram_updates",
                   "tasks", "outbox_messages")
# Vínculo do aluno: a tabela dessa parte, linha inteira, em ordem de `id`.
STUDENT_LINK_STATE_TABLES = ("invite_sends",)
# Disparo e resposta: as tabelas dessa parte com `id`, linha inteira, em ordem de `id`.
DISPATCH_STATE_TABLES = ("question_refs", "dispatches", "deliveries", "submissions", "free_messages",
                   # O contato do professor.
                   "offering_contacts")
# Relatórios e acompanhamento: as tabelas dessa parte com `id`, linha
# inteira, em ordem de `id` (as colunas de `outbox_messages` e `invite_sends` já entram pela linha inteira
# das tabelas de oferecimentos e bots e do vínculo do aluno; `team_notice_floors`, de linha única gravada pela migração, e
# `dispatch_preview_contents`, sem `id` e que só muda junto com a prévia, ficam fora).
REPORT_STATE_TABLES = ("dispatch_reports", "team_notices", "dispatch_previews", "offering_digests", "invite_batches")


def _db_state(sql: Any) -> dict:
    """Instantâneo do que um pedido recusado não pode mudar (pessoas, tokens, revogações, eventos).

    Oferecimentos e bots: também a linha inteira das tabelas e filas (`OFFERING_BOT_STATE_TABLES`). Vínculo do aluno: e da tabela
    `invite_sends` (`STUDENT_LINK_STATE_TABLES`). Disparo e resposta: e das tabelas com `id` (`DISPATCH_STATE_TABLES`). Relatórios: e
    das tabelas com `id` (`REPORT_STATE_TABLES`). Banco de questões local: e de `question_banks`, lida como `id`, nome,
    versão e o SHA-256 do pacote (não a linha inteira: o JSON da linha traria o pacote em hexadecimal)."""
    return {
        "people": sql.execute("SELECT pseudo_id::text FROM people ORDER BY id").fetchall(),
        "identities": sql.execute("SELECT count(*) FROM identities").fetchone()[0],
        "tokens": sql.execute(
            "SELECT token_id::text, label, permissions, revoked_at FROM api_tokens ORDER BY id").fetchall(),
        "events": sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall(),
        **{table: sql.execute(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id").fetchall()
           for table in OFFERING_BOT_STATE_TABLES + STUDENT_LINK_STATE_TABLES + DISPATCH_STATE_TABLES + REPORT_STATE_TABLES},
        # Banco de questões local
        "question_banks": sql.execute(
            "SELECT id, name, version, encode(sha256(archive), 'hex') FROM question_banks ORDER BY id").fetchall(),
    }


def _table_text(sql: Any, table: str) -> str:
    return sql.execute(f"SELECT coalesce(string_agg(t::text, E'\\n'), '') FROM {table} t").fetchone()[0]


@contextlib.contextmanager
def _context(label: str) -> Iterator[None]:
    """Prefixa a falha com o caso do laço (operação, cabeçalho…)."""
    try:
        yield
    except AssertionError as exc:
        raise AssertionError(f"[{label}] {exc}") from exc


def _post_token(client: Any, token: str, label: str, permissions: list[str]) -> Any:
    return client.post("/v1/tokens", json={"label": label, "permissions": permissions}, headers=auth(token))


def _revoke(client: Any, token: str, token_id: str) -> Any:
    return client.post(f"/v1/tokens/{token_id}/revoke", headers=auth(token))


# ---------------------------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------------------------


def test_create_token_returns_exact_body_and_stores_only_digest(client: Any, clock: Any, sql: Any) -> None:
    """201 com as chaves exatas, rótulo aparado, permissões em ordem
    alfabética, segredo no padrão; o banco guarda só o SHA-256 do segredo. Com as 8
    permissões, fora de ordem, saem em ordem alfabética."""
    clock.advance(1.5)
    created_at = clock.now()
    resp = _post_token(client, ADMIN_TOKEN, "  token do cli diario  ", ["people:read", "events:read"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert_matches("tokens.create", 201, body)
    remember_secret(body["secret"])
    secret = body["secret"]

    assert set(body) == {"token_id", "label", "permissions", "created_at", "revoked_at", "secret"}
    assert body == {
        "token_id": body["token_id"],
        "label": "token do cli diario",
        "permissions": ["events:read", "people:read"],
        "created_at": iso(created_at),
        "revoked_at": None,
        "secret": secret,
    }
    token_uuid = uuid.UUID(body["token_id"])
    assert token_uuid.version == 4 and str(token_uuid) == body["token_id"]
    assert SECRET_PATTERN.fullmatch(secret)

    rows = sql.execute(
        "SELECT token_id::text, label, secret_sha256, permissions, created_at, revoked_at FROM api_tokens"
    ).fetchall()
    assert rows == [(body["token_id"], "token do cli diario", _sha256(secret),
                     ["events:read", "people:read"], created_at, None)]
    tokens_text = _table_text(sql, "api_tokens")
    assert secret not in tokens_text and secret[3:] not in tokens_text
    events_text = _table_text(sql, "events")
    assert secret not in events_text and secret[3:] not in events_text and _sha256(secret) not in events_text

    # Mesmo rótulo e mesmas permissões: outro token, outro segredo, outro resumo.
    resp2 = _post_token(client, ADMIN_TOKEN, "  token do cli diario  ", ["people:read", "events:read"])
    assert resp2.status_code == 201, resp2.text
    body2 = resp2.json()
    assert_matches("tokens.create", 201, body2)
    remember_secret(body2["secret"])
    assert body2["token_id"] != body["token_id"]
    assert body2["secret"] != secret
    digests = [r[0] for r in sql.execute("SELECT secret_sha256 FROM api_tokens ORDER BY id").fetchall()]
    assert digests == [_sha256(secret), _sha256(body2["secret"])]

    # Todas as permissões, fora de ordem, saem em ordem alfabética.
    # São as oito.
    resp3 = _post_token(client, ADMIN_TOKEN, "todas as permissoes",
                        ["tokens:manage", "people:read", "identity:reveal", "events:read", "people:write",
                         "offerings:write", "bots:manage", "offerings:read"])
    assert resp3.status_code == 201, resp3.text
    body3 = resp3.json()
    assert_matches("tokens.create", 201, body3)
    remember_secret(body3["secret"])
    assert body3["permissions"] == ALL_PERMISSIONS
    stored = sql.execute("SELECT permissions FROM api_tokens WHERE token_id = %s",
                         (body3["token_id"],)).fetchone()[0]
    assert stored == ALL_PERMISSIONS


def test_new_token_has_exactly_its_permissions_and_never_expires(client: Any, clock: Any, sql: Any) -> None:
    """o token novo lista pessoas e eventos (200), é barrado no
    cadastro (403 `people:write`) e continua valendo depois de 10 anos de relógio."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Listada Pelo Token", ra="5500000001")
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read", "events:read"], label="leitor novo")

    resp = client.get("/v1/people", headers=auth(secret))
    assert resp.status_code == 200, resp.text
    assert_matches("people.list", 200, resp.json())
    assert resp.json()["items"] == [person]
    page = list_events(client, secret, "person.registered")
    assert [e["data"] for e in page["items"]] == [{"pseudo_id": person["pseudo_id"], "alias": person["alias"]}]

    before = _db_state(sql)
    resp = client.post("/v1/people", json={"name": "Pessoa Sem Permissao", "ra": "5500000002"},
                       headers=auth(secret))
    assert_error(resp, 403, "forbidden", permission="people:write")
    assert_no_echo(resp, "Pessoa Sem Permissao", "5500000002")
    assert _db_state(sql) == before

    clock.set(datetime(2041, 1, 1, tzinfo=timezone.utc))
    resp = client.get("/v1/people", headers=auth(secret))
    assert resp.status_code == 200, resp.text
    assert_matches("people.list", 200, resp.json())
    assert resp.json()["items"] == [person]
    page = list_events(client, secret, "person.registered")
    assert [e["data"] for e in page["items"]] == [{"pseudo_id": person["pseudo_id"], "alias": person["alias"]}]


# ---------------------------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------------------------


def test_list_tokens_in_creation_order_without_secrets(client: Any, clock: Any, sql: Any) -> None:
    """ordem de criação mesmo com o relógio voltando; chaves exatas; sem
    segredo nem resumo; o token inicial não aparece (nem na tabela)."""
    resp = client.get("/v1/tokens", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text
    assert_matches("tokens.list", 200, resp.json())
    assert resp.json() == {"items": []}
    assert sql.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 0

    start = clock.now()
    grants = [["people:read"], ["people:read", "events:read"], ["tokens:manage"],
              ["people:write", "identity:reveal"], list(reversed(ALL_PERMISSIONS))]
    expected = []
    for i, permissions in enumerate(grants):
        at = clock.set(start - timedelta(hours=i))
        # Rótulos em ordem alfabética inversa à de criação.
        label = f"rotulo {len(grants) - i}"
        _, body = create_token(client, ADMIN_TOKEN, permissions, label=label)
        expected.append({"token_id": body["token_id"], "label": label, "permissions": sorted(permissions),
                         "created_at": iso(at), "revoked_at": None})
    clock.set(start + timedelta(days=1))

    resp = client.get("/v1/tokens", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_matches("tokens.list", 200, body)
    assert body == {"items": expected}
    assert find_secrets(resp.text, all_secrets()) == []
    assert _sha256(ADMIN_TOKEN) not in _table_text(sql, "api_tokens")


# ---------------------------------------------------------------------------------------------
# Revogação
# ---------------------------------------------------------------------------------------------


def test_revoke_token_is_immediate_and_idempotent(client: Any, clock: Any, sql: Any) -> None:
    """Revogação: `revoked_at == iso(clock)`; o uso seguinte dá 401;
    revogar de novo mantém o horário e não gera outro `token.revoked`; os outros tokens seguem."""
    secret, created = create_token(client, ADMIN_TOKEN, ["people:read", "tokens:manage"], label="token a revogar")
    other_secret, other = create_token(client, ADMIN_TOKEN, ["people:read"], label="token que continua")
    assert client.get("/v1/people", headers=auth(secret)).status_code == 200

    revoked_at = clock.advance(90)
    resp = _revoke(client, ADMIN_TOKEN, created["token_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_matches("tokens.revoke", 200, body)
    expected = {**_public(created), "revoked_at": iso(revoked_at)}
    assert body == expected
    assert find_secrets(resp.text, all_secrets()) == []

    assert_error(client.get("/v1/people", headers=auth(secret)), 401, "unauthenticated")
    assert_error(client.get("/v1/tokens", headers=auth(secret)), 401, "unauthenticated")
    resp = client.get("/v1/people", headers=auth(other_secret))
    assert resp.status_code == 200, resp.text

    clock.advance(3600)
    resp = _revoke(client, ADMIN_TOKEN, created["token_id"])
    assert resp.status_code == 200, resp.text
    assert_matches("tokens.revoke", 200, resp.json())
    assert resp.json() == expected

    rows = sql.execute("SELECT token_id::text, revoked_at FROM api_tokens ORDER BY id").fetchall()
    assert rows == [(created["token_id"], revoked_at), (other["token_id"], None)]

    resp = client.get("/v1/tokens", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [expected, _public(other)]}

    revoked_events = all_events(client, ADMIN_TOKEN, "token.revoked")
    assert [_without_seq(e) for e in revoked_events] == [{
        "type": "token.revoked",
        "occurred_at": iso(revoked_at),
        "actor_token_id": "bootstrap",
        "data": {"token_id": created["token_id"]},
    }]


def test_revoke_unknown_token_is_not_found(client: Any, sql: Any) -> None:
    """Revogação: um `token_id` válido que não existe → 404, sem eco
    e sem mudança."""
    _, existing = create_token(client, ADMIN_TOKEN, ["people:read"], label="token existente")
    unknown = str(uuid.uuid4())
    before = _db_state(sql)
    resp = _revoke(client, ADMIN_TOKEN, unknown)
    assert_error(resp, 404, "not_found")
    assert_no_echo(resp, unknown)
    assert _db_state(sql) == before
    assert before["tokens"][0][0] == existing["token_id"] and before["tokens"][0][3] is None


MALFORMED_TOKEN_IDS: dict[str, Callable[[str], str]] = {
    "bootstrap": lambda existing: "bootstrap",
    "short": lambda existing: "abc",
    "uppercase_of_existing": lambda existing: existing.upper(),
    "hex_without_hyphens_of_existing": lambda existing: uuid.UUID(existing).hex,
    "canonical_uuid_not_v4": lambda existing: "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
}


@pytest.mark.parametrize("case", list(MALFORMED_TOKEN_IDS))
def test_revoke_rejects_malformed_token_id(client: Any, sql: Any, case: str) -> None:
    """`token_id` fora do UUID canônico minúsculo → `[{token_id, invalid_format}]`,
    sem eco e sem revogar nada (nem o token cujo id foi reescrito)."""
    _, existing = create_token(client, ADMIN_TOKEN, ["people:read"], label="token existente")
    value = MALFORMED_TOKEN_IDS[case](existing["token_id"])
    before = _db_state(sql)
    resp = _revoke(client, ADMIN_TOKEN, value)
    assert_error(resp, 400, "invalid_request", fields=[("token_id", "invalid_format")])
    if len(value) >= 8:
        assert_no_echo(resp, value)
    assert _db_state(sql) == before


# ---------------------------------------------------------------------------------------------
# Matriz de 401
# ---------------------------------------------------------------------------------------------


def _auth_missing(client: Any) -> tuple[dict, list[str]]:
    return {}, []


def _auth_basic(client: Any) -> tuple[dict, list[str]]:
    # O próprio token inicial, mas no esquema errado.
    return {"Authorization": f"Basic {ADMIN_TOKEN}"}, [ADMIN_TOKEN]


def _auth_bearer_empty(client: Any) -> tuple[dict, list[str]]:
    return {"Authorization": "Bearer "}, []


def _auth_bearer_without_value(client: Any) -> tuple[dict, list[str]]:
    return {"Authorization": "Bearer"}, []


def _auth_unknown(client: Any) -> tuple[dict, list[str]]:
    unknown = "cq_" + stdlib_secrets.token_urlsafe(32)
    return auth(unknown), [unknown]


def _auth_digest_of_valid(client: Any) -> tuple[dict, list[str]]:
    # O resumo guardado no banco não serve como segredo.
    secret, _ = create_token(client, ADMIN_TOKEN, ALL_PERMISSIONS, label="token cujo resumo e enviado")
    digest = _sha256(secret)
    return auth(digest), [digest, secret]


def _auth_revoked(client: Any) -> tuple[dict, list[str]]:
    secret, body = create_token(client, ADMIN_TOKEN, ALL_PERMISSIONS, label="token revogado")
    resp = _revoke(client, ADMIN_TOKEN, body["token_id"])
    assert resp.status_code == 200, resp.text
    return auth(secret), [secret]


def _auth_non_ascii(client: Any) -> tuple[dict, list[str]]:
    raw = "cq_" + "ç" * 43
    encoded = raw.encode("utf-8")
    return {"Authorization": b"Bearer " + encoded}, [raw, encoded.decode("latin-1")]


AUTH_CASES: dict[str, Callable[[Any], tuple[dict, list[str]]]] = {
    "missing": _auth_missing,
    "basic": _auth_basic,
    "bearer_empty": _auth_bearer_empty,
    "bearer_without_value": _auth_bearer_without_value,
    "unknown": _auth_unknown,
    "digest_of_valid": _auth_digest_of_valid,
    "revoked": _auth_revoked,
    "non_ascii": _auth_non_ascii,
}


@pytest.mark.parametrize("case", list(AUTH_CASES))
@pytest.mark.parametrize("op_id", V1_OPS)
def test_unauthenticated_matrix(client: Any, sql: Any, op_id: str, case: str) -> None:
    """em toda operação /v1, sem cabeçalho, `Basic`, `Bearer` vazio,
    desconhecido, resumo, revogado ou com byte não ASCII → 401 exato (nunca 500), sem eco do
    token e sem efeito no banco. Oferecimentos e bots: também as 10 operações dessa parte, sem efeito nas tabelas
    dessa parte nem nas filas. Vínculo do aluno: também as 5 operações dessa parte, sem
    efeito em `invite_sends` nem nas colunas do vínculo. Relatórios e acompanhamento: também as 5
    operações dessa parte, sem efeito nas suas tabelas (`REPORT_STATE_TABLES`)."""
    state = seed_state(client, revoke_targets=1)
    headers, sentinels = AUTH_CASES[case](client)
    before = _db_state(sql)
    resp = VALID_CALLS[op_id](client, state, headers)
    assert_error(resp, 401, "unauthenticated")
    if sentinels:
        assert_no_echo(resp, *sentinels)
    assert find_secrets(resp.text, all_secrets()) == []
    assert _db_state(sql) == before


# ---------------------------------------------------------------------------------------------
# Matriz de 403
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", PROTECTED_OPS)
def test_forbidden_matrix_names_required_permission(client: Any, sql: Any, op_id: str) -> None:
    """Um token com todas as outras permissões → 403 nomeando a
    exigida, sem efeito (só a revelação negada vira evento); o token inicial e um token com todas as
    permissões nunca recebem 403. Oferecimentos e bots: também as 10 operações
    dessa parte, com `offerings:read`, `offerings:write` ou `bots:manage` (as outras 7 não bastam). Vínculo do aluno:
    também as 5 operações dessa parte, com `people:write` (`students.import`),
    `offerings:read` (`students.summary`) ou `offerings:write` (os três de convite), sem permissão
    própria e sem efeito em `invite_sends`. Relatórios e acompanhamento: também as 5
    operações dessa parte, com `offerings:read` nas leituras e `offerings:write` no reenvio e na alteração
    (`REPORT_PERMISSIONS`), sem permissão própria e sem efeito nas suas tabelas (`REPORT_STATE_TABLES`). CLI:
    também as 3 operações dessa parte, com `offerings:read` no `doctor` e
    `people:write` no expurgo (`DOCTOR_PURGE_PERMISSIONS`; `offerings:write` não basta), sem permissão própria e sem efeito
    (`_db_state`)."""
    assert set(PROTECTED_OPS) == set(V1_OPS)
    required = CATALOG_OPS[op_id]["permission"]
    assert set(REPORT_PERMISSIONS) <= set(PROTECTED_OPS)
    assert required == REPORT_PERMISSIONS.get(op_id, required)
    assert set(DOCTOR_PURGE_PERMISSIONS) <= set(PROTECTED_OPS)
    assert required == {**REPORT_PERMISSIONS, **DOCTOR_PURGE_PERMISSIONS}.get(op_id, required)
    state = seed_state(client, revoke_targets=3)
    others = [p for p in ALL_PERMISSIONS if p != required]
    assert len(others) == 7
    limited, _ = create_token(client, ADMIN_TOKEN, others, label="todas menos a exigida")
    full, _ = create_token(client, ADMIN_TOKEN, ALL_PERMISSIONS, label="todas as permissoes")

    before = _db_state(sql)
    resp = VALID_CALLS[op_id](client, state, auth(limited))
    assert_error(resp, 403, "forbidden", permission=required)
    after = _db_state(sql)
    assert {k: v for k, v in after.items() if k != "events"} == {k: v for k, v in before.items() if k != "events"}
    assert after["events"][:len(before["events"])] == before["events"]
    assert [t for _, t in after["events"][len(before["events"]):]] == DENIAL_EVENTS.get(op_id, [])

    for label, token in (("token inicial", ADMIN_TOKEN), ("token com todas as permissões", full)):
        with _context(f"{op_id} com {label}"):
            resp = VALID_CALLS[op_id](client, state, auth(token))
            assert resp.status_code == SUCCESS_STATUS[op_id], resp.text
            assert_matches(op_id, resp.status_code, resp.json())


# ---------------------------------------------------------------------------------------------
# Sem token inicial
# ---------------------------------------------------------------------------------------------


def test_without_bootstrap_token_only_database_tokens_work(client: Any, app_factory: Any, sql: Any) -> None:
    """sem `CQ_ADMIN_TOKEN`, o antigo token inicial dá 401 em toda operação /v1, e os tokens
    do banco continuam funcionando (e aparecem como autores dos eventos). Oferecimentos e bots: também nas 10
    operações dessa parte, com um token do banco com as 8 permissões. Vínculo do aluno: e nas 5 operações
    dessa parte (200 em todas). Relatórios: e nas 5 operações dessa parte (200 em todas)."""
    state = seed_state(client, revoke_targets=2)
    full_secret, full = create_token(client, ADMIN_TOKEN, ALL_PERMISSIONS, label="token completo do banco")

    app = app_factory(admin_token=None)
    with TestClient(app, raise_server_exceptions=False) as no_bootstrap:
        created_token_id = None
        for op_id in V1_OPS:
            with _context(f"{op_id} com o antigo token inicial"):
                before = _db_state(sql)
                resp = VALID_CALLS[op_id](no_bootstrap, state, auth(ADMIN_TOKEN))
                assert_error(resp, 401, "unauthenticated")
                assert_no_echo(resp, ADMIN_TOKEN)
                assert _db_state(sql) == before
            with _context(f"{op_id} com o token do banco"):
                resp = VALID_CALLS[op_id](no_bootstrap, state, auth(full_secret))
                assert resp.status_code == SUCCESS_STATUS[op_id], resp.text
                assert_matches(op_id, resp.status_code, resp.json())
                if op_id == "tokens.create":
                    created_token_id = resp.json()["token_id"]

    assert created_token_id is not None
    created_events = [e for e in all_events(client, ADMIN_TOKEN, "token.created")
                      if e["data"]["token_id"] == created_token_id]
    assert [e["actor_token_id"] for e in created_events] == [full["token_id"]]


# ---------------------------------------------------------------------------------------------
# Escalada
# ---------------------------------------------------------------------------------------------

ACTOR_PERMISSIONS = ["tokens:manage", "people:read"]


@pytest.mark.parametrize("requested, missing", [
    pytest.param(["identity:reveal"], "identity:reveal", id="identity_reveal"),
    pytest.param(["identity:reveal", "events:read"], "events:read", id="first_missing_alphabetically"),
    pytest.param(["people:write", "identity:reveal", "people:read"], "identity:reveal",
                 id="first_missing_not_request_order"),
])
def test_create_token_rejects_escalation(client: Any, sql: Any, requested: list[str], missing: str) -> None:
    """um token só concede permissões que tem; o 403 nomeia a primeira,
    em ordem alfabética, das que faltam; nenhum token e nenhum evento."""
    actor_secret, _ = create_token(client, ADMIN_TOKEN, ACTOR_PERMISSIONS, label="gestor limitado")
    before = _db_state(sql)
    resp = _post_token(client, actor_secret, "token pedido", requested)
    assert_error(resp, 403, "forbidden", permission=missing)
    assert find_secrets(resp.text, all_secrets()) == []
    assert _db_state(sql) == before


@pytest.mark.parametrize("requested", [
    pytest.param(["people:read"], id="people_read"),
    pytest.param(["people:read", "tokens:manage"], id="all_held"),
])
def test_create_token_grants_subset_of_actor(client: Any, clock: Any, sql: Any, requested: list[str]) -> None:
    """um subconjunto das permissões do token que cria → 201 exato, com o evento
    `token.created` atribuído a esse token."""
    actor_secret, actor = create_token(client, ADMIN_TOKEN, ACTOR_PERMISSIONS, label="gestor limitado")
    clock.advance(5)
    resp = _post_token(client, actor_secret, "token pedido", requested)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert_matches("tokens.create", 201, body)
    remember_secret(body["secret"])
    assert body == {"token_id": body["token_id"], "label": "token pedido", "permissions": sorted(requested),
                    "created_at": iso(clock.now()), "revoked_at": None, "secret": body["secret"]}
    events = [e for e in all_events(client, ADMIN_TOKEN, "token.created") if e["data"]["token_id"] == body["token_id"]]
    assert [_without_seq(e) for e in events] == [{
        "type": "token.created",
        "occurred_at": iso(clock.now()),
        "actor_token_id": actor["token_id"],
        "data": {"token_id": body["token_id"], "label": "token pedido", "permissions": sorted(requested)},
    }]


# ---------------------------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------------------------

JSON_HEADERS = {"Content-Type": "application/json"}

VALIDATION_CASES = [
    # label
    pytest.param({"json": {"permissions": ["people:read"]}}, [("label", "missing")], [], id="label_missing"),
    pytest.param({"json": {"label": None, "permissions": ["people:read"]}}, [("label", "missing")], [],
                 id="label_null"),
    pytest.param({"json": {"label": "", "permissions": ["people:read"]}}, [("label", "too_short")], [],
                 id="label_empty"),
    pytest.param({"json": {"label": "      ", "permissions": ["people:read"]}}, [("label", "too_short")], [],
                 id="label_only_spaces"),
    pytest.param({"json": {"label": LABEL_101, "permissions": ["people:read"]}}, [("label", "too_long")],
                 [LABEL_101], id="label_101_chars"),
    pytest.param({"json": {"label": 7, "permissions": ["people:read"]}}, [("label", "invalid_format")], [],
                 id="label_not_string"),
    # permissions
    pytest.param({"json": {"label": "rotulo valido"}}, [("permissions", "missing")], [],
                 id="permissions_missing"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": None}}, [("permissions", "missing")], [],
                 id="permissions_null"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": []}}, [("permissions", "too_few")], [],
                 id="permissions_empty"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": [*ALL_PERMISSIONS, "permissao:sobrando"]}},
                 [("permissions", "too_many")], ["permissao:sobrando"], id="permissions_nine_items"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": ["people:read"] * 9}},
                 [("permissions", "too_many")], [], id="permissions_nine_repeated_items"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": "x"}}, [("permissions", "invalid_format")],
                 [], id="permissions_string"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": [7]}},
                 [("permissions[0]", "invalid_format")], [], id="permission_item_not_string"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": ["people:read", "permissao:inexistente"]}},
                 [("permissions[1]", "invalid_value")], ["permissao:inexistente"], id="permission_unknown"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": ["PEOPLE:READ"]}},
                 [("permissions[0]", "invalid_value")], [], id="permission_wrong_case"),
    pytest.param({"json": {"label": "rotulo valido",
                           "permissions": ["permissao:falsa-um", "people:read", "permissao:falsa-dois"]}},
                 [("permissions[0]", "invalid_value"), ("permissions[2]", "invalid_value")],
                 ["permissao:falsa-um", "permissao:falsa-dois"], id="permissions_two_unknown"),
    pytest.param({"json": {"label": "rotulo valido", "permissions": ["people:read", "events:read", "people:read"]}},
                 [("permissions[2]", "duplicate")], [], id="permission_duplicate"),
    # campo extra, vários erros e ordem
    pytest.param({"json": {"label": "rotulo valido", "permissions": ["people:read"],
                           "campo_extra_sentinela": "valor-extra-sentinela"}},
                 [("body", "unknown_field")], ["campo_extra_sentinela", "valor-extra-sentinela"], id="extra_field"),
    pytest.param({"json": {"permissions": []}}, [("label", "missing"), ("permissions", "too_few")], [],
                 id="two_errors_in_schema_order"),
    pytest.param({"json": {"campo_extra_sentinela": 1, "label": 7, "permissions": "x"}},
                 [("label", "invalid_format"), ("permissions", "invalid_format"), ("body", "unknown_field")],
                 ["campo_extra_sentinela"], id="unknown_field_last"),
    pytest.param({"json": {"label": LABEL_101, "permissions": ["people:read", "permissao:inexistente"]}},
                 [("label", "too_long"), ("permissions[1]", "invalid_value")],
                 [LABEL_101, "permissao:inexistente"], id="label_and_permission_errors"),
    # corpo
    pytest.param({"content": b"", "headers": JSON_HEADERS}, [("body", "missing")], [], id="body_empty"),
    pytest.param({"content": b"isto-nao-e-json-sentinela", "headers": JSON_HEADERS},
                 [("body", "invalid_format")], ["isto-nao-e-json-sentinela"], id="body_not_json"),
    pytest.param({"json": ["people:read"]}, [("body", "invalid_format")], [], id="body_is_list"),
    # JSON que não é UTF-8: o de um cliente em latin-1/cp1252 e uma sequência inválida.
    pytest.param({"content": '{"label": "rótulo sentinela-latina", "permissions": ["people:read"]}'.encode("latin-1"),
                  "headers": JSON_HEADERS},
                 [("body", "invalid_format")], ["rótulo sentinela-latina", "sentinela-latina"], id="body_latin1"),
    pytest.param({"content": b"\xc3\x28", "headers": JSON_HEADERS}, [("body", "invalid_format")], [],
                 id="body_invalid_utf8"),
]


@pytest.mark.parametrize("request_kwargs, fields, sentinels", VALIDATION_CASES)
def test_create_token_validation(client: Any, sql: Any, request_kwargs: dict, fields: list,
                                 sentinels: list[str]) -> None:
    """`label` (missing/too_short/too_long/invalid_format),
    `permissions` (missing/too_few/too_many/invalid_format/invalid_value/duplicate), campo extra,
    precedência e ordem dos erros → 400 exato, sem eco, sem token e sem evento. Com 8
    permissões, `too_many` é a partir de 9 itens."""
    kwargs = dict(request_kwargs)
    headers = {**auth(ADMIN_TOKEN), **kwargs.pop("headers", {})}
    before = _db_state(sql)
    resp = client.post("/v1/tokens", headers=headers, **kwargs)
    assert_error(resp, 400, "invalid_request", fields=fields)
    if sentinels:
        assert_no_echo(resp, *sentinels)
    assert _db_state(sql) == before


def test_create_token_accepts_boundaries(client: Any, clock: Any, sql: Any) -> None:
    """rótulo com 100 caracteres (também com espaços nas pontas, medido depois
    de aparar), uma permissão e todas (as 8) → 201 exato."""
    cases = [
        (LABEL_100, ["people:read"], LABEL_100),
        ("   " + LABEL_100 + "   ", ["events:read"], LABEL_100),
        ("r", list(reversed(ALL_PERMISSIONS)), "r"),
    ]
    for sent_label, permissions, stored_label in cases:
        with _context(f"label={len(sent_label)} caracteres, permissions={permissions}"):
            clock.advance(1)
            resp = _post_token(client, ADMIN_TOKEN, sent_label, permissions)
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert_matches("tokens.create", 201, body)
            remember_secret(body["secret"])
            assert body == {"token_id": body["token_id"], "label": stored_label,
                            "permissions": sorted(permissions), "created_at": iso(clock.now()),
                            "revoked_at": None, "secret": body["secret"]}
            stored = sql.execute("SELECT label FROM api_tokens WHERE token_id = %s",
                                 (body["token_id"],)).fetchone()[0]
            assert stored == stored_label


# ---------------------------------------------------------------------------------------------
# Ordem das verificações
# ---------------------------------------------------------------------------------------------


def test_check_order_authentication_validation_permission(client: Any, sql: Any) -> None:
    """Ordem 401 → 400 → 403 → 404: corpo inválido sem `tokens:manage` → 400;
    sem token → 401; validação antes da checagem de subconjunto; na revogação, 400 antes de 403 e
    403 antes de 404."""
    reader_secret, reader = create_token(client, ADMIN_TOKEN, ["people:read"], label="leitor sem gestao")
    actor_secret, _ = create_token(client, ADMIN_TOKEN, ACTOR_PERMISSIONS, label="gestor limitado")
    before = _db_state(sql)

    resp = _post_token(client, reader_secret, "   ", [])
    assert_error(resp, 400, "invalid_request", fields=[("label", "too_short"), ("permissions", "too_few")])

    resp = client.post("/v1/tokens", json={"label": "   ", "permissions": []})
    assert_error(resp, 401, "unauthenticated")

    resp = _post_token(client, reader_secret, "rotulo valido", ["people:read"])
    assert_error(resp, 403, "forbidden", permission="tokens:manage")

    resp = _post_token(client, actor_secret, "rotulo valido", ["identity:reveal", "permissao:inexistente"])
    assert_error(resp, 400, "invalid_request", fields=[("permissions[1]", "invalid_value")])
    assert_no_echo(resp, "permissao:inexistente")

    resp = _revoke(client, reader_secret, "bootstrap")
    assert_error(resp, 400, "invalid_request", fields=[("token_id", "invalid_format")])

    resp = client.post("/v1/tokens/bootstrap/revoke")
    assert_error(resp, 401, "unauthenticated")

    resp = _revoke(client, reader_secret, str(uuid.uuid4()))
    assert_error(resp, 403, "forbidden", permission="tokens:manage")

    resp = _revoke(client, reader_secret, reader["token_id"])
    assert_error(resp, 403, "forbidden", permission="tokens:manage")

    assert _db_state(sql) == before


# ---------------------------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------------------------


def test_token_events_are_exact(client: Any, clock: Any, sql: Any) -> None:
    """`token.created` (rótulo aparado, permissões em ordem
    alfabética) e `token.revoked` exatos, com `actor_token_id` (`bootstrap` ou o `token_id` do autor)
    e `occurred_at`; revogar de novo não gera evento; nada de segredo nos eventos."""
    t0 = clock.now()
    resp = _post_token(client, ADMIN_TOKEN, "  gestor de tokens  ", ["tokens:manage", "people:read"])
    assert resp.status_code == 201, resp.text
    manager = resp.json()
    assert_matches("tokens.create", 201, manager)
    remember_secret(manager["secret"])

    t1 = clock.advance(10)
    _, reader = create_token(client, manager["secret"], ["people:read"], label="leitor criado por token")

    t2 = clock.advance(10)
    resp = _revoke(client, manager["secret"], reader["token_id"])
    assert resp.status_code == 200, resp.text

    t3 = clock.advance(10)
    resp = _revoke(client, ADMIN_TOKEN, manager["token_id"])
    assert resp.status_code == 200, resp.text
    clock.advance(10)
    resp = _revoke(client, ADMIN_TOKEN, manager["token_id"])
    assert resp.status_code == 200, resp.text

    expected = [
        {"type": "token.created", "occurred_at": iso(t0), "actor_token_id": "bootstrap",
         "data": {"token_id": manager["token_id"], "label": "gestor de tokens",
                  "permissions": ["people:read", "tokens:manage"]}},
        {"type": "token.created", "occurred_at": iso(t1), "actor_token_id": manager["token_id"],
         "data": {"token_id": reader["token_id"], "label": "leitor criado por token",
                  "permissions": ["people:read"]}},
        {"type": "token.revoked", "occurred_at": iso(t2), "actor_token_id": manager["token_id"],
         "data": {"token_id": reader["token_id"]}},
        {"type": "token.revoked", "occurred_at": iso(t3), "actor_token_id": "bootstrap",
         "data": {"token_id": manager["token_id"]}},
    ]
    events = all_events(client, ADMIN_TOKEN, "token.created", "token.revoked")
    assert [_without_seq(e) for e in events] == expected
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in zip(seqs, seqs[1:]))
    assert find_secrets(json.dumps(events, ensure_ascii=False), all_secrets()) == []

    rows = sql.execute("SELECT type, occurred_at, actor_token_id, data FROM events ORDER BY seq").fetchall()
    assert rows == [(e["type"], t, e["actor_token_id"], e["data"]) for e, t in zip(expected, (t0, t1, t2, t3))]
    assert find_secrets(_table_text(sql, "events"), all_secrets()) == []
