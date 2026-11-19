"""Porta de verificação e adaptador SMTP contra o SMTP falso.




`cq_server.adapters.smtp.SmtpVerificationChannel` fala com o SMTP falso (`tests/helpers/smtp.py`,
servidor real em 127.0.0.1), que registra cada conexão (verbo, TLS e código de cada comando, sem
argumentos), captura as mensagens decodificadas e acusa em `protocol_errors()` qualquer desvio do
cliente (comando fora de ordem, AUTH sem TLS, linha QP acima de 76, 8 bits sem `BODY=8BITMIME`,
`SMTPUTF8` no MAIL, segredo num argumento, fechamento sem QUIT depois de 2xx/3xx sem falha programada).
A fixture `smtp` exige `protocol_errors() == []` e nenhum segredo conhecido nas mensagens, e liga a
guarda de rede (`network_guard`), que anota cada `smtplib.connect` e `socket.connect` do processo.

Seções:
1. **Porta:** `repr(VerificationMessage)` sem endereço nem corpo
   (`test_message_repr_hides_address_and_body`, verde desde o início).
2. **Entrega e mensagem**: nos três modos, uma conexão por envio, EHLO
   explícito com o domínio do remetente (de novo depois do STARTTLS), um AUTH, envelope
   `MAIL FROM:<remetente>` sem `SMTPUTF8`, `RCPT TO:<ascii_email>`, cabeçalhos exatos (`Date` e
   `Message-ID` por forma; o `Subject` decodificado igual ao assunto, ver a nota abaixo),
   quoted-printable com linhas de até 76, nada acima de 127 no bruto e texto decodificado igual ao
   corpo; `none` com `127.0.0.1` entrega (a regra do loopback é só do `MailSettings`).
3. **TLS**: certificado verificado com o contexto padrão e com a CA injetada; STARTTLS
   não anunciado; `SSLKEYLOGFILE` nunca lido; queda no handshake com o conjunto declarado de tipos.
4. **AUTH**: PLAIN quando anunciado, senão LOGIN sem resposta inicial, uma tentativa por envio;
   535/454; servidor sem AUTH ou sem PLAIN e sem LOGIN; sem credenciais, nenhum AUTH.
5. **Destinatário**: o `ascii_email` no envelope e no `To`; revalidação que falha
   (inclusive parte local não ASCII) → `PermanentFailure("recipient", "EmailSyntaxError")`, sem conectar.
6. **Classificação**: uma linha por linha da tabela única de classificação, com o
   resultado, a categoria e o `error_type` exatos (nada do texto do servidor; na queda na abertura, o
   conjunto declarado de tipos); EHLO sem recuo para HELO;
   um código inesperado por etapa; porta fechada.
7. **Prazo total**: `hang` por etapa, `drip`, RSET e QUIT, com o teto de T + 0,5 s
   reais (T = 2 s, o `timeout_seconds` da fixture `smtp_settings`), a hierarquia de `SmtpDeadline` e
   nenhuma thread `cq-smtp-deadline` viva depois do `send` (conferido em todo envio, por `_send`).
8. **Vazamentos**: fd 1 e 2 vazios, nenhum log, resultado ou atributo do canal com
   endereço, código, senha ou as formas do fio, com controle positivo de cada detector.

Tabela única de classificação → função que confere a linha:
- exceção sem código com o prazo vencido → `test_hang_at_each_stage_is_a_deadline_failure`,
  `test_drip_is_bounded_by_the_deadline`;
- saudação, STARTTLS (o caso de outro `SMTPResponseException`), AUTH, MAIL, RCPT, DATA 4xx/5xx, o
  código inesperado de cada etapa, `SMTPServerDisconnected` e o erro no QUIT →
  `test_each_classification_row` (o código antes do prazo também em
  `test_rset_hang_after_a_refusal_keeps_the_refusal_result`);
- EHLO (inclusive o código inesperado) → `test_ehlo_refusal_is_classified_without_falling_back_to_helo`;
- STARTTLS não anunciado → `test_starttls_not_advertised_is_a_permanent_server_refusal`;
- `SSLCertVerificationError` → `test_certificate_failure_is_a_permanent_server_refusal`;
- outro `ssl.SSLError` (queda no handshake) → `test_drop_in_the_tls_handshake_is_temporary`;
- sem AUTH, sem PLAIN nem LOGIN → `test_server_without_a_usable_auth_is_a_permanent_server_refusal`;
- revalidação do destinatário → `test_recipient_that_fails_revalidation_is_refused_without_connecting`;
- `ConnectionError` → `test_closed_port_is_a_temporary_connection_refused`;
- `UnicodeEncodeError`/`ValueError` na montagem → `test_assembly_errors_are_permanent_server_refusals`.
O caso de qualquer outra `Exception` não tem gatilho pelo SMTP falso sem trocar partes do `smtplib` (fica sem
caso aqui; o tratador cobre a `Exception` que escapa do `send`).

**Tempo.** O prazo é tempo real (socket), medido com `time.perf_counter()` em volta do `send` e conferido
pelo teto (≤ T + 0,5 s), sem o caso do servidor que goteja e depois silencia no Windows. O
piso `T − 0,1` só vale onde o prazo tem de vencer: no resultado `SmtpDeadline`, que a tabela exige com o
prazo vencido pelo relógio (`time.monotonic() >= início + T − 0,05`; a folga cobre os 15,6 ms do
`monotonic` no Windows), e no RSET mudo depois de uma recusa, em que o piso prova que o prazo venceu e
que, mesmo assim, o código SMTP decidiu. **Rede de segurança** (não é prova): nos casos com `hang` ou
`drip`, um temporizador solta as conexões presas (`release_hangs`) T + 5,5 s depois do início, para que
um adaptador que ignore o prazo faça o teste falhar pelo teto em vez de travar a suíte.

**Assunto dobrado.** No Python 3.12, o gerador com `max_line_length=76` dobra o assunto do convite
(`… Algoritmos e Programação …`) deixando o espaço antes da dobra e o da continuação, e o texto
decodificado (RFC 5322, seção 2.2.3, e RFC 2047, seção 6.2, como o dublê o decodifica) fica com dois espaços antes da
palavra codificada. A mensagem tem de chegar com o assunto exato (conferido por
igualdade no texto decodificado): o limite de 76 vale para o corpo em quoted-printable no adaptador,
e a dobra dos cabeçalhos é detalhe de implementação que o teste não fixa, só o resultado.

**No esqueleto**, `SmtpVerificationChannel.send` lança `NotImplementedError` (o construtor é real): toda
função, exceto a da seção 1, chama `send` e falha por isso. As asserções sobre o que o esqueleto já tem
ficam em funções que chamam `send`: a hierarquia de `SmtpDeadline` em
`test_hang_at_each_stage_is_a_deadline_failure`; o `capabilities` do canal em
`test_delivers_the_exact_message_in_each_security_mode`; o `repr` do canal sem a senha em
`test_nothing_leaks_to_fds_logs_results_or_the_channel`.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import email.utils
import re
import secrets
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from typing import Any

import email_validator
import pytest

from cq_server.adapters.smtp import SmtpDeadline, SmtpVerificationChannel
from cq_server.ports.verification import (
    Delivered,
    PermanentFailure,
    TemporaryFailure,
    VerificationCapabilities,
    VerificationMessage,
)
from tests.helpers.canary import STUDENT_A, STUDENT_PREFIXES, find_canary, find_secrets
from tests.helpers.clock import FakeClock
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import (
    find_invite_codes,
    invite_code_digest,
    invite_code_label,
    mail_secrets,
)
from tests.helpers.smtp import SENDER, AuthAttempt, CapturedMessage, FakeSmtp, SmtpCommand, SmtpConnection

# ---------------------------------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------------------------------

T = 2.0  # `timeout_seconds` da fixture `smtp_settings`
CEILING = T + 0.5  # teto do prazo total em tempo real
FLOOR = T - 0.1  # piso de um `SmtpDeadline` (classificação pelo tempo; ver o docstring)
SAFETY_SECONDS = CEILING + 5.0  # rede de segurança dos casos com `hang`/`drip` (não é prova)
DEADLINE_THREAD = "cq-smtp-deadline"  # nome fixo do vigia
SENDER_DOMAIN = "cq.example.org"  # domínio de `SENDER`: EHLO e `Message-ID`
FROM_NAME = "Canal de Questões"  # nome do remetente

# Queda no handshake TLS: o tipo depende do sistema.
HANDSHAKE_DROP_TYPES = frozenset({"SMTPServerDisconnected", "SSLEOFError", "SSLZeroReturnError",
                                  "SSLError", "ConnectionResetError", "ConnectionAbortedError"})
# Queda na abertura (RST logo depois de o servidor aceitar, antes da saudação): o tipo também depende do
# sistema. No Linux, o `ConnectionResetError` pode sair do `socket.create_connection`, antes do `getreply` que o
# embrulharia em `SMTPServerDisconnected` (o caso de `ConnectionError` na classificação de erros do adaptador).
OPENING_DROP_TYPES = frozenset({"SMTPServerDisconnected", "ConnectionResetError"})
OPENING_DROP = object()  # o esperado da queda na abertura: temporária, com um dos `OPENING_DROP_TYPES`
UNREADABLE = "XYZ ilegível"  # linha que o `smtplib` lê como código −1
ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")  # a forma do `error_type` na porta de verificação

# Opções da API base (`people.validate_email`) com `allow_smtputf8=False`: o oráculo da revalidação.
CORE_EMAIL_OPTIONS: dict[str, Any] = {"check_deliverability": False, "allow_display_name": False,
                                    "allow_quoted_local": False, "allow_domain_literal": False,
                                    "allow_smtputf8": False}

ADDRESS = "Aluno.Teste@Example.ORG"
ADDRESS_ASCII = "Aluno.Teste@example.org"  # `ascii_email`: caixa da parte local, domínio minúsculo

COURSE = "MC102 Algoritmos e Programação de Computadores"
TERM = "2026s2"
BOT_USERNAME = "cq_mc102_algoritmos_prog_2026bot"  # 32 caracteres: o link tem 74
CODE = secrets.token_urlsafe(16)  # código de convite sintético (22 caracteres base64url)
LINK = f"https://t.me/{BOT_USERNAME}?start={CODE}"
VALIDITY = "2031-01-08 09:00 -03 (America/Sao_Paulo)"
SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
BODY = (
    "Olá!\n"
    "\n"
    f"Você está na lista da turma do oferecimento {COURSE} ({TERM}). As questões da disciplina "
    "chegam por um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    f"{LINK}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot "
    "vai mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    f"O link vale até {VALIDITY}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
    "e-mail institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.\n"
)
# Corpo e assunto com as bordas do quoted-printable e do dot-stuffing: linha que começa com ponto,
# `=` literal, espaço e TAB no fim da linha, caractere fora do BMP e linha longa sem espaço.
EDGE_SUBJECT = "Seu link para o bot do oferecimento Cálculo 𝔸 🎓 (2026s2)"
EDGE_BODY = (
    ".linha que começa com ponto\n"
    "..duas\n"
    ".\n"
    "igual = e =3D literal\n"
    "fim com espaços   \n"
    "fim com TAB\t\n"
    "emoji 🎓 e 𝔸 fora do BMP\n"
    + "ã" * 90 + "\n"
    + "x" * 200 + "\n"
)

# Rótulos do detector para o e-mail do aluno canário A (controle positivo das varreduras).
STUDENT_A_EMAIL_LABELS = sorted([
    f"aluno-email-dominio:{STUDENT_A.email}", f"aluno-email-local:{STUDENT_A.email}",
    f"aluno-email:{STUDENT_A.email}", "aluno-palavra:jurubeba", "aluno-palavra:leocadia"])

assert len(BOT_USERNAME) == 32 and len(CODE) == 22 and len(LINK) == 74


# ---------------------------------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------------------------------


def _reset(smtp: FakeSmtp, clock: FakeClock, **config: Any) -> None:
    """Novo `reset` do dublê com o relógio do teste (credenciais novas, salvo `credentials=`)."""
    smtp.reset(clock, **config)


def _settings(base: Any, smtp: FakeSmtp, **changes: Any) -> Any:
    """O `MailSettings` do teste: o da fixture `smtp_settings` com a porta e as credenciais do `reset`
    mais recente do dublê (nenhuma, se o dublê não tem) e as trocas pedidas."""
    creds = smtp.credentials
    values: dict[str, Any] = {"port": smtp.port,
                              "username": creds.username if creds is not None else None,
                              "password": creds.password if creds is not None else None}
    values.update(changes)
    return dataclasses.replace(base, **values)


def _channel(base: Any, smtp: FakeSmtp, *, trust: bool = True, **changes: Any) -> SmtpVerificationChannel:
    """O adaptador real; com `trust`, o contexto TLS confia só na CA do dublê (por injeção);
    sem, o contexto padrão (`ssl_context=None`)."""
    return SmtpVerificationChannel(_settings(base, smtp, **changes),
                                   ssl_context=smtp.client_context() if trust else None)


def _message(address: str = ADDRESS, *, subject: str = SUBJECT, body: str = BODY) -> VerificationMessage:
    return VerificationMessage(address=address, subject=subject, body=body)


@dataclasses.dataclass(frozen=True)
class _Sent:
    result: Any
    elapsed: float
    before: datetime
    after: datetime


def _deadline_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == DEADLINE_THREAD and t.is_alive()]


def _send(channel: SmtpVerificationChannel, message: VerificationMessage) -> _Sent:
    """Chama `send` medindo o tempo real e confere o que vale para todo envio: o resultado é
    exatamente um dos três tipos da porta, e nenhuma thread `cq-smtp-deadline` fica viva (o adaptador
    faz `cancel()` e `join()` do vigia antes de devolver)."""
    before = datetime.now(timezone.utc)
    start = time.perf_counter()
    result = channel.send(message)
    elapsed = time.perf_counter() - start
    after = datetime.now(timezone.utc)
    assert type(result) in (Delivered, TemporaryFailure, PermanentFailure)
    assert _deadline_threads() == []
    return _Sent(result, elapsed, before, after)


@contextlib.contextmanager
def _released_if_stuck(smtp: FakeSmtp) -> Iterator[None]:
    """Rede de segurança dos casos com `hang` ou `drip` (não é prova de nada): se o adaptador ignorar o
    prazo, o dublê solta as conexões presas `SAFETY_SECONDS` depois do início (`release_hangs`), o
    `send` volta e o teste falha pelo teto, em vez de travar a suíte. Um adaptador correto devolve
    antes do disparo, e o temporizador é cancelado e esperado na saída."""
    timer = threading.Timer(SAFETY_SECONDS, smtp.release_hangs)
    timer.name = "rede-de-seguranca"
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()


def _finished(smtp: FakeSmtp) -> list[SmtpConnection]:
    """As conexões do dublê depois de todas terminarem (ao menos uma)."""

    def done() -> list[SmtpConnection] | None:
        conns = smtp.connections()
        return conns if conns and all(c.outcome != "open" for c in conns) else None

    return smtp.wait_for(done)


def _only_connection(smtp: FakeSmtp) -> SmtpConnection:
    conns = _finished(smtp)
    assert len(conns) == 1, smtp.transcript()
    return conns[0]


def _attempts(network_guard: Any) -> list[tuple[str, str | None, int | None, bool]]:
    return [(a.event, a.host, a.port, a.blocked) for a in network_guard.attempts()]


def _one_connection_to(port: int) -> list[tuple[str, str, int, bool]]:
    """Uma conexão por envio: o `smtplib.connect` e o `socket.connect` a 127.0.0.1."""
    return [("smtplib.connect", "127.0.0.1", port, False), ("socket.connect", "127.0.0.1", port, False)]


def _commands(security: str, mechanism: str | None = "PLAIN") -> tuple[SmtpCommand, ...]:
    """A conversa exata de uma entrega (verbo, TLS, código), com o EHLO explícito na abertura e de
    novo depois do STARTTLS, um único AUTH e o QUIT."""
    tls = security != "none"
    out: list[SmtpCommand] = []
    if security == "tls":
        out.append(SmtpCommand("<tls>", True, None))
    out += [SmtpCommand("<connect>", security == "tls", 220), SmtpCommand("EHLO", security == "tls", 250)]
    if security == "starttls":
        out += [SmtpCommand("STARTTLS", False, 220), SmtpCommand("<tls>", True, None),
                SmtpCommand("EHLO", True, 250)]
    if mechanism == "PLAIN":
        out.append(SmtpCommand("AUTH", tls, 235))
    elif mechanism == "LOGIN":
        out += [SmtpCommand("AUTH", tls, 334), SmtpCommand("<auth-response>", tls, 334),
                SmtpCommand("<auth-response>", tls, 235)]
    out += [SmtpCommand("MAIL", tls, 250), SmtpCommand("RCPT", tls, 250), SmtpCommand("DATA", tls, 354),
            SmtpCommand("<data-end>", tls, 250), SmtpCommand("QUIT", tls, 221)]
    return tuple(out)


def _assert_message_ref(result: Delivered, *forbidden: str) -> None:
    """`Delivered.message_ref`: texto não vazio, ASCII visível, sem nenhum dos textos proibidos
    (endereço, código, corpo, senha). O valor não é fixado."""
    assert type(result.message_ref) is str
    assert re.fullmatch(r"[\x21-\x7e]{1,998}", result.message_ref)
    for text in forbidden:
        assert text.lower() not in result.message_ref.lower()


def _assert_invite_message(message: CapturedMessage, sent: _Sent, *, address_ascii: str,
                           subject: str, body: str) -> None:
    """A mensagem do convite como o dublê a recebeu."""
    # Envelope: remetente e endereço de envio (o `ascii_email`), sem SMTPUTF8 nem BODY=8BITMIME.
    assert message.mail_from == SENDER
    assert tuple(p.lower() for p in message.mail_params) == (f"size={len(message.raw)}",)
    assert message.rcpt_to == (address_ascii,)
    assert message.acknowledged is True
    # Cabeçalhos: exatamente estes, sem repetição.
    assert sorted(name.lower() for name in message.header_names()) == [
        "content-transfer-encoding", "content-type", "date", "from", "message-id", "mime-version",
        "subject", "to"]
    assert message.header("From") == f"{FROM_NAME} <{SENDER}>"
    assert message.message["From"].addresses == (Address(FROM_NAME, addr_spec=SENDER),)
    assert message.header("To") == address_ascii  # só o endereço, sem nome
    assert message.message["To"].addresses == (Address("", addr_spec=address_ascii),)
    # Assunto exato depois da decodificação (sem o espaço dobrado da dobra do Python 3.12 com
    # `max_line_length=76`; ver "Assunto dobrado" no docstring do módulo).
    assert message.header("Subject") == subject
    assert message.header("MIME-Version") == "1.0"
    assert message.header("Content-Transfer-Encoding") == "quoted-printable"
    assert message.message.get_content_type() == "text/plain"
    assert message.message.get_content_charset() == "utf-8"
    assert message.message.is_multipart() is False
    # `Date` por forma: UTC (+0000) e entre o antes e o depois do envio, em tempo real.
    date = message.header("Date")
    assert date is not None
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} \+0000", date)
    moment = email.utils.parsedate_to_datetime(date)
    assert moment.utcoffset() == timedelta(0)
    assert sent.before.replace(microsecond=0) <= moment <= sent.after
    # `Message-ID` por forma: o domínio do remetente (nunca o nome da máquina).
    assert re.fullmatch(r"<[^<>@\s]+@cq\.example\.org>", message.header("Message-ID") or "")
    # Corpo: texto decodificado igual; bruto em 7 bits; linhas codificadas de até 76 caracteres.
    assert message.text == body
    assert max(message.raw) < 0x80
    _, separator, encoded = message.raw.partition(b"\r\n\r\n")
    assert separator == b"\r\n\r\n"
    assert max(len(line) for line in encoded.split(b"\r\n")) <= 76


def _assert_delivered(smtp: FakeSmtp, sent: _Sent, *, security: str, address_ascii: str,
                      subject: str = SUBJECT, body: str = BODY, mechanism: str | None = "PLAIN") -> None:
    """Entrega completa: resultado, a mensagem e a conversa exata numa só conexão."""
    assert type(sent.result) is Delivered
    _assert_message_ref(sent.result, address_ascii, CODE, LINK)
    [message] = smtp.messages()
    _assert_invite_message(message, sent, address_ascii=address_ascii, subject=subject, body=body)
    conn = _only_connection(smtp)
    tls = security != "none"
    assert conn.commands == _commands(security, mechanism)
    assert conn.ehlo_name == SENDER_DOMAIN  # `local_hostname` = domínio do remetente
    if mechanism is None:
        assert conn.auth == ()
    else:
        assert conn.auth == (AuthAttempt(mechanism, mechanism == "PLAIN", True, True, tls, 235),)
    assert (conn.outcome, conn.quit, conn.tls) == ("quit", True, tls)
    assert (message.conn_seq, message.tls, message.authenticated) == (conn.seq, tls, mechanism is not None)
    if tls:
        assert conn.tls_version in {"TLSv1.2", "TLSv1.3"}
        assert message.tls_version == conn.tls_version
    else:
        assert (conn.tls_version, message.tls_version) == (None, None)


def _wire_forms(username: str, password: str) -> list[str]:
    """A senha e as formas em que o `smtplib` a põe no fio (PLAIN e LOGIN), como `mail_secrets`."""
    return [password,
            base64.b64encode(f"\0{username}\0{password}".encode()).decode("ascii"),
            base64.b64encode(password.encode()).decode("ascii")]


# ---------------------------------------------------------------------------------------------------
# 1. Porta
# ---------------------------------------------------------------------------------------------------


def test_message_repr_hides_address_and_body() -> None:
    """`VerificationMessage` com o endereço (identidade) e o corpo (link com o código) fora do `repr`
    e do `str`; só o assunto aparece; a mensagem é imutável (identidade só pela revelação).
    Verde desde o início: os tipos da porta são reais no esqueleto."""
    message = _message(STUDENT_A.email, subject="Assunto do convite")
    assert repr(message) == "VerificationMessage(subject='Assunto do convite')"
    assert str(message) == repr(message)
    assert find_canary(repr(message), prefixes=STUDENT_PREFIXES) == []
    assert find_invite_codes(repr(message), [invite_code_digest(CODE)]) == []
    assert "Olá" not in repr(message) and BOT_USERNAME not in repr(message)
    # Controles positivos dos detectores.
    assert find_canary(STUDENT_A.email, prefixes=STUDENT_PREFIXES) == STUDENT_A_EMAIL_LABELS
    assert find_invite_codes(BODY, [invite_code_digest(CODE)]) == [
        invite_code_label(invite_code_digest(CODE))]
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.address = ADDRESS  # type: ignore[misc]
    assert (message.address, message.subject, message.body) == (STUDENT_A.email, "Assunto do convite", BODY)


# ---------------------------------------------------------------------------------------------------
# 2. Entrega e mensagem
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("security", ["none", "starttls", "tls"])
def test_delivers_the_exact_message_in_each_security_mode(smtp: FakeSmtp, clock: FakeClock,
                                                          smtp_settings: Any, network_guard: Any,
                                                          security: str) -> None:
    """Entrega nos três modos: `none` com `127.0.0.1`
    entrega (o adaptador não confere de novo o loopback); com a CA injetada, `starttls` e `tls`
    entregam com todo comando depois do handshake em TLS. Uma conexão por envio; EHLO explícito com o
    domínio do remetente, repetido depois do STARTTLS; um AUTH PLAIN; envelope
    `MAIL FROM:<remetente>` sem `SMTPUTF8`; `RCPT TO:<ascii_email>`; `From` `"Canal de Questões"
    <remetente>`, `To` sem nome, `Date` em UTC e `Message-ID` com o domínio do remetente (por forma),
    quoted-printable com linhas de até 76, bruto em 7 bits e texto igual ao corpo.
    Junto: o `capabilities` do canal (`address_kind == "email"`)."""
    _reset(smtp, clock, security=security)
    channel = _channel(smtp_settings, smtp, security=security)
    assert channel.capabilities == VerificationCapabilities(address_kind="email")
    sent = _send(channel, _message())
    _assert_delivered(smtp, sent, security=security, address_ascii=ADDRESS_ASCII)
    assert _attempts(network_guard) == _one_connection_to(smtp.port)
    assert [(link.rcpt_to, link.url, link.bot_username, link.code, link.acknowledged)
            for link in smtp.invite_links()] == [((ADDRESS_ASCII,), LINK, BOT_USERNAME, CODE, True)]


@pytest.mark.parametrize(("address", "address_ascii"), [
    pytest.param(ADDRESS, ADDRESS_ASCII, id="dominio-em-maiusculas"),
    pytest.param("Aluno.IDN@Exämple.ORG", "Aluno.IDN@xn--exmple-cua.org", id="dominio-idn"),
    pytest.param("aluno@MÜNCHEN.Example.DE", "aluno@xn--mnchen-3ya.example.de", id="idn-maiusculo"),
    pytest.param(STUDENT_A.email, "Leocadia.JURUBEBA@jequitiba.example.org", id="aluno-canario"),
])
def test_envelope_and_to_use_the_ascii_email(smtp: FakeSmtp, mail_channel: SmtpVerificationChannel,
                                             address: str, address_ascii: str) -> None:
    """O endereço de envio é o `ascii_email` do `email_validator` (parte local com a caixa gravada,
    domínio em minúsculas e, se IDN, em punycode), no `RCPT TO` e no `To`, sem caminho SMTPUTF8
    (e-mail da lista com parte local não ASCII, o domínio internacional em
    punycode). O oráculo (`email_validator` com as opções da API base e
    `allow_smtputf8=False`) concorda com o literal esperado. Usa o `mail_channel` da fixture."""
    assert email_validator.validate_email(address, **CORE_EMAIL_OPTIONS).ascii_email == address_ascii
    sent = _send(mail_channel, _message(address))
    _assert_delivered(smtp, sent, security="none", address_ascii=address_ascii)


@pytest.mark.parametrize(("subject", "body"), [
    pytest.param(SUBJECT, BODY, id="convite"),
    pytest.param(EDGE_SUBJECT, EDGE_BODY, id="bordas"),
])
def test_body_round_trips_through_quoted_printable(smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any,
                                                   subject: str, body: str) -> None:
    """Assunto e corpo chegam intactos depois do quoted-printable e do dot-stuffing: linha que começa
    com ponto, `=` literal, espaço e TAB no fim da linha, caracteres fora do BMP, linha longa.
    O dublê acusa linha codificada acima de 76 e 8 bits sem
    `BODY=8BITMIME` (a fixture exige `protocol_errors() == []`)."""
    channel = _channel(smtp_settings, smtp)
    sent = _send(channel, _message(subject=subject, body=body))
    _assert_delivered(smtp, sent, security="none", address_ascii=ADDRESS_ASCII, subject=subject, body=body)


# ---------------------------------------------------------------------------------------------------
# 3. TLS
# ---------------------------------------------------------------------------------------------------

# (certificado, CA injetada, alerta do cliente anotado pelo dublê)
CERTIFICATE_FAILURES = [
    pytest.param("valid", False, "TLSV1_ALERT_UNKNOWN_CA", id="contexto-padrao"),
    pytest.param("wrong_host", True, "SSLV3_ALERT_BAD_CERTIFICATE", id="nome-errado"),
    pytest.param("expired", True, "SSLV3_ALERT_CERTIFICATE_EXPIRED", id="vencido"),
    pytest.param("self_signed", True, "TLSV1_ALERT_UNKNOWN_CA", id="autoassinado"),
]


@pytest.mark.parametrize("security", ["starttls", "tls"])
@pytest.mark.parametrize(("cert", "trust", "alert"), CERTIFICATE_FAILURES)
def test_certificate_failure_is_a_permanent_server_refusal(smtp: FakeSmtp, clock: FakeClock,
                                                           smtp_settings: Any, security: str, cert: str,
                                                           trust: bool, alert: str) -> None:
    """Verificação do certificado: com o contexto padrão
    (`ssl.SSLContext(PROTOCOL_TLS_CLIENT)` + `load_default_certs()`, sem a CA de teste), o certificado
    `valid` do dublê é recusado com alerta de CA desconhecida; com a CA injetada, `wrong_host`,
    `expired` e `self_signed` são recusados. Resultado: `PermanentFailure("server",
    "SSLCertVerificationError")`; o alerta é lido com `smtp.wait_for`; nada depois do handshake
    (nenhum AUTH, MAIL, RCPT nem DATA) e nenhuma mensagem."""
    _reset(smtp, clock, security=security, cert=cert)
    sent = _send(_channel(smtp_settings, smtp, trust=trust, security=security), _message())
    assert sent.result == PermanentFailure("server", "SSLCertVerificationError")
    conn = smtp.wait_for(lambda: next((c for c in smtp.connections()
                                       if c.tls_alert is not None and c.outcome != "open"), None))
    assert (conn.tls_alert, conn.outcome, conn.tls, conn.auth) == (alert, "tls_failed", False, ())
    expected = ("<tls>",) if security == "tls" else ("<connect>", "EHLO", "STARTTLS", "<tls>")
    assert conn.verbs == expected
    assert _only_connection(smtp) == conn
    assert smtp.messages() == []


def test_starttls_not_advertised_is_a_permanent_server_refusal(smtp: FakeSmtp, clock: FakeClock,
                                                               smtp_settings: Any) -> None:
    """Modo `starttls` sem STARTTLS anunciado: `PermanentFailure("server", "SMTPNotSupportedError")`,
    sem voltar ao texto claro: nenhum AUTH nem MAIL, e o QUIT (STARTTLS
    ausente)."""
    _reset(smtp, clock, security="starttls", advertise_starttls=False)
    sent = _send(_channel(smtp_settings, smtp, security="starttls"), _message())
    assert sent.result == PermanentFailure("server", "SMTPNotSupportedError")
    conn = _only_connection(smtp)
    assert conn.commands == (SmtpCommand("<connect>", False, 220), SmtpCommand("EHLO", False, 250),
                             SmtpCommand("QUIT", False, 221))
    assert (conn.auth, conn.tls, conn.outcome) == ((), False, "quit")
    assert smtp.messages() == []


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_default_tls_context_does_not_read_sslkeylogfile(smtp: FakeSmtp, clock: FakeClock,
                                                         smtp_settings: Any, tmp_path: Any,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         security: str) -> None:
    """Com `SSLKEYLOGFILE` apontando para um arquivo inexistente, definido **antes** de construir o
    adaptador com o contexto padrão, o arquivo não existe depois do envio, em `starttls` e em `tls` (o
    certificado do dublê falha, porque a CA de teste não é confiável, mas o handshake acontece: o
    dublê anota o alerta). Controle positivo: no mesmo ambiente, `ssl.create_default_context()` cria o
    arquivo."""
    keylog = tmp_path / "chaves-tls.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    _reset(smtp, clock, security=security)
    channel = _channel(smtp_settings, smtp, trust=False, security=security)
    sent = _send(channel, _message())
    assert sent.result == PermanentFailure("server", "SSLCertVerificationError")
    assert _only_connection(smtp).tls_alert == "TLSV1_ALERT_UNKNOWN_CA"
    assert keylog.exists() is False
    assert list(tmp_path.iterdir()) == []
    ssl.create_default_context()  # controle positivo: este lê o ambiente
    assert keylog.read_text(encoding="ascii").startswith("# TLS secrets log file")


# (modo, programação da falha no handshake)
HANDSHAKE_DROPS = [
    pytest.param("starttls", lambda s: s.fail_network("tls", "reset"), id="starttls-rst"),
    pytest.param("starttls", lambda s: s.fail_network("tls", "close"), id="starttls-fin"),
    pytest.param("starttls", lambda s: s.fail("tls", 454), id="starttls-texto-claro"),
    pytest.param("tls", lambda s: s.fail_network("tls", "reset"), id="tls-rst"),
    pytest.param("tls", lambda s: s.fail_network("tls", "close"), id="tls-fin"),
    pytest.param("tls", lambda s: s.fail("tls", 454), id="tls-texto-claro"),
]


@pytest.mark.parametrize(("security", "arrange"), HANDSHAKE_DROPS)
def test_drop_in_the_tls_handshake_is_temporary(smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any,
                                                security: str,
                                                arrange: Callable[[FakeSmtp], None]) -> None:
    """Queda no handshake TLS (RST, FIN ou texto em claro no lugar do handshake): temporária, com o
    `error_type` no conjunto declarado, porque o tipo depende do sistema (na classificação de erros
    do adaptador, o caso de outro `ssl.SSLError` e o da queda no handshake); nada depois do handshake."""
    _reset(smtp, clock, security=security)
    arrange(smtp)
    sent = _send(_channel(smtp_settings, smtp, security=security), _message())
    assert type(sent.result) is TemporaryFailure
    assert sent.result.error_type in HANDSHAKE_DROP_TYPES
    conn = _only_connection(smtp)
    assert conn.verbs[-1] == "<tls>"
    assert not {"AUTH", "MAIL", "RCPT", "DATA"} & set(conn.verbs)
    assert smtp.messages() == []


# ---------------------------------------------------------------------------------------------------
# 4. AUTH
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("advertised", "mechanism"), [
    pytest.param(("PLAIN", "LOGIN"), "PLAIN", id="plain-e-login"),
    pytest.param(("LOGIN", "PLAIN"), "PLAIN", id="login-e-plain"),
    pytest.param(("CRAM-MD5", "PLAIN", "LOGIN"), "PLAIN", id="cram-md5-plain-login"),
    pytest.param(("LOGIN",), "LOGIN", id="so-login"),
    pytest.param(("CRAM-MD5", "LOGIN"), "LOGIN", id="cram-md5-e-login"),
])
@pytest.mark.parametrize("security", ["none", "starttls"])
def test_auth_uses_plain_when_advertised_else_login_without_initial_response(
        smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, security: str,
        advertised: tuple[str, ...], mechanism: str) -> None:
    """Um único AUTH por envio: PLAIN (com resposta inicial) quando anunciado, senão LOGIN sem
    resposta inicial; nunca o CRAM-MD5, mesmo anunciado e preferido pelo `login()` do `smtplib`.
    No `starttls`, o AUTH só depois do TLS."""
    _reset(smtp, clock, security=security, advertise_auth=advertised)
    sent = _send(_channel(smtp_settings, smtp, security=security), _message())
    _assert_delivered(smtp, sent, security=security, address_ascii=ADDRESS_ASCII, mechanism=mechanism)


def _wrong_password() -> str:
    return "senha-errada-" + secrets.token_urlsafe(12)


# (anunciados, falha programada, conversa depois do EHLO, tentativas anotadas, resultado)
AUTH_REFUSALS = [
    pytest.param(("PLAIN", "LOGIN"), lambda s: s.fail("auth", 535), False,
                 (SmtpCommand("AUTH", False, 535),), (),
                 PermanentFailure("auth", "SMTPAuthenticationError"), id="535"),
    pytest.param(("PLAIN", "LOGIN"), lambda s: s.fail("auth", 454), False,
                 (SmtpCommand("AUTH", False, 454),), (),
                 TemporaryFailure("SMTPAuthenticationError"), id="454"),
    pytest.param(("PLAIN", "LOGIN"), None, True,
                 (SmtpCommand("AUTH", False, 535),),
                 (AuthAttempt("PLAIN", True, True, False, False, 535),),
                 PermanentFailure("auth", "SMTPAuthenticationError"), id="senha-errada-plain"),
    pytest.param(("LOGIN",), None, True,
                 (SmtpCommand("AUTH", False, 334), SmtpCommand("<auth-response>", False, 334),
                  SmtpCommand("<auth-response>", False, 535)),
                 (AuthAttempt("LOGIN", False, True, False, False, 535),),
                 PermanentFailure("auth", "SMTPAuthenticationError"), id="senha-errada-login"),
]


@pytest.mark.parametrize(("advertised", "arrange", "wrong", "exchange", "attempts", "expected"),
                         AUTH_REFUSALS)
def test_auth_refusal_is_classified_after_a_single_attempt(
        smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, advertised: tuple[str, ...],
        arrange: Callable[[FakeSmtp], None] | None, wrong: bool,
        exchange: tuple[SmtpCommand, ...], attempts: tuple[AuthAttempt, ...], expected: Any) -> None:
    """AUTH recusado: 535 → `PermanentFailure("auth", "SMTPAuthenticationError")`; 454 →
    `TemporaryFailure("SMTPAuthenticationError")`. Uma
    tentativa por envio:
    com a senha errada e PLAIN e LOGIN anunciados, nada de segunda tentativa com o LOGIN (o `login()`
    do `smtplib` tentaria); nenhum MAIL; o QUIT fecha a conexão."""
    _reset(smtp, clock, advertise_auth=advertised)
    if arrange is not None:
        arrange(smtp)
    changes = {"password": _wrong_password()} if wrong else {}
    sent = _send(_channel(smtp_settings, smtp, **changes), _message())
    assert sent.result == expected
    conn = _only_connection(smtp)
    assert conn.commands == ((SmtpCommand("<connect>", False, 220), SmtpCommand("EHLO", False, 250))
                             + exchange + (SmtpCommand("QUIT", False, 221),))
    assert conn.auth == attempts
    assert smtp.messages() == []


@pytest.mark.parametrize(("config", "verbs"), [
    pytest.param({"credentials": None}, ("<connect>", "EHLO", "QUIT"), id="sem-auth-anunciado"),
    pytest.param({"advertise_auth": ()}, ("<connect>", "EHLO", "QUIT"), id="auth-sem-mecanismos"),
    pytest.param({"advertise_auth": ("CRAM-MD5",)}, ("<connect>", "EHLO", "QUIT"), id="so-cram-md5"),
    pytest.param({"security": "starttls", "advertise_auth": ("CRAM-MD5",)},
                 ("<connect>", "EHLO", "STARTTLS", "<tls>", "EHLO", "QUIT"), id="starttls-so-cram-md5"),
])
def test_server_without_a_usable_auth_is_a_permanent_server_refusal(
        smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, config: dict[str, Any],
        verbs: tuple[str, ...]) -> None:
    """Com credenciais, servidor sem AUTH, ou sem PLAIN e sem LOGIN →
    `PermanentFailure("server", "SMTPNotSupportedError")`, sem AUTH nem MAIL (o caso do AUTH
    ausente no servidor)."""
    _reset(smtp, clock, **config)
    credentials = {"username": "cq-smtp-ficticio", "password": secrets.token_urlsafe(24)}
    channel = _channel(smtp_settings, smtp, security=config.get("security", "none"), **credentials)
    sent = _send(channel, _message())
    assert sent.result == PermanentFailure("server", "SMTPNotSupportedError")
    conn = _only_connection(smtp)
    assert (conn.verbs, conn.auth, conn.outcome) == (verbs, (), "quit")
    assert smtp.messages() == []


def test_without_credentials_no_auth_is_attempted(smtp: FakeSmtp, clock: FakeClock,
                                                  smtp_settings: Any) -> None:
    """Sem usuário e senha (as duas ou nenhuma), o adaptador não autentica e entrega
    num servidor sem AUTH."""
    _reset(smtp, clock, credentials=None)
    sent = _send(_channel(smtp_settings, smtp, username=None, password=None), _message())
    _assert_delivered(smtp, sent, security="none", address_ascii=ADDRESS_ASCII, mechanism=None)


# ---------------------------------------------------------------------------------------------------
# 5. Destinatário
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("address", [
    pytest.param("joão@example.org", id="parte-local-nao-ascii"),
    pytest.param("Álvaro.Souza@Example.ORG", id="parte-local-nao-ascii-maiuscula"),
    pytest.param("sem-arroba.example.org", id="sem-arroba"),
    pytest.param("aluno@example", id="dominio-sem-ponto"),
    pytest.param("aluno@exemplo.test", id="dominio-test"),
    pytest.param("a b@example.org", id="espaco"),
    pytest.param("aluno@example.org\r\nBcc: outro@example.org", id="injecao-de-cabecalho"),
    pytest.param('"entre.aspas"@example.org', id="parte-local-entre-aspas"),
    pytest.param("aluno@[127.0.0.1]", id="literal-de-dominio"),
    pytest.param("Nome <aluno@example.org>", id="nome-de-exibicao"),
    pytest.param("", id="vazio"),
])
def test_recipient_that_fails_revalidation_is_refused_without_connecting(
        smtp: FakeSmtp, mail_channel: SmtpVerificationChannel, network_guard: Any, address: str) -> None:
    """Destinatário que não passa na revalidação do `email_validator` com `allow_smtputf8=False` →
    `PermanentFailure("recipient", <tipo da exceção do email_validator>)`, **sem conectar** (zero
    conexões); parte local não ASCII → `EmailSyntaxError` (email-validator 2.3.0), sem caminho SMTPUTF8
    (e-mail da lista com parte local não ASCII).
    O oráculo confirma o tipo."""
    with pytest.raises(email_validator.EmailNotValidError) as oracle:
        email_validator.validate_email(address, **CORE_EMAIL_OPTIONS)
    assert type(oracle.value).__name__ == "EmailSyntaxError"
    sent = _send(mail_channel, _message(address))
    assert sent.result == PermanentFailure("recipient", "EmailSyntaxError")
    assert _attempts(network_guard) == []
    assert smtp.protocol_errors() == []
    assert (smtp.connections(), smtp.messages()) == ([], [])


# ---------------------------------------------------------------------------------------------------
# 6. Classificação
# ---------------------------------------------------------------------------------------------------

# (modo, programação, comando em que a falha programada caiu, resultado, mensagem capturada)
# Resultado `Delivered` é conferido pelo tipo e pela mensagem (nada fixa o `message_ref`).
CLASSIFICATION = [
    # Saudação: `SMTPConnectError` 4xx / 5xx.
    pytest.param("none", lambda s: s.fail("connect", 421), SmtpCommand("<connect>", False, 421),
                 TemporaryFailure("SMTPConnectError"), None, id="saudacao-421"),
    pytest.param("none", lambda s: s.fail("connect", 554), SmtpCommand("<connect>", False, 554),
                 PermanentFailure("server", "SMTPConnectError"), None, id="saudacao-554"),
    # STARTTLS 4xx / 5xx: `SMTPResponseException`.
    pytest.param("starttls", lambda s: s.fail("starttls", 454), SmtpCommand("STARTTLS", False, 454),
                 TemporaryFailure("SMTPResponseException"), None, id="starttls-454"),
    pytest.param("starttls", lambda s: s.fail("starttls", 554), SmtpCommand("STARTTLS", False, 554),
                 PermanentFailure("server", "SMTPResponseException"), None, id="starttls-554"),
    # STARTTLS respondido com 250 no lugar do 220: caso de outro `SMTPResponseException`, código
    # inesperado → temporária com o nome do tipo.
    pytest.param("starttls", lambda s: s.fail("starttls", 250), SmtpCommand("STARTTLS", False, 250),
                 TemporaryFailure("SMTPResponseException"), None, id="inesperado-starttls-250"),
    # AUTH 5xx / 4xx (também depois do TLS implícito).
    pytest.param("tls", lambda s: s.fail("auth", 535), SmtpCommand("AUTH", True, 535),
                 PermanentFailure("auth", "SMTPAuthenticationError"), None, id="tls-auth-535"),
    pytest.param("tls", lambda s: s.fail("auth", 454), SmtpCommand("AUTH", True, 454),
                 TemporaryFailure("SMTPAuthenticationError"), None, id="tls-auth-454"),
    # AUTH respondido com 250 no lugar do 235: código inesperado → temporária.
    pytest.param("none", lambda s: s.fail("auth", 250), SmtpCommand("AUTH", False, 250),
                 TemporaryFailure("SMTPAuthenticationError"), None, id="inesperado-auth-250"),
    # MAIL: `SMTPSenderRefused` 4xx / 5xx.
    pytest.param("none", lambda s: s.fail("mail", 451), SmtpCommand("MAIL", False, 451),
                 TemporaryFailure("SMTPSenderRefused"), None, id="mail-451"),
    pytest.param("none", lambda s: s.fail("mail", 553), SmtpCommand("MAIL", False, 553),
                 PermanentFailure("server", "SMTPSenderRefused"), None, id="mail-553"),
    # RCPT: `SMTPRecipientsRefused` 4xx (inclusive 421) / 5xx.
    pytest.param("none", lambda s: s.fail("rcpt", 450), SmtpCommand("RCPT", False, 450),
                 TemporaryFailure("SMTPRecipientsRefused"), None, id="rcpt-450"),
    pytest.param("none", lambda s: s.fail("rcpt", 421), SmtpCommand("RCPT", False, 421),
                 TemporaryFailure("SMTPRecipientsRefused"), None, id="rcpt-421"),
    pytest.param("none", lambda s: s.fail("rcpt", 550), SmtpCommand("RCPT", False, 550),
                 PermanentFailure("recipient", "SMTPRecipientsRefused"), None, id="rcpt-550"),
    pytest.param("starttls", lambda s: s.fail("rcpt", 550), SmtpCommand("RCPT", True, 550),
                 PermanentFailure("recipient", "SMTPRecipientsRefused"), None, id="starttls-rcpt-550"),
    # DATA: `SMTPDataError` 4xx / 5xx, no DATA e no fim do DATA.
    pytest.param("none", lambda s: s.fail("data", 452), SmtpCommand("DATA", False, 452),
                 TemporaryFailure("SMTPDataError"), None, id="data-452"),
    pytest.param("none", lambda s: s.fail("data", 554), SmtpCommand("DATA", False, 554),
                 PermanentFailure("server", "SMTPDataError"), None, id="data-554"),
    pytest.param("none", lambda s: s.fail("data_end", 451), SmtpCommand("<data-end>", False, 451),
                 TemporaryFailure("SMTPDataError"), None, id="fim-do-data-451"),
    pytest.param("none", lambda s: s.fail("data_end", 554), SmtpCommand("<data-end>", False, 554),
                 PermanentFailure("server", "SMTPDataError"), None, id="fim-do-data-554"),
    # Código inesperado (−1, 2xx ou 3xx), um caso por etapa: temporária com o nome do tipo.
    pytest.param("none", lambda s: s.fail("connect", 554, raw=UNREADABLE),
                 SmtpCommand("<connect>", False, -1), TemporaryFailure("SMTPConnectError"), None,
                 id="inesperado-saudacao-ilegivel"),
    pytest.param("none", lambda s: s.fail("mail", 354), SmtpCommand("MAIL", False, 354),
                 TemporaryFailure("SMTPSenderRefused"), None, id="inesperado-mail-354"),
    pytest.param("none", lambda s: s.fail("rcpt", 550, raw=UNREADABLE), SmtpCommand("RCPT", False, -1),
                 TemporaryFailure("SMTPRecipientsRefused"), None, id="inesperado-rcpt-ilegivel"),
    pytest.param("none", lambda s: s.fail("data", 250), SmtpCommand("DATA", False, 250),
                 TemporaryFailure("SMTPDataError"), None, id="inesperado-data-250"),
    # Queda: `SMTPServerDisconnected` (o `smtplib` embrulha o erro de leitura no `getreply`); na abertura, o
    # tipo depende do sistema (`OPENING_DROP_TYPES`).
    pytest.param("none", lambda s: s.fail_network("connect", "reset"), SmtpCommand("<connect>", False, None),
                 OPENING_DROP, None, id="fora-do-ar-rst"),
    pytest.param("none", lambda s: s.fail_network("mail", "close"), SmtpCommand("MAIL", False, None),
                 TemporaryFailure("SMTPServerDisconnected"), None, id="queda-no-mail"),
    pytest.param("none", lambda s: s.fail_network("rcpt", "reset"), SmtpCommand("RCPT", False, None),
                 TemporaryFailure("SMTPServerDisconnected"), None, id="rst-no-rcpt"),
    pytest.param("tls", lambda s: s.fail_network("rcpt", "reset"), SmtpCommand("RCPT", True, None),
                 TemporaryFailure("SMTPServerDisconnected"), None, id="tls-rst-no-rcpt"),
    # Queda depois de o servidor aceitar o corpo (`after_accept`): temporária, mensagem capturada
    # sem confirmação (o aluno pode receber dois e-mails, e só o último vale).
    pytest.param("none", lambda s: s.fail_network("data_end", "close", after_accept=True),
                 SmtpCommand("<data-end>", False, None), TemporaryFailure("SMTPServerDisconnected"),
                 "sem-confirmacao", id="queda-depois-do-corpo-aceito"),
    pytest.param("none", lambda s: s.fail_network("data_end", "reset", after_accept=True),
                 SmtpCommand("<data-end>", False, None), TemporaryFailure("SMTPServerDisconnected"),
                 "sem-confirmacao", id="rst-depois-do-corpo-aceito"),
    # Erro no QUIT depois do 250 do fim do DATA: `Delivered`.
    pytest.param("none", lambda s: s.fail("quit", 500), SmtpCommand("QUIT", False, 500), Delivered, "entregue",
                 id="quit-500"),
    pytest.param("none", lambda s: s.fail_network("quit", "reset"), SmtpCommand("QUIT", False, None),
                 Delivered, "entregue", id="quit-rst"),
    pytest.param("none", lambda s: s.fail_network("quit", "close"), SmtpCommand("QUIT", False, None),
                 Delivered, "entregue", id="quit-fin"),
]


@pytest.mark.parametrize(("security", "arrange", "reached", "expected", "captured"), CLASSIFICATION)
def test_each_classification_row(smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, security: str,
                                 arrange: Callable[[FakeSmtp], None], reached: SmtpCommand, expected: Any,
                                 captured: str | None) -> None:
    """Uma linha da tabela única de classificação por caso:
    o código SMTP decide (4xx temporária, 5xx definitiva com a categoria da linha); −1, 2xx ou
    3xx inesperados são temporária com o nome do tipo, um caso por etapa; queda sem código é
    `SMTPServerDisconnected` (exato: o `smtplib` o embrulha no `getreply`), inclusive depois de o
    servidor aceitar o corpo, com a mensagem capturada sem confirmação (envio
    repetido por falha incerta); na abertura, antes da saudação, a queda é temporária com um dos
    `OPENING_DROP_TYPES`, porque o tipo depende do sistema; erro no QUIT depois do 250 do fim do DATA é
    `Delivered`. O resultado é exato (só o nome do tipo, nada do texto do servidor), a falha programada
    foi de fato alcançada e não há recuo para HELO."""
    _reset(smtp, clock, security=security)
    arrange(smtp)
    sent = _send(_channel(smtp_settings, smtp, security=security), _message())
    if expected is Delivered:
        assert type(sent.result) is Delivered
        _assert_message_ref(sent.result, ADDRESS_ASCII, CODE, LINK)
    elif expected is OPENING_DROP:
        assert type(sent.result) is TemporaryFailure
        assert sent.result.error_type in OPENING_DROP_TYPES
    else:
        assert sent.result == expected
        assert ERROR_TYPE.fullmatch(sent.result.error_type)
    conn = _only_connection(smtp)
    assert reached in conn.commands
    assert "HELO" not in conn.verbs
    messages = smtp.messages()
    if captured is None:
        assert messages == []
        assert "<data-end>" not in conn.verbs or reached.verb == "<data-end>"
    else:
        [message] = messages
        assert message.acknowledged is (captured == "entregue")
        assert (message.rcpt_to, message.text) == ((ADDRESS_ASCII,), BODY)
        if captured == "entregue":
            _assert_invite_message(message, sent, address_ascii=ADDRESS_ASCII, subject=SUBJECT, body=BODY)


@pytest.mark.parametrize(("security", "code", "on_tls", "expected"), [
    pytest.param("none", 421, False, TemporaryFailure("SMTPHeloError"), id="421"),
    pytest.param("none", 554, False, PermanentFailure("server", "SMTPHeloError"), id="554"),
    pytest.param("starttls", 421, True, TemporaryFailure("SMTPHeloError"), id="depois-do-starttls-421"),
    pytest.param("starttls", 554, True, PermanentFailure("server", "SMTPHeloError"),
                 id="depois-do-starttls-554"),
    pytest.param("tls", 554, True, PermanentFailure("server", "SMTPHeloError"), id="tls-554"),
    pytest.param("none", 220, False, TemporaryFailure("SMTPHeloError"), id="inesperado-220"),
])
def test_ehlo_refusal_is_classified_without_falling_back_to_helo(
        smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, security: str, code: int, on_tls: bool,
        expected: Any) -> None:
    """EHLO com resposta diferente de 250: o adaptador chama `ehlo()` explicitamente (na abertura e de
    novo depois do STARTTLS) e levanta `SMTPHeloError`, **sem** recuar para HELO (sem ESMTP não há
    STARTTLS nem AUTH): 4xx → `TemporaryFailure("SMTPHeloError")`, 5xx → `PermanentFailure("server",
    "SMTPHeloError")`, código inesperado (220) → temporária; nenhum HELO no `transcript()`, nenhum AUTH
    nem MAIL."""
    _reset(smtp, clock, security=security)
    smtp.fail("ehlo", code, when=lambda context: context.tls is on_tls)
    sent = _send(_channel(smtp_settings, smtp, security=security), _message())
    conn = _only_connection(smtp)
    assert "HELO" not in smtp.transcript().split()
    assert "HELO" not in conn.verbs
    assert SmtpCommand("EHLO", on_tls, code) in conn.commands
    assert sent.result == expected
    assert not {"AUTH", "MAIL", "RCPT", "DATA"} & set(conn.verbs)
    assert conn.verbs.count("EHLO") == 1 + (security == "starttls")
    assert smtp.messages() == []


@pytest.mark.parametrize(("changes", "expected"), [
    pytest.param({"body": "Olá, corpo com \udcff (surrogate isolado)\n"},
                 PermanentFailure("server", "UnicodeEncodeError"), id="corpo-com-surrogate"),
    pytest.param({"subject": "Assunto\r\nBcc: outro@example.org"}, PermanentFailure("server", "ValueError"),
                 id="assunto-com-quebra-de-linha"),
])
def test_assembly_errors_are_permanent_server_refusals(smtp: FakeSmtp, mail_channel: SmtpVerificationChannel,
                                                       changes: dict[str, str], expected: Any) -> None:
    """`UnicodeEncodeError` ou `ValueError` na montagem da mensagem → `PermanentFailure("server", <nome do
    tipo>)`; nenhuma transação é aberta (nenhum MAIL) e nada é entregue."""
    sent = _send(mail_channel, _message(**changes))
    assert sent.result == expected
    assert smtp.protocol_errors() == []
    assert not any("MAIL" in conn.verbs for conn in smtp.connections())
    assert smtp.messages() == []


def test_closed_port_is_a_temporary_connection_refused(smtp: FakeSmtp, smtp_settings: Any,
                                                       network_guard: Any) -> None:
    """O único caso de porta fechada (`closed_port()`), com `timeout_seconds=10` para que o prazo não
    vença antes da recusa (no Windows, a recusa em 127.0.0.1 leva cerca de 2 s):
    `TemporaryFailure("ConnectionRefusedError")` (rede é temporária; servidor fora do
    ar; na classificação de erros do adaptador, o caso do `ConnectionError`)."""
    port = smtp.closed_port()
    channel = _channel(smtp_settings, smtp, port=port, timeout_seconds=10)
    sent = _send(channel, _message())
    assert sent.result == TemporaryFailure("ConnectionRefusedError")
    assert sent.elapsed < 10
    assert _attempts(network_guard) == _one_connection_to(port)
    assert (smtp.connections(), smtp.messages()) == ([], [])


# ---------------------------------------------------------------------------------------------------
# 7. Prazo total
# ---------------------------------------------------------------------------------------------------

VERB_OF = {"connect": "<connect>", "ehlo": "EHLO", "starttls": "STARTTLS", "tls": "<tls>",
           "auth": "AUTH", "mail": "MAIL", "rcpt": "RCPT", "data": "DATA", "data_end": "<data-end>",
           "rset": "RSET", "quit": "QUIT"}

# (modo, etapa presa): toda etapa da saudação ao fim do DATA; `tls` só nos modos com TLS; depois do
# TLS, a saudação, o EHLO, o AUTH e o fim do DATA (o socket é outro: `SSLSocket`).
HANG_CASES = [
    *[("none", stage) for stage in ("connect", "ehlo", "auth", "mail", "rcpt", "data", "data_end")],
    *[("starttls", stage) for stage in ("starttls", "tls", "auth", "data_end")],
    *[("tls", stage) for stage in ("tls", "connect", "ehlo", "data_end")],
]


def _tls_at(security: str, stage: str) -> bool:
    """Se o comando da etapa presa corre sobre TLS (como o dublê o anota)."""
    if security == "none" or stage in ("starttls", "tls"):
        return False
    return True


@pytest.mark.parametrize(("security", "stage"), [pytest.param(*case, id="-".join(case)) for case in HANG_CASES])
def test_hang_at_each_stage_is_a_deadline_failure(smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any,
                                                  security: str, stage: str) -> None:
    """Servidor mudo numa etapa (`hang`): `TemporaryFailure("SmtpDeadline")` em no máximo T + 0,5 s
    reais, classificado pelo tempo (o prazo total venceu: pelo menos T − 0,1 s); no fim do DATA, a
    mensagem fica capturada sem confirmação (`after_accept`); a conexão termina (o adaptador
    a fecha) e nenhuma thread `cq-smtp-deadline` fica viva (tempo limite da conversa SMTP).
    No `tls`, o handshake preso termina pelo tempo do
    socket (o vigia não o cobre, uma limitação conhecida do prazo total), dentro do mesmo teto. Junto: a hierarquia
    de `SmtpDeadline` (`smtplib.SMTPServerDisconnected`), real no esqueleto."""
    assert SmtpDeadline.__bases__ == (smtplib.SMTPServerDisconnected,)
    assert issubclass(SmtpDeadline, smtplib.SMTPException)
    _reset(smtp, clock, security=security)
    smtp.fail_network(stage, "hang", after_accept=stage == "data_end")
    with _released_if_stuck(smtp):
        sent = _send(_channel(smtp_settings, smtp, security=security), _message())
    assert sent.result == TemporaryFailure("SmtpDeadline")
    assert FLOOR <= sent.elapsed <= CEILING, sent.elapsed
    conn = _only_connection(smtp)
    assert conn.commands[-1] == SmtpCommand(VERB_OF[stage], _tls_at(security, stage), None)
    assert conn.outcome == "abandoned"
    if stage == "data_end":
        [message] = smtp.messages()
        assert (message.acknowledged, message.rcpt_to, message.text) == (False, (ADDRESS_ASCII,), BODY)
    else:
        assert smtp.messages() == []


@pytest.mark.parametrize("stage", ["connect", "data_end"])
def test_drip_is_bounded_by_the_deadline(smtp: FakeSmtp, smtp_settings: Any, stage: str) -> None:
    """Resposta que goteja sem fim (`drip`: linhas de continuação válidas a cada 0,2 s; na saudação, 1
    byte por vez) na saudação e no fim do DATA: o mesmo resultado e o mesmo teto do servidor mudo,
    `TemporaryFailure("SmtpDeadline")` em no máximo T + 0,5 s (o
    tempo por operação do `smtplib` sozinho deixaria passar)."""
    smtp.fail_network(stage, "drip")
    with _released_if_stuck(smtp):
        sent = _send(_channel(smtp_settings, smtp), _message())
    assert sent.result == TemporaryFailure("SmtpDeadline")
    assert FLOOR <= sent.elapsed <= CEILING, sent.elapsed
    conn = _only_connection(smtp)
    assert (conn.commands[-1], conn.outcome) == (SmtpCommand(VERB_OF[stage], False, None), "abandoned")
    assert smtp.messages() == []


def test_hang_at_quit_after_delivery_is_delivered(smtp: FakeSmtp, smtp_settings: Any) -> None:
    """Servidor mudo no QUIT, depois do 250 do fim do DATA: `Delivered`, dentro do teto, com a mensagem
    confirmada (na classificação de erros do adaptador, o erro no QUIT depois da entrega, e o prazo)."""
    smtp.fail_network("quit", "hang")
    with _released_if_stuck(smtp):
        sent = _send(_channel(smtp_settings, smtp), _message())
    assert type(sent.result) is Delivered
    _assert_message_ref(sent.result, ADDRESS_ASCII, CODE, LINK)
    assert sent.elapsed <= CEILING, sent.elapsed
    [message] = smtp.messages()
    _assert_invite_message(message, sent, address_ascii=ADDRESS_ASCII, subject=SUBJECT, body=BODY)
    conn = _only_connection(smtp)
    assert conn.commands[-1] == SmtpCommand("QUIT", False, None)
    assert conn.commands[:-1] == _commands("none")[:-1]


@pytest.mark.parametrize(("stage", "code", "expected"), [
    pytest.param("mail", 451, TemporaryFailure("SMTPSenderRefused"), id="mail-451"),
    pytest.param("rcpt", 550, PermanentFailure("recipient", "SMTPRecipientsRefused"), id="rcpt-550"),
    pytest.param("data_end", 554, PermanentFailure("server", "SMTPDataError"), id="fim-do-data-554"),
])
def test_rset_hang_after_a_refusal_keeps_the_refusal_result(smtp: FakeSmtp, smtp_settings: Any, stage: str,
                                                            code: int, expected: Any) -> None:
    """O RSET só vem depois de uma recusa (o `smtplib` o manda antes de levantar a recusa); com o
    servidor mudo no RSET, o prazo total vence, e o resultado é o da recusa: o código SMTP vem
    primeiro, mesmo com o prazo vencido (a ordem da classificação),
    dentro do teto; o piso prova que o prazo venceu."""
    smtp.fail(stage, code)
    smtp.fail_network("rset", "hang")
    with _released_if_stuck(smtp):
        sent = _send(_channel(smtp_settings, smtp), _message())
    assert sent.result == expected
    assert FLOOR <= sent.elapsed <= CEILING, sent.elapsed
    conn = _only_connection(smtp)
    assert conn.commands[-2:] == (SmtpCommand(VERB_OF[stage], False, code), SmtpCommand("RSET", False, None))
    assert conn.outcome == "abandoned"
    assert smtp.messages() == []


# ---------------------------------------------------------------------------------------------------
# 8. Vazamentos
# ---------------------------------------------------------------------------------------------------

LEAK_ADDRESS_ASCII = "Leocadia.JURUBEBA@jequitiba.example.org"  # o `ascii_email` do aluno canário A
ECHO_TEXT = f"5.6.0 <{LEAK_ADDRESS_ASCII}>: Message rejected for {LEAK_ADDRESS_ASCII}"

# (modo, confiar na CA, configuração do dublê, programação, senha errada, resultado)
LEAK_SCENARIOS = [
    pytest.param("none", True, {}, None, False, Delivered, id="entregue"),
    pytest.param("none", True, {}, lambda s: s.fail("rcpt", 550), False,
                 PermanentFailure("recipient", "SMTPRecipientsRefused"), id="rcpt-550-com-eco"),
    pytest.param("none", True, {}, lambda s: s.fail("data_end", 554, ECHO_TEXT), False,
                 PermanentFailure("server", "SMTPDataError"), id="fim-do-data-554-com-eco"),
    pytest.param("none", True, {}, lambda s: s.fail("mail", 553, f"5.7.1 <{SENDER}> {LEAK_ADDRESS_ASCII}"),
                 False, PermanentFailure("server", "SMTPSenderRefused"), id="mail-553-com-eco"),
    pytest.param("none", True, {}, None, True, PermanentFailure("auth", "SMTPAuthenticationError"),
                 id="senha-errada-plain"),
    pytest.param("none", True, {"advertise_auth": ("LOGIN",)}, None, True,
                 PermanentFailure("auth", "SMTPAuthenticationError"), id="senha-errada-login"),
    pytest.param("starttls", False, {}, None, False, PermanentFailure("server", "SSLCertVerificationError"),
                 id="certificado"),
    pytest.param("none", True, {}, lambda s: s.fail_network("connect", "reset"), False,
                 OPENING_DROP, id="fora-do-ar"),
    pytest.param("none", True, {}, lambda s: s.fail_network("rcpt", "hang"), False,
                 TemporaryFailure("SmtpDeadline"), id="prazo-no-rcpt"),
]


@pytest.mark.parametrize(("security", "trust", "config", "arrange", "wrong", "expected"), LEAK_SCENARIOS)
def test_nothing_leaks_to_fds_logs_results_or_the_channel(
        smtp: FakeSmtp, clock: FakeClock, smtp_settings: Any, capfd: pytest.CaptureFixture[str],
        security: str, trust: bool, config: dict[str, Any], arrange: Callable[[FakeSmtp], None] | None,
        wrong: bool, expected: Any) -> None:
    """Vazamentos (erro do servidor de e-mail com o endereço na
    mensagem): com o e-mail do aluno canário A como destinatário e o servidor ecoando o
    endereço nas recusas, depois do envio: stdout e stderr vazios (nunca `set_debuglevel`); nenhum log
    com o endereço, o código do convite, a senha ou as suas formas no fio (PLAIN e LOGIN, com a senha
    errada nas duas trocas); nenhum atributo do resultado com a exceção do `smtplib` ou o seu texto (o
    resultado tem só os campos da porta, textos, sem nada do servidor); o canal não guarda exceção nem
    o endereço, o código ou o texto do servidor em atributo, e o `repr` e o `str` do canal não têm a
    senha (o `repr` é real no esqueleto). Controles positivos de cada detector."""
    _reset(smtp, clock, security=security, **config)
    if arrange is not None:
        arrange(smtp)
    assert smtp.credentials is not None
    username = smtp.credentials.username
    password = _wrong_password() if wrong else smtp.credentials.password
    known = list(dict.fromkeys(mail_secrets(smtp) + _wire_forms(username, password)))
    digests = [invite_code_digest(CODE)]
    server_texts = ["Recipient address rejected", "Message rejected", "User unknown", "credentials invalid",
                    "5.1.1", "5.6.0", "5.7.1", "5.7.8", "fake-smtp.invalid", "UGFzc3dvcmQ6", "VXNlcm5hbWU6"]
    channel = _channel(smtp_settings, smtp, trust=trust, security=security, password=password)
    capfd.readouterr()
    with capture_logs() as logs, _released_if_stuck(smtp):
        sent = _send(channel, _message(STUDENT_A.email))
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    if expected is Delivered:
        assert type(sent.result) is Delivered
    elif expected is OPENING_DROP:
        assert type(sent.result) is TemporaryFailure
        assert sent.result.error_type in OPENING_DROP_TYPES
    else:
        assert sent.result == expected
    # O resultado: só os campos da porta, textos, sem nada do envio.
    fields = {f.name: getattr(sent.result, f.name) for f in dataclasses.fields(sent.result)}
    assert vars(sent.result) == fields
    assert all(type(value) is str for value in fields.values())
    # O canal: nenhuma exceção guardada; `repr`/`str` sem a senha; nenhum atributo com o endereço, o
    # código ou o texto do servidor.
    assert not any(isinstance(value, BaseException) for value in vars(channel).values())
    outputs = [logs.text, repr(sent.result), *fields.values(), repr(channel), str(channel)]
    for text in [*outputs, repr(vars(channel))]:
        assert find_canary(text, prefixes=STUDENT_PREFIXES) == []
        assert find_invite_codes(text, digests) == []
        assert BOT_USERNAME not in text
        assert [marker for marker in server_texts if marker in text] == []
    for text in outputs:
        assert find_secrets(text, known) == []
    # Controles positivos: os detectores acham o que procuram.
    assert find_canary(LEAK_ADDRESS_ASCII, prefixes=STUDENT_PREFIXES) == STUDENT_A_EMAIL_LABELS
    for secret in known:
        assert f"segredo:{secret[:6]}…" in find_secrets(f"<{secret}>", known)
    assert find_invite_codes(LINK, digests) == [invite_code_label(digests[0])]
    assert [marker for marker in server_texts if marker in ECHO_TEXT + " User unknown"] == [
        "Message rejected", "User unknown", "5.6.0"]
    if expected is Delivered:
        [message] = smtp.messages()
        assert message.rcpt_to == (LEAK_ADDRESS_ASCII,)
        assert find_invite_codes(message.text or "", digests) == [invite_code_label(digests[0])]
