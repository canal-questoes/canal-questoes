"""Canal de console.

O canal de entrega de console: cada envio escreve **uma** linha JSON no `stream` recebido (a saída
padrão ou um arquivo, conforme `CQ_CONSOLE_OUTPUT`; worker.py) e faz `flush()` logo depois, para
que a linha já esteja visível quando a fila de saída registrar o envio.

A linha é `json.dumps({"channel_ref", "message_id", "recipient", "text"}, ensure_ascii=False)`,
seguida de uma única quebra de linha. O `json.dumps` escapa as quebras de linha do texto (`\\n`,
`\\r`), então a saída é sempre uma linha só, e o Unicode sai como está. `recipient` é o
`pseudo_id` em texto: o canal só conhece pseudônimos (a identidade só sai pela revelação).

Campos da mensagem rica (resposta citada, edição, pedido de resposta, mídia, formatação e teclado) e a
degradação do "Não sei" sem botões:
- a linha de toda mensagem **sem campo rico** continua exatamente a de cima; com algum campo rico,
  ganha só as chaves dos campos presentes e `channel_ref = str(message_id)`, nunca o `chat_id`;
- `ConsoleChannel.formatter`/`formatter_for` dão sempre o `PlainFormatter` (reexportado daqui, do
  núcleo: `cq_server.telegramrender`); dialeto fora dos três → `ValueError`;
- `ConsoleInbox`: a entrada simulada do aluno (texto, resposta pela chave, `/naosei`), gravada por
  `reception.store_console_updates`.

A linha com campo rico: `reply_to` (a referência de `reply_to_ref`), `edit_of` (a
de `edit_ref`), `reply_request` (`{"placeholder": <dica>}`: o console não tem a interface do
`ForceReply`) e `media` (`{media_id, filename, content_type, as_document, caption}`, nunca os bytes);
o teclado, o `markup_only` e o `answerable` não ganham chave (o console não tem botões), só mudam o
`channel_ref`. O console não edita (`can_edit = False`): uma mensagem com `edit_ref` é só mais uma
linha.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any, TextIO

from cq_server import reception
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.ports.delivery import (
    TEXT_FORMATS,
    ChannelCapabilities,
    MessageFormatter,
    OutgoingMessage,
    RetryLater,
    Sent,
    Undeliverable,
)
from cq_server.ports.telegram import MinimalUpdate
from cq_server.telegramrender import PlainFormatter

__all__ = ["CHANNEL_REF_PREFIX", "CONSOLE_CAPABILITIES", "ConsoleChannel", "ConsoleInbox", "PlainFormatter"]

CHANNEL_REF_PREFIX = "console-"

CONSOLE_CAPABILITIES = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                           supports_rich_text=False)


class ConsoleChannel:
    """Canal de entrega que escreve uma linha JSON por mensagem no `stream`."""

    capabilities: ChannelCapabilities

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.capabilities = CONSOLE_CAPABILITIES

    @property
    def formatter(self) -> MessageFormatter:
        """O formatador do canal (sempre o `PlainFormatter`)."""
        return PlainFormatter()

    def formatter_for(self, text_format: str) -> MessageFormatter:
        """Sempre o `PlainFormatter` para `rich` · `html` · `plain`; outro → `ValueError`."""
        if not isinstance(text_format, str) or text_format not in TEXT_FORMATS:
            raise ValueError(f"dialeto de texto desconhecido: {text_format!r}")
        return PlainFormatter()

    def send(self, message: OutgoingMessage) -> Sent | RetryLater | Undeliverable:
        fields: dict[str, Any] = {
            "message_id": int(message.message_id),
            "recipient": str(message.recipient),
            "text": message.text,
        }
        if _has_rich_fields(message):
            # Campo rico: referência numérica, que a entrada simulada consegue citar; nunca o chat.
            channel_ref = str(message.message_id)
            fields.update(_rich_keys(message))
        else:
            # `uuid4` vem de `os.urandom`: 122 bits aleatórios, único por envio na prática.
            channel_ref = CHANNEL_REF_PREFIX + uuid.uuid4().hex
        line = json.dumps({"channel_ref": channel_ref, **fields}, ensure_ascii=False)
        with self._lock:
            # Uma só escrita (linha + quebra) e o flush logo depois.
            self._stream.write(line + "\n")
            self._stream.flush()
        return Sent(channel_ref)


def _has_rich_fields(message: OutgoingMessage) -> bool:
    """Algum campo rico fora do padrão: a linha simples (a de cima) não vale para ela."""
    return (message.text_format != "plain" or message.keyboard is not None
            or message.reply_request is not None or message.reply_to_ref is not None
            or message.edit_ref is not None or message.markup_only or message.media is not None
            or message.answerable)


def _rich_keys(message: OutgoingMessage) -> dict[str, Any]:
    """As chaves dos campos ricos da linha: só as dos campos presentes; os bytes da mídia e a
    rota nunca saem."""
    keys: dict[str, Any] = {}
    if message.reply_to_ref is not None:
        keys["reply_to"] = message.reply_to_ref
    if message.reply_request is not None:
        keys["reply_request"] = {"placeholder": message.reply_request.placeholder}
    if message.edit_ref is not None:
        keys["edit_of"] = message.edit_ref
    if message.media is not None:
        media = message.media
        keys["media"] = {"media_id": media.media_id, "filename": media.filename,
                         "content_type": media.content_type, "as_document": media.as_document,
                         "caption": media.caption}
    return keys


# O comando no início do texto, como a entidade `bot_command` do Telegram no deslocamento 0 (o mesmo
# critério do adaptador do Telegram, `adapters.telegram._command`: o trecho que o Telegram marcaria).
_COMMAND = re.compile(r"/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]{1,64})?(?![A-Za-z0-9_@])")
DONT_KNOW_COMMAND = "/naosei"


class ConsoleInbox:
    """Entrada simulada do aluno pelo console: monta os `MinimalUpdate` que o Telegram daria
    (mensagem em chat privado) e os grava por `reception.store_console_updates` (`update_id`
    negativos). Só para o console e para os testes; nenhum caminho de produção a liga."""

    def __init__(self, conninfo: str, *, clock: Clock) -> None:
        self._conninfo = conninfo
        self._clock = clock
        self._db = LazyConnection(conninfo)
        self._lock = threading.Lock()

    def send_text(self, bot_id: int, chat_id: int, text: str, *, reply_to: str | None = None) -> None:
        """Um texto do aluno, opcionalmente em resposta à mensagem de `channel_ref` `reply_to`."""
        self._store(bot_id, chat_id, text, reply_to)

    def choose(self, bot_id: int, chat_id: int, reply_to: str, key: str) -> None:
        """A escolha degradada pela chave: um texto em resposta à mensagem da questão."""
        self._store(bot_id, chat_id, key, reply_to)

    def dont_know(self, bot_id: int, chat_id: int, reply_to: str | None = None) -> None:
        """O `/naosei` (a degradação do "Não sei" sem botões)."""
        self._store(bot_id, chat_id, DONT_KNOW_COMMAND, reply_to)

    def _store(self, bot_id: int, chat_id: int, text: str, reply_to: str | None) -> None:
        """Monta a mensagem do chat privado como o adaptador do Telegram a minimizaria e a grava com a
        sua tarefa, numa transação. A mensagem citada é o `message_id` numérico da linha do
        console (`channel_ref = str(message_id)`); uma referência não numérica não cita nada."""
        now = self._clock.now()
        match = _COMMAND.match(text)
        update = MinimalUpdate(update_id=0, kind="message", chat_id=chat_id, chat_type="private",
                               sender_id=chat_id, text=text, command=match.group(0) if match else None,
                               details={"message_id": None, "date": int(now.timestamp())})
        quoted = int(reply_to) if reply_to is not None and reply_to.isascii() and reply_to.isdigit() else None
        with self._lock:
            reception.store_console_updates(self._db.get(), bot_id=bot_id, updates=[update],
                                            reply_to={0: quoted} if quoted else None, now=now)

    def close(self) -> None:
        """Fecha a conexão (idempotente)."""
        self._db.close()
