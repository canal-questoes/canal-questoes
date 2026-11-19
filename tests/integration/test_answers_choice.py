"""Escolha única e múltipla, confirmação dos toques e feedback, com dois toques seguidos e rápidos
no mesmo teclado.


Contratos: a entrega, a seleção guardada (`delivery_contents.selection`), a submissão, o teclado e o dado
`cq1:`, o roteamento, a validação e a confirmação do toque, a aplicação ao campo, a resposta crua, a
correção, o evento `submission.graded`, os desfechos e os
textos.

Tudo passa pelo caminho real, só com dublês: o oferecimento, o bot e a lista entram pela API; o disparo
é preparado pela API (`dispatches.prepare`), validado pelo worker (`dispatch.validate`, com a obtenção
falsa e o motor falso estrito pelo adaptador HTTP real), confirmado pela API (`dispatches.confirm`,
imediato) e aberto e entregue pelo worker (`dispatch.open`, `delivery.prepare` e a fila de saída pela Bot
API falsa com a extensão do disparo e da resposta, `rich_bot_api`). O aluno toca nos botões pela `rich_bot_api` (`tap` com o
teclado corrente; `tap_raw` com dados obsoletos, forjados, de outra mensagem ou fora de `cq1:`).
O vínculo do aluno (coberto por `test_student_join.py`) é gravado por SQL com as mesmas linhas que o
`/aceito` grava (matrícula `active` com o termo e o endereço `active` no chat), depois de o chat falar
com o bot uma vez (a Bot API falsa só entrega a chats que já a procuraram); o "Olá" desse chat, antes do
vínculo, segue o comportamento dos bots (`not_handled`, conferido como regressão).

Conferências por caso: a linha literal do update, o desfecho, a
confirmação de **todo** toque exatamente uma vez (`answers()`) com o texto fixo, a resposta crua
exata recebida pelo motor falso, a submissão com os campos como o motor devolveu,
a entrega, a edição da mensagem da questão e o evento `submission.graded` exato. O motor
falso confere cada pedido e cada resposta contra o contrato (fixture `engine_fake`), e a Bot API falsa,
protocolo e política (fixture `rich_bot_api`). A sentinela do gabarito nunca chega ao aluno antes do
fechamento.

Os textos esperados são os textos fixos ao aluno, escritos aqui por extenso (não vêm do código). Horários
pelo `FakeClock`, com saltos de até 10 s e a posse do bot conferida a cada salto (a margem de renovação do
worker é de 13 s). Ids internos, tokens, `update_id` e pseudônimos nunca como literal.

Escolhas [I] deste arquivo: o toque válido (`m`, `u`, `s`) e os "sem efeito" são
confirmados **sem texto** (não há texto fixo para eles); a "ação
inexistente" dos toques inválidos é a ação `u` (desmarcar) e a `c` (escolher, reservada) numa escolha única;
nas escolhas, `ratio` e `residual` das partes do evento são nulos (não há razão nem resíduo numérico numa
alternativa).

Regras de resposta e de feedback (a escolha única marca e envia, a entrega sem resposta fica sem
gabarito e o feedback traz a nota):
- a escolha única só **marca** a alternativa (✅, ação `m`, edição só dos botões, sem correção) e a
  resposta sai no "Enviar" (ação `s`), como na múltipla; o teclado da única é `(a)` a `(d)`, "Enviar" e
  "Não sei"; "marcar e enviar" = tocar na alternativa (`answer_toggled`) e no "Enviar" do mesmo teclado
  (a versão dos controles não muda ao marcar); o "Enviar" sem marcação manda o campo **ausente**, como o
  PL; o teclado da entrega `no_answer` também marca e envia;
- a entrega sem resposta no fechamento fica `no_answer` sem o gabarito (`revealed_at` nulo);
- o feedback traz o resultado em três faixas (✅ correta, ◐ parcialmente correta, ❌ incorreta) e a
  nota (`nota N%`, o selo do PL: 100 com nota ≥ 1, ⌊nota × 100⌋ entre 0 e 1, 0 com nota ≤ 0) em toda
  correção válida, e, na múltipla (com qualquer crédito parcial, inclusive `off`), a
  marca ✅/❌ logo depois do texto de cada alternativa escolhida;
- `_release_reader` avança o relógio 2 s depois de soltar o leitor (a espera do `getUpdates` depois
  da falha temporária, nos bots).
O `VERDICT_PARTIAL` e o modelo `SCORE` vêm dos textos fixos ao aluno, por extenso.

No esqueleto, toda função falha no primeiro
`dispatches.prepare` (rota ausente, 404). Alguns casos também dependem de outras partes: a espera da transição, o
`question_failed`, o eco de texto digitado e a escolha única degradada pelo console (`choose`, que é
texto).

"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
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
from tests.helpers.bot_api_dispatch import DISPATCH_ERRORS
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    COLOR_OPTIONS,
    GRADE_BROKEN,
    PRIME_OPTIONS,
    SINGLE_NO_ANSWER,
    multiple_choice_field,
    preset,
    select_count_error,
    single_choice_field,
)
from tests.helpers.logs import capture_logs

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
WINDOW_MINUTES = 5
WORKER_ID = "worker-respostas-escolha"
REPO = "https://repos.cq.test/disciplina/questoes-escolha.git"
COMMIT = "c0ffee12" * 5
ENGINE_TIMEOUT = 10.0
PROCESS_TIMEOUT = 20.0
CLOCK_STEP = 10  # segundos por salto do relógio com o worker no ar
LEASE_MARGIN = 13  # poll 1 s + Telegram 2 s + renovação 10 s (bot_settings dos testes)
STEP = 15  # o "tempo até responder" dos casos (response_ms = 15000)
# Os relatórios e o acompanhamento têm 3 tipos
# de evento próprios, em literal (o teste os exclui do recorte "nenhum evento" e confere o excluído).
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")

CALLBACK = re.compile(r"cq1:([A-Za-z0-9_-]{22}):([cmusn]):([0-9a-z]{1,2}):([0-9a-z]{1,4})(?::([A-Za-z0-9]{1,16}))?")

# Os textos fixos ao aluno, por extenso.
ALREADY_ANSWERED = "Você já respondeu a esta questão."
GRADING_IN_PROGRESS = "Sua resposta está sendo corrigida."
UNAVAILABLE = "Esta questão não está disponível."
PART_ALREADY_ANSWERED = "Esta parte já foi respondida."
QUESTION_FAILED = ("Esta questão teve um problema e não conseguiu corrigir a sua resposta. "
                   "A sua resposta foi guardada.")
FORMAT_ERROR_PREFIX = "Resposta não aceita (não conta como tentativa): "
DONT_KNOW_HOWTO = 'Para responder "Não sei", responda a esta mensagem com /naosei.'
CORRECT = "✅ correta"
INCORRECT = "❌ incorreta"
# O feedback de nota parcial (`VERDICT_PARTIAL` e o modelo `SCORE` = "nota {n}%").
VERDICT_PARTIAL = "◐ parcialmente correta"
CLOSED_NOTICE = "Janela fechada."
# O "No answer was submitted." do PL vira "Escolha uma alternativa."
SINGLE_NO_ANSWER_PT = "Escolha uma alternativa."
FEEDBACK_HEAD = "Sua resposta:"
DONT_KNOW_BUTTON = "Não sei"
SUBMIT_BUTTON = "Enviar"
# "You must select between (\d+) and (\d+) options." → "Marque de {a} a {b} alternativas."
SELECT_BETWEEN_1_4 = "Marque de 1 a 4 alternativas."
# As alternativas dos presets (tests/helpers/engine.py): chave → texto da opção.
COLORS = {o.key: o.html for o in COLOR_OPTIONS}  # a Vermelho, b Azul, c Verde, d Amarelo
PRIMES = {o.key: o.html for o in PRIME_OPTIONS}  # a Dois, b Nove, c Sete, d Dez
# O modelo `FEEDBACK`: "Sua resposta: {echo} · {verdict} · {score}"; o veredito em três faixas
# (✅, ◐ ou ❌).
_ECHO = re.compile(r"Sua resposta:\s*(?P<echo>.*?)\s*·\s*(?:✅|◐|❌)", re.S)

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio


class _NoRouteChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota (bot e endereço), então nada pode
    chegar aqui (conferido no fim)."""

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


def _b36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value %= 36 ** 4
    out = ""
    while value:
        value, rest = divmod(value, 36)
        out = digits[rest] + out
    return out or "0"


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


_DELIVERY_SQL = """
    SELECT d.id, d.delivery_uuid::text AS delivery_uuid, d.state, d.undelivered_reason, d.failure,
           d.current_field, d.render_version, d.controls_version, d.button_token, d.sent_at,
           d.answered_at, d.revealed_at, c.parts, c.selection
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

_UPDATE_SQL = """
    SELECT u.id, u.update_id, u.kind, u.chat_id, u.sender_id, u.text, u.details,
           u.reply_to_message_id, u.received_at, u.state, u.outcome, u.processed_at
      FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
     WHERE b.telegram_bot_id = %s AND u.update_id = %s
"""


class _Env:
    """Um oferecimento ativo com bot, alunos vinculados e o worker no ar (`telegram`: `run_worker` com
    `bots` sobre a `rich_bot_api`; `console`: `run_worker(…, console_routes=True)` com um
    `ConsoleChannel` em memória e a `ConsoleInbox`)."""

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
        self.questions: dict[tuple[str, str], int | ConsoleLine] = {}
        n = next(_counter)
        self.code = f"mc102-{label}-{n}"
        self.offering = create_offering(self.client, ADMIN_TOKEN, code=self.code, course=COURSE, term=TERM,
                                        timezone=ZONE, response_window_minutes=WINDOW_MINUTES)
        self.bot = self.rich.add_bot(f"cq_{label}_{n:03d}_bot")
        register_bot(self.client, ADMIN_TOKEN, self.code, self.rich.token_of(self.bot))
        activate_bot(self.client, ADMIN_TOKEN, self.code)
        self.bot_row = self.sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s",
                                        (self.bot,)).fetchone()[0]
        rows = [{"name": f"Pessoa Resposta Escolha {n:03d} {k}", "ra": f"571{n:05d}{k:02d}",
                 "email": f"resposta.escolha{n}.{k}@turma.example.org"} for k in range(students)]
        import_students(self.client, ADMIN_TOKEN, self.code, rows)
        self.worker = self._start()
        self.students = []
        for k, row in enumerate(rows):
            user = make_user(640 + k)
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

    def restart(self) -> None:
        """Para o worker (com bots) e sobe outro no lugar, como depois de uma queda."""
        assert self.mode == "telegram"
        elapsed = self.fx["bot_worker_thread"].stop_one(self.worker)
        assert elapsed != float("inf"), "o worker não parou"
        self.worker = self._start()

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
        """Avança o relógio em saltos de até 10 s; com bots, espera a posse ter folga depois de cada
        salto (o worker renova quando faltam menos de 13 s)."""
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
        """O vínculo do aluno por SQL: matrícula `active` com o termo 1 e endereço `active` no chat."""
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
        """Prepara pela API e espera a validação pelo worker; devolve o disparo validado."""
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
        """Espera as entregas do disparo nos estados `expected` e a fila delas vazia (pedidos de campo
        enviados); guarda a mensagem da questão de cada aluno; devolve o disparo consultado."""
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
        """Prepara, valida, confirma (imediato) e espera todas as entregas `delivered`."""
        dispatch = self.confirm(self.prepare(qid, question))
        return self.settle(dispatch, {"delivered": len(self.students)})

    def _note_question(self, dispatch_id: str, student: _Student) -> None:
        known = list(self.questions.values())  # a `ConsoleLine` não é hashable (tem o `raw`, um dict)
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
        mid = self.questions[(dispatch["dispatch_id"], student.pseudo)]
        if self.mode == "telegram":
            return self.rich.current(self.bot, student.chat, mid)
        return mid

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

    def update(self, update_id: int) -> dict | None:
        return _one(self.sql, _UPDATE_SQL, (self.bot, update_id))

    def processed(self, update_id: int) -> dict:
        return self.wait(lambda: (row := self.update(update_id)) is not None and row["state"] == "processed"
                         and row)

    def tap(self, student: _Student, message_id: int, data: str, *, raw: bool = False) -> tuple[Any, dict]:
        tap = (self.rich.tap_raw if raw else self.rich.tap)(self.bot, student.user, message_id, data)
        return tap, self.processed(tap.update_id)

    def answer(self, tap: Any) -> Any:
        found = self.wait(lambda: [a for a in self.rich.answers(self.bot) if a.callback_id == tap.callback_id])
        assert len(found) == 1, "toque confirmado mais de uma vez"
        return found[0]

    def answer_calls(self, tap: Any) -> list[Any]:
        return [c for c in self.rich.calls("answerCallbackQuery", bot_id=self.bot)
                if c.params.get("callback_query_id") == tap.callback_id]

    def grade_calls(self) -> list[Any]:
        return self.engine_fake.calls("grade")

    def last_seq(self) -> int:
        return self.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]

    def events(self, cursor: int, *types: str) -> list[dict]:
        items = list_events(self.client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
        return [{k: v for k, v in e.items() if k != "seq"} for e in items]

    def option_hashes(self, qid: str, keys: list[str]) -> list[str]:
        variants = self.engine_fake.issued_variants(qid)
        assert variants, "nenhuma variante emitida"
        options = variants[-1].ir["fields"][0]["options"]
        by_key = {o["key"]: o["content_hash"] for o in options}
        return [by_key[k] for k in keys]

    def graded_event(self, dispatch: dict, delivery: dict, submission: dict, student: _Student, at: datetime,
                     *, score: float, parts: list[dict], late: bool = False) -> dict:
        return {"type": "submission.graded", "occurred_at": iso(at), "actor_token_id": None, "data": {
            "offering_id": self.offering["offering_id"], "dispatch_id": dispatch["dispatch_id"],
            "delivery_id": delivery["delivery_uuid"], "submission_id": submission["submission_uuid"],
            "question_ref_id": dispatch["question"]["question_ref_id"], "person": student.pseudo,
            "valid": True, "late": late, "normalized": False, "score": score, "parts": parts}}

    def chat_texts(self, student: _Student) -> list[str]:
        """Tudo o que o aluno viu no chat: as mensagens (estado atual) e cada versão editada."""
        messages = self.rich.messages(self.bot, student.chat)
        edits = self.rich.edits(self.bot, student.chat)
        texts = [m.text or "" for m in messages] + [m.plain_text or "" for m in messages]
        texts += [m.caption or "" for m in messages]
        texts += [e.after.text or "" for e in edits] + [e.after.plain_text or "" for e in edits]
        return texts


def _assert_as_engine(submission: dict, response: dict) -> None:
    """a submissão guarda o que o motor devolveu."""
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


def _assert_choice_echo(text: str | None, options: Mapping[str, str], chosen: Iterable[str], *,
                        marks: Mapping[str, bool] | None = None) -> None:
    """A mensagem é editada com a alternativa escolhida e ✅ ou ❌: o eco do feedback
    (entre "Sua resposta:" e o veredito) traz a chave e o texto de cada alternativa escolhida, e nem a
    chave nem o texto de nenhuma não escolhida. [I] A forma do eco de uma escolha não está fixada nos
    textos fixos: conferem-se só a presença (a chave como símbolo isolado, o texto da opção) e a
    ausência, sem fixar ordem, separador nem pontuação. O resto da mensagem (o enunciado com todas as
    alternativas, o gabarito revelado) fica fora da conferência.

    Com `marks` (a marca de cada alternativa no feedback): cada escolhida
    de `marks` tem, logo depois do texto dela, a marca ✅ (`True`, chave entre as corretas) ou ❌
    (`False`), e não a outra."""
    match = _ECHO.search(text or "")
    assert match, f"feedback sem 'Sua resposta: {{eco}} · {{veredito}}': {text!r}"
    echo = match.group("echo")
    picked = set(chosen)
    for key, label in options.items():
        has_key = re.search(rf"(?<![0-9A-Za-zÀ-ÿ]){re.escape(key)}(?![0-9A-Za-zÀ-ÿ])", echo) is not None
        if key in picked:
            assert has_key and label in echo, f"o eco {echo!r} não traz a alternativa escolhida ({key}) {label}"
        else:
            assert not has_key and label not in echo, (
                f"o eco {echo!r} traz a alternativa não escolhida ({key}) {label}")
    for key, right in (marks or {}).items():
        assert key in picked, f"marca pedida para a alternativa não escolhida {key}"
        mark, other = ("✅", "❌") if right else ("❌", "✅")
        head = rf"\({re.escape(key)}\)\s*{re.escape(options[key])}\s*"
        assert re.search(head + re.escape(mark), echo), (
            f"o eco {echo!r} não traz {mark} logo depois de ({key}) {options[key]}")
        assert not re.search(head + re.escape(other), echo), (
            f"o eco {echo!r} traz {other} logo depois de ({key}) {options[key]}")


def _button(message: Any, label: str) -> str:
    found = [b.data for b in message.buttons() if b.label == label]
    assert len(found) == 1, f"botão {label!r} ausente ou repetido: {[b.label for b in message.buttons()]}"
    return found[0]


def _labels(message: Any) -> list[list[str]]:
    return [[b.label for b in row] for row in (message.keyboard or ())]


def _parts_of(data: str) -> list[str]:
    assert CALLBACK.fullmatch(data), f"dado fora da gramática cq1: {data!r}"
    return data.split(":")


def _reader_hanging(env: _Env, mark: int) -> bool:
    """Um `getUpdates` depois de `mark` está preso (aberto e fora dos long polls em espera)."""
    waiting = {c.seq for c in env.rich.pending_polls(env.bot)}
    return any(c.seq > mark and c.ended is None and c.seq not in waiting
               for c in env.rich.calls("getUpdates", bot_id=env.bot))


def _hang_reader(env: _Env) -> None:
    """Prende o leitor do bot: nenhum update chega ao sistema até `_release_reader`."""
    calls = env.rich.calls("getUpdates", bot_id=env.bot)
    mark = max(c.seq for c in calls) if calls else 0
    env.rich.fail_network("getUpdates", kind="hang", times=None, bot_id=env.bot)
    env.wait(lambda: _reader_hanging(env, mark))


def _release_reader(env: _Env) -> None:
    """Solta o leitor e avança o relógio 2 s: o `getUpdates` solto falha (conexão fechada sem resposta) e
    o leitor espera 2 s no `FakeClock` antes da próxima consulta, como o leitor
    faz depois de uma falha temporária."""
    env.rich.clear_failures()
    env.rich.release_hangs()
    env.advance(2)


def _data(message: Any, action: str, key: str | None = None) -> str:
    """O dado do único botão de `message` com a ação `action` (e a chave `key`, se dada), pelo dado e não
    pelo rótulo (o rótulo da alternativa marcada da única é `✅ (k)`)."""
    found = [b.data for b in message.buttons()
             if _parts_of(b.data)[2] == action and (key is None or _parts_of(b.data)[5:] == [key])]
    assert len(found) == 1, f"botão {action}:{key} ausente ou repetido: {[b.data for b in message.buttons()]}"
    return found[0]


def _mark_and_submit(env: _Env, student: _Student, message_id: int, key: str) -> tuple[Any, dict, Any, dict]:
    """marca a alternativa `key` da escolha única (`m`, `answer_toggled`) e toca no "Enviar" (`s`)
    do mesmo teclado (marcar não muda a versão dos controles); devolve (marcação, linha, envio, linha)."""
    current = env.rich.current(env.bot, student.chat, message_id)
    mark, mark_row = env.tap(student, message_id, _data(current, "m", key))
    assert mark_row["outcome"] == "answer_toggled", f"marcação de {key}: {mark_row['outcome']}"
    submit, submit_row = env.tap(student, message_id, _data(current, "s"))
    return mark, mark_row, submit, submit_row


def _assert_single_labels(message: Any, marked: str | None = None) -> None:
    """O teclado da escolha única de 4 alternativas: `(a)` a `(d)` numa linha (a marcada como
    `✅ (k)`), "Enviar" e "Não sei" em linhas próprias."""
    first = [f"✅ ({k})" if k == marked else f"({k})" for k in "abcd"]
    assert _labels(message) == [first, [SUBMIT_BUTTON], [DONT_KNOW_BUTTON]]


@pytest.fixture
def make_env(client: Any, sql: Any, clock: Any, rich_bot_api: Any, engine_fake: Any, repo_fetcher: Any,
             settings: Any, db_url: str, bot_worker_thread: Any, worker_thread: Any) -> Iterator[Callable[..., _Env]]:
    fx = {"client": client, "sql": sql, "clock": clock, "rich_bot_api": rich_bot_api, "engine_fake": engine_fake,
          "repo_fetcher": repo_fetcher, "settings": settings, "db_url": db_url,
          "bot_worker_thread": bot_worker_thread, "worker_thread": worker_thread}
    made: list[_Env] = []

    def make(*, mode: str = "telegram", students: int = 1, label: str = "escolha") -> _Env:
        env = _Env(fx, mode=mode, students=students, label=label)
        made.append(env)
        return env

    yield make
    bot_worker_thread.stop()
    worker_thread.stop()
    for env in made:
        env.close()
        assert env.default_channel.calls == [], "mensagem sem rota chegou ao canal padrão"


# ---------------------------------------------------------------------------------------------
# Escolha única


def test_single_choice_mark_then_submit_is_recorded_confirmed_graded_and_edited(make_env: Any) -> None:
    """A escolha única marca e envia, e a nota aparece em
    toda correção válida: o toque numa alternativa
    só marca, e o envio é pelo botão
    "Enviar".

    - o teclado da escolha única: `(a)` a `(d)` numa linha, "Enviar" e "Não sei" em linhas próprias,
      com o dado `cq1:<token da entrega>:m:0:<controls_version>:<chave>` nas alternativas
      e `cq1:<token>:s:0:<controls_version>` no "Enviar";
    - o toque em `(b)` só marca: `answer_toggled`, confirmado sem texto, sem chamada ao motor nem
      submissão, a 1ª linha do teclado com `✅ (b)` e o texto da questão igual;
    - o "Enviar" (15 s depois do envio): a linha literal do update (`callback_query`, com o dado do
      "Enviar" e o número da mensagem) e o desfecho `answer_recorded`; a confirmação sem texto, uma vez,
      **antes** da correção; o motor recebe `{"resposta": "b"}` (`str`); a submissão `graded` com a nota,
      as notas por parte, o feedback, `params` e `correct_answers` como o motor devolveu, `late = false`,
      sem normalização e `response_ms = 15000` (medido até o "Enviar"); a entrega `answered` sem
      campo corrente; a edição com o feedback "✅ correta · nota 100%" e o eco da
      alternativa escolhida (a chave `b` e o texto "Azul", sem as outras); o
      `submission.graded` exato (com o hash do conteúdo da alternativa escolhida);
    - um dado fora de `cq1:` segue o comportamento dos bots: `not_handled`, sem `answerCallbackQuery`;
    - toques de novo na entrega respondida (a alternativa marcada, o "Enviar" e o "Não sei", com os dados
      antigos) → "você já respondeu", submissões `refused/already_answered`, `answer_refused`, sem nova
      correção nem evento;
    - todo toque `cq1:` confirmado exatamente uma vez; a sentinela do gabarito nunca chega ao aluno."""
    env = make_env()
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    assert (delivery["state"], delivery["current_field"]) == ("delivered", 0)
    _assert_single_labels(question)
    data_b = _button(question, "(b)")
    parts = _parts_of(data_b)
    version = _b36(delivery["controls_version"])
    assert parts == ["cq1", delivery["button_token"], "m", "0", version, "b"]
    data_submit = _button(question, SUBMIT_BUTTON)
    assert _parts_of(data_submit) == ["cq1", delivery["button_token"], "s", "0", version]
    data_dont_know = _button(question, DONT_KNOW_BUTTON)
    assert _parts_of(data_dont_know) == ["cq1", delivery["button_token"], "n", "0", version]

    cursor = env.last_seq()
    grades_before = len(env.grade_calls())
    mark, mark_row = env.tap(student, question.message_id, data_b)
    assert mark_row["outcome"] == "answer_toggled"
    assert env.answer(mark).text is None
    marked = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and _labels(m)[0] == ["(a)", "✅ (b)", "(c)", "(d)"] and m)
    _assert_single_labels(marked, "b")
    assert marked.text == question.text, "a marcação mudou o texto da questão"
    assert len(env.grade_calls()) == grades_before
    assert env.submissions(delivery) == []
    toggled = env.delivery(dispatch, student)
    assert (toggled["state"], toggled["current_field"], toggled["selection"], toggled["controls_version"]) == (
        "delivered", 0, ["b"], delivery["controls_version"])

    t1 = env.advance(STEP)
    tap, row = env.tap(student, question.message_id, data_submit)
    assert {k: row[k] for k in ("kind", "chat_id", "sender_id", "text", "details", "received_at", "state",
                                "outcome", "processed_at")} == {
        "kind": "callback_query", "chat_id": student.chat, "sender_id": student.chat, "text": None,
        "details": {"callback_id": tap.callback_id, "data": data_submit, "message_id": question.message_id},
        "received_at": t1, "state": "processed", "outcome": "answer_recorded", "processed_at": t1}
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1
    call = calls[0]
    assert call.request["raw_submitted_answers"] == {"resposta": "b"}
    confirmation = env.answer(tap)
    assert confirmation.text is None
    assert confirmation.started < call.started, "a confirmação saiu depois da correção"
    _assert_as_engine(graded, call.response)
    assert {k: graded[k] for k in ("telegram_update_id", "valid", "refusal", "failure_code", "late",
                                   "normalized_fields", "raw_input", "score", "response_ms",
                                   "delay_notice_sent", "received_at", "graded_at")} == {
        "telegram_update_id": row["id"], "valid": True, "refusal": None, "failure_code": None, "late": False,
        "normalized_fields": [], "raw_input": {"resposta": "b"}, "score": 1.0, "response_ms": STEP * 1000,
        "delay_notice_sent": False, "received_at": t1, "graded_at": t1}
    answered = env.delivery(dispatch, student)
    assert (answered["state"], answered["answered_at"], answered["current_field"]) == ("answered", t1, None)

    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and CORRECT in (m.plain_text or "") and m)
    assert FEEDBACK_HEAD in edited.plain_text
    assert "❌" not in edited.plain_text and VERDICT_PARTIAL not in edited.plain_text
    assert f"{CORRECT} · nota 100%" in edited.plain_text, f"feedback sem a nota: {edited.plain_text!r}"
    _assert_choice_echo(edited.plain_text, COLORS, ["b"])  # "b" e "Azul"; nenhuma das outras
    hash_b = env.option_hashes("single_choice", ["b"])
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=1.0,
        parts=[{"index": 0, "score": 1.0, "choices": hash_b, "ratio": None, "residual": None}])]

    # Fora de `cq1:`: o dado fora do formato segue o comportamento dos bots (`not_handled`, sem confirmação).
    foreign, foreign_row = env.tap(student, question.message_id, "resposta-b", raw=True)
    assert (foreign_row["outcome"], foreign_row["details"]["data"]) == ("not_handled", "resposta-b")

    # A entrega respondida: "você já respondeu" a qualquer controle, sem nova correção.
    refusals = []
    for data in (data_b, data_submit, data_dont_know):
        again, again_row = env.tap(student, question.message_id, data, raw=True)
        assert again_row["outcome"] == "answer_refused"
        assert env.answer(again).text == ALREADY_ANSWERED
        refusals.append((again, again_row))
    subs = env.wait_submissions(delivery, ["graded", "refused", "refused", "refused"])
    assert [(s["telegram_update_id"], s["refusal"], s["valid"]) for s in subs[1:]] == [
        (each[1]["id"], "already_answered", None) for each in refusals]
    assert len(env.grade_calls()) == grades_before + 1
    assert env.delivery(dispatch, student)["state"] == "answered"
    assert env.answer_calls(foreign) == [], "o dado fora de cq1: foi confirmado"
    for each in (mark, tap, *(r[0] for r in refusals)):
        assert len(env.answer_calls(each)) == 1
    assert len(env.events(cursor, "submission.graded")) == 1
    assert all(ANSWER_SENTINEL not in text for text in env.chat_texts(student))


def test_single_choice_marks_only_and_swaps_without_grading(make_env: Any) -> None:
    """Na escolha única, `m` →
    `selection = [chave]`, trocando a anterior; edição só dos botões; sem correção; `m` da marcada é
    absoluto.

    - o teclado: `(a)` a `(d)` com `m`, "Enviar" com `s` e "Não sei";
    - marcar `(b)` → `answer_toggled`, confirmado sem texto, a 1ª linha `(a)`, `✅ (b)`, `(c)`, `(d)`,
      com `m` também no `✅ (b)`; marcar `(c)` → só `✅ (c)` marcada (troca, no máximo uma);
    - tocar no `✅ (c)` → `answer_toggled`, a seleção `["c"]` e a `controls_version` iguais; nenhuma edição
      nova, nenhum `fallback`, nenhuma linha da fila `failed` (se uma edição sair, o 400 "message is not
      modified" conta como sucesso);
    - duas marcações seguidas no teclado inicial (`(a)` e `(d)`, no mesmo lote do leitor preso): só o que
      vale em qualquer ordem das tarefas: exatamente uma das duas chaves fica em `selection`, o único ✅
      mostrado coincide com ela, e a última edição aplicada corresponde à seleção gravada;
    - as edições mudam só o teclado (o texto igual ao original); nenhuma chamada ao motor, nenhuma
      submissão, nenhum evento; a entrega `delivered`, `current_field` 0, a versão dos controles igual;
      a seleção sobrevive à troca do worker.

    Com o worker completo, a regra
    `notice.dispatch_opened` e a tarefa `preview.send` registram os avisos à equipe do disparo (sem membro da
    equipe: `no_recipient`; a prévia, `gave_up` se a tarefa correu depois da abertura), com o evento
    `notice.recorded`, em horário que depende da varredura dos relatórios.
    O "nenhum evento" da marcação vale sem os 3 tipos dos relatórios (`REPORT_EVENT_TYPES`); contrapartida exata
    na mesma função: no teste inteiro (sem cursor), os eventos desses tipos são exatamente os 2 `notice.recorded` do
    disparo (`dispatch_preview`, `no_recipient` ou `gave_up`; `dispatch_opened`, `no_recipient`; todos com
    `recipient_count` 0 e os campos fora do tipo nulos), nenhum `report.frozen` nem `offering.digest_changed`
    (o disparo segue aberto), e os desses tipos depois do cursor são um subconjunto deles. No esqueleto, a espera
    dos 2 avisos estoura (nenhum aviso é registrado)."""
    env = make_env(label="marcar")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    token, version = delivery["button_token"], _b36(delivery["controls_version"])
    _assert_single_labels(question)
    for key in "abcd":
        assert _parts_of(_button(question, f"({key})")) == ["cq1", token, "m", "0", version, key]
    assert _parts_of(_button(question, SUBMIT_BUTTON)) == ["cq1", token, "s", "0", version]
    original_text = question.text
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    def shows(first_row: list[str]) -> Any:
        return env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                        and _labels(m)[0] == first_row and m)

    first, row = env.tap(student, question.message_id, _button(question, "(b)"))
    assert row["outcome"] == "answer_toggled"
    shown = shows(["(a)", "✅ (b)", "(c)", "(d)"])
    _assert_single_labels(shown, "b")
    assert _parts_of(_button(shown, "✅ (b)")) == ["cq1", token, "m", "0", version, "b"]
    swap, row = env.tap(student, question.message_id, _button(shown, "(c)"))
    assert row["outcome"] == "answer_toggled"
    shown = shows(["(a)", "(b)", "✅ (c)", "(d)"])
    swapped = env.delivery(dispatch, student)
    assert (swapped["state"], swapped["current_field"], swapped["selection"], swapped["controls_version"]) == (
        "delivered", 0, ["c"], delivery["controls_version"])
    taps = [first, swap]

    # Tocar na marcada: absoluto, sem mudança; a edição sem mudança, se sair, é sucesso.
    _wait_outbox_idle(env, delivery)
    messages_before = [m.message_id for m in env.rich.messages(env.bot, student.chat)]
    accepted_edits = len(env.rich.edits(env.bot, student.chat, question.message_id))
    again, row = env.tap(student, question.message_id, _button(shown, "✅ (c)"))
    assert row["outcome"] == "answer_toggled"
    taps.append(again)
    _wait_outbox_idle(env, delivery)
    same = env.delivery(dispatch, student)
    assert (same["selection"], same["controls_version"]) == (["c"], delivery["controls_version"])
    assert [m.message_id for m in env.rich.messages(env.bot, student.chat)] == messages_before
    assert len(env.rich.edits(env.bot, student.chat, question.message_id)) == accepted_edits
    now = env.rich.current(env.bot, student.chat, question.message_id)
    assert (now.text, _labels(now)) == (shown.text, _labels(shown))
    for method in ("editMessageText", "editMessageReplyMarkup"):
        for call in env.rich.calls(method, bot_id=env.bot):
            if call.outcome == "error":
                assert (call.status, call.description) == DISPATCH_ERRORS["not_modified"], (
                    f"{method} falhou com {call.status} {call.description!r}")
    rows = _all(env.sql, "SELECT state, delivery_role FROM outbox_messages WHERE delivery_id = %s ORDER BY id",
                (delivery["id"],))
    assert all(r["state"] != "failed" for r in rows), rows
    assert all(r["delivery_role"] != "fallback" for r in rows), rows

    # Duas marcações seguidas no teclado inicial, no mesmo lote: vale uma, em qualquer ordem.
    _hang_reader(env)
    pair = [env.rich.tap(env.bot, student.user, question.message_id, _button(question, f"({key})"))
            for key in "ad"]
    _release_reader(env)
    for each in pair:
        assert env.processed(each.update_id)["outcome"] == "answer_toggled"
    taps += pair
    _wait_outbox_idle(env, delivery)
    final = env.delivery(dispatch, student)
    assert final["selection"] in (["a"], ["d"]), final["selection"]
    expected = [f"✅ ({k})" if [k] == final["selection"] else f"({k})" for k in "abcd"]
    current = env.rich.current(env.bot, student.chat, question.message_id)
    assert _labels(current)[0] == expected, "o ✅ mostrado não coincide com a seleção gravada"
    edits = env.rich.edits(env.bot, student.chat, question.message_id)
    assert edits and _labels(edits[-1].after)[0] == expected
    assert all(e.after.text == e.before.text == original_text for e in edits), "a marcação mudou o texto"
    assert (final["state"], final["current_field"], final["controls_version"]) == (
        "delivered", 0, delivery["controls_version"])
    assert len(env.grade_calls()) == grades_before
    assert env.submissions(delivery) == []
    assert env.events(cursor, "submission.graded") == []
    # A marcação não gera evento de tipo algum (a consulta pede todos os tipos do
    # catálogo; a renovação da posse do bot não grava evento).
    from cq_server.events import EventType

    after_marks = env.events(cursor, *(t.value for t in EventType))
    # Os eventos dos relatórios saem do recorte.
    assert [e for e in after_marks if e["type"] not in REPORT_EVENT_TYPES] == [], [e["type"] for e in after_marks]
    # Contrapartida exata: os avisos do disparo, esperados sem cursor. (A consulta pede todos os tipos do
    # `EventType` e filtra aqui: no esqueleto, sem os tipos dos relatórios, a espera estoura em vez de um 400.)
    every_type = [t.value for t in EventType]

    def notices_of_dispatch() -> list[dict]:
        return [e for e in env.events(0, *every_type) if e["type"] == "notice.recorded"
                and e["data"]["dispatch_id"] == dispatch["dispatch_id"]]

    env.wait(lambda: len(notices_of_dispatch()) == 2)
    notices = notices_of_dispatch()
    pairs = sorted((e["data"]["notice_type"], e["data"]["state"]) for e in notices)
    assert pairs in ([("dispatch_opened", "no_recipient"), ("dispatch_preview", "no_recipient")],
                     [("dispatch_opened", "no_recipient"), ("dispatch_preview", "gave_up")]), pairs
    for e in notices:
        assert e["data"] == {
            "offering_id": env.offering["offering_id"], "notice_type": e["data"]["notice_type"],
            "dispatch_id": dispatch["dispatch_id"], "report_version": None, "episode_seq": None, "batch_id": None,
            "window_started_at": None, "local_date": None, "state": e["data"]["state"], "recipient_count": 0}, e
    # No teste inteiro, os eventos desses tipos são só esses 2 (nenhum `report.frozen` nem `offering.digest_changed`:
    # o disparo segue aberto); os desses tipos depois do cursor são um subconjunto deles.
    assert [e for e in env.events(0, *every_type) if e["type"] in REPORT_EVENT_TYPES] == notices
    assert all(e in notices for e in after_marks if e["type"] in REPORT_EVENT_TYPES), after_marks
    for each in taps:
        assert env.answer(each).text is None
        assert len(env.answer_calls(each)) == 1

    # A seleção sobrevive à troca do worker.
    env.restart()
    assert env.delivery(dispatch, student)["selection"] == final["selection"]


def test_single_choice_submit_sends_the_marked_key_as_one_str(make_env: Any) -> None:
    """O "Enviar" da única manda a chave marcada (
    `parts[name] = selection[0]`, `str`).

    Marcar `(a)`, trocar para `(b)`, avançar o relógio e tocar em "Enviar" → `answer_recorded`; o motor
    recebe exatamente `{"resposta": "b"}` (`str`, não lista); `raw_input == {"resposta": "b"}`;
    `response_ms` medido até o "Enviar"; o `submission.graded` com o hash só de `b`; o eco só de `b`
    ("Azul")."""
    env = make_env(label="enviarunica")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    first, row = env.tap(student, question.message_id, _data(question, "m", "a"))
    assert row["outcome"] == "answer_toggled"
    swap, row = env.tap(student, question.message_id, _data(question, "m", "b"))
    assert row["outcome"] == "answer_toggled"
    env.wait(lambda: _labels(env.rich.current(env.bot, student.chat, question.message_id))[0]
             == ["(a)", "✅ (b)", "(c)", "(d)"])
    assert env.delivery(dispatch, student)["selection"] == ["b"]
    t1 = env.advance(STEP)
    submit, row = env.tap(student, question.message_id, _data(question, "s"))
    assert (row["outcome"], row["received_at"]) == ("answer_recorded", t1)
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1
    assert calls[0].request["raw_submitted_answers"] == {"resposta": "b"}
    assert isinstance(calls[0].request["raw_submitted_answers"]["resposta"], str)
    _assert_as_engine(graded, calls[0].response)
    assert (graded["valid"], graded["score"], graded["raw_input"], graded["response_ms"], graded["late"]) == (
        True, 1.0, {"resposta": "b"}, STEP * 1000, False)
    assert env.delivery(dispatch, student)["state"] == "answered"
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and CORRECT in (m.plain_text or "") and m)
    _assert_choice_echo(edited.plain_text, COLORS, ["b"])
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=1.0,
        parts=[{"index": 0, "score": 1.0, "choices": env.option_hashes("single_choice", ["b"]),
                "ratio": None, "residual": None}])]
    for each in (first, swap, submit):
        assert env.answer(each).text is None
        assert len(env.answer_calls(each)) == 1


def test_single_choice_empty_submit_goes_absent_like_pl(make_env: Any) -> None:
    """O "Enviar" sem
    marcação → `parts[name] = null` e o campo **ausente** na resposta crua, como o formulário do PL sem
    rádio marcado, por
    fidelidade ao PL.

    - sem `allow-blank`: "Enviar" sem marcação → `answer_recorded`; o corpo exato do `grade` é `{}`,
      aceito pelo motor falso **sem** desligar a conferência das formas (`expect_raw_shape`); a submissão
      `graded` com `valid = false` e `format_errors == {"resposta": <"No answer was submitted.">}`; ao
      aluno, o prefixo do erro de formato e "Escolha uma alternativa.", nunca o inglês; a entrega
      `delivered`, o campo reaberto (`controls_version` sobe), `selection == []`; nenhum
      `submission.graded`;
    - com `allow-blank` (outro disparo): "Enviar" vazio → nota 0, "❌ incorreta · nota 0%",
      a entrega `answered`, `raw_input == {"resposta": None}` e o corpo do `grade` também `{}`."""
    env = make_env(label="vaziaunica")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    empty, row = env.tap(student, question.message_id, _data(question, "s"))
    assert row["outcome"] == "answer_recorded"
    [invalid] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1
    assert calls[0].request["raw_submitted_answers"] == {}
    _assert_as_engine(invalid, calls[0].response)
    assert invalid["format_errors"] == {"resposta": {"html": SINGLE_NO_ANSWER, "text": SINGLE_NO_ANSWER}}
    assert (invalid["valid"], invalid["score"], invalid["normalized_fields"]) == (False, None, [])
    expected_text = FORMAT_ERROR_PREFIX + SINGLE_NO_ANSWER_PT
    env.wait(lambda: any(expected_text in text for text in env.chat_texts(student)))
    assert not any(SINGLE_NO_ANSWER in text for text in env.chat_texts(student))
    reopened = env.wait(lambda: (d := env.delivery(dispatch, student))
                        and d["controls_version"] > delivery["controls_version"] and d)
    assert (reopened["state"], reopened["current_field"], reopened["selection"]) == ("delivered", 0, [])
    assert env.events(cursor, "submission.graded") == []
    assert env.answer(empty).text is None
    assert len(env.answer_calls(empty)) == 1

    # Com `allow-blank`: nota 0, como o PL.
    blank = env.open("text", preset("single_choice", fields=(single_choice_field(allow_blank=True),)))
    blank_question = env.question(blank, student)
    blank_delivery = env.delivery(blank, student)
    grades_before = len(env.grade_calls())
    submit, row = env.tap(student, blank_question.message_id, _data(blank_question, "s"))
    assert row["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(blank_delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {}
    _assert_as_engine(graded, calls[0].response)
    assert (graded["valid"], graded["score"], graded["raw_input"]) == (True, 0.0, {"resposta": None})
    assert env.delivery(blank, student)["state"] == "answered"
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, blank_question.message_id))
                      and INCORRECT in (m.plain_text or "") and m)
    assert f"{INCORRECT} · nota 0%" in edited.plain_text, f"feedback sem a nota: {edited.plain_text!r}"
    assert CORRECT not in edited.plain_text
    assert len(env.answer_calls(submit)) == 1


def test_single_choice_double_submit_yields_one_valid_submission(make_env: Any) -> None:
    """Dois toques seguidos e rápidos em "Enviar" na única (correção pendente
    → `grading_pending`).

    Com `b` marcada e o motor fora do ar, dois "Enviar" no mesmo lote do leitor preso: um
    `answer_recorded` (confirmado sem texto) e um `answer_refused` com "Sua resposta está sendo
    corrigida." (submissão `refused/grading_pending`), em qualquer ordem das tarefas; o motor volta: uma
    só correção, com `{"resposta": "b"}`, uma só submissão válida, a entrega `answered`."""
    env = make_env(label="duploenviarunica")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    mark, row = env.tap(student, question.message_id, _data(question, "m", "b"))
    assert row["outcome"] == "answer_toggled"
    env.wait(lambda: _labels(env.rich.current(env.bot, student.chat, question.message_id))[0]
             == ["(a)", "✅ (b)", "(c)", "(d)"])
    submit_data = _data(question, "s")
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    env.engine_fake.stop()
    _hang_reader(env)
    taps = [env.rich.tap(env.bot, student.user, question.message_id, submit_data) for _ in range(2)]
    _release_reader(env)
    rows = [env.processed(each.update_id) for each in taps]
    assert sorted(r["outcome"] for r in rows) == ["answer_recorded", "answer_refused"]
    recorded = 0 if rows[0]["outcome"] == "answer_recorded" else 1
    refused = 1 - recorded
    assert env.answer(taps[recorded]).text is None
    assert env.answer(taps[refused]).text == GRADING_IN_PROGRESS
    pending, refusal = env.wait_submissions(delivery, ["grading", "refused"])
    assert (pending["telegram_update_id"], pending["raw_input"]) == (rows[recorded]["id"], {"resposta": "b"})
    assert (refusal["telegram_update_id"], refusal["refusal"], refusal["valid"]) == (
        rows[refused]["id"], "grading_pending", None)
    assert env.delivery(dispatch, student)["state"] == "delivered"

    env.engine_fake.start()
    for _ in range(8):  # a repetição da correção pela varredura, sem fixar o intervalo
        env.advance(CLOCK_STEP)
        with contextlib.suppress(AssertionError):
            env.wait(lambda: env.submissions(delivery)[0]["outcome"] == "graded", timeout=4.0)
            break
    graded, _ = env.wait_submissions(delivery, ["graded", "refused"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1, f"correções: {len(calls)}"
    assert calls[0].request["raw_submitted_answers"] == {"resposta": "b"}
    _assert_as_engine(graded, calls[0].response)
    assert (graded["submission_uuid"], graded["valid"], graded["score"]) == (pending["submission_uuid"], True, 1.0)
    assert [s["valid"] for s in env.submissions(delivery)] == [True, None]
    assert env.delivery(dispatch, student)["state"] == "answered"
    events = env.events(cursor, "submission.graded")
    assert len(events) == 1 and events[0]["data"]["submission_id"] == graded["submission_uuid"]
    for each in [mark, *taps]:
        assert len(env.answer_calls(each)) == 1


def _close_all(env: _Env, dispatches: Iterable[dict]) -> None:
    """Leva o relógio além do `closes_at` dos disparos e espera todos `closed` (a varredura repete o
    `dispatch.close` depois de alguns segundos do relógio; sem fixar o intervalo)."""
    items = list(dispatches)
    closes_at = max(_when(d["closes_at"]) for d in items)
    env.advance((closes_at - env.clock.now()).total_seconds() + 1)

    def all_closed() -> bool:
        return all(get_dispatch(env.client, ADMIN_TOKEN, env.code, d["dispatch_id"])["state"] == "closed"
                   for d in items)

    for _ in range(10):
        with contextlib.suppress(AssertionError):
            env.wait(all_closed, timeout=4.0)
            return
        env.advance(6)
    assert all_closed(), "os disparos não fecharam"


def test_single_choice_marking_after_answer_and_late_marking(make_env: Any) -> None:
    """A marcação depois da resposta e a marcação tardia (a marcação não enviada não é
    resposta; a edição de fechamento da `no_answer` leva o mesmo teclado da entrega aberta, com a
    marcação).

    - depois de respondida, marcar → "você já respondeu", `answer_refused`, submissão
      `refused/already_answered` com `raw_input == {}`;
    - uma segunda entrega deixada sem resposta até o fechamento fica `no_answer`; a edição de fechamento
      ("Janela fechada.") traz o teclado com as alternativas (`m`), o "Enviar" (`s`) e o "Não sei",
      na versão nova dos controles; marcar → `answer_toggled`, sem submissão; "Enviar" →
      `answer_recorded`, a entrega `answered_late`, `late = true`;
    - uma terceira entrega marcada (`b`) antes do horário e não enviada: no fechamento fica `no_answer`
      (marcar não é resposta), com `selection == ["b"]`; o teclado da edição de fechamento mostra
      `✅ (b)` e o "Enviar", e o "Enviar" depois do fechamento manda `{"resposta": "b"}` → `answered_late`,
      `late = true`."""
    env = make_env(label="marcaratrasada")
    student = env.students[0]
    answered_d = env.open("single_choice", preset("single_choice"))
    late_d = env.open("number", preset("single_choice"))
    marked_d = env.open("integer", preset("single_choice"))
    q1, q2, q3 = (env.question(d, student) for d in (answered_d, late_d, marked_d))
    d1, d2, d3 = (env.delivery(d, student) for d in (answered_d, late_d, marked_d))

    # 1. Respondida: marcar depois → "você já respondeu".
    _, _, _, row = _mark_and_submit(env, student, q1.message_id, "b")
    assert row["outcome"] == "answer_recorded"
    env.wait_submissions(d1, ["graded"])
    env.wait(lambda: env.delivery(answered_d, student)["state"] == "answered")
    after, after_row = env.tap(student, q1.message_id, _data(q1, "m", "a"), raw=True)
    assert after_row["outcome"] == "answer_refused"
    assert env.answer(after).text == ALREADY_ANSWERED
    subs = env.wait_submissions(d1, ["graded", "refused"])
    assert (subs[1]["refusal"], subs[1]["raw_input"], subs[1]["valid"], subs[1]["telegram_update_id"]) == (
        "already_answered", {}, None, after_row["id"])

    # 3. Marcada antes do horário e não enviada.
    pre, row = env.tap(student, q3.message_id, _data(q3, "m", "b"))
    assert row["outcome"] == "answer_toggled"
    env.wait(lambda: _labels(env.rich.current(env.bot, student.chat, q3.message_id))[0]
             == ["(a)", "✅ (b)", "(c)", "(d)"])

    _close_all(env, (answered_d, late_d, marked_d))

    def closed_message(dispatch: dict, question: Any, before: dict) -> tuple[dict, Any]:
        delivery = env.wait(lambda: (d := env.delivery(dispatch, student)) and d["state"] == "no_answer"
                            and d["controls_version"] > before["controls_version"] and d)
        version = _b36(delivery["controls_version"])
        message = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                           and SUBMIT_BUTTON in [b.label for b in m.buttons()]
                           and _parts_of(_button(m, SUBMIT_BUTTON))[4] == version and m)
        return delivery, message

    closed2, message2 = closed_message(late_d, q2, d2)
    closed3, message3 = closed_message(marked_d, q3, d3)
    assert closed3["selection"] == ["b"], "o fechamento apagou a marcação não enviada"
    for delivery, message, marked in ((closed2, message2, None), (closed3, message3, "b")):
        assert CLOSED_NOTICE in (message.plain_text or "")
        _assert_single_labels(message, marked)
        version = _b36(delivery["controls_version"])
        for key in "abcd":
            assert _parts_of(_data(message, "m", key)) == ["cq1", delivery["button_token"], "m", "0", version, key]
        assert _parts_of(_data(message, "s")) == ["cq1", delivery["button_token"], "s", "0", version]
    assert env.submissions(d2) == [] and env.submissions(d3) == []
    grades_before = len(env.grade_calls())

    # 2. Atrasada: marcar (sem submissão) e "Enviar".
    late_mark, row = env.tap(student, q2.message_id, _data(message2, "m", "c"))
    assert row["outcome"] == "answer_toggled"
    assert env.submissions(d2) == []
    assert env.delivery(late_d, student)["selection"] == ["c"]
    late_submit, row = env.tap(student, q2.message_id, _data(message2, "s"))
    assert row["outcome"] == "answer_recorded"
    [late] = env.wait_submissions(d2, ["graded"])
    assert (late["valid"], late["late"], late["raw_input"]) == (True, True, {"resposta": "c"})
    env.wait(lambda: env.delivery(late_d, student)["state"] == "answered_late")

    # 3. A marcação de antes do horário vai no "Enviar" depois do fechamento.
    marked_submit, row = env.tap(student, q3.message_id, _data(message3, "s"))
    assert row["outcome"] == "answer_recorded"
    [marked] = env.wait_submissions(d3, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert [c.request["raw_submitted_answers"] for c in calls] == [{"resposta": "c"}, {"resposta": "b"}]
    assert (marked["valid"], marked["late"], marked["raw_input"], marked["score"]) == (
        True, True, {"resposta": "b"}, 1.0)
    env.wait(lambda: env.delivery(marked_d, student)["state"] == "answered_late")
    for each in (after, pre, late_mark, late_submit, marked_submit):
        assert len(env.answer_calls(each)) == 1


# ---------------------------------------------------------------------------------------------
# Escolha múltipla


def test_multiple_choice_toggles_edit_only_markers_and_submit_sends_the_list(make_env: Any) -> None:
    """Múltipla: marcar e desmarcar absolutos.

    - o teclado da múltipla: `⬜ (a)` a `⬜ (d)` numa linha, "Enviar" e "Não sei" em linhas próprias;
    - cada alternância (`m`/`u`) é confirmada sem texto, com `answer_toggled`, sem correção nem
      submissão; as edições da questão só mudam os marcadores (o texto fica igual) e podem se fundir (no
      máximo uma por toque); a seleção fica guardada (`delivery_contents.selection`) e sobrevive à troca
      do worker (parada e subida de outro);
    - "Enviar" manda a lista na ordem das opções (`["a", "c"]`), a submissão `graded` com nota 1, ✅ com o
      eco das duas alternativas marcadas (`a` e `c`, sem `b` nem `d`), cada uma com a
      marca ✅ logo depois do texto e "✅ correta · nota 100%" (o feedback com a nota
      parcial), e o `submission.graded` com os
      hashes das duas alternativas."""
    env = make_env(label="multipla")
    student = env.students[0]
    dispatch = env.open("multiple_choice", preset("multiple_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    assert _labels(question) == [["⬜ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)"], [SUBMIT_BUTTON], [DONT_KNOW_BUTTON]]
    version = _b36(delivery["controls_version"])
    for key in "abcd":
        assert _parts_of(_button(question, f"⬜ ({key})")) == ["cq1", delivery["button_token"], "m", "0", version, key]
    assert _parts_of(_button(question, SUBMIT_BUTTON)) == ["cq1", delivery["button_token"], "s", "0", version]
    original_text = question.text

    grades_before = len(env.grade_calls())
    cursor = env.last_seq()
    taps = []
    # Duas marcações seguidas, antes de a edição chegar (os dois dados do teclado inicial valem).
    first = env.rich.tap(env.bot, student.user, question.message_id, _button(question, "⬜ (a)"))
    second = env.rich.tap(env.bot, student.user, question.message_id, _button(question, "⬜ (c)"))
    taps += [first, second]
    for each in (first, second):
        assert env.processed(each.update_id)["outcome"] == "answer_toggled"
    shown = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                     and _labels(m)[0] == ["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)"] and m)
    marked_b, row = env.tap(student, question.message_id, _button(shown, "⬜ (b)"))
    assert row["outcome"] == "answer_toggled"
    shown = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                     and _labels(m)[0] == ["✅ (a)", "✅ (b)", "✅ (c)", "⬜ (d)"] and m)
    assert _parts_of(_button(shown, "✅ (b)"))[2] == "u"
    unmarked_b, row = env.tap(student, question.message_id, _button(shown, "✅ (b)"))
    assert row["outcome"] == "answer_toggled"
    taps += [marked_b, unmarked_b]
    shown = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                     and _labels(m)[0] == ["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)"] and m)
    for each in taps:
        assert env.answer(each).text is None
    edits = env.rich.edits(env.bot, student.chat, question.message_id)
    assert 1 <= len(edits) <= len(taps)
    assert all(e.after.text == e.before.text == original_text for e in edits), "a alternância mudou o texto"
    assert len(env.grade_calls()) == grades_before
    assert env.submissions(delivery) == []
    toggled = env.delivery(dispatch, student)
    assert (toggled["state"], toggled["current_field"], sorted(toggled["selection"])) == ("delivered", 0, ["a", "c"])
    assert toggled["controls_version"] == delivery["controls_version"]

    # A seleção sobrevive à troca do worker.
    env.restart()
    assert sorted(env.delivery(dispatch, student)["selection"]) == ["a", "c"]
    t1 = env.advance(STEP)
    current = env.rich.current(env.bot, student.chat, question.message_id)
    submit, row = env.tap(student, question.message_id, _button(current, SUBMIT_BUTTON))
    assert (row["outcome"], row["received_at"]) == ("answer_recorded", t1)
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1
    assert calls[0].request["raw_submitted_answers"] == {"resposta": ["a", "c"]}
    assert env.answer(submit).text is None
    _assert_as_engine(graded, calls[0].response)
    assert (graded["valid"], graded["score"], graded["raw_input"], graded["late"]) == (
        True, 1.0, {"resposta": ["a", "c"]}, False)
    assert env.delivery(dispatch, student)["state"] == "answered"
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and CORRECT in (m.plain_text or "") and m)
    assert f"{CORRECT} · nota 100%" in edited.plain_text, f"feedback sem a nota: {edited.plain_text!r}"
    assert FEEDBACK_HEAD in edited.plain_text
    # "a" e "c" (Dois, Sete), cada uma com ✅ depois do texto; nem "b" nem "d"
    _assert_choice_echo(edited.plain_text, PRIMES, ["a", "c"], marks={"a": True, "c": True})
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=1.0,
        parts=[{"index": 0, "score": 1.0, "choices": env.option_hashes("multiple_choice", ["a", "c"]),
                "ratio": None, "residual": None}])]
    for each in taps + [submit]:
        assert len(env.answer_calls(each)) == 1


def test_empty_submit_follows_pl_and_single_mark_goes_as_one_value_with_partial_score(make_env: Any) -> None:
    """O "Enviar" vazio segue o PL, e uma só marcação vai como valor único, com nota parcial.

    - "Enviar" sem marcação: o campo **ausente** no corpo exato do `grade` (`{}`), aceito pelo motor falso
      sem desligar a conferência das formas; o erro de formato do PL ("You must select between 1
      and 4 options.") traduzido ("Marque de 1 a 4 alternativas.") ao aluno, sem inglês; a submissão
      `graded` com `valid = false` e o erro original; a entrega continua `delivered`, com o campo
      reaberto (`controls_version` sobe) e sem evento;
    - uma marcação e "Enviar": o motor recebe o valor único `"a"` (como o PL), nota 0,5 (crédito parcial)
      → ◐ parcialmente correta com "nota 50%" e a marca ✅ em (a) (feedback de nota parcial),
      sem a nota em decimal nem outro símbolo, com o eco
      da alternativa marcada (`a`, sem as outras); a nota parcial na submissão e no
      evento."""
    env = make_env(label="vazia")
    student = env.students[0]
    dispatch = env.open("multiple_choice", preset("multiple_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    empty, row = env.tap(student, question.message_id, _button(question, SUBMIT_BUTTON))
    assert row["outcome"] == "answer_recorded"
    [invalid] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1
    assert calls[0].request["raw_submitted_answers"] == {}
    _assert_as_engine(invalid, calls[0].response)
    assert invalid["format_errors"] == {"resposta": select_count_error(1, 4)}
    assert (invalid["valid"], invalid["score"], invalid["normalized_fields"]) == (False, None, [])
    expected_text = FORMAT_ERROR_PREFIX + SELECT_BETWEEN_1_4
    env.wait(lambda: any(expected_text in text for text in env.chat_texts(student)))
    assert not any("You must select" in text for text in env.chat_texts(student))
    reopened = env.wait(lambda: (d := env.delivery(dispatch, student))
                        and d["controls_version"] > delivery["controls_version"] and d)
    assert (reopened["state"], reopened["current_field"], reopened["selection"]) == ("delivered", 0, [])
    assert env.events(cursor, "submission.graded") == []

    # Uma marcação: o valor único, crédito parcial, ◐ e a nota.
    current = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                       and _parts_of(_button(m, SUBMIT_BUTTON))[4] == _b36(reopened["controls_version"]) and m)
    mark, row = env.tap(student, question.message_id, _button(current, "⬜ (a)"))
    assert row["outcome"] == "answer_toggled"
    current = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                       and _labels(m)[0][0] == "✅ (a)" and m)
    t1 = env.advance(STEP)
    submit, row = env.tap(student, question.message_id, _button(current, SUBMIT_BUTTON))
    assert row["outcome"] == "answer_recorded"
    graded = env.wait_submissions(delivery, ["graded", "graded"])[1]
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 2
    assert calls[1].request["raw_submitted_answers"] == {"resposta": "a"}
    _assert_as_engine(graded, calls[1].response)
    assert (graded["valid"], graded["score"], graded["partial_scores"], graded["raw_input"]) == (
        True, 0.5, {"resposta": {"score": 0.5, "weight": 1}}, {"resposta": ["a"]})
    assert env.delivery(dispatch, student)["state"] == "answered"
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and VERDICT_PARTIAL in (m.plain_text or "") and m)
    assert CORRECT not in edited.plain_text and INCORRECT not in edited.plain_text
    for forbidden in ("0,5", "0.5", "⚠", "🟡"):
        assert forbidden not in edited.plain_text, f"feedback parcial com {forbidden!r}"
    assert "nota 50%" in edited.plain_text, f"feedback parcial sem a nota: {edited.plain_text!r}"
    assert FEEDBACK_HEAD in edited.plain_text
    # a marcação única, com o crédito parcial e a marca ✅ em (a)
    _assert_choice_echo(edited.plain_text, PRIMES, ["a"], marks={"a": True})
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=0.5,
        parts=[{"index": 0, "score": 0.5, "choices": env.option_hashes("multiple_choice", ["a"]),
                "ratio": None, "residual": None}])]
    for each in (empty, mark, submit):
        assert env.answer(each).text is None
        assert len(env.answer_calls(each)) == 1


def test_option_marks_with_any_partial_credit_mode(make_env: Any) -> None:
    """Marcas por alternativa com qualquer `partial_credit`, inclusive
    `off`, como os selos do `pl-checkbox`, no feedback
    de nota parcial.


    Dois disparos de múltipla (gabarito `a` e `c`), um `net-correct` e um `off`; o aluno marca `a` e `b` e
    toca em "Enviar" nos dois: o motor recebe `["a", "b"]`, a nota é 0 nos dois, e o feedback de cada um
    traz "(a) Dois ✅" e "(b) Nove ❌" (a marca logo depois do texto) e "❌ incorreta · nota 0%"."""
    env = make_env(label="marcasmultipla")
    student = env.students[0]
    dispatches = [
        ("net-correct", env.open("multiple_choice", preset("multiple_choice"))),
        ("off", env.open("number",
                         preset("multiple_choice", fields=(multiple_choice_field(partial_credit="off"),)))),
    ]
    for mode, dispatch in dispatches:
        question = env.question(dispatch, student)
        delivery = env.delivery(dispatch, student)
        grades_before = len(env.grade_calls())
        mid = question.message_id
        taps = []
        for key in "ab":
            mark, row = env.tap(student, mid, _data(question, "m", key))
            assert row["outcome"] == "answer_toggled"
            taps.append(mark)
        env.wait(lambda mid=mid: _labels(env.rich.current(env.bot, student.chat, mid))[0]
                 == ["✅ (a)", "✅ (b)", "⬜ (c)", "⬜ (d)"])
        submit, row = env.tap(student, mid, _data(question, "s"))
        taps.append(submit)
        assert row["outcome"] == "answer_recorded"
        [graded] = env.wait_submissions(delivery, ["graded"])
        calls = env.grade_calls()[grades_before:]
        assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {"resposta": ["a", "b"]}
        _assert_as_engine(graded, calls[0].response)
        assert (graded["valid"], graded["score"]) == (True, 0.0)
        edited = env.wait(lambda mid=mid: (m := env.rich.current(env.bot, student.chat, mid))
                          and INCORRECT in (m.plain_text or "") and m)
        assert f"{INCORRECT} · nota 0%" in edited.plain_text, (
            f"feedback sem a nota, crédito parcial {mode}: {edited.plain_text!r}")
        _assert_choice_echo(edited.plain_text, PRIMES, ["a", "b"], marks={"a": True, "b": False})
        for each in taps:
            assert len(env.answer_calls(each)) == 1


# ---------------------------------------------------------------------------------------------
# Toque duplo rápido


def _wait_outbox_idle(env: _Env, delivery: dict) -> None:
    """Espera a fila da entrega sem mensagem `pending` nem `leased`."""
    env.wait(lambda: env.sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE delivery_id = %s AND state IN ('pending', 'leased')",
        (delivery["id"],)).fetchone()[0] == 0)


def test_double_submit_before_grading_yields_one_valid_submission_and_one_refusal(make_env: Any) -> None:
    """Toque duplo rápido (dois "Enviar" seguidos: no máximo uma submissão válida):
    correção pendente → `grading_pending`, submissão `refused`,
    `answer_refused` e o texto `GRADING_IN_PROGRESS`.

    Com `a` e `c` marcadas, o aluno toca duas vezes em "Enviar" no mesmo teclado, antes de a correção do
    primeiro sair: o leitor preso junta os dois toques num lote, e o motor fora do ar segura a correção
    pendente (o resultado não depende da ordem das tarefas). Um toque grava a resposta
    (`answer_recorded`, confirmado sem texto); o outro é recusado (`answer_refused`, confirmado com "Sua
    resposta está sendo corrigida.", submissão `refused/grading_pending` ligada ao update dele). O motor
    volta: uma só correção, com a lista `["a", "c"]`, uma só submissão válida, a entrega `answered`, ✅
    com o eco das duas alternativas (cada uma com a marca ✅ e "nota 100%": feedback de nota parcial)
    e um só `submission.graded`."""
    env = make_env(label="duploenviar")
    student = env.students[0]
    dispatch = env.open("multiple_choice", preset("multiple_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    marks = [env.rich.tap(env.bot, student.user, question.message_id, _button(question, f"⬜ ({key})"))
             for key in "ac"]
    for each in marks:
        assert env.processed(each.update_id)["outcome"] == "answer_toggled"
    shown = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                     and _labels(m)[0] == ["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)"] and m)
    submit_data = _button(shown, SUBMIT_BUTTON)
    grades_before = len(env.grade_calls())
    cursor = env.last_seq()

    env.engine_fake.stop()
    _hang_reader(env)
    taps = [env.rich.tap(env.bot, student.user, question.message_id, submit_data) for _ in range(2)]
    _release_reader(env)
    rows = [env.processed(each.update_id) for each in taps]
    assert sorted(r["outcome"] for r in rows) == ["answer_recorded", "answer_refused"]
    recorded = 0 if rows[0]["outcome"] == "answer_recorded" else 1
    refused = 1 - recorded
    assert env.answer(taps[recorded]).text is None
    assert env.answer(taps[refused]).text == GRADING_IN_PROGRESS
    pending, refusal = env.wait_submissions(delivery, ["grading", "refused"])
    assert (pending["telegram_update_id"], pending["raw_input"]) == (rows[recorded]["id"], {"resposta": ["a", "c"]})
    assert (refusal["telegram_update_id"], refusal["refusal"], refusal["valid"]) == (
        rows[refused]["id"], "grading_pending", None)
    assert env.delivery(dispatch, student)["state"] == "delivered"
    assert env.events(cursor, "submission.graded") == []

    env.engine_fake.start()
    for _ in range(8):  # a repetição da correção pela varredura, sem fixar o intervalo
        env.advance(CLOCK_STEP)
        with contextlib.suppress(AssertionError):
            env.wait(lambda: env.submissions(delivery)[0]["outcome"] == "graded", timeout=4.0)
            break
    graded, _ = env.wait_submissions(delivery, ["graded", "refused"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1, f"correções: {len(calls)}"
    assert calls[0].request["raw_submitted_answers"] == {"resposta": ["a", "c"]}
    _assert_as_engine(graded, calls[0].response)
    assert (graded["submission_uuid"], graded["valid"], graded["score"]) == (pending["submission_uuid"], True, 1.0)
    assert [s["valid"] for s in env.submissions(delivery)] == [True, None]
    assert env.delivery(dispatch, student)["state"] == "answered"
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and CORRECT in (m.plain_text or "") and m)
    assert f"{CORRECT} · nota 100%" in edited.plain_text, f"feedback sem a nota: {edited.plain_text!r}"
    _assert_choice_echo(edited.plain_text, PRIMES, ["a", "c"], marks={"a": True, "c": True})
    events = env.events(cursor, "submission.graded")
    assert len(events) == 1 and events[0]["data"]["submission_id"] == graded["submission_uuid"]
    for each in marks + taps:
        assert len(env.answer_calls(each)) == 1


def test_double_toggle_on_the_same_button_keeps_one_mark_and_unchanged_edit_is_success(make_env: Any) -> None:
    """Toque duplo rápido (duas alternâncias: uma edição sem mudança conta como
    sucesso); marcar e desmarcar absolutos; correção pendente,
    `m` muda `selection`, `render_version += 1`, sem mudar `controls_version`, e
    (`MessageNotModified` é sucesso).

    O aluno toca em `⬜ (a)` e, com a edição já aplicada no servidor mas não no cliente dele, toca de novo
    no mesmo botão (o dado do teclado antigo, por `tap_raw`; a versão dos controles não mudou). Marcar é
    absoluto: a segunda alternância é aceita (`answer_toggled`, confirmada sem texto) e a seleção continua
    `["a"]`. A renderização do estado não muda, então a edição da segunda, se o sistema a mandar, recebe
    do Telegram 400 "message is not modified", que conta como sucesso: nenhuma mensagem nova (sem
    `fallback`), nenhuma linha da fila da entrega `failed`, o texto e o teclado da questão iguais. Depois,
    "Enviar" manda o valor único `"a"` (uma marcação, como o PL) numa só correção, com o eco de `a`, a
    marca ✅ depois dele, "◐ parcialmente correta" e "nota 50%" (o feedback com a nota
    parcial)."""
    env = make_env(label="duploalternar")
    student = env.students[0]
    dispatch = env.open("multiple_choice", preset("multiple_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    data_a = _button(question, "⬜ (a)")
    grades_before = len(env.grade_calls())

    first, row = env.tap(student, question.message_id, data_a)
    assert row["outcome"] == "answer_toggled"
    marked = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and _labels(m)[0] == ["✅ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)"] and m)
    _wait_outbox_idle(env, delivery)
    messages_before = [m.message_id for m in env.rich.messages(env.bot, student.chat)]
    accepted_edits = len(env.rich.edits(env.bot, student.chat, question.message_id))

    second, row = env.tap(student, question.message_id, data_a, raw=True)
    assert row["outcome"] == "answer_toggled"
    _wait_outbox_idle(env, delivery)
    for each in (first, second):
        assert env.answer(each).text is None
    toggled = env.delivery(dispatch, student)
    assert (toggled["state"], toggled["current_field"], toggled["selection"]) == ("delivered", 0, ["a"])
    assert toggled["controls_version"] == delivery["controls_version"]
    assert env.submissions(delivery) == []
    assert len(env.grade_calls()) == grades_before
    # A edição sem mudança é sucesso: nenhuma mensagem nova, nenhuma falha na fila, a questão igual.
    assert [m.message_id for m in env.rich.messages(env.bot, student.chat)] == messages_before
    assert len(env.rich.edits(env.bot, student.chat, question.message_id)) == accepted_edits
    now = env.rich.current(env.bot, student.chat, question.message_id)
    assert (now.text, _labels(now)) == (marked.text, _labels(marked))
    for method in ("editMessageText", "editMessageReplyMarkup"):
        for call in env.rich.calls(method, bot_id=env.bot):
            if call.outcome == "error":
                assert (call.status, call.description) == DISPATCH_ERRORS["not_modified"], (
                    f"{method} falhou com {call.status} {call.description!r}")
    rows = _all(env.sql, "SELECT state, delivery_role FROM outbox_messages WHERE delivery_id = %s ORDER BY id",
                (delivery["id"],))
    assert all(r["state"] != "failed" for r in rows), rows
    assert all(r["delivery_role"] != "fallback" for r in rows), rows

    t1 = env.advance(STEP)
    submit, row = env.tap(student, question.message_id, _button(now, SUBMIT_BUTTON))
    assert (row["outcome"], row["received_at"]) == ("answer_recorded", t1)
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {"resposta": "a"}
    _assert_as_engine(graded, calls[0].response)
    assert (graded["valid"], graded["score"], graded["raw_input"]) == (True, 0.5, {"resposta": ["a"]})
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and VERDICT_PARTIAL in (m.plain_text or "") and m)
    assert "nota 50%" in edited.plain_text, f"feedback parcial sem a nota: {edited.plain_text!r}"
    _assert_choice_echo(edited.plain_text, PRIMES, ["a"], marks={"a": True})
    for each in (first, second, submit):
        assert len(env.answer_calls(each)) == 1


# ---------------------------------------------------------------------------------------------
# Toques inválidos, superados e em entregas indisponíveis


def test_invalid_taps_are_confirmed_without_effect_and_stale_ones_say_part_answered(make_env: Any) -> None:
    """Toques inválidos e superados.

    Com duas entregas abertas do mesmo aluno (D1 e D2), toques por `tap_raw`:
    - sem efeito, confirmados sem texto, `answer_ignored`, sem submissão: remetente que não é o aluno
      (outro chat), `button_token` inexistente, alternativa inexistente (`z`), ação inexistente para o
      campo (`u` e `c` numa escolha única) e o dado de D1 na mensagem **conhecida** de D2;
    - superados, confirmados com "esta parte já foi respondida", `answer_stale`, sem submissão: campo que
      não é o corrente (`1`) e versão que não é a corrente;
    - as duas entregas continuam `delivered`, sem correção; depois, marcar e enviar em D1 ainda vale
      (`answer_toggled` e `answer_recorded`), com a escolha única que marca e
      envia."""
    env = make_env(label="invalidos")
    student = env.students[0]
    first = env.open("single_choice", preset("single_choice"))
    second = env.open("single_choice", preset("single_choice"))
    q1, q2 = env.question(first, student), env.question(second, student)
    assert q1.message_id != q2.message_id
    d1, d2 = env.delivery(first, student), env.delivery(second, student)
    data_b = _button(q1, "(b)")
    _, token, _, field, version, _ = _parts_of(data_b)
    other_version = _b36(d1["controls_version"] + 1)
    grades_before = len(env.grade_calls())
    stranger = make_user(690)

    ignored = [
        (stranger, q1.message_id, data_b),
        (student.user, q1.message_id, f"cq1:{'A' * 22}:m:{field}:{version}:b"),
        (student.user, q1.message_id, f"cq1:{token}:m:{field}:{version}:z"),
        (student.user, q1.message_id, f"cq1:{token}:u:{field}:{version}:b"),
        (student.user, q1.message_id, f"cq1:{token}:c:{field}:{version}:b"),
        (student.user, q2.message_id, data_b),
    ]
    stale = [
        (student.user, q1.message_id, f"cq1:{token}:m:1:{version}:b"),
        (student.user, q1.message_id, f"cq1:{token}:m:{field}:{other_version}:b"),
    ]
    taps = []
    for sender, message_id, data in ignored:
        tap = env.rich.tap_raw(env.bot, sender, message_id, data)
        row = env.processed(tap.update_id)
        assert row["outcome"] == "answer_ignored", f"toque {data!r} de {sender.user_id}: {row['outcome']}"
        assert env.answer(tap).text is None
        taps.append(tap)
    for sender, message_id, data in stale:
        tap = env.rich.tap_raw(env.bot, sender, message_id, data)
        row = env.processed(tap.update_id)
        assert row["outcome"] == "answer_stale", f"toque {data!r}: {row['outcome']}"
        assert env.answer(tap).text == PART_ALREADY_ANSWERED
        taps.append(tap)
    assert env.submissions(d1) == [] and env.submissions(d2) == []
    assert len(env.grade_calls()) == grades_before
    for dispatch, before in ((first, d1), (second, d2)):
        after = env.delivery(dispatch, student)
        assert {k: after[k] for k in ("state", "current_field", "controls_version", "selection", "parts")} == {
            k: before[k] for k in ("state", "current_field", "controls_version", "selection", "parts")}

    mark, _, valid, row = _mark_and_submit(env, student, q1.message_id, "b")
    assert row["outcome"] == "answer_recorded"
    env.wait_submissions(d1, ["graded"])
    assert env.delivery(first, student)["state"] == "answered"
    assert env.delivery(second, student)["state"] == "delivered"
    for each in taps + [mark, valid]:
        assert len(env.answer_calls(each)) == 1


def test_taps_on_not_delivered_and_question_failed_deliveries_are_refused_unavailable(make_env: Any) -> None:
    """Toques em entregas `not_delivered` e `question_failed` (`Broken`).

    - entrega `not_delivered`: o 400 definitivo do envio da questão com o efeito aplicado
      (`after_effect`: a mensagem chegou ao aluno com o teclado) leva a entrega a
      `not_delivered/channel_refused`; o toque nessa cópia (número de mensagem desconhecido no endereço,
      cópia não registrada) → "esta questão não está disponível", `answer_refused` e submissão `refused/unavailable`;
    - entrega `question_failed`: a correção `broken` da primeira resposta (marcar `(b)` e "Enviar")
      leva a `question_failed/grading_broken` (submissão `broken` com o código e a fase, sem o detalhe
      técnico), a edição com `QUESTION_FAILED`; um toque de novo → "esta questão não está disponível" e
      `refused/unavailable`, sem nova correção.
      A recusa da cópia vem antes da ação."""
    env = make_env(label="indisponivel")
    student = env.students[0]
    env.rich.fail("sendRichMessage", status=400, description="Bad Request: message is too long",
                  bot_id=env.bot, times=1, after_effect=True)
    lost = env.settle(env.confirm(env.prepare("single_choice", preset("single_choice"))), {"not_delivered": 1})
    delivery = env.delivery(lost, student)
    assert (delivery["state"], delivery["undelivered_reason"], delivery["current_field"]) == (
        "not_delivered", "channel_refused", None)
    copy = env.question(lost, student)
    grades_before = len(env.grade_calls())
    tap, row = env.tap(student, copy.message_id, _button(copy, "(b)"))
    assert row["outcome"] == "answer_refused"
    assert env.answer(tap).text == UNAVAILABLE
    [refused] = env.wait_submissions(delivery, ["refused"])
    assert (refused["refusal"], refused["valid"], refused["telegram_update_id"]) == ("unavailable", None, row["id"])
    assert env.delivery(lost, student)["state"] == "not_delivered"

    broken_question = preset("single_choice", grade_broken=GRADE_BROKEN)
    failing = env.open("broken_on_grade", broken_question)
    question = env.question(failing, student)
    data_b = _button(question, "(b)")
    delivery = env.delivery(failing, student)
    mark, _, first, row = _mark_and_submit(env, student, question.message_id, "b")
    assert row["outcome"] == "answer_recorded"
    [broken] = env.wait_submissions(delivery, ["broken"])
    assert (broken["failure_code"], broken["failure_phase"], broken["valid"], broken["score"]) == (
        "question_code_error", "grade", None, None)
    failed = env.wait(lambda: (d := env.delivery(failing, student)) and d["state"] == "question_failed" and d)
    assert (failed["failure"], failed["current_field"]) == ("grading_broken", None)
    env.wait(lambda: QUESTION_FAILED in (env.rich.current(env.bot, student.chat, question.message_id).plain_text
                                         or ""))
    again, row = env.tap(student, question.message_id, data_b, raw=True)
    assert row["outcome"] == "answer_refused"
    assert env.answer(again).text == UNAVAILABLE
    subs = env.wait_submissions(delivery, ["broken", "refused"])
    assert (subs[1]["refusal"], subs[1]["telegram_update_id"]) == ("unavailable", row["id"])
    assert len(env.grade_calls()) == grades_before + 1
    assert all("Traceback" not in text for text in env.chat_texts(student))
    for each in (tap, mark, first, again):
        assert len(env.answer_calls(each)) == 1


def test_tap_recorded_after_closes_at_waits_for_the_close_transition(make_env: Any) -> None:
    """A entrada depois do horário espera a transição (a espera não conta rodada); limitação
    conhecida: o toque que esperou traz a versão de antes da edição de
    fechamento.

    O leitor do bot fica preso (nenhuma consulta bem-sucedida) enquanto o relógio passa de `closes_at`:
    a primeira rodada do `dispatch.close` termina sem efeito (barreira), e o disparo continua `open`. O
    toque de marcação em `(b)` (ação `m`), gravado depois de `closes_at` com o disparo ainda
    aberto, termina a tarefa `telegram.update` `done` **sem efeito** (update `pending`, nenhuma
    submissão, nenhuma confirmação, entrega `delivered`). Depois da transição, o reprocessamento o trata
    como atrasado: a versão é a de antes da edição de fechamento → "esta parte já foi respondida"
    (`answer_stale`), confirmado uma vez; a entrega fica `no_answer`, sem o gabarito (`revealed_at`
    nulo, já que o gabarito só vai a quem já respondeu), sem submissão;
    nenhuma rodada da tarefa do update termina `failed`."""
    env = make_env(label="espera")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    dispatch_row = env.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s::uuid",
                                   (dispatch["dispatch_id"],)).fetchone()[0]
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    closes_at = _when(dispatch["closes_at"])

    _hang_reader(env)
    tap = env.rich.tap(env.bot, student.user, question.message_id, _button(question, "(b)"))
    env.advance((closes_at - env.clock.now()).total_seconds() + 1)
    close_payload = json.dumps({"dispatch_row_id": dispatch_row})
    env.wait(lambda: env.sql.execute(
        "SELECT count(*) FROM tasks WHERE task_type = 'dispatch.close' AND state = 'done' AND payload @> %s::jsonb",
        (close_payload,)).fetchone()[0] >= 1)
    assert get_dispatch(env.client, ADMIN_TOKEN, env.code, dispatch["dispatch_id"])["state"] == "open"

    _release_reader(env)
    stored = env.wait(lambda: env.update(tap.update_id))
    update_payload = json.dumps({"update_row_id": stored["id"]})

    def first_round_done() -> bool:
        return env.sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'telegram.update' AND "
                               "state = 'done' AND payload @> %s::jsonb", (update_payload,)).fetchone()[0] >= 1

    env.wait(first_round_done)
    waiting = env.update(tap.update_id)
    assert waiting["received_at"] > closes_at
    assert (waiting["state"], waiting["outcome"], waiting["processed_at"]) == ("pending", None, None)
    assert env.submissions(delivery) == []
    assert env.answer_calls(tap) == []
    assert env.delivery(dispatch, student)["state"] == "delivered"
    assert get_dispatch(env.client, ADMIN_TOKEN, env.code, dispatch["dispatch_id"])["state"] == "open"

    # A transição e o reprocessamento (a varredura repete depois de 5 s do relógio).
    for _ in range(8):
        env.advance(6)
        with contextlib.suppress(AssertionError):
            env.wait(lambda: env.update(tap.update_id)["state"] == "processed", timeout=4.0)
            break
    final = env.processed(tap.update_id)
    assert (final["outcome"], final["received_at"]) == ("answer_stale", waiting["received_at"])
    assert env.answer(tap).text == PART_ALREADY_ANSWERED
    assert get_dispatch(env.client, ADMIN_TOKEN, env.code, dispatch["dispatch_id"])["state"] == "closed"
    closed = env.delivery(dispatch, student)
    assert closed["state"] == "no_answer" and closed["revealed_at"] is None
    assert env.submissions(delivery) == []
    rounds = env.sql.execute("SELECT state FROM tasks WHERE task_type = 'telegram.update' AND payload @> %s::jsonb "
                             "ORDER BY id", (update_payload,)).fetchall()
    assert len(rounds) >= 2 and all(r[0] == "done" for r in rounds)
    assert len(env.answer_calls(tap)) == 1


def test_confirmation_failure_is_logged_and_does_not_block_processing(make_env: Any) -> None:
    """a confirmação que falha (toque expirado: 400 `query is too old`) vira o
    registro `telegram.callback_answer_failed`, sem dado do toque, do chat nem do token, e o toque é
    processado normalmente (submissão `graded`, entrega `answered`), com uma única tentativa de
    confirmação. O
    toque cuja confirmação expira é o "Enviar" da única, depois de marcar `(b)` com o leitor livre."""
    env = make_env(label="confirmacao")
    student = env.students[0]
    dispatch = env.open("single_choice", preset("single_choice"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    data_b = _button(question, "(b)")
    data_submit = _data(question, "s")
    _, row = env.tap(student, question.message_id, data_b)
    assert row["outcome"] == "answer_toggled"

    _hang_reader(env)
    tap = env.rich.tap(env.bot, student.user, question.message_id, data_submit)
    env.rich.expire_callback(tap.callback_id)
    with capture_logs() as captured:
        _release_reader(env)
        row = env.processed(tap.update_id)
        env.wait_submissions(delivery, ["graded"])
        env.wait(lambda: env.answer_calls(tap) and all(c.ended is not None for c in env.answer_calls(tap)))
        env.wait(lambda: captured.with_event("telegram.callback_answer_failed"))
    assert row["outcome"] == "answer_recorded"
    assert env.delivery(dispatch, student)["state"] == "answered"
    assert [a for a in env.rich.answers(env.bot) if a.callback_id == tap.callback_id] == []
    assert [c.status for c in env.answer_calls(tap)] == [400]
    records = captured.with_event("telegram.callback_answer_failed")
    assert len(records) == 1
    text = json.dumps(records[0], ensure_ascii=False)
    for leaked in (tap.callback_id, data_b, data_submit, str(student.chat), env.rich.token_of(env.bot)):
        assert leaked not in text


# ---------------------------------------------------------------------------------------------
# Eco, versões, nota acima de 1 e o console


def test_typed_echo_is_escaped_in_the_channel_mode_and_truncated_at_200(make_env: Any) -> None:
    """Uma resposta digitada (campo de texto, por "responder" à questão)
    com `<b>`, `&`, `</code>`, uma URL e um `/comando`, mais longa que 200 caracteres: o motor recebe o
    texto literal inteiro, a submissão o guarda literal, e o feedback (❌) ecoa só os primeiros 200
    caracteres com "…", escapados no Rich (`&lt;b&gt;`, `&amp;`, `&lt;/code&gt;`), sem erro de protocolo
    nem de política (fixture)."""
    env = make_env(label="eco")
    student = env.students[0]
    dispatch = env.open("text", preset("text"))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    head = "<b>negrito</b> & </code> https://exemplo.test/caminho?x=1 /comando "
    answer = head + "x" * (230 - len(head)) + " FIM-DO-ECO"
    assert len(answer) > 200 and "FIM-DO-ECO" not in answer[:200]
    uid = env.rich.push_text(env.bot, student.user, answer, reply_to_message_id=question.message_id)
    row = env.processed(uid)
    assert (row["text"], row["reply_to_message_id"], row["outcome"]) == (answer, question.message_id,
                                                                         "answer_recorded")
    [graded] = env.wait_submissions(delivery, ["graded"])
    call = env.grade_calls()[-1]
    assert call.request["raw_submitted_answers"] == {"resposta": answer}
    _assert_as_engine(graded, call.response)
    assert (graded["raw_input"], graded["valid"], graded["score"]) == ({"resposta": answer}, True, 0.0)
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and INCORRECT in (m.plain_text or "") and m)
    assert edited.text_format == "rich_html"
    assert "&lt;b&gt;negrito&lt;/b&gt; &amp; &lt;/code&gt;" in edited.text
    assert "<b>negrito</b>" not in edited.text
    assert head.strip() in edited.plain_text
    # Truncado em 200 com "…" antes do escape (o feedback dos textos fixos ao aluno): o eco é
    # exatamente os 200 primeiros caracteres seguidos de "…" (nem 199, nem 201).
    match = _ECHO.search(edited.plain_text)
    assert match, f"feedback sem 'Sua resposta: {{eco}} · {{veredito}}': {edited.plain_text!r}"
    assert match.group("echo") == answer[:200] + "…"
    assert answer[:200] + "…" in edited.plain_text
    assert answer[:201] not in edited.plain_text and "FIM-DO-ECO" not in edited.plain_text


def test_version_mismatch_params_and_correct_answers_are_stored_as_returned(make_env: Any) -> None:
    """com a correção acusando versões diferentes do motor e do PL, a submissão
    grava `engine_mismatch` e `pl_mismatch` como vieram (verdadeiros), e `params`, `correct_answers`,
    `submitted_answers`, `partial_scores` e `feedback` como o motor devolveu; o evento não leva nada disso
    além da nota e das partes, e a sentinela do gabarito não chega ao aluno nem ao evento. A resposta é
    marcar `(b)` e "Enviar", sem avançar o relógio entre os dois (a escolha única
    marca e envia)."""
    env = make_env(label="versoes")
    student = env.students[0]
    dispatch = env.open("version_mismatch", preset("single_choice", version_mismatch=(True, True)))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    cursor = env.last_seq()
    t1 = env.clock.now()
    mark, _, tap, row = _mark_and_submit(env, student, question.message_id, "b")
    assert row["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(delivery, ["graded"])
    call = env.grade_calls()[-1]
    assert call.response["version_mismatch"] == {"engine": True, "pl": True}
    _assert_as_engine(graded, call.response)
    assert (graded["engine_mismatch"], graded["pl_mismatch"]) == (True, True)
    assert graded["correct_answers"]["gabarito_sentinela"] == ANSWER_SENTINEL
    events = env.events(cursor, "submission.graded")
    assert events == [env.graded_event(dispatch, delivery, graded, student, t1, score=1.0,
                                       parts=[{"index": 0, "score": 1.0,
                                               "choices": env.option_hashes("version_mismatch", ["b"]),
                                               "ratio": None, "residual": None}])]
    assert ANSWER_SENTINEL not in json.dumps(events, ensure_ascii=False)
    env.wait(lambda: CORRECT in (env.rich.current(env.bot, student.chat, question.message_id).plain_text or ""))
    assert all(ANSWER_SENTINEL not in text for text in env.chat_texts(student))
    for each in (mark, tap):
        assert len(env.answer_calls(each)) == 1


def test_score_above_one_is_stored_as_returned_and_shown_as_given(make_env: Any) -> None:
    """Nota acima de 1: o `grade()` do `server.py`
    dá nota 1,5 à resposta certa; a submissão guarda 1,5 como veio (sem teto), o feedback é "✅ correta ·
    nota 100%" (o selo do PL tem teto: 100 com nota ≥ 1), nunca "1,5", "1.5" nem "150", e o evento
    leva `score = 1.5` (a nota da parte continua a do motor, 1). A resposta é marcar `(b)` e "Enviar",
    sem avançar o relógio entre os dois. A escolha única marca e envia, e a nota
    aparece em porcentagem.
    """
    env = make_env(label="acima")
    student = env.students[0]
    dispatch = env.open("score_above_one", preset("single_choice", score_override=1.5))
    question = env.question(dispatch, student)
    delivery = env.delivery(dispatch, student)
    cursor = env.last_seq()
    t1 = env.clock.now()
    mark, _, tap, row = _mark_and_submit(env, student, question.message_id, "b")
    assert row["outcome"] == "answer_recorded"
    [graded] = env.wait_submissions(delivery, ["graded"])
    call = env.grade_calls()[-1]
    assert call.response["score"] == 1.5
    _assert_as_engine(graded, call.response)
    assert (graded["score"], graded["valid"]) == (1.5, True)
    edited = env.wait(lambda: (m := env.rich.current(env.bot, student.chat, question.message_id))
                      and CORRECT in (m.plain_text or "") and m)
    assert f"{CORRECT} · nota 100%" in edited.plain_text, f"feedback sem a nota: {edited.plain_text!r}"
    for forbidden in ("1,5", "1.5", "150"):
        assert forbidden not in edited.plain_text, f"nota acima de 1 mostrada como {forbidden!r}"
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=1.5,
        parts=[{"index": 0, "score": 1.0, "choices": env.option_hashes("score_above_one", ["b"]),
                "ratio": None, "residual": None}])]
    assert env.delivery(dispatch, student)["state"] == "answered"
    for each in (mark, tap):
        assert len(env.answer_calls(each)) == 1


def test_author_grade_score_is_shown_below_one_negative_and_at_one(make_env: Any) -> None:
    """O feedback e o `SCORE` dos textos fixos ao aluno (o
    selo do PL, 100 com nota ≥ 1, ⌊nota × 100⌋ entre 0 e 1 e 0 com nota ≤ 0), no feedback
    de nota parcial: `server_score = 1.0` também mostra a nota (não é controle
    negativo), e uma nota abaixo de zero mostra 0 (o piso do selo).


    Escolha única com a nota que o `grade()` do `server.py` põe (`server_score`), respondida pelo fluxo
    de marcar e enviar (a alternativa certa e "Enviar"), em três disparos: 0,7 → "◐ parcialmente correta ·
    nota 70%"; -0,25 → "❌ incorreta · nota 0%"; 1,0 → "✅ correta · nota 100%"; a submissão e o evento
    com a nota como veio (as notas das partes ficam as do motor, 1)."""
    env = make_env(label="notaautor")
    student = env.students[0]
    cases = [
        ("number", 0.7, f"{VERDICT_PARTIAL} · nota 70%", (CORRECT, INCORRECT)),
        ("integer", -0.25, f"{INCORRECT} · nota 0%", (CORRECT, VERDICT_PARTIAL, "-25", "-0,25")),
        ("text", 1.0, f"{CORRECT} · nota 100%", (INCORRECT, VERDICT_PARTIAL)),
    ]
    for qid, score, expected, forbidden in cases:
        dispatch = env.open(qid, preset("single_choice", server_score=score))
        question = env.question(dispatch, student)
        delivery = env.delivery(dispatch, student)
        cursor = env.last_seq()
        t1 = env.clock.now()
        mark, _, submit, row = _mark_and_submit(env, student, question.message_id, "b")
        assert row["outcome"] == "answer_recorded"
        [graded] = env.wait_submissions(delivery, ["graded"])
        call = env.grade_calls()[-1]
        assert call.request["raw_submitted_answers"] == {"resposta": "b"}
        assert call.response["score"] == score
        _assert_as_engine(graded, call.response)
        assert (graded["valid"], graded["score"], graded["partial_scores"]) == (
            True, score, {"resposta": {"score": 1.0, "weight": 1}})
        edited = env.wait(lambda mid=question.message_id: (m := env.rich.current(env.bot, student.chat, mid))
                          and FEEDBACK_HEAD in (m.plain_text or "") and m)
        assert expected in edited.plain_text, f"nota {score}: {edited.plain_text!r}"
        for each in forbidden:
            assert each not in edited.plain_text, f"nota {score}: {each!r} em {edited.plain_text!r}"
        _assert_choice_echo(edited.plain_text, COLORS, ["b"])
        assert env.events(cursor, "submission.graded") == [env.graded_event(
            dispatch, delivery, graded, student, t1, score=score,
            parts=[{"index": 0, "score": 1.0, "choices": env.option_hashes(qid, ["b"]),
                    "ratio": None, "residual": None}])]
        for each in (mark, submit):
            assert len(env.answer_calls(each)) == 1


def test_single_choice_degraded_by_key_in_console_has_the_same_outcome(make_env: Any) -> None:
    """Escolha única degradada pela chave no console.

    Pelo console (sem botões nem edição): a questão sai numa linha que se pode citar (`channel_ref`
    numérico, sem `chat_id`), com as alternativas pela chave e o aviso do `/naosei`; a escolha pela chave
    (`choose`, um texto `b` em resposta à questão) grava a linha literal (update `message` com o texto e o
    número da mensagem respondida, `update_id` negativo), `answer_recorded`; o motor recebe
    `{"resposta": "b"}`; a submissão `graded` com nota 1 como o motor devolveu; a entrega `answered`; o
    feedback ✅ chega como linha nova em resposta à questão, com o eco da alternativa (`b`, "Azul"); o
    `submission.graded` exato, como no Telegram."""
    env = make_env(mode="console", label="console")
    student = env.students[0]
    assert student.console is not None
    dispatch = env.open("single_choice", preset("single_choice"))
    line = env.question(dispatch, student)
    assert line.channel_ref == str(line.message_id)
    assert "chat" not in line.raw and str(student.chat) not in json.dumps(line.raw)
    for piece in ("Qual é a cor do céu num dia limpo?", "Vermelho", "Azul", "Verde", "Amarelo", DONT_KNOW_HOWTO):
        assert piece in line.text
    delivery = env.delivery(dispatch, student)
    assert delivery["state"] == "delivered"
    cursor = env.last_seq()
    grades_before = len(env.grade_calls())
    t1 = env.advance(STEP)
    student.console.mark()
    student.console.choose(line, "b")
    stored = env.wait(lambda: _one(env.sql, "SELECT id, update_id, kind, chat_id, sender_id, text, "
                                   "reply_to_message_id, received_at, state, outcome FROM telegram_updates "
                                   "WHERE bot_id = %s AND chat_id = %s AND state = 'processed' AND update_id < 0 "
                                   "ORDER BY id DESC LIMIT 1", (env.bot_row, student.chat)))
    assert {k: stored[k] for k in ("kind", "chat_id", "sender_id", "text", "reply_to_message_id", "received_at",
                                   "outcome")} == {
        "kind": "message", "chat_id": student.chat, "sender_id": student.chat, "text": "b",
        "reply_to_message_id": line.message_id, "received_at": t1, "outcome": "answer_recorded"}
    [graded] = env.wait_submissions(delivery, ["graded"])
    calls = env.grade_calls()[grades_before:]
    assert len(calls) == 1 and calls[0].request["raw_submitted_answers"] == {"resposta": "b"}
    _assert_as_engine(graded, calls[0].response)
    assert (graded["valid"], graded["score"], graded["raw_input"], graded["response_ms"],
            graded["telegram_update_id"]) == (True, 1.0, {"resposta": "b"}, STEP * 1000, stored["id"])
    assert env.delivery(dispatch, student)["state"] == "answered"
    feedback = student.console.wait_for_line(lambda ln: CORRECT in ln.text, timeout=PROCESS_TIMEOUT)
    assert str(feedback.reply_to) == line.channel_ref
    assert FEEDBACK_HEAD in feedback.text and ANSWER_SENTINEL not in feedback.text
    _assert_choice_echo(feedback.text, COLORS, ["b"])  # a alternativa da chave digitada, como no Telegram
    assert env.events(cursor, "submission.graded") == [env.graded_event(
        dispatch, delivery, graded, student, t1, score=1.0,
        parts=[{"index": 0, "score": 1.0, "choices": env.option_hashes("single_choice", ["b"]),
                "ratio": None, "residual": None}])]
