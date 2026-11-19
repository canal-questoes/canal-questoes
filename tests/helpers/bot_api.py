"""Bot API falsa do Telegram.

Servidor HTTP de verdade (`ThreadingHTTPServer` em `127.0.0.1`, porta livre, HTTP/1.1 com
`Content-Length`), alcançado pelo adaptador real pela URL `base_url` (`CQ_TELEGRAM_API_URL`). Só
usa a biblioteca padrão e **não** importa `cq_server`: se o adaptador errar, o dublê continua capaz
de acusar.

**Fidelidade** ao servidor oficial (`tdlib/telegram-bot-api@e3e9dd8`):
- erro `{"ok": false, "error_code", "description", "parameters"?}` com status HTTP = `error_code`;
  sucesso `{"ok": true, "result", "description"?}`; as descrições de `ERRORS` são as do servidor;
- token conferido como no `ClientManager` (401 `invalid token specified`, 421 `forbidden token
  specified`); token desconhecido, revogado ou substituído → 401 `Unauthorized`;
- métodos sem distinção de caixa; caminho fora de `/bot<token>/<método>` → 404 `Not Found`; o
  ambiente `/test/` e os métodos fora do dublê → 404 `Not Found: method not found`;
- `getUpdates`: fila por bot com confirmação por `offset` (o que fica abaixo é esquecido; negativo
  apaga a fila menos os |offset| últimos; acima do fim + 10 ou abaixo da cabeça − 100000 volta à
  cabeça); `limit` e `timeout` recortados em [1, 100] e [0, 50]; `allowed_updates` persistente
  (lista vazia ou só nomes desconhecidos = padrão; tipos fora da lista são descartados na geração do
  update); long poll que bloqueia até chegar update, vencer o `timeout` (tempo real), o token ser
  revogado ou trocado, ou vir `reset`/`close`; um `getUpdates` do mesmo bot que vai bloquear encerra
  o pedido **mais antigo** com 409; com webhook ativo, 409 até o `deleteWebhook`; no máximo um 409
  de conflito a cada `conflict_delay_seconds` (os demais saem com esse atraso, como o servidor faz
  com 3 s), desligado (0) por padrão e ligado em `reset(conflict_delay_seconds=3.0)`;
- `deleteWebhook` com `"Webhook was deleted"` ou `"Webhook is already deleted"`;
- `sendMessage`: 403 no chat bloqueado, 400 `chat not found` no chat que nunca mandou update ao bot.

**Desvios deliberados:** não reproduz o anti-laço do servidor (mesmo `offset` em menos de 3 s), a
retenção de 24 h, o corte do lote em 4 MiB nem a espera de 1 ms para juntar updates; os textos dos
400 de limite (descrição, comandos, texto) e de corpo ilegível são do dublê.

**Estrito:** tudo o que desvia do protocolo do sistema (POST com corpo JSON UTF-8, nada na consulta)
ou da documentação vai para `protocol_errors()`, que a fixture exige vazio: parâmetro na consulta,
multipart ou formulário, método HTTP ≠ POST, JSON inválido, método desconhecido, `logOut`/`close`,
parâmetro não documentado, parâmetro obrigatório ausente, limites fora da documentação (`limit`,
`timeout`, `offset`, `allowed_updates`, descrição até 512, 1 a 100 comandos, comando
`^[a-z0-9_]{1,32}$`, descrição do comando 1 a 256, texto 1 a 4096, contados em unidades UTF-16),
`deleteWebhook` com `drop_pending_updates=true`, gancho `on_call` ou predicado de `fail` que falhou
e erro interno do dublê.

**Silencioso:** nada em stdout nem stderr (`log_message`, `log_error` e `handle_error`
sobrescritos; `OSError` na escrita engolido), porque o `http.server` escreveria a linha do pedido,
com o token, em stderr.

**Registro:** cada pedido vira um `BotApiCall` (sem o token), com `started`/`ended` do relógio
monotônico de alta resolução `monotonic()` e `clock_at` do relógio passado em `reset(clock=)`. Um
long poll cujo cliente fecha a conexão termina em até 0,1 s (o socket é conferido a cada 0,01 s)
com `outcome = "abandoned"`, `ended` no instante da detecção, sem resposta nem 409.

Os tokens são `"<bot_id>:<35 caracteres base64url>"`, registrados em `remember_secret` inteiros e na
parte secreta. Os ids não colidem com os do canário: `bot_id` desde 6100000001, `update_id` desde
`810000000 + 1_000_000 × (índice do bot % 1000)`, `message_id` crescente por chat.

**Gancho de extensão** (para o disparo e a resposta). Sem extensão instalada, tudo fica byte
a byte como no dublê base (o autoteste congelado `tests/unit/test_bot_api_fake.py` continua valendo).
O gancho:
- `install(extension)` liga uma extensão (a `DispatchExtension` de `tests/helpers/bot_api_dispatch.py`) até o
  próximo `reset()`, que a desfaz; `extension` devolve a instalada (ou `None`).
- A busca do método passa por `_method_table()`: os `METHODS` do dublê base mais os `methods` da extensão.
  `_check_method` (de `fail`, `fail_network` e `on_call`) usa a mesma tabela.
- `_check_params(…, extension=)` consulta a extensão primeiro; ela devolve `None` para deixar a regra
  do dublê base valer.
- multipart deixa de ser erro de protocolo **só** nos `upload_methods` da extensão, que o decifra.
- o despacho vai ao `handle` da extensão nos métodos que ela declara em `handles` (os novos e os do dublê base
  que ela assume, como `sendMessage` e `answerCallbackQuery`); os demais seguem nos `_m_…` daqui.
- `fail(…, after_effect=True)` (só com a extensão e num método que ela atende): o efeito é aplicado
  (a mensagem existe) e só então a falha programada é respondida, como a mensagem duplicada.
- `push_text(…, reply_to_message_id=)` põe o `reply_to_message` na mensagem; sem ele, a
  mensagem é a do dublê base.
Nenhum import novo: a extensão é consultada pelos atributos (duck typing), e este módulo continua só
com a biblioteca padrão e o registro de segredos.
"""

from __future__ import annotations

import atexit
import bisect
import copy
import http.client
import itertools
import json
import math
import os
import re
import secrets
import select
import socket
import socketserver
import struct
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypeVar

from tests.helpers.secrets import remember_secret

T = TypeVar("T")

# ---------------------------------------------------------------------------------------------
# Constantes do servidor oficial
# ---------------------------------------------------------------------------------------------

ERRORS: dict[str, tuple[int, str]] = {
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
    "conflict_poll": (409, "Conflict: terminated by other getUpdates request; make sure that only "
                           "one bot instance is running"),
    "conflict_webhook": (409, "Conflict: can't use getUpdates method while webhook is active; use "
                              "deleteWebhook to delete the webhook first"),
    "conflict_set_webhook": (409, "Conflict: terminated by setWebhook request"),
    "server_restart": (500, "Internal Server Error: restart"),
}


def too_many_requests(seconds: int) -> tuple[int, str]:
    """O 429 do servidor oficial (vai com `parameters.retry_after` e o cabeçalho `Retry-After`)."""
    return 429, f"Too Many Requests: retry after {seconds}"


# Tipos de update do servidor (Client.cpp, `get_update_type_name`), na ordem do enum.
UPDATE_TYPES: tuple[str, ...] = (
    "message", "edited_message", "channel_post", "edited_channel_post", "inline_query",
    "chosen_inline_result", "callback_query", "custom_event", "custom_query", "shipping_query",
    "pre_checkout_query", "poll", "poll_answer", "my_chat_member", "chat_member",
    "chat_join_request",
    "chat_boost", "removed_chat_boost", "message_reaction", "message_reaction_count",
    "business_connection", "business_message", "edited_business_message",
    "deleted_business_messages", "purchased_paid_media", "managed_bot", "guest_message",
    "subscription", "stopped_message_generation",
)
DEFAULT_ALLOWED_UPDATES: frozenset[str] = frozenset(UPDATE_TYPES) - {
    "chat_member", "message_reaction", "message_reaction_count"}

# Métodos que o dublê atende (nome canônico). `logOut` e `close` nunca podem ser chamados.
METHODS: tuple[str, ...] = (
    "getMe", "getUpdates", "deleteWebhook", "getWebhookInfo", "setMyDescription", "setMyCommands",
    "sendMessage", "answerCallbackQuery",
)
FORBIDDEN_METHODS: tuple[str, ...] = ("logOut", "close")
_METHOD_BY_LOWER = {m.lower(): m for m in METHODS}

# Parâmetros documentados de cada método (Bot API 10.3).
_PARAMS: dict[str, frozenset[str]] = {
    "getMe": frozenset(),
    "getWebhookInfo": frozenset(),
    "getUpdates": frozenset({"offset", "limit", "timeout", "allowed_updates"}),
    "deleteWebhook": frozenset({"drop_pending_updates"}),
    "setMyDescription": frozenset({"description", "language_code"}),
    "setMyCommands": frozenset({"commands", "scope", "language_code"}),
    "sendMessage": frozenset({
        "business_connection_id", "chat_id", "message_thread_id", "direct_messages_topic_id",
        "ephemeral_message_parameters", "text", "parse_mode", "entities", "link_preview_options",
        "disable_notification", "protect_content", "allow_paid_broadcast", "message_effect_id",
        "suggested_post_parameters", "reply_parameters", "reply_markup"}),
    "answerCallbackQuery": frozenset({"callback_query_id", "text", "show_alert", "url",
                                      "cache_time"}),
}
_COMMAND_SCOPES = frozenset({"default", "all_private_chats", "all_group_chats",
                             "all_chat_administrators", "chat", "chat_administrators",
                             "chat_member"})
_BOT_COMMAND_RE = re.compile(r"[a-z0-9_]{1,32}")
_LANGUAGE_CODE_RE = re.compile(r"[a-z]{2}")

FIRST_BOT_ID = 6_100_000_001
UPDATE_ID_BASE = 810_000_000
UPDATE_ID_STRIDE = 1_000_000
NEUTRAL_USER_BASE = 5_100_000_000
SOCKET_CHECK_SECONDS = 0.01
MAX_QUEUE_EVENTS = 100_000
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
HTML_BAD_GATEWAY = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
                    b"<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx</center>\r\n"
                    b"</body>\r\n</html>\r\n")

_AUTO: Any = object()


def monotonic() -> float:
    """O relógio de `started`/`ended`: `time.perf_counter()`, monotônico e de alta resolução.

    Não é o `time.monotonic()` porque, no Windows com Python 3.12, ele anda em passos de 15,6 ms
    (`GetTickCount64`), o que junta intervalos distintos. Quem compara com `started`/`ended` (ex.: o
    instante de um `abort()`) mede com esta função.
    """
    return time.perf_counter()


# ---------------------------------------------------------------------------------------------
# Tipos públicos
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotApiCall:
    """Um pedido recebido pelo dublê, **sem o token**.

    `method` é o nome canônico (ex.: `getUpdates`, mesmo pedido como `getupdates`), ou o nome pedido
    se o método não existe no dublê (vazio se o caminho não tinha método). `token_state` é `valid`,
    `revoked` (substituído por `rotate_token` ou revogado), `unknown` ou `malformed`; `bot_id` vem
    do token (o prefixo, se desconhecido). `token_version` é a versão do token do bot (1 = o de
    `add_bot`; cada `rotate_token` soma 1). `params` é o corpo JSON (mais a consulta, se houver).
    `started`/`ended` são de `monotonic()` (`ended` é `None` enquanto o pedido está aberto) e
    `clock_at` é o `now()` do relógio de `reset(clock=)` na chegada. `outcome` é `pending` (aberto),
    `ok`, `error` (resposta com `ok` falso, ou corpo que não é JSON), `reset` (conexão cortada),
    `hang` (resposta presa) ou `abandoned` (o cliente fechou a conexão durante a espera). `result`
    é o `result` das respostas `ok` (em `getUpdates`, a lista entregue). `programmed` diz que o
    resultado veio de `fail`/`fail_network`. `conflicted_by` é o `seq` do `getUpdates` que encerrou
    este com 409.
    """

    seq: int
    method: str
    bot_id: int | None
    token_state: str
    token_version: int | None
    params: dict[str, Any]
    http_method: str
    content_type: str | None
    has_query: bool
    connection_id: int
    started: float
    ended: float | None
    clock_at: datetime
    outcome: str
    status: int | None
    description: str | None
    result: Any
    retry_after: int | None
    programmed: bool
    conflicted_by: int | None


@dataclass(frozen=True)
class SentMessage:
    """Uma mensagem aceita por `sendMessage`."""

    bot_id: int
    chat_id: int
    message_id: int
    text: str
    clock_at: datetime
    call_seq: int


@dataclass(frozen=True)
class BotState:
    """Retrato do estado de um bot no dublê."""

    bot_id: int
    username: str | None
    first_name: str
    token_version: int
    revoked: bool
    webhook_url: str | None
    allowed_updates: tuple[str, ...]
    pending_update_ids: tuple[int, ...]
    redeliveries: tuple[int, ...]
    next_update_id: int
    dropped_updates: int
    description: str | None
    commands: tuple[dict[str, Any], ...] | None
    blocked_chats: frozenset[int]
    known_chats: frozenset[int]


@dataclass(frozen=True)
class Reply:
    """Resposta crua de `FakeBotApi.request` (cabeçalhos em minúsculas)."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass(frozen=True)
class TelegramUser:
    """Remetente neutro (sem alvos canário), com a forma de `CanaryTelegramUser` (`as_user`,
    `as_private_chat`); qualquer objeto com esses dois métodos serve de remetente."""

    user_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None

    def as_user(self) -> dict[str, Any]:
        user: dict[str, Any] = {"id": self.user_id, "is_bot": False, "first_name": self.first_name}
        if self.last_name is not None:
            user["last_name"] = self.last_name
        if self.username is not None:
            user["username"] = self.username
        user["language_code"] = "pt-br"
        return user

    def as_private_chat(self) -> dict[str, Any]:
        chat: dict[str, Any] = {"id": self.user_id, "type": "private",
                                "first_name": self.first_name}
        if self.last_name is not None:
            chat["last_name"] = self.last_name
        if self.username is not None:
            chat["username"] = self.username
        return chat


def make_user(index: int) -> TelegramUser:
    """O `index`-ésimo remetente neutro (ids desde 5100000000, fora da faixa canário `73…`)."""
    return TelegramUser(NEUTRAL_USER_BASE + index, f"Pessoa {index}")


@dataclass(frozen=True)
class ExtensionReply:
    """Extensão: o desfecho que o `handle` de uma extensão devolve ao dublê.

    `error` = `(status, descrição)` dá a resposta de erro oficial; senão, `result` (e `description`,
    se houver) vai numa resposta `ok`. Quem escreve a resposta HTTP é sempre o dublê.
    """

    result: Any = None
    error: tuple[int, str] | None = None
    description: str | None = None


# ---------------------------------------------------------------------------------------------
# Estado interno (sempre sob o único `threading.Condition` do dublê)
# ---------------------------------------------------------------------------------------------


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _Call:
    def __init__(self, *, seq: int, gen: int, method: str, bot_id: int | None, token_state: str,
                 token_version: int | None, params: dict[str, Any], http_method: str,
                 content_type: str | None, has_query: bool, connection_id: int, started: float,
                 clock_at: datetime) -> None:
        self.seq = seq
        self.gen = gen
        self.method = method
        self.bot_id = bot_id
        self.token_state = token_state
        self.token_version = token_version
        self.params = params
        self.http_method = http_method
        self.content_type = content_type
        self.has_query = has_query
        self.connection_id = connection_id
        self.started = started
        self.clock_at = clock_at
        self.ended: float | None = None
        self.outcome = "pending"
        self.status: int | None = None
        self.description: str | None = None
        self.result: Any = None
        self.retry_after: int | None = None
        self.programmed = False
        self.conflicted_by: int | None = None
        self._snap: BotApiCall | None = None

    def touch(self) -> None:
        self._snap = None

    def snapshot(self) -> BotApiCall:
        if self._snap is None:
            self._snap = BotApiCall(
                seq=self.seq, method=self.method, bot_id=self.bot_id, token_state=self.token_state,
                token_version=self.token_version, params=self.params, http_method=self.http_method,
                content_type=self.content_type, has_query=self.has_query,
                connection_id=self.connection_id, started=self.started, ended=self.ended,
                clock_at=self.clock_at, outcome=self.outcome, status=self.status,
                description=self.description, result=self.result, retry_after=self.retry_after,
                programmed=self.programmed, conflicted_by=self.conflicted_by)
        return self._snap


class _Poll:
    def __init__(self, call: _Call, bot: _Bot, offset: int, limit: int, deadline: float) -> None:
        self.call = call
        self.bot = bot
        self.offset = offset
        self.limit = limit
        self.deadline = deadline
        self.abort: tuple[int, str] | None = None
        self.respond_at = 0.0


class _Hang:
    def __init__(self, gen: int, seq: int) -> None:
        self.gen = gen
        self.seq = seq


@dataclass
class _Failure:
    method: str
    kind: str  # http | reset | hang | not_json
    times: int | None
    when: Callable[[BotApiCall], bool] | None
    bot_id: int | None
    status: int | None = None
    description: str | None = None
    retry_after: int | None = None
    after_effect: bool = False  # extensão: aplica o efeito da extensão antes de responder a falha


class _Bot:
    def __init__(self, *, bot_id: int, index: int, username: str | None, first_name: str,
                 is_bot: bool, extra: Mapping[str, Any] | None, token: str, next_id: int) -> None:
        self.bot_id = bot_id
        self.index = index
        self.username = username
        self.first_name = first_name
        self.is_bot = is_bot
        self.extra = dict(extra or {})
        self.token = token
        self.token_version = 1
        self.revoked = False
        self.webhook_url: str | None = None
        self.allowed: frozenset[str] = DEFAULT_ALLOWED_UPDATES
        self.queue: list[dict[str, Any]] = []  # ordenada por update_id, sem repetição
        self.next_id = next_id
        self.history: dict[int, dict[str, Any]] = {}
        self.redeliveries: list[dict[str, Any]] = []
        self.description: str | None = None
        self.commands: list[dict[str, Any]] | None = None
        self.blocked: dict[int, str] = {}
        self.known_chats: dict[int, str] = {}
        self.message_ids: dict[int, int] = {}
        self.dropped = 0
        self.next_conflict_time = 0.0
        self.polls: list[_Poll] = []

    def user(self) -> dict[str, Any]:
        user: dict[str, Any] = {"id": self.bot_id, "is_bot": True, "first_name": self.first_name}
        if self.username is not None:
            user["username"] = self.username
        return user


# ---------------------------------------------------------------------------------------------
# Funções auxiliares puras
# ---------------------------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _client_gone(sock: socket.socket) -> bool:
    """O cliente fechou a conexão? (`select` + `recv(1, MSG_PEEK)`, sem bloquear nem consumir.)"""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _parse_path(path: str) -> tuple[str, bool, str] | None:
    """`/bot<token>/<método>` ou `/bot<token>/test/<método>` → (token, teste?, método)."""
    if not path.startswith("/bot"):
        return None
    token, sep, tail = path[4:].partition("/")
    if not sep:
        return None
    is_test = False
    if tail.startswith("test/"):
        is_test = True
        tail = tail[5:]
    return token, is_test, tail


def _update_type(payload: Mapping[str, Any]) -> str:
    kinds = [k for k in payload if k != "update_id"]
    if len(kinds) != 1:
        raise ValueError("um update tem exatamente um campo além de update_id")
    return kinds[0]


def _update_chat(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    body = next((v for k, v in payload.items() if k != "update_id"), None)
    if not isinstance(body, dict):
        return None
    chat = body.get("chat")
    if isinstance(chat, dict):
        return chat
    message = body.get("message")
    if isinstance(message, dict) and isinstance(message.get("chat"), dict):
        return message["chat"]
    return None


def _chat_id_of(chat: Any) -> int:
    if _is_int(chat):
        return chat
    user_id = getattr(chat, "user_id", None)
    if _is_int(user_id):
        return user_id
    if hasattr(chat, "as_private_chat"):
        return chat.as_private_chat()["id"]
    raise TypeError("chat deve ser um id inteiro ou um remetente")


def _default_description(status: int) -> str:
    known = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
             409: ERRORS["conflict_poll"][1], 421: ERRORS["forbidden_token"][1],
             429: "Too Many Requests", 500: ERRORS["server_restart"][1], 502: "Bad Gateway"}
    if status in known:
        return known[status]
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"Error {status}"


def _check_params(method: str, params: Mapping[str, Any], *,
                  extension: Any = None) -> tuple[list[str], str | None]:
    """Problemas de protocolo dos parâmetros e, se o servidor recusaria, a descrição do 400.

    Com `extension`, a extensão é consultada primeiro (`extension.check_params`); se ela
    devolve `None`, vale a regra daqui, como no dublê base.
    """
    if extension is not None:
        checked = extension.check_params(method, params)
        if checked is not None:
            return checked
    problems: list[str] = []
    bad: str | None = None

    def flag(problem: str, bad_request: str | None = None) -> None:
        nonlocal bad
        problems.append(problem)
        if bad_request is not None and bad is None:
            bad = bad_request

    for name in params:
        if name not in _PARAMS[method]:
            flag(f"parâmetro não documentado: {name!r}")

    def check_language_code() -> None:
        if "language_code" in params:
            value = params["language_code"]
            if not isinstance(value, str) or (value and not _LANGUAGE_CODE_RE.fullmatch(value)):
                flag("language_code fora de duas letras ISO 639-1")

    if method == "getUpdates":
        if "offset" in params:
            value = params["offset"]
            if not _is_int(value) or not INT32_MIN <= value <= INT32_MAX:
                flag("offset não é um inteiro de 32 bits")
        if "limit" in params:
            value = params["limit"]
            if not _is_int(value) or not 1 <= value <= 100:
                flag("limit fora de 1 a 100")
        if "timeout" in params:
            value = params["timeout"]
            if not _is_int(value) or not 0 <= value <= 50:
                flag("timeout fora de 0 a 50")
        if "allowed_updates" in params:
            value = params["allowed_updates"]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                flag("allowed_updates não é uma lista de textos")
            elif any(x not in UPDATE_TYPES for x in value):
                flag("allowed_updates com tipo de update desconhecido")
    elif method == "deleteWebhook":
        if "drop_pending_updates" in params:
            value = params["drop_pending_updates"]
            if not isinstance(value, bool):
                flag("drop_pending_updates não é booleano")
            elif value:
                flag("drop_pending_updates=true descarta as pendências")
    elif method == "setMyDescription":
        if "description" in params:
            value = params["description"]
            if not isinstance(value, str):
                flag("description não é texto", "Bad Request: description must be a string")
            elif _utf16_len(value) > 512:
                flag("description com mais de 512 unidades UTF-16",
                     "Bad Request: description is too long")
        check_language_code()
    elif method == "setMyCommands":
        if "commands" not in params:
            flag("parâmetro obrigatório ausente: 'commands'",
                 'Bad Request: parameter "commands" is required')
        else:
            commands = params["commands"]
            if not isinstance(commands, list):
                flag("commands não é uma lista", "Bad Request: can't parse commands JSON object")
            else:
                if not 1 <= len(commands) <= 100:
                    flag("commands fora de 1 a 100 itens", "Bad Request: invalid bot commands")
                for i, command in enumerate(commands):
                    if not isinstance(command, dict):
                        flag(f"commands[{i}] não é um objeto", "Bad Request: invalid bot commands")
                        continue
                    for key in command:
                        if key not in ("command", "description", "is_ephemeral"):
                            flag(f"commands[{i}] com campo não documentado: {key!r}")
                    name = command.get("command")
                    if not isinstance(name, str) or not _BOT_COMMAND_RE.fullmatch(name):
                        flag(f"commands[{i}].command fora de ^[a-z0-9_]{{1,32}}$",
                             "Bad Request: invalid bot commands")
                    text = command.get("description")
                    if not isinstance(text, str) or not 1 <= _utf16_len(text) <= 256:
                        flag(f"commands[{i}].description fora de 1 a 256 unidades UTF-16",
                             "Bad Request: invalid bot commands")
                    if "is_ephemeral" in command and not isinstance(command["is_ephemeral"], bool):
                        flag(f"commands[{i}].is_ephemeral não é booleano")
        if "scope" in params:
            scope = params["scope"]
            if not isinstance(scope, dict) or scope.get("type") not in _COMMAND_SCOPES:
                flag("scope não é um BotCommandScope", "Bad Request: can't parse BotCommandScope")
        check_language_code()
    elif method == "sendMessage":
        if "chat_id" not in params:
            flag("parâmetro obrigatório ausente: 'chat_id'", "Bad Request: chat_id is empty")
        elif not (_is_int(params["chat_id"]) or isinstance(params["chat_id"], str)):
            flag("chat_id não é inteiro nem texto", "Bad Request: chat not found")
        if "text" not in params:
            flag("parâmetro obrigatório ausente: 'text'", "Bad Request: message text is empty")
        elif not isinstance(params["text"], str):
            flag("text não é texto", "Bad Request: message text is empty")
        elif _utf16_len(params["text"]) == 0:
            flag("text vazio", "Bad Request: message text is empty")
        elif _utf16_len(params["text"]) > 4096:
            flag("text com mais de 4096 unidades UTF-16", "Bad Request: message is too long")
    elif method == "answerCallbackQuery":
        if not isinstance(params.get("callback_query_id"), str):
            flag("callback_query_id ausente ou não é texto",
                 'Bad Request: parameter "callback_query_id" is required')
        if "text" in params and (not isinstance(params["text"], str)
                                 or _utf16_len(params["text"]) > 200):
            flag("text com mais de 200 unidades UTF-16", "Bad Request: text is too long")
    return problems, bad


# ---------------------------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 128
    fake: FakeBotApi

    def server_bind(self) -> None:
        # Sem `socket.getfqdn` (o `HTTPServer.server_bind` faz uma busca de DNS que pode demorar).
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Silêncio: o padrão imprime o traceback em stderr."""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeBotApi/1.0"
    sys_version = ""
    server: _Server
    connection_id: int
    fake_call: _Call | None = None

    def setup(self) -> None:
        super().setup()
        self.connection_id = self.server.fake._register_connection(self.connection)

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self.server.fake._unregister_connection(self.connection)

    def _dispatch(self) -> None:
        self.server.fake._serve(self)

    do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = _dispatch

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio: o padrão escreve a linha do pedido (com o token) em stderr."""

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - assinatura da base
        """Silêncio."""

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """Silêncio."""


def _write(h: _Handler, status: int, body: bytes, content_type: str,
           headers: tuple[tuple[str, str], ...] = ()) -> None:
    try:
        h.send_response(status)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            h.send_header(name, value)
        h.end_headers()
        h.wfile.write(body)
        h.wfile.flush()
    except OSError:
        h.close_connection = True


def _read_body(h: _Handler) -> tuple[bytes, list[str]]:
    problems: list[str] = []
    encoding = (h.headers.get("Transfer-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        problems.append("corpo com Transfer-Encoding (esperado Content-Length)")
        if "chunked" not in encoding:
            h.close_connection = True
            return b"", problems
        chunks: list[bytes] = []
        while True:
            size_line = h.rfile.readline(65537).split(b";")[0].strip()
            try:
                size = int(size_line, 16)
            except ValueError:
                h.close_connection = True
                return b"".join(chunks), problems
            if size == 0:
                while h.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks), problems
            chunks.append(h.rfile.read(size))
            h.rfile.readline(65537)
    length = h.headers.get("Content-Length")
    if length is None:
        return b"", problems
    try:
        size = int(length)
        if size < 0:
            raise ValueError
    except ValueError:
        problems.append("Content-Length inválido")
        h.close_connection = True
        return b"", problems
    return h.rfile.read(size), problems


# ---------------------------------------------------------------------------------------------
# A Bot API falsa
# ---------------------------------------------------------------------------------------------


class FakeBotApi:
    """Bot API falsa: servidor HTTP em `127.0.0.1` (ver o docstring do módulo).

    Uso típico: `bot = bot_api.add_bot()`; o sistema recebe `bot_api.token_of(bot)` e
    `bot_api.base_url`; o teste injeta updates (`push_*`), programa falhas (`fail*`) e confere
    `calls()`, `sent_messages()` e `protocol_errors()`. `reset(clock=)` limpa tudo entre testes.
    """

    def __init__(self, *, clock: Any = None) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._clock: Any = clock if clock is not None else _UtcClock()
        self._gen = 0
        self._closing = False
        self._closed = False
        self._bot_index = itertools.count()  # nunca volta: ids de bot únicos na sessão
        self._conn_ids = itertools.count(1)
        self._callback_ids = itertools.count(1)
        self._connections: set[socket.socket] = set()
        self._active = 0
        self._all_tokens: list[str] = []
        self._init_state(conflict_delay_seconds=0.0)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self.port: int = int(self._server.server_address[1])
        self.base_url: str = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, name="fake-bot-api",
                                        daemon=True)
        self._thread.start()

    def _init_state(self, *, conflict_delay_seconds: float) -> None:
        self._bots: dict[int, _Bot] = {}
        self._tokens: dict[str, tuple[int, int]] = {}
        self._issued: list[str] = []
        self._calls: list[_Call] = []
        self._seq = itertools.count(1)
        self._failures: list[_Failure] = []
        self._hooks: dict[str, list[Callable[[BotApiCall], None]]] = {}
        self._problems: list[str] = []
        self._sent: list[SentMessage] = []
        self._last_seq = 0
        self._released_through = 0  # `release_hangs` solta as presas com `seq` até aqui
        self._conflict_delay = float(conflict_delay_seconds)
        self._extension: Any = None  # extensão: `install`; o `reset` a desfaz

    # -- ciclo de vida -------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def reset(self, clock: Any = None, *, conflict_delay_seconds: float = 0.0) -> None:
        """Esquece bots, tokens, filas, chamadas, falhas, ganchos e erros; troca o relógio.

        Os long polls e as respostas presas em aberto terminam (401 `Unauthorized`, porque o token
        deixou de existir), e nada do que eles fizerem depois entra no registro novo.
        `conflict_delay_seconds` liga o espaçamento dos 409 de conflito (o servidor usa 3 s).
        """
        if conflict_delay_seconds < 0:
            raise ValueError("conflict_delay_seconds não pode ser negativo")
        with self._cond:
            self._gen += 1
            self._clock = clock if clock is not None else _UtcClock()
            self._init_state(conflict_delay_seconds=conflict_delay_seconds)
            self._cond.notify_all()

    def close(self) -> None:
        """Para o servidor (idempotente); os pedidos em espera terminam com 500 `restart`."""
        with self._cond:
            if self._closed or self._closing:
                return
            self._closing = True
            self._cond.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        deadline = monotonic() + 2.0
        with self._cond:
            while self._active and monotonic() < deadline:
                self._cond.wait(0.01)
            connections = list(self._connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._closed = True

    # -- extensão --------------------------------------------------------------------

    def install(self, extension: Any) -> Any:
        """Liga `extension` até o próximo `reset()` e a devolve.

        A extensão é consultada pelos atributos: `methods` (os nomes canônicos novos, fora de
        `METHODS` e de `FORBIDDEN_METHODS`), `handles` (os métodos que o `handle` dela atende: os
        novos e os do dublê base que ela assume), `upload_methods` (os que aceitam multipart),
        `bind(fake)`, `check_params(method, params)` (`None` = regra do dublê base),
        `parse_multipart(method, content_type, body) -> (params, problemas)` e
        `handle(method, call, bot_id, params) -> ExtensionReply`. Uma segunda extensão sem `reset`
        é erro (`ValueError`).
        """
        methods = tuple(extension.methods)
        handles = frozenset(extension.handles)
        uploads = frozenset(extension.upload_methods)
        with self._cond:
            if self._extension is not None:
                raise ValueError("já há uma extensão instalada (o reset a desfaz)")
            base = {m.lower() for m in METHODS} | {m.lower() for m in FORBIDDEN_METHODS}
            lowered = [m.lower() for m in methods]
            if any(m in base for m in lowered) or len(set(lowered)) != len(lowered):
                raise ValueError("a extensão repete um método do dublê base, um proibido ou um dela mesma")
            known = set(METHODS) | set(methods)
            if not handles <= known or not uploads <= set(methods) or not set(methods) <= handles:
                raise ValueError("handles e upload_methods da extensão fora dos métodos conhecidos")
            extension.bind(self)
            self._extension = extension
            return extension

    @property
    def extension(self) -> Any:
        """A extensão instalada desde o último `reset` (ou `None`)."""
        with self._cond:
            return self._extension

    def _method_table(self) -> dict[str, str]:
        """Nome em minúsculas → nome canônico: os `METHODS` do dublê base mais os da extensão instalada."""
        extension = self._extension
        if extension is None:
            return _METHOD_BY_LOWER
        return {**_METHOD_BY_LOWER, **{m.lower(): m for m in extension.methods}}

    def _upload_method(self, path: str) -> tuple[str, Any] | None:
        """O método de upload da extensão pedido em `path` (e a extensão), ou `None`."""
        with self._cond:
            extension = self._extension
            if extension is None:
                return None
            parsed = _parse_path(path)
            if parsed is None or parsed[1]:
                return None
            canonical = self._method_table().get(parsed[2].lower())
            if canonical is None or canonical not in extension.upload_methods:
                return None
            return canonical, extension

    def __enter__(self) -> FakeBotApi:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- bots e tokens -------------------------------------------------------------------------

    def add_bot(self, username: str | None = _AUTO, *, first_name: str = "Bot de teste",
                bot_id: int | None = None, is_bot: bool = True,
                extra: Mapping[str, Any] | None = None) -> int:
        """Cria um bot e devolve o seu id (o prefixo do token).

        `username=None` faz o `getMe` sair sem `username`; `is_bot=False` e `extra` (campos a mais
        no `getMe`, que o sistema deve aceitar) servem aos casos de `NotABot` e de campos novos.
        """
        with self._cond:
            index = next(self._bot_index)
            if bot_id is None:
                bot_id = FIRST_BOT_ID + index
            if not _is_int(bot_id) or not 0 < bot_id < 2**54:
                raise ValueError("bot_id fora de 1 a 2^54 - 1")
            if bot_id in self._bots:
                raise ValueError("bot_id já existe no dublê")
            if username is _AUTO:
                username = f"cq_teste_{index:04d}_bot"
            token = self._new_token(bot_id)
            bot = _Bot(bot_id=bot_id, index=index, username=username, first_name=first_name,
                       is_bot=is_bot, extra=extra, token=token,
                       next_id=UPDATE_ID_BASE + UPDATE_ID_STRIDE * (index % 1000))
            self._bots[bot_id] = bot
            self._tokens[token] = (bot_id, 1)
            return bot_id

    def _new_token(self, bot_id: int) -> str:
        secret = secrets.token_urlsafe(27)[:35]
        token = f"{bot_id}:{secret}"
        remember_secret(token)
        remember_secret(secret)
        self._all_tokens.append(token)  # da sessão inteira: `_scrub` também tira os antigos
        self._issued.append(token)
        return token

    def _bot(self, bot_id: int) -> _Bot:
        try:
            return self._bots[bot_id]
        except KeyError:
            raise KeyError(f"bot {bot_id} não existe no dublê") from None

    def rotate_token(self, bot_id: int) -> str:
        """Gera um token novo (versão + 1) e devolve-o; o anterior passa a dar 401 `Unauthorized`,
        também nos long polls em aberto. Recupera um bot revogado."""
        with self._cond:
            bot = self._bot(bot_id)
            bot.token_version += 1
            bot.token = self._new_token(bot_id)
            bot.revoked = False
            self._tokens[bot.token] = (bot_id, bot.token_version)
            self._cond.notify_all()
            return bot.token

    def revoke(self, bot_id: int) -> None:
        """Revoga o token atual: 401 já, também nos long polls em aberto (até `rotate_token`)."""
        with self._cond:
            self._bot(bot_id).revoked = True
            self._cond.notify_all()

    def token_of(self, bot_id: int) -> str:
        with self._cond:
            return self._bot(bot_id).token

    def all_tokens(self) -> list[str]:
        """Os tokens emitidos desde o último `reset`, na ordem (inclusive os substituídos e os
        revogados), para as varreduras."""
        with self._cond:
            return list(self._issued)

    def state(self, bot_id: int) -> BotState:
        with self._cond:
            bot = self._bot(bot_id)
            return BotState(
                bot_id=bot.bot_id, username=bot.username, first_name=bot.first_name,
                token_version=bot.token_version, revoked=bot.revoked, webhook_url=bot.webhook_url,
                allowed_updates=tuple(t for t in UPDATE_TYPES if t in bot.allowed),
                pending_update_ids=tuple(u["update_id"] for u in bot.queue),
                redeliveries=tuple(u["update_id"] for u in bot.redeliveries),
                next_update_id=bot.next_id, dropped_updates=bot.dropped,
                description=bot.description,
                commands=None if bot.commands is None else tuple(copy.deepcopy(bot.commands)),
                blocked_chats=frozenset(bot.blocked), known_chats=frozenset(bot.known_chats))

    def set_webhook(self, bot_id: int, url: str | None = "https://webhook.invalid/cq") -> None:
        """Liga (ou desliga, com `None`) um webhook, como faria um terceiro: o long poll em aberto
        recebe 409 `terminated by setWebhook request` e os `getUpdates` seguintes 409 de webhook."""
        with self._cond:
            bot = self._bot(bot_id)
            bot.webhook_url = url
            if url:
                for poll in list(bot.polls):
                    self._abort_poll(poll, ERRORS["conflict_set_webhook"], by=None)
            self._cond.notify_all()

    def block_chat(self, bot_id: int, chat: Any, *, deactivated: bool = False) -> None:
        """`sendMessage` para o chat passa a dar 403 (`bot was blocked by the user`, ou `user is
        deactivated`). `chat` é o id ou o remetente."""
        with self._cond:
            kind = "deactivated" if deactivated else "blocked"
            self._bot(bot_id).blocked[_chat_id_of(chat)] = kind

    def unblock_chat(self, bot_id: int, chat: Any) -> None:
        with self._cond:
            self._bot(bot_id).blocked.pop(_chat_id_of(chat), None)

    # -- injeção de updates --------------------------------------------------------------------

    def _date(self, date: int | datetime | None) -> int:
        if date is None:
            date = self._clock.now()
        if isinstance(date, datetime):
            return int(date.timestamp())
        return int(date)

    def _next_message_id(self, bot: _Bot, chat_id: int) -> int:
        bot.message_ids[chat_id] = bot.message_ids.get(chat_id, 0) + 1
        return bot.message_ids[chat_id]

    def _enqueue(self, bot: _Bot, payload: Mapping[str, Any], update_id: int | None) -> int | None:
        kind = _update_type(payload)
        chat = _update_chat(payload)
        if chat is not None and _is_int(chat.get("id")):
            bot.known_chats[chat["id"]] = str(chat.get("type") or "private")
        if kind not in bot.allowed:
            bot.dropped += 1  # descartado na geração, sem consumir número (Client.cpp:18392)
            self._cond.notify_all()
            return None
        if update_id is None:
            update_id = bot.next_id
            bot.next_id += 1
        else:
            if not _is_int(update_id) or not 0 < update_id <= INT32_MAX:
                raise ValueError("update_id fora de 1 a 2^31 - 1")
            if any(u["update_id"] == update_id for u in bot.queue):
                raise ValueError("update_id já está na fila")
            bot.next_id = max(bot.next_id, update_id + 1)
        update = {"update_id": update_id, **{k: copy.deepcopy(v) for k, v in payload.items()
                                             if k != "update_id"}}
        ids = [u["update_id"] for u in bot.queue]
        bot.queue.insert(bisect.bisect_left(ids, update_id), update)
        bot.history[update_id] = update
        self._cond.notify_all()
        return update_id

    def push_update(self, bot_id: int, update: Mapping[str, Any], *,
                    update_id: int | None = None) -> int | None:
        """Injeta um update cru (um só campo além de `update_id`). O `update_id` vem da sequência
        do bot, do argumento ou do próprio `update`. Devolve o `update_id`, ou `None` se o tipo está
        fora do `allowed_updates` atual (descartado na geração, como no servidor)."""
        if update_id is None and "update_id" in update:
            update_id = update["update_id"]
        with self._cond:
            return self._enqueue(self._bot(bot_id), update, update_id)

    def push_text(self, bot_id: int, sender: Any, text: str, *,
                  entities: list[dict[str, Any]] | None = None, bot_command: bool = False,
                  date: int | datetime | None = None, chat: Mapping[str, Any] | None = None,
                  extra: Mapping[str, Any] | None = None,
                  reply_to_message_id: int | None = None) -> int | None:
        """Mensagem de texto de `sender` (objeto com `as_user()` e `as_private_chat()`).

        Sem `entities`, o texto não tem entidade (um `/ajuda` assim não é comando); com
        `bot_command=True`, ganha a entidade `bot_command` no deslocamento 0 sobre a 1ª palavra (em
        unidades UTF-16). `chat` troca o chat privado (ex.: grupo); `extra` acrescenta campos à
        mensagem (ex.: `reply_to_message`, `forward_origin`). `date` padrão: o relógio do dublê.

        Com a extensão, `reply_to_message_id` faz do texto uma resposta ("responder") à mensagem
        do bot com esse id no mesmo chat: a mensagem ganha `reply_to_message` com `message_id`,
        `from` (o bot), `chat`, `date` e `text`. Junto com um `reply_to_message` em `extra`, é erro.
        """
        if reply_to_message_id is not None:
            if not _is_int(reply_to_message_id) or reply_to_message_id <= 0:
                raise ValueError("reply_to_message_id deve ser um inteiro positivo")
            if extra is not None and "reply_to_message" in extra:
                raise ValueError("reply_to_message_id e extra['reply_to_message'] juntos")
            with self._cond:
                bot = self._bot(bot_id)
                the_chat = dict(chat) if chat is not None else sender.as_private_chat()
                replied = {"reply_to_message": {
                    "message_id": reply_to_message_id,
                    "from": bot.user(),
                    "chat": the_chat,
                    "date": self._date(date),
                    "text": "Mensagem do bot",
                }}
            extra = {**dict(extra or {}), **replied}
        with self._cond:
            bot = self._bot(bot_id)
            the_chat = dict(chat) if chat is not None else sender.as_private_chat()
            message: dict[str, Any] = {
                "message_id": self._next_message_id(bot, the_chat["id"]),
                "from": sender.as_user(),
                "chat": the_chat,
                "date": self._date(date),
                "text": text,
            }
            all_entities: list[dict[str, Any]] = []
            if bot_command:
                all_entities.append({"offset": 0, "length": _utf16_len(text.split(" ", 1)[0]),
                                     "type": "bot_command"})
            all_entities.extend(copy.deepcopy(entities or []))
            if all_entities:
                message["entities"] = all_entities
            message.update(copy.deepcopy(dict(extra or {})))
            return self._enqueue(bot, {"message": message}, None)

    def push_start(self, bot_id: int, sender: Any, code: str | None = None, *,
                   username: str | None = None, date: int | datetime | None = None,
                   chat: Mapping[str, Any] | None = None) -> int | None:
        """`/start` (ou `/start@<username>`) com o código do deep link, com a entidade `bot_command`
        (`{"offset": 0, "length": 6, "type": "bot_command"}` no `/start` simples)."""
        text = "/start" + (f"@{username}" if username is not None else "")
        if code is not None:
            text += f" {code}"
        return self.push_text(bot_id, sender, text, bot_command=True, date=date, chat=chat)

    def push_callback(self, bot_id: int, sender: Any, data: str, *, message_id: int | None = None,
                      chat: Mapping[str, Any] | None = None,
                      date: int | datetime | None = None) -> int | None:
        """`callback_query` de um botão numa mensagem do bot no chat privado de `sender`."""
        with self._cond:
            bot = self._bot(bot_id)
            the_chat = dict(chat) if chat is not None else sender.as_private_chat()
            n = next(self._callback_ids)
            query = {
                "id": str(9_900_000_000_000_000 + n),
                "from": sender.as_user(),
                "message": {
                    "message_id": (message_id if message_id is not None
                                   else self._next_message_id(bot, the_chat["id"])),
                    "from": bot.user(),
                    "chat": the_chat,
                    "date": self._date(date),
                    "text": "Mensagem do bot",
                },
                "chat_instance": str(-(8_800_000_000_000_000 + n)),
                "data": data,
            }
            return self._enqueue(bot, {"callback_query": query}, None)

    def push_my_chat_member(self, bot_id: int, sender: Any, *, old_status: str = "member",
                            new_status: str = "kicked",
                            date: int | datetime | None = None) -> int | None:
        """`my_chat_member` do chat privado (o padrão é o bloqueio: `member` → `kicked`, com
        `until_date: 0`). `kicked` também bloqueia o chat para `sendMessage`; `member` o
        desbloqueia."""
        with self._cond:
            bot = self._bot(bot_id)
            chat = sender.as_private_chat()

            def member(status: str) -> dict[str, Any]:
                data: dict[str, Any] = {"user": bot.user(), "status": status}
                if status == "kicked":
                    data["until_date"] = 0
                return data

            update = {"my_chat_member": {
                "chat": chat,
                "from": sender.as_user(),
                "date": self._date(date),
                "old_chat_member": member(old_status),
                "new_chat_member": member(new_status),
            }}
            if new_status == "kicked":
                bot.blocked[chat["id"]] = "blocked"
            elif new_status == "member":
                bot.blocked.pop(chat["id"], None)
            return self._enqueue(bot, update, None)

    def redeliver(self, bot_id: int, update_id: int) -> None:
        """Entrega de novo, uma vez, um update já injetado (mesmo `update_id` e conteúdo) na próxima
        resposta de `getUpdates` do bot, qualquer que seja o `offset`."""
        with self._cond:
            bot = self._bot(bot_id)
            if update_id not in bot.history:
                raise ValueError("update_id nunca foi injetado neste bot")
            bot.redeliveries.append(copy.deepcopy(bot.history[update_id]))
            self._cond.notify_all()

    def restart_sequence(self, bot_id: int, start: int) -> None:
        """Recomeça a numeração em `start`, como depois de uma semana sem updates: a fila é
        esvaziada (no servidor ela já está vazia nesse caso) e o próximo update recebe `start`.
        Chame depois que o sistema já gravou o que foi injetado."""
        if not _is_int(start) or not 0 < start <= INT32_MAX - MAX_QUEUE_EVENTS:
            raise ValueError("start fora do inteiro de 32 bits")
        with self._cond:
            bot = self._bot(bot_id)
            bot.queue.clear()
            bot.redeliveries.clear()
            bot.next_id = start
            self._cond.notify_all()

    # -- falhas programadas --------------------------------------------------------------------

    def _check_method(self, method: str) -> None:
        """O método tem de estar em `_method_table()` (os do dublê base
        e, com a extensão instalada, também os dela); sem extensão, a regra do dublê base."""
        with self._cond:
            known = method in self._method_table().values()
        if method != "*" and not known:
            raise ValueError(f"método fora do dublê: {method}")

    def fail(self, method: str, *, status: int | None = None, description: str | None = None,
             retry_after: int | None = None, times: int | None = 1,
             when: Callable[[BotApiCall], bool] | None = None, bot_id: int | None = None,
             error: str | None = None, after_effect: bool = False) -> None:
        """As próximas `times` chamadas a `method` (`"*"` = qualquer; `times=None` = até
        `clear_failures`) que casam com `bot_id` e `when(call)` recebem o erro.

        `error` é uma chave de `ERRORS`; `retry_after` sozinho dá o 429 oficial com
        `parameters.retry_after` e `Retry-After`; `status=429` sem `retry_after` dá 429 sem
        `parameters`. Sem `description`, vale a do servidor para o status.

        Extensão: `after_effect=True` aplica antes o efeito do método (a extensão atende o
        pedido: a mensagem passa a existir, com o teclado) e só então responde a falha, se o token,
        o bot e os parâmetros permitirem o efeito. Só com a extensão instalada e num método que ela
        atende (`ValueError` sem ela, com `"*"` ou num método que ela não atende).
        """
        self._check_method(method)
        if after_effect:
            with self._cond:
                extension = self._extension
            if extension is None or method == "*" or method not in extension.handles:
                raise ValueError("after_effect só com a extensão instalada e num método dela")
        if error is not None:
            err_status, err_description = ERRORS[error]
            status = err_status if status is None else status
            description = err_description if description is None else description
        if retry_after is not None and status is None:
            status = 429
        if status is None:
            raise ValueError("fail: informe status, error ou retry_after")
        if description is None:
            description = (too_many_requests(retry_after)[1] if status == 429 and retry_after
                           is not None else _default_description(status))
        self._add_failure(_Failure(method=method, kind="http", times=times, when=when,
                                   bot_id=bot_id, status=status, description=description,
                                   retry_after=retry_after, after_effect=after_effect))

    def fail_network(self, method: str, *, kind: str, times: int | None = 1,
                     when: Callable[[BotApiCall], bool] | None = None, bot_id: int | None = None,
                     status: int = 502) -> None:
        """Falha de rede nas próximas chamadas: `reset` (corta a conexão com RST, sem resposta),
        `hang` (não responde até `release_hangs`, `reset`, `close` ou o cliente desistir; ao soltar,
        fecha a conexão sem resposta) ou `not_json` (`status` com corpo HTML, como um proxy)."""
        self._check_method(method)
        if kind not in ("reset", "hang", "not_json"):
            raise ValueError("kind deve ser reset, hang ou not_json")
        self._add_failure(_Failure(method=method, kind=kind, times=times, when=when,
                                   bot_id=bot_id, status=status))

    def _add_failure(self, failure: _Failure) -> None:
        if failure.times is not None and (not _is_int(failure.times) or failure.times < 1):
            raise ValueError("times deve ser um inteiro positivo ou None")
        with self._cond:
            self._failures.append(failure)

    def release_hangs(self) -> None:
        """Solta as respostas presas de todas as chamadas já recebidas (a conexão é fechada sem
        resposta), inclusive a que ainda está entrando na espera; as chamadas seguintes que casarem
        com um `fail_network(kind="hang")` voltam a ficar presas."""
        with self._cond:
            self._released_through = self._last_seq
            self._cond.notify_all()

    def clear_failures(self) -> None:
        """Remove as falhas programadas que ainda não foram usadas."""
        with self._cond:
            self._failures.clear()

    # -- observação ----------------------------------------------------------------------------

    def on_call(self, method: str, callback: Callable[[BotApiCall], None] | None) -> None:
        """Registra um gancho chamado na chegada de cada chamada a `method` (`"*"` = todas), antes
        do processamento, fora do lock do dublê, com o `BotApiCall` do pedido. Uma exceção no gancho
        vira erro de protocolo. `callback=None` remove os ganchos do método."""
        self._check_method(method)
        with self._cond:
            if callback is None:
                self._hooks.pop(method, None)
            else:
                self._hooks.setdefault(method, []).append(callback)

    def calls(self, method: str | None = None, *, bot_id: int | None = None) -> list[BotApiCall]:
        """As chamadas recebidas desde o último `reset`, na ordem de chegada (inclui as abertas)."""
        with self._cond:
            return [c.snapshot() for c in self._calls
                    if (method is None or c.method == method)
                    and (bot_id is None or c.bot_id == bot_id)]

    def sent_messages(self, bot_id: int | None = None, *,
                      chat_id: int | None = None) -> list[SentMessage]:
        with self._cond:
            return [m for m in self._sent if (bot_id is None or m.bot_id == bot_id)
                    and (chat_id is None or m.chat_id == chat_id)]

    def transcript(self) -> str:
        """Uma linha por chamada (sem token nem valores de parâmetros), para mensagens de falha."""
        with self._cond:
            calls = [c.snapshot() for c in self._calls]
        if not calls:
            return "(nenhuma chamada)"
        t0 = calls[0].started
        lines = []
        for c in calls:
            end = "aberta" if c.ended is None else f"{c.ended - t0:.3f}"
            lines.append(f"#{c.seq} {c.method or '?'} bot={c.bot_id} v={c.token_version} "
                         f"token={c.token_state} {c.outcome} status={c.status} "
                         f"{c.started - t0:.3f}..{end} params={sorted(c.params)}")
        return "\n".join(lines)

    def wait_for(self, predicate: Callable[[], T], *, timeout: float = 5.0) -> T:
        """Espera `predicate()` ficar verdadeiro e devolve o valor; `AssertionError` no tempo."""
        deadline = monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - monotonic()
            if remaining <= 0:
                tail = "\n".join(self.transcript().splitlines()[-20:])
                raise AssertionError(f"wait_for: condição não satisfeita em {timeout} s\n{tail}")
            with self._cond:
                self._cond.wait(min(SOCKET_CHECK_SECONDS, remaining))

    def _polls_of(self, bot_id: int | None, exclude_abandoned: bool) -> list[BotApiCall]:
        return [c for c in self.calls("getUpdates", bot_id=bot_id) if c.bot_id is not None
                and not (exclude_abandoned and c.outcome == "abandoned")]

    def max_concurrency(self, bot_id: int, *, exclude_abandoned: bool = False) -> int:
        """O maior número de `getUpdates` do bot abertos ao mesmo tempo, sobre os intervalos
        fechados `[started, ended]` (aberto = até agora); só `getUpdates`."""
        events: list[tuple[float, int]] = []
        for c in self._polls_of(bot_id, exclude_abandoned):
            events.append((c.started, 0))
            events.append((c.ended if c.ended is not None else math.inf, 1))
        events.sort()
        current = best = 0
        for _, kind in events:
            if kind == 0:
                current += 1
                best = max(best, current)
            else:
                current -= 1
        return best

    def overlaps(self, bot_id: int | None = None, *,
                 exclude_abandoned: bool = False) -> list[tuple[BotApiCall, BotApiCall]]:
        """Pares `(anterior, posterior)` de `getUpdates` do mesmo bot com intervalos sobrepostos."""
        calls = sorted(self._polls_of(bot_id, exclude_abandoned), key=lambda c: (c.started, c.seq))
        pairs: list[tuple[BotApiCall, BotApiCall]] = []
        for i, a in enumerate(calls):
            a_end = a.ended if a.ended is not None else math.inf
            for b in calls[i + 1:]:
                if b.started > a_end:
                    break
                if b.bot_id == a.bot_id:
                    pairs.append((a, b))
        return pairs

    def conflicts(self, bot_id: int | None = None) -> list[tuple[BotApiCall, BotApiCall]]:
        """Pares `(encerrado, causador)`: o `getUpdates` em espera que um posterior do mesmo bot
        encerrou com 409 (o `outcome` do encerrado diz se o 409 chegou ou se ele foi abandonado)."""
        calls = self.calls("getUpdates", bot_id=bot_id)
        by_seq = {c.seq: c for c in self.calls("getUpdates")}
        return [(c, by_seq[c.conflicted_by]) for c in calls if c.conflicted_by in by_seq]

    def pending_polls(self, bot_id: int | None = None) -> list[BotApiCall]:
        """Os long polls que esperam updates agora (sem os já encerrados por conflito)."""
        with self._cond:
            polls = [p.call for bot in self._bots.values() for p in bot.polls
                     if bot_id is None or bot.bot_id == bot_id]
            return [c.snapshot() for c in sorted(polls, key=lambda c: c.seq)]

    def protocol_errors(self) -> list[str]:
        with self._cond:
            return list(self._problems)

    # -- cliente do lado do teste --------------------------------------------------------------

    def request(self, token: str, method: str, params: Mapping[str, Any] | None = None, *,
                timeout: float = 10.0, http_method: str = "POST", query: str = "",
                body: bytes | None = None, content_type: str = "application/json",
                path: str | None = None) -> Reply:
        """Faz um pedido ao dublê como um cliente qualquer (ex.: o consumidor externo do teste).

        Sem `body`, envia `params` em JSON; `path` substitui o caminho inteiro.
        """
        target = path if path is not None else f"/bot{token}/{method}"
        if query:
            target += f"?{query}"
        if body is None and params is not None:
            body = json.dumps(dict(params)).encode("utf-8")
        headers = {"Content-Type": content_type} if body is not None else {}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(http_method, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return Reply(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        finally:
            conn.close()

    # -- internos: conexões e registro ---------------------------------------------------------

    def _register_connection(self, sock: socket.socket) -> int:
        with self._cond:
            self._connections.add(sock)
            return next(self._conn_ids)

    def _unregister_connection(self, sock: socket.socket) -> None:
        with self._cond:
            self._connections.discard(sock)

    def _scrub(self, text: str) -> str:
        for token in self._all_tokens:
            text = text.replace(token, "[token]").replace(token.split(":", 1)[1], "[token]")
        return text

    def _problem(self, gen: int, text: str) -> None:
        with self._cond:
            if gen == self._gen:
                self._problems.append(self._scrub(text))

    def _finish(self, call: _Call, outcome: str, *, status: int | None = None,
                description: str | None = None, result: Any = None,
                retry_after: int | None = None, programmed: bool = False) -> None:
        with self._cond:
            call.ended = monotonic()
            call.outcome = outcome
            call.status = status
            call.description = description
            call.result = copy.deepcopy(result)
            call.retry_after = retry_after
            call.programmed = programmed
            call.touch()
            self._cond.notify_all()

    def _reply_ok(self, h: _Handler, call: _Call, result: Any,
                  description: str | None = None) -> None:
        payload: dict[str, Any] = {"ok": True, "result": result}
        if description is not None:
            payload["description"] = description
        self._finish(call, "ok", status=200, result=result)
        _write(h, 200, json.dumps(payload).encode("utf-8"), "application/json")

    def _reply_error(self, h: _Handler, call: _Call, error: tuple[int, str], *,
                     retry_after: int | None = None, programmed: bool = False) -> None:
        status, description = error
        payload: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
        headers: tuple[tuple[str, str], ...] = ()
        if retry_after is not None:
            payload["parameters"] = {"retry_after": retry_after}
            headers = (("Retry-After", str(retry_after)),)
        self._finish(call, "error", status=status, description=description,
                     retry_after=retry_after, programmed=programmed)
        _write(h, status, json.dumps(payload).encode("utf-8"), "application/json", headers)

    def _abandon(self, h: _Handler, call: _Call, *, outcome: str = "abandoned",
                 programmed: bool = False) -> None:
        self._finish(call, outcome, programmed=programmed)
        h.close_connection = True

    def _block(self, h: _Handler, decide: Callable[[], T | None]) -> T | None:
        """Espera `decide()` (chamado sob o lock) devolver algo; `None` se o cliente foi embora.

        O socket é conferido a cada `SOCKET_CHECK_SECONDS` (e a cada aviso do `Condition`)."""
        while True:
            with self._cond:
                decision = decide()
                if decision is not None:
                    return decision
                self._cond.wait(SOCKET_CHECK_SECONDS)
            if _client_gone(h.connection):
                return None

    # -- internos: atendimento -----------------------------------------------------------------

    def _serve(self, h: _Handler) -> None:
        started = monotonic()
        with self._cond:
            if self._closing:
                closing = True
            else:
                closing = False
                self._active += 1
        if closing:
            h.close_connection = True
            status, description = ERRORS["server_restart"]
            _write(h, status, json.dumps({"ok": False, "error_code": status,
                                          "description": description}).encode("utf-8"),
                   "application/json")
            return
        gen = self._gen
        h.fake_call = None
        try:
            self._serve_inner(h, started)
        except Exception as exc:  # noqa: BLE001 - erro do próprio dublê: acusado, nunca impresso
            call = h.fake_call
            prefix = f"{call.method or '?'}: " if call is not None else ""
            self._problem(gen, f"{prefix}erro interno do dublê: {type(exc).__name__}: {exc}")
            h.close_connection = True
            status, description = 500, "Internal Server Error: fake bot api"
            if call is not None and call.ended is None:
                self._finish(call, "error", status=status, description=description)
            _write(h, status, json.dumps({"ok": False, "error_code": status,
                                          "description": description}).encode("utf-8"),
                   "application/json")
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def _token_state(self, token: str) -> tuple[str, int | None, int | None,
                                                tuple[int, str] | None]:
        """(estado, bot_id, versão, erro imediato) pela regra do `ClientManager`.
"""
        if not token or token[0] == "0" or len(token) > 80 or ":" not in token:
            return "malformed", None, None, ERRORS["invalid_token"]
        prefix = token.split(":", 1)[0]
        if not re.fullmatch(r"-?[0-9]{1,19}", prefix) or not -(2**63) <= int(prefix) < 2**63:
            return "malformed", None, None, ERRORS["forbidden_token"]
        user_id = int(prefix)
        if user_id <= 0 or user_id >= 2**54:
            return "malformed", None, None, ERRORS["invalid_token"]
        entry = self._tokens.get(token)
        if entry is None:
            return "unknown", user_id, None, None
        bot_id, version = entry
        bot = self._bots.get(bot_id)
        if bot is not None and not bot.revoked and bot.token_version == version:
            return "valid", bot_id, version, None
        return "revoked", bot_id, version, None

    def _serve_inner(self, h: _Handler, started: float) -> None:
        split = urllib.parse.urlsplit(h.path)
        body, problems = _read_body(h)
        content_type = h.headers.get("Content-Type")
        media = (content_type or "").split(";")[0].strip().lower()
        params: dict[str, Any] = {}
        bad_body = False
        if h.command != "POST":
            problems.append(f"método HTTP {h.command} (esperado POST)")
        if body:
            if media == "application/json":
                charset = re.search(r"charset=([^;\s]+)", content_type or "", re.IGNORECASE)
                if charset and charset.group(1).strip("\"'").lower() not in ("utf-8", "utf8"):
                    problems.append("corpo JSON fora de UTF-8")
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    problems.append("corpo não é JSON UTF-8 válido")
                    bad_body = True
                else:
                    if isinstance(decoded, dict):
                        params = decoded
                    else:
                        problems.append("corpo JSON não é um objeto")
                        bad_body = True
            elif media == "multipart/form-data":
                upload = self._upload_method(split.path)  # só com a extensão
                if upload is None:
                    problems.append("corpo multipart/form-data (esperado JSON)")
                else:
                    params, upload_problems = upload[1].parse_multipart(upload[0],
                                                                        content_type or "", body)
                    problems.extend(upload_problems)
            elif media == "application/x-www-form-urlencoded":
                problems.append("corpo de formulário (esperado JSON)")
                params = dict(urllib.parse.parse_qsl(body.decode("utf-8", "replace")))
            else:
                problems.append(f"Content-Type não suportado: {media or 'ausente'}")
        if split.query:
            problems.append("parâmetros na consulta da URL")
            params = {**dict(urllib.parse.parse_qsl(split.query)), **params}

        parsed = _parse_path(split.path)
        bad_request: str | None = None
        with self._cond:
            gen = self._gen
            seq = next(self._seq)
            clock_at = self._clock.now()
            early: tuple[int, str] | None = None
            canonical: str | None = None
            extension = self._extension  # extensão instalada, se houver
            if parsed is None:
                method = ""
                token_state, bot_id, version = "malformed", None, None
                early = ERRORS["not_found"]
                problems.append("caminho fora de /bot<token>/<método>")
            else:
                token, is_test, requested = parsed
                canonical = self._method_table().get(requested.lower())
                method = canonical or self._scrub(requested)[:64]
                token_state, bot_id, version, token_error = self._token_state(token)
                if is_test:
                    problems.append("ambiente de teste /test/ do Telegram")
                    early = ERRORS["method_not_found"]
                elif token_error is not None:
                    early = token_error
                if canonical is None:
                    if requested.lower() in {m.lower() for m in FORBIDDEN_METHODS}:
                        problems.append(f"chamada proibida ao método {method}")
                    else:
                        problems.append("método desconhecido ou fora do dublê")
                elif not bad_body:
                    param_problems, bad_request = _check_params(canonical, params,
                                                                extension=extension)
                    problems.extend(param_problems)
            call = _Call(seq=seq, gen=gen, method=method, bot_id=bot_id, token_state=token_state,
                         token_version=version, params=copy.deepcopy(params),
                         http_method=h.command, content_type=content_type,
                         has_query=bool(split.query), connection_id=h.connection_id,
                         started=started, clock_at=clock_at)
            self._calls.append(call)
            self._last_seq = seq
            h.fake_call = call
            for problem in problems:
                self._problems.append(self._scrub(f"{method or '?'}: {problem}"))
            hooks = [*self._hooks.get(canonical or "", ()), *self._hooks.get("*", ())]
            snapshot = call.snapshot()
            self._cond.notify_all()

        for hook in hooks:
            try:
                hook(snapshot)
            except Exception as exc:  # noqa: BLE001 - vira erro de protocolo
                self._problem(gen, f"{method}: gancho on_call falhou: {type(exc).__name__}: {exc}")

        if early is not None:
            return self._reply_error(h, call, early)
        if bad_body:
            return self._reply_error(h, call, (400, "Bad Request: can't parse JSON object"))
        failure = self._take_failure(call)
        if failure is not None:
            if failure.after_effect:  # extensão: o efeito antes da falha
                self._apply_effect(call, extension, canonical, token_state, bad_request, params)
            return self._apply_failure(h, call, failure)
        if token_state != "valid":
            return self._reply_error(h, call, ERRORS["unauthorized"])
        if canonical is None:
            return self._reply_error(h, call, ERRORS["method_not_found"])
        with self._cond:
            bot = self._bots.get(bot_id) if gen == self._gen else None
        if bot is None:
            return self._reply_error(h, call, ERRORS["unauthorized"])
        if bad_request is not None:
            return self._reply_error(h, call, (400, bad_request))
        if extension is not None and canonical in extension.handles:  # extensão
            reply = extension.handle(canonical, call.snapshot(), bot.bot_id, params)
            if reply.error is not None:
                return self._reply_error(h, call, reply.error)
            return self._reply_ok(h, call, reply.result, reply.description)
        handler = getattr(self, f"_m_{canonical}")
        return handler(h, call, bot, params)

    def _apply_effect(self, call: _Call, extension: Any, canonical: str | None, token_state: str,
                      bad_request: str | None, params: dict[str, Any]) -> None:
        """Extensão: o efeito do pedido pela extensão, sem resposta (`fail(…, after_effect=True)`).

        Só acontece se o pedido teria efeito: token válido, bot existente, parâmetros aceitos e
        método atendido pela extensão; o desfecho do efeito (inclusive um erro dele) é descartado,
        e quem responde é a falha programada."""
        if (extension is None or canonical is None or canonical not in extension.handles
                or token_state != "valid" or bad_request is not None):
            return
        with self._cond:
            bot = self._bots.get(call.bot_id) if call.gen == self._gen else None
        if bot is None:
            return
        extension.handle(canonical, call.snapshot(), bot.bot_id, params)

    def _take_failure(self, call: _Call) -> _Failure | None:
        with self._cond:
            if call.gen != self._gen:
                return None
            snapshot = call.snapshot()
            for failure in self._failures:
                if failure.method not in ("*", call.method):
                    continue
                if failure.bot_id is not None and failure.bot_id != call.bot_id:
                    continue
                if failure.when is not None:
                    try:
                        if not failure.when(snapshot):
                            continue
                    except Exception as exc:  # noqa: BLE001 - vira erro de protocolo
                        self._problems.append(self._scrub(
                            f"{call.method}: predicado de fail falhou: "
                            f"{type(exc).__name__}: {exc}"))
                        continue
                if failure.times is not None:
                    failure.times -= 1
                    if failure.times == 0:
                        self._failures.remove(failure)
                return copy.copy(failure)
            return None

    def _apply_failure(self, h: _Handler, call: _Call, failure: _Failure) -> None:
        if failure.kind == "http":
            assert failure.status is not None and failure.description is not None
            return self._reply_error(h, call, (failure.status, failure.description),
                                     retry_after=failure.retry_after, programmed=True)
        if failure.kind == "not_json":
            self._finish(call, "error", status=failure.status, programmed=True)
            _write(h, int(failure.status or 502), HTML_BAD_GATEWAY, "text/html")
            return None
        if failure.kind == "reset":
            self._finish(call, "reset", programmed=True)
            h.close_connection = True
            try:
                linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
                h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                h.connection.close()
            except OSError:
                pass
            return None
        # hang
        hang = _Hang(call.gen, call.seq)

        def decide() -> bool | None:
            if (hang.gen != self._gen or self._closing
                    or hang.seq <= self._released_through):
                return True
            return None

        self._block(h, decide)
        self._abandon(h, call, outcome="hang", programmed=True)
        return None

    # -- métodos da Bot API --------------------------------------------------------------------

    def _m_getMe(self, h: _Handler, call: _Call, bot: _Bot, params: dict[str, Any]) -> None:
        with self._cond:
            user: dict[str, Any] = {"id": bot.bot_id, "is_bot": bot.is_bot,
                                    "first_name": bot.first_name}
            if bot.username is not None:
                user["username"] = bot.username
            user.update({"can_join_groups": True, "can_read_all_group_messages": False,
                         "supports_inline_queries": False})
            user.update(copy.deepcopy(bot.extra))
        self._reply_ok(h, call, user)

    def _m_getWebhookInfo(self, h: _Handler, call: _Call, bot: _Bot,
                          params: dict[str, Any]) -> None:
        with self._cond:
            info: dict[str, Any] = {"url": bot.webhook_url or "", "has_custom_certificate": False,
                                    "pending_update_count": len(bot.queue)}
            if bot.webhook_url:
                info["max_connections"] = 40
        self._reply_ok(h, call, info)

    def _m_deleteWebhook(self, h: _Handler, call: _Call, bot: _Bot,
                         params: dict[str, Any]) -> None:
        with self._cond:
            if params.get("drop_pending_updates") is True:
                bot.queue.clear()
                bot.redeliveries.clear()
            had = bot.webhook_url is not None
            bot.webhook_url = None
            self._cond.notify_all()
        self._reply_ok(h, call, True,
                       "Webhook was deleted" if had else "Webhook is already deleted")

    def _m_setMyDescription(self, h: _Handler, call: _Call, bot: _Bot,
                            params: dict[str, Any]) -> None:
        with self._cond:
            bot.description = params.get("description", "")
        self._reply_ok(h, call, True)

    def _m_setMyCommands(self, h: _Handler, call: _Call, bot: _Bot,
                         params: dict[str, Any]) -> None:
        with self._cond:
            bot.commands = copy.deepcopy(params["commands"])
        self._reply_ok(h, call, True)

    def _m_answerCallbackQuery(self, h: _Handler, call: _Call, bot: _Bot,
                               params: dict[str, Any]) -> None:
        self._reply_ok(h, call, True)

    def _m_sendMessage(self, h: _Handler, call: _Call, bot: _Bot, params: dict[str, Any]) -> None:
        raw = params["chat_id"]
        chat_id: int | None = raw if _is_int(raw) else None
        if isinstance(raw, str) and re.fullmatch(r"-?[0-9]{1,19}", raw):
            chat_id = int(raw)
        with self._cond:
            if chat_id is not None and chat_id in bot.blocked:
                error = ERRORS["user_deactivated" if bot.blocked[chat_id] == "deactivated"
                               else "blocked"]
                message = None
            elif chat_id is None or chat_id not in bot.known_chats:
                error = ERRORS["chat_not_found"]
                message = None
            else:
                error = None
                message = {
                    "message_id": self._next_message_id(bot, chat_id),
                    "from": bot.user(),
                    "chat": {"id": chat_id, "type": bot.known_chats[chat_id]},
                    "date": self._date(None),
                    "text": params["text"],
                }
                if call.gen == self._gen:
                    self._sent.append(SentMessage(bot.bot_id, chat_id, message["message_id"],
                                                  params["text"], call.clock_at, call.seq))
        if error is not None:
            return self._reply_error(h, call, error)
        return self._reply_ok(h, call, message)

    # -- getUpdates ----------------------------------------------------------------------------

    def _conflict_time(self, bot: _Bot) -> float:
        """Instante do próximo 409 de conflito (`Client::fail_query_conflict`)."""
        now = monotonic()
        if now >= bot.next_conflict_time:
            bot.next_conflict_time = now + self._conflict_delay
            return now
        return now + self._conflict_delay

    def _abort_poll(self, poll: _Poll, error: tuple[int, str], *, by: _Call | None) -> None:
        if poll in poll.bot.polls:
            poll.bot.polls.remove(poll)
        poll.abort = error
        poll.respond_at = self._conflict_time(poll.bot)
        if by is not None:
            poll.call.conflicted_by = by.seq
            poll.call.touch()
        self._cond.notify_all()

    @staticmethod
    def _take_batch(bot: _Bot, offset: int, limit: int) -> tuple[list[dict[str, Any]],
                                                                 list[dict[str, Any]]]:
        """A regra do `do_get_updates` + `TQueue::get`: esquece o que está
        abaixo de `from` e devolve até `limit` updates a partir dele, com as reentregas."""
        queue = bot.queue
        if offset < 0:
            keep = -offset
            if len(queue) > keep:
                del queue[:len(queue) - keep]
        head = queue[0]["update_id"] if queue else bot.next_id
        if offset <= 0 or offset > bot.next_id + 10 or offset < head - MAX_QUEUE_EVENTS:
            start = head
        else:
            start = offset
        ids = [u["update_id"] for u in queue]
        del queue[:bisect.bisect_left(ids, start)]
        batch = queue[:limit]
        consumed: list[dict[str, Any]] = []
        if bot.redeliveries:
            seen = {u["update_id"] for u in batch}
            extra: list[dict[str, Any]] = []
            remaining: list[dict[str, Any]] = []
            for update in bot.redeliveries:
                if update["update_id"] in seen:
                    consumed.append(update)
                elif len(batch) + len(extra) < limit:
                    extra.append(update)
                    consumed.append(update)
                    seen.add(update["update_id"])
                else:
                    remaining.append(update)
            bot.redeliveries = remaining
            batch = sorted([*batch, *extra], key=lambda u: u["update_id"])
        return copy.deepcopy(batch), consumed

    def _m_getUpdates(self, h: _Handler, call: _Call, bot: _Bot, params: dict[str, Any]) -> None:
        offset = params.get("offset", 0)
        offset = offset if _is_int(offset) and INT32_MIN <= offset <= INT32_MAX else 0
        limit = params.get("limit", 100)
        limit = min(max(limit, 1), 100) if _is_int(limit) else 100
        timeout = params.get("timeout", 0)
        timeout = min(max(timeout, 0), 50) if _is_int(timeout) else 0

        with self._cond:
            if bot.webhook_url:
                respond_at = self._conflict_time(bot)
                webhook = True
            else:
                webhook = False
                allowed = params.get("allowed_updates")
                if isinstance(allowed, str):
                    try:
                        allowed = json.loads(allowed)
                    except ValueError:
                        allowed = None
                if isinstance(allowed, list) and all(isinstance(x, str) for x in allowed):
                    names = {x.lower() for x in allowed} & set(UPDATE_TYPES)
                    bot.allowed = frozenset(names) if names else DEFAULT_ALLOWED_UPDATES
                batch, consumed = self._take_batch(bot, offset, limit)
                immediate = bool(batch) or timeout == 0
                if not immediate:
                    for other in list(bot.polls):
                        self._abort_poll(other, ERRORS["conflict_poll"], by=call)
                    # Como o servidor: `offset <= 0` (já aplicado) vira a cabeça da fila na espera.
                    wait_offset = offset if offset > 0 else bot.next_id
                    poll = _Poll(call, bot, wait_offset, limit, call.started + timeout)
                    bot.polls.append(poll)
                    self._cond.notify_all()

        if webhook:
            return self._webhook_conflict(h, call, respond_at)
        if immediate:
            return self._reply_ok(h, call, batch)

        def decide() -> tuple[str, Any, list[dict[str, Any]]] | None:
            if call.gen != self._gen:
                return "error", ERRORS["unauthorized"], []
            if self._closing:
                return "error", ERRORS["server_restart"], []
            if poll.abort is not None:
                if monotonic() >= poll.respond_at:
                    return "error", poll.abort, []
                return None
            if bot.revoked or bot.token_version != call.token_version:
                bot.polls.remove(poll)
                return "error", ERRORS["unauthorized"], []
            found, taken = self._take_batch(bot, poll.offset, limit)
            if found:
                bot.polls.remove(poll)
                return "ok", found, taken
            if monotonic() >= poll.deadline:
                bot.polls.remove(poll)
                return "ok", [], []
            return None

        decision = self._block(h, decide)
        if decision is None or _client_gone(h.connection):
            with self._cond:
                if poll in bot.polls:
                    bot.polls.remove(poll)
                if decision is not None and decision[2] and call.gen == self._gen:
                    bot.redeliveries[:0] = decision[2]  # a reentrega não chegou: volta à fila
            return self._abandon(h, call)
        kind, value, _ = decision
        if kind == "ok":
            return self._reply_ok(h, call, value)
        return self._reply_error(h, call, value)

    def _webhook_conflict(self, h: _Handler, call: _Call, respond_at: float) -> None:
        def decide() -> bool | None:
            if call.gen != self._gen or self._closing or monotonic() >= respond_at:
                return True
            return None

        if respond_at > monotonic() and (self._block(h, decide) is None
                                              or _client_gone(h.connection)):
            return self._abandon(h, call)
        return self._reply_error(h, call, ERRORS["conflict_webhook"])


# ---------------------------------------------------------------------------------------------
# Dublê da sessão
# ---------------------------------------------------------------------------------------------

_SESSION: FakeBotApi | None = None
_SESSION_LOCK = threading.Lock()


def session_fake() -> FakeBotApi:
    """O dublê único da sessão de testes (criado na 1ª chamada, fechado na saída do processo).

    A fixture `bot_api` e os helpers sem `bot_api` (ex.: `seed_state`) usam este mesmo servidor; a
    fixture faz `reset(clock=clock)` a cada teste.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = FakeBotApi()
            atexit.register(_SESSION.close)
        return _SESSION
