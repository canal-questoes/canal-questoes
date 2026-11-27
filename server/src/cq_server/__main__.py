"""CLI de operação do servidor: `cq-server migrate | serve | worker`.

- `migrate`: aplica as migrações que faltam e termina.
- `serve`: aplica as migrações (o lock consultivo serializa dois executores) e
  sobe a API no uvicorn, sem o access log.
- `worker`: roda o laço do worker até receber SIGTERM ou SIGINT.

Bots: `serve` e `worker` carregam também o `BotSettings`
do ambiente (`BotSettings.from_env`) e montam o `BotRuntime` com a fábrica HTTP do adaptador
(`adapters.telegram.http_factory`); uma configuração inválida dos bots é erro de configuração
(código 2), como a do `Settings`. `migrate` não precisa dela.

Verificação por e-mail: `build_verification(env, settings)`
monta o canal de verificação do worker a partir de `MailSettings.from_env(env)`: sem `CQ_SMTP_HOST`,
registra uma vez `worker.mail_disabled` (logger `cq_server.worker`) e devolve `None`; configuração
inválida ou invariante violada levanta `ConfigError`; com e-mail configurado, devolve o
`SmtpVerificationChannel`. O `worker` a chama com `os.environ` antes de subir o laço: o `ConfigError`
sai como o do `Settings` (registro `config.invalid`, código 2).

Motor, obtenção e entrega:
`build_engine(env, settings)` e `build_repo_fetcher(env, settings)` montam o motor e a obtenção do worker
como a `build_verification` (aviso único no logger `cq_server.worker`, `ConfigError` com mensagem exata,
sem ler `os.environ` nem configurar o logging; nenhuma conexão). O `worker` as chama **só depois** de a
verificação do e-mail ter sucesso, nessa ordem, e por último lê `DeliverySettings.from_env`; cada
`ConfigError` sai como o do `Settings` (`config.invalid`, código 2). O resultado vai ao `run_worker(engine=…,
fetcher=…, delivery=…)`. Depois da obtenção pelo git, `build_bank_fetcher(env, settings)` monta a publicação dos
bancos de questões enviados, na mesma raiz (`None` sem raiz utilizável, sem aviso próprio), e o resultado vai ao
`run_worker(bank_fetcher=…)`; o `fetcher` continua o da obtenção pelo git. O `serve` lê
`RepoSettings.from_env` **antes** da migração (`ConfigError` → `config.invalid`, código 2) e passa a lista de
hosts ao `create_app(…, repo_settings=…)`. Os adaptadores do motor, do git e dos bancos (`adapters.engine`,
`adapters.git`, `adapters.banks`) só são importados aqui dentro, na raiz de composição; o `GitTestOptions` (só
dos testes) não tem caminho por aqui.

Sonda do e-mail: `build_mail_probe(env, settings, probe)` monta o laço da
sonda do servidor de e-mail (`CQ_MAIL_PROBE_INTERVAL_SECONDS`, inteiro positivo, padrão 21600), sem ler
`os.environ` nem conectar. O `worker` a chama com `os.environ` e o canal do `build_verification` (ou `None`) depois
da entrega; o `ConfigError` sai como o do `Settings` (`config.invalid`, código 2). Só esta partida liga a sonda.

Só este módulo chama `configure_logging`. Todo o resto da saída do processo também
sai em JSON pelo mesmo formatter: avisos do `warnings`, exceções não tratadas (na thread principal e
nas outras), exceções que o Python ignora (em `__del__`, finalizadores, callbacks de weakref) e erros
de configuração. Nenhum traceback nem mensagem de exceção chega ao stdout ou ao stderr, nem a
mensagem e os argumentos de um registro cuja emissão falhou, porque podem carregar identidade ou
segredos, que não podem vazar.

Códigos de saída: 0 (sucesso ou parada pedida), 1 (falha), 2 (uso incorreto ou configuração
inválida), 130 (interrompido durante `migrate`).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import pathlib
import signal
import sys
import threading
import time
import warnings
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, TextIO

from cq_server.config import (
    BotSettings,
    ConfigError,
    DeliverySettings,
    EngineSettings,
    MailSettings,
    RepoSettings,
    Settings,
    _positive_int,
    check_engine_timeout,
    check_fetch_timeout,
    check_mail_timeout,
)
from cq_server.clock import SystemClock
from cq_server.logs import configure_logging

if TYPE_CHECKING:
    from cq_server.adapters.banks import BankRepoFetcher
    from cq_server.bots import BotRuntime
    from cq_server.mail_probe import MailProbeLoop
    from cq_server.ports.engine import QuestionEngine
    from cq_server.ports.repos import RepoFetcher
    from cq_server.ports.verification import MailServerProbe, VerificationChannel

logger = logging.getLogger("cq_server.main")

DEFAULT_HOST = "0.0.0.0"  # dentro do container; o Compose só publica a porta em 127.0.0.1
DEFAULT_PORT = 8000
LOG_LEVEL = "INFO"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class _ArgumentParser(argparse.ArgumentParser):
    """`argparse` que devolve o erro de uso em vez de encerrar o processo com a mensagem padrão."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="cq-server", description="Operação do servidor canal-questoes.")
    commands = parser.add_subparsers(dest="command", metavar="{migrate,serve,worker}")
    commands.required = True
    commands.add_parser("migrate", help="aplica as migrações do banco e termina")
    serve = commands.add_parser("serve", help="aplica as migrações e sobe a API")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"endereço de escuta (padrão {DEFAULT_HOST})")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"porta (padrão {DEFAULT_PORT})")
    commands.add_parser("worker", help="roda o worker até receber SIGTERM ou SIGINT")
    return parser


# ---------------------------------------------------------------------------------------------
# Saída do processo só em JSON
# ---------------------------------------------------------------------------------------------


def _log_unhandled(exc_type: type[BaseException], exc: BaseException | None,
                   tb: TracebackType | None) -> None:
    if exc is None:
        logger.error("exceção não tratada", extra={"event": "unhandled_exception",
                                                   "exception": exc_type.__name__})
        return
    logger.error("exceção não tratada", exc_info=(exc_type, exc, tb))


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


def _show_warning(message: Warning | str, category: type[Warning], filename: str, lineno: int,
                  file: TextIO | None = None, line: str | None = None) -> None:
    """Avisos do `warnings` em JSON, só com a categoria e o local: o texto do aviso pode trazer
    valores (a mesma regra das exceções)."""
    path = pathlib.PurePath(filename)
    where = "/".join(path.parts[-2:]) if len(path.parts) >= 2 else path.name
    logging.getLogger("py.warnings").warning("aviso", extra={
        "event": "warning", "category": category.__name__, "location": f"{where}:{lineno}"})


def _unraisable_hook(args: sys.UnraisableHookArgs) -> None:
    """Exceções que o Python ignora (em `__del__`, finalizadores, callbacks de weakref) em JSON, só
    com o tipo e o local. O gancho padrão imprime no stderr o `repr` do objeto, o traceback e a
    mensagem da exceção."""
    _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


def _setup_process_logging() -> None:
    configure_logging(LOG_LEVEL)
    warnings.showwarning = _show_warning
    sys.excepthook = _log_unhandled
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable_hook
    # No padrão (`True`), uma falha ao emitir um registro faz `Handler.handleError` imprimir no
    # stderr o traceback, a mensagem e os argumentos do registro, que podem carregar identidade ou
    # segredos, que nunca saem no stderr. Desligado, o registro que falhou só se perde.
    logging.raiseExceptions = False


def _load_settings() -> Settings | None:
    try:
        return Settings.from_env(os.environ)
    except ConfigError as exc:
        # As mensagens de `ConfigError` nunca trazem o valor (config.py).
        logger.error("configuração inválida", extra={"event": "config.invalid", "detail": str(exc)})
        return None


def _load_bot_runtime() -> BotRuntime | None:
    """O `BotRuntime` do ambiente, ou `None` se a configuração dos bots é inválida."""
    from cq_server.adapters.telegram import http_factory
    from cq_server.bots import BotRuntime

    try:
        bot_settings = BotSettings.from_env(os.environ)
    except ConfigError as exc:
        # As mensagens de `ConfigError` nunca trazem o valor (nem a chave-mestra; config.py).
        logger.error("configuração inválida", extra={"event": "config.invalid", "detail": str(exc)})
        return None
    return BotRuntime(bot_settings, api_factory=http_factory(bot_settings))


def build_verification(env: Mapping[str, str], settings: Settings) -> VerificationChannel | None:
    """O canal de verificação do worker (ver o docstring do módulo). Não lê `os.environ`, não
    configura o logging e não instala sinais.

    Sem `CQ_SMTP_HOST` (ausente ou vazio; as demais `CQ_SMTP_*` são ignoradas): um único
    `worker.mail_disabled` (WARNING) no logger `cq_server.worker` e `None`. Configuração
    inválida (`MailSettings.from_env`) ou prazo do SMTP que não cabe na posse (`check_mail_timeout`):
    `ConfigError` com a mensagem exata, sem o valor. Com e-mail: o `SmtpVerificationChannel` com o
    contexto TLS padrão, sem nenhum registro e sem conectar."""
    from cq_server.adapters.smtp import SmtpVerificationChannel

    mail = MailSettings.from_env(env)
    if mail is None:
        logging.getLogger("cq_server.worker").warning(
            "worker.mail_disabled", extra={"event": "worker.mail_disabled"})
        return None
    check_mail_timeout(mail, settings)
    return SmtpVerificationChannel(mail)


def build_mail_probe(env: Mapping[str, str], settings: Settings, probe: MailServerProbe | None) -> MailProbeLoop:
    """O laço do teste do servidor de e-mail do worker, no molde de
    `build_engine`: lê `CQ_MAIL_PROBE_INTERVAL_SECONDS` de `env` (inteiro positivo; padrão 21600) e monta o
    `MailProbeLoop` com `probe` (o canal do `build_verification`, ou `None` sem e-mail) e o `conninfo` do
    papel do worker. Não lê `os.environ` e não conecta.

    Vazia vale como ausente (o padrão); `0`, negativo, fracionário ou texto: `ConfigError` com a frase fixa de
    `_positive_int` de `config`, que traz só o nome da variável (sem eco do valor). O `conninfo` leva o
    `application_name = cq-worker/mail-probe`."""
    from cq_server.mail_probe import MAIL_PROBE_INTERVAL_DEFAULT, MAIL_PROBE_INTERVAL_ENV, MailProbeLoop
    from cq_server.worker import _role_conninfo

    interval = _positive_int(env, MAIL_PROBE_INTERVAL_ENV, MAIL_PROBE_INTERVAL_DEFAULT)
    return MailProbeLoop(probe, _role_conninfo(settings.database_url, "mail-probe"), interval, SystemClock(),
                         time.sleep)


def build_engine(env: Mapping[str, str], settings: Settings) -> QuestionEngine | None:
    """O motor do worker (`EngineSettings.from_env(env)` e `check_engine_timeout`); sem
    `CQ_ENGINE_URL` (ausente ou vazia), um único `worker.engine_disabled` (WARNING, logger
    `cq_server.worker`) e `None`. Configuração inválida ou prazo que não cabe na posse: `ConfigError`
    com a mensagem exata, sem o valor. Com o motor: o `HttpQuestionEngine`, sem registro e sem
    conectar (o construtor não toca a rede)."""
    engine = EngineSettings.from_env(env)
    if engine is None:
        logging.getLogger("cq_server.worker").warning(
            "worker.engine_disabled", extra={"event": "worker.engine_disabled"})
        return None
    check_engine_timeout(engine, settings)
    from cq_server.adapters.engine import HttpQuestionEngine

    return HttpQuestionEngine(engine)


def _usable_root(root: str) -> bool:
    """A raiz dos repositórios existe, é diretório e aceita escrita (nada é criado aqui)."""
    return os.path.isdir(root) and os.access(root, os.W_OK)


def build_repo_fetcher(env: Mapping[str, str], settings: Settings) -> RepoFetcher | None:
    """A obtenção do worker (`RepoSettings.from_env(env)` e `check_fetch_timeout`); sem
    `CQ_REPOS_ROOT` (ou com a raiz inexistente, que não é diretório ou sem escrita), um único
    `worker.repos_disabled` e `None`. `ConfigError` com a mensagem exata, sem o valor. Com a raiz: o
    `GitRepoFetcher` de produção (sem opções de teste), sem registro nem conexão."""
    repo = RepoSettings.from_env(env)
    check_fetch_timeout(repo, settings)
    if repo.repos_root is None or not _usable_root(repo.repos_root):
        logging.getLogger("cq_server.worker").warning(
            "worker.repos_disabled", extra={"event": "worker.repos_disabled"})
        return None
    from cq_server.adapters.git import GitRepoFetcher

    return GitRepoFetcher(repo)


def build_bank_fetcher(env: Mapping[str, str], settings: Settings) -> BankRepoFetcher | None:
    """A publicação dos bancos de questões enviados no worker (as referências `bank:`), com a mesma raiz e os
    mesmos tetos da obtenção pelo git e o pacote carregado do PostgreSQL por uma conexão curta; sem raiz
    utilizável, `None`, sem registro próprio (o aviso é o da obtenção pelo git). O adaptador só é importado
    aqui dentro.

    O carregador abre uma conexão curta por publicação (`application_name = cq-worker/banks`), só de leitura, e a
    fecha ao devolver o pacote ou `BankMissing`; a falha de conexão ou de consulta sobe ao adaptador, que a
    transforma em falha temporária pelo tipo da exceção. Nada conecta na montagem."""
    repo = RepoSettings.from_env(env)
    if repo.repos_root is None or not _usable_root(repo.repos_root):
        return None
    from psycopg.conninfo import make_conninfo

    from cq_server import db
    from cq_server.adapters.banks import BankRepoFetcher
    from cq_server.banks import BankMissing, load_archive

    conninfo = make_conninfo(settings.database_url, application_name="cq-worker/banks")

    def load(name: str, version: str) -> bytes | BankMissing:
        with db.connect(conninfo, autocommit=True) as conn:
            return load_archive(conn, name, version)

    return BankRepoFetcher(repo, load)


def _sweep_staging(fetcher: RepoFetcher | None) -> None:
    """Na partida do worker, com a obtenção: remove as sobras de `.staging` com mais de 1 hora
    (as de uma obtenção interrompida). A falha de disco só vira um aviso com o tipo do erro (sem caminho), e o
    worker sobe mesmo assim: a sobra só ocupa espaço."""
    sweep = getattr(fetcher, "sweep_staging", None)
    if sweep is None:
        return
    try:
        removed = sweep(datetime.now(timezone.utc))
    except OSError as exc:
        logging.getLogger("cq_server.worker").warning(
            "worker.staging_sweep_failed",
            extra={"event": "worker.staging_sweep_failed", "error_type": type(exc).__name__})
        return
    if removed:
        logging.getLogger("cq_server.worker").info(
            "worker.staging_swept", extra={"event": "worker.staging_swept", "removed": int(removed)})


def _config_invalid(exc: ConfigError) -> None:
    """O registro `config.invalid` da configuração (a mensagem de `ConfigError` nunca traz o valor)."""
    logger.error("configuração inválida", extra={"event": "config.invalid", "detail": str(exc)})


def _load_verification(settings: Settings) -> tuple[bool, VerificationChannel | None]:
    """O canal de verificação do ambiente: `(True, canal ou None)`, ou `(False, None)` com o
    registro `config.invalid` (a mensagem de `ConfigError` nunca traz o valor nem a senha)."""
    try:
        return True, build_verification(os.environ, settings)
    except ConfigError as exc:
        logger.error("configuração inválida", extra={"event": "config.invalid", "detail": str(exc)})
        return False, None


# ---------------------------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------------------------


def _migrate(settings: Settings) -> list[str]:
    from cq_server.migrate import apply_migrations, current_version

    applied = apply_migrations(settings.database_url)
    logger.info("migrações aplicadas", extra={
        "event": "migrations.applied", "applied": applied,
        "schema_version": current_version(settings.database_url)})
    return applied


def _cmd_migrate(settings: Settings) -> int:
    _migrate(settings)
    return EXIT_OK


def _cmd_serve(settings: Settings, host: str, port: int, bots: BotRuntime) -> int:
    import uvicorn

    from cq_server.api import create_app
    from cq_server.doctor import DoctorSettings

    # A lista de hosts de repositório antes da migração; inválida → código 2.
    try:
        repo_settings = RepoSettings.from_env(os.environ)
    except ConfigError as exc:
        _config_invalid(exc)
        return EXIT_USAGE
    # Os limites do `doctor`, também antes da migração; inválidos →
    # `config.invalid`, código 2 (a mensagem de `DoctorSettings.from_env` traz só o nome da variável).
    try:
        doctor_settings = DoctorSettings.from_env(os.environ)
    except ValueError as exc:
        _config_invalid(ConfigError(str(exc)))
        return EXIT_USAGE
    _migrate(settings)
    # A cifra do mesmo `BotRuntime` que vai ao `create_app`, com que o
    # `collect` testa a chave-mestra (não vem do ambiente; `None` sem chave).
    doctor_settings = dataclasses.replace(doctor_settings, token_cipher=bots.cipher)
    app = create_app(settings, bots=bots, repo_settings=repo_settings, doctor_settings=doctor_settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # o logging já está configurado (JSON na raiz)
        access_log=False,  # a API registra cada requisição pelo próprio logger
        server_header=False,
    )
    return EXIT_OK


def _cmd_worker(settings: Settings, bots: BotRuntime) -> int:
    from cq_server.worker import run_worker

    loaded, verification = _load_verification(settings)
    if not loaded:
        return EXIT_USAGE
    # O motor, a obtenção e, por último, a entrega, só depois do e-mail.
    try:
        engine = build_engine(os.environ, settings)
    except ConfigError as exc:
        _config_invalid(exc)
        return EXIT_USAGE
    try:
        fetcher = build_repo_fetcher(os.environ, settings)
        # A publicação dos bancos enviados, na mesma raiz; o `fetcher` segue o da obtenção pelo git, intacto.
        bank_fetcher = build_bank_fetcher(os.environ, settings)
        delivery = DeliverySettings.from_env(os.environ)
        # Só esta partida liga a sonda do e-mail, com o canal do `build_verification` (ou
        # `None` sem e-mail: grava `not_configured` sem conexão); o intervalo inválido sai como os demais.
        mail_probe = build_mail_probe(os.environ, settings, verification)
    except ConfigError as exc:
        _config_invalid(exc)
        if engine is not None:
            engine.close()
        return EXIT_USAGE
    _sweep_staging(fetcher)

    stop = threading.Event()
    failure: list[BaseException] = []

    def target() -> None:
        try:
            run_worker(settings, stop=stop, bots=bots, verification=verification, engine=engine,
                       fetcher=fetcher, bank_fetcher=bank_fetcher, delivery=delivery, mail_probe=mail_probe)
        except BaseException as exc:  # noqa: BLE001 - registrado e convertido em código de saída
            failure.append(exc)
            logger.error("o worker parou com erro", exc_info=exc)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        # Só a thread do worker espera no `stop`; o sinal chega na thread principal, que nunca
        # segura a trava do evento, então o `set` aqui não se bloqueia contra ela mesma.
        if not stop.is_set():
            logger.info("parada pedida", extra={"event": "worker.stop_requested",
                                                "signal": signal.Signals(signum).name})
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)

    thread = threading.Thread(target=target, name="cq-worker")
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    finally:
        if engine is not None:
            engine.close()
    if failure:
        return EXIT_FAILURE
    logger.info("worker encerrado", extra={"event": "worker.stopped"})
    return EXIT_OK


def run(argv: Sequence[str] | None = None) -> int:
    """Roda o comando pedido e devolve o código de saída."""
    _setup_process_logging()
    try:
        args = _build_parser().parse_args(argv)
    except _UsageError:
        # Sem o motivo do argparse: ele repete o valor digitado, que pode ser um segredo ou uma
        # identidade.
        logger.error("uso incorreto", extra={"event": "usage.invalid",
                                             "detail": "argumentos inválidos; veja cq-server --help"})
        return EXIT_USAGE

    settings = _load_settings()
    if settings is None:
        return EXIT_USAGE

    bots: BotRuntime | None = None
    if args.command in ("serve", "worker"):
        bots = _load_bot_runtime()
        if bots is None:
            return EXIT_USAGE

    try:
        if args.command == "migrate":
            return _cmd_migrate(settings)
        assert bots is not None
        if args.command == "serve":
            return _cmd_serve(settings, args.host, args.port, bots)
        return _cmd_worker(settings, bots)
    except KeyboardInterrupt:
        logger.info("interrompido", extra={"event": "interrupted", "command": args.command})
        return EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001 - só o tipo e o local saem no log (logs.JsonFormatter)
        logger.exception("falha no comando", extra={"command": args.command})
        return EXIT_FAILURE


def main() -> None:
    """Ponto de entrada do script `cq-server`."""
    sys.exit(run())


if __name__ == "__main__":
    main()
