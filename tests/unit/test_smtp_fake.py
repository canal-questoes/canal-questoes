"""Autoteste do SMTP falso (verde desde o início).

O SMTP falso (`tests/helpers/smtp.py`) é o oráculo de todos os testes que mandam e-mail. Se ele
errar o dot-stuffing, a troca AUTH, o STARTTLS, a verificação do certificado, as falhas programadas
ou as regras estritas, os testes do adaptador e do envio passam no vazio; se ele escrever em stdout
ou stderr, os segredos vazam nas varreduras. Estes testes usam o `smtplib` como cliente (e um
cliente cru, para os bytes que o `smtplib` recusa mandar), conferem o próprio arnês e passam antes
da implementação por natureza.
"""

from __future__ import annotations

import ast
import base64
import email.policy
import pathlib
import re
import smtplib
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from typing import Any

import pytest

from tests.helpers import smtp as smtp_module
from tests.helpers.clock import START, FakeClock
from tests.helpers.secrets import SECRETS
from tests.helpers.smtp import (
    CERT_KINDS,
    HOSTNAME,
    NETWORK_KINDS,
    SECURITY_MODES,
    SENDER,
    STAGES,
    AuthAttempt,
    CapturedMessage,
    FakeSmtp,
    InviteLink,
    SmtpCommand,
    SmtpConnection,
    SmtpCredentials,
    StageContext,
    session_smtp,
)

HELPER = pathlib.Path(__file__).resolve().parents[1] / "helpers" / "smtp.py"
LOCAL = "cq.example.org"
RCPT = "Aluno.Teste@Example.ORG"
OTHER_RCPT = "outra.pessoa@example.com"
POLICY = email.policy.default.clone(max_line_length=76)
CODE = "AbCdEfGhIjKlMnOpQrStU-"  # 22 caracteres base64url
LINK = f"https://t.me/cq_teste_bot?start={CODE}"


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server() -> Iterator[FakeSmtp]:
    fake = FakeSmtp()
    yield fake
    fake.close()


@pytest.fixture
def fake(server: FakeSmtp, clock: FakeClock) -> Iterator[FakeSmtp]:
    """O dublê do módulo, limpo a cada teste, como a fixture `smtp` faz."""
    server.reset(clock=clock)
    yield server
    server.reset()


def _smtp(fake: FakeSmtp, *, timeout: float = 5.0) -> smtplib.SMTP:
    return smtplib.SMTP(fake.host, fake.port, local_hostname=LOCAL, timeout=timeout)


def _smtp_ssl(fake: FakeSmtp, context: ssl.SSLContext | None = None, *,
              timeout: float = 5.0) -> smtplib.SMTP_SSL:
    return smtplib.SMTP_SSL(fake.host, fake.port, local_hostname=LOCAL, timeout=timeout,
                            context=context if context is not None else fake.client_context())


def _login(client: smtplib.SMTP, fake: FakeSmtp) -> None:
    assert fake.credentials is not None
    client.user, client.password = fake.credentials
    code, _ = client.auth("PLAIN", client.auth_plain)
    assert code == 235


def _plain(fake: FakeSmtp) -> str:
    assert fake.credentials is not None
    user, password = fake.credentials
    return base64.b64encode(f"\0{user}\0{password}".encode()).decode()


def _email(body: str = "Olá!\nSegunda linha.\n", *, subject: str = "Aviso de acentuação",
           to: str = RCPT, policy: Any = POLICY) -> EmailMessage:
    msg = EmailMessage(policy=policy)
    msg["From"] = f'"Canal de Questões" <{SENDER}>'
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Wed, 01 Jan 2031 00:00:00 +0000"
    msg["Message-ID"] = "<id-1@cq.example.org>"
    msg.set_content(body, cte="quoted-printable")
    return msg


def _ended(fake: FakeSmtp, index: int = -1) -> SmtpConnection:
    """Espera a conexão `index` terminar e devolve o registro."""

    def done() -> SmtpConnection | None:
        conns = fake.connections()
        if conns and conns[index].outcome != "open":
            return conns[index]
        return None

    return fake.wait_for(done)


def _last_verb(fake: FakeSmtp, index: int = -1) -> str:
    """O último verbo registrado na conexão `index` (vazio se ela ainda não existe ou não tem
    comandos)."""
    conns = fake.connections()
    try:
        return conns[index].verbs[-1]
    except IndexError:
        return ""


def _deliver(fake: FakeSmtp, msg: EmailMessage | None = None) -> None:
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        client.send_message(msg if msg is not None else _email())


class _Raw:
    """Cliente cru, para os bytes que o `smtplib` recusa mandar e para ler as respostas exatas."""

    def __init__(self, fake: FakeSmtp, *, timeout: float = 5.0, greet: bool = True) -> None:
        self.sock = socket.create_connection((fake.host, fake.port), timeout=timeout)
        self.buffer = b""
        self.greeting = self.reply() if greet else []

    def _line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("o dublê fechou a conexão")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\r\n")
        return line

    def reply(self) -> list[bytes]:
        lines = []
        while True:
            line = self._line()
            lines.append(line)
            if line[3:4] != b"-":
                return lines

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def cmd(self, text: str) -> list[bytes]:
        self.send(text.encode("utf-8") + b"\r\n")
        return self.reply()

    def close(self) -> None:
        self.sock.close()


def _watchdog(fake: FakeSmtp, client: smtplib.SMTP, delay: float) -> Callable[[], None]:
    """Derruba o socket do cliente depois de `delay` s, como o vigia do prazo do adaptador
    (`socket.socket.shutdown`); por segurança, solta as conexões presas do dublê 2 s depois.
    Devolve a função que cancela os dois."""

    def down() -> None:
        try:
            socket.socket.shutdown(client.sock, socket.SHUT_RDWR)
        except (OSError, AttributeError, TypeError):
            pass

    timers = [threading.Timer(delay, down), threading.Timer(delay + 2.0, fake.release_hangs)]
    for timer in timers:
        timer.daemon = True
        timer.start()

    def cancel() -> None:
        for timer in timers:
            timer.cancel()

    return cancel


# ---------------------------------------------------------------------------------------------
# Estrutura
# ---------------------------------------------------------------------------------------------


def test_helper_uses_only_the_standard_library_cryptography_and_the_secret_registry() -> None:
    """Só biblioteca padrão, `cryptography` e o registro de segredos dos testes; nada de
    `cq_server` (o dublê tem de ser independente do adaptador que ele confere)."""
    source = HELPER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    outside = {name for name in imported
               if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"}
    assert {name.split(".")[0] for name in outside} == {"cryptography", "tests"}
    assert {name for name in outside if name.startswith("tests")} == {"tests.helpers.secrets"}
    assert not any(name.startswith("cq_") for name in imported)
    # Remetente e domínios em example.* (nunca .test, que o email-validator recusa).
    assert re.search(r"[A-Za-z0-9-]\.test\b", source) is None
    assert SENDER == "convites@cq.example.org"
    assert STAGES == ("connect", "ehlo", "starttls", "tls", "auth", "mail", "rcpt", "data",
                      "data_end", "rset", "quit")
    assert NETWORK_KINDS == ("reset", "close", "hang", "drip")
    assert SECURITY_MODES == ("none", "starttls", "tls")
    assert CERT_KINDS == ("valid", "wrong_host", "expired", "self_signed")


def test_listens_on_loopback_with_an_os_chosen_port(fake: FakeSmtp) -> None:
    assert fake.host == "127.0.0.1"
    assert 1024 <= fake.port <= 65535
    raw = _Raw(fake)
    assert raw.greeting == [f"220 {HOSTNAME} ESMTP".encode()]
    assert raw.cmd("QUIT") == [b"221 2.0.0 Bye"]
    raw.close()
    assert _ended(fake).outcome == "quit"


@pytest.mark.parametrize(("config", "before_tls", "after_tls"), [
    ({"security": "none"}, ["AUTH PLAIN LOGIN"], None),
    ({"security": "none", "credentials": None}, [], None),
    ({"security": "none", "advertise_auth": ("LOGIN",)}, ["AUTH LOGIN"], None),
    ({"security": "none", "advertise_auth": ()}, [], None),
    ({"security": "starttls"}, ["STARTTLS"], ["AUTH PLAIN LOGIN"]),
    ({"security": "starttls", "advertise_starttls": False}, [], None),
    ({"security": "starttls", "advertise_auth": ("CRAM-MD5", "LOGIN")}, ["STARTTLS"],
     ["AUTH CRAM-MD5 LOGIN"]),
    ({"security": "tls"}, None, ["AUTH PLAIN LOGIN"]),
])
def test_ehlo_advertises_the_subset_and_never_smtputf8(fake: FakeSmtp, config: dict[str, Any],
                                                        before_tls: list[str] | None,
                                                        after_tls: list[str] | None) -> None:
    """EHLO multilinha com SIZE, 8BITMIME e ENHANCEDSTATUSCODES; STARTTLS só antes do TLS no modo
    `starttls`; AUTH só com TLS ativo ou no modo `none`; nunca SMTPUTF8, PIPELINING, CHUNKING nem
    DSN."""
    fake.reset(**config)
    base = [HOSTNAME, "SIZE 10485760", "8BITMIME", "ENHANCEDSTATUSCODES"]

    def lines(client: smtplib.SMTP) -> list[str]:
        code, text = client.ehlo()
        assert code == 250
        return text.decode().split("\n")

    if config["security"] == "tls":
        client: smtplib.SMTP = _smtp_ssl(fake)
    else:
        client = _smtp(fake)
    with client:
        seen = lines(client)
        if before_tls is not None:
            assert seen == base + before_tls
        if after_tls is not None:
            if config["security"] == "starttls":
                client.starttls(context=fake.client_context())
                seen = lines(client)
            assert seen == base + after_tls
    for text in seen:
        assert not re.search(r"SMTPUTF8|PIPELINING|CHUNKING|DSN", text)
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Entrega e captura
# ---------------------------------------------------------------------------------------------


def test_delivery_captures_envelope_headers_decoded_text_and_raw(fake: FakeSmtp,
                                                                   clock: FakeClock) -> None:
    clock.advance(90)
    msg = _email(f"Olá, çãõ!\nO link: {LINK}\n", subject="Seu link para o oferecimento Cálculo "
                 "(2026s2)")
    before = smtp_module.monotonic()
    _deliver(fake, msg)
    after = smtp_module.monotonic()
    [captured] = fake.messages()
    assert isinstance(captured, CapturedMessage)
    assert (captured.seq, captured.conn_seq) == (1, 1)
    assert captured.mail_from == SENDER
    assert captured.rcpt_to == (RCPT,)  # a caixa recebida
    assert len(captured.mail_params) == 1 and re.fullmatch(r"size=[0-9]+", captured.mail_params[0])
    assert (captured.tls, captured.tls_version, captured.authenticated) == (False, None, True)
    assert captured.acknowledged is True
    assert captured.clock_at == START.replace(minute=1, second=30)
    assert before <= captured.started <= captured.ended <= after
    assert captured.headers == (
        ("From", f"Canal de Questões <{SENDER}>"),
        ("To", RCPT),
        ("Subject", "Seu link para o oferecimento Cálculo (2026s2)"),
        ("Date", "Wed, 01 Jan 2031 00:00:00 +0000"),
        ("Message-ID", "<id-1@cq.example.org>"),
        ("Content-Type", 'text/plain; charset="utf-8"'),
        ("Content-Transfer-Encoding", "quoted-printable"),
        ("MIME-Version", "1.0"),
    )
    assert captured.header("subject") == "Seu link para o oferecimento Cálculo (2026s2)"
    assert captured.header("X-Ausente") is None
    assert captured.header_names()[0] == "From"
    assert captured.text == f"Olá, çãõ!\nO link: {LINK}\n"
    # O fio leva QP em 7 bits: o link não aparece literal no `raw` (o `=` vira `=3D`), só no texto.
    assert captured.raw.isascii() and LINK not in captured.raw.decode("ascii")
    assert captured.raw == msg.as_bytes(policy=POLICY.clone(linesep="\r\n"))
    assert captured.message["Subject"] == "Seu link para o oferecimento Cálculo (2026s2)"
    [conn] = fake.connections()
    assert conn.verbs == ("<connect>", "EHLO", "AUTH", "MAIL", "RCPT", "DATA", "<data-end>",
                          "QUIT")
    assert [c.code for c in conn.commands] == [220, 250, 235, 250, 250, 354, 250, 221]
    assert (conn.seq, conn.ehlo_name, conn.tls, conn.quit, conn.outcome) == (1, LOCAL, False,
                                                                             True, "quit")
    assert conn.clock_at == START.replace(minute=1, second=30)
    assert conn.auth == (AuthAttempt("PLAIN", True, True, True, False, 235),)
    assert fake.protocol_errors() == []


def test_queued_reply_names_the_message(fake: FakeSmtp) -> None:
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        for index in (1, 2):
            client.mail(SENDER)
            client.rcpt(RCPT)
            assert client.data(b"Subject: x\r\n\r\ncorpo\r\n") == (
                250, f"2.0.0 Ok: queued as Q{index}".encode())
    assert [m.seq for m in fake.messages()] == [1, 2]
    assert fake.protocol_errors() == []


def test_dot_stuffing_is_undone(fake: FakeSmtp) -> None:
    body = b"Subject: pontos\r\n\r\n.linha\r\n.\r\n..dois\r\nfim.\r\n"
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        client.sendmail(SENDER, [RCPT], body)
    [captured] = fake.messages()
    assert captured.raw == body
    assert captured.text == ".linha\n.\n..dois\nfim.\n"
    assert fake.protocol_errors() == []


def test_message_without_text_part_has_text_none(fake: FakeSmtp) -> None:
    body = (b"Subject: html\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n"
            b"\r\n<p>oi</p>\r\n")
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        client.sendmail(SENDER, [RCPT], body)
    assert fake.messages()[0].text is None
    assert fake.protocol_errors() == []


def test_invite_links_extracts_the_codes_and_remembers_them(fake: FakeSmtp) -> None:
    other = "ZyXwVuTsRqPoNmLkJiHgF_"
    _deliver(fake, _email(f"Link: {LINK}\nDe novo: {LINK}\nOutro: https://t.me/outro_bot?start="
                          f"{other}\nCurto: https://t.me/x_bot?start=abc\n"))
    _deliver(fake, _email(f"Só este: {LINK}\n", to=OTHER_RCPT))
    SECRETS.clear()
    links = fake.invite_links()
    assert links == [
        InviteLink(message_seq=1, rcpt_to=(RCPT,), url=LINK, bot_username="cq_teste_bot",
                   code=CODE, acknowledged=True),
        InviteLink(message_seq=1, rcpt_to=(RCPT,), url=f"https://t.me/outro_bot?start={other}",
                   bot_username="outro_bot", code=other, acknowledged=True),
        InviteLink(message_seq=2, rcpt_to=(OTHER_RCPT,), url=LINK, bot_username="cq_teste_bot",
                   code=CODE, acknowledged=True),
    ]
    assert SECRETS == [CODE, other]
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("mechanism", "initial"), [("PLAIN", True), ("PLAIN", False),
                                                     ("LOGIN", True), ("LOGIN", False)])
def test_auth_plain_and_login_with_and_without_initial_response(fake: FakeSmtp, mechanism: str,
                                                                initial: bool) -> None:
    """As quatro formas passam e não são acusadas (os argumentos da troca AUTH ficam fora da busca
    de segredos)."""
    assert fake.credentials is not None
    with _smtp(fake) as client:
        client.ehlo()
        client.user, client.password = fake.credentials
        method = client.auth_plain if mechanism == "PLAIN" else client.auth_login
        assert client.auth(mechanism, method, initial_response_ok=initial) == (
            235, b"2.7.0 Authentication successful")
        client.sendmail(SENDER, [RCPT], b"Subject: x\r\n\r\ncorpo\r\n")
    [conn] = fake.connections()
    responses = {("PLAIN", True): 0, ("PLAIN", False): 1, ("LOGIN", True): 1,
                 ("LOGIN", False): 2}[(mechanism, initial)]
    assert conn.verbs[:3 + responses] == ("<connect>", "EHLO", "AUTH",
                                          *["<auth-response>"] * responses)
    assert [c.code for c in conn.commands[2:3 + responses]] == [334] * responses + [235]
    assert conn.auth == (AuthAttempt(mechanism, initial, True, True, False, 235),)
    assert fake.messages()[0].authenticated is True
    assert fake.protocol_errors() == []


def test_login_challenges_and_empty_plain_challenge_are_the_documented_ones(
        fake: FakeSmtp) -> None:
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    assert raw.cmd("AUTH LOGIN") == [b"334 VXNlcm5hbWU6"]
    assert raw.cmd("*") == [b"501 5.0.0 Authentication cancelled"]
    assert raw.cmd("AUTH PLAIN") == [b"334 "]
    assert raw.cmd("*") == [b"501 5.0.0 Authentication cancelled"]
    assert raw.cmd("QUIT") == [b"221 2.0.0 Bye"]
    raw.close()
    conn = _ended(fake)
    assert conn.auth == (AuthAttempt("LOGIN", False, False, False, False, 501),
                         AuthAttempt("PLAIN", False, False, False, False, 501))
    assert fake.protocol_errors() == []  # `*` é o cancelamento legítimo (RFC 4954, seção 4)


def test_auth_wrong_credentials_give_535(fake: FakeSmtp) -> None:
    assert fake.credentials is not None
    user, password = fake.credentials
    with _smtp(fake) as client:
        client.ehlo()
        client.user, client.password = user, password + "x"
        with pytest.raises(smtplib.SMTPAuthenticationError) as info:
            client.auth("PLAIN", client.auth_plain)
        assert (info.value.smtp_code, info.value.smtp_error) == (
            535, b"5.7.8 Authentication credentials invalid")
        client.user, client.password = "outra-pessoa", password
        with pytest.raises(smtplib.SMTPAuthenticationError):
            client.auth("LOGIN", client.auth_login, initial_response_ok=False)
        other = base64.b64encode(f"alguem\0{user}\0{password}".encode()).decode()
        assert client.docmd("AUTH", f"PLAIN {other}")[0] == 535  # authzid diferente do authcid
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}")[0] == 235
    conn = _ended(fake)
    assert conn.auth == (AuthAttempt("PLAIN", True, True, False, False, 535),
                         AuthAttempt("LOGIN", False, False, True, False, 535),
                         AuthAttempt("PLAIN", True, True, True, False, 535),
                         AuthAttempt("PLAIN", True, True, True, False, 235))
    assert fake.protocol_errors() == []


def test_auth_protocol_errors_501_503_504_and_530(fake: FakeSmtp) -> None:
    """`*` (501, legítimo), base64 inválido e PLAIN malformado (501), mecanismo não anunciado ou
    não implementado (504), AUTH antes do EHLO, repetido ou durante a transação (503)."""
    fake.reset(advertise_auth=("CRAM-MD5", "PLAIN"))
    with _smtp(fake) as client:
        assert client.docmd("AUTH", "PLAIN")[0] == 503  # antes do EHLO
        client.ehlo()
        assert client.docmd("AUTH", "PLAIN !!!") == (501, b"5.5.2 Cannot decode response")
        malformed = base64.b64encode(b"sem-nul").decode()
        assert client.docmd("AUTH", f"PLAIN {malformed}") == (501, b"5.5.2 Invalid PLAIN message")
        assert client.docmd("AUTH", "LOGIN") == (504, b"5.5.4 Unrecognized authentication type")
        assert client.docmd("AUTH", "CRAM-MD5")[0] == 504
        assert client.docmd("AUTH", "PLAIN = extra")[0] == 501
        assert client.docmd("AUTH", "PLAIN =") == (501, b"5.5.2 Invalid PLAIN message")
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}")[0] == 235
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}") == (
            503, b"5.5.1 Error: already authenticated")
        client.mail(SENDER)
    with _smtp(fake) as client:
        client.ehlo()
        client.docmd("AUTH", f"PLAIN {_plain(fake)}")
        client.mail(SENDER)
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}")[0] == 503  # durante a transação
    assert fake.protocol_errors() == [
        "#1 AUTH: comando fora de ordem: antes do EHLO",
        "#1 AUTH: base64 inválido na troca AUTH",
        "#1 AUTH: mensagem PLAIN malformada",
        "#1 AUTH: AUTH com mecanismo não anunciado ou não implementado",
        "#1 AUTH: AUTH com mecanismo não anunciado ou não implementado",
        "#1 AUTH: AUTH com sintaxe inválida",
        "#1 AUTH: mensagem PLAIN malformada",
        "#1 AUTH: comando fora de ordem: AUTH depois de um AUTH aceito",
        "#2 AUTH: comando fora de ordem: AUTH durante a transação",
    ]


def test_auth_before_tls_is_530_and_unadvertised_auth_is_502(fake: FakeSmtp) -> None:
    fake.reset(security="starttls")
    with _smtp(fake) as client:
        client.ehlo()
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}") == (
            530, b"5.7.0 Must issue a STARTTLS command first")
    assert fake.protocol_errors() == ["#1 AUTH: AUTH sem TLS fora do modo none"]
    fake.reset(credentials=None)
    with _smtp(fake) as client:
        client.ehlo()
        assert client.docmd("AUTH", "PLAIN =")[0] == 502
        assert client.sendmail(SENDER, [RCPT], b"Subject: x\r\n\r\nsem AUTH\r\n") == {}
    assert fake.messages()[0].authenticated is False
    assert fake.protocol_errors() == ["#1 AUTH: AUTH não anunciado"]


def test_mail_without_auth_is_530_when_there_are_credentials(fake: FakeSmtp) -> None:
    with _smtp(fake) as client:
        client.ehlo()
        assert client.mail(SENDER) == (530, b"5.7.0 Authentication required")
    assert fake.protocol_errors() == [
        "#1 MAIL: comando fora de ordem: MAIL sem AUTH havendo credenciais"]


# ---------------------------------------------------------------------------------------------
# STARTTLS e TLS implícito
# ---------------------------------------------------------------------------------------------


def test_starttls_returns_to_the_initial_state(fake: FakeSmtp) -> None:
    """Depois do handshake a sessão volta ao estado inicial: qualquer comando sem novo EHLO é fora
    de ordem; o novo EHLO anuncia AUTH e não anuncia STARTTLS (RFC 3207, seção 4.2)."""
    fake.reset(security="starttls")
    with _smtp(fake) as client:
        client.ehlo()
        assert client.starttls(context=fake.client_context()) == (
            220, b"2.0.0 Ready to start TLS")
        assert client.docmd("MAIL", f"FROM:<{SENDER}>") == (503, b"5.5.1 Error: send EHLO first")
        assert client.docmd("NOOP")[0] == 503
        client.ehlo()
        assert "starttls" not in client.esmtp_features
        assert client.docmd("STARTTLS") == (503, b"5.5.1 TLS already active")
        _login(client, fake)
        client.send_message(_email())
    conn = _ended(fake)
    assert conn.verbs == ("<connect>", "EHLO", "STARTTLS", "<tls>", "MAIL", "NOOP", "EHLO",
                          "STARTTLS", "AUTH", "MAIL", "RCPT", "DATA", "<data-end>", "QUIT")
    assert [c.tls for c in conn.commands] == [False] * 3 + [True] * 11
    assert conn.tls is True and conn.tls_version in ("TLSv1.2", "TLSv1.3")
    assert conn.auth == (AuthAttempt("PLAIN", True, True, True, True, 235),)
    [captured] = fake.messages()
    assert (captured.tls, captured.tls_version) == (True, conn.tls_version)
    assert fake.protocol_errors() == [
        "#1 MAIL: comando fora de ordem: depois do STARTTLS sem novo EHLO",
        "#1 NOOP: comando fora de ordem: depois do STARTTLS sem novo EHLO",
        "#1 STARTTLS: STARTTLS com TLS já ativo",
    ]


def test_starttls_misuse_is_reported(fake: FakeSmtp) -> None:
    fake.reset(security="starttls")
    with _smtp(fake) as client:
        assert client.docmd("STARTTLS")[0] == 503  # antes do EHLO
        client.ehlo()
        assert client.docmd("STARTTLS", "agora") == (501, b"5.5.4 Syntax: STARTTLS")
    assert fake.protocol_errors() == ["#1 STARTTLS: comando fora de ordem: antes do EHLO",
                                      "#1 STARTTLS: STARTTLS com argumento"]
    fake.reset(security="starttls", advertise_starttls=False)
    with _smtp(fake) as client:
        client.ehlo()
        assert client.docmd("STARTTLS") == (502, b"5.5.1 Command not implemented")
    assert fake.protocol_errors() == ["#1 STARTTLS: STARTTLS não anunciado"]
    fake.reset(security="none")
    with _smtp(fake) as client:
        client.ehlo()
        with pytest.raises(smtplib.SMTPNotSupportedError):
            client.starttls(context=fake.client_context())
        assert client.docmd("STARTTLS")[0] == 502
    assert fake.protocol_errors() == ["#1 STARTTLS: STARTTLS não anunciado"]


def test_plaintext_bytes_after_starttls_are_a_protocol_error(fake: FakeSmtp) -> None:
    fake.reset(security="starttls")
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    raw.send(b"STARTTLS\r\nNOOP\r\n")
    assert raw.reply() == [b"220 2.0.0 Ready to start TLS"]
    raw.close()
    conn = _ended(fake)
    assert (conn.outcome, conn.tls_alert, conn.tls) == ("tls_failed", None, False)
    assert fake.protocol_errors() == ["#1 STARTTLS: bytes em claro depois do STARTTLS"]


def test_implicit_tls_delivers_with_the_test_ca(fake: FakeSmtp) -> None:
    fake.reset(security="tls")
    with _smtp_ssl(fake) as client:
        client.ehlo()
        _login(client, fake)
        client.send_message(_email())
    conn = _ended(fake)
    assert conn.verbs[:3] == ("<tls>", "<connect>", "EHLO")
    assert all(c.tls for c in conn.commands[1:])
    assert conn.tls_version in ("TLSv1.2", "TLSv1.3")
    assert fake.messages()[0].tls is True
    assert fake.protocol_errors() == []


def test_ca_pem_and_client_context(fake: FakeSmtp) -> None:
    assert fake.ca_pem.startswith("-----BEGIN CERTIFICATE-----\n")
    assert fake.ca_pem.rstrip().endswith("-----END CERTIFICATE-----")
    context = fake.client_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context is not fake.client_context()
    assert [c["subject"] for c in context.get_ca_certs()] == [((("commonName", "CQ Test CA"),),)]


@pytest.mark.parametrize("security", ["starttls", "tls"])
@pytest.mark.parametrize(("cert", "trust", "message", "alert"), [
    ("valid", "none", r"unable to get local issuer certificate", "TLSV1_ALERT_UNKNOWN_CA"),
    ("wrong_host", "test", r"IP address mismatch", "SSLV3_ALERT_BAD_CERTIFICATE"),
    ("expired", "test", r"certificate has expired", "SSLV3_ALERT_CERTIFICATE_EXPIRED"),
    ("self_signed", "test", r"self.signed certificate", "TLSV1_ALERT_UNKNOWN_CA"),
])
def test_a_verifying_client_refuses_bad_certificates(fake: FakeSmtp, security: str, cert: str,
                                                     trust: str, message: str,
                                                     alert: str) -> None:
    """O cliente que verifica recusa o certificado, o dublê anota o alerta, nada depois do
    handshake chega, e o fechamento sem QUIT não é acusado."""
    fake.reset(security=security, cert=cert)
    context = (fake.client_context() if trust == "test"
               else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    with pytest.raises(ssl.SSLCertVerificationError) as info:
        if security == "tls":
            _smtp_ssl(fake, context)
        else:
            client = _smtp(fake)
            client.ehlo()
            try:
                client.starttls(context=context)
            finally:
                client.close()
    assert re.search(message, info.value.verify_message)
    conn = _ended(fake)
    assert (conn.outcome, conn.tls_alert, conn.tls, conn.auth) == ("tls_failed", alert, False, ())
    assert conn.verbs[-1] == "<tls>"
    assert not {"AUTH", "MAIL", "RCPT", "DATA"} & set(conn.verbs)
    assert fake.messages() == []
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Regras estritas
# ---------------------------------------------------------------------------------------------


def test_transaction_order_and_mail_rcpt_data_syntax(fake: FakeSmtp) -> None:
    with _smtp(fake) as client:
        assert client.docmd("MAIL", f"FROM:<{SENDER}>") == (503, b"5.5.1 Error: send EHLO first")
        client.ehlo()
        _login(client, fake)
        assert client.docmd("RCPT", f"TO:<{RCPT}>") == (503, b"5.5.1 Error: need MAIL command")
        assert client.docmd("DATA")[0] == 503
        assert client.docmd("MAIL", "FROM:sem-colchetes@example.org")[0] == 501
        assert client.docmd("MAIL", f"FROM:<{SENDER}> SIZE=abc")[0] == 501
        assert client.docmd("MAIL", f"FROM:<{SENDER}> SIZE=10485761") == (
            552, b"5.3.4 Message size exceeds fixed maximum message size")
        assert client.docmd("MAIL", f"FROM:<{SENDER}> BODY=BINARYMIME")[0] == 501
        assert client.docmd("MAIL", f"FROM:<{SENDER}> RET=HDRS")[0] == 555
        assert client.docmd("MAIL", f"FROM:<{SENDER}> BODY=7BIT SIZE=10")[0] == 250
        assert client.docmd("MAIL", f"FROM:<{SENDER}>") == (503,
                                                             b"5.5.1 Error: nested MAIL command")
        assert client.docmd("DATA")[0] == 503
        assert client.docmd("RCPT", "TO:<>")[0] == 501
        assert client.docmd("RCPT", f"TO:<{RCPT}> NOTIFY=NEVER")[0] == 555
        assert client.docmd("RCPT", f"TO:<{RCPT}>")[0] == 250
        assert client.docmd("DATA", "agora")[0] == 501
        assert client.docmd("RSET", "tudo")[0] == 501
        assert client.docmd("RSET") == (250, b"2.0.0 Ok")
        assert client.docmd("QUIT", "agora")[0] == 501
    assert fake.protocol_errors() == [
        "#1 MAIL: comando fora de ordem: antes do EHLO",
        "#1 RCPT: comando fora de ordem: RCPT sem MAIL",
        "#1 DATA: comando fora de ordem: DATA sem RCPT aceito",
        "#1 MAIL: MAIL com sintaxe inválida",
        "#1 MAIL: SIZE malformado",
        "#1 MAIL: BODY com valor desconhecido",
        "#1 MAIL: parâmetro desconhecido no MAIL",
        "#1 MAIL: comando fora de ordem: MAIL com transação aberta",
        "#1 DATA: comando fora de ordem: DATA sem RCPT aceito",
        "#1 RCPT: RCPT com sintaxe inválida",
        "#1 RCPT: parâmetro no RCPT",
        "#1 DATA: DATA com argumento",
        "#1 RSET: RSET com argumento",
        "#1 QUIT: QUIT com argumento",
    ]


def test_smtputf8_in_mail_is_a_protocol_error(fake: FakeSmtp) -> None:
    """O dublê nunca anuncia SMTPUTF8, e o parâmetro no MAIL é erro de protocolo."""
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        assert not client.has_extn("smtputf8")
        assert client.docmd("MAIL", f"FROM:<{SENDER}> SMTPUTF8") == (
            555, b"5.5.4 Unsupported option: SMTPUTF8")
        assert client.docmd("MAIL", f"FROM:<{SENDER}> BODY=8BITMIME")[0] == 250
    assert fake.protocol_errors() == [
        "#1 MAIL: parâmetro SMTPUTF8 no MAIL (o dublê não anuncia SMTPUTF8)"]


def test_verbs_outside_the_subset_and_helo(fake: FakeSmtp) -> None:
    fake.reset(credentials=None)
    with _smtp(fake) as client:
        assert client.docmd("NOOP") == (250, b"2.0.0 Ok")
        assert client.docmd("RSET") == (250, b"2.0.0 Ok")
        assert client.docmd("VRFY", "alguem") == (502, b"5.5.1 Command not implemented")
        assert client.docmd("XYZZY") == (500, b"5.5.2 Command unrecognized")
        assert client.docmd("EHLO") == (501, b"5.5.4 Syntax: EHLO hostname")
        assert client.docmd("HELO")[0] == 501
        assert client.docmd("HELO", LOCAL) == (250, HOSTNAME.encode())
        assert client.docmd("MAIL", f"FROM:<{SENDER}> SIZE=10")[0] == 555  # HELO: sem ESMTP
    assert [c.verb for c in _ended(fake).commands][1:5] == ["NOOP", "RSET", "VRFY", "?"]
    assert fake.protocol_errors() == [
        "#1 VRFY: verbo fora do subconjunto",
        "#1 ?: verbo desconhecido",
        "#1 EHLO: EHLO sem domínio",
        "#1 HELO: HELO sem domínio",
        "#1 MAIL: parâmetro no MAIL sem EHLO",
    ]


def test_command_line_limits_bare_cr_lf_and_8bit(fake: FakeSmtp) -> None:
    raw = _Raw(fake)
    assert raw.cmd("NOOP " + "x" * 505) == [b"250 2.0.0 Ok"]  # 512 octetos com o CRLF
    assert raw.cmd("NOOP " + "x" * 506) == [b"500 5.5.6 Line too long"]
    raw.send(b"NOOP\n")
    assert raw.reply() == [b"250 2.0.0 Ok"]
    raw.send(b"NO\rOP x\r\n")
    assert raw.reply()[0].startswith(b"500")
    raw.send(b"NOOP \xc3\xa1\r\n")
    assert raw.reply() == [b"250 2.0.0 Ok"]
    raw.send(b"X" * 13000 + b"\r\n")
    assert raw.reply() == [b"500 5.5.6 Line too long"]
    assert raw.cmd("QUIT") == [b"221 2.0.0 Bye"]
    raw.close()
    assert fake.protocol_errors() == [
        "#1 NOOP: linha de comando com mais de 512 octetos",
        "#1 NOOP: LF solto no fim da linha de comando",
        "#1 ?: CR solto na linha de comando",
        "#1 ?: verbo desconhecido",
        "#1 NOOP: byte acima de 0x7F na linha de comando",
        "#1 ?: linha de comando com mais de 12288 octetos",
    ]


def test_data_rules_text_lines_8bit_bare_lf_and_quoted_printable(fake: FakeSmtp) -> None:
    ok_1000 = b"a" * 998 + b"\r\n"
    ok_dot = b"." + b"a" * 997 + b"\r\n"  # 1000 octetos; no fio, 1001 com o ponto dobrado
    long_line = b"b" * 999 + b"\r\n"
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        client.sendmail(SENDER, [RCPT], b"Subject: 1\r\n\r\n" + ok_1000)  # 1000 com o CRLF
        client.sendmail(SENDER, [RCPT], b"Subject: 2\r\n\r\n" + ok_dot)
        client.sendmail(SENDER, [RCPT], b"Subject: 3\r\n\r\n" + long_line)
        client.sendmail(SENDER, [RCPT], "Subject: 4\r\n\r\ncafé\r\n".encode())
        client.sendmail(SENDER, [RCPT], "Subject: 5\r\n\r\ncafé\r\n".encode(),
                        mail_options=["BODY=8BITMIME"])
        client.sendmail(SENDER, [RCPT], b"Subject: 6\r\n\r\nlinha\nsolta\r\nfim\rcr\r\n")
        long_text = "é" + "a" * 300 + "\n"
        client.send_message(_email(long_text, policy=email.policy.default))  # QP em 78
        client.send_message(_email(long_text))  # QP em 76
    assert len(fake.messages()) == 8
    assert fake.messages()[1].raw == b"Subject: 2\r\n\r\n" + ok_dot
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    raw.cmd(f"AUTH PLAIN {_plain(fake)}")
    raw.cmd(f"MAIL FROM:<{SENDER}>")
    raw.cmd(f"RCPT TO:<{RCPT}>")
    assert raw.cmd("DATA")[0].startswith(b"354")
    raw.send(b"Subject: 9\r\n\r\n.sem ponto dobrado\r\n.\r\n")
    assert raw.reply() == [b"250 2.0.0 Ok: queued as Q9"]
    raw.cmd("QUIT")
    raw.close()
    assert fake.messages()[-1].raw == b"Subject: 9\r\n\r\nsem ponto dobrado\r\n"
    assert fake.protocol_errors() == [
        "#1 <data>: linha de texto com mais de 1000 octetos",
        "#1 <data>: byte acima de 0x7F no DATA sem BODY=8BITMIME",
        "#1 <data>: LF solto no DATA",
        "#1 <data>: CR solto no DATA",
        "#1 <data>: linha em quoted-printable com mais de 76 caracteres",
        "#2 <data>: linha que começa com ponto sem o ponto dobrado",
    ]


def test_secret_in_command_arguments_is_flagged_but_not_the_auth_exchange(
        fake: FakeSmtp) -> None:
    """A senha e as formas do fio num argumento de comando (EHLO, MAIL, RCPT, NOOP, verbo) são
    acusadas; a troca AUTH legítima (PLAIN com resposta inicial e as linhas do LOGIN) não.
    Os textos dos erros, a transcrição e os registros nunca trazem o segredo."""
    assert fake.credentials is not None
    user, password = fake.credentials
    password_b64 = base64.b64encode(password.encode()).decode()
    assert password_b64 in fake.credentials.secret_forms()
    for mechanism, initial in (("PLAIN", True), ("LOGIN", False), ("LOGIN", True)):
        with _smtp(fake) as client:
            client.ehlo()
            client.user, client.password = user, password
            method = client.auth_plain if mechanism == "PLAIN" else client.auth_login
            client.auth(mechanism, method, initial_response_ok=initial)
    assert fake.protocol_errors() == []
    with _smtp(fake) as client:
        client.ehlo(_plain(fake))
        _login(client, fake)
        assert client.docmd("MAIL", f"FROM:<{password}@example.org>")[0] == 250
        assert client.docmd("RCPT", f"TO:<x{password_b64}@example.org>")[0] == 250
        client.docmd("NOOP", password)
        client.docmd(password)
    problems = fake.protocol_errors()
    assert problems == [
        "#4 EHLO: segredo conhecido no argumento do comando",
        "#4 MAIL: segredo conhecido no argumento do comando",
        "#4 RCPT: segredo conhecido no argumento do comando",
        "#4 NOOP: segredo conhecido no argumento do comando",
        "#4 ?: segredo conhecido no argumento do comando",
        "#4 ?: verbo desconhecido",
    ]
    texts = [repr(problems), fake.transcript(), repr(fake.connections()), repr(fake.credentials)]
    for text in texts:
        for form in fake.credentials.secret_forms():
            assert form not in text
    assert fake.connections()[-1].ehlo_name == "[segredo]"


def test_credentials_are_drawn_per_reset_exposed_and_never_remembered(fake: FakeSmtp) -> None:
    first = fake.credentials
    assert isinstance(first, SmtpCredentials)
    assert re.fullmatch(r"cq-smtp-[0-9a-f]{8}", first.username)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", first.password)
    user, password = first
    assert first.secret_forms() == (
        password,
        base64.b64encode(f"\0{user}\0{password}".encode()).decode(),
        base64.b64encode(f"{user}\0{user}\0{password}".encode()).decode(),
        base64.b64encode(password.encode()).decode(),
    )
    assert repr(first) == f"SmtpCredentials(username={user!r}, password='[oculta]')"
    assert password not in repr(first) and password not in str(first)
    fake.reset()
    assert fake.credentials != first and fake.credentials is not None
    fake.reset(credentials=("usuario-fixo", "senha fixa com espaco"))
    assert fake.credentials == SmtpCredentials("usuario-fixo", "senha fixa com espaco")
    _deliver(fake)
    fake.reset(credentials=None)
    assert fake.credentials is None
    assert SECRETS == []  # sem remember_secret para as credenciais


def test_closing_without_quit_is_flagged_after_2xx_or_3xx(fake: FakeSmtp) -> None:
    with _smtp(fake) as client:
        client.ehlo()
    client = _smtp(fake)
    client.ehlo()
    client.close()
    assert _ended(fake).outcome == "client_closed"
    client = _smtp(fake)
    client.ehlo()
    _login(client, fake)
    client.mail(SENDER)
    client.rcpt(RCPT)
    client.docmd("DATA")
    client.close()  # desiste no meio do DATA (última resposta 354)
    _ended(fake)
    client = _smtp(fake)
    client.ehlo()
    client.docmd("AUTH", "PLAIN =")  # 501: a última resposta não é 2xx nem 3xx
    client.close()
    _ended(fake)
    assert fake.protocol_errors() == [
        "#2: conexão encerrada pelo cliente sem QUIT (última resposta 250)",
        "#3: conexão encerrada pelo cliente sem QUIT (última resposta 354)",
        "#4 AUTH: mensagem PLAIN malformada",
    ]


def test_closing_without_quit_is_not_flagged_with_alert_failed_handshake_or_deadline(
        fake: FakeSmtp, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.reset(security="starttls")
    # tls_alert: o cliente recusa o certificado depois do 220 do STARTTLS.
    client = _smtp(fake)
    client.ehlo()
    with pytest.raises(ssl.SSLCertVerificationError):
        client.starttls(context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    client.close()
    assert _ended(fake).tls_alert == "TLSV1_ALERT_UNKNOWN_CA"
    # Handshake falho sem alerta: o cliente some depois do 220.
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    assert raw.cmd("STARTTLS") == [b"220 2.0.0 Ready to start TLS"]
    raw.close()
    conn = _ended(fake)
    assert (conn.outcome, conn.tls_alert) == ("tls_failed", None)
    # Handshake falho com lixo em claro no lugar do ClientHello.
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    raw.cmd("STARTTLS")
    raw.send(b"EHLO em claro\r\n")
    raw.close()
    assert _ended(fake).outcome == "tls_failed"
    # Prazo do cliente: ele desiste de uma resposta presa.
    fake.reset(security="none")
    fake.fail_network("mail", "hang")
    client = _smtp(fake, timeout=0.3)
    client.ehlo()
    _login(client, fake)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        client.mail(SENDER)
    client.close()
    assert _ended(fake).outcome == "abandoned"
    # Prazo do dublê: ele responde 421 e fecha.
    monkeypatch.setattr(smtp_module, "IDLE_TIMEOUT_SECONDS", 0.3)
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    assert raw.reply() == [f"421 4.4.2 {HOSTNAME} Error: timeout exceeded".encode()]
    raw.close()
    conn = _ended(fake)
    assert (conn.outcome, conn.verbs[-1]) == ("idle_timeout", "<timeout>")
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Falhas programadas
# ---------------------------------------------------------------------------------------------

# Etapa → (modo, código 4yz, texto padrão, código 5yz, texto padrão).
SMTP_FAILURES: dict[str, tuple[str, int, str, int, str]] = {
    "connect": ("none", 450, "4.3.2 Service temporarily unavailable", 554,
                "5.3.2 No SMTP service here"),
    "ehlo": ("none", 451, "4.3.2 EHLO temporarily rejected", 554, "5.5.1 EHLO rejected"),
    "starttls": ("starttls", 454, "4.7.0 TLS not available due to temporary reason", 554,
                 "5.7.0 TLS not available"),
    "auth": ("none", 454, "4.7.0 Temporary authentication failure", 535,
             "5.7.8 Authentication credentials invalid"),
    "mail": ("none", 451, "4.3.0 Sender temporarily rejected", 553,
             "5.7.1 Sender address rejected"),
    "rcpt": ("none", 450, "4.2.1 Recipient temporarily unavailable", 550,
             f"5.1.1 <{RCPT}>: Recipient address rejected: User unknown"),
    "data": ("none", 451, "4.3.0 DATA temporarily rejected", 554, "5.5.0 DATA rejected"),
    "data_end": ("none", 451, "4.3.0 Message temporarily deferred", 554, "5.6.0 Message rejected"),
    "rset": ("none", 451, "4.3.0 RSET temporarily failed", 501, "5.5.0 RSET failed"),
    "quit": ("none", 451, "4.3.0 QUIT temporarily failed", 502, "5.5.0 QUIT failed"),
}
VERB_OF = {"connect": "<connect>", "ehlo": "EHLO", "starttls": "STARTTLS", "tls": "<tls>",
           "auth": "AUTH", "mail": "MAIL", "rcpt": "RCPT", "data": "DATA",
           "data_end": "<data-end>", "rset": "RSET", "quit": "QUIT"}
BODY = b"Subject: x\r\n\r\ncorpo\r\n"


def _step(client: smtplib.SMTP, fake: FakeSmtp, stage: str) -> tuple[int, bytes]:
    """Executa o comando da etapa (depois de `_reach`) e devolve a resposta."""
    if stage == "ehlo":
        return client.docmd("EHLO", LOCAL)
    if stage == "starttls":
        return client.docmd("STARTTLS")
    if stage == "auth":
        return client.docmd("AUTH", f"PLAIN {_plain(fake)}")
    if stage == "mail":
        return client.docmd("MAIL", f"FROM:<{SENDER}>")
    if stage == "rcpt":
        return client.docmd("RCPT", f"TO:<{RCPT}>")
    if stage == "data":
        return client.docmd("DATA")
    if stage == "data_end":
        client.send(BODY + b".\r\n")
        return client.getreply()
    if stage == "rset":
        return client.docmd("RSET")
    assert stage == "quit"
    return client.docmd("QUIT")


def _reach(fake: FakeSmtp, stage: str, *, timeout: float = 5.0) -> smtplib.SMTP:
    """Conecta e avança até logo antes do comando de `stage`."""
    client = _smtp(fake, timeout=timeout)
    if stage == "ehlo":
        return client
    client.ehlo()
    if stage in ("starttls", "tls"):
        return client
    if fake.security == "starttls":
        client.starttls(context=fake.client_context())
        client.ehlo()
    if stage == "auth":
        return client
    _login(client, fake)
    if stage in ("mail", "rset", "quit"):
        return client
    assert client.docmd("MAIL", f"FROM:<{SENDER}>")[0] == 250
    if stage == "rcpt":
        return client
    assert client.docmd("RCPT", f"TO:<{RCPT}>")[0] == 250
    if stage == "data":
        return client
    assert client.docmd("DATA")[0] == 354
    return client


@pytest.mark.parametrize("klass", [4, 5])
@pytest.mark.parametrize("stage", [s for s in STAGES if s != "tls"])
def test_fail_answers_the_code_with_the_default_text_and_has_no_effect(
        fake: FakeSmtp, stage: str, klass: int) -> None:
    """A resposta programada substitui a normal (texto padrão com código estendido; o 550 do RCPT
    repete o endereço); o comando não tem efeito, e a repetição passa; nada é acusado."""
    mode, code4, text4, code5, text5 = SMTP_FAILURES[stage]
    code, text = (code4, text4) if klass == 4 else (code5, text5)
    fake.reset(security=mode)
    fake.fail(stage, code)
    if stage == "connect":
        with pytest.raises(smtplib.SMTPConnectError) as info:
            _smtp(fake)
        assert (info.value.smtp_code, info.value.smtp_error) == (code, text.encode())
        raw = _Raw(fake)
        assert raw.greeting == [b"220 fake-smtp.invalid ESMTP"]  # times=1: a próxima é normal
        assert raw.cmd("QUIT") == [b"221 2.0.0 Bye"]
        raw.close()
        first = _ended(fake, 0)
        assert first.commands == (SmtpCommand("<connect>", False, code),)
        assert first.outcome == "client_closed"  # o smtplib fecha sem QUIT; falha programada
    else:
        client = _reach(fake, stage)
        assert _step(client, fake, stage) == (code, text.encode())
        # Sem efeito: a repetição passa (sem 503 de transação ou AUTH repetidos).
        expected = {"ehlo": 250, "starttls": 220, "auth": 235, "mail": 250, "rcpt": 250,
                    "data": 354, "rset": 250, "quit": 221}
        if stage == "data_end":
            assert fake.messages() == []
            assert client.docmd("MAIL", f"FROM:<{SENDER}>")[0] == 250
            client.quit()
        elif stage == "starttls":
            assert _step(client, fake, stage)[0] == 220
            client.close()
            assert _ended(fake).outcome == "tls_failed"
        else:
            assert _step(client, fake, stage)[0] == expected[stage]
            if stage == "data":
                assert _step(client, fake, "data_end")[0] == 250
            if stage == "rcpt":
                assert client.docmd("DATA")[0] == 354
                assert _step(client, fake, "data_end")[0] == 250
                assert fake.messages()[0].rcpt_to == (RCPT,)
            if stage != "quit":
                client.quit()
            else:
                client.close()
        conn = _ended(fake)
        assert SmtpCommand(VERB_OF[stage], False, code) in conn.commands
    assert fake.protocol_errors() == []


def test_other_codes_at_connect_keep_the_session_answering_503_except_quit(
        fake: FakeSmtp) -> None:
    fake.fail("connect", 554, "5.3.2 Sem serviço\nsegunda linha")
    raw = _Raw(fake, greet=False)
    assert raw.reply() == [b"554-5.3.2 Sem servi\xc3\xa7o", b"554 segunda linha"]
    assert raw.cmd(f"EHLO {LOCAL}") == [b"503 5.5.1 No SMTP service here"]
    assert raw.cmd(f"MAIL FROM:<{SENDER}>") == [b"503 5.5.1 No SMTP service here"]
    assert raw.cmd("QUIT") == [b"221 2.0.0 Bye"]
    raw.close()
    assert _ended(fake).outcome == "quit"
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("stage", [s for s in STAGES if s != "tls"])
def test_421_answers_and_always_closes_the_connection(fake: FakeSmtp, stage: str) -> None:
    fake.reset(security=SMTP_FAILURES[stage][0])
    fake.fail(stage, 421)
    text = b"4.3.2 fake-smtp.invalid Service not available, closing transmission channel"
    if stage == "connect":
        with pytest.raises(smtplib.SMTPConnectError) as info:
            _smtp(fake)
        assert (info.value.smtp_code, info.value.smtp_error) == (421, text)
    else:
        client = _reach(fake, stage)
        assert _step(client, fake, stage) == (421, text)
        with pytest.raises(smtplib.SMTPServerDisconnected):
            client.docmd("NOOP")
    conn = _ended(fake)
    assert (conn.outcome, conn.commands[-1]) == ("closed", SmtpCommand(VERB_OF[stage], False, 421))
    assert fake.protocol_errors() == []


def test_raw_reply_is_read_by_smtplib_as_minus_one(fake: FakeSmtp) -> None:
    """`raw` manda a linha literal; o `smtplib` lê o código −1, e o registro também."""
    fake.fail("connect", 450, raw="XYZ ilegível")
    with pytest.raises(smtplib.SMTPConnectError) as info:
        _smtp(fake)
    assert (info.value.smtp_code, info.value.smtp_error) == (-1, "ilegível".encode())
    assert _ended(fake).commands == (SmtpCommand("<connect>", False, -1),)
    fake.fail("rcpt", 550, raw="XYZ ilegível")
    fake.fail("mail", 550, raw="250 parece sucesso")
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        assert client.mail(SENDER) == (250, b"parece sucesso")
        with pytest.raises(smtplib.SMTPRecipientsRefused) as refused:
            client.sendmail(SENDER, [RCPT], BODY)
        assert refused.value.recipients == {RCPT: (-1, "ilegível".encode())}
    conn = _ended(fake)
    assert SmtpCommand("RCPT", False, -1) in conn.commands
    assert fake.protocol_errors() == []


def test_unexpected_2xx_and_3xx_codes(fake: FakeSmtp) -> None:
    """Códigos inesperados: MAIL respondido com 354 e DATA com 250 (sem entrar no
    modo de dados)."""
    fake.fail("mail", 354)
    fake.fail("data", 250)
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        with pytest.raises(smtplib.SMTPSenderRefused) as sender:
            client.sendmail(SENDER, [RCPT], BODY)
        assert sender.value.smtp_code == 354
        with pytest.raises(smtplib.SMTPDataError) as data:
            client.sendmail(SENDER, [RCPT], BODY)
        assert (data.value.smtp_code, data.value.smtp_error) == (
            250, b"2.0.0 Unexpected reply from the fake")
    assert fake.messages() == []
    assert _ended(fake).outcome == "quit"
    assert fake.protocol_errors() == []


def test_rcpt_550_default_text_repeats_the_address_on_purpose(fake: FakeSmtp) -> None:
    fake.fail("rcpt", 550)
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        with pytest.raises(smtplib.SMTPRecipientsRefused) as info:
            client.sendmail(SENDER, [RCPT], BODY)
    assert RCPT in str(info.value)  # a armadilha para quem registrar str(SMTPRecipientsRefused)
    assert RCPT not in fake.transcript() and RCPT not in repr(fake.connections())
    assert fake.protocol_errors() == []


NETWORK_CASES = [(stage, kind) for stage in STAGES for kind in NETWORK_KINDS
                 if not (kind == "drip" and stage in ("tls", "connect"))]


@pytest.mark.parametrize(("stage", "kind"), NETWORK_CASES)
def test_fail_network_per_stage(fake: FakeSmtp, stage: str, kind: str) -> None:
    """`reset` (RST), `close` (FIN), `hang` (até o cliente desistir) e `drip` (continuações sem
    fim, até o vigia do cliente derrubar o socket): o cliente vê a falha, o desfecho fica
    registrado e nada é acusado."""
    mode = "starttls" if stage in ("starttls", "tls") else "none"
    fake.reset(security=mode)
    fake.fail_network(stage, kind, interval=0.05)
    timeout = 0.3
    started = time.perf_counter()
    if stage == "connect":
        with pytest.raises(OSError):
            _smtp(fake, timeout=timeout)
    else:
        client = _reach(fake, stage, timeout=timeout if kind == "hang" else 5.0)
        cancel = _watchdog(fake, client, 0.3) if kind == "drip" else None
        try:
            with pytest.raises(OSError):
                if stage == "tls":
                    client.starttls(context=fake.client_context())
                else:
                    _step(client, fake, stage)
        finally:
            if cancel is not None:
                cancel()
            client.close()
    conn = _ended(fake)
    outcome = {"reset": "reset", "close": "closed", "hang": "abandoned", "drip": "abandoned"}[kind]
    assert conn.outcome == outcome
    assert conn.commands[-1] == SmtpCommand(VERB_OF[stage], False, None)
    assert conn.ended is not None and conn.ended - conn.started < 3.0
    assert time.perf_counter() - started < 3.0
    assert fake.messages() == []
    assert fake.protocol_errors() == []


def test_implicit_tls_network_failures_in_the_handshake(fake: FakeSmtp) -> None:
    fake.reset(security="tls")
    for kind, outcome in (("reset", "reset"), ("close", "closed"), ("hang", "abandoned")):
        fake.fail_network("tls", kind)
        with pytest.raises(OSError):
            _smtp_ssl(fake, timeout=0.3)
        conn = _ended(fake)
        assert (conn.outcome, conn.commands) == (outcome, (SmtpCommand("<tls>", False, None),))
    fake.fail_network("connect", "reset")
    with pytest.raises(OSError):
        _smtp_ssl(fake, timeout=0.3)
    conn = _ended(fake)
    assert (conn.outcome, conn.verbs, conn.tls) == ("reset", ("<tls>", "<connect>"), True)
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_fail_at_the_tls_stage_answers_in_clear_and_closes(fake: FakeSmtp, security: str) -> None:
    """Na etapa `tls`, a resposta programada vai em claro no lugar do handshake: o cliente vê uma
    falha de TLS, e o dublê fecha a conexão."""
    fake.reset(security=security)
    fake.fail("tls", 454)
    with pytest.raises(OSError):
        if security == "tls":
            _smtp_ssl(fake, timeout=2)
        else:
            client = _smtp(fake, timeout=2)
            client.ehlo()
            try:
                client.starttls(context=fake.client_context())
            finally:
                client.close()
    conn = _ended(fake)
    assert (conn.outcome, conn.commands[-1], conn.tls) == (
        "closed", SmtpCommand("<tls>", False, 454), False)
    assert fake.protocol_errors() == []


def test_drip_at_connect_sends_one_byte_at_a_time(fake: FakeSmtp) -> None:
    fake.fail_network("connect", "drip", interval=0.05)
    sock = socket.create_connection((fake.host, fake.port), timeout=2)
    chunks: list[bytes] = []
    started = time.perf_counter()
    while sum(map(len, chunks)) < 10:
        chunks.append(sock.recv(100))
    elapsed = time.perf_counter() - started
    sock.close()
    assert b"".join(chunks)[:10] == b"220-fake-s"
    # Um byte por vez: só um atraso do próprio teste junta dois bytes num `recv`.
    assert max(map(len, chunks)) <= 2 and len(chunks) >= 7
    assert elapsed >= 0.35  # 9 intervalos de 0,05 s, com folga
    conn = _ended(fake)
    assert (conn.outcome, conn.commands) == ("abandoned", (SmtpCommand("<connect>", False, None),))
    assert fake.protocol_errors() == []


def test_drip_elsewhere_sends_valid_continuation_lines(fake: FakeSmtp) -> None:
    fake.fail_network("mail", "drip", interval=0.02)
    raw = _Raw(fake)
    raw.cmd(f"EHLO {LOCAL}")
    raw.cmd(f"AUTH PLAIN {_plain(fake)}")
    raw.send(f"MAIL FROM:<{SENDER}>\r\n".encode())
    lines = [raw._line() for _ in range(3)]
    raw.close()
    assert lines == [f"250-{HOSTNAME} drip {n}".encode() for n in (1, 2, 3)]
    assert _ended(fake).outcome == "abandoned"
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("kind", NETWORK_KINDS)
def test_after_accept_captures_the_message_unacknowledged(fake: FakeSmtp, kind: str) -> None:
    fake.fail_network("data_end", kind, after_accept=True, interval=0.05)
    client = _reach(fake, "data_end", timeout=0.3 if kind == "hang" else 5.0)
    cancel = _watchdog(fake, client, 0.3) if kind == "drip" else None
    try:
        with pytest.raises(OSError):
            _step(client, fake, "data_end")
    finally:
        if cancel is not None:
            cancel()
        client.close()
    [captured] = fake.messages()
    assert (captured.acknowledged, captured.raw, captured.rcpt_to) == (False, BODY, (RCPT,))
    conn = _ended(fake)
    assert conn.commands[-1] == SmtpCommand("<data-end>", False, None)
    fake.fail_network("data_end", kind, interval=0.05)  # sem after_accept: nada capturado
    client = _reach(fake, "data_end", timeout=0.3 if kind == "hang" else 5.0)
    cancel = _watchdog(fake, client, 0.3) if kind == "drip" else None
    try:
        with pytest.raises(OSError):
            _step(client, fake, "data_end")
    finally:
        if cancel is not None:
            cancel()
        client.close()
    _ended(fake)
    assert len(fake.messages()) == 1
    assert fake.protocol_errors() == []


def test_after_accept_link_is_extracted_with_acknowledged_false(fake: FakeSmtp) -> None:
    fake.fail_network("data_end", "reset", after_accept=True)
    with pytest.raises(OSError):
        _deliver(fake, _email(f"link: {LINK}\n"))
    _deliver(fake, _email(f"link novo: {LINK[:-1]}X\n"))
    assert [(link.code, link.acknowledged) for link in fake.invite_links()] == [
        (CODE, False), (CODE[:-1] + "X", True)]


def test_times_when_and_clear_failures(fake: FakeSmtp) -> None:
    seen: list[StageContext] = []

    def only_other(context: StageContext) -> bool:
        seen.append(context)
        return context.argument == OTHER_RCPT

    fake.fail("rcpt", 550, when=only_other, times=None)
    fake.fail("mail", 451, times=2)
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        assert client.mail(SENDER)[0] == 451
        assert client.mail(SENDER)[0] == 451
        assert client.mail(SENDER)[0] == 250
        assert client.rcpt(RCPT)[0] == 250
        assert client.rcpt(OTHER_RCPT)[0] == 550
        assert client.rcpt(OTHER_RCPT)[0] == 550
        fake.clear_failures()
        assert client.rcpt(OTHER_RCPT)[0] == 250
    assert seen[0] == StageContext(stage="rcpt", conn_seq=1, argument=RCPT, tls=False,
                                   authenticated=True, mail_from=SENDER, rcpt_to=())
    assert seen[1].rcpt_to == (RCPT,)
    fake.fail("ehlo", 554, when=lambda context: 1 / 0)
    fake.fail("auth", 454, when=lambda context: context.argument == "PLAIN")
    with _smtp(fake) as client:
        assert client.ehlo()[0] == 250  # predicado que falhou não aplica a falha
        assert client.docmd("AUTH", f"PLAIN {_plain(fake)}")[0] == 454
    assert fake.protocol_errors() == [
        "#2: predicado when falhou na etapa ehlo: ZeroDivisionError: division by zero"]


def test_release_hangs_closes_the_hung_connections_and_new_ones_hang_again(
        fake: FakeSmtp) -> None:
    fake.fail_network("mail", "hang", times=None)
    box: dict[str, Any] = {}

    def talk() -> None:
        with _smtp(fake) as client:
            client.ehlo()
            _login(client, fake)
            try:
                client.mail(SENDER)
            except OSError as exc:
                box["error"] = exc

    thread = threading.Thread(target=talk)
    thread.start()
    fake.wait_for(lambda: _last_verb(fake, 0) == "MAIL")
    released = time.perf_counter()
    fake.release_hangs()
    thread.join(3)
    assert isinstance(box["error"], smtplib.SMTPServerDisconnected)
    assert time.perf_counter() - released < 1.0
    assert _ended(fake, 0).outcome == "closed"
    with _smtp(fake, timeout=0.3) as client:
        client.ehlo()
        _login(client, fake)
        with pytest.raises(smtplib.SMTPServerDisconnected):
            client.mail(SENDER)
    assert _ended(fake, 1).outcome == "abandoned"
    assert fake.protocol_errors() == []


def test_fail_argument_validation(fake: FakeSmtp) -> None:
    for args, kwargs in [
        (("helo", 550), {}), (("mail", 199), {}), (("mail", 600), {}),
        (("mail", True), {}), (("mail", "550"), {}), (("mail", 550, "a\rb"), {}),
        (("mail", 550), {"raw": "a\nb"}), (("mail", 550, "texto"), {"raw": "550 x"}),
        (("mail", 550), {"times": 0}), (("mail", 550), {"when": "sim"}),
    ]:
        with pytest.raises(ValueError):
            fake.fail(*args, **kwargs)
    for args, kwargs in [
        (("helo", "reset"), {}), (("mail", "timeout"), {}), (("tls", "drip"), {}),
        (("mail", "reset"), {"after_accept": True}), (("mail", "drip"), {"interval": 0}),
        (("mail", "hang"), {"times": -1}),
    ]:
        with pytest.raises(ValueError):
            fake.fail_network(*args, **kwargs)
    for kwargs in [{"security": "ssl"}, {"cert": "revoked"}, {"advertise_auth": "PLAIN"},
                   {"advertise_auth": ("XOAUTH2",)}, {"credentials": ("sem-senha",)},
                   {"credentials": ("u", "")}, {"advertise_starttls": "sim"}]:
        with pytest.raises(ValueError):
            fake.reset(**kwargs)
    with _smtp(fake) as client:  # nada foi programado
        client.ehlo()
        _login(client, fake)
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Observação e ciclo de vida
# ---------------------------------------------------------------------------------------------


def test_reset_forgets_everything_and_ends_old_connections(fake: FakeSmtp,
                                                           clock: FakeClock) -> None:
    fake.fail("mail", 451, times=None)
    old = _Raw(fake)
    old.cmd("XYZZY")
    hung = _Raw(fake)
    fake.fail_network("ehlo", "hang")
    hung.send(f"EHLO {LOCAL}\r\n".encode())
    fake.wait_for(lambda: _last_verb(fake, 1) == "EHLO")
    assert fake.protocol_errors() == ["#1 ?: verbo desconhecido"]
    other = FakeClock(START.replace(year=2032))
    fake.reset(clock=other)
    assert (fake.connections(), fake.messages(), fake.protocol_errors()) == ([], [], [])
    with pytest.raises((EOFError, OSError)):
        hung.reply()  # a conexão presa foi fechada sem resposta
    with pytest.raises((EOFError, OSError)):
        old.cmd("NOOP")  # a conexão antiga também
    hung.close()
    old.close()
    _deliver(fake)  # a falha programada antes do reset não vale mais
    assert fake.messages()[0].clock_at == START.replace(year=2032)
    assert [c.seq for c in fake.connections()] == [1]
    assert fake.protocol_errors() == []


def test_connections_and_transcript_have_no_arguments(fake: FakeSmtp) -> None:
    _deliver(fake, _email(f"link {LINK}\n"))
    fake.fail("rcpt", 550)
    with _smtp(fake) as client:
        client.ehlo()
        _login(client, fake)
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            client.sendmail(SENDER, [OTHER_RCPT], BODY)
    assert fake.credentials is not None
    transcript = fake.transcript()
    assert transcript.splitlines()[:3] == ["#1 início +0.000", "#1 <connect> tls=False 220",
                                           "#1 EHLO tls=False 250"]
    assert re.search(r"^#2 RCPT tls=False 550$", transcript, re.MULTILINE)
    assert re.search(r"^#2 fim quit \+[0-9.]+$", transcript, re.MULTILINE)
    records = repr(fake.connections())
    leaks = (SENDER, RCPT, OTHER_RCPT, CODE, "cq_teste_bot", fake.credentials.username,
             *fake.credentials.secret_forms())
    for text in (transcript, records, repr(fake.protocol_errors())):
        for leak in leaks:
            assert leak not in text
    assert FakeSmtp.transcript.__doc__ and "sem argumentos" in FakeSmtp.transcript.__doc__


def test_wait_for_returns_the_value_or_fails_with_transcript(fake: FakeSmtp) -> None:
    with _smtp(fake) as client:
        client.noop()
    assert fake.wait_for(lambda: len(fake.connections())) == 1
    assert fake.wait_for(lambda: fake.connections()[0].outcome == "quit", 1.0) is True
    with pytest.raises(AssertionError, match=r"wait_for: condição não satisfeita em 0\.05 s\n"
                                             r"#1 início"):
        fake.wait_for(lambda: False, timeout=0.05)
    fake.reset()
    with pytest.raises(AssertionError, match=r"\(nenhuma conexão\)"):
        fake.wait_for(lambda: None, timeout=0.02)


def test_closed_port_refuses_connections(fake: FakeSmtp) -> None:
    port = fake.closed_port()
    assert port != fake.port and fake.closed_port() == port
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=10).close()


def test_close_sends_421_to_open_connections_and_is_idempotent() -> None:
    fake = FakeSmtp()
    idle = _smtp(fake)
    idle.ehlo()
    fake.fail_network("mail", "hang")
    hung = _Raw(fake)
    hung.cmd(f"EHLO {LOCAL}")
    hung.cmd(f"AUTH PLAIN {_plain(fake)}")
    hung.send(f"MAIL FROM:<{SENDER}>\r\n".encode())
    fake.wait_for(lambda: _last_verb(fake, 1) == "MAIL")
    port = fake.closed_port()
    fake.close()
    assert fake.closed is True
    assert idle.getreply() == (421, f"4.3.2 {HOSTNAME} Service shutting down".encode())
    assert hung.reply() == [f"421 4.3.2 {HOSTNAME} Service shutting down".encode()]
    assert [c.outcome for c in fake.connections()] == ["closed", "closed"]
    idle.close()
    hung.close()
    fake.close()
    with pytest.raises(OSError):
        socket.create_connection((fake.host, fake.port), timeout=2).close()
    with pytest.raises(OSError):
        socket.create_connection((fake.host, port), timeout=10).close()


def test_session_smtp_is_one_live_server() -> None:
    first = session_smtp()
    assert session_smtp() is first and first.closed is False
    first.reset()
    with _smtp(first) as client:
        assert client.noop() == (250, b"2.0.0 Ok")
    assert first.protocol_errors() == []
    first.reset()


def test_internal_error_is_a_protocol_error_and_closes_the_connection(
        fake: FakeSmtp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake, "_cmd_noop", lambda *args: 1 / 0)
    with _smtp(fake) as client:
        with pytest.raises(smtplib.SMTPServerDisconnected):
            client.noop()
    assert _ended(fake).outcome == "closed"
    assert fake.protocol_errors() == [
        "#1: erro interno do dublê: ZeroDivisionError: division by zero"]


def test_silent_on_fd1_and_fd2(capfd: pytest.CaptureFixture[str],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Nada em stdout nem stderr, em nenhum caminho (o `socketserver` imprimiria o traceback em
    stderr)."""
    capfd.readouterr()
    with FakeSmtp(clock=FakeClock()) as fake:
        _deliver(fake, _email(f"link {LINK}\n"))
        fake.invite_links()
        for raw_bytes in (b"LIXO\xff\r\n", b"MAIL\n", b"X" * 20000 + b"\r\n", b"\r\n",
                          b"AUTH PLAIN\r\n!!!\r\n"):
            with socket.create_connection((fake.host, fake.port), timeout=5) as sock:
                sock.sendall(raw_bytes)
                sock.shutdown(socket.SHUT_WR)
                try:
                    while sock.recv(65536):
                        pass
                except OSError:
                    pass
        fake.reset(security="starttls")
        client = _smtp(fake)
        client.ehlo()
        with pytest.raises(ssl.SSLCertVerificationError):
            client.starttls(context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        client.close()
        fake.reset(security="tls", cert="expired")
        with pytest.raises(ssl.SSLCertVerificationError):
            _smtp_ssl(fake)
        fake.reset()
        for stage, kind in (("connect", "reset"), ("mail", "close"), ("rcpt", "hang")):
            fake.fail_network(stage, kind)
            with pytest.raises(OSError):
                with _smtp(fake, timeout=0.3) as client:
                    client.ehlo()
                    _login(client, fake)
                    client.sendmail(SENDER, [RCPT], BODY)
        fake.fail("ehlo", 554, when=lambda context: 1 / 0)
        monkeypatch.setattr(fake, "_cmd_noop", lambda *args: 1 / 0)
        with _smtp(fake) as client:
            client.ehlo()
            with pytest.raises(smtplib.SMTPServerDisconnected):
                client.noop()
        problems = fake.protocol_errors()
        hung = _Raw(fake)
        fake.fail_network("ehlo", "hang")
        hung.send(f"EHLO {LOCAL}\r\n".encode())
        fake.wait_for(lambda: _last_verb(fake) == "EHLO")
    hung.close()
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    division = "ZeroDivisionError: division by zero"
    assert f"#4: erro interno do dublê: {division}" in problems
    assert f"#4: predicado when falhou na etapa ehlo: {division}" in problems


def test_type_shapes() -> None:
    """Os registros são imutáveis e comparáveis (os testes do sistema comparam por igualdade)."""
    command = SmtpCommand("EHLO", False, 250)
    with pytest.raises(AttributeError):
        command.code = 500  # type: ignore[misc]
    assert AuthAttempt("PLAIN", True, True, True, True, 235) == AuthAttempt(
        "PLAIN", True, True, True, True, 235)
