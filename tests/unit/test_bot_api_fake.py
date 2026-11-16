"""Autoteste da Bot API falsa.

A Bot API falsa (`tests/helpers/bot_api.py`) é o oráculo de todos os testes que tocam o Telegram. Se
ela errar o formato dos erros, a confirmação por `offset`, o 409 dos polls concorrentes, o
`allowed_updates`, a revogação ou os limites, os testes do sistema passam no vazio; se ela escrever
em stdout ou stderr, o token vaza nas varreduras. Estes testes conferem o próprio arnês e passam
antes da implementação por natureza.
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import pathlib
import re
import socket
import struct
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.helpers.bot_api import (
    DEFAULT_ALLOWED_UPDATES,
    ERRORS,
    FIRST_BOT_ID,
    HTML_BAD_GATEWAY,
    UPDATE_ID_BASE,
    UPDATE_ID_STRIDE,
    UPDATE_TYPES,
    FakeBotApi,
    Reply,
    SentMessage,
    TelegramUser,
    make_user,
    monotonic,
    session_fake,
    too_many_requests,
)
from tests.helpers.clock import START, FakeClock
from tests.helpers.secrets import SECRETS

HELPER = pathlib.Path(__file__).resolve().parents[1] / "helpers" / "bot_api.py"


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server() -> Iterator[FakeBotApi]:
    fake = FakeBotApi()
    yield fake
    fake.close()


@pytest.fixture
def fake(server: FakeBotApi, clock: FakeClock) -> Iterator[FakeBotApi]:
    """O dublê do módulo, limpo a cada teste, como a fixture `bot_api` faz."""
    server.reset(clock=clock)
    yield server
    server.reset(clock=clock)


def _envelope(key: str) -> dict[str, Any]:
    status, description = ERRORS[key]
    return {"ok": False, "error_code": status, "description": description}


def _ids(reply: Reply) -> list[int]:
    assert reply.status == 200, reply.body
    return [u["update_id"] for u in reply.json()["result"]]


def _async(fake: FakeBotApi, token: str, method: str, params: dict[str, Any] | None,
           timeout: float = 10.0) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["reply"] = fake.request(token, method, params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - o teste confere o tipo
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _wait_pending(fake: FakeBotApi, bot_id: int, count: int = 1) -> list[Any]:
    return fake.wait_for(lambda: (polls := fake.pending_polls(bot_id)) and len(polls) == count
                         and polls)


def _linger_zero() -> bytes:
    return struct.pack("HH" if os.name == "nt" else "ii", 1, 0)


def _raw_poll(fake: FakeBotApi, token: str, params: dict[str, Any]) -> socket.socket:
    """Um `getUpdates` por socket cru, para o teste controlar o fechamento da conexão."""
    body = json.dumps(params).encode("utf-8")
    sock = socket.create_connection(("127.0.0.1", fake.port), timeout=5)
    sock.sendall(f"POST /bot{token}/getUpdates HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                 .encode("ascii") + body)
    return sock


# ---------------------------------------------------------------------------------------------
# Estrutura
# ---------------------------------------------------------------------------------------------


def test_helper_uses_only_the_standard_library_and_never_cq_server() -> None:
    """Só biblioteca padrão e o registro de segredos dos testes; nada de `cq_server` nem HTTP de
    terceiros (o dublê tem de ser independente do adaptador que ele confere)."""
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    outside = {name for name in imported
               if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"}
    assert outside == {"tests.helpers.secrets"}
    assert not any(name.startswith("cq_") for name in imported)


def test_error_descriptions_are_the_official_ones() -> None:
    """As descrições exatas do servidor oficial."""
    assert ERRORS == {
        "unauthorized": (401, "Unauthorized"),
        "invalid_token": (401, "Unauthorized: invalid token specified"),
        "forbidden_token": (421, "Misdirected Request: forbidden token specified"),
        "blocked": (403, "Forbidden: bot was blocked by the user"),
        "user_deactivated": (403, "Forbidden: user is deactivated"),
        "kicked": (403, "Forbidden: bot was kicked from the group chat"),
        "chat_not_found": (400, "Bad Request: chat not found"),
        "private_chat_not_found": (400, "Bad Request: private chat not found"),
        "not_found": (404, "Not Found"),
        "method_not_found": (404, "Not Found: method not found"),
        "conflict_poll": (409, "Conflict: terminated by other getUpdates request; make sure that "
                               "only one bot instance is running"),
        "conflict_webhook": (409, "Conflict: can't use getUpdates method while webhook is active; "
                                  "use deleteWebhook to delete the webhook first"),
        "conflict_set_webhook": (409, "Conflict: terminated by setWebhook request"),
        "server_restart": (500, "Internal Server Error: restart"),
    }
    assert too_many_requests(5) == (429, "Too Many Requests: retry after 5")
    assert DEFAULT_ALLOWED_UPDATES == set(UPDATE_TYPES) - {
        "chat_member", "message_reaction", "message_reaction_count"}
    assert len(UPDATE_TYPES) == 29


def test_base_url_is_loopback_http_without_trailing_slash(fake: FakeBotApi) -> None:
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]{2,5}", fake.base_url)
    assert fake.base_url == f"http://127.0.0.1:{fake.port}"


# ---------------------------------------------------------------------------------------------
# Tokens, getMe e formato das respostas
# ---------------------------------------------------------------------------------------------


def test_tokens_have_the_official_shape_and_enter_the_secret_registry() -> None:
    with FakeBotApi() as fresh:
        first, second = fresh.add_bot(), fresh.add_bot()
        assert (first, second) == (FIRST_BOT_ID, FIRST_BOT_ID + 1)
        assert fresh.state(first).next_update_id == UPDATE_ID_BASE
        assert fresh.state(second).next_update_id == UPDATE_ID_BASE + UPDATE_ID_STRIDE
        token = fresh.token_of(first)
        assert re.fullmatch(r"6100000001:[A-Za-z0-9_-]{35}", token)
        secret = token.split(":", 1)[1]
        assert token in SECRETS and secret in SECRETS
        rotated = fresh.rotate_token(first)
        assert re.fullmatch(r"6100000001:[A-Za-z0-9_-]{35}", rotated) and rotated != token
        assert rotated in SECRETS and rotated.split(":", 1)[1] in SECRETS
        assert fresh.token_of(first) == rotated
        assert fresh.all_tokens() == [token, fresh.token_of(second), rotated]
        assert fresh.state(first).username == "cq_teste_0000_bot"
        assert fresh.state(second).username == "cq_teste_0001_bot"


def test_get_me_returns_the_bot_user_and_methods_ignore_case(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    username = fake.state(bot).username
    assert username is not None and re.fullmatch(r"cq_teste_[0-9]{4}_bot", username)
    expected = {"ok": True, "result": {
        "id": bot, "is_bot": True, "first_name": "Bot de teste", "username": username,
        "can_join_groups": True, "can_read_all_group_messages": False,
        "supports_inline_queries": False}}

    reply = fake.request(token, "getMe", {})
    assert reply.status == 200
    assert reply.headers["content-type"] == "application/json"
    assert int(reply.headers["content-length"]) == len(reply.body)
    assert reply.json() == expected
    assert fake.request(token, "GETME", None).json() == expected
    assert [(c.method, c.token_state, c.token_version, c.outcome, c.status)
            for c in fake.calls()] == [("getMe", "valid", 1, "ok", 200)] * 2

    nameless = fake.add_bot(None)
    not_a_bot = fake.add_bot(is_bot=False)
    wide = fake.add_bot(bot_id=8123456789, first_name="Outro", extra={"has_main_web_app": False})
    assert "username" not in fake.request(fake.token_of(nameless), "getMe", {}).json()["result"]
    assert fake.request(fake.token_of(not_a_bot), "getMe", {}).json()["result"]["is_bot"] is False
    result = fake.request(fake.token_of(wide), "getMe", {}).json()["result"]
    assert (result["id"], result["first_name"], result["has_main_web_app"]) == (
        8123456789, "Outro", False)
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("key", sorted(ERRORS))
def test_programmed_error_has_the_official_envelope(fake: FakeBotApi, key: str) -> None:
    """Status HTTP = `error_code`, corpo exato, e a falha vale só para `times` chamadas."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    fake.fail("getMe", error=key)
    reply = fake.request(token, "getMe", {})
    assert reply.status == ERRORS[key][0]
    assert reply.headers["content-type"] == "application/json"
    assert reply.json() == _envelope(key)
    assert "retry-after" not in reply.headers
    call = fake.calls()[0]
    assert (call.outcome, call.status, call.description, call.programmed, call.retry_after) == (
        "error", ERRORS[key][0], ERRORS[key][1], True, None)
    assert fake.request(token, "getMe", {}).status == 200
    assert fake.protocol_errors() == []


def test_retry_after_comes_as_parameter_and_header(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(1)
    fake.push_text(bot, user, "oi")
    fake.fail("sendMessage", retry_after=7)
    reply = fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": "olá"})
    assert reply.status == 429
    assert reply.json() == {"ok": False, "error_code": 429,
                            "description": "Too Many Requests: retry after 7",
                            "parameters": {"retry_after": 7}}
    assert reply.headers["retry-after"] == "7"
    assert fake.calls()[-1].retry_after == 7
    assert fake.sent_messages() == []

    fake.fail("getMe", status=429)
    reply = fake.request(token, "getMe", {})
    assert reply.status == 429
    assert reply.json() == {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    assert "retry-after" not in reply.headers

    fake.fail("getMe", status=418, description="I'm a teapot")
    assert fake.request(token, "getMe", {}).json() == {
        "ok": False, "error_code": 418, "description": "I'm a teapot"}
    assert fake.protocol_errors() == []


def test_token_checks_follow_the_client_manager(fake: FakeBotApi) -> None:
    """`invalid token specified`, `forbidden token specified` e o 401 genérico."""
    invalid = _envelope("invalid_token")
    cases = [
        ("0123:abc", invalid, "malformed", None),
        ("123abc", invalid, "malformed", None),
        ("1:" + "a" * 79, invalid, "malformed", None),
        ("abc:def", _envelope("forbidden_token"), "malformed", None),
        ("-5:abc", invalid, "malformed", None),
        (f"{2**54}:abc", invalid, "malformed", None),
        ("123456:desconhecido", _envelope("unauthorized"), "unknown", 123456),
    ]
    for token, body, state, bot_id in cases:
        reply = fake.request(token, "getMe", {})
        assert (reply.status, reply.json()) == (body["error_code"], body), token
        call = fake.calls()[-1]
        assert (call.token_state, call.bot_id, call.token_version) == (state, bot_id, None)
    assert fake.protocol_errors() == []


def test_paths_and_methods_outside_the_fake_are_404(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    not_found, method_not_found = _envelope("not_found"), _envelope("method_not_found")
    assert fake.request(token, "", {}, path="/getMe").json() == not_found
    assert fake.request(token, "", {}, path=f"/bot{token}").json() == not_found
    assert fake.request(token, "", {}, path=f"/bot{token}/test/getMe").json() == method_not_found
    for method in ("sendPhoto", "logOut", "close"):
        reply = fake.request(token, method, {})
        assert (reply.status, reply.json()) == (404, method_not_found)
    assert fake.protocol_errors() == [
        "?: caminho fora de /bot<token>/<método>",
        "?: caminho fora de /bot<token>/<método>",
        "getMe: ambiente de teste /test/ do Telegram",
        "sendPhoto: método desconhecido ou fora do dublê",
        "logOut: chamada proibida ao método logOut",
        "close: chamada proibida ao método close",
    ]


def test_protocol_deviations_are_reported(fake: FakeBotApi) -> None:
    """Consulta na URL, método HTTP, multipart, formulário e JSON inválido vão para
    `protocol_errors()` (o sistema só faz POST com corpo JSON UTF-8)."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    assert fake.request(token, "getMe", None, query="x=1").status == 200
    assert fake.calls()[-1].has_query is True
    assert fake.request(token, "getMe", None, http_method="GET").status == 200
    assert fake.calls()[-1].http_method == "GET"
    multipart = b"--x\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n1\r\n--x--\r\n"
    assert fake.request(token, "getMe", body=multipart,
                        content_type="multipart/form-data; boundary=x").status == 200
    reply = fake.request(token, "getUpdates", body=b"timeout=0",
                         content_type="application/x-www-form-urlencoded")
    assert reply.json() == {"ok": True, "result": []}
    assert fake.calls()[-1].params == {"timeout": "0"}
    for body in (b"{", b"[]"):
        reply = fake.request(token, "getMe", body=body)
        assert (reply.status, reply.json()) == (400, {
            "ok": False, "error_code": 400, "description": "Bad Request: can't parse JSON object"})
    assert fake.request(token, "getMe", {"extra": 1}).status == 200
    assert fake.request(token, "getMe", body=b"{}", content_type="text/plain").status == 200
    assert fake.protocol_errors() == [
        "getMe: parâmetros na consulta da URL",
        "getMe: parâmetro não documentado: 'x'",
        "getMe: método HTTP GET (esperado POST)",
        "getMe: corpo multipart/form-data (esperado JSON)",
        "getUpdates: corpo de formulário (esperado JSON)",
        "getUpdates: timeout fora de 0 a 50",
        "getMe: corpo não é JSON UTF-8 válido",
        "getMe: corpo JSON não é um objeto",
        "getMe: parâmetro não documentado: 'extra'",
        "getMe: Content-Type não suportado: text/plain",
    ]


def test_documented_limits_are_protocol_errors(fake: FakeBotApi) -> None:
    """Limites da documentação, contados em unidades UTF-16."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(2)
    pending = fake.push_text(bot, user, "fica na fila")  # faz os getUpdates voltarem na hora

    def call(method: str, params: dict[str, Any]) -> Reply:
        return fake.request(token, method, params)

    for params in ({"timeout": 0, "limit": 0}, {"timeout": 0, "limit": 101},
                   {"timeout": 51}, {"timeout": -1}, {"timeout": 0, "offset": 2**31},
                   {"timeout": 0, "offset": 1.5}, {"timeout": 0, "limit": True},
                   {"timeout": 0, "allowed_updates": "message"},
                   {"timeout": 0, "allowed_updates": ["mensagem"]}):
        assert _ids(call("getUpdates", params)) == [pending], params

    def bad(reply: Reply, description: str) -> None:
        assert (reply.status, reply.json()) == (400, {
            "ok": False, "error_code": 400, "description": description})

    wide = "\U0001d538"  # fora do BMP: 2 unidades UTF-16
    assert call("setMyDescription", {"description": wide * 256}).status == 200
    assert fake.state(bot).description == wide * 256
    bad(call("setMyDescription", {"description": wide * 256 + "a"}),
        "Bad Request: description is too long")
    assert call("setMyDescription", {"description": "x", "language_code": "pt-BR"}).status == 200

    many = [{"command": f"c{i}", "description": "d"} for i in range(100)]
    many[0] = {"command": "a" * 32, "description": "é" * 256}
    assert call("setMyCommands", {"commands": many}).status == 200
    assert fake.state(bot).commands == tuple(many)
    bad(call("setMyCommands", {"commands": []}), "Bad Request: invalid bot commands")
    bad(call("setMyCommands", {"commands": [*many, {"command": "x", "description": "d"}]}),
        "Bad Request: invalid bot commands")
    for command in ({"command": "Start", "description": "x"},
                    {"command": "a" * 33, "description": "x"},
                    {"command": "start", "description": ""},
                    {"command": "start", "description": wide * 128 + "a"}):
        bad(call("setMyCommands", {"commands": [command]}), "Bad Request: invalid bot commands")
    assert call("setMyCommands", {"commands": [
        {"command": "start", "description": "x", "emoji": "y"}]}).status == 200
    bad(call("setMyCommands", {}), 'Bad Request: parameter "commands" is required')

    bad(call("sendMessage", {"chat_id": user.user_id, "text": ""}),
        "Bad Request: message text is empty")
    bad(call("sendMessage", {"chat_id": user.user_id, "text": "a" * 4097}),
        "Bad Request: message is too long")
    assert call("sendMessage", {"chat_id": user.user_id, "text": wide * 2048}).status == 200
    bad(call("sendMessage", {"chat_id": user.user_id, "text": wide * 2048 + "a"}),
        "Bad Request: message is too long")
    bad(call("sendMessage", {"text": "oi"}), "Bad Request: chat_id is empty")
    bad(call("sendMessage", {"chat_id": user.user_id}), "Bad Request: message text is empty")
    bad(call("answerCallbackQuery", {"callback_query_id": "1", "text": "a" * 201}),
        "Bad Request: text is too long")
    assert call("deleteWebhook", {"drop_pending_updates": "false"}).status == 200

    assert fake.protocol_errors() == [
        "getUpdates: limit fora de 1 a 100",
        "getUpdates: limit fora de 1 a 100",
        "getUpdates: timeout fora de 0 a 50",
        "getUpdates: timeout fora de 0 a 50",
        "getUpdates: offset não é um inteiro de 32 bits",
        "getUpdates: offset não é um inteiro de 32 bits",
        "getUpdates: limit fora de 1 a 100",
        "getUpdates: allowed_updates não é uma lista de textos",
        "getUpdates: allowed_updates com tipo de update desconhecido",
        "setMyDescription: description com mais de 512 unidades UTF-16",
        "setMyDescription: language_code fora de duas letras ISO 639-1",
        "setMyCommands: commands fora de 1 a 100 itens",
        "setMyCommands: commands fora de 1 a 100 itens",
        "setMyCommands: commands[0].command fora de ^[a-z0-9_]{1,32}$",
        "setMyCommands: commands[0].command fora de ^[a-z0-9_]{1,32}$",
        "setMyCommands: commands[0].description fora de 1 a 256 unidades UTF-16",
        "setMyCommands: commands[0].description fora de 1 a 256 unidades UTF-16",
        "setMyCommands: commands[0] com campo não documentado: 'emoji'",
        "setMyCommands: parâmetro obrigatório ausente: 'commands'",
        "sendMessage: text vazio",
        "sendMessage: text com mais de 4096 unidades UTF-16",
        "sendMessage: text com mais de 4096 unidades UTF-16",
        "sendMessage: parâmetro obrigatório ausente: 'chat_id'",
        "sendMessage: parâmetro obrigatório ausente: 'text'",
        "answerCallbackQuery: text com mais de 200 unidades UTF-16",
        "deleteWebhook: drop_pending_updates não é booleano",
    ]


# ---------------------------------------------------------------------------------------------
# getUpdates: fila, offset, long poll, 409 e allowed_updates
# ---------------------------------------------------------------------------------------------


def test_offset_confirms_and_forgets_like_the_official_queue(fake: FakeBotApi,
                                                            clock: FakeClock) -> None:
    """sem `offset` nada é confirmado; `offset` esquece o que está abaixo;
    negativo mantém os |offset| últimos; fora da faixa volta à cabeça sem esquecer."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(3)
    base = fake.state(bot).next_update_id

    def get(**params: Any) -> list[int]:
        return _ids(fake.request(token, "getUpdates", {"timeout": 0, **params}))

    ids = [fake.push_text(bot, user, f"m{i}") for i in range(3)]
    assert ids == [base, base + 1, base + 2]
    first = fake.request(token, "getUpdates", {"timeout": 0}).json()["result"][0]
    assert first == {"update_id": base, "message": {
        "message_id": 1, "from": user.as_user(), "chat": user.as_private_chat(),
        "date": int(clock.now().timestamp()), "text": "m0"}}
    assert get() == ids
    assert get(offset=ids[0] + 1) == ids[1:]
    assert fake.state(bot).pending_update_ids == tuple(ids[1:])
    assert get(limit=1) == [ids[1]]
    assert get(offset=ids[2] + 1) == []
    assert fake.state(bot).pending_update_ids == ()

    more = [fake.push_text(bot, user, f"n{i}") for i in range(2)]
    assert get(offset=more[-1] + 12) == more  # acima do fim + 10: volta à cabeça
    assert fake.state(bot).pending_update_ids == tuple(more)
    assert get(offset=more[-1] + 11) == []  # dentro da faixa: esquece tudo abaixo, e esse risco fica aceito
    assert fake.state(bot).pending_update_ids == ()

    three = [fake.push_text(bot, user, f"o{i}") for i in range(3)]
    assert get(offset=-1) == [three[-1]]
    assert fake.state(bot).pending_update_ids == (three[-1],)
    last = fake.push_text(bot, user, "p")
    assert get(offset=last - 100_001) == [three[-1], last]  # abaixo da cabeça: nada é esquecido
    assert fake.state(bot).pending_update_ids == (three[-1], last)
    assert fake.protocol_errors() == []


def test_long_poll_blocks_until_an_update_or_the_timeout(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(4)
    thread, box = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    pushed_at = monotonic()
    update_id = fake.push_text(bot, user, "acorda")
    thread.join(3)
    assert _ids(box["reply"]) == [update_id]
    call = fake.calls("getUpdates")[-1]
    assert call.outcome == "ok" and call.ended is not None and call.ended - pushed_at < 0.5
    assert call.result == box["reply"].json()["result"]

    reply = fake.request(token, "getUpdates", {"timeout": 1, "offset": update_id + 1})
    assert reply.json() == {"ok": True, "result": []}
    call = fake.calls("getUpdates")[-1]
    assert call.ended is not None and 1.0 <= call.ended - call.started < 1.5
    assert fake.pending_polls() == []
    assert fake.protocol_errors() == []


def test_a_newer_poll_ends_the_older_with_409(fake: FakeBotApi) -> None:
    """perde o **mais antigo**; o novo fica esperando; `timeout=0` não encerra."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    thread_a, box_a = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    thread_b, box_b = _async(fake, token, "getUpdates", {"timeout": 5})
    thread_a.join(2)
    assert (box_a["reply"].status, box_a["reply"].json()) == (409, _envelope("conflict_poll"))
    b_seq = fake.calls("getUpdates")[1].seq
    fake.wait_for(lambda: [c.seq for c in fake.pending_polls(bot)] == [b_seq])
    assert fake.request(token, "getUpdates", {"timeout": 0}).json() == {"ok": True, "result": []}
    assert [c.seq for c in fake.pending_polls(bot)] == [b_seq]
    update_id = fake.push_text(bot, make_user(5), "para o B")
    thread_b.join(2)
    assert _ids(box_b["reply"]) == [update_id]

    a, b, zero = fake.calls("getUpdates")
    assert (a.outcome, a.status, a.programmed, a.conflicted_by) == ("error", 409, False, b.seq)
    assert (b.outcome, zero.outcome) == ("ok", "ok")
    assert fake.conflicts(bot) == [(a, b)]
    assert fake.overlaps(bot) == [(a, b), (b, zero)]
    assert fake.max_concurrency(bot) == 2
    assert fake.protocol_errors() == []


def test_conflict_409s_are_spaced_when_the_delay_is_on(fake: FakeBotApi, clock: FakeClock) -> None:
    """Com `conflict_delay_seconds`, um 409 dentro da janela sai com esse atraso (o servidor usa
    3 s, Client.cpp `fail_query_conflict`); o padrão é sem atraso (teste anterior)."""
    fake.reset(clock=clock, conflict_delay_seconds=1.0)
    bot = fake.add_bot()
    token = fake.token_of(bot)
    t1, box1 = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    t2, box2 = _async(fake, token, "getUpdates", {"timeout": 5})
    t1.join(2)
    assert box1["reply"].json() == _envelope("conflict_poll")
    a2_seq = fake.calls("getUpdates")[1].seq
    fake.wait_for(lambda: [c.seq for c in fake.pending_polls(bot)] == [a2_seq])
    t3, box3 = _async(fake, token, "getUpdates", {"timeout": 5})
    t2.join(3)
    assert box2["reply"].json() == _envelope("conflict_poll")
    update_id = fake.push_text(bot, make_user(6), "para o terceiro")
    t3.join(2)
    assert _ids(box3["reply"]) == [update_id]

    a1, a2, a3 = fake.calls("getUpdates")
    assert a1.ended is not None and a1.ended - a2.started < 0.3
    assert a2.ended is not None and 1.0 <= a2.ended - a3.started < 1.8
    assert fake.conflicts(bot) == [(a1, a2), (a2, a3)]
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("how", ["fin", "rst"])
def test_abandoned_poll_ends_within_100ms_without_reply(fake: FakeBotApi, how: str) -> None:
    """O cliente fecha a conexão (FIN do `shutdown` + `close`, como o `abort()` do adaptador, ou
    RST): o dublê acaba o poll em até 0,1 s, `abandoned`, sem resposta nem 409."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    sock = _raw_poll(fake, token, {"timeout": 5})
    _wait_pending(fake, bot)
    if how == "rst":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _linger_zero())
    closed_at = monotonic()
    if how == "fin":
        sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    call = fake.wait_for(lambda: [c for c in fake.calls("getUpdates") if c.ended is not None],
                         timeout=2)[0]
    assert (call.outcome, call.status, call.description, call.result) == (
        "abandoned", None, None, None)
    assert call.ended is not None and call.ended - closed_at <= 0.1
    assert fake.pending_polls() == [] and fake.conflicts() == []
    assert fake.request(token, "getUpdates", {"timeout": 0}).json() == {"ok": True, "result": []}
    assert fake.max_concurrency(bot) == 1
    assert fake.protocol_errors() == []


def test_max_concurrency_and_overlaps_can_exclude_abandoned(fake: FakeBotApi,
                                                            clock: FakeClock) -> None:
    """Um poll encerrado por conflito (409 adiado) cujo cliente desiste termina `abandoned`, e só
    `exclude_abandoned=True` o tira da contagem; `overlaps()` e `max_concurrency()` só olham
    `getUpdates`."""
    fake.reset(clock=clock, conflict_delay_seconds=1.0)
    bot = fake.add_bot()
    token = fake.token_of(bot)
    t1, box1 = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    sock = _raw_poll(fake, token, {"timeout": 5})
    t1.join(2)
    assert box1["reply"].json() == _envelope("conflict_poll")
    a2_seq = fake.calls("getUpdates")[1].seq
    fake.wait_for(lambda: [c.seq for c in fake.pending_polls(bot)] == [a2_seq])
    t3, box3 = _async(fake, token, "getUpdates", {"timeout": 5})
    fake.wait_for(lambda: [c.seq for c in fake.pending_polls(bot)] == [a2_seq + 1])
    assert fake.calls("getUpdates")[1].ended is None  # o 409 do segundo está adiado
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    fake.wait_for(lambda: fake.calls("getUpdates")[1].outcome == "abandoned", timeout=2)
    assert fake.request(token, "getMe", {}).status == 200  # outro método não conta
    update_id = fake.push_text(bot, make_user(8), "fim")
    t3.join(2)
    assert _ids(box3["reply"]) == [update_id]

    a1, a2, a3 = fake.calls("getUpdates")
    assert (a1.outcome, a2.outcome, a3.outcome) == ("error", "abandoned", "ok")
    assert fake.conflicts(bot) == [(a1, a2), (a2, a3)]
    assert fake.overlaps(bot) == [(a1, a2), (a2, a3)]
    assert fake.overlaps(bot, exclude_abandoned=True) == []
    assert fake.max_concurrency(bot) == 2
    assert fake.max_concurrency(bot, exclude_abandoned=True) == 1
    assert fake.protocol_errors() == []


def test_allowed_updates_persist_and_filter_at_generation(fake: FakeBotApi) -> None:
    """a lista persiste; vazia ou só com nomes desconhecidos = padrão; o que
    está fora é descartado na geração (sem número), e não afeta o que já estava na fila."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(9)
    edited = {"edited_message": {"message_id": 1, "from": user.as_user(),
                                 "chat": user.as_private_chat(), "date": 0, "edit_date": 1,
                                 "text": "editado"}}

    def get(**params: Any) -> Reply:
        return fake.request(token, "getUpdates", {"timeout": 0, **params})

    assert fake.state(bot).allowed_updates == tuple(
        t for t in UPDATE_TYPES if t in DEFAULT_ALLOWED_UPDATES)
    before = fake.push_update(bot, edited)
    assert before is not None
    ours = ["message", "callback_query", "my_chat_member"]
    assert _ids(get(allowed_updates=ours)) == [before]
    assert fake.state(bot).allowed_updates == tuple(ours)
    assert fake.push_update(bot, edited) is None
    assert fake.state(bot).dropped_updates == 1
    kept = [fake.push_text(bot, user, "oi"), fake.push_callback(bot, user, "botao"),
            fake.push_my_chat_member(bot, user)]
    reply = get(offset=before + 1)  # sem allowed_updates: vale a lista anterior
    assert [next(k for k in u if k != "update_id") for u in reply.json()["result"]] == ours
    assert _ids(reply) == kept

    assert _ids(get(offset=kept[-1] + 1, allowed_updates=["message"])) == []
    assert fake.push_callback(bot, user, "fora") is None
    assert _ids(get(allowed_updates=[])) == []
    assert fake.state(bot).allowed_updates == tuple(
        t for t in UPDATE_TYPES if t in DEFAULT_ALLOWED_UPDATES)
    assert fake.push_callback(bot, user, "dentro") is not None
    assert fake.push_update(bot, {"chat_member": {"chat": user.as_private_chat()}}) is None
    fake.request(token, "getUpdates", {"timeout": 0, "offset": -1})
    get(allowed_updates=["message"])
    get(allowed_updates=["desconhecido"])
    assert "edited_message" in fake.state(bot).allowed_updates
    get(allowed_updates=["message"])
    get(allowed_updates=["callback_query", 1])
    assert fake.state(bot).allowed_updates == ("message",)
    assert fake.state(bot).dropped_updates == 3
    assert fake.protocol_errors() == [
        "getUpdates: allowed_updates com tipo de update desconhecido",
        "getUpdates: allowed_updates não é uma lista de textos",
    ]


# ---------------------------------------------------------------------------------------------
# Revogação, troca de token, envio e webhook
# ---------------------------------------------------------------------------------------------


def test_revoke_and_rotate_answer_401_also_to_pending_polls(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    old = fake.token_of(bot)
    thread, box = _async(fake, old, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    revoked_at = monotonic()
    fake.revoke(bot)
    thread.join(2)
    assert (box["reply"].status, box["reply"].json()) == (401, _envelope("unauthorized"))
    ended = fake.calls("getUpdates")[-1].ended
    assert ended is not None and ended - revoked_at < 0.5
    assert fake.request(old, "getMe", {}).json() == _envelope("unauthorized")
    assert fake.calls()[-1].token_state == "revoked"
    assert fake.state(bot).revoked is True

    new = fake.rotate_token(bot)
    assert (fake.state(bot).token_version, fake.state(bot).revoked) == (2, False)
    assert fake.request(new, "getMe", {}).status == 200
    assert (fake.calls()[-1].token_state, fake.calls()[-1].token_version) == ("valid", 2)
    assert fake.request(old, "getMe", {}).status == 401
    assert (fake.calls()[-1].token_state, fake.calls()[-1].token_version) == ("revoked", 1)

    thread, box = _async(fake, new, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    newest = fake.rotate_token(bot)
    thread.join(2)
    assert box["reply"].json() == _envelope("unauthorized")
    assert fake.request(newest, "getMe", {}).status == 200
    assert fake.protocol_errors() == []


def test_send_message_needs_a_known_unblocked_chat(fake: FakeBotApi, clock: FakeClock) -> None:
    """`chat not found` para quem nunca falou com o bot; 403 no bloqueio;
    `my_chat_member` para `kicked` bloqueia e de volta a `member` desbloqueia."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(10)

    def send(text: str, chat_id: Any = user.user_id) -> Reply:
        return fake.request(token, "sendMessage", {"chat_id": chat_id, "text": text})

    assert (send("olá").status, send("olá").json()) == (400, _envelope("chat_not_found"))
    fake.push_text(bot, user, "oi")
    reply = send("Olá!")
    assert reply.json() == {"ok": True, "result": {
        "message_id": 2,
        "from": {"id": bot, "is_bot": True, "first_name": "Bot de teste",
                 "username": fake.state(bot).username},
        "chat": {"id": user.user_id, "type": "private"},
        "date": int(clock.now().timestamp()),
        "text": "Olá!"}}
    assert fake.sent_messages(bot) == [
        SentMessage(bot, user.user_id, 2, "Olá!", clock.now(), fake.calls()[-1].seq)]

    fake.block_chat(bot, user)
    assert (send("x").status, send("x").json()) == (403, _envelope("blocked"))
    fake.block_chat(bot, user.user_id, deactivated=True)
    assert send("x").json() == _envelope("user_deactivated")
    fake.unblock_chat(bot, user)
    assert send("de volta").json()["result"]["message_id"] == 3
    fake.push_my_chat_member(bot, user)
    assert send("x").json() == _envelope("blocked")
    assert fake.state(bot).blocked_chats == {user.user_id}
    fake.push_my_chat_member(bot, user, old_status="kicked", new_status="member")
    assert send("desbloqueado", chat_id=str(user.user_id)).status == 200
    assert [m.text for m in fake.sent_messages(bot, chat_id=user.user_id)] == [
        "Olá!", "de volta", "desbloqueado"]
    assert fake.request(token, "answerCallbackQuery", {"callback_query_id": "1"}).json() == {
        "ok": True, "result": True}
    assert fake.protocol_errors() == []


def test_webhook_gives_409_until_delete_webhook(fake: FakeBotApi) -> None:
    """descrições do `deleteWebhook`, 409 de webhook e o 409 do poll em aberto."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    assert fake.request(token, "deleteWebhook", {"drop_pending_updates": False}).json() == {
        "ok": True, "result": True, "description": "Webhook is already deleted"}
    thread, box = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    fake.set_webhook(bot)
    thread.join(2)
    assert box["reply"].json() == _envelope("conflict_set_webhook")
    assert fake.conflicts() == []
    reply = fake.request(token, "getUpdates", {"timeout": 1})
    assert (reply.status, reply.json()) == (409, _envelope("conflict_webhook"))
    assert fake.request(token, "getWebhookInfo", {}).json() == {"ok": True, "result": {
        "url": "https://webhook.invalid/cq", "has_custom_certificate": False,
        "pending_update_count": 0, "max_connections": 40}}
    assert fake.request(token, "deleteWebhook", {"drop_pending_updates": False}).json() == {
        "ok": True, "result": True, "description": "Webhook was deleted"}
    assert fake.request(token, "getUpdates", {"timeout": 0}).json() == {"ok": True, "result": []}
    assert fake.request(token, "getWebhookInfo", {}).json() == {"ok": True, "result": {
        "url": "", "has_custom_certificate": False, "pending_update_count": 0}}

    fake.push_text(bot, make_user(11), "vai ser descartado")
    assert fake.request(token, "deleteWebhook", {"drop_pending_updates": True}).status == 200
    assert fake.state(bot).pending_update_ids == ()
    assert fake.protocol_errors() == [
        "deleteWebhook: drop_pending_updates=true descarta as pendências"]


# ---------------------------------------------------------------------------------------------
# Falhas de rede e programadas
# ---------------------------------------------------------------------------------------------


def test_network_failures_reset_hang_and_not_json(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)

    fake.fail_network("getMe", kind="reset")
    with pytest.raises((OSError, http.client.HTTPException)):
        fake.request(token, "getMe", {})
    call = fake.calls()[-1]
    assert (call.outcome, call.status, call.programmed) == ("reset", None, True)

    fake.fail_network("getMe", kind="not_json")
    reply = fake.request(token, "getMe", {})
    assert (reply.status, reply.headers["content-type"], reply.body) == (
        502, "text/html", HTML_BAD_GATEWAY)
    with pytest.raises(ValueError):
        reply.json()
    call = fake.calls()[-1]
    assert (call.outcome, call.status, call.programmed) == ("error", 502, True)

    fake.fail_network("getMe", kind="hang")
    with pytest.raises(TimeoutError):
        fake.request(token, "getMe", {}, timeout=0.3)
    call = fake.wait_for(lambda: (c := fake.calls()[-1]).ended is not None and c)
    assert (call.outcome, call.status) == ("hang", None)
    assert call.ended is not None and call.ended - call.started >= 0.3

    fake.fail_network("getMe", kind="hang")
    thread, box = _async(fake, token, "getMe", {}, timeout=5)
    fake.wait_for(lambda: fake.calls()[-1].seq == call.seq + 1)
    fake.release_hangs()
    thread.join(2)
    assert isinstance(box["error"], (OSError, http.client.HTTPException))
    assert fake.calls()[-1].outcome == "hang"
    assert fake.request(token, "getMe", {}).status == 200
    assert fake.protocol_errors() == []


def test_programmed_failures_honor_times_when_and_bot(fake: FakeBotApi) -> None:
    bot, other = fake.add_bot(), fake.add_bot()
    token, other_token = fake.token_of(bot), fake.token_of(other)
    fake.fail("getMe", error="server_restart", times=2, bot_id=bot)
    assert fake.request(other_token, "getMe", {}).status == 200
    assert [fake.request(token, "getMe", {}).status for _ in range(3)] == [500, 500, 200]

    fake.fail("getUpdates", status=502, description="Bad Gateway", times=None,
              when=lambda c: c.params.get("offset") == 5)
    statuses = [fake.request(token, "getUpdates", params).status
                for params in ({"timeout": 0, "offset": 5}, {"timeout": 0},
                               {"timeout": 0, "offset": 5})]
    assert statuses == [502, 200, 502]
    fake.clear_failures()
    assert fake.request(token, "getUpdates", {"timeout": 0, "offset": 5}).status == 200

    fake.fail("*", status=503, description="Service Unavailable")
    assert fake.request(token, "setMyCommands", {"commands": [
        {"command": "start", "description": "x"}]}).json() == {
        "ok": False, "error_code": 503, "description": "Service Unavailable"}

    with pytest.raises(ValueError):
        fake.fail("sendPhoto", status=500)
    with pytest.raises(ValueError):
        fake.fail("getMe")
    with pytest.raises(ValueError):
        fake.fail("getMe", status=500, times=0)
    with pytest.raises(ValueError):
        fake.fail_network("getMe", kind="slow")
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Registro, relógio, ganchos, reentrega e injeção
# ---------------------------------------------------------------------------------------------


def test_calls_record_clock_monotonic_times_and_connection(fake: FakeBotApi,
                                                          clock: FakeClock) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    try:
        for _ in range(2):
            conn.request("POST", f"/bot{token}/getMe", body=b"{}",
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert (resp.status, resp.version) == (200, 11)
            resp.read()
            clock.advance(90)
    finally:
        conn.close()
    fake.request(token, "getMe", None)
    c1, c2, c3 = fake.calls()
    assert (c1.seq, c2.seq, c3.seq) == (1, 2, 3)
    assert (c1.clock_at, c2.clock_at, c3.clock_at) == (
        START, START + timedelta(seconds=90), START + timedelta(seconds=180))
    assert c1.ended is not None and c2.ended is not None and c3.ended is not None
    assert c1.started <= c1.ended <= c2.started <= c2.ended <= c3.started <= c3.ended
    assert c1.connection_id == c2.connection_id != c3.connection_id  # HTTP/1.1 mantém a conexão
    assert (c1.http_method, c1.content_type, c1.has_query, c1.params) == (
        "POST", "application/json", False, {})
    assert (c3.content_type, c3.params) == (None, {})

    user = make_user(12)
    fake.push_text(bot, user, "agora")
    fake.push_text(bot, user, "antes", date=datetime(2030, 5, 1, tzinfo=timezone.utc))
    dates = [u["message"]["date"] for u in fake.request(
        token, "getUpdates", {"timeout": 0}).json()["result"]]
    assert dates == [int(clock.now().timestamp()),
                     int(datetime(2030, 5, 1, tzinfo=timezone.utc).timestamp())]
    assert fake.protocol_errors() == []


def test_on_call_hook_runs_before_processing(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    token = fake.token_of(bot)
    seen: list[tuple[Any, ...]] = []
    fake.on_call("getUpdates", lambda c: seen.append(
        (c.method, c.params, c.outcome, fake.state(bot).pending_update_ids)))
    everything: list[str] = []
    fake.on_call("*", lambda c: everything.append(c.method))
    update_id = fake.push_text(bot, make_user(13), "x")
    assert _ids(fake.request(token, "getUpdates", {"timeout": 0, "offset": update_id + 1})) == []
    assert seen == [("getUpdates", {"timeout": 0, "offset": update_id + 1}, "pending",
                     (update_id,))]

    def broken(call: Any) -> None:
        raise AssertionError("falhou de propósito")

    fake.on_call("getMe", broken)
    assert fake.request(token, "getMe", {}).status == 200
    fake.on_call("getMe", None)
    assert fake.request(token, "getMe", {}).status == 200
    assert everything == ["getUpdates", "getMe", "getMe"]
    assert fake.protocol_errors() == [
        "getMe: gancho on_call falhou: AssertionError: falhou de propósito"]
    with pytest.raises(ValueError):
        fake.on_call("sendPhoto", broken)


def test_redeliver_and_restart_sequence(fake: FakeBotApi) -> None:
    """Reentrega com o mesmo `update_id` e conteúdo, uma vez, qualquer que seja o `offset`; a
    numeração recomeça abaixo e acima do `offset` antigo sem perda."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(14)
    ids = [fake.push_text(bot, user, "um"), fake.push_text(bot, user, "dois")]
    original = fake.request(token, "getUpdates", {"timeout": 0}).json()["result"]
    assert _ids(fake.request(token, "getUpdates", {"timeout": 0, "offset": ids[1] + 1})) == []

    fake.redeliver(bot, ids[0])
    assert fake.state(bot).redeliveries == (ids[0],)
    again = fake.request(token, "getUpdates", {"timeout": 0, "offset": ids[1] + 1}).json()
    assert again["result"] == [original[0]]
    assert _ids(fake.request(token, "getUpdates", {"timeout": 0, "offset": ids[1] + 1})) == []

    thread, box = _async(fake, token, "getUpdates", {"timeout": 5, "offset": ids[1] + 1})
    _wait_pending(fake, bot)
    fake.redeliver(bot, ids[1])
    thread.join(2)
    assert box["reply"].json()["result"] == [original[1]]

    fake.restart_sequence(bot, 1000)
    assert fake.push_text(bot, user, "abaixo") == 1000
    assert _ids(fake.request(token, "getUpdates", {"timeout": 0, "offset": ids[1] + 1})) == [1000]
    fake.restart_sequence(bot, 5_000_000)
    assert fake.push_text(bot, user, "acima") == 5_000_000
    assert _ids(fake.request(token, "getUpdates", {"timeout": 0, "offset": 1001})) == [5_000_000]
    assert fake.push_update(bot, {"message": {"message_id": 9, "chat": user.as_private_chat(),
                                              "date": 0, "text": "x"}},
                            update_id=4_999_000) == 4_999_000
    assert fake.state(bot).pending_update_ids == (4_999_000, 5_000_000)

    with pytest.raises(ValueError):
        fake.restart_sequence(bot, 0)
    with pytest.raises(ValueError):
        fake.restart_sequence(bot, 2**31)
    with pytest.raises(ValueError):
        fake.redeliver(bot, 42)
    with pytest.raises(ValueError):
        fake.push_update(bot, {"message": {}, "callback_query": {}})
    with pytest.raises(ValueError):
        fake.push_update(bot, {"message": {}}, update_id=5_000_000)
    assert fake.protocol_errors() == []


def test_injected_updates_have_the_official_shapes(fake: FakeBotApi, clock: FakeClock) -> None:
    """Formas oficiais da Bot API: entidade `bot_command` no `/start`, `callback_query` e
    `my_chat_member` de `member` para `kicked` com `until_date: 0`."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    username = fake.state(bot).username
    user = TelegramUser(5_199_999_999, "Fulana", "de Tal", "fulana_tal")
    assert user.as_user() == {"id": 5_199_999_999, "is_bot": False, "first_name": "Fulana",
                              "last_name": "de Tal", "username": "fulana_tal",
                              "language_code": "pt-br"}
    assert user.as_private_chat() == {"id": 5_199_999_999, "type": "private",
                                      "first_name": "Fulana", "last_name": "de Tal",
                                      "username": "fulana_tal"}
    assert make_user(3) == TelegramUser(5_100_000_003, "Pessoa 3")
    now = int(clock.now().timestamp())
    fake.push_start(bot, user, "ABCdef_ghi-jkl012345678")
    fake.push_start(bot, user, "c0d1go", username=username)
    fake.push_start(bot, user)
    fake.push_text(bot, user, "/ajuda agora", bot_command=True)
    fake.push_text(bot, user, "/ajuda")
    group = {"id": -1001234567890, "type": "supergroup", "title": "Turma"}
    fake.push_text(bot, user, "no grupo", chat=group, extra={"reply_to_message": {
        "message_id": 1, "from": user.as_user(), "chat": group, "date": now, "text": "a"}})
    fake.push_callback(bot, user, "resposta:1")
    fake.push_my_chat_member(bot, user)
    updates = fake.request(token, "getUpdates", {"timeout": 0}).json()["result"]
    messages = [u["message"] for u in updates[:6]]
    assert [(m["text"], m.get("entities")) for m in messages] == [
        ("/start ABCdef_ghi-jkl012345678", [{"offset": 0, "length": 6, "type": "bot_command"}]),
        (f"/start@{username} c0d1go",
         [{"offset": 0, "length": 7 + len(username), "type": "bot_command"}]),
        ("/start", [{"offset": 0, "length": 6, "type": "bot_command"}]),
        ("/ajuda agora", [{"offset": 0, "length": 6, "type": "bot_command"}]),
        ("/ajuda", None),
        ("no grupo", None),
    ]
    assert [m["message_id"] for m in messages] == [1, 2, 3, 4, 5, 1]
    assert all(m["from"] == user.as_user() and m["date"] == now for m in messages)
    assert messages[5]["chat"] == group and messages[5]["reply_to_message"]["text"] == "a"
    bot_user = {"id": bot, "is_bot": True, "first_name": "Bot de teste", "username": username}
    callback = updates[6]["callback_query"]
    assert set(callback) == {"id", "from", "message", "chat_instance", "data"}
    assert (callback["from"], callback["data"]) == (user.as_user(), "resposta:1")
    assert callback["message"] == {"message_id": 6, "from": bot_user,
                                   "chat": user.as_private_chat(), "date": now,
                                   "text": "Mensagem do bot"}
    assert updates[7]["my_chat_member"] == {
        "chat": user.as_private_chat(), "from": user.as_user(), "date": now,
        "old_chat_member": {"user": bot_user, "status": "member"},
        "new_chat_member": {"user": bot_user, "status": "kicked", "until_date": 0}}
    assert fake.state(bot).known_chats == {user.user_id, group["id"]}
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Ciclo de vida, sessão, sigilo e silêncio
# ---------------------------------------------------------------------------------------------


def test_reset_ends_pending_polls_and_forgets_everything(server: FakeBotApi,
                                                         clock: FakeClock) -> None:
    server.reset(clock=clock)
    bot = server.add_bot()
    token = server.token_of(bot)
    thread, box = _async(server, token, "getUpdates", {"timeout": 5})
    _wait_pending(server, bot)
    server.fail("getMe", status=500, times=None)
    server.on_call("getMe", lambda c: None)
    server.request(token, "sendPhoto", {})
    assert server.protocol_errors() != []
    server.reset(clock=clock)
    thread.join(2)
    assert box["reply"].json() == _envelope("unauthorized")
    assert server.calls() == [] and server.protocol_errors() == []
    assert server.pending_polls() == [] and server.sent_messages() == []
    assert server.all_tokens() == []
    with pytest.raises(KeyError):
        server.token_of(bot)
    assert server.request(token, "getMe", {}).json() == _envelope("unauthorized")
    assert [(c.token_state, c.bot_id) for c in server.calls()] == [("unknown", bot)]
    fresh = server.add_bot()
    assert fresh != bot  # ids de bot não se repetem na sessão
    assert server.request(server.token_of(fresh), "getMe", {}).status == 200
    assert server.all_tokens() == [server.token_of(fresh)]
    server.reset(clock=clock)


def test_close_ends_pending_polls_and_is_idempotent() -> None:
    fake = FakeBotApi()
    bot = fake.add_bot()
    token = fake.token_of(bot)
    thread, box = _async(fake, token, "getUpdates", {"timeout": 5})
    _wait_pending(fake, bot)
    fake.close()
    thread.join(2)
    assert (box["reply"].status, box["reply"].json()) == (500, _envelope("server_restart"))
    assert fake.closed is True
    fake.close()
    with pytest.raises(OSError):
        fake.request(token, "getMe", {}, timeout=2)


def test_session_fake_is_one_live_server() -> None:
    first = session_fake()
    assert session_fake() is first and first.closed is False
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]{2,5}", first.base_url)
    first.reset()
    assert first.request("123:desconhecido", "getMe", {}).json() == _envelope("unauthorized")
    first.reset()


def test_wait_for_returns_the_value_or_fails_with_transcript(fake: FakeBotApi) -> None:
    bot = fake.add_bot()
    fake.request(fake.token_of(bot), "getMe", {})
    assert fake.wait_for(lambda: len(fake.calls())) == 1
    with pytest.raises(AssertionError, match=r"wait_for: condição não satisfeita em 0\.05 s\n#1 "
                                             r"getMe"):
        fake.wait_for(lambda: False, timeout=0.05)


def test_records_never_contain_the_token(fake: FakeBotApi) -> None:
    """Registro de chamadas, transcrição, erros de protocolo e estado sem o token (nem a parte
    secreta); o token que aparece no lugar do método é trocado por `[token]`."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    secret = token.split(":", 1)[1]
    user = make_user(15)
    fake.push_text(bot, user, "oi")
    fake.request(token, "getMe", {})
    fake.request(token, "getUpdates", {"timeout": 0, "limit": 0})
    fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": "olá"})
    fake.request(token, "naoExiste", {})
    fake.request(token, "", {}, path=f"/bot/{token}/getMe")
    fake.fail("getMe", error="unauthorized")
    fake.request(token, "getMe", {})
    texts = [repr(fake.calls()), fake.transcript(), repr(fake.protocol_errors()),
             repr(fake.state(bot)), repr(fake.sent_messages()), repr(fake.pending_polls())]
    for text in texts:
        assert token not in text and secret not in text
    assert "[token]/getMe: método desconhecido ou fora do dublê" in fake.protocol_errors()
    assert [c.token_state for c in fake.calls()][-2] == "malformed"


def test_silent_on_fd1_and_fd2(capfd: pytest.CaptureFixture[str],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Nada em stdout nem stderr, em nenhum caminho (o `http.server` padrão escreveria a linha do
    pedido, com o token, em stderr)."""
    capfd.readouterr()
    with FakeBotApi(clock=FakeClock()) as fake:
        bot = fake.add_bot()
        token = fake.token_of(bot)
        fake.request(token, "getMe", {})
        fake.request("123:desconhecido", "getMe", {})
        fake.request(token, "", None, path="/favicon.ico", http_method="GET")
        fake.request(token, "naoExiste", {})
        fake.request(token, "getMe", body=b"{")
        for raw in (b"LIXO\r\n\r\n", b"POST /bot" + token.encode() + b"/getMe HTTP/1.1\r\n",
                    b"POST /bot" + token.encode() + b"/getMe HTTP/1.1\r\nContent-Length: x\r\n\r\n",
                    b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n"):
            with socket.create_connection(("127.0.0.1", fake.port), timeout=5) as sock:
                sock.sendall(raw)
                sock.shutdown(socket.SHUT_WR)
                try:
                    while sock.recv(65536):
                        pass
                except OSError:
                    pass
        sock = _raw_poll(fake, token, {"timeout": 5})
        _wait_pending(fake, bot)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _linger_zero())
        sock.close()
        fake.wait_for(lambda: fake.calls("getUpdates")[-1].outcome == "abandoned")
        fake.fail_network("getMe", kind="reset")
        with pytest.raises((OSError, http.client.HTTPException)):
            fake.request(token, "getMe", {})
        fake.fail_network("getMe", kind="hang")
        with pytest.raises(TimeoutError):
            fake.request(token, "getMe", {}, timeout=0.2)
        fake.fail_network("getMe", kind="not_json")
        fake.request(token, "getMe", {})
        fake.on_call("getMe", lambda c: 1 / 0)
        fake.fail("getMe", status=500, when=lambda c: 1 / 0)
        fake.request(token, "getMe", {})
        fake.on_call("getMe", None)
        monkeypatch.setattr(fake, "_m_getMe", lambda *args: 1 / 0)
        assert fake.request(token, "getMe", {}).status == 500
        thread, box = _async(fake, token, "getUpdates", {"timeout": 5})
        _wait_pending(fake, bot)
        problems = fake.protocol_errors()
    thread.join(2)
    assert box["reply"].status == 500
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    assert "getMe: erro interno do dublê: ZeroDivisionError: division by zero" in problems
    assert "getMe: gancho on_call falhou: ZeroDivisionError: division by zero" in problems
    assert "getMe: predicado de fail falhou: ZeroDivisionError: division by zero" in problems
