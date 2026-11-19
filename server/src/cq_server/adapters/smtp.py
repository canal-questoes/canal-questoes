"""Adaptador SMTP da porta de verificação: o envio da mensagem de verificação por e-mail e o teste do
servidor sem envio, sobre o `smtplib`.

- `SmtpVerificationChannel(settings, *, ssl_context=None)` implementa a porta
  `ports.verification.VerificationChannel` sobre o `smtplib`: uma conexão por envio, `ehlo()` explícito
  (na abertura e de novo depois do STARTTLS, sem recuar para HELO), um único AUTH (PLAIN, senão LOGIN sem
  resposta inicial) e a mensagem de verificação em quoted-printable. Sem `ssl_context`, usa
  `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` + `load_default_certs()`, que não lê o ambiente
  (`SSLKEYLOGFILE`); nunca mexe em `keylog_filename`. Não confere `security="none"` fora do loopback: a
  regra é só do `MailSettings`.
- O endereço de envio é o `ascii_email` do `email_validator`, revalidado com `allow_smtputf8=False`; sem
  caminho SMTPUTF8. O destinatário recusado na revalidação e o erro na montagem da mensagem
  voltam antes de conectar.
- A classificação das falhas é uma tabela única: o código SMTP primeiro, depois o prazo
  total (`SmtpDeadline`, pelo tempo), depois o tipo. O vigia do prazo é a thread `cq-smtp-deadline`,
  cancelada e esperada antes de o `send` devolver.
- O `Subject` sai em palavras codificadas (RFC 2047) com a dobra só entre elas, e o texto decodificado é
  o assunto exato (a dobra da política do Python 3.12 deixa um espaço a mais).
- `probe()` cumpre a porta `MailServerProbe` com a mesma conexão, o mesmo
  prazo e o mesmo vigia do envio, sem `MAIL FROM`, e classifica a falha pela etapa (`_probe_failure`), não pela
  tabela do envio (que só vale para o `send`).

Este é o único módulo que importa `smtplib` (o núcleo não depende do transporte). Nada é registrado em log, e nenhum
resultado, atributo ou `repr` contém o endereço, o corpo, o código do convite, a senha ou o texto do
servidor: do erro, só o nome do tipo sai do adaptador, e o canal não guarda exceção. Nunca
`set_debuglevel` (a depuração do `smtplib` escreve a conversa inteira no stderr).

Limites conhecidos: o prazo cobre a conversa SMTP; a resolução do nome, o `connect` com
vários endereços (o tempo vale por endereço) e o handshake do TLS implícito ficam só com o tempo por
operação do socket. O prazo depende de interfaces não documentadas do `smtplib` (`send`, `getreply` e o
atributo `file`), iguais nas versões 3.12.3 e 3.12.14.
"""

from __future__ import annotations

import email.utils
import io
import smtplib
import socket
import ssl
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.headerregistry import Address, HeaderRegistry
from email.message import EmailMessage
from email.policy import EmailPolicy
from typing import Any

import email_validator

from cq_server.config import MailSettings
from cq_server.ports.verification import (
    Delivered,
    PermanentFailure,
    ProbeResult,
    TemporaryFailure,
    VerificationCapabilities,
    VerificationMessage,
)

# Nome fixo da thread que vigia o prazo total da conversa SMTP.
DEADLINE_THREAD_NAME = "cq-smtp-deadline"

# Folga da classificação pelo tempo (o prazo vencido de `_Deadline.expired()`): cobre a granularidade do
# `monotonic` e dos tempos do socket no Windows (15,6 ms).
DEADLINE_SLACK_SECONDS = 0.05

# Nome do remetente e limite das linhas codificadas (RFC 2045, seção 6.7).
FROM_NAME = "Canal de Questões"
LINE_LENGTH = 76

# Revalidação do destinatário: as opções do cadastro de pessoas (`people.validate_email`), sem SMTPUTF8.
_EMAIL_OPTIONS: dict[str, Any] = {
    "check_deliverability": False,
    "allow_smtputf8": False,
    "allow_display_name": False,
    "allow_quoted_local": False,
    "allow_domain_literal": False,
}

# Frases fixas das exceções levantadas aqui (nunca com endereço, credencial ou texto do servidor).
_MSG_DEADLINE = "prazo total da conversa SMTP vencido"
_MSG_NO_AUTH = "o servidor não anuncia AUTH"
_MSG_NO_MECHANISM = "o servidor não anuncia AUTH PLAIN nem LOGIN"
_MSG_NO_ASCII = "destinatário sem forma ASCII"
_MSG_SUBJECT = "assunto com caractere de controle ou quebra de linha"

# Palavras codificadas do `Subject` (RFC 2047): UTF-8 em Q, até 75 caracteres cada; fora dos
# caracteres literais permitidos numa `phrase` (RFC 2047, seção 5, regra 3), todo octeto vira `=XX`, e o espaço, `_`.
_EW_PREFIX = "=?utf-8?q?"
_EW_SUFFIX = "?="
_EW_MAX_LENGTH = 75
_Q_LITERAL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!*+-/")
# Categorias recusadas no assunto (o núcleo já as troca por espaço).
_SUBJECT_FORBIDDEN = frozenset({"Cc", "Zl", "Zp"})


class SmtpDeadline(smtplib.SMTPServerDisconnected):
    """O prazo total da conversa SMTP venceu; o nome do tipo é o `error_type` da linha do prazo."""


# ---------------------------------------------------------------------------------------------
# Mensagem
# ---------------------------------------------------------------------------------------------


def _q_encode(char: str) -> str:
    """Um caractere em Q (RFC 2047, seção 4.2); um caractere nunca se divide entre duas palavras."""
    if char == " ":
        return "_"
    if char in _Q_LITERAL:
        return char
    return "".join(f"={octet:02X}" for octet in char.encode("utf-8"))


def _encoded_words(text: str, first_room: int) -> list[str]:
    """`text` em palavras codificadas: a primeira com até `first_room` caracteres, as demais com até
    75 (`_EW_MAX_LENGTH`)."""
    chrome = len(_EW_PREFIX) + len(_EW_SUFFIX)
    words: list[str] = []
    current = ""
    room = first_room
    for char in text:
        piece = _q_encode(char)
        if current and chrome + len(current) + len(piece) > room:
            words.append(_EW_PREFIX + current + _EW_SUFFIX)
            current = ""
            room = _EW_MAX_LENGTH
        current += piece
    words.append(_EW_PREFIX + current + _EW_SUFFIX)
    return words


class _ExactSubjectHeader:
    """`Subject` guardado como veio (sem o analisador do `email`, que decodificaria um `=?…?=` literal e
    apararia as pontas) e escrito em palavras codificadas, uma por linha, com linhas de até 76 caracteres
    (`LINE_LENGTH`).

    A dobra fica só entre duas palavras codificadas, cujo espaço não conta na decodificação (RFC 2047
    seção 6.2): o texto decodificado é o assunto exato. Caractere de controle ou quebra de linha
    (categorias `Cc`, `Zl` e `Zp`) é `ValueError`, e substituto isolado é `UnicodeEncodeError` (erros de
    montagem). A `EmailPolicy` o usa pelo `HeaderRegistry` deste módulo, que o combina
    com o `BaseHeader`."""

    max_count = 1

    @classmethod
    def parse(cls, value: str, kwds: dict[str, Any]) -> None:
        value.encode("utf-8")  # substituto isolado: UnicodeEncodeError
        if any(unicodedata.category(char) in _SUBJECT_FORBIDDEN for char in value):
            raise ValueError(_MSG_SUBJECT)
        kwds["parse_tree"] = None
        kwds["decoded"] = value

    def fold(self, *, policy: EmailPolicy) -> str:
        name: str = self.name  # type: ignore[attr-defined]  # do `BaseHeader`
        words = _encoded_words(str(self), LINE_LENGTH - len(name) - 2)
        return f"{name}: " + (policy.linesep + " ").join(words) + policy.linesep


_HEADERS = HeaderRegistry()
_HEADERS.map_to_type("subject", _ExactSubjectHeader)

# A política da mensagem: a do `email` (`policy.default`) com CRLF, linhas de 76 no quoted-printable
# e o `Subject` exato.
_POLICY = EmailPolicy(linesep="\r\n", max_line_length=LINE_LENGTH, header_factory=_HEADERS)


def _envelope_address(address: str) -> str:
    """O endereço de envio: o `ascii_email` do `email_validator` (domínio IDN em punycode), revalidado
    com `allow_smtputf8=False`; `EmailNotValidError` (parte local não ASCII: `EmailSyntaxError`) se não
    passar."""
    validated = email_validator.validate_email(address, **_EMAIL_OPTIONS)
    if validated.ascii_email is None:  # não ocorre com `allow_smtputf8=False` (email-validator 2.3.0)
        raise email_validator.EmailSyntaxError(_MSG_NO_ASCII)
    return str(validated.ascii_email)


def _assemble(message: VerificationMessage, address: str, sender: str) -> tuple[bytes, str]:
    """A mensagem de verificação em bytes de 7 bits, pronta para o DATA, e o seu `Message-ID`.

    `From` com o nome fixo, `To` só com o endereço de envio, `Date` em UTC, `Message-ID` com o domínio
    do remetente (nunca o nome da máquina) e o corpo em `text/plain; charset=utf-8` em
    quoted-printable. Erro de montagem: `ValueError` ou `UnicodeEncodeError`."""
    domain = sender.rpartition("@")[2]
    message_id = email.utils.make_msgid(domain=domain)
    mail = EmailMessage(policy=_POLICY)
    mail["From"] = Address(display_name=FROM_NAME, addr_spec=sender)
    mail["To"] = Address(addr_spec=address)
    mail["Subject"] = message.subject
    mail["Date"] = datetime.now(timezone.utc)
    mail["Message-ID"] = message_id
    mail.set_content(message.body, subtype="plain", charset="utf-8", cte="quoted-printable")
    return mail.as_bytes(), message_id


# ---------------------------------------------------------------------------------------------
# Prazo total
# ---------------------------------------------------------------------------------------------


class _Deadline:
    """O prazo total de uma conversa SMTP, com o vigia.

    `remaining()` é o tempo que resta pelo `time.monotonic()`; `expired()` é a regra da classificação
    (`time.monotonic() >= início + T − 0,05`). O vigia é um `threading.Timer` de nome
    `cq-smtp-deadline` que, no fim do prazo, faz o `shutdown` do socket do cliente vigiado (pelo
    `socket.socket`, que não mexe no objeto TLS), para acordar uma leitura ou escrita presa; `stop()`
    faz `cancel()` e `join()`. A trava serializa esse `shutdown` com o `close()` do cliente: o vigia
    nunca age num descritor já fechado e talvez reaproveitado."""

    def __init__(self, seconds: float) -> None:
        self.started = time.monotonic()
        self.ends = self.started + seconds
        self.lock = threading.Lock()
        self.client: smtplib.SMTP | None = None
        self._watchdog = threading.Timer(seconds, self._expire)
        self._watchdog.name = DEADLINE_THREAD_NAME
        self._watchdog.daemon = True
        self._watchdog.start()

    def remaining(self) -> float:
        return self.ends - time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() >= self.ends - DEADLINE_SLACK_SECONDS

    def stop(self) -> None:
        self._watchdog.cancel()
        self._watchdog.join()

    def _expire(self) -> None:
        with self.lock:
            sock = self.client.sock if self.client is not None else None
            if sock is None:
                return
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass  # já fechado, separado pelo `wrap_socket` do STARTTLS ou sem conexão


class _DeadlineReader(io.RawIOBase):
    """Leitura crua do socket em que cada `recv` espera no máximo o que resta do prazo. Limita também a
    resposta que goteja (linhas de continuação sem fim), que o tempo por operação do `smtplib`, sozinho,
    deixaria passar; depois do prazo, `SmtpDeadline`."""

    def __init__(self, sock: socket.socket, deadline: _Deadline) -> None:
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._deadline.remaining()
        if remaining <= 0:
            raise SmtpDeadline(_MSG_DEADLINE)
        self._sock.settimeout(remaining)
        return self._sock.recv_into(buffer)


class _DeadlineSMTP(smtplib.SMTP):
    """`smtplib.SMTP` sob o prazo total: antes de cada escrita e de cada resposta, recusa depois do prazo
    (fecha a conexão e levanta `SmtpDeadline`) e põe o tempo restante no socket; a resposta é lida pelo
    `_DeadlineReader`. O cliente se registra no prazo antes de conectar, para que o vigia o alcance já
    na saudação."""

    def __init__(self, deadline: _Deadline, *args: Any, **kwargs: Any) -> None:
        self._cq_deadline = deadline
        deadline.client = self
        super().__init__(*args, **kwargs)

    def _cq_arm(self) -> None:
        remaining = self._cq_deadline.remaining()
        if remaining <= 0:
            self.close()
            raise SmtpDeadline(_MSG_DEADLINE)
        if self.sock is not None:
            self.sock.settimeout(remaining)

    def send(self, s: Any) -> None:
        self._cq_arm()
        super().send(s)

    def getreply(self) -> tuple[int, bytes]:
        self._cq_arm()
        if self.file is None and self.sock is not None:
            self.file = io.BufferedReader(_DeadlineReader(self.sock, self._cq_deadline))
        return super().getreply()

    def close(self) -> None:
        with self._cq_deadline.lock:
            super().close()


class _DeadlineSMTPSSL(_DeadlineSMTP, smtplib.SMTP_SSL):
    """TLS implícito (`security="tls"`) sob o prazo total; o handshake fica com o tempo do socket."""


# ---------------------------------------------------------------------------------------------
# Classificação (tabela única)
# ---------------------------------------------------------------------------------------------


def _reply_code(exc: Exception) -> int | None:
    """O código SMTP da exceção (o do destinatário, na recusa do RCPT), ou `None`."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [reply[0] for reply in exc.recipients.values()]
        return int(codes[0]) if codes else None
    if isinstance(exc, smtplib.SMTPResponseException):
        return int(exc.smtp_code)
    return None


def _category(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    return "server"


def _classify(exc: Exception, deadline: _Deadline) -> TemporaryFailure | PermanentFailure:
    """A linha da tabela única: o código SMTP primeiro (4xx temporária, 5xx definitiva, mesmo com o prazo
    vencido; −1, 2xx ou 3xx inesperados, temporária), depois o prazo pelo tempo, depois o tipo. Só o
    nome do tipo sai daqui."""
    name = type(exc).__name__
    code = _reply_code(exc)
    if code is not None:
        if 400 <= code <= 499:
            return TemporaryFailure(name)
        if 500 <= code <= 599:
            return PermanentFailure(_category(exc), name)
        return TemporaryFailure(name)
    if deadline.expired():
        return TemporaryFailure(SmtpDeadline.__name__)
    # `SSLCertVerificationError` é também `ValueError`; `SMTPNotSupportedError`, `OSError`. O `ValueError`
    # da montagem volta antes de conectar (`send`); um `ValueError` da conversa (ex.: `binascii.Error` de um
    # desafio 334 mal formado no AUTH) cai em "outra `Exception`": temporária.
    if isinstance(exc, (ssl.SSLCertVerificationError, smtplib.SMTPNotSupportedError)):
        return PermanentFailure("server", name)
    return TemporaryFailure(name)


# ---------------------------------------------------------------------------------------------
# Conversa SMTP
# ---------------------------------------------------------------------------------------------


def _default_context() -> ssl.SSLContext:
    """O contexto TLS padrão: verificação do certificado e do nome já ligadas pelo protocolo,
    CAs do sistema, sem ler o ambiente (o `ssl.create_default_context()` lê `SSLKEYLOGFILE`)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return context


def _hello(client: smtplib.SMTP) -> None:
    """EHLO explícito; resposta diferente de 250 é `SMTPHeloError`, sem recuo para HELO (sem ESMTP não
    há STARTTLS nem AUTH)."""
    code, reply = client.ehlo()
    if code != 250:
        raise smtplib.SMTPHeloError(code, reply)


def _authenticate(client: smtplib.SMTP, username: str, password: str) -> None:
    """Um único AUTH: PLAIN com resposta inicial, se anunciado; senão, LOGIN sem
    resposta inicial; nunca o CRAM-MD5 nem uma segunda tentativa (o `login()` do `smtplib` faria as
    duas coisas). Sem AUTH, ou sem PLAIN e sem LOGIN: `SMTPNotSupportedError`."""
    if not client.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError(_MSG_NO_AUTH)
    mechanisms = client.esmtp_features["auth"].upper().split()
    client.user, client.password = username, password
    if "PLAIN" in mechanisms:
        client.auth("PLAIN", client.auth_plain, initial_response_ok=True)
    elif "LOGIN" in mechanisms:
        client.auth("LOGIN", client.auth_login, initial_response_ok=False)
    else:
        raise smtplib.SMTPNotSupportedError(_MSG_NO_MECHANISM)


def _hang_up(client: smtplib.SMTP | None) -> None:
    """QUIT de melhor esforço: o erro não muda o resultado (depois do 250 do fim do DATA, a mensagem
    foi entregue). Depois do prazo, a guarda o recusa sem escrever nada."""
    if client is None or client.sock is None:
        return
    try:
        client.quit()
    except Exception:  # noqa: BLE001 - melhor esforço; nada da exceção é guardado
        pass


def _probe_failure(stage: ProbeResult, exc: Exception, deadline: _Deadline) -> ProbeResult:
    """A falha da sonda pela etapa: o prazo vencido, em qualquer etapa, é `connection`;
    na etapa da conexão e do `EHLO`, um erro do TLS (o handshake ou o certificado do TLS implícito) é `tls` e
    o resto (recusa, saudação 4xx/5xx, conexão derrubada, `EHLO` recusado) é `connection`; no STARTTLS (não
    anunciado, recusado, handshake ou certificado), `tls`; no AUTH (não anunciado, sem mecanismo aceito ou
    recusado), `auth`.

    [I] O `EHLO` depois do STARTTLS conta como etapa do `EHLO` (`connection`), salvo erro do próprio TLS."""
    if isinstance(exc, SmtpDeadline) or deadline.expired():
        return "connection"
    if stage == "connection" and isinstance(exc, ssl.SSLError):
        return "tls"
    return stage


class SmtpVerificationChannel:
    """Canal de verificação por e-mail (ver o docstring do módulo)."""

    capabilities = VerificationCapabilities(address_kind="email")

    def __init__(self, settings: MailSettings, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self.settings = settings
        self.ssl_context = ssl_context

    def send(self, message: VerificationMessage) -> Delivered | TemporaryFailure | PermanentFailure:
        """Entrega `message` numa conexão própria e devolve o resultado da porta.

        O destinatário que não passa na revalidação e o erro de montagem voltam sem conectar."""
        try:
            address = _envelope_address(message.address)
        except email_validator.EmailNotValidError as exc:
            return PermanentFailure("recipient", type(exc).__name__)
        try:
            raw, message_id = _assemble(message, address, self.settings.sender)
        except ValueError as exc:  # inclui UnicodeEncodeError
            return PermanentFailure("server", type(exc).__name__)
        context = None
        if self.settings.security != "none":
            context = self.ssl_context if self.ssl_context is not None else _default_context()
        return self._deliver(address, raw, message_id, context)

    def _deliver(self, address: str, raw: bytes, message_id: str,
                 context: ssl.SSLContext | None) -> Delivered | TemporaryFailure | PermanentFailure:
        """A conversa SMTP sob o prazo total; no fim, QUIT de melhor esforço, o vigia cancelado e esperado
        e a conexão fechada, nessa ordem, antes de devolver."""
        settings = self.settings
        deadline = _Deadline(settings.timeout_seconds)
        try:
            client = self._connect(deadline, context)
            _hello(client)
            if settings.security == "starttls":
                client.starttls(context=context)
                _hello(client)  # o STARTTLS zera o que o EHLO anunciou (RFC 3207)
            if settings.username is not None and settings.password is not None:
                _authenticate(client, settings.username, settings.password)
            client.sendmail(settings.sender, [address], raw)
            return Delivered(message_id)
        except Exception as exc:  # noqa: BLE001 - a tabela única decide; só o nome do tipo sai daqui
            return _classify(exc, deadline)
        finally:
            _hang_up(deadline.client)
            deadline.stop()
            if deadline.client is not None:
                deadline.client.close()

    def _connect(self, deadline: _Deadline, context: ssl.SSLContext | None) -> smtplib.SMTP:
        """Abre a conexão (o host vai ao construtor, que guarda o nome para o TLS; nunca `SMTP()`
        seguido de `connect()`), com o domínio do remetente no EHLO (nunca o nome da máquina)."""
        settings = self.settings
        local_hostname = settings.sender.rpartition("@")[2]
        if settings.security == "tls":
            return _DeadlineSMTPSSL(deadline, settings.host, settings.port, local_hostname=local_hostname,
                                    timeout=deadline.remaining(), context=context)
        return _DeadlineSMTP(deadline, settings.host, settings.port, local_hostname=local_hostname,
                             timeout=deadline.remaining())

    def probe(self) -> ProbeResult:
        """O teste do servidor sem envio, que cumpre a porta
        `MailServerProbe`: conexão, `EHLO`, STARTTLS (conforme `security`), AUTH e `QUIT`, sob o mesmo prazo
        total; **nunca** `MAIL FROM`. A falha é classificada pela etapa (`connection`, `tls`, `auth`; ver
        `_probe_failure`), não pelo `_classify` do envio. No fim, como no envio: QUIT de melhor esforço (a falha
        dele não muda o resultado), o vigia cancelado e esperado e a conexão fechada, antes de devolver. Nada da
        exceção sai daqui: só o resultado do conjunto fechado.

        "Sem configuração → `not_configured`" fica com quem monta: sem `CQ_SMTP_*` não há `MailSettings` nem
        canal, e o laço recebe `probe=None` (`cq_server.mail_probe`)."""
        settings = self.settings
        context = None
        if settings.security != "none":
            context = self.ssl_context if self.ssl_context is not None else _default_context()
        deadline = _Deadline(settings.timeout_seconds)
        stage: ProbeResult = "connection"
        try:
            client = self._connect(deadline, context)  # no TLS implícito, o handshake já vem aqui
            _hello(client)
            if settings.security == "starttls":
                stage = "tls"
                client.starttls(context=context)  # não anunciado: `SMTPNotSupportedError`
                stage = "connection"
                _hello(client)  # o STARTTLS zera o que o EHLO anunciou (RFC 3207)
            if settings.username is not None and settings.password is not None:
                stage = "auth"
                _authenticate(client, settings.username, settings.password)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - a etapa decide; nada da exceção sai daqui
            return _probe_failure(stage, exc, deadline)
        finally:
            _hang_up(deadline.client)
            deadline.stop()
            if deadline.client is not None:
                deadline.client.close()

    def __repr__(self) -> str:
        return f"SmtpVerificationChannel(settings={self.settings!r})"
