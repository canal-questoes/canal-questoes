"""Adaptador HTTP da Bot API.


`cq_server.adapters.telegram.HttpTelegramBotApi` fala com a **Bot API falsa** (`tests/helpers/
bot_api.py`, servidor HTTP real em 127.0.0.1), que registra cada pedido (`BotApiCall`, sem o token)
e acusa em `protocol_errors()` qualquer desvio (consulta na URL, corpo que não é JSON, parâmetro não
documentado, limite fora da documentação, `drop_pending_updates=true`); a fixture `bot_api` exige
`protocol_errors() == []` no fim de cada teste.

O que se confere aqui:
- toda chamada é `POST {base_url}/bot{token}/{método}` com corpo JSON UTF-8 e nada na consulta,
  com os parâmetros exatos de cada método (os da porta do Telegram);
- a classificação de cada linha da tabela de erros do Telegram (inclusive o 403 genérico, que não é bloqueio);
- os tempos (`poll_timeout_seconds + timeout_seconds` no `getUpdates`, `timeout_seconds` nos demais)
  e o `abort()`, que interrompe um `getUpdates` preso em até 2 s;
- o sigilo do token: exceções (inclusive a cadeia), `repr`, e os logs do transporte, com
  controle positivo; e que nenhum `httpx2.Client` é instanciado (ele registra a URL com o token).

As respostas com HTTP 200 cujo corpo decide o resultado (JSON sem `ok`, lista, HTML e `ok: false`)
usam o servidor mínimo `_StubServer` deste arquivo, que devolve um corpo fixo e guarda o pedido: o
dublê sempre manda `ok` e usa o `error_code` como status HTTP.

No esqueleto, `HttpTelegramBotApi.__init__` e a fábrica de `http_factory` lançam
`NotImplementedError`; todo teste constrói o adaptador.
"""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
import socket
import threading
import traceback
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest

from cq_server.adapters.telegram import HttpTelegramBotApi, http_factory
from cq_server.ports.telegram import (
    Aborted,
    BotCommand,
    BotIdentity,
    ChatUnreachable,
    ConcurrentConsumer,
    MinimalUpdate,
    NotABot,
    RequestRejected,
    RetryAfter,
    TelegramError,
    TelegramUnavailable,
    TokenRejected,
    WebhookActive,
)
from tests.helpers.bot_api import (
    ERRORS,
    HTML_BAD_GATEWAY,
    BotApiCall,
    FakeBotApi,
    SentMessage,
    make_user,
    monotonic,
)
from tests.helpers.canary import find_secrets
from tests.helpers.clock import FakeClock
from tests.helpers.logs import capture_transport_logs
from tests.helpers.secrets import all_secrets, bot_secrets, remember_secret

# A lista exata de toda `getUpdates`.
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
# Os comandos do perfil, como a porta os recebe.
COMMANDS = [BotCommand("start", "Começar ou usar um link de convite"),
            BotCommand("ajuda", "Ver como usar este bot")]
COMMANDS_PARAM = [{"command": "start", "description": "Começar ou usar um link de convite"},
                  {"command": "ajuda", "description": "Ver como usar este bot"}]
# Os seis métodos da porta, na ordem em que `_invoke` os chama.
METHODS = ("getMe", "setMyDescription", "setMyCommands", "deleteWebhook", "getUpdates",
           "sendMessage")
# Todos os erros da porta do Telegram.
ALL_ERRORS = frozenset({TokenRejected, RetryAfter, TelegramUnavailable, WebhookActive,
                        ConcurrentConsumer, ChatUnreachable, RequestRejected, NotABot, Aborted})
# Prazo do `abort()` nos testes (em até 2 s).
ABORT_SECONDS = 2.0


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


class _Apis:
    """Constrói adaptadores (por padrão contra a Bot API falsa) e fecha todos no fim do teste."""

    def __init__(self, fake: FakeBotApi) -> None:
        self._fake = fake
        self.created: list[Any] = []

    def __call__(self, token: str, *, base_url: str | None = None, poll_timeout_seconds: int = 1,
                 timeout_seconds: float = 2.0) -> HttpTelegramBotApi:
        url = self._fake.base_url if base_url is None else base_url
        api = HttpTelegramBotApi(token, base_url=url, poll_timeout_seconds=poll_timeout_seconds,
                                 timeout_seconds=timeout_seconds)
        self.created.append(api)
        return api

    def track(self, api: Any) -> Any:
        self.created.append(api)
        return api


@pytest.fixture
def make_api(bot_api: FakeBotApi) -> Iterator[_Apis]:
    apis = _Apis(bot_api)
    yield apis
    bot_api.release_hangs()
    for api in apis.created:
        api.close()


class _Background:
    """Uma chamada ao adaptador numa thread (para `abort()` e conflitos): guarda o resultado, a
    exceção e o instante do fim (`bot_api.monotonic()`)."""

    def __init__(self, call: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.ended: float | None = None
        self._thread = threading.Thread(target=self._run, args=(call,),
                                        name="cq-test-adapter-call", daemon=True)
        self._thread.start()

    def _run(self, call: Callable[[], Any]) -> None:
        try:
            self.result = call()
        except BaseException as exc:  # noqa: BLE001 - guardada para o teste conferir
            self.error = exc
        finally:
            self.ended = monotonic()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _invoke(api: Any, method: str, chat_id: int) -> Any:
    """Chama o método da porta que corresponde ao método da Bot API."""
    if method == "getMe":
        return api.get_me()
    if method == "setMyDescription":
        return api.set_my_description("Descrição do bot")
    if method == "setMyCommands":
        return api.set_my_commands(COMMANDS)
    if method == "deleteWebhook":
        return api.delete_webhook()
    if method == "getUpdates":
        return api.get_updates(offset=None)
    if method == "sendMessage":
        return api.send_message(chat_id, "Olá")
    raise AssertionError(f"método desconhecido: {method}")


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _charset_is_utf8(content_type: str | None) -> bool:
    match = re.search(r"charset=([^;\s]+)", content_type or "", re.IGNORECASE)
    return match is None or match.group(1).strip("\"'").lower() in ("utf-8", "utf8")


def _settled(fake: FakeBotApi) -> list[BotApiCall]:
    """As chamadas do dublê, depois que todas terminaram (o dublê vê o fim de uma conexão presa
    quando o cliente a fecha)."""
    def ready() -> list[BotApiCall] | None:
        calls = fake.calls()
        return calls if calls and all(c.ended is not None for c in calls) else None

    return fake.wait_for(ready, timeout=5)


def _exception_texts(exc: BaseException) -> list[str]:
    """`str`, `repr`, `args`, notas e traceback formatado da exceção e de toda a cadeia
    (`__cause__` e `__context__`)."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts += [str(current), repr(current), repr(current.args),
                  "".join(traceback.format_exception(current))]
        texts += [str(note) for note in getattr(current, "__notes__", ())]
        pending += [current.__cause__, current.__context__]
    return texts


def _closed_port_url() -> str:
    """URL de uma porta local sem servidor (conexão recusada)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _local_token() -> str:
    """Token no formato da Bot API para o servidor mínimo (registrado nas varreduras)."""
    secret = secrets.token_urlsafe(27)[:35]
    token = f"6100009999:{secret}"
    remember_secret(token)
    remember_secret(secret)
    return token


def _raw_message(call: BotApiCall, index: int = 0) -> dict[str, Any]:
    """O `message` cru que o dublê entregou numa resposta de `getUpdates`."""
    return call.result[index]["message"]


# ---------------------------------------------------------------------------------------------
# Forma dos pedidos e parâmetros exatos
# ---------------------------------------------------------------------------------------------


def test_every_method_posts_json_with_exact_params(bot_api: FakeBotApi, clock: FakeClock,
                                                   make_api: _Apis) -> None:
    """Os parâmetros de cada método, como manda a porta do Telegram:
    cada método faz **uma** chamada `POST` com corpo JSON UTF-8 e nada na
    consulta, com os parâmetros exatos: `getMe` sem parâmetros; `setMyDescription` só `description`;
    `setMyCommands` só `commands` (sem `scope` nem `language_code`); `deleteWebhook` com
    `drop_pending_updates=false`; `getUpdates` com `timeout` = `poll_timeout_seconds`, `limit=100` e
    `allowed_updates` exatos, sem `offset`; `sendMessage` só `{chat_id, text}` (sem `parse_mode`).
    Os retornos: `BotIdentity` do `getMe`, `None` nos `set*`/`deleteWebhook`, a lista de
    `MinimalUpdate` e o `message_id` inteiro do `sendMessage`; texto não ASCII chega igual."""
    bot = bot_api.add_bot()
    user = make_user(1)
    update_id = bot_api.push_text(bot, user, "olá")
    api = make_api(bot_api.token_of(bot), poll_timeout_seconds=1, timeout_seconds=2)

    identity = api.get_me()
    description = "Bot do oferecimento Cálculo I (2026 · 2º semestre). Toque no link 🙂"
    assert api.set_my_description(description) is None
    assert api.set_my_commands(COMMANDS) is None
    assert api.delete_webhook() is None
    updates = api.get_updates(offset=None)
    text = "Olá, ação!\nSegunda linha 🙂 — fim"
    message_id = api.send_message(user.user_id, text)

    calls = bot_api.calls()
    assert [c.method for c in calls] == list(METHODS)
    for call in calls:
        assert call.http_method == "POST"
        assert _media_type(call.content_type) == "application/json"
        assert _charset_is_utf8(call.content_type)
        assert call.has_query is False
        assert (call.bot_id, call.token_state, call.token_version) == (bot, "valid", 1)
        assert (call.outcome, call.status) == ("ok", 200)
    assert [c.params for c in calls] == [
        {},
        {"description": description},
        {"commands": COMMANDS_PARAM},
        {"drop_pending_updates": False},
        {"timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES},
        {"chat_id": user.user_id, "text": text},
    ]
    assert type(calls[3].params["drop_pending_updates"]) is bool
    assert type(calls[4].params["timeout"]) is int and type(calls[4].params["limit"]) is int
    assert type(calls[5].params["chat_id"]) is int
    assert bot_api.protocol_errors() == []

    assert identity == BotIdentity(telegram_bot_id=bot, username=bot_api.state(bot).username)
    raw = _raw_message(calls[4])
    assert type(updates) is list
    assert updates == [MinimalUpdate(update_id=update_id, kind="message", chat_id=user.user_id,
                                     chat_type="private", sender_id=user.user_id, text="olá",
                                     command=None,
                                     details={"message_id": raw["message_id"],
                                              "date": int(clock.now().timestamp())})]
    sent = bot_api.sent_messages(bot)
    assert sent == [SentMessage(bot_id=bot, chat_id=user.user_id, message_id=sent[0].message_id,
                                text=text, clock_at=clock.now(), call_seq=calls[5].seq)]
    assert message_id == sent[0].message_id and type(message_id) is int
    state = bot_api.state(bot)
    assert state.description == description
    assert state.commands == tuple(COMMANDS_PARAM)


def test_get_updates_sends_the_offset_and_returns_minimal_updates(
        bot_api: FakeBotApi, clock: FakeClock, make_api: _Apis) -> None:
    """sem `offset` na 1ª chamada; com `offset`, o
    parâmetro vai como inteiro junto de `timeout`, `limit` e `allowed_updates` exatos; o adaptador
    devolve os updates já minimizados (`message` com e sem comando, `callback_query`), na ordem, e
    não confirma nada sozinho (o que não foi pedido com `offset` fica pendente no dublê)."""
    bot = bot_api.add_bot()
    user = make_user(2)
    first = bot_api.push_text(bot, user, "primeira")
    clock.advance(60)
    second = bot_api.push_start(bot, user, "abcdefghijklmnopqrstuv")
    api = make_api(bot_api.token_of(bot))

    batch = api.get_updates(offset=None)
    clock.advance(60)
    third = bot_api.push_callback(bot, user, "resposta:b")
    later = api.get_updates(offset=second + 1)

    polls = bot_api.calls("getUpdates")
    assert [c.params for c in polls] == [
        {"timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES},
        {"offset": second + 1, "timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES},
    ]
    assert type(polls[1].params["offset"]) is int
    raw_first, raw_second = _raw_message(polls[0], 0), _raw_message(polls[0], 1)
    assert batch == [
        MinimalUpdate(update_id=first, kind="message", chat_id=user.user_id, chat_type="private",
                      sender_id=user.user_id, text="primeira", command=None,
                      details={"message_id": raw_first["message_id"], "date": raw_first["date"]}),
        MinimalUpdate(update_id=second, kind="message", chat_id=user.user_id,
                      chat_type="private", sender_id=user.user_id,
                      text="/start abcdefghijklmnopqrstuv", command="/start",
                      details={"message_id": raw_second["message_id"],
                               "date": raw_second["date"]}),
    ]
    assert raw_second["date"] == raw_first["date"] + 60
    query = polls[1].result[0]["callback_query"]
    assert later == [MinimalUpdate(
        update_id=third, kind="callback_query", chat_id=user.user_id, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": query["id"], "data": "resposta:b",
                 "message_id": query["message"]["message_id"]})]
    assert bot_api.state(bot).pending_update_ids == (third,)


def test_delete_webhook_removes_the_webhook_without_dropping_updates(
        bot_api: FakeBotApi, make_api: _Apis) -> None:
    """Remover o webhook sem descartar as mensagens pendentes:
    com webhook ativo e um update pendente, `delete_webhook()` manda `drop_pending_updates=false`
    (booleano), o webhook sai, a pendência fica e é entregue no `getUpdates` seguinte."""
    bot = bot_api.add_bot()
    user = make_user(3)
    pending = bot_api.push_text(bot, user, "pendente")
    bot_api.set_webhook(bot)
    api = make_api(bot_api.token_of(bot))

    assert api.delete_webhook() is None
    [call] = bot_api.calls("deleteWebhook")
    assert call.params == {"drop_pending_updates": False}
    assert type(call.params["drop_pending_updates"]) is bool
    assert (call.outcome, call.status, call.result) == ("ok", 200, True)
    state = bot_api.state(bot)
    assert state.webhook_url is None
    assert state.pending_update_ids == (pending,)

    [update] = api.get_updates(offset=None)
    assert (update.update_id, update.kind, update.text) == (pending, "message", "pendente")


def test_get_me_returns_the_identity_and_ignores_new_fields(bot_api: FakeBotApi,
                                                           make_api: _Apis) -> None:
    """O `getMe` falso cobre campos extras (aceita) e o `id`
    8123456789: o `getMe` devolve `BotIdentity` com o `id` (maior que 2^32, como `int`) e o
    `username`, e aceita campos que a Bot API ainda não tem."""
    bot = bot_api.add_bot("cq_disciplina_mc102_bot", bot_id=8_123_456_789,
                          extra={"has_main_web_app": False, "can_connect_to_business": False,
                                 "campo_futuro": {"aninhado": [1, 2]}})
    api = make_api(bot_api.token_of(bot))
    identity = api.get_me()
    assert identity == BotIdentity(telegram_bot_id=8_123_456_789,
                                   username="cq_disciplina_mc102_bot")
    assert type(identity) is BotIdentity and type(identity.telegram_bot_id) is int
    [call] = bot_api.calls("getMe")
    assert (call.params, call.outcome, call.status) == ({}, "ok", 200)


def test_get_me_without_username_or_not_a_bot_raises_not_a_bot(bot_api: FakeBotApi,
                                                               make_api: _Apis) -> None:
    """Confirmar que o token é de um bot e obter o nome de usuário
    (`NotABot`): `getMe` sem `username`, ou com `is_bot` falso, dá `NotABot` (uma chamada cada)."""
    without_username = bot_api.add_bot(None)
    not_a_bot = bot_api.add_bot("cq_pessoa_comum_bot", is_bot=False)
    for bot in (without_username, not_a_bot):
        api = make_api(bot_api.token_of(bot))
        with pytest.raises(TelegramError) as info:
            api.get_me()
        assert type(info.value) is NotABot
    assert [(c.method, c.bot_id, c.outcome, c.status) for c in bot_api.calls()] == [
        ("getMe", without_username, "ok", 200), ("getMe", not_a_bot, "ok", 200)]


# ---------------------------------------------------------------------------------------------
# Classificação
# ---------------------------------------------------------------------------------------------


def _fail(method: str, **kwargs: Any) -> Callable[..., None]:
    def arrange(fake: FakeBotApi, bot: int, user: Any) -> None:
        fake.fail(method, **kwargs)
    return arrange


def _fail_network(method: str, kind: str) -> Callable[..., None]:
    def arrange(fake: FakeBotApi, bot: int, user: Any) -> None:
        fake.fail_network(method, kind=kind)
    return arrange


def _block(*, deactivated: bool = False) -> Callable[..., None]:
    def arrange(fake: FakeBotApi, bot: int, user: Any) -> None:
        fake.block_chat(bot, user, deactivated=deactivated)
    return arrange


def _webhook(fake: FakeBotApi, bot: int, user: Any) -> None:
    fake.set_webhook(bot)


def _unknown_chat(fake: FakeBotApi, bot: int, user: Any) -> int:
    return make_user(99).user_id


CLASSIFICATION = [
    pytest.param("getMe", _fail("getMe", error="unauthorized"), TokenRejected, ("error", 401),
                 id="401-unauthorized"),
    pytest.param("setMyDescription", _fail("setMyDescription", error="invalid_token"),
                 TokenRejected, ("error", 401), id="401-invalid-token"),
    pytest.param("sendMessage", _block(), ChatUnreachable, ("error", 403),
                 id="403-blocked-by-the-user"),
    pytest.param("sendMessage", _block(deactivated=True), ChatUnreachable, ("error", 403),
                 id="403-user-is-deactivated"),
    pytest.param("sendMessage", _fail("sendMessage", error="kicked"), RequestRejected,
                 ("error", 403), id="403-kicked-nao-e-bloqueio"),
    pytest.param("setMyCommands", _fail("setMyCommands", status=403), RequestRejected,
                 ("error", 403), id="403-generico"),
    pytest.param("sendMessage", _unknown_chat, RequestRejected, ("error", 400),
                 id="400-chat-not-found"),
    pytest.param("setMyDescription",
                 _fail("setMyDescription", status=400,
                       description="Bad Request: description is too long"),
                 RequestRejected, ("error", 400), id="400-set-my-description"),
    pytest.param("getUpdates",
                 _fail("getUpdates", status=400, description="Bad Request: wrong offset"),
                 RequestRejected, ("error", 400), id="400-get-updates"),
    pytest.param("getMe", _fail("getMe", error="not_found"), RequestRejected, ("error", 404),
                 id="404-not-found"),
    pytest.param("deleteWebhook", _fail("deleteWebhook", error="method_not_found"),
                 RequestRejected, ("error", 404), id="404-method-not-found"),
    pytest.param("getMe", _fail("getMe", status=418, description="I'm a teapot"),
                 RequestRejected, ("error", 418), id="418-desconhecido"),
    pytest.param("getMe", _fail("getMe", error="forbidden_token"), RequestRejected,
                 ("error", 421), id="421-desconhecido"),
    pytest.param("getUpdates", _webhook, WebhookActive, ("error", 409), id="409-webhook"),
    pytest.param("getUpdates", _fail("getUpdates", error="conflict_poll"), ConcurrentConsumer,
                 ("error", 409), id="409-outro-getupdates"),
    pytest.param("getUpdates", _fail("getUpdates", retry_after=7), RetryAfter, ("error", 429),
                 id="429-com-retry-after"),
    pytest.param("sendMessage", _fail("sendMessage", status=429), TelegramUnavailable,
                 ("error", 429), id="429-sem-retry-after"),
    pytest.param("getMe", _fail("getMe", error="server_restart"), TelegramUnavailable,
                 ("error", 500), id="500"),
    pytest.param("setMyCommands", _fail("setMyCommands", status=502), TelegramUnavailable,
                 ("error", 502), id="502-json"),
    pytest.param("deleteWebhook", _fail("deleteWebhook", status=503), TelegramUnavailable,
                 ("error", 503), id="503-json"),
    pytest.param("getUpdates", _fail_network("getUpdates", "not_json"), TelegramUnavailable,
                 ("error", 502), id="502-html"),
    pytest.param("sendMessage", _fail_network("sendMessage", "reset"), TelegramUnavailable,
                 ("reset", None), id="conexao-cortada"),
    pytest.param("getMe", _fail_network("getMe", "hang"), TelegramUnavailable, ("hang", None),
                 id="tempo-esgotado"),
]


@pytest.mark.parametrize("method, arrange, expected, fake_result", CLASSIFICATION)
def test_each_telegram_response_maps_to_exactly_one_error(
        bot_api: FakeBotApi, make_api: _Apis, method: str, arrange: Callable[..., int | None],
        expected: type[TelegramError], fake_result: tuple[str, int | None]) -> None:
    """cada linha da
    classificação dá exatamente o tipo esperado, depois de **uma** chamada (o adaptador não repete
    sozinho): 401 → `TokenRejected`; 403 `blocked by the user` ou `user is deactivated` →
    `ChatUnreachable`; outro 403 (inclusive `kicked`), 400, 404 e códigos desconhecidos (418, 421) →
    `RequestRejected`; 409 de webhook → `WebhookActive`; outro 409 → `ConcurrentConsumer`; 429 com
    `retry_after` → `RetryAfter`; 429 sem `retry_after`, 5xx, HTML de proxy, conexão cortada e tempo
    esgotado → `TelegramUnavailable`."""
    bot = bot_api.add_bot()
    user = make_user(40)
    bot_api.push_text(bot, user, "oi")
    chat_id = arrange(bot_api, bot, user) or user.user_id
    api = make_api(bot_api.token_of(bot), poll_timeout_seconds=1, timeout_seconds=0.5)
    watchdog = threading.Timer(6.0, bot_api.release_hangs)
    watchdog.start()
    try:
        with pytest.raises(TelegramError) as info:
            _invoke(api, method, chat_id)
    finally:
        watchdog.cancel()
        bot_api.release_hangs()
    assert type(info.value) is expected
    calls = _settled(bot_api)
    assert [(c.method, c.outcome, c.status) for c in calls] == [(method, *fake_result)]


def _stub_json(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize("status, content_type, payload, expected", [
    pytest.param(200, "application/json",
                 _stub_json({"result": {"id": 8_123_456_789, "is_bot": True, "first_name": "Bot",
                                        "username": "cq_stub_bot"}}),
                 TelegramUnavailable, id="json-sem-ok"),
    pytest.param(200, "application/json", b"[]", TelegramUnavailable, id="json-que-nao-e-objeto"),
    pytest.param(200, "text/html", HTML_BAD_GATEWAY, TelegramUnavailable, id="html-com-http-200"),
    pytest.param(200, "application/json",
                 _stub_json({"ok": False, "error_code": 401, "description": "Unauthorized"}),
                 TokenRejected, id="ok-falso-com-http-200"),
])
def test_the_body_decides_not_the_http_status(stub_server: Callable[..., _StubServer],
                                              make_api: _Apis, status: int, content_type: str,
                                              payload: bytes,
                                              expected: type[TelegramError]) -> None:
    """Corpo que não é JSON (ou sem `ok`) → `TelegramUnavailable`; o adaptador
    decide pelo campo `ok` do corpo, não pelo status (`ok=false` → erro tipado
    pelo `error_code`): com HTTP 200, um corpo sem `ok` (objeto sem o campo, lista ou HTML) dá
    `TelegramUnavailable`, e `ok: false` com `error_code: 401` dá `TokenRejected`. O pedido chega
    como `POST /bot<token>/getMe` com corpo JSON `{}` e sem consulta, e a exceção não tem o
    token."""
    token = _local_token()
    server = stub_server(status, content_type, payload)
    api = make_api(token, base_url=server.base_url)
    with pytest.raises(TelegramError) as info:
        api.get_me()
    assert type(info.value) is expected
    [(verb, path, request_type, body)] = server.requests
    assert (verb, path) == ("POST", f"/bot{token}/getMe")
    assert _media_type(request_type) == "application/json"
    assert json.loads(body.decode("utf-8")) == {}
    for text in _exception_texts(info.value):
        assert find_secrets(text, all_secrets()) == []


def test_revoked_token_raises_token_rejected_on_every_method(bot_api: FakeBotApi,
                                                            make_api: _Apis) -> None:
    """Recusar o token em qualquer chamada: revogado no meio de um
    long poll, o `getUpdates` em espera sai com `TokenRejected`; depois, cada um dos seis métodos
    também dá `TokenRejected` (401 `Unauthorized` no dublê, com o token já revogado)."""
    bot = bot_api.add_bot()
    user = make_user(41)
    first = bot_api.push_text(bot, user, "oi")
    api = make_api(bot_api.token_of(bot), poll_timeout_seconds=10, timeout_seconds=2)
    [update] = api.get_updates(offset=None)
    assert update.update_id == first

    waiting = _Background(lambda: api.get_updates(offset=first + 1))
    bot_api.wait_for(lambda: bot_api.pending_polls(bot))
    bot_api.revoke(bot)
    assert waiting.join(5)
    assert type(waiting.error) is TokenRejected

    for method in METHODS:
        with pytest.raises(TelegramError) as info:
            _invoke(api, method, user.user_id)
        assert type(info.value) is TokenRejected
    calls = bot_api.calls()
    assert [(c.method, c.token_state, c.outcome, c.status, c.description) for c in calls[1:]] == [
        ("getUpdates", "valid", "error", 401, "Unauthorized"),
        *[(method, "revoked", "error", 401, "Unauthorized") for method in METHODS]]


def test_retry_after_carries_the_seconds(bot_api: FakeBotApi, make_api: _Apis) -> None:
    """Esperar pelo menos o tempo indicado (`RetryAfter(seconds: int)`;
    o `retry_after` vale em todo método): o 429 com `parameters.retry_after` dá
    `RetryAfter` com `seconds` igual ao valor do Telegram, como `int`, em qualquer método."""
    bot = bot_api.add_bot()
    user = make_user(42)
    bot_api.push_text(bot, user, "oi")
    api = make_api(bot_api.token_of(bot))
    cases = (("getMe", 1), ("setMyDescription", 30), ("getUpdates", 7), ("sendMessage", 3600))
    for method, seconds in cases:
        bot_api.fail(method, retry_after=seconds)
        with pytest.raises(TelegramError) as info:
            _invoke(api, method, user.user_id)
        assert type(info.value) is RetryAfter
        assert info.value.seconds == seconds and type(info.value.seconds) is int
    assert [(c.method, c.status, c.retry_after) for c in bot_api.calls()] == [
        (method, 429, seconds) for method, seconds in cases]


def test_get_updates_terminated_by_another_consumer_raises_concurrent_consumer(
        bot_api: FakeBotApi, make_api: _Apis) -> None:
    """Outro 409 → `ConcurrentConsumer`: com o
    `getUpdates` do adaptador em espera, um consumidor externo (o teste, com o mesmo token) faz
    outro long poll; o dublê encerra o mais antigo com 409 `terminated by other getUpdates
    request`, e o adaptador levanta `ConcurrentConsumer`."""
    bot = bot_api.add_bot()
    token = bot_api.token_of(bot)
    api = make_api(token, poll_timeout_seconds=10, timeout_seconds=2)
    waiting = _Background(lambda: api.get_updates(offset=None))
    bot_api.wait_for(lambda: bot_api.pending_polls(bot))

    reply = bot_api.request(token, "getUpdates", {"timeout": 1})
    assert (reply.status, reply.json()) == (200, {"ok": True, "result": []})
    assert waiting.join(5)
    assert type(waiting.error) is ConcurrentConsumer
    first, second = bot_api.calls("getUpdates", bot_id=bot)
    [(ended, by)] = bot_api.conflicts(bot)
    assert (ended.seq, by.seq) == (first.seq, second.seq)
    assert (ended.outcome, ended.status, ended.description) == (
        "error", 409, ERRORS["conflict_poll"][1])


# ---------------------------------------------------------------------------------------------
# Tempos e abort()
# ---------------------------------------------------------------------------------------------


def test_read_timeouts_follow_the_constructor(bot_api: FakeBotApi, make_api: _Apis) -> None:
    """O timeout de leitura do `getUpdates` é `poll_timeout_seconds +
    timeout_seconds` (os demais métodos com `timeout_seconds`; e
    tempo esgotado → `TelegramUnavailable`): com `poll_timeout_seconds=1` e
    `timeout_seconds=0.5`, um long poll vazio de 1 s termina com `[]` (não é cortado em 0,5 s); um
    `getMe` sem resposta dá `TelegramUnavailable` perto de 0,5 s; um `getUpdates` sem resposta dá
    `TelegramUnavailable` só depois de 1,5 s."""
    bot = bot_api.add_bot()
    api = make_api(bot_api.token_of(bot), poll_timeout_seconds=1, timeout_seconds=0.5)

    assert api.get_updates(offset=None) == []
    [poll] = bot_api.calls("getUpdates")
    assert (poll.outcome, poll.status, poll.result, poll.params["timeout"]) == ("ok", 200, [], 1)

    watchdog = threading.Timer(8.0, bot_api.release_hangs)
    watchdog.start()
    try:
        bot_api.fail_network("getMe", kind="hang")
        started = monotonic()
        with pytest.raises(TelegramError) as info:
            api.get_me()
        short = monotonic() - started
        assert type(info.value) is TelegramUnavailable

        bot_api.fail_network("getUpdates", kind="hang")
        started = monotonic()
        with pytest.raises(TelegramError) as info:
            api.get_updates(offset=None)
        slow = monotonic() - started
        assert type(info.value) is TelegramUnavailable
    finally:
        watchdog.cancel()
        bot_api.release_hangs()
    assert 0.4 <= short < 3.0, short
    assert 1.4 <= slow < 4.5, slow
    assert [(c.method, c.outcome) for c in _settled(bot_api)] == [
        ("getUpdates", "ok"), ("getMe", "hang"), ("getUpdates", "hang")]


@pytest.mark.parametrize("stuck", ["long-poll", "resposta-presa"])
def test_abort_interrupts_a_stuck_get_updates(bot_api: FakeBotApi, make_api: _Apis,
                                              stuck: str) -> None:
    """O `abort()` interrompe a chamada em andamento de outra thread (em até 2 s
    nos testes): com um `getUpdates` preso (long poll de 30 s sem updates, ou
    resposta que nunca chega) e leitura de 40 s, `abort()` de outra thread faz a chamada sair com
    `Aborted` em menos de 2 s, e o dublê vê a conexão fechada também em menos de 2 s."""
    bot = bot_api.add_bot()
    if stuck == "resposta-presa":
        bot_api.fail_network("getUpdates", kind="hang")
    api = make_api(bot_api.token_of(bot), poll_timeout_seconds=30, timeout_seconds=10)
    waiting = _Background(lambda: api.get_updates(offset=None))
    if stuck == "long-poll":
        bot_api.wait_for(lambda: bot_api.pending_polls(bot))
    else:
        bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates") if c.outcome == "pending"])

    asked = monotonic()
    api.abort()
    assert waiting.join(ABORT_SECONDS + 1.0), "getUpdates continuou preso depois de abort()"
    assert type(waiting.error) is Aborted
    assert waiting.ended is not None and waiting.ended - asked < ABORT_SECONDS
    [call] = _settled(bot_api)
    assert call.outcome == ("abandoned" if stuck == "long-poll" else "hang")
    assert call.ended is not None and call.ended - asked < ABORT_SECONDS
    assert bot_api.pending_polls(bot) == []


def test_unreachable_api_raises_telegram_unavailable(bot_api: FakeBotApi, make_api: _Apis) -> None:
    """Rede → `TelegramUnavailable`: conexão recusada (porta local
    sem servidor) dá `TelegramUnavailable`, sem chegar ao dublê."""
    bot = bot_api.add_bot()
    api = make_api(bot_api.token_of(bot), base_url=_closed_port_url(), timeout_seconds=2)
    started = monotonic()
    with pytest.raises(TelegramError) as info:
        api.get_me()
    assert type(info.value) is TelegramUnavailable
    assert monotonic() - started < 10.0
    assert bot_api.calls() == []


# ---------------------------------------------------------------------------------------------
# Sigilo do token
# ---------------------------------------------------------------------------------------------


def test_exceptions_never_carry_the_token_or_the_url(bot_api: FakeBotApi, make_api: _Apis) -> None:
    """`str()` nunca contém token nem URL (o 418
    dá falha definitiva sem vazar o corpo): para uma exceção de **cada** tipo da porta (401, 429, 5xx,
    HTML, conexão cortada, tempo esgotado, conexão recusada, 409 de webhook e de conflito, 403 de
    bloqueio, 400, 418 cuja descrição ecoa o token, `NotABot` e `Aborted`), `str`, `repr`, `args`,
    notas e traceback formatado, dela e de toda a cadeia `__cause__`/`__context__`, não contêm o
    token, a parte secreta nem a URL da Bot API. Controle positivo: o detector acha o token na
    URL."""
    main = bot_api.add_bot()
    revoked = bot_api.add_bot()
    hooked = bot_api.add_bot()
    nameless = bot_api.add_bot(None)
    user = make_user(43)
    bot_api.push_text(main, user, "oi")
    token = bot_api.token_of(main)
    bot_api.revoke(revoked)
    bot_api.set_webhook(hooked)
    closed = _closed_port_url()

    api = make_api(token, poll_timeout_seconds=1, timeout_seconds=0.5)
    api_revoked = make_api(bot_api.token_of(revoked))
    api_hooked = make_api(bot_api.token_of(hooked))
    api_nameless = make_api(bot_api.token_of(nameless))
    api_closed = make_api(token, base_url=closed, timeout_seconds=2)
    api_aborted = make_api(token, poll_timeout_seconds=30, timeout_seconds=10)

    raised: list[tuple[str, BaseException, str]] = []

    def catch(label: str, call: Callable[[], Any], base: str = bot_api.base_url) -> None:
        with pytest.raises(TelegramError) as info:
            call()
        raised.append((label, info.value, base))

    watchdog = threading.Timer(15.0, bot_api.release_hangs)
    watchdog.start()
    try:
        catch("401", api_revoked.get_me)
        bot_api.fail("sendMessage", retry_after=3)
        catch("429", lambda: api.send_message(user.user_id, "Olá"))
        bot_api.fail("getMe", error="server_restart")
        catch("500", api.get_me)
        bot_api.fail_network("setMyDescription", kind="not_json")
        catch("502-html", lambda: api.set_my_description("Descrição"))
        bot_api.fail_network("deleteWebhook", kind="reset")
        catch("conexao-cortada", api.delete_webhook)
        bot_api.fail_network("setMyCommands", kind="hang")
        catch("tempo-esgotado", lambda: api.set_my_commands(COMMANDS))
        catch("conexao-recusada", api_closed.get_me, closed)
        catch("409-webhook", lambda: api_hooked.get_updates(offset=None))
        bot_api.fail("getUpdates", error="conflict_poll")
        catch("409-conflito", lambda: api.get_updates(offset=None))
        catch("400", lambda: api.send_message(make_user(99).user_id, "Olá"))
        bot_api.fail("getMe", status=418, description=f"I'm a teapot: {token}")
        catch("418-com-eco-do-token", api.get_me)
        catch("not-a-bot", api_nameless.get_me)
        bot_api.block_chat(main, user)
        catch("403-bloqueio", lambda: api.send_message(user.user_id, "Olá"))

        bot_api.fail_network("getUpdates", kind="hang", bot_id=main)
        waiting = _Background(lambda: api_aborted.get_updates(offset=None))
        bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=main)
                                  if c.outcome == "pending"])
        api_aborted.abort()
        assert waiting.join(ABORT_SECONDS + 1.0)
        assert waiting.error is not None
        raised.append(("aborted", waiting.error, bot_api.base_url))
    finally:
        watchdog.cancel()

    assert {type(exc) for _, exc, _ in raised} == ALL_ERRORS
    known = all_secrets()
    assert find_secrets(f"{bot_api.base_url}/bot{token}/getMe", known) != [], "controle positivo"
    for label, exc, base in raised:
        for text in _exception_texts(exc):
            assert find_secrets(text, known) == [], label
            assert f"{base}/bot" not in text, label


def test_reprs_hide_the_token_and_the_master_key(bot_api: FakeBotApi, bot_settings: Any,
                                                 bots: Any, make_api: _Apis) -> None:
    """Sigilo nos `repr` (`HttpTelegramBotApi`: `repr` sem o token; `BotRuntime`: `repr`
    sem a chave): `repr` e `str` do adaptador (direto e pela `http_factory`), do `BotRuntime` e do
    `BotSettings` não contêm o token, a parte secreta, a chave-mestra nem a URL com o token.
    Controle positivo: o detector acha o token e a chave quando eles estão no texto."""
    bot = bot_api.add_bot()
    token = bot_api.token_of(bot)
    direct = make_api(token)
    via_factory = make_api.track(http_factory(bot_settings)(token))
    known = all_secrets() + bot_secrets()
    assert find_secrets(token, known) != [] and find_secrets(bot_settings.master_key, known) != []
    for obj in (direct, via_factory, bots, bot_settings, http_factory(bot_settings)):
        for text in (repr(obj), str(obj), f"{obj}"):
            assert find_secrets(text, known) == [], type(obj).__name__
            assert f"{bot_api.base_url}/bot" not in text


def test_adapter_never_instantiates_an_httpx2_client(bot_api: FakeBotApi, bot_settings: Any,
                                                     make_api: _Apis,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem cliente do httpx2 (o `httpx2.Client` registra em INFO a URL com o token; o
    adaptador chama o transporte direto): nenhum `httpx2.Client` nem `httpx2.AsyncClient` é criado
    ao construir o adaptador (direto e pela fábrica), chamar os seis métodos, receber um erro e
    fechar. Controle positivo: um `httpx2.Client` criado pelo teste é visto pelo espião."""
    created: list[str] = []
    for cls in (httpx2.Client, httpx2.AsyncClient):
        original = cls.__init__

        def spy(self: Any, *args: Any, _original: Any = original, _name: str = cls.__name__,
                **kwargs: Any) -> None:
            created.append(_name)
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", spy)

    bot = bot_api.add_bot()
    user = make_user(44)
    bot_api.push_text(bot, user, "oi")
    token = bot_api.token_of(bot)
    api = make_api(token)
    for method in METHODS:
        _invoke(api, method, user.user_id)
    bot_api.fail("getMe", error="unauthorized")
    with pytest.raises(TelegramError):
        api.get_me()
    api.close()
    via_factory = make_api.track(http_factory(bot_settings)(token))
    assert via_factory.get_me() == BotIdentity(telegram_bot_id=bot,
                                               username=bot_api.state(bot).username)
    assert created == []

    httpx2.Client(trust_env=False).close()
    assert created == ["Client"], "controle positivo"


def test_transport_logs_never_contain_the_token(bot_api: FakeBotApi, make_api: _Apis) -> None:
    """com `capture_transport_logs()` (DEBUG em `httpx2` e
    `httpcore2`), os seis métodos, um 401, um 429 e uma conexão cortada não deixam o token, a parte
    secreta nem a URL com o token em registro nenhum. Controle positivo: na mesma captura, um
    `httpx2.Client` comum registra a URL com o token (INFO no logger `httpx2`)."""
    bot = bot_api.add_bot()
    user = make_user(45)
    bot_api.push_text(bot, user, "oi")
    token = bot_api.token_of(bot)
    with capture_transport_logs() as captured:
        api = make_api(token)
        for method in METHODS:
            _invoke(api, method, user.user_id)
        bot_api.fail("sendMessage", error="unauthorized")
        with pytest.raises(TelegramError):
            api.send_message(user.user_id, "Olá")
        bot_api.fail("getUpdates", retry_after=3)
        with pytest.raises(TelegramError):
            api.get_updates(offset=None)
        bot_api.fail_network("getMe", kind="reset")
        with pytest.raises(TelegramError):
            api.get_me()
        api.close()
        adapter_text = captured.text
        adapter_records = len(captured.records)
        with httpx2.Client(trust_env=False) as plain:
            response = plain.post(f"{bot_api.base_url}/bot{token}/getMe", json={})
        assert response.status_code == 200
        control_text = captured.text[len(adapter_text):]
        control_records = captured.records[adapter_records:]

    known = all_secrets()
    assert find_secrets(adapter_text, known) == []
    assert f"{bot_api.base_url}/bot" not in adapter_text
    assert find_secrets(control_text, known) != [], "controle positivo"
    assert any(r.get("logger") == "httpx2" and r.get("level") == "INFO"
               and token in r.get("message", "") for r in control_records)


# ---------------------------------------------------------------------------------------------
# Fábrica
# ---------------------------------------------------------------------------------------------


def test_http_factory_builds_the_adapter_from_the_settings(bot_api: FakeBotApi, bot_settings: Any,
                                                          make_api: _Apis) -> None:
    """A fábrica (`http_factory(settings) -> TelegramBotApiFactory`: URL e tempos de
    `BotSettings`; um objeto por token; `close()` idempotente): a fábrica de um `BotSettings` com
    `poll_timeout_seconds=3` cria, para cada token, um adaptador que fala com a Bot API falsa
    daquele bot e manda `timeout: 3` no `getUpdates`; fechar duas vezes não falha."""
    settings = dataclasses.replace(bot_settings, poll_timeout_seconds=3)
    factory = http_factory(settings)
    first, second = bot_api.add_bot(), bot_api.add_bot()
    user = make_user(46)
    update_id = bot_api.push_text(first, user, "oi")
    api_first = make_api.track(factory(bot_api.token_of(first)))
    api_second = make_api.track(factory(bot_api.token_of(second)))

    assert api_first.get_me() == BotIdentity(telegram_bot_id=first,
                                             username=bot_api.state(first).username)
    assert api_second.get_me() == BotIdentity(telegram_bot_id=second,
                                              username=bot_api.state(second).username)
    [update] = api_first.get_updates(offset=None)
    assert (update.update_id, update.text) == (update_id, "oi")
    calls = bot_api.calls()
    assert [(c.method, c.bot_id, c.params) for c in calls] == [
        ("getMe", first, {}), ("getMe", second, {}),
        ("getUpdates", first, {"timeout": 3, "limit": 100, "allowed_updates": ALLOWED_UPDATES})]
    api_first.close()
    api_first.close()


# ---------------------------------------------------------------------------------------------
# Servidor mínimo para respostas que o dublê não produz
# ---------------------------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _StubServer

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size)
        self.server.requests.append((self.command, self.path, self.headers.get("Content-Type"),
                                     body))
        status, content_type, payload = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    do_GET = do_PUT = do_POST

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio: o padrão escreve a linha do pedido (com o token) em stderr."""

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio."""


class _StubServer(ThreadingHTTPServer):
    """Responde a todo pedido com `reply` = (status, Content-Type, corpo) e guarda o pedido."""

    daemon_threads = True

    def __init__(self, reply: tuple[int, str, bytes]) -> None:
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.reply = reply
        self.requests: list[tuple[str, str, str | None, bytes]] = []
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Silêncio: o padrão imprime o traceback em stderr."""


@pytest.fixture
def stub_server() -> Iterator[Callable[..., _StubServer]]:
    started: list[tuple[_StubServer, threading.Thread]] = []

    def start(status: int, content_type: str, payload: bytes) -> _StubServer:
        server = _StubServer((status, content_type, payload))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  name="cq-test-stub-bot-api", daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(5)
