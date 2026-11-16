"""Número, inteiro e texto; vírgula, erros de formato, ligação do texto ao campo e correção pendente.
Os cenários das respostas digitadas, com a vírgula decimal normalizada e marcada.


Contratos: a submissão, o pedido de campo, o roteamento, as recusas, a ligação do texto ao campo, a
aplicação, a resposta crua e o console; a correção (erro de formato, `server_format_error`, correção
pendente), as mensagens livres, os desfechos, os textos e a
tradução dos erros do PL.


Tudo passa pelo caminho real, só com dublês, como em `test_answers_choice.py`: oferecimento, bot e lista
pela API; o disparo preparado e confirmado pela API (imediato) e validado, aberto e entregue pelo worker,
com a obtenção falsa e o motor falso estrito pelo adaptador HTTP real. O vínculo do aluno é gravado
por SQL com as linhas do `/aceito` (depois de o chat falar com o bot uma vez: o "Olá" segue o comportamento dos bots,
`not_handled`, conferido como regressão). Os casos de vírgula, erro de formato e
novo pedido, ligação por "responder" e pelo campo único e correção pendente rodam **pelos dois canais**:
a `rich_bot_api` (`run_worker` com `bots`) e o console (`run_worker(…, console_routes=True)` com um
`ConsoleChannel` em memória e a `ConsoleInbox`, que grava a entrada pelo caminho do Telegram).

Conferências: a linha literal do update e o desfecho; a resposta crua exata recebida pelo motor
falso (a checagem das formas aceitas pelo motor falso fica ligada); a submissão como o motor devolveu
(validade, nota, notas por parte, erros de formato originais) e a entrada literal
com a marca de normalização; a entrega; os textos ao aluno em português, sem o original em inglês;
os eventos. A conferência da resposta crua vale para todos os casos, inclusive a múltipla digitada, que vai ao motor
como o texto literal, sem nenhuma mudança (a
adaptação de canal da escolha múltipla digitada), e o texto vazio ou só de espaços num campo de texto, que também
vai literal, e quem decide é o `pl-string-input` do PL (a regra: nada é aparado).

Os textos esperados são os textos fixos ao aluno e os da tradução dos erros do PL, escritos aqui por extenso.
Horários pelo `FakeClock`, com saltos de até 10 s e a posse do bot conferida a cada salto. Ids internos,
`update_id` e pseudônimos nunca como literal.

Escolhas [I] deste arquivo: no evento de um campo numérico, `ratio` e `residual` são
conferidos só pelo tipo (número ou nulo), porque nada no produto fixa a fórmula; a mensagem sem texto é uma
figurinha (`sticker`) injetada crua; a mensagem editada do aluno (`edited_message`) nem chega ao sistema,
porque o `allowed_updates` dos bots não a pede (conferido nas consultas).

No esqueleto, toda função falha no primeiro `dispatches.prepare`
(rota ausente, 404). Alguns casos também dependem de outras partes: as recusas de entrega encerrada e a contagem
só dos disparos abertos (que fecham um disparo), os casos de mensagem livre (nenhum campo digitável e o
ambíguo) e o "Não sei" com correção pendente.


O `_note_question` só olha as
questões do próprio aluno; com as mensagens livres opcionais por oferecimento, o ambiente cadastra o oferecimento com
as mensagens livres ligadas (`free_messages=True`), porque o caso da mensagem livre afirma o modo ligado
(o desligado, padrão, fica em `test_free_messages.py`); antes da implementação o cadastro dá 400 e todo caso falha
no `create_offering`; com o feedback de nota parcial,
o feedback da múltipla digitada traz a nota e as marcas ✅/❌ por alternativa escolhida.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import pytest
from psycopg.rows import dict_row

from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_events,
    prepare_dispatch,
    register_bot,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleStudent
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    INTEGER_INVALID_HTML,
    INTEGER_INVALID_TEXT,
    NUMBER_INVALID_HTML,
    NUMBER_INVALID_TEXT,
    SERVER_FORMAT_ERROR,
    TEXT_BLANK,
    html_to_text,
    invalid_option_error,
    number_field,
    preset,
)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
WINDOW_MINUTES = 5
WORKER_ID = "worker-respostas-digitadas"
REPO = "https://repos.cq.test/disciplina/questoes-digitadas.git"
COMMIT = "d161fade" * 5
ENGINE_TIMEOUT = 10.0
PROCESS_TIMEOUT = 20.0
CLOCK_STEP = 10
LEASE_MARGIN = 13
STEP = 15
MODES = ["telegram", "console"]

# Os textos fixos ao aluno, por extenso.
ALREADY_ANSWERED = "Você já respondeu a esta questão."
GRADING_DELAYED = "Resposta recebida; a correção chega em instantes."
GRADING_IN_PROGRESS = "Sua resposta está sendo corrigida."
UNAVAILABLE = "Esta questão não está disponível."
REPLY_WITH_TEXT = "Responda com texto."
FREE_MESSAGE_REPLY = ("Recebi a sua mensagem, mas ela não foi tomada como resposta. Para responder, use os "
                      'botões da questão ou toque em "Responder" na mensagem da questão.')
USE_REPLY = ('Há mais de uma questão aberta esperando texto. Toque em "Responder" na mensagem da questão '
             'certa e digite a resposta.')
FORMAT_ERROR_PREFIX = "Resposta não aceita (não conta como tentativa): "
CORRECT = "✅ correta"
INCORRECT = "❌ incorreta"
# O feedback de nota parcial (`SCORE` e o feedback dos textos fixos ao aluno): a nota sai em toda correção válida, depois do veredito,
# com o número do selo do PL; as marcas por alternativa vêm depois do texto.
SCORE = "nota {n}%"
MARK_RIGHT, MARK_WRONG = "✅", "❌"
DONT_KNOW_BUTTON = "Não sei"
# A tradução dos erros de formato do PL (nada do original chega ao aluno).
NUMBER_UNREADABLE = "Não consegui ler a resposta como número. Não use variáveis nem outros símbolos."
DIVISION_BY_ZERO = "A fração tem divisão por zero."
INTEGER_UNREADABLE = ("Não consegui ler a resposta como número inteiro. Não use símbolos, frações nem "
                      "notação científica.")
INVALID_OPTION = "Uma das alternativas marcadas não existe nesta questão."
BLANK = "A resposta está em branco."
OUTSIDE_FIELDS = "A questão recusou a resposta. Confira o formato e envie de novo."
ENGLISH = ("Invalid", "invalid", "You selected", "You must", "server check", "submitted answer", "division")
# A dica do motor para o número (engine/src/ir/fields.ts; `tests/helpers/engine.py`, `_hint`).
NUMBER_HINT = "Número; ex.: 4.25, -17 ou 3/7"
LABEL, SUFFIX = "Velocidade:", "m/s"

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio


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
class _Student:
    user: Any
    ra: str
    pseudo: str
    person_row: int
    endpoint_row: int
    console: ConsoleStudent | None = None

    @property
    def chat(self) -> int:
        return self.user.user_id


def _one(sql: Any, query: str, params: tuple = ()) -> dict | None:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _all(sql: Any, query: str, params: tuple = ()) -> list[dict]:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


_DELIVERY_SQL = """
    SELECT d.id, d.delivery_uuid::text AS delivery_uuid, d.state, d.undelivered_reason, d.failure,
           d.current_field, d.render_version, d.controls_version, d.sent_at, d.answered_at, d.revealed_at,
           c.parts, c.selection
      FROM deliveries d
      JOIN dispatches x ON x.id = d.dispatch_id
      JOIN enrollments e ON e.id = d.enrollment_id
      JOIN people p ON p.id = e.person_id
      LEFT JOIN delivery_contents c ON c.delivery_id = d.id
     WHERE x.dispatch_uuid = %s::uuid AND p.pseudo_id = %s::uuid
"""

_SUBMISSIONS_SQL = """
    SELECT s.id, s.submission_uuid::text AS submission_uuid, s.telegram_update_id, s.outcome, s.valid,
           s.refusal, s.failure_code, s.failure_phase, s.late, s.normalized_fields, s.raw_input,
           s.submitted_answers, s.format_errors, s.feedback, s.params, s.partial_scores,
           s.correct_answers, s.score, s.engine_mismatch, s.pl_mismatch, s.response_ms,
           s.delay_notice_sent, s.received_at, s.graded_at
      FROM submissions s
     WHERE s.delivery_id = %s
     ORDER BY s.id
"""

_ROW_COLUMNS = """
    SELECT u.id, u.update_id, u.kind, u.chat_id, u.sender_id, u.text, u.reply_to_message_id,
           u.received_at, u.state, u.outcome, u.processed_at
      FROM telegram_updates u
"""


class _Env:
    """Um oferecimento ativo com bot, alunos vinculados e o worker no ar, pelo Telegram (`rich_bot_api`)
    ou pelo console (`ConsoleChannel` + `ConsoleInbox`). As referências de mensagem são o `message_id`
    (Telegram) ou a `ConsoleLine` (console)."""

    def __init__(self, fx: dict[str, Any], *, mode: str, students: int, label: str) -> None:
        self.fx = fx
        self.mode = mode
        self.client = fx["client"]
        self.sql = fx["sql"]
        self.clock = fx["clock"]
        self.rich = fx["rich_bot_api"]
        self.engine_fake = fx["engine_fake"]
        self.engines: list[HttpQuestionEngine] = []
        self.default_channel = _NoRouteChannel()
        self.stream = io.StringIO()
        self.questions: dict[tuple[str, str], Any] = {}
        n = next(_counter)
        self.code = f"mc102-{label}-{n}"
        # O ambiente cadastra o oferecimento com as mensagens livres (opcionais) ligadas; o caso da mensagem
        # livre deste arquivo afirma o comportamento do modo ligado (o desligado fica em test_free_messages.py).
        self.offering = create_offering(self.client, ADMIN_TOKEN, code=self.code, course=COURSE, term=TERM,
                                        timezone=ZONE, response_window_minutes=WINDOW_MINUTES,
                                        free_messages=True)
        self.bot = self.rich.add_bot(f"cq_{label}_{n:03d}_bot")
        register_bot(self.client, ADMIN_TOKEN, self.code, self.rich.token_of(self.bot))
        activate_bot(self.client, ADMIN_TOKEN, self.code)
        self.bot_row = self.sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s",
                                        (self.bot,)).fetchone()[0]
        rows = [{"name": f"Pessoa Resposta Digitada {n:03d} {k}", "ra": f"572{n:05d}{k:02d}",
                 "email": f"resposta.digitada{n}.{k}@turma.example.org"} for k in range(students)]
        import_students(self.client, ADMIN_TOKEN, self.code, rows)
        self.worker = self._start()
        self.students: list[_Student] = []
        for k, row in enumerate(rows):
            user = make_user(660 + k)
            if mode == "telegram":
                self.introduce(user)
            self.students.append(self._link(row["ra"], user))

    # -- worker --------------------------------------------------------------------------------

    def _start(self) -> Any:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url,
                                                   timeout_seconds=ENGINE_TIMEOUT))
        self.engines.append(engine)
        kwargs = {"engine": engine, "fetcher": self.fx["repo_fetcher"], "delivery": DeliverySettings()}
        if self.mode == "telegram":
            thread = self.fx["bot_worker_thread"](
                channel=self.default_channel,
                settings=dataclasses.replace(self.fx["settings"], worker_id=WORKER_ID), **kwargs)
            self.wait(lambda: self.rich.pending_polls(self.bot))
            return thread
        return self.fx["worker_thread"](channel=ConsoleChannel(self.stream), console_routes=True, **kwargs)

    def close(self) -> None:
        for engine in self.engines:
            with contextlib.suppress(NotImplementedError):
                engine.close()

    # -- tempo e espera ------------------------------------------------------------------------

    def wait(self, predicate: Callable[[], Any], timeout: float = PROCESS_TIMEOUT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def _lease_left(self) -> float:
        until = self.sql.execute("SELECT lease_until FROM bots WHERE id = %s", (self.bot_row,)).fetchone()[0]
        return -1.0 if until is None else (until - self.clock.now()).total_seconds()

    def advance(self, seconds: float) -> datetime:
        left = seconds
        while left > 0:
            step = min(CLOCK_STEP, left)
            left -= step
            self.clock.advance(step)
            if self.mode == "telegram":
                self.wait(lambda: self._lease_left() >= LEASE_MARGIN)
        return self.clock.now()

    # -- alunos --------------------------------------------------------------------------------

    def introduce(self, user: Any) -> None:
        """O chat fala com o bot uma vez, antes do vínculo: segue o comportamento dos bots (`not_handled`)."""
        uid = self.rich.push_text(self.bot, user, "Olá")
        row = self.wait(lambda: _one(self.sql, "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b "
                                     "ON b.id = u.bot_id WHERE b.telegram_bot_id = %s AND u.update_id = %s "
                                     "AND u.state = 'processed'", (self.bot, uid)))
        assert row == {"state": "processed", "outcome": "not_handled"}

    def _link(self, ra: str, user: Any) -> _Student:
        found = _one(self.sql, "SELECT p.id AS person_row, p.pseudo_id::text AS pseudo, e.id AS enrollment_row "
                     "FROM identities i JOIN people p ON p.id = i.person_id "
                     "JOIN enrollments e ON e.person_id = p.id JOIN offerings o ON o.id = e.offering_id "
                     "WHERE i.ra = %s AND o.offering_id = %s::uuid", (ra, self.offering["offering_id"]))
        assert found is not None
        now = self.clock.now()
        self.sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                         "terms_accepted_at = %s WHERE id = %s", (now, now, found["enrollment_row"]))
        endpoint = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (found["person_row"], self.bot_row, user.user_id, now, now)).fetchone()[0]
        console = None
        if self.mode == "console":
            console = ConsoleStudent(ConsoleInbox(self.fx["db_url"], clock=self.clock), bot_id=self.bot_row,
                                     chat_id=user.user_id, recipient=found["pseudo"], source=self.stream)
        return _Student(user=user, ra=ra, pseudo=found["pseudo"], person_row=found["person_row"],
                        endpoint_row=endpoint, console=console)

    # -- disparos ------------------------------------------------------------------------------

    def prepare(self, qid: str, question: Any) -> dict:
        self.engine_fake.program((REPO, COMMIT, qid), question)
        prepared = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid)
        assert prepared["state"] == "validating"
        dispatch_id = prepared["dispatch_id"]

        def settled() -> dict | None:
            body = get_dispatch(self.client, ADMIN_TOKEN, self.code, dispatch_id)
            return None if body["state"] == "validating" else body

        body = self.wait(settled)
        assert (body["state"], body["confirmed"]) == ("scheduled", False), (
            f"validação inesperada: {body['state']} {body['rejection']}")
        return body

    def confirm(self, dispatch: dict) -> dict:
        confirmed = confirm_dispatch(self.client, ADMIN_TOKEN, self.code, dispatch["dispatch_id"])
        assert confirmed["confirmed"] is True
        return confirmed

    def settle(self, dispatch: dict, expected: dict[str, int]) -> dict:
        dispatch_id = dispatch["dispatch_id"]

        def ready() -> bool:
            counts = dict(self.sql.execute(
                "SELECT d.state, count(*) FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
                "WHERE x.dispatch_uuid = %s::uuid GROUP BY d.state", (dispatch_id,)).fetchall())
            queued = self.sql.execute(
                "SELECT count(*) FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
                "JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid "
                "AND o.state IN ('pending', 'leased')", (dispatch_id,)).fetchone()[0]
            return counts == expected and queued == 0

        self.wait(ready)
        for student in self.students:
            self._note_question(dispatch_id, student)
        return get_dispatch(self.client, ADMIN_TOKEN, self.code, dispatch_id)

    def open(self, qid: str, question: Any) -> dict:
        dispatch = self.confirm(self.prepare(qid, question))
        return self.settle(dispatch, {"delivered": len(self.students)})

    def _note_question(self, dispatch_id: str, student: _Student) -> None:
        # Só as questões já anotadas do próprio aluno; o `message_id` da Bot API falsa é por
        # chat, e a questão de outro aluno pode ter o mesmo número.
        known = [ref for (_, pseudo), ref in self.questions.items() if pseudo == student.pseudo]
        if self.mode == "telegram":
            new = [m.message_id for m in self.rich.messages(self.bot, student.chat)
                   if m.keyboard is not None and m.message_id not in known]
        else:
            assert student.console is not None
            new = [line for line in student.console.lines() if line.answerable and not line.has_rich_fields
                   and line not in known]
        if new:
            assert len(new) == 1, f"mais de uma mensagem de questão nova: {len(new)}"
            self.questions[(dispatch_id, student.pseudo)] = new[0]

    def question(self, dispatch: dict, student: _Student) -> Any:
        """A referência da mensagem da questão: o `message_id` (Telegram) ou a `ConsoleLine`."""
        return self.questions[(dispatch["dispatch_id"], student.pseudo)]

    def prompts(self, dispatch: dict, student: _Student) -> list[Any]:
        """Os pedidos de campo em resposta à questão do disparo, em ordem de envio."""
        question = self.question(dispatch, student)
        if self.mode == "telegram":
            return [m.message_id for m in self.rich.messages(self.bot, student.chat)
                    if m.force_reply and m.reply_to_message_id == question]
        assert student.console is not None
        return [line for line in student.console.lines() if line.reply_request is not None
                and str(line.reply_to) == question.channel_ref]

    def media(self, student: _Student) -> list[Any]:
        if self.mode == "telegram":
            return [m.message_id for m in self.rich.messages(self.bot, student.chat) if m.upload is not None]
        assert student.console is not None
        return [line for line in student.console.lines() if line.media is not None]

    def texts(self, student: _Student) -> list[str]:
        """O texto (depois do parsing) de tudo o que o aluno recebeu: mensagens, legendas e cada versão
        editada (Telegram), ou as linhas (console)."""
        if self.mode == "console":
            assert student.console is not None
            return [line.text for line in student.console.lines()]
        messages = self.rich.messages(self.bot, student.chat)
        edits = self.rich.edits(self.bot, student.chat)
        return ([m.plain_text or "" for m in messages] + [m.caption or "" for m in messages]
                + [e.after.plain_text or "" for e in edits])

    def occurrences(self, student: _Student, fragment: str) -> int:
        """Quantas vezes `fragment` chegou ao aluno: mensagens e legendas que o contêm, e edições que o
        trouxeram (Telegram), ou linhas (console)."""
        if self.mode == "console":
            assert student.console is not None
            return sum(fragment in line.text for line in student.console.lines())
        messages = self.rich.messages(self.bot, student.chat)
        count = sum(fragment in (m.plain_text or "") or fragment in (m.caption or "") for m in messages)
        count += sum(fragment in (e.after.plain_text or "") and fragment not in (e.before.plain_text or "")
                     for e in self.rich.edits(self.bot, student.chat))
        return count

    def wait_text(self, student: _Student, fragment: str, at_least: int = 1) -> None:
        self.wait(lambda: self.occurrences(student, fragment) >= at_least)

    # -- entradas do aluno ---------------------------------------------------------------------

    def _mark(self, student: _Student) -> int:
        return self.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates WHERE bot_id = %s "
                                "AND chat_id = %s", (self.bot_row, student.chat)).fetchone()[0]

    def _row_after(self, student: _Student, mark: int, *, processed: bool) -> dict:
        def ready() -> dict | None:
            row = _one(self.sql, _ROW_COLUMNS + " WHERE u.bot_id = %s AND u.chat_id = %s AND u.id > %s "
                       "ORDER BY u.id LIMIT 1", (self.bot_row, student.chat, mark))
            return row if row is not None and (not processed or row["state"] == "processed") else None

        return self.wait(ready)

    def update(self, update_id: int) -> dict | None:
        return _one(self.sql, _ROW_COLUMNS + " JOIN bots b ON b.id = u.bot_id WHERE b.telegram_bot_id = %s "
                    "AND u.update_id = %s", (self.bot, update_id))

    def processed(self, update_id: int) -> dict:
        return self.wait(lambda: (row := self.update(update_id)) is not None and row["state"] == "processed"
                         and row)

    def reply(self, student: _Student, target: Any, text: str, *, processed: bool = True) -> dict:
        """Um texto em resposta ("responder") a `target`; devolve a linha do update (processada)."""
        mark = self._mark(student)
        if self.mode == "telegram":
            self.rich.push_text(self.bot, student.user, text, reply_to_message_id=target)
        else:
            assert student.console is not None
            student.console.reply(target, text)
        return self._row_after(student, mark, processed=processed)

    def say(self, student: _Student, text: str) -> dict:
        """Um texto sem "responder"."""
        mark = self._mark(student)
        if self.mode == "telegram":
            self.rich.push_text(self.bot, student.user, text)
        else:
            assert student.console is not None
            student.console.say(text)
        return self._row_after(student, mark, processed=True)

    def dont_know(self, dispatch: dict, student: _Student) -> tuple[Any, dict]:
        """O "Não sei": o botão do teclado corrente (Telegram) ou o `/naosei` em resposta à questão
        (console); devolve o toque (ou `None`) e a linha do update."""
        question = self.question(dispatch, student)
        mark = self._mark(student)
        if self.mode == "telegram":
            message = self.rich.current(self.bot, student.chat, question)
            data = [b.data for b in message.buttons() if b.label == DONT_KNOW_BUTTON]
            assert len(data) == 1
            tap = self.rich.tap(self.bot, student.user, question, data[0])
            return tap, self._row_after(student, mark, processed=True)
        assert student.console is not None
        student.console.dont_know(question)
        return None, self._row_after(student, mark, processed=True)

    # -- leituras ------------------------------------------------------------------------------

    def delivery(self, dispatch: dict, student: _Student) -> dict:
        row = _one(self.sql, _DELIVERY_SQL, (dispatch["dispatch_id"], student.pseudo))
        assert row is not None
        return row

    def submissions(self, delivery: dict) -> list[dict]:
        return _all(self.sql, _SUBMISSIONS_SQL, (delivery["id"],))

    def wait_submissions(self, delivery: dict, outcomes: list[str]) -> list[dict]:
        def ready() -> list[dict] | None:
            subs = self.submissions(delivery)
            return subs if [s["outcome"] for s in subs] == outcomes else None

        return self.wait(ready)

    def grade_calls(self) -> list[Any]:
        return self.engine_fake.calls("grade")

    def last_seq(self) -> int:
        return self.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]

    def events(self, cursor: int, *types: str) -> list[dict]:
        items = list_events(self.client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
        return [{k: v for k, v in e.items() if k != "seq"} for e in items]

    def dispatch_state(self, dispatch: dict) -> str:
        return get_dispatch(self.client, ADMIN_TOKEN, self.code, dispatch["dispatch_id"])["state"]

    def answer_text(self, tap: Any) -> str | None:
        found = self.wait(lambda: [a for a in self.rich.answers(self.bot) if a.callback_id == tap.callback_id])
        assert len(found) == 1
        return found[0].text


def _assert_as_engine(submission: dict, response: dict) -> None:
    """a submissão guarda a validade, a nota, as notas por parte e os erros de formato
    (e o resto) como o motor devolveu."""
    assert submission["valid"] is response["valid"]
    assert submission["score"] == response["score"]
    assert submission["partial_scores"] == response["partial_scores"]
    assert submission["format_errors"] == response["format_errors"]
    assert submission["submitted_answers"] == response["submitted_answers"]
    assert submission["feedback"] == response["feedback"]
    assert submission["params"] == response["params"]
    assert submission["correct_answers"] == response["correct_answers"]
    assert (submission["engine_mismatch"], submission["pl_mismatch"]) == (
        response["version_mismatch"]["engine"], response["version_mismatch"]["pl"])


def _assert_number_event(env: _Env, event: dict, dispatch: dict, delivery: dict, submission: dict,
                         student: _Student, at: datetime, *, score: float, normalized: bool) -> None:
    """O `submission.graded` de um campo numérico: tudo exato, menos `ratio` e `residual` (só o tipo)."""
    data = dict(event["data"])
    parts = data.pop("parts")
    assert {k: v for k, v in event.items() if k != "data"} == {
        "type": "submission.graded", "occurred_at": iso(at), "actor_token_id": None}
    assert data == {"offering_id": env.offering["offering_id"], "dispatch_id": dispatch["dispatch_id"],
                    "delivery_id": delivery["delivery_uuid"], "submission_id": submission["submission_uuid"],
                    "question_ref_id": dispatch["question"]["question_ref_id"], "person": student.pseudo,
                    "valid": True, "late": False, "normalized": normalized, "score": score}
    assert len(parts) == 1
    assert {k: parts[0][k] for k in ("index", "score", "choices")} == {"index": 0, "score": score, "choices": []}
    assert set(parts[0]) == {"index", "score", "choices", "ratio", "residual"}
    for key in ("ratio", "residual"):
        assert parts[0][key] is None or isinstance(parts[0][key], (int, float))


def _marked_feedback(text: str, verdict: str, marks: dict[str, tuple[str, bool]]) -> bool:
    """O feedback de nota parcial (o feedback dos textos fixos ao aluno): o feedback da
    múltipla traz o veredito seguido da nota (`verdict` já com " · nota N%") e, logo depois do texto de
    cada alternativa escolhida, a marca ✅ (entre as corretas) ou ❌. `marks`: chave → (texto, correta)."""
    if verdict not in text:
        return False
    return all(re.search(rf"\({re.escape(key)}\) {re.escape(label)} {MARK_RIGHT if right else MARK_WRONG}", text)
               is not None for key, (label, right) in marks.items())


def _assert_no_english(env: _Env, student: _Student) -> None:
    for text in env.texts(student):
        for word in ENGLISH:
            assert word not in text, f"texto em inglês chegou ao aluno ({word!r})"


@pytest.fixture
def make_env(client: Any, sql: Any, clock: Any, rich_bot_api: Any, engine_fake: Any, repo_fetcher: Any,
             settings: Any, db_url: str, bot_worker_thread: Any, worker_thread: Any) -> Iterator[Callable[..., _Env]]:
    fx = {"client": client, "sql": sql, "clock": clock, "rich_bot_api": rich_bot_api, "engine_fake": engine_fake,
          "repo_fetcher": repo_fetcher, "settings": settings, "db_url": db_url,
          "bot_worker_thread": bot_worker_thread, "worker_thread": worker_thread}
    made: list[_Env] = []

    def make(*, mode: str = "telegram", students: int = 1, label: str = "digitada") -> _Env:
        env = _Env(fx, mode=mode, students=students, label=label)
        made.append(env)
        return env

    yield make
    bot_worker_thread.stop()
    worker_thread.stop()
    for env in made:
        env.close()
        assert env.default_channel.calls == [], "mensagem sem rota chegou ao canal padrão"


def _number_question(correct: float) -> Any:
    return preset("number", fields=(number_field(correct=correct),))


# ---------------------------------------------------------------------------------------------
# Pedido do campo, vírgula e erros de formato


@pytest.mark.parametrize("mode", MODES)
def test_field_prompt_and_decimal_comma_normalized_for_number(make_env: Any, mode: str) -> None:
    """Pedido do campo e vírgula decimal normalizada no número.

    - o pedido do campo sai em resposta à questão, com "Campo 1 de 1", o rótulo, o sufixo e "digite a
      resposta"; no Telegram, com a interface de resposta (`ForceReply`) e a dica do motor (1 a 64) no
      `input_field_placeholder`; no console, numa linha citável com o pedido de resposta (a dica) e a
      referência à questão;
    - `3,14` em resposta ao pedido: a linha literal gravada (`3,14`, a resposta ao pedido),
      `answer_recorded`; o motor recebe `"3.14"`; a submissão guarda `3,14` como entrada literal e
      `normalized_fields = ["resposta"]`, com a validade, a nota e as notas por parte do motor; a entrega
      `answered`; o feedback ecoa `3,14` com ✅; o `submission.graded` com `normalized = true`."""
    env = make_env(mode=mode, label="virgula")
    student = env.students[0]
    dispatch = env.open("number", _number_question(3.14))
    question = env.question(dispatch, student)
    [prompt] = env.prompts(dispatch, student)
    if mode == "telegram":
        message = env.rich.current(env.bot, student.chat, prompt)
        assert (message.force_reply, message.placeholder, message.reply_to_message_id) == (
            True, NUMBER_HINT, question)
        prompt_text = message.plain_text or ""
    else:
        assert prompt.channel_ref == str(prompt.message_id)
        assert NUMBER_HINT in json.dumps(prompt.reply_request, ensure_ascii=False)
        prompt_text = prompt.text
    for piece in ("Campo 1 de 1", LABEL, SUFFIX, "digite a resposta"):
        assert piece in prompt_text
    assert all(ANSWER_SENTINEL not in text for text in env.texts(student))
    delivery = env.delivery(dispatch, student)
    assert (delivery["state"], delivery["current_field"]) == ("delivered", 0)

    t1 = env.advance(STEP)
    cursor = env.last_seq()
    grades_before = len(env.grade_calls())
    row = env.reply(student, prompt, "3,14")
    prompt_id = prompt if mode == "telegram" else prompt.message_id
    assert {k: row[k] for k in ("kind", "text", "reply_to_message_id", "received_at", "outcome")} == {
        "kind": "message", "text": "3,14", "reply_to_message_id": prompt_id, "received_at": t1,
        "outcome": "answer_recorded"}
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {"resposta": "3.14"}
    _assert_as_engine(graded, calls[0].response)
    assert {k: graded[k] for k in ("telegram_update_id", "valid", "score", "raw_input", "normalized_fields",
                                   "late", "response_ms", "received_at", "graded_at")} == {
        "telegram_update_id": row["id"], "valid": True, "score": 1.0, "raw_input": {"resposta": "3,14"},
        "normalized_fields": ["resposta"], "late": False, "response_ms": STEP * 1000, "received_at": t1,
        "graded_at": t1}
    answered = env.delivery(dispatch, student)
    assert (answered["state"], answered["answered_at"], answered["current_field"]) == ("answered", t1, None)
    env.wait_text(student, CORRECT)
    assert any("3,14" in text and CORRECT in text for text in env.texts(student))
    [event] = env.events(cursor, "submission.graded")
    _assert_number_event(env, event, dispatch, delivery, graded, student, t1, score=1.0, normalized=True)


@pytest.mark.parametrize("mode", MODES)
def test_comma_with_point_or_two_commas_is_literal_format_error_and_the_field_is_asked_again(
        make_env: Any, mode: str) -> None:
    """Vírgula com ponto ou duas vírgulas: erro de formato literal, e o campo é pedido de novo.

    `1,234.5` e depois `1,2,3` em resposta ao pedido: o motor recebe o texto literal (sem normalização);
    cada submissão `graded` com `valid = false`, sem nota, os erros originais do PL em inglês (`html` e
    `text`) guardados e `normalized_fields` vazio; o aluno recebe a explicação em português ("Resposta não
    aceita (não conta como tentativa): Não consegui ler a resposta como número. …") e um novo pedido do
    campo (com a dica), sem nenhum texto em inglês; a entrega continua `delivered`, no mesmo campo, e sem
    evento. Uma resposta válida depois (`6.5`, em resposta ao último pedido) é corrigida normalmente (✅)."""
    env = make_env(mode=mode, label="formato")
    student = env.students[0]
    dispatch = env.open("number", preset("number"))
    delivery = env.delivery(dispatch, student)
    cursor = env.last_seq()
    grades_before = len(env.grade_calls())
    explanation = FORMAT_ERROR_PREFIX + NUMBER_UNREADABLE
    outcomes: list[str] = []
    for n, literal in enumerate(("1,234.5", "1,2,3"), start=1):
        prompts = env.prompts(dispatch, student)
        assert len(prompts) == n
        row = env.reply(student, prompts[-1], literal)
        assert (row["text"], row["outcome"]) == (literal, "answer_recorded")
        outcomes.append("graded")
        invalid = env.wait_submissions(delivery, outcomes)[-1]
        call = env.grade_calls()[grades_before + n - 1]
        assert call.request["raw_submitted_answers"] == {"resposta": literal}
        _assert_as_engine(invalid, call.response)
        assert {k: invalid[k] for k in ("valid", "score", "raw_input", "normalized_fields", "format_errors",
                                        "telegram_update_id")} == {
            "valid": False, "score": None, "raw_input": {"resposta": literal}, "normalized_fields": [],
            "format_errors": {"resposta": {"html": NUMBER_INVALID_HTML, "text": NUMBER_INVALID_TEXT}},
            "telegram_update_id": row["id"]}
        env.wait_text(student, explanation, at_least=n)
        env.wait(lambda: len(env.prompts(dispatch, student)) == n + 1)
        still = env.delivery(dispatch, student)
        assert (still["state"], still["current_field"], still["parts"]) == ("delivered", 0, {})
    if mode == "telegram":
        newest = env.rich.current(env.bot, student.chat, env.prompts(dispatch, student)[-1])
        assert (newest.force_reply, newest.placeholder) == (True, NUMBER_HINT)
    _assert_no_english(env, student)
    assert env.events(cursor, "submission.graded") == []

    row = env.reply(student, env.prompts(dispatch, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    graded = env.wait_submissions(delivery, ["graded", "graded", "graded"])[-1]
    call = env.grade_calls()[grades_before + 2]
    assert call.request["raw_submitted_answers"] == {"resposta": "6.5"}
    _assert_as_engine(graded, call.response)
    assert (graded["valid"], graded["score"], graded["normalized_fields"]) == (True, 1.0, [])
    assert env.delivery(dispatch, student)["state"] == "answered"
    env.wait_text(student, CORRECT)
    assert len(env.events(cursor, "submission.graded")) == 1


def test_integer_and_text_fields_do_not_normalize_the_comma(make_env: Any) -> None:
    """Com um disparo de inteiro e outro de texto abertos, cada texto
    em resposta ao pedido do seu campo: `4,2` no inteiro vai literal ao motor, sem normalização, e volta
    com o erro de formato do inteiro traduzido; `azul,claro` no texto vai literal e é corrigido (❌, nota
    0), também sem normalização."""
    env = make_env(label="naonormaliza")
    student = env.students[0]
    integer = env.open("integer", preset("integer"))
    text = env.open("text", preset("text"))
    int_delivery, text_delivery = env.delivery(integer, student), env.delivery(text, student)
    grades_before = len(env.grade_calls())

    row = env.reply(student, env.prompts(integer, student)[-1], "4,2")
    assert row["outcome"] == "answer_recorded"
    [invalid] = env.wait_submissions(int_delivery, ["graded"])
    call = env.grade_calls()[grades_before]
    assert call.request["raw_submitted_answers"] == {"resposta": "4,2"}
    _assert_as_engine(invalid, call.response)
    assert {k: invalid[k] for k in ("valid", "raw_input", "normalized_fields", "format_errors")} == {
        "valid": False, "raw_input": {"resposta": "4,2"}, "normalized_fields": [],
        "format_errors": {"resposta": {"html": INTEGER_INVALID_HTML, "text": INTEGER_INVALID_TEXT}}}
    env.wait_text(student, FORMAT_ERROR_PREFIX + INTEGER_UNREADABLE)
    assert env.delivery(integer, student)["state"] == "delivered"

    row = env.reply(student, env.prompts(text, student)[-1], "azul,claro")
    assert row["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(text_delivery, ["graded"])
    call = env.grade_calls()[grades_before + 1]
    assert call.request["raw_submitted_answers"] == {"resposta": "azul,claro"}
    _assert_as_engine(graded, call.response)
    assert {k: graded[k] for k in ("valid", "score", "raw_input", "normalized_fields")} == {
        "valid": True, "score": 0.0, "raw_input": {"resposta": "azul,claro"}, "normalized_fields": []}
    assert env.delivery(text, student)["state"] == "answered"
    env.wait_text(student, INCORRECT)
    _assert_no_english(env, student)


@pytest.mark.parametrize("mode", MODES)
def test_empty_or_blank_text_goes_literal_and_the_engine_decides(make_env: Any, mode: str) -> None:
    """Texto vazio ou em branco; fidelidade ao PL; a regra: nada é aparado, e todo o resto
    vai literal; a tradução dos erros do PL para o campo `text`.


    Num campo de texto (`allow_blank` falso, `remove-leading-trailing` falso, como no padrão do PL):
    - `""` em resposta ao pedido vai ao motor como `""` (presente, nunca como campo ausente): o
      `pl-string-input` recusa ("Invalid format. The submitted answer was left blank."), a submissão
      `graded` guarda `valid = false`, sem nota e com o erro original; o aluno recebe "Resposta não aceita
      (não conta como tentativa): A resposta está em branco.", sem inglês, e um novo pedido do campo; a
      entrega continua `delivered`, no mesmo campo, sem parte e sem evento (não conta tentativa);
    - `"   "` (só espaços) em resposta ao novo pedido vai ao motor como `"   "`, sem ser aparado pelo
      sistema: o PL não o toma por branco (só apara com `remove-leading-trailing`) e o corrige (nota 0,
      ❌), e a entrega fica `answered`, com um `submission.graded`.
    Controle de que nada é normalizado: em cada caso, a entrada literal gravada (`raw_input`) é igual à
    enviada ao motor, e `normalized_fields` fica vazio."""
    env = make_env(mode=mode, label="textovazio")
    student = env.students[0]
    dispatch = env.open("text", preset("text"))
    delivery = env.delivery(dispatch, student)
    cursor = env.last_seq()
    grades_before = len(env.grade_calls())

    [prompt] = env.prompts(dispatch, student)
    row = env.reply(student, prompt, "")
    assert (row["kind"], row["text"], row["outcome"]) == ("message", "", "answer_recorded")
    [blank] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {"resposta": ""}
    _assert_as_engine(blank, calls[0].response)
    assert {k: blank[k] for k in ("valid", "score", "raw_input", "normalized_fields", "format_errors",
                                  "telegram_update_id")} == {
        "valid": False, "score": None, "raw_input": {"resposta": ""}, "normalized_fields": [],
        "format_errors": {"resposta": {"html": TEXT_BLANK, "text": TEXT_BLANK}},
        "telegram_update_id": row["id"]}
    env.wait_text(student, FORMAT_ERROR_PREFIX + BLANK)
    env.wait(lambda: len(env.prompts(dispatch, student)) == 2)
    still = env.delivery(dispatch, student)
    assert (still["state"], still["current_field"], still["parts"], still["answered_at"]) == (
        "delivered", 0, {}, None)
    assert env.events(cursor, "submission.graded") == []
    _assert_no_english(env, student)

    t1 = env.advance(STEP)
    row = env.reply(student, env.prompts(dispatch, student)[-1], "   ")
    assert (row["text"], row["outcome"]) == ("   ", "answer_recorded")
    graded = env.wait_submissions(delivery, ["graded", "graded"])[1]
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 2 and calls[1].request["raw_submitted_answers"] == {"resposta": "   "}
    _assert_as_engine(graded, calls[1].response)
    assert {k: graded[k] for k in ("valid", "score", "raw_input", "normalized_fields", "format_errors",
                                   "telegram_update_id")} == {
        "valid": True, "score": 0.0, "raw_input": {"resposta": "   "}, "normalized_fields": [],
        "format_errors": {}, "telegram_update_id": row["id"]}
    answered = env.delivery(dispatch, student)
    assert (answered["state"], answered["answered_at"], answered["current_field"]) == ("answered", t1, None)
    env.wait_text(student, INCORRECT)
    assert env.occurrences(student, FORMAT_ERROR_PREFIX) == 1
    assert len(env.events(cursor, "submission.graded")) == 1
    _assert_no_english(env, student)


# ---------------------------------------------------------------------------------------------
# Ligação do texto ao campo


@pytest.mark.parametrize("mode", MODES)
def test_reply_to_question_media_prompt_or_fallback_binds_to_the_current_field(make_env: Any, mode: str) -> None:
    """O texto por "responder" vai ao campo corrente. Numa questão de número com figura, o texto por
    "responder" à mídia, à questão e ao pedido vai ao campo corrente (cada um vira uma submissão da
    entrega, com a entrada literal); no console, onde as edições chegam como linhas novas (papel
    `fallback`, com a referência à questão), a resposta a um `fallback` também. As inválidas (`abc`,
    `1/0`) voltam com a explicação traduzida ("A fração tem divisão por zero."); a última (`6,5`, com a
    vírgula) é corrigida com ✅ e normalizada."""
    env = make_env(mode=mode, label="ligacao")
    student = env.students[0]
    dispatch = env.open("with_media", preset("with_media"))
    delivery = env.delivery(dispatch, student)
    question = env.question(dispatch, student)
    [media] = env.media(student)
    grades_before = len(env.grade_calls())
    literals: list[str] = []

    def answer(target: Any, literal: str) -> dict:
        row = env.reply(student, target, literal)
        assert (row["text"], row["outcome"]) == (literal, "answer_recorded")
        literals.append(literal)
        subs = env.wait_submissions(delivery, ["graded"] * len(literals))
        assert (subs[-1]["raw_input"], subs[-1]["telegram_update_id"]) == ({"resposta": literal}, row["id"])
        call = env.grade_calls()[grades_before + len(literals) - 1]
        _assert_as_engine(subs[-1], call.response)
        return subs[-1]

    assert answer(media, "abc")["valid"] is False
    env.wait_text(student, FORMAT_ERROR_PREFIX + NUMBER_UNREADABLE)
    assert answer(question, "1/0")["valid"] is False
    env.wait_text(student, FORMAT_ERROR_PREFIX + DIVISION_BY_ZERO)
    if mode == "console":
        assert student.console is not None
        fallbacks = [line for line in student.console.lines() if line.reply_to is not None
                     and str(line.reply_to) == question.channel_ref and line.reply_request is None
                     and line.media is None and "Observe a figura." in line.text]
        assert fallbacks, "nenhuma edição chegou como linha nova no console"
        assert answer(fallbacks[-1], "x")["valid"] is False
    last = answer(env.prompts(dispatch, student)[-1], "6,5")
    assert (last["valid"], last["score"], last["normalized_fields"]) == (True, 1.0, ["resposta"])
    assert env.grade_calls()[-1].request["raw_submitted_answers"] == {"resposta": "6.5"}
    assert env.delivery(dispatch, student)["state"] == "answered"
    env.wait_text(student, CORRECT)
    _assert_no_english(env, student)


@pytest.mark.parametrize("mode", MODES)
def test_text_without_reply_goes_to_the_single_open_typeable_field_counting_only_open_dispatches(
        make_env: Any, mode: str) -> None:
    """O texto sem "responder" vai ao único campo digitável aberto (contando só os disparos abertos).

    Dois disparos de número (A e C) abertos; C respondido pelo pedido. Os dois fecham pelo horário (A
    fica `no_answer`, com o campo digitável, e C `answered`); um terceiro (B) abre. Então:
    - texto em resposta à questão de C (encerrada, com o disparo fechado) → "você já respondeu",
      `answer_refused`, submissão `refused/already_answered`;
    - texto em resposta a uma mensagem desconhecida → segue a regra sem "responder";
    - texto sem "responder" → o único campo digitável aberto é o de B (A não conta: disparo fechado) → a
      submissão é de B (a primeira, `abc`, com erro de formato; a segunda, `6.5`, ✅), e A continua sem
      submissão."""
    env = make_env(mode=mode, label="campounico")
    student = env.students[0]
    first = env.open("number", preset("number"))
    answered = env.open("number", preset("number"))
    row = env.reply(student, env.prompts(answered, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    env.wait_submissions(env.delivery(answered, student), ["graded"])
    env.wait(lambda: env.delivery(answered, student)["state"] == "answered")
    closes_at = max(_when(first["closes_at"]), _when(answered["closes_at"]))
    env.advance((closes_at - env.clock.now()).total_seconds() + 1)
    env.wait(lambda: env.dispatch_state(first) == "closed" and env.dispatch_state(answered) == "closed")
    assert env.delivery(first, student)["state"] == "no_answer"

    later = env.open("number", preset("number"))
    c_delivery = env.delivery(answered, student)
    row = env.reply(student, env.question(answered, student), "7")
    assert row["outcome"] == "answer_refused"
    subs = env.wait_submissions(c_delivery, ["graded", "refused"])
    assert (subs[1]["refusal"], subs[1]["telegram_update_id"], subs[1]["valid"]) == (
        "already_answered", row["id"], None)
    env.wait_text(student, ALREADY_ANSWERED)

    b_delivery = env.delivery(later, student)
    unknown = 999_999 if mode == "telegram" else "999999"
    row = env.reply(student, unknown, "abc")
    assert (row["reply_to_message_id"], row["outcome"]) == (999_999, "answer_recorded")
    [invalid] = env.wait_submissions(b_delivery, ["graded"])
    assert (invalid["valid"], invalid["raw_input"], invalid["telegram_update_id"]) == (
        False, {"resposta": "abc"}, row["id"])
    row = env.say(student, "6.5")
    assert (row["text"], row["reply_to_message_id"], row["outcome"]) == ("6.5", None, "answer_recorded")
    graded = env.wait_submissions(b_delivery, ["graded", "graded"])[1]
    _assert_as_engine(graded, env.grade_calls()[-1].response)
    assert (graded["valid"], graded["score"], graded["telegram_update_id"]) == (True, 1.0, row["id"])
    assert env.delivery(later, student)["state"] == "answered"
    assert env.submissions(env.delivery(first, student)) == []
    assert env.delivery(first, student)["state"] == "no_answer"


def test_ambiguous_or_no_typeable_field_text_becomes_a_free_message(make_env: Any) -> None:
    """O texto ambíguo ou sem campo digitável vira mensagem livre.

    - só uma escolha com botões aberta (nenhum campo digitável): o texto sem "responder" vira mensagem
      livre (`free_message`, `ambiguous = false`), com a resposta fixa `FREE_MESSAGE_REPLY`, sem
      submissão;
    - dois campos de número abertos: o texto sem "responder" vira mensagem livre ambígua
      (`free_message_ambiguous`, `ambiguous = true`) com `USE_REPLY` ("use Responder"), mesmo dentro do
      intervalo da resposta fixa anterior (limitador por tipo), e nenhuma das duas entregas recebe
      submissão."""
    env = make_env(label="livre")
    student = env.students[0]
    choice = env.open("single_choice", preset("single_choice"))
    row = env.say(student, "Oi, tudo bem?")
    assert row["outcome"] == "free_message"
    env.wait_text(student, FREE_MESSAGE_REPLY)
    assert env.submissions(env.delivery(choice, student)) == []

    one = env.open("number", preset("number"))
    two = env.open("number", preset("number"))
    row = env.say(student, "6.5")
    assert row["outcome"] == "free_message_ambiguous"
    env.wait_text(student, USE_REPLY)
    assert env.submissions(env.delivery(one, student)) == []
    assert env.submissions(env.delivery(two, student)) == []
    rows = _all(env.sql, "SELECT f.ambiguous, f.replied, u.update_id FROM free_messages f "
                "JOIN telegram_updates u ON u.id = f.telegram_update_id WHERE f.delivery_endpoint_id = %s "
                "ORDER BY f.id", (student.endpoint_row,))
    assert [(r["ambiguous"], r["replied"]) for r in rows] == [(False, True), (True, True)]
    assert env.occurrences(student, FREE_MESSAGE_REPLY) == 1 and env.occurrences(student, USE_REPLY) == 1


def test_message_without_text_bound_to_a_field_and_edited_messages_change_nothing(make_env: Any) -> None:
    """Mensagem sem texto ligada a um campo e mensagens editadas não mudam nada.

    - uma figurinha em resposta ao pedido do campo → "responda com texto", `answer_not_text`, sem
      submissão nem mensagem livre; o mesmo sem "responder", com um único campo digitável aberto;
    - a edição de uma mensagem do aluno (`edited_message`) nem chega ao sistema (fora do
      `allowed_updates` de toda consulta): nenhuma linha nova, nenhuma submissão; a entrega continua
      `delivered`."""
    env = make_env(label="semtexto")
    student = env.students[0]
    dispatch = env.open("number", preset("number"))
    delivery = env.delivery(dispatch, student)
    prompt = env.prompts(dispatch, student)[-1]
    date = int(env.clock.now().timestamp())
    sticker = {"file_id": "cq-figurinha-1", "file_unique_id": "cq-figurinha-1", "type": "regular",
               "width": 512, "height": 512, "is_animated": False, "is_video": False}
    for n, reply in enumerate((prompt, None), start=1):
        message: dict[str, Any] = {"message_id": 800_000 + n, "from": student.user.as_user(),
                                   "chat": student.user.as_private_chat(), "date": date, "sticker": sticker}
        if reply is not None:
            message["reply_to_message"] = {"message_id": reply, "from": {"id": env.bot, "is_bot": True,
                                                                          "first_name": "Bot de teste"},
                                           "chat": student.user.as_private_chat(), "date": date,
                                           "text": "Campo"}
        uid = env.rich.push_update(env.bot, {"message": message})
        row = env.processed(uid)
        assert (row["kind"], row["text"], row["reply_to_message_id"], row["outcome"]) == (
            "message", None, reply, "answer_not_text")
        env.wait_text(student, REPLY_WITH_TEXT, at_least=n)
    assert env.submissions(delivery) == []
    assert env.sql.execute("SELECT count(*) FROM free_messages").fetchone()[0] == 0

    rows_before = env.sql.execute("SELECT count(*) FROM telegram_updates").fetchone()[0]
    edited = {"message_id": 800_010, "from": student.user.as_user(), "chat": student.user.as_private_chat(),
              "date": date, "edit_date": date + 1, "text": "6.5"}
    assert env.rich.push_update(env.bot, {"edited_message": edited}) is None
    row = env.say(student, "abc")  # um texto depois, para provar que a consulta seguiu
    assert row["outcome"] == "answer_recorded"
    assert env.sql.execute("SELECT count(*) FROM telegram_updates").fetchone()[0] == rows_before + 1
    assert all("edited_message" not in (c.params.get("allowed_updates") or [])
               for c in env.rich.calls("getUpdates", bot_id=env.bot))
    [invalid] = env.wait_submissions(delivery, ["graded"])
    assert invalid["raw_input"] == {"resposta": "abc"}
    assert env.delivery(dispatch, student)["state"] == "delivered"


def test_chat_that_is_not_the_current_endpoint_is_ignored(make_env: Any) -> None:
    """Chat que não é o endereço vigente (troca de conta com o disparo aberto). Depois da troca de conta (o
    endereço da abertura fica `inactive` e o chat novo vira o vigente), o texto do chat antigo em resposta
    à questão segue o comportamento dos bots (`not_handled`: não é mais aluno vinculado), sem submissão; o texto sem
    "responder" do chat novo chega à entrega pela regra do campo digitável único e é corrigido."""
    env = make_env(label="troca")
    student = env.students[0]
    dispatch = env.open("number", preset("number"))
    delivery = env.delivery(dispatch, student)
    question = env.question(dispatch, student)
    new_user = make_user(699)
    env.introduce(new_user)
    now = env.clock.now()
    env.sql.execute("UPDATE delivery_endpoints SET state = 'inactive', changed_at = %s WHERE id = %s",
                    (now, student.endpoint_row))
    env.sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                    "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                    (student.person_row, env.bot_row, new_user.user_id, now, now))
    row = env.reply(student, question, "6.5")
    assert row["outcome"] == "not_handled"
    assert env.submissions(delivery) == []
    moved = dataclasses.replace(student, user=new_user)
    row = env.say(moved, "6.5")
    assert row["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(delivery, ["graded"])
    assert (graded["valid"], graded["score"], graded["telegram_update_id"]) == (True, 1.0, row["id"])
    assert env.delivery(dispatch, student)["state"] == "answered"


# ---------------------------------------------------------------------------------------------
# Correção pendente


@pytest.mark.parametrize("mode", MODES)
def test_engine_down_sends_delay_notice_once_refuses_new_entries_and_grades_when_back(
        make_env: Any, mode: str) -> None:
    """Motor fora do ar (`EngineUnavailable`).

    Com o motor fora do ar (conexão recusada): a resposta fica gravada (`answer_recorded`), a submissão
    `grading` com a marca de aviso, e o aluno recebe "resposta recebida; a correção chega em instantes"
    **uma** vez, mesmo depois de uma segunda tentativa que também falha; uma nova resposta e o "Não sei"
    durante a correção pendente → "sua resposta está sendo corrigida", submissões
    `refused/grading_pending`, `answer_refused`; o motor volta e a correção sai sem o aluno repetir: a
    submissão `graded` (✅), a entrega `answered`, e o aviso continua uma vez só."""
    env = make_env(mode=mode, label="motorfora")
    student = env.students[0]
    dispatch = env.open("number", preset("number"))
    delivery = env.delivery(dispatch, student)
    env.engine_fake.stop()
    row = env.reply(student, env.prompts(dispatch, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    [pending] = env.wait_submissions(delivery, ["grading"])
    env.wait_text(student, GRADING_DELAYED)
    env.wait(lambda: env.submissions(delivery)[0]["delay_notice_sent"] is True)
    assert (pending["valid"], pending["score"], pending["raw_input"]) == (None, None, {"resposta": "6.5"})

    def grade_attempts() -> int:
        found = env.sql.execute("SELECT coalesce(max(attempts), 0) FROM tasks WHERE task_type = 'submission.grade' "
                                "AND payload @> %s::jsonb", (json.dumps({"submission_row_id": pending["id"]}),))
        return found.fetchone()[0]

    env.advance(10)
    env.wait(lambda: grade_attempts() >= 2)
    assert env.occurrences(student, GRADING_DELAYED) == 1

    row = env.reply(student, env.prompts(dispatch, student)[-1], "7")
    assert row["outcome"] == "answer_refused"
    env.wait_text(student, GRADING_IN_PROGRESS)
    tap, row = env.dont_know(dispatch, student)
    assert row["outcome"] == "answer_refused"
    if tap is not None:
        assert env.answer_text(tap) == GRADING_IN_PROGRESS
    else:
        env.wait_text(student, GRADING_IN_PROGRESS, at_least=2)
    subs = env.wait_submissions(delivery, ["grading", "refused", "refused"])
    assert [(s["refusal"], s["valid"]) for s in subs[1:]] == [("grading_pending", None), ("grading_pending", None)]
    assert env.delivery(dispatch, student)["state"] == "delivered"

    env.engine_fake.start()
    env.advance(15)
    subs = env.wait_submissions(delivery, ["graded", "refused", "refused"])
    graded = subs[0]
    _assert_as_engine(graded, env.grade_calls()[-1].response)
    assert (graded["valid"], graded["score"], graded["raw_input"], graded["delay_notice_sent"]) == (
        True, 1.0, {"resposta": "6.5"}, True)
    assert env.grade_calls()[-1].request["raw_submitted_answers"] == {"resposta": "6.5"}
    assert env.delivery(dispatch, student)["state"] == "answered"
    env.wait_text(student, CORRECT)
    assert env.occurrences(student, GRADING_DELAYED) == 1


@pytest.mark.parametrize("mode", MODES)
def test_pending_grading_that_ends_in_format_error_clears_the_mark_and_asks_again(make_env: Any, mode: str) -> None:
    """A correção pendente de `1,2,3` (motor fora do ar) sai, com
    o motor de volta, como erro de formato: a submissão `graded` com `valid = false` e o erro original, a
    marca de correção pendente some (nenhuma submissão `grading`), a entrega continua `delivered`, o aluno
    recebe a explicação e um novo pedido do campo, e uma resposta válida depois é corrigida."""
    env = make_env(mode=mode, label="pendenteformato")
    student = env.students[0]
    dispatch = env.open("number", preset("number"))
    delivery = env.delivery(dispatch, student)
    env.engine_fake.stop()
    row = env.reply(student, env.prompts(dispatch, student)[-1], "1,2,3")
    assert row["outcome"] == "answer_recorded"
    env.wait_submissions(delivery, ["grading"])
    env.wait_text(student, GRADING_DELAYED)
    env.engine_fake.start()
    env.advance(15)
    [invalid] = env.wait_submissions(delivery, ["graded"])
    assert env.grade_calls()[-1].request["raw_submitted_answers"] == {"resposta": "1,2,3"}
    _assert_as_engine(invalid, env.grade_calls()[-1].response)
    assert (invalid["valid"], invalid["normalized_fields"], invalid["format_errors"]) == (
        False, [], {"resposta": {"html": NUMBER_INVALID_HTML, "text": NUMBER_INVALID_TEXT}})
    env.wait_text(student, FORMAT_ERROR_PREFIX + NUMBER_UNREADABLE)
    env.wait(lambda: len(env.prompts(dispatch, student)) == 2)
    still = env.delivery(dispatch, student)
    assert (still["state"], still["current_field"]) == ("delivered", 0)
    row = env.reply(student, env.prompts(dispatch, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    graded = env.wait_submissions(delivery, ["graded", "graded"])[1]
    assert (graded["valid"], graded["score"]) == (True, 1.0)
    assert env.delivery(dispatch, student)["state"] == "answered"
    _assert_no_english(env, student)


# ---------------------------------------------------------------------------------------------
# Recusas do texto ligado a uma entrega


def test_replies_to_not_delivered_failed_or_answered_deliveries_are_refused(make_env: Any) -> None:
    """Recusas de entrega (as recusas do toque valem para o texto).

    - entrega `not_delivered` (a figura saiu, e o envio da questão teve um 400 definitivo:
      `channel_refused`, com o pedido do campo cancelado): o texto em resposta à figura → "esta questão
      não está disponível", `answer_refused`, submissão `refused/unavailable`;
    - entrega `question_failed` (correção `broken`): o texto em resposta à questão → a mesma recusa;
    - entrega encerrada (`answered`): o texto em resposta à questão → "você já respondeu",
      `refused/already_answered`; nenhuma dessas recusas chama o motor."""
    env = make_env(label="recusas")
    student = env.students[0]
    env.rich.fail("sendRichMessage", status=400, description="Bad Request: message is too long",
                  bot_id=env.bot, times=1)
    lost = env.settle(env.confirm(env.prepare("with_media", preset("with_media"))), {"not_delivered": 1})
    lost_delivery = env.delivery(lost, student)
    assert (lost_delivery["state"], lost_delivery["undelivered_reason"]) == ("not_delivered", "channel_refused")
    [media] = env.media(student)
    grades_before = len(env.grade_calls())
    row = env.reply(student, media, "6.5")
    assert row["outcome"] == "answer_refused"
    [refused] = env.wait_submissions(lost_delivery, ["refused"])
    assert (refused["refusal"], refused["telegram_update_id"]) == ("unavailable", row["id"])
    env.wait_text(student, UNAVAILABLE)

    broken = env.open("broken_on_grade", preset("broken_on_grade"))
    broken_delivery = env.delivery(broken, student)
    row = env.reply(student, env.prompts(broken, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    env.wait_submissions(broken_delivery, ["broken"])
    env.wait(lambda: env.delivery(broken, student)["state"] == "question_failed")
    row = env.reply(student, env.question(broken, student), "7")
    assert row["outcome"] == "answer_refused"
    subs = env.wait_submissions(broken_delivery, ["broken", "refused"])
    assert (subs[1]["refusal"], subs[1]["telegram_update_id"]) == ("unavailable", row["id"])
    env.wait_text(student, UNAVAILABLE, at_least=2)

    done = env.open("number", preset("number"))
    done_delivery = env.delivery(done, student)
    env.reply(student, env.prompts(done, student)[-1], "6.5")
    env.wait_submissions(done_delivery, ["graded"])
    env.wait(lambda: env.delivery(done, student)["state"] == "answered")
    calls = len(env.grade_calls())
    row = env.reply(student, env.question(done, student), "7")
    assert row["outcome"] == "answer_refused"
    subs = env.wait_submissions(done_delivery, ["graded", "refused"])
    assert (subs[1]["refusal"], subs[1]["telegram_update_id"]) == ("already_answered", row["id"])
    env.wait_text(student, ALREADY_ANSWERED)
    assert len(env.grade_calls()) == calls == grades_before + 2
    assert env.delivery(done, student)["state"] == "answered"


def test_reply_to_media_while_the_question_is_queued_is_retried_without_recording(make_env: Any) -> None:
    """o texto em resposta à figura de uma entrega `pending` (a `question`
    segurada na fila por 429) faz o tratador `telegram.update` pedir `RetryTaskLater(2)`: nada é gravado
    (nem parte, nem submissão, nem desfecho; o update continua `pending`, e a tarefa volta a `pending` com
    uma tentativa). Depois do envio aceito da questão, o reprocessamento grava a resposta no campo
    corrente, e a correção leva a entrega a `answered`."""
    env = make_env(label="mediafila")
    student = env.students[0]
    env.rich.fail("sendRichMessage", status=429, retry_after=1, bot_id=env.bot, times=None)
    dispatch = env.confirm(env.prepare("with_media", preset("with_media")))
    env.wait(lambda: env.media(student))
    env.wait(lambda: env.sql.execute(
        "SELECT count(*) FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id JOIN dispatches x "
        "ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid AND o.delivery_role = 'question' "
        "AND o.state = 'pending' AND o.attempts >= 1", (dispatch["dispatch_id"],)).fetchone()[0] == 1)
    delivery = env.delivery(dispatch, student)
    assert delivery["state"] == "pending"
    [media] = env.media(student)
    row = env.reply(student, media, "6.5", processed=False)
    task = env.wait(lambda: _one(env.sql, "SELECT state, attempts FROM tasks WHERE task_type = 'telegram.update' "
                                 "AND payload @> %s::jsonb AND attempts >= 1 AND state = 'pending'",
                                 (json.dumps({"update_row_id": row["id"]}),)))
    assert task == {"state": "pending", "attempts": 1}
    stored = env.update(row["update_id"])
    assert (stored["state"], stored["outcome"], stored["reply_to_message_id"]) == ("pending", None, media)
    assert env.submissions(delivery) == []
    held = env.delivery(dispatch, student)
    assert (held["state"], held["parts"], held["answered_at"]) == ("pending", {}, None)

    env.rich.clear_failures()
    env.advance(10)
    env.wait(lambda: env.delivery(dispatch, student)["state"] == "delivered")
    env.advance(10)
    final = env.processed(row["update_id"])
    assert final["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(delivery, ["graded"])
    assert (graded["valid"], graded["raw_input"], graded["telegram_update_id"]) == (True, {"resposta": "6.5"},
                                                                                  row["id"])
    env.wait(lambda: env.delivery(dispatch, student)["state"] == "answered")


# ---------------------------------------------------------------------------------------------
# Múltipla digitada e o erro do server.py


def test_typed_multiple_choice_goes_to_the_engine_as_the_literal_text(make_env: Any) -> None:
    """Fidelidade ao PL; as formas da resposta crua aceitas pelo motor falso (a adaptação de canal da
    escolha múltipla digitada).

    Texto em resposta à questão de escolha múltipla (vale como escolha digitada; gabarito {a, c}, crédito
    parcial `net-correct`): o motor recebe o **texto literal**, sem divisão, reordenação nem descarte,
    como o PL o receberia de um formulário com um único valor, e o `pl-checkbox` do PL
    (`set(submitted_key)`, `pl-checkbox.py@a193f72:726-752`) decide. Com a checagem das formas aceitas pelo motor falso
    ligada:
    - `b,a` vai como `"b,a"` (e não `["a","b"]`, nem `b.a` pela vírgula decimal): o PL recusa a `,`
      ("You selected an invalid option: ,"), o aluno recebe "Resposta não aceita (não conta como
      tentativa): Uma das alternativas marcadas não existe nesta questão.", sem eco nem inglês, e a
      entrega continua `delivered`, no mesmo campo, sem parte gravada e sem evento (não conta tentativa);
    - `a b` vai como `"a b"`: o PL recusa o espaço, da mesma forma;
    - `ab` vai como `"ab"`: o PL lê {a, b} e corrige (nota 0, ❌), e a entrega fica `answered`;
    - (outro aluno) `ca` vai como `"ca"` (e não `"ac"`): o PL lê {a, c} e corrige (nota 1, ✅).
    Feedback no formato da nota parcial (o ramo `str` do eco,
    com as marcas por alternativa): para `ab`, "(a) Dois ✅" e "(b) Nove ❌" com "❌ incorreta · nota 0%"; para `ca`, "(a) Dois ✅" e
    "(c) Sete ✅" com "✅ correta · nota 100%", na mesma mensagem.
    Controle de que nenhuma normalização acontece: em cada caso, a entrada literal gravada (`raw_input`)
    é igual à enviada ao motor, e `normalized_fields` fica vazio."""
    env = make_env(students=2, label="multidigitada")
    first, second = env.students
    dispatch = env.open("multiple_choice", preset("multiple_choice"))
    cursor = env.last_seq()
    cases = [(first, "b,a", ","), (first, "a b", " "), (first, "ab", 0.0), (second, "ca", 1.0)]
    seen: dict[str, list[str]] = {first.pseudo: [], second.pseudo: []}
    for student, literal, expected in cases:
        delivery = env.delivery(dispatch, student)
        row = env.reply(student, env.question(dispatch, student), literal)
        assert (row["text"], row["outcome"]) == (literal, "answer_recorded")
        seen[student.pseudo].append("graded")
        submission = env.wait_submissions(delivery, seen[student.pseudo])[-1]
        call = env.grade_calls()[-1]
        assert call.request["raw_submitted_answers"] == {"resposta": literal}
        assert submission["raw_input"] == call.request["raw_submitted_answers"]
        _assert_as_engine(submission, call.response)
        assert (submission["normalized_fields"], submission["telegram_update_id"]) == ([], row["id"])
        if isinstance(expected, str):
            assert (submission["valid"], submission["score"]) == (False, None)
            assert submission["format_errors"] == {"resposta": invalid_option_error(expected)}
            env.wait_text(student, FORMAT_ERROR_PREFIX + INVALID_OPTION, at_least=len(seen[student.pseudo]))
            still = env.delivery(dispatch, student)
            assert (still["state"], still["current_field"], still["parts"]) == ("delivered", 0, {})
            assert env.events(cursor, "submission.graded") == []
        else:
            assert (submission["valid"], submission["score"]) == (True, expected)
            assert submission["submitted_answers"] == {"resposta": literal}
            assert env.delivery(dispatch, student)["state"] == "answered"
    env.wait(lambda: any(_marked_feedback(text, f"{INCORRECT} · {SCORE.format(n=0)}",
                                          {"a": ("Dois", True), "b": ("Nove", False)})
                         for text in env.texts(first)))
    env.wait(lambda: any(_marked_feedback(text, f"{CORRECT} · {SCORE.format(n=100)}",
                                          {"a": ("Dois", True), "c": ("Sete", True)})
                         for text in env.texts(second)))
    assert env.occurrences(second, FORMAT_ERROR_PREFIX) == 0
    assert len(env.events(cursor, "submission.graded")) == 2
    for student in (first, second):
        _assert_no_english(env, student)


def test_server_format_error_outside_the_fields_returns_all_parts(make_env: Any) -> None:
    """o `grade()` do `server.py` acrescenta um erro
    de formato com chave fora de `fields[]`: a submissão `graded` com `valid = false` e o erro original
    (como o motor devolveu), todas as partes limpas e o campo corrente de volta ao primeiro; o aluno recebe
    "A questão recusou a resposta. Confira o formato e envie de novo." (o texto do `server.py` não chega)
    e um novo pedido do primeiro campo."""
    env = make_env(label="servidor")
    student = env.students[0]
    dispatch = env.open("server_format_error", preset("server_format_error"))
    delivery = env.delivery(dispatch, student)
    row = env.reply(student, env.prompts(dispatch, student)[-1], "6.5")
    assert row["outcome"] == "answer_recorded"
    [invalid] = env.wait_submissions(delivery, ["graded"])
    call = env.grade_calls()[-1]
    assert call.request["raw_submitted_answers"] == {"resposta": "6.5"}
    _assert_as_engine(invalid, call.response)
    assert invalid["format_errors"] == {SERVER_FORMAT_ERROR.key: {
        "html": SERVER_FORMAT_ERROR.html, "text": html_to_text(SERVER_FORMAT_ERROR.html)}}
    assert (invalid["valid"], invalid["score"]) == (False, None)
    env.wait_text(student, FORMAT_ERROR_PREFIX + OUTSIDE_FIELDS)
    env.wait(lambda: len(env.prompts(dispatch, student)) == 2)
    reopened = env.delivery(dispatch, student)
    assert (reopened["state"], reopened["current_field"], reopened["parts"]) == ("delivered", 0, {})
    _assert_no_english(env, student)
