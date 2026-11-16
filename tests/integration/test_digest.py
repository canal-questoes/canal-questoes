"""Resumo diário opcional à equipe.



O que se confere:
- **desligado por padrão**: sem linha em `offering_digests`, nada é agendado nem enviado, inclusive com
  atividade no dia; desligado (linha com `enabled = false`), o mesmo;
- **ligado às 20:00 no fuso**: um único aviso `daily_digest` por data local
  (`digest:<AAAA-MM-DD>`), entregue pelo `teamnotices.deliver` a cada membro da equipe com endereço ativo
  (prioridade 2), com a adesão (os 7 estados do vínculo do aluno, conferidos contra o `students.summary` no mesmo
  instante) e os vínculos novos no dia, cada disparo aberto ou fechado no dia com respostas, corretas,
  parcialmente corretas e "não sei" em par (`DIGEST_DISPATCH_LINE`) e as atrasadas recebidas no dia (pelo
  `received_at`); o `notice.recorded` com o assunto (`local_date`); o `next_due_at` avançado para a
  ocorrência seguinte; a fotografia em `team_notices.data` sem texto livre (sem o título); e a linha de um
  disparo de escolha múltipla com notas parciais (no prazo e atrasada) e "Não sei" no prazo e atrasado,
  com cada lado do par pela marca de atraso da submissão (parcialmente corretas 1/2, "não sei" 1/2);
- **dia sem atividade** → `no_activity` com evento, sem envio; o vencimento de um convite não conta;

- **cada gatilho de atividade isolado**, com um único fato no dia local: convite enviado,
  envio com falha, resposta atrasada de um disparo fechado noutro dia e disparo aberto no dia que ainda não
  fechou → o resumo registrado e entregue;
- **worker fora no horário**: de volta no mesmo dia → sai uma vez; no dia seguinte →
  `missed` com evento; vários dias fora → um `missed` (a data devida) e o salto para a próxima ocorrência;
- **rascunho e arquivado** → nada; ligado em rascunho e o oferecimento ativado depois → só avança a data
  anterior à ativação e envia a partir dela; **encerrado** com atividade → envia;
- **operações**: `offerings.get_digest` sem linha em qualquer estado;
  `offerings.update_digest` com as entradas inválidas do contrato, igual à configuração → 200 sem
  evento nem escrita, com mudança → `offering.digest_changed`; ligar depois do horário de hoje → amanhã;
  aceito em rascunho e encerrado; arquivado → `invalid_state`;
- **no máximo um resumo por data local**: na reexecução do tratador para a mesma
  data, e no reagendamento depois do resumo do dia já enviado (mudar o horário para mais tarde; desligar e
  religar), o `record` devolve `None`: nenhum segundo `daily_digest` nem `no_activity`, nenhuma mensagem
  nova, e o `next_due_at` avança para a data seguinte;
- **reanimação pela varredura**: o resumo
  devido sem tarefa viva (a `digest.daily:<id>:r1` terminada em falha antes de registrar o aviso) é
  reanimado uma única vez (`r2`) depois do `retry_gap`, com um único aviso, um envio por membro e um
  evento;
- **resumo longo**: em várias partes, na ordem, cada parte com linhas inteiras e sob o limite de
  4096, com todos os números e todos os disparos; o worker com o ritmo rápido por chat, e as
  partes identificadas pelas mensagens do próprio aviso (`channel_ref`), sem
  outra mensagem do chat com o marcador `• ` ou o cabeçalho.

**Montagem.** A equipe simulada (fixture `team_members`: oferecimento `active` com bot próprio na Bot API
falsa, fuso `America/Sao_Paulo`, disciplina "Algoritmos e Programação", 2 membros com endereço ativo
vinculados pelo link de equipe), o worker completo (fixture `report_worker`, com a extensão da Bot API falsa para as questões,
o motor falso e a obtenção falsa). Os alunos entram por SQL nas colunas do oferecimento e do vínculo (matrícula ativa com o
termo aceito e endereço ativo, como `test_dispatch_opening.py`), os convites por SQL nas colunas do vínculo
(`invite_sends`, `invites`), e os disparos e as respostas pelo caminho real (API, worker, toques na
Bot API falsa). Nos testes do tratador, o resumo é ligado **por SQL** (`offering_digests`, com o
`next_due_at` calculado à mão), para não depender das rotas; os testes das operações usam a API.

**Relógio** (`FakeClock`): começa em 2031-01-01T00:00Z, que é 2030-12-31 21:00 em São Paulo (UTC−3, sem
horário de verão); o horário padrão (20:00 locais) de 2031-01-01 é 23:00Z. Os horários esperados são
literais calculados à mão.

**Leituras [I] deste arquivo** (o conteúdo é fixo, a grafia não): a data do
`DAILY_DIGEST` sai como `AAAA-MM-DD` (a do `subject_key` e do `local_date`); as linhas do resumo são as do
modelo, na ordem, seguidas de uma `DIGEST_DISPATCH_LINE` por disparo, na ordem de criação do disparo; o
texto é conferido linha a linha sobre o texto lido pela Bot API falsa (sem as linhas vazias).

**Vermelho no esqueleto:** a tabela `offering_digests` (e `team_notices`) não existe (migração `0005`
ausente) e as rotas `GET`/`PATCH /v1/offerings/{ref}/digest` não existem (404): cada teste falha na primeira
leitura ou escrita do resumo (UndefinedTable) ou na primeira chamada às operações (404), nunca na montagem.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import time as time_module
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import noticetexts
from cq_server.deliverytexts import DONT_KNOW_BUTTON, SUBMIT_BUTTON
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, auth, confirm_dispatch, create_offering,
                               get_offering_digest, prepare_dispatch, register_bot, register_person,
                               students_summary, update_offering_digest)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error
from tests.helpers.engine import FakeQuestion, preset, single_choice_field
from tests.helpers.team import team_messages

UTC = timezone.utc
TZ = "America/Sao_Paulo"
COURSE = "Algoritmos e Programação"
REPO = "https://repos.cq.test/disciplina/resumo-diario.git"
COMMIT = "5d16" * 10
ADMIN = auth(ADMIN_TOKEN)
ADMIN_ACTOR = "bootstrap"
# O primeiro dia local inteiro do relógio falso (2031-01-01T00:00Z = 2030-12-31 21:00 em São Paulo).
DAY = date(2031, 1, 1)
DIGEST_KIND = "daily_digest"
DIGEST_TASK = "digest.daily"
# Tempo real máximo de espera por um efeito do worker; e a espera em que "nada acontece" (a varredura roda a
# cada 1 s real).
WAIT = 25.0
QUIET = 3.0
# O limite de markup de uma mensagem à equipe.
TEXT_LIMIT = 4096

_users = itertools.count(1)
_codes = itertools.count(1)


def _local(day: date, hh: int, mm: int = 0) -> datetime:
    """O instante (UTC) de `hh:mm` locais em São Paulo no dia `day` (2031: UTC−3, sem horário de verão)."""
    return datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo(TZ)).astimezone(UTC)


# Conferência dos literais (à mão: São Paulo em 2031 é UTC−3).
assert _local(DAY, 20) == datetime(2031, 1, 1, 23, 0, tzinfo=UTC)
assert _local(DAY, 9) == datetime(2031, 1, 1, 12, 0, tzinfo=UTC)


def _pair(on_time: int, with_late: int) -> str:
    """O par do texto fixo, escrito à mão (não pela função do núcleo)."""
    return f"{on_time} no prazo · {with_late} com atrasadas"


def _dispatch_line(title: str, answered: tuple[int, int], correct: tuple[int, int],
                   partially: tuple[int, int], dont_know: tuple[int, int]) -> str:
    return noticetexts.DIGEST_DISPATCH_LINE.format(title=title, answered=_pair(*answered), correct=_pair(*correct),
                                                   partially_correct=_pair(*partially),
                                                   dont_know=_pair(*dont_know))


def _lines(text: str | None) -> list[str]:
    return [line for line in (text or "").split("\n") if line.strip()]


@dataclasses.dataclass(frozen=True)
class Student:
    pseudo_id: str
    person_row: int
    enrollment_row: int
    user: Any

    @property
    def chat(self) -> int:
        return self.user.user_id


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int
    closes_at: datetime


class World:
    """A equipe simulada, o worker e as consultas do resumo diário (ver o docstring do módulo)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, rich: Any, engine_fake: Any,
                 team: Any, report_worker: Any, bot_worker_thread: Any) -> None:
        self.client, self.sql, self.clock, self.rich, self.engine_fake = client, sql, clock, rich, engine_fake
        self.team = team
        self.offering = team.offering
        self.code = team.code
        self.bot = team.bot_id
        self.bot_row = team.bot_row
        self.offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                        (self.offering["offering_id"],)).fetchone()[0]
        self._start_worker = report_worker
        self._bot_worker_thread = bot_worker_thread
        self.worker: Any = None

    # -- worker --------------------------------------------------------------------------------

    def start(self, **kwargs: Any) -> None:
        """Sobe o worker; os `kwargs` vão à fixture `report_worker` (ex.: `delivery=`)."""
        assert self.worker is None, "já há um worker do teste no ar"
        self.worker = self._start_worker(stream=self.team.stream, **kwargs)

    def stop(self) -> None:
        assert self.worker is not None
        elapsed = self._bot_worker_thread.stop_one(self.worker.thread)
        assert elapsed != float("inf"), "o worker não parou em 10 s"
        self.worker = None

    def wait(self, predicate: Callable[[], Any], timeout: float = WAIT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def advance_until(self, predicate: Callable[[], Any], *, step: float = 15.0, tries: int = 40) -> Any:
        """Espera `predicate()`; sem ele em 1,5 s reais, avança o relógio `step` s (menos que a posse de 30 s)."""
        for _ in range(tries):
            deadline = time_module.monotonic() + 1.5
            while time_module.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time_module.sleep(0.05)
            self.clock.advance(step)
        value = predicate()
        assert value, f"condição não satisfeita depois de {tries} avanços de {step} s"
        return value

    # -- resumo diário (SQL) -------------------------------------------------------------------

    def enable_sql(self, *, next_due_at: datetime, local_time: str = "20:00", enabled: bool = True,
                   offering_row: int | None = None) -> None:
        """Liga (ou grava desligado) o resumo por SQL, com o `next_due_at` dado."""
        self.sql.execute(
            "INSERT INTO offering_digests (offering_id, enabled, local_time, next_due_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (offering_row or self.offering_row, enabled, local_time, next_due_at if enabled else None,
             self.clock.now()))

    def config(self, offering_row: int | None = None) -> tuple | None:
        row = self.sql.execute("SELECT enabled, local_time, next_due_at FROM offering_digests WHERE offering_id = %s",
                               (offering_row or self.offering_row,)).fetchone()
        return None if row is None else tuple(row)

    def notices(self, offering_row: int | None = None) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, kind, subject_key, state, recipient_count, local_date, dispatch_id, report_id, data, "
                "created_at, finished_at FROM team_notices WHERE offering_id = %s AND kind = %s ORDER BY id",
                (offering_row or self.offering_row, DIGEST_KIND)).fetchall()

    def digest_tasks(self) -> list[tuple]:
        return [tuple(r) for r in self.sql.execute(
            "SELECT id, state FROM tasks WHERE task_type = %s ORDER BY id", (DIGEST_TASK,)).fetchall()]

    def messages(self, notice_id: int | None = None) -> list[Any]:
        found = team_messages(self.sql, DIGEST_KIND)
        return [m for m in found if notice_id is None or m.notice_id == notice_id]

    def wait_notice(self, local_date: date, state: str, offering_row: int | None = None) -> dict:
        def ready() -> dict | None:
            rows = [n for n in self.notices(offering_row) if n["local_date"] == local_date]
            return rows[0] if rows and rows[0]["state"] == state else None
        return self.wait(ready)

    def wait_sent(self, notice_id: int, count: int) -> list[Any]:
        """As mensagens do aviso, quando há `count` e todas `sent`."""
        return self.wait(lambda: (found := self.messages(notice_id)) and len(found) == count
                         and all(m.state == "sent" for m in found) and found)

    def digest_texts(self, member: Any) -> list[list[str]]:
        """As linhas de cada mensagem do resumo diário lidas pela Bot API falsa no chat do membro."""
        return [_lines(m.plain_text) for m in self.rich.messages(self.bot, member.chat_id)
                if m.plain_text and m.plain_text.startswith("Resumo do dia")]

    def notice_events(self) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "seq"} for e in all_events(self.client, ADMIN_TOKEN, "notice.recorded")
                if e["data"]["offering_id"] == self.offering["offering_id"] and e["data"]["notice_type"] == DIGEST_KIND]

    def recorded(self, local_date: date, state: str, recipient_count: int, at: datetime) -> dict:
        """O `notice.recorded` exato de um resumo diário."""
        return {"type": "notice.recorded", "occurred_at": iso(at), "actor_token_id": None,
                "data": {"offering_id": self.offering["offering_id"], "notice_type": DIGEST_KIND,
                         "dispatch_id": None, "report_version": None, "episode_seq": None, "batch_id": None,
                         "window_started_at": None, "local_date": local_date.isoformat(), "state": state,
                         "recipient_count": recipient_count}}

    # -- alunos e convites (SQL, colunas do oferecimento e do vínculo) --------------------------

    def student(self, *, linked: bool = True) -> Student:
        """Um aluno com matrícula (`active` com o termo aceito, ativada agora, e endereço ativo no bot; ou
        `pending`, sem endereço)."""
        n = next(_users)
        user = make_user(60_000 + n)
        person = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Resumo {n}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (person["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        if linked:
            enrollment_row = self.sql.execute(
                "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
                "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) RETURNING id",
                (person_row, self.offering_row, now, now, now)).fetchone()[0]
            self.sql.execute(
                "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
                "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_row, self.bot_row, user.user_id, now, now))
            self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        else:
            enrollment_row = self.sql.execute(
                "INSERT INTO enrollments (person_id, offering_id, role, state, created_at) "
                "VALUES (%s, %s, 'student', 'pending', %s) RETURNING id",
                (person_row, self.offering_row, now)).fetchone()[0]
        return Student(person["pseudo_id"], person_row, enrollment_row, user)

    def invite_send(self, student: Student, *, sent: bool, valid_hours: int = 24,
                    created_at: datetime | None = None) -> int:
        """Um envio de convite da coordenação terminado agora (`sent`, com o convite pendente, ou `failed`)."""
        now = self.clock.now()
        start = created_at or now
        if sent:
            send_row = self.sql.execute(
                "INSERT INTO invite_sends (enrollment_id, origin, valid_hours, state, attempts, created_at, "
                "finished_at) VALUES (%s, 'coordination', %s, 'sent', 1, %s, %s) RETURNING id",
                (student.enrollment_row, valid_hours, start, now)).fetchone()[0]
            self.sql.execute(
                "INSERT INTO invites (enrollment_id, code_sha256, state, expires_at, created_at, invite_send_id) "
                "VALUES (%s, %s, 'pending', %s, %s, %s)",
                (student.enrollment_row, uuid.uuid4().hex * 2, now + timedelta(hours=valid_hours), now, send_row))
        else:
            send_row = self.sql.execute(
                "INSERT INTO invite_sends (enrollment_id, origin, valid_hours, state, failure, error_type, attempts, "
                "created_at, finished_at) VALUES (%s, 'coordination', %s, 'failed', 'recipient_refused', "
                "'SMTPRecipientsRefused', 1, %s, %s) RETURNING id",
                (student.enrollment_row, valid_hours, start, now)).fetchone()[0]
        return int(send_row)

    # -- disparos e respostas (caminho real) ---------------------------------------------------

    def program(self, qid: str, question: FakeQuestion, commit: str = COMMIT) -> None:
        self.engine_fake.program((REPO, commit, qid), question)

    def prepare(self, qid: str, closes_at: datetime, commit: str = COMMIT) -> Disp:
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=commit, qid=qid,
                                closes_at=iso(closes_at))
        row = self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                               (body["dispatch_id"],)).fetchone()[0]
        return Disp(body["dispatch_id"], int(row), closes_at)

    def state(self, d: Disp) -> str:
        return self.sql.execute("SELECT state FROM dispatches WHERE id = %s", (d.row,)).fetchone()[0]

    def wait_state(self, d: Disp, state: str) -> None:
        def ready() -> bool:
            current = self.state(d)
            assert current in ("validating", "scheduled", "open", "closed"), f"disparo em {current}"
            return current == state
        self.wait(ready)

    def deliveries(self, d: Disp) -> list[tuple]:
        return [tuple(r) for r in self.sql.execute(
            "SELECT enrollment_id, state FROM deliveries WHERE dispatch_id = %s ORDER BY id", (d.row,)).fetchall()]

    def open(self, qid: str, question: FakeQuestion, students: list[Student], closes_at: datetime,
             commit: str = COMMIT) -> Disp:
        """Prepara um imediato com `closes_at`, espera a validação, confirma e espera as entregas `delivered`.
        O `qid` é o de um preset do motor falso (a árvore `course` da obtenção falsa tem uma questão por
        preset); o comportamento é o `question` programado para `(REPO, commit, qid)`."""
        self.program(qid, question, commit)
        d = self.prepare(qid, closes_at, commit)
        self.wait_state(d, "scheduled")
        confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)
        self.wait_state(d, "open")
        self.wait(lambda: len(self.deliveries(d)) == len(students)
                  and all(state == "delivered" for _, state in self.deliveries(d)))
        return d

    def close(self, d: Disp) -> None:
        self.clock.set(d.closes_at + timedelta(seconds=1))
        self.advance_until(lambda: self.state(d) == "closed", step=15.0)

    def question(self, student: Student) -> int:
        found = [m.message_id for m in self.rich.messages(self.bot, student.chat) if m.keyboard is not None]
        assert found, "o aluno não recebeu a questão"
        return found[-1]

    def processed(self, update_id: int) -> dict:
        def ready() -> dict | None:
            with self.sql.cursor(row_factory=dict_row) as cur:
                row = cur.execute("SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                                  "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (self.bot, update_id)).fetchone()
            return row if row is not None and row["state"] == "processed" else None
        return self.wait(ready)

    def tap(self, student: Student, label: str) -> str:
        mid = self.question(student)
        current = self.rich.current(self.bot, student.chat, mid)
        found = [b.data for b in current.buttons() if b.label == label]
        assert len(found) == 1, f"botão {label!r} ausente: {[b.label for b in current.buttons()]}"
        tap = self.rich.tap(self.bot, student.user, mid, found[0])
        return self.processed(tap.update_id)["outcome"]

    def answer(self, student: Student, key: str) -> None:
        """Marca a alternativa `key` da escolha única e toca no "Enviar"."""
        assert self.tap(student, f"({key})") == "answer_toggled"
        assert self.tap(student, SUBMIT_BUTTON) in ("answer_recorded",)

    def mark_and_submit(self, student: Student, keys: str) -> None:
        """Marca as alternativas `keys` da escolha múltipla (`⬜ (x)`) e toca no "Enviar" (as alternâncias
        não mudam a versão dos controles; a seleção fica guardada na entrega)."""
        for key in keys:
            assert self.tap(student, f"⬜ ({key})") == "answer_toggled"
        assert self.tap(student, SUBMIT_BUTTON) == "answer_recorded"

    def dont_know(self, student: Student) -> None:
        assert self.tap(student, DONT_KNOW_BUTTON) == "answer_dont_know"

    def wait_final(self, d: Disp, count: int) -> None:
        """Espera `count` submissões finais do disparo (`graded` ou `dont_know`), nenhuma em correção."""
        def ready() -> bool:
            rows = self.sql.execute(
                "SELECT s.outcome FROM submissions s JOIN deliveries d ON d.id = s.delivery_id "
                "WHERE d.dispatch_id = %s", (d.row,)).fetchall()
            return len(rows) == count and all(r[0] in ("graded", "dont_know") for r in rows)
        self.wait(ready)

    def wait_no_answer(self, d: Disp, student: Student) -> None:
        """A entrega do aluno chegou a `no_answer` e a edição do fechamento já saiu (nada na fila)."""
        def ready() -> bool:
            state = self.sql.execute("SELECT state FROM deliveries WHERE dispatch_id = %s AND enrollment_id = %s",
                                     (d.row, student.enrollment_row)).fetchone()[0]
            busy = self.sql.execute(
                "SELECT count(*) FROM outbox_messages m JOIN deliveries v ON v.id = m.delivery_id "
                "WHERE v.dispatch_id = %s AND m.state IN ('pending', 'leased')", (d.row,)).fetchone()[0]
            return state == "no_answer" and busy == 0
        self.wait(ready)

    # -- esperado ------------------------------------------------------------------------------

    def expected_lines(self, day: date, *, new_links: int, late: int, dispatch_lines: list[str],
                       dispatch_count: int | None = None) -> list[str]:
        """As linhas do resumo (o `DAILY_DIGEST` com a adesão do `students.summary`, no mesmo instante,
        e uma `DIGEST_DISPATCH_LINE` por disparo); `dispatch_count` troca a contagem de disparos do cabeçalho
        (o resumo longo confere as linhas dos disparos à parte)."""
        s = students_summary(self.client, ADMIN_TOKEN, self.code)
        head = noticetexts.DAILY_DIGEST.format(
            date=day.isoformat(), course=COURSE, linked=s["linked"], sending=s["sending"],
            terms_presented=s["terms_presented"], invite_valid=s["invite_valid"], send_failed=s["send_failed"],
            invite_lapsed=s["invite_lapsed"], no_invite=s["no_invite"], new_links=new_links, late=late,
            dispatches=len(dispatch_lines) if dispatch_count is None else dispatch_count)
        return _lines(head) + dispatch_lines

    def assert_digest_sent(self, day: date, at: datetime, expected: list[str],
                           alternatives: tuple[list[str], ...] = ()) -> dict:
        """Um único `daily_digest` `queued` da data, entregue a cada membro ativo (uma mensagem, prioridade 2,
        `sent`, dialeto `rich`), com o texto `expected` (ou, só onde a regra admite duas leituras, um
        dos `alternatives`); o evento; o `next_due_at` da data seguinte."""
        notice = self.wait_notice(day, "queued")
        members = self.team.active
        assert [(n["kind"], n["subject_key"], n["state"], n["recipient_count"], n["local_date"], n["dispatch_id"],
                 n["report_id"], n["finished_at"]) for n in self.notices()] == [
            (DIGEST_KIND, f"digest:{day.isoformat()}", "queued", len(members), day, None, None, at)]
        sent = self.wait_sent(notice["id"], len(members))
        assert sorted(m.recipient for m in sent) == sorted(m.recipient for m in members)
        assert {(m.priority, m.text_format, m.role) for m in sent} == {(2, "rich", None)}
        for member in members:
            texts = self.digest_texts(member)
            assert texts in ([expected], *([alt] for alt in alternatives)), (member.label, texts)
        data = json.dumps(notice["data"], ensure_ascii=False)
        assert COURSE not in data, "a fotografia do resumo não guarda texto"
        assert self.notice_events() == [self.recorded(day, "queued", len(members), at)]
        assert self.config() == (True, "20:00", _local(day + timedelta(days=1), 20))
        return notice


@pytest.fixture
def world(client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, engine_fake: Any,
          team_members: Any, report_worker: Any, bot_worker_thread: Any) -> World:
    return World(client=client, sql=sql, clock=clock, rich=rich_bot_api, engine_fake=engine_fake, team=team_members,
                 report_worker=report_worker, bot_worker_thread=bot_worker_thread)


SKY = preset("single_choice")  # "Cor do céu", alternativa certa "b" (Azul), errada "a" (Vermelho)
# "Números primos", escolha múltipla com as certas "a" e "c" (Dois, Sete) e crédito parcial `net-correct`:
# só "a" ou só "c" → nota 0,5; "a" e "c" → 1; só "b" → max(0, (0 − 1)/2) = 0.
PRIMES = preset("multiple_choice")


# ---------------------------------------------------------------------------------------------
# Desligado por padrão e desligado
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("configured", ["absent", "disabled"])
def test_digest_off_sends_nothing_even_with_activity(world: World, configured: str) -> None:
    """Sem linha em `offering_digests` (o padrão, inclusive nos oferecimentos
    existentes, sem backfill) ou com a linha desligada, nenhum `digest.daily` é agendado e nenhum aviso
    `daily_digest` é registrado, mesmo com atividade no dia (um vínculo novo) e com o horário passado."""
    if configured == "absent":
        assert world.config() is None
    else:
        world.enable_sql(next_due_at=_local(DAY, 20), enabled=False)
        assert world.config() == (False, "20:00", None)
    world.start()
    world.clock.set(_local(DAY, 9))
    world.student()
    world.clock.set(_local(DAY, 20, 30))
    time_module.sleep(QUIET)
    world.clock.set(_local(DAY + timedelta(days=1), 20, 30))
    time_module.sleep(QUIET)
    assert world.notices() == []
    assert world.digest_tasks() == []
    assert world.messages() == []
    assert world.notice_events() == []


# ---------------------------------------------------------------------------------------------
# Ligado: um resumo por data local, com adesão, disparos do dia e atrasadas
# ---------------------------------------------------------------------------------------------


def test_enabled_digest_sends_once_at_local_time_with_adhesion_dispatch_and_late(world: World) -> None:
    """Ligado às 20:00 no fuso, com 4 alunos
    vinculados no dia e um disparo aberto e fechado no dia: A acerta e B erra no prazo, C toca "Não sei" no
    prazo e D acerta depois do fechamento (atrasada recebida no dia). Antes das 20:00 nada sai; às 20:00
    (23:00Z) um único resumo, a cada membro ativo, com a adesão, 4 vínculos novos, 1 atrasada e a linha do
    disparo em par (respostas 2/3, corretas 1/2, parcialmente 0/0, não sei 1/1, calculados à mão); o
    `notice.recorded` com `local_date`; o `next_due_at` de amanhã; a fotografia sem o título."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.start()
    world.clock.set(_local(DAY, 9))
    a, b, c, d = (world.student() for _ in range(4))
    disp = world.open("single_choice", SKY, [a, b, c, d], closes_at=_local(DAY, 10))
    world.answer(a, "b")
    world.answer(b, "a")
    world.dont_know(c)
    world.wait_final(disp, 3)
    world.close(disp)
    world.clock.set(_local(DAY, 10, 5))
    world.wait_no_answer(disp, d)
    world.answer(d, "b")
    world.wait_final(disp, 4)
    late = world.sql.execute(
        "SELECT s.late, s.received_at FROM submissions s JOIN deliveries v ON v.id = s.delivery_id "
        "WHERE v.dispatch_id = %s AND v.enrollment_id = %s", (disp.row, d.enrollment_row)).fetchall()
    assert late == [(True, _local(DAY, 10, 5))]

    world.clock.set(_local(DAY, 19, 59))
    time_module.sleep(QUIET)
    assert world.notices() == []

    world.clock.set(_local(DAY, 20))
    expected = world.expected_lines(DAY, new_links=4, late=1, dispatch_lines=[
        _dispatch_line("Cor do céu", (2, 3), (1, 2), (0, 0), (1, 1))])
    notice = world.assert_digest_sent(DAY, _local(DAY, 20), expected)
    assert "Cor do céu" not in json.dumps(notice["data"], ensure_ascii=False)
    for student in (a, b, c, d):
        assert student.pseudo_id not in json.dumps(notice["data"])


def test_digest_dispatch_line_pairs_partial_scores_and_late_dont_know(world: World) -> None:
    """Notas parciais e "não sei" atrasado: a linha do disparo do resumo
    diário traz as parcialmente corretas (0 < nota < 1) e distingue os dois lados de cada par pela marca de
    atraso da submissão, não pelos estados da entrega. Um disparo de escolha múltipla ("Números primos",
    certas "a" e "c", crédito parcial) aberto e fechado no dia, com 6 alunos:
    - no prazo: A marca só "a" (nota 0,5), B marca "a" e "c" (nota 1), C toca "Não sei";
    - depois do fechamento (atrasadas recebidas no dia): D marca só "c" (nota 0,5), E toca "Não sei", F marca
      só "b" (nota 0).
    Calculado à mão (só as submissões finais): respostas (`graded` válidas) 2 no prazo
    (A, B) e 4 com atrasadas (A, B, D, F); corretas 1 e 1 (B); parcialmente corretas 1 e 2 (A; A e D);
    "não sei" 1 e 2 (C; C e E). Uma implementação que zere as parcialmente corretas, ou que conte o "não
    sei" pelos estados das entregas (que juntam o no prazo e o atrasado), falha aqui.

    **Leitura [I]**: nada no produto diz se o "Não sei" atrasado
    entra nas "respostas atrasadas recebidas no dia" do cabeçalho; o teste aceita as duas leituras (2, só
    as `graded` atrasadas D e F; ou 3, todas as submissões finais atrasadas, com E), e só nesse número."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.start()
    world.clock.set(_local(DAY, 9))
    a, b, c, d, e, f = (world.student() for _ in range(6))
    disp = world.open("multiple_choice", PRIMES, [a, b, c, d, e, f], closes_at=_local(DAY, 10))
    world.mark_and_submit(a, "a")
    world.mark_and_submit(b, "ac")
    world.dont_know(c)
    world.wait_final(disp, 3)
    world.close(disp)
    world.clock.set(_local(DAY, 10, 5))
    for late_student in (d, e, f):
        world.wait_no_answer(disp, late_student)
    world.mark_and_submit(d, "c")
    world.dont_know(e)
    world.mark_and_submit(f, "b")
    world.wait_final(disp, 6)
    # Controle positivo: as notas e as marcas de atraso são as do cálculo à mão.
    rows = world.sql.execute(
        "SELECT v.enrollment_id, s.outcome, s.score, s.late, s.received_at FROM submissions s "
        "JOIN deliveries v ON v.id = s.delivery_id WHERE v.dispatch_id = %s", (disp.row,)).fetchall()
    by_student = {r[0]: tuple(r[1:]) for r in rows}
    in_time = {a.enrollment_row: ("graded", 0.5), b.enrollment_row: ("graded", 1.0),
               c.enrollment_row: ("dont_know", None)}
    late = {d.enrollment_row: ("graded", 0.5), e.enrollment_row: ("dont_know", None),
            f.enrollment_row: ("graded", 0.0)}
    assert {k: (v[0], v[1], v[2]) for k, v in by_student.items()} == {
        **{k: (*v, False) for k, v in in_time.items()}, **{k: (*v, True) for k, v in late.items()}}
    assert all(by_student[k][3] == _local(DAY, 10, 5) for k in late)

    world.clock.set(_local(DAY, 20))
    line = _dispatch_line("Números primos", (2, 4), (1, 1), (1, 2), (1, 2))
    graded_late_only = world.expected_lines(DAY, new_links=6, late=2, dispatch_lines=[line])
    every_late_final = world.expected_lines(DAY, new_links=6, late=3, dispatch_lines=[line])
    notice = world.assert_digest_sent(DAY, _local(DAY, 20), graded_late_only, alternatives=(every_late_final,))
    assert "Números primos" not in json.dumps(notice["data"], ensure_ascii=False)


def test_day_without_activity_records_no_activity_and_invite_lapse_does_not_count(world: World) -> None:
    """Um convite enviado na véspera, com validade de 4 horas, vence
    no dia (o vencimento não é atividade); nenhum outro fato no dia → às 20:00, `no_activity` com o
    `notice.recorded`, sem mensagem; o `next_due_at` avança para amanhã."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    pending = world.student(linked=False)
    # Enviado na véspera local (2030-12-31 21:00, o relógio do início), com validade de 4 h: vence às 01:00
    # locais de 2031-01-01 (04:00Z), dentro do dia local.
    world.invite_send(pending, sent=True, valid_hours=4)
    world.start()
    world.clock.set(_local(DAY, 20))
    notice = world.wait_notice(DAY, "no_activity")
    assert [(n["subject_key"], n["state"], n["recipient_count"], n["finished_at"]) for n in world.notices()] == [
        (f"digest:{DAY.isoformat()}", "no_activity", 0, _local(DAY, 20))]
    assert world.messages(notice["id"]) == []
    assert world.notice_events() == [world.recorded(DAY, "no_activity", 0, _local(DAY, 20))]
    assert world.config() == (True, "20:00", _local(DAY + timedelta(days=1), 20))
    summary = students_summary(world.client, ADMIN_TOKEN, world.code)
    assert summary["invite_lapsed"] == 1, "controle positivo: o convite venceu no dia"


@pytest.mark.parametrize("trigger", ["invite_sent", "invite_failed", "late_answer_other_day",
                                     "dispatch_opened_still_open"])
def test_each_activity_trigger_alone_sends_the_digest(world: World, trigger: str) -> None:
    """Com um único fato no dia local e nada mais, o resumo é registrado (`queued`) e
    entregue, nunca `no_activity`: (a) um convite enviado; (b) um envio com falha; (c) uma resposta atrasada
    (pelo `received_at`) de um disparo fechado na véspera; (d) um disparo aberto no dia que ainda não fechou,
    com a linha dele (`DIGEST_DISPATCH_LINE`) e o desempenho em par. Os alunos e os disparos da véspera são
    criados às 21:00 da véspera local (o relógio do início)."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.start()
    late = 0
    lines: list[str] = []
    if trigger in ("invite_sent", "invite_failed"):
        pending = world.student(linked=False)
        world.clock.set(_local(DAY, 9))
        world.invite_send(pending, sent=trigger == "invite_sent")
    elif trigger == "late_answer_other_day":
        student = world.student()
        disp = world.open("single_choice", SKY, [student], closes_at=world.clock.now() + timedelta(minutes=30))
        world.close(disp)
        assert disp.closes_at < _local(DAY, 0), "o disparo fecha na véspera local"
        world.wait_no_answer(disp, student)
        world.clock.set(_local(DAY, 9))
        world.answer(student, "b")
        world.wait_final(disp, 1)
        late = 1
    else:
        student = world.student()
        world.clock.set(_local(DAY, 9))
        disp = world.open("single_choice", SKY, [student], closes_at=_local(DAY + timedelta(days=1), 12))
        world.answer(student, "b")
        world.wait_final(disp, 1)
        lines = [_dispatch_line("Cor do céu", (1, 1), (1, 1), (0, 0), (0, 0))]
    world.clock.set(_local(DAY, 20))
    expected = world.expected_lines(DAY, new_links=0, late=late, dispatch_lines=lines)
    world.assert_digest_sent(DAY, _local(DAY, 20), expected)


# ---------------------------------------------------------------------------------------------
# Worker fora no horário: no mesmo dia, no dia seguinte, vários dias
# ---------------------------------------------------------------------------------------------


def test_worker_down_at_time_and_back_the_same_day_sends_once(world: World) -> None:
    """O worker fora às 20:00 e de volta às 22:30 do mesmo dia local → o resumo
    da data sai uma vez (registrado às 22:30); depois, nada mais na data."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.clock.set(_local(DAY, 9))
    world.student()
    world.clock.set(_local(DAY, 22, 30))
    world.start()
    expected = world.expected_lines(DAY, new_links=1, late=0, dispatch_lines=[])
    world.assert_digest_sent(DAY, _local(DAY, 22, 30), expected)
    world.clock.set(_local(DAY, 23, 50))
    time_module.sleep(QUIET)
    assert len(world.notices()) == 1


@pytest.mark.parametrize("days_down", [1, 3])
def test_worker_down_until_a_later_day_records_one_missed_and_skips(world: World, days_down: int) -> None:
    """O worker fora do horário de 2031-01-01 até as 10:00 de
    `days_down` dias depois → um único `missed` com a data devida (2031-01-01) e o evento, sem mensagem;
    nenhum registro das datas do meio; o `next_due_at` salta para as 20:00 do dia da volta."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.clock.set(_local(DAY, 9))
    world.student()
    back = DAY + timedelta(days=days_down)
    world.clock.set(_local(back, 10))
    world.start()
    notice = world.wait_notice(DAY, "missed")
    assert [(n["subject_key"], n["state"], n["recipient_count"], n["local_date"], n["finished_at"])
            for n in world.notices()] == [(f"digest:{DAY.isoformat()}", "missed", 0, DAY, _local(back, 10))]
    assert world.messages(notice["id"]) == []
    assert world.notice_events() == [world.recorded(DAY, "missed", 0, _local(back, 10))]
    assert world.config() == (True, "20:00", _local(back, 20))


# ---------------------------------------------------------------------------------------------
# Estados do oferecimento: rascunho, ativado depois, arquivado, encerrado
# ---------------------------------------------------------------------------------------------


def test_draft_sends_nothing_and_activation_later_starts_from_its_day(world: World) -> None:
    """Rascunho: um oferecimento em rascunho com o resumo ligado
    e o horário passado → nada (nenhum aviso; o `next_due_at` intocado); ativado no dia seguinte (2031-01-02
    10:00), a data devida anterior à ativação só avança (nenhum `missed` nem `no_activity` de 2031-01-01, e
    o `next_due_at` passa às 20:00 de 2031-01-02), e o tratador age a partir da data da ativação: às 20:00
    de 2031-01-02 registra a data (sem atividade: `no_activity`)."""
    world.start()
    draft = create_offering(world.client, ADMIN_TOKEN, code=f"resumo-rascunho-{next(_codes)}")
    draft_row = world.sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                  (draft["offering_id"],)).fetchone()[0]
    world.enable_sql(next_due_at=_local(DAY, 20), offering_row=draft_row)
    world.clock.set(_local(DAY, 20, 30))
    time_module.sleep(QUIET)
    assert world.notices(draft_row) == []
    assert world.config(draft_row) == (True, "20:00", _local(DAY, 20))

    next_day = DAY + timedelta(days=1)
    world.clock.set(_local(next_day, 10))
    bot = world.rich.add_bot()
    register_bot(world.client, ADMIN_TOKEN, draft["code"], world.rich.token_of(bot))
    assert activate_bot(world.client, ADMIN_TOKEN, draft["code"])["state"] == "active"
    world.wait(lambda: world.config(draft_row) == (True, "20:00", _local(next_day, 20)))
    assert world.notices(draft_row) == []
    world.clock.set(_local(next_day, 20))
    notice = world.wait_notice(next_day, "no_activity", draft_row)
    assert [(n["local_date"], n["state"]) for n in world.notices(draft_row)] == [(next_day, "no_activity")]
    assert notice["finished_at"] == _local(next_day, 20)


def test_archived_offering_sends_nothing(world: World) -> None:
    """Arquivado (só `active` e `closed` recebem): o oferecimento arquivado com o
    resumo ligado e atividade no dia → nenhum aviso nem tarefa depois do horário."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.clock.set(_local(DAY, 9))
    world.student()
    closed = world.client.post(f"/v1/offerings/{world.code}/close", headers=ADMIN)
    assert closed.status_code == 200 and closed.json()["state"] == "closed", closed.text
    prepared = world.client.post(f"/v1/offerings/{world.code}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text
    archived = world.client.post(f"/v1/offerings/{world.code}/archive",
                                 json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert archived.status_code == 200 and archived.json()["state"] == "archived", archived.text
    world.start()
    world.clock.set(_local(DAY, 20, 30))
    time_module.sleep(QUIET)
    assert world.notices() == []
    assert world.digest_tasks() == []


def test_closed_offering_with_activity_sends_and_operations_are_accepted(world: World) -> None:
    """O oferecimento **encerrado** (`closed`) aceita
    `offerings.update_digest` (200, `offering.digest_changed`) e `offerings.get_digest` (200); com o resumo
    ligado e atividade no dia (um vínculo novo antes do encerramento), o resumo sai às 20:00."""
    world.clock.set(_local(DAY, 9))
    world.student()
    closed = world.client.post(f"/v1/offerings/{world.code}/close", headers=ADMIN)
    assert closed.status_code == 200 and closed.json()["state"] == "closed", closed.text
    body = update_offering_digest(world.client, world.code, enabled=True)
    view = {"offering": {"offering_id": world.offering["offering_id"], "code": world.code}, "timezone": TZ,
            "enabled": True, "local_time": "20:00"}
    assert body == view
    assert get_offering_digest(world.client, world.code) == view
    assert _digest_events(world) == [_changed(world, _local(DAY, 9), enabled=True, local_time="20:00")]
    assert world.config() == (True, "20:00", _local(DAY, 20))
    world.start()
    world.clock.set(_local(DAY, 20))
    world.assert_digest_sent(DAY, _local(DAY, 20), world.expected_lines(DAY, new_links=1, late=0, dispatch_lines=[]))


# ---------------------------------------------------------------------------------------------
# Operações (offerings.get_digest, offerings.update_digest)
# ---------------------------------------------------------------------------------------------


def _patch(client: Any, ref: str, body: Any = None, *, raw: bool = False) -> Any:
    if raw:
        return client.request("PATCH", f"/v1/offerings/{ref}/digest", headers=ADMIN)
    return client.patch(f"/v1/offerings/{ref}/digest", json=body, headers=ADMIN)


def _digest_events(world: World) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "seq"}
            for e in all_events(world.client, ADMIN_TOKEN, "offering.digest_changed")
            if e["data"]["offering_id"] == world.offering["offering_id"]]


def _changed(world: World, at: datetime, *, enabled: bool, local_time: str) -> dict:
    return {"type": "offering.digest_changed", "occurred_at": iso(at), "actor_token_id": ADMIN_ACTOR,
            "data": {"offering_id": world.offering["offering_id"], "enabled": enabled, "local_time": local_time}}


def test_get_digest_defaults_without_row_in_every_state(world: World) -> None:
    """Sem configuração gravada, `{enabled: false, local_time: "20:00"}` com
    o `OfferingRef` e o fuso, em rascunho, ativo, encerrado e arquivado; nenhuma linha é criada pela leitura."""
    draft = create_offering(world.client, ADMIN_TOKEN, code=f"resumo-leitura-{next(_codes)}", timezone="America/Manaus")
    assert get_offering_digest(world.client, draft["code"]) == {
        "offering": {"offering_id": draft["offering_id"], "code": draft["code"]}, "timezone": "America/Manaus",
        "enabled": False, "local_time": "20:00"}
    view = {"offering": {"offering_id": world.offering["offering_id"], "code": world.code}, "timezone": TZ,
            "enabled": False, "local_time": "20:00"}
    assert get_offering_digest(world.client, world.code) == view
    assert get_offering_digest(world.client, world.offering["offering_id"]) == view
    assert world.client.post(f"/v1/offerings/{world.code}/close", headers=ADMIN).status_code == 200
    assert get_offering_digest(world.client, world.code) == view
    prepared = world.client.post(f"/v1/offerings/{world.code}/archive/prepare", headers=ADMIN).json()
    assert world.client.post(f"/v1/offerings/{world.code}/archive",
                             json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN).status_code == 200
    assert get_offering_digest(world.client, world.code) == view
    assert world.sql.execute("SELECT count(*) FROM offering_digests").fetchone()[0] == 0
    assert_error(world.client.get("/v1/offerings/oferta-inexistente-resumo/digest", headers=ADMIN), 404, "not_found")


def test_update_digest_rejects_invalid_input_without_writing(world: World) -> None:
    """`local_time` `24:00` e `7h` → `[{local_time, invalid_format}]`;
    `enabled` não booleano (texto, número, `null`) → `[{enabled, invalid_format}]`; sem corpo →
    `[{body, missing}]`; `{}` → `[{body, too_few}]`; campo extra → `[{body, unknown_field}]`; inexistente →
    404. Nenhuma escrita nem evento."""
    for bad in ("24:00", "7h", "7:00", "20:60"):
        assert_error(_patch(world.client, world.code, {"local_time": bad}), 400, "invalid_request",
                     fields=[("local_time", "invalid_format")])
    for bad in ("sim", 1, 0, None, "true", [True]):
        assert_error(_patch(world.client, world.code, {"enabled": bad}), 400, "invalid_request",
                     fields=[("enabled", "invalid_format")])
    assert_error(_patch(world.client, world.code, raw=True), 400, "invalid_request", fields=[("body", "missing")])
    assert_error(_patch(world.client, world.code, {}), 400, "invalid_request", fields=[("body", "too_few")])
    assert_error(_patch(world.client, world.code, {"enabled": True, "campo_extra": 1}), 400, "invalid_request",
                 fields=[("body", "unknown_field")])
    assert_error(_patch(world.client, "oferta-inexistente-resumo", {"enabled": True}), 404, "not_found")
    assert world.sql.execute("SELECT count(*) FROM offering_digests").fetchone()[0] == 0
    assert _digest_events(world) == []
    assert get_offering_digest(world.client, world.code)["enabled"] is False


def test_update_digest_changes_with_event_and_same_value_without_event(world: World) -> None:
    """Às 21:00 locais (o relógio do
    início, 2030-12-31), ligar → 200 com a configuração e `offering.digest_changed`; o `next_due_at` é a
    próxima ocorrência, as 20:00 de amanhã (já passou hoje); repetir o mesmo valor (inclusive só
    `local_time: "20:00"`) → 200 sem evento nem escrita; mudar para 22:30 (ainda hoje) → o `next_due_at` de
    hoje às 22:30; desligar → `next_due_at` nulo; arquivado → `invalid_state` sem escrita."""
    start = world.clock.now()
    assert start == _local(DAY - timedelta(days=1), 21)
    ref = {"offering_id": world.offering["offering_id"], "code": world.code}

    on = update_offering_digest(world.client, world.code, enabled=True)
    assert on == {"offering": ref, "timezone": TZ, "enabled": True, "local_time": "20:00"}
    assert world.config() == (True, "20:00", _local(DAY, 20))
    updated_at = world.sql.execute("SELECT updated_at FROM offering_digests").fetchone()[0]
    assert updated_at == start

    world.clock.advance(60)
    for same in ({"enabled": True}, {"local_time": "20:00"}, {"enabled": True, "local_time": "20:00"}):
        assert update_offering_digest(world.client, world.code, **same) == on
    assert world.sql.execute("SELECT updated_at FROM offering_digests").fetchone()[0] == updated_at

    later = update_offering_digest(world.client, world.code, local_time="22:30")
    assert later == {"offering": ref, "timezone": TZ, "enabled": True, "local_time": "22:30"}
    assert world.config() == (True, "22:30", _local(DAY - timedelta(days=1), 22, 30))

    off = update_offering_digest(world.client, world.code, enabled=False)
    assert off == {"offering": ref, "timezone": TZ, "enabled": False, "local_time": "22:30"}
    assert world.config() == (False, "22:30", None)
    t1 = start + timedelta(seconds=60)
    assert _digest_events(world) == [_changed(world, start, enabled=True, local_time="20:00"),
                                     _changed(world, t1, enabled=True, local_time="22:30"),
                                     _changed(world, t1, enabled=False, local_time="22:30")]

    assert world.client.post(f"/v1/offerings/{world.code}/close", headers=ADMIN).status_code == 200
    prepared = world.client.post(f"/v1/offerings/{world.code}/archive/prepare", headers=ADMIN).json()
    assert world.client.post(f"/v1/offerings/{world.code}/archive",
                             json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN).status_code == 200
    for body in ({"enabled": True}, {"local_time": "07:00"}):
        assert_error(_patch(world.client, world.code, body), 409, "invalid_state", state="archived")
    assert world.config() == (False, "22:30", None)
    assert len(_digest_events(world)) == 3


def test_update_digest_accepted_in_draft(world: World) -> None:
    """Em rascunho, ligar e escolher o horário → 200 e o evento; o
    `next_due_at` gravado (o tratador só age depois da ativação)."""
    draft = create_offering(world.client, ADMIN_TOKEN, code=f"resumo-rascunho-op-{next(_codes)}")
    body = update_offering_digest(world.client, draft["code"], enabled=True, local_time="07:15")
    assert body == {"offering": {"offering_id": draft["offering_id"], "code": draft["code"]}, "timezone": TZ,
                    "enabled": True, "local_time": "07:15"}
    row = world.sql.execute("SELECT id FROM offerings WHERE offering_id = %s", (draft["offering_id"],)).fetchone()[0]
    assert world.config(row) == (True, "07:15", _local(DAY, 7, 15))
    events = [e for e in all_events(world.client, ADMIN_TOKEN, "offering.digest_changed")
              if e["data"]["offering_id"] == draft["offering_id"]]
    assert [e["data"] for e in events] == [{"offering_id": draft["offering_id"], "enabled": True,
                                            "local_time": "07:15"}]


# ---------------------------------------------------------------------------------------------
# No máximo um resumo por data local (reexecução e reagendamento)
# ---------------------------------------------------------------------------------------------


def _assert_single_digest(world: World, notice: dict, day: date) -> None:
    assert [(n["id"], n["local_date"], n["state"]) for n in world.notices()] == [(notice["id"], day, "queued")]
    assert len(world.messages()) == len(world.team.active)
    assert len(world.notice_events()) == 1


def test_reexecution_for_the_same_date_sends_nothing_new(world: World) -> None:
    """Depois do resumo de 2031-01-01, o `next_due_at` volta por
    SQL para as 20:00 do mesmo dia (a reexecução do tratador para a mesma data) → o tratador roda de novo
    (outra tarefa `digest.daily`, `done`), o `record` devolve `None`: nenhum segundo `daily_digest` nem
    `no_activity`, nenhuma mensagem nova, nenhum evento novo; o `next_due_at` avança para amanhã."""
    world.enable_sql(next_due_at=_local(DAY, 20))
    world.clock.set(_local(DAY, 9))
    world.student()
    world.start()
    world.clock.set(_local(DAY, 20))
    notice = world.assert_digest_sent(DAY, _local(DAY, 20),
                                      world.expected_lines(DAY, new_links=1, late=0, dispatch_lines=[]))
    tasks_before = world.digest_tasks()
    world.clock.set(_local(DAY, 20, 30))
    world.sql.execute("UPDATE offering_digests SET next_due_at = %s", (_local(DAY, 20),))
    world.wait(lambda: len(world.digest_tasks()) > len(tasks_before)
               and all(state == "done" for _, state in world.digest_tasks()))
    world.wait(lambda: world.config() == (True, "20:00", _local(DAY + timedelta(days=1), 20)))
    _assert_single_digest(world, notice, DAY)


@pytest.mark.parametrize("change", ["later_time", "off_and_on"])
def test_rescheduling_after_todays_digest_sends_no_second_one(world: World, change: str) -> None:
    """Reagendamento no mesmo dia: resumo de 2031-01-01 enviado
    às 20:00; às 21:00 do mesmo dia local, (a) `offerings.update_digest` com `local_time = "22:00"`, ou (b)
    desligar e religar (o novo `next_due_at` ainda no mesmo dia local: 22:00 ou 21:00) → o `next_due_at`
    volta para hoje, o tratador roda para a mesma data, o `record` devolve `None`: nenhum segundo
    `daily_digest` nem `no_activity`, nenhuma mensagem nova; o `next_due_at` avança para a data seguinte."""
    update_offering_digest(world.client, world.code, enabled=True)
    assert world.config() == (True, "20:00", _local(DAY, 20))
    world.clock.set(_local(DAY, 9))
    world.student()
    world.start()
    world.clock.set(_local(DAY, 20))
    notice = world.assert_digest_sent(DAY, _local(DAY, 20),
                                      world.expected_lines(DAY, new_links=1, late=0, dispatch_lines=[]))
    world.clock.set(_local(DAY, 21))
    if change == "later_time":
        update_offering_digest(world.client, world.code, local_time="22:00")
        assert world.config() == (True, "22:00", _local(DAY, 22))
        world.clock.set(_local(DAY, 22, 1))
        tomorrow = (True, "22:00", _local(DAY + timedelta(days=1), 22))
    else:
        update_offering_digest(world.client, world.code, enabled=False)
        update_offering_digest(world.client, world.code, enabled=True, local_time="21:00")
        assert world.config() == (True, "21:00", _local(DAY, 21))
        world.clock.set(_local(DAY, 21, 1))
        tomorrow = (True, "21:00", _local(DAY + timedelta(days=1), 21))
    world.wait(lambda: world.config() == tomorrow)
    time_module.sleep(QUIET)
    _assert_single_digest(world, notice, DAY)


# ---------------------------------------------------------------------------------------------
# Reanimação pela varredura sem tarefa viva
# ---------------------------------------------------------------------------------------------


def test_sweep_revives_due_digest_without_live_task(world: World, queue: Any, settings: Any) -> None:
    """Reanimação pela varredura: o resumo de 2031-01-01
    ligado, com um vínculo novo no dia, vence às 20:00 com o worker fora. A regra `digest.daily` da
    varredura (`scheduler.REPORT_RULES`, numa rodada chamada pelo teste) enfileira `digest.daily:<id>:r1`, e com
    ela viva nenhuma outra; a r1 termina em falha definitiva antes de registrar o aviso. O resumo fica
    devido sem tarefa viva: nenhum aviso, nenhuma mensagem, nenhum evento, o `next_due_at` ainda nas 20:00
    de hoje. O worker volta às 20:00: dentro do `retry_gap` (o `backoff_max_seconds` da fixture, 60 s) nada
    é reanimado; às 20:01:01 (23:01:01Z, à mão) a varredura do worker reanima a tarefa exatamente uma vez
    (`digest.daily:<id>:r2`, `done`), que registra um único `daily_digest` `queued` às 20:01:01, entregue
    uma vez a cada membro ativo, com um único `notice.recorded`; o `next_due_at` avança para as 20:00 de
    amanhã e a regra não vence mais (nenhuma r3)."""
    from cq_server.scheduler import REPORT_RULES, sweep_once

    world.enable_sql(next_due_at=_local(DAY, 20))
    digest_row = world.sql.execute("SELECT id FROM offering_digests WHERE offering_id = %s",
                                   (world.offering_row,)).fetchone()[0]
    world.clock.set(_local(DAY, 9))
    world.student()
    due = _local(DAY, 20)
    world.clock.set(due)
    rules = tuple(rule for rule in REPORT_RULES if rule.task_type == DIGEST_TASK)
    assert len(rules) == 1, "a regra digest.daily da varredura"
    retry_gap = float(settings.backoff_max_seconds)
    assert retry_gap == 60.0
    assert sweep_once(world.sql, now=due, retry_gap=retry_gap, rules=rules).enqueued == {DIGEST_TASK: 1}
    assert sweep_once(world.sql, now=due, retry_gap=retry_gap, rules=rules).enqueued == {}, "com a r1 viva"
    claimed = queue.claim("teste-resumo", task_types=[DIGEST_TASK])
    assert claimed is not None
    assert claimed.payload == {rules[0].subject_key: digest_row}
    assert queue.fail(claimed.id, "teste-resumo", "FalhaSimulada", permanent=True)
    # Devido sem tarefa viva: a r1 `failed`, nenhum aviso, o `next_due_at` intocado.
    assert world.digest_tasks() == [(claimed.id, "failed")]
    assert world.notices() == []
    assert world.messages() == []
    assert world.notice_events() == []
    assert world.config() == (True, "20:00", due)

    world.start()
    time_module.sleep(QUIET)
    assert world.digest_tasks() == [(claimed.id, "failed")], "o retry_gap depois da falha não é dispensado"
    assert world.notices() == []

    revived_at = due + timedelta(seconds=retry_gap + 1)
    assert revived_at == datetime(2031, 1, 1, 23, 1, 1, tzinfo=UTC)
    world.clock.set(revived_at)
    expected = world.expected_lines(DAY, new_links=1, late=0, dispatch_lines=[])
    notice = world.assert_digest_sent(DAY, revived_at, expected)
    world.wait(lambda: [state for _, state in world.digest_tasks()] == ["failed", "done"])
    keys = [r[0] for r in world.sql.execute(
        "SELECT idempotency_key FROM tasks WHERE task_type = %s ORDER BY id", (DIGEST_TASK,)).fetchall()]
    assert keys == [f"{DIGEST_TASK}:{digest_row}:r1", f"{DIGEST_TASK}:{digest_row}:r2"]
    time_module.sleep(QUIET)
    assert [state for _, state in world.digest_tasks()] == ["failed", "done"], "reanimada uma única vez"
    _assert_single_digest(world, notice, DAY)
    assert world.config() == (True, "20:00", _local(DAY + timedelta(days=1), 20))
    assert sweep_once(world.sql, now=revived_at, retry_gap=retry_gap, rules=rules).enqueued == {}


# ---------------------------------------------------------------------------------------------
# Resumo longo em partes
# ---------------------------------------------------------------------------------------------


LONG_DISPATCHES = 30


def _long_title(k: int) -> str:
    return f"Disparo longo {k:02d} " + "com um título bem comprido para o resumo diário " * 6


def test_long_digest_is_split_into_ordered_parts_with_every_number(world: World) -> None:
    """Resumo longo (os números nunca são cortados): 30 disparos abertos no
    dia (alvo vazio), cada um com título longo → o resumo não cabe numa mensagem: sai em várias partes
    (`notice:<id>:p<pessoa>:part<k>`), na ordem por destinatário; cada parte cabe em 4096 caracteres de
    markup e tem só linhas inteiras; a primeira começa pelo cabeçalho do `DAILY_DIGEST`; juntas, as
    partes têm uma linha por disparo, na ordem de criação, cada uma com o começo do título (truncado só no
    título) e os quatro pares inteiros (0/0); um único aviso, `queued`.

    O worker sobe com o ritmo rápido por
    chat (`chat_interval_seconds = 0.05`, como `test_preview_buttons.py`), para que as partes não fiquem
    atrás de dezenas de avisos e prévias na fila por chat além da espera de 25 s; as partes são também
    lidas pelas mensagens do próprio aviso (`channel_ref`), entregues na ordem das partes, e nenhuma outra
    mensagem do chat (com controle positivo: há outras) tem o marcador `• ` ou o cabeçalho do resumo."""
    from cq_server.config import DeliverySettings

    world.enable_sql(next_due_at=_local(DAY, 20))
    world.start(delivery=DeliverySettings(chat_interval_seconds=0.05))
    world.clock.set(_local(DAY, 9))
    titles = []
    for k in range(LONG_DISPATCHES):
        # Um commit por disparo (a árvore `course` vale para todo par), cada um com o seu título.
        question = FakeQuestion(_long_title(k), "Qual é a cor do céu?", (single_choice_field(),))
        world.open("single_choice", question, [], closes_at=_local(DAY + timedelta(days=1), 12),
                   commit=f"{0xd16e5700 + k:040x}")
        titles.append(_long_title(k))
    world.clock.set(_local(DAY, 20))
    notice = world.wait_notice(DAY, "queued")
    members = world.team.active
    pairs = (f"respostas {_pair(0, 0)}; corretas {_pair(0, 0)}; parcialmente corretas {_pair(0, 0)}; "
             f"não sei {_pair(0, 0)}")
    keys = dict(world.sql.execute("SELECT id, idempotency_key FROM outbox_messages WHERE notice_id = %s",
                                  (notice["id"],)).fetchall())
    for member in members:
        sent = world.wait(lambda m=member: (found := [x for x in world.messages(notice["id"]) if x.recipient == m.recipient])
                          and all(x.state == "sent" for x in found) and found)
        assert len(sent) >= 2, "o resumo longo sai em mais de uma parte"
        person_row = world.sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (member.pseudo_id,)).fetchone()[0]
        assert [keys[x.message_id] for x in sent] == [
            f"notice:{notice['id']}:p{person_row}:part{k}" for k in range(1, len(sent) + 1)]
        assert all(len(x.text or "") <= TEXT_LIMIT and x.priority == 2 for x in sent)
        parts = [_lines(m.plain_text) for m in world.rich.messages(world.bot, member.chat_id)
                 if m.plain_text and ("Resumo do dia" in m.plain_text or "• " in m.plain_text)]
        # As partes pelas mensagens do próprio aviso `daily_digest`, entregues na ordem das partes; nenhuma
        # outra mensagem do chat do membro tem o marcador `• ` ou o cabeçalho (o filtro acima não conta outra).
        refs = [int(x.channel_ref) for x in sent]
        assert refs == sorted(refs) and len(set(refs)) == len(refs), refs
        own = [_lines(world.rich.current(world.bot, member.chat_id, ref).plain_text) for ref in refs]
        others = [m for m in world.rich.messages(world.bot, member.chat_id) if m.message_id not in set(refs)]
        assert others, "controle positivo: o chat do membro tem outras mensagens (boas-vindas, avisos, prévias)"
        assert [m.message_id for m in others
                if m.plain_text and ("• " in m.plain_text or "Resumo do dia" in m.plain_text)] == []
        assert parts == own
        assert len(parts) == len(sent)
        head = world.expected_lines(DAY, new_links=0, late=0, dispatch_lines=[], dispatch_count=LONG_DISPATCHES)
        assert parts[0][:len(head)] == head
        body = parts[0][len(head):] + [line for part in parts[1:] for line in part]
        assert len(body) == LONG_DISPATCHES
        for line, title in zip(body, titles, strict=True):
            assert line.startswith("• ") and line.endswith(": " + pairs), line
            shown = line[len("• "):-len(": " + pairs)]
            assert shown and title.startswith(shown.rstrip("…").rstrip()), (shown, title)
    assert [(n["state"], n["recipient_count"]) for n in world.notices()] == [("queued", len(members))]
