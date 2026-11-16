"""Adaptador HTTP da Bot API do Telegram: a porta do Telegram sobre um transporte abortável, sem log e
sem o token em nenhuma mensagem.

- `HttpTelegramBotApi` implementa a porta `ports.telegram.TelegramBotApi` para um token: toda chamada
  é `POST {base_url}/bot{token}/{método}` com corpo JSON UTF-8 e nada na consulta, pelo transporte
  chamado direto (sem `httpx2.Client`, que registra a URL com o token), sobre um backend de rede do
  `httpcore2` que `abort()` interrompe. A resposta é decidida pelo campo `ok`, não pelo status.
- `minimize_update` é a minimização na borda (uma lista de inclusão): nome,
  sobrenome, `@username` e `language_code` nunca aparecem no resultado, em nenhum nível.
- `http_factory(settings)` é a `TelegramBotApiFactory` que o `__main__` e os testes passam ao
  `BotRuntime`.

Nenhum log, exceção formatada ou `repr` contém o token ou a URL da Bot API (a URL contém o token).
O adaptador não registra nada em log; as exceções da porta levam só uma frase fixa (e, no máximo, o
código numérico do Telegram), são levantadas fora do bloco `except` que as originou e com
`from None`: nem `__cause__` nem `__context__` carregam a exceção do transporte.

Mensagem rica e respostas:
- o transporte abortável e o vigia do prazo total moram em `adapters/_http.py` (comuns a este
  adaptador e ao do motor de questões); este módulo passa a eles o nome da thread
  do vigia e as suas frases, e importa `httpx2` e `httpcore2` (a classificação das falhas
  do transporte é daqui);
- os métodos da mensagem rica (`get_updates_with_replies`, `send_rich_message`, `edit_message_text`,
  `edit_message_reply_markup`, `answer_callback_query`, `send_photo`, `send_document`) e
  `extract_reply_to` (puro); `send_message` aceita argumentos nomeados opcionais. Só os não nulos vão
  ao corpo: sem eles, o `sendMessage` manda exatamente `{chat_id, text}`;
- os objetos (`reply_markup`, `reply_parameters`, `link_preview_options`, `rich_message`) vão aninhados
  no corpo JSON; multipart (`httpx2.Request(data=, files=)`) só em `sendPhoto` e `sendDocument`, com o
  `reply_parameters` em texto JSON;
- só no 400, a descrição (sem distinção de caixa) separa `MessageNotModified`, `EditTargetUnavailable`
  e `CallbackQueryExpired` (subclasses de `RequestRejected`); a mensagem é fixa, sem a descrição.
  `minimize_update` e `MinimalUpdate` não trazem a resposta citada (ela vem de `extract_reply_to`).
"""

from __future__ import annotations

import json
import math
import re
import threading
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

import httpcore2  # classificação das falhas do transporte
import httpx2  # pedido e tempos por fase

from cq_server.adapters._http import _AbortableTransport, _CallDeadline
from cq_server.config import BotSettings
from cq_server.ports.telegram import (
    Aborted,
    BotCommand,
    BotIdentity,
    CallbackQueryExpired,
    ChatUnreachable,
    ConcurrentConsumer,
    EditTargetUnavailable,
    MessageNotModified,
    MinimalUpdate,
    NotABot,
    RequestRejected,
    RetryAfter,
    TelegramBotApi,
    TelegramBotApiFactory,
    TelegramError,
    TelegramUnavailable,
    TokenRejected,
    UploadFile,
    WebhookActive,
)

# Parâmetros fixos de toda `getUpdates`.
ALLOWED_UPDATES = ("message", "callback_query", "my_chat_member")
GET_UPDATES_LIMIT = 100

# Os tipos de chat conhecidos; qualquer outro vira `chat_type = None`.
CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})

# Descrições do 403 que são bloqueio (comparadas sem distinção de caixa).
_BLOCKED_DESCRIPTIONS = ("blocked by the user", "user is deactivated")

# Descrições do 400 que separam as subclasses de
# `RequestRejected` (comparadas sem distinção de caixa, só no 400).
_NOT_MODIFIED_DESCRIPTIONS = ("message is not modified",)
_EDIT_TARGET_DESCRIPTIONS = ("message to edit not found", "message can't be edited")
_QUERY_EXPIRED_DESCRIPTIONS = ("query is too old",)

# U+0000 e substitutos UTF-16 (os isolados viram U+FFFD).
_UNSAFE_CHARS = re.compile("[\x00\ud800-\udfff]")

# Frases fixas das exceções (nunca com token, URL, corpo ou descrição do Telegram).
_MSG_ABORTED = "chamada à Bot API interrompida por abort()"
_MSG_TIMEOUT = "tempo esgotado na chamada à Bot API"
_MSG_NETWORK = "falha de rede na chamada à Bot API"
_MSG_NOT_JSON = "resposta da Bot API sem JSON com o campo ok"
_MSG_MALFORMED = "resposta da Bot API fora do formato documentado"
_MSG_NOT_A_BOT = "o token não é de um bot com nome de usuário"
_MSG_DEADLINE = "prazo total da chamada à Bot API vencido"

# Nome fixo da thread que vigia o prazo total do `getUpdates` (nunca com o token).
DEADLINE_THREAD_NAME = "cq-telegram-poll-deadline"


# ---------------------------------------------------------------------------------------------
# Minimização
# ---------------------------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """Inteiro JSON de verdade (`bool` não conta)."""
    return type(value) is int


def _int_or_none(value: Any) -> int | None:
    return value if _is_int(value) else None


def _clean_text(value: Any) -> str | None:
    """O texto como chegou, com U+0000 e cada substituto UTF-16 isolado trocados por U+FFFD.

    Um par alto+baixo que chegue como dois pontos de código separados continua sendo o mesmo par
    em UTF-16 (a volta pela codificação o junta num só caractere); o resto do texto não muda.
    """
    if not isinstance(value, str):
        return None
    if _UNSAFE_CHARS.search(value) is None:
        return value
    units = value.encode("utf-16-le", "surrogatepass")
    return units.decode("utf-16-le", "replace").replace("\x00", "�")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _chat_type(chat: Mapping[str, Any]) -> str | None:
    value = chat.get("type")
    return value if isinstance(value, str) and value in CHAT_TYPES else None


def _command(text: str | None, entities: Any) -> str | None:
    """O trecho do texto coberto por uma entidade `bot_command` no deslocamento 0 (em unidades
    UTF-16), ou `None`. A posição da entidade na lista não importa."""
    if text is None or not isinstance(entities, list):
        return None
    units = text.encode("utf-16-le")
    for entity in entities:
        if not isinstance(entity, Mapping) or entity.get("type") != "bot_command":
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if not (_is_int(offset) and offset == 0 and _is_int(length)):
            continue
        if not 0 < length <= len(units) // 2:
            continue
        try:
            return units[:2 * length].decode("utf-16-le")
        except UnicodeDecodeError:
            # O comprimento corta um par substituto ao meio: a entidade não é válida.
            continue
    return None


def extract_reply_to(raw: Mapping[str, Any]) -> int | None:
    """O `message.reply_to_message.message_id` de um update cru, se é
    inteiro (não booleano) maior que 0; senão `None`. Só o `message` conta (nem `callback_query`, nem
    `edited_message`, nem outro tipo); nada mais do `reply_to_message` é lido, e o update não muda."""
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return None
    reply = message.get("reply_to_message")
    if not isinstance(reply, Mapping):
        return None
    message_id = reply.get("message_id")
    if not _is_int(message_id) or message_id <= 0:
        return None
    return message_id


def minimize_update(raw: Mapping[str, Any]) -> MinimalUpdate:
    """O update cru da Bot API reduzido à lista de inclusão da minimização (função pura).

    O tipo é o primeiro de `message`, `callback_query` e `my_chat_member` presente (a Bot API manda
    no máximo um); qualquer outro é `other`, sem colunas e com `details == {}`. Só os campos da
    lista são lidos: nada mais do update chega ao resultado, em nível nenhum. Lança `ValueError`
    (sem dados) se o update não tem `update_id` inteiro.
    """
    update_id = raw.get("update_id")
    if not _is_int(update_id):
        raise ValueError("update sem update_id inteiro")

    message = raw.get("message")
    if isinstance(message, Mapping):
        chat = _mapping(message.get("chat"))
        text = _clean_text(message.get("text"))
        return MinimalUpdate(
            update_id=update_id, kind="message",
            chat_id=_int_or_none(chat.get("id")), chat_type=_chat_type(chat),
            sender_id=_int_or_none(_mapping(message.get("from")).get("id")),
            text=text, command=_command(text, message.get("entities")),
            details={"message_id": _int_or_none(message.get("message_id")),
                     "date": _int_or_none(message.get("date"))})

    query = raw.get("callback_query")
    if isinstance(query, Mapping):
        origin = _mapping(query.get("message"))
        return MinimalUpdate(
            update_id=update_id, kind="callback_query",
            chat_id=_int_or_none(_mapping(origin.get("chat")).get("id")), chat_type=None,
            sender_id=_int_or_none(_mapping(query.get("from")).get("id")),
            text=None, command=None,
            details={"callback_id": _clean_text(query.get("id")),
                     "data": _clean_text(query.get("data")),
                     "message_id": _int_or_none(origin.get("message_id"))})

    member = raw.get("my_chat_member")
    if isinstance(member, Mapping):
        chat = _mapping(member.get("chat"))
        old_member = _mapping(member.get("old_chat_member"))
        new_member = _mapping(member.get("new_chat_member"))
        return MinimalUpdate(
            update_id=update_id, kind="my_chat_member",
            chat_id=_int_or_none(chat.get("id")), chat_type=_chat_type(chat),
            sender_id=_int_or_none(_mapping(member.get("from")).get("id")),
            text=None, command=None,
            details={"date": _int_or_none(member.get("date")),
                     "old_status": _clean_text(old_member.get("status")),
                     "new_status": _clean_text(new_member.get("status"))})

    return MinimalUpdate(update_id=update_id, kind="other", chat_id=None, chat_type=None,
                         sender_id=None, text=None, command=None, details={})


# ---------------------------------------------------------------------------------------------
# Segredo (o transporte abortável está em `adapters/_http.py`)
# ---------------------------------------------------------------------------------------------


class _Secret:
    """Guarda um valor secreto (o token, ou o prefixo de URL que o contém) com `repr` mascarado."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<oculto>"

    __str__ = __repr__


# ---------------------------------------------------------------------------------------------
# Classificação
# ---------------------------------------------------------------------------------------------


def _parse_body(body: bytes) -> Any:
    """O JSON do corpo, ou `None` se ele não é JSON (sem levantar)."""
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _error_for(payload: Mapping[str, Any], status: int) -> TelegramError:
    """A exceção da porta para uma resposta com `ok: false`: o código vem do `error_code` (ou, sem
    ele, do status HTTP, que o servidor iguala ao `error_code`); a descrição só separa o 403 de
    bloqueio, o 409 de webhook e as três subclasses do 400; nunca entra na mensagem."""
    code = payload.get("error_code")
    if not _is_int(code):
        code = status
    description = payload.get("description")
    description = description.lower() if isinstance(description, str) else ""
    if code == 401:
        return TokenRejected("o Telegram recusou o token (401)")
    if code == 403:
        if any(phrase in description for phrase in _BLOCKED_DESCRIPTIONS):
            return ChatUnreachable("o chat bloqueou o bot ou a conta foi desativada (403)")
        return RequestRejected("o Telegram recusou o pedido (403)")
    if code == 409:
        if "webhook" in description:
            return WebhookActive("há um webhook ativo no bot (409)")
        return ConcurrentConsumer("outro getUpdates do mesmo token encerrou este (409)")
    if code == 429:
        retry_after = _mapping(payload.get("parameters")).get("retry_after")
        if _is_int(retry_after) and retry_after >= 0:
            return RetryAfter(retry_after)
        return TelegramUnavailable("limite de taxa sem retry_after (429)")
    if 500 <= code <= 599:
        return TelegramUnavailable(f"Telegram indisponível ({code})")
    if code == 400:
        if any(phrase in description for phrase in _NOT_MODIFIED_DESCRIPTIONS):
            return MessageNotModified("a edição não muda a mensagem (400)")
        if any(phrase in description for phrase in _EDIT_TARGET_DESCRIPTIONS):
            return EditTargetUnavailable("a mensagem a editar não existe ou não pode ser editada (400)")
        if any(phrase in description for phrase in _QUERY_EXPIRED_DESCRIPTIONS):
            return CallbackQueryExpired("a confirmação do toque expirou ou já foi feita (400)")
    return RequestRejected(f"o Telegram recusou o pedido ({code})")


def _non_null(params: Mapping[str, Any]) -> dict[str, Any]:
    """Só os parâmetros não nulos, na ordem."""
    return {name: value for name, value in params.items() if value is not None}


def _json_text(value: Mapping[str, Any]) -> str:
    """Um objeto em texto JSON para um campo do multipart (a Bot API: "JSON-serialized")."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _message_id(result: Any) -> int:
    """O `message_id` inteiro da mensagem aceita; senão `TelegramUnavailable`."""
    message_id = _mapping(result).get("message_id")
    if not _is_int(message_id):
        raise TelegramUnavailable(_MSG_MALFORMED) from None
    return message_id


# ---------------------------------------------------------------------------------------------
# O adaptador
# ---------------------------------------------------------------------------------------------


class HttpTelegramBotApi:
    """A Bot API de um token, sobre HTTP (o `repr` nunca mostra o token).

    Tempos por fase: conexão, escrita e fila com `timeout_seconds`;
    leitura com `timeout_seconds`, menos no `getUpdates`, que lê com
    `poll_timeout_seconds + timeout_seconds`. Esses tempos valem por fase e, na leitura, por
    `recv`: sozinhos, não limitam a duração de uma resposta que goteja.

    Prazo total do `getUpdates`: além dos tempos por fase, cada
    `getUpdates` tem um prazo total igual à margem que o worker exige da posse antes de consultar
    (`poll_timeout_seconds + timeout_seconds`; `bots.poll_allowed`), contado do início da chamada
    (antes da fila, da conexão e da escrita). Ao vencer, um vigia (a thread
    `cq-telegram-poll-deadline`, uma por chamada, cancelada e esperada no fim dela por qualquer
    caminho) derruba as conexões em uso pela chamada, que sai com `TelegramUnavailable` (frase
    fixa): a conexão é fechada pelo cliente, e a chamada seguinte do mesmo objeto abre outra. Uma
    resposta já lida inteira não vira falha, e o vigia nunca derruba conexão depois do fim da
    chamada. Nenhuma chamada é repetida pelo adaptador.

    O que o prazo garante é que **nenhum pedido sai depois dele**: um stream que nasce ou faz E/S
    depois do prazo é recusado antes de escrever. A **duração** da chamada, porém, pode passar da
    margem em três trechos que o vigia não interrompe, porque neles ainda não há stream preso à
    chamada (limite conhecido, como o do prazo no SMTP): a resolução
    de nomes (`getaddrinfo`, dentro do `socket.create_connection`, que não obedece a nenhum tempo
    do Python e dura o que o resolvedor do sistema levar), o `connect` (limitado pelo tempo de
    conexão) e o aperto de mão TLS (o `wrap_socket` desliga o socket de baixo do stream registrado,
    e o `kill()` não o alcança; limitado pelo tempo da fase). Nesses casos a chamada sai com
    `TelegramUnavailable`, e o worker segue a regra de falha transitória da posse do bot (com
    `last_problem = telegram_unavailable`, espera crescente, posse mantida). Há ainda uma folga
    pequena entre o `agora` que `poll_allowed` confere e o início do prazo (a latência daquele
    `SELECT`), em que a chamada pode terminar depois do `lease_until` sem nenhum pedido novo.

    `abort()` (de outra thread) derruba as conexões abertas: a chamada em andamento sai com
    `Aborted` (mesmo que o prazo total também tenha vencido), e toda chamada seguinte deste objeto
    também, sem tocar a rede (um objeto novo por posse). `close()` faz o mesmo,
    cancela os vigias ainda ativos e fecha o transporte; é idempotente.
    """

    def __init__(self, token: str, *, base_url: str, poll_timeout_seconds: int,
                 timeout_seconds: float) -> None:
        if not _is_int(poll_timeout_seconds) or poll_timeout_seconds < 0:
            raise ValueError("poll_timeout_seconds deve ser um inteiro não negativo")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds deve ser um número finito maior que zero")
        base = base_url.rstrip("/")
        scheme = urllib.parse.urlsplit(base).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError("base_url da Bot API deve usar http:// ou https://")
        # O token vai no caminho, com tudo o que não é `[A-Za-z0-9_.~:-]` escapado: nada dele vira
        # consulta, fragmento nem outro segmento.
        quoted = urllib.parse.quote(token, safe=":")
        self._endpoint = _Secret(f"{base}/bot{quoted}/")
        self._poll_timeout_seconds = poll_timeout_seconds
        self._timeout = httpx2.Timeout(timeout_seconds).as_dict()
        self._poll_timeout = httpx2.Timeout(
            timeout_seconds, read=poll_timeout_seconds + timeout_seconds).as_dict()
        # O prazo total do `getUpdates`: a margem de `bots.poll_allowed`.
        self._poll_deadline_seconds = poll_timeout_seconds + timeout_seconds
        self._deadlines_lock = threading.Lock()
        self._deadlines: set[_CallDeadline] = set()
        self._transport = _AbortableTransport(https=scheme == "https",
                                              aborted_message=_MSG_ABORTED,
                                              deadline_message=_MSG_DEADLINE)

    # -- métodos da porta -------------------------------------------------------------------------

    def get_me(self) -> BotIdentity:
        result = self._call("getMe", {})
        if not isinstance(result, Mapping) or not _is_int(result.get("id")):
            raise TelegramUnavailable(_MSG_MALFORMED) from None
        username = result.get("username")
        if result.get("is_bot") is not True or not isinstance(username, str) or not username:
            raise NotABot(_MSG_NOT_A_BOT) from None
        return BotIdentity(telegram_bot_id=result["id"], username=username)

    def set_my_description(self, description: str) -> None:
        self._call("setMyDescription", {"description": description})

    def set_my_commands(self, commands: Sequence[BotCommand]) -> None:
        self._call("setMyCommands", {"commands": [
            {"command": command.command, "description": command.description}
            for command in commands]})

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, *, offset: int | None) -> list[MinimalUpdate]:
        return [minimize_update(item) for item in self._poll(offset)]

    def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = None,
                     reply_markup: Mapping[str, Any] | None = None,
                     reply_parameters: Mapping[str, Any] | None = None,
                     link_preview_options: Mapping[str, Any] | None = None) -> int:
        # Sem os argumentos da mensagem rica, o corpo é exatamente `{chat_id, text}`.
        params = _non_null({"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                            "reply_markup": reply_markup, "reply_parameters": reply_parameters,
                            "link_preview_options": link_preview_options})
        return _message_id(self._call("sendMessage", params))

    # -- mensagem rica ------------------------------------------------------------------------------

    def get_updates_with_replies(self, *, offset: int | None,
                                 ) -> tuple[list[MinimalUpdate], dict[int, int]]:
        """A mesma chamada `getUpdates` de `get_updates`, devolvendo também `update_id` →
        `reply_to_message_id` só dos updates em que `extract_reply_to` não é `None`."""
        raw = self._poll(offset)
        updates = [minimize_update(item) for item in raw]
        replies: dict[int, int] = {}
        for item in raw:
            reply_to = extract_reply_to(item)
            if reply_to is not None:
                replies[item["update_id"]] = reply_to
        return updates, replies

    def send_rich_message(self, chat_id: int, rich_message: Mapping[str, Any], *,
                          reply_markup: Mapping[str, Any] | None = None,
                          reply_parameters: Mapping[str, Any] | None = None) -> int:
        params = _non_null({"chat_id": chat_id, "rich_message": rich_message,
                            "reply_markup": reply_markup, "reply_parameters": reply_parameters})
        return _message_id(self._call("sendRichMessage", params))

    def edit_message_text(self, chat_id: int, message_id: int, *, text: str | None = None,
                          parse_mode: str | None = None,
                          rich_message: Mapping[str, Any] | None = None,
                          reply_markup: Mapping[str, Any] | None = None,
                          link_preview_options: Mapping[str, Any] | None = None) -> None:
        params = _non_null({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "parse_mode": parse_mode, "rich_message": rich_message,
                            "reply_markup": reply_markup,
                            "link_preview_options": link_preview_options})
        self._call("editMessageText", params)

    def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                  reply_markup: Mapping[str, Any]) -> None:
        self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                              "reply_markup": reply_markup})

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> None:
        self._call("answerCallbackQuery", _non_null({"callback_query_id": callback_query_id,
                                                      "text": text}))

    def send_photo(self, chat_id: int, photo: UploadFile, *, caption: str | None = None,
                   reply_parameters: Mapping[str, Any] | None = None) -> int:
        return self._send_file("sendPhoto", "photo", chat_id, photo, caption=caption,
                               reply_parameters=reply_parameters)

    def send_document(self, chat_id: int, document: UploadFile, *, caption: str | None = None,
                      reply_parameters: Mapping[str, Any] | None = None) -> int:
        return self._send_file("sendDocument", "document", chat_id, document, caption=caption,
                               reply_parameters=reply_parameters)

    def abort(self) -> None:
        self._transport.abort()

    def close(self) -> None:
        self._transport.close()
        # Nenhum vigia sobrevive ao `close()` (as chamadas em andamento já saem pelo abort).
        with self._deadlines_lock:
            deadlines = list(self._deadlines)
        for deadline in deadlines:
            deadline.stop()

    def __repr__(self) -> str:
        return "HttpTelegramBotApi(token=<oculto>)"

    __str__ = __repr__

    # -- chamada ----------------------------------------------------------------------------------

    def _poll(self, offset: int | None) -> list[Mapping[str, Any]]:
        """A chamada `getUpdates` (a consulta feita a cada posse do bot): os updates crus, cada um com
        `update_id` inteiro; senão `TelegramUnavailable`."""
        params: dict[str, Any] = {}
        if offset is not None:
            params["offset"] = offset
        params.update({"timeout": self._poll_timeout_seconds, "limit": GET_UPDATES_LIMIT,
                       "allowed_updates": list(ALLOWED_UPDATES)})
        result = self._call("getUpdates", params, timeout=self._poll_timeout,
                            deadline_seconds=self._poll_deadline_seconds)
        if not isinstance(result, list) or not all(
                isinstance(item, Mapping) and _is_int(item.get("update_id")) for item in result):
            raise TelegramUnavailable(_MSG_MALFORMED) from None
        return result

    def _send_file(self, method: str, field_name: str, chat_id: int, upload: UploadFile, *,
                   caption: str | None, reply_parameters: Mapping[str, Any] | None) -> int:
        """Um upload por `multipart/form-data`: os campos de texto só
        quando não nulos, o `reply_parameters` em texto JSON e o arquivo com nome, tipo e bytes."""
        fields: dict[str, str] = {"chat_id": str(chat_id)}
        if caption is not None:
            fields["caption"] = caption
        if reply_parameters is not None:
            fields["reply_parameters"] = _json_text(reply_parameters)
        files = {field_name: (upload.filename, upload.data, upload.content_type)}
        return _message_id(self._call(method, fields, files=files))

    def _call(self, method: str, params: Mapping[str, Any], *,
              timeout: dict[str, float | None] | None = None,
              deadline_seconds: float | None = None,
              files: Mapping[str, tuple[str, bytes, str]] | None = None) -> Any:
        """Uma chamada `POST`; devolve o `result` de uma resposta `ok: true` ou levanta a exceção da
        porta. Sem `files`, o corpo é JSON (`content=`); com `files`, é
        `multipart/form-data` (`data=` com os campos de texto de `params` e `files=`). Com
        `deadline_seconds`, a troca HTTP inteira (fila, conexão, escrita e leitura) tem esse prazo
        total, contado daqui."""
        if self._transport.aborted:
            raise Aborted(_MSG_ABORTED) from None
        deadline: _CallDeadline | None = None
        failure: TelegramError | None = None
        status = 0
        content = b""
        try:
            extensions = {"timeout": self._timeout if timeout is None else timeout}
            if files is None:
                # `ensure_ascii`: o corpo é ASCII (portanto UTF-8) mesmo com substitutos isolados.
                body = json.dumps(params, ensure_ascii=True, separators=(",", ":"),
                                  allow_nan=False).encode("ascii")
            try:
                # Dentro do bloco protegido: se o vigia não puder nascer (ex.: threads esgotadas),
                # a chamada sai com a falha da porta, e nada do prazo fica ligado.
                deadline = self._start_deadline(deadline_seconds)
                if files is None:
                    request = httpx2.Request(
                        "POST", self._endpoint.value + method, content=body,
                        headers={"Content-Type": "application/json"}, extensions=extensions)
                else:
                    request = httpx2.Request(
                        "POST", self._endpoint.value + method, data=dict(params),
                        files=dict(files), extensions=extensions)
                status, content = self._exchange(request, deadline)
            except Exception as exc:  # noqa: BLE001 - toda falha do transporte vira erro da porta
                failure = self._transport_failure(exc, deadline)
        finally:
            self._stop_deadline(deadline)
        # Fora do `except`: a exceção do transporte (que pode citar o pedido) não fica na cadeia.
        if failure is not None:
            raise failure from None

        payload = _parse_body(content)
        if not isinstance(payload, Mapping) or type(payload.get("ok")) is not bool:
            raise TelegramUnavailable(_MSG_NOT_JSON) from None
        if payload["ok"]:
            if "result" not in payload:
                raise TelegramUnavailable(_MSG_MALFORMED) from None
            return payload["result"]
        raise _error_for(payload, status) from None

    def _exchange(self, request: httpx2.Request,
                  deadline: _CallDeadline | None) -> tuple[int, bytes]:
        """A troca HTTP: o status e o corpo inteiro. Lido o corpo, a chamada acaba para o vigia do
        prazo; se o vigia disparou bem nesse instante, a resposta já lida vale, e uma falha ao
        fechar a conexão que ele derrubou é ignorada. O `read()` do `httpx2` já devolve a conexão
        ao pool (o `iter_raw` fecha a resposta), um pouco antes do `finish()`: um disparo nessa
        janela derruba uma conexão ociosa, o que é inofensivo, porque o objeto é usado por uma
        thread só e o `httpcore2` descarta a conexão de socket fechado na chamada seguinte."""
        response = self._transport.handle_request(request)
        try:
            response.read()
        except BaseException:
            response.close()
            raise
        if deadline is not None:
            deadline.finish()
        try:
            response.close()
        except Exception:
            if deadline is None or not deadline.expired:
                raise
        return response.status_code, response.content

    def _start_deadline(self, seconds: float | None) -> _CallDeadline | None:
        """Liga o vigia do prazo total (se houver prazo) para a chamada desta thread."""
        if seconds is None:
            return None
        deadline = _CallDeadline(seconds, thread_name=DEADLINE_THREAD_NAME)
        with self._deadlines_lock:
            self._deadlines.add(deadline)
        self._transport.set_deadline(deadline)
        try:
            deadline.start()
        except BaseException:
            self._stop_deadline(deadline)
            raise
        return deadline

    def _stop_deadline(self, deadline: _CallDeadline | None) -> None:
        """Encerra a chamada para o vigia (por qualquer caminho), cancela-o e o espera."""
        if deadline is None:
            return
        self._transport.set_deadline(None)
        deadline.stop()
        with self._deadlines_lock:
            self._deadlines.discard(deadline)

    def _transport_failure(self, exc: Exception,
                           deadline: _CallDeadline | None = None) -> TelegramError:
        """A exceção da porta para uma falha do transporte (só pelo tipo; nada dela é copiado).
        O `abort()` tem precedência; depois vem o prazo total vencido; depois, o tipo."""
        if self._transport.aborted:
            return Aborted(_MSG_ABORTED)
        if deadline is not None and deadline.expired:
            return TelegramUnavailable(_MSG_DEADLINE)
        if isinstance(exc, (httpcore2.TimeoutException, httpx2.TimeoutException)):
            return TelegramUnavailable(_MSG_TIMEOUT)
        return TelegramUnavailable(_MSG_NETWORK)


def http_factory(settings: BotSettings) -> TelegramBotApiFactory:
    """A fábrica da porta sobre HTTP, com a URL e os tempos de `settings` (não chama nada aqui).

    Guarda só a URL e os tempos (não a chave-mestra); cada chamada cria um objeto novo para o token.
    """
    base_url = settings.telegram_api_url
    poll_timeout_seconds = settings.poll_timeout_seconds
    timeout_seconds = settings.telegram_timeout_seconds

    def factory(token: str) -> TelegramBotApi:
        return HttpTelegramBotApi(token, base_url=base_url,
                                  poll_timeout_seconds=poll_timeout_seconds,
                                  timeout_seconds=timeout_seconds)

    return factory
