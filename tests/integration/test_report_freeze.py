"""Congelamento do relatório e resumo à equipe no fechamento.



Cobre:
- o fechamento sem correção pendente → a versão 1 `closing`, o `report.frozen` e o resumo a cada membro com
  endereço ativo, com os números da versão congelada; o resumo composto pelo `ChannelSummaryRenderer`
  sobre o `content` gravado, sem distribuição nem anexo; o `team_notices.report_id` ligando o
  aviso à versão e o texto em `outbox_messages.text`; a chave `notice:<id>:p<pessoa>`; o
  `notice.recorded`; a tarefa criada pela varredura; a reexecução sem efeito; a versão 1 imutável;
- a correção no prazo pendente: o congelamento só depois dela; sem ela em 10 min, a versão 1 com
  `grading_pending`; a correção que termina depois do congelamento fica fora da versão 1 e entra no cálculo
  no momento e na versão 2 do reenvio;
- as atrasadas recebidas antes do congelamento dentro da versão 1 e o reenvio entregue:
  a versão 2 com o par "com atrasadas" diferente, o `summary_resent` composto com
  `resend=True`, um resumo por membro, a reexecução sem duplicar;
- a queda do worker antes de congelar, depois de congelar e antes de enfileirar → uma versão e um resumo por
  destinatário; a varredura que reanima o congelamento de um disparo fechado sem versão;
- o arquivamento com disparo aberto → congela e envia como no horário, e os já fechados não ganham versão
  nova;
- sem destinatário → versão congelada e aviso `no_recipient` com evento;
- o piso por fato: o `dispatch.closed` no piso ou abaixo → congela e o aviso fica `not_sent`; o disparo
  aberto antes do piso e fechado depois → resumo enviado;
- concorrência com duas conexões (a ordem das travas): congelamento × reenvio (o
  reenvio na espera é recusado; durante o congelamento, ele espera e congela a versão 2; a versão 1 continua
  `closing`) e congelamento × `notice.send` do "aberto" antecipado pendente, sem impasse.

**Montagem** (dublês e injeção de falhas nos testes; sem `UPDATE` de estado do disparo por SQL):
- a **equipe simulada** pela fixture `team_members` (`tests/helpers/team.py`: o oferecimento `active` com
  bot próprio e os membros vinculados pelo link de equipe, pelo Telegram, com endereço ativo,
  bloqueado ou sem endereço); as mensagens à equipe lidas por `team_messages` (por aviso e destinatário,
  na ordem da fila);
- os alunos importados pela API da lista e vinculados por SQL (matrícula ativa com o termo aceito e o endereço
  `active` no bot), como em `tests/integration/test_answers_typed.py`; o worker do console com rota
  (`run_worker(console_routes=True)`, motor e obtenção falsos) valida, abre, entrega e corrige; os alunos
  respondem pela `ConsoleInbox` (`ConsoleStudent`) com o relógio avançado antes de cada resposta; quando o
  worker precisa rodar depois do fechamento (as atrasadas), o tratador `report.freeze` dele é substituído por
  um que não faz nada (`handlers=`, como o `_Gate` dos testes do disparo), e o congelamento do teste é o do tratador real;
- os disparos sem alvo pelos tratadores reais do núcleo, com a posse pelo `TaskQueue.claim` só do tipo
  pedido; o `dispatch.close` pela varredura real (`DISPATCH_RULES`);
- o **congelamento** e o **envio dos avisos** pelos tratadores reais (`reports.freeze_task_handler` e o
  `notice.send` de `teamnotices.task_handlers`), montados antes da varredura, sobre as tarefas que a varredura
  cria com as regras de `scheduler.REPORT_RULES`; o canal é o `TelegramChannel` com rota (o do worker com bots,
  modo padrão `rich`), e o resumo esperado é o `ChannelSummaryRenderer(channel.formatter, …)` sobre o
  `content` gravado;
- a queda do worker é um `Crash(BaseException)` (`tests/helpers/channels.py`): no primeiro uso do formatador do
  canal (antes do cálculo, portanto antes de gravar) ou em `outbox.enqueue_notice_text` (a função do módulo pela
  qual o `teamnotices.deliver` enfileira: depois de gravar a versão, antes de enfileirar); a queda depois
  do COMMIT é a tarefa processada e não concluída;
- o piso é gravado como a migração `0005` o gravaria sobre os fatos já existentes (o maior
  `events.seq` em `team_notice_floors.event_floor`), simulando o deploy entre os dois fatos;
- a concorrência segura uma transação no meio por um ponto de espera numa função do módulo usada no caminho
  (`teamnotices.record` no congelamento, depois do INSERT da versão; `teamnotices.team_recipients` no
  `notice.send`, depois de travar o aviso e antes das identidades) e confere, por `pg_stat_activity`, que a
  outra transação está esperando uma trava antes de soltar a primeira.

**Números, à mão** (questão `number`, gabarito 6,5): cada cenário traz os seus no
docstring do teste; os quartis com n ≥ 2 pelo `statistics.quantiles(..., n=4, method="inclusive")` e a mediana
pelo `statistics.median`; n = 1 com o próprio valor nos três.

Ids internos, `seq`, `report_uuid` e pseudônimos nunca como literal esperado: só as relações e as formas.

**Vermelho no esqueleto**: `reports.freeze_task_handler` e
`teamnotices.task_handlers` lançam `NotImplementedError`, as rotas dos relatórios não existem e as tabelas
`dispatch_reports`, `team_notices` e `team_notice_floors` não existem. Cada função monta o tratador do
congelamento antes da primeira conferência dos relatórios e falha nele; as asserções sobre o disparo (preparo, validação,
abertura, respostas, fechamento, arquivamento) ficam na mesma função; o reenvio e o `notice.send` do
"aberto" antecipado ficam verdes depois, com as suas implementações.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import statistics
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import outbox, reports, scheduler, teamnotices
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.tasks import TaskQueue, enqueue_in_transaction
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    get_report,
    import_students,
    list_events,
    prepare_dispatch,
    register_bot,
    resend_report_summary,
)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import Crash
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent
from tests.helpers.engine import preset
from tests.helpers.team import ACTIVE, BLOCKED, NO_ENDPOINT, team_messages

ADMIN = auth(ADMIN_TOKEN)
REPO = "https://repos.cq.test/disciplina/questoes-congelamento.git"
COMMIT = "f0e1d2c3" * 5
COMMIT_SHORT = COMMIT[:12]
QID = "number"
TITLE = "Velocidade média"
LABEL = "Velocidade:"
WINDOW = timedelta(minutes=10)
WORKER = "teste-congelamento"
ENGINE_TIMEOUT = 10.0
WAIT = 20.0
FREEZE_WAIT = timedelta(seconds=reports.FREEZE_WAIT_SECONDS)

_counter = itertools.count(1)
_users = itertools.count(1200)


# ---------------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Fx:
    client: Any
    sql: psycopg.Connection
    db_url: str
    clock: Any
    settings: Any
    bots: Any
    bot_api: Any
    engine_fake: Any
    repo_fetcher: Any
    worker_thread: Any
    closers: list = dataclasses.field(default_factory=list)


@pytest.fixture
def fx(client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any, bots: Any, bot_api: Any,
       engine_fake: Any, repo_fetcher: Any, worker_thread: Any) -> Iterator[_Fx]:
    engine_fake.program((REPO, COMMIT, QID), preset("number"))
    made = _Fx(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, bots=bots, bot_api=bot_api,
               engine_fake=engine_fake, repo_fetcher=repo_fetcher, worker_thread=worker_thread)
    yield made
    worker_thread.stop()
    for close in reversed(made.closers):
        close()


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _oref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _engine(fx: _Fx) -> HttpQuestionEngine:
    engine = HttpQuestionEngine(EngineSettings(engine_url=fx.engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    fx.closers.append(engine.close)
    return engine


def _channel(fx: _Fx) -> Any:
    """O canal com rota do worker com bots, no modo padrão (`rich`)."""
    from cq_server.bots import TelegramChannel

    channel = TelegramChannel(fx.db_url, clock=fx.clock, runtime=fx.bots, default=ConsoleChannel(io.StringIO()),
                              delivery=DeliverySettings())
    fx.closers.append(channel.close)
    return channel


def _bot_row(fx: _Fx, offering: dict) -> int:
    return int(fx.sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id "
                              "WHERE o.offering_id = %s", (uuid.UUID(offering["offering_id"]),)).fetchone()[0])


def _offering(fx: _Fx, prefix: str) -> dict:
    """Um oferecimento `active` com bot próprio e sem equipe."""
    offering = create_offering(fx.client, ADMIN_TOKEN, code=f"{prefix}-{next(_counter)}")
    bot = fx.bot_api.add_bot()
    register_bot(fx.client, ADMIN_TOKEN, offering["code"], fx.bot_api.token_of(bot))
    assert activate_bot(fx.client, ADMIN_TOKEN, offering["code"])["state"] == "active"
    return offering


def _row(fx: _Fx, dispatch_id: str) -> int:
    return int(fx.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                              (uuid.UUID(dispatch_id),)).fetchone()[0])


def _person_row(fx: _Fx, pseudo_id: str) -> int:
    return int(fx.sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (uuid.UUID(pseudo_id),)).fetchone()[0])


def _sweep(fx: _Fx, rules: Any) -> None:
    with psycopg.connect(fx.db_url, autocommit=True) as conn:
        scheduler.sweep_once(conn, now=fx.clock.now(), retry_gap=float(fx.settings.backoff_max_seconds), rules=rules)


def _rules(task_type: str) -> list:
    found = [rule for rule in scheduler.REPORT_RULES if rule.task_type == task_type]
    assert len(found) == 1, f"a regra {task_type} não está em REPORT_RULES"
    return found


def _drain(fx: _Fx, task_type: str, handler: Any, *, complete: bool = True, limit: int = 20) -> list[int]:
    """Processa as tarefas `task_type` disponíveis com `handler` (posse pelo `TaskQueue.claim` só desse tipo);
    com `complete=False`, processa uma e não a conclui (a queda depois do COMMIT). Devolve os ids processados."""
    queue = TaskQueue(fx.db_url, clock=fx.clock, settings=fx.settings)
    done: list[int] = []
    try:
        while True:
            task = queue.claim(WORKER, task_types=[task_type])
            if task is None:
                return done
            handler(task)
            done.append(task.id)
            if not complete:
                return done
            assert queue.complete(task.id, WORKER), f"{task_type} não concluída (posse perdida)"
            assert len(done) <= limit, f"mais de {limit} tarefas {task_type}"
    finally:
        queue.close()


def _freezer(fx: _Fx, channel: Any) -> Any:
    """O tratador real do congelamento (montado antes de qualquer conferência dos relatórios)."""
    handler = reports.freeze_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings, channel=channel)
    fx.closers.append(handler.close)
    return handler


def _freeze(fx: _Fx, handler: Any) -> list[int]:
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    return _drain(fx, reports.FREEZE_TASK_TYPE, handler)


def _sender(fx: _Fx, channel: Any) -> Any:
    """O tratador real `notice.send` (de `teamnotices.task_handlers`)."""
    handlers = teamnotices.task_handlers(fx.db_url, clock=fx.clock, settings=fx.settings, channel=channel)
    for handler in handlers.values():
        fx.closers.append(handler.close)
    return handlers[teamnotices.SEND_TASK_TYPE]


def _tasks(fx: _Fx, task_type: str) -> list[tuple]:
    return fx.sql.execute("SELECT payload, idempotency_key, state FROM tasks WHERE task_type = %s ORDER BY id",
                          (task_type,)).fetchall()


def _versions(fx: _Fx, row: int) -> list[tuple]:
    return fx.sql.execute("SELECT id, version, trigger, content, frozen_at FROM dispatch_reports "
                          "WHERE dispatch_id = %s ORDER BY version", (row,)).fetchall()


def _notices(fx: _Fx, row: int, *kinds: str) -> list[dict]:
    with fx.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT id, kind, subject_key, state, recipient_count, report_id, local_date, finished_at, created_at "
            "FROM team_notices WHERE dispatch_id = %s AND kind = ANY(%s) ORDER BY id", (row, list(kinds))).fetchall()


def _events(fx: _Fx, event_type: str, dispatch_id: str, **match: Any) -> list[dict]:
    items = list_events(fx.client, ADMIN_TOKEN, event_type, limit=500)["items"]
    return [{k: v for k, v in e.items() if k != "seq"} for e in items
            if e["data"].get("dispatch_id") == dispatch_id and all(e["data"].get(k) == v for k, v in match.items())]


def _frozen_event(offering: dict, dispatch_id: str, version: int, trigger: str, at: datetime,
                  actor: str | None = None) -> dict:
    return {"type": "report.frozen", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": offering["offering_id"], "dispatch_id": dispatch_id, "version": version,
                     "trigger": trigger}}


def _recorded_event(offering: dict, dispatch_id: str, *, notice_type: str, version: int | None, state: str,
                    count: int, at: datetime) -> dict:
    return {"type": "notice.recorded", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": offering["offering_id"], "notice_type": notice_type, "dispatch_id": dispatch_id,
                     "report_version": version, "episode_seq": None, "batch_id": None, "window_started_at": None,
                     "local_date": None, "state": state, "recipient_count": count}}


def _summary(channel: Any, offering: dict, content: dict, *, resend: bool = False) -> str:
    """O resumo esperado: o `ChannelSummaryRenderer` sobre o `content` gravado."""
    from cq_server.reportrender import ChannelSummaryRenderer

    rendered = ChannelSummaryRenderer(channel.formatter, timezone=offering["timezone"],
                                      offering_code=offering["code"], resend=resend)
    return rendered.render(reports.report_from_dict(content)).body


def _assert_summaries(fx: _Fx, team: Any, notice: dict, *, kind: str, text: str, channel: Any) -> None:
    """Uma mensagem por membro com endereço ativo, na ordem dos membros (matrícula), com o texto exato no
    dialeto do canal, prioridade 2, sem papel nem mídia (nenhum anexo) e a chave `notice:<id>:p<pessoa>`."""
    messages = team_messages(fx.sql, kind)
    assert [m.recipient for m in messages] == team.recipients
    assert all(m.notice_id == notice["id"] for m in messages)
    assert [(m.text, m.text_format, m.priority, m.role, m.media_id, m.field_index, m.state) for m in messages] == [
        (text, channel.formatter.text_format, 2, None, None, None, "pending")] * len(team.recipients)
    keys = [r[0] for r in fx.sql.execute("SELECT idempotency_key FROM outbox_messages WHERE id = ANY(%s) ORDER BY id",
                                         ([m.message_id for m in messages],)).fetchall()]
    assert keys == [f"notice:{notice['id']}:p{_person_row(fx, pseudo)}" for pseudo in team.recipients]


def _pair(on_time: int, with_late: int | None = None) -> dict:
    return {"on_time": on_time, "with_late": on_time if with_late is None else with_late}


def _rate(on_time: float | None, with_late: float | None = None) -> dict:
    return {"on_time": on_time, "with_late": on_time if with_late is None else with_late}


def _top(items: list[tuple[str, int, int]], others: tuple[int, int]) -> dict:
    return {"items": [{"value": v, "count": _pair(a, b)} for v, a, b in items], "others": _pair(*others)}


def _times(values: list[int]) -> dict:
    if len(values) == 1:
        return {"observations": 1, "negative": int(values[0] < 0), "median_ms": values[0], "q1_ms": values[0],
                "q3_ms": values[0]}
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    assert median == statistics.median(values)
    return {"observations": len(values), "negative": sum(v < 0 for v in values), "median_ms": median,
            "q1_ms": q1, "q3_ms": q3}


def _report(offering: dict, opened: Any, *, computed_at: datetime, frozen: dict | None, grading_pending: int,
            target: int, performance: dict, times: list[int], values: dict, ratios: dict) -> dict:
    """O `Report` exato do disparo `number` fechado, com os números passados como literais."""
    return {
        "report_schema_version": "1",
        "offering": _oref(offering),
        "timezone": offering["timezone"],
        "dispatch": {"dispatch_id": opened.dispatch_id, "question": {"qid": QID, "commit_short": COMMIT_SHORT},
                     "title": TITLE, "state": "closed", "partial": False, "origin": "api",
                     "opens_at": opened.opens_at, "closes_at": opened.closes_at},
        "computed_at": iso(computed_at),
        "frozen": frozen,
        "grading_pending": grading_pending,
        "reach": {"target": target, "delivered": target, "not_delivered": 0,
                  "not_delivered_by_reason": {"endpoint_blocked": 0, "channel_refused": 0, "engine_unavailable": 0,
                                              "bot_no_access": 0, "channel_unavailable": 0},
                  "question_failed": 0, "question_failed_by_stage": {"preparation": 0, "grading": 0}, "pending": 0},
        "performance": performance,
        "response_time": _times(times),
        "distribution": {"fields": [{"index": 0, "type": "number", "label": LABEL, "unavailable": None,
                                     "choices": None, "values": values, "ratios": ratios, "unrated": _pair(0),
                                     "texts": None}]},
        "friction": {"fields": [{"index": 0, "format_errors": _pair(0), "decimal_comma": _pair(0)}],
                     "unmapped_format_errors": _pair(0)},
    }


def _frozen(version: int, trigger: str, at: datetime) -> dict:
    return {"version": version, "trigger": trigger, "frozen_at": iso(at)}


def _close_offering_and_archive(fx: _Fx, ref: str) -> None:
    closed = fx.client.post(f"/v1/offerings/{ref}/close", headers=ADMIN)
    assert closed.status_code == 200 and closed.json()["state"] == "closed", closed.text[:300]
    prepared = fx.client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text[:300]
    archived = fx.client.post(f"/v1/offerings/{ref}/archive",
                              json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert archived.status_code == 200 and archived.json()["state"] == "archived", archived.text[:300]


# --- disparos sem alvo --------------------------------------------------------------------------------


@dataclasses.dataclass
class _Plain:
    dispatch_id: str
    row: int
    opens_at: str
    closes_at: str


def _plain_open(fx: _Fx, offering: dict, *, window: timedelta = WINDOW) -> _Plain:
    """Um disparo sem alvo aberto pelos tratadores reais (validação, confirmação pela API, abertura)."""
    from cq_server.dispatches import OPEN_TASK_TYPE, VALIDATE_TASK_TYPE, open_task_handler, validate_task_handler

    code = offering["code"]
    body = prepare_dispatch(fx.client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid=QID,
                            closes_at=iso(fx.clock.now() + window))
    dispatch_id = body["dispatch_id"]
    validator = validate_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings, engine=_engine(fx),
                                      fetcher=fx.repo_fetcher, channel=ConsoleChannel(io.StringIO()))
    fx.closers.append(validator.close)
    _drain(fx, VALIDATE_TASK_TYPE, validator)
    assert get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["state"] == "scheduled"
    confirm_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)
    opener = open_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings)
    fx.closers.append(opener.close)
    _drain(fx, OPEN_TASK_TYPE, opener)
    shown = get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)
    assert (shown["state"], shown["target_count"]) == ("open", 0)
    return _Plain(dispatch_id=dispatch_id, row=_row(fx, dispatch_id), opens_at=shown["opens_at"],
                  closes_at=shown["closes_at"])


def _close_due(fx: _Fx, channel: Any = None) -> None:
    from cq_server.dispatches import CLOSE_TASK_TYPE, close_task_handler

    _sweep(fx, scheduler.DISPATCH_RULES)
    closer = close_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings,
                                channel=channel if channel is not None else ConsoleChannel(io.StringIO()))
    fx.closers.append(closer.close)
    _drain(fx, CLOSE_TASK_TYPE, closer)


def _plain_close(fx: _Fx, offering: dict, plain: _Plain) -> datetime:
    fx.clock.set(max(fx.clock.now(), _at(plain.closes_at) + timedelta(seconds=1)))
    _close_due(fx)
    assert get_dispatch(fx.client, ADMIN_TOKEN, offering["code"], plain.dispatch_id)["state"] == "closed"
    return fx.clock.now()


# --- cenário com alunos ---------------------------------------------------------------------------------


class _HeldFreeze:
    """O tratador `report.freeze` do worker do teste quando ele roda depois do fechamento: não faz nada (o
    congelamento do teste é o do tratador real, chamado pelo teste)."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, task: Any) -> None:
        self.calls.append(task.id)

    def close(self) -> None:
        return None


@dataclasses.dataclass
class _Student:
    label: str
    person_row: int
    pseudo_id: str
    console: ConsoleStudent


@dataclasses.dataclass
class _Opened:
    dispatch_id: str
    row: int
    opens_at: str
    closes_at: str
    sent_at: datetime
    questions: dict[str, ConsoleLine]


class _World:
    """Alunos vinculados a `offering` e o worker do console com rota (ver o docstring do módulo)."""

    def __init__(self, fx: _Fx, offering: dict, *, students: int) -> None:
        self.fx = fx
        self.offering = offering
        self.code = offering["code"]
        self.bot_row = _bot_row(fx, offering)
        self.stream = io.StringIO()
        n = next(_counter)
        rows = [{"name": f"Pessoa Congelamento {n:03d} {chr(65 + k)}", "ra": f"57{n:04d}{k:04d}",
                 "email": f"pessoa.congelamento.{n}.{k}@turma.example.org"} for k in range(students)]
        import_students(fx.client, ADMIN_TOKEN, self.code, rows)
        self.students = [self._link(chr(65 + k), row["ra"]) for k, row in enumerate(rows)]

    def _link(self, label: str, ra: str) -> _Student:
        fx = self.fx
        with fx.sql.cursor(row_factory=dict_row) as cur:
            found = cur.execute(
                "SELECT p.id AS person_row, p.pseudo_id::text AS pseudo, e.id AS enrollment_row "
                "FROM identities i JOIN people p ON p.id = i.person_id JOIN enrollments e ON e.person_id = p.id "
                "JOIN offerings o ON o.id = e.offering_id WHERE i.ra = %s AND o.offering_id = %s",
                (ra, uuid.UUID(self.offering["offering_id"]))).fetchone()
        now = fx.clock.now()
        fx.sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                       "terms_accepted_at = %s WHERE id = %s", (now, now, found["enrollment_row"]))
        chat_id = make_user(next(_users)).user_id
        fx.sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                       "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                       (found["person_row"], self.bot_row, chat_id, now, now))
        inbox = ConsoleInbox(fx.db_url, clock=fx.clock)
        fx.closers.append(inbox.close)
        console = ConsoleStudent(inbox, bot_id=self.bot_row, chat_id=chat_id, recipient=found["pseudo"],
                                 source=self.stream)
        return _Student(label=label, person_row=int(found["person_row"]), pseudo_id=found["pseudo"], console=console)

    def student(self, label: str) -> _Student:
        return next(s for s in self.students if s.label == label)

    def start(self, *, hold_freeze: bool = False) -> None:
        extra = {"handlers": {reports.FREEZE_TASK_TYPE: _HeldFreeze()}} if hold_freeze else {}
        self.fx.worker_thread(channel=ConsoleChannel(self.stream), console_routes=True, engine=_engine(self.fx),
                              fetcher=self.fx.repo_fetcher, delivery=DeliverySettings(), **extra)

    def stop(self) -> None:
        self.fx.worker_thread.stop()

    @staticmethod
    def _questions(student: _Student) -> list[ConsoleLine]:
        return [line for line in student.console.lines() if line.answerable and not line.has_rich_fields]

    @staticmethod
    def _prompts(student: _Student, question: ConsoleLine) -> list[ConsoleLine]:
        return [line for line in student.console.lines()
                if line.reply_request is not None and str(line.reply_to) == question.channel_ref]

    def open(self) -> _Opened:
        fx = self.fx
        prepared = prepare_dispatch(fx.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=QID,
                                    closes_at=iso(fx.clock.now() + WINDOW))
        dispatch_id = prepared["dispatch_id"]
        _wait(lambda: get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)["state"] != "validating",
              "a validação do disparo")
        assert get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)["state"] == "scheduled"
        confirm_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)
        row = _row(fx, dispatch_id)

        def settled() -> bool:
            states = [r[0] for r in fx.sql.execute("SELECT state FROM deliveries WHERE dispatch_id = %s",
                                                   (row,)).fetchall()]
            queued = fx.sql.execute(
                "SELECT count(*) FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
                "WHERE d.dispatch_id = %s AND o.state IN ('pending', 'leased')", (row,)).fetchone()[0]
            return (states == ["delivered"] * len(self.students) and not queued
                    and all(len(self._questions(s)) == 1 for s in self.students))

        _wait(settled, "as entregas da abertura")
        questions = {s.label: self._questions(s)[0] for s in self.students}
        _wait(lambda: all(self._prompts(s, questions[s.label]) for s in self.students), "os pedidos do campo")
        shown = get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)
        sent = {r[0] for r in fx.sql.execute("SELECT sent_at FROM deliveries WHERE dispatch_id = %s",
                                             (row,)).fetchall()}
        assert len(sent) == 1
        return _Opened(dispatch_id=dispatch_id, row=row, opens_at=shown["opens_at"], closes_at=shown["closes_at"],
                       sent_at=sent.pop(), questions=questions)

    def submissions(self, opened: _Opened, label: str) -> list[dict]:
        with self.fx.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT s.outcome, s.valid, s.late, s.score, s.response_ms FROM submissions s "
                "JOIN deliveries d ON d.id = s.delivery_id JOIN enrollments e ON e.id = d.enrollment_id "
                "WHERE d.dispatch_id = %s AND e.person_id = %s ORDER BY s.id",
                (opened.row, self.student(label).person_row)).fetchall()

    def answer(self, opened: _Opened, label: str, text: str, *, at: int | None = None, late: bool = False,
               outcome: str = "graded") -> dict:
        """No prazo: em resposta ao último pedido do campo, com o relógio em `sent_at + at` segundos; atrasada
        (`late=True`): em resposta à mensagem da questão. Espera a submissão no `outcome` pedido."""
        student = self.student(label)
        question = opened.questions[label]
        if at is not None:
            self.fx.clock.set(opened.sent_at + timedelta(seconds=at))
        target = question if late else self._prompts(student, question)[-1]
        before = len(self.submissions(opened, label))
        student.console.reply(target, text)

        def ready() -> dict | None:
            subs = self.submissions(opened, label)
            return subs[-1] if len(subs) == before + 1 and subs[-1]["outcome"] == outcome else None

        sub = _wait(ready, f"a submissão de {label} em {outcome}")
        if outcome == "graded":
            assert (sub["valid"], sub["late"]) == (True, late), f"{label}: {sub}"
        return sub

    def close(self, opened: _Opened) -> datetime:
        """Para o worker, leva o relógio além de `closes_at` e fecha pela varredura e pelo tratador reais."""
        self.stop()
        self.fx.clock.set(_at(opened.closes_at) + timedelta(seconds=1))
        _close_due(self.fx)
        assert get_dispatch(self.fx.client, ADMIN_TOKEN, self.code, opened.dispatch_id)["state"] == "closed"
        return self.fx.clock.now()


def _grade_pending(fx: _Fx, world: _World, opened: _Opened, label: str) -> dict:
    """Com o motor de volta, as rodadas da correção pendente (a varredura real do disparo e o tratador real
    `submission.grade`), com o relógio avançando o `backoff_max_seconds` entre elas, até a submissão sair de
    `grading`."""
    from cq_server.grading import GRADE_TASK_TYPE, grade_task_handler

    for _ in range(6):
        _sweep(fx, scheduler.DISPATCH_RULES)
        grader = grade_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings, engine=_engine(fx),
                                    channel=ConsoleChannel(io.StringIO()))
        fx.closers.append(grader.close)
        _drain(fx, GRADE_TASK_TYPE, grader)
        last = world.submissions(opened, label)[-1]
        if last["outcome"] != "grading":
            return last
        fx.clock.advance(fx.settings.backoff_max_seconds + 1)
    raise AssertionError("a correção pendente não terminou")


# --- concorrência ---------------------------------------------------------------------------------------


class _Hold:
    """Ponto de espera numa função do módulo: na primeira chamada da thread escolhida, avisa (`entered`) e
    espera `release`; as demais chamadas passam direto para a função original."""

    def __init__(self, original: Callable[..., Any]) -> None:
        self.original = original
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        self._held = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not self._held and threading.current_thread() is self.thread:
            self._held = True
            self.entered.set()
            assert self.release.wait(WAIT), "o ponto de espera não foi solto"
        return self.original(*args, **kwargs)


class _Runner(threading.Thread):
    """Roda `target` numa thread e guarda o resultado ou a exceção."""

    def __init__(self, target: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._target_fn = target
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._target_fn()
        except BaseException as exc:  # noqa: BLE001 - conferida pelo teste
            self.error = exc


def _lock_waiters(fx: _Fx) -> int:
    """Quantas sessões deste banco esperam uma trava (`pg_stat_activity.wait_event_type = 'Lock'`)."""
    return int(fx.sql.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                              "AND wait_event_type = 'Lock'").fetchone()[0])


# ---------------------------------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("team_members", [(ACTIVE, BLOCKED, ACTIVE, NO_ENDPOINT)], indirect=True)
def test_closing_freezes_version_one_once_and_queues_one_summary_per_active_member(fx: _Fx, team_members: Any) -> None:
    """Os membros sem endereço ativo ficam fora do resumo.
    Equipe de 4 (ativo, bloqueado, ativo, sem endereço); 4 alunos: A
    responde `6.5` em +10 s (correta), B `13` em +20 s (incorreta), C e D não respondem. Fechado pelo horário,
    a varredura cria uma única tarefa `report.freeze` (`{dispatch_row_id}`, chave `report.freeze:<id>:r1`) e o
    tratador congela a versão 1 `closing` com o relatório do momento (números à mão: alvo e entregues 4;
    respondidas (2, 2); corretas (1, 1); incorretas (1, 1); taxas de corretas, parcialmente corretas e
    incorretas 0,25 / 0 / 0,25, resposta 0,5, nota média 0,5; tempos 10 000 e 20 000 ms; `values` e `ratios`
    sem item com 2 ocorrências, `others` (2, 2)),
    grava o `report.frozen` (sem ator), registra o `dispatch_closed` com `report_id` da versão e o entrega
    (`queued`, 2 destinatários): uma mensagem a cada membro ativo, na ordem dos membros, com o texto do
    `ChannelSummaryRenderer` sobre o `content` gravado, `text_format` do canal, prioridade 2, sem anexo, chave
    `notice:<id>:p<pessoa>`; e o `notice.recorded` exato. Reexecutar (outra tarefa do mesmo disparo) e varrer
    de novo não muda nada; o gatilho recusa `UPDATE` e `DELETE` da versão."""
    team = team_members
    channel = _channel(fx)
    world = _World(fx, team.offering, students=4)
    world.start()
    opened = world.open()
    world.answer(opened, "A", "6.5", at=10)
    world.answer(opened, "B", "13", at=20)
    closed_at = world.close(opened)
    freezer = _freezer(fx, channel)

    assert len(_freeze(fx, freezer)) == 1
    assert _tasks(fx, reports.FREEZE_TASK_TYPE) == [
        ({"dispatch_row_id": opened.row}, f"report.freeze:{opened.row}:r1", "done")]
    [v1] = _versions(fx, opened.row)
    performance = {"answered": _pair(2), "correct": _pair(1), "partially_correct": _pair(0), "incorrect": _pair(1),
                   "dont_know": _pair(0), "correct_rate": _rate(0.25), "partially_correct_rate": _rate(0.0),
                   "incorrect_rate": _rate(0.25), "response_rate": _rate(0.5), "mean_score": _rate(0.5)}
    content = _report(team.offering, opened, computed_at=closed_at, frozen=None, grading_pending=0, target=4,
                      performance=performance, times=[10_000, 20_000], values=_top([], (2, 2)),
                      ratios=_top([], (2, 2)))
    assert (v1[1], v1[2], v1[3], v1[4]) == (1, "closing", content, closed_at)
    assert get_report(fx.client, team.code, opened.dispatch_id, version=1) == {
        **content, "frozen": _frozen(1, "closing", closed_at)}
    assert get_report(fx.client, team.code, opened.dispatch_id) == content
    assert _events(fx, "report.frozen", opened.dispatch_id) == [
        _frozen_event(team.offering, opened.dispatch_id, 1, "closing", closed_at)]

    [notice] = _notices(fx, opened.row, "dispatch_closed")
    assert {k: notice[k] for k in notice if k != "id"} == {
        "kind": "dispatch_closed", "subject_key": f"report:{v1[0]}", "state": "queued", "recipient_count": 2,
        "report_id": v1[0], "local_date": None, "finished_at": closed_at, "created_at": closed_at}
    _assert_summaries(fx, team, notice, kind="dispatch_closed", text=_summary(channel, team.offering, content),
                      channel=channel)
    assert _events(fx, "notice.recorded", opened.dispatch_id, notice_type="dispatch_closed") == [
        _recorded_event(team.offering, opened.dispatch_id, notice_type="dispatch_closed", version=1, state="queued",
                        count=2, at=closed_at)]

    before = (_versions(fx, opened.row), _notices(fx, opened.row, "dispatch_closed"), team_messages(fx.sql),
              fx.sql.execute("SELECT max(seq) FROM events").fetchone()[0])
    fx.clock.advance(5)
    with psycopg.connect(fx.db_url, autocommit=True) as conn, conn.transaction():
        enqueue_in_transaction(conn, reports.FREEZE_TASK_TYPE, {"dispatch_row_id": opened.row}, now=fx.clock.now(),
                               idempotency_key=f"report.freeze:{opened.row}:r9")
    assert len(_drain(fx, reports.FREEZE_TASK_TYPE, freezer)) == 1
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    assert len(_tasks(fx, reports.FREEZE_TASK_TYPE)) == 2, "a varredura não cria rodada com a versão closing gravada"
    assert (_versions(fx, opened.row), _notices(fx, opened.row, "dispatch_closed"), team_messages(fx.sql),
            fx.sql.execute("SELECT max(seq) FROM events").fetchone()[0]) == before

    for statement in ("UPDATE dispatch_reports SET trigger = 'resend' WHERE id = %s",
                      "DELETE FROM dispatch_reports WHERE id = %s"):
        with pytest.raises(psycopg.Error):
            fx.sql.execute(statement, (v1[0],))
    assert _versions(fx, opened.row) == before[0]


def test_on_time_grading_pending_holds_the_freeze_until_it_is_graded(fx: _Fx) -> None:
    """O congelamento só depois da correção no prazo pendente.
    Sem equipe; 2 alunos: B responde `13` em +10 s (motor no ar, incorreta); com o motor parado,
    A responde `6.5` em +20 s e a submissão fica `grading`. Fechado, a varredura não cria `report.freeze`
    enquanto a correção está pendente (antes dos 10 min); com o motor de volta, a correção termina (A no prazo,
    `late = false`, `response_ms` 20 000) e a varredura seguinte, ainda dentro dos 10 min, congela a versão 1
    com A: respondidas (2, 2), corretas (1, 1), incorretas (1, 1), taxas 0,5 / 0 / 0,5, resposta 1,0, nota média
    0,5, tempos 10 000 e 20 000 ms, `grading_pending = 0`; o aviso `dispatch_closed` fica `no_recipient`."""
    offering = _offering(fx, "congela-pendente")
    world = _World(fx, offering, students=2)
    world.start()
    opened = world.open()
    world.answer(opened, "B", "13", at=10)
    fx.engine_fake.stop()
    world.answer(opened, "A", "6.5", at=20, outcome="grading")
    closed_at = world.close(opened)
    freezer = _freezer(fx, _channel(fx))

    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    assert _tasks(fx, reports.FREEZE_TASK_TYPE) == [], "com correção no prazo pendente, o congelamento espera"
    fx.engine_fake.start()
    graded = _grade_pending(fx, world, opened, "A")
    assert (graded["outcome"], graded["late"], graded["score"], graded["response_ms"]) == ("graded", False, 1.0, 20_000)
    assert fx.clock.now() < closed_at + FREEZE_WAIT
    frozen_at = fx.clock.now()
    assert len(_freeze(fx, freezer)) == 1
    performance = {"answered": _pair(2), "correct": _pair(1), "partially_correct": _pair(0), "incorrect": _pair(1),
                   "dont_know": _pair(0), "correct_rate": _rate(0.5), "partially_correct_rate": _rate(0.0),
                   "incorrect_rate": _rate(0.5), "response_rate": _rate(1.0), "mean_score": _rate(0.5)}
    content = _report(offering, opened, computed_at=frozen_at, frozen=None, grading_pending=0, target=2,
                      performance=performance, times=[10_000, 20_000], values=_top([], (2, 2)),
                      ratios=_top([], (2, 2)))
    [v1] = _versions(fx, opened.row)
    assert (v1[1], v1[2], v1[3], v1[4]) == (1, "closing", content, frozen_at)
    [notice] = _notices(fx, opened.row, "dispatch_closed")
    assert (notice["state"], notice["recipient_count"], notice["report_id"]) == ("no_recipient", 0, v1[0])


def test_without_grading_in_ten_minutes_version_one_keeps_it_pending_and_resend_includes_it(fx: _Fx) -> None:
    """Sem a correção em 10 min, a versão 1 com `grading_pending` (a correção que só
    termina depois do congelamento). O mesmo cenário do teste anterior, com o motor
    parado até depois do congelamento: em `closed_at + 599 s` a varredura não cria `report.freeze`; em `+ 600 s`,
    cria, e a versão 1 sai com `grading_pending = 1` e sem A (respondidas (1, 1), incorretas (1, 1), corretas
    (0, 0), resposta 0,5, nota média 0, tempo só de B, 10 000 ms; `values` e `ratios` com `others` (1, 1)).
    Com o motor de volta, a correção de A termina no prazo: o `content` da versão 1 não muda; o `reports.get`
    no momento inclui A (respondidas (2, 2), corretas (1, 1), resposta 1,0, nota média 0,5, tempos 10 000 e
    20 000 ms, `grading_pending = 0`); o `reports.resend_summary` congela a versão 2 com esse conteúdo."""
    offering = _offering(fx, "congela-espera")
    world = _World(fx, offering, students=2)
    world.start()
    opened = world.open()
    world.answer(opened, "B", "13", at=10)
    fx.engine_fake.stop()
    world.answer(opened, "A", "6.5", at=20, outcome="grading")
    closed_at = world.close(opened)
    freezer = _freezer(fx, _channel(fx))

    fx.clock.set(closed_at + FREEZE_WAIT - timedelta(seconds=1))
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    assert _tasks(fx, reports.FREEZE_TASK_TYPE) == []
    fx.clock.set(closed_at + FREEZE_WAIT)
    frozen_at = fx.clock.now()
    assert len(_freeze(fx, freezer)) == 1
    pending_perf = {"answered": _pair(1), "correct": _pair(0), "partially_correct": _pair(0), "incorrect": _pair(1),
                    "dont_know": _pair(0), "correct_rate": _rate(0.0), "partially_correct_rate": _rate(0.0),
                    "incorrect_rate": _rate(0.5), "response_rate": _rate(0.5), "mean_score": _rate(0.0)}
    content_v1 = _report(offering, opened, computed_at=frozen_at, frozen=None, grading_pending=1, target=2,
                         performance=pending_perf, times=[10_000], values=_top([], (1, 1)), ratios=_top([], (1, 1)))
    [v1] = _versions(fx, opened.row)
    assert (v1[1], v1[2], v1[3], v1[4]) == (1, "closing", content_v1, frozen_at)

    fx.engine_fake.start()
    graded = _grade_pending(fx, world, opened, "A")
    assert (graded["outcome"], graded["late"], graded["response_ms"]) == ("graded", False, 20_000)
    assert _versions(fx, opened.row) == [v1], "a correção tardia não muda a versão 1"
    now = fx.clock.now()
    full_perf = {"answered": _pair(2), "correct": _pair(1), "partially_correct": _pair(0), "incorrect": _pair(1),
                 "dont_know": _pair(0), "correct_rate": _rate(0.5), "partially_correct_rate": _rate(0.0),
                 "incorrect_rate": _rate(0.5), "response_rate": _rate(1.0), "mean_score": _rate(0.5)}
    content_now = _report(offering, opened, computed_at=now, frozen=None, grading_pending=0, target=2,
                          performance=full_perf, times=[10_000, 20_000], values=_top([], (2, 2)),
                          ratios=_top([], (2, 2)))
    assert get_report(fx.client, offering["code"], opened.dispatch_id) == content_now
    assert get_report(fx.client, offering["code"], opened.dispatch_id, version=1) == {
        **content_v1, "frozen": _frozen(1, "closing", frozen_at)}
    assert resend_report_summary(fx.client, offering["code"], opened.dispatch_id)["frozen"] == _frozen(2, "resend", now)
    assert [(v[1], v[2], v[3]) for v in _versions(fx, opened.row)] == [
        (1, "closing", content_v1), (2, "resend", content_now)]


def test_late_answers_before_the_freeze_enter_version_one_and_the_resend_reaches_each_member(
        fx: _Fx, team_members: Any) -> None:
    """As atrasadas recebidas antes do congelamento entram na versão 1.
    Equipe de 2 membros ativos; 4 alunos: A responde `6.5` no prazo em +10 s; fechado, B responde `6.5`
    atrasada (antes do congelamento); D não responde. Versão 1: respondidas (1, 2), corretas (1, 2), taxa de
    corretas e de resposta (0,25; 0,5), nota média (1,0; 1,0), tempo só de A (10 000 ms), `values` `6.5` (1, 2) e
    `ratios` `1` (1, 2); o resumo de fechamento a cada membro. Depois, C responde `6.5` atrasada; o
    `reports.resend_summary` congela a versão 2 (respondidas (1, 3), corretas (1, 3), taxas (0,25; 0,75),
    `values` `6.5` (1, 3), `ratios` `1` (1, 3)) e registra o `summary_resent`; o `notice.send` (tarefa da
    varredura) o entrega: `queued` com 2 destinatários, `report_id` da versão 2, uma mensagem por membro com o
    texto do `ChannelSummaryRenderer(…, resend=True)` sobre o `content` da versão 2 (diferente do resumo de
    fechamento), chave `notice:<id>:p<pessoa>`, o `notice.recorded` com `report_version = 2`; reexecutar o
    `notice.send` do mesmo aviso não duplica."""
    team = team_members
    channel = _channel(fx)
    world = _World(fx, team.offering, students=4)
    world.start()
    opened = world.open()
    world.answer(opened, "A", "6.5", at=10)
    world.close(opened)
    world.start(hold_freeze=True)
    fx.clock.advance(30)
    late_b = world.answer(opened, "B", "6.5", late=True)
    assert late_b["late"] is True
    world.stop()
    freezer = _freezer(fx, channel)
    fx.clock.advance(1)
    frozen_at = fx.clock.now()
    assert len(_freeze(fx, freezer)) == 1

    perf_v1 = {"answered": _pair(1, 2), "correct": _pair(1, 2), "partially_correct": _pair(0),
               "incorrect": _pair(0), "dont_know": _pair(0), "correct_rate": _rate(0.25, 0.5),
               "partially_correct_rate": _rate(0.0), "incorrect_rate": _rate(0.0), "response_rate": _rate(0.25, 0.5),
               "mean_score": _rate(1.0)}
    content_v1 = _report(team.offering, opened, computed_at=frozen_at, frozen=None, grading_pending=0, target=4,
                         performance=perf_v1, times=[10_000], values=_top([("6.5", 1, 2)], (0, 0)),
                         ratios=_top([("1", 1, 2)], (0, 0)))
    [v1] = _versions(fx, opened.row)
    assert (v1[1], v1[2], v1[3]) == (1, "closing", content_v1)
    [closed_notice] = _notices(fx, opened.row, "dispatch_closed")
    closed_text = _summary(channel, team.offering, content_v1)
    _assert_summaries(fx, team, closed_notice, kind="dispatch_closed", text=closed_text, channel=channel)

    world.start(hold_freeze=True)
    fx.clock.advance(30)
    world.answer(opened, "C", "6.5", late=True)
    world.stop()
    resent_at = fx.clock.now()
    sender = _sender(fx, channel)
    assert resend_report_summary(fx.client, team.code, opened.dispatch_id)["frozen"] == _frozen(2, "resend", resent_at)
    perf_v2 = {**perf_v1, "answered": _pair(1, 3), "correct": _pair(1, 3), "correct_rate": _rate(0.25, 0.75),
               "response_rate": _rate(0.25, 0.75)}
    content_v2 = _report(team.offering, opened, computed_at=resent_at, frozen=None, grading_pending=0, target=4,
                         performance=perf_v2, times=[10_000], values=_top([("6.5", 1, 3)], (0, 0)),
                         ratios=_top([("1", 1, 3)], (0, 0)))
    versions = _versions(fx, opened.row)
    assert [(v[1], v[2], v[3]) for v in versions] == [(1, "closing", content_v1), (2, "resend", content_v2)]
    [resent] = _notices(fx, opened.row, "summary_resent")
    assert (resent["state"], resent["report_id"], resent["subject_key"]) == (
        "pending", versions[1][0], f"report:{versions[1][0]}")

    fx.clock.advance(1)
    sent_at = fx.clock.now()
    _sweep(fx, _rules(teamnotices.SEND_TASK_TYPE))
    _drain(fx, teamnotices.SEND_TASK_TYPE, sender)
    [resent] = _notices(fx, opened.row, "summary_resent")
    assert (resent["state"], resent["recipient_count"], resent["finished_at"]) == ("queued", 2, sent_at)
    resent_text = _summary(channel, team.offering, {**content_v2, "frozen": _frozen(2, "resend", resent_at)},
                           resend=True)
    assert resent_text != closed_text
    _assert_summaries(fx, team, resent, kind="summary_resent", text=resent_text, channel=channel)
    assert _events(fx, "notice.recorded", opened.dispatch_id, notice_type="summary_resent") == [
        _recorded_event(team.offering, opened.dispatch_id, notice_type="summary_resent", version=2, state="queued",
                        count=2, at=sent_at)]
    with psycopg.connect(fx.db_url, autocommit=True) as conn, conn.transaction():
        enqueue_in_transaction(conn, teamnotices.SEND_TASK_TYPE, {"notice_row_id": resent["id"]}, now=fx.clock.now(),
                               idempotency_key=f"{teamnotices.SEND_TASK_TYPE}:{resent['id']}:r9")
    _drain(fx, teamnotices.SEND_TASK_TYPE, sender)
    assert len(team_messages(fx.sql, "summary_resent")) == 2, "a reexecução não duplica o resumo reenviado"


class _CrashOnFormatter:
    """O canal com rota cuja primeira leitura do formatador derruba o worker (`Crash`): antes do cálculo do
    relatório, portanto antes de gravar a versão."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def formatter(self) -> Any:
        raise Crash()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_worker_crashes_around_the_freeze_leave_one_version_and_one_summary_per_member(
        fx: _Fx, team_members: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Congelamento (a queda depois do commit não repete nada).
    Equipe de 2 membros ativos; disparo sem alvo fechado pelo horário; a mesma tarefa `report.freeze` (r1):
    1ª tentativa, queda antes de congelar (o formatador do canal) → nenhuma versão, aviso ou mensagem; 2ª,
    queda depois de gravar a versão e antes de enfileirar (`outbox.enqueue_notice_text`) → a transação volta,
    nenhuma versão; 3ª, queda antes de congelar de novo; a posse esgotada falha a tarefa, e a **varredura
    reanima** o congelamento (r2, porque o disparo segue fechado sem versão); a r2 congela e não é concluída
    (queda depois do COMMIT) e, vencida a posse, roda de novo sem efeito. No fim: uma versão `closing`, um
    `report.frozen`, um `dispatch_closed` `queued` com uma mensagem por membro e um `notice.recorded`."""
    team = team_members
    channel = _channel(fx)
    plain = _plain_open(fx, team.offering)
    _plain_close(fx, team.offering, plain)
    lease = timedelta(seconds=fx.settings.lease_seconds + 1)
    _freezer(fx, channel)
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))

    def nothing_written() -> None:
        assert _versions(fx, plain.row) == []
        assert _notices(fx, plain.row, "dispatch_closed") == []
        assert team_messages(fx.sql, "dispatch_closed") == []

    for attempt in (1, 2, 3):
        if attempt > 1:
            fx.clock.set(fx.clock.now() + lease)
        crashing_channel = _CrashOnFormatter(channel) if attempt != 2 else channel
        handler = _freezer(fx, crashing_channel)
        if attempt == 2:
            original = outbox.enqueue_notice_text

            def crash_on_enqueue(*args: Any, **kwargs: Any) -> Any:
                raise Crash()

            monkeypatch.setattr(outbox, "enqueue_notice_text", crash_on_enqueue)
        with pytest.raises(Crash):
            _drain(fx, reports.FREEZE_TASK_TYPE, handler)
        if attempt == 2:
            monkeypatch.setattr(outbox, "enqueue_notice_text", original)
        nothing_written()
    fx.clock.set(fx.clock.now() + lease)
    assert _drain(fx, reports.FREEZE_TASK_TYPE, _freezer(fx, channel)) == [], "posse esgotada: a r1 falha"
    assert [(t[1], t[2]) for t in _tasks(fx, reports.FREEZE_TASK_TYPE)] == [
        (f"report.freeze:{plain.row}:r1", "failed")]
    nothing_written()

    fx.clock.advance(fx.settings.backoff_max_seconds + 1)
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    assert [(t[1], t[2]) for t in _tasks(fx, reports.FREEZE_TASK_TYPE)] == [
        (f"report.freeze:{plain.row}:r1", "failed"), (f"report.freeze:{plain.row}:r2", "pending")]
    frozen_at = fx.clock.now()
    assert len(_drain(fx, reports.FREEZE_TASK_TYPE, _freezer(fx, channel), complete=False)) == 1
    snapshot = (_versions(fx, plain.row), _notices(fx, plain.row, "dispatch_closed"),
                team_messages(fx.sql, "dispatch_closed"))
    fx.clock.set(fx.clock.now() + lease)
    assert len(_drain(fx, reports.FREEZE_TASK_TYPE, _freezer(fx, channel))) == 1
    assert (_versions(fx, plain.row), _notices(fx, plain.row, "dispatch_closed"),
            team_messages(fx.sql, "dispatch_closed")) == snapshot

    [v1] = snapshot[0]
    assert (v1[1], v1[2], v1[4]) == (1, "closing", frozen_at)
    [notice] = snapshot[1]
    assert (notice["state"], notice["recipient_count"], notice["report_id"]) == ("queued", 2, v1[0])
    _assert_summaries(fx, team, notice, kind="dispatch_closed", text=_summary(channel, team.offering, v1[3]),
                      channel=channel)
    assert _events(fx, "report.frozen", plain.dispatch_id) == [
        _frozen_event(team.offering, plain.dispatch_id, 1, "closing", frozen_at)]
    assert _events(fx, "notice.recorded", plain.dispatch_id, notice_type="dispatch_closed") == [
        _recorded_event(team.offering, plain.dispatch_id, notice_type="dispatch_closed", version=1, state="queued",
                        count=2, at=frozen_at)]


def test_archiving_with_an_open_dispatch_freezes_and_sends_like_the_window_and_spares_closed_ones(
        fx: _Fx, team_members: Any) -> None:
    """Congelamento no arquivamento. Equipe de 2 membros ativos; dois disparos sem alvo: X
    fechado pelo horário e congelado (versão 1), Y aberto. O oferecimento é encerrado e arquivado: o arquivamento fecha Y
    com `trigger = archive` (o `dispatch.closed` exato, controle do disparo); a varredura cria o `report.freeze`
    só de Y, que congela a versão 1 `closing` e entrega o resumo a cada membro, como no horário; X continua
    com só a versão 1 e um único resumo."""
    team = team_members
    channel = _channel(fx)
    x = _plain_open(fx, team.offering, window=timedelta(minutes=10))
    y = _plain_open(fx, team.offering, window=timedelta(minutes=50))
    _plain_close(fx, team.offering, x)
    freezer = _freezer(fx, channel)
    assert len(_freeze(fx, freezer)) == 1
    x_before = (_versions(fx, x.row), _notices(fx, x.row, "dispatch_closed"))

    fx.clock.advance(60)
    archived_at = fx.clock.now()
    _close_offering_and_archive(fx, team.code)
    assert get_dispatch(fx.client, ADMIN_TOKEN, team.code, y.dispatch_id)["state"] == "closed"
    closed = [e for e in list_events(fx.client, ADMIN_TOKEN, "dispatch.closed", limit=500)["items"]
              if e["data"]["dispatch_id"] == y.dispatch_id]
    assert [(e["data"]["trigger"], e["occurred_at"]) for e in closed] == [("archive", iso(archived_at))]
    assert len(_freeze(fx, freezer)) == 1
    assert [t[0] for t in _tasks(fx, reports.FREEZE_TASK_TYPE)] == [{"dispatch_row_id": x.row},
                                                                    {"dispatch_row_id": y.row}]
    [v1] = _versions(fx, y.row)
    assert (v1[1], v1[2], v1[4]) == (1, "closing", archived_at)
    [notice] = _notices(fx, y.row, "dispatch_closed")
    assert (notice["state"], notice["recipient_count"], notice["report_id"]) == ("queued", 2, v1[0])
    messages = [m for m in team_messages(fx.sql, "dispatch_closed") if m.notice_id == notice["id"]]
    assert [(m.recipient, m.text) for m in messages] == [
        (pseudo, _summary(channel, team.offering, v1[3])) for pseudo in team.recipients]
    assert (_versions(fx, x.row), _notices(fx, x.row, "dispatch_closed")) == x_before


def test_without_recipients_the_version_is_frozen_and_the_notice_is_no_recipient(fx: _Fx) -> None:
    """oferecimento sem equipe; disparo sem alvo fechado pelo
    horário → a versão 1 `closing` e o `report.frozen`; o aviso `dispatch_closed` termina `no_recipient`
    (`recipient_count = 0`, `finished_at`, `report_id` da versão), sem mensagem na fila, com o
    `notice.recorded` exato (`state = no_recipient`)."""
    offering = _offering(fx, "congela-sem-equipe")
    plain = _plain_open(fx, offering)
    closed_at = _plain_close(fx, offering, plain)
    freezer = _freezer(fx, _channel(fx))
    outbox_before = fx.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0]
    assert len(_freeze(fx, freezer)) == 1
    [v1] = _versions(fx, plain.row)
    assert (v1[1], v1[2], v1[4]) == (1, "closing", closed_at)
    assert _events(fx, "report.frozen", plain.dispatch_id) == [
        _frozen_event(offering, plain.dispatch_id, 1, "closing", closed_at)]
    [notice] = _notices(fx, plain.row, "dispatch_closed")
    assert {k: notice[k] for k in notice if k != "id"} == {
        "kind": "dispatch_closed", "subject_key": f"report:{v1[0]}", "state": "no_recipient", "recipient_count": 0,
        "report_id": v1[0], "local_date": None, "finished_at": closed_at, "created_at": closed_at}
    assert fx.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == outbox_before
    assert _events(fx, "notice.recorded", plain.dispatch_id, notice_type="dispatch_closed") == [
        _recorded_event(offering, plain.dispatch_id, notice_type="dispatch_closed", version=1, state="no_recipient",
                        count=0, at=closed_at)]


def test_closed_at_or_below_the_floor_is_frozen_without_summary_and_opened_before_closed_after_is_sent(
        fx: _Fx, team_members: Any) -> None:
    """Piso por fato nos avisos de disparo. Equipe de 2 membros ativos; dois disparos sem
    alvo abertos; A fecha pelo horário; o piso é gravado como a migração `0005` o gravaria naquele instante (o
    maior `events.seq`, que inclui o `dispatch.closed` de A); depois, B fecha. Congelados: A tem a versão 1 e o
    aviso `not_sent` (sem mensagem, `recipient_count = 0`, `notice.recorded` com `not_sent`); B, aberto antes
    do piso e fechado depois, tem a versão 1 e o resumo a cada membro (`queued`)."""
    team = team_members
    channel = _channel(fx)
    a = _plain_open(fx, team.offering, window=timedelta(minutes=10))
    b = _plain_open(fx, team.offering, window=timedelta(minutes=30))
    _plain_close(fx, team.offering, a)
    assert get_dispatch(fx.client, ADMIN_TOKEN, team.code, b.dispatch_id)["state"] == "open"
    freezer = _freezer(fx, channel)
    fx.sql.execute("UPDATE team_notice_floors SET event_floor = (SELECT max(seq) FROM events) WHERE id = 1")
    _plain_close(fx, team.offering, b)
    frozen_at = fx.clock.now()
    assert len(_freeze(fx, freezer)) == 2

    [notice_a] = _notices(fx, a.row, "dispatch_closed")
    [va] = _versions(fx, a.row)
    assert (va[1], va[2]) == (1, "closing")
    assert {k: notice_a[k] for k in ("state", "recipient_count", "report_id", "finished_at")} == {
        "state": "not_sent", "recipient_count": 0, "report_id": va[0], "finished_at": frozen_at}
    assert [m for m in team_messages(fx.sql, "dispatch_closed") if m.notice_id == notice_a["id"]] == []
    assert _events(fx, "notice.recorded", a.dispatch_id, notice_type="dispatch_closed") == [
        _recorded_event(team.offering, a.dispatch_id, notice_type="dispatch_closed", version=1, state="not_sent",
                        count=0, at=frozen_at)]
    [notice_b] = _notices(fx, b.row, "dispatch_closed")
    [vb] = _versions(fx, b.row)
    assert (notice_b["state"], notice_b["recipient_count"], notice_b["report_id"]) == ("queued", 2, vb[0])
    messages = [m for m in team_messages(fx.sql, "dispatch_closed") if m.notice_id == notice_b["id"]]
    assert [(m.recipient, m.text) for m in messages] == [
        (pseudo, _summary(channel, team.offering, vb[3])) for pseudo in team.recipients]


def test_resend_waits_for_a_freeze_in_progress_and_is_refused_before_it(fx: _Fx,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Reenvio só depois da versão do fechamento; ordem das travas (reenvio: oferecimento → disparo `FOR UPDATE`; congelamento: disparo →
    avisos → identidades → versão → aviso). Disparo sem alvo fechado, sem equipe: o reenvio antes do
    congelamento → 409 `invalid_dispatch_state`, nada gravado. Com duas conexões: o congelamento para no
    registro do aviso `dispatch_closed` (`teamnotices.record`), já com a versão 1 inserida e não confirmada; o
    reenvio, numa segunda conexão (a API), fica esperando uma trava (`pg_stat_activity`); solto o congelamento,
    os dois terminam sem erro: a versão 1 `closing` e a versão 2 `resend`, nessa ordem (o reenvio viu a versão
    do fechamento confirmada)."""
    offering = _offering(fx, "congela-reenvio")
    plain = _plain_open(fx, offering)
    frozen_at = _plain_close(fx, offering, plain)
    freezer = _freezer(fx, _channel(fx))
    url = f"/v1/offerings/{offering['code']}/dispatches/{plain.dispatch_id}/report/resend"
    refused = fx.client.post(url, headers=ADMIN)
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "invalid_dispatch_state")
    assert _versions(fx, plain.row) == []

    hold = _Hold(teamnotices.record)
    monkeypatch.setattr(teamnotices, "record", hold)
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    freeze = _Runner(lambda: _drain(fx, reports.FREEZE_TASK_TYPE, freezer))
    hold.thread = freeze
    freeze.start()
    assert hold.entered.wait(WAIT), "o congelamento não chegou ao registro do aviso"
    waiters = _lock_waiters(fx)
    resend = _Runner(lambda: fx.client.post(url, headers=ADMIN))
    resend.start()
    _wait(lambda: _lock_waiters(fx) > waiters, "o reenvio esperando a trava do congelamento")
    assert resend.is_alive()
    hold.release.set()
    freeze.join(WAIT)
    resend.join(WAIT)
    assert not freeze.is_alive() and not resend.is_alive(), "impasse entre o congelamento e o reenvio"
    assert freeze.error is None, repr(freeze.error)
    assert resend.error is None, repr(resend.error)
    assert resend.result.status_code == 200, resend.result.text[:300]
    assert resend.result.json() == {"offering": _oref(offering), "dispatch_id": plain.dispatch_id,
                                    "frozen": _frozen(2, "resend", frozen_at)}
    assert [(v[1], v[2]) for v in _versions(fx, plain.row)] == [(1, "closing"), (2, "resend")]


def test_freeze_and_the_send_of_the_early_opened_notice_take_locks_in_order_without_deadlock(
        fx: _Fx, team_members: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordem das travas (aviso antes do lock dos eventos, nos dois caminhos) no congelamento;
    disparo que fecha antes do aviso "aberto". Equipe de 2 membros ativos; disparo sem alvo aberto e fechado pelos tratadores (sem a regra do
    "aberto" no meio): o gancho do fechamento registra o `dispatch_opened` `pending`. Com duas conexões: o
    `notice.send` desse aviso para depois de travá-lo (`teamnotices.team_recipients`, antes das identidades);
    o congelamento, na outra, fica esperando a trava do aviso (`pg_stat_activity`); solto o `notice.send`, os
    dois terminam sem erro: o "aberto" `queued` uma vez (o congelamento não o entrega de novo), o resumo de
    fechamento `queued`, e cada membro com exatamente uma mensagem de cada, o "aberto" antes do "fechado"."""
    team = team_members
    channel = _channel(fx)
    plain = _plain_open(fx, team.offering)
    _plain_close(fx, team.offering, plain)
    freezer = _freezer(fx, channel)
    sender = _sender(fx, channel)
    assert [(n["state"], n["subject_key"]) for n in _notices(fx, plain.row, "dispatch_opened")] == [
        ("pending", f"dispatch:{plain.row}")]

    hold = _Hold(teamnotices.team_recipients)
    monkeypatch.setattr(teamnotices, "team_recipients", hold)
    _sweep(fx, _rules(teamnotices.SEND_TASK_TYPE))
    _sweep(fx, _rules(reports.FREEZE_TASK_TYPE))
    send = _Runner(lambda: _drain(fx, teamnotices.SEND_TASK_TYPE, sender))
    hold.thread = send
    send.start()
    assert hold.entered.wait(WAIT), "o notice.send não chegou à leitura dos destinatários"
    waiters = _lock_waiters(fx)
    freeze = _Runner(lambda: _drain(fx, reports.FREEZE_TASK_TYPE, freezer))
    freeze.start()
    _wait(lambda: _lock_waiters(fx) > waiters, "o congelamento esperando a trava do aviso")
    hold.release.set()
    send.join(WAIT)
    freeze.join(WAIT)
    assert not send.is_alive() and not freeze.is_alive(), "impasse entre o congelamento e o notice.send"
    assert send.error is None, repr(send.error)
    assert freeze.error is None, repr(freeze.error)

    [opened_notice] = _notices(fx, plain.row, "dispatch_opened")
    [closed_notice] = _notices(fx, plain.row, "dispatch_closed")
    assert (opened_notice["state"], opened_notice["recipient_count"]) == ("queued", 2)
    assert (closed_notice["state"], closed_notice["recipient_count"]) == ("queued", 2)
    for pseudo in team.recipients:
        mine = [m.kind for m in team_messages(fx.sql, recipient=pseudo)
                if m.notice_id in (opened_notice["id"], closed_notice["id"])]
        assert mine == ["dispatch_opened", "dispatch_closed"]
