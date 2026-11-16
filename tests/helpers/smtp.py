"""SMTP falso local.

Servidor SMTP de verdade (`socketserver.ThreadingTCPServer` em `127.0.0.1`, porta escolhida pelo
sistema), alcançado pelo adaptador real (`cq_server.adapters.smtp`) e pelo `smtplib` dos
autotestes. Só usa a biblioteca padrão e a `cryptography` (que gera a CA de teste) e **não**
importa `cq_server`: se o adaptador errar, o dublê continua capaz de acusar. O desenho segue a Bot
API falsa (`tests/helpers/bot_api.py`): um único `threading.Condition(RLock)`, uma
geração por `reset`, registro com `started`/`ended` de `monotonic()` e `clock_at` do relógio de
`reset(clock=)`.

**Subconjunto do protocolo** (RFC 5321, 3207, 4954 e 4616; verbos e palavras-chave sem distinção de
caixa):
- saudação `220 fake-smtp.invalid ESMTP`; `EHLO <domínio>` multilinha com `SIZE 10485760`,
  `8BITMIME` e `ENHANCEDSTATUSCODES`, mais `STARTTLS` (só no modo `starttls`, antes do TLS e com
  `advertise_starttls`) e `AUTH <mecanismos>` (só com credenciais, com TLS ativo ou no modo
  `none`); **nunca** anuncia `SMTPUTF8` (sem caminho SMTPUTF8), `PIPELINING`, `CHUNKING` nem
  `DSN`; o EHLO zera a transação; `HELO <domínio>` responde 250, sem extensões;
- `STARTTLS` (220 e o handshake; depois dele, a sessão volta ao estado inicial e exige novo EHLO) e
  TLS implícito no modo `tls` (o handshake roda no tratador, antes da saudação);
- `AUTH PLAIN` e `AUTH LOGIN`, com e sem resposta inicial: desafio `334 ` no PLAIN,
  `334 VXNlcm5hbWU6` e `334 UGFzc3dvcmQ6` no LOGIN; `=` é resposta vazia; `*` cancela (501 5.0.0);
  base64 inválido dá 501 5.5.2; mecanismo não anunciado, 504 5.5.4; 503 durante a transação ou
  depois de um sucesso; 530 5.7.0 antes do TLS nos modos `starttls` e `tls`; 235 2.7.0 no sucesso e
  535 5.7.8 na falha; o PLAIN exige `[authzid] NUL authcid NUL senha`, com `authzid` vazio ou igual
  ao `authcid`; `CRAM-MD5` pode ser anunciado (`advertise_auth`), mas não é implementado (504 e
  erro de protocolo);
- `MAIL FROM:<caminho> [SIZE=n] [BODY=7BIT|8BITMIME]` (exige EHLO, nenhuma transação aberta e AUTH
  quando há credenciais; `SIZE` acima do limite dá 552; parâmetro desconhecido ou `SMTPUTF8`, 555);
  `RCPT TO:<caminho>` (exige MAIL; guarda o endereço **com a caixa recebida**); `DATA` (354; lê até
  `CRLF.CRLF`, desfaz o dot-stuffing, responde `250 2.0.0 Ok: queued as Q<n>` e zera a transação);
  `RSET`, `NOOP` e `QUIT` (`221 2.0.0 Bye` e fecha);
- outro verbo conhecido recebe 502 5.5.1; um desconhecido, 500 5.5.2;
- o dublê espera `IDLE_TIMEOUT_SECONDS` (10 s; o RFC recomenda 5 min) pelo próximo comando e então
  responde `421 4.4.2` e fecha (desfecho `idle_timeout`).

Desvios deliberados: sem cabeçalho `Received` (RFC 5321, seção 4.4) e sem PIPELINING, CHUNKING, DSN,
SMTPUTF8, VRFY/EXPN, relay ou fila.

**Estrito:** tudo o que desvia do protocolo vai para `protocol_errors()`, que a fixture `smtp`
exige vazio:
- verbo fora do subconjunto (inclusive extensão não anunciada: `STARTTLS`, `AUTH`, mecanismo);
- comando fora de ordem: MAIL, RCPT, DATA, AUTH ou STARTTLS antes do EHLO; qualquer comando (exceto
  EHLO, HELO e QUIT) depois do STARTTLS sem novo EHLO; MAIL sem AUTH havendo credenciais; MAIL com
  transação aberta; RCPT sem MAIL; DATA sem RCPT aceito; AUTH repetido ou durante a transação;
- AUTH sem TLS fora do modo `none`;
- bytes em claro no buffer quando o STARTTLS termina;
- linha de comando com mais de 512 octetos (12.288 no AUTH, RFC 4954, seção 4), ou linha de texto com
  mais de 1.000 (com o CRLF e sem contar o ponto dobrado);
- CR ou LF soltos em comando ou no DATA; linha do DATA que começa com ponto sem o ponto dobrado;
- byte acima de 0x7F numa linha de comando (inclusive endereço), ou no DATA sem `BODY=8BITMIME`;
- `SIZE` malformado, `BODY` desconhecido, parâmetro `SMTPUTF8` ou desconhecido no MAIL, parâmetro
  no RCPT, sintaxe inválida de MAIL/RCPT, argumento em `STARTTLS`, `DATA`, `RSET` ou `QUIT`,
  EHLO/HELO sem domínio, base64 inválido ou mensagem PLAIN malformada no AUTH;
- linha codificada em quoted-printable com mais de 76 caracteres (RFC 2045, seção 6.7);
- segredo conhecido (a senha de `credentials` e as suas formas do fio,
  `SmtpCredentials.secret_forms()`) num argumento de comando — EHLO, MAIL, RCPT e os demais —,
  **exceto** os argumentos da troca AUTH: a resposta inicial do PLAIN ou do LOGIN e as linhas de
  resposta aos desafios;
- conexão encerrada pelo cliente sem `QUIT` quando a última resposta foi 2xx ou 3xx e não houve
  falha programada na conexão, **exceto** as conexões com `tls_alert`, handshake falho ou
  encerradas pelo prazo: as encerradas pelo prazo do cliente só ocorrem durante uma falha
  programada (`hang` ou `drip`, desfecho `abandoned`), e as encerradas pelo prazo do dublê têm o
  desfecho `idle_timeout`;
- exceção interna do dublê, ou predicado `when` que falhou.

Os textos dos erros nunca trazem os argumentos (endereços, nomes, segredos): só a conexão, o verbo
e o problema.

**Silencioso:** nada em stdout nem stderr (`handle_error` sobrescrito, nenhum `print`); `OSError` e
`ssl.SSLError` na escrita são engolidos e anotados na conexão.

**TLS:** `reset(security=…, cert=…)`, com `security` em `none`, `starttls` ou `tls` e `cert` em
`valid`, `wrong_host`, `expired` ou `self_signed`. Na primeira criação, a `cryptography` gera uma CA
de teste (EC P-256, SHA-256, `CN=CQ Test CA`, validade de 1 dia) e quatro certificados de
servidor: `valid` (SAN `DNS:localhost`, `IP:127.0.0.1` e `IP:::1`), `wrong_host` (SAN
`DNS:smtp.other.invalid`), `expired` (vencido pelo relógio **real**; o `FakeClock` não afeta o
OpenSSL) e `self_signed` (fora da CA). Os PEM passam por um diretório temporário, apagado logo
depois de carregados (o `load_cert_chain` só aceita caminho). O servidor usa
`SSLContext(PROTOCOL_TLS_SERVER)` com `minimum_version = TLSv1_2`, sem pedir certificado ao cliente
e sem ler o ambiente (nada de `SSLKEYLOGFILE`), sobre `ssl.SSLObject` e `MemoryBIO`: o dublê lê
sempre o socket cru (o que também dispensa o `MSG_PEEK`, que o `SSLSocket` recusa), e o handshake o
lê em modo bloqueante, para não perder o alerta do cliente (ver `_tls_stage`). Uma falha de
handshake vira `tls_alert` na conexão quando o cliente mandou um alerta (ex.:
`TLSV1_ALERT_UNKNOWN_CA`), e o desfecho é `tls_failed`. `ca_pem` e `client_context()` (contexto de
cliente que confia só na CA de teste) servem aos testes.

**Credenciais:** cada `reset` sorteia `SmtpCredentials("cq-smtp-" + token_hex(4),
token_urlsafe(24))`, exposto no atributo `credentials`, que o `mail_secrets()` lê;
`reset(credentials=None)` faz um servidor sem AUTH anunciado. O dublê **não** chama
`remember_secret` para elas (ficam fora de `all_secrets()`), nunca guarda a senha recebida e
registra cada tentativa como `AuthAttempt(mechanism, initial_response, user_ok, password_ok, tls,
code)`.

**Captura:** `messages()` devolve `CapturedMessage(seq, conn_seq, mail_from, mail_params, rcpt_to,
tls, tls_version, authenticated, raw, message, headers, text, acknowledged, started, ended,
clock_at)`: `raw` são os bytes com o dot-stuffing desfeito; `message`, o
`BytesParser(policy=email.policy.default)`; `headers`, os pares (nome, valor decodificado, RFC
2047) na ordem da mensagem; `text`, o corpo `text/plain` com QP, base64 e charset desfeitos e as
quebras de linha em `\\n`; `acknowledged=False` quando a falha programada veio depois de o dublê
aceitar a mensagem (`after_accept`). `invite_links()` extrai `https://t.me/<bot>?start=<código>`
(22 caracteres base64url) de `text` e registra cada código no `remember_secret` (como os tokens).

**Falhas programadas**, nas etapas de `STAGES` (`connect`, `ehlo`, `starttls`, `tls` = o handshake,
`auth`, `mail`, `rcpt`, `data`, `data_end`, `rset` e `quit`):
- `fail(stage, code, text=None, *, raw=None, times=1, when=None)`: responde `code` (3 dígitos, de
  2yz a 5yz: os 2yz e 3yz servem aos códigos inesperados, ex.: MAIL com 354, DATA com 250)
  no lugar da resposta normal, e o comando não tem efeito; `text` troca o texto padrão (que leva
  código estendido; o 550 de `rcpt` repete `<endereço>` de propósito, como armadilha para quem
  registrar `str(SMTPRecipientsRefused)`); `raw` manda uma linha de resposta literal (ex.:
  `"XYZ ilegível"`, que o `smtplib` lê como código −1), e `code` só decide o efeito; 421
  sempre fecha a conexão depois de responder; qualquer outro código em `connect` mantém a conexão e
  responde 503 a tudo, menos ao QUIT (RFC 5321, seção 3.1); a etapa `ehlo` recusa o EHLO (não o HELO; o
  adaptador não recua para HELO); na etapa `tls`, a linha vai em claro no lugar do
  handshake, e o dublê fecha a conexão (o cliente vê uma falha de TLS);
- `fail_network(stage, kind, *, times=1, when=None, after_accept=False,
  interval=0.2)`: `reset` (RST com `SO_LINGER` 0, como na Bot API falsa), `close` (FIN sem resposta), `hang`
  (nenhuma resposta até o cliente desistir, desfecho `abandoned`, ou até `release_hangs`, `reset`
  ou `close`, que fecham a conexão) e `drip` (linhas de continuação `NNN-…` válidas a cada
  `interval`, sem fim; em `connect`, 1 byte por vez; não vale em `tls`); `after_accept=True` só
  vale em `data_end`: a mensagem entra em `messages()` com `acknowledged=False` antes da falha;
- `times=None` repete até `clear_failures`; `when(StageContext)` escolhe as ocorrências (o
  `argument` é o endereço no MAIL e no RCPT, o domínio no EHLO e só o mecanismo no AUTH);
- "servidor fora do ar" é `fail_network("connect", "reset", times=None)`; `closed_port()` dá uma
  porta sem ouvinte para o único teste de recusa real.

**Observação:** `connections()` devolve `SmtpConnection(seq, gen, started, ended, clock_at,
ehlo_name, tls, tls_version, tls_alert, commands, auth, quit, outcome)`; em `commands` ficam só o
verbo, o estado do TLS e o código da resposta, **sem argumentos** (as etapas sem comando aparecem
como `<connect>`, `<tls>`, `<auth-response>`, `<data-end>`, `<timeout>` e `<shutdown>`); `outcome`
é `open`, `quit`, `client_closed`, `abandoned`, `reset`, `closed`, `tls_failed` ou `idle_timeout`.
`transcript()` dá uma linha por comando, sem argumentos; `wait_for(predicate, timeout=5.0)` levanta
`AssertionError` com o fim do `transcript` se o prazo vencer; `protocol_errors()` espera até
`SETTLE_SECONDS` o dublê terminar de processar o que já chegou (ex.: o fechamento de uma conexão) e
devolve a lista de desvios.

**Ciclo de vida:** `session_smtp()` é o dublê único da sessão, fechado no `atexit`; `close()` é
idempotente e manda `421 4.3.2` às conexões abertas. Remetente e domínios dos testes em `example.*`
(nunca `.test`, que o `email-validator` recusa): `SENDER`.
"""

from __future__ import annotations

import atexit
import binascii
import copy
import ipaddress
import itertools
import math
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import ssl
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any, NamedTuple, TypeVar

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tests.helpers.secrets import remember_secret

T = TypeVar("T")

# ---------------------------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------------------------

HOSTNAME = "fake-smtp.invalid"
SENDER = "convites@cq.example.org"
SIZE_LIMIT = 10_485_760
COMMAND_LINE_LIMIT = 512
AUTH_LINE_LIMIT = 12_288
TEXT_LINE_LIMIT = 1000
QP_LINE_LIMIT = 76
IDLE_TIMEOUT_SECONDS = 10.0  # lido a cada uso (o autoteste o encurta com `monkeypatch`)
SOCKET_CHECK_SECONDS = 0.01
SETTLE_SECONDS = 1.0
IO_TIMEOUT_SECONDS = 1.0

STAGES: tuple[str, ...] = ("connect", "ehlo", "starttls", "tls", "auth", "mail", "rcpt", "data",
                           "data_end", "rset", "quit")
NETWORK_KINDS: tuple[str, ...] = ("reset", "close", "hang", "drip")
SECURITY_MODES: tuple[str, ...] = ("none", "starttls", "tls")
CERT_KINDS: tuple[str, ...] = ("valid", "wrong_host", "expired", "self_signed")
AUTH_MECHANISMS: tuple[str, ...] = ("PLAIN", "LOGIN", "CRAM-MD5")  # CRAM-MD5: só anunciado
OUTCOMES: tuple[str, ...] = ("open", "quit", "client_closed", "abandoned", "reset", "closed",
                             "tls_failed", "idle_timeout")
SUBSET_VERBS = frozenset({"EHLO", "HELO", "STARTTLS", "AUTH", "MAIL", "RCPT", "DATA", "RSET",
                          "NOOP", "QUIT"})
KNOWN_UNSUPPORTED_VERBS = frozenset({"VRFY", "EXPN", "HELP", "ETRN", "BDAT", "TURN", "ATRN", "SEND",
                                     "SOML", "SAML", "XCLIENT", "XFORWARD"})

# Código da resposta de sucesso de cada etapa (usado nas linhas de continuação do `drip`).
_SUCCESS_CODE = {"connect": 220, "ehlo": 250, "starttls": 220, "auth": 334, "mail": 250,
                 "rcpt": 250, "data": 354, "data_end": 250, "rset": 250, "quit": 221}

_LOGIN_USER_CHALLENGE = "VXNlcm5hbWU6"  # base64("Username:")
_LOGIN_PASSWORD_CHALLENGE = "UGFzc3dvcmQ6"  # base64("Password:")

_MAIL_RE = re.compile(r"FROM:<([^<>\s]*)>((?: +\S+)*) *", re.IGNORECASE)
_RCPT_RE = re.compile(r"TO:<([^<>\s]*)>((?: +\S+)*) *", re.IGNORECASE)
_INVITE_LINK_RE = re.compile(
    r"https://t\.me/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])")
_LINE_BREAK_RE = re.compile(r"\r\n|\n")

_RANDOM: Any = object()


def monotonic() -> float:
    """O relógio de `started`/`ended`: `time.perf_counter()` (no Windows, o `time.monotonic()` anda
    em passos de 15,6 ms; ver `tests/helpers/bot_api.py`)."""
    return time.perf_counter()


# ---------------------------------------------------------------------------------------------
# Tipos públicos
# ---------------------------------------------------------------------------------------------


class SmtpCredentials(NamedTuple):
    """Usuário e senha do dublê (sorteados por `reset`). O `repr` não mostra a senha."""

    username: str
    password: str

    def secret_forms(self) -> tuple[str, ...]:
        """A senha e as formas em que o `smtplib` a põe no fio: o base64 da mensagem PLAIN (sem e
        com `authzid`) e o base64 da senha (LOGIN). É o que o `protocol_errors()` procura nos
        argumentos de comando; o `mail_secrets()` procura o mesmo nas saídas."""
        user, password = self.username, self.password
        forms = [password,
                 _b64(f"\0{user}\0{password}"),
                 _b64(f"{user}\0{user}\0{password}"),
                 _b64(password)]
        return tuple(dict.fromkeys(forms))

    def __repr__(self) -> str:
        return f"SmtpCredentials(username={self.username!r}, password='[oculta]')"


@dataclass(frozen=True)
class AuthAttempt:
    """Uma tentativa de AUTH (o dublê nunca guarda a senha recebida). `code` é a resposta final
    (235, 535, 501…)."""

    mechanism: str
    initial_response: bool
    user_ok: bool
    password_ok: bool
    tls: bool
    code: int | None


@dataclass(frozen=True)
class SmtpCommand:
    """Um comando (ou etapa) da conexão: só o verbo, o estado do TLS e o código da resposta, sem
    argumentos. `code` é `None` quando não houve resposta completa (falha de rede, handshake) e −1
    quando a resposta programada (`raw`) é ilegível para o `smtplib`."""

    verb: str
    tls: bool
    code: int | None


@dataclass(frozen=True)
class SmtpConnection:
    """Uma conexão recebida pelo dublê (ver o docstring do módulo)."""

    seq: int
    gen: int
    started: float
    ended: float | None
    clock_at: datetime
    ehlo_name: str | None
    tls: bool
    tls_version: str | None
    tls_alert: str | None
    commands: tuple[SmtpCommand, ...]
    auth: tuple[AuthAttempt, ...]
    quit: bool
    outcome: str

    @property
    def verbs(self) -> tuple[str, ...]:
        return tuple(c.verb for c in self.commands)


@dataclass(frozen=True)
class CapturedMessage:
    """Uma mensagem recebida pelo DATA (ver o docstring do módulo)."""

    seq: int
    conn_seq: int
    mail_from: str
    mail_params: tuple[str, ...]
    rcpt_to: tuple[str, ...]
    tls: bool
    tls_version: str | None
    authenticated: bool
    raw: bytes = field(repr=False)
    message: EmailMessage = field(repr=False, compare=False)
    headers: tuple[tuple[str, str], ...]
    text: str | None
    acknowledged: bool
    started: float
    ended: float
    clock_at: datetime

    def header(self, name: str) -> str | None:
        """O valor decodificado do cabeçalho `name` (sem distinção de caixa), ou `None`; um
        cabeçalho repetido é `ValueError`."""
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        if len(values) > 1:
            raise ValueError(f"cabeçalho repetido: {name}")
        return values[0] if values else None

    def header_names(self) -> tuple[str, ...]:
        return tuple(k for k, _ in self.headers)


@dataclass(frozen=True)
class InviteLink:
    """Um link de convite extraído do texto decodificado de uma mensagem."""

    message_seq: int
    rcpt_to: tuple[str, ...]
    url: str
    bot_username: str
    code: str
    acknowledged: bool


@dataclass(frozen=True)
class StageContext:
    """O que o predicado `when` de `fail`/`fail_network` recebe. `argument`: o endereço no MAIL e
    no RCPT, o domínio no EHLO, só o mecanismo no AUTH e `None` nas demais etapas."""

    stage: str
    conn_seq: int
    argument: str | None
    tls: bool
    authenticated: bool
    mail_from: str | None
    rcpt_to: tuple[str, ...]


# ---------------------------------------------------------------------------------------------
# Material TLS (uma CA de teste por processo)
# ---------------------------------------------------------------------------------------------


class _TlsMaterial:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CQ Test CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        self.ca_pem: str = ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        good_names: list[x509.GeneralName] = [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]
        leaves = {
            "valid": self._leaf(ca_key, ca_cert, good_names, now - timedelta(hours=1),
                                now + timedelta(days=1)),
            "wrong_host": self._leaf(ca_key, ca_cert, [x509.DNSName("smtp.other.invalid")],
                                     now - timedelta(hours=1), now + timedelta(days=1)),
            "expired": self._leaf(ca_key, ca_cert, good_names, now - timedelta(days=3),
                                  now - timedelta(days=1)),
            "self_signed": self._leaf(None, None, good_names, now - timedelta(hours=1),
                                      now + timedelta(days=1)),
        }
        self.contexts: dict[str, ssl.SSLContext] = {}
        folder = tempfile.mkdtemp(prefix="cq-fake-smtp-")
        try:
            for kind, (cert_pem, key_pem) in leaves.items():
                cert_path = os.path.join(folder, f"{kind}.crt")
                key_path = os.path.join(folder, f"{kind}.key")
                with open(cert_path, "wb") as fh:
                    fh.write(cert_pem)
                with open(key_path, "wb") as fh:
                    fh.write(key_pem)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(cert_path, key_path)
                self.contexts[kind] = context
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _leaf(ca_key: Any, ca_cert: Any, names: list[x509.GeneralName], not_before: datetime,
              not_after: datetime) -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-smtp")])
        issuer_key = key if ca_key is None else ca_key
        issuer_name = subject if ca_cert is None else ca_cert.subject
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                issuer_key.public_key()), critical=False)
        )
        cert = builder.sign(issuer_key, hashes.SHA256())
        key_pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        return cert.public_bytes(serialization.Encoding.PEM), key_pem


_TLS: _TlsMaterial | None = None
_TLS_LOCK = threading.Lock()


def _tls_material() -> _TlsMaterial:
    global _TLS
    with _TLS_LOCK:
        if _TLS is None:
            _TLS = _TlsMaterial()
        return _TLS


# ---------------------------------------------------------------------------------------------
# Funções auxiliares puras
# ---------------------------------------------------------------------------------------------


def _b64(text: str) -> str:
    return binascii.b2a_base64(text.encode("utf-8"), newline=False).decode("ascii")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reply_code(line: str) -> int:
    """O código que o `smtplib` lê numa linha de resposta (`int(line[:3])`, senão −1)."""
    try:
        return int(line.encode("utf-8")[:3])
    except ValueError:
        return -1


def _format_reply(code: int, lines: Sequence[str]) -> bytes:
    lines = list(lines) or [""]
    out = [f"{code}-{line}\r\n" for line in lines[:-1]]
    out.append(f"{code} {lines[-1]}\r\n")
    return "".join(out).encode("utf-8")


def _default_text(stage: str, code: int, argument: str | None) -> str:
    """Texto padrão das respostas programadas (com código estendido, RFC 2034/3463)."""
    klass = code // 100
    if code == 421:
        return f"4.3.2 {HOSTNAME} Service not available, closing transmission channel"
    if klass in (2, 3):
        return ("2.0.0 " if klass == 2 else "") + "Unexpected reply from the fake"
    if stage == "rcpt" and code == 550:
        return f"5.1.1 <{argument}>: Recipient address rejected: User unknown"
    if code == 552:
        return "5.3.4 Message size exceeds fixed maximum message size"
    table = {
        ("connect", 4): "4.3.2 Service temporarily unavailable",
        ("connect", 5): "5.3.2 No SMTP service here",
        ("ehlo", 4): "4.3.2 EHLO temporarily rejected",
        ("ehlo", 5): "5.5.1 EHLO rejected",
        ("starttls", 4): "4.7.0 TLS not available due to temporary reason",
        ("starttls", 5): "5.7.0 TLS not available",
        ("tls", 4): "4.7.0 TLS not available due to temporary reason",
        ("tls", 5): "5.7.0 TLS not available",
        ("auth", 4): "4.7.0 Temporary authentication failure",
        ("auth", 5): "5.7.8 Authentication credentials invalid",
        ("mail", 4): "4.3.0 Sender temporarily rejected",
        ("mail", 5): "5.7.1 Sender address rejected",
        ("rcpt", 4): "4.2.1 Recipient temporarily unavailable",
        ("rcpt", 5): "5.1.0 Recipient address rejected",
        ("data", 4): "4.3.0 DATA temporarily rejected",
        ("data", 5): "5.5.0 DATA rejected",
        ("data_end", 4): "4.3.0 Message temporarily deferred",
        ("data_end", 5): "5.6.0 Message rejected",
        ("rset", 4): "4.3.0 RSET temporarily failed",
        ("rset", 5): "5.5.0 RSET failed",
        ("quit", 4): "4.3.0 QUIT temporarily failed",
        ("quit", 5): "5.5.0 QUIT failed",
    }
    return table[(stage, klass)]


def _set_linger_zero(sock: socket.socket) -> None:
    linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)


def _close_quietly(sock: socket.socket | None, *, rst: bool = False, fin: bool = False) -> None:
    if sock is None:
        return
    try:
        if rst:
            _set_linger_zero(sock)
        elif fin:
            sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        sock.close()
    except (OSError, ValueError):
        pass


def _abort_socket(sock: socket.socket) -> None:
    """Interrompe, de outra thread, uma operação bloqueada no socket: o `shutdown` acorda o `recv`
    no Linux; no Windows, só fechar o socket o acorda."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    if os.name == "nt":
        try:
            sock.close()
        except (OSError, ValueError):
            pass


def _readable(sock: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return bool(readable)


def _decode_b64(text: str) -> bytes | None:
    """Base64 estrito do AUTH (`=` é a resposta vazia, RFC 4954, seção 4); `None` se inválido."""
    if text == "=":
        return b""
    try:
        return binascii.a2b_base64(text.encode("ascii"), strict_mode=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


# ---------------------------------------------------------------------------------------------
# Estado interno (sempre sob o único `threading.Condition` do dublê)
# ---------------------------------------------------------------------------------------------


class _Stop(Exception):
    """A conexão terminou (o desfecho já foi registrado)."""


class _Cancel(Exception):
    """O cliente cancelou a troca AUTH com `*`."""


class _BadBase64(Exception):
    """Resposta do AUTH que não é base64 válido."""


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Config:
    security: str
    cert: str
    credentials: SmtpCredentials | None
    advertise_starttls: bool
    advertise_auth: tuple[str, ...]


@dataclass
class _Failure:
    stage: str
    kind: str  # smtp | reset | close | hang | drip
    times: int | None
    when: Callable[[StageContext], Any] | None
    code: int | None = None
    text: str | None = None
    raw: str | None = None
    after_accept: bool = False
    interval: float = 0.2


class _Conn:
    def __init__(self, *, seq: int, gen: int, config: _Config, sock: socket.socket,
                 started: float, clock_at: datetime) -> None:
        self.seq = seq
        self.gen = gen
        self.config = config
        self.sock = sock
        self.started = started
        self.clock_at = clock_at
        self.ended: float | None = None
        self.outcome = "open"
        self.ehlo_name: str | None = None
        self.tls_active = False
        self.tls_version: str | None = None
        self.tls_alert: str | None = None
        self.tls_failed = False
        self.tls_error: str | None = None
        self.handshaking = False
        self.handshake_expired = False
        self.aborted = False
        self.tls: ssl.SSLObject | None = None
        self.bio_in = ssl.MemoryBIO()
        self.bio_out = ssl.MemoryBIO()
        self.commands: list[SmtpCommand] = []
        self.auth: list[AuthAttempt] = []
        self.quit = False
        self.programmed = False
        self.last_code: int | None = None
        self.write_failed = False
        self.buffer = bytearray()
        self.busy = True  # processando o que já chegou (o `protocol_errors` espera)
        self.greeted = False
        self.esmtp = False
        self.after_starttls = False
        self.authenticated = False
        self.no_service = False
        self.mail_from: str | None = None
        self.mail_params: tuple[str, ...] = ()
        self.body_8bit = False
        self.rcpts: list[str] = []

    def reset_transaction(self) -> None:
        self.mail_from = None
        self.mail_params = ()
        self.body_8bit = False
        self.rcpts = []

    def snapshot(self) -> SmtpConnection:
        return SmtpConnection(
            seq=self.seq, gen=self.gen, started=self.started, ended=self.ended,
            clock_at=self.clock_at, ehlo_name=self.ehlo_name, tls=self.tls_active,
            tls_version=self.tls_version, tls_alert=self.tls_alert, commands=tuple(self.commands),
            auth=tuple(self.auth), quit=self.quit, outcome=self.outcome)


# ---------------------------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------------------------


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 64
    fake: FakeSmtp

    def process_request(self, request: Any, client_address: Any) -> None:
        self.fake._accepted()
        super().process_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Silêncio: o padrão imprime o traceback em stderr."""


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        self.server.fake._serve(self.request)


# ---------------------------------------------------------------------------------------------
# O SMTP falso
# ---------------------------------------------------------------------------------------------


class FakeSmtp:
    """SMTP falso: servidor em `127.0.0.1` (ver o docstring do módulo).

    Uso típico: a fixture `smtp` faz `reset(clock=clock)`; o sistema recebe `host`, `port` e
    `credentials`; o teste programa falhas (`fail*`) e confere `messages()`, `connections()`,
    `invite_links()` e `protocol_errors()`.
    """

    host = "127.0.0.1"

    def __init__(self, *, clock: Any = None) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._tls = _tls_material()
        self.ca_pem: str = self._tls.ca_pem
        self._gen = 0
        self._closing = False
        self._closed = False
        self._live: set[_Conn] = set()
        self._accepting = 0
        self._hang_counter = 0
        self._released_through = 0
        self._closed_port_sock: socket.socket | None = None
        self.credentials: SmtpCredentials | None = None
        self._config = _Config("none", "valid", None, True, ("PLAIN", "LOGIN"))
        self._clock: Any = _UtcClock()
        self._init_state()
        self.reset(clock)
        self._server = _Server((self.host, 0), _Handler)
        self._server.fake = self
        self.port: int = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, name="fake-smtp",
                                        daemon=True)
        self._thread.start()

    def _init_state(self) -> None:
        self._conns: list[_Conn] = []
        self._messages: list[CapturedMessage] = []
        self._problems: list[str] = []
        self._failures: list[_Failure] = []
        self._conn_seq = itertools.count(1)
        self._msg_seq = itertools.count(1)

    # -- ciclo de vida -------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def security(self) -> str:
        return self._config.security

    @property
    def cert(self) -> str:
        return self._config.cert

    def reset(self, clock: Any = None, *, security: str = "none", cert: str = "valid",
              credentials: Any = _RANDOM, advertise_starttls: bool = True,
              advertise_auth: Sequence[str] = ("PLAIN", "LOGIN")) -> None:
        """Esquece conexões, mensagens, falhas e erros; troca o relógio e a configuração.

        `credentials`: omitido sorteia `SmtpCredentials` novas; `None` faz um servidor sem AUTH
        anunciado (MAIL não exige AUTH); um par `(usuário, senha)` fixa as credenciais. As conexões
        abertas (inclusive as presas em `hang`/`drip`) são fechadas sem resposta, e nada do que
        elas fizerem depois entra no registro novo.
        """
        if security not in SECURITY_MODES:
            raise ValueError(f"security deve ser um de {SECURITY_MODES}")
        if cert not in CERT_KINDS:
            raise ValueError(f"cert deve ser um de {CERT_KINDS}")
        if not isinstance(advertise_starttls, bool):
            raise ValueError("advertise_starttls deve ser booleano")
        if isinstance(advertise_auth, str):
            raise ValueError("advertise_auth deve ser uma sequência de mecanismos")
        mechanisms = tuple(str(m).upper() for m in advertise_auth)
        if any(m not in AUTH_MECHANISMS for m in mechanisms) or len(set(mechanisms)) != len(
                mechanisms):
            raise ValueError(f"advertise_auth aceita mecanismos distintos de {AUTH_MECHANISMS}")
        creds: SmtpCredentials | None
        if credentials is _RANDOM:
            creds = SmtpCredentials("cq-smtp-" + secrets.token_hex(4), secrets.token_urlsafe(24))
        elif credentials is None:
            creds = None
        else:
            try:
                user, password = credentials
            except (TypeError, ValueError):
                raise ValueError("credentials deve ser None ou um par (usuário, senha)") from None
            for value in (user, password):
                if (not isinstance(value, str) or not value
                        or any(ch in value for ch in "\0\r\n")):
                    raise ValueError("usuário e senha devem ser textos não vazios, sem NUL, CR "
                                     "nem LF")
            creds = SmtpCredentials(user, password)
        with self._cond:
            self._gen += 1
            self._clock = clock if clock is not None else _UtcClock()
            self._config = _Config(security, cert, creds, advertise_starttls, mechanisms)
            self.credentials = creds
            self._init_state()
            self._released_through = self._hang_counter
            self._cond.notify_all()
        self._abort_handshakes()

    def close(self) -> None:
        """Para o servidor (idempotente); as conexões abertas recebem `421 4.3.2` e são fechadas."""
        with self._cond:
            if self._closed or self._closing:
                return
            self._closing = True
            self._cond.notify_all()
        self._abort_handshakes()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        deadline = monotonic() + 2.0
        with self._cond:
            while self._live and monotonic() < deadline:
                self._cond.wait(SOCKET_CHECK_SECONDS)
            remaining = list(self._live)
            reserved, self._closed_port_sock = self._closed_port_sock, None
        for conn in remaining:
            _close_quietly(conn.sock, fin=True)
        _close_quietly(reserved)
        self._closed = True

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- falhas programadas --------------------------------------------------------------------

    @staticmethod
    def _check_common(stage: str, times: Any, when: Any) -> None:
        if stage not in STAGES:
            raise ValueError(f"etapa fora do dublê: {stage!r} (etapas: {STAGES})")
        if times is not None and (not _is_int(times) or times < 1):
            raise ValueError("times deve ser um inteiro positivo ou None")
        if when is not None and not callable(when):
            raise ValueError("when deve ser chamável ou None")

    def fail(self, stage: str, code: int, text: str | None = None, *, raw: str | None = None,
             times: int | None = 1, when: Callable[[StageContext], Any] | None = None) -> None:
        """As próximas `times` ocorrências de `stage` (`None` = até `clear_failures`) que casam
        com `when(StageContext)` recebem a resposta `code` no lugar da normal (ver o docstring do
        módulo). `text` pode ter `\\n` (resposta multilinha); `raw` é uma linha literal, sem CR nem
        LF, e exclui `text`."""
        self._check_common(stage, times, when)
        if not _is_int(code) or not 200 <= code <= 599:
            raise ValueError("code deve ser um inteiro de 3 dígitos, de 2yz a 5yz")
        if text is not None and raw is not None:
            raise ValueError("use text ou raw, não os dois")
        if text is not None and (not isinstance(text, str) or "\r" in text or "\0" in text):
            raise ValueError("text deve ser um texto sem CR nem NUL")
        if raw is not None and (not isinstance(raw, str) or any(ch in raw for ch in "\r\n\0")):
            raise ValueError("raw deve ser uma linha sem CR, LF nem NUL")
        self._add_failure(_Failure(stage=stage, kind="smtp", times=times, when=when, code=code,
                                   text=text, raw=raw))

    def fail_network(self, stage: str, kind: str, *, times: int | None = 1,
                     when: Callable[[StageContext], Any] | None = None,
                     after_accept: bool = False, interval: float = 0.2) -> None:
        """Falha de rede nas próximas `times` ocorrências de `stage`: `reset`, `close`, `hang` ou
        `drip` (ver o docstring do módulo)."""
        self._check_common(stage, times, when)
        if kind not in NETWORK_KINDS:
            raise ValueError(f"kind deve ser um de {NETWORK_KINDS}")
        if kind == "drip" and stage == "tls":
            raise ValueError("drip não vale na etapa tls (o handshake)")
        if not isinstance(after_accept, bool):
            raise ValueError("after_accept deve ser booleano")
        if after_accept and stage != "data_end":
            raise ValueError("after_accept só vale na etapa data_end")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or interval <= 0):
            raise ValueError("interval deve ser um número positivo")
        self._add_failure(_Failure(stage=stage, kind=kind, times=times, when=when,
                                   after_accept=after_accept, interval=float(interval)))

    def _add_failure(self, failure: _Failure) -> None:
        with self._cond:
            self._failures.append(failure)

    def release_hangs(self) -> None:
        """Solta as conexões presas (`hang`) ou gotejando (`drip`) agora: o dublê as fecha sem
        resposta (desfecho `closed`). As ocorrências seguintes que casarem com uma falha programada
        voltam a ficar presas."""
        with self._cond:
            self._released_through = self._hang_counter
            self._cond.notify_all()

    def clear_failures(self) -> None:
        """Remove as falhas programadas que ainda não foram usadas."""
        with self._cond:
            self._failures.clear()

    # -- observação ----------------------------------------------------------------------------

    def messages(self) -> list[CapturedMessage]:
        """As mensagens recebidas desde o último `reset`, na ordem de chegada."""
        with self._cond:
            return list(self._messages)

    def invite_links(self) -> list[InviteLink]:
        """Os links de convite (`https://t.me/<bot>?start=<código>`) do texto decodificado das
        mensagens, em ordem; cada código entra em `remember_secret`."""
        links: list[InviteLink] = []
        for message in self.messages():
            if message.text is None:
                continue
            seen: set[str] = set()
            for match in _INVITE_LINK_RE.finditer(message.text):
                url = match.group(0)
                if url in seen:
                    continue
                seen.add(url)
                remember_secret(match.group(2))
                links.append(InviteLink(message_seq=message.seq, rcpt_to=message.rcpt_to, url=url,
                                        bot_username=match.group(1), code=match.group(2),
                                        acknowledged=message.acknowledged))
        return links

    def connections(self) -> list[SmtpConnection]:
        """As conexões recebidas desde o último `reset`, em ordem (inclui as abertas)."""
        with self._cond:
            return [c.snapshot() for c in self._conns]

    def transcript(self) -> str:
        """Uma linha por comando, **sem argumentos**, para mensagens de falha."""
        conns = self.connections()
        if not conns:
            return "(nenhuma conexão)"
        t0 = conns[0].started
        lines: list[str] = []
        for c in conns:
            lines.append(f"#{c.seq} início +{c.started - t0:.3f}")
            for cmd in c.commands:
                code = "-" if cmd.code is None else str(cmd.code)
                lines.append(f"#{c.seq} {cmd.verb} tls={cmd.tls} {code}")
            if c.ended is not None:
                alert = f" alerta={c.tls_alert}" if c.tls_alert else ""
                lines.append(f"#{c.seq} fim {c.outcome}{alert} +{c.ended - t0:.3f}")
        return "\n".join(lines)

    def wait_for(self, predicate: Callable[[], T], timeout: float = 5.0) -> T:
        """Espera `predicate()` ficar verdadeiro e devolve o valor; `AssertionError` no prazo."""
        deadline = monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - monotonic()
            if remaining <= 0:
                tail = "\n".join(self.transcript().splitlines()[-20:])
                raise AssertionError(f"wait_for: condição não satisfeita em {timeout} s\n{tail}")
            with self._cond:
                self._cond.wait(min(SOCKET_CHECK_SECONDS, remaining))

    def protocol_errors(self) -> list[str]:
        """Os desvios do protocolo desde o último `reset` (ver o docstring do módulo), depois de
        esperar até `SETTLE_SECONDS` o dublê processar o que já chegou."""
        self._settle()
        with self._cond:
            return list(self._problems)

    def _settle(self) -> None:
        deadline = monotonic() + SETTLE_SECONDS
        while True:
            with self._cond:
                conns = [c for c in self._live if c.gen == self._gen and c.outcome == "open"]
                accepting = self._accepting
            if not accepting and not any(c.busy or _readable(c.sock) for c in conns):
                return
            if monotonic() >= deadline:
                return
            with self._cond:
                self._cond.wait(SOCKET_CHECK_SECONDS)

    # -- apoio aos testes ------------------------------------------------------------------------

    def client_context(self) -> ssl.SSLContext:
        """Contexto de cliente que confia só na CA de teste, com verificação de certificado e de
        nome (`PROTOCOL_TLS_CLIENT`), sem ler o ambiente."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=self.ca_pem)
        return context

    def closed_port(self) -> int:
        """Uma porta de `127.0.0.1` sem ouvinte (reservada por um socket ligado que nunca escuta,
        até `close()`): a conexão é recusada."""
        with self._cond:
            if self._closed_port_sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((self.host, 0))
                self._closed_port_sock = sock
            return int(self._closed_port_sock.getsockname()[1])

    # -- internos: registro ----------------------------------------------------------------------

    def _accepted(self) -> None:
        with self._cond:
            self._accepting += 1

    def _scrub(self, text: str) -> str:
        creds = self._config.credentials
        if creds is not None:
            for form in sorted(creds.secret_forms(), key=len, reverse=True):
                text = text.replace(form, "[segredo]")
        return text

    def _problem(self, conn: _Conn, text: str) -> None:
        with self._cond:
            if conn.gen == self._gen:
                self._problems.append(self._scrub(text))
                self._cond.notify_all()

    def _flag(self, conn: _Conn, verb: str, text: str) -> None:
        self._problem(conn, f"#{conn.seq} {verb}: {text}")

    def _record(self, conn: _Conn, verb: str, code: int | None) -> None:
        with self._cond:
            conn.commands.append(SmtpCommand(verb=verb, tls=conn.tls_active, code=code))
            if code is not None:
                conn.last_code = code
            self._cond.notify_all()

    def _end(self, conn: _Conn, outcome: str) -> None:
        with self._cond:
            if conn.outcome != "open":
                return
            conn.outcome = outcome
            conn.ended = monotonic()
            conn.busy = False
            if (outcome == "client_closed" and not conn.quit and conn.last_code is not None
                    and 200 <= conn.last_code < 400 and not conn.programmed
                    and conn.tls_alert is None and not conn.tls_failed):
                self._problem(conn, f"#{conn.seq}: conexão encerrada pelo cliente sem QUIT "
                                    f"(última resposta {conn.last_code})")
            self._cond.notify_all()

    # -- internos: E/S ---------------------------------------------------------------------------

    def _send(self, conn: _Conn, data: bytes) -> bool:
        """Manda `data` (cifrado, com TLS ativo); um erro de escrita é engolido e anotado."""
        try:
            if conn.tls is not None:
                conn.tls.write(data)
                data = conn.bio_out.read()
            conn.sock.sendall(data)
            return True
        except (OSError, ValueError):
            conn.write_failed = True
            return False

    def _reply(self, conn: _Conn, verb: str, code: int, lines: Sequence[str] | None = None, *,
               raw: str | None = None, then_end: str | None = None) -> None:
        """Registra o comando com o código e manda a resposta (registrar antes de mandar: quem
        recebe a resposta já encontra o registro)."""
        if raw is not None:
            data = raw.encode("utf-8") + b"\r\n"
            recorded = _reply_code(raw)
        else:
            data = _format_reply(code, lines if lines is not None else [""])
            recorded = code
        self._record(conn, verb, recorded)
        if then_end is not None:
            self._end(conn, then_end)
        self._send(conn, data)

    def _poll(self, conn: _Conn, timeout: float) -> bytes | None:
        """Até `timeout` pelo que o cliente mandar: os bytes (decifrados, com TLS ativo), `b""` se
        ele foi embora (FIN, RST ou `close_notify`) ou `None` se nada chegou."""
        if conn.tls is not None:
            data = self._tls_read(conn)
            if data is not None:
                conn.busy = True
                return data
        conn.busy = False
        try:
            readable, _, _ = select.select([conn.sock], [], [], timeout)
            if not readable:
                return None
            conn.busy = True
            raw = conn.sock.recv(65536)
        except TimeoutError:
            return None
        except (OSError, ValueError):
            return b""
        if conn.tls is None or not raw:
            return raw
        conn.bio_in.write(raw)
        return self._tls_read(conn)

    def _tls_read(self, conn: _Conn) -> bytes | None:
        """Decifra o que já chegou: os bytes, `b""` no `close_notify` ou num registro inválido, ou
        `None` se falta o resto do registro."""
        assert conn.tls is not None
        try:
            data: bytes | None = conn.tls.read(65536)
        except ssl.SSLWantReadError:
            data = None
        except (ssl.SSLZeroReturnError, ssl.SSLError, ValueError):
            data = b""
        pending = conn.bio_out.read()
        if pending:
            try:
                conn.sock.sendall(pending)
            except (OSError, ValueError):
                conn.write_failed = True
        return data

    def _check_stop(self, conn: _Conn, *, can_reply: bool = True) -> None:
        with self._cond:
            closing = self._closing
            stale = conn.gen != self._gen
        if closing:
            if can_reply:
                self._reply(conn, "<shutdown>", 421,
                            [f"4.3.2 {HOSTNAME} Service shutting down"])
            self._end(conn, "closed")
            raise _Stop
        if stale:
            self._end(conn, "closed")
            raise _Stop

    def _idle_timeout(self, conn: _Conn) -> None:
        self._reply(conn, "<timeout>", 421, [f"4.4.2 {HOSTNAME} Error: timeout exceeded"],
                    then_end="idle_timeout")
        raise _Stop

    def _read_line(self, conn: _Conn, limit: int) -> tuple[bytes, bool]:
        """A próxima linha (até o LF, inclusive), e se ela passou de `limit` (então o conteúdo é
        descartado). Termina a conexão (`_Stop`) no fechamento pelo cliente, no prazo, no `close`
        e no `reset`."""
        deadline = monotonic() + IDLE_TIMEOUT_SECONDS
        overlong = False
        while True:
            index = conn.buffer.find(b"\n")
            if index >= 0:
                line = bytes(conn.buffer[:index + 1])
                del conn.buffer[:index + 1]
                return line, overlong or len(line) > limit
            if len(conn.buffer) > limit:
                overlong = True
                del conn.buffer[:]
            self._check_stop(conn)
            data = self._poll(conn, SOCKET_CHECK_SECONDS)
            if data is None:
                if monotonic() >= deadline:
                    self._idle_timeout(conn)
                continue
            if not data:
                self._end(conn, "client_closed")
                raise _Stop
            conn.buffer += data

    # -- internos: atendimento -------------------------------------------------------------------

    def _serve(self, sock: socket.socket) -> None:
        with self._cond:
            if self._accepting > 0:
                self._accepting -= 1
            if self._closing:
                conn = None
            else:
                conn = _Conn(seq=next(self._conn_seq), gen=self._gen, config=self._config,
                             sock=sock, started=monotonic(), clock_at=self._clock.now())
                self._conns.append(conn)
                self._live.add(conn)
            self._cond.notify_all()
        if conn is None:
            try:
                sock.sendall(_format_reply(421, [f"4.3.2 {HOSTNAME} Service shutting down"]))
            except OSError:
                pass
            return
        try:
            sock.settimeout(IO_TIMEOUT_SECONDS)
            self._run(conn)
        except _Stop:
            pass
        except Exception as exc:  # noqa: BLE001 - erro do próprio dublê: acusado, nunca impresso
            self._problem(conn, f"#{conn.seq}: erro interno do dublê: {type(exc).__name__}: {exc}")
            self._end(conn, "closed")
        finally:
            self._end(conn, "closed")
            _close_quietly(conn.sock)
            with self._cond:
                self._live.discard(conn)
                self._cond.notify_all()

    def _run(self, conn: _Conn) -> None:
        if conn.config.security == "tls":
            self._tls_stage(conn)
        failure = self._take_failure(conn, "connect", None)
        if failure is not None:
            self._apply_failure(conn, "connect", "<connect>", failure, None)
        else:
            self._reply(conn, "<connect>", 220, [f"{HOSTNAME} ESMTP"])
        while True:
            line, overlong = self._read_line(conn, AUTH_LINE_LIMIT)
            self._command(conn, line, overlong)

    def _take_failure(self, conn: _Conn, stage: str, argument: str | None) -> _Failure | None:
        with self._cond:
            if conn.gen != self._gen:
                return None
            context = StageContext(stage=stage, conn_seq=conn.seq, argument=argument,
                                   tls=conn.tls_active, authenticated=conn.authenticated,
                                   mail_from=conn.mail_from, rcpt_to=tuple(conn.rcpts))
            for failure in list(self._failures):
                if failure.stage != stage:
                    continue
                if failure.when is not None:
                    try:
                        matched = bool(failure.when(context))
                    except Exception as exc:  # noqa: BLE001 - vira erro de protocolo
                        self._problem(conn, f"#{conn.seq}: predicado when falhou na etapa {stage}: "
                                            f"{type(exc).__name__}: {exc}")
                        continue
                    if not matched:
                        continue
                if failure.times is not None:
                    failure.times -= 1
                    if failure.times == 0:
                        self._failures.remove(failure)
                conn.programmed = True
                return copy.copy(failure)
            return None

    def _apply_failure(self, conn: _Conn, stage: str, verb: str, failure: _Failure,
                       argument: str | None) -> None:
        """Aplica a falha programada; volta só quando a conexão continua (resposta SMTP que não é
        421)."""
        if failure.kind == "smtp":
            assert failure.code is not None
            if failure.raw is not None:
                self._reply(conn, verb, failure.code, raw=failure.raw)
            else:
                text = (failure.text if failure.text is not None
                        else _default_text(stage, failure.code, argument))
                self._reply(conn, verb, failure.code, text.split("\n"))
            if failure.code == 421 or stage == "tls":
                # Na etapa tls, a linha foi em claro no lugar do handshake: não há como seguir.
                self._end(conn, "closed")
                _close_quietly(conn.sock, fin=True)
                raise _Stop
            if stage == "connect":
                conn.no_service = True
            return
        self._record(conn, verb, None)
        if failure.kind == "reset":
            self._end(conn, "reset")
            _close_quietly(conn.sock, rst=True)
            raise _Stop
        if failure.kind == "close":
            self._end(conn, "closed")
            _close_quietly(conn.sock, fin=True)
            raise _Stop
        self._stall(conn, stage, failure)

    def _stall(self, conn: _Conn, stage: str, failure: _Failure) -> None:
        """`hang` e `drip`: até o cliente desistir (`abandoned`), `release_hangs`, `reset` ou
        `close` (a conexão é fechada, `closed`)."""
        with self._cond:
            self._hang_counter += 1
            ticket = self._hang_counter
        drip = failure.kind == "drip"
        chunks: Iterator[bytes] = iter(())
        if drip and stage == "connect":
            chunks = itertools.cycle(bytes([b]) for b in f"220-{HOSTNAME} drip\r\n".encode("ascii"))
        elif drip:
            code = _SUCCESS_CODE[stage]
            chunks = (f"{code}-{HOSTNAME} drip {n}\r\n".encode("ascii") for n in itertools.count(1))
        next_drip = monotonic()
        while True:
            with self._cond:
                closing = self._closing
                stale = conn.gen != self._gen
                released = ticket <= self._released_through
            if closing:
                if conn.tls_active or stage != "tls":
                    self._send(conn, _format_reply(
                        421, [f"4.3.2 {HOSTNAME} Service shutting down"]))
                self._end(conn, "closed")
                raise _Stop
            if stale or released:
                self._end(conn, "closed")
                _close_quietly(conn.sock, fin=True)
                raise _Stop
            if drip and monotonic() >= next_drip:
                if not self._send(conn, next(chunks)):
                    self._end(conn, "abandoned")
                    raise _Stop
                next_drip += failure.interval
            data = self._poll(conn, SOCKET_CHECK_SECONDS)
            if data is None:
                continue
            if not data:
                self._end(conn, "abandoned")
                raise _Stop
            conn.buffer += data
            conn.busy = False

    def _tls_stage(self, conn: _Conn) -> None:
        """O handshake TLS (no modo `tls`, antes da saudação; no `starttls`, depois do 220).

        O TLS roda sobre `ssl.SSLObject` e `MemoryBIO`, e o handshake lê o socket cru em modo
        **bloqueante** com `recv(65536)`: a leitura já está posta quando o alerta do cliente chega e
        leva o registro inteiro. Com `select` e leitura depois, ou com o `SSLSocket` (que lê o
        cabeçalho do registro e o corpo em duas chamadas), o RST que o cliente manda ao fechar com
        dados não lidos (o resto do voo do servidor) às vezes chega antes, e o Windows descarta o
        alerta junto (medido: 1 a 10% das recusas de certificado sem `tls_alert`). O prazo
        (`IDLE_TIMEOUT_SECONDS`), o `reset` e o `close` interrompem o handshake preso
        (`_abort_socket`).
        """
        failure = self._take_failure(conn, "tls", None)
        if failure is not None:
            self._apply_failure(conn, "tls", "<tls>", failure, None)
            return  # pragma: no cover - toda falha de rede termina a conexão
        context = self._tls.contexts[conn.config.cert]
        bio_in, bio_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = context.wrap_bio(bio_in, bio_out, server_side=True)
        with self._cond:
            conn.handshaking = True
            conn.busy = False
        timer = threading.Timer(IDLE_TIMEOUT_SECONDS, self._handshake_expired, args=(conn,))
        timer.daemon = True
        timer.name = "fake-smtp-handshake"
        timer.start()
        try:
            conn.sock.settimeout(None)
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pending = bio_out.read()
                    if pending:
                        conn.sock.sendall(pending)
                    raw = conn.sock.recv(65536)
                    if not raw:
                        raise ConnectionAbortedError("o cliente fechou durante o handshake")
                    bio_in.write(raw)
            pending = bio_out.read()
            if pending:
                conn.sock.sendall(pending)
            conn.sock.settimeout(IO_TIMEOUT_SECONDS)
        except (OSError, ValueError) as exc:
            pending = bio_out.read()  # o alerta do próprio dublê, se houver (melhor esforço)
            if pending:
                try:
                    conn.sock.sendall(pending)
                except (OSError, ValueError):
                    pass
            reason = getattr(exc, "reason", None)
            with self._cond:
                conn.handshaking = False
                expired = conn.handshake_expired
                interrupted = self._closing or conn.aborted
                conn.tls_failed = not expired and not interrupted
                conn.tls_error = f"{type(exc).__name__}:{reason}"
                if isinstance(reason, str) and "ALERT" in reason:
                    conn.tls_alert = reason
            self._record(conn, "<tls>", None)
            self._end(conn, "idle_timeout" if expired else "closed" if interrupted
                      else "tls_failed")
            raise _Stop from None
        finally:
            timer.cancel()
            with self._cond:
                conn.handshaking = False
                conn.busy = True
        with self._cond:
            conn.tls = tls
            conn.bio_in = bio_in
            conn.bio_out = bio_out
            conn.tls_active = True
            conn.tls_version = tls.version()
        self._record(conn, "<tls>", None)
        self._check_stop(conn)

    def _handshake_expired(self, conn: _Conn) -> None:
        with self._cond:
            if not conn.handshaking:
                return
            conn.handshake_expired = True
        _abort_socket(conn.sock)

    def _abort_handshakes(self) -> None:
        """Interrompe os handshakes presos das conexões antigas (`reset`) ou de todas (`close`)."""
        with self._cond:
            stuck = [c for c in self._live if c.handshaking
                     and (self._closing or c.gen != self._gen)]
            for conn in stuck:
                conn.aborted = True
        for conn in stuck:
            _abort_socket(conn.sock)

    # -- internos: comandos ----------------------------------------------------------------------

    def _command(self, conn: _Conn, line: bytes, overlong: bool) -> None:
        if overlong:
            self._reply(conn, "?", 500, ["5.5.6 Line too long"])
            self._flag(conn, "?", f"linha de comando com mais de {AUTH_LINE_LIMIT} octetos")
            return
        body = line[:-1]
        bare_lf = not body.endswith(b"\r")
        if not bare_lf:
            body = body[:-1]
        text = body.decode("utf-8", "replace")
        verb_raw, _, argument = text.partition(" ")
        upper = verb_raw.upper()
        verb = upper if upper in SUBSET_VERBS or upper in KNOWN_UNSUPPORTED_VERBS else "?"
        if bare_lf:
            self._flag(conn, verb, "LF solto no fim da linha de comando")
        if b"\r" in body:
            self._flag(conn, verb, "CR solto na linha de comando")
            argument = argument.replace("\r", "")
        if not body.isascii():
            self._flag(conn, verb, "byte acima de 0x7F na linha de comando")
        limit = AUTH_LINE_LIMIT if verb == "AUTH" else COMMAND_LINE_LIMIT
        if len(line) > limit:
            self._reply(conn, verb, 500, ["5.5.6 Line too long"])
            self._flag(conn, verb, f"linha de comando com mais de {limit} octetos")
            return
        self._check_secret(conn, verb, verb_raw, argument)
        if conn.no_service and verb != "QUIT":
            self._reply(conn, verb, 503, ["5.5.1 No SMTP service here"])
            return
        if verb in KNOWN_UNSUPPORTED_VERBS:
            self._reply(conn, verb, 502, ["5.5.1 Command not implemented"])
            self._flag(conn, verb, "verbo fora do subconjunto")
            return
        if verb == "?":
            self._reply(conn, verb, 500, ["5.5.2 Command unrecognized"])
            self._flag(conn, verb, "verbo desconhecido")
            return
        if conn.after_starttls and not conn.greeted and verb not in ("EHLO", "HELO", "QUIT"):
            self._reply(conn, verb, 503, ["5.5.1 Error: send EHLO first"])
            self._flag(conn, verb, "comando fora de ordem: depois do STARTTLS sem novo EHLO")
            return
        getattr(self, f"_cmd_{verb.lower()}")(conn, verb, argument)

    def _check_secret(self, conn: _Conn, verb: str, verb_raw: str, argument: str) -> None:
        creds = conn.config.credentials
        if creds is None:
            return
        checked = f"{verb_raw} {argument}"
        if verb == "AUTH":
            # Os argumentos da troca AUTH (a resposta inicial) ficam de fora.
            parts = argument.split(" ")
            checked = " ".join([verb_raw, parts[0], *parts[2:]])
        if any(form in checked for form in creds.secret_forms()):
            self._flag(conn, verb, "segredo conhecido no argumento do comando")

    def _require_greeting(self, conn: _Conn, verb: str) -> bool:
        if conn.greeted:
            return True
        self._reply(conn, verb, 503, ["5.5.1 Error: send EHLO first"])
        self._flag(conn, verb, "comando fora de ordem: antes do EHLO")
        return False

    def _auth_offered(self, conn: _Conn) -> bool:
        config = conn.config
        return (config.credentials is not None and bool(config.advertise_auth)
                and (conn.tls_active or config.security == "none"))

    def _cmd_ehlo(self, conn: _Conn, verb: str, argument: str) -> None:
        name = argument.strip()
        if not name:
            self._reply(conn, verb, 501, ["5.5.4 Syntax: EHLO hostname"])
            self._flag(conn, verb, "EHLO sem domínio")
            return
        failure = self._take_failure(conn, "ehlo", name)
        if failure is not None:
            self._apply_failure(conn, "ehlo", verb, failure, name)
            return
        config = conn.config
        with self._cond:
            conn.ehlo_name = self._scrub(name)
            conn.greeted = True
            conn.esmtp = True
            conn.reset_transaction()
        lines = [HOSTNAME, f"SIZE {SIZE_LIMIT}", "8BITMIME", "ENHANCEDSTATUSCODES"]
        if config.security == "starttls" and config.advertise_starttls and not conn.tls_active:
            lines.append("STARTTLS")
        if self._auth_offered(conn):
            lines.append("AUTH " + " ".join(config.advertise_auth))
        self._reply(conn, verb, 250, lines)

    def _cmd_helo(self, conn: _Conn, verb: str, argument: str) -> None:
        name = argument.strip()
        if not name:
            self._reply(conn, verb, 501, ["5.5.4 Syntax: HELO hostname"])
            self._flag(conn, verb, "HELO sem domínio")
            return
        with self._cond:
            conn.ehlo_name = self._scrub(name)
            conn.greeted = True
            conn.esmtp = False
            conn.reset_transaction()
        self._reply(conn, verb, 250, [HOSTNAME])

    def _cmd_starttls(self, conn: _Conn, verb: str, argument: str) -> None:
        config = conn.config
        if conn.tls_active:
            self._reply(conn, verb, 503, ["5.5.1 TLS already active"])
            self._flag(conn, verb, "STARTTLS com TLS já ativo")
            return
        if config.security != "starttls" or not config.advertise_starttls:
            self._reply(conn, verb, 502, ["5.5.1 Command not implemented"])
            self._flag(conn, verb, "STARTTLS não anunciado")
            return
        if not self._require_greeting(conn, verb):
            return
        if argument.strip():
            self._reply(conn, verb, 501, ["5.5.4 Syntax: STARTTLS"])
            self._flag(conn, verb, "STARTTLS com argumento")
            return
        failure = self._take_failure(conn, "starttls", None)
        if failure is not None:
            self._apply_failure(conn, "starttls", verb, failure, None)
            return
        self._reply(conn, verb, 220, ["2.0.0 Ready to start TLS"])
        if conn.buffer:
            self._flag(conn, verb, "bytes em claro depois do STARTTLS")
            del conn.buffer[:]
        self._tls_stage(conn)
        with self._cond:
            conn.greeted = False
            conn.esmtp = False
            conn.after_starttls = True
            conn.authenticated = False
            conn.reset_transaction()

    def _cmd_auth(self, conn: _Conn, verb: str, argument: str) -> None:
        config = conn.config
        parts = argument.split()
        mechanism = parts[0].upper() if parts else ""
        if not self._require_greeting(conn, verb):
            return
        if conn.mail_from is not None:
            self._reply(conn, verb, 503, ["5.5.1 Error: MAIL transaction in progress"])
            self._flag(conn, verb, "comando fora de ordem: AUTH durante a transação")
            return
        if conn.authenticated:
            self._reply(conn, verb, 503, ["5.5.1 Error: already authenticated"])
            self._flag(conn, verb, "comando fora de ordem: AUTH depois de um AUTH aceito")
            return
        if config.security != "none" and not conn.tls_active:
            self._reply(conn, verb, 530, ["5.7.0 Must issue a STARTTLS command first"])
            self._flag(conn, verb, "AUTH sem TLS fora do modo none")
            return
        if not self._auth_offered(conn):
            self._reply(conn, verb, 502, ["5.5.1 Command not implemented"])
            self._flag(conn, verb, "AUTH não anunciado")
            return
        if not mechanism or len(parts) > 2:
            self._reply(conn, verb, 501, ["5.5.4 Syntax: AUTH mechanism [initial-response]"])
            self._flag(conn, verb, "AUTH com sintaxe inválida")
            return
        if mechanism not in config.advertise_auth or mechanism == "CRAM-MD5":
            self._reply(conn, verb, 504, ["5.5.4 Unrecognized authentication type"])
            self._flag(conn, verb, "AUTH com mecanismo não anunciado ou não implementado")
            return
        failure = self._take_failure(conn, "auth", mechanism)
        if failure is not None:
            self._apply_failure(conn, "auth", verb, failure, mechanism)
            return
        initial = parts[1] if len(parts) > 1 else None
        tls = conn.tls_active
        creds = config.credentials
        assert creds is not None
        state = {"verb": verb}

        def challenge(text: str) -> bytes:
            self._reply(conn, state["verb"], 334, [text])
            state["verb"] = "<auth-response>"
            return self._auth_response(conn)

        def decode(text: str) -> bytes:
            value = _decode_b64(text)
            if value is None:
                raise _BadBase64
            return value

        def finish(code: int, text: str, user_ok: bool = False, password_ok: bool = False,
                   problem: str | None = None) -> None:
            # A tentativa entra no registro antes da resposta (quem a recebe já a encontra).
            with self._cond:
                conn.auth.append(AuthAttempt(mechanism=mechanism,
                                             initial_response=initial is not None,
                                             user_ok=user_ok, password_ok=password_ok, tls=tls,
                                             code=code))
                if code == 235:
                    conn.authenticated = True
            if problem is not None:
                self._flag(conn, verb, problem)
            self._reply(conn, state["verb"], code, [text])

        expected_user = creds.username.encode("utf-8")
        expected_password = creds.password.encode("utf-8")
        try:
            if mechanism == "PLAIN":
                message = decode(initial) if initial is not None else challenge("")
                pieces = message.split(b"\0")
                if len(pieces) != 3:
                    finish(501, "5.5.2 Invalid PLAIN message",
                           problem="mensagem PLAIN malformada")
                    return
                authzid, authcid, password = pieces
                user_ok = authcid == expected_user
                authz_ok = authzid in (b"", authcid)
            else:  # LOGIN
                user = decode(initial) if initial is not None else challenge(_LOGIN_USER_CHALLENGE)
                password = challenge(_LOGIN_PASSWORD_CHALLENGE)
                user_ok = user == expected_user
                authz_ok = True
        except _Cancel:
            finish(501, "5.0.0 Authentication cancelled")
            return
        except _BadBase64:
            finish(501, "5.5.2 Cannot decode response", problem="base64 inválido na troca AUTH")
            return
        password_ok = password == expected_password
        if user_ok and password_ok and authz_ok:
            finish(235, "2.7.0 Authentication successful", True, True)
        else:
            finish(535, "5.7.8 Authentication credentials invalid", user_ok, password_ok)

    def _auth_response(self, conn: _Conn) -> bytes:
        line, overlong = self._read_line(conn, AUTH_LINE_LIMIT)
        if overlong:
            self._flag(conn, "<auth-response>", f"linha com mais de {AUTH_LINE_LIMIT} octetos")
            raise _BadBase64
        body = line[:-1]
        if body.endswith(b"\r"):
            body = body[:-1]
        else:
            self._flag(conn, "<auth-response>", "LF solto no fim da linha")
        text = body.decode("ascii", "replace")
        if text == "*":
            raise _Cancel
        value = _decode_b64(text)
        if value is None:
            raise _BadBase64
        return value

    def _cmd_mail(self, conn: _Conn, verb: str, argument: str) -> None:
        config = conn.config
        if not self._require_greeting(conn, verb):
            return
        if conn.mail_from is not None:
            self._reply(conn, verb, 503, ["5.5.1 Error: nested MAIL command"])
            self._flag(conn, verb, "comando fora de ordem: MAIL com transação aberta")
            return
        if config.credentials is not None and not conn.authenticated:
            self._reply(conn, verb, 530, ["5.7.0 Authentication required"])
            self._flag(conn, verb, "comando fora de ordem: MAIL sem AUTH havendo credenciais")
            return
        match = _MAIL_RE.fullmatch(argument)
        if match is None:
            self._reply(conn, verb, 501, ["5.5.4 Syntax: MAIL FROM:<address>"])
            self._flag(conn, verb, "MAIL com sintaxe inválida")
            return
        address, params = match.group(1), tuple(match.group(2).split())
        if params and not conn.esmtp:
            self._reply(conn, verb, 555, ["5.5.4 Unsupported option"])
            self._flag(conn, verb, "parâmetro no MAIL sem EHLO")
            return
        body_8bit = False
        for param in params:
            key, has_value, value = param.partition("=")
            key = key.upper()
            if key == "SIZE":
                if not has_value or not value.isascii() or not value.isdigit() or len(value) > 20:
                    self._reply(conn, verb, 501, ["5.5.4 Bad SIZE parameter"])
                    self._flag(conn, verb, "SIZE malformado")
                    return
                if int(value) > SIZE_LIMIT:
                    self._reply(conn, verb, 552,
                                ["5.3.4 Message size exceeds fixed maximum message size"])
                    return
            elif key == "BODY":
                if value.upper() not in ("7BIT", "8BITMIME"):
                    self._reply(conn, verb, 501, ["5.5.4 Bad BODY parameter"])
                    self._flag(conn, verb, "BODY com valor desconhecido")
                    return
                body_8bit = value.upper() == "8BITMIME"
            elif key == "SMTPUTF8":
                self._reply(conn, verb, 555, ["5.5.4 Unsupported option: SMTPUTF8"])
                self._flag(conn, verb, "parâmetro SMTPUTF8 no MAIL (o dublê não anuncia SMTPUTF8)")
                return
            else:
                self._reply(conn, verb, 555, ["5.5.4 Unsupported option"])
                self._flag(conn, verb, "parâmetro desconhecido no MAIL")
                return
        failure = self._take_failure(conn, "mail", address)
        if failure is not None:
            self._apply_failure(conn, "mail", verb, failure, address)
            return
        with self._cond:
            conn.mail_from = address
            conn.mail_params = params
            conn.body_8bit = body_8bit
            conn.rcpts = []
        self._reply(conn, verb, 250, ["2.1.0 Ok"])

    def _cmd_rcpt(self, conn: _Conn, verb: str, argument: str) -> None:
        if not self._require_greeting(conn, verb):
            return
        if conn.mail_from is None:
            self._reply(conn, verb, 503, ["5.5.1 Error: need MAIL command"])
            self._flag(conn, verb, "comando fora de ordem: RCPT sem MAIL")
            return
        match = _RCPT_RE.fullmatch(argument)
        if match is None or not match.group(1):
            self._reply(conn, verb, 501, ["5.5.4 Syntax: RCPT TO:<address>"])
            self._flag(conn, verb, "RCPT com sintaxe inválida")
            return
        address = match.group(1)
        if match.group(2).split():
            self._reply(conn, verb, 555, ["5.5.4 Unsupported option"])
            self._flag(conn, verb, "parâmetro no RCPT")
            return
        failure = self._take_failure(conn, "rcpt", address)
        if failure is not None:
            self._apply_failure(conn, "rcpt", verb, failure, address)
            return
        with self._cond:
            conn.rcpts.append(address)
        self._reply(conn, verb, 250, ["2.1.5 Ok"])

    def _cmd_data(self, conn: _Conn, verb: str, argument: str) -> None:
        if not self._require_greeting(conn, verb):
            return
        if argument.strip():
            self._reply(conn, verb, 501, ["5.5.4 Syntax: DATA"])
            self._flag(conn, verb, "DATA com argumento")
            return
        if conn.mail_from is None or not conn.rcpts:
            self._reply(conn, verb, 503, ["5.5.1 Error: need RCPT command"])
            self._flag(conn, verb, "comando fora de ordem: DATA sem RCPT aceito")
            return
        failure = self._take_failure(conn, "data", None)
        if failure is not None:
            self._apply_failure(conn, "data", verb, failure, None)
            return
        self._reply(conn, verb, 354, ["End data with <CR><LF>.<CR><LF>"])
        started = monotonic()
        raw, too_big = self._read_data(conn)
        ended = monotonic()
        if too_big:
            with self._cond:
                conn.reset_transaction()
            self._reply(conn, "<data-end>", 552,
                        ["5.3.4 Message size exceeds fixed maximum message size"])
            return
        message, headers, text = self._parse_message(conn, raw)
        failure = self._take_failure(conn, "data_end", None)
        if failure is not None:
            # Resposta programada ou falha de rede: a mensagem só é capturada com `after_accept`
            # (o dublê a aceitou antes de falhar), e então com `acknowledged=False`.
            if failure.after_accept:
                self._capture(conn, raw, message, headers, text, started, ended,
                              acknowledged=False)
            with self._cond:
                conn.reset_transaction()
            self._apply_failure(conn, "data_end", "<data-end>", failure, None)
            return
        captured = self._capture(conn, raw, message, headers, text, started, ended,
                                 acknowledged=True)
        with self._cond:
            conn.reset_transaction()
        self._reply(conn, "<data-end>", 250, [f"2.0.0 Ok: queued as Q{captured.seq}"])

    def _read_data(self, conn: _Conn) -> tuple[bytes, bool]:
        """O conteúdo do DATA até `CRLF.CRLF`, com o dot-stuffing desfeito, e se passou do
        `SIZE_LIMIT`. Os desvios são anotados uma vez por mensagem."""
        chunks: list[bytes] = []
        size = 0
        too_big = False
        flagged: set[str] = set()

        def flag(text: str) -> None:
            if text not in flagged:
                flagged.add(text)
                self._flag(conn, "<data>", text)

        while True:
            line, _ = self._read_line(conn, 1 << 62)
            if line == b".\r\n":
                break
            if line.endswith(b"\r\n"):
                content = line[:-2]
            else:
                content = line[:-1]
                flag("LF solto no DATA")
            if b"\r" in content:
                flag("CR solto no DATA")
            if line.startswith(b"."):
                if not line.startswith(b".."):
                    flag("linha que começa com ponto sem o ponto dobrado")
                line = line[1:]
            if len(line) > TEXT_LINE_LIMIT:
                flag(f"linha de texto com mais de {TEXT_LINE_LIMIT} octetos")
            if not conn.body_8bit and not line.isascii():
                flag("byte acima de 0x7F no DATA sem BODY=8BITMIME")
            size += len(line)
            if size > SIZE_LIMIT:
                too_big = True
            else:
                chunks.append(line)
        return b"".join(chunks), too_big

    def _parse_message(self, conn: _Conn,
                       raw: bytes) -> tuple[EmailMessage, tuple[tuple[str, str], ...], str | None]:
        message = BytesParser(policy=default_policy).parsebytes(raw)
        assert isinstance(message, EmailMessage)
        try:
            headers = tuple((name, str(value)) for name, value in message.items())
        except Exception as exc:  # noqa: BLE001 - cabeçalho ilegível vira erro de protocolo
            self._flag(conn, "<data>", f"cabeçalho ilegível: {type(exc).__name__}")
            headers = ()
        text: str | None = None
        body = message.get_body(("plain",))
        if body is not None:
            try:
                text = str(body.get_content()).replace("\r\n", "\n")
            except Exception as exc:  # noqa: BLE001 - corpo ilegível vira erro de protocolo
                self._flag(conn, "<data>", f"corpo text/plain ilegível: {type(exc).__name__}")
        for part in message.walk():
            if part.is_multipart():
                continue
            if str(part.get("Content-Transfer-Encoding", "")).strip().lower() != "quoted-printable":
                continue
            payload = part.get_payload()
            if isinstance(payload, str) and any(
                    len(piece) > QP_LINE_LIMIT for piece in _LINE_BREAK_RE.split(payload)):
                self._flag(conn, "<data>", f"linha em quoted-printable com mais de {QP_LINE_LIMIT} "
                                           "caracteres")
        return message, headers, text

    def _capture(self, conn: _Conn, raw: bytes, message: EmailMessage,
                 headers: tuple[tuple[str, str], ...], text: str | None, started: float,
                 ended: float, *, acknowledged: bool) -> CapturedMessage:
        with self._cond:
            captured = CapturedMessage(
                seq=next(self._msg_seq), conn_seq=conn.seq, mail_from=conn.mail_from or "",
                mail_params=conn.mail_params, rcpt_to=tuple(conn.rcpts), tls=conn.tls_active,
                tls_version=conn.tls_version, authenticated=conn.authenticated, raw=raw,
                message=message, headers=headers, text=text, acknowledged=acknowledged,
                started=started, ended=ended, clock_at=self._clock.now())
            if conn.gen == self._gen:
                self._messages.append(captured)
            self._cond.notify_all()
            return captured

    def _cmd_rset(self, conn: _Conn, verb: str, argument: str) -> None:
        if argument.strip():
            self._reply(conn, verb, 501, ["5.5.4 Syntax: RSET"])
            self._flag(conn, verb, "RSET com argumento")
            return
        failure = self._take_failure(conn, "rset", None)
        if failure is not None:
            self._apply_failure(conn, "rset", verb, failure, None)
            return
        with self._cond:
            conn.reset_transaction()
        self._reply(conn, verb, 250, ["2.0.0 Ok"])

    def _cmd_noop(self, conn: _Conn, verb: str, argument: str) -> None:
        self._reply(conn, verb, 250, ["2.0.0 Ok"])

    def _cmd_quit(self, conn: _Conn, verb: str, argument: str) -> None:
        with self._cond:
            conn.quit = True
        if argument.strip():
            self._reply(conn, verb, 501, ["5.5.4 Syntax: QUIT"])
            self._flag(conn, verb, "QUIT com argumento")
            return
        failure = self._take_failure(conn, "quit", None)
        if failure is not None:
            self._apply_failure(conn, "quit", verb, failure, None)
            return
        self._reply(conn, verb, 221, ["2.0.0 Bye"], then_end="quit")
        raise _Stop


# ---------------------------------------------------------------------------------------------
# Dublê da sessão
# ---------------------------------------------------------------------------------------------

_SESSION: FakeSmtp | None = None
_SESSION_LOCK = threading.Lock()


def session_smtp() -> FakeSmtp:
    """O dublê único da sessão de testes (criado na 1ª chamada, fechado na saída do processo).

    A fixture `smtp` e os helpers sem fixture usam este mesmo servidor; a fixture faz
    `reset(clock=clock)` a cada teste.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = FakeSmtp()
            atexit.register(_SESSION.close)
        return _SESSION
