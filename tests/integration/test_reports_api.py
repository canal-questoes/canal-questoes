"""Relatório do disparo pela API: `reports.get`, `reports.list_versions` e `reports.resend_summary`.


- **Leitura**:
  `reports.get` sem consulta → 200 com o `Report` exato do cálculo no momento (`assert_matches`); `format=csv`
  → `text/csv; charset=utf-8` com o corpo do `CsvReportRenderer` sobre o mesmo `Report`, relido igual ao
  JSON, no momento e numa versão congelada; `format` inválido ou repetido, `version` fora do padrão ou acima
  de 2147483647 → os 400 do contrato; `version=latest` sem versão e versão inexistente → 404; a ordem das
  verificações; os erros sempre no envelope JSON, também com `format=csv`; os 4 estados recusados com
  `invalid_dispatch_state`; o oferecimento arquivado ainda consultável; `reports.list_versions` com
  lista vazia, paginação por `after`/`limit` (cursor = número da versão) e as consultas inválidas; as
  versões congeladas com `frozen` preenchido e redigidas de novo na leitura; nenhuma identidade nem
  pseudônimo como dado.
- **Reenvio**: `reports.resend_summary` sem
  corpo; o fechado com a versão `closing` → 200 `{offering, dispatch_id, frozen}` com `trigger = "resend"`, a
  versão `max + 1`, o `report.frozen` e o aviso `summary_resent` `pending` (a API não compõe o texto); não
  idempotente; a versão 1 inalterada; o oferecimento encerrado aceito, o arquivado → `invalid_state`; o
  aberto, o fechado ainda sem a versão `closing` e os que não abriram → `invalid_dispatch_state`, sem versão
  gravada; a ordem das verificações.

**Montagem** (dublês e injeção de falhas nos testes; sem `UPDATE` de estado do disparo por SQL):
- o oferecimento e o bot pela API do oferecimento (bot da Bot API falsa); a semana do teste começa no `FakeClock`
  (2031-01-01T00:00:00Z) e o fuso é `America/Recife` (UTC−3, sem horário de verão);
- **cenário respondido** (`_World`): 5 alunos importados pela API da lista e vinculados por SQL (matrícula ativa
  com o termo aceito e o endereço `active` no bot), como em `tests/integration/test_answers_typed.py`; o
  worker do console com rota (`run_worker(console_routes=True)` com o motor e a obtenção falsos) valida,
  abre e entrega a questão `number` (gabarito 6,5); os alunos respondem pela `ConsoleInbox`
  (`ConsoleStudent`) ao pedido do campo, com o relógio avançado antes de cada resposta (o `response_ms` é
  `received_at − sent_at`): A manda `abc` (erro de formato) em +5 s e `6.5` em +10 s; B, `6.5` em +20 s; C,
  `13,0` em +30 s (vírgula decimal); D, `13` em +40 s; E não responde;
- **cenário sem alvo** (`_plain_*`): oferecimento sem aluno; a validação, a abertura e o fechamento pelos
  tratadores reais do núcleo, montados como o `run_worker` monta, com a posse das tarefas pelo
  `TaskQueue.claim` só do tipo pedido; o `dispatch.close` sai da varredura real (`sweep_once` com
  `DISPATCH_RULES`), seu único produtor;
- o **fechamento** do cenário respondido também é feito assim, com o worker parado antes de o relógio passar
  do `closes_at` (o worker com os relatórios congelaria sozinho, e o teste precisa do disparo fechado ainda sem versão);
- o **congelamento** pelo tratador real `reports.freeze_task_handler` (montado antes da varredura) sobre a
  tarefa `report.freeze` que a varredura cria com a regra de `scheduler.REPORT_RULES` (a regra do
  congelamento); o estado "fechado ainda sem a versão `closing`" é o intervalo entre o fechamento e essa varredura.

**Números do cenário respondido, à mão**: alvo 5, entregues 5, nenhuma não
entregue nem pendente; `answered` (4, 4) = A, B, C, D; `correct` (2, 2) = A, B; `incorrect` (2, 2) = C, D (13 ≠
6,5); `partially_correct` e `dont_know` (0, 0); as taxas sobre as 5 entregues: corretas 2/5 = 0,4, incorretas
0,4, parcialmente 0,0, resposta 4/5 = 0,8; a nota média (1 + 1 + 0 + 0)/4 = 0,5; o tempo até responder das 4
válidas no prazo, 10 000, 20 000, 30 000 e 40 000 ms, com a mediana e os quartis de
`statistics.median`/`statistics.quantiles(..., n=4, method="inclusive")`; a distribuição do campo 0
(`number`, rótulo `Velocidade:`): `values` com `6.5` (2, 2) e as demais, `13.0` e `13` (1 cada, abaixo do mínimo
2), em `others` (2, 2); `ratios` com `1` (2, 2) e `2` (2, 2) (13/6,5 = 2, `format(2.0, ".3g")`), na ordem da
cadeia, `others` (0, 0); `unrated` (0, 0); a fricção do campo 0: `format_errors` (1, 1) (o `abc` de A) e
`decimal_comma` (1, 1) (o `13,0` de C), `unmapped_format_errors` (0, 0). Todos no prazo: cada par tem os dois
lados iguais.

**CSV**: o corpo é exatamente o `CsvReportRenderer().render` do `Report` lido em JSON
na mesma consulta e no mesmo estado (`report_from_dict`), e a releitura confere, sem fixar os nomes das
seções e métricas: toda contagem em par do JSON (os `Pair` e os `RatePair` não nulos) tem
uma linha com os dois números, e só elas têm `with_late`; cada item das listas dos mais comuns tem uma linha
com o índice do campo em `field`, o valor em `key` (tirado um `'` inicial) e a contagem; cada motivo e
estágio do alcance tem uma linha com o nome em `key`; os escalares do alcance, de `grading_pending` e do
tempo até responder aparecem em `on_time` com `with_late` vazio.

Ids internos, `seq`, cursores de evento, `report_uuid` e pseudônimos nunca como literal esperado: só as
relações e as formas. Os números das versões e o cursor de `reports.list_versions` são o número da versão,
literais por definição.

**Vermelho no esqueleto**: as rotas dos relatórios não existem (os
atalhos de `tests/helpers/api.py` afirmam 200 e recebem 404, rota ausente; os casos de erro esperam 400, 405
ou 409 e recebem o 404 da rota ausente), e `reports.freeze_task_handler` lança `NotImplementedError`; as
tabelas `dispatch_reports` e `team_notices` não existem. Cada função falha na primeira chamada a uma rota do
relatórios ou no tratador do congelamento. As asserções sobre o disparo (o preparo, a validação, a abertura, as
respostas e o fechamento) ficam na mesma função de um caso que falha. Os casos com versões
congeladas e o reenvio ficam verdes depois, com as suas implementações.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import itertools
import json
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import reports, scheduler
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    cancel_dispatch,
    confirm_dispatch,
    create_offering,
    create_token,
    get_dispatch,
    get_report,
    import_students,
    list_events,
    list_report_versions,
    marker,
    prepare_dispatch,
    register_bot,
    register_person,
    resend_report_summary,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent
from tests.helpers.contract import REPORT_CSV_MEDIA, assert_error, assert_matches, assert_no_echo
from tests.helpers.engine import ANSWER_SENTINEL, preset

ADMIN = auth(ADMIN_TOKEN)

TZ = "America/Recife"
REPO = "https://repos.cq.test/disciplina/questoes-relatorio.git"
COMMIT = "5eb0a1c2" * 5
COMMIT_SHORT = COMMIT[:12]
QID = "number"
UNSUPPORTED_QID = "unsupported"
TITLE = "Velocidade média"
LABEL = "Velocidade:"
WINDOW = timedelta(minutes=10)
OFFERING_WINDOW_MINUTES = 60
WORKER = "teste-relatorios-api"
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
WAIT = 20.0
MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"
MISSING_DISPATCH_ID = "0d0d0d0d-0d0d-4d0d-8d0d-0d0d0d0d0d0d"
REPORT_EVENT_TYPES = ("report.frozen",)
NOTICE_EVENT_TYPES = ("notice.recorded",)
# Nenhum campo do relatório tem nome de gabarito.
ANSWER_KEY_NAMES = ("correct_answer", "answer_key", "is_correct", "correct_value", "explanation")

_counter = itertools.count(1)
_users = itertools.count(700)


# ---------------------------------------------------------------------------------------------------
# Apoio comum
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Fx:
    client: Any
    sql: psycopg.Connection
    db_url: str
    clock: Any
    settings: Any
    bot_api: Any
    engine_fake: Any
    repo_fetcher: Any
    worker_thread: Any
    engines: list = dataclasses.field(default_factory=list)
    inboxes: list = dataclasses.field(default_factory=list)


@pytest.fixture
def fx(client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any, bot_api: Any,
       engine_fake: Any, repo_fetcher: Any, worker_thread: Any) -> Iterator[_Fx]:
    engine_fake.program((REPO, COMMIT, QID), preset("number"))
    engine_fake.program((REPO, COMMIT, UNSUPPORTED_QID), preset("unsupported"))
    made = _Fx(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, bot_api=bot_api,
               engine_fake=engine_fake, repo_fetcher=repo_fetcher, worker_thread=worker_thread)
    yield made
    worker_thread.stop()
    for engine in made.engines:
        engine.close()
    for inbox in made.inboxes:
        inbox.close()


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _oref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _engine(fx: _Fx) -> HttpQuestionEngine:
    engine = HttpQuestionEngine(EngineSettings(engine_url=fx.engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    fx.engines.append(engine)
    return engine


def _offering(fx: _Fx, prefix: str) -> tuple[dict, int]:
    """Um oferecimento `active` (fuso `TZ`) com bot próprio registrado; devolve o corpo e o id interno do bot."""
    offering = create_offering(fx.client, ADMIN_TOKEN, code=f"{prefix}-{next(_counter)}", timezone=TZ,
                               response_window_minutes=OFFERING_WINDOW_MINUTES)
    bot = fx.bot_api.add_bot()
    register_bot(fx.client, ADMIN_TOKEN, offering["code"], fx.bot_api.token_of(bot))
    assert activate_bot(fx.client, ADMIN_TOKEN, offering["code"])["state"] == "active"
    bot_row = fx.sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()[0]
    return offering, int(bot_row)


def _row(fx: _Fx, dispatch_id: str) -> int:
    found = fx.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s", (uuid.UUID(dispatch_id),)).fetchone()
    assert found is not None, "disparo ausente do banco"
    return int(found[0])


def _offering_row(fx: _Fx, offering: dict) -> int:
    return int(fx.sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                              (uuid.UUID(offering["offering_id"]),)).fetchone()[0])


def _drain(fx: _Fx, task_type: str, handler: Any, *, limit: int = 40) -> int:
    """Processa as tarefas `task_type` disponíveis com `handler` (posse pelo `TaskQueue.claim` só desse tipo,
    como o worker) e as conclui; devolve quantas processou. O tratador é fechado no fim."""
    from cq_server.tasks import TaskQueue

    queue = TaskQueue(fx.db_url, clock=fx.clock, settings=fx.settings)
    processed = 0
    try:
        while True:
            task = queue.claim(WORKER, task_types=[task_type])
            if task is None:
                return processed
            handler(task)
            assert queue.complete(task.id, WORKER), f"{task_type} não concluída (posse perdida)"
            processed += 1
            assert processed <= limit, f"mais de {limit} tarefas {task_type}"
    finally:
        queue.close()
        handler.close()


def _sweep(fx: _Fx, rules: Any) -> None:
    with psycopg.connect(fx.db_url, autocommit=True) as conn:
        scheduler.sweep_once(conn, now=fx.clock.now(), retry_gap=float(fx.settings.backoff_max_seconds),
                             rules=rules)


def _validate_all(fx: _Fx) -> int:
    from cq_server.dispatches import VALIDATE_TASK_TYPE, validate_task_handler

    return _drain(fx, VALIDATE_TASK_TYPE, validate_task_handler(
        fx.db_url, clock=fx.clock, settings=fx.settings, engine=_engine(fx), fetcher=fx.repo_fetcher,
        channel=ConsoleChannel(io.StringIO())))


def _open_due(fx: _Fx) -> int:
    from cq_server.dispatches import OPEN_TASK_TYPE, open_task_handler

    return _drain(fx, OPEN_TASK_TYPE, open_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings))


def _close_due(fx: _Fx) -> int:
    """A varredura real do disparo enfileira o `dispatch.close` dos abertos vencidos, e o tratador real fecha (sem
    worker no ar, o bot não está sendo consultado: a barreira (b) está satisfeita)."""
    from cq_server.dispatches import CLOSE_TASK_TYPE, close_task_handler

    _sweep(fx, scheduler.DISPATCH_RULES)
    return _drain(fx, CLOSE_TASK_TYPE, close_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings,
                                                          channel=ConsoleChannel(io.StringIO())))


def _freeze_due(fx: _Fx) -> int:
    """O congelamento pelo tratador real (montado antes) sobre as tarefas `report.freeze` que a varredura cria
    com a regra de `REPORT_RULES` (a regra do congelamento)."""
    handler = reports.freeze_task_handler(fx.db_url, clock=fx.clock, settings=fx.settings,
                                          channel=ConsoleChannel(io.StringIO()))
    rules = [rule for rule in scheduler.REPORT_RULES if rule.task_type == reports.FREEZE_TASK_TYPE]
    assert len(rules) == 1, "a regra report.freeze não está em REPORT_RULES"
    _sweep(fx, rules)
    return _drain(fx, reports.FREEZE_TASK_TYPE, handler)


def _post_close_offering(fx: _Fx, ref: str) -> None:
    resp = fx.client.post(f"/v1/offerings/{ref}/close", headers=ADMIN)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["state"] == "closed"


def _post_archive(fx: _Fx, ref: str) -> None:
    prepared = fx.client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text[:300]
    resp = fx.client.post(f"/v1/offerings/{ref}/archive",
                          json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["state"] == "archived"


# --- cenário sem alvo -------------------------------------------------------------------------------


def _plain_prepare(fx: _Fx, code: str, *, qid: str = QID) -> str:
    closes_at = fx.clock.now() + WINDOW
    body = prepare_dispatch(fx.client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid=qid, closes_at=iso(closes_at))
    assert body["state"] == "validating"
    return body["dispatch_id"]


def _plain_scheduled(fx: _Fx, code: str) -> str:
    dispatch_id = _plain_prepare(fx, code)
    _validate_all(fx)
    body = get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)
    assert (body["state"], body["confirmed"]) == ("scheduled", False), body.get("rejection")
    return dispatch_id


def _plain_open(fx: _Fx, code: str) -> str:
    dispatch_id = _plain_scheduled(fx, code)
    confirm_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)
    _open_due(fx)
    body = get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)
    assert (body["state"], body["target_count"]) == ("open", 0)
    return dispatch_id


def _plain_closed(fx: _Fx, code: str) -> str:
    """Um disparo sem alvo fechado pelo horário, ainda sem versão congelada."""
    dispatch_id = _plain_open(fx, code)
    closes_at = _at(get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["closes_at"])
    fx.clock.set(closes_at + timedelta(seconds=1))
    _close_due(fx)
    assert get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["state"] == "closed"
    return dispatch_id


def _not_opened(fx: _Fx, code: str) -> dict[str, str]:
    """Os 4 estados que não abriram, cada um pelo caminho real: `validating` (preparado, sem
    validação), `scheduled` (validado), `rejected` (questão não suportada) e `cancelled` (pelo operador)."""
    rejected = _plain_prepare(fx, code, qid=UNSUPPORTED_QID)
    scheduled = _plain_prepare(fx, code)
    cancelled = _plain_prepare(fx, code)
    _validate_all(fx)
    cancel_dispatch(fx.client, ADMIN_TOKEN, code, cancelled)
    validating = _plain_prepare(fx, code)
    states = {"validating": validating, "scheduled": scheduled, "rejected": rejected, "cancelled": cancelled}
    for state, dispatch_id in states.items():
        assert get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["state"] == state
    return states


# --- cenário respondido -----------------------------------------------------------------------------


@dataclasses.dataclass
class _Student:
    label: str
    person_row: int
    pseudo_id: str
    alias: str
    chat_id: int
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
    """Um oferecimento ativo com bot, `students` alunos vinculados e o worker do console com rota (ver o
    docstring do módulo)."""

    def __init__(self, fx: _Fx, *, students: int, prefix: str = "relatorio") -> None:
        self.fx = fx
        self.offering, self.bot_row = _offering(fx, prefix)
        self.code = self.offering["code"]
        self.stream = io.StringIO()
        n = next(_counter)
        rows = [{"name": f"Pessoa Relatorio {n:03d} {chr(65 + k)}", "ra": f"58{n:04d}{k:04d}",
                 "email": f"pessoa.relatorio.{n}.{k}@turma.example.org"} for k in range(students)]
        import_students(fx.client, ADMIN_TOKEN, self.code, rows)
        self.students = [self._link(chr(65 + k), row["ra"]) for k, row in enumerate(rows)]

    def _link(self, label: str, ra: str) -> _Student:
        fx = self.fx
        with fx.sql.cursor(row_factory=dict_row) as cur:
            found = cur.execute(
                "SELECT p.id AS person_row, p.pseudo_id::text AS pseudo, p.alias, e.id AS enrollment_row "
                "FROM identities i JOIN people p ON p.id = i.person_id JOIN enrollments e ON e.person_id = p.id "
                "JOIN offerings o ON o.id = e.offering_id WHERE i.ra = %s AND o.offering_id = %s",
                (ra, uuid.UUID(self.offering["offering_id"]))).fetchone()
        assert found is not None
        now = fx.clock.now()
        fx.sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                       "terms_accepted_at = %s WHERE id = %s", (now, now, found["enrollment_row"]))
        chat_id = make_user(next(_users)).user_id
        fx.sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                       "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                       (found["person_row"], self.bot_row, chat_id, now, now))
        inbox = ConsoleInbox(fx.db_url, clock=fx.clock)
        fx.inboxes.append(inbox)
        console = ConsoleStudent(inbox, bot_id=self.bot_row, chat_id=chat_id, recipient=found["pseudo"],
                                 source=self.stream)
        return _Student(label=label, person_row=int(found["person_row"]), pseudo_id=found["pseudo"],
                        alias=found["alias"], chat_id=chat_id, console=console)

    def student(self, label: str) -> _Student:
        return next(s for s in self.students if s.label == label)

    # -- worker -----------------------------------------------------------------------------------

    def start(self, **kwargs: Any) -> None:
        self.fx.worker_thread(channel=ConsoleChannel(self.stream), console_routes=True, engine=_engine(self.fx),
                              fetcher=self.fx.repo_fetcher, delivery=DeliverySettings(), **kwargs)

    def stop(self) -> None:
        self.fx.worker_thread.stop()

    # -- disparo ----------------------------------------------------------------------------------

    def open(self) -> _Opened:
        """Prepara o imediato com `closes_at` (agora + 10 min), espera a validação, confirma e espera toda
        entrega `delivered` com a questão e o pedido do campo no console de cada aluno."""
        fx = self.fx
        prepared = prepare_dispatch(fx.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=QID,
                                    closes_at=iso(fx.clock.now() + WINDOW))
        dispatch_id = prepared["dispatch_id"]
        _wait(lambda: get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)["state"] != "validating",
              "a validação do disparo")
        body = get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)
        assert (body["state"], body["confirmed"]) == ("scheduled", False), body.get("rejection")
        known = {s.label: {line.index for line in self._questions(s)} for s in self.students}
        confirm_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)
        row = _row(fx, dispatch_id)

        def settled() -> bool:
            states = [r[0] for r in fx.sql.execute("SELECT state FROM deliveries WHERE dispatch_id = %s",
                                                   (row,)).fetchall()]
            queued = fx.sql.execute(
                "SELECT count(*) FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
                "WHERE d.dispatch_id = %s AND o.state IN ('pending', 'leased')", (row,)).fetchone()[0]
            if states != ["delivered"] * len(self.students) or queued:
                return False
            return all(len([q for q in self._questions(s) if q.index not in known[s.label]]) == 1
                       for s in self.students)

        _wait(settled, "as entregas da abertura")
        questions = {s.label: next(q for q in self._questions(s) if q.index not in known[s.label])
                     for s in self.students}
        _wait(lambda: all(self._prompts(s, questions[s.label]) for s in self.students), "os pedidos do campo")
        opened = get_dispatch(fx.client, ADMIN_TOKEN, self.code, dispatch_id)
        assert opened["state"] == "open"
        sent = {r[0] for r in fx.sql.execute("SELECT sent_at FROM deliveries WHERE dispatch_id = %s",
                                             (row,)).fetchall()}
        assert len(sent) == 1, "as questões saíram em horários diferentes"
        return _Opened(dispatch_id=dispatch_id, row=row, opens_at=opened["opens_at"],
                       closes_at=opened["closes_at"], sent_at=sent.pop(), questions=questions)

    @staticmethod
    def _questions(student: _Student) -> list[ConsoleLine]:
        return [line for line in student.console.lines() if line.answerable and not line.has_rich_fields]

    @staticmethod
    def _prompts(student: _Student, question: ConsoleLine) -> list[ConsoleLine]:
        return [line for line in student.console.lines()
                if line.reply_request is not None and str(line.reply_to) == question.channel_ref]

    def submissions(self, opened: _Opened, student: _Student) -> list[dict]:
        with self.fx.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT s.outcome, s.valid, s.late, s.response_ms FROM submissions s "
                "JOIN deliveries d ON d.id = s.delivery_id JOIN enrollments e ON e.id = d.enrollment_id "
                "WHERE d.dispatch_id = %s AND e.person_id = %s ORDER BY s.id",
                (opened.row, student.person_row)).fetchall()

    def answer(self, opened: _Opened, label: str, text: str, *, valid: bool = True) -> dict:
        """Responde ao último pedido do campo e espera a correção (`graded`); com `valid=False`, espera também o
        novo pedido do campo."""
        student = self.student(label)
        question = opened.questions[label]
        prompts = self._prompts(student, question)
        before = len(self.submissions(opened, student))
        student.console.reply(prompts[-1], text)

        def graded() -> dict | None:
            subs = self.submissions(opened, student)
            return subs[-1] if len(subs) == before + 1 and subs[-1]["outcome"] == "graded" else None

        sub = _wait(graded, f"a correção da resposta de {label}")
        assert sub["valid"] is valid, f"{label}: validade {sub['valid']}"
        if not valid:
            _wait(lambda: len(self._prompts(student, question)) == len(prompts) + 1, f"o novo pedido para {label}")
        return sub

    def answered_scenario(self) -> _Opened:
        """O cenário respondido do docstring do módulo (worker no ar do começo ao fim; o relógio avança só com
        as entregas feitas)."""
        self.start()
        opened = self.open()
        clock = self.fx.clock
        clock.advance(5)
        self.answer(opened, "A", "abc", valid=False)
        for label, text, valid_at in (("A", "6.5", 10), ("B", "6.5", 20), ("C", "13,0", 30), ("D", "13", 40)):
            clock.set(opened.sent_at + timedelta(seconds=valid_at))
            sub = self.answer(opened, label, text)
            assert (sub["late"], sub["response_ms"]) == (False, valid_at * 1000)
        return opened

    def close(self, opened: _Opened) -> datetime:
        """Para o worker, leva o relógio além de `closes_at` e fecha pela varredura e pelo tratador reais."""
        self.stop()
        self.fx.clock.set(_at(opened.closes_at) + timedelta(seconds=1))
        _close_due(self.fx)
        assert get_dispatch(self.fx.client, ADMIN_TOKEN, self.code, opened.dispatch_id)["state"] == "closed"
        return self.fx.clock.now()


# --- o `Report` esperado ----------------------------------------------------------------------------


def _pair(on_time: int, with_late: int | None = None) -> dict:
    return {"on_time": on_time, "with_late": on_time if with_late is None else with_late}


def _rate(on_time: float | None, with_late: float | None = None) -> dict:
    return {"on_time": on_time, "with_late": on_time if with_late is None else with_late}


def _top(items: list[tuple[str, int]], others: int) -> dict:
    return {"items": [{"value": value, "count": _pair(count)} for value, count in items], "others": _pair(others)}


def _times(values: list[int]) -> dict:
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    assert median == statistics.median(values)
    return {"observations": len(values), "negative": sum(v < 0 for v in values), "median_ms": median,
            "q1_ms": q1, "q3_ms": q3}


def _scenario_report(world: _World, opened: _Opened, *, state: str, computed_at: datetime,
                     frozen: dict | None = None, title: str | None = TITLE) -> dict:
    """O `Report` exato do cenário respondido (números à mão, no docstring do módulo)."""
    return {
        "report_schema_version": "1",
        "offering": _oref(world.offering),
        "timezone": TZ,
        "dispatch": {"dispatch_id": opened.dispatch_id, "question": {"qid": QID, "commit_short": COMMIT_SHORT},
                     "title": title, "state": state, "partial": state == "open", "origin": "api",
                     "opens_at": opened.opens_at, "closes_at": opened.closes_at},
        "computed_at": iso(computed_at),
        "frozen": frozen,
        "grading_pending": 0,
        "reach": {"target": 5, "delivered": 5, "not_delivered": 0,
                  "not_delivered_by_reason": {"endpoint_blocked": 0, "channel_refused": 0, "engine_unavailable": 0,
                                              "bot_no_access": 0, "channel_unavailable": 0},
                  "question_failed": 0, "question_failed_by_stage": {"preparation": 0, "grading": 0},
                  "pending": 0},
        "performance": {"answered": _pair(4), "correct": _pair(2), "partially_correct": _pair(0),
                        "incorrect": _pair(2), "dont_know": _pair(0), "correct_rate": _rate(0.4),
                        "partially_correct_rate": _rate(0.0), "incorrect_rate": _rate(0.4),
                        "response_rate": _rate(0.8), "mean_score": _rate(0.5)},
        "response_time": _times([10_000, 20_000, 30_000, 40_000]),
        "distribution": {"fields": [{"index": 0, "type": "number", "label": LABEL, "unavailable": None,
                                     "choices": None, "values": _top([("6.5", 2)], 2),
                                     "ratios": _top([("1", 2), ("2", 2)], 0), "unrated": _pair(0),
                                     "texts": None}]},
        "friction": {"fields": [{"index": 0, "format_errors": _pair(1), "decimal_comma": _pair(1)}],
                     "unmapped_format_errors": _pair(0)},
    }


def _frozen(version: int, trigger: str, at: datetime) -> dict:
    return {"version": version, "trigger": trigger, "frozen_at": iso(at)}


def _url(code: str, dispatch_id: str, suffix: str = "") -> str:
    return f"/v1/offerings/{code}/dispatches/{dispatch_id}/report{suffix}"


def _get(fx: _Fx, code: str, dispatch_id: str, *, params: Any = None, token: str | None = ADMIN_TOKEN,
         suffix: str = "") -> Any:
    return fx.client.get(_url(code, dispatch_id, suffix), params=params, headers=auth(token))


def _resend(fx: _Fx, code: str, dispatch_id: str, *, token: str | None = ADMIN_TOKEN, **kwargs: Any) -> Any:
    return fx.client.post(_url(code, dispatch_id, "/resend"), headers=auth(token), **kwargs)


def _json_error(resp: Any, status: int, code: str, **extra: Any) -> None:
    """O erro exato no envelope JSON, com a mídia `application/json` (também com `format=csv`)."""
    assert resp.headers.get("content-type", "").startswith("application/json"), resp.headers.get("content-type")
    assert_error(resp, status, code, **extra)


def _no_identity(text: str, world: _World | None) -> None:
    """nenhum pseudônimo, apelido, nome, RA ou e-mail de aluno como dado; nenhum nome de campo
    de gabarito nem a sentinela do gabarito do motor."""
    assert ANSWER_SENTINEL not in text
    if world is None:
        return
    for student in world.students:
        for value in (student.pseudo_id, student.alias, f"Pessoa Relatorio"):
            assert value not in text, "identidade de aluno numa resposta de relatório"


def _no_answer_key_names(node: Any, extra: tuple[str, ...] = ()) -> None:
    names = set(ANSWER_KEY_NAMES) | set(extra)
    if isinstance(node, dict):
        assert not set(node) & names, f"nome de gabarito no relatório: {set(node) & names}"
        for value in node.values():
            _no_answer_key_names(value, extra)
    elif isinstance(node, list):
        for value in node:
            _no_answer_key_names(value, extra)


# --- releitura do CSV -------------------------------------------------------------------------------


def _csv_rows(text: str) -> list[list[str]]:
    """As linhas de dados do CSV (sem o BOM e sem o cabeçalho), com um `'` inicial tirado das 4 células de
    texto (a releitura tira sempre um `'` inicial)."""
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text[1:], newline=""), strict=True))
    assert rows[0] == ["section", "field", "key", "metric", "on_time", "with_late"]
    out = []
    for row in rows[1:]:
        out.append([cell[1:] if index < 4 and cell.startswith("'") else cell for index, cell in enumerate(row)])
    return out


def _num(cell: str) -> float | None:
    return None if cell == "" else float(cell)


def _json_pairs(node: Any) -> list[tuple[float | None, float]]:
    out: list[tuple[float | None, float]] = []
    if isinstance(node, dict):
        if set(node) == {"on_time", "with_late"}:
            if node["with_late"] is not None:
                out.append((None if node["on_time"] is None else float(node["on_time"]), float(node["with_late"])))
        else:
            for value in node.values():
                out += _json_pairs(value)
    elif isinstance(node, list):
        for value in node:
            out += _json_pairs(value)
    return out


def _assert_csv_rereads_as_json(text: str, body: dict) -> None:
    """A releitura do CSV igual ao JSON (ver o docstring do módulo, "CSV")."""
    rows = _csv_rows(text)
    paired = Counter((_num(r[4]), float(r[5])) for r in rows if r[5] != "")
    assert paired == Counter(_json_pairs(body)), "as contagens em par do CSV diferem das do JSON"
    for field in body["distribution"]["fields"]:
        lists = [field[name] for name in ("values", "ratios", "texts") if field[name] is not None]
        items = [(item["value"], item["count"]) for top in lists for item in top["items"]]
        items += [(choice["content_hash"], choice["count"]) for choice in field["choices"] or ()]
        for key, count in items:
            found = [r for r in rows if r[1] == str(field["index"]) and r[2] == key
                     and (_num(r[4]), _num(r[5])) == (count["on_time"], count["with_late"])]
            assert found, f"o item {key!r} do campo {field['index']} não está no CSV com a contagem"
    reach = body["reach"]
    named = list(reach["not_delivered_by_reason"].items()) + list(reach["question_failed_by_stage"].items())
    for key, count in named:
        assert [r for r in rows if r[2] == key and _num(r[4]) == count and r[5] == ""], f"motivo {key} fora do CSV"
    scalars = Counter(_num(r[4]) for r in rows if r[5] == "" and r[4] != "")
    expected = [reach[k] for k in ("target", "delivered", "not_delivered", "question_failed", "pending")]
    expected += [body["grading_pending"]]
    expected += [v for v in body["response_time"].values() if v is not None]
    assert not Counter(float(v) for v in expected) - scalars, "escalar do JSON ausente do CSV"


def _assert_csv_is_renderer_output(resp: Any, body: dict) -> str:
    """o corpo do CSV é o `CsvReportRenderer().render` do `Report` do JSON da mesma consulta."""
    from cq_server.reportrender import CsvReportRenderer

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == REPORT_CSV_MEDIA
    rendered = CsvReportRenderer().render(reports.report_from_dict(body))
    assert rendered.media_type == REPORT_CSV_MEDIA
    assert resp.content == rendered.body.encode("utf-8"), "o CSV da API não é o do CsvReportRenderer"
    text = resp.content.decode("utf-8")
    _assert_csv_rereads_as_json(text, body)
    return text


# ---------------------------------------------------------------------------------------------------
# Leitura — reports.get
# ---------------------------------------------------------------------------------------------------


def test_get_returns_the_exact_report_computed_now_for_open_and_closed_dispatch(fx: _Fx) -> None:
    """com o disparo aberto, `reports.get` sem
    consulta (e com `format=json`) devolve o `Report` exato do cenário respondido, marcado parcial, com
    `computed_at` = agora e `frozen = null` (`assert_matches`); fechado (E fica `no_answer` e continua nas
    entregues), o mesmo relatório com `state = closed`, sem `partial`; o leitor com `offerings:read` consulta;
    o mesmo pelo `offering_id`; horários em UTC com `Z`; nenhum pseudônimo, apelido ou nome de aluno, nenhum
    nome de campo de gabarito e nenhuma sentinela do gabarito, que está gravada na submissão (controle
    positivo)."""
    world = _World(fx, students=5)
    opened = world.answered_scenario()
    assert fx.sql.execute(
        "SELECT count(*) FROM submissions s JOIN deliveries d ON d.id = s.delivery_id "
        "WHERE d.dispatch_id = %s AND s.correct_answers::text LIKE %s", (opened.row, f"%{ANSWER_SENTINEL}%"),
    ).fetchone()[0] == 5, "controle positivo: a sentinela do gabarito está nas submissões corrigidas"
    reader, _ = create_token(fx.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")

    now = fx.clock.now()
    expected = _scenario_report(world, opened, state="open", computed_at=now)
    body = get_report(fx.client, world.code, opened.dispatch_id)
    assert body == expected
    assert get_report(fx.client, world.code, opened.dispatch_id, fmt="json") == expected
    assert get_report(fx.client, reader, world.offering["offering_id"], opened.dispatch_id) == expected
    text = json.dumps(body, ensure_ascii=False)
    _no_identity(text, world)
    _no_answer_key_names(body)
    _no_answer_key_names(body["distribution"], extra=("correct",))
    assert all(body["dispatch"][k].endswith("Z") for k in ("opens_at", "closes_at"))

    closed_at = world.close(opened)
    states = dict(fx.sql.execute(
        "SELECT p.pseudo_id::text, d.state FROM deliveries d JOIN enrollments e ON e.id = d.enrollment_id "
        "JOIN people p ON p.id = e.person_id WHERE d.dispatch_id = %s", (opened.row,)).fetchall())
    assert states[world.student("E").pseudo_id] == "no_answer"
    closed = get_report(fx.client, world.code, opened.dispatch_id)
    assert closed == _scenario_report(world, opened, state="closed", computed_at=closed_at)
    assert_matches("reports.get", 200, closed)


def test_csv_is_the_csv_renderer_output_and_rereads_as_the_json_now_and_frozen(fx: _Fx) -> None:
    """`format=csv` → 200 `text/csv; charset=utf-8`, corpo
    com BOM e `\\r\\n` igual, byte a byte, ao `CsvReportRenderer().render` do `Report` que `reports.get` devolve
    em JSON no mesmo estado, e relido igual ao JSON (contagens em par, itens dos mais comuns, motivos e
    escalares), com o disparo aberto (cálculo no momento) e na versão congelada (`version=1` e
    `version=latest`); o CSV não traz pseudônimo, apelido nem nome de aluno."""
    world = _World(fx, students=5)
    opened = world.answered_scenario()
    body = get_report(fx.client, world.code, opened.dispatch_id)
    csv_now = _assert_csv_is_renderer_output(_get(fx, world.code, opened.dispatch_id, params={"format": "csv"}), body)
    assert csv_now.endswith("\r\n")
    _no_identity(csv_now, world)

    frozen_at = world.close(opened)
    assert _freeze_due(fx) == 1
    for version in ("1", "latest"):
        frozen = get_report(fx.client, world.code, opened.dispatch_id, version=version)
        assert frozen == _scenario_report(world, opened, state="closed", computed_at=frozen_at,
                                          frozen=_frozen(1, "closing", frozen_at))
        resp = _get(fx, world.code, opened.dispatch_id, params={"format": "csv", "version": version})
        _no_identity(_assert_csv_is_renderer_output(resp, frozen), world)


INVALID_GET_QUERIES = [
    pytest.param([("format", "xml")], [("format", "invalid_value")], id="format-xml"),
    pytest.param([("format", "CSV")], [("format", "invalid_value")], id="format-maiusculo"),
    pytest.param([("format", "")], [("format", "invalid_value")], id="format-vazio"),
    pytest.param([("format", "json"), ("format", "csv")], [("format", "invalid_value")], id="format-repetido"),
    pytest.param([("version", "0")], [("version", "invalid_format")], id="version-0"),
    pytest.param([("version", "01")], [("version", "invalid_format")], id="version-zero-a-esquerda"),
    pytest.param([("version", "-1")], [("version", "invalid_format")], id="version-negativa"),
    pytest.param([("version", "LATEST")], [("version", "invalid_format")], id="version-latest-maiusculo"),
    pytest.param([("version", "1.0")], [("version", "invalid_format")], id="version-fracao"),
    pytest.param([("version", "12345678901")], [("version", "invalid_format")], id="version-11-digitos"),
    pytest.param([("version", "2147483648")], [("version", "out_of_range")], id="version-2147483648"),
    pytest.param([("version", "9999999999")], [("version", "out_of_range")], id="version-10-digitos"),
    pytest.param([("parametro_sentinela", "1")], [("query", "unknown_field")], id="consulta-desconhecida"),
]


@pytest.mark.parametrize(("params", "fields"), INVALID_GET_QUERIES)
def test_get_rejects_invalid_format_and_version_in_the_json_envelope(fx: _Fx, params: list,
                                                                     fields: list[tuple[str, str]]) -> None:
    """Contrato (`format` fora de `json`/`csv` ou repetido → `[{format,
    invalid_value}]`; `version` fora de `^([1-9][0-9]{0,9}|latest)$` → `[{version, invalid_format}]`, acima de
    2147483647 → `[{version, out_of_range}]`; consulta desconhecida → `[{query, unknown_field}]`): 400 exato no
    envelope JSON, mesmo com `format=csv` junto, e antes do 403 (token sem `offerings:read`) e do 404 do
    oferecimento; a mesma consulta sem o erro devolve 200 (controle positivo)."""
    offering, _ = _offering(fx, "consulta-relatorio")
    dispatch_id = _plain_open(fx, offering["code"])
    no_read, _ = create_token(fx.client, ADMIN_TOKEN, ["people:read"], label="sem leitura")
    resp = _get(fx, offering["code"], dispatch_id, params=params)
    _json_error(resp, 400, "invalid_request", fields=fields)
    if all(key != "format" for key, _ in params):
        with_csv = _get(fx, offering["code"], dispatch_id, params=[*params, ("format", "csv")])
        _json_error(with_csv, 400, "invalid_request", fields=fields)
    _json_error(_get(fx, offering["code"], dispatch_id, params=params, token=no_read), 400, "invalid_request",
                fields=fields)
    _json_error(_get(fx, "mc999-inexistente", dispatch_id, params=params), 400, "invalid_request", fields=fields)
    assert get_report(fx.client, offering["code"], dispatch_id)["dispatch"]["dispatch_id"] == dispatch_id


def test_get_follows_the_contract_order_and_answers_errors_in_json(fx: _Fx) -> None:
    """Contrato (401 → 404/405 da rota → 400 da forma → 403 → 404 do oferecimento →
    404 do disparo, inexistente ou de outro oferecimento → `invalid_dispatch_state` → 404 da versão): sem
    token → 401; método fora da rota → 405; `dispatch_id` e `ref` malformados → 400 sem eco, antes do 403 e
    do 404; token sem `offerings:read` → 403 (antes do 404 do oferecimento); oferecimento inexistente (código
    e identificador) → 404; disparo inexistente e o de outro oferecimento (pelo código e pelo identificador
    deste) → 404, com o 200 pelo dono (controle positivo); `version=1` num disparo em `validating` →
    `invalid_dispatch_state` (antes da versão); num aberto sem versão → 404. Com `format=csv`, todo erro sai no
    envelope JSON."""
    mine, _ = _offering(fx, "ordem-minha")
    other, _ = _offering(fx, "ordem-outra")
    no_read, _ = create_token(fx.client, ADMIN_TOKEN, ["people:read"], label="sem leitura")
    foreign = _plain_open(fx, other["code"])
    validating = _plain_prepare(fx, other["code"])
    assert get_report(fx.client, other["code"], foreign)["dispatch"]["state"] == "open"

    for fmt in (None, "csv"):
        params = {} if fmt is None else {"format": fmt}
        _json_error(_get(fx, other["code"], foreign, params=params, token=None), 401, "unauthenticated")
        for method in ("POST", "DELETE"):
            resp = fx.client.request(method, _url(other["code"], foreign), params=params, headers=ADMIN)
            _json_error(resp, 405, "method_not_allowed")
        for bad in ("disparo-sentinela-invalido", foreign.upper(), foreign.replace("-", "")):
            resp = _get(fx, other["code"], bad, params=params)
            _json_error(resp, 400, "invalid_request", fields=[("dispatch_id", "invalid_format")])
            assert_no_echo(resp, bad)
            _json_error(_get(fx, "mc999-inexistente", bad, params=params, token=no_read), 400, "invalid_request",
                        fields=[("dispatch_id", "invalid_format")])
        _json_error(_get(fx, "Oferta_Formato_Invalido", foreign, params=params), 400, "invalid_request",
                    fields=[("ref", "invalid_format")])
        _json_error(_get(fx, other["code"], foreign, params=params, token=no_read), 403, "forbidden",
                    permission="offerings:read")
        _json_error(_get(fx, "mc999-inexistente", foreign, params=params, token=no_read), 403, "forbidden",
                    permission="offerings:read")
        for ref in ("mc999-inexistente", MISSING_OFFERING_ID):
            _json_error(_get(fx, ref, foreign, params=params), 404, "not_found")
        _json_error(_get(fx, other["code"], MISSING_DISPATCH_ID, params=params), 404, "not_found")
        for ref in (mine["code"], mine["offering_id"]):
            _json_error(_get(fx, ref, foreign, params=params), 404, "not_found")
        _json_error(_get(fx, other["code"], validating, params={**params, "version": "1"}), 409,
                    "invalid_dispatch_state")
        _json_error(_get(fx, other["code"], foreign, params={**params, "version": "1"}), 404, "not_found")


@pytest.mark.parametrize("suffix", ["", "/versions"], ids=["get", "list_versions"])
def test_dispatch_that_never_opened_is_refused_with_invalid_dispatch_state(fx: _Fx, suffix: str) -> None:
    """Contrato: `reports.get` (sem consulta, com `format=csv` e com
    `version=latest`) e `reports.list_versions` de um disparo em `validating`, `scheduled`, `rejected` ou
    `cancelled` → 409 `invalid_dispatch_state` no envelope JSON; nada é gravado (nenhuma versão, nenhum
    aviso, nenhum evento novo); o aberto do mesmo oferecimento responde 200 (controle positivo)."""
    offering, _ = _offering(fx, "nunca-abriu")
    states = _not_opened(fx, offering["code"])
    opened = _plain_open(fx, offering["code"])
    seq = fx.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]
    queries = [{}] if suffix else [{}, {"format": "csv"}, {"version": "latest"}]
    for state, dispatch_id in states.items():
        for params in queries:
            _json_error(_get(fx, offering["code"], dispatch_id, params=params, suffix=suffix), 409,
                        "invalid_dispatch_state")
    assert fx.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0] == seq
    assert fx.sql.execute("SELECT count(*) FROM dispatch_reports").fetchone()[0] == 0
    assert fx.sql.execute("SELECT count(*) FROM team_notices WHERE kind IN ('dispatch_closed', 'summary_resent')"
                          ).fetchone()[0] == 0
    ok = _get(fx, offering["code"], opened, suffix=suffix)
    assert ok.status_code == 200, ok.text[:300]


def test_version_latest_and_missing_versions_are_not_found(fx: _Fx) -> None:
    """Contrato: `version=latest` sem versão congelada → 404, no aberto e no
    fechado ainda sem a versão 1; depois do congelamento, `latest` e `1` devolvem a versão 1 com `frozen`
    preenchido (`closing`, o horário do congelamento) e o conteúdo gravado, e `2` (inexistente) → 404; no JSON
    e no CSV."""
    offering, _ = _offering(fx, "versao-relatorio")
    code = offering["code"]
    dispatch_id = _plain_open(fx, code)
    for params in ({"version": "latest"}, {"version": "latest", "format": "csv"}, {"version": "1"}):
        _json_error(_get(fx, code, dispatch_id, params=params), 404, "not_found")
    closes_at = _at(get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["closes_at"])
    fx.clock.set(closes_at + timedelta(seconds=1))
    _close_due(fx)
    _json_error(_get(fx, code, dispatch_id, params={"version": "latest"}), 404, "not_found")
    now_body = get_report(fx.client, code, dispatch_id)
    assert (now_body["frozen"], now_body["dispatch"]["state"], now_body["reach"]["target"]) == (None, "closed", 0)

    frozen_at = fx.clock.now()
    assert _freeze_due(fx) == 1
    expected = {**now_body, "frozen": _frozen(1, "closing", frozen_at)}
    assert get_report(fx.client, code, dispatch_id, version="latest") == expected
    assert get_report(fx.client, code, dispatch_id, version=1) == expected
    for params in ({"version": "2"}, {"version": "2", "format": "csv"}, {"version": "2147483647"}):
        _json_error(_get(fx, code, dispatch_id, params=params), 404, "not_found")


def test_frozen_version_is_redacted_again_on_read(fx: _Fx) -> None:
    """A versão congelada sai redigida de novo, com a redação automática: a
    versão 1 é congelada com o título da questão (`Velocidade média`) em claro; uma pessoa cadastrada depois
    com esse nome completo faz o título sair como `[pessoa <apelido>]` na leitura da versão 1 (JSON e CSV) e no
    cálculo no momento, enquanto o `content` gravado continua com o título original (controle positivo: a
    redação é na saída)."""
    world = _World(fx, students=5)
    opened = world.answered_scenario()
    frozen_at = world.close(opened)
    assert _freeze_due(fx) == 1
    before = get_report(fx.client, world.code, opened.dispatch_id, version=1)
    assert before["dispatch"]["title"] == TITLE

    person = register_person(fx.client, ADMIN_TOKEN, name="Velocidade Média")
    hidden = marker(person["alias"])
    expected = _scenario_report(world, opened, state="closed", computed_at=frozen_at,
                                frozen=_frozen(1, "closing", frozen_at), title=hidden)
    assert get_report(fx.client, world.code, opened.dispatch_id, version=1) == expected
    now_body = get_report(fx.client, world.code, opened.dispatch_id)
    assert now_body["dispatch"]["title"] == hidden
    csv_text = get_report(fx.client, world.code, opened.dispatch_id, version=1, fmt="csv")
    assert TITLE.casefold() not in csv_text.casefold()
    stored = fx.sql.execute("SELECT content->'dispatch'->>'title', content->'frozen' FROM dispatch_reports "
                            "WHERE dispatch_id = %s AND version = 1", (opened.row,)).fetchone()
    assert stored == (TITLE, None), "o conteúdo gravado não muda: a redação nova é só na leitura"


def test_archived_offering_report_and_versions_are_still_readable(fx: _Fx) -> None:
    """Depois de encerrar e arquivar o oferecimento,
    `reports.get` (no momento, `version=1` e CSV) e `reports.list_versions` continuam respondendo 200 com o
    mesmo conteúdo de antes do arquivamento."""
    world = _World(fx, students=5)
    opened = world.answered_scenario()
    frozen_at = world.close(opened)
    assert _freeze_due(fx) == 1
    now_before = get_report(fx.client, world.code, opened.dispatch_id)
    v1_before = get_report(fx.client, world.code, opened.dispatch_id, version=1)
    csv_before = get_report(fx.client, world.code, opened.dispatch_id, version=1, fmt="csv")
    page_before = list_report_versions(fx.client, world.code, opened.dispatch_id)
    assert page_before == {"offering": _oref(world.offering), "dispatch_id": opened.dispatch_id,
                           "items": [_frozen(1, "closing", frozen_at)], "next_cursor": 1}

    _post_close_offering(fx, world.code)
    _post_archive(fx, world.code)
    assert get_report(fx.client, world.code, opened.dispatch_id) == now_before
    assert get_report(fx.client, world.code, opened.dispatch_id, version=1) == v1_before
    assert get_report(fx.client, world.code, opened.dispatch_id, version=1, fmt="csv") == csv_before
    assert list_report_versions(fx.client, world.code, opened.dispatch_id) == page_before


# ---------------------------------------------------------------------------------------------------
# Leitura — reports.list_versions
# ---------------------------------------------------------------------------------------------------


def test_list_versions_is_empty_while_open_and_before_the_closing_version(fx: _Fx) -> None:
    """Contrato: lista vazia é válida no aberto e no fechado ainda sem a versão 1, com
    `next_cursor` = 0 (o `after` padrão, como `dispatches.list`) ou o `after` recebido; depois do congelamento,
    `[{1, closing, frozen_at}]` com `next_cursor = 1`, e `after=1` → vazia com `next_cursor = 1`."""
    offering, _ = _offering(fx, "versoes-vazias")
    code = offering["code"]
    dispatch_id = _plain_open(fx, code)
    empty = {"offering": _oref(offering), "dispatch_id": dispatch_id, "items": [], "next_cursor": 0}
    assert list_report_versions(fx.client, code, dispatch_id) == empty
    assert list_report_versions(fx.client, code, dispatch_id, after=7) == {**empty, "next_cursor": 7}
    closes_at = _at(get_dispatch(fx.client, ADMIN_TOKEN, code, dispatch_id)["closes_at"])
    fx.clock.set(closes_at + timedelta(seconds=1))
    _close_due(fx)
    assert list_report_versions(fx.client, code, dispatch_id) == empty
    frozen_at = fx.clock.now()
    assert _freeze_due(fx) == 1
    assert list_report_versions(fx.client, code, dispatch_id) == {
        **empty, "items": [_frozen(1, "closing", frozen_at)], "next_cursor": 1}
    assert list_report_versions(fx.client, code, dispatch_id, after=1) == {**empty, "next_cursor": 1}


INVALID_LIST_QUERIES = [
    pytest.param({"limit": "0"}, [("limit", "out_of_range")], id="limit-0"),
    pytest.param({"limit": "501"}, [("limit", "out_of_range")], id="limit-501"),
    pytest.param({"limit": "limite-sentinela"}, [("limit", "invalid_format")], id="limit-texto"),
    pytest.param({"after": "cursor-sentinela"}, [("after", "invalid_format")], id="after-texto"),
    pytest.param({"after": "-1"}, [("after", "out_of_range")], id="after-negativo"),
    pytest.param({"format": "csv"}, [("query", "unknown_field")], id="format-nao-existe-aqui"),
    pytest.param({"parametro_sentinela": "valor_sentinela"}, [("query", "unknown_field")], id="desconhecido"),
]


@pytest.mark.parametrize(("params", "fields"), INVALID_LIST_QUERIES)
def test_list_versions_rejects_invalid_query(fx: _Fx, params: dict, fields: list[tuple[str, str]]) -> None:
    """Contrato (`after` e `limit` da API base: padrão 100, teto 500): consulta inválida → 400 exato,
    sem eco, antes do 403 e do 404 do oferecimento; sem token → 401; token sem `offerings:read` → 403; disparo
    de outro oferecimento → 404; a consulta válida responde 200 (controle positivo)."""
    offering, _ = _offering(fx, "versoes-invalidas")
    other, _ = _offering(fx, "versoes-outra")
    code = offering["code"]
    dispatch_id = _plain_open(fx, code)
    no_read, _ = create_token(fx.client, ADMIN_TOKEN, ["people:read"], label="sem leitura")
    resp = _get(fx, code, dispatch_id, params=params, suffix="/versions")
    _json_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "limite-sentinela", "cursor-sentinela", "parametro_sentinela", "valor_sentinela")
    _json_error(_get(fx, code, dispatch_id, params=params, token=no_read, suffix="/versions"), 400,
                "invalid_request", fields=fields)
    _json_error(_get(fx, code, dispatch_id, token=None, suffix="/versions"), 401, "unauthenticated")
    _json_error(_get(fx, code, dispatch_id, token=no_read, suffix="/versions"), 403, "forbidden",
                permission="offerings:read")
    _json_error(_get(fx, "mc999-inexistente", dispatch_id, suffix="/versions"), 404, "not_found")
    _json_error(_get(fx, other["code"], dispatch_id, suffix="/versions"), 404, "not_found")
    assert list_report_versions(fx.client, code, dispatch_id, limit=500)["items"] == []


# ---------------------------------------------------------------------------------------------------
# Reenvio — reports.resend_summary
# ---------------------------------------------------------------------------------------------------


def _report_rows(fx: _Fx, row: int) -> list[tuple]:
    return fx.sql.execute("SELECT id, version, trigger, content, frozen_at FROM dispatch_reports "
                          "WHERE dispatch_id = %s ORDER BY version", (row,)).fetchall()


def _last_seq(fx: _Fx) -> int:
    return fx.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events(fx: _Fx, cursor: int, *types: str) -> list[dict]:
    items = list_events(fx.client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def test_resend_freezes_the_next_version_and_records_the_pending_summary_notice(fx: _Fx) -> None:
    """Reenvio: o disparo fechado com a versão
    `closing` → `reports.resend_summary` (sem corpo) → 200 exato `{offering, dispatch_id, frozen: {2, resend,
    agora}}`; a versão 2 gravada com `trigger = resend` e o conteúdo do cálculo no momento (igual ao
    `reports.get` sem consulta, com `frozen` nulo), o `report.frozen {offering_id, dispatch_id, 2, resend}`
    com o ator do token, e o aviso `summary_resent` registrado `pending` (`report:<id da versão 2>`,
    `report_id` da versão 2, `recipient_count = 0`, sem `finished_at`), sem mensagem na fila nem
    `notice.recorded` (a API não compõe o texto; o worker entrega); `reports.get?version=2` e
    `version=latest` devolvem a versão 2; a versão 1 não muda."""
    offering, _ = _offering(fx, "reenvio")
    code = offering["code"]
    dispatch_id = _plain_closed(fx, code)
    t_freeze = fx.clock.now()
    assert _freeze_due(fx) == 1
    row = _row(fx, dispatch_id)
    [v1] = _report_rows(fx, row)
    fx.clock.advance(90)
    now = fx.clock.now()
    computed = get_report(fx.client, code, dispatch_id)
    cursor = _last_seq(fx)
    outbox_before = fx.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0]

    body = resend_report_summary(fx.client, code, dispatch_id)
    assert body == {"offering": _oref(offering), "dispatch_id": dispatch_id, "frozen": _frozen(2, "resend", now)}
    rows = _report_rows(fx, row)
    assert rows[0] == v1, "a versão 1 não muda"
    assert [(r[1], r[2], r[4]) for r in rows] == [(1, "closing", t_freeze), (2, "resend", now)]
    assert rows[1][3] == {**computed, "frozen": None}
    assert get_report(fx.client, code, dispatch_id, version=2) == {**computed, "frozen": _frozen(2, "resend", now)}
    assert get_report(fx.client, code, dispatch_id, version="latest") == {
        **computed, "frozen": _frozen(2, "resend", now)}
    assert _events(fx, cursor, *REPORT_EVENT_TYPES) == [
        {"type": "report.frozen", "occurred_at": iso(now), "actor_token_id": "bootstrap",
         "data": {"offering_id": offering["offering_id"], "dispatch_id": dispatch_id, "version": 2,
                  "trigger": "resend"}}]
    notices = fx.sql.execute(
        "SELECT offering_id, kind, subject_key, state, recipient_count, dispatch_id, report_id, local_date, "
        "finished_at, created_at FROM team_notices WHERE kind = 'summary_resent'").fetchall()
    assert notices == [(_offering_row(fx, offering), "summary_resent", f"report:{rows[1][0]}", "pending", 0, row,
                        rows[1][0], None, None, now)]
    assert fx.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == outbox_before
    assert _events(fx, cursor, *NOTICE_EVENT_TYPES) == []


def test_resend_is_not_idempotent_and_pages_versions_by_number(fx: _Fx) -> None:
    """Reenvio não idempotente (cada pedido congela uma versão); contrato (paginação por
    `after` e `limit`, padrão 100, teto 500, cursor = número da versão): 101 reenvios → as versões 2 a
    102, cada uma com o seu `report.frozen` e o seu aviso `summary_resent`; a lista padrão traz as versões 1 a
    100 com `next_cursor = 100`; `after=100` → 101 e 102 (`next_cursor = 102`); `after=102` → vazia
    (`next_cursor = 102`); `limit=2` → 1 e 2; `after=2&limit=500` → 3 a 102; a versão 1 continua `closing`."""
    offering, _ = _offering(fx, "reenvio-paginas")
    code = offering["code"]
    dispatch_id = _plain_closed(fx, code)
    frozen_at = fx.clock.now()
    assert _freeze_due(fx) == 1
    cursor = _last_seq(fx)
    for version in range(2, 103):
        assert resend_report_summary(fx.client, code, dispatch_id)["frozen"] == _frozen(version, "resend", frozen_at)
    items = [_frozen(1, "closing", frozen_at)] + [_frozen(v, "resend", frozen_at) for v in range(2, 103)]
    base = {"offering": _oref(offering), "dispatch_id": dispatch_id}
    assert list_report_versions(fx.client, code, dispatch_id) == {**base, "items": items[:100], "next_cursor": 100}
    assert list_report_versions(fx.client, code, dispatch_id, after=100) == {
        **base, "items": items[100:], "next_cursor": 102}
    assert list_report_versions(fx.client, code, dispatch_id, after=102) == {**base, "items": [], "next_cursor": 102}
    assert list_report_versions(fx.client, code, dispatch_id, limit=2) == {**base, "items": items[:2], "next_cursor": 2}
    assert list_report_versions(fx.client, code, dispatch_id, after=2, limit=500) == {
        **base, "items": items[2:], "next_cursor": 102}
    frozen_events = _events(fx, cursor, *REPORT_EVENT_TYPES)
    assert [(e["data"]["version"], e["data"]["trigger"]) for e in frozen_events] == [
        (v, "resend") for v in range(2, 103)]
    assert fx.sql.execute("SELECT count(*) FROM team_notices WHERE kind = 'summary_resent' AND state = 'pending'"
                          ).fetchone()[0] == 101


def test_resend_in_closed_offering_is_accepted_and_in_archived_is_invalid_state(fx: _Fx) -> None:
    """Contrato: no oferecimento **encerrado** (`closed`), o reenvio
    responde 200 com a versão `resend`; depois do arquivamento, `invalid_state` com `state = archived`, sem
    versão nova nem aviso, e a leitura continua (`reports.list_versions` igual)."""
    offering, _ = _offering(fx, "reenvio-encerrado")
    code = offering["code"]
    dispatch_id = _plain_closed(fx, code)
    frozen_at = fx.clock.now()
    assert _freeze_due(fx) == 1
    _post_close_offering(fx, code)
    assert resend_report_summary(fx.client, code, dispatch_id)["frozen"] == _frozen(2, "resend", frozen_at)
    _post_archive(fx, code)
    page = list_report_versions(fx.client, code, dispatch_id)
    seq = _last_seq(fx)
    _json_error(_resend(fx, code, dispatch_id), 409, "invalid_state", state="archived")
    assert list_report_versions(fx.client, code, dispatch_id) == page
    assert [r[1] for r in _report_rows(fx, _row(fx, dispatch_id))] == [1, 2]
    assert _last_seq(fx) == seq


def test_resend_is_refused_for_open_closed_without_closing_and_never_opened(fx: _Fx) -> None:
    """Contrato: o disparo aberto, o fechado ainda sem a versão `closing` (o intervalo
    até o congelamento, que pode durar até 10 min) e os 4 que não abriram → 409 `invalid_dispatch_state`, sem
    versão gravada, sem aviso e sem evento; congelada a versão 1 (`closing`), o reenvio do mesmo disparo dá a
    versão 2 (controle positivo: a versão 1 é sempre a do fechamento)."""
    offering, _ = _offering(fx, "reenvio-recusado")
    code = offering["code"]
    states = _not_opened(fx, code)
    opened = _plain_open(fx, code)
    seq = _last_seq(fx)
    for dispatch_id in (*states.values(), opened):
        _json_error(_resend(fx, code, dispatch_id), 409, "invalid_dispatch_state")
    closes_at = _at(get_dispatch(fx.client, ADMIN_TOKEN, code, opened)["closes_at"])
    fx.clock.set(closes_at + timedelta(seconds=1))
    _close_due(fx)
    seq = _last_seq(fx)
    _json_error(_resend(fx, code, opened), 409, "invalid_dispatch_state")
    assert _last_seq(fx) == seq
    assert fx.sql.execute("SELECT count(*) FROM dispatch_reports").fetchone()[0] == 0
    assert fx.sql.execute("SELECT count(*) FROM team_notices WHERE kind = 'summary_resent'").fetchone()[0] == 0
    frozen_at = fx.clock.now()
    assert _freeze_due(fx) == 1
    assert [(r[1], r[2]) for r in _report_rows(fx, _row(fx, opened))] == [(1, "closing")]
    assert resend_report_summary(fx.client, code, opened)["frozen"] == _frozen(2, "resend", frozen_at)


def test_resend_rejects_a_body_and_follows_the_contract_order(fx: _Fx) -> None:
    """Contrato (401 → 404/405 da rota → 400 da forma e do corpo → 403 → 404 do oferecimento →
    404 do disparo → `invalid_state` → `invalid_dispatch_state`): sem token → 401; `GET` na rota do reenvio →
    405; corpo (JSON, `{}` ou texto) → 400 `[{body, unknown_field}]` sem eco, também com o token só de
    leitura (antes do 403); `dispatch_id` e `ref` malformados → 400; token só com `offerings:read` → 403
    `offerings:write`; oferecimento inexistente → 404; disparo inexistente e o de outro oferecimento → 404;
    no oferecimento arquivado, um disparo que nunca abriu → `invalid_state` (antes do
    `invalid_dispatch_state`). Nenhuma recusa grava versão, aviso ou evento; o pedido válido sem corpo
    responde 200 (controle positivo)."""
    offering, _ = _offering(fx, "reenvio-ordem")
    other, _ = _offering(fx, "reenvio-ordem-outra")
    archived, _ = _offering(fx, "reenvio-ordem-arquivada")
    code = offering["code"]
    dispatch_id = _plain_closed(fx, code)
    assert _freeze_due(fx) == 1
    cancelled = _plain_prepare(fx, archived["code"])
    _validate_all(fx)
    _post_close_offering(fx, archived["code"])
    _post_archive(fx, archived["code"])
    assert get_dispatch(fx.client, ADMIN_TOKEN, archived["code"], cancelled)["state"] == "cancelled"
    reader, _ = create_token(fx.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    seq = _last_seq(fx)
    versions = fx.sql.execute("SELECT count(*) FROM dispatch_reports").fetchone()[0]

    _json_error(_resend(fx, code, dispatch_id, token=None), 401, "unauthenticated")
    _json_error(fx.client.get(_url(code, dispatch_id, "/resend"), headers=ADMIN), 405, "method_not_allowed")
    for kwargs in ({"json": {"campo_extra_x": "valor-sentinela-corpo"}}, {"json": {}},
                   {"content": b"texto-sentinela-cru", "headers": {**ADMIN, "Content-Type": "text/plain"}}):
        for token in (ADMIN_TOKEN, reader):
            call = dict(kwargs)
            if "headers" in call:
                call["headers"] = {**call["headers"], **auth(token)}
                resp = fx.client.post(_url(code, dispatch_id, "/resend"), **call)
            else:
                resp = _resend(fx, code, dispatch_id, token=token, **call)
            _json_error(resp, 400, "invalid_request", fields=[("body", "unknown_field")])
            assert_no_echo(resp, "campo_extra_x", "valor-sentinela-corpo", "texto-sentinela-cru")
    _json_error(_resend(fx, code, "disparo-sentinela-invalido"), 400, "invalid_request",
                fields=[("dispatch_id", "invalid_format")])
    _json_error(_resend(fx, "Oferta_Formato_Invalido", dispatch_id), 400, "invalid_request",
                fields=[("ref", "invalid_format")])
    _json_error(_resend(fx, code, dispatch_id, token=reader), 403, "forbidden", permission="offerings:write")
    _json_error(_resend(fx, "mc999-inexistente", dispatch_id, token=reader), 403, "forbidden",
                permission="offerings:write")
    for ref in ("mc999-inexistente", MISSING_OFFERING_ID):
        _json_error(_resend(fx, ref, dispatch_id), 404, "not_found")
    _json_error(_resend(fx, code, MISSING_DISPATCH_ID), 404, "not_found")
    _json_error(_resend(fx, other["code"], dispatch_id), 404, "not_found")
    _json_error(_resend(fx, archived["code"], cancelled), 409, "invalid_state", state="archived")
    assert _last_seq(fx) == seq
    assert fx.sql.execute("SELECT count(*) FROM dispatch_reports").fetchone()[0] == versions
    assert fx.sql.execute("SELECT count(*) FROM team_notices WHERE kind = 'summary_resent'").fetchone()[0] == 0
    assert resend_report_summary(fx.client, code, dispatch_id)["frozen"]["version"] == 2
