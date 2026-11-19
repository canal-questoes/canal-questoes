"""Registro do token do bot pela API.

Cobre o registro e a troca do token; o token do bot por argumento
(a API só recebe o token no corpo), o token em lugar errado e a chave-mestra trocada.


Convenções: todo contato com o Telegram é com a Bot API falsa, e as chamadas são
conferidas no registro do dublê (`bot_api.calls()`); horários pelo `FakeClock` e `iso`; ids internos,
`seq` e cursores só por relação; toda resposta de sucesso passa por `assert_matches`; nenhuma
resposta, erro, log, evento ou coluna do banco contém o token, e cada varredura tem controle
positivo. A decifragem do token gravado é feita aqui, de forma independente, com os primitivos da
`cryptography` e a chave de teste.
"""

from __future__ import annotations

import base64
import dataclasses
import itertools
import json
import logging
import time
import urllib.parse
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from cq_server.notices import ConfigNotice, NoticeListener
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_token,
    register_bot,
    uuid4_with_prefix,
)
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import capture_logs, capture_transport_logs
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY, remember_secret

ADMIN = auth(ADMIN_TOKEN)

# Tipos de evento de bot, escritos como texto (os enums só mudam na implementação).
BOT_EVENT_TYPES = ("bot.registered", "bot.token_replaced", "bot.access_changed", "bot.lease_changed",
                   "bot.profile_updated")

# Mensagens fixas de `problem`.
PROBLEM_MESSAGES = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "token_unreadable": "O token guardado não pode ser lido com a chave-mestra atual. Registre o token "
                        "de novo.",
}

# Todas as permissões menos `bots:manage`.
WITHOUT_BOTS_MANAGE = ["events:read", "identity:reveal", "offerings:read", "offerings:write",
                       "people:read", "people:write", "tokens:manage"]

REGISTER_ROUTE = "/v1/offerings/{ref}/bot"

# Alfabeto da parte secreta do token; `_secret(n)` dá n caracteres válidos.
_SECRET_CHARS = "Ab0_-Zy9"


def _secret(n: int) -> str:
    return (_SECRET_CHARS * (n // len(_SECRET_CHARS) + 1))[:n]


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _path(ref: str, suffix: str = "/bot") -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}{suffix}"


def _put_bot(client: Any, ref: str, body: Any, headers: dict[str, str] = ADMIN, **kwargs: Any) -> Any:
    return client.put(_path(ref), json=body, headers=headers, **kwargs)


def _get_ok(client: Any, op_id: str, path: str) -> dict:
    resp = client.get(path, headers=ADMIN)
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _get_bot(client: Any, ref: str) -> dict:
    return _get_ok(client, "bots.get", _path(ref))


def _get_offering(client: Any, ref: str) -> dict:
    return _get_ok(client, "offerings.get", _path(ref, ""))


def _list_offerings(client: Any) -> dict:
    return _get_ok(client, "offerings.list", "/v1/offerings")


def _post_ok(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _archive(client: Any, ref: str) -> dict:
    """Arquiva em duas fases, a partir de `draft` ou `closed`."""
    prepared = _post_ok(client, "offerings.prepare_archive", _path(ref, "/archive/prepare"))
    archived = _post_ok(client, "offerings.archive", _path(ref, "/archive"),
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived"
    return archived


def _close(client: Any, ref: str) -> dict:
    closed = _post_ok(client, "offerings.close", _path(ref, "/close"))
    assert closed["state"] == "closed"
    return closed


def _bot_body(offering: dict, telegram_bot_id: int, username: str, *, registered_at: str,
              token_replaced_at: str | None = None, state: str = "registered",
              profile_state: str = "pending", lease: dict | None = None,
              last_poll_ok_at: str | None = None, problem: str | None = None) -> dict:
    """O `Bot` exato do contrato."""
    return {
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
        "username": username,
        "telegram_bot_id": telegram_bot_id,
        "state": state,
        "profile_state": profile_state,
        "registered_at": registered_at,
        "token_replaced_at": token_replaced_at,
        "lease": lease,
        "last_poll_ok_at": last_poll_ok_at,
        "problem": None if problem is None else {"code": problem, "message": PROBLEM_MESSAGES[problem]},
    }


def _summary(telegram_bot_id: int, username: str, *, state: str = "registered",
             profile_state: str = "pending") -> dict:
    """O resumo `bot` do `Offering`."""
    return {"username": username, "telegram_bot_id": telegram_bot_id, "state": state,
            "profile_state": profile_state}


def _strip_seq(events: list[dict]) -> list[dict]:
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _bot_events(client: Any) -> list[dict]:
    return _strip_seq(all_events(client, ADMIN_TOKEN, *BOT_EVENT_TYPES))


def _event(event_type: str, at: str, data: dict, actor: str | None = "bootstrap") -> dict:
    return {"type": event_type, "occurred_at": at, "actor_token_id": actor, "data": data}


def _internal_bot_id(sql: psycopg.Connection, telegram_bot_id: int) -> int:
    rows = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (telegram_bot_id,)).fetchall()
    assert len(rows) == 1, f"esperava um bot com telegram_bot_id {telegram_bot_id}, há {len(rows)}"
    return int(rows[0][0])


_BOT_COLUMNS = ("bot_uuid", "offering_id", "telegram_bot_id", "username", "token_ciphertext",
                "token_version", "state", "access_problem", "profile_state", "lease_owner",
                "lease_until", "last_poll_ok_at", "last_problem", "last_problem_at", "update_epoch",
                "last_update_at", "registered_at", "token_replaced_at")


def _bot_row(sql: psycopg.Connection, telegram_bot_id: int) -> dict:
    rows = sql.execute(f"SELECT {', '.join(_BOT_COLUMNS)} FROM bots WHERE telegram_bot_id = %s",
                       (telegram_bot_id,)).fetchall()
    assert len(rows) == 1, f"esperava um bot com telegram_bot_id {telegram_bot_id}, há {len(rows)}"
    row = dict(zip(_BOT_COLUMNS, rows[0], strict=True))
    row["token_ciphertext"] = bytes(row["token_ciphertext"])
    return row


def _offering_internal_id(sql: psycopg.Connection, offering: dict) -> int:
    rows = sql.execute("SELECT id FROM offerings WHERE offering_id::text = %s",
                       (offering["offering_id"],)).fetchall()
    assert len(rows) == 1
    return int(rows[0][0])


def _sync_tasks(sql: psycopg.Connection) -> list[tuple[Any, ...]]:
    """As tarefas `bot.sync_profile` (payload, chave de idempotência, estado, tentativas), por id."""
    return [tuple(row) for row in sql.execute(
        "SELECT payload, idempotency_key, state, attempts FROM tasks "
        "WHERE task_type = 'bot.sync_profile' ORDER BY id").fetchall()]


def _db_state(sql: psycopg.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Retrato do que o registro pode gravar: bots, eventos, tarefas e oferecimentos."""
    return {
        "bots": [tuple(r) for r in sql.execute(
            "SELECT id, bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, "
            "token_version, state, access_problem, profile_state, lease_owner, lease_until, "
            "registered_at, token_replaced_at FROM bots ORDER BY id").fetchall()],
        "events": [tuple(r) for r in sql.execute(
            "SELECT seq, type, data::text FROM events ORDER BY seq").fetchall()],
        "tasks": [tuple(r) for r in sql.execute(
            "SELECT id, task_type, idempotency_key, state FROM tasks ORDER BY id").fetchall()],
        "offerings": [tuple(r) for r in sql.execute(
            "SELECT id, state, activated_at, closed_at, archived_at FROM offerings ORDER BY id").fetchall()],
    }


def _hkdf(ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(ikm)


def _key_id(master_key: str) -> bytes:
    """`key_id` do token cifrado: HKDF-SHA256 da chave decodificada, `info=b"cq/master-key-id/v1"`."""
    return _hkdf(base64.urlsafe_b64decode(master_key + "="), b"cq/master-key-id/v1", 4)


def _decrypt(blob: bytes, *, bot_uuid: uuid.UUID, telegram_bot_id: int, master_key: str = MASTER_KEY) -> str:
    """Decifragem independente do formato v1, sem usar `cq_server.secretbox`."""
    ikm = base64.urlsafe_b64decode(master_key + "=")
    assert len(ikm) == 32
    assert blob[0] == 1, "versão do blob diferente de 1"
    assert blob[1:5] == _key_id(master_key), "key_id do blob não é o da chave informada"
    key = _hkdf(ikm, b"cq/bot-token/aes-256-gcm/v1", 32)
    aad = (b"cq/bot-token/v1\x00" + blob[0:5] + bot_uuid.bytes
           + telegram_bot_id.to_bytes(8, "big", signed=True))
    return AESGCM(key).decrypt(blob[5:17], blob[17:], aad).decode("ascii")


def _collect(listener: NoticeListener, want: int, within: float) -> list[ConfigNotice]:
    """Soma chamadas de `wait` até ter `want` avisos ou passar `within` segundos (o aviso é
    assíncrono; espera com prazo, como em `test_notices.py`)."""
    received: list[ConfigNotice] = []
    deadline = time.monotonic() + within
    while len(received) < want:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        received.extend(listener.wait(remaining))
    return received


def _response_text(resp: Any) -> str:
    return resp.text + "\n" + "\n".join(f"{k}: {v}" for k, v in resp.headers.items())


def _token_forms(*tokens: str) -> list[str]:
    """Os tokens inteiros e as partes secretas (o que a varredura procura)."""
    forms: list[str] = []
    for token in tokens:
        forms.append(token)
        if ":" in token:
            forms.append(token.split(":", 1)[1])
    return forms


def _plain_token_hits(scan: list[tuple[str, str, str]], secrets: list[str]) -> list[tuple[str, str]]:
    return sorted({(table, column) for table, column, text in scan if find_secrets(text, secrets)})


def _getme_calls(bot_api: Any) -> list[tuple[Any, ...]]:
    return [(c.method, c.bot_id, c.token_state, c.token_version) for c in bot_api.calls()]


def _new_bot(bot_api: Any, **kwargs: Any) -> tuple[int, str, str]:
    bot = bot_api.add_bot(**kwargs)
    return bot, bot_api.token_of(bot), bot_api.state(bot).username


def _request_records(cap: Any) -> list[dict]:
    return [r for r in cap.records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]


# ---------------------------------------------------------------------------------------------
# Registro com sucesso
# ---------------------------------------------------------------------------------------------


def test_register_returns_exact_bot_and_offering_summary(client: Any, clock: Any, bot_api: Any,
                                                         sql: psycopg.Connection, db_url: str) -> None:
    """O 200
    traz o `Bot` exato (username e id do `getMe`, `registered`, perfil `pending`, `registered_at` do
    `FakeClock`, sem posse nem problema), igual ao de `bots.get` pelo código e pelo identificador; a
    única chamada ao Telegram é um `getMe` (POST JSON sem consulta, corpo `{}`); `offerings.get` e
    `offerings.list` passam a trazer o resumo `bot` exato; um único evento `bot.registered`
    exato; a tarefa `bot.sync_profile` com o payload e a chave do contrato; o
    aviso `bot` com o `offering_id` depois do commit; o registro `request` da rota; nenhuma saída
    contém o token."""
    offering = create_offering(client, ADMIN_TOKEN)
    clock.advance(60)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    assert find_secrets(json.dumps({"token": token}), _token_forms(token)) != [], "controle positivo"

    with NoticeListener(db_url) as listener:
        with capture_logs() as cap:
            resp = _put_bot(client, offering["code"], {"token": token})
        notices = _collect(listener, 1, 3)

    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("bots.register", 200, body)
    expected = _bot_body(offering, bot, username, registered_at=registered_at)
    assert body == expected

    calls = bot_api.calls()
    assert [(c.method, c.bot_id, c.token_state, c.token_version, c.params, c.http_method, c.has_query,
             c.outcome, c.clock_at) for c in calls] == [
        ("getMe", bot, "valid", 1, {}, "POST", False, "ok", clock.now())]

    for ref in (offering["code"], offering["offering_id"]):
        assert _get_bot(client, ref) == expected
        assert _get_offering(client, ref) == {**offering, "bot": _summary(bot, username)}
    assert _list_offerings(client) == {"items": [{**offering, "bot": _summary(bot, username)}],
                                       "next_cursor": _offering_internal_id(sql, offering)}

    assert _bot_events(client) == [_event("bot.registered", registered_at, {
        "offering_id": offering["offering_id"], "telegram_bot_id": bot, "username": username})]
    internal = _internal_bot_id(sql, bot)
    assert _sync_tasks(sql) == [({"bot_id": internal}, f"bot.sync_profile:{internal}:1:active", "pending", 0)]
    assert notices == [ConfigNotice(topic="bot", key=offering["offering_id"])]

    requests = _request_records(cap)
    assert len(requests) == 1, requests
    assert {"logger": "cq_server.api", "event": "request", "method": "PUT", "route": REGISTER_ROUTE,
            "status": 200}.items() <= requests[0].items()
    forms = _token_forms(token)
    assert find_secrets(_response_text(resp), forms) == []
    assert find_secrets(cap.text, forms) == []
    assert find_secrets(json.dumps(all_events(client, ADMIN_TOKEN, *BOT_EVENT_TYPES)), forms) == []


def test_register_stores_token_only_encrypted(client: Any, clock: Any, bot_api: Any,
                                              sql: psycopg.Connection, db_url: str) -> None:
    """Token só cifrado: a linha de `bots` tem os valores exatos do
    registro (UUID v4 próprio, `token_version` 1, `registered`, perfil `pending`, sem posse, época 0,
    `registered_at` do relógio); o blob tem o formato v1 (`0x01 ‖ key_id ‖ nonce ‖ cifrado ‖ tag`) e,
    decifrado aqui de forma independente com a chave de teste e o dado associado (bot_uuid e
    telegram_bot_id), dá o token; o `key_id` não é o de outra chave; a varredura de todas as colunas de
    todas as tabelas não acha o token nem a parte secreta, e acha numa tabela temporária (controle
    positivo)."""
    offering = create_offering(client, ADMIN_TOKEN)
    clock.advance(5)
    registered_at = clock.now()
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)

    row = _bot_row(sql, bot)
    bot_uuid = row["bot_uuid"]
    assert isinstance(bot_uuid, uuid.UUID) and bot_uuid.version == 4
    assert str(bot_uuid) != offering["offering_id"]
    blob = row["token_ciphertext"]
    assert {k: v for k, v in row.items() if k not in ("bot_uuid", "token_ciphertext")} == {
        "offering_id": _offering_internal_id(sql, offering), "telegram_bot_id": bot,
        "username": username, "token_version": 1, "state": "registered", "access_problem": None,
        "profile_state": "pending", "lease_owner": None, "lease_until": None, "last_poll_ok_at": None,
        "last_problem": None, "last_problem_at": None, "update_epoch": 0, "last_update_at": None,
        "registered_at": registered_at, "token_replaced_at": None}

    assert len(blob) == 1 + 4 + 12 + len(token) + 16
    assert blob[0] == 1 and blob[1:5] == _key_id(MASTER_KEY) != _key_id(OTHER_MASTER_KEY)
    assert _decrypt(blob, bot_uuid=bot_uuid, telegram_bot_id=bot) == token
    assert token.encode("ascii") not in blob

    forms = _token_forms(token)
    assert _plain_token_hits(scan_all_tables(db_url), forms) == []
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE cq_controle_token (valor text)")
        conn.execute("INSERT INTO cq_controle_token (valor) VALUES (%s)", (token,))
        assert _plain_token_hits(scan_all_tables(conn), forms) == [("cq_controle_token", "valor")]


# ---------------------------------------------------------------------------------------------
# Entrada inválida: recusada antes de qualquer chamada ao Telegram
# ---------------------------------------------------------------------------------------------

_RAW: Any = object()

FORMAT_CASES = [
    pytest.param({}, [("token", "missing")], id="sem-token"),
    pytest.param({"token": None}, [("token", "missing")], id="nulo"),
    pytest.param({"token": 1234567}, [("token", "invalid_format")], id="numero"),
    pytest.param({"token": True}, [("token", "invalid_format")], id="booleano"),
    pytest.param({"token": ["1234567:" + _secret(35)]}, [("token", "invalid_format")], id="lista"),
    pytest.param({"token": {"valor": "1234567:" + _secret(35)}}, [("token", "invalid_format")], id="objeto"),
    pytest.param({"token": ""}, [("token", "too_short")], id="vazio"),
    pytest.param({"token": "1234567:" + _secret(73)}, [("token", "too_long")], id="81-caracteres"),
    pytest.param({"token": "1234567:" + _secret(29)}, [("token", "invalid_format")], id="segredo-29"),
    pytest.param({"token": "0123456:" + _secret(30)}, [("token", "invalid_format")], id="prefixo-zero"),
    pytest.param({"token": "1" * 17 + ":" + _secret(30)}, [("token", "invalid_format")],
                 id="prefixo-17-digitos"),
    pytest.param({"token": "-1234567:" + _secret(30)}, [("token", "invalid_format")], id="prefixo-negativo"),
    pytest.param({"token": "1234567" + _secret(35)}, [("token", "invalid_format")], id="sem-dois-pontos"),
    pytest.param({"token": "1234567:" + _secret(30) + ":x"}, [("token", "invalid_format")],
                 id="dois-pontos-no-segredo"),
    pytest.param({"token": "1234567:" + _secret(29) + "+"}, [("token", "invalid_format")], id="mais"),
    pytest.param({"token": "1234567:" + _secret(29) + "="}, [("token", "invalid_format")], id="igual"),
    pytest.param({"token": "1234567:" + _secret(15) + " " + _secret(15)}, [("token", "invalid_format")],
                 id="espaco-interno"),
    pytest.param({"token": "1234567:" + _secret(15) + "\u0000" + _secret(15)}, [("token", "invalid_format")],
                 id="nul"),
    pytest.param({"token": "1234567:" + _secret(15) + "ç" + _secret(15)}, [("token", "invalid_format")],
                 id="acento"),
    pytest.param(_RAW, [("token", "invalid_format")], id="substituto-isolado"),
    pytest.param({"token": "1234567:" + _secret(35), "bot_token": "1234567:" + _secret(35)},
                 [("body", "unknown_field")], id="campo-extra"),
    pytest.param({"bot_token": "1234567:" + _secret(35)}, [("token", "missing"), ("body", "unknown_field")],
                 id="so-campo-extra"),
    pytest.param(["1234567:" + _secret(35)], [("body", "invalid_format")], id="corpo-lista"),
]


def _sentinels(value: Any) -> list[str]:
    """Valores (e chaves) enviados com 8+ caracteres, para `assert_no_echo`; de um texto com U+0000,
    o trecho antes dele; de um token, também a parte secreta."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _sentinels(key) + _sentinels(item)
    elif isinstance(value, list):
        for item in value:
            found += _sentinels(item)
    elif isinstance(value, str):
        for candidate in (value, value.split(":", 1)[-1]):
            candidate = candidate.split("\u0000", 1)[0]
            if len(candidate) >= 8:
                found.append(candidate)
    return list(dict.fromkeys(found))


def _token_values(value: Any) -> list[str]:
    """Os textos com a cara de token (com `:`) de um corpo, para `remember_secret`."""
    if isinstance(value, dict):
        return [t for item in value.values() for t in _token_values(item)]
    if isinstance(value, list):
        return [t for item in value for t in _token_values(item)]
    return [value] if isinstance(value, str) and ":" in value else []


@pytest.mark.parametrize(("body", "fields"), FORMAT_CASES)
def test_register_rejects_local_format_without_calling_telegram(body: Any, fields: list[tuple[str, str]],
                                                                client: Any, bot_api: Any,
                                                                sql: psycopg.Connection) -> None:
    """Entrada inválida pelas regras de validação da API (precedência por
    campo, `null` = ausente, campo desconhecido no fim); formato local (no máximo
    80 caracteres, `^[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}$`, inclusive a parte secreta de 29
    caracteres): 400 com os `fields` exatos, sem eco do valor, **nenhuma** chamada à Bot API falsa e
    nada gravado (bots, eventos, tarefas)."""
    offering = create_offering(client, ADMIN_TOKEN)
    for value in _token_values(body if body is not _RAW else {}):
        remember_secret(value)
    before = _db_state(sql)

    if body is _RAW:
        raw = ('{"token": "1234567:' + _secret(15) + '\\ud800' + _secret(15) + '"}').encode("ascii")
        resp = client.put(_path(offering["code"]), content=raw,
                          headers={**ADMIN, "Content-Type": "application/json"})
    else:
        resp = _put_bot(client, offering["code"], body)

    assert_error(resp, 400, "invalid_request", fields=fields)
    sentinels = _sentinels(body) if body is not _RAW else [_secret(15)]
    if sentinels:
        assert_no_echo(resp, *sentinels)
    assert bot_api.calls() == []
    assert _db_state(sql) == before
    assert _get_offering(client, offering["code"]) == offering


def test_register_empty_or_non_json_body(client: Any, bot_api: Any, sql: psycopg.Connection) -> None:
    """Regras de validação da API: corpo vazio → `[{body, missing}]`;
    corpo que não é JSON → `[{body, invalid_format}]`, sem eco; nenhuma chamada ao Telegram e nada
    gravado."""
    offering = create_offering(client, ADMIN_TOKEN)
    before = _db_state(sql)
    empty = client.put(_path(offering["code"]), headers=ADMIN)
    assert_error(empty, 400, "invalid_request", fields=[("body", "missing")])
    not_json = client.put(_path(offering["code"]), content=b"token=1234567:naoejsonsentinela",
                          headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(not_json, 400, "invalid_request", fields=[("body", "invalid_format")])
    assert_no_echo(not_json, "naoejsonsentinela")
    assert bot_api.calls() == []
    assert _db_state(sql) == before


# ---------------------------------------------------------------------------------------------
# Token recusado pelo Telegram, ou que não é de um bot
# ---------------------------------------------------------------------------------------------

REJECTED_CASES = ["desconhecido", "segredo-30", "80-caracteres", "revogado", "substituido", "421",
                  "nao-e-bot", "sem-username", "400", "403-generico", "404", "418"]


def _rejected_token(case: str, bot_api: Any) -> tuple[str, int, str]:
    """(token, bot_id registrado pelo dublê, `token_state` esperado na chamada) de cada caso."""
    if case == "desconhecido":
        return "1234567:" + _secret(35), 1234567, "unknown"
    if case == "segredo-30":
        return "7654321:" + _secret(30), 7654321, "unknown"
    if case == "80-caracteres":
        token = "1234567:" + _secret(72)
        assert len(token) == 80
        return token, 1234567, "unknown"
    if case == "nao-e-bot":
        bot = bot_api.add_bot(is_bot=False)
        return bot_api.token_of(bot), bot, "valid"
    if case == "sem-username":
        bot = bot_api.add_bot(username=None)
        return bot_api.token_of(bot), bot, "valid"
    bot = bot_api.add_bot()
    token = bot_api.token_of(bot)
    if case == "revogado":
        bot_api.revoke(bot)
        return token, bot, "revoked"
    if case == "substituido":
        bot_api.rotate_token(bot)
        return token, bot, "revoked"
    if case == "421":
        bot_api.fail("getMe", error="forbidden_token", bot_id=bot)
    elif case == "400":
        bot_api.fail("getMe", status=400, description="Bad Request: invalid request", bot_id=bot)
    elif case == "403-generico":
        bot_api.fail("getMe", error="kicked", bot_id=bot)
    elif case == "404":
        bot_api.fail("getMe", error="not_found", bot_id=bot)
    elif case == "418":
        bot_api.fail("getMe", status=418, description="I'm a teapot", bot_id=bot)
    else:  # pragma: no cover - caso novo sem preparo
        raise AssertionError(case)
    return token, bot, "valid"


@pytest.mark.parametrize("case", REJECTED_CASES)
def test_register_token_refused_by_telegram(case: str, client: Any, bot_api: Any,
                                            sql: psycopg.Connection) -> None:
    """Token recusado pelo `getMe` (`TokenRejected`, `NotABot` ou
    `RequestRejected` → `[{token, invalid_value}]`; 401 → `TokenRejected`; 421, outro 403,
    400, 404 e códigos desconhecidos → `RequestRejected`; `is_bot` falso ou sem `username` →
    `NotABot`): exatamente um `getMe`, 400 exato sem eco do token, nada gravado. Inclui as fronteiras
    do formato local que chegam ao Telegram (parte secreta de 30 e token de 80 caracteres)."""
    offering = create_offering(client, ADMIN_TOKEN)
    token, bot_id, token_state = _rejected_token(case, bot_api)
    remember_secret(token)
    before = _db_state(sql)

    resp = _put_bot(client, offering["code"], {"token": token})

    assert_error(resp, 400, "invalid_request", fields=[("token", "invalid_value")])
    assert_no_echo(resp, token, token.split(":", 1)[1])
    assert [(c.method, c.bot_id, c.token_state) for c in bot_api.calls()] == [("getMe", bot_id, token_state)]
    assert _db_state(sql) == before
    assert _get_offering(client, offering["code"]) == offering
    assert_error(client.get(_path(offering["code"]), headers=ADMIN), 404, "not_found")


# ---------------------------------------------------------------------------------------------
# Telegram indisponível
# ---------------------------------------------------------------------------------------------

UNAVAILABLE_CASES = ["429", "429-sem-retry-after", "500", "502-html", "conexao-cortada", "resposta-presa",
                     "409-webhook", "409-consumidor", "403-bloqueio", "403-desativado"]


def _program_unavailable(case: str, bot_api: Any, bot: int) -> None:
    if case == "429":
        bot_api.fail("getMe", retry_after=5, bot_id=bot)
    elif case == "429-sem-retry-after":
        bot_api.fail("getMe", status=429, bot_id=bot)
    elif case == "500":
        bot_api.fail("getMe", error="server_restart", bot_id=bot)
    elif case == "502-html":
        bot_api.fail_network("getMe", kind="not_json", bot_id=bot)
    elif case == "conexao-cortada":
        bot_api.fail_network("getMe", kind="reset", bot_id=bot)
    elif case == "resposta-presa":
        bot_api.fail_network("getMe", kind="hang", bot_id=bot)
    elif case == "409-webhook":
        bot_api.fail("getMe", error="conflict_webhook", bot_id=bot)
    elif case == "409-consumidor":
        bot_api.fail("getMe", error="conflict_poll", bot_id=bot)
    elif case == "403-bloqueio":
        bot_api.fail("getMe", error="blocked", bot_id=bot)
    elif case == "403-desativado":
        bot_api.fail("getMe", error="user_deactivated", bot_id=bot)
    else:  # pragma: no cover - caso novo sem preparo
        raise AssertionError(case)


@pytest.mark.parametrize("case", UNAVAILABLE_CASES)
def test_register_telegram_unavailable(case: str, client: Any, clock: Any, bot_api: Any,
                                       sql: psycopg.Connection) -> None:
    """Telegram indisponível (`RetryAfter`, `TelegramUnavailable`,
    `WebhookActive`, `ConcurrentConsumer` e `ChatUnreachable` no `getMe` → 503 `unavailable`) e a classificação
    (429 com e sem `retry_after`, 5xx, corpo HTML, conexão cortada, tempo esgotado, 409, 403 de
    bloqueio): exatamente um `getMe` (a API não repete), 503 exato sem eco, nada gravado; depois,
    com o Telegram de volta, o mesmo token é aceito."""
    offering = create_offering(client, ADMIN_TOKEN)
    bot, token, username = _new_bot(bot_api)
    _program_unavailable(case, bot_api, bot)
    before = _db_state(sql)

    resp = _put_bot(client, offering["code"], {"token": token})

    assert_error(resp, 503, "unavailable")
    assert_no_echo(resp, token, token.split(":", 1)[1])
    # A resposta presa só termina no dublê quando é solta (ou quando ele percebe que o cliente
    # desistiu); as demais já terminaram.
    bot_api.release_hangs()
    bot_api.wait_for(lambda: all(c.ended is not None for c in bot_api.calls()))
    assert [(c.method, c.bot_id, c.token_state, c.programmed) for c in bot_api.calls()] == [
        ("getMe", bot, "valid", True)]
    assert _db_state(sql) == before
    assert _get_offering(client, offering["code"]) == offering

    # Com o Telegram de volta (e passado o `retry_after`), o mesmo token é um primeiro registro: a
    # tentativa recusada não deixou bot, troca de token nem evento.
    clock.advance(5)
    again = register_bot(client, ADMIN_TOKEN, offering["code"], token)
    assert again == _bot_body(offering, bot, username, registered_at=iso(clock.now()))
    assert [e["type"] for e in _bot_events(client)] == ["bot.registered"]


def test_register_telegram_unreachable(app_factory: Any, bot_settings: Any, bot_api: Any,
                                       sql: psycopg.Connection) -> None:
    """Telegram inalcançável: com a URL da Bot API numa porta fechada, o
    registro dá 503 `unavailable` sem eco e nada é gravado (e o dublê não recebe nada)."""
    closed_port = dataclasses.replace(bot_settings, telegram_api_url="http://127.0.0.1:9")
    with TestClient(app_factory(bot_settings=closed_port), raise_server_exceptions=False) as http:
        offering = create_offering(http, ADMIN_TOKEN)
        bot, token, _ = _new_bot(bot_api)
        before = _db_state(sql)
        resp = _put_bot(http, offering["code"], {"token": token})
        assert_error(resp, 503, "unavailable")
        assert_no_echo(resp, token, token.split(":", 1)[1])
        assert _db_state(sql) == before
    assert bot_api.calls() == []


# ---------------------------------------------------------------------------------------------
# Sem chave-mestra
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["chave-ausente", "sem-runtime"])
def test_register_without_master_key_is_not_configured(variant: str, app_factory: Any, bot_settings: Any,
                                                       bot_api: Any, sql: psycopg.Connection) -> None:
    """Sem chave-mestra (`create_app` sem `bots`: as operações de bot existem e o
    registro responde 503 `not_configured`; ordem: validação → permissão →
    chave-mestra → oferecimento): com `BotSettings(master_key=None)` ou sem `BotRuntime`, o registro
    dá 503 `not_configured` com **zero** chamadas ao Telegram, também num oferecimento inexistente
    (antes do 404); o 400 de formato e o 403 vêm antes; `bots.get` continua existindo (404 sem bot);
    nada é gravado."""
    if variant == "chave-ausente":
        app = app_factory(bot_settings=dataclasses.replace(bot_settings, master_key=None))
    else:
        app = app_factory(bots=None)
    with TestClient(app, raise_server_exceptions=False) as http:
        offering = create_offering(http, ADMIN_TOKEN)
        weak, _ = create_token(http, ADMIN_TOKEN, WITHOUT_BOTS_MANAGE, label="sem bots manage")
        bot, token, _ = _new_bot(bot_api)
        before = _db_state(sql)

        existing = _put_bot(http, offering["code"], {"token": token})
        missing = _put_bot(http, "oferta-inexistente", {"token": token})
        bad_format = _put_bot(http, offering["code"], {"token": "1234567:" + _secret(29)})
        forbidden = _put_bot(http, offering["code"], {"token": token}, headers=auth(weak))

        assert_error(existing, 503, "not_configured")
        assert_error(missing, 503, "not_configured")
        assert_error(bad_format, 400, "invalid_request", fields=[("token", "invalid_format")])
        assert_error(forbidden, 403, "forbidden", permission="bots:manage")
        for resp in (existing, missing, bad_format, forbidden):
            assert_no_echo(resp, token, token.split(":", 1)[1])
        assert_error(http.get(_path(offering["code"]), headers=ADMIN), 404, "not_found")
        assert _get_offering(http, offering["code"]) == offering
        assert _db_state(sql) == before
    assert bot_api.calls() == []


# ---------------------------------------------------------------------------------------------
# Ordem das verificações e referência
# ---------------------------------------------------------------------------------------------


def test_register_check_order_permission_before_telegram(client: Any, clock: Any, bot_api: Any,
                                                         sql: psycopg.Connection) -> None:
    """Ordem: 401 → 400 → 403 → regras, com a permissão `bots:manage`; o token do bot
    nunca vem por argumento (a API só lê o token do corpo): sem token ou com token
    desconhecido → 401; referência e token malformados → 400 antes do 403; sem `bots:manage` → 403
    (também num oferecimento inexistente, antes do 404); oferecimento inexistente (código e UUID) →
    404; em todos, **zero** chamadas ao Telegram e nada gravado. Um token só com `bots:manage`
    registra (200), e o evento traz o `actor_token_id` dele."""
    offering = create_offering(client, ADMIN_TOKEN)
    weak, _ = create_token(client, ADMIN_TOKEN, WITHOUT_BOTS_MANAGE, label="sem bots manage")
    manager, manager_token = create_token(client, ADMIN_TOKEN, ["bots:manage"], label="so bots manage")
    bot, token, username = _new_bot(bot_api)
    remember_secret(token)
    code = offering["code"]
    missing_uuid = str(uuid4_with_prefix("dddd", fill="7"))
    before = _db_state(sql)

    cases = [
        (_put_bot(client, code, {"token": token}, headers={}), (401, "unauthenticated", {})),
        (_put_bot(client, code, {"token": token}, headers=auth("cq_desconhecido_" + "z" * 34)),
         (401, "unauthenticated", {})),
        (_put_bot(client, "REF-INVALIDA", {"token": token}, headers=auth(weak)),
         (400, "invalid_request", {"fields": [("ref", "invalid_format")]})),
        (_put_bot(client, code, {"token": "1234567:" + _secret(29)}, headers=auth(weak)),
         (400, "invalid_request", {"fields": [("token", "invalid_format")]})),
        (_put_bot(client, code, {"token": token}, headers=auth(weak)),
         (403, "forbidden", {"permission": "bots:manage"})),
        (_put_bot(client, "oferta-inexistente", {"token": token}, headers=auth(weak)),
         (403, "forbidden", {"permission": "bots:manage"})),
        (_put_bot(client, "oferta-inexistente", {"token": token}), (404, "not_found", {})),
        (_put_bot(client, missing_uuid, {"token": token}), (404, "not_found", {})),
    ]
    for resp, (status, error_code, extra) in cases:
        assert_error(resp, status, error_code, **extra)
        assert_no_echo(resp, token, token.split(":", 1)[1])
    assert bot_api.calls() == []
    assert _db_state(sql) == before

    clock.advance(10)
    registered_at = iso(clock.now())
    resp = _put_bot(client, code, {"token": token}, headers=auth(manager))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.register", 200, resp.json())
    assert resp.json() == _bot_body(offering, bot, username, registered_at=registered_at)
    assert _bot_events(client) == [_event("bot.registered", registered_at, {
        "offering_id": offering["offering_id"], "telegram_bot_id": bot, "username": username},
        actor=manager_token["token_id"])]
    assert _getme_calls(bot_api) == [("getMe", bot, "valid", 1)]


def test_register_token_in_wrong_place_is_refused_without_echo(client: Any, bot_api: Any,
                                                              sql: psycopg.Connection) -> None:
    """Token em lugar errado: o token na consulta da URL →
    `[{query, unknown_field}]`; no caminho (no lugar da referência) → `[{ref, invalid_format}]`; num
    campo a mais do corpo → `[{body, unknown_field}]`; nenhuma resposta nem log ecoa o token, nenhuma
    chamada ao Telegram e nada gravado (controle positivo: o detector acha o token num texto que o
    contém; o log tem um registro por pedido)."""
    offering = create_offering(client, ADMIN_TOKEN)
    bot, token, _ = _new_bot(bot_api)
    forms = _token_forms(token)
    before = _db_state(sql)

    with capture_logs() as cap:
        in_query = client.put(_path(offering["code"]), params={"token": token}, json={"token": token},
                              headers=ADMIN)
        in_path = client.put(_path(token), json={"token": token}, headers=ADMIN)
        extra = _put_bot(client, offering["code"], {"token": token, "bot_token": token})

    assert_error(in_query, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert_error(in_path, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_error(extra, 400, "invalid_request", fields=[("body", "unknown_field")])
    for resp in (in_query, in_path, extra):
        assert find_secrets(_response_text(resp), forms) == []
    assert find_secrets(cap.text, forms) == []
    assert find_secrets(f"texto com {token}", forms) != [], "controle positivo do detector"
    assert [(r.get("method"), r.get("route"), r.get("status")) for r in _request_records(cap)] == [
        ("PUT", REGISTER_ROUTE, 400)] * 3
    assert bot_api.calls() == []
    assert _db_state(sql) == before


def test_register_on_archived_offering_is_invalid_state(client: Any, clock: Any, bot_api: Any,
                                                        sql: psycopg.Connection) -> None:
    """Oferecimento arquivado (`invalid_state` antes das chamadas ao Telegram):
    num oferecimento arquivado (sem bot, ou com bot e um token novo do mesmo bot), o registro dá 409
    `invalid_state` com `state: archived` e **zero** chamadas ao Telegram; o 400 de formato vem antes;
    nada muda (o bot arquivado continua com o corpo do registro)."""
    empty = create_offering(client, ADMIN_TOKEN)
    _archive(client, empty["code"])
    with_bot = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, with_bot["code"], token)
    _archive(client, with_bot["code"])
    other, other_token, _ = _new_bot(bot_api)
    rotated = bot_api.rotate_token(bot)
    unknown = "1234567:" + _secret(35)
    remember_secret(unknown)
    calls_before = len(bot_api.calls())
    before = _db_state(sql)

    for ref, value in ((empty["code"], other_token), (empty["code"], unknown),
                       (with_bot["code"], rotated), (with_bot["offering_id"], other_token)):
        resp = _put_bot(client, ref, {"token": value})
        assert_error(resp, 409, "invalid_state", state="archived")
        assert_no_echo(resp, value, value.split(":", 1)[1])
    bad = _put_bot(client, with_bot["code"], {"token": "1234567:" + _secret(29)})
    assert_error(bad, 400, "invalid_request", fields=[("token", "invalid_format")])

    assert bot_api.calls()[calls_before:] == []
    assert _db_state(sql) == before
    assert _get_bot(client, with_bot["code"]) == _bot_body(with_bot, bot, username, registered_at=registered_at)


# ---------------------------------------------------------------------------------------------
# Troca de token do mesmo bot
# ---------------------------------------------------------------------------------------------


def test_register_same_bot_replaces_token(client: Any, clock: Any, bot_api: Any, sql: psycopg.Connection,
                                          db_url: str) -> None:
    """Troca de token do mesmo bot: com o
    perfil já `configured`, o token novo (`rotate_token`) dá 200 com `token_replaced_at` do relógio,
    `registered_at` mantido e perfil de volta a `pending`; no banco, `token_version` 2, o mesmo
    `bot_uuid` e um blob novo que decifra para o token novo; eventos `bot.registered` e
    `bot.token_replaced` (`token_version: 2`) exatos, sem `bot.access_changed`; uma tarefa
    `bot.sync_profile` por versão; o aviso `bot`; dois `getMe` (versões 1 e 2 no dublê)."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    internal = _internal_bot_id(sql, bot)
    first = _bot_row(sql, bot)
    sql.execute("UPDATE bots SET profile_state = 'configured' WHERE id = %s", (internal,))
    assert _get_bot(client, offering["code"])["profile_state"] == "configured"

    clock.advance(3600)
    replaced_at = iso(clock.now())
    new_token = bot_api.rotate_token(bot)
    with NoticeListener(db_url) as listener:
        resp = _put_bot(client, offering["code"], {"token": new_token})
        notices = _collect(listener, 1, 3)

    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.register", 200, resp.json())
    expected = _bot_body(offering, bot, username, registered_at=registered_at, token_replaced_at=replaced_at)
    assert resp.json() == expected
    assert _get_bot(client, offering["offering_id"]) == expected
    assert _get_offering(client, offering["code"]) == {**offering, "bot": _summary(bot, username)}

    second = _bot_row(sql, bot)
    assert second["bot_uuid"] == first["bot_uuid"]
    assert second["token_version"] == 2
    assert second["token_ciphertext"] != first["token_ciphertext"]
    assert _decrypt(second["token_ciphertext"], bot_uuid=second["bot_uuid"], telegram_bot_id=bot) == new_token
    assert (second["state"], second["access_problem"], second["profile_state"], second["registered_at"],
            second["token_replaced_at"]) == ("registered", None, "pending", first["registered_at"],
                                             clock.now())

    assert _bot_events(client) == [
        _event("bot.registered", registered_at, {"offering_id": offering["offering_id"],
                                                 "telegram_bot_id": bot, "username": username}),
        _event("bot.token_replaced", replaced_at, {"offering_id": offering["offering_id"],
                                                   "telegram_bot_id": bot, "token_version": 2}),
    ]
    assert _sync_tasks(sql) == [
        ({"bot_id": internal}, f"bot.sync_profile:{internal}:1:active", "pending", 0),
        ({"bot_id": internal}, f"bot.sync_profile:{internal}:2:active", "pending", 0),
    ]
    assert notices == [ConfigNotice(topic="bot", key=offering["offering_id"])]
    assert _getme_calls(bot_api) == [("getMe", bot, "valid", 1), ("getMe", bot, "valid", 2)]
    assert find_secrets(_response_text(resp), _token_forms(token, new_token)) == []


@pytest.mark.parametrize("problem", ["token_rejected", "token_unreadable"])
def test_register_recovers_bot_without_access(problem: str, client: Any, clock: Any, bot_api: Any,
                                              sql: psycopg.Connection) -> None:
    """Recuperação (se o bot estava sem acesso, ele volta a funcionar);
    troca com `bot.access_changed {restored, problem: null}`: o bot
    `no_access` aparece em `bots.get` com o `problem` exato e no resumo do oferecimento; o novo
    registro (token girado para `token_rejected`; o mesmo token de novo para `token_unreadable`, como
    a mensagem pede) dá 200 `registered` sem problema, e os eventos `bot.token_replaced` e
    `bot.access_changed` saem nessa ordem, no mesmo horário."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    internal = _internal_bot_id(sql, bot)
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = %s, lease_owner = NULL, "
                "lease_until = NULL WHERE id = %s", (problem, internal))
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=registered_at, state="no_access", problem=problem)
    assert _get_offering(client, offering["code"])["bot"] == _summary(bot, username, state="no_access")

    clock.advance(600)
    replaced_at = iso(clock.now())
    new_token = bot_api.rotate_token(bot) if problem == "token_rejected" else token
    resp = _put_bot(client, offering["code"], {"token": new_token})

    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.register", 200, resp.json())
    expected = _bot_body(offering, bot, username, registered_at=registered_at, token_replaced_at=replaced_at)
    assert resp.json() == expected
    assert _get_bot(client, offering["code"]) == expected
    assert _get_offering(client, offering["code"])["bot"] == _summary(bot, username)
    row = _bot_row(sql, bot)
    assert (row["state"], row["access_problem"], row["token_version"]) == ("registered", None, 2)
    assert _decrypt(row["token_ciphertext"], bot_uuid=row["bot_uuid"], telegram_bot_id=bot) == new_token
    oid = offering["offering_id"]
    assert _bot_events(client) == [
        _event("bot.registered", registered_at, {"offering_id": oid, "telegram_bot_id": bot, "username": username}),
        _event("bot.token_replaced", replaced_at, {"offering_id": oid, "telegram_bot_id": bot, "token_version": 2}),
        _event("bot.access_changed", replaced_at, {"offering_id": oid, "access": "restored", "problem": None}),
    ]


def test_register_with_other_master_key_writes_new_blob(client: Any, clock: Any, app_factory: Any,
                                                        bot_settings: Any, bot_api: Any,
                                                        sql: psycopg.Connection) -> None:
    """Chave-mestra trocada (a troca grava um blob novo sem
    decifrar o antigo): um bot registrado com a chave A é consultado por uma API com a chave B sem
    decifrar nada (mesmo corpo), e o novo registro do mesmo token pela API com a chave B dá 200, com
    `token_version` 2 e um blob cujo `key_id` é o da chave B, que decifra com B para o token."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    other_key = dataclasses.replace(bot_settings, master_key=OTHER_MASTER_KEY)
    with TestClient(app_factory(bot_settings=other_key), raise_server_exceptions=False) as other:
        assert _get_bot(other, offering["code"]) == _bot_body(offering, bot, username,
                                                              registered_at=registered_at)
        clock.advance(30)
        replaced_at = iso(clock.now())
        resp = _put_bot(other, offering["code"], {"token": token})
        assert resp.status_code == 200, resp.text[:300]
        assert_matches("bots.register", 200, resp.json())
        assert resp.json() == _bot_body(offering, bot, username, registered_at=registered_at,
                                        token_replaced_at=replaced_at)
    row = _bot_row(sql, bot)
    assert row["token_version"] == 2
    assert row["token_ciphertext"][1:5] == _key_id(OTHER_MASTER_KEY) != _key_id(MASTER_KEY)
    assert _decrypt(row["token_ciphertext"], bot_uuid=row["bot_uuid"], telegram_bot_id=bot,
                    master_key=OTHER_MASTER_KEY) == token
    assert [e["type"] for e in _bot_events(client)] == ["bot.registered", "bot.token_replaced"]


def test_register_keeps_lease_on_token_replacement(client: Any, clock: Any, bot_api: Any,
                                                   sql: psycopg.Connection) -> None:
    """Posse mantida (troca de token com o mesmo dono:
    nenhum `bot.lease_changed`; `lease`): com o oferecimento ativo e uma posse
    vigente, `bots.get` mostra `{worker_id, until}` e `last_poll_ok_at`; a troca de token mantém a
    posse (mesmo dono e validade, no corpo e no banco) e não gera evento de posse."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    internal = _internal_bot_id(sql, bot)
    until = clock.now() + timedelta(seconds=30)
    polled = clock.now() + timedelta(seconds=2)
    sql.execute("UPDATE bots SET lease_owner = 'worker-posse-teste', lease_until = %s, "
                "last_poll_ok_at = %s WHERE id = %s", (until, polled, internal))
    lease = {"worker_id": "worker-posse-teste", "until": iso(until)}
    assert _get_bot(client, offering["code"]) == _bot_body(
        offering, bot, username, registered_at=registered_at, lease=lease, last_poll_ok_at=iso(polled))

    clock.advance(5)
    replaced_at = iso(clock.now())
    resp = _put_bot(client, offering["code"], {"token": bot_api.rotate_token(bot)})
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.register", 200, resp.json())
    assert resp.json() == _bot_body(offering, bot, username, registered_at=registered_at,
                                    token_replaced_at=replaced_at, lease=lease, last_poll_ok_at=iso(polled))
    row = _bot_row(sql, bot)
    assert (row["lease_owner"], row["lease_until"], row["token_version"]) == ("worker-posse-teste", until, 2)
    assert [e["type"] for e in _bot_events(client)] == ["bot.registered", "bot.token_replaced"]


# ---------------------------------------------------------------------------------------------
# Conflitos
# ---------------------------------------------------------------------------------------------


def test_register_other_bot_is_bot_conflict(client: Any, clock: Any, bot_api: Any,
                                            sql: psycopg.Connection) -> None:
    """Outro bot (um token de outro bot dá conflito: o
    oferecimento já tem um bot com outro `telegram_bot_id` → 409 `bot_conflict` com
    `[{token, invalid_value}]`, depois do `getMe`): exatamente um `getMe` do outro bot, nada muda no
    bot atual, e o outro bot continua livre (registra em outro oferecimento)."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    other, other_token, other_username = _new_bot(bot_api)
    before = _db_state(sql)
    calls_before = len(bot_api.calls())

    resp = _put_bot(client, offering["code"], {"token": other_token})

    assert_error(resp, 409, "bot_conflict", fields=[("token", "invalid_value")])
    assert_no_echo(resp, other_token, other_token.split(":", 1)[1])
    assert _getme_calls(bot_api)[calls_before:] == [("getMe", other, "valid", 1)]
    assert _db_state(sql) == before
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=registered_at)

    second = create_offering(client, ADMIN_TOKEN)
    registered = register_bot(client, ADMIN_TOKEN, second["code"], other_token)
    assert registered == _bot_body(second, other, other_username, registered_at=iso(clock.now()))


@pytest.mark.parametrize("owner_state", ["draft", "active", "closed", "archived"])
def test_register_bot_of_other_offering_is_duplicate(owner_state: str, client: Any, clock: Any,
                                                     bot_api: Any, sql: psycopg.Connection) -> None:
    """Bot de outro oferecimento (inclusive arquivado; o `telegram_bot_id` já
    pertence a outro oferecimento → 409 `bot_conflict` com `[{token, duplicate}]`): o bot de um
    oferecimento em qualquer estado não entra em outro; exatamente um `getMe` a mais, nada gravado, e
    o outro oferecimento continua sem bot."""
    owner = create_offering(client, ADMIN_TOKEN)
    bot, token, _ = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, owner["code"], token)
    if owner_state in ("active", "closed"):
        activate_bot(client, ADMIN_TOKEN, owner["code"])
    if owner_state == "closed":
        _close(client, owner["code"])
    if owner_state == "archived":
        _archive(client, owner["code"])
    assert _get_offering(client, owner["code"])["state"] == owner_state
    target = create_offering(client, ADMIN_TOKEN)
    before = _db_state(sql)
    calls_before = len(bot_api.calls())

    resp = _put_bot(client, target["code"], {"token": token})

    assert_error(resp, 409, "bot_conflict", fields=[("token", "duplicate")])
    assert_no_echo(resp, token, token.split(":", 1)[1])
    assert _getme_calls(bot_api)[calls_before:] == [("getMe", bot, "valid", 1)]
    assert _db_state(sql) == before
    assert _get_offering(client, target["code"]) == target
    assert_error(client.get(_path(target["code"]), headers=ADMIN), 404, "not_found")


# ---------------------------------------------------------------------------------------------
# O token nunca sai
# ---------------------------------------------------------------------------------------------


def test_bot_token_never_in_responses_logs_or_events(client: Any, clock: Any, bot_api: Any,
                                                     sql: psycopg.Connection, db_url: str) -> None:
    """Token fora de toda saída (em nenhum momento o token volta numa
    resposta, num log, num evento): registro, formato inválido, token recusado, Telegram fora,
    conflito, duplicado, troca e as consultas, com a captura dos logs do sistema e do transporte
    (`httpx2`/`httpcore2` em DEBUG); nenhum corpo, cabeçalho, log, evento ou coluna do banco contém um
    token (inteiro ou a parte secreta). Controles positivos: o detector acha o token; a captura do
    transporte está ativa; há um registro `request` por pedido; a varredura do banco acha o token numa
    tabela temporária."""
    first = create_offering(client, ADMIN_TOKEN)
    second = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    other, other_token, _ = _new_bot(bot_api)
    bad = "1234567:" + _secret(29)
    unknown = "7654321:" + _secret(35)
    for value in (bad, unknown):
        remember_secret(value)
    bot_api.fail("getMe", error="server_restart", times=1, bot_id=other)

    responses: list[tuple[str, Any]] = []
    with capture_logs() as cap, capture_transport_logs() as tcap:
        logging.getLogger("httpcore2").debug("controle-da-captura-do-transporte")
        responses.append(("registro", _put_bot(client, first["code"], {"token": token})))
        responses.append(("formato", _put_bot(client, first["code"], {"token": bad})))
        responses.append(("recusado", _put_bot(client, first["code"], {"token": unknown})))
        responses.append(("indisponivel", _put_bot(client, second["code"], {"token": other_token})))
        responses.append(("outro-bot", _put_bot(client, first["code"], {"token": other_token})))
        responses.append(("duplicado", _put_bot(client, second["code"], {"token": token})))
        rotated = bot_api.rotate_token(bot)
        clock.advance(60)
        replaced_at = iso(clock.now())
        responses.append(("troca", _put_bot(client, first["code"], {"token": rotated})))
        responses.append(("bots.get", client.get(_path(first["code"]), headers=ADMIN)))
        responses.append(("offerings.get", client.get(_path(first["code"], ""), headers=ADMIN)))
        responses.append(("offerings.list", client.get("/v1/offerings", headers=ADMIN)))

    assert [(name, resp.status_code) for name, resp in responses] == [
        ("registro", 200), ("formato", 400), ("recusado", 400), ("indisponivel", 503),
        ("outro-bot", 409), ("duplicado", 409), ("troca", 200), ("bots.get", 200),
        ("offerings.get", 200), ("offerings.list", 200)]
    by_name = dict(responses)
    for name, op_id in (("registro", "bots.register"), ("troca", "bots.register"), ("bots.get", "bots.get"),
                        ("offerings.get", "offerings.get"), ("offerings.list", "offerings.list")):
        assert_matches(op_id, 200, by_name[name].json())
    assert by_name["registro"].json() == _bot_body(first, bot, username, registered_at=registered_at)
    replaced = _bot_body(first, bot, username, registered_at=registered_at, token_replaced_at=replaced_at)
    assert by_name["troca"].json() == replaced
    assert by_name["bots.get"].json() == replaced
    first_with_bot = {**first, "bot": _summary(bot, username)}
    assert by_name["offerings.get"].json() == first_with_bot
    assert by_name["offerings.list"].json() == {"items": [first_with_bot, second],
                                                "next_cursor": _offering_internal_id(sql, second)}
    assert_error(by_name["formato"], 400, "invalid_request", fields=[("token", "invalid_format")])
    assert_error(by_name["recusado"], 400, "invalid_request", fields=[("token", "invalid_value")])
    assert_error(by_name["indisponivel"], 503, "unavailable")
    assert_error(by_name["outro-bot"], 409, "bot_conflict", fields=[("token", "invalid_value")])
    assert_error(by_name["duplicado"], 409, "bot_conflict", fields=[("token", "duplicate")])
    secrets = _token_forms(*bot_api.all_tokens(), bad, unknown)
    assert find_secrets(json.dumps({"valor": rotated}), secrets) != [], "controle positivo do detector"
    for name, resp in responses:
        assert find_secrets(_response_text(resp), secrets) == [], f"token na resposta de {name}"
        assert rotated not in str(resp.request.url) and token not in str(resp.request.url)
    assert "controle-da-captura-do-transporte" in tcap.text
    assert find_secrets(tcap.text, secrets) == [], "token nos logs do transporte"
    assert [r.get("status") for r in _request_records(cap) if r.get("method") == "PUT"] == [
        200, 400, 400, 503, 409, 409, 200]
    assert find_secrets(cap.text, secrets) == [], "token nos logs do sistema"
    event_texts = [row[0] for row in sql.execute("SELECT data::text FROM events ORDER BY seq")]
    assert event_texts, "a varredura dos eventos não pode ser vazia"
    assert [t for t in event_texts if find_secrets(t, secrets)] == [], "token num evento"
    assert _plain_token_hits(scan_all_tables(db_url), secrets) == []
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE cq_controle_varredura (valor text)")
        conn.execute("INSERT INTO cq_controle_varredura (valor) VALUES (%s)", (f"prefixo {rotated} sufixo",))
        assert _plain_token_hits(scan_all_tables(conn), secrets) == [("cq_controle_varredura", "valor")]
