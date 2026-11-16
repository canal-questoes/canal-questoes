"""Canais de entrega de teste (porta `DeliveryChannel`).

- `ScriptedChannel(results, capabilities=None, on_send=None)`: devolve (ou levanta) os resultados na
  ordem dada; registra cada `OutgoingMessage` recebida em `calls`. `on_send(message)` roda antes do
  resultado (ex.: avançar o relógio durante o envio).
- `FlushSpyStream`: `StringIO` que conta as chamadas de `flush()` e a posição de cada uma.
- `Crash`: `BaseException` que não é `Exception`, para simular a queda do processo.
- `ScriptedVerificationChannel(results=None, *, delegate=None, on_send=None, capabilities=None)` (
  porta `VerificationChannel`, para a injeção de falhas nos testes): o canal de verificação
  roteirizado, com `capabilities = VerificationCapabilities("email")`. Cada envio consome um passo de
  `results`: um resultado da porta (`Delivered`, `TemporaryFailure`, `PermanentFailure`) é devolvido;
  uma exceção é levantada (`Crash()` é a queda **antes** de delegar; uma `Exception` é o erro que o
  tratador trata como `TemporaryFailure`); `DELEGATE` delega ao canal real `delegate` (ex.: o
  `SmtpVerificationChannel` do SMTP falso) e devolve o resultado dele; `Delegate(then=Crash())` delega e
  levanta a queda **depois** de o canal real aceitar (a queda entre o envio e a transação final). Com `results=None`, todo
  envio é delegado. Registra cada `VerificationMessage` em `calls` e cada resultado do canal real em
  `delegated`; `on_send(message)` roda antes do resultado e da delegação (ex.: avançar o `FakeClock` e
  rodar outra execução da mesma tarefa, para a transação final ser superada). O mesmo canal faz o papel do canal que não é
  SMTP.

Disparo e resposta: o `ScriptedChannel` tem o argumento só-nomeado
`formatter` (um `MessageFormatter`; padrão
`None`), resolvido **só no acesso** a `channel.formatter`: sem ele, um `PlainFormatter()` construído no
primeiro acesso e guardado (nunca no `__init__`, para quem não usa o formatador não o construir), e
`formatter_for(text_format)` (`rich` · `html` · `plain`): `channel.formatter` quando o dialeto coincide;
senão um `TelegramRichFormatter()`, `TelegramHtmlFormatter()` ou `PlainFormatter()` novo; outro dialeto →
`ValueError`, como nos canais reais. Os três formatadores vêm de `cq_server.telegramrender`.
`capabilities` e `calls` valem também no disparo (os campos do disparo já chegam nas `OutgoingMessage` de `calls`).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cq_server.ports.delivery import ChannelCapabilities, MessageFormatter, OutgoingMessage
from cq_server.ports.verification import VerificationCapabilities, VerificationChannel, VerificationMessage
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter

# Os dialetos gravados que `formatter_for` aceita, e o formatador de cada um.
_FORMATTER_BY_DIALECT: dict[str, Callable[[], MessageFormatter]] = {
    "rich": TelegramRichFormatter,
    "html": TelegramHtmlFormatter,
    "plain": PlainFormatter,
}


class Crash(BaseException):
    """Queda simulada do worker: não é `Exception`, então não é tratada como falha do canal."""


class ScriptedChannel:
    def __init__(self, results: Iterable[Any], capabilities: ChannelCapabilities | None = None,
                 on_send: Callable[[OutgoingMessage], None] | None = None, *,
                 formatter: MessageFormatter | None = None) -> None:
        self._results = list(results)
        self.calls: list[OutgoingMessage] = []
        self.on_send = on_send
        self.capabilities = capabilities or ChannelCapabilities(
            can_edit=True, max_buttons=8, supports_math=True, supports_rich_text=True)
        # Só guarda o argumento; o padrão é resolvido no primeiro acesso a `formatter`.
        self._formatter = formatter

    @property
    def formatter(self) -> MessageFormatter:
        """O formatador do canal; sem `formatter=`, um `PlainFormatter()` criado no primeiro
        acesso e guardado."""
        if self._formatter is None:
            self._formatter = PlainFormatter()
        return self._formatter

    @formatter.setter
    def formatter(self, value: MessageFormatter) -> None:
        self._formatter = value

    def formatter_for(self, text_format: str) -> MessageFormatter:
        """O formatador de um dialeto gravado (`rich` · `html` · `plain`):
        `self.formatter` quando o dialeto coincide; senão um formatador novo do dialeto; outro →
        `ValueError`."""
        if text_format not in _FORMATTER_BY_DIALECT:
            raise ValueError(f"dialeto de texto desconhecido: {text_format!r}")
        if self.formatter.text_format == text_format:
            return self.formatter
        return _FORMATTER_BY_DIALECT[text_format]()

    def send(self, message: OutgoingMessage) -> Any:
        self.calls.append(message)
        if self.on_send is not None:
            self.on_send(message)
        assert self._results, "ScriptedChannel sem resultado roteirizado para este envio"
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FlushSpyStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_positions: list[int] = []

    def flush(self) -> None:
        self.flush_positions.append(len(self.getvalue()))
        super().flush()


# --- canal de verificação roteirizado ----------------------------------------------------


@dataclass(frozen=True)
class Delegate:
    """Passo de `ScriptedVerificationChannel`: delega o envio ao canal real; com `then`, levanta `then`
    depois de o canal real devolver (ex.: `Delegate(then=Crash())`, a queda depois de o SMTP falso
    aceitar a mensagem)."""

    then: BaseException | None = None


DELEGATE = Delegate()


class ScriptedVerificationChannel:
    def __init__(self, results: Iterable[Any] | None = None, *, delegate: VerificationChannel | None = None,
                 on_send: Callable[[VerificationMessage], None] | None = None,
                 capabilities: VerificationCapabilities | None = None) -> None:
        self._results = None if results is None else list(results)
        if delegate is None:
            assert self._results is not None, (
                "ScriptedVerificationChannel sem roteiro precisa de um canal real (`delegate`)")
            assert not any(isinstance(step, Delegate) for step in self._results), (
                "passo `Delegate` sem canal real (`delegate`)")
        self.delegate = delegate
        self.calls: list[VerificationMessage] = []
        self.delegated: list[Any] = []
        self.on_send = on_send
        self.capabilities = capabilities or VerificationCapabilities("email")

    def send(self, message: VerificationMessage) -> Any:
        self.calls.append(message)
        if self.on_send is not None:
            self.on_send(message)
        if self._results is None:
            step: Any = DELEGATE
        else:
            assert self._results, "ScriptedVerificationChannel sem resultado roteirizado para este envio"
            step = self._results.pop(0)
        if isinstance(step, Delegate):
            assert self.delegate is not None
            result = self.delegate.send(message)
            self.delegated.append(result)
            if step.then is not None:
                raise step.then
            return result
        if isinstance(step, BaseException):
            raise step
        return step
