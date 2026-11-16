"""Fixtures comuns dos testes, começando pelas gerais.

Pré-requisito: um Postgres de teste (scripts/test-postgres.sh up), ou `CQ_TEST_PG_DSN`.

Oferecimentos e bots:
- `bot_api`: a Bot API falsa da sessão (`tests/helpers/bot_api.py`), com `reset(clock=clock)` a cada
  teste; no fim, `protocol_errors() == []` e nenhum parâmetro de `calls()` nem texto de
  `sent_messages()` contém um segredo conhecido (`all_secrets()` e `bot_secrets()`);
- `bot_settings` e `bots`: o `BotSettings` dos testes (chave-mestra da sessão, URL da Bot API falsa e
  tempos curtos dentro do invariante) e o `BotRuntime` com `http_factory(bot_settings)`;
- `app`, `client` e `app_factory` montam o app com `bots=bots` (nada chama o Telegram sem que o teste
  peça); `app_factory(bots=…)` ou `app_factory(bot_settings=…)` trocam o runtime, e `bots=None`
  monta sem bots;
- `bot_worker_thread(**kwargs)`: `run_worker(…, bots=…)` numa thread (aceita `clock=`,
  `bot_settings=`, `bots=` e `settings=`), com `.stop()`, `.stop_one(thread)` e parada garantida no
  fim;
- `stop_one(thread)`: a parada ordenada de um worker só (a mesma de `bot_worker_thread.stop_one`).
As fixtures gerais (`settings`, `worker_thread`, `cli_invoke`…) valem também aqui.

Vínculo do aluno (lista e convites):
- `network_guard`: a guarda de rede (`NetworkGuard`), um gancho de auditoria da sessão
  (`sys.addaudithook`, instalado no primeiro uso e sem efeito até ser ligado); ligado durante o teste,
  barra com `NetworkGuardViolation` todo `socket.connect` (e todo `smtplib.connect`, antes da
  resolução do nome) a destino fora do loopback e anota as tentativas (`attempts()`, `blocked()`);
  no fim, nenhuma tentativa barrada;
- `smtp`: o SMTP falso da sessão (`tests/helpers/smtp.py`), com `reset(clock=clock)` a cada teste
  (credenciais sorteadas) e a guarda de rede ligada (depende de `network_guard`); no
  fim, `protocol_errors() == []` e nenhum segredo conhecido (`all_secrets()`, `bot_secrets()` e
  `mail_secrets()`, fora os códigos de convite das próprias mensagens) nos cabeçalhos, no texto nem no
  bruto das mensagens capturadas;
- `smtp_settings`: o `MailSettings` que aponta para o SMTP falso (`127.0.0.1`, a porta do dublê,
  `security="none"`, as credenciais do `reset` da fixture `smtp`, o remetente `SENDER` em `example.*`
  e o prazo total de 2 s); um teste que chama `smtp.reset(…)` de novo monta o seu (ex.:
  `dataclasses.replace(smtp_settings, …)`);
- `mail_channel`: o adaptador real, `SmtpVerificationChannel(smtp_settings)` (contexto TLS padrão; os
  testes de TLS montam o adaptador com o contexto do dublê).
O `bot_worker_thread` e o `worker_thread` bastam: o `verification=` vai pelos kwargs que eles
repassam ao `run_worker`.

Disparo e resposta (os dublês do motor, da obtenção e da Bot API, com falhas injetáveis nos
testes):
- `engine_fake`: o motor falso estrito da sessão (`tests/helpers/engine.py`, `session_engine()`), com
  `reset(repos_root=…)` a cada teste, ligado à área de repositórios temporária do teste; no fim,
  `protocol_errors() == []` e um `reset()` que religa o servidor parado e solta as
  respostas presas. A fixture não programa questão nenhuma (o teste programa as suas);
- `repo_fetcher`: a obtenção falsa (`tests/helpers/repos.py`, `FakeRepoFetcher`) na **mesma** área
  temporária que o `engine_fake` lê (`<tmp_path>/cq-repos`); no fim, `protocol_errors() == []`;
- `rich_bot_api`: a Bot API falsa de oferecimentos e bots (`bot_api`) com a extensão do disparo instalada
  (`install(DispatchExtension())`, desfeita pelo `reset()` da fixture `bot_api`); no fim,
  `protocol_errors() == []` e `policy_errors() == []`, antes das conferências da `bot_api`;
- `_seed_request` (`autouse`): guarda o `request` do teste no `ContextVar` `SEED_REQUEST` de
  `tests/helpers/calls.py`, para que `seed_state(…, dispatch_targets=K)` peça as fixtures de que a
  validação precisa (`settings`, `clock`, `engine_fake`, `repo_fetcher`, `bots`) só quando a semente
  de disparos é pedida (os testes unitários não ganham banco);
- `app` e `app_factory`: montam com `repo_settings=TEST_REPO_SETTINGS` (o host
  `repos.cq.test`), para que a semente e o `VALID_CALLS` preparem disparos pelo `client` e pelos apps
  da `app_factory` (o padrão do `create_app`, `RepoSettings()`, recusa todo host). A `app_factory` aceita
  `repo_settings=` para trocar a lista.
  As demais
  fixtures bastam (o `bot_worker_thread` e o `worker_thread` repassam `engine=`,
  `fetcher=`, `delivery=` e `console_routes=` ao `run_worker` pelos kwargs).

Relatórios e acompanhamento (a equipe simulada e o worker completo), com as
fixtures no fim do módulo:
- `team_members`: a equipe simulada (`tests/helpers/team.py`), por padrão 2 membros com endereço ativo no bot
  de um oferecimento `active` novo, vinculados pelo link de equipe com um worker temporário que a
  própria fixture sobe e para; parametrizável (indireta) por estados (`active`, `blocked`, `no_endpoint`) e
  pelo canal do vínculo (Telegram ou console);
- `report_worker`: sobe o `bot_worker_thread` ou o `worker_thread` (`console_routes=True`) com o motor
  falso, a obtenção falsa e o canal com rota; o `run_worker` não tem parâmetro dos relatórios (os tratadores entram
  pelo próprio `run_worker`).
A semente dos relatórios (`seed_state(…, report_targets=K)`, tests/helpers/calls.py) usa as mesmas fixtures que a dos disparos
pede pelo `SEED_REQUEST` (`settings`, `clock`, `engine_fake`, `repo_fetcher`, `bots`).

CLI:
- `cli_invoke` define `CQ_PROFILE` e `CQ_STACK_DIR` como `None` (o ambiente do desenvolvedor não
  vaza para os testes) e tem o argumento nomeado opcional `stack=None`: o
  `Stack` de nível 1, posto em `ctx.obj["stack"]` ao lado do `http` só quando dado (sem ele, o `obj` fica
  sem a chave `stack`);
- fixtures no fim do módulo: `fake_stack`, um `FakeStack`
  (`tests/helpers/stack_fake.py`) com os quatro serviços "ok" e a api em `TEST_API_PORT`; `fake_runner`, um
  `FakeRunner` (`tests/helpers/docker_fake.py`) com as amostras da pilha saudável e o Compose 2.27.0; e
  `api_subprocess`, a API dos testes num subprocesso do loopback (`tests/helpers/api_server.py`) sobre o
  banco do teste (`db_url`, o mesmo do `client` e do `sql`), com o `stop` no fim (escopo de função).
"""

from __future__ import annotations

import dataclasses
import io
import ipaddress
import itertools
import json
import os
import pathlib
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.bot_api import FakeBotApi, monotonic, session_fake
from tests.helpers.canary import find_secrets
from tests.helpers.clock import FakeClock
from tests.helpers.secrets import MASTER_KEY, all_secrets, bot_secrets, reset_secrets
from tests.helpers.secrets import mail_secrets
from tests.helpers.smtp import SENDER as SMTP_SENDER
from tests.helpers.smtp import FakeSmtp, session_smtp

DEFAULT_DSN = "postgresql://postgres@127.0.0.1:55432/postgres"

# Avanço que vence a posse do `settings` dos testes (a fronteira é estrita: lease_until < agora).
LEASE_SECONDS = 30
EXPIRE = LEASE_SECONDS + 1


@pytest.fixture(autouse=True)
def _reset_secret_registry() -> Iterator[None]:
    reset_secrets()
    yield
    reset_secrets()


def _dsn_for(base: str, dbname: str) -> str:
    return pg_conninfo.make_conninfo(base, dbname=dbname)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("CQ_TEST_PG_DSN", DEFAULT_DSN)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:  # pragma: no cover - depende do ambiente
        pytest.exit(
            "Postgres de teste indisponível. Suba com `scripts/test-postgres.sh up` ou defina "
            f"CQ_TEST_PG_DSN. ({type(exc).__name__})",
            returncode=3,
        )
    return dsn


def _admin(pg_dsn: str) -> psycopg.Connection:
    return psycopg.connect(pg_dsn, autocommit=True)


@pytest.fixture(scope="session")
def template_db(pg_dsn: str) -> Iterator[str]:
    from cq_server.migrate import apply_migrations

    name = f"cq_tpl_{os.getpid()}"
    with _admin(pg_dsn) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    url = _dsn_for(pg_dsn, name)
    try:
        apply_migrations(url)
        yield name
    finally:
        with _admin(pg_dsn) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_factory(pg_dsn: str, template_db: str) -> Iterator[Callable[[], str]]:
    """Cria bancos novos, clonados do template migrado; todos são removidos no fim do teste."""
    created: list[str] = []

    def make() -> str:
        name = f"cq_t_{uuid.uuid4().hex[:16]}"
        with _admin(pg_dsn) as conn:
            # O clone falha se houver conexão aberta ao template.
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (template_db,),
            )
            conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
        created.append(name)
        return _dsn_for(pg_dsn, name)

    yield make
    with _admin(pg_dsn) as conn:
        for name in created:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_url(db_factory: Callable[[], str]) -> str:
    return db_factory()


@pytest.fixture
def empty_db_url(pg_dsn: str) -> Iterator[str]:
    name = f"cq_e_{uuid.uuid4().hex[:16]}"
    with _admin(pg_dsn) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield _dsn_for(pg_dsn, name)
    with _admin(pg_dsn) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def unreachable_db_url() -> str:
    """URL de um Postgres que não existe (porta fechada), com senha para checar vazamentos."""
    return "postgresql://cq:senha-que-nao-pode-vazar@127.0.0.1:9/cq"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(db_url: str) -> Any:
    from cq_server.config import Settings

    return Settings(
        database_url=db_url,
        admin_token=ADMIN_TOKEN,
        lease_seconds=LEASE_SECONDS,
        max_attempts=3,
        backoff_base_seconds=2,
        backoff_max_seconds=60,
        poll_interval_seconds=0.05,
    )


@pytest.fixture
def bot_api(clock: FakeClock, _reset_secret_registry: None) -> Iterator[FakeBotApi]:
    """A Bot API falsa da sessão, limpa e com o relógio do teste; confere o protocolo no fim."""
    fake = session_fake()
    fake.reset(clock=clock)
    try:
        yield fake
        problems = fake.protocol_errors()
        known = all_secrets() + bot_secrets()
        leaked = [f"{call.method}#{call.seq}" for call in fake.calls()
                  if find_secrets(json.dumps(call.params, ensure_ascii=False), known)]
        leaked += [f"sendMessage#{sent.call_seq}" for sent in fake.sent_messages()
                   if find_secrets(sent.text, known)]
        assert problems == [], f"desvios de protocolo na Bot API falsa: {problems}"
        assert leaked == [], f"segredo conhecido nos parâmetros enviados ao Telegram: {leaked}"
    finally:
        fake.reset()


@pytest.fixture
def bot_settings(bot_api: FakeBotApi) -> Any:
    """`BotSettings` dos testes: posse 30 s, renovação 10 s,
    reconciliação 0,05 s, consulta 1 s e timeout 2 s (30 - 10 >= 1 + 2), na Bot API falsa."""
    from cq_server.config import BotSettings

    return BotSettings(
        telegram_api_url=bot_api.base_url,
        master_key=MASTER_KEY,
        lease_seconds=30,
        renew_seconds=10,
        reconcile_seconds=0.05,
        poll_timeout_seconds=1,
        telegram_timeout_seconds=2,
    )


def _runtime(bot_settings: Any) -> Any:
    from cq_server.adapters.telegram import http_factory
    from cq_server.bots import BotRuntime

    return BotRuntime(bot_settings, api_factory=http_factory(bot_settings))


@pytest.fixture
def bots(bot_settings: Any) -> Any:
    """O `BotRuntime` dos testes, com a fábrica HTTP real apontada para a Bot API falsa."""
    return _runtime(bot_settings)


@pytest.fixture
def app(settings: Any, clock: FakeClock, bots: Any) -> Any:
    from cq_server.api import create_app
    from tests.helpers.calls import TEST_REPO_SETTINGS

    return create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS)


_DEFAULT: Any = object()


@pytest.fixture
def app_factory(settings: Any, clock: FakeClock, bots: Any) -> Callable[..., Any]:
    from cq_server.api import create_app
    from tests.helpers.calls import TEST_REPO_SETTINGS

    default_runtime = bots

    def make(*, bots: Any = _DEFAULT, bot_settings: Any = None, repo_settings: Any = _DEFAULT,
             **overrides: Any) -> Any:
        runtime = default_runtime if bots is _DEFAULT else bots
        if bot_settings is not None:
            assert bots is _DEFAULT, "use bots= ou bot_settings=, não os dois"
            runtime = _runtime(bot_settings)
        repos = TEST_REPO_SETTINGS if repo_settings is _DEFAULT else repo_settings
        return create_app(dataclasses.replace(settings, **overrides), clock=clock, bots=runtime,
                          repo_settings=repos)

    return make


@pytest.fixture
def client(app: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def sql(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def queue(db_url: str, clock: FakeClock, settings: Any) -> Iterator[Any]:
    from cq_server.tasks import TaskQueue

    q = TaskQueue(db_url, clock=clock, settings=settings)
    yield q
    q.close()


@pytest.fixture
def outbox(db_url: str, clock: FakeClock, settings: Any) -> Iterator[Any]:
    from cq_server.outbox import Outbox

    o = Outbox(db_url, clock=clock, settings=settings)
    yield o
    o.close()


@pytest.fixture
def worker_thread(settings: Any, clock: FakeClock) -> Iterator[Callable[..., threading.Thread]]:
    """Sobe `run_worker` numa thread com o relógio do teste; no fim, `stop` + `join(10)` sempre.

    Uso: `start(**kwargs)` (repasse para `run_worker`, ex.: `channel=`, `handlers=`, `settings=`).
    Devolve a thread; `stop_worker()` encerra antes do fim do teste, se preciso.
    """
    from cq_server.worker import run_worker

    started: list[tuple[threading.Thread, threading.Event]] = []

    def start(**kwargs: Any) -> threading.Thread:
        stop = threading.Event()
        cfg = kwargs.pop("settings", settings)
        thread = threading.Thread(
            target=run_worker, args=(cfg,), kwargs={"stop": stop, "clock": clock, **kwargs}, daemon=True)
        thread.start()
        started.append((thread, stop))
        return thread

    def stop_worker() -> None:
        for thread, stop in started:
            stop.set()
        for thread, stop in started:
            thread.join(10)

    start.stop = stop_worker  # type: ignore[attr-defined]
    yield start
    stop_worker()
    alive = [t for t, _ in started if t.is_alive()]
    assert not alive, "run_worker não terminou em 10 s depois de stop"


@pytest.fixture
def cli_invoke(client: Any, tmp_path: pathlib.Path) -> Callable[..., Any]:
    """Roda o CLI em processo contra a API do teste."""
    from click.testing import CliRunner

    from cq_client.cli import cli

    missing_config = tmp_path / "config-inexistente.toml"
    _default_http = object()

    def invoke(args: list[str], *, json: bool = False, http: Any = _default_http,
               env: dict[str, str | None] | None = None, token: str | None = ADMIN_TOKEN,
               stack: Any = None) -> Any:
        full_env: dict[str, str | None] = {
            "CQ_API_URL": "http://testserver",
            "CQ_TOKEN": token,
            "CQ_CONFIG": str(missing_config),
            # O perfil e o diretório da pilha do desenvolvedor não vazam para o teste.
            "CQ_PROFILE": None,
            "CQ_STACK_DIR": None,
        }
        full_env.update(env or {})
        obj = None
        if http is _default_http:
            obj = {"http": client}
        elif http is not None:
            obj = {"http": http}
        if stack is not None:
            # O `Stack` de nível 1 ao lado do `http`, só quando dado.
            obj = {**(obj or {}), "stack": stack}
        argv = (["--json"] if json else []) + list(args)
        return CliRunner().invoke(cli, argv, env=full_env, obj=obj)

    return invoke


class BotWorkerThread(threading.Thread):
    """`run_worker(…, bots=…)` numa thread; guarda a exceção que escapar (`error`), se a função
    voltou normalmente (`returned`) e o instante do fim (`ended_at`, de `bot_api.monotonic()`)."""

    _names = itertools.count(1)

    def __init__(self, settings: Any, kwargs: dict[str, Any]) -> None:
        super().__init__(name=f"cq-test-bot-worker-{next(self._names)}", daemon=True)
        self.stop_event = threading.Event()
        self.returned = False
        self.ended_at: float | None = None
        self._error: BaseException | None = None
        self._error_seen = False
        self._settings = settings
        self._kwargs = kwargs

    def run(self) -> None:
        from cq_server.worker import run_worker

        try:
            run_worker(self._settings, stop=self.stop_event, **self._kwargs)
            self.returned = True
        except BaseException as exc:  # noqa: BLE001 - guardada para o teste conferir
            self._error = exc
        finally:
            self.ended_at = monotonic()

    @property
    def error(self) -> BaseException | None:
        """A exceção que escapou de `run_worker` (ler marca a exceção como conferida pelo teste)."""
        self._error_seen = True
        return self._error

    @property
    def unexpected_error(self) -> BaseException | None:
        return None if self._error_seen else self._error


def _stop_one(thread: BotWorkerThread, timeout: float = 10.0) -> float:
    """Parada ordenada de um worker só: liga o `stop` dele e espera a thread até `timeout`.

    Devolve quanto tempo (s) a thread levou para terminar depois do `stop`, medido com
    `bot_api.monotonic()`; se ela não terminou no prazo, devolve `inf` (o teste decide o que afirmar).
    """
    asked = monotonic()
    thread.stop_event.set()
    thread.join(timeout)
    if thread.is_alive() or thread.ended_at is None:
        return float("inf")
    return max(0.0, thread.ended_at - asked)


@pytest.fixture
def stop_one() -> Callable[..., float]:
    """A parada ordenada de um worker só de `bot_worker_thread` (ver `_stop_one`)."""
    return _stop_one


@pytest.fixture
def bot_worker_thread(settings: Any, clock: FakeClock,
                      bots: Any) -> Iterator[Callable[..., BotWorkerThread]]:
    """Sobe `run_worker(…, bots=…)` numa thread; no fim, `stop` + `join(10)` de todas, sempre.

    Uso: `start(**kwargs)`, repassados a `run_worker` (ex.: `channel=`, `handlers=`), com:
    `clock=` (padrão: o `FakeClock` do teste; ex.: `SystemClock()`), `settings=` (o `Settings` do
    teste), `bot_settings=` (monta um `BotRuntime` próprio com `http_factory`) ou `bots=` (o runtime
    pronto; padrão: a fixture `bots`). Devolve a `BotWorkerThread`. `start.stop()` para todas;
    `start.stop_one(thread)` para uma só e devolve o tempo até ela terminar. No fim, falha se uma
    thread não terminou ou se terminou com uma exceção que o teste não leu em `thread.error`.
    """
    started: list[BotWorkerThread] = []

    def start(**kwargs: Any) -> BotWorkerThread:
        cfg = kwargs.pop("settings", settings)
        own_settings = kwargs.pop("bot_settings", None)
        runtime = kwargs.pop("bots", _DEFAULT)
        if own_settings is not None:
            assert runtime is _DEFAULT, "use bots= ou bot_settings=, não os dois"
            runtime = _runtime(own_settings)
        elif runtime is _DEFAULT:
            runtime = bots
        kwargs.setdefault("clock", clock)
        thread = BotWorkerThread(cfg, {**kwargs, "bots": runtime})
        started.append(thread)
        thread.start()
        return thread

    def stop_all() -> None:
        for thread in started:
            thread.stop_event.set()
        for thread in started:
            thread.join(10)

    start.stop = stop_all  # type: ignore[attr-defined]
    start.stop_one = _stop_one  # type: ignore[attr-defined]
    yield start
    stop_all()
    alive = [t.name for t in started if t.is_alive()]
    assert not alive, f"run_worker com bots não terminou em 10 s depois de stop: {alive}"
    unexpected = [f"{t.name}: {type(t.unexpected_error).__name__}" for t in started
                  if t.unexpected_error is not None]
    assert not unexpected, f"run_worker com bots terminou com exceção não conferida: {unexpected}"


# --- Vínculo do aluno: guarda de rede e SMTP falso --------


class NetworkGuardViolation(BaseException):
    """Conexão barrada pela guarda de rede: destino fora do loopback.

    Deriva de `BaseException` para que nenhum `except Exception` do código testado (o adaptador SMTP
    classifica as exceções como falhas do envio) a transforme num resultado comum; além disso, a
    fixture `network_guard` falha o teste no fim se houve alguma tentativa barrada.
    """


@dataclasses.dataclass(frozen=True)
class NetworkAttempt:
    """Uma tentativa de conexão vista pelo gancho com a guarda ligada.

    `event` é `socket.connect` ou `smtplib.connect`; `host` e `port` são os do destino (`None` numa
    família sem host e porta, como `AF_UNIX`); `blocked` diz se a guarda a barrou.
    """

    event: str
    host: str | None
    port: int | None
    blocked: bool


_GUARDED_EVENTS = frozenset({"socket.connect", "smtplib.connect"})


def _loopback_host(host: str) -> bool:
    """`localhost` ou um IP de loopback (`127.0.0.0/8`, `::1`); o `::ffff:127.0.0.1` não é (3.12)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _socket_destination(address: Any) -> tuple[str | None, int | None, bool]:
    """(host, porta, permitido) do endereço de `socket.connect`: um arquivo (`AF_UNIX`) é local; um
    par `(host, porta, …)` só vale com host de loopback; qualquer outra forma é barrada."""
    if isinstance(address, (str, bytes)):
        return None, None, True
    if isinstance(address, tuple) and len(address) >= 2:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if isinstance(host, str):
            return host, _port(address[1]), _loopback_host(host)
    return None, None, False


class NetworkGuard:
    """Guarda de rede da sessão: um único gancho de auditoria
    (`sys.addaudithook`, que não pode ser removido), instalado no primeiro `enable()` e sem efeito
    enquanto a guarda está desligada.

    Ligada, ela anota cada `socket.connect` e cada `smtplib.connect` do processo (de qualquer thread,
    inclusive a do worker) e barra, com `NetworkGuardViolation`, antes da chamada ao sistema, os que
    vão para fora do loopback; o `smtplib.connect` é barrado já pelo host, antes da resolução do nome.
    O psycopg conecta em C (libpq) e não passa pelo gancho.
    """

    Violation = NetworkGuardViolation

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = 0
        self._installed = False
        self._attempts: list[NetworkAttempt] = []

    def enable(self) -> None:
        """Liga a guarda (instala o gancho na primeira vez) e esquece as tentativas anteriores."""
        with self._lock:
            if not self._installed:
                sys.addaudithook(self._hook)
                self._installed = True
            if self._enabled == 0:
                self._attempts = []
            self._enabled += 1

    def disable(self) -> list[NetworkAttempt]:
        """Desliga a guarda e devolve as tentativas anotadas enquanto ela esteve ligada."""
        with self._lock:
            self._enabled = max(0, self._enabled - 1)
            return list(self._attempts)

    @property
    def enabled(self) -> bool:
        return self._enabled > 0

    def attempts(self, event: str | None = None) -> list[NetworkAttempt]:
        """As tentativas anotadas desde que a guarda foi ligada (só as de `event`, se dado)."""
        with self._lock:
            return [a for a in self._attempts if event is None or a.event == event]

    def blocked(self) -> list[NetworkAttempt]:
        """As tentativas barradas desde que a guarda foi ligada."""
        with self._lock:
            return [a for a in self._attempts if a.blocked]

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in _GUARDED_EVENTS or not self._enabled:
            return
        if event == "socket.connect":
            host, port, allowed = _socket_destination(args[1] if len(args) > 1 else None)
        else:  # smtplib.connect: (smtp, host, port)
            host = str(args[1]) if len(args) > 1 else None
            port = _port(args[2]) if len(args) > 2 else None
            allowed = host is not None and _loopback_host(host)
        with self._lock:
            if not self._enabled:
                return
            self._attempts.append(NetworkAttempt(event, host, port, not allowed))
        if not allowed:
            raise NetworkGuardViolation(
                f"guarda de rede: {event} para {host}:{port} barrado (destino fora do loopback)")


_NETWORK_GUARD = NetworkGuard()


@pytest.fixture
def network_guard() -> Iterator[NetworkGuard]:
    """A guarda de rede da sessão, ligada durante o teste (ver `NetworkGuard`); no fim, falha se
    alguma conexão foi barrada, mesmo que o código testado tenha engolido a exceção."""
    _NETWORK_GUARD.enable()
    try:
        yield _NETWORK_GUARD
    finally:
        attempts = _NETWORK_GUARD.disable()
    blocked = [f"{a.event} {a.host}:{a.port}" for a in attempts if a.blocked]
    assert blocked == [], f"a guarda de rede barrou conexões fora do loopback: {blocked}"


def _smtp_known_secrets(fake: FakeSmtp) -> list[str]:
    """Os segredos que nenhuma mensagem capturada pode trazer: `all_secrets()`, `bot_secrets()` e
    `mail_secrets()` (se o dublê tem credenciais), fora os códigos de convite lidos das próprias
    mensagens, que são o conteúdo legítimo do e-mail (a leitura os registra em `remember_secret`)."""
    own_codes = {link.code for link in fake.invite_links()}
    known = all_secrets() + bot_secrets()
    if fake.credentials is not None:
        known += mail_secrets(fake)
    return [secret for secret in dict.fromkeys(known) if secret not in own_codes]


@pytest.fixture
def smtp(network_guard: NetworkGuard, clock: FakeClock,
         _reset_secret_registry: None) -> Iterator[FakeSmtp]:
    """O SMTP falso da sessão, limpo, com o relógio do teste e credenciais sorteadas; liga a guarda de
    rede e, no fim, confere o protocolo e a ausência de segredos conhecidos nas mensagens."""
    fake = session_smtp()
    fake.reset(clock=clock)
    try:
        yield fake
        problems = fake.protocol_errors()
        known = _smtp_known_secrets(fake)
        leaked = []
        for message in fake.messages():
            texts = [value for _, value in message.headers]
            texts += [message.text or "", message.raw.decode("latin-1")]
            if any(find_secrets(text, known) for text in texts):
                leaked.append(f"mensagem #{message.seq}")
        assert problems == [], f"desvios de protocolo no SMTP falso: {problems}"
        assert leaked == [], f"segredo conhecido nas mensagens capturadas pelo SMTP falso: {leaked}"
    finally:
        fake.reset()


@pytest.fixture
def smtp_settings(smtp: FakeSmtp) -> Any:
    """`MailSettings` dos testes: o SMTP falso em `127.0.0.1` (nunca
    `localhost`), sem TLS (`none`, só no loopback), com as credenciais do `reset` da fixture `smtp`,
    o remetente do dublê em `example.*` e o prazo total de 2 s (2 + 15 <= 30 s da posse)."""
    from cq_server.config import MailSettings

    assert smtp.credentials is not None, "a fixture smtp sorteia credenciais no reset"
    return MailSettings(host="127.0.0.1", port=smtp.port, security="none",
                        username=smtp.credentials.username, password=smtp.credentials.password,
                        sender=SMTP_SENDER, timeout_seconds=2)


@pytest.fixture
def mail_channel(smtp_settings: Any) -> Any:
    """O adaptador real da porta de verificação sobre o SMTP falso (`SmtpVerificationChannel`), com o
    contexto TLS padrão; no esqueleto, o construtor só guarda os argumentos."""
    from cq_server.adapters.smtp import SmtpVerificationChannel

    return SmtpVerificationChannel(smtp_settings)


# --- Disparo: motor falso, obtenção falsa, Bot API estendida e a semente de disparos ---------------
# (o motor falso e a obtenção falsa leem a mesma área de repositórios temporária do teste; no fim de cada
# teste, nenhum desvio de protocolo)

# O subdiretório de `tmp_path` com a área de repositórios do teste (o leiaute `<repo_key>/<commit>/` do
# motor), compartilhado pelo `engine_fake` e pelo `repo_fetcher`; nome próprio para não colidir com os
# arquivos que os testes criam em `tmp_path`.
REPOS_DIR = "cq-repos"


def _repos_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / REPOS_DIR
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def engine_fake(tmp_path: pathlib.Path) -> Iterator[Any]:
    """O motor falso estrito da sessão (`session_engine()`), limpo e ligado à área de repositórios do
    teste; no fim, nenhum desvio de protocolo. O `reset()` final religa o servidor
    que o teste parou (`stop`) e solta as respostas presas (`slow`)."""
    from tests.helpers.engine import session_engine

    fake = session_engine()
    fake.reset(repos_root=_repos_root(tmp_path))
    try:
        yield fake
        problems = fake.protocol_errors()
        assert problems == [], f"desvios de protocolo no motor falso: {problems}"
    finally:
        fake.reset()


@pytest.fixture
def repo_fetcher(tmp_path: pathlib.Path) -> Iterator[Any]:
    """A obtenção falsa (`FakeRepoFetcher`) na mesma área de repositórios que o `engine_fake` lê; no
    fim, nenhum `ensure` fora da forma validada (`protocol_errors() == []`)."""
    from tests.helpers.repos import FakeRepoFetcher

    fetcher = FakeRepoFetcher(_repos_root(tmp_path))
    yield fetcher
    problems = fetcher.protocol_errors()
    assert problems == [], f"desvios de protocolo na obtenção falsa: {problems}"


@pytest.fixture
def rich_bot_api(bot_api: FakeBotApi) -> Iterator[Any]:
    """A Bot API falsa com a extensão do disparo (`bot_api.install(DispatchExtension())`); responde aos atributos
    do dublê e aos da extensão. No fim, nenhum desvio de protocolo nem de política; as conferências da
    fixture `bot_api` (segredos) vêm depois, e o `reset()` dela desfaz a extensão."""
    from tests.helpers.bot_api_dispatch import DispatchExtension

    rich = bot_api.install(DispatchExtension())
    yield rich
    problems = rich.protocol_errors()
    policy = rich.policy_errors()
    assert problems == [], f"desvios de protocolo na Bot API falsa: {problems}"
    assert policy == [], f"violações de política na Bot API falsa: {policy}"


@pytest.fixture(autouse=True)
def _seed_request(request: pytest.FixtureRequest) -> Iterator[None]:
    """Guarda o `request` do teste em `tests.helpers.calls.SEED_REQUEST`. Não pede fixture
    nenhuma: a semente de disparos só lê `settings`, `clock`, `engine_fake`, `repo_fetcher` e `bots`
    quando `dispatch_targets` é pedido."""
    from tests.helpers.calls import SEED_REQUEST

    token = SEED_REQUEST.set(request)
    try:
        yield
    finally:
        SEED_REQUEST.reset(token)


# --- Relatórios: equipe simulada e worker completo ------------------------------------------------
# (os dublês da equipe e o worker completo, com a API e o canal)

# O `worker_id` do worker temporário que a fixture `team_members` sobe só para processar o `/start` dos
# membros (identifica as posses e os eventos `bot.lease_changed` dele).
TEAM_LINK_WORKER_ID = "w-team-link"
# Prazo do adaptador do motor no worker dos relatórios: dentro do invariante `timeout + 15 <= lease_seconds` (30 s).
REPORT_WORKER_ENGINE_TIMEOUT = 10.0
_TEAM_PARAM_KEYS = frozenset({"states", "channel"})


def _team_param(request: pytest.FixtureRequest) -> tuple[tuple[str, ...], str]:
    """Os estados dos membros e o canal do vínculo pedidos pela parametrização indireta (ver
    `team_members`); sem parametrização, dois membros ativos pelo Telegram."""
    from tests.helpers.team import ACTIVE, TELEGRAM

    param = getattr(request, "param", None)
    if param is None:
        return (ACTIVE, ACTIVE), TELEGRAM
    if isinstance(param, dict):
        unknown = sorted(set(param) - _TEAM_PARAM_KEYS)
        assert not unknown, f"team_members: chaves desconhecidas na parametrização: {unknown}"
        return tuple(param.get("states", (ACTIVE, ACTIVE))), param.get("channel", TELEGRAM)
    assert not isinstance(param, str), "team_members: a parametrização é uma sequência de estados"
    return tuple(param), TELEGRAM


@pytest.fixture
def team_members(request: pytest.FixtureRequest, client: Any, sql: psycopg.Connection, settings: Any,
                 clock: FakeClock, bot_api: FakeBotApi, bot_worker_thread: Any, worker_thread: Any,
                 db_url: str) -> Any:
    """A equipe simulada (`tests/helpers/team.py`), vinculada a um oferecimento `active` novo, com bot
    próprio na Bot API falsa (`create_team_offering`), pelo caminho real do link de equipe; devolve a `Team`.

    Por padrão, 2 membros com endereço ativo pelo Telegram. A parametrização indireta
    (`@pytest.mark.parametrize("team_members", [...], indirect=True)`) aceita uma sequência de estados de
    `tests.helpers.team` (`ACTIVE`, `BLOCKED`, `NO_ENDPOINT`; ex.: `(ACTIVE, BLOCKED, NO_ENDPOINT)`) ou um
    `dict` com `states` (a sequência) e `channel` (`TELEGRAM`, o padrão, ou `CONSOLE`: o `/start` pela
    `ConsoleInbox`; sem `BLOCKED`).

    O vínculo precisa de um worker que processe o `/start`: a fixture sobe um temporário (o
    `bot_worker_thread` com `bots` no Telegram; o `worker_thread` com `console_routes=True` e um
    `ConsoleChannel` no console), com o `worker_id` `TEAM_LINK_WORKER_ID` e o `FakeClock` do teste, e o
    para (parada ordenada, que solta a posse do bot) antes de devolver a equipe; o teste sobe o seu worker
    depois (ex.: `report_worker`). A saída do console desse worker fica em `team.stream` (no console, as
    boas-vindas dos membros). O relógio do teste não se move.
    """
    from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
    from tests.helpers.team import CONSOLE, TELEGRAM, create_team_offering, link_team

    states, channel = _team_param(request)
    assert channel in (TELEGRAM, CONSOLE), f"team_members: canal desconhecido: {channel!r}"
    offering, bot_id = create_team_offering(client, bot_api)
    stream = io.StringIO()
    own = dataclasses.replace(settings, worker_id=TEAM_LINK_WORKER_ID)
    if channel == TELEGRAM:
        thread = bot_worker_thread(channel=ConsoleChannel(stream), settings=own)
        try:
            team = link_team(client, sql, offering, bot_id, states, bot_api=bot_api)
        finally:
            elapsed = bot_worker_thread.stop_one(thread)
        assert elapsed != float("inf"), "team_members: o worker do vínculo não parou em 10 s"
        assert thread.error is None, f"team_members: o worker do vínculo terminou com {type(thread.error).__name__}"
    else:
        inbox = ConsoleInbox(db_url, clock=clock)
        worker_thread(channel=ConsoleChannel(stream), console_routes=True, settings=own)
        try:
            team = link_team(client, sql, offering, bot_id, states, inbox=inbox)
        finally:
            worker_thread.stop()
            inbox.close()
    return dataclasses.replace(team, stream=stream)


@dataclasses.dataclass(frozen=True)
class ReportWorker:
    """Um worker completo em execução: a thread, o modo (`console`) e a saída do `ConsoleChannel` (`stream`:
    no console, as mensagens de todos; com bots, só as sem rota por bot)."""

    thread: threading.Thread
    console: bool
    stream: io.StringIO


@pytest.fixture
def report_worker(bot_worker_thread: Any, worker_thread: Any, engine_fake: Any,
              repo_fetcher: Any) -> Iterator[Callable[..., ReportWorker]]:
    """Sobe o worker completo (os tratadores dos relatórios entram pelo próprio `run_worker`, que não tem
    parâmetro deles): o `bot_worker_thread` (com `bots`; canal com rota por bot) ou, com `console=True`, o
    `worker_thread` com `console_routes=True`; nos dois, com o motor falso (`HttpQuestionEngine`
    sobre o `engine_fake`), a obtenção falsa (`repo_fetcher`), `DeliverySettings()` e um `ConsoleChannel`
    num `StringIO` (o `stream=` dado, ex.: `team.stream`, ou um novo).

    Uso: `start(*, console=False, stream=None, **kwargs)`, com os `kwargs` repassados à fixture de baixo
    (ex.: `settings=`, `clock=`, `channel=`, `handlers=`); devolve o `ReportWorker`. `start.stop()` para todos.
    No fim, os workers param antes de o motor ser fechado; as conferências das duas fixtures de baixo
    continuam valendo.
    """
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.config import DeliverySettings, EngineSettings

    engines: list[Any] = []

    def start(*, console: bool = False, stream: io.StringIO | None = None, **kwargs: Any) -> ReportWorker:
        out = stream if stream is not None else io.StringIO()
        if "engine" not in kwargs:
            engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                                       timeout_seconds=REPORT_WORKER_ENGINE_TIMEOUT))
            engines.append(engine)
            kwargs["engine"] = engine
        kwargs.setdefault("fetcher", repo_fetcher)
        kwargs.setdefault("delivery", DeliverySettings())
        kwargs.setdefault("channel", ConsoleChannel(out))
        if console:
            thread = worker_thread(console_routes=True, **kwargs)
        else:
            thread = bot_worker_thread(**kwargs)
        return ReportWorker(thread=thread, console=console, stream=out)

    def stop_all() -> None:
        bot_worker_thread.stop()
        worker_thread.stop()

    start.stop = stop_all  # type: ignore[attr-defined]
    try:
        yield start
    finally:
        stop_all()
        for engine in engines:
            engine.close()


# --------------------------------------------------------------------------------------------------
# CLI: dublês da pilha e a API num subprocesso
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_stack() -> Any:
    """Um `FakeStack` com os quatro serviços "ok" e a api publicada em `TEST_API_PORT`; o teste o
    reconfigura por `set_timeline(...)` e `fail(...)` ou monta outro."""
    from tests.helpers.stack_fake import FakeStack

    return FakeStack(["healthy"])


@pytest.fixture
def fake_runner() -> Any:
    """Um `FakeRunner` com as amostras da pilha saudável e o Compose 2.27.0; o teste troca as
    amostras por `set_sample(...)` ou monta outro."""
    from tests.helpers.docker_fake import FakeRunner

    return FakeRunner()


@pytest.fixture
def api_subprocess(db_url: str) -> Iterator[Any]:
    """A API dos testes num subprocesso do loopback sobre o banco do teste, parada no fim."""
    from tests.helpers.api_server import serve

    server = serve(db_url)
    try:
        yield server
    finally:
        server.stop()
