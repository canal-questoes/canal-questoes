"""Botões inertes e resposta à prévia (relatórios e acompanhamento).


O que se confere:
- **toque** num botão da prévia (alternativa, "Enviar" e "Não sei"), em qualquer estado do disparo
  (agendado, aberto, fechado, cancelado) → `answerCallbackQuery` com `PREVIEW_TAP` e o desfecho
  `preview_tapped`, sem edição da mensagem, sem mensagem nova, sem entrega, submissão ou seleção (só o
  update fica registrado: além dele, só a tarefa `telegram.update` da recepção, sem evento, aviso ou outra
  tarefa);
- o toque depois do **arquivamento** do oferecimento: nenhum
  worker consulta o bot, nenhuma confirmação e nada gravado;
- o dado da prévia tocado no bot de **outro** oferecimento → `answer_ignored` (confirmação sem texto, como no disparo);
  o dado fora de `cq1:` → `not_handled`, sem confirmação;
- **resposta** por "responder" a cada mensagem da prévia (cabeçalho, mídia, questão, pedido) no Telegram e,
  **inclusive ao cabeçalho**, no console (o cabeçalho sai com referência numérica) → `PREVIEW_REPLY`
  pela fila (resposta ao chat do update, prioridade 2, chave `preview-reply:<update>`) e `preview_replied`;
- **controle negativo**: a resposta ao resumo de fechamento, ao aviso "aberto" e ao de
  rejeição → `not_handled`, sem mensagem; o texto sem "responder" de um membro da equipe → `not_handled`;
  nenhum texto da equipe vira resposta (nenhuma submissão);
- o dado de cada botão da prévia com até 64 bytes (e a Bot API falsa sem erro de política, pela fixture).

**Montagem.** Como em `test_dispatch_preview.py`: um oferecimento `active` com bot próprio na Bot API falsa
com a extensão do disparo, o worker completo dos relatórios (`report_worker`; no console, `console=True` com a
`ConsoleInbox`), a equipe simulada vinculada pelo link de equipe (`link_team`) e disparos agendados de alvo vazio
(sem alunos: abrir e fechar não envia nada a ninguém). Os QIDs são os dos presets do motor falso.

**Vermelho no esqueleto:** a tabela `team_notices` não existe e nenhuma prévia é enviada: cada teste falha
na primeira leitura das mensagens à equipe (`team_messages`, UndefinedTable), nunca na montagem. As
asserções sobre o comportamento que o esqueleto já tem (o `not_handled` do dado fora de `cq1:`,
o `answer_ignored`; o `PREVIEW_TAP` real) ficam na mesma função de um caso vermelho.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import time as time_module
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import noticetexts
from cq_server.adapters.console import ConsoleInbox
from cq_server.config import DeliverySettings
from cq_server.deliverytexts import DONT_KNOW_BUTTON, SUBMIT_BUTTON
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, cancel_dispatch, confirm_dispatch, create_offering,
                               prepare_dispatch, register_bot)
from tests.helpers.clock import iso
from tests.helpers.engine import FakeMedia, FakeQuestion, number_field, preset
from tests.helpers.team import ACTIVE, link_team, team_lines, team_messages

REPO = "https://repos.cq.test/disciplina/previa-botoes.git"
COMMIT = "b07a" * 10
TZ = "America/Sao_Paulo"
PREVIEW_KIND = "dispatch_preview"
WAIT = 25.0
QUIET = 3.0
FAST = DeliverySettings(chat_interval_seconds=0.05)
# Uma figura e um campo de número: a prévia tem cabeçalho, mídia, questão e pedido de campo.
WITH_MEDIA = FakeQuestion("Com figura", "Observe a figura e responda.", (number_field(),),
                          media=(FakeMedia("clientFilesQuestion/figura.png", alt="Plano inclinado"),),
                          extra_elements=("pl-figure",))
REPLY_TEXT = "Acho que a resposta é 6,5"

_codes = itertools.count(1)


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int
    opens_at: datetime | None
    closes_at: datetime | None


class Flow:
    """Um oferecimento ativo com bot, o worker dos relatórios e a equipe (Telegram ou console)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, rich: Any, engine_fake: Any,
                 report_worker: Any, db_url: str, console: bool = False) -> None:
        self.client, self.sql, self.clock, self.rich, self.engine_fake = client, sql, clock, rich, engine_fake
        self.console = console
        self.offering = create_offering(client, ADMIN_TOKEN, code=f"previa-botoes-{next(_codes)}", timezone=TZ)
        self.code = self.offering["code"]
        self.bot = rich.add_bot()
        register_bot(client, ADMIN_TOKEN, self.code, rich.token_of(self.bot))
        activate_bot(client, ADMIN_TOKEN, self.code)
        self.bot_row = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (self.bot,)).fetchone()[0]
        self.stream = io.StringIO()
        self.inbox = ConsoleInbox(db_url, clock=clock) if console else None
        self.worker = report_worker(console=console, stream=self.stream, delivery=FAST)
        if self.inbox is not None:
            self.team = link_team(client, sql, self.offering, self.bot, (ACTIVE,), inbox=self.inbox)
        else:
            self.team = link_team(client, sql, self.offering, self.bot, (ACTIVE,), bot_api=rich)
        self.member = self.team.active[0]

    def close(self) -> None:
        if self.inbox is not None:
            self.inbox.close()

    # -- disparos ------------------------------------------------------------------------------

    def prepare(self, qid: str, question: FakeQuestion, *, opens_in: timedelta | None = timedelta(minutes=10),
                window: timedelta = timedelta(minutes=30)) -> Disp:
        self.engine_fake.program((REPO, COMMIT, qid), question)
        now = self.clock.now()
        opens_at = None if opens_in is None else now + opens_in
        closes_at = (opens_at or now) + window
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at), closes_at=iso(closes_at))
        row = self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                               (body["dispatch_id"],)).fetchone()[0]
        return Disp(body["dispatch_id"], int(row), opens_at, closes_at)

    def state(self, d: Disp) -> str:
        return self.sql.execute("SELECT state FROM dispatches WHERE id = %s", (d.row,)).fetchone()[0]

    def wait(self, predicate: Callable[[], Any], timeout: float = WAIT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def wait_state(self, d: Disp, state: str) -> None:
        def ready() -> bool:
            current, rejection = self.sql.execute("SELECT state, rejection FROM dispatches WHERE id = %s",
                                                  (d.row,)).fetchone()
            assert current in ("validating", state), f"disparo em {current} (rejeição {rejection}), esperado {state}"
            return current == state
        self.wait(ready)

    def advance_until(self, predicate: Callable[[], Any], *, step: float = 15.0, tries: int = 60) -> Any:
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

    def to_state(self, d: Disp, state: str) -> None:
        """Leva o disparo agendado (alvo vazio) a `state`: `scheduled` (nada), `cancelled` (pela API), `open`
        (confirmado e aberto no horário) ou `closed` (aberto e fechado depois do `closes_at`)."""
        if state == "scheduled":
            return
        if state == "cancelled":
            assert cancel_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)["state"] == "cancelled"
            return
        confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)
        assert d.opens_at is not None and d.closes_at is not None
        self.clock.set(d.opens_at + timedelta(seconds=1))
        self.advance_until(lambda: self.state(d) == "open")
        if state == "closed":
            self.clock.set(d.closes_at + timedelta(seconds=1))
            self.advance_until(lambda: self.state(d) == "closed")

    # -- mensagens à equipe ----------------------------------------------------------------------

    def messages(self, kind: str | None = PREVIEW_KIND) -> list[Any]:
        return team_messages(self.sql, kind, self.member)

    def wait_sent(self, kind: str, count: int) -> list[Any]:
        return self.wait(lambda: (found := self.messages(kind)) and len(found) >= count
                         and all(m.state == "sent" for m in found) and found)

    def update_row(self, update_id: int) -> dict:
        def ready() -> dict | None:
            with self.sql.cursor(row_factory=dict_row) as cur:
                row = cur.execute("SELECT u.id, u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                                  "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (self.bot, update_id)).fetchone()
            return row if row is not None and row["state"] == "processed" else None
        return self.wait(ready)

    def last_update(self, after_id: int) -> dict:
        """O primeiro update do bot processado depois de `after_id` (entrada simulada do console)."""
        def ready() -> dict | None:
            with self.sql.cursor(row_factory=dict_row) as cur:
                row = cur.execute("SELECT id, state, outcome FROM telegram_updates WHERE bot_id = %s AND id > %s "
                                  "ORDER BY id LIMIT 1", (self.bot_row, after_id)).fetchone()
            return row if row is not None and row["state"] == "processed" else None
        return self.wait(ready)

    def max_update(self) -> int:
        return self.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates WHERE bot_id = %s",
                                (self.bot_row,)).fetchone()[0]

    def reply_rows(self, update_row_id: int) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT text, priority, idempotency_key, recipient_person_id, bot_id, notice_id, "
                               "delivery_endpoint_id, state FROM outbox_messages WHERE reply_to_update_id = %s "
                               "ORDER BY id", (update_row_id,)).fetchall()

    def outbox_count(self) -> int:
        return self.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0]

    def team_submissions(self) -> int:
        return self.sql.execute(
            "SELECT count(*) FROM submissions s JOIN deliveries v ON v.id = s.delivery_id "
            "JOIN enrollments e ON e.id = v.enrollment_id WHERE e.role = 'team'").fetchone()[0]

    def answers_to(self, callback_id: str) -> list[Any]:
        return [a for a in self.rich.answers(self.bot) if a.callback_id == callback_id]

    # -- instantâneos ---------------------------------------------------------------------

    def snapshot(self) -> dict[str, int]:
        """Os maiores ids (e o total da fila) das tabelas que um toque não pode fazer crescer, mais as
        tarefas executáveis agora (pelo relógio falso) e as em posse."""
        now = self.clock.now()
        row = self.sql.execute(
            "SELECT (SELECT coalesce(max(seq), 0) FROM events), (SELECT coalesce(max(id), 0) FROM tasks), "
            "(SELECT count(*) FROM outbox_messages), (SELECT coalesce(max(id), 0) FROM telegram_updates), "
            "(SELECT coalesce(max(id), 0) FROM team_notices), "
            "(SELECT count(*) FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s))",
            (now,)).fetchone()
        return dict(zip(("events", "tasks", "outbox", "updates", "notices", "runnable"), row, strict=True))

    def settle(self, extra: Callable[[], bool] = lambda: True, tries: int = 8) -> dict[str, int]:
        """Espera o mundo parado (com o relógio falso parado): nenhuma tarefa em posse, `extra()` verdadeiro e
        dois instantâneos iguais com `QUIET` s de intervalo; devolve o último."""
        for _ in range(tries):
            self.wait(lambda: extra() and self.sql.execute(
                "SELECT count(*) FROM tasks WHERE state = 'leased'").fetchone()[0] == 0)
            first = self.snapshot()
            time_module.sleep(QUIET)
            second = self.snapshot()
            if first == second and extra():
                return second
        raise AssertionError(f"o mundo não parou em {tries} tentativas")

    def new_tasks(self, baseline: dict[str, int]) -> list[tuple[str, Any]]:
        return [(t, p) for t, p in self.sql.execute("SELECT task_type, payload FROM tasks WHERE id > %s ORDER BY id",
                                                     (baseline["tasks"],)).fetchall()]

    def new_events(self, baseline: dict[str, int]) -> list[str]:
        return [r[0] for r in self.sql.execute("SELECT type FROM events WHERE seq > %s ORDER BY seq",
                                               (baseline["events"],)).fetchall()]

    def new_updates(self, baseline: dict[str, int]) -> list[tuple[int, int]]:
        """Os updates gravados depois do instantâneo, como (bot do Telegram, `update_id`)."""
        return [(int(b), int(u)) for b, u in self.sql.execute(
            "SELECT b.telegram_bot_id, u.update_id FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "WHERE u.id > %s ORDER BY u.id", (baseline["updates"],)).fetchall()]

    def last_call_seq(self) -> int:
        return max((c.seq for c in self.rich.calls()), default=0)


@pytest.fixture
def make_flow(client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, engine_fake: Any,
              report_worker: Any, db_url: str) -> Any:
    made: list[Flow] = []

    def make(**kwargs: Any) -> Flow:
        flow = Flow(client=client, sql=sql, clock=clock, rich=rich_bot_api, engine_fake=engine_fake,
                    report_worker=report_worker, db_url=db_url, **kwargs)
        made.append(flow)
        return flow

    yield make
    report_worker.stop()
    for flow in made:
        flow.close()


def _preview_question(flow: Flow, d: Disp) -> Any:
    """A mensagem da questão da prévia, como a Bot API falsa a guarda (depois de enviada)."""
    rows = flow.wait_sent(PREVIEW_KIND, 2)
    [question] = [r for r in rows if r.role == "question"]
    return flow.rich.current(flow.bot, flow.member.chat_id, int(question.channel_ref))


# ---------------------------------------------------------------------------------------------
# Toques
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["scheduled", "open", "closed", "cancelled"])
def test_preview_taps_get_preview_tap_in_any_dispatch_state(make_flow: Callable[..., Flow], state: str) -> None:
    """Com a prévia da escolha única entregue, o disparo vai a `state`;
    o toque numa alternativa, no "Enviar" e no "Não sei" da prévia → `answerCallbackQuery` com
    `PREVIEW_TAP` (uma vez por toque) e `preview_tapped`; nenhuma edição da mensagem (o teclado continua o
    mesmo), nenhuma mensagem nova na fila e nenhuma entrega, submissão ou seleção. `PREVIEW_TAP` (real no
    esqueleto) cabe em 200 caracteres."""
    flow = make_flow()
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    question = _preview_question(flow, d)
    before_keyboard = question.keyboard
    flow.to_state(d, state)
    assert flow.state(d) == state
    # As consequências do estado (avisos, congelamento) gravadas antes do instantâneo; o relógio
    # falso fica parado daqui em diante, então nada novo vence durante os toques.
    baseline = flow.settle()
    answers_before = len(flow.rich.answers(flow.bot))
    tapped: list[tuple[int, int]] = []
    outbox_before = flow.outbox_count()
    labels = {b.label: b.data for b in question.buttons()}
    for label in ("(b)", SUBMIT_BUTTON, DONT_KNOW_BUTTON):
        tap = flow.rich.tap(flow.bot, flow.member.user, question.message_id, labels[label])
        assert flow.update_row(tap.update_id)["outcome"] == "preview_tapped", label
        tapped.append((flow.update_row(tap.update_id)["id"], tap.update_id))
        found = flow.wait(lambda t=tap: flow.answers_to(t.callback_id))
        assert [a.text for a in found] == [noticetexts.PREVIEW_TAP], label
    time_module.sleep(1.0)
    assert flow.rich.edits(flow.bot, flow.member.chat_id, question.message_id) == []
    assert flow.rich.current(flow.bot, flow.member.chat_id, question.message_id).keyboard == before_keyboard
    assert flow.outbox_count() == outbox_before
    assert flow.sql.execute("SELECT count(*) FROM deliveries WHERE dispatch_id = %s", (d.row,)).fetchone()[0] == 0
    assert flow.team_submissions() == 0
    assert len(noticetexts.PREVIEW_TAP) <= 200
    # Sem criar registro além do update recebido: controle positivo, os 3 updates chegaram e
    # foram gravados; além deles, só a tarefa `telegram.update` que a recepção cria para cada update
    # ([F] `reception.py:253-257`) e a confirmação prevista (uma `answerCallbackQuery` por toque); nenhum
    # evento, aviso ou outra tarefa.
    time_module.sleep(QUIET)
    assert len({row for row, _ in tapped}) == 3
    assert flow.new_updates(baseline) == [(flow.bot, update_id) for _, update_id in tapped]
    assert flow.new_tasks(baseline) == [("telegram.update", {"update_row_id": row}) for row, _ in tapped]
    assert flow.new_events(baseline) == []
    after = flow.snapshot()
    assert (after["notices"], after["outbox"]) == (baseline["notices"], baseline["outbox"])
    assert len(flow.rich.answers(flow.bot)) == answers_before + 3


def test_preview_data_on_another_bot_is_ignored_and_non_cq1_data_is_not_handled(
        make_flow: Callable[..., Flow]) -> None:
    """O dado de um botão da prévia tocado no bot de outro
    oferecimento → `answer_ignored`, confirmado sem texto (o token não é de uma prévia desse oferecimento);
    no bot da prévia, um dado fora de `cq1:` → `not_handled`, sem confirmação; todo dado da
    prévia tem até 64 bytes."""
    flow = make_flow()
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    question = _preview_question(flow, d)
    datas = [b.data for b in question.buttons()]
    assert datas and all(data.startswith("cq1:") and len(data.encode("utf-8")) <= 64 for data in datas)

    other = create_offering(flow.client, ADMIN_TOKEN, code=f"previa-outro-{next(_codes)}", timezone=TZ)
    other_bot = flow.rich.add_bot()
    register_bot(flow.client, ADMIN_TOKEN, other["code"], flow.rich.token_of(other_bot))
    activate_bot(flow.client, ADMIN_TOKEN, other["code"])
    flow.wait(lambda: flow.rich.pending_polls(other_bot))
    tap = flow.rich.tap_raw(other_bot, flow.member.user, question.message_id, datas[0])

    def other_row() -> dict | None:
        with flow.sql.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                              "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (other_bot, tap.update_id)).fetchone()
        return row if row is not None and row["state"] == "processed" else None

    assert flow.wait(other_row)["outcome"] == "answer_ignored"
    answered = flow.wait(lambda: [a for a in flow.rich.answers(other_bot) if a.callback_id == tap.callback_id])
    assert [a.text for a in answered] == [None]

    raw = flow.rich.tap_raw(flow.bot, flow.member.user, question.message_id, "fora-do-formato")
    assert flow.update_row(raw.update_id)["outcome"] == "not_handled"
    time_module.sleep(1.0)
    assert flow.answers_to(raw.callback_id) == []


def test_preview_tap_after_archiving_reaches_no_worker_and_records_nothing(make_flow: Callable[..., Flow]) -> None:
    """Toque depois do arquivamento:
    com a prévia entregue, um toque antes do arquivamento é consultado e confirmado
    (controle positivo: `getUpdates`, `sendMessage` e `answerCallbackQuery` do bot existem); o oferecimento é
    encerrado (o disparo agendado vira `cancelled`) e arquivado (a posse do bot é solta). Esperado o
    estado (nenhum long poll do bot aberto, o mundo parado), um toque num botão da prévia chega à Bot API
    falsa e fica sem resposta: nenhum `getUpdates` do bot depois disso, nenhuma `answerCallbackQuery`,
    `send*` ou `edit*` do bot depois do arquivamento (o `setMyDescription` da descrição arquivada
    fica fora) e nada gravado (updates, tarefas, eventos, avisos, mensagens na fila)."""
    from tests.helpers.api import auth

    flow = make_flow()
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    question = _preview_question(flow, d)
    labels = {b.label: b.data for b in question.buttons()}
    # Controle positivo: antes do arquivamento, o bot é consultado, envia e confirma o toque da prévia.
    before = flow.rich.tap(flow.bot, flow.member.user, question.message_id, labels["(b)"])
    assert flow.update_row(before.update_id)["outcome"] == "preview_tapped"
    assert [a.text for a in flow.wait(lambda: flow.answers_to(before.callback_id))] == [noticetexts.PREVIEW_TAP]
    methods_before = {c.method for c in flow.rich.calls(bot_id=flow.bot)}
    assert {"getUpdates", "sendMessage", "answerCallbackQuery"} <= methods_before, sorted(methods_before)

    headers = auth(ADMIN_TOKEN)
    closed = flow.client.post(f"/v1/offerings/{flow.code}/close", headers=headers)
    assert closed.status_code == 200, closed.text
    assert flow.state(d) == "cancelled"
    prepared = flow.client.post(f"/v1/offerings/{flow.code}/archive/prepare", headers=headers)
    assert prepared.status_code == 200, prepared.text
    archived = flow.client.post(f"/v1/offerings/{flow.code}/archive",
                                json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=headers)
    assert archived.status_code == 200 and archived.json()["state"] == "archived", archived.text
    archived_mark = flow.last_call_seq()

    # Espera o estado, não o relógio: o long poll em curso no arquivamento termina e o mundo para.
    baseline = flow.settle(lambda: not flow.rich.pending_polls(flow.bot))
    polls_mark = flow.last_call_seq()
    tap = flow.rich.tap(flow.bot, flow.member.user, question.message_id, labels[SUBMIT_BUTTON])
    # Mais que o long poll do teste (1 s) somado à espera da reconciliação: um worker que consultasse o bot o faria.
    time_module.sleep(QUIET)

    calls = flow.rich.calls(bot_id=flow.bot)
    assert [c.method for c in calls if c.seq > polls_mark and c.method == "getUpdates"] == []
    assert [c.method for c in calls if c.seq > archived_mark and (
        c.method == "answerCallbackQuery" or c.method.startswith("send") or c.method.startswith("edit"))] == []
    assert flow.rich.pending_polls(flow.bot) == []
    assert flow.answers_to(tap.callback_id) == []
    assert flow.sql.execute("SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND update_id = %s",
                            (flow.bot_row, tap.update_id)).fetchone()[0] == 0
    assert flow.new_updates(baseline) == []
    assert flow.new_tasks(baseline) == []
    assert flow.new_events(baseline) == []
    assert flow.snapshot() == baseline


# ---------------------------------------------------------------------------------------------
# Resposta por "responder"
# ---------------------------------------------------------------------------------------------


def test_reply_to_each_preview_message_on_telegram_gets_preview_reply(make_flow: Callable[..., Flow]) -> None:
    """A resposta por "responder" ao cabeçalho, à figura, à questão e
    ao pedido de campo da prévia → `preview_replied` e uma mensagem `PREVIEW_REPLY` pela fila, em resposta
    ao chat do update (sem pessoa, com o bot, prioridade 2, chave `preview-reply:<update>`), enviada ao chat
    do membro; nenhuma submissão."""
    flow = make_flow()
    d = flow.prepare("with_media", WITH_MEDIA)
    flow.wait_state(d, "scheduled")
    rows = flow.wait_sent(PREVIEW_KIND, 4)
    assert [r.role for r in rows] == [None, "media", "question", "field_prompt"]
    for row in rows:
        sent_before = [m for m in flow.rich.messages(flow.bot, flow.member.chat_id)
                       if m.text == noticetexts.PREVIEW_REPLY]
        update_id = flow.rich.push_text(flow.bot, flow.member.user, REPLY_TEXT,
                                        reply_to_message_id=int(row.channel_ref))
        update = flow.update_row(update_id)
        assert update["outcome"] == "preview_replied", row.role
        replies = flow.wait(lambda u=update: (found := flow.reply_rows(u["id"])) and found[0]["state"] == "sent" and found)
        assert [(r["text"], r["priority"], r["idempotency_key"], r["recipient_person_id"], r["bot_id"],
                 r["delivery_endpoint_id"]) for r in replies] == [
            (noticetexts.PREVIEW_REPLY, 2, f"preview-reply:{update['id']}", None, flow.bot_row, None)]
        sent_after = [m for m in flow.rich.messages(flow.bot, flow.member.chat_id)
                      if m.text == noticetexts.PREVIEW_REPLY]
        assert len(sent_after) == len(sent_before) + 1
    assert flow.team_submissions() == 0


def test_reply_on_console_including_the_header_gets_preview_reply(make_flow: Callable[..., Flow]) -> None:
    """Pelo console, toda mensagem da prévia sai com referência numérica,
    **inclusive o cabeçalho** (texto simples com `notice_id` da prévia, `answerable`); a resposta a cada uma
    pela entrada simulada (`reply_to`) → `preview_replied` e `PREVIEW_REPLY` pela fila (prioridade 2, chave
    `preview-reply:<update>`)."""
    flow = make_flow(console=True)
    d = flow.prepare("number", preset("number"))
    flow.wait_state(d, "scheduled")
    rows = flow.wait_sent(PREVIEW_KIND, 3)
    assert [r.role for r in rows] == [None, "question", "field_prompt"]
    lines = flow.wait(lambda: (found := [line for line in team_lines(flow.stream, flow.member)
                                         if line.message_id in {r.message_id for r in rows}])
                      and len(found) == 3 and found)
    assert all(line.answerable for line in lines), [line.channel_ref for line in lines]
    assert lines[0].text.startswith("Prévia do disparo: ")
    assert flow.inbox is not None
    for line in lines:
        mark = flow.max_update()
        flow.inbox.send_text(flow.bot_row, flow.member.chat_id, REPLY_TEXT, reply_to=line.channel_ref)
        update = flow.last_update(mark)
        assert update["outcome"] == "preview_replied", line.channel_ref
        replies = flow.wait(lambda u=update: flow.reply_rows(u["id"]))
        assert [(r["text"], r["priority"], r["idempotency_key"]) for r in replies] == [
            (noticetexts.PREVIEW_REPLY, 2, f"preview-reply:{update['id']}")]
    assert flow.team_submissions() == 0


def test_replies_to_other_team_notices_and_plain_texts_are_not_handled(make_flow: Callable[..., Flow]) -> None:
    """Controle negativo: a resposta por "responder" ao aviso de rejeição, ao aviso
    "aberto" e ao resumo de fechamento → `not_handled`, sem mensagem; um texto sem "responder" de um membro
    da equipe → `not_handled`, sem mensagem; nenhum texto da equipe vira resposta (nenhuma submissão)."""
    flow = make_flow()
    rejected = flow.prepare("broken_on_variant", preset("broken_on_variant"))
    flow.wait_state(rejected, "rejected")
    [rejection] = flow.wait_sent("dispatch_rejected", 1)

    d = flow.prepare("single_choice", preset("single_choice"), opens_in=None, window=timedelta(minutes=10))
    flow.wait_state(d, "scheduled")
    confirm_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    flow.advance_until(lambda: flow.state(d) == "open", step=5.0)
    [opened] = flow.wait_sent("dispatch_opened", 1)
    assert d.closes_at is not None
    flow.clock.set(d.closes_at + timedelta(seconds=1))
    flow.advance_until(lambda: flow.state(d) == "closed")
    [closed] = flow.advance_until(lambda: (found := flow.messages("dispatch_closed")) and found[0].state == "sent"
                                  and found, step=60.0)

    for notice in (rejection, opened, closed):
        before = flow.outbox_count()
        update_id = flow.rich.push_text(flow.bot, flow.member.user, REPLY_TEXT,
                                        reply_to_message_id=int(notice.channel_ref))
        update = flow.update_row(update_id)
        assert update["outcome"] == "not_handled", notice.kind
        assert flow.reply_rows(update["id"]) == []
        assert flow.outbox_count() == before
    plain = flow.rich.push_text(flow.bot, flow.member.user, "Bom dia, equipe")
    update = flow.update_row(plain)
    assert update["outcome"] == "not_handled"
    time_module.sleep(1.0)
    assert flow.reply_rows(update["id"]) == []
    assert flow.team_submissions() == 0
