"""Porta de canal de entrega: as mensagens que o núcleo manda pelo canal (Telegram ou console).

Mensagens por bot: `BotRoute` (o bot, a versão do token e o chat de destino, com o
`chat_id` fora do `repr`); `OutgoingMessage.recipient` pode ser `None` (resposta a quem não está
vinculado) e tem `route` (`None` nas mensagens sem bot); `Undeliverable.blocked`
(`True`: o endereço de entrega fica `blocked`).

Mensagem rica: teclado, pedido de resposta, resposta a, edição, mídia e dialeto do texto
(os campos abaixo):
- `InlineButton`, `ReplyRequest` e `MediaPayload` (os bytes fora do `repr`);
- `OutgoingMessage` tem 8 campos **no fim, todos com padrão** (`text_format`, `keyboard`,
  `reply_request`, `reply_to_ref`, `edit_ref`, `markup_only`, `media`, `answerable`): com os padrões,
  a mensagem é a simples (só o texto), e os canais seguem o caminho simples;
- `Formatted` e o `Protocol` `MessageFormatter` (o dialeto do canal: `rich` · `html` · `plain`);
- `DeliveryChannel` tem `formatter` (o do modo corrente) e `formatter_for(text_format)` (o de um
  dialeto gravado; fora dos três → `ValueError`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

# Os dialetos de texto; `plain` é o da mensagem simples.
TEXT_FORMATS = ("rich", "html", "plain")


@dataclass(frozen=True)
class ChannelCapabilities:
    can_edit: bool
    max_buttons: int
    supports_math: bool
    supports_rich_text: bool


@dataclass(frozen=True)
class BotRoute:
    """Destino de uma mensagem por bot: o chat é identidade e não sai no `repr` (a identidade só sai pela revelação)."""

    bot_id: int
    token_version: int
    chat_id: int = field(repr=False)


@dataclass(frozen=True)
class InlineButton:
    """Um botão do teclado: `label` em texto puro; `data` é o `callback_data` (`cq1:`)."""

    label: str
    data: str


@dataclass(frozen=True)
class ReplyRequest:
    """Pedido de resposta (`ForceReply`); `placeholder` é a dica do campo (1 a 64) ou nada."""

    placeholder: str | None


@dataclass(frozen=True)
class MediaPayload:
    """Uma mídia da variante: foto ou documento; os bytes (da IR) ficam fora do `repr`."""

    media_id: str
    filename: str
    content_type: str
    as_document: bool
    caption: str | None
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class OutgoingMessage:
    message_id: int
    recipient: uuid.UUID | None
    text: str
    route: BotRoute | None = None
    # Mensagem rica (no fim e com padrão): dialeto, teclado, pedido de resposta, resposta a, edição de,
    # edição só do teclado, mídia e "aceita resposta do aluno".
    text_format: str = "plain"
    keyboard: tuple[tuple[InlineButton, ...], ...] | None = None
    reply_request: ReplyRequest | None = None
    reply_to_ref: str | None = None
    edit_ref: str | None = None
    markup_only: bool = False
    media: MediaPayload | None = None
    answerable: bool = False


@dataclass(frozen=True)
class Sent:
    channel_ref: str


@dataclass(frozen=True)
class RetryLater:
    reason: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class Undeliverable:
    reason: str
    blocked: bool = False


@dataclass(frozen=True)
class Formatted:
    """Texto já escrito no dialeto do canal: `length` contado como o canal conta e
    `fits` dentro de todos os limites do modo."""

    markup: str
    text_format: str
    length: int
    fits: bool


class MessageFormatter(Protocol):
    """Escreve no dialeto do canal; o núcleo decide **o que** mostrar."""

    text_format: str
    max_length: int

    def from_ir(self, canonical_html: str, *, media_labels: Mapping[str, str]) -> Formatted: ...

    def escape(self, text: str) -> Formatted: ...

    def compose(self, blocks: Sequence[Formatted]) -> Formatted: ...

    def plain(self, canonical_html: str) -> str: ...


class DeliveryChannel(Protocol):
    capabilities: ChannelCapabilities
    # O formatador do modo corrente do canal.
    formatter: MessageFormatter

    def send(self, message: OutgoingMessage) -> Sent | RetryLater | Undeliverable: ...

    def formatter_for(self, text_format: str) -> MessageFormatter:
        """O formatador de um dialeto gravado (`rich` · `html` · `plain`; outro → `ValueError`)."""
        ...
