"""Configuração do motor no worker: a leitura de `CQ_ENGINE_URL` e de
`CQ_ENGINE_TIMEOUT_SECONDS`, a conferência do prazo contra a posse da tarefa e a montagem do motor
no `cq-server worker`.

`EngineSettings.from_env(env)` lê `CQ_ENGINE_URL` e `CQ_ENGINE_TIMEOUT_SECONDS` do mapa recebido (nunca do
ambiente do processo) e devolve `None` sem `CQ_ENGINE_URL` (ausente ou vazia): o motor fica desligado.
Com a URL, ela tem de ser `http(s)://host[:porta][/]`, sem userinfo, consulta, fragmento nem caminho além
de `/`; o prazo é um número finito maior que zero (padrão 40; vazio vale o padrão). Toda recusa é
`ConfigError` com a mensagem **fixa**, sem o valor recebido (nem no traceback). `check_engine_timeout`
confere `timeout_seconds + 15 <= lease_seconds`. `build_engine(env, settings)` (em
`cq_server.__main__`) monta o motor como a `build_verification`: sem `CQ_ENGINE_URL`, um único
`worker.engine_disabled` (WARNING, logger `cq_server.worker`) e `None`; configurado, o
`HttpQuestionEngine`, sem registro e sem chamar o motor; inválido, `ConfigError`. O `cq-server worker`
com a URL inválida sai com código 2 e o registro `config.invalid` (um caso por subprocesso); o
`worker.mail_disabled` sai antes e é contado à parte.

As mensagens exatas abaixo são fixadas por este teste no molde das do SMTP; a
regra é só que sejam fixas e sem o valor ([I]).

No esqueleto, `from_env`, `check_engine_timeout` e `build_engine` lançam `NotImplementedError`, e o
`_cmd_worker` não chama `build_engine`: todos os casos ficam vermelhos. As asserções sobre o que o
esqueleto já tem (os padrões da dataclass, o `Settings` da API base sem campo novo) ficam na mesma função de um
caso vermelho.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import time
import traceback
from collections.abc import Callable
from typing import Any

import pytest

from cq_server.config import ConfigError, EngineSettings, Settings, check_engine_timeout
from tests.helpers.engine import repo_key
from tests.helpers.logs import capture_logs
from tests.helpers.processes import spawn_worker

# --- mensagens exatas (fixas, sem o valor) --------------------------------------------------------

MSG_URL = ("CQ_ENGINE_URL deve ser uma URL http ou https só com host e porta, sem usuário, consulta, "
           "fragmento nem caminho.")
MSG_TIMEOUT = "CQ_ENGINE_TIMEOUT_SECONDS deve ser um número finito maior que zero."
MSG_LEASE = "CQ_ENGINE_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."

FIELDS = ["engine_url", "timeout_seconds"]
ENGINE_URL = "http://engine:8080"
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

COURSE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "course"
REPO = "https://repos.cq.test/curso-teste.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

SUBPROCESS_TIMEOUT = 60.0  # subida de um processo Python novo com o pacote inteiro


def _assert_config_error(call: Callable[[], object], message: str, *, hidden: tuple[str, ...] = ()) -> None:
    """`call()` lança `ConfigError` com `message` exata; nem a mensagem, nem o `repr`, nem os `args`, nem o
    traceback formatado (com as exceções encadeadas) contêm os textos de `hidden`."""
    with pytest.raises(ConfigError) as excinfo:
        call()
    exc = excinfo.value
    assert str(exc) == message
    assert exc.args == (message,)
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        for value in hidden:
            assert value not in text


def _assert_reads(env: dict[str, str], expected: dict[str, object]) -> EngineSettings:
    settings = EngineSettings.from_env(env)
    assert isinstance(settings, EngineSettings)
    assert dataclasses.asdict(settings) == expected
    return settings


# --- motor desligado ------------------------------------------------------------------------------

NO_URL = [
    ("ambiente-vazio", {}),
    ("url-vazia", {"CQ_ENGINE_URL": ""}),
    ("so-o-prazo", {"CQ_ENGINE_TIMEOUT_SECONDS": "7.5"}),
    ("prazo-invalido-sem-url", {"CQ_ENGINE_TIMEOUT_SECONDS": "nan"}),
    ("prazo-invalido-url-vazia", {"CQ_ENGINE_URL": "", "CQ_ENGINE_TIMEOUT_SECONDS": "-1"}),
]


@pytest.mark.parametrize("env", [e for _, e in NO_URL], ids=[i for i, _ in NO_URL])
def test_without_url_engine_is_disabled(env: dict[str, str]) -> None:
    """Motor desligado quando a URL falta (`from_env` devolve
    `None` sem `CQ_ENGINE_URL`, ausente ou vazia; vazio vale como ausente): `from_env({})`, a URL
    vazia e o prazo sozinho (válido ou não) → `None`, sem `ConfigError`."""
    assert EngineSettings.from_env(env) is None


def test_from_env_reads_only_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Só o mapa recebido (como nas outras configurações): com `CQ_ENGINE_URL` e o prazo no ambiente do
    processo, `from_env({})` continua `None`, e `from_env` com só a URL continua com o padrão de 40 s."""
    monkeypatch.setenv("CQ_ENGINE_URL", "http://motor-do-ambiente:9999")
    monkeypatch.setenv("CQ_ENGINE_TIMEOUT_SECONDS", "3")
    assert EngineSettings.from_env({}) is None
    _assert_reads({"CQ_ENGINE_URL": ENGINE_URL}, {"engine_url": ENGINE_URL, "timeout_seconds": 40})


# --- padrões e leitura ----------------------------------------------------------------------------

def test_defaults_from_env_and_dataclass() -> None:
    """Padrões (`CQ_ENGINE_TIMEOUT_SECONDS` padrão 40; `EngineSettings(engine_url:
    str, timeout_seconds: float = 40)`, congelada; o padrão 40 da dataclass, real no esqueleto,
    afirmado na mesma função de um `from_env`; o prazo vazio vale o padrão): `from_env` com só a
    URL e com o prazo vazio dá exatamente `{engine_url, timeout_seconds: 40}`; a dataclass tem os campos
    previstos, nesta ordem, com o padrão 40 como único padrão, e é congelada. O `Settings` da API base não ganha
    campo novo."""
    settings = _assert_reads({"CQ_ENGINE_URL": ENGINE_URL}, {"engine_url": ENGINE_URL, "timeout_seconds": 40})
    _assert_reads({"CQ_ENGINE_URL": ENGINE_URL, "CQ_ENGINE_TIMEOUT_SECONDS": ""},
                  {"engine_url": ENGINE_URL, "timeout_seconds": 40})
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.timeout_seconds = 5  # type: ignore[misc]
    # Padrões da dataclass.
    assert [f.name for f in dataclasses.fields(EngineSettings)] == FIELDS
    defaults = {f.name: f.default for f in dataclasses.fields(EngineSettings)
                if f.default is not dataclasses.MISSING}
    assert defaults == {"timeout_seconds": 40}
    assert dataclasses.asdict(EngineSettings(engine_url=ENGINE_URL)) == {"engine_url": ENGINE_URL,
                                                                        "timeout_seconds": 40}
    # O `Settings` da API base não lê as variáveis do motor (test_config.py:61 congelado).
    r2 = Settings.from_env({"CQ_DATABASE_URL": CORE_DATABASE_URL, "CQ_ENGINE_URL": ENGINE_URL,
                            "CQ_ENGINE_TIMEOUT_SECONDS": "7.5"})
    assert dataclasses.asdict(r2) == CORE_SETTINGS_DEFAULTS


VALID_URLS = [
    ("nome-com-porta", "http://engine:8080"),
    ("https-sem-porta", "https://motor.example.org"),
    ("https-com-porta", "https://motor.example.org:8443"),
    ("ipv4-loopback", "http://127.0.0.1:18080"),
    ("ipv6-com-porta", "http://[::1]:8080"),
]


@pytest.mark.parametrize("url", [u for _, u in VALID_URLS], ids=[i for i, _ in VALID_URLS])
def test_valid_url_is_kept_exactly(url: str) -> None:
    """URL válida (`http(s)://host[:porta][/]`): `http` e `https`, com e sem porta, nome, IPv4 e
    IPv6 → a URL lida exatamente, com o prazo padrão; com uma `/` final, a URL também é aceita (o valor
    guardado, sem a barra final, é o mesmo)."""
    _assert_reads({"CQ_ENGINE_URL": url}, {"engine_url": url, "timeout_seconds": 40})
    with_slash = EngineSettings.from_env({"CQ_ENGINE_URL": url + "/"})
    assert isinstance(with_slash, EngineSettings)
    assert with_slash.engine_url.rstrip("/") == url
    assert with_slash.timeout_seconds == 40


VALID_TIMEOUTS = [("7.5", 7.5), ("40", 40), ("0.25", 0.25), ("45", 45)]


@pytest.mark.parametrize(("raw", "value"), VALID_TIMEOUTS, ids=[r for r, _ in VALID_TIMEOUTS])
def test_timeout_is_read_as_a_number(raw: str, value: float) -> None:
    """Prazo válido (`CQ_ENGINE_TIMEOUT_SECONDS` finito, > 0): o prazo lido como número (a posse
    é conferida à parte, por `check_engine_timeout`)."""
    settings = _assert_reads({"CQ_ENGINE_URL": ENGINE_URL, "CQ_ENGINE_TIMEOUT_SECONDS": raw},
                             {"engine_url": ENGINE_URL, "timeout_seconds": value})
    assert isinstance(settings.timeout_seconds, (int, float))
    assert not isinstance(settings.timeout_seconds, bool)


# --- recusas --------------------------------------------------------------------------------------

USER_MARK = "usuario-sentinela-3a"
PASS_MARK = "senha-sentinela-9f"
INVALID_URLS = [
    ("userinfo", f"http://{USER_MARK}:{PASS_MARK}@engine:8080", (USER_MARK, PASS_MARK)),
    ("so-usuario", f"http://{USER_MARK}@engine:8080", (USER_MARK,)),
    ("consulta", "http://engine:8080?chave=consulta-sentinela", ("consulta-sentinela",)),
    ("consulta-apos-barra", "http://engine:8080/?chave=consulta-sentinela", ("consulta-sentinela",)),
    ("fragmento", "http://engine:8080#fragmento-sentinela", ("fragmento-sentinela",)),
    ("caminho-v1", "http://engine:8080/v1", ()),
    ("caminho-qualquer", "http://engine:8080/caminho-sentinela", ("caminho-sentinela",)),
    ("esquema-ftp", "ftp://host-sentinela.example:8080", ("host-sentinela",)),
    ("esquema-file", "file:///tmp/motor-sentinela", ("motor-sentinela",)),
    ("sem-esquema", "host-sentinela.example:8080", ("host-sentinela",)),
    ("sem-host", "http://:8080", ()),
    ("so-esquema", "http://", ()),
    ("porta-nao-numerica", "http://host-sentinela.example:porta", ("host-sentinela",)),
]


@pytest.mark.parametrize(("url", "hidden"), [(u, h) for _, u, h in INVALID_URLS],
                         ids=[i for i, *_ in INVALID_URLS])
def test_invalid_url_is_config_error_without_the_value(url: str, hidden: tuple[str, ...]) -> None:
    """URL inválida (`http(s)://host[:porta][/]`, sem userinfo, consulta nem fragmento;
    mensagens fixas, sem o valor; sem caminho além de `/`): userinfo, consulta,
    fragmento, caminho (`http://engine:8080/v1`), esquema fora de `http(s)`, sem esquema, sem host e porta
    não numérica → `ConfigError` com a mensagem fixa, sem o valor (nem no traceback), com o prazo válido
    ou ausente."""
    _assert_config_error(lambda: EngineSettings.from_env({"CQ_ENGINE_URL": url}), MSG_URL,
                         hidden=(url, *hidden))
    env = {"CQ_ENGINE_URL": url, "CQ_ENGINE_TIMEOUT_SECONDS": "7.5"}
    _assert_config_error(lambda: EngineSettings.from_env(env), MSG_URL, hidden=(url, *hidden))


INVALID_TIMEOUTS = ["0", "-1", "-0.5", "nan", "inf", "-inf", "abc", "SentinelaPrazo7"]


@pytest.mark.parametrize("raw", INVALID_TIMEOUTS)
def test_invalid_timeout_is_config_error_without_the_value(raw: str) -> None:
    """Prazo inválido (`CQ_ENGINE_TIMEOUT_SECONDS` finito, > 0; número finito
    maior que zero): 0, negativo, `nan`, `inf`, `-inf` e texto → `ConfigError` com a mensagem fixa, sem o
    valor."""
    env = {"CQ_ENGINE_URL": ENGINE_URL, "CQ_ENGINE_TIMEOUT_SECONDS": raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: EngineSettings.from_env(env), MSG_TIMEOUT, hidden=hidden)


# --- invariante com a posse --------------------------------------------------------------

def _settings(lease: float) -> Settings:
    return Settings(database_url=CORE_DATABASE_URL, lease_seconds=lease)


ACCEPTED_TIMEOUT_LEASE = [
    ("padroes-40-60", 40, 60),
    ("limite-45-60", 45, 60),
    ("limite-15-30", 15, 30),
    ("limite-fracionario-7.5-22.5", 7.5, 22.5),
    ("testes-5-30", 5, 30),
]
REFUSED_TIMEOUT_LEASE = [
    ("45.5-60", 45.5, 60),
    ("40-54.9", 40, 54.9),
    ("padrao-40-posse-30", 40, 30),
    ("15.5-30", 15.5, 30),
]


@pytest.mark.parametrize(("timeout", "lease"), [(t, lease) for _, t, lease in ACCEPTED_TIMEOUT_LEASE],
                         ids=[i for i, *_ in ACCEPTED_TIMEOUT_LEASE])
def test_engine_timeout_fitting_the_lease_is_accepted(timeout: float, lease: float) -> None:
    """O prazo cabe na posse (invariante `timeout + ENGINE_LEASE_MARGIN_SECONDS (15) <= CQ_LEASE_SECONDS`;
    com os padrões, 40 + 15 ≤ 60): a igualdade é aceita (`45 + 15 == 60`,
    `15 + 15 == 30`) e `check_engine_timeout` devolve `None`."""
    assert check_engine_timeout(EngineSettings(engine_url=ENGINE_URL, timeout_seconds=timeout),
                                _settings(lease)) is None


@pytest.mark.parametrize(("timeout", "lease"), [(t, lease) for _, t, lease in REFUSED_TIMEOUT_LEASE],
                         ids=[i for i, *_ in REFUSED_TIMEOUT_LEASE])
def test_engine_timeout_beyond_the_lease_is_config_error(timeout: float, lease: float) -> None:
    """`timeout + 15 > lease` → `ConfigError` com a mensagem
    exata, sem os valores."""
    engine = EngineSettings(engine_url=ENGINE_URL, timeout_seconds=timeout)
    settings = _settings(lease)
    _assert_config_error(lambda: check_engine_timeout(engine, settings), MSG_LEASE)


# --- build_engine em processo ---------------------------------------------------------------------

def _build(env: dict[str, str], settings: Settings) -> Any:
    from cq_server.__main__ import build_engine

    return build_engine(env, settings)


NO_URL_ENVS = [
    pytest.param({}, id="sem-variaveis"),
    pytest.param({"CQ_ENGINE_URL": ""}, id="url-vazia"),
    pytest.param({"CQ_ENGINE_TIMEOUT_SECONDS": "nan"}, id="prazo-invalido-ignorado"),
]


@pytest.mark.parametrize("env", NO_URL_ENVS)
def test_build_engine_without_url_returns_none_and_warns_once(
        env: dict[str, str], monkeypatch: pytest.MonkeyPatch, network_guard: Any, capfd: Any) -> None:
    """Sem URL (sem `CQ_ENGINE_URL`, `None` e um único `worker.engine_disabled`,
    WARNING, logger `cq_server.worker`; `build_engine` não lê `os.environ`: a URL
    no ambiente do processo não conta): devolve `None`, registra exatamente um registro com
    `{level: WARNING, logger: cq_server.worker, event: worker.engine_disabled}`, nada em fd 1 e 2 e
    nenhuma conexão."""
    monkeypatch.setenv("CQ_ENGINE_URL", "http://motor-do-ambiente:9999")
    capfd.readouterr()
    with capture_logs() as cap:
        engine = _build(env, _settings(60))
    assert engine is None
    assert len(cap.records) == 1, cap.records
    expected = {"level": "WARNING", "logger": "cq_server.worker", "event": "worker.engine_disabled"}
    assert expected.items() <= cap.records[0].items(), cap.records[0]
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []


INVALID_BUILD_ENVS = [
    pytest.param({"CQ_ENGINE_URL": "http://engine:8080/v1"}, 60, MSG_URL, id="url-com-caminho"),
    pytest.param({"CQ_ENGINE_URL": f"http://{USER_MARK}:{PASS_MARK}@engine:8080"}, 60, MSG_URL,
                 id="url-com-userinfo"),
    pytest.param({"CQ_ENGINE_URL": ENGINE_URL, "CQ_ENGINE_TIMEOUT_SECONDS": "0"}, 60, MSG_TIMEOUT,
                 id="prazo-zero"),
    pytest.param({"CQ_ENGINE_URL": ENGINE_URL}, 30, MSG_LEASE, id="padrao-40-passa-da-posse-30"),
    pytest.param({"CQ_ENGINE_URL": ENGINE_URL, "CQ_ENGINE_TIMEOUT_SECONDS": "15.5"}, 30, MSG_LEASE,
                 id="prazo-15.5-passa-da-posse-30"),
]


@pytest.mark.parametrize(("env", "lease", "message"), INVALID_BUILD_ENVS)
def test_build_engine_rejects_invalid_configuration(env: dict[str, str], lease: int, message: str,
                                                    network_guard: Any, capfd: Any) -> None:
    """Configuração inválida (`build_engine` com `EngineSettings.from_env` e
    `check_engine_timeout`; `ConfigError` com a mensagem exata, sem o valor; a saída 2 e o
    `config.invalid` são do `cq-server worker`, testados no subprocesso): cada configuração inválida →
    `ConfigError`, sem registro, sem nada em fd 1 e 2, sem conexão e sem o userinfo."""
    capfd.readouterr()
    with capture_logs() as cap:
        with pytest.raises(ConfigError) as raised:
            _build(env, _settings(lease))
    assert str(raised.value) == message
    assert raised.value.args == (message,)
    assert cap.records == []
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    assert network_guard.attempts() == []
    for text in (str(raised.value), repr(raised.value), "".join(traceback.format_exception(raised.value))):
        assert USER_MARK not in text
        assert PASS_MARK not in text


@pytest.mark.parametrize(("timeout", "lease"), [(None, 60), ("15", 30)],
                         ids=["padrao-40-posse-60", "prazo-15-no-limite-da-posse-30"])
def test_build_engine_with_url_returns_the_http_engine_silently(
        timeout: str | None, lease: int, engine_fake: Any, capfd: Any) -> None:
    """Configurado (o `HttpQuestionEngine`, sem registro e sem nenhuma
    chamada ao motor na subida; o limite `15 + 15 == 30` é aceito): `build_engine`
    devolve o adaptador real sem nenhum registro, nada em fd 1 e 2 e sem pedido ao motor falso; o motor
    devolvido usa a URL do ambiente: uma inspeção chega ao motor falso (`health` e `inspect`) e devolve
    `Supported`; o `repr` só mostra `host:porta`."""
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.ports.engine import QuestionRef, Supported

    shutil.copytree(COURSE, engine_fake.repos_root / repo_key(REPO) / COMMIT)
    engine_fake.program("number", "number")
    env = {"CQ_ENGINE_URL": engine_fake.base_url}
    if timeout is not None:
        env["CQ_ENGINE_TIMEOUT_SECONDS"] = timeout
    capfd.readouterr()
    with capture_logs() as cap:
        engine = _build(env, _settings(lease))
    try:
        assert type(engine) is HttpQuestionEngine
        assert cap.records == []
        assert capfd.readouterr() == ("", "")
        assert engine_fake.calls() == []
        assert repr(engine) == f"HttpQuestionEngine(url=127.0.0.1:{engine_fake.port})"
        result = engine.inspect(QuestionRef(repo=REPO, commit=COMMIT, qid="number"))
        assert type(result) is Supported
        assert result.question.title == "Velocidade média"
        assert [c.op for c in engine_fake.calls()] == ["health", "inspect"]
    finally:
        engine.close()


# --- o cq-server worker em subprocesso ------------------------------------------------------------

def _json_records(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _wait(predicate: Callable[[], Any], message: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.05)


def test_worker_process_with_invalid_engine_url_exits_2_with_config_invalid(db_url: str, bot_api: Any) -> None:
    """URL inválida no processo (configuração inválida de `EngineSettings` →
    `ConfigError` com mensagem fixa, sem o valor, e o `cq-server worker` sai com código 2 e o registro
    `config.invalid` da API base; o `_cmd_worker` chama `build_engine` só depois de
    `_load_verification`): `python -m cq_server worker` com `CQ_ENGINE_URL` com userinfo sai com código 2
    sem `worker.started`; há exatamente um `{level: ERROR, logger: cq_server.main, event: config.invalid,
    detail: <mensagem exata>}`; o `worker.mail_disabled` (sem `CQ_SMTP_HOST`) sai uma vez, antes
    dele, e é contado à parte (o teste não afirma a contagem total de registros); o userinfo não aparece
    em fd 1 e 2."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-motor-invalido",
                         "CQ_ENGINE_URL": f"http://{USER_MARK}:{PASS_MARK}@engine:8080"})
    try:
        # O processo tem de sair antes de subir o laço: um `worker.started` já é falha.
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0],
              f"o cq-server worker não saiu nem subiu em {SUBPROCESS_TIMEOUT} s", SUBPROCESS_TIMEOUT)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o cq-server worker não saiu com código 2 (saiu: {code}; registros: {events})"
    assert "worker.started" not in events, events
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    expected = {"level": "ERROR", "logger": "cq_server.main", "event": "config.invalid", "detail": MSG_URL}
    assert expected.items() <= invalid[0].items(), invalid[0]
    disabled = [r for r in records if r.get("event") == "worker.mail_disabled"]
    assert len(disabled) == 1, events
    assert events.index("worker.mail_disabled") < events.index("config.invalid"), events
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert USER_MARK not in text, f"usuário da URL na {surface}"
        assert PASS_MARK not in text, f"senha da URL na {surface}"
