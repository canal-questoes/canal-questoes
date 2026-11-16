"""Porta da Bot API do Telegram.

O núcleo fala com o Telegram só por esta porta; o adaptador HTTP fica em
`cq_server.adapters.telegram`. Um objeto `TelegramBotApi` por token, criado pela
`TelegramBotApiFactory` (que recebe o token).

Os erros são todos `TelegramError`, e o `str()` deles nunca contém o token nem a URL da Bot API (a
URL contém o token). A classificação por resposta do Telegram é a do adaptador.

`MinimalUpdate` é o update já minimizado pelo adaptador: só os campos da lista de inclusão
da minimização, e o `repr` não mostra `chat_id`, `sender_id` nem `text` (identidade e texto
literal do remetente; a identidade só sai pela revelação).

Mensagem rica:
- `UploadFile` (os bytes fora do `repr`), para `send_photo` e `send_document` (multipart);
- `MessageNotModified`, `EditTargetUnavailable` e `CallbackQueryExpired`, **subclasses** de
  `RequestRejected` (decididas pela descrição, só no 400): todo `except RequestRejected` genérico
  também as pega;
- `send_message` aceita só argumentos nomeados opcionais (só os não nulos vão ao corpo: a chamada simples
  manda exatamente `{chat_id, text}`); os demais métodos são os da mensagem rica;
- `get_updates_with_replies`: a mesma chamada `getUpdates`, devolvendo também `update_id` →
  `reply_to_message_id` (só os não nulos); `get_updates` e `MinimalUpdate` não trazem a resposta citada.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# Valores de `MinimalUpdate.kind`.
UPDATE_KINDS = ("message", "callback_query", "my_chat_member", "other")


@dataclass(frozen=True)
class BotIdentity:
    """O bot segundo o `getMe`."""

    telegram_bot_id: int
    username: str


@dataclass(frozen=True)
class BotCommand:
    """Um comando do menu do bot (`setMyCommands`)."""

    command: str
    description: str


@dataclass(frozen=True)
class MinimalUpdate:
    """Update minimizado; `kind` em `UPDATE_KINDS`.

    `command` é o trecho do texto coberto por uma entidade `bot_command` no deslocamento 0 (em
    unidades UTF-16), ou `None`; `details` só tem os campos da lista de inclusão.
    """

    update_id: int
    kind: str
    chat_id: int | None = field(repr=False)
    chat_type: str | None
    sender_id: int | None = field(repr=False)
    text: str | None = field(repr=False)
    command: str | None
    details: dict


# --- erros ---------------------------------------------------------------------------------------


class TelegramError(Exception):
    """Falha de uma chamada à Bot API; o `str()` nunca contém o token nem a URL."""


class TokenRejected(TelegramError):
    """401: o Telegram recusou o token."""


class RetryAfter(TelegramError):
    """429 com `parameters.retry_after`: nenhuma chamada antes de `seconds` segundos."""

    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


class TelegramUnavailable(TelegramError):
    """5xx, rede, tempo esgotado, corpo que não é JSON (ou sem `ok`), 429 sem `retry_after`."""


class WebhookActive(TelegramError):
    """409 com webhook configurado."""


class ConcurrentConsumer(TelegramError):
    """409 de outro `getUpdates` do mesmo token."""


class ChatUnreachable(TelegramError):
    """403 de bloqueio pelo usuário ou de conta desativada."""


class RequestRejected(TelegramError):
    """400, outros 403, 404 e códigos desconhecidos: falha definitiva."""


class MessageNotModified(RequestRejected):
    """400 "message is not modified" (a edição sem mudança conta como sucesso)."""


class EditTargetUnavailable(RequestRejected):
    """400 "message to edit not found" ou "message can't be edited"."""


class CallbackQueryExpired(RequestRejected):
    """400 "query is too old" na confirmação do toque."""


class NotABot(TelegramError):
    """`getMe` sem `is_bot` verdadeiro ou sem `username`."""


class Aborted(TelegramError):
    """Chamada interrompida por `abort()`."""


# --- arquivo enviado por multipart --------------------------------------------------------------


@dataclass(frozen=True)
class UploadFile:
    """Um arquivo de `send_photo`/`send_document`: os bytes vêm da IR (conteúdo de terceiros)."""

    filename: str
    content_type: str
    data: bytes = field(repr=False)


# --- a porta -------------------------------------------------------------------------------------


class TelegramBotApi(Protocol):
    """Bot API de um token."""

    def get_me(self) -> BotIdentity: ...

    def set_my_description(self, description: str) -> None: ...

    def set_my_commands(self, commands: Sequence[BotCommand]) -> None: ...

    def delete_webhook(self) -> None: ...

    def get_updates(self, *, offset: int | None) -> list[MinimalUpdate]: ...

    def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = None,
                     reply_markup: Mapping[str, Any] | None = None,
                     reply_parameters: Mapping[str, Any] | None = None,
                     link_preview_options: Mapping[str, Any] | None = None) -> int: ...

    # --- mensagem rica ---

    def get_updates_with_replies(self, *, offset: int | None,
                                 ) -> tuple[list[MinimalUpdate], dict[int, int]]: ...

    def send_rich_message(self, chat_id: int, rich_message: Mapping[str, Any], *,
                          reply_markup: Mapping[str, Any] | None = None,
                          reply_parameters: Mapping[str, Any] | None = None) -> int: ...

    def edit_message_text(self, chat_id: int, message_id: int, *, text: str | None = None,
                          parse_mode: str | None = None,
                          rich_message: Mapping[str, Any] | None = None,
                          reply_markup: Mapping[str, Any] | None = None,
                          link_preview_options: Mapping[str, Any] | None = None) -> None: ...

    def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                  reply_markup: Mapping[str, Any]) -> None: ...

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> None: ...

    def send_photo(self, chat_id: int, photo: UploadFile, *, caption: str | None = None,
                   reply_parameters: Mapping[str, Any] | None = None) -> int: ...

    def send_document(self, chat_id: int, document: UploadFile, *, caption: str | None = None,
                      reply_parameters: Mapping[str, Any] | None = None) -> int: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


TelegramBotApiFactory = Callable[[str], TelegramBotApi]
