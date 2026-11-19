"""Configuração do servidor, lida do ambiente.

Regras de `Settings.from_env`:
- `CQ_DATABASE_URL` é obrigatória; vazia é erro;
- qualquer opcional vazio vale como ausente (o padrão);
- `CQ_ADMIN_TOKEN`, se presente, tem pelo menos 32 caracteres;
- inteiros só por `int()`, maiores que zero; números de ponto flutuante finitos e maiores que zero.

Nenhuma mensagem de erro nem o `repr` mostra o token inicial ou a senha da URL do banco
(nenhum segredo sai do processo).

`BotSettings` reúne as configurações dos bots, à parte do `Settings`:
a URL da Bot API, os tempos da posse e da consulta e a chave-mestra dos tokens (`CQ_MASTER_KEY`, fora
do `repr` e do `str`). As regras de `from_env` e da validação na construção estão em `BotSettings`
e nas suas mensagens fixas.

`MailSettings` reúne as configurações do servidor de e-mail do worker
(`CQ_SMTP_*`), à parte do `Settings` e do `BotSettings`; a senha fica fora do `repr` e
do `str`. `check_mail_timeout` confere a invariante entre o prazo total da conversa SMTP e a posse das
tarefas. As regras e as mensagens exatas estão nas constantes `_MSG_*`; sem `CQ_SMTP_HOST`, `from_env`
devolve `None` (e-mail desligado) e ignora as demais `CQ_SMTP_*`.

Entrega, motor e repositórios: três dataclasses próprias, fora do
`Settings` e do `BotSettings`:
- `EngineSettings` (`CQ_ENGINE_URL`, `CQ_ENGINE_TIMEOUT_SECONDS`), com `check_engine_timeout`;
- `RepoSettings` (`CQ_REPO_ALLOWED_HOSTS`, `CQ_REPOS_ROOT`, `CQ_REPO_FETCH_TIMEOUT_SECONDS`,
  `CQ_REPO_MAX_BYTES`, `CQ_REPO_MAX_FILES`), com `check_fetch_timeout`;
- `DeliverySettings` (`CQ_TELEGRAM_FORMAT`, `CQ_TELEGRAM_MAX_BUTTONS`, `CQ_SEND_CHAT_INTERVAL_SECONDS`,
  `CQ_SEND_BOT_PER_SECOND`, `CQ_TELEGRAM_PHOTO_MAX_BYTES`, `CQ_FREE_MESSAGE_REPLY_SECONDS`).
Nos três `from_env`, uma variável vazia vale como ausente (o padrão). As mensagens de `ConfigError` são
fixas, sem o valor (nem na cadeia de exceções: as recusas saem com `from None`), no molde das
outras: os números com as frases de `_positive_int` e `_positive_float`, a faixa dos botões como a de
`CQ_POLL_TIMEOUT_SECONDS` e a enumeração do dialeto como a de `CQ_SMTP_SECURITY`.
"""

from __future__ import annotations

import base64
import ipaddress
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import NoReturn, TypeVar
from urllib.parse import urlsplit, urlunsplit

import email_validator

MIN_ADMIN_TOKEN_LENGTH = 32

_HIDDEN = "***"


class ConfigError(Exception):
    """Configuração inválida ou ausente."""


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value else None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} deve ser um número inteiro maior que zero.") from None
    if value <= 0:
        raise ConfigError(f"{name} deve ser um número inteiro maior que zero.")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _optional(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} deve ser um número finito maior que zero.") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} deve ser um número finito maior que zero.")
    return value


# `password` e `sslpassword`; valor entre aspas simples (com escapes) ou sem aspas (com `\ `).
_KV_PASSWORD = re.compile(r"(\w*password\s*=\s*)('(?:[^'\\]|\\.)*'?|(?:\\.|\S)*)", re.IGNORECASE)
_QUERY_PASSWORD = re.compile(r"(?i)((?:^|&)\w*password=)[^&#]*")


def mask_conninfo(conninfo: str) -> str:
    """A URL (ou `chave=valor`) do banco com a senha trocada por `***`, para exibição.

    Na dúvida (texto que não se consegue interpretar), não mostra nada da URL.
    """
    try:
        if "://" in conninfo:
            parts = urlsplit(conninfo)
            netloc = parts.netloc
            if "@" in netloc:
                userinfo, hostinfo = netloc.rsplit("@", 1)
                if ":" in userinfo:
                    userinfo = userinfo.split(":", 1)[0] + ":" + _HIDDEN
                netloc = f"{userinfo}@{hostinfo}"
            query = _QUERY_PASSWORD.sub(lambda m: m.group(1) + _HIDDEN, parts.query)
            return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
        return _KV_PASSWORD.sub(lambda m: m.group(1) + _HIDDEN, conninfo)
    except Exception:  # noqa: BLE001 - exibição só; nunca derruba nem mostra o valor
        return "<oculto>"


@dataclass(frozen=True, repr=False)
class Settings:
    database_url: str
    admin_token: str | None = None
    lease_seconds: float = 60
    max_attempts: int = 5
    backoff_base_seconds: float = 2
    backoff_max_seconds: float = 300
    poll_interval_seconds: float = 1
    worker_id: str | None = None
    console_output: str = "-"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        database_url = env.get("CQ_DATABASE_URL")
        if not database_url:
            raise ConfigError("CQ_DATABASE_URL é obrigatória.")
        admin_token = _optional(env, "CQ_ADMIN_TOKEN")
        if admin_token is not None and len(admin_token) < MIN_ADMIN_TOKEN_LENGTH:
            raise ConfigError(
                f"CQ_ADMIN_TOKEN deve ter pelo menos {MIN_ADMIN_TOKEN_LENGTH} caracteres.")
        defaults = cls(database_url=database_url)
        return cls(
            database_url=database_url,
            admin_token=admin_token,
            lease_seconds=_positive_float(env, "CQ_LEASE_SECONDS", defaults.lease_seconds),
            max_attempts=_positive_int(env, "CQ_MAX_ATTEMPTS", defaults.max_attempts),
            backoff_base_seconds=_positive_float(env, "CQ_BACKOFF_BASE_SECONDS",
                                                 defaults.backoff_base_seconds),
            backoff_max_seconds=_positive_float(env, "CQ_BACKOFF_MAX_SECONDS",
                                                defaults.backoff_max_seconds),
            poll_interval_seconds=_positive_float(env, "CQ_POLL_INTERVAL_SECONDS",
                                                  defaults.poll_interval_seconds),
            worker_id=_optional(env, "CQ_WORKER_ID"),
            console_output=_optional(env, "CQ_CONSOLE_OUTPUT") or defaults.console_output,
        )

    def __repr__(self) -> str:
        token = None if self.admin_token is None else "<definido>"
        return (
            f"Settings(database_url={mask_conninfo(self.database_url)!r}, admin_token={token}, "
            f"lease_seconds={self.lease_seconds!r}, max_attempts={self.max_attempts!r}, "
            f"backoff_base_seconds={self.backoff_base_seconds!r}, "
            f"backoff_max_seconds={self.backoff_max_seconds!r}, "
            f"poll_interval_seconds={self.poll_interval_seconds!r}, worker_id={self.worker_id!r}, "
            f"console_output={self.console_output!r})"
        )

    __str__ = __repr__


def redaction_secrets(settings: Settings | None) -> tuple[str, ...]:
    """Os valores literais trocados por `[segredo]` na redação do texto livre (nenhum segredo sai): o
    token inicial configurado, quando há. A mesma regra do `ApiContext.secrets` da API; os tratadores do
    worker a montam a partir do `Settings` que recebem na raiz de composição (como o `Outbox` e a
    `TaskQueue`)."""
    token = None if settings is None else getattr(settings, "admin_token", None)
    return tuple(value for value in (token,) if value)


# --- configurações dos bots -----------------------------------------------------------------------

POLL_TIMEOUT_MIN = 1
POLL_TIMEOUT_MAX = 50
MASTER_KEY_LENGTH = 43  # 32 bytes em base64url, sem "="
MASTER_KEY_BYTES = 32

# Mensagens exatas; nenhuma leva o valor recebido.
_MSG_POLL = "CQ_POLL_TIMEOUT_SECONDS deve ser um número inteiro de 1 a 50."
_MSG_URL = ("CQ_TELEGRAM_API_URL deve usar https:// com host (http:// só em 127.0.0.1, localhost ou "
            "[::1]), sem credenciais, consulta, fragmento nem barra final.")
_MSG_KEY = 'CQ_MASTER_KEY deve ter 32 bytes aleatórios em base64url, sem "=" (43 caracteres).'
_MSG_LEASE = ("CQ_BOT_LEASE_SECONDS menos CQ_BOT_RENEW_SECONDS deve ser pelo menos "
              "CQ_POLL_TIMEOUT_SECONDS mais CQ_TELEGRAM_TIMEOUT_SECONDS.")
_MSG_RECONCILE = "CQ_BOT_RECONCILE_SECONDS não pode ser maior que CQ_BOT_RENEW_SECONDS."

_MASTER_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_POLL_PATTERN = re.compile(r"[0-9]+")
# Só ASCII visível: um espaço ou controle que o `urlsplit` descartaria em silêncio já é recusa.
_URL_CHARS = re.compile(r"[\x21-\x7e]+")
_HOST_PATTERN = re.compile(r"[a-z0-9._-]+")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _number_message(name: str) -> str:
    return f"{name} deve ser um número finito maior que zero."


def master_key_bytes(value: object) -> bytes | None:
    """Os 32 bytes da chave-mestra, ou `None` se ela não está na forma exigida.

    A forma é a canônica: 43 caracteres base64url sem `=` (fullmatch) que decodificam para 32 bytes e
    recodificam para o mesmo texto. O `base64` da biblioteca padrão não é estrito (aceita `+/` e
    ignora os bits finais), por isso a recodificação.
    """
    if not isinstance(value, str) or _MASTER_KEY_PATTERN.fullmatch(value) is None:
        return None
    raw = base64.urlsafe_b64decode(value + "=")
    if len(raw) != MASTER_KEY_BYTES:
        return None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        return None
    return raw


def _valid_api_url(url: object) -> bool:
    """`https://` com host, ou `http://` só com host de loopback; sem credenciais, consulta,
    fragmento nem barra final."""
    if not isinstance(url, str) or _URL_CHARS.fullmatch(url) is None:
        return False
    if url.startswith("https://"):
        secure = True
    elif url.startswith("http://"):
        secure = False
    else:
        return False
    if "?" in url or "#" in url or url.endswith("/"):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - porta inválida levanta ValueError
    except ValueError:
        return False
    if not parts.netloc or "@" in parts.netloc or not host:
        return False
    if parts.netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
    elif _HOST_PATTERN.fullmatch(host) is None:
        return False
    return secure or host in _LOOPBACK_HOSTS


def _is_positive_finite(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _strict_poll_timeout(env: Mapping[str, str], name: str, default: int) -> int:
    """Inteiro decimal ASCII de 1 a 50 (sem sinal, espaços, expoente nem ponto)."""
    raw = _optional(env, name)
    if raw is None:
        return default
    if _POLL_PATTERN.fullmatch(raw) is None:
        raise ConfigError(_MSG_POLL)
    value = int(raw)
    if not POLL_TIMEOUT_MIN <= value <= POLL_TIMEOUT_MAX:
        raise ConfigError(_MSG_POLL)
    return value


@dataclass(frozen=True)
class BotSettings:
    """Configurações dos bots; a chave-mestra fica fora do `repr`.

    A validação roda na construção (`__post_init__`), inclusive a direta e a de
    `dataclasses.replace`, com os nomes das variáveis nas mensagens: primeiro cada campo (URL,
    tempos, consulta, chave), depois os invariantes entre campos, que só se definem sobre números
    válidos:
    - posse: `lease_seconds − renew_seconds ≥ poll_timeout_seconds + telegram_timeout_seconds`
      (a posse cobre uma consulta inteira mesmo com uma renovação perdida);
    - reconciliação: `reconcile_seconds ≤ renew_seconds`.
    """

    telegram_api_url: str = "https://api.telegram.org"
    lease_seconds: float = 60
    renew_seconds: float = 20
    reconcile_seconds: float = 10
    poll_timeout_seconds: int = 25
    telegram_timeout_seconds: float = 10
    master_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not _valid_api_url(self.telegram_api_url):
            raise ConfigError(_MSG_URL)
        for name, value in (("CQ_BOT_LEASE_SECONDS", self.lease_seconds),
                            ("CQ_BOT_RENEW_SECONDS", self.renew_seconds),
                            ("CQ_BOT_RECONCILE_SECONDS", self.reconcile_seconds)):
            if not _is_positive_finite(value):
                raise ConfigError(_number_message(name))
        poll = self.poll_timeout_seconds
        if (not isinstance(poll, int) or isinstance(poll, bool)
                or not POLL_TIMEOUT_MIN <= poll <= POLL_TIMEOUT_MAX):
            raise ConfigError(_MSG_POLL)
        if not _is_positive_finite(self.telegram_timeout_seconds):
            raise ConfigError(_number_message("CQ_TELEGRAM_TIMEOUT_SECONDS"))
        if self.master_key is not None and master_key_bytes(self.master_key) is None:
            raise ConfigError(_MSG_KEY)
        if self.lease_seconds - self.renew_seconds < poll + self.telegram_timeout_seconds:
            raise ConfigError(_MSG_LEASE)
        if self.reconcile_seconds > self.renew_seconds:
            raise ConfigError(_MSG_RECONCILE)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BotSettings:
        """Lê as variáveis dos bots de `env` (e não do ambiente do processo); vazio vale como
        ausente. `ConfigError` com a mensagem fixa, sem o valor."""
        defaults = cls()
        return cls(
            telegram_api_url=_optional(env, "CQ_TELEGRAM_API_URL") or defaults.telegram_api_url,
            lease_seconds=_positive_float(env, "CQ_BOT_LEASE_SECONDS", defaults.lease_seconds),
            renew_seconds=_positive_float(env, "CQ_BOT_RENEW_SECONDS", defaults.renew_seconds),
            reconcile_seconds=_positive_float(env, "CQ_BOT_RECONCILE_SECONDS",
                                              defaults.reconcile_seconds),
            poll_timeout_seconds=_strict_poll_timeout(env, "CQ_POLL_TIMEOUT_SECONDS",
                                                      defaults.poll_timeout_seconds),
            telegram_timeout_seconds=_positive_float(env, "CQ_TELEGRAM_TIMEOUT_SECONDS",
                                                     defaults.telegram_timeout_seconds),
            master_key=_optional(env, "CQ_MASTER_KEY"),
        )


# --- configurações do e-mail ----------------------------------------------------------------------

SMTP_SECURITY_MODES = ("starttls", "tls", "none")
DEFAULT_SMTP_SECURITY = "starttls"
# Porta padrão de cada modo, só quando `CQ_SMTP_PORT` não vem.
SMTP_DEFAULT_PORTS = {"starttls": 587, "tls": 465, "none": 25}
SMTP_PORT_MIN = 1
SMTP_PORT_MAX = 65535
DEFAULT_SMTP_TIMEOUT_SECONDS = 10
# Folga, em segundos, entre o prazo total da conversa SMTP e a posse das tarefas: o trabalho
# no banco antes e depois do envio cabe nela.
MAIL_LEASE_MARGIN_SECONDS = 15

# Mensagens exatas; nenhuma leva o valor recebido.
_MSG_SMTP_PORT = "CQ_SMTP_PORT deve ser um número inteiro de 1 a 65535."
_MSG_SMTP_SECURITY = "CQ_SMTP_SECURITY deve ser starttls, tls ou none."
_MSG_SMTP_PLAINTEXT = ("CQ_SMTP_SECURITY=none só é aceito com CQ_SMTP_HOST em 127.0.0.1, localhost "
                       "ou ::1.")
_MSG_SMTP_HOST = "CQ_SMTP_HOST deve ser um nome de host ou endereço IP, sem espaços."
_MSG_SMTP_PAIR = "CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD devem vir juntos."
_MSG_SMTP_ASCII = ("CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD só aceitam caracteres ASCII visíveis e "
                   "espaço.")
_MSG_SMTP_FROM = "CQ_SMTP_FROM deve ser um endereço de e-mail ASCII válido."
_MSG_SMTP_TIMEOUT = "CQ_SMTP_TIMEOUT_SECONDS deve ser um número finito maior que zero."
_MSG_SMTP_LEASE = "CQ_SMTP_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."

# Porta: inteiro decimal ASCII (sem sinal, espaços, expoente nem ponto); a faixa vem depois.
_SMTP_PORT_PATTERN = re.compile(r"[0-9]{1,5}")
# Nome de host: letras e dígitos ASCII, `.`, `_` e `-`, sem espaço; um IP (v4 ou v6) passa pelo
# `ipaddress`.
_SMTP_HOST_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}")
# Usuário e senha: ASCII 0x20–0x7E (visíveis e espaço; sem NUL, DEL nem outros controles).
_SMTP_CREDENTIAL = re.compile(r"[\x20-\x7e]+")


def _valid_smtp_host(host: object) -> bool:
    """Nome de host ou endereço IP, sem espaço."""
    if not isinstance(host, str) or not host:
        return False
    if _SMTP_HOST_NAME.fullmatch(host) is not None:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _valid_smtp_port(port: object) -> bool:
    return (isinstance(port, int) and not isinstance(port, bool)
            and SMTP_PORT_MIN <= port <= SMTP_PORT_MAX)


def _valid_sender(sender: object) -> bool:
    """Endereço ASCII válido pelo `email_validator`, sem SMTPUTF8, sem DNS e sem nome de exibição
    (as opções do cadastro de pessoas). O validador recusa nomes de uso especial (ex.:
    `.test`)."""
    if not isinstance(sender, str) or not sender or not sender.isascii():
        return False
    try:
        email_validator.validate_email(
            sender,
            check_deliverability=False,
            allow_smtputf8=False,
            allow_display_name=False,
            allow_quoted_local=False,
            allow_domain_literal=False,
        )
    except email_validator.EmailNotValidError:
        return False
    return True


def _smtp_port_from_env(env: Mapping[str, str], security: str) -> int:
    """A porta de `CQ_SMTP_PORT` (a faixa é conferida na construção), ou a padrão do modo."""
    raw = _optional(env, "CQ_SMTP_PORT")
    if raw is None:
        return SMTP_DEFAULT_PORTS.get(security, SMTP_DEFAULT_PORTS[DEFAULT_SMTP_SECURITY])
    if _SMTP_PORT_PATTERN.fullmatch(raw) is None:
        raise ConfigError(_MSG_SMTP_PORT)
    return int(raw)


@dataclass(frozen=True)
class MailSettings:
    """Configurações do servidor de e-mail do worker; a senha fica fora do `repr`.

    `security` em `starttls` · `tls` · `none`; `timeout_seconds` é o prazo total da conversa SMTP de
    um envio. A validação roda na construção (`__post_init__`), inclusive a direta
    e a de `dataclasses.replace`, com as mensagens exatas (`_MSG_*`) e sem o valor recebido,
    nesta ordem: host, porta, modo, texto claro só no loopback, credenciais (as duas ou nenhuma,
    depois a faixa ASCII), remetente e prazo. A regra do `none` só no loopback fica **só** aqui: o
    adaptador não a confere de novo.
    """

    host: str
    port: int
    security: str
    username: str | None
    password: str | None = field(repr=False)
    sender: str
    timeout_seconds: float = DEFAULT_SMTP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not _valid_smtp_host(self.host):
            raise ConfigError(_MSG_SMTP_HOST)
        if not _valid_smtp_port(self.port):
            raise ConfigError(_MSG_SMTP_PORT)
        if self.security not in SMTP_SECURITY_MODES:
            raise ConfigError(_MSG_SMTP_SECURITY)
        if self.security == "none" and self.host not in _LOOPBACK_HOSTS:
            raise ConfigError(_MSG_SMTP_PLAINTEXT)
        if ((self.username is None) != (self.password is None)
                or self.username == "" or self.password == ""):
            raise ConfigError(_MSG_SMTP_PAIR)
        for value in (self.username, self.password):
            if value is not None and (not isinstance(value, str)
                                      or _SMTP_CREDENTIAL.fullmatch(value) is None):
                raise ConfigError(_MSG_SMTP_ASCII)
        if not _valid_sender(self.sender):
            raise ConfigError(_MSG_SMTP_FROM)
        if not _is_positive_finite(self.timeout_seconds):
            raise ConfigError(_MSG_SMTP_TIMEOUT)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MailSettings | None:
        """Lê as `CQ_SMTP_*` de `env` (e não do ambiente do processo); vazio vale como ausente.

        Sem `CQ_SMTP_HOST` (ausente ou vazio), o e-mail fica desligado: devolve `None` e ignora as
        demais variáveis, mesmo inválidas. Com o host, `ConfigError` com a mensagem exata de
        `_MSG_*`, sem o valor; a porta padrão depende do modo (587, 465 ou 25).
        """
        host = _optional(env, "CQ_SMTP_HOST")
        if host is None:
            return None
        security = _optional(env, "CQ_SMTP_SECURITY") or DEFAULT_SMTP_SECURITY
        port = _smtp_port_from_env(env, security)
        timeout = _positive_float(env, "CQ_SMTP_TIMEOUT_SECONDS", DEFAULT_SMTP_TIMEOUT_SECONDS)
        return cls(
            host=host,
            port=port,
            security=security,
            username=_optional(env, "CQ_SMTP_USERNAME"),
            password=_optional(env, "CQ_SMTP_PASSWORD"),
            sender=_optional(env, "CQ_SMTP_FROM") or "",
            timeout_seconds=timeout,
        )


def check_mail_timeout(mail: MailSettings, settings: Settings) -> None:
    """Invariante com a posse das tarefas: o prazo total da conversa SMTP
    mais `MAIL_LEASE_MARGIN_SECONDS` cabe na posse (`CQ_LEASE_SECONDS`); senão `ConfigError`, sem os
    valores. Conferida pelo worker na montagem do canal."""
    if mail.timeout_seconds + MAIL_LEASE_MARGIN_SECONDS > settings.lease_seconds:
        raise ConfigError(_MSG_SMTP_LEASE)


# --------------------------------------------------------------------------------------------------
# Motor, obtenção e entrega
# --------------------------------------------------------------------------------------------------

DEFAULT_ENGINE_TIMEOUT_SECONDS = 40
DEFAULT_REPO_FETCH_TIMEOUT_SECONDS = 40
DEFAULT_REPO_MAX_BYTES = 104_857_600
DEFAULT_REPO_MAX_FILES = 20_000
TELEGRAM_FORMATS = ("rich", "html")
DEFAULT_TELEGRAM_FORMAT = "rich"
TELEGRAM_MAX_BUTTONS_MIN = 1
TELEGRAM_MAX_BUTTONS_MAX = 100
# A margem entre o prazo de uma chamada externa e a posse da tarefa.
ENGINE_LEASE_MARGIN_SECONDS = 15
FETCH_LEASE_MARGIN_SECONDS = 15


# Mensagens exatas (fixas, sem o valor).
_MSG_ENGINE_URL = ("CQ_ENGINE_URL deve ser uma URL http ou https só com host e porta, sem usuário, "
                   "consulta, fragmento nem caminho.")
_MSG_ENGINE_LEASE = "CQ_ENGINE_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."
_MSG_REPO_HOSTS = ("CQ_REPO_ALLOWED_HOSTS deve ser uma lista, separada por vírgulas, de nomes DNS em "
                   "minúsculas com ponto, sem porta nem endereço IP.")
_MSG_REPOS_ROOT = "CQ_REPOS_ROOT deve ser um caminho absoluto."
_MSG_REPO_LEASE = "CQ_REPO_FETCH_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."
_MSG_TELEGRAM_FORMAT = "CQ_TELEGRAM_FORMAT deve ser rich ou html."
_MSG_MAX_BUTTONS = "CQ_TELEGRAM_MAX_BUTTONS deve ser um número inteiro de 1 a 100."

# Host da URL do motor: nome (letras e dígitos ASCII, `.`, `_` e `-`) ou IPv4; um IPv6 vem entre
# colchetes e passa pelo `ipaddress`.
_ENGINE_HOST_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}")
_ENGINE_PORT = re.compile(r"[0-9]{1,5}")
# Nome DNS em minúsculas **com ponto** (os rótulos como os do `questionrefs.REPO_URL`).
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_NAME = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})+")
_BUTTONS_PATTERN = re.compile(r"[0-9]+")


def _engine_url_ok(url: str) -> bool:
    """`http(s)://host[:porta][/]`, sem userinfo, consulta, fragmento nem caminho além de `/`
    e só com ASCII visível."""
    if _URL_CHARS.fullmatch(url) is None:
        return False
    for prefix in ("http://", "https://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            break
    else:
        return False
    if rest.endswith("/"):
        rest = rest[:-1]
    if not rest or any(c in rest for c in "/?#@\\"):
        return False
    if rest.startswith("["):
        close = rest.find("]")
        if close < 0:
            return False
        host, tail = rest[1:close], rest[close + 1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
    else:
        host, sep, port = rest.partition(":")
        tail = sep + port
        if _ENGINE_HOST_NAME.fullmatch(host) is None:
            return False
    if tail:
        if not tail.startswith(":") or _ENGINE_PORT.fullmatch(tail[1:]) is None:
            return False
        if not 1 <= int(tail[1:]) <= 65535:
            return False
    return True


def _dns_host_ok(host: str) -> bool:
    """Nome DNS em minúsculas com ponto, sem porta, IP literal nem `localhost`."""
    if len(host) > 253 or _DNS_NAME.fullmatch(host) is None:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    # Um último rótulo só de dígitos é um IPv4 escrito de outro jeito, nunca um nome DNS.
    return not host.rsplit(".", 1)[-1].isdigit()


def _raise(message: str) -> NoReturn:
    raise ConfigError(message) from None


_Number = TypeVar("_Number", int, float)


def _checked(read: Callable[[Mapping[str, str], str, _Number], _Number], env: Mapping[str, str],
             name: str, default: _Number) -> _Number:
    """`read(env, name, default)` (`_positive_int` ou `_positive_float`, com as frases fixas), com
    a recusa levantada de novo fora do `except`: nem o `__context__` (o `ValueError` da conversão,
    que cita o valor) fica na exceção."""
    try:
        return read(env, name, default)
    except ConfigError as exc:
        error = ConfigError(str(exc))
    raise error from None


@dataclass(frozen=True)
class EngineSettings:
    """O motor de questões do worker; sem `CQ_ENGINE_URL`, motor desligado."""

    engine_url: str
    timeout_seconds: float = DEFAULT_ENGINE_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EngineSettings | None:
        """Lê as `CQ_ENGINE_*` de `env` (e não do ambiente do processo); `None` sem `CQ_ENGINE_URL`
        (ausente ou vazia), ignorando o prazo mesmo inválido. Com a URL: `http(s)://host[:porta][/]`
        e o prazo finito maior que zero (padrão 40); `ConfigError` com a mensagem fixa, sem o valor.
        """
        url = _optional(env, "CQ_ENGINE_URL")
        if url is None:
            return None
        if not _engine_url_ok(url):
            _raise(_MSG_ENGINE_URL)
        timeout = _checked(_positive_float, env, "CQ_ENGINE_TIMEOUT_SECONDS",
                           DEFAULT_ENGINE_TIMEOUT_SECONDS)
        return cls(engine_url=url, timeout_seconds=timeout)


def check_engine_timeout(engine: EngineSettings, settings: Settings) -> None:
    """`ConfigError` se `timeout_seconds + 15 > CQ_LEASE_SECONDS`,
    sem os valores; a igualdade é aceita."""
    if engine.timeout_seconds + ENGINE_LEASE_MARGIN_SECONDS > settings.lease_seconds:
        raise ConfigError(_MSG_ENGINE_LEASE)


@dataclass(frozen=True)
class RepoSettings:
    """A obtenção de `repo@commit`: a lista de hosts permitidos (vazia recusa tudo;
    é a única que a api lê), a raiz do volume (`None`: obtenção desligada), o prazo e os tetos."""

    allowed_hosts: frozenset[str] = frozenset()
    repos_root: str | None = None
    fetch_timeout_seconds: float = DEFAULT_REPO_FETCH_TIMEOUT_SECONDS
    max_bytes: int = DEFAULT_REPO_MAX_BYTES
    max_files: int = DEFAULT_REPO_MAX_FILES

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RepoSettings:
        """Lê as `CQ_REPO*` de `env` (e não do ambiente do processo); vazio vale como ausente.
        `ConfigError` com a mensagem fixa, sem o valor."""
        hosts_raw = _optional(env, "CQ_REPO_ALLOWED_HOSTS")
        hosts: frozenset[str] = frozenset()
        if hosts_raw is not None:
            items = hosts_raw.split(",")
            if not all(_dns_host_ok(item) for item in items):
                _raise(_MSG_REPO_HOSTS)
            hosts = frozenset(items)
        root = _optional(env, "CQ_REPOS_ROOT")
        if root is not None and not (root.startswith("/") or os.path.isabs(root)):
            _raise(_MSG_REPOS_ROOT)
        return cls(
            allowed_hosts=hosts,
            repos_root=root,
            fetch_timeout_seconds=_checked(_positive_float, env, "CQ_REPO_FETCH_TIMEOUT_SECONDS",
                                           DEFAULT_REPO_FETCH_TIMEOUT_SECONDS),
            max_bytes=_checked(_positive_int, env, "CQ_REPO_MAX_BYTES", DEFAULT_REPO_MAX_BYTES),
            max_files=_checked(_positive_int, env, "CQ_REPO_MAX_FILES", DEFAULT_REPO_MAX_FILES),
        )


def check_fetch_timeout(repo: RepoSettings, settings: Settings) -> None:
    """`ConfigError` se `fetch_timeout_seconds + 15 > CQ_LEASE_SECONDS`,
    sem os valores; a igualdade é aceita."""
    if repo.fetch_timeout_seconds + FETCH_LEASE_MARGIN_SECONDS > settings.lease_seconds:
        raise ConfigError(_MSG_REPO_LEASE)


@dataclass(frozen=True)
class DeliverySettings:
    """A entrega pelo Telegram: dialeto (`rich` · `html`), botões, ritmo, fotos e o
    intervalo da resposta fixa às mensagens livres."""

    telegram_format: str = DEFAULT_TELEGRAM_FORMAT
    max_buttons: int = 12
    chat_interval_seconds: float = 1.0
    bot_per_second: int = 30
    photo_max_bytes: int = 10_000_000
    free_reply_seconds: int = 600

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DeliverySettings:
        """Lê as 6 variáveis da entrega de `env` (e não do ambiente do processo); vazio vale como
        ausente. `ConfigError` com a mensagem fixa,
        sem o valor."""
        defaults = cls()
        telegram_format = _optional(env, "CQ_TELEGRAM_FORMAT") or defaults.telegram_format
        if telegram_format not in TELEGRAM_FORMATS:
            _raise(_MSG_TELEGRAM_FORMAT)
        max_buttons = defaults.max_buttons
        raw_buttons = _optional(env, "CQ_TELEGRAM_MAX_BUTTONS")
        if raw_buttons is not None:
            # Inteiro decimal ASCII (sem sinal, espaços, expoente nem ponto), de 1 a 100.
            if _BUTTONS_PATTERN.fullmatch(raw_buttons) is None:
                _raise(_MSG_MAX_BUTTONS)
            max_buttons = int(raw_buttons)
            if not TELEGRAM_MAX_BUTTONS_MIN <= max_buttons <= TELEGRAM_MAX_BUTTONS_MAX:
                _raise(_MSG_MAX_BUTTONS)
        return cls(
            telegram_format=telegram_format,
            max_buttons=max_buttons,
            chat_interval_seconds=float(_checked(_positive_float, env,
                                                 "CQ_SEND_CHAT_INTERVAL_SECONDS",
                                                 defaults.chat_interval_seconds)),
            bot_per_second=_checked(_positive_int, env, "CQ_SEND_BOT_PER_SECOND",
                                    defaults.bot_per_second),
            photo_max_bytes=_checked(_positive_int, env, "CQ_TELEGRAM_PHOTO_MAX_BYTES",
                                     defaults.photo_max_bytes),
            free_reply_seconds=_checked(_positive_int, env, "CQ_FREE_MESSAGE_REPLY_SECONDS",
                                        defaults.free_reply_seconds),
        )
