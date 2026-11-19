"""Porta de verificação: o canal que leva ao aluno o link pessoal de convite, e o teste do servidor de
e-mail.

O núcleo (`students`) fala com o servidor de e-mail só por esta porta; o adaptador SMTP fica em
`cq_server.adapters.smtp`, e o `__main__` o monta (`build_verification`). Nesta versão, o único tipo de
endereço é `"email"` (`VerificationCapabilities.address_kind`).

O `send` devolve um de três resultados:
- `Delivered(message_ref)`: o servidor aceitou a mensagem;
- `TemporaryFailure(error_type)`: vale tentar de novo;
- `PermanentFailure(category, error_type)`: não adianta tentar de novo; `category` em `recipient` ·
  `auth` · `server`.

`error_type` é só o nome de um tipo (`^[A-Za-z_][A-Za-z0-9_]{0,99}$`), nunca o texto do servidor. Só uma
`Exception` que escape do `send` vale como `TemporaryFailure(type(exc).__name__)` no tratador; uma
`BaseException` passa.

`VerificationMessage` leva o endereço e o corpo (que tem o link com o código do convite), os dois fora do
`repr` (a identidade só sai pela revelação).

A porta `MailServerProbe`, o teste do
servidor de e-mail sem envio (conexão, `EHLO`, STARTTLS, AUTH, `QUIT`; nunca `MAIL FROM`), e o
`ProbeResult`, o conjunto fechado do resultado que o worker grava em `mail_probe`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class VerificationCapabilities:
    """O que o núcleo lê do canal: o tipo de endereço (só `"email"` nesta versão)."""

    address_kind: str


@dataclass(frozen=True)
class VerificationMessage:
    """Mensagem a enviar; o endereço (identidade) e o corpo (link com o código) ficam fora do `repr`."""

    address: str = field(repr=False)
    subject: str
    body: str = field(repr=False)


@dataclass(frozen=True)
class Delivered:
    """O servidor aceitou a mensagem; `message_ref` identifica o envio no canal."""

    message_ref: str


@dataclass(frozen=True)
class TemporaryFailure:
    """Falha que vale tentar de novo; `error_type` é o nome do tipo do erro."""

    error_type: str


@dataclass(frozen=True)
class PermanentFailure:
    """Falha definitiva; `category` em `recipient` · `auth` · `server`, `error_type` é o nome do tipo."""

    category: str
    error_type: str


class VerificationChannel(Protocol):
    capabilities: VerificationCapabilities

    def send(self, message: VerificationMessage) -> Delivered | TemporaryFailure | PermanentFailure: ...


# O resultado do teste do servidor de e-mail, o mesmo conjunto do CHECK da
# coluna `mail_probe.result` (migração 0007).
ProbeResult = Literal["ok", "connection", "tls", "auth", "not_configured"]


class MailServerProbe(Protocol):
    """O teste do servidor de e-mail sem envio; o `SmtpVerificationChannel` cumpre a porta."""

    def probe(self) -> ProbeResult: ...
