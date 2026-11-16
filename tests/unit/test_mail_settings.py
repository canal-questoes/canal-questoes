"""Configuração do e-mail do worker: a leitura das variáveis `CQ_SMTP_*`, a validação e a
conferência do tempo limite contra a posse da tarefa.

`MailSettings.from_env(env)` lê as 7 variáveis `CQ_SMTP_*` do mapa recebido (nunca do ambiente do
processo) e devolve `None` sem `CQ_SMTP_HOST` (ausente ou vazio): o e-mail fica desligado e as demais
`CQ_SMTP_*` são ignoradas, mesmo inválidas. Com o host, a validação roda, e
roda também na construção direta (`MailSettings(...)`, `dataclasses.replace`). Toda recusa é
`ConfigError` com a mensagem **exata** esperada, sem o valor recebido (nem no traceback, com as
exceções encadeadas). Vazio vale como ausente (regra da API base). A regra do texto claro (`none`) só no
loopback (`127.0.0.1`, `localhost`, `::1`) é conferida **só aqui**. `repr` e `str` nunca mostram
a senha. `check_mail_timeout(mail, settings)` confere `timeout_seconds + 15 <= lease_seconds`.
O `Settings` da API base e o `BotSettings` dos bots não mudam.

No esqueleto, `from_env` e `check_mail_timeout` lançam `NotImplementedError` e a construção direta não
valida; os campos, os padrões da dataclass e o `repr=False` da senha são reais. Só
`test_repr_and_str_hide_password` é verde desde o início; as asserções sobre os padrões da
dataclass ficam na função que também lê os padrões por `from_env`.
"""

from __future__ import annotations

import dataclasses
import math
import traceback
from collections.abc import Callable
from typing import Any

import pytest

from cq_server.config import BotSettings, ConfigError, MailSettings, Settings, check_mail_timeout

# --- mensagens exatas ------------------------------------------------------------

MSG_PORT = "CQ_SMTP_PORT deve ser um número inteiro de 1 a 65535."
MSG_SECURITY = "CQ_SMTP_SECURITY deve ser starttls, tls ou none."
MSG_PLAINTEXT = ("CQ_SMTP_SECURITY=none só é aceito com CQ_SMTP_HOST em 127.0.0.1, localhost ou "
                 "::1.")
MSG_HOST = "CQ_SMTP_HOST deve ser um nome de host ou endereço IP, sem espaços."
MSG_PAIR = "CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD devem vir juntos."
MSG_ASCII = ("CQ_SMTP_USERNAME e CQ_SMTP_PASSWORD só aceitam caracteres ASCII visíveis e "
             "espaço.")
MSG_SENDER = "CQ_SMTP_FROM deve ser um endereço de e-mail ASCII válido."
MSG_TIMEOUT = "CQ_SMTP_TIMEOUT_SECONDS deve ser um número finito maior que zero."
MSG_LEASE = "CQ_SMTP_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."

# --- valores ---------------------------------------------------------------------------------------

SMTP_VARS = ["CQ_SMTP_HOST", "CQ_SMTP_PORT", "CQ_SMTP_SECURITY", "CQ_SMTP_USERNAME",
             "CQ_SMTP_PASSWORD", "CQ_SMTP_FROM", "CQ_SMTP_TIMEOUT_SECONDS"]
FIELDS = ["host", "port", "security", "username", "password", "sender", "timeout_seconds"]

HOST = "smtp.example.com"
SENDER = "convites@example.org"
USERNAME = "cq-convites"
# Senha fictícia só com ASCII 0x20–0x7E (com espaço e til); o marcador nunca pode aparecer num erro.
PASSWORD_MARK = "SenhaSentinela"
PASSWORD = PASSWORD_MARK + "-Q7 ~x"

# O mínimo com o e-mail ligado: o host e o remetente (obrigatório com o host).
BASE_ENV = {"CQ_SMTP_HOST": HOST, "CQ_SMTP_FROM": SENDER}
BASE_VALUES = {"host": HOST, "port": 587, "security": "starttls", "username": None, "password": None,
               "sender": SENDER, "timeout_seconds": 10}
# Com as credenciais (a base dos casos de erro: a senha está sempre no ambiente e nunca no erro).
CRED_ENV = {**BASE_ENV, "CQ_SMTP_USERNAME": USERNAME, "CQ_SMTP_PASSWORD": PASSWORD}

# As 7 variáveis, todas diferentes da base.
FULL_ENV = {
    "CQ_SMTP_HOST": "mail.example.net",
    "CQ_SMTP_PORT": "2525",
    "CQ_SMTP_SECURITY": "tls",
    "CQ_SMTP_USERNAME": USERNAME,
    "CQ_SMTP_PASSWORD": PASSWORD,
    "CQ_SMTP_FROM": "cq.convites+r4@mail.example.net",
    "CQ_SMTP_TIMEOUT_SECONDS": "7.5",
}
FULL_VALUES = {"host": "mail.example.net", "port": 2525, "security": "tls", "username": USERNAME,
               "password": PASSWORD, "sender": "cq.convites+r4@mail.example.net",
               "timeout_seconds": 7.5}

# O `Settings` da API base com só `CQ_DATABASE_URL` (tests/unit/test_config.py) e o `BotSettings` dos bots com
# o ambiente vazio (tests/unit/test_bot_settings.py).
CORE_DATABASE_URL = "postgresql://cq@db.example:5432/cq"
CORE_SETTINGS_DEFAULTS = {
    "database_url": CORE_DATABASE_URL,
    "admin_token": None,
    "lease_seconds": 60,
    "max_attempts": 5,
    "backoff_base_seconds": 2,
    "backoff_max_seconds": 300,
    "poll_interval_seconds": 1,
    "worker_id": None,
    "console_output": "-",
}
BOT_DEFAULTS = {
    "telegram_api_url": "https://api.telegram.org",
    "lease_seconds": 60,
    "renew_seconds": 20,
    "reconcile_seconds": 10,
    "poll_timeout_seconds": 25,
    "telegram_timeout_seconds": 10,
    "master_key": None,
}

# Construção direta válida (a base dos casos da construção direta).
BASE_KWARGS = {"host": HOST, "port": 587, "security": "starttls", "username": USERNAME,
               "password": PASSWORD, "sender": SENDER, "timeout_seconds": 10}


def _assert_config_error(call: Callable[[], object], message: str, *,
                         hidden: tuple[str, ...] = ()) -> None:
    """`call()` lança `ConfigError` com `message` exata; nem a mensagem, nem o `repr`, nem os `args`, nem
    o traceback formatado (com as exceções encadeadas) contêm a senha, o marcador dela ou os textos de
    `hidden`."""
    with pytest.raises(ConfigError) as excinfo:
        call()
    exc = excinfo.value
    assert str(exc) == message
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        for value in (PASSWORD, PASSWORD_MARK, *hidden):
            assert value not in text


def _assert_reads(env: dict[str, str], expected: dict[str, object]) -> MailSettings:
    """`from_env(env)` devolve um `MailSettings` com exatamente os campos esperados (a porta como
    `int`)."""
    settings = MailSettings.from_env(env)
    assert isinstance(settings, MailSettings)
    assert dataclasses.asdict(settings) == expected
    assert type(settings.port) is int
    return settings


# --- e-mail desligado --------------------------------------------------------------

NO_HOST = [
    ("ambiente-vazio", {}),
    ("host-vazio", {"CQ_SMTP_HOST": ""}),
    ("porta-abc-sem-host", {"CQ_SMTP_PORT": "abc"}),
    ("todas-invalidas-sem-host", {
        "CQ_SMTP_PORT": "abc", "CQ_SMTP_SECURITY": "ssl", "CQ_SMTP_USERNAME": "usuárioç",
        "CQ_SMTP_FROM": "sem-arroba", "CQ_SMTP_TIMEOUT_SECONDS": "nan"}),
    ("todas-invalidas-host-vazio", {
        "CQ_SMTP_HOST": "", "CQ_SMTP_PORT": "0", "CQ_SMTP_SECURITY": "none",
        "CQ_SMTP_PASSWORD": PASSWORD, "CQ_SMTP_FROM": "convites@example.test",
        "CQ_SMTP_TIMEOUT_SECONDS": "-1"}),
    ("todas-validas-sem-host", {k: v for k, v in FULL_ENV.items() if k != "CQ_SMTP_HOST"}),
    ("todas-vazias", {var: "" for var in SMTP_VARS}),
]


@pytest.mark.parametrize("env", [e for _, e in NO_HOST], ids=[i for i, _ in NO_HOST])
def test_without_host_mail_is_disabled_and_other_variables_are_ignored(env: dict[str, str]) -> None:
    """Worker sem `CQ_SMTP_HOST` (as demais `CQ_SMTP_*` são ignoradas, mesmo inválidas):
    `from_env({})`, `CQ_SMTP_HOST` vazio, `CQ_SMTP_PORT=abc` sem host e as demais
    variáveis inválidas, válidas ou vazias sem host → `None`, sem `ConfigError`."""
    assert MailSettings.from_env(env) is None


def test_from_env_reads_only_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Como na API base e nos bots: `from_env(env)` lê o mapa recebido, e não o ambiente do processo; com as
    7 variáveis no ambiente do processo, `from_env({})` continua `None` e `from_env(BASE_ENV)` continua
    com os padrões."""
    for var, value in FULL_ENV.items():
        monkeypatch.setenv(var, value)
    assert MailSettings.from_env({}) is None
    _assert_reads(BASE_ENV, BASE_VALUES)


# --- padrões e leitura -----------------------------------------------------------------------------

def test_defaults_from_env_and_dataclass() -> None:
    """Padrões (tempo limite explícito, 10 s por padrão; porta 587 com `starttls`, 465
    com `tls` e 25 com `none`; `starttls` é o padrão; sem credenciais; os padrões da dataclass,
    reais no esqueleto, afirmados nesta função): `from_env` com só o host e o remetente dá os padrões
    exatos; a dataclass tem os campos previstos, nesta ordem, com `timeout_seconds = 10` como único
    padrão, e é congelada."""
    settings = _assert_reads(BASE_ENV, BASE_VALUES)
    _assert_reads({**BASE_ENV, "CQ_SMTP_SECURITY": "starttls"}, BASE_VALUES)
    _assert_reads({**BASE_ENV, "CQ_SMTP_SECURITY": "tls"},
                  {**BASE_VALUES, "security": "tls", "port": 465})
    _assert_reads({**BASE_ENV, "CQ_SMTP_HOST": "127.0.0.1", "CQ_SMTP_SECURITY": "none"},
                  {**BASE_VALUES, "host": "127.0.0.1", "security": "none", "port": 25})
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 25  # type: ignore[misc]
    # Padrões da dataclass.
    assert [f.name for f in dataclasses.fields(MailSettings)] == FIELDS
    defaults = {f.name: f.default for f in dataclasses.fields(MailSettings)
                if f.default is not dataclasses.MISSING}
    assert defaults == {"timeout_seconds": 10}
    direct = MailSettings(host=HOST, port=587, security="starttls", username=None, password=None,
                          sender=SENDER)
    assert dataclasses.asdict(direct) == BASE_VALUES


def test_every_variable_is_read() -> None:
    """Todas as variáveis (host, porta, modo, usuário, senha, remetente e tempo limite por variáveis de ambiente,
    só no worker; as 7 variáveis): todas juntas → cada campo com o valor lido (a porta como
    `int`, o tempo como número); `repr` e `str` do resultado sem a senha."""
    settings = _assert_reads(FULL_ENV, FULL_VALUES)
    assert settings.timeout_seconds == 7.5
    for text in (repr(settings), str(settings)):
        assert PASSWORD not in text
        assert PASSWORD_MARK not in text


READ_CASES = [
    ("host", {"CQ_SMTP_HOST": "mail.example.net"}, {"host": "mail.example.net"}),
    ("porta", {"CQ_SMTP_PORT": "2525"}, {"port": 2525}),
    ("modo", {"CQ_SMTP_SECURITY": "tls"}, {"security": "tls", "port": 465}),
    ("credenciais", {"CQ_SMTP_USERNAME": USERNAME, "CQ_SMTP_PASSWORD": PASSWORD},
     {"username": USERNAME, "password": PASSWORD}),
    ("remetente", {"CQ_SMTP_FROM": "cq.convites+r4@mail.example.net"},
     {"sender": "cq.convites+r4@mail.example.net"}),
    ("tempo", {"CQ_SMTP_TIMEOUT_SECONDS": "7.5"}, {"timeout_seconds": 7.5}),
]


@pytest.mark.parametrize(("env", "changes"), [(e, c) for _, e, c in READ_CASES],
                         ids=[i for i, *_ in READ_CASES])
def test_each_variable_maps_to_its_own_field(env: dict[str, str], changes: dict[str, object]) -> None:
    """cada variável, sozinha sobre a base, muda só o seu campo (o modo `tls`
    muda também a porta padrão para 465), sem troca de variáveis."""
    _assert_reads({**BASE_ENV, **env}, {**BASE_VALUES, **changes})


EXPLICIT_PORTS = [
    ("starttls", {"CQ_SMTP_SECURITY": "starttls", "CQ_SMTP_PORT": "2587"}, "starttls", 2587),
    ("tls", {"CQ_SMTP_SECURITY": "tls", "CQ_SMTP_PORT": "2465"}, "tls", 2465),
    ("none", {"CQ_SMTP_HOST": "127.0.0.1", "CQ_SMTP_SECURITY": "none", "CQ_SMTP_PORT": "2525"},
     "none", 2525),
    ("limite-1", {"CQ_SMTP_PORT": "1"}, "starttls", 1),
    ("limite-65535", {"CQ_SMTP_PORT": "65535"}, "starttls", 65535),
]


@pytest.mark.parametrize(("env", "security", "port"), [(e, s, p) for _, e, s, p in EXPLICIT_PORTS],
                         ids=[i for i, *_ in EXPLICIT_PORTS])
def test_explicit_port_wins_over_the_mode_default(env: dict[str, str], security: str,
                                                  port: int) -> None:
    """Porta explícita (porta inteira de 1 a 65535; o padrão por modo só vale sem `CQ_SMTP_PORT`): a
    porta explícita vale em cada modo, e 1 e 65535 são aceitas."""
    settings = _assert_reads({**BASE_ENV, **env}, {**BASE_VALUES, "host": env.get("CQ_SMTP_HOST", HOST),
                                                   "security": security, "port": port})
    assert settings.port == port


EMPTY_OPTIONALS = [
    ("porta", {"CQ_SMTP_PORT": ""}),
    ("modo", {"CQ_SMTP_SECURITY": ""}),
    ("credenciais", {"CQ_SMTP_USERNAME": "", "CQ_SMTP_PASSWORD": ""}),
    ("tempo", {"CQ_SMTP_TIMEOUT_SECONDS": ""}),
    ("todas", {"CQ_SMTP_PORT": "", "CQ_SMTP_SECURITY": "", "CQ_SMTP_USERNAME": "",
               "CQ_SMTP_PASSWORD": "", "CQ_SMTP_TIMEOUT_SECONDS": ""}),
]


@pytest.mark.parametrize("env", [e for _, e in EMPTY_OPTIONALS], ids=[i for i, _ in EMPTY_OPTIONALS])
def test_empty_variable_counts_as_absent(env: dict[str, str]) -> None:
    """Vazio vale como ausente (regra da API base): porta, modo, credenciais e tempo
    vazios → os padrões (587, `starttls`, sem credenciais, 10 s)."""
    _assert_reads({**BASE_ENV, **env}, BASE_VALUES)


def test_other_variables_do_not_feed_mail_and_mail_does_not_feed_core_or_bot_settings() -> None:
    """`MailSettings` à parte do `Settings` e do `BotSettings`, que não mudam:
    com as 7 `CQ_SMTP_*` e as variáveis da API base e dos bots no mesmo mapa, `MailSettings` lê
    só as suas (o `CQ_LEASE_SECONDS` e o `CQ_TELEGRAM_TIMEOUT_SECONDS` não mudam o tempo do e-mail), e
    `Settings.from_env` e `BotSettings.from_env` dão exatamente os padrões da API base e dos bots, sem a senha do
    SMTP no `repr`."""
    env = {"CQ_DATABASE_URL": CORE_DATABASE_URL, **FULL_ENV}
    _assert_reads(env, FULL_VALUES)
    others = {"CQ_LEASE_SECONDS": "45", "CQ_TELEGRAM_TIMEOUT_SECONDS": "3",
              "CQ_POLL_INTERVAL_SECONDS": "0.5", "CQ_BOT_LEASE_SECONDS": "80"}
    _assert_reads({**BASE_ENV, **others}, BASE_VALUES)
    settings = Settings.from_env(env)
    assert dataclasses.asdict(settings) == CORE_SETTINGS_DEFAULTS
    for text in (repr(settings), str(settings)):
        assert PASSWORD not in text
    assert dataclasses.asdict(BotSettings.from_env(env)) == BOT_DEFAULTS


# --- porta, modo e host ----------------------------------------------------------------------------

INVALID_PORTS = ["0", "65536", "abc", "25.5", "-1", "SentinelaPorta9"]


@pytest.mark.parametrize("raw", INVALID_PORTS)
def test_invalid_port_is_config_error(raw: str) -> None:
    """Porta inválida (porta inteira de 1 a 65535): 0, 65536, `abc`, `25.5` (e -1) → `ConfigError` com a
    mensagem exata, sem o valor (inclusive no traceback)."""
    env = {**CRED_ENV, "CQ_SMTP_PORT": raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_PORT, hidden=hidden)


INVALID_SECURITY = ["ssl", "plain", "SentinelaModo7"]


@pytest.mark.parametrize("raw", INVALID_SECURITY)
def test_unknown_security_mode_is_config_error(raw: str) -> None:
    """Modos (`starttls` · `tls` · `none`): modo desconhecido → `ConfigError` com a
    mensagem exata, sem o valor."""
    env = {**CRED_ENV, "CQ_SMTP_SECURITY": raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_SECURITY, hidden=hidden)


PLAINTEXT_REFUSED_HOSTS = ["smtp.example.com", "192.0.2.10", "10.0.0.1", "::ffff:127.0.0.1",
                           "localhost.example.org", "127.0.0.2", "sentinela-host.example.org"]


@pytest.mark.parametrize("host", PLAINTEXT_REFUSED_HOSTS)
def test_plaintext_outside_loopback_is_config_error(host: str, network_guard: Any) -> None:
    """Cifragem (conexão cifrada com verificação de certificado, exceto para um servidor no próprio
    computador, regra conferida na leitura da configuração; o único lugar da regra: o adaptador
    não confere de novo; `none` só com `127.0.0.1`, `localhost` ou `::1`):
    `smtp.example.com`, `192.0.2.10`, `10.0.0.1`, `::ffff:127.0.0.1` (não é loopback na 3.12),
    `localhost.example.org` e `127.0.0.2` → `ConfigError` com a mensagem exata, sem o host, com e sem
    porta explícita, e sem nenhuma tentativa de conexão (a guarda de rede não vê nada)."""
    env = {**CRED_ENV, "CQ_SMTP_HOST": host, "CQ_SMTP_SECURITY": "none"}
    hidden = ("sentinela-host",) if host.startswith("sentinela") else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_PLAINTEXT, hidden=hidden)
    with_port = {**env, "CQ_SMTP_PORT": "25"}
    _assert_config_error(lambda: MailSettings.from_env(with_port), MSG_PLAINTEXT, hidden=hidden)
    assert network_guard.attempts() == []


LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1"]


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
def test_plaintext_on_loopback_is_accepted(host: str, network_guard: Any) -> None:
    """O dublê no próprio computador (`none` aceito com `127.0.0.1`,
    `localhost` ou `::1`, porta padrão 25), na leitura e na construção direta, sem conexão."""
    _assert_reads({**CRED_ENV, "CQ_SMTP_HOST": host, "CQ_SMTP_SECURITY": "none"},
                  {**BASE_VALUES, "host": host, "security": "none", "port": 25, "username": USERNAME,
                   "password": PASSWORD})
    direct = MailSettings(**{**BASE_KWARGS, "host": host, "security": "none", "port": 2525,
                             "timeout_seconds": 2})
    assert dataclasses.asdict(direct) == {**BASE_KWARGS, "host": host, "security": "none",
                                          "port": 2525, "timeout_seconds": 2}
    assert network_guard.attempts() == []


ENCRYPTED_HOSTS = ["smtp.example.com", "mail.example.net", "192.0.2.10", "10.0.0.1",
                   "::ffff:127.0.0.1", "localhost.example.org", "127.0.0.1", "localhost", "::1"]


@pytest.mark.parametrize("security", ["starttls", "tls"])
@pytest.mark.parametrize("host", ENCRYPTED_HOSTS)
def test_any_host_name_or_ip_is_accepted_with_encryption(host: str, security: str) -> None:
    """Qualquer host com cifragem (host: nome ou IP, sem espaço; a restrição de loopback é só do `none`):
    nomes e IPs, dentro e fora do loopback, são aceitos em `starttls` e em `tls`, sem mudança."""
    port = 587 if security == "starttls" else 465
    _assert_reads({**BASE_ENV, "CQ_SMTP_HOST": host, "CQ_SMTP_SECURITY": security},
                  {**BASE_VALUES, "host": host, "security": security, "port": port})


HOSTS_WITH_SPACES = ["smtp example.com", " smtp.example.com", "smtp.example.com ", "Sentinela Host"]


@pytest.mark.parametrize("host", HOSTS_WITH_SPACES)
def test_host_with_space_is_config_error(host: str) -> None:
    """Host sem espaço: espaço no meio, no início ou no fim → `ConfigError` com a
    mensagem exata, sem o valor."""
    env = {**CRED_ENV, "CQ_SMTP_HOST": host}
    hidden = ("Sentinela Host",) if "Sentinela" in host else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_HOST, hidden=hidden)


# --- credenciais -----------------------------------------------------------------------------------

HALF_CREDENTIALS = [
    ("so-usuario", {"CQ_SMTP_USERNAME": USERNAME}),
    ("so-senha", {"CQ_SMTP_PASSWORD": PASSWORD}),
    ("usuario-vazio-com-senha", {"CQ_SMTP_USERNAME": "", "CQ_SMTP_PASSWORD": PASSWORD}),
    ("senha-vazia-com-usuario", {"CQ_SMTP_USERNAME": USERNAME, "CQ_SMTP_PASSWORD": ""}),
]


@pytest.mark.parametrize("env", [e for _, e in HALF_CREDENTIALS], ids=[i for i, _ in HALF_CREDENTIALS])
def test_username_and_password_must_come_together(env: dict[str, str]) -> None:
    """Usuário e senha, as duas ou nenhuma (vazio vale como ausente): usuário sem senha e senha
    sem usuário → `ConfigError` com a mensagem exata, sem a senha."""
    full = {**BASE_ENV, **env}
    _assert_config_error(lambda: MailSettings.from_env(full), MSG_PAIR)


# Marcador dos usuários recusados (nunca aparece num erro).
USERNAME_MARK = "UsuarioSentinela"
NON_ASCII_CREDENTIALS = [
    ("senha-cedilha", USERNAME, PASSWORD_MARK + "ç"),
    ("senha-nul", USERNAME, PASSWORD_MARK + "\x00Nul"),
    ("senha-del", USERNAME, PASSWORD_MARK + "\x7fDel"),
    ("senha-tabulacao", USERNAME, PASSWORD_MARK + "\tTab"),
    ("usuario-cedilha", USERNAME_MARK + "ç", PASSWORD),
    ("usuario-nul", USERNAME_MARK + "\x00", PASSWORD),
    ("usuario-del", USERNAME_MARK + "\x7f", PASSWORD),
    ("usuario-quebra-de-linha", USERNAME_MARK + "\n", PASSWORD),
]


@pytest.mark.parametrize(("username", "password"), [(u, p) for _, u, p in NON_ASCII_CREDENTIALS],
                         ids=[i for i, *_ in NON_ASCII_CREDENTIALS])
def test_credentials_outside_visible_ascii_are_config_error(username: str, password: str) -> None:
    """Credenciais visíveis (usuário e senha só com ASCII 0x20–0x7E): `ç`, NUL, `\\x7f` (e controles)
    → `ConfigError` com a mensagem exata, sem o usuário nem a senha."""
    env = {**BASE_ENV, "CQ_SMTP_USERNAME": username, "CQ_SMTP_PASSWORD": password}
    hidden = (USERNAME_MARK,)
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_ASCII, hidden=hidden)


def test_credentials_with_space_and_tilde_are_kept_exactly() -> None:
    """Limites das credenciais (ASCII 0x20–0x7E, inclusive o espaço e o `~`): usuário e senha nos limites da faixa
    são aceitos e guardados sem aparar."""
    username = " ~cq convites~ "
    password = " !Senha~Sentinela " + "".join(chr(c) for c in range(0x20, 0x7F))
    _assert_reads({**BASE_ENV, "CQ_SMTP_USERNAME": username, "CQ_SMTP_PASSWORD": password},
                  {**BASE_VALUES, "username": username, "password": password})


# --- remetente -------------------------------------------------------------------------------------

INVALID_SENDERS = [
    ("sem-arroba", "SentinelaRemetente"),
    ("sem-dominio", "convites@"),
    ("arroba-duplo", "convites@@example.org"),
    ("nao-ascii", "convitês@example.org"),
    ("dominio-test", "convites@example.test"),
]


@pytest.mark.parametrize("sender", [s for _, s in INVALID_SENDERS], ids=[i for i, _ in INVALID_SENDERS])
def test_invalid_sender_is_config_error(sender: str) -> None:
    """Remetente (`CQ_SMTP_FROM` ASCII validado; endereço ASCII válido, `email_validator`
    sem SMTPUTF8): inválido, não ASCII e em `.test` (nome de uso especial) → `ConfigError` com a mensagem
    exata, sem o valor."""
    env = {**CRED_ENV, "CQ_SMTP_FROM": sender}
    hidden = (sender,) if sender.startswith("Sentinela") else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_SENDER, hidden=hidden)


@pytest.mark.parametrize("env", [{k: v for k, v in CRED_ENV.items() if k != "CQ_SMTP_FROM"},
                                 {**CRED_ENV, "CQ_SMTP_FROM": ""}],
                         ids=["ausente", "vazio"])
def test_sender_is_required_with_the_host(env: dict[str, str]) -> None:
    """Remetente obrigatório (`CQ_SMTP_FROM` obrigatório com o host; vazio vale como ausente): sem remetente →
    `ConfigError` com a mensagem exata."""
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_SENDER)


# --- tempo limite ----------------------------------------------------------------------------------

INVALID_TIMEOUTS = ["0", "-1", "nan", "inf", "-inf", "abc", "SentinelaTempo3"]


@pytest.mark.parametrize("raw", INVALID_TIMEOUTS)
def test_invalid_timeout_is_config_error(raw: str) -> None:
    """Tempo limite explícito (número finito > 0): 0, negativo, `nan`, `inf`
    (e texto) → `ConfigError` com a mensagem exata, sem o valor."""
    env = {**CRED_ENV, "CQ_SMTP_TIMEOUT_SECONDS": raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: MailSettings.from_env(env), MSG_TIMEOUT, hidden=hidden)


# --- invariante com a posse das tarefas ----------------------------------------------------

def _settings(lease: float) -> Settings:
    return Settings(database_url=CORE_DATABASE_URL, lease_seconds=lease)


def _mail(timeout: float) -> MailSettings:
    return MailSettings(**{**BASE_KWARGS, "timeout_seconds": timeout})


ACCEPTED_TIMEOUT_LEASE = [
    ("limite-10-25", 10, 25),
    ("testes-2-30", 2, 30),
    ("padroes-10-60", 10, 60),
    ("limite-45-60", 45, 60),
    ("limite-fracionario-7.5-22.5", 7.5, 22.5),
]
REFUSED_TIMEOUT_LEASE = [
    ("10-24.9", 10, 24.9),
    ("15.5-30", 15.5, 30),
    ("45.5-60", 45.5, 60),
    ("10-10", 10, 10),
]


@pytest.mark.parametrize(("timeout", "lease"), [(t, lease) for _, t, lease in ACCEPTED_TIMEOUT_LEASE],
                         ids=[i for i, *_ in ACCEPTED_TIMEOUT_LEASE])
def test_mail_timeout_fitting_the_lease_is_accepted(timeout: float, lease: float) -> None:
    """O tempo limite cabe na posse (o tempo limite, somado ao trabalho no banco, cabe na posse da tarefa; invariante
    `CQ_SMTP_TIMEOUT_SECONDS + 15 <= CQ_LEASE_SECONDS`, conferida na subida do worker;
    `check_mail_timeout` devolve `None`): a igualdade é aceita (`10 + 15 == 25`), e também os valores
    dos testes (2 s com a posse de 30 s) e os padrões (10 s com 60 s)."""
    assert check_mail_timeout(_mail(timeout), _settings(lease)) is None


@pytest.mark.parametrize(("timeout", "lease"), [(t, lease) for _, t, lease in REFUSED_TIMEOUT_LEASE],
                         ids=[i for i, *_ in REFUSED_TIMEOUT_LEASE])
def test_mail_timeout_beyond_the_lease_is_config_error(timeout: float, lease: float) -> None:
    """Além da posse (invariante com a posse das tarefas): `timeout + 15 > lease` →
    `ConfigError` com a mensagem exata (sem os valores), sem a senha."""
    mail = _mail(timeout)
    settings = _settings(lease)
    _assert_config_error(lambda: check_mail_timeout(mail, settings), MSG_LEASE)


# --- construção direta ------------------------------------------------------------------------------

DIRECT_CASES = [
    ("porta-zero", {"port": 0}, MSG_PORT),
    ("porta-65536", {"port": 65536}, MSG_PORT),
    ("porta-nao-inteira", {"port": 25.5}, MSG_PORT),
    ("modo-desconhecido", {"security": "ssl"}, MSG_SECURITY),
    ("none-fora-do-loopback", {"security": "none", "port": 25}, MSG_PLAINTEXT),
    ("none-ipv4-mapeado", {"security": "none", "port": 25, "host": "::ffff:127.0.0.1"}, MSG_PLAINTEXT),
    ("host-com-espaco", {"host": "smtp example.com"}, MSG_HOST),
    ("usuario-sem-senha", {"password": None}, MSG_PAIR),
    ("senha-sem-usuario", {"username": None}, MSG_PAIR),
    ("senha-cedilha", {"password": PASSWORD_MARK + "ç"}, MSG_ASCII),
    ("usuario-nul", {"username": "cq\x00convites"}, MSG_ASCII),
    ("usuario-del", {"username": "cq\x7fconvites"}, MSG_ASCII),
    ("remetente-vazio", {"sender": ""}, MSG_SENDER),
    ("remetente-invalido", {"sender": "convites@"}, MSG_SENDER),
    ("remetente-nao-ascii", {"sender": "convitês@example.org"}, MSG_SENDER),
    ("remetente-test", {"sender": "convites@example.test"}, MSG_SENDER),
    ("tempo-zero", {"timeout_seconds": 0}, MSG_TIMEOUT),
    ("tempo-negativo", {"timeout_seconds": -1}, MSG_TIMEOUT),
    ("tempo-nan", {"timeout_seconds": math.nan}, MSG_TIMEOUT),
    ("tempo-inf", {"timeout_seconds": math.inf}, MSG_TIMEOUT),
]


@pytest.mark.parametrize(("kwargs", "message"), [(k, m) for _, k, m in DIRECT_CASES],
                         ids=[i for i, *_ in DIRECT_CASES])
def test_direct_construction_runs_the_same_validation(kwargs: dict[str, object], message: str) -> None:
    """Construção direta (a validação roda na construção e em `from_env`, com as mesmas mensagens;
    a regra do `none` fora do loopback só no `MailSettings`, também na construção direta): cada
    violação, por `MailSettings(...)` e por `dataclasses.replace` de uma configuração válida →
    `ConfigError` com a mensagem exata, sem a senha."""
    base = MailSettings(**BASE_KWARGS)
    _assert_config_error(lambda: MailSettings(**{**BASE_KWARGS, **kwargs}), message)
    _assert_config_error(lambda: dataclasses.replace(base, **kwargs), message)


# --- `repr` e `str` (verde desde o início) --------------------------------------------------------

def test_repr_and_str_hide_password() -> None:
    """A senha não aparece em respostas, logs, eventos nem saídas (senha com
    `repr=False`; `repr` e `str` nunca mostram a senha): `repr` e `str` de `MailSettings`
    construído direto não contêm a senha nem um pedaço dela, e o campo guarda a senha exata. Verde já
    no esqueleto."""
    settings = MailSettings(host="127.0.0.1", port=2525, security="none", username=USERNAME,
                            password=PASSWORD, sender=SENDER, timeout_seconds=2)
    assert settings.password == PASSWORD
    for text in (repr(settings), str(settings)):
        assert PASSWORD not in text
        assert PASSWORD_MARK not in text
        assert PASSWORD[:8] not in text
        assert PASSWORD[-8:] not in text
