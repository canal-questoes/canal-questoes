"""Abertura do disparo e entrega das variantes.

Cobre a abertura, o alvo, a entrega das variantes, as mídias, o ritmo e a idempotência da abertura, com as
regras do alvo fixado na abertura, do alvo vazio, do 400/413 na abertura, do campo corrente desde a
variante, da classificação do envio recusado, da foto reenviada como documento, da tolerância da abertura
e da configuração de entrega até os tratadores:
- abertura imediata na confirmação e agendada só no horário, pela varredura; o agendado sem
  confirmação nunca abre (expira);
- o alvo fixado na abertura (matrícula `student` ativa e endereço vigente, `active` ou `blocked`, no
  bot do oferecimento); quem se vincula depois não recebe;
- uma entrega por aluno, com seed própria (base 36, < 2³²), `button_token`, snapshot, IR com a versão,
  `media_blobs`, `rendered` e as mensagens na fila **na mesma transação** (um gatilho de restrição
  adiado, criado pelo teste no banco do teste, fotografa a entrega e a fila no commit de cada
  `delivery_contents`), inclusive depois de uma queda do worker (`Crash`) no meio da abertura;
- `current_field` = o primeiro campo logo depois do `delivery.prepare` (entrega ainda `pending`) e
  depois do `delivered`;
- a mensagem da questão pela `rich_bot_api` (Rich com `skip_entity_detection`): enunciado,
  alternativas com as chaves na ordem da variante, teclado `cq1:` (na escolha única, um botão que marca
  cada alternativa, ação `m`, e o "Enviar", ação `s`), "Não sei" e o prazo no fuso do
  oferecimento; o HTML simples quando o worker recebe `DeliverySettings(telegram_format="html")`;
- os desfechos do envio: 403 de bloqueio, outro definitivo, mídia antes da
  questão (foto até o limite, documento acima), mídia ausente, falha definitiva de mídia, a nova
  tentativa como documento e o pedido de campo que falha;
- as falhas da variante: quebrada, não suportada, acima do limite do canal e recusada pelo motor
  (400/413); `not_found` → `question.refetch` e a entrega continua `pending`;
- `window_missed` com a tolerância de 60 s; o alvo vazio; o ritmo de 1/s por chat e o limite
  por bot, medidos sobre os envios aceitos.

**Montagem.** O oferecimento, o bot (Bot API falsa com a extensão do disparo) e a ativação entram pela API
de oferecimentos e bots; as pessoas, pela API base; a matrícula ativa com o termo aceito e o endereço de entrega entram
por SQL nas colunas do esquema do banco (como `test_outbox_routes.py` faz), e o chat do aluno fica conhecido do
dublê por um `my_chat_member` (`kicked` → `member`), que a recepção trata como `not_handled`. O disparo
é preparado e confirmado pela API (`tests/helpers/api.py`) e validado, aberto e entregue pelo worker
real (`run_worker(…, bots=…, engine=…, fetcher=…, delivery=…)`, fixture `bot_worker_thread`), com o
adaptador HTTP real do motor (`HttpQuestionEngine`) sobre o motor falso estrito e a obtenção falsa.

Horários pelo `FakeClock` (parado, salvo quando o teste o avança); ids internos, seeds, `button_token`
e `message_id` nunca como literal esperado: vêm do banco, do dublê ou da relação entre eles. No
esqueleto, cada teste falha no primeiro passo do disparo (a rota `dispatches.prepare` ausente, 404, ou uma
tabela do disparo ausente).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import deliverytexts
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.deliveries import load_ir
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, confirm_dispatch, create_offering,
                               get_dispatch, prepare_dispatch, register_bot, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.bot_api_dispatch import Button
from tests.helpers.channels import Crash
from tests.helpers.clock import iso
from tests.helpers.engine import (FakeMedia, FakeOption, FakeQuestion, number_field, png_bytes, preset,
                                  single_choice_field)

REPO = "https://repos.cq.test/disparos/abertura.git"
COMMIT = "0ab1" * 10
TZ = "America/Sao_Paulo"
# Tempo real máximo de espera por um efeito do worker.
WAIT = 20.0
# Ritmo curto nos casos que não medem o ritmo (padrões configuráveis).
FAST = DeliverySettings(chat_interval_seconds=0.05)
# Posse dos testes (30 s, `settings`) mais 1 s: a tarefa em posse de um worker que caiu fica livre.
EXPIRE = 31
# Métodos que entregam uma mensagem ao chat (os envios medidos pelo ritmo).
SEND_METHODS = ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument")
DELIVERY_COUNT_KEYS = ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                       "answered_late", "question_failed")
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{22}")

# Escolha única com as alternativas fora da ordem alfabética: a mensagem segue a ordem da variante.
ORDERED_OPTIONS = (FakeOption("d", "Amarelo"), FakeOption("b", "Azul"), FakeOption("a", "Vermelho"),
                   FakeOption("c", "Verde"))
ORDERED_CHOICE = FakeQuestion("Cor do céu", "Qual é a cor do céu num dia limpo?",
                              (single_choice_field(options=ORDERED_OPTIONS),))
# Duas mídias: a primeira abaixo do limite de foto do teste (foto), a segunda acima (documento).
SMALL_PNG = png_bytes()
LARGE_PNG = png_bytes(3000)
PHOTO_LIMIT = 1000
TWO_MEDIA = FakeQuestion("Duas figuras", "Observe as duas figuras e responda.", (number_field(),),
                         media=(FakeMedia("clientFilesQuestion/pequena.png", alt="Plano inclinado",
                                          data=SMALL_PNG),
                                FakeMedia("clientFilesQuestion/grande.png", alt="Gráfico da velocidade",
                                          data=LARGE_PNG)),
                         extra_elements=("pl-figure",))
# Fórmula no enunciado: o Rich a escreve em `tg-math`, o HTML simples em `code`.
MATH_CHOICE = FakeQuestion("Potência", 'Quanto vale <cq-math display="inline">x^2</cq-math> para x = 3?',
                           (single_choice_field(options=(FakeOption("a", "6"), FakeOption("b", "9"),
                                                         FakeOption("c", "12"))),))

BROKEN_VARIANT = {"outcome": "broken", "warnings": [],
                  "reason": {"code": "question_code_error", "phase": "generate",
                             "message": "O código da questão falhou ao gerar a variante."}}
UNSUPPORTED_VARIANT = {"outcome": "unsupported",
                       "reasons": [{"code": "unsupported_elements", "elements": ["pl-matrix-input"],
                                    "message": "A variante usa elementos que o canal não suporta."}]}

_codes = itertools.count(1)


def _base36(value: int) -> str:
    """A seed canônica do PL: base 36 minúscula, sem zeros à esquerda."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        value, rest = divmod(value, 36)
        out = digits[rest] + out
        if value == 0:
            return out


def _chat_of(call: Any) -> int | None:
    """O `chat_id` de um pedido (no multipart das mídias, ele chega como texto)."""
    value = call.params.get("chat_id")
    return int(value) if isinstance(value, (int, str)) and str(value).lstrip("-").isdigit() else None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_hhmm(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(TZ)).strftime("%H:%M")


@dataclasses.dataclass(frozen=True)
class Student:
    pseudo_id: str
    person_row: int
    enrollment_row: int | None
    endpoint_row: int | None
    user: Any

    @property
    def chat(self) -> int:
        return self.user.user_id


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int


class EngineProxy:
    """`QuestionEngine` que delega ao adaptador real e aplica um roteiro na criação da variante: na
    chamada `crash_on` (1ª = 1), a variante é criada pelo motor e o worker cai (`Crash`) antes de
    gravá-la; na chamada `oversize_on`, o enunciado da variante devolvida passa dos limites do canal."""

    def __init__(self, inner: Any, *, crash_on: int | None = None, oversize_on: int | None = None) -> None:
        self._inner = inner
        self._crash_on = crash_on
        self._oversize_on = oversize_on
        self.seeds: list[str] = []

    def inspect(self, ref: Any) -> Any:
        return self._inner.inspect(ref)

    def create_variant(self, ref: Any, seed: str) -> Any:
        result = self._inner.create_variant(ref, seed)
        self.seeds.append(seed)
        if len(self.seeds) == self._crash_on:
            raise Crash()
        if len(self.seeds) == self._oversize_on:
            ir = copy.deepcopy(result.ir)
            ir["statement"]["html"] = "<p>" + "Enunciado longo demais para o canal. " * 1200 + "</p>"
            return dataclasses.replace(result, ir=ir)
        return result

    def grade(self, ref: Any, snapshot: Any, raw_answers: Any) -> Any:
        return self._inner.grade(ref, snapshot, raw_answers)

    def close(self) -> None:
        return None


class Flow:
    """Um oferecimento ativo com bot, os alunos e o disparo do teste (ver o docstring do módulo)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any,
                 rich: Any, engine_fake: Any, repo_fetcher: Any, start: Any, stop_one: Any,
                 window: int | None = None) -> None:
        self.client, self.sql, self.db_url, self.clock, self.settings = client, sql, db_url, clock, settings
        self.rich, self.engine_fake, self.fetcher = rich, engine_fake, repo_fetcher
        self._start, self._stop_one = start, stop_one
        self._engines: list[Any] = []
        self._users = itertools.count(1)
        self._workers = itertools.count(1)
        extra = {} if window is None else {"response_window_minutes": window}
        self.offering = create_offering(client, ADMIN_TOKEN, code=f"disparo-abertura-{next(_codes)}",
                                        timezone=TZ, **extra)
        self.code = self.offering["code"]
        self.bot = rich.add_bot()
        register_bot(client, ADMIN_TOKEN, self.code, rich.token_of(self.bot))
        activate_bot(client, ADMIN_TOKEN, self.code)
        self.offering_row, self.bot_row = sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id "
            "WHERE b.telegram_bot_id = %s", (self.bot,)).fetchone()

    # -- montagem ------------------------------------------------------------------------------

    def program(self, qid: str, question: FakeQuestion) -> None:
        self.engine_fake.program((REPO, COMMIT, qid), question)

    def other_bot(self) -> tuple[int, int]:
        """Um bot registrado noutro oferecimento (rascunho): (id do Telegram, `bots.id`)."""
        offering = create_offering(self.client, ADMIN_TOKEN, code=f"disparo-outro-{next(_codes)}", timezone=TZ)
        bot = self.rich.add_bot()
        register_bot(self.client, ADMIN_TOKEN, offering["code"], self.rich.token_of(bot))
        row = self.sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()[0]
        return bot, row

    def student(self, *, enrollment: str | None = "active", role: str = "student",
                endpoint: str | None = "active", bot: tuple[int, int] | None = None) -> Student:
        """Uma pessoa com matrícula (`enrollment`: `active`, `pending` ou `None`) e endereço de entrega
        (`endpoint`: `active`, `blocked`, `inactive` ou `None`) no bot do oferecimento (ou em `bot`)."""
        n = next(self._users)
        user = make_user(700 + n)
        person = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Abertura {n}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (person["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment_row = None
        if enrollment is not None:
            active = enrollment == "active"
            terms = (1, now) if active and role == "student" else (None, None)
            enrollment_row = self.sql.execute(
                "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
                "terms_version, terms_accepted_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (person_row, self.offering_row, role, enrollment, now, now if active else None,
                 *terms)).fetchone()[0]
        endpoint_row = None
        if endpoint is not None:
            tg_bot, bot_row = bot if bot is not None else (self.bot, self.bot_row)
            endpoint_row = self.sql.execute(
                "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                "changed_at) VALUES (%s, %s, 'telegram', %s, %s, %s, %s) RETURNING id",
                (person_row, bot_row, user.user_id, endpoint, now, now)).fetchone()[0]
            # O chat passa a ser conhecido do dublê (senão, 400 `chat not found`).
            self.rich.push_my_chat_member(tg_bot, user, old_status="kicked", new_status="member")
        return Student(person["pseudo_id"], person_row, enrollment_row, endpoint_row, user)

    def prepare(self, qid: str, *, opens_at: datetime | None = None, closes_at: datetime | None = None) -> Disp:
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at),
                                closes_at=None if closes_at is None else iso(closes_at))
        row = self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                               (body["dispatch_id"],)).fetchone()[0]
        return Disp(body["dispatch_id"], row)

    def confirm(self, d: Disp) -> dict:
        return confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)

    def audit_contents(self) -> None:
        """Gatilho de restrição adiado (no banco do teste): no commit da transação que grava cada
        `delivery_contents`, fotografa a entrega e as mensagens dela na fila."""
        self.sql.execute(
            "CREATE TABLE cq_test_contents_audit (delivery_id bigint, state text, variant_at timestamptz, "
            "current_field smallint, render_version integer, roles text[])")
        self.sql.execute(
            "CREATE FUNCTION cq_test_contents_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "INSERT INTO cq_test_contents_audit SELECT d.id, d.state, d.variant_at, d.current_field, "
            "d.render_version, ARRAY(SELECT m.delivery_role FROM outbox_messages m "
            "WHERE m.delivery_id = d.id ORDER BY m.id) FROM deliveries d WHERE d.id = NEW.delivery_id; "
            "RETURN NULL; END $$")
        self.sql.execute(
            "CREATE CONSTRAINT TRIGGER cq_test_contents_audit AFTER INSERT ON delivery_contents "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cq_test_contents_audit()")

    def audit_rows(self) -> dict[int, list[tuple]]:
        out: dict[int, list[tuple]] = {}
        for row in self.sql.execute("SELECT delivery_id, state, variant_at, current_field, render_version, "
                                    "roles FROM cq_test_contents_audit").fetchall():
            out.setdefault(row[0], []).append(tuple(row[1:]))
        return out

    # -- worker --------------------------------------------------------------------------------

    def engine(self) -> Any:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url, timeout_seconds=10))
        self._engines.append(engine)
        return engine

    def start(self, *, delivery: DeliverySettings = FAST, engine: Any = None, **kwargs: Any) -> Any:
        settings = dataclasses.replace(self.settings, worker_id=f"worker-abertura-{next(self._workers)}")
        return self._start(settings=settings, engine=engine if engine is not None else self.engine(),
                           fetcher=self.fetcher, delivery=delivery, **kwargs)

    def stop(self, thread: Any) -> None:
        assert self._stop_one(thread) != float("inf"), "o worker não parou em 10 s"

    def close(self) -> None:
        for engine in self._engines:
            engine.close()

    # -- espera --------------------------------------------------------------------------------

    def wait(self, predicate: Callable[[], Any], timeout: float = WAIT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def wait_state(self, d: Disp, state: str) -> dict:
        """Espera o disparo chegar a `state`; falha logo se ele chegar a outro estado final."""
        def ready() -> dict | None:
            row = self.dispatch(d)
            if row["state"] == state:
                return row
            assert row["state"] in ("validating", "scheduled", "open"), (
                f"disparo em {row['state']} (rejeição {row['rejection']}, cancelamento {row['cancel_reason']})")
            return None
        return self.wait(ready)

    def validated(self, d: Disp, **kwargs: Any) -> Any:
        """Sobe um worker e espera a validação (`scheduled`); devolve o worker, ainda no ar."""
        worker = self.start(**kwargs)
        self.wait_state(d, "scheduled")
        return worker

    def settled(self, d: Disp, count: int) -> list[dict] | None:
        """As entregas, quando há `count`, nenhuma `pending` e nenhuma mensagem delas na fila."""
        rows = self.deliveries(d)
        if len(rows) != count or any(r["state"] == "pending" for r in rows):
            return None
        busy = self.sql.execute(
            "SELECT count(*) FROM outbox_messages m JOIN deliveries d ON d.id = m.delivery_id "
            "WHERE d.dispatch_id = %s AND m.state IN ('pending', 'leased')", (d.row,)).fetchone()[0]
        return rows if busy == 0 else None

    def wait_settled(self, d: Disp, count: int) -> list[dict]:
        return self.wait(lambda: self.settled(d, count))

    def advance_until(self, predicate: Callable[[], Any], *, step: float, tries: int = 10) -> Any:
        """Avança o relógio em passos de `step` s até `predicate()`; entre os passos, espera até 2 s
        reais (a varredura roda a cada 1 s real)."""
        for _ in range(tries):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(0.05)
            self.clock.advance(step)
        value = predicate()
        assert value, f"condição não satisfeita depois de {tries} avanços de {step} s"
        return value

    # -- consultas -----------------------------------------------------------------------------

    def _rows(self, query: str, params: tuple) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchall()

    def dispatch(self, d: Disp) -> dict:
        return self._rows("SELECT * FROM dispatches WHERE id = %s", (d.row,))[0]

    def deliveries(self, d: Disp) -> list[dict]:
        return self._rows("SELECT * FROM deliveries WHERE dispatch_id = %s ORDER BY id", (d.row,))

    def contents(self, delivery_row: int) -> dict | None:
        rows = self._rows("SELECT * FROM delivery_contents WHERE delivery_id = %s", (delivery_row,))
        return rows[0] if rows else None

    def messages(self, delivery_row: int) -> list[dict]:
        return self._rows("SELECT * FROM outbox_messages WHERE delivery_id = %s ORDER BY id", (delivery_row,))

    def tasks(self, task_type: str) -> list[dict]:
        return self._rows("SELECT * FROM tasks WHERE task_type = %s ORDER BY id", (task_type,))

    def ref_row(self, qid: str) -> int:
        return self.sql.execute("SELECT id FROM question_refs WHERE commit_sha = %s AND qid = %s",
                                (COMMIT, qid)).fetchone()[0]

    def events(self, d: Disp, *types: str) -> list[tuple]:
        """Os eventos dos tipos dados deste disparo, sem o `seq` (na ordem do `seq`)."""
        return [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"])
                for e in all_events(self.client, ADMIN_TOKEN, *types)
                if e["data"].get("dispatch_id") == d.id]

    def sent_to(self, chat: int) -> list[Any]:
        """As mensagens aceitas pelo dublê neste chat do bot, na ordem de criação."""
        return self.rich.messages(self.bot, chat)

    def calls_to(self, chat: int) -> list[Any]:
        """Todos os pedidos de envio ao chat (aceitos ou não), na ordem de chegada."""
        return [c for c in self.rich.calls() if c.method in SEND_METHODS and _chat_of(c) == chat]

    def create_variant_calls(self, since: int) -> list[Any]:
        return self.engine_fake.calls("create_variant")[since:]


@pytest.fixture
def flow_factory(client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any,
                 rich_bot_api: Any, engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any,
                 stop_one: Any) -> Iterator[Callable[..., Flow]]:
    made: list[Flow] = []

    def make(**kwargs: Any) -> Flow:
        flow = Flow(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, rich=rich_bot_api,
                    engine_fake=engine_fake, repo_fetcher=repo_fetcher, start=bot_worker_thread,
                    stop_one=stop_one, **kwargs)
        made.append(flow)
        return flow

    try:
        yield make
    finally:
        bot_worker_thread.stop()
        for flow in made:
            flow.close()


@pytest.fixture
def flow(flow_factory: Callable[..., Flow]) -> Flow:
    return flow_factory()


def _delivery_event(flow: Flow, d: Disp, delivery: dict, student: Student, **extra: Any) -> dict:
    return {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
            "delivery_id": str(delivery["delivery_uuid"]), "person": student.pseudo_id, **extra}


def _without_media_bytes(ir: dict) -> dict:
    out = copy.deepcopy(ir)
    for media in out["media"]:
        media.pop("data_base64", None)
    return out


# ---------------------------------------------------------------------------------------------
# Abertura imediata: uma entrega por aluno, conteúdo gravado e a mensagem da questão
# ---------------------------------------------------------------------------------------------


def test_immediate_dispatch_opens_on_confirmation_with_one_delivery_per_student(flow: Flow) -> None:
    """Abertura imediata:
    o imediato abre na confirmação; cada aluno do alvo ganha uma entrega com seed própria (base 36,
    < 2³²) e `button_token` distintos, o snapshot e a IR emitidos pelo motor, `rendered` no dialeto do
    canal e a `question` na fila **na mesma transação** (gatilho adiado); a mensagem sai pela
    `rich_bot_api` em Rich (`skip_entity_detection`), com o enunciado, as alternativas na ordem da
    variante, o teclado `cq1:` (cada alternativa marca, ação `m`, e a linha "Enviar", ação `s`),
    "Não sei" e o prazo no fuso do oferecimento; o envio aceito leva a `delivered`, com
    `delivery.delivered`, e a consulta mostra as contagens."""
    flow.program("single_choice", ORDERED_CHOICE)
    students = [flow.student() for _ in range(3)]
    flow.audit_contents()
    t0 = flow.clock.now()
    closes_at = t0 + timedelta(hours=5, minutes=30)
    d = flow.prepare("single_choice", closes_at=closes_at)
    flow.validated(d)
    before = len(flow.engine_fake.calls("create_variant"))

    confirmed = flow.confirm(d)
    assert (confirmed["state"], confirmed["confirmed"], confirmed["target_count"]) == ("scheduled", True, 3)
    rows = flow.wait_settled(d, 3)

    dispatch = flow.dispatch(d)
    assert (dispatch["state"], dispatch["immediate"], dispatch["confirmed_at"], dispatch["opens_at"],
            dispatch["opened_at"], dispatch["closes_at"]) == ("open", True, t0, t0, t0, closes_at)
    by_enrollment = {r["enrollment_id"]: r for r in rows}
    assert sorted(by_enrollment) == sorted(s.enrollment_row for s in students)
    seeds = [r["seed"] for r in rows]
    tokens = [r["button_token"] for r in rows]
    assert len(set(seeds)) == 3 and all(0 <= s < 2**32 for s in seeds)
    assert len(set(tokens)) == 3 and all(TOKEN_SHAPE.fullmatch(t) for t in tokens)
    assert not set(tokens) & {s.pseudo_id for s in students}

    ref = {"repo": REPO, "commit": COMMIT, "qid": "single_choice"}
    calls = flow.create_variant_calls(before)
    assert [c.request["question_ref"] for c in calls] == [ref] * 3
    assert sorted(c.request["seed"] for c in calls) == sorted(_base36(s) for s in seeds)
    issued = {v.seed: v for v in flow.engine_fake.issued_variants("single_choice")}
    audit = flow.audit_rows()
    for student in students:
        delivery = by_enrollment[student.enrollment_row]
        seed36 = _base36(delivery["seed"])
        [question] = flow.messages(delivery["id"])
        [msg] = flow.sent_to(student.chat)
        assert (delivery["delivery_endpoint_id"], delivery["state"], delivery["undelivered_reason"],
                delivery["failure"], delivery["current_field"], delivery["render_version"],
                delivery["controls_version"], delivery["principal_message_id"], delivery["created_at"],
                delivery["variant_at"], delivery["sent_at"], delivery["answered_at"], delivery["revealed_at"]) == (
            student.endpoint_row, "delivered", None, None, 0, 1, 1, question["id"], t0, t0, t0, None, None)
        contents = flow.contents(delivery["id"])
        assert contents is not None
        assert contents["snapshot"] == issued[seed36].snapshot
        assert contents["ir"] == issued[seed36].ir and contents["ir"]["ir_version"] == "1.0"
        assert (contents["media_refs"], contents["parts"], contents["selection"]) == ({}, {}, [])
        assert contents["rendered"]["format"] == "rich"
        assert (question["delivery_role"], question["operation"], question["state"], question["text"],
                question["media_id"], question["field_index"], question["priority"], question["attempts"],
                question["recipient_person_id"], question["bot_id"], question["delivery_endpoint_id"],
                question["channel_ref"]) == (
            "question", "send", "sent", None, None, None, 0, 1, student.person_row, flow.bot_row,
            student.endpoint_row, str(msg.message_id))
        # Na mesma transação: no commit do conteúdo, a variante, o campo corrente e a mensagem já estão.
        assert audit[delivery["id"]] == [("pending", t0, 0, 1, ["question"])]

        assert (msg.method, msg.rich_message["skip_entity_detection"]) == ("sendRichMessage", True)
        text = msg.plain_text
        assert "Qual é a cor do céu num dia limpo?" in text
        positions = [text.index(o.html) for o in ORDERED_OPTIONS]
        assert positions == sorted(positions), "as alternativas saem na ordem da variante"
        for option in ORDERED_OPTIONS:
            assert re.search(rf"(?<![A-Za-z0-9]){option.key}\W{{1,4}}{option.html}", text), option
        assert deliverytexts.deadline(closes_at, TZ) in text
        assert _local_hhmm(closes_at) in text
        token = delivery["button_token"]
        assert msg.keyboard == (
            tuple(Button(f"({o.key})", f"cq1:{token}:m:0:1:{o.key}") for o in ORDERED_OPTIONS),
            (Button(deliverytexts.SUBMIT_BUTTON, f"cq1:{token}:s:0:1"),),
            (Button(deliverytexts.DONT_KNOW_BUTTON, f"cq1:{token}:n:0:1"),))

    by_delivery = {str(r["delivery_uuid"]): r for r in rows}
    events = flow.events(d, "dispatch.opened", "delivery.delivered", "delivery.not_delivered")
    assert events[0] == ("dispatch.opened", iso(t0), None, {"offering_id": flow.offering["offering_id"],
                                                             "dispatch_id": d.id, "target_count": 3})
    student_of = {s.enrollment_row: s for s in students}
    assert sorted(events[1:], key=lambda e: e[3]["delivery_id"]) == sorted(
        [("delivery.delivered", iso(t0), None,
          _delivery_event(flow, d, r, student_of[r["enrollment_id"]])) for r in by_delivery.values()],
        key=lambda e: e[3]["delivery_id"])
    shown = get_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    assert (shown["state"], shown["opened_at"], shown["target_count"]) == ("open", iso(t0), 3)
    assert shown["deliveries"] == {**{k: 0 for k in DELIVERY_COUNT_KEYS}, "delivered": 3, "grading_pending": 0}


def test_scheduled_dispatch_opens_only_at_its_time_and_unconfirmed_never_opens(flow: Flow) -> None:
    """O agendado confirmado continua `scheduled` até o horário
    (nenhuma `dispatch.open`, nenhuma entrega nem envio antes) e abre pela varredura no horário, com a
    chave `dispatch.open:<id>:r1`; o agendado sem confirmação, com o mesmo horário, nunca abre: expira
    (`cancelled/expired`, com `dispatch.cancelled`) sem entregas."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    t0 = flow.clock.now()
    opens_at, closes_at = t0 + timedelta(hours=1), t0 + timedelta(hours=3)
    confirmed_d = flow.prepare("single_choice", opens_at=opens_at, closes_at=closes_at)
    unconfirmed_d = flow.prepare("single_choice", opens_at=opens_at, closes_at=closes_at)
    flow.validated(confirmed_d)
    flow.wait_state(unconfirmed_d, "scheduled")
    confirmed = flow.confirm(confirmed_d)
    assert (confirmed["state"], confirmed["confirmed"], confirmed["opens_at"]) == ("scheduled", True, iso(opens_at))

    flow.clock.set(opens_at - timedelta(seconds=1))
    time.sleep(2.5)  # mais de duas rodadas da varredura (1 s real)
    assert flow.tasks("dispatch.open") == []
    assert [flow.dispatch(x)["state"] for x in (confirmed_d, unconfirmed_d)] == ["scheduled", "scheduled"]
    assert flow.deliveries(confirmed_d) == [] and flow.sent_to(student.chat) == []

    flow.clock.set(opens_at)
    [delivery] = flow.wait_settled(confirmed_d, 1)
    flow.wait_state(unconfirmed_d, "cancelled")
    opened = flow.dispatch(confirmed_d)
    assert (opened["state"], opened["opened_at"], delivery["state"], delivery["sent_at"]) == (
        "open", opens_at, "delivered", opens_at)
    [task] = flow.tasks("dispatch.open")
    assert (task["payload"], task["idempotency_key"], task["state"]) == (
        {"dispatch_row_id": confirmed_d.row}, f"dispatch.open:{confirmed_d.row}:r1", "done")
    expired = flow.dispatch(unconfirmed_d)
    assert (expired["cancel_reason"], expired["cancelled_at"], expired["opened_at"], expired["confirmed_at"]) == (
        "expired", opens_at, None, None)
    assert flow.deliveries(unconfirmed_d) == []
    assert len(flow.sent_to(student.chat)) == 1
    assert flow.events(unconfirmed_d, "dispatch.opened", "dispatch.cancelled") == [
        ("dispatch.cancelled", iso(opens_at), None, {"offering_id": flow.offering["offering_id"],
                                                     "dispatch_id": unconfirmed_d.id, "cancel_reason": "expired"})]


def test_target_is_fixed_at_opening_and_blocked_endpoint_is_not_delivered_without_channel(flow: Flow) -> None:
    """Alvo fixado na abertura: o alvo são as matrículas `student` ativas com
    endereço vigente (`active` ou `blocked`) no bot do oferecimento; ficam fora a matrícula pendente,
    o endereço `inactive`, o membro da equipe, o endereço noutro bot e quem não tem endereço. O
    endereço `blocked` vira `not_delivered/endpoint_blocked` na abertura, sem variante nem chamada ao
    canal, com `delivery.not_delivered`; quem se vincula depois da abertura não recebe.

    Avisos à equipe: o membro da equipe (matrícula `team` ativa com endereço ativo no bot)
    continua fora do alvo, mas recebe os avisos à equipe: a prévia
    (cabeçalho e questão; a escolha única não tem pedido de campo) e o aviso "aberto". As chamadas ao chat
    dele saem da asserção dos de fora do alvo e ganham a asserção exata do subconjunto (as 3 mensagens à
    equipe, na ordem); a `create_variant` da prévia (a seed da amostra, a mesma da validação) sai da lista das
    variantes da abertura, com a contagem exata (uma prévia). A prévia ao membro é esperada `sent` antes da
    confirmação: no disparo imediato, sem a espera, a abertura venceria a corrida e a prévia registraria
    `gave_up`."""
    flow.program("single_choice", preset("single_choice"))
    other = flow.other_bot()
    active = flow.student()
    blocked = flow.student(endpoint="blocked")
    outsiders = [flow.student(enrollment="pending"), flow.student(endpoint="inactive"),
                 flow.student(role="team"), flow.student(bot=other), flow.student(endpoint=None)]
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    before = len(flow.engine_fake.calls("create_variant"))
    # Avisos à equipe: a prévia ao membro da equipe termina antes da confirmação. O disparo
    # imediato enfileira a `dispatch.open` na transação da confirmação, e a `preview.send` só nasce na varredura
    # seguinte; uma prévia que corre depois de o disparo sair de `scheduled` registra `gave_up`, sem mensagem e
    # sem motor. Esperar as 2 mensagens da prévia `sent` (como os e2e e o
    # canário dos avisos à equipe) torna determinística a contrapartida exata abaixo.
    from tests.helpers.team import team_messages

    member = outsiders[2]
    flow.wait(lambda: (x := team_messages(flow.sql, "dispatch_preview", member.pseudo_id)) and len(x) >= 2
              and all(m.state == "sent" for m in x) and x)
    t0 = flow.clock.now()

    assert flow.confirm(d)["target_count"] == 2
    rows = flow.wait_settled(d, 2)
    by_enrollment = {r["enrollment_id"]: r for r in rows}
    assert sorted(by_enrollment) == sorted([active.enrollment_row, blocked.enrollment_row])
    ok, refused = by_enrollment[active.enrollment_row], by_enrollment[blocked.enrollment_row]
    assert (ok["state"], ok["sent_at"]) == ("delivered", t0)
    assert (refused["state"], refused["undelivered_reason"], refused["variant_at"], refused["sent_at"],
            refused["current_field"], refused["principal_message_id"], refused["delivery_endpoint_id"]) == (
        "not_delivered", "endpoint_blocked", None, None, None, None, blocked.endpoint_row)
    assert flow.contents(refused["id"]) is None and flow.messages(refused["id"]) == []
    # Avisos à equipe: as mensagens à equipe ao membro (separadas pelo destinatário, com a
    # asserção exata do subconjunto: a prévia, cabeçalho e questão, e o aviso "aberto", todas aceitas).
    member_messages = flow.wait(lambda: (rows := team_messages(flow.sql, recipient=member.pseudo_id))
                                and len(rows) >= 3 and all(m.state in ("sent", "failed") for m in rows) and rows)
    assert [(m.kind, m.role, m.state) for m in member_messages] == [
        ("dispatch_preview", None, "sent"), ("dispatch_preview", "question", "sent"),
        ("dispatch_opened", None, "sent")]
    member_calls = flow.calls_to(member.chat)
    assert len(member_calls) == 3 and [c.outcome for c in member_calls] == ["ok"] * 3, member_calls
    # A `create_variant` da prévia usa a seed da amostra (a da validação, a 1ª chamada do fluxo) e sai da lista
    # das variantes da abertura; contagem exata: a seed da amostra foi ao motor duas vezes (validação e prévia).
    # (As seeds comparadas pelo valor, `int(seed, 36)`: a da prévia é a base 36 canônica da seed gravada.)
    sample_seed = int(flow.create_variant_calls(0)[0].request["seed"], 36)
    assert [int(c.request["seed"], 36) for c in flow.create_variant_calls(0)].count(sample_seed) == 2
    assert [c.request["seed"] for c in flow.create_variant_calls(before)
            if int(c.request["seed"], 36) != sample_seed] == [_base36(ok["seed"])]
    assert flow.calls_to(blocked.chat) == []
    assert all(flow.calls_to(s.chat) == [] for s in outsiders if s is not member)
    assert flow.sql.execute("SELECT state FROM delivery_endpoints WHERE id = %s",
                            (blocked.endpoint_row,)).fetchone()[0] == "blocked"
    assert sorted(flow.events(d, "dispatch.opened", "delivery.not_delivered")) == [
        ("delivery.not_delivered", iso(t0), None,
         _delivery_event(flow, d, refused, blocked, undelivered_reason="endpoint_blocked")),
        ("dispatch.opened", iso(t0), None, {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
                                            "target_count": 2})]

    late = flow.student()
    time.sleep(2.5)  # duas rodadas da varredura: o alvo não muda
    assert [r["enrollment_id"] for r in flow.deliveries(d)] == [r["enrollment_id"] for r in rows]
    assert flow.calls_to(late.chat) == []


def test_current_field_is_set_by_prepare_while_pending_and_kept_when_delivered(flow: Flow) -> None:
    """Campo corrente desde a variante: logo depois do `delivery.prepare`, com
    a `question` ainda sem desfecho (500 na 1ª tentativa), a entrega está `pending` com `variant_at` e
    `current_field` = 0, e o pedido do campo espera a `question` (ordem por entrega); depois do envio
    aceito, `delivered` com o mesmo campo corrente, e o pedido sai em resposta à questão, com
    `ForceReply` e a dica do campo."""
    flow.program("number", preset("number"))
    student = flow.student()
    d = flow.prepare("number", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    flow.rich.fail("sendRichMessage", status=500, times=1)
    t0 = flow.clock.now()
    flow.confirm(d)

    def retried() -> list[dict] | None:
        rows = flow.deliveries(d)
        if len(rows) != 1:
            return None
        msgs = flow.messages(rows[0]["id"])
        return msgs if msgs and msgs[0]["attempts"] == 1 and msgs[0]["state"] == "pending" else None

    question, prompt = flow.wait(retried)
    [pending] = flow.deliveries(d)
    assert (pending["state"], pending["variant_at"], pending["current_field"], pending["render_version"],
            pending["controls_version"], pending["sent_at"], pending["principal_message_id"]) == (
        "pending", t0, 0, 1, 1, None, None)
    assert (prompt["delivery_role"], prompt["operation"], prompt["field_index"], prompt["state"],
            prompt["attempts"]) == ("field_prompt", "send", 0, "pending", 0)

    flow.clock.advance(3)
    t1 = flow.clock.now()
    [delivered] = flow.wait_settled(d, 1)
    assert (delivered["state"], delivered["current_field"], delivered["sent_at"],
            delivered["principal_message_id"]) == ("delivered", 0, t1, question["id"])
    shown_question, shown_prompt = flow.sent_to(student.chat)
    assert shown_prompt.message_id > shown_question.message_id
    assert (shown_prompt.force_reply, shown_prompt.placeholder, shown_prompt.reply_parameters) == (
        True, "Número; ex.: 4.25, -17 ou 3/7", {"message_id": shown_question.message_id,
                                               "allow_sending_without_reply": True})
    for piece in ("Campo 1 de 1", "Velocidade:", "m/s"):
        assert piece in shown_prompt.plain_text


def test_send_outcome_blocks_on_403_and_refuses_on_other_definitive(flow: Flow) -> None:
    """Classificação do envio recusado: o 403 de bloqueio no envio da questão deixa o
    endereço `blocked` (regra dos bots, com `endpoint.blocked`) e a entrega
    `not_delivered/endpoint_blocked`; outro erro definitivo (400) dá `not_delivered/channel_refused`,
    com o endereço ainda `active`; as demais entregas seguem."""
    flow.program("single_choice", preset("single_choice"))
    fine, blocked, refused = flow.student(), flow.student(), flow.student()
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    flow.rich.block_chat(flow.bot, blocked.user)
    flow.rich.fail("sendRichMessage", status=400, times=1,
                   when=lambda call: _chat_of(call) == refused.chat)
    t0 = flow.clock.now()
    flow.confirm(d)
    rows = {r["enrollment_id"]: r for r in flow.wait_settled(d, 3)}

    assert rows[fine.enrollment_row]["state"] == "delivered"
    assert [(r["state"], r["undelivered_reason"], r["sent_at"], r["current_field"])
            for r in (rows[blocked.enrollment_row], rows[refused.enrollment_row])] == [
        ("not_delivered", "endpoint_blocked", None, None), ("not_delivered", "channel_refused", None, None)]
    endpoints = dict(flow.sql.execute("SELECT id, state FROM delivery_endpoints WHERE id = ANY(%s)",
                                      ([blocked.endpoint_row, refused.endpoint_row],)).fetchall())
    assert endpoints == {blocked.endpoint_row: "blocked", refused.endpoint_row: "active"}
    for student in (blocked, refused):
        [question] = flow.messages(rows[student.enrollment_row]["id"])
        assert (question["delivery_role"], question["state"]) == ("question", "failed")
        assert flow.sent_to(student.chat) == []
    events = flow.events(d, "delivery.not_delivered")
    assert sorted(events, key=lambda e: e[3]["person"]) == sorted([
        ("delivery.not_delivered", iso(t0), None,
         _delivery_event(flow, d, rows[blocked.enrollment_row], blocked, undelivered_reason="endpoint_blocked")),
        ("delivery.not_delivered", iso(t0), None,
         _delivery_event(flow, d, rows[refused.enrollment_row], refused, undelivered_reason="channel_refused"))],
        key=lambda e: e[3]["person"])
    blocked_events = [e for e in all_events(flow.client, ADMIN_TOKEN, "endpoint.blocked")
                      if e["data"]["person"] == blocked.pseudo_id]
    assert [(e["occurred_at"], e["data"]) for e in blocked_events] == [
        (iso(t0), {"offering_id": flow.offering["offering_id"], "person": blocked.pseudo_id,
                   "channel": "telegram"})]


# ---------------------------------------------------------------------------------------------
# Mídias e pedido de campo
# ---------------------------------------------------------------------------------------------


def test_media_go_before_the_question_as_photo_up_to_the_limit_and_document_above(flow: Flow) -> None:
    """Uma mensagem por mídia `ok`, antes da questão e do pedido de
    campo, na ordem da IR: até `photo_max_bytes`, `sendPhoto`; acima, `sendDocument`; os bytes vêm da
    IR, a legenda é `Figura N` com o `alt`; os bytes ficam uma vez só em `media_blobs` para as duas
    entregas, a IR guardada não leva `data_base64` e `load_ir` a recompõe inteira, sem o motor."""
    flow.program("with_media", TWO_MEDIA)
    students = [flow.student(), flow.student()]
    flow.audit_contents()
    d = flow.prepare("with_media", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d, delivery=dataclasses.replace(FAST, photo_max_bytes=PHOTO_LIMIT))
    flow.confirm(d)
    rows = {r["enrollment_id"]: r for r in flow.wait_settled(d, 2)}

    shas = {"m1": _sha(SMALL_PNG), "m2": _sha(LARGE_PNG)}
    blobs = flow.sql.execute("SELECT sha256, content_type, size_bytes, data FROM media_blobs ORDER BY size_bytes"
                             ).fetchall()
    assert [(b[0], b[1], b[2], bytes(b[3])) for b in blobs] == [
        (shas["m1"], "image/png", len(SMALL_PNG), SMALL_PNG), (shas["m2"], "image/png", len(LARGE_PNG), LARGE_PNG)]
    issued = {v.seed: v for v in flow.engine_fake.issued_variants("with_media")}
    audit = flow.audit_rows()
    for student in students:
        delivery = rows[student.enrollment_row]
        assert delivery["state"] == "delivered"
        variant = issued[_base36(delivery["seed"])]
        contents = flow.contents(delivery["id"])
        assert contents["media_refs"] == shas
        assert contents["ir"] == _without_media_bytes(variant.ir)
        with psycopg.connect(flow.db_url) as conn:
            assert load_ir(conn, delivery["id"]) == variant.ir
        msgs = flow.messages(delivery["id"])
        assert [(m["delivery_role"], m["operation"], m["media_id"], m["field_index"], m["state"]) for m in msgs] == [
            ("media", "send", "m1", None, "sent"), ("media", "send", "m2", None, "sent"),
            ("question", "send", None, None, "sent"), ("field_prompt", "send", None, 0, "sent")]
        assert delivery["principal_message_id"] == msgs[2]["id"]
        assert audit[delivery["id"]] == [("pending", delivery["variant_at"], 0, 1,
                                          ["media", "media", "question", "field_prompt"])]
        photo, document, question, prompt = flow.sent_to(student.chat)
        assert [m.method for m in (photo, document)] == ["sendPhoto", "sendDocument"]
        assert question.keyboard is not None and prompt.force_reply is True
        assert [(u.method, u.field, u.content_type, u.sha256) for u in flow.rich.uploads(flow.bot, student.chat)] == [
            ("sendPhoto", "photo", "image/png", shas["m1"]), ("sendDocument", "document", "image/png", shas["m2"])]
        assert photo.caption.startswith("Figura 1") and "Plano inclinado" in photo.caption
        assert document.caption.startswith("Figura 2") and "Gráfico da velocidade" in document.caption
        assert "Observe as duas figuras e responda." in question.plain_text
        assert [m["channel_ref"] for m in msgs] == [str(m.message_id) for m in (photo, document, question, prompt)]


def test_missing_media_becomes_a_textual_notice(flow: Flow) -> None:
    """A mídia `missing` da variante não é enviada nem guardada; a mensagem da
    questão leva o aviso `[Figura N indisponível]` no lugar, e a entrega fica `delivered`."""
    flow.program("missing_media", preset("missing_media"))
    student = flow.student()
    d = flow.prepare("missing_media", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    assert delivery["state"] == "delivered"
    assert flow.contents(delivery["id"])["media_refs"] == {}
    assert flow.sql.execute("SELECT count(*) FROM media_blobs").fetchone()[0] == 0
    assert [(m["delivery_role"], m["state"]) for m in flow.messages(delivery["id"])] == [
        ("question", "sent"), ("field_prompt", "sent")]
    question, _prompt = flow.sent_to(student.chat)
    assert deliverytexts.MEDIA_UNAVAILABLE.format(n=1) in question.plain_text
    assert flow.rich.uploads(flow.bot, student.chat) == []


@pytest.mark.parametrize("document", ["accepted", "refused"])
def test_photo_rejected_is_retried_once_as_document_in_the_same_send(flow: Flow, document: str) -> None:
    """Um 400 no `sendPhoto` leva a **um** `sendDocument` com os
    mesmos bytes e a mesma legenda, logo em seguida (mesma chamada ao canal, antes da questão); aceito,
    a mídia fica `sent` e a questão sai sem aviso; recusado também, a mídia falha de vez (`failed`,
    com o registro da fila de saída) e vira o aviso textual na questão, sem impedir a entrega."""
    flow.program("with_media", preset("with_media"))
    student = flow.student()
    d = flow.prepare("with_media", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    flow.rich.fail("sendPhoto", status=400, times=1)
    if document == "refused":
        flow.rich.fail("sendDocument", status=400, times=1)
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    assert delivery["state"] == "delivered"
    [photo_call] = [c for c in flow.calls_to(student.chat) if c.method == "sendPhoto"]
    [document_call] = [c for c in flow.calls_to(student.chat) if c.method == "sendDocument"]
    [question_call] = [c for c in flow.calls_to(student.chat) if c.method == "sendRichMessage"
                       and "force_reply" not in str(c.params.get("reply_markup"))]
    assert photo_call.status == 400 and photo_call.seq < document_call.seq < question_call.seq
    assert document_call.params["document"]["sha256"] == photo_call.params["photo"]["sha256"] == _sha(png_bytes())
    assert document_call.params.get("caption") == photo_call.params.get("caption")
    media_row = flow.messages(delivery["id"])[0]
    question = [m for m in flow.sent_to(student.chat) if m.method == "sendRichMessage"][0]
    notice = deliverytexts.MEDIA_UNAVAILABLE.format(n=1)
    if document == "accepted":
        assert (media_row["delivery_role"], media_row["state"], document_call.status) == ("media", "sent", 200)
        assert [u.method for u in flow.rich.uploads(flow.bot, student.chat)] == ["sendDocument"]
        assert notice not in question.plain_text
    else:
        assert (media_row["delivery_role"], media_row["state"], document_call.status) == ("media", "failed", 400)
        assert flow.rich.uploads(flow.bot, student.chat) == []
        assert notice in question.plain_text


def test_definitive_field_prompt_failure_does_not_change_the_delivery(flow: Flow) -> None:
    """O pedido de campo que falha de vez fica `failed` (registro da fila de saída),
    sem mensagem nova; a entrega continua `delivered`, com a mesma principal, o mesmo campo e as mesmas
    versões."""
    flow.program("number", preset("number"))
    student = flow.student()
    d = flow.prepare("number", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)

    def is_prompt(call: Any) -> bool:
        return "force_reply" in str(call.params.get("reply_markup"))

    flow.rich.fail("sendRichMessage", status=400, times=1, when=is_prompt)
    flow.rich.fail("sendMessage", status=400, times=1, when=is_prompt)
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    question, prompt = flow.messages(delivery["id"])
    assert [(m["delivery_role"], m["state"]) for m in (question, prompt)] == [("question", "sent"),
                                                                              ("field_prompt", "failed")]
    assert (delivery["state"], delivery["current_field"], delivery["render_version"], delivery["controls_version"],
            delivery["principal_message_id"]) == ("delivered", 0, 1, 1, question["id"])
    assert len(flow.sent_to(student.chat)) == 1
    assert flow.events(d, "delivery.not_delivered", "delivery.question_failed") == []


# ---------------------------------------------------------------------------------------------
# Falhas da variante
# ---------------------------------------------------------------------------------------------


def _first_failed_and_other(flow: Flow, d: Disp, before: int, students: list[Student]) -> tuple[dict, dict, Student]:
    """A entrega da 1ª criação de variante da abertura (a que recebeu a falha programada), a outra
    entrega e o aluno da primeira."""
    rows = flow.wait_settled(d, 2)
    first_seed = flow.create_variant_calls(before)[0].request["seed"]
    [failed] = [r for r in rows if _base36(r["seed"]) == first_seed]
    [other] = [r for r in rows if r is not failed]
    [student] = [s for s in students if s.enrollment_row == failed["enrollment_id"]]
    return failed, other, student


def _assert_question_failed(flow: Flow, d: Disp, failed: dict, other: dict, student: Student, failure: str,
                            at: datetime) -> None:
    assert (failed["state"], failed["failure"], failed["undelivered_reason"], failed["variant_at"],
            failed["sent_at"], failed["current_field"], failed["principal_message_id"]) == (
        "question_failed", failure, None, None, None, None, None)
    assert flow.contents(failed["id"]) is None and flow.messages(failed["id"]) == []
    assert flow.sent_to(student.chat) == []
    assert other["state"] == "delivered"
    assert flow.events(d, "delivery.question_failed") == [
        ("delivery.question_failed", iso(at), None, _delivery_event(flow, d, failed, student, failure=failure))]


@pytest.mark.parametrize(("body", "failure"), [(BROKEN_VARIANT, "variant_broken"),
                                               (UNSUPPORTED_VARIANT, "variant_unsupported")],
                         ids=["broken", "unsupported"])
def test_broken_or_unsupported_variant_fails_only_that_delivery(flow: Flow, body: dict, failure: str) -> None:
    """A variante quebrada ou não suportada na abertura deixa só
    aquela entrega em `question_failed` (`variant_broken`/`variant_unsupported`), sem conteúdo nem
    mensagem, com `delivery.question_failed`; a outra entrega é entregue."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student(), flow.student()]
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    before = len(flow.engine_fake.calls("create_variant"))
    flow.engine_fake.inject("create_variant", body=body, times=1)
    t0 = flow.clock.now()
    flow.confirm(d)
    failed, other, student = _first_failed_and_other(flow, d, before, students)
    _assert_question_failed(flow, d, failed, other, student, failure, t0)


def test_variant_above_channel_limits_fails_only_that_delivery(flow: Flow) -> None:
    """A variante da abertura que não cabe nos limites do canal (enunciado acima de
    32 768 no Rich) deixa só aquela entrega em `question_failed/channel_limits_exceeded`; a outra
    variante, que cabe, é entregue."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student(), flow.student()]
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    worker = flow.validated(d)
    flow.stop(worker)
    before = len(flow.engine_fake.calls("create_variant"))
    t0 = flow.clock.now()
    flow.confirm(d)
    flow.start(engine=EngineProxy(flow.engine(), oversize_on=1))
    failed, other, student = _first_failed_and_other(flow, d, before, students)
    _assert_question_failed(flow, d, failed, other, student, "channel_limits_exceeded", t0)


@pytest.mark.parametrize("status", [400, 413])
def test_engine_rejection_on_create_variant_is_engine_rejected(flow: Flow, status: int) -> None:
    """400/413 na abertura: `invalid_request` (400) ou `payload_too_large` (413) na
    criação da variante → `question_failed/engine_rejected` com `delivery.question_failed`, só naquela
    entrega (repetir não resolve)."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student(), flow.student()]
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    before = len(flow.engine_fake.calls("create_variant"))
    flow.engine_fake.fail("create_variant", error=status, times=1)
    t0 = flow.clock.now()
    flow.confirm(d)
    failed, other, student = _first_failed_and_other(flow, d, before, students)
    _assert_question_failed(flow, d, failed, other, student, "engine_rejected", t0)
    assert len([c for c in flow.create_variant_calls(before) if c.request["seed"] == _base36(failed["seed"])]) == 1


def test_variant_not_found_refetches_the_question_and_keeps_the_delivery_pending(flow: Flow) -> None:
    """Nova obtenção: `not_found` na criação da variante (conteúdo sumido do motor)
    enfileira `question.refetch:<ref>:r1` e a entrega continua `pending`, sem variante; a obtenção
    nova roda e a criação repetida entrega a questão."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    fetches = flow.fetcher.count(REPO, COMMIT)
    flow.engine_fake.fail("create_variant", error=404, times=1)
    flow.confirm(d)

    [task] = flow.wait(lambda: flow.tasks("question.refetch"))
    ref_row = flow.ref_row("single_choice")
    assert (task["payload"], task["idempotency_key"]) == ({"question_ref_row_id": ref_row},
                                                          f"question.refetch:{ref_row}:r1")
    [pending] = flow.deliveries(d)
    assert (pending["state"], pending["variant_at"], pending["current_field"]) == ("pending", None, None)
    assert flow.sent_to(student.chat) == []
    flow.wait(lambda: flow.tasks("question.refetch")[0]["state"] == "done")
    assert flow.fetcher.count(REPO, COMMIT) == fetches + 1

    flow.advance_until(lambda: flow.settled(d, 1), step=5)
    [delivery] = flow.deliveries(d)
    assert delivery["state"] == "delivered"
    assert len(flow.tasks("question.refetch")) == 1


def test_media_blocked_by_403_blocks_the_endpoint_and_the_question_is_not_sent(flow: Flow) -> None:
    """Um 403 de bloqueio no envio de uma mídia bloqueia o
    endereço (regra dos bots); a `question` seguinte não chama o canal, e a entrega fica
    `not_delivered/endpoint_blocked` (não `channel_refused`), com o pedido de campo cancelado."""
    flow.program("with_media", preset("with_media"))
    student = flow.student()
    d = flow.prepare("with_media", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d)
    flow.rich.fail("sendPhoto", error="blocked", times=1)
    t0 = flow.clock.now()
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    assert (delivery["state"], delivery["undelivered_reason"], delivery["sent_at"]) == (
        "not_delivered", "endpoint_blocked", None)
    assert flow.sql.execute("SELECT state FROM delivery_endpoints WHERE id = %s",
                            (student.endpoint_row,)).fetchone()[0] == "blocked"
    media, question, prompt = flow.messages(delivery["id"])
    assert (media["delivery_role"], media["state"], media["attempts"]) == ("media", "failed", 1)
    # A `question` chega ao canal com o endereço já bloqueado (sem chamada) ou é cancelada com a entrega.
    assert question["delivery_role"] == "question"
    assert (question["state"], question["attempts"]) in {("failed", 1), ("cancelled", 0)}
    assert (prompt["delivery_role"], prompt["state"], prompt["attempts"]) == ("field_prompt", "cancelled", 0)
    assert [c.method for c in flow.calls_to(student.chat)] == ["sendPhoto"]
    assert flow.events(d, "delivery.not_delivered") == [
        ("delivery.not_delivered", iso(t0), None,
         _delivery_event(flow, d, delivery, student, undelivered_reason="endpoint_blocked"))]


# ---------------------------------------------------------------------------------------------
# Janela perdida, alvo vazio, dialeto e ritmo
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [1, 61])
@pytest.mark.parametrize("kind", ["immediate", "scheduled"])
def test_opening_tolerates_a_short_delay_and_misses_a_five_minute_window(flow_factory: Callable[..., Flow],
                                                                          kind: str, delay: int) -> None:
    """Tolerância da abertura antes da janela perdida: com a janela de
    exatamente 5 min (a janela do oferecimento), imediato ou agendado, e a abertura processada
    `delay` s depois da confirmação (imediato) ou de `opens_at` (agendado): 1 s → abre normalmente;
    61 s → `cancelled/window_missed`, com `dispatch.cancelled`, sem entregas nem envio."""
    flow = flow_factory(window=5)
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    opens_at = None if kind == "immediate" else flow.clock.now() + timedelta(minutes=10)
    d = flow.prepare("single_choice", opens_at=opens_at)
    flow.stop(flow.validated(d))
    flow.confirm(d)
    opens_at = flow.dispatch(d)["opens_at"]
    assert flow.dispatch(d)["closes_at"] == opens_at + timedelta(minutes=5)
    flow.clock.set(opens_at + timedelta(seconds=delay))
    at = flow.clock.now()
    flow.start()

    if delay == 1:
        [delivery] = flow.wait_settled(d, 1)
        dispatch = flow.dispatch(d)
        assert (dispatch["state"], dispatch["opened_at"], delivery["state"]) == ("open", at, "delivered")
        assert len(flow.sent_to(student.chat)) == 1
    else:
        dispatch = flow.wait_state(d, "cancelled")
        assert (dispatch["cancel_reason"], dispatch["cancelled_at"], dispatch["opened_at"]) == (
            "window_missed", at, None)
        assert flow.deliveries(d) == [] and flow.calls_to(student.chat) == []
        assert flow.events(d, "dispatch.opened", "dispatch.cancelled") == [
            ("dispatch.cancelled", iso(at), None, {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
                                                   "cancel_reason": "window_missed"})]


def test_empty_target_opens_with_zero_and_closes_normally(flow: Flow) -> None:
    """Alvo vazio: sem nenhum aluno no alvo, o disparo abre com zero entregas
    (`dispatch.opened` com `target_count` 0) e fecha normalmente no horário (`dispatch.closed`,
    `trigger = window`, todas as contagens 0)."""
    flow.program("single_choice", preset("single_choice"))
    flow.student(enrollment="pending")
    t0 = flow.clock.now()
    closes_at = t0 + timedelta(minutes=30)
    d = flow.prepare("single_choice", closes_at=closes_at)
    flow.validated(d)
    assert flow.confirm(d)["target_count"] == 0
    flow.wait_state(d, "open")
    assert flow.deliveries(d) == []

    flow.clock.set(closes_at + timedelta(seconds=1))
    closed = flow.advance_until(lambda: flow.dispatch(d)["state"] == "closed" and flow.dispatch(d), step=6)
    assert closed["opened_at"] == t0 and closed["closed_at"] >= closes_at
    assert flow.events(d, "dispatch.opened", "dispatch.closed") == [
        ("dispatch.opened", iso(t0), None, {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
                                            "target_count": 0}),
        ("dispatch.closed", iso(closed["closed_at"]), None,
         {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id, "trigger": "window",
          "counts": {k: 0 for k in DELIVERY_COUNT_KEYS}})]


def test_html_format_setting_reaches_the_channel(flow: Flow) -> None:
    """Configuração de entrega até os tratadores: com `run_worker(…, delivery=DeliverySettings(
    telegram_format="html"))`, a questão sai por `sendMessage` com `parse_mode = "HTML"` e a prévia de
    link desligada, com as capacidades do HTML simples (botões, fórmula em `code`, sem `tg-math` nem
    `sendRichMessage`), e o dialeto gravado na entrega é `html`."""
    flow.program("single_choice", MATH_CHOICE)
    student = flow.student()
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d, delivery=dataclasses.replace(FAST, telegram_format="html"))
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    assert delivery["state"] == "delivered"
    assert flow.contents(delivery["id"])["rendered"]["format"] == "html"
    [msg] = flow.sent_to(student.chat)
    assert (msg.method, msg.parse_mode, msg.link_preview_options, msg.rich_message) == (
        "sendMessage", "HTML", {"is_disabled": True}, None)
    assert "<code>x^2</code>" in msg.text and "tg-math" not in msg.text
    token = delivery["button_token"]
    assert msg.keyboard == (
        tuple(Button(f"({k})", f"cq1:{token}:m:0:1:{k}") for k in ("a", "b", "c")),
        (Button(deliverytexts.SUBMIT_BUTTON, f"cq1:{token}:s:0:1"),),
        (Button(deliverytexts.DONT_KNOW_BUTTON, f"cq1:{token}:n:0:1"),))
    assert [c.method for c in flow.rich.calls() if c.method == "sendRichMessage"] == []


def test_pacing_respects_one_per_second_per_chat_over_accepted_sends(flow: Flow) -> None:
    """Com o padrão (1 mensagem por segundo por chat), os envios aceitos
    a um mesmo chat (mídia, questão e pedido de campo) saem com pelo menos ~1 s entre si; o `sendPhoto`
    recusado com 400 e o `sendDocument` que o substitui saem na mesma chamada ao canal, sem nova espera
    do ritmo, e o ritmo é medido sobre os aceitos."""
    assert (DeliverySettings().chat_interval_seconds, DeliverySettings().bot_per_second) == (1.0, 30)
    flow.program("with_media", preset("with_media"))
    retried, plain = flow.student(), flow.student()
    d = flow.prepare("with_media", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d, delivery=DeliverySettings())
    flow.rich.fail("sendPhoto", status=400, times=1, when=lambda call: _chat_of(call) == retried.chat)
    flow.confirm(d)
    flow.wait_settled(d, 2)

    for student in (retried, plain):
        accepted = flow.sent_to(student.chat)
        assert len(accepted) == 3
        gaps = [b.started - a.started for a, b in zip(accepted, accepted[1:])]
        assert all(gap >= 0.85 for gap in gaps), f"ritmo por chat desrespeitado: {gaps}"
    photo_call, document_call = [c for c in flow.calls_to(retried.chat) if c.method in ("sendPhoto", "sendDocument")]
    assert (photo_call.method, photo_call.status, document_call.method) == ("sendPhoto", 400, "sendDocument")
    assert document_call.started - photo_call.started < 0.5


def test_pacing_respects_the_per_bot_limit(flow: Flow) -> None:
    """Com `bot_per_second = 3`, nenhuma janela de 1 s tem mais de 3 envios
    aceitos do bot (quaisquer 4 envios seguidos ocupam ~1 s ou mais), e cada aluno recebe uma questão."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student() for _ in range(7)]
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.validated(d, delivery=DeliverySettings(bot_per_second=3))
    flow.confirm(d)
    flow.wait_settled(d, 7)

    assert all(len(flow.sent_to(s.chat)) == 1 for s in students)
    starts = sorted(m.started for m in flow.rich.messages(flow.bot))
    assert len(starts) == 7
    spans = [starts[i + 3] - starts[i] for i in range(len(starts) - 3)]
    assert all(span >= 0.85 for span in spans), f"limite por bot desrespeitado: {spans}"


# ---------------------------------------------------------------------------------------------
# Queda no meio da abertura
# ---------------------------------------------------------------------------------------------


def test_crash_in_the_middle_of_opening_leaves_no_duplicate_and_no_student_behind(flow: Flow) -> None:
    """Idempotência da abertura: o worker cai (`Crash`) depois de o motor
    criar a 2ª variante e antes de gravá-la: nada dessa entrega fica gravado pela metade (sem conteúdo,
    sem `variant_at`, sem mensagem); outro worker, depois da posse, retoma a mesma tarefa com a mesma
    seed; no fim, cada aluno tem exatamente uma entrega, um conteúdo, uma `question` e uma mensagem
    recebida, e cada conteúdo foi gravado com a sua mensagem na mesma transação."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student() for _ in range(3)]
    flow.audit_contents()
    d = flow.prepare("single_choice", closes_at=flow.clock.now() + timedelta(hours=2))
    flow.stop(flow.validated(d))
    before = len(flow.engine_fake.calls("create_variant"))
    flow.confirm(d)
    proxy = EngineProxy(flow.engine(), crash_on=2)
    crashed = flow.start(engine=proxy)
    flow.wait(lambda: not crashed.is_alive())
    assert isinstance(crashed.error, Crash), f"esperada a queda por Crash, veio {crashed.error!r}"

    rows = flow.deliveries(d)
    assert len(rows) == 3
    first, lost = proxy.seeds
    by_seed = {_base36(r["seed"]): r for r in rows}
    half = by_seed[lost]
    assert (half["state"], half["variant_at"], half["current_field"], half["render_version"]) == (
        "pending", None, None, 0)
    assert flow.contents(half["id"]) is None and flow.messages(half["id"]) == []
    assert flow.contents(by_seed[first]["id"]) is not None

    flow.clock.advance(EXPIRE)
    flow.start()
    rows = flow.wait_settled(d, 3)
    assert sorted(r["enrollment_id"] for r in rows) == sorted(s.enrollment_row for s in students)
    assert all(r["state"] == "delivered" for r in rows)
    seeds = [c.request["seed"] for c in flow.create_variant_calls(before)]
    assert sorted(seeds) == sorted([_base36(r["seed"]) for r in rows] + [lost])
    audit = flow.audit_rows()
    for row in rows:
        assert [m["delivery_role"] for m in flow.messages(row["id"])] == ["question"]
        assert [(a[0], a[4]) for a in audit[row["id"]]] == [("pending", ["question"])]
    assert all(len(flow.sent_to(s.chat)) == 1 for s in students)
    [task] = [t for t in flow.tasks("delivery.prepare") if t["payload"] == {"delivery_row_id": half["id"]}]
    assert (task["state"], task["attempts"]) == ("done", 2)
