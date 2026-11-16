"""As metas de tempo dos avisos e dos relatórios, medidas em tempo real
(exatas, sem margem).

Com o **worker já no ar** e os dublês, sem margem e sem repetição automática:
1. **Aviso enfileirado ≤ 5 s** depois da rejeição, do cancelamento automático e da última entrega da abertura
   (os avisos `dispatch_rejected`, `dispatch_cancelled` e `dispatch_opened`);
2. **resumo de fechamento enviado ≤ 60 s** depois do fechamento de um disparo de 30 alunos sem correção
   pendente;
3. **relatório sob demanda ≤ 2 s** de um disparo com 100 entregas e 300 tentativas, em JSON e em CSV.

Política: uma falha é rodada de novo uma vez, isolada, e registrada
como tal; duas seguidas são falha real, e mudar as metas exige rever os testes congelados. Nada aqui repete sozinho.
Cada meta tem a sua função, para que a nova rodada seja isolada.

Montagem:
- (1) e (2): a equipe pela fixture `team_members` (dois membros com endereço ativo, pelo vínculo real
  na Bot API falsa), o oferecimento e o bot dela; a `rich_bot_api` instalada antes da equipe; os alunos com
  matrícula ativa e endereço por SQL nas colunas já existentes, com o chat conhecido do dublê por um
  `my_chat_member` (como o `test_telegram_timing.py`); o worker com bots pela fixture `report_worker` (o
  caminho de produção, com o `DeliverySettings()` padrão: Rich, 1 mensagem por segundo por chat e 30 por
  segundo por bot), subido **antes** de qualquer medida. O `FakeClock` só anda com o sistema parado e em
  saltos de até 10 s (a posse do bot conferida depois de cada salto).
- (3): o worker de console (`report_worker(console=True)`), que não tem ritmo de envio: 100 alunos importados
  pela API de importação de alunos e ativados por SQL; as 300 tentativas pela `ConsoleInbox` (cada aluno: dois erros de formato
  e a resposta certa com vírgula, cada um em resposta ao pedido do campo mais recente).

Medidas (o `monotonic()` da Bot API falsa, `tests.helpers.bot_api.monotonic`, é o `perf_counter()`, o mesmo
relógio do teste):
- (1) o **fato** (rejeição, cancelamento) é o primeiro instante em que um observador do banco, numa conexão
  própria e consultando a cada `WATCH_SECONDS` (5 ms), vê o disparo no estado novo; a **última entrega** é o
  maior `started` (chegada à Bot API falsa) das mensagens de questão da abertura, anterior à passagem da
  entrega a `delivered`; o **aviso enfileirado** é o primeiro instante em que o observador vê o aviso
  `queued` (as mensagens já na fila, na mesma transação). [I] O atraso do observador (até um intervalo mais
  a consulta) encurta a medida da rejeição e do cancelamento em até alguns milissegundos e alonga a da
  abertura; a meta é conferida sem margem sobre o valor medido;
- (2) do salto do `FakeClock` além de `closes_at` (com o sistema parado logo antes) ao maior `started` dos
  resumos de fechamento aos dois membros na Bot API falsa; as esperas do protocolo determinístico do
  fechamento entram na medida;
- (3) só a chamada HTTP à API em processo (`client.get`), sem a conferência do esquema, que vem depois.

Leituras [I] deste arquivo: o cancelamento automático usado é o `expired` (o disparo agendado
não confirmado até o `confirm_deadline`, que é o `opens_at`); a primeira consulta do relatório em (3), sobre o
disparo recém-aberto sem respostas, é uma conferência de conteúdo (parcial, alcance de 100), fora da medida.

No esqueleto: (1) e (2) falham na primeira consulta a
`team_notices` (tabela ausente, migração `0005`); (3), na primeira consulta do relatório (rota ausente, 404).
Verdes só depois dos avisos e da fila e do relatório.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg

from cq_server.adapters.console import ConsoleInbox
from cq_server.bots import profile_task_handler
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.tasks import TaskQueue
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    get_report,
    import_students,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso
from tests.helpers.contract import assert_report_response
from tests.helpers.team import Team, team_messages

# --- metas de tempo (exatas, sem margem) -----------------------------------------------------

NOTICE_QUEUED = 5.0
CLOSE_TO_SUMMARY = 60.0
REPORT_ON_DEMAND = 2.0

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

ZONE = "America/Sao_Paulo"
WORKER_ID = "worker-tempos-avisos"
REPO = "https://repos.cq.test/disciplina/questoes-tempos-avisos.git"
COMMIT = "6a4c" * 10
ORIGIN = "integration"
WAIT = 60.0  # espera real por uma condição fora das medidas
MEASURE_WAIT = 120.0  # espera real pelo fim de uma medida (a meta é conferida depois, pelo valor medido)
WATCH_SECONDS = 0.005
CLOCK_STEP = 10
LEASE_MARGIN = 13
RECHECK = 6
RESPONSE_SECONDS = 30
NOTICE_STUDENTS = 3
SUMMARY_STUDENTS = 30
REPORT_STUDENTS = 100
ATTEMPTS = ("abc", "x1", "6,5")  # dois erros de formato e a resposta certa (com vírgula decimal)
USER_BASE = 7_800
PROFILE = "bot.sync_profile"


# --- montagem ----------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _S:
    i: int
    user: Any
    person_row: int

    @property
    def chat(self) -> int:
        return self.user.user_id


class _NoRouteChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota, então nada pode chegar aqui."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


@dataclasses.dataclass
class _T:
    client: Any
    sql: psycopg.Connection
    clock: Any
    rich: Any
    team: Team
    offering_row: int
    students: list[_S] = dataclasses.field(default_factory=list)

    @property
    def code(self) -> str:
        return self.team.code

    @property
    def bot(self) -> int:
        return self.team.bot_id

    @property
    def bot_row(self) -> int:
        return self.team.bot_row


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _lease_left(t: _T) -> float:
    until = t.sql.execute("SELECT lease_until FROM bots WHERE id = %s", (t.bot_row,)).fetchone()[0]
    return -1.0 if until is None else (until - t.clock.now()).total_seconds()


def _lease_ok(t: _T) -> None:
    _wait(lambda: _lease_left(t) >= LEASE_MARGIN, "a renovação da posse do bot")


def _advance(t: _T, seconds: float) -> datetime:
    left = seconds
    while left > 0:
        step = min(CLOCK_STEP, left)
        left -= step
        t.clock.advance(step)
        _lease_ok(t)
    return t.clock.now()


def _busy(sql: psycopg.Connection, clock: Any) -> list[str]:
    now = clock.now()
    busy = [f"tarefa {k}" for (k,) in sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"mensagem {m}" for (m,) in sql.execute(
        "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"update {u}" for (u,) in sql.execute(
        "SELECT id FROM telegram_updates WHERE state = 'pending'").fetchall()]
    return busy


def _settle(sql: psycopg.Connection, clock: Any) -> None:
    for _ in range(10):
        _wait(lambda: not _busy(sql, clock), "o sistema parar", timeout=MEASURE_WAIT)
        time.sleep(1.2)
        if not _busy(sql, clock):
            return
    raise AssertionError(f"o sistema não parou: {_busy(sql, clock)}")


def _student(t: _T, i: int) -> _S:
    """Uma pessoa com matrícula `student` ativa (termo aceito) e endereço de entrega ativo no bot, com o
    chat conhecido da Bot API falsa (como o `test_telegram_timing.py`)."""
    user = make_user(USER_BASE + i)
    person = register_person(t.client, ADMIN_TOKEN, name=f"Pessoa Tempos Seis {i:03d}")
    person_row = t.sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (person["pseudo_id"],)).fetchone()[0]
    now = t.clock.now()
    t.sql.execute(
        "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, terms_version, "
        "terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
        (person_row, t.offering_row, now, now, now))
    t.sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_row, t.bot_row, user.user_id, now, now))
    t.rich.push_my_chat_member(t.bot, user, old_status="kicked", new_status="member")
    return _S(i=i, user=user, person_row=person_row)


def _start(t: _T, report_worker: Any, settings: Any, count: int) -> _NoRouteChannel:
    """Os alunos, o worker com bots no ar e os `my_chat_member` processados, antes de qualquer medida (os
    updates do vínculo da equipe já estavam processados)."""
    processed = ("SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND state = 'processed'")
    before = t.sql.execute(processed, (t.bot_row,)).fetchone()[0]
    t.students = [_student(t, i) for i in range(count)]
    default_channel = _NoRouteChannel()
    report_worker(channel=default_channel, settings=dataclasses.replace(settings, worker_id=WORKER_ID))
    _wait(lambda: t.rich.pending_polls(t.bot), "o leitor do bot consultando")
    _wait(lambda: t.sql.execute(processed, (t.bot_row,)).fetchone()[0] == before + count, "os my_chat_member")
    _lease_ok(t)
    _settle(t.sql, t.clock)
    return default_channel


def _setup(rich: Any, team: Team, client: Any, sql: psycopg.Connection, clock: Any) -> _T:
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s::uuid",
                               (team.offering["offering_id"],)).fetchone()[0]
    assert sql.execute("SELECT count(*) FROM team_notices").fetchone()[0] == 0, "nenhum aviso antes das medidas"
    return _T(client=client, sql=sql, clock=clock, rich=rich, team=team, offering_row=offering_row)


def _dispatch_state(sql: psycopg.Connection, dispatch_id: str) -> str:
    return sql.execute("SELECT state FROM dispatches WHERE dispatch_uuid = %s::uuid", (dispatch_id,)).fetchone()[0]


def _question_messages(t: _T, dispatch_id: str) -> list[Any]:
    """As mensagens de questão (a principal de cada entrega) do disparo, na Bot API falsa."""
    rows = t.sql.execute(
        "SELECT o.channel_ref, d.enrollment_id FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "JOIN outbox_messages o ON o.id = d.principal_message_id WHERE x.dispatch_uuid = %s::uuid "
        "AND o.state = 'sent'", (dispatch_id,)).fetchall()
    chats = dict(t.sql.execute(
        "SELECT e.id, de.chat_id FROM enrollments e JOIN delivery_endpoints de ON de.person_id = e.person_id "
        "AND de.bot_id = %s WHERE e.offering_id = %s", (t.bot_row, t.offering_row)).fetchall())
    return [t.rich.current(t.bot, chats[enrollment], int(ref)) for ref, enrollment in rows]


# --- o observador do banco ---------------------------------------------------------------------------


class _Watcher:
    """Observa, numa conexão própria e a cada `WATCH_SECONDS`, os estados dos disparos do oferecimento e os
    avisos à equipe de cada disparo; guarda o primeiro `monotonic()` em que viu cada (disparo, estado) e cada
    (disparo, tipo do aviso, estado do aviso)."""

    _SQL = ("SELECT d.dispatch_uuid::text, d.state, n.kind, n.state FROM dispatches d "
            "LEFT JOIN team_notices n ON n.dispatch_id = d.id WHERE d.offering_id = %s")

    def __init__(self, conninfo: str, offering_row: int) -> None:
        self._conninfo = conninfo
        self._offering_row = offering_row
        self._seen: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="cq-test-observador-avisos", daemon=True)

    def __enter__(self) -> _Watcher:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(10)
        assert not self._thread.is_alive(), "o observador não parou"

    def _run(self) -> None:
        try:
            with psycopg.connect(self._conninfo, autocommit=True) as conn:
                while not self._stop.is_set():
                    rows = conn.execute(self._SQL, (self._offering_row,)).fetchall()
                    now = monotonic()
                    with self._lock:
                        for dispatch_id, state, kind, notice_state in rows:
                            self._seen.setdefault(("dispatch", dispatch_id, state), now)
                            if kind is not None:
                                self._seen.setdefault(("notice", dispatch_id, kind, notice_state), now)
                    time.sleep(WATCH_SECONDS)
        except BaseException as exc:  # noqa: BLE001 - guardada para o teste conferir
            self.error = exc

    def first(self, *key: str) -> float | None:
        if self.error is not None:
            raise AssertionError(f"o observador falhou: {type(self.error).__name__}: {self.error}")
        with self._lock:
            return self._seen.get(key)

    def wait(self, *key: str) -> float:
        return _wait(lambda: self.first(*key), f"o observador ver {key}", timeout=MEASURE_WAIT)


# --- (1) aviso enfileirado ≤ 5 s ----------------------------------------------------------------------


def test_notices_are_queued_within_five_seconds_of_rejection_cancellation_and_last_delivery(
        rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        engine_fake: Any, repo_fetcher: Any, report_worker: Any) -> None:
    """Meta (1): com o worker no ar, da rejeição, do cancelamento automático (`expired`) e da última
    entrega da abertura até o aviso enfileirado (`dispatch_rejected`, `dispatch_cancelled`,
    `dispatch_opened`), ≤ 5 s cada, medidos pelo observador do banco e pelo `monotonic()` da Bot API falsa,
    sem margem; os avisos chegam aos dois membros (controle contra o vazio)."""
    t = _setup(rich_bot_api, team_members, client, sql, clock)
    default_channel = _start(t, report_worker, settings, NOTICE_STUDENTS)
    engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    elapsed: dict[str, float] = {}
    with _Watcher(settings.database_url, t.offering_row) as watcher:
        # Rejeição: a questão não suportada é rejeitada na validação.
        t_prep = _advance(t, 10)
        rejected = prepare_dispatch(client, ADMIN_TOKEN, t.code, repo=REPO, commit=COMMIT, qid="unsupported",
                                    closes_at=iso(t_prep + timedelta(minutes=10)), origin=ORIGIN)["dispatch_id"]
        fact = watcher.wait("dispatch", rejected, "rejected")
        queued = watcher.wait("notice", rejected, "dispatch_rejected", "queued")
        elapsed["rejeição"] = queued - fact
        _settle(sql, clock)

        # Cancelamento automático: o agendado sem confirmação até o `opens_at` (o `confirm_deadline`).
        opens = t.clock.now() + timedelta(minutes=3)
        expired = prepare_dispatch(client, ADMIN_TOKEN, t.code, repo=REPO, commit=COMMIT, qid="single_choice",
                                   opens_at=iso(opens), closes_at=iso(opens + timedelta(minutes=6)),
                                   origin=ORIGIN)["dispatch_id"]
        watcher.wait("dispatch", expired, "scheduled")
        _settle(sql, clock)
        _advance(t, (opens - t.clock.now()).total_seconds() + 1)
        fact = watcher.wait("dispatch", expired, "cancelled")
        queued = watcher.wait("notice", expired, "dispatch_cancelled", "queued")
        elapsed["cancelamento"] = queued - fact
        _settle(sql, clock)
        assert get_dispatch(client, ADMIN_TOKEN, t.code, expired)["cancel_reason"] == "expired"

        # Abertura: da última entrega (o maior `started` das questões) ao aviso "aberto" enfileirado.
        t_prep = _advance(t, 10)
        opened = prepare_dispatch(client, ADMIN_TOKEN, t.code, repo=REPO, commit=COMMIT, qid="number",
                                  closes_at=iso(t_prep + timedelta(minutes=10)), origin=ORIGIN)["dispatch_id"]
        watcher.wait("dispatch", opened, "scheduled")
        _settle(sql, clock)
        _advance(t, 10)
        assert confirm_dispatch(client, ADMIN_TOKEN, t.code, opened)["confirmed"] is True
        queued = watcher.wait("notice", opened, "dispatch_opened", "queued")
        questions = _wait(lambda: (found := _question_messages(t, opened)) and len(found) == NOTICE_STUDENTS
                          and found, "as questões da abertura")
        elapsed["abertura"] = queued - max(q.started for q in questions)
        _settle(sql, clock)
    for kind in ("dispatch_rejected", "dispatch_cancelled", "dispatch_opened"):
        for member in t.team.active:
            messages = team_messages(sql, kind, member)
            assert [(m.state, m.priority) for m in messages] == [("sent", 2)], (kind, member.label, messages)
    assert default_channel.calls == []
    for what, seconds in elapsed.items():
        assert seconds <= NOTICE_QUEUED, (
            f"meta de tempo: {what} → aviso enfileirado em {seconds:.3f} s (meta {NOTICE_QUEUED} s)")
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []


# --- (2) resumo de fechamento de 30 alunos ≤ 60 s -------------------------------------------------------


def test_closing_summary_of_thirty_students_is_sent_within_sixty_seconds(
        rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        engine_fake: Any, repo_fetcher: Any, report_worker: Any) -> None:
    """Meta (2): os 30 alunos respondem no prazo (sem correção pendente no fechamento); do salto do
    `FakeClock` além de `closes_at` ao maior `started` dos resumos de fechamento aos dois membros na Bot API
    falsa, ≤ 60 s, sem margem; o resumo traz as 30 respostas (controle contra o vazio)."""
    t = _setup(rich_bot_api, team_members, client, sql, clock)
    default_channel = _start(t, report_worker, settings, SUMMARY_STUDENTS)
    engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    t_prep = _advance(t, 10)
    closes = t_prep + timedelta(minutes=6)
    dispatch_id = prepare_dispatch(client, ADMIN_TOKEN, t.code, repo=REPO, commit=COMMIT, qid="number",
                                   closes_at=iso(closes), origin=ORIGIN)["dispatch_id"]
    _wait(lambda: _dispatch_state(sql, dispatch_id) == "scheduled", "a validação")
    _settle(sql, clock)
    _advance(t, 10)
    confirm_dispatch(client, ADMIN_TOKEN, t.code, dispatch_id)
    _wait(lambda: len(_question_messages(t, dispatch_id)) == SUMMARY_STUDENTS, "as trinta questões",
          timeout=MEASURE_WAIT)
    _settle(sql, clock)
    _advance(t, RESPONSE_SECONDS)
    prompts = {}
    for s in t.students:
        row = _wait(lambda s=s: sql.execute(
            "SELECT o.channel_ref FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
            "JOIN dispatches x ON x.id = d.dispatch_id JOIN enrollments e ON e.id = d.enrollment_id "
            "WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s AND o.delivery_role = 'field_prompt' "
            "AND o.state = 'sent'", (dispatch_id, s.person_row)).fetchone(), f"o pedido do aluno {s.i}")
        prompts[s.i] = int(row[0])
    for s in t.students:
        assert t.rich.push_text(t.bot, s.user, "6,5", reply_to_message_id=prompts[s.i]) is not None
    _wait(lambda: sql.execute(
        "SELECT count(*) FROM submissions su JOIN deliveries d ON d.id = su.delivery_id JOIN dispatches x "
        "ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid AND su.outcome = 'graded'",
        (dispatch_id,)).fetchone()[0] == SUMMARY_STUDENTS, "as trinta correções", timeout=MEASURE_WAIT)
    _settle(sql, clock)
    assert get_dispatch(client, ADMIN_TOKEN, t.code, dispatch_id)["deliveries"]["answered"] == SUMMARY_STUDENTS

    # A medida: o salto além de `closes_at`, com o sistema parado logo antes.
    _advance(t, (closes - t.clock.now()).total_seconds() - 1)
    _settle(sql, clock)
    t.clock.advance(2)
    trigger = monotonic()
    _lease_ok(t)
    deadline = time.monotonic() + MEASURE_WAIT
    while _dispatch_state(sql, dispatch_id) != "closed":
        window = time.monotonic() + 2.5
        while time.monotonic() < window and _dispatch_state(sql, dispatch_id) != "closed":
            time.sleep(0.05)
        if _dispatch_state(sql, dispatch_id) == "closed":
            break
        assert time.monotonic() < deadline, f"o disparo não fechou em {MEASURE_WAIT} s"
        _advance(t, RECHECK)

    def summaries() -> list[Any] | None:
        sent = []
        for member in t.team.active:
            found = [m for m in team_messages(sql, "dispatch_closed", member) if m.state == "sent"]
            if len(found) != 1 or member.chat_id is None:
                return None
            sent.append(t.rich.current(t.bot, member.chat_id, int(found[0].channel_ref or "0")))
        return sent

    sent = _wait(summaries, "os resumos de fechamento enviados", timeout=MEASURE_WAIT)
    elapsed = max(m.started for m in sent) - trigger
    for message in sent:
        text = message.plain_text or ""
        assert f"Respostas: {SUMMARY_STUDENTS} no prazo · {SUMMARY_STUDENTS} com atrasadas" in text, text
        assert (f"Alcance: {SUMMARY_STUDENTS} alunos-alvo, {SUMMARY_STUDENTS} entregues, 0 não entregues, "
                "0 em falha de questão") in text, text
    assert default_channel.calls == []
    assert elapsed <= CLOSE_TO_SUMMARY, (
        f"meta de tempo: fechamento → resumo enviado em {elapsed:.3f} s (meta {CLOSE_TO_SUMMARY} s)")
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []


# --- (3) relatório sob demanda de 100 entregas e 300 tentativas ≤ 2 s ------------------------------------


def _process_profile_tasks(db_url: str, clock: Any, settings: Any, bots: Any) -> None:
    """A `bot.sync_profile` do registro processada antes do worker de console (que não tem esse tratador)."""
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    handler = profile_task_handler(db_url, clock=clock, runtime=bots, settings=settings)
    try:
        while queue.work_once("preparacao-tempos-avisos", {PROFILE: handler}) is not None:
            pass
    finally:
        close = getattr(handler, "close", None)
        if close is not None:
            close()
        queue.close()


def test_on_demand_report_of_hundred_deliveries_and_three_hundred_attempts_within_two_seconds(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, db_url: str, bot_api: Any, bots: Any,
        engine_fake: Any, repo_fetcher: Any, report_worker: Any) -> None:
    """Meta (3): com 100 entregas e 300 tentativas gravadas (cada aluno: dois erros de formato e a
    resposta certa), o relatório sob demanda (cálculo no momento) sai em ≤ 2 s, em JSON e em CSV, medido só
    na chamada HTTP; o conteúdo confere as 100 respostas, os 200 erros de formato e as 100 vírgulas
    (controle contra o vazio)."""
    code = "tempos-relatorio-avisos"
    offering = create_offering(client, ADMIN_TOKEN, code=code, timezone=ZONE)
    tg_bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, code)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    _process_profile_tasks(db_url, clock, settings, bots)
    rows = [{"name": f"Pessoa Relatorio Tempos {i:03d}", "ra": f"5980{i:06d}",
             "email": f"relatorio.tempos{i:03d}@turma.example.org"} for i in range(REPORT_STUDENTS)]
    imported = import_students(client, ADMIN_TOKEN, code, rows)
    assert imported["enrollments_created"] == REPORT_STUDENTS
    now = clock.now()
    persons = [r for (r,) in sql.execute(
        "SELECT person_id FROM enrollments WHERE offering_id = %s AND role = 'student' ORDER BY id",
        (offering_row,)).fetchall()]
    for index, person_id in enumerate(persons):
        sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                    "terms_accepted_at = %s WHERE person_id = %s AND offering_id = %s", (now, now, person_id,
                                                                                         offering_row))
        sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                    "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                    (person_id, bot_row, 5_980_000_000 + index, now, now))
    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        report_worker(console=True, settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        engine_fake.program_presets()
        repo_fetcher.add(REPO, COMMIT)
        t_prep = clock.advance(60)
        dispatch_id = prepare_dispatch(client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid="number",
                                       closes_at=iso(t_prep + timedelta(minutes=30)), origin=ORIGIN)["dispatch_id"]
        _wait(lambda: _dispatch_state(sql, dispatch_id) == "scheduled", "a validação")
        _settle(sql, clock)
        clock.advance(60)
        confirm_dispatch(client, ADMIN_TOKEN, code, dispatch_id)
        _wait(lambda: sql.execute(
            "SELECT count(*) FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
            "WHERE x.dispatch_uuid = %s::uuid AND d.state = 'delivered'", (dispatch_id,)).fetchone()[0]
            == REPORT_STUDENTS, "as cem entregas", timeout=MEASURE_WAIT)
        _settle(sql, clock)
        # Conferência de conteúdo, fora da medida: o disparo aberto, sem respostas, sai parcial.
        empty = get_report(client, code, dispatch_id)
        assert (empty["dispatch"]["state"], empty["dispatch"]["partial"], empty["reach"]["target"],
                empty["reach"]["delivered"], empty["performance"]["answered"], empty["response_time"]["observations"]
                ) == ("open", True, REPORT_STUDENTS, REPORT_STUDENTS, {"on_time": 0, "with_late": 0}, 0)

        # As 300 tentativas, em três rodadas, cada uma em resposta ao pedido do campo mais recente.
        clock.advance(RESPONSE_SECONDS)
        replied: dict[int, int] = {}
        for round_number, text in enumerate(ATTEMPTS, start=1):
            prompts = _wait(lambda: (found := _attempt_prompts(sql, dispatch_id))
                            and len(found) == REPORT_STUDENTS
                            and all(found[d][0] > replied.get(d, 0) for d in found) and found,
                            f"os pedidos da rodada {round_number}", timeout=MEASURE_WAIT)
            for delivery_id, (message_id, chat_id) in prompts.items():
                inbox.send_text(bot_row, chat_id, text, reply_to=str(message_id))
                replied[delivery_id] = message_id
            _wait(lambda n=round_number: _rounds_done(sql, dispatch_id, n), f"a rodada {round_number}",
                  timeout=MEASURE_WAIT)
        _settle(sql, clock)
        assert sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == len(ATTEMPTS) * REPORT_STUDENTS

        path = f"/v1/offerings/{code}/dispatches/{dispatch_id}/report"
        started = time.perf_counter()
        resp = client.get(path, headers=auth(ADMIN_TOKEN))
        json_seconds = time.perf_counter() - started
        started = time.perf_counter()
        csv_resp = client.get(path, params={"format": "csv"}, headers=auth(ADMIN_TOKEN))
        csv_seconds = time.perf_counter() - started
    finally:
        report_worker.stop()
        inbox.close()
    body = assert_report_response(resp)
    assert isinstance(body, dict), "o relatório em JSON"
    full = {"on_time": REPORT_STUDENTS, "with_late": REPORT_STUDENTS}
    assert (body["reach"]["target"], body["reach"]["delivered"]) == (REPORT_STUDENTS, REPORT_STUDENTS)
    assert (body["performance"]["answered"], body["performance"]["correct"]) == (full, full)
    assert body["response_time"] == {"observations": REPORT_STUDENTS, "negative": 0,
                                     "median_ms": RESPONSE_SECONDS * 1000, "q1_ms": RESPONSE_SECONDS * 1000,
                                     "q3_ms": RESPONSE_SECONDS * 1000}
    assert body["friction"] == {
        "fields": [{"index": 0, "format_errors": {"on_time": 2 * REPORT_STUDENTS, "with_late": 2 * REPORT_STUDENTS},
                    "decimal_comma": full}],
        "unmapped_format_errors": {"on_time": 0, "with_late": 0}}
    field = body["distribution"]["fields"][0]
    assert (field["values"], field["ratios"], field["unrated"]) == (
        {"items": [{"value": "6.5", "count": full}], "others": {"on_time": 0, "with_late": 0}},
        {"items": [{"value": "1", "count": full}], "others": {"on_time": 0, "with_late": 0}},
        {"on_time": 0, "with_late": 0})
    csv_text = assert_report_response(csv_resp)
    assert isinstance(csv_text, str) and csv_text.startswith("﻿")
    assert json_seconds <= REPORT_ON_DEMAND, (
        f"meta de tempo: relatório em JSON em {json_seconds:.3f} s (meta {REPORT_ON_DEMAND} s)")
    assert csv_seconds <= REPORT_ON_DEMAND, (
        f"meta de tempo: relatório em CSV em {csv_seconds:.3f} s (meta {REPORT_ON_DEMAND} s)")


def _attempt_prompts(sql: psycopg.Connection, dispatch_id: str) -> dict[int, tuple[int, int]]:
    """Por entrega do disparo: (o id do pedido do campo enviado mais recente, o chat do aluno)."""
    rows = sql.execute(
        "SELECT d.id, max(o.id), de.chat_id FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "JOIN enrollments e ON e.id = d.enrollment_id JOIN bots b ON b.offering_id = x.offering_id "
        "JOIN delivery_endpoints de ON de.person_id = e.person_id AND de.bot_id = b.id AND de.state = 'active' "
        "JOIN outbox_messages o ON o.delivery_id = d.id AND o.delivery_role = 'field_prompt' AND o.state = 'sent' "
        "WHERE x.dispatch_uuid = %s::uuid GROUP BY d.id, de.chat_id", (dispatch_id,)).fetchall()
    return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}


def _rounds_done(sql: psycopg.Connection, dispatch_id: str, attempts: int) -> bool:
    """Toda entrega com `attempts` submissões corrigidas e nenhum update pendente."""
    counts = [n for (n,) in sql.execute(
        "SELECT count(s.id) FILTER (WHERE s.outcome = 'graded') FROM deliveries d JOIN dispatches x "
        "ON x.id = d.dispatch_id LEFT JOIN submissions s ON s.delivery_id = d.id WHERE x.dispatch_uuid = %s::uuid "
        "GROUP BY d.id", (dispatch_id,)).fetchall()]
    pending = sql.execute("SELECT count(*) FROM telegram_updates WHERE state = 'pending'").fetchone()[0]
    return len(counts) == REPORT_STUDENTS and set(counts) == {attempts} and pending == 0

