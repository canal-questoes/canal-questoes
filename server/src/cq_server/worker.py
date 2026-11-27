"""Laço do worker: tarefas, mensagens de saída, bots, varredura e sonda do servidor de e-mail.

`run_worker(settings, *, stop, clock=None, channel=None, handlers=None, bots=None,
verification=None)`:

1. **Canal:** sem `channel`, usa o `ConsoleChannel` escrevendo em `settings.console_output`: `-` é a
   saída padrão (resolvida na chamada, não na importação); outro valor é o caminho de um arquivo,
   aberto em modo acréscimo e fechado ao sair.
2. **Esquema:** antes do laço, espera `current_version == EXPECTED_VERSION`, conferindo a cada
   `poll_interval`. Cada tentativa que falha (ex.: banco fora do ar) registra `event=exception` no
   logger `cq_server.worker` (o `JsonFormatter` só põe o tipo e a posição, nunca a mensagem, que
   pode trazer a URL do banco).
3. **Laço:** em cada iteração, uma tarefa (`TaskQueue.work_once`) e uma mensagem
   (`Outbox.dispatch_once`), que emitem os próprios registros `task.processed`/`outbox.dispatched`.
   Uma `Exception` fora do tratador ou do canal (ex.: o banco caiu) é registrada do mesmo modo, e o
   laço segue depois de `poll_interval`. Sem trabalho, espera até `poll_interval`, acordando antes
   com um aviso de configuração (`notices.NoticeListener`, o aviso como gatilho).
4. **Parada:** o `stop` é conferido entre as tentativas e as iterações, e só ele encerra. Uma
   `BaseException` que não é `Exception` (queda do processo) não é tratada: escapa, e os itens em
   posse ficam para outro worker depois que a posse vencer.

Não configura o logging: só o `cq-server` (`__main__`) faz isso.

**Bots:** com `bots=None`, o
comportamento é o do laço acima, só tarefas e mensagens (nenhuma thread nova). Com um `BotRuntime`:
- os tratadores `telegram.update` (`reception.update_task_handler`) e `bot.sync_profile`
  (`bots.profile_task_handler`) entram no laço; os do chamador têm precedência;
- a entrega passa pelo `bots.TelegramChannel(…, default=channel)`, com
  `Outbox.dispatch_once(…, include_routed=True)` se houver chave-mestra; sem ela, só as mensagens sem
  bot, e o worker registra uma única vez `worker.bots_disabled` (WARNING), sem assumir posse nem
  marcar bots;
- com chave-mestra, depois da espera do esquema, sobe a thread de reconciliação `cq-bots`
  (`_BotSupervisor`): a cada `reconcile_seconds` e a cada aviso de tópico `bot`, solta as próprias
  posses que deixaram de ser elegíveis, assume as elegíveis sem dono ou vencidas e sobe ou para as
  threads de consulta `cq-poll-<id interno>` (`_Poller`); renova as posses a cada `renew_seconds`
  (relógio monotônico) e sempre que falte a folga da posse no `Clock` injetado;
- cada thread de consulta decifra o token uma vez (`reception.poll_token`), usa um objeto da porta
  por posse, começa por `deleteWebhook` com o `offset` vazio, confere a posse antes de cada
  `getUpdates` (`reception.poll_status`), grava cada resposta `ok` antes de o `offset` avançar
  (`reception.store_updates`) e reage a cada falha da consulta (401, 409, 429, espera crescente); toda espera
  dela é um prazo no `Clock` injetado, conferido em fatias reais de até 0,05 s e interrompido pela
  parada;
- **parada ordenada** (`stop`): a reconciliação para as consultas (eventos de parada e `abort()`)
  assim que vê o `stop`; depois do laço, `run_worker` espera cada thread por até 2 s, solta as posses
  numa transação (`bot.lease_changed {released, shutdown}` e o aviso `bot`) e retorna;
- **queda** (`BaseException` escapando do laço): liga os eventos de parada da reconciliação e das
  consultas, chama `abort()`, espera as threads por até 2 s **sem** soltar posses nem escrever no
  banco, e relança;
- uma `Exception` na `cq-bots` ou numa `cq-poll-*` é registrada com `event=exception` (só tipo e
  posição), e o ciclo segue depois de `reconcile_seconds` ou da espera crescente; a thread não morre.

Registros: `bot.lease` (`offering_id`, `change`, `reason`), `bot.poll_problem`
(`offering_id`, `problem`) e `worker.bots_disabled`; nenhum leva token, URL da Bot API, `chat_id`,
`sender_id`, texto de update, código de convite ou link.

**Convites:** `verification` é o canal da porta
`ports.verification` que o tratador `invite.email` (`students.invite_email_task_handler`) usa (`None`:
os envios falham de vez com `not_configured`). O tratador entra sempre no laço, nos dois caminhos (com e
sem `bots`), com a própria conexão (`application_name = cq-worker/invites`, aberta no primeiro uso) e o
`close()` no `ExitStack`; os tratadores do chamador têm precedência.

**Entrega e correção das questões:**
`run_worker(…, engine=None, fetcher=None, delivery=None, console_routes=False, bank_fetcher=None)`: o motor
(`ports.engine`), a obtenção (`ports.repos`), a configuração de entrega (`None` = `DeliverySettings()`),
o modo de console com rota (a entrada simulada do aluno) e a publicação dos bancos enviados (com ela, os
tratadores recebem o roteador `banks.RoutingRepoFetcher`, que manda as referências `bank:` ao banco e as
demais ao `fetcher`; sem ela, o `fetcher` vai intacto).
- sem `bots` e sem `console_routes`: exatamente o caminho dos convites (só o `invite.email`, nenhum tratador
  das questões e nenhuma varredura);
- com `bots` ou com `console_routes=True`: os tratadores das 8 tarefas das questões (`dispatch.validate`,
  `.expire`, `.open`, `.close`, `delivery.prepare`, `.reveal`, `submission.grade` e
  `question.refetch`), cada um com a própria conexão, recebendo o canal com rota (o `TelegramChannel`
  com bots; o `ConsoleChannel` no console), o motor e a obtenção prontos (nada é montado nem chamado na
  subida, e o `formatter` do canal só é lido ao processar uma tarefa); o `telegram.update` com o
  canal e a entrega (no console, `runtime=None`); a fila de saída com o conteúdo e o desfecho das
  mensagens de entrega (`deliveries.DeliveryContentLoader`/`DeliveryOutcomeRecorder`) e as mensagens
  com rota; e a varredura (`scheduler.Sweeper`) só com os tipos que têm tratador;
- com `bots`: o `TelegramChannel(…, delivery=…)`, o `SendPacer` com o ritmo da entrega e o leitor de
  cada bot pelo `get_updates_with_replies`, com o `reply_to` repassado a `store_updates`.
- **recepção durante a correção** (`GRADING_IN_PROGRESS`): a
  chamada `grade` do motor roda numa thread própria (`cq-grade-call`), e, enquanto ela dura, o laço
  processa só as tarefas `telegram.update` (`TaskQueue.work_once(…, task_types=…)`), em ordem de `id`;
  assim um toque ou uma resposta durante a correção recebe "sua resposta está sendo corrigida" sem
  esperar o motor. As demais tarefas e a fila de saída continuam esperando, como no laço único.

**Sonda do servidor de e-mail:** `run_worker(…, mail_probe=None)`: sem o laço, nenhuma sonda do
servidor de e-mail roda (nenhuma conexão ao SMTP, nenhuma escrita em `mail_probe`). Com um `MailProbeLoop`, nos três
caminhos, depois da espera do esquema, sobe a thread `cq-mail-probe`, que roda `run_forever` (o teste na partida e a
cada intervalo) fora do laço principal: não assume posse de tarefa, não cria linha em `tasks` e não segura a fila de
convites e resumos. Ela para pelo mesmo `stop`; ao sair do `run_worker` (também na queda), a parada lhe é sinalizada
e ela é esperada por até `STOP_JOIN_SECONDS` (um teste em curso termina no prazo do SMTP; a thread é daemon). Só o
`__main__` monta o laço (`build_mail_probe`).
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TextIO

from psycopg.conninfo import make_conninfo

from cq_server import db, deliveries, digests, dispatches, grading, previews, reception, reports, students, teamnotices
from cq_server._leasing import MAX_WAIT_SECONDS, LazyConnection
from cq_server.adapters.console import ConsoleChannel
from cq_server.banks import RoutingRepoFetcher
from cq_server.bots import (
    BotRuntime,
    TelegramChannel,
    acquire_leases,
    mark_no_access,
    profile_task_handler,
    release_leases,
    renew_leases,
)
from cq_server.clock import Clock, SystemClock
from cq_server.config import DeliverySettings, Settings
from cq_server.migrate import EXPECTED_VERSION, current_version
from cq_server.notices import NoticeListener
from cq_server.offerings import BOT_NOTICE_TOPIC, PROFILE_TASK_TYPE
from cq_server.outbox import Outbox
from cq_server.pacing import SendPacer
from cq_server.ports.delivery import DeliveryChannel
from cq_server.ports.engine import QuestionEngine, QuestionRef
from cq_server.ports.repos import RepoFetcher
from cq_server.ports.telegram import (
    Aborted,
    ConcurrentConsumer,
    RetryAfter,
    TelegramBotApi,
    TelegramError,
    TokenRejected,
    WebhookActive,
)
from cq_server.ports.verification import VerificationChannel
from cq_server.scheduler import Sweeper
from cq_server.tasks import TaskQueue

if TYPE_CHECKING:
    from cq_server.mail_probe import MailProbeLoop

logger = logging.getLogger("cq_server.worker")

APPLICATION_NAME_PREFIX = "cq-worker/"  # `application_name` por papel

CONSOLE_STDOUT = "-"

# Bots: as esperas das threads de consulta e de reconciliação.
SLICE_SECONDS = 0.05  # fatia real de toda espera das threads dos bots
STOP_JOIN_SECONDS = 2.0  # espera por thread na parada e na queda
CONFLICT_WAIT_SECONDS = 3.0  # 409 de outro consumidor: pelo menos 3 s antes do próximo getUpdates
POLL_BACKOFF_BASE_SECONDS = 2.0  # espera crescente da consulta: min(2·2^(k−1), 60)
POLL_BACKOFF_MAX_SECONDS = 60.0
LISTENER_RETRY_SECONDS = 1.0  # intervalo mínimo entre tentativas de reabrir o ouvinte dos avisos
_MAX_POLL_BACKOFF_EXPONENT = 16  # 2·2^16 já passa do teto de 60 s

# A recepção durante a correção (ver o docstring do módulo).
GRADE_CALL_THREAD_NAME = "cq-grade-call"
GRADE_PUMP_WAIT_SECONDS = 0.05  # espera real entre as consultas da fila sem `telegram.update` pendente

# A thread da sonda do servidor de e-mail.
MAIL_PROBE_THREAD_NAME = "cq-mail-probe"


def _role_conninfo(conninfo: str, role: str) -> str:
    """O `conninfo` com `application_name = cq-worker/<papel>`, para distinguir as
    conexões do worker no `pg_stat_activity` (laço, avisos, reconciliação, consulta, entrega, perfil)."""
    return make_conninfo(conninfo, application_name=f"{APPLICATION_NAME_PREFIX}{role}")


def default_worker_id() -> str:
    """Identificador aleatório do worker (dono das posses), sem nada do host nem do usuário."""
    return f"worker-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def _console_stream(console_output: str) -> Iterator[TextIO]:
    if console_output == CONSOLE_STDOUT:
        yield sys.stdout
        return
    # Uma linha por envio, sempre com `\n` (sem tradução de quebra de linha no Windows).
    with open(console_output, "a", encoding="utf-8", newline="") as stream:
        yield stream


def _wait_for_schema(settings: Settings, stop: threading.Event) -> bool:
    """Espera o esquema esperado; `False` se o `stop` chegou antes."""
    reported: str | None = None
    while not stop.is_set():
        try:
            version = current_version(settings.database_url)
        except Exception:  # noqa: BLE001 - registrada (só tipo e posição) e tentada de novo
            logger.exception("worker.schema_check_failed", extra={"phase": "schema_check"})
        else:
            if version == EXPECTED_VERSION:
                return True
            shown = version or "nenhuma"
            if shown != reported:
                reported = shown
                logger.info("worker.waiting_schema", extra={
                    "event": "worker.waiting_schema", "version": version, "expected": EXPECTED_VERSION})
        stop.wait(settings.poll_interval_seconds)
    return False


class _Idle:
    """Espera ociosa do laço: até `poll_interval`, acordada por um aviso de configuração.

    O ouvinte é só um gatilho: se ele falhar (ex.: banco fora do ar), a falha é registrada, ele é
    descartado e a espera vira um `stop.wait`; na próxima espera, tenta-se de novo.
    """

    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._listener: NoticeListener | None = None

    def wait(self) -> None:
        interval = self._settings.poll_interval_seconds
        if self._stop.is_set():
            return
        try:
            if self._listener is None:
                listener = NoticeListener(_role_conninfo(self._settings.database_url, "notices"))
                listener.__enter__()
                self._listener = listener
            self._listener.wait(interval)
        except Exception:  # noqa: BLE001 - registrada; a espera segue sem o gatilho
            logger.exception("worker.notice_listener_failed", extra={"phase": "notice_listener"})
            self.close()
            self._stop.wait(interval)

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:  # noqa: BLE001 - só limpeza
                pass


class _StopOrExit:
    """A parada da thread da sonda: o `stop` do worker ou a saída do `run_worker` (também na queda). O
    `MailProbeLoop.run_forever` só consulta `is_set()`."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.exited = threading.Event()

    def is_set(self) -> bool:
        return self._stop.is_set() or self.exited.is_set()


@contextlib.contextmanager
def _mail_probe_thread(mail_probe: MailProbeLoop | None, stop: threading.Event) -> Iterator[None]:
    """Sobe a thread `cq-mail-probe` com o laço da sonda (ver o docstring do módulo); sem laço, nada."""
    if mail_probe is None:
        yield
        return
    halt = _StopOrExit(stop)
    thread = threading.Thread(target=mail_probe.run_forever, args=(halt,), name=MAIL_PROBE_THREAD_NAME,
                              daemon=True)
    thread.start()
    try:
        yield
    finally:
        halt.exited.set()
        thread.join(STOP_JOIN_SECONDS)


def _sweep_types(handlers: Mapping[str, Callable]) -> frozenset[str]:
    """Os tipos que a varredura pode enfileirar no laço, os
    que têm tratador (nunca uma tarefa que falharia sem tratador)."""
    return frozenset(handlers)


def _loop(settings: Settings, stop: threading.Event, clock: Clock, channel: DeliveryChannel,
          handlers: Mapping[str, Callable], worker_id: str, *, include_routed: bool = False,
          sweep_types: frozenset[str] | None = None, content: deliveries.DeliveryContent | None = None,
          outcome: deliveries.DeliveryOutcome | None = None, pacer: SendPacer | None = None,
          notice_content: previews.NoticeContentLoader | None = None) -> None:
    """O laço do worker. Com `sweep_types`, a varredura (`scheduler.Sweeper`) roda no mesmo `try`,
    depois da tarefa e da mensagem, só com esses tipos; uma rodada que enfileira ou abandona conta como
    ocupada. Sem `sweep_types` (o caminho sem bots e sem console com rota), nenhuma varredura. A fila
    de saída recebe o conteúdo, o desfecho e o ritmo das mensagens de entrega (`None`: sem as questões).
    `notice_content`: o carregador das mensagens da prévia (`None`: elas não são candidatas)."""
    queue = TaskQueue(_role_conninfo(settings.database_url, "tasks"), clock=clock, settings=settings)
    outbox = Outbox(_role_conninfo(settings.database_url, "outbox"), clock=clock, settings=settings,
                    content=content, outcome=outcome, pacer=pacer, notice_content=notice_content)
    sweeper = (Sweeper(_role_conninfo(settings.database_url, "sweep"), clock=clock, settings=settings,
                       handled_types=sweep_types) if sweep_types is not None else None)
    idle = _Idle(settings, stop)
    try:
        while not stop.is_set():
            busy = False
            try:
                if queue.work_once(worker_id, handlers) is not None:
                    busy = True
                if not stop.is_set() and outbox.dispatch_once(
                        channel, worker_id, include_routed=include_routed) is not None:
                    busy = True
                if sweeper is not None and not stop.is_set():
                    swept = sweeper.maybe_run()
                    if swept is not None and (swept.enqueued or swept.abandoned):
                        busy = True
            except Exception:  # noqa: BLE001 - fora do tratador e do canal; registrada e o laço segue
                logger.exception("worker.iteration_failed", extra={"phase": "loop"})
                stop.wait(settings.poll_interval_seconds)
                continue
            if not busy:
                idle.wait()
    finally:
        idle.close()
        queue.close()
        outbox.close()
        if sweeper is not None:
            sweeper.close()


# ---------------------------------------------------------------------------------------------
# Runtime dos bots
# ---------------------------------------------------------------------------------------------


def _log_lease(offering_id: uuid.UUID | None, change: str, reason: str) -> None:
    """Registro `bot.lease`."""
    logger.info("bot.lease", extra={"event": "bot.lease", "offering_id": str(offering_id),
                                    "change": change, "reason": reason})


def _poll_backoff_seconds(failures: int) -> float:
    """Espera crescente da consulta: `min(2·2^(k−1), 60)` s, k ≥ 1."""
    exponent = min(max(failures - 1, 0), _MAX_POLL_BACKOFF_EXPONENT)
    return min(POLL_BACKOFF_BASE_SECONDS * (2 ** exponent), POLL_BACKOFF_MAX_SECONDS)


class _Poller:
    """A thread de consulta de um bot (`cq-poll-<id interno>`), para uma posse e uma versão do token.

    Decifra o token uma vez, cria um objeto da porta (um por posse) e consulta até perder a posse,
    o bot deixar de ser elegível, a versão do token mudar ou vir a parada (ver o docstring do
    módulo). O `stop_event` é conferido antes de toda escrita no banco: depois da parada (ou da
    queda), a thread não grava mais nada."""

    def __init__(self, supervisor: _BotSupervisor, bot_id: int, token_version: int,
                 offering_id: uuid.UUID) -> None:
        self.bot_id = bot_id
        self.token_version = token_version
        self.offering_id = offering_id
        self.stop_event = threading.Event()
        self._sup = supervisor
        self._lock = threading.Lock()
        self._api: TelegramBotApi | None = None
        self._aborted = False
        self.thread = threading.Thread(target=self._run, name=f"cq-poll-{bot_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def stop(self, *, abort: bool) -> None:
        """Pede o fim da thread; com `abort`, interrompe a chamada em andamento (`abort()` da porta)."""
        self.stop_event.set()
        if abort:
            self.abort()

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            api = self._api
        if api is not None:
            try:
                api.abort()
            except Exception:  # noqa: BLE001 - registrada (só tipo e posição)
                logger.exception("bot.poll_abort_failed", extra={"phase": "poll"})

    # -- a thread ---------------------------------------------------------------------------------

    def _run(self) -> None:
        conn = LazyConnection(_role_conninfo(self._sup.conninfo, "poll"))
        ended_normally = False
        try:
            self._poll(conn)
            ended_normally = True
        except Exception:  # noqa: BLE001 - último recurso: registrada; a reconciliação decide
            logger.exception("bot.poll_failed", extra={"phase": "poll"})
        finally:
            with self._lock:
                api, self._api = self._api, None
                self._aborted = True
            if api is not None:
                try:
                    api.close()
                except Exception:  # noqa: BLE001 - só limpeza
                    pass
            conn.close()
            # Um fim normal (posse perdida, troca de token…) acorda a reconciliação; depois de uma
            # exceção inesperada, ela só sobe outra thread no ciclo seguinte (sem laço apertado).
            if ended_normally:
                self._sup.request(full=True)

    def _now(self) -> datetime:
        return self._sup.clock.now()

    def _deadline(self, since: datetime, seconds: float) -> datetime:
        """O prazo `since + seconds` no `Clock` injetado."""
        return since + timedelta(seconds=min(max(float(seconds), 0.0), MAX_WAIT_SECONDS))

    def _sleep(self, seconds: float) -> bool:
        """Espera `seconds` a partir de agora (ver `_sleep_until`)."""
        return self._sleep_until(self._deadline(self._now(), seconds))

    def _sleep_until(self, deadline: datetime) -> bool:
        """Espera o `Clock` injetado chegar a `deadline`, conferindo em fatias reais de até 0,05 s;
        `False` se veio a parada antes."""
        while not self.stop_event.is_set():
            if self._now() >= deadline:
                return True
            self.stop_event.wait(SLICE_SECONDS)
        return False

    def _token(self, conn: LazyConnection) -> str | None:
        """O token da versão em uso, ou `None` se não há consulta."""
        failures = 0
        while not self.stop_event.is_set():
            cipher = self._sup.runtime.cipher
            if cipher is None:  # pragma: no cover - a reconciliação só roda com chave-mestra
                return None
            try:
                got = reception.poll_token(conn.get(), bot_id=self.bot_id,
                                           token_version=self.token_version, cipher=cipher,
                                           now=self._now())
            except Exception:  # noqa: BLE001 - registrada; tenta de novo depois da espera crescente
                logger.exception("bot.poll_failed", extra={"phase": "poll"})
                failures += 1
                if not self._sleep(_poll_backoff_seconds(failures)):
                    return None
                continue
            if isinstance(got, reception.PollStop):
                if got.opened:
                    if got.unreadable is not None:
                        logger.warning("bot.token_unreadable", extra={
                            "event": "bot.token_unreadable", "offering_id": str(self.offering_id),
                            "reason": got.unreadable})
                    _log_lease(self.offering_id, "released", "no_access")
                return None
            return got
        return None

    def _poll(self, conn: LazyConnection) -> None:
        sup = self._sup
        token = self._token(conn)
        if token is None:
            return
        api = sup.runtime.api_factory(token)
        del token
        with self._lock:
            aborted = self._aborted
            if not aborted:
                self._api = api
        if aborted:
            api.close()
            return

        offset: int | None = None  # nova posse, offset vazio
        delete_webhook = True
        failures = 0
        while not self.stop_event.is_set():
            try:
                status = reception.poll_status(conn.get(), bot_id=self.bot_id, worker_id=sup.worker_id,
                                               token_version=self.token_version, now=self._now(),
                                               settings=sup.bot_settings)
            except Exception:  # noqa: BLE001 - registrada; segue depois da espera crescente
                logger.exception("bot.poll_failed", extra={"phase": "poll"})
                failures += 1
                if not self._sleep(_poll_backoff_seconds(failures)):
                    return
                continue
            if status == reception.POLL_WAIT:
                # Sem a folga: espera a renovação, conferindo de novo em fatias de 0,05 s.
                sup.request(renew=True)
                self.stop_event.wait(SLICE_SECONDS)
                continue
            if status != reception.POLL_OK:
                return

            # O instante da chamada, no `Clock` injetado: a espera crescente conta a partir dele
            # (o prazo não depende de quanto a thread demorou para perceber a falha).
            started = self._now()
            try:
                if delete_webhook:
                    api.delete_webhook()
                    delete_webhook = False
                    continue
                # A mesma chamada `getUpdates`, com a mensagem citada de cada update.
                updates, reply_to = api.get_updates_with_replies(offset=offset)
            except Aborted:
                return
            except WebhookActive:
                self._problem(conn, "webhook_active")
                delete_webhook = True
                continue
            except ConcurrentConsumer:
                # Os prazos são fixados antes de qualquer escrita no banco.
                deadline = self._deadline(self._now(), CONFLICT_WAIT_SECONDS)
                if not self._still_owner(conn):
                    return
                self._problem(conn, "concurrent_consumer")
                if not self._sleep_until(deadline):
                    return
                continue
            except RetryAfter as exc:
                # O `retry_after` é piso; com falhas seguidas acumuladas, vale a espera crescente se
                # for maior. O 429 não conta como falha seguida.
                wait = max(exc.seconds, _poll_backoff_seconds(failures)) if failures else exc.seconds
                deadline = self._deadline(self._now(), wait)
                self._problem(conn, "rate_limited")
                if not self._sleep_until(deadline):
                    return
                continue
            except TokenRejected:
                if not self._lose_access(conn):
                    failures += 1
                    if not self._sleep(_poll_backoff_seconds(failures)):
                        return
                continue
            except TelegramError:
                # `TelegramUnavailable`, `RequestRejected`, `ChatUnreachable` e os demais.
                failures += 1
                deadline = self._deadline(started, _poll_backoff_seconds(failures))
                self._problem(conn, "telegram_unavailable")
                if not self._sleep_until(deadline):
                    return
                continue

            if self.stop_event.is_set():
                return  # parada: o lote não é gravado nem confirmado (o próximo dono o recebe)
            try:
                stored = reception.store_updates(conn.get(), bot_id=self.bot_id, worker_id=sup.worker_id,
                                                 updates=updates, now=self._now(), reply_to=reply_to)
            except Exception:  # noqa: BLE001 - lote descartado, offset mantido, espera crescente
                logger.exception("bot.poll_failed", extra={"phase": "store"})
                failures += 1
                if not self._sleep(_poll_backoff_seconds(failures)):
                    return
                continue
            if stored is None:
                return  # não é mais a dona: nada gravado, nada confirmado
            failures = 0
            # Só depois do commit; resposta vazia: offset vazio.
            offset = max(update.update_id for update in updates) + 1 if updates else None

    def _problem(self, conn: LazyConnection, problem: str) -> None:
        """Grava o problema visível e registra `bot.poll_problem`."""
        if self.stop_event.is_set():
            return
        logger.warning("bot.poll_problem", extra={"event": "bot.poll_problem",
                                                  "offering_id": str(self.offering_id), "problem": problem})
        try:
            reception.record_poll_problem(conn.get(), bot_id=self.bot_id, worker_id=self._sup.worker_id,
                                          problem=problem, now=self._now())
        except Exception:  # noqa: BLE001 - registrada; o problema volta a ser gravado na próxima falha
            logger.exception("bot.poll_failed", extra={"phase": "problem"})

    def _still_owner(self, conn: LazyConnection) -> bool:
        """Depois de um 409 de outro consumidor: se a thread ainda é a dona.
        Sem banco, supõe que sim (a espera de 3 s vale, e a conferência seguinte decide)."""
        try:
            status = reception.poll_status(conn.get(), bot_id=self.bot_id, worker_id=self._sup.worker_id,
                                           token_version=self.token_version, now=self._now(),
                                           settings=self._sup.bot_settings)
        except Exception:  # noqa: BLE001 - registrada
            logger.exception("bot.poll_failed", extra={"phase": "poll"})
            return True
        return status in (reception.POLL_OK, reception.POLL_WAIT)

    def _lose_access(self, conn: LazyConnection) -> bool:
        """401 na consulta: a perda de acesso do bot, com a versão do token em uso
        (outra versão não é derrubada). `False` se o banco falhou (tenta de novo depois)."""
        if self.stop_event.is_set():
            return True
        try:
            opened = mark_no_access(conn.get(), bot_id=self.bot_id, token_version=self.token_version,
                                    problem="token_rejected", now=self._now())
        except Exception:  # noqa: BLE001 - registrada
            logger.exception("bot.poll_failed", extra={"phase": "no_access"})
            return False
        if opened:
            _log_lease(self.offering_id, "released", "no_access")
        return True


class _BotNotices:
    """O ouvinte dos avisos `bot` da reconciliação, numa conexão própria.

    Só um gatilho: se falhar, é registrado e descartado, a espera vira um
    `stop.wait`, e a reabertura é tentada depois de `LISTENER_RETRY_SECONDS`. Ao (re)abrir, pede uma
    reconciliação, porque um aviso pode ter chegado sem ninguém escutando."""

    def __init__(self, conninfo: str, stop: threading.Event) -> None:
        self._conninfo = conninfo
        self._stop = stop
        self._listener: NoticeListener | None = None
        self._retry_at = 0.0

    def wait(self, timeout: float) -> bool:
        """Espera até `timeout` s; `True` se chegou um aviso `bot` (ou se o ouvinte acabou de abrir)."""
        if self._listener is None:
            if time.monotonic() < self._retry_at:
                self._stop.wait(timeout)
                return False
            try:
                listener = NoticeListener(self._conninfo)
                listener.__enter__()
            except Exception:  # noqa: BLE001 - registrada; a espera segue sem o gatilho
                logger.exception("worker.notice_listener_failed", extra={"phase": "bots_notices"})
                self._retry_at = time.monotonic() + LISTENER_RETRY_SECONDS
                self._stop.wait(timeout)
                return False
            self._listener = listener
            return True
        try:
            notices = self._listener.wait(timeout)
        except Exception:  # noqa: BLE001 - registrada; o ouvinte é reaberto depois
            logger.exception("worker.notice_listener_failed", extra={"phase": "bots_notices"})
            self.close()
            self._retry_at = time.monotonic() + LISTENER_RETRY_SECONDS
            return False
        return any(notice.topic == BOT_NOTICE_TOPIC for notice in notices)

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except Exception:  # noqa: BLE001 - só limpeza
                pass


class _BotSupervisor:
    """A reconciliação dos bots (thread `cq-bots`) e as threads de consulta dela.

    `_pollers` são os bots cuja posse este processo gere (`held` de `bots.acquire_leases`): thread
    de consulta viva ou esperando o reinício por troca de `token_version`. `_lease_until` guarda o
    fim de cada posse no `Clock` injetado, para renovar quando falta a folga da posse.
    Só a thread `cq-bots` muda os dois; a thread do laço lê na parada, sob o `_lock`."""

    def __init__(self, settings: Settings, runtime: BotRuntime, clock: Clock, worker_id: str,
                 worker_stop: threading.Event) -> None:
        self.conninfo = settings.database_url
        self.runtime = runtime
        self.bot_settings = runtime.settings
        self.clock = clock
        self.worker_id = worker_id
        self._worker_stop = worker_stop
        self._lock = threading.Lock()
        self._pollers: dict[int, _Poller] = {}
        self._lease_until: dict[int, datetime] = {}
        self._offering_of: dict[int, uuid.UUID] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._want_full = True
        self._want_renew = False
        self._thread = threading.Thread(target=self._run, name="cq-bots", daemon=True)

    # -- controle (outras threads) ----------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def request(self, *, full: bool = False, renew: bool = False) -> None:
        """Acorda a reconciliação: `full` pede uma reconciliação; `renew`, uma renovação."""
        with self._lock:
            self._want_full = self._want_full or full
            self._want_renew = self._want_renew or renew
        self._wake.set()

    def shutdown(self) -> None:
        """Parada ordenada: para as consultas (`abort()`), espera cada thread por até 2 s e solta as
        posses numa transação, com `bot.lease_changed {released, shutdown}` e o aviso `bot`."""
        self._halt()
        with self._lock:
            ids = set(self._pollers) | set(self._lease_until)
            offering_of = dict(self._offering_of)
        try:
            with db.connect(_role_conninfo(self.conninfo, "bots"), autocommit=True) as conn:
                # Todas as posses deste worker no banco, e não só as da memória (a parada
                # ordenada solta todas): uma assunção confirmada cuja atualização da memória
                # falhou também é solta.
                owned = reception.own_leases(conn, worker_id=self.worker_id)
                for bot_id, lease in owned.items():
                    offering_of.setdefault(bot_id, lease.offering_id)
                ids |= set(owned)
                if not ids:
                    return
                released = release_leases(conn, worker_id=self.worker_id, bot_ids=sorted(ids),
                                          now=self.clock.now(), reason="shutdown")
        except Exception:  # noqa: BLE001 - registrada; as posses vencem sozinhas
            logger.exception("worker.bots_release_failed", extra={"phase": "shutdown"})
            return
        for bot_id in released:
            _log_lease(offering_of.get(bot_id), "released", "shutdown")

    def crash(self) -> None:
        """Queda: liga os eventos de parada, chama `abort()` e espera as threads por até 2 s, sem
        soltar posses nem escrever no banco."""
        self._halt()

    def _halt(self) -> None:
        self._stop.set()
        self._wake.set()
        pollers = self._stop_pollers()
        deadline = time.monotonic() + STOP_JOIN_SECONDS
        if self._thread.is_alive():
            self._thread.join(max(0.0, deadline - time.monotonic()))
        # Nenhuma thread nova depois da parada (a criação confere `_halted()` sob o `_lock`).
        for poller in pollers + self._stop_pollers():
            if poller.is_alive():
                poller.thread.join(max(0.0, deadline - time.monotonic()))

    def _stop_pollers(self) -> list[_Poller]:
        with self._lock:
            pollers = list(self._pollers.values())
        for poller in pollers:
            poller.stop(abort=True)
        return pollers

    # -- a thread cq-bots -------------------------------------------------------------------------

    def _halted(self) -> bool:
        return self._stop.is_set() or self._worker_stop.is_set()

    def _run(self) -> None:
        conn = LazyConnection(_role_conninfo(self.conninfo, "bots"))
        notices = _BotNotices(_role_conninfo(self.conninfo, "bot-notices"), self._stop)
        cfg = self.bot_settings
        next_full = time.monotonic()
        next_renew = time.monotonic() + cfg.renew_seconds
        try:
            while not self._halted():
                self._wake.clear()
                with self._lock:
                    full, self._want_full = self._want_full, False
                    renew, self._want_renew = self._want_renew, False
                now_m = time.monotonic()
                full = full or now_m >= next_full
                renew = renew or now_m >= next_renew or self._renew_due()
                try:
                    if renew:
                        self._renew(conn)
                        next_renew = time.monotonic() + cfg.renew_seconds
                    if full and not self._halted():
                        self._reconcile(conn)
                        next_full = time.monotonic() + cfg.reconcile_seconds
                except Exception:  # noqa: BLE001 - registrada; o ciclo segue depois de reconcile_seconds
                    logger.exception("worker.bots_failed", extra={"phase": "bots"})
                    self._pause(cfg.reconcile_seconds)
                    continue
                self._idle(notices, min(next_full, next_renew))
        finally:
            # A parada do worker (ou a queda) chega aqui: as consultas param já, em paralelo com o
            # fim do laço; a soltura das posses fica com `shutdown`, na thread do laço.
            if self._halted():
                self._stop_pollers()
            notices.close()
            conn.close()

    def _pause(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._halted():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, SLICE_SECONDS))

    def _idle(self, notices: _BotNotices, deadline: float) -> None:
        """Espera até `deadline` (monotônico), um aviso `bot`, um pedido das consultas, a falta de
        folga numa posse ou a parada."""
        while not self._halted() and not self._wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._renew_due():
                return
            if notices.wait(min(remaining, SLICE_SECONDS)):
                with self._lock:
                    self._want_full = True
                return

    def _renew_due(self) -> bool:
        """Alguma posse com menos de `poll_timeout + telegram_timeout + renew_seconds` pela frente,
        no `Clock` injetado (hora de renovar)."""
        cfg = self.bot_settings
        margin = timedelta(seconds=cfg.poll_timeout_seconds + cfg.telegram_timeout_seconds
                           + cfg.renew_seconds)
        now = self.clock.now()
        with self._lock:
            untils = list(self._lease_until.values())
        return any(until - now < margin for until in untils)

    def _renew(self, conn: LazyConnection) -> None:
        """Renova as posses geridas; uma linha não devolvida é posse perdida: a consulta para (com
        `abort()`), sem soltar."""
        with self._lock:
            ids = sorted(self._lease_until)
        if not ids or self._halted():
            return
        now = self.clock.now()
        kept = renew_leases(conn.get(), worker_id=self.worker_id, bot_ids=ids, now=now,
                            lease_seconds=self.bot_settings.lease_seconds)
        until = now + timedelta(seconds=self.bot_settings.lease_seconds)
        lost: list[_Poller] = []
        with self._lock:
            for bot_id in ids:
                if bot_id in kept:
                    if bot_id in self._lease_until:
                        self._lease_until[bot_id] = until
                    continue
                self._lease_until.pop(bot_id, None)
                poller = self._pollers.get(bot_id)
                if poller is not None:
                    lost.append(poller)
        for poller in lost:
            poller.stop(abort=True)

    def _reconcile(self, conn: LazyConnection) -> None:
        """Uma reconciliação: solta as próprias posses que deixaram de ser
        elegíveis, acerta as threads de consulta com as posses do banco e assume as elegíveis."""
        c = conn.get()
        now = self.clock.now()
        own = reception.own_leases(c, worker_id=self.worker_id)

        stale = sorted(bot_id for bot_id, lease in own.items() if not lease.eligible)
        if stale:
            with self._lock:
                stopping = [self._pollers[bot_id] for bot_id in stale if bot_id in self._pollers]
            for poller in stopping:
                poller.stop(abort=True)
            if self._halted():
                return
            released = release_leases(c, worker_id=self.worker_id, bot_ids=stale, now=now,
                                      reason="not_eligible")
            with self._lock:
                for bot_id in released:
                    self._lease_until.pop(bot_id, None)
            for bot_id in released:
                _log_lease(own[bot_id].offering_id, "released", "not_eligible")

        eligible = {bot_id: lease for bot_id, lease in own.items() if lease.eligible}
        to_abort: list[_Poller] = []
        to_retire: list[_Poller] = []
        to_start: list[_Poller] = []
        with self._lock:
            for bot_id, poller in list(self._pollers.items()):
                lease = eligible.get(bot_id)
                if lease is None:
                    # Posse perdida (ou o bot deixou de ser elegível): a consulta para, sem soltar.
                    self._lease_until.pop(bot_id, None)
                    if poller.is_alive():
                        to_abort.append(poller)
                    else:
                        del self._pollers[bot_id]
                    continue
                if lease.lease_until is not None:
                    self._lease_until[bot_id] = lease.lease_until
                self._offering_of[bot_id] = lease.offering_id
                if poller.is_alive():
                    if poller.token_version != lease.token_version:
                        # Troca de token com o mesmo dono: a thread sai depois da chamada em curso
                        # e outra sobe com o token novo (sem evento de posse).
                        to_retire.append(poller)
                    continue
                if not self._halted():
                    fresh = _Poller(self, bot_id, lease.token_version, lease.offering_id)
                    self._pollers[bot_id] = fresh
                    to_start.append(fresh)
        for poller in to_abort:
            poller.stop(abort=True)
        for poller in to_retire:
            poller.stop(abort=False)
        for poller in to_start:
            poller.start()

        if self._halted():
            return
        with self._lock:
            held = sorted(self._pollers)
        leased = acquire_leases(c, worker_id=self.worker_id, now=now,
                                lease_seconds=self.bot_settings.lease_seconds, held=held)
        if not leased:
            return
        offerings = reception.offering_ids(c, [bot.bot_id for bot in leased])
        until = now + timedelta(seconds=self.bot_settings.lease_seconds)
        to_abort = []
        to_start = []
        with self._lock:
            for bot in leased:
                offering_id = offerings[bot.bot_id]
                self._lease_until[bot.bot_id] = until
                self._offering_of[bot.bot_id] = offering_id
                previous = self._pollers.get(bot.bot_id)
                if previous is not None:
                    # Uma consulta de uma posse anterior ainda termina: a nova sobe quando ela sair.
                    to_abort.append(previous)
                    continue
                if not self._halted():
                    fresh = _Poller(self, bot.bot_id, bot.token_version, offering_id)
                    self._pollers[bot.bot_id] = fresh
                    to_start.append(fresh)
        for bot in leased:
            _log_lease(offerings[bot.bot_id], "acquired", "acquired")
        for poller in to_abort:
            poller.stop(abort=True)
        for poller in to_start:
            poller.start()


class _GradingEngine:
    """O motor visto pela correção (ver o docstring do módulo): `grade` roda numa thread própria e,
    enquanto ela dura, o `pump` (preso depois de montados os tratadores) processa as tarefas
    `telegram.update`; sem `pump`, ou com a parada pedida, a chamada é a de sempre. O resultado e as
    exceções do motor (inclusive uma `BaseException`) voltam a quem chamou como se a chamada fosse
    direta; os demais métodos vão direto ao motor."""

    def __init__(self, engine: QuestionEngine, stop: threading.Event) -> None:
        self._engine = engine
        self._stop = stop
        self._pump: Callable[[], bool] | None = None

    def attach(self, pump: Callable[[], bool]) -> None:
        self._pump = pump

    def inspect(self, ref: QuestionRef) -> Any:
        return self._engine.inspect(ref)

    def create_variant(self, ref: QuestionRef, seed: str) -> Any:
        return self._engine.create_variant(ref, seed)

    def close(self) -> None:
        self._engine.close()

    def grade(self, ref: QuestionRef, snapshot: Mapping[str, Any], submitted: Mapping[str, Any]) -> Any:
        pump = self._pump
        if pump is None or self._stop.is_set():
            return self._engine.grade(ref, snapshot, submitted)
        outcome: dict[str, Any] = {}

        def call() -> None:
            try:
                outcome["value"] = self._engine.grade(ref, snapshot, submitted)
            except BaseException as exc:  # noqa: BLE001 - relançada na thread do laço
                outcome["error"] = exc

        thread = threading.Thread(target=call, name=GRADE_CALL_THREAD_NAME, daemon=True)
        thread.start()
        pumping = True
        while thread.is_alive():
            if pumping and not self._stop.is_set():
                try:
                    if pump():
                        continue
                except Exception:  # noqa: BLE001 - registrada; a correção segue sem a recepção
                    logger.exception("worker.grade_pump_failed", extra={"phase": "grade_pump"})
                    pumping = False
            thread.join(GRADE_PUMP_WAIT_SECONDS)
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]


def _attach_grade_pump(settings: Settings, resources: contextlib.ExitStack, clock: Clock,
                       grading_engine: _GradingEngine | None, handlers: Mapping[str, Callable],
                       worker_id: str) -> None:
    """Prende ao motor da correção a recepção durante a correção: uma fila própria (conexão aberta no
    primeiro uso) que só pega `telegram.update`, com o tratador final do laço (o do chamador, se houver)."""
    update_handler = handlers.get(reception.UPDATE_TASK_TYPE)
    if grading_engine is None or update_handler is None:
        return
    queue = TaskQueue(_role_conninfo(settings.database_url, "grade-pump"), clock=clock, settings=settings)
    resources.callback(queue.close)
    only = (reception.UPDATE_TASK_TYPE,)
    route = {reception.UPDATE_TASK_TYPE: update_handler}
    grading_engine.attach(lambda: queue.work_once(worker_id, route, task_types=only) is not None)


def _question_tasks(settings: Settings, resources: contextlib.ExitStack, *, clock: Clock,
                 channel: DeliveryChannel, engine: QuestionEngine | None,
                 fetcher: RepoFetcher | None,
                 grade_engine: QuestionEngine | None = None) -> dict[str, Callable]:
    """Os tratadores das 8 tarefas das questões, cada um com a própria
    conexão (aberta no primeiro uso) e o `close()` no `ExitStack`. Nada lê o `formatter` do canal nem
    chama o motor ou a obtenção aqui. `grade_engine`: o motor da correção, quando
    diferente do das outras tarefas (o `_GradingEngine`)."""

    def conninfo(role: str) -> str:
        return _role_conninfo(settings.database_url, role)

    built: dict[str, Callable] = {
        dispatches.VALIDATE_TASK_TYPE: dispatches.validate_task_handler(
            conninfo("validate"), clock=clock, settings=settings, engine=engine, fetcher=fetcher,
            channel=channel),
        dispatches.EXPIRE_TASK_TYPE: dispatches.expire_task_handler(conninfo("expire"), clock=clock,
                                                                    settings=settings),
        dispatches.OPEN_TASK_TYPE: dispatches.open_task_handler(conninfo("open"), clock=clock, settings=settings),
        dispatches.CLOSE_TASK_TYPE: dispatches.close_task_handler(conninfo("close"), clock=clock,
                                                                  settings=settings, channel=channel),
        deliveries.PREPARE_TASK_TYPE: deliveries.prepare_task_handler(
            conninfo("prepare"), clock=clock, settings=settings, engine=engine, channel=channel),
        deliveries.REVEAL_TASK_TYPE: deliveries.reveal_task_handler(conninfo("reveal"), clock=clock,
                                                                    settings=settings, channel=channel),
        grading.GRADE_TASK_TYPE: grading.grade_task_handler(
            conninfo("grade"), clock=clock, settings=settings,
            engine=grade_engine if grade_engine is not None else engine, channel=channel),
        grading.REFETCH_TASK_TYPE: grading.refetch_task_handler(conninfo("refetch"), clock=clock,
                                                                settings=settings, fetcher=fetcher),
    }
    for handler in built.values():
        resources.callback(handler.close)  # type: ignore[attr-defined]
    return built


def _report_handlers(settings: Settings, resources: contextlib.ExitStack, *, clock: Clock,
                 channel: DeliveryChannel, engine: QuestionEngine | None,
                 delivery: DeliverySettings) -> dict[str, Callable]:
    """Os tratadores dos relatórios e avisos, só no worker com bots e no console com rota (no
    caminho só dos convites, nenhum), com o canal com rota e a própria conexão de cada um (aberta no primeiro uso),
    com o `close()` no `ExitStack`. Nada lê o `formatter` do canal nem chama o motor aqui. São
    `report.freeze`, os de `teamnotices.task_handlers` (`notice.send`), `preview.send`, com o
    motor e o `DeliverySettings` do worker (o mesmo do `DeliveryContentLoader`), e `digest.daily`."""
    built: dict[str, Callable] = {
        reports.FREEZE_TASK_TYPE: reports.freeze_task_handler(
            _role_conninfo(settings.database_url, "report-freeze"), clock=clock, settings=settings,
            channel=channel),
        previews.PREVIEW_TASK_TYPE: previews.preview_task_handler(
            _role_conninfo(settings.database_url, "preview"), clock=clock, settings=settings, engine=engine,
            channel=channel, delivery=delivery),
        digests.DIGEST_TASK_TYPE: digests.digest_task_handler(
            _role_conninfo(settings.database_url, "digest"), clock=clock, settings=settings, channel=channel),
    }
    built.update(teamnotices.task_handlers(
        _role_conninfo(settings.database_url, "team-notices"), clock=clock, settings=settings,
        channel=channel))
    for handler in built.values():
        resources.callback(handler.close)  # type: ignore[attr-defined]
    return built


def _delivery_collaborators(clock: Clock, channel: DeliveryChannel, delivery: DeliverySettings,
                            ) -> tuple[deliveries.DeliveryContent, deliveries.DeliveryOutcome]:
    """O conteúdo e o desfecho das mensagens de entrega da fila, sobre o canal com
    rota (o formatador do dialeto gravado é lido na posse, nunca aqui)."""
    return (deliveries.DeliveryContentLoader(channel, delivery=delivery),
            deliveries.DeliveryOutcomeRecorder(channel, clock=clock))


def run_worker(settings: Settings, *, stop: threading.Event, clock: Clock | None = None,
               channel: DeliveryChannel | None = None,
               handlers: Mapping[str, Callable] | None = None,
               bots: BotRuntime | None = None,
               verification: VerificationChannel | None = None,
               engine: QuestionEngine | None = None,
               fetcher: RepoFetcher | None = None,
               delivery: DeliverySettings | None = None,
               console_routes: bool = False,
               mail_probe: MailProbeLoop | None = None,
               bank_fetcher: RepoFetcher | None = None) -> None:
    """Processa tarefas e mensagens até o `stop` (ver o docstring do módulo).

    `mail_probe` é o laço do teste do servidor de e-mail; sem ele (`None`), nenhuma sonda roda. Com
    ele, a thread `cq-mail-probe` sobe depois da espera do esquema, nos três caminhos.

    `bank_fetcher` é a publicação dos bancos de questões enviados (as referências `bank:`). Só com ele os
    tratadores recebem o roteador da obtenção (`banks.RoutingRepoFetcher`), com o `fetcher` no lado do git e o
    `bank_fetcher` no do banco; sem ele, recebem o `fetcher` intacto, o mesmo objeto (ou `None`)."""
    clock = clock if clock is not None else SystemClock()
    if bank_fetcher is not None:
        fetcher = RoutingRepoFetcher(git=fetcher, bank=bank_fetcher)
    delivery = delivery if delivery is not None else DeliverySettings()
    caller_handlers = dict(handlers) if handlers is not None else {}
    worker_id = settings.worker_id or default_worker_id()

    with contextlib.ExitStack() as resources:
        if channel is None:
            stream = resources.enter_context(_console_stream(settings.console_output))
            channel = ConsoleChannel(stream)
        # O tratador `invite.email`, nos dois caminhos; a conexão dele só abre no primeiro uso.
        invite_handler = students.invite_email_task_handler(
            _role_conninfo(settings.database_url, "invites"), clock=clock, settings=settings,
            channel=verification)
        resources.callback(invite_handler.close)
        if bots is None and not console_routes:
            base_handlers: dict[str, Callable] = {students.INVITE_EMAIL_TASK_TYPE: invite_handler}
            base_handlers.update(caller_handlers)
            if not _wait_for_schema(settings, stop):
                return
            logger.info("worker.started", extra={"event": "worker.started", "worker_id": worker_id})
            resources.enter_context(_mail_probe_thread(mail_probe, stop))
            _loop(settings, stop, clock, channel, base_handlers, worker_id)
            return

        if bots is None:
            # O console com rota. As mensagens com rota vão ao `ConsoleChannel`,
            # e o `telegram.update` (a entrada simulada gravada pela `ConsoleInbox`) roda sem runtime.
            grading_engine = _GradingEngine(engine, stop) if engine is not None else None
            console_handlers: dict[str, Callable] = _question_tasks(settings, resources, clock=clock, channel=channel,
                                                                 engine=engine, fetcher=fetcher,
                                                                 grade_engine=grading_engine)
            update_handler = reception.update_task_handler(
                _role_conninfo(settings.database_url, "updates"), clock=clock, runtime=None, settings=settings,
                channel=channel, delivery=delivery)
            resources.callback(update_handler.close)
            console_handlers[reception.UPDATE_TASK_TYPE] = update_handler
            console_handlers[students.INVITE_EMAIL_TASK_TYPE] = invite_handler
            console_handlers.update(_report_handlers(settings, resources, clock=clock, channel=channel, engine=engine,
                                                 delivery=delivery))
            console_handlers.update(caller_handlers)
            _attach_grade_pump(settings, resources, clock, grading_engine, console_handlers, worker_id)
            content, outcome = _delivery_collaborators(clock, channel, delivery)
            if not _wait_for_schema(settings, stop):
                return
            logger.info("worker.started", extra={"event": "worker.started", "worker_id": worker_id})
            resources.enter_context(_mail_probe_thread(mail_probe, stop))
            _loop(settings, stop, clock, channel, console_handlers, worker_id, include_routed=True,
                  sweep_types=_sweep_types(console_handlers), content=content, outcome=outcome,
                  notice_content=previews.NoticeContentLoader())
            return

        telegram_channel = TelegramChannel(_role_conninfo(settings.database_url, "telegram"),
                                           clock=clock, runtime=bots, default=channel, delivery=delivery)
        resources.callback(telegram_channel.close)
        update_handler = reception.update_task_handler(_role_conninfo(settings.database_url, "updates"),
                                                       clock=clock, runtime=bots, settings=settings,
                                                       channel=telegram_channel, delivery=delivery)
        resources.callback(update_handler.close)
        bot_grading_engine = _GradingEngine(engine, stop) if engine is not None else None
        all_handlers: dict[str, Callable] = _question_tasks(settings, resources, clock=clock, channel=telegram_channel,
                                                         engine=engine, fetcher=fetcher,
                                                         grade_engine=bot_grading_engine)
        all_handlers.update({
            reception.UPDATE_TASK_TYPE: update_handler,
            PROFILE_TASK_TYPE: profile_task_handler(_role_conninfo(settings.database_url, "profile"),
                                                    clock=clock, runtime=bots, settings=settings),
            students.INVITE_EMAIL_TASK_TYPE: invite_handler,
        })
        all_handlers.update(_report_handlers(settings, resources, clock=clock, channel=telegram_channel, engine=engine,
                                     delivery=delivery))
        all_handlers.update(caller_handlers)
        _attach_grade_pump(settings, resources, clock, bot_grading_engine, all_handlers, worker_id)
        content, outcome = _delivery_collaborators(clock, telegram_channel, delivery)
        pacer = SendPacer(chat_interval_seconds=delivery.chat_interval_seconds,
                          bot_per_second=delivery.bot_per_second)

        if not _wait_for_schema(settings, stop):
            return
        logger.info("worker.started", extra={"event": "worker.started", "worker_id": worker_id})
        resources.enter_context(_mail_probe_thread(mail_probe, stop))
        has_key = bots.cipher is not None
        supervisor: _BotSupervisor | None = None
        if has_key:
            supervisor = _BotSupervisor(settings, bots, clock, worker_id, stop)
            supervisor.start()
        else:
            logger.warning("worker.bots_disabled", extra={"event": "worker.bots_disabled"})
        try:
            _loop(settings, stop, clock, telegram_channel, all_handlers, worker_id,
                  include_routed=has_key, sweep_types=_sweep_types(all_handlers), content=content,
                  outcome=outcome, pacer=pacer, notice_content=previews.NoticeContentLoader())
        except BaseException:
            if supervisor is not None:
                supervisor.crash()
            raise
        if supervisor is not None:
            supervisor.shutdown()
