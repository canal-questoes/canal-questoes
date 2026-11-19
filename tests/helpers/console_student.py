"""Aluno simulado pelo console (dublê dos testes).

Lê as linhas JSON que o `ConsoleChannel` escreve e acha as
suas pelo `recipient` (o `pseudo_id` em texto): a linha **nunca** traz o `chat_id`
(a identidade só sai pela revelação). Responde pelo `channel_ref` da linha, com a `ConsoleInbox` (a entrada simulada do
adaptador de console: `send_text(…, reply_to=…)`, `choose` e `dont_know`), que grava a entrada pelo
mesmo caminho do Telegram (linha literal antes).

O teste é quem conhece o `bot_id` e o `chat_id` do aluno (o endereço que o vínculo do aluno gravou) e o
`pseudo_id` (pela API); o aluno simulado só junta os três. Nenhum import do núcleo além da
`ConsoleInbox` (usada só como tipo: qualquer objeto com os mesmos métodos serve).

Fonte das linhas (`source`): um `io.StringIO` (o `ConsoleChannel(StringIO)` dos testes), o caminho
do arquivo de `CQ_CONSOLE_OUTPUT` ou uma função sem argumentos que devolve o texto inteiro. Uma linha
ainda sem a quebra final é ignorada até completar (o canal escreve linha e quebra numa só escrita).
Uma linha que não é JSON, ou que não tem as 4 chaves básicas, é erro (`AssertionError`): o canal
nunca a escreveria.
"""

from __future__ import annotations

import io
import json
import pathlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cq_server.adapters.console import ConsoleInbox

T = TypeVar("T")

# As chaves básicas (sempre presentes) e as do disparo (só nas linhas com campo do disparo).
PLAIN_KEYS: frozenset[str] = frozenset({"channel_ref", "message_id", "recipient", "text"})
RICH_KEYS: frozenset[str] = frozenset({"reply_to", "reply_request", "edit_of", "media"})
DONT_KNOW_COMMAND = "/naosei"
POLL_SECONDS = 0.01


@dataclass(frozen=True)
class ConsoleLine:
    """Uma linha JSON do `ConsoleChannel`, na ordem em que foi escrita (`index`, desde 0, na fonte
    inteira). `raw` é o objeto lido, com as chaves exatas; os campos do disparo ausentes ficam `None`."""

    index: int
    channel_ref: str
    message_id: int
    recipient: str
    text: str
    reply_to: Any = None
    reply_request: Any = None
    edit_of: Any = None
    media: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def answerable(self) -> bool:
        """A linha tem a referência numérica que a entrada simulada consegue citar: as
        linhas com campo do disparo levam `channel_ref = str(message_id)`; as básicas, `console-<hex>`."""
        return self.channel_ref.isdigit()

    @property
    def has_rich_fields(self) -> bool:
        return set(self.raw) != PLAIN_KEYS


def read_lines(text: str) -> list[ConsoleLine]:
    """As linhas completas de `text` (cada uma terminada por `\\n`), lidas e conferidas."""
    lines: list[ConsoleLine] = []
    complete = text.split("\n")[:-1]  # o último pedaço é vazio ou uma linha ainda incompleta
    for index, line in enumerate(complete):
        try:
            data = json.loads(line)
        except ValueError:
            raise AssertionError(f"linha {index} do console não é JSON: {line[:80]!r}") from None
        if not isinstance(data, dict) or not PLAIN_KEYS <= set(data) <= PLAIN_KEYS | RICH_KEYS:
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise AssertionError(f"linha {index} do console com chaves fora do contrato: {keys}")
        if not isinstance(data["channel_ref"], str) or not isinstance(data["recipient"], str):
            raise AssertionError(f"linha {index} do console com channel_ref ou recipient fora "
                                 f"de texto")
        if not isinstance(data["message_id"], int) or not isinstance(data["text"], str):
            raise AssertionError(f"linha {index} do console com message_id ou text fora do tipo")
        lines.append(ConsoleLine(
            index=index, channel_ref=data["channel_ref"], message_id=data["message_id"],
            recipient=data["recipient"], text=data["text"], reply_to=data.get("reply_to"),
            reply_request=data.get("reply_request"), edit_of=data.get("edit_of"),
            media=data.get("media"), raw=data))
    return lines


def _reader(source: Any) -> Callable[[], str]:
    if isinstance(source, io.StringIO):
        return source.getvalue
    if isinstance(source, (str, pathlib.Path)):
        path = pathlib.Path(source)

        def read_file() -> str:
            try:
                return path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

        return read_file
    if callable(source):
        return source
    raise TypeError("source deve ser um StringIO, um caminho ou uma função que devolve o texto")


class ConsoleStudent:
    """Um aluno que conversa com o bot pelo console.

    `inbox` é a `ConsoleInbox` (ou objeto com os mesmos métodos); `bot_id` e `chat_id` são o bot e
    o chat privado do endereço do aluno; `recipient` é o `pseudo_id` (texto ou `UUID`).
    """

    def __init__(self, inbox: ConsoleInbox, *, bot_id: int, chat_id: int,
                 recipient: str | uuid.UUID, source: Any) -> None:
        self.inbox = inbox
        self.bot_id = bot_id
        self.chat_id = chat_id
        self.recipient = str(recipient)
        self._read = _reader(source)
        self._seen = 0

    # -- leitura -------------------------------------------------------------------------------

    def all_lines(self) -> list[ConsoleLine]:
        """Todas as linhas completas da fonte (de todos os destinatários), conferidas."""
        return read_lines(self._read())

    def lines(self) -> list[ConsoleLine]:
        """As linhas deste aluno (pelo `recipient`), na ordem em que foram escritas."""
        return [line for line in self.all_lines() if line.recipient == self.recipient]

    def new_lines(self) -> list[ConsoleLine]:
        """As linhas deste aluno escritas depois da última chamada a `new_lines` (ou a `mark`)."""
        mine = [line for line in self.lines() if line.index >= self._seen]
        self.mark()
        return mine

    def mark(self) -> None:
        """Daqui em diante, `new_lines` e `wait_for_line(new=True)` só veem linhas novas."""
        self._seen = len(self.all_lines())

    def last(self) -> ConsoleLine:
        mine = self.lines()
        if not mine:
            raise AssertionError(f"nenhuma linha do console para {self.recipient}")
        return mine[-1]

    def wait_for(self, predicate: Callable[[], T], *, timeout: float = 5.0) -> T:
        """Espera `predicate()` ficar verdadeiro (o worker escreve em outra thread) e devolve o
        valor; `AssertionError` no tempo, com as linhas do aluno."""
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                texts = [line.text[:60] for line in self.lines()[-10:]]
                raise AssertionError(f"console: condição não satisfeita em {timeout} s; "
                                     f"últimas linhas de {self.recipient}: {texts}")
            time.sleep(POLL_SECONDS)

    def wait_for_line(self, predicate: Callable[[ConsoleLine], bool] | None = None, *,
                      new: bool = True, timeout: float = 5.0) -> ConsoleLine:
        """A primeira linha deste aluno (só as novas desde `mark`, com `new=True`) que casa com
        `predicate` (qualquer uma, sem ele); marca a leitura até ela."""
        start = self._seen if new else 0

        def found() -> ConsoleLine | None:
            for line in self.lines():
                if line.index >= start and (predicate is None or predicate(line)):
                    return line
            return None

        line = self.wait_for(found, timeout=timeout)
        self._seen = max(self._seen, line.index + 1)
        return line

    def wait_for_text(self, fragment: str, *, new: bool = True,
                      timeout: float = 5.0) -> ConsoleLine:
        """A primeira linha deste aluno cujo texto contém `fragment`."""
        return self.wait_for_line(lambda line: fragment in line.text, new=new, timeout=timeout)

    # -- escrita (pela `ConsoleInbox`) ---------------------------------------------------------

    @staticmethod
    def _ref(target: ConsoleLine | str | int | None) -> str | None:
        if target is None or isinstance(target, str):
            return target
        if isinstance(target, ConsoleLine):
            return target.channel_ref
        if isinstance(target, int) and not isinstance(target, bool):
            return str(target)
        raise TypeError("a mensagem citada é uma ConsoleLine, um channel_ref ou um message_id")

    def say(self, text: str) -> None:
        """Um texto solto (sem "responder")."""
        self.inbox.send_text(self.bot_id, self.chat_id, text)

    def reply(self, target: ConsoleLine | str | int, text: str) -> None:
        """Um texto em resposta ("responder") à mensagem `target` (a linha, o `channel_ref` ou o
        `message_id` numérico), pelo `channel_ref`."""
        self.inbox.send_text(self.bot_id, self.chat_id, text, reply_to=self._ref(target))

    def choose(self, target: ConsoleLine | str | int, key: str) -> None:
        """A escolha degradada pela chave, em resposta à mensagem da questão."""
        ref = self._ref(target)
        assert ref is not None
        self.inbox.choose(self.bot_id, self.chat_id, ref, key)

    def dont_know(self, target: ConsoleLine | str | int | None = None) -> None:
        """O `/naosei`, a degradação do "Não sei" sem botões."""
        self.inbox.dont_know(self.bot_id, self.chat_id, self._ref(target))
