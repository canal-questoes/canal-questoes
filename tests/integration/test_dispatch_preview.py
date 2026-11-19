"""Prévia renderizada do disparo no chat da equipe: o momento e o conteúdo da prévia, a sua origem e a
falha ao gerá-la, as questões com várias partes, os botões inertes, a dica do pedido de campo, as mídias
e a desistência, e o motor fora de transação.

O que se confere:
- **amostra** (`previews.record_sample`, chamada pela validação do disparo dentro do `if updated:`): só o
  disparo que passa a `scheduled` ganha `dispatch_previews` (a `seed` = `int(seed, 36)` da cadeia que foi
  ao motor na validação, o `button_token` de 22 caracteres, `created_at` = `validated_at`) e
  `dispatch_preview_contents.ir` (a IR emitida pelo motor, sem `data_base64`); o rejeitado não;
- **tarefa** `preview.send`: uma `create_variant(ref, _base36(seed))` por execução, com a seed gravada,
  inclusive a seed alta 4294967295 (`1z141z3`), que reproduz a variante validada;
- **cabeçalho**: título, abertura e fechamento no fuso (ou
  `PREVIEW_IMMEDIATE`), alunos-alvo, `PREVIEW_PARTS` com N ≥ 2, os avisos pelo tipo
  (`CORE_WARNING_MESSAGES`/`PREVIEW_ENGINE_WARNINGS`, nunca a mensagem do motor) e
  `cq dispatches confirm <código> <dispatch_id>`; nenhum botão de confirmação; o título longo truncado só
  no campo variável, cabendo em 4096, com as contagens e o comando inteiros;
- **mensagens da questão**: escolha única, múltipla, número e várias partes, cada uma comparada
  com o `deliveries._render` da entrega sobre o mesmo `_View` (estado inicial, o token da prévia), afirmando só
  as diferenças declaradas: a linha `PREVIEW_FIELD_HINT` e a ausência de
  `ForceReply` no pedido de campo e a ausência de "Prazo:" no imediato sem fechamento; com
  controle positivo (uma diferença não declarada é acusada pela comparação);
- **mídias e pedido**: a legenda (`deliveries._caption`/`_figure_number`), a escolha foto ×
  documento pelo `photo_max_bytes` do `DeliverySettings` do worker (uma imagem acima do limite vai como
  documento) e o texto do pedido (`deliverytexts.field_request`) iguais aos da entrega para a mesma variante;
  a ordem das mensagens por destinatário (cabeçalho, mídias, questão, pedido), as chaves
  `notice:<id>:p<pessoa>:<papel>[:<mN|i>]` e a prioridade 2; a mídia ausente não vira mensagem;
- **sem gabarito** com `show_correct_answer`: a sentinela do gabarito do motor falso em nenhuma
  mensagem à equipe; controle positivo: ela está na amostra gravada (`dispatch_preview_contents.ir`);
- **reexecução**: sem aviso, seed, entrega, submissão ou seleção novos; a prévia
  fora do alvo e das contagens do disparo;
- **reanimação**: a prévia devida sem tarefa viva (a r1 terminou `failed`) é
  reanimada pela varredura uma única vez (`:r2`) e conclui, sem duplicar aviso, mensagem ou evento;
- **desistências**: o motor fora do ar → rodadas `failed` e, depois de 5, `gave_up` com o
  `notice.recorded`, sem mensagem parcial; a prévia que só roda depois da abertura → `gave_up`; sem
  destinatário → `no_recipient` sem chamar o motor; `EngineNotFound` → o `question.refetch` do disparo e a
  rodada `failed`;
- **concorrência**: o motor lento segura a `create_variant` da prévia e uma confirmação
  concorrente (outra conexão, pela API) termina sem esperar o motor.

**Montagem.** Um oferecimento `active` com bot próprio na Bot API falsa com a extensão do disparo
(`rich_bot_api`), fuso `America/Sao_Paulo` e janela de 45 min; o worker completo (fixture
`report_worker`: `run_worker` com bots, o motor falso estrito pelo adaptador HTTP real e a obtenção falsa); a
equipe simulada vinculada pelos bots com esse worker no ar (`tests/helpers/team.py`, `link_team`); os alunos
por SQL nas colunas do esquema do banco (como `test_dispatch_opening.py`). Disparos **agendados** (abertura e
fechamento informados) salvo no teste do imediato. Ids, seeds, tokens e `message_id` nunca são literais:
vêm do banco, do dublê ou da relação entre eles.

**Leituras [I] deste teste** (o conteúdo é fixo, a grafia não): os horários do
cabeçalho saem como `timezones.format_local` (o formato dos textos fixos ao aluno, o mesmo do "Prazo:");
as linhas opcionais do cabeçalho entram antes da última (a do comando), primeiro `PREVIEW_PARTS` e depois
os avisos, na ordem dos avisos da prévia do disparo (`dispatches.get`); o texto é conferido linha a linha
sobre o texto lido pela Bot API falsa (sem as linhas vazias); a linha `PREVIEW_FIELD_HINT` é a última do
pedido de campo.

**Vermelho no esqueleto:** as tabelas `dispatch_previews`, `dispatch_preview_contents` e `team_notices`
não existem (migração `0005` ausente) e a validação não grava a amostra: cada teste falha na primeira
leitura dessas tabelas (UndefinedTable), depois de a validação do disparo levar o disparo a `scheduled` (ou a
`rejected`), nunca na montagem.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import html
import itertools
import re
import secrets as real_secrets
import time as time_module
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import deliverytexts, noticetexts
from cq_server.config import DeliverySettings
from cq_server.dispatches import CORE_WARNING_MESSAGES
from cq_server.timezones import format_local
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, confirm_dispatch, create_offering,
                               get_dispatch, prepare_dispatch, register_bot, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.engine import (ANSWER_SENTINEL, FakeMedia, FakeQuestion, number_field, png_bytes, preset)
from tests.helpers.team import ACTIVE, NO_ENDPOINT, link_team, team_messages

REPO = "https://repos.cq.test/disciplina/previa.git"
COMMIT = "9e1f" * 10
TZ = "America/Sao_Paulo"
WINDOW_MINUTES = 45
PREVIEW_KIND = "dispatch_preview"
PREVIEW_TASK = "preview.send"
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{22}")
WAIT = 25.0
QUIET = 3.0
TEXT_LIMIT = 4096
# A seed alta (2³² − 1) em base 36 canônica.
HIGH_SEED = 4_294_967_295
HIGH_SEED36 = "1z141z3"
assert int(HIGH_SEED36, 36) == HIGH_SEED

# Os QIDs são os dos presets do motor falso (a árvore `course` da obtenção falsa tem uma questão por preset);
# o comportamento de cada um é o programado pelo teste (`FakeQuestion`).
# Mídias: a primeira abaixo do limite de foto do worker do teste (foto), a segunda acima (documento).
SMALL_PNG = png_bytes()
LARGE_PNG = png_bytes(3000)
PHOTO_LIMIT = 1000
assert len(SMALL_PNG) <= PHOTO_LIMIT < len(LARGE_PNG)
TWO_MEDIA = FakeQuestion("Duas figuras", "Observe as duas figuras e responda.",
                         (number_field(hint="Número em m/s; ex.: 4.25"),),
                         media=(FakeMedia("clientFilesQuestion/pequena.png", alt="Plano inclinado", data=SMALL_PNG),
                                FakeMedia("clientFilesQuestion/grande.png", alt="Gráfico da velocidade",
                                          data=LARGE_PNG)),
                         extra_elements=("pl-figure",))
FAST = DeliverySettings(chat_interval_seconds=0.05, photo_max_bytes=PHOTO_LIMIT)

_codes = itertools.count(1)
_users = itertools.count(1)


def _lines(text: str | None) -> list[str]:
    return [line for line in (text or "").split("\n") if line.strip()]


def _after_first(qid: str) -> Callable[[Any], bool]:
    """Predicado do motor falso: as chamadas a `create_variant` da questão `qid` depois da primeira (a da
    validação); avaliado uma vez por chamada (`FakeEngine._take`)."""
    seen = {"n": 0}

    def when(call: Any) -> bool:
        if not isinstance(call.request, dict) or call.request.get("question_ref", {}).get("qid") != qid:
            return False
        seen["n"] += 1
        return seen["n"] >= 2
    return when


def _without_media_bytes(ir: dict) -> dict:
    out = copy.deepcopy(ir)
    for media in out.get("media") or []:
        media.pop("data_base64", None)
    return out


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int
    qid: str
    opens_at: datetime | None
    closes_at: datetime | None


class Flow:
    """Um oferecimento ativo com bot, o worker completo, a equipe e os disparos do teste (ver o docstring)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, rich: Any, engine_fake: Any,
                 report_worker: Any, delivery: DeliverySettings = FAST) -> None:
        self.client, self.sql, self.clock, self.rich, self.engine_fake = client, sql, clock, rich, engine_fake
        self.offering = create_offering(client, ADMIN_TOKEN, code=f"previa-disparo-{next(_codes)}", timezone=TZ,
                                        response_window_minutes=WINDOW_MINUTES)
        self.code = self.offering["code"]
        self.bot = rich.add_bot()
        register_bot(client, ADMIN_TOKEN, self.code, rich.token_of(self.bot))
        activate_bot(client, ADMIN_TOKEN, self.code)
        self.offering_row, self.bot_row = sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE b.telegram_bot_id = %s",
            (self.bot,)).fetchone()
        self.worker = report_worker(delivery=delivery)
        self.team: Any = None
        self.students: list[Any] = []

    # -- montagem ------------------------------------------------------------------------------

    def link(self, states: tuple[str, ...] = (ACTIVE, ACTIVE)) -> Any:
        self.team = link_team(self.client, self.sql, self.offering, self.bot, states, bot_api=self.rich)
        return self.team

    def student(self) -> Any:
        """Um aluno com matrícula `active` (termo aceito) e endereço ativo no bot, por SQL."""
        n = next(_users)
        user = make_user(50_000 + n)
        person = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Previa {n}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (person["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment = self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, terms_version, "
            "terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) RETURNING id",
            (person_row, self.offering_row, now, now, now)).fetchone()[0]
        self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_row, self.bot_row, user.user_id, now, now))
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        self.students.append((person["pseudo_id"], enrollment, user))
        return person

    def program(self, qid: str, question: FakeQuestion) -> None:
        self.engine_fake.program((REPO, COMMIT, qid), question)

    def prepare(self, qid: str, question: FakeQuestion, *, opens_in: timedelta | None = timedelta(days=2),
                window: timedelta | None = timedelta(hours=1)) -> Disp:
        """Prepara o disparo da questão `qid`: agendado para `opens_in` com `window` de janela (padrão) ou,
        com `opens_in=None`, imediato (com `closes_at` = agora + `window`, ou sem fechamento informado)."""
        self.program(qid, question)
        now = self.clock.now()
        opens_at = None if opens_in is None else now + opens_in
        closes_at = None if window is None else (opens_at or now) + window
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at),
                                closes_at=None if closes_at is None else iso(closes_at))
        row = self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                               (body["dispatch_id"],)).fetchone()[0]
        return Disp(body["dispatch_id"], int(row), qid, opens_at, closes_at)

    # -- espera --------------------------------------------------------------------------------

    def wait(self, predicate: Callable[[], Any], timeout: float = WAIT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def state(self, d: Disp) -> str:
        return self.sql.execute("SELECT state FROM dispatches WHERE id = %s", (d.row,)).fetchone()[0]

    def wait_state(self, d: Disp, state: str) -> None:
        """Espera o disparo chegar a `state` saindo de `validating`; falha logo noutro estado final."""
        def ready() -> bool:
            current, rejection, cancel = self.sql.execute(
                "SELECT state, rejection, cancel_reason FROM dispatches WHERE id = %s", (d.row,)).fetchone()
            assert current in ("validating", state), (
                f"disparo em {current} (rejeição {rejection}, cancelamento {cancel}), esperado {state}")
            return current == state
        self.wait(ready)

    def advance_until(self, predicate: Callable[[], Any], *, step: float, tries: int = 60) -> Any:
        """Espera `predicate()`; sem ele em 1,5 s reais, avança o relógio `step` s."""
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

    # -- consultas -----------------------------------------------------------------------------

    def _rows(self, query: str, params: tuple) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchall()

    def sample(self, d: Disp) -> dict | None:
        """A amostra (`dispatch_previews` + `dispatch_preview_contents`) do disparo, ou `None`."""
        rows = self._rows("SELECT p.id, p.seed, p.button_token, p.created_at, c.ir, c.media_refs, c.rendered "
                          "FROM dispatch_previews p LEFT JOIN dispatch_preview_contents c ON c.preview_id = p.id "
                          "WHERE p.dispatch_id = %s", (d.row,))
        assert len(rows) <= 1
        return rows[0] if rows else None

    def notices(self, d: Disp) -> list[dict]:
        return self._rows("SELECT id, kind, subject_key, state, recipient_count, dispatch_id, report_id, local_date, "
                          "finished_at FROM team_notices WHERE dispatch_id = %s AND kind = %s ORDER BY id",
                          (d.row, PREVIEW_KIND))

    def wait_notice(self, d: Disp, state: str) -> dict:
        def ready() -> dict | None:
            rows = self.notices(d)
            return rows[0] if rows and rows[0]["state"] == state else None
        return self.wait(ready)

    def preview_messages(self, member: Any = None) -> list[Any]:
        return team_messages(self.sql, PREVIEW_KIND, member)

    def wait_sent(self, member: Any, count: int) -> list[Any]:
        """As mensagens da prévia ao membro, quando há `count` e todas `sent`."""
        return self.wait(lambda: (found := self.preview_messages(member)) and len(found) == count
                         and all(m.state == "sent" for m in found) and found)

    def received(self, member: Any, message: Any) -> Any:
        """A mensagem aceita pela Bot API falsa que corresponde à linha da fila (`channel_ref`)."""
        assert message.channel_ref is not None and message.channel_ref.isdigit(), message
        return self.rich.current(self.bot, member.chat_id, int(message.channel_ref))

    def variant_calls(self, qid: str) -> list[Any]:
        return [c for c in self.engine_fake.calls("create_variant")
                if isinstance(c.request, dict) and c.request.get("question_ref", {}).get("qid") == qid]

    def preview_tasks(self) -> list[dict]:
        return self._rows("SELECT id, state, payload, attempts FROM tasks WHERE task_type = %s ORDER BY id",
                          (PREVIEW_TASK,))

    def keys(self, notice_id: int) -> dict[int, str]:
        return dict(self.sql.execute("SELECT id, idempotency_key FROM outbox_messages WHERE notice_id = %s",
                                     (notice_id,)).fetchall())

    def person_row(self, member: Any) -> int:
        return self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (member.pseudo_id,)).fetchone()[0]

    def preview_events(self, d: Disp) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "seq"} for e in all_events(self.client, ADMIN_TOKEN, "notice.recorded")
                if e["data"]["notice_type"] == PREVIEW_KIND and e["data"]["dispatch_id"] == d.id]

    def recorded(self, d: Disp, state: str, recipient_count: int, at: datetime) -> dict:
        return {"type": "notice.recorded", "occurred_at": iso(at), "actor_token_id": None,
                "data": {"offering_id": self.offering["offering_id"], "notice_type": PREVIEW_KIND, "dispatch_id": d.id,
                         "report_version": None, "episode_seq": None, "batch_id": None, "window_started_at": None,
                         "local_date": None, "state": state, "recipient_count": recipient_count}}

    # -- esperado (a entrega ao aluno sobre o mesmo `_View`) ------------------------------------------

    def expected_question(self, d: Disp, sample: dict) -> tuple[str, list[list[tuple[str, str]]] | None]:
        """O texto (markup Rich) e o teclado que o `_render` da entrega dá ao estado inicial da variante da amostra,
        com o token da prévia."""
        from cq_server.bots import telegram_capabilities
        from cq_server.deliveries import _render, _View, build_rendered
        from cq_server.telegramrender import TelegramRichFormatter

        formatter = TelegramRichFormatter()
        rendered = build_rendered(sample["ir"], formatter=formatter, capabilities=telegram_capabilities(FAST))
        single = self.sql.execute("SELECT coalesce(q.single_variant, false) FROM dispatches s "
                                  "JOIN question_refs q ON q.id = s.question_ref_id WHERE s.id = %s",
                                  (d.row,)).fetchone()[0]
        view = _View(state="pending", current_field=0, controls_version=1, render_version=0,
                     button_token=sample["button_token"], revealed=False, dispatch_state="open",
                     closes_at=d.closes_at, timezone=TZ, single_variant=bool(single), rendered=rendered)
        composed, keyboard, fits = _render(view, formatter)
        assert fits
        rows = None if keyboard is None else [[(b.label, b.data) for b in row] for row in keyboard]
        return composed.markup, rows

    def expected_header(self, d: Disp, *, parts: int = 1) -> list[str]:
        """As linhas do cabeçalho (leituras [I] do docstring)."""
        body = get_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)
        preview = body["preview"]
        title = preview["title"] if preview["title"] is not None else noticetexts.UNTITLED
        if d.opens_at is None and d.closes_at is None:
            text = noticetexts.PREVIEW_IMMEDIATE.format(title=title, minutes=WINDOW_MINUTES,
                                                        target=preview["target_count"], code=self.code,
                                                        dispatch_id=d.id)
        else:
            assert d.opens_at is not None and d.closes_at is not None
            text = noticetexts.PREVIEW_HEADER.format(title=title, opens_at=format_local(d.opens_at, TZ),
                                                     closes_at=format_local(d.closes_at, TZ),
                                                     target=preview["target_count"], code=self.code, dispatch_id=d.id)
        lines = _lines(text)
        optional = [noticetexts.PREVIEW_PARTS.format(parts=parts)] if parts >= 2 else []
        for warning in preview["warnings"]:
            kind = warning["kind"]
            optional.append(CORE_WARNING_MESSAGES[kind] if kind in CORE_WARNING_MESSAGES
                            else noticetexts.PREVIEW_ENGINE_WARNINGS[kind])
        return lines[:-1] + optional + lines[-1:]


def _keyboard(message: Any) -> list[list[tuple[str, str]]] | None:
    if message.keyboard is None:
        return None
    return [[(b.label, b.data) for b in row] for row in message.keyboard]


def _differences(expected: tuple[str, Any], actual: tuple[str, Any]) -> list[str]:
    """As diferenças entre (markup, teclado) esperados e recebidos (a comparação com a entrega ao aluno)."""
    out = []
    if expected[0] != actual[0]:
        out.append("texto")
    if expected[1] != actual[1]:
        out.append("teclado")
    return out


@pytest.fixture
def make_flow(client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, engine_fake: Any,
              report_worker: Any) -> Callable[..., Flow]:
    def make(**kwargs: Any) -> Flow:
        return Flow(client=client, sql=sql, clock=clock, rich=rich_bot_api, engine_fake=engine_fake,
                    report_worker=report_worker, **kwargs)
    return make


# ---------------------------------------------------------------------------------------------
# Amostra da validação
# ---------------------------------------------------------------------------------------------


def test_validation_records_the_sample_only_when_scheduled(make_flow: Callable[..., Flow]) -> None:
    """O disparo validado (`scheduled`) ganha
    `dispatch_previews` com a seed da validação (`int(seed, 36)` da cadeia que foi ao motor), o token de 22
    caracteres e `created_at` = `validated_at`, e
    `dispatch_preview_contents.ir` igual à IR emitida sem `data_base64`; o rejeitado na validação
    (`broken_on_variant`) não tem amostra."""
    flow = make_flow()
    d = flow.prepare("with_media", preset("with_media"))
    flow.wait_state(d, "scheduled")
    sample = flow.sample(d)
    [call] = flow.variant_calls("with_media")[:1]
    seed36 = call.request["seed"]
    assert sample is not None
    assert sample["seed"] == int(seed36, 36)
    assert TOKEN_SHAPE.fullmatch(sample["button_token"])
    validated_at = flow.sql.execute("SELECT validated_at FROM dispatches WHERE id = %s", (d.row,)).fetchone()[0]
    assert sample["created_at"] == validated_at
    issued = [v for v in flow.engine_fake.issued_variants("with_media") if v.seed == seed36]
    assert issued and "data_base64" in issued[0].ir["media"][0], "controle positivo: a IR emitida leva os bytes"
    assert sample["ir"] == _without_media_bytes(issued[0].ir)

    rejected = flow.prepare("broken_on_variant", preset("broken_on_variant"))
    flow.wait_state(rejected, "rejected")
    assert flow.sample(rejected) is None
    assert flow.sql.execute("SELECT count(*) FROM dispatch_previews WHERE dispatch_id = %s",
                            (rejected.row,)).fetchone()[0] == 0


def test_preview_task_reproduces_the_validated_variant_with_a_high_seed(
        make_flow: Callable[..., Flow], monkeypatch: pytest.MonkeyPatch) -> None:
    """Com a seed da validação em 2³² − 1, a amostra grava
    4294967295; a tarefa da prévia chama `create_variant` **uma** vez, com a mesma referência e a seed
    `1z141z3` (base 36 canônica), e a variante devolvida é a validada (a mesma IR da amostra); a prévia sai
    (`queued`)."""
    from cq_server import dispatches

    class _Seeds:
        """`secrets` do módulo `dispatches` com a seed da validação fixada em 2³² − 1 (o resto delega)."""

        def randbelow(self, n: int) -> int:
            return n - 1 if n == 2 ** 32 else real_secrets.randbelow(n)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_secrets, name)

    monkeypatch.setattr(dispatches, "secrets", _Seeds())
    flow = make_flow()
    flow.link()
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    sample = flow.sample(d)
    assert sample is not None and sample["seed"] == HIGH_SEED
    flow.wait_notice(d, "queued")
    calls = flow.variant_calls("single_choice")
    ref = {"repo": REPO, "commit": COMMIT, "qid": "single_choice"}
    assert [(c.request["question_ref"], c.request["seed"], c.outcome) for c in calls] == [
        (ref, HIGH_SEED36, "ok"), (ref, HIGH_SEED36, "ok")]
    assert calls[1].response["ir"] == sample["ir"]


# ---------------------------------------------------------------------------------------------
# Cabeçalho e mensagens da questão
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("qid", ["single_choice", "multiple_choice"])
def test_choice_preview_header_and_question_equal_the_student_render(make_flow: Callable[..., Flow], qid: str) -> None:
    """Com 3 alunos no alvo e 2 membros com endereço ativo
    (mais um sem endereço), cada membro recebe o cabeçalho (linhas exatas, sem botão) e a questão igual ao
    `_render` da entrega sobre o mesmo `_View` com o token da prévia (texto e teclado; a escolha não tem pedido
    de campo); todo botão é `cq1:` com o token da prévia, até 64 bytes, e nenhum é de confirmação. Controle
    positivo: uma diferença não declarada (um caractere do texto, um rótulo do teclado) é acusada."""
    flow = make_flow()
    team = flow.link((ACTIVE, NO_ENDPOINT, ACTIVE))
    for _ in range(3):
        flow.student()
    d = flow.prepare(qid, preset(qid))
    flow.wait_state(d, "scheduled")
    notice = flow.wait_notice(d, "queued")
    assert (notice["recipient_count"], notice["subject_key"]) == (2, f"dispatch:{d.row}")
    sample = flow.sample(d)
    assert sample is not None
    expected = flow.expected_question(d, sample)
    header = flow.expected_header(d)
    assert "Alunos-alvo: 3" in header
    for member in team.active:
        header_row, question_row = flow.wait_sent(member, 2)
        assert (header_row.role, header_row.text_format, question_row.role) == (None, "rich", "question")
        head = flow.received(member, header_row)
        assert _lines(head.plain_text) == header
        assert head.keyboard is None and head.force_reply is False
        question = flow.received(member, question_row)
        actual = (question.text, _keyboard(question))
        assert _differences(expected, actual) == []
        buttons = question.buttons()
        assert buttons and all(b.data.startswith(f"cq1:{sample['button_token']}:") for b in buttons)
        assert all(len(b.data.encode("utf-8")) <= 64 for b in buttons)
        labels = {b.label for b in buttons}
        assert not any("onfirm" in label for label in labels)
        # Controle positivo da comparação.
        assert _differences(expected, (question.text + "x", _keyboard(question))) == ["texto"]
        mutated = copy.deepcopy(_keyboard(question))
        assert mutated is not None
        mutated[0][0] = ("(z)", mutated[0][0][1])
        assert _differences(expected, (question.text, mutated)) == ["teclado"]
    assert flow.preview_messages(team.member("membro-2")) == []


def test_number_preview_field_prompt_has_the_hint_line_and_no_force_reply(make_flow: Callable[..., Flow]) -> None:
    """Na questão de número, depois da questão vem o
    pedido de campo com o texto do aluno (`deliverytexts.field_request(1, 1, rótulo, sufixo)`) e a linha
    `PREVIEW_FIELD_HINT` com a dica do campo, **sem** `ForceReply` (nem placeholder) e sem teclado; a questão
    é igual ao `_render` da entrega (as únicas diferenças declaradas estão no pedido)."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.student()
    d = flow.prepare("number", preset("number"))
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None
    spec = sample["ir"]["fields"][0]
    [member] = team.active
    header_row, question_row, prompt_row = flow.wait_sent(member, 3)
    assert [r.role for r in (header_row, question_row, prompt_row)] == [None, "question", "field_prompt"]
    assert prompt_row.field_index == 0
    question = flow.received(member, question_row)
    assert _differences(flow.expected_question(d, sample), (question.text, _keyboard(question))) == []
    prompt = flow.received(member, prompt_row)
    r5_text = deliverytexts.field_request(1, 1, html.unescape(spec["label_html"]), html.unescape(spec["suffix_html"]))
    assert _lines(prompt.plain_text) == _lines(r5_text) + [noticetexts.PREVIEW_FIELD_HINT.format(hint=spec["hint"])]
    assert (prompt.force_reply, prompt.placeholder, prompt.keyboard) == (False, None, None)


def test_multipart_preview_shows_the_first_field_and_the_parts_line(make_flow: Callable[..., Flow]) -> None:
    """A questão de 5 partes → o cabeçalho com
    `A questão tem 5 partes; a prévia mostra a primeira.` antes da linha do comando; a questão igual ao
    `_render` da entrega no 1º campo (com "Parte 1 de 5"); o pedido do 1º campo (número) com a dica."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.student()
    d = flow.prepare("multipart", preset("multipart"))
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None and len(sample["ir"]["fields"]) == 5
    [member] = team.active
    header_row, question_row, prompt_row = flow.wait_sent(member, 3)
    header = flow.expected_header(d, parts=5)
    assert noticetexts.PREVIEW_PARTS.format(parts=5) == "A questão tem 5 partes; a prévia mostra a primeira."
    assert _lines(flow.received(member, header_row).plain_text) == header
    question = flow.received(member, question_row)
    assert _differences(flow.expected_question(d, sample), (question.text, _keyboard(question))) == []
    assert deliverytexts.progress(1, 5) in question.plain_text
    spec = sample["ir"]["fields"][0]
    r5_text = deliverytexts.field_request(1, 5, html.unescape(spec["label_html"]), html.unescape(spec["suffix_html"]))
    assert _lines(flow.received(member, prompt_row).plain_text) == _lines(r5_text) + [
        noticetexts.PREVIEW_FIELD_HINT.format(hint=spec["hint"])]


def test_media_captions_photo_or_document_order_keys_and_priority(make_flow: Callable[..., Flow]) -> None:
    """Legenda, documento e pedido da prévia: com o worker em `photo_max_bytes = 1000`, a
    figura pequena sai como foto e a grande como documento, cada uma com a legenda da entrega
    (`_caption(_figure_number(mN, i), alt)`) e os bytes da variante; por destinatário, a ordem é cabeçalho,
    mídias (m1, m2), questão, pedido, também na ordem de envio; as chaves
    `notice:<id>:p<pessoa>:<papel>[:<mN|i>]`; tudo com prioridade 2; os blobs em `media_refs`."""
    from cq_server.deliveries import _caption, _figure_number

    flow = make_flow()
    team = flow.link()
    flow.student()
    d = flow.prepare("with_media", TWO_MEDIA)
    flow.wait_state(d, "scheduled")
    notice = flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None
    assert sample["media_refs"] == {"m1": hashlib.sha256(SMALL_PNG).hexdigest(),
                                    "m2": hashlib.sha256(LARGE_PNG).hexdigest()}
    keys = flow.keys(notice["id"])
    expected_media = [("m1", "sendPhoto", SMALL_PNG, "Plano inclinado", "pequena.png"),
                      ("m2", "sendDocument", LARGE_PNG, "Gráfico da velocidade", "grande.png")]
    for member in team.active:
        rows = flow.wait_sent(member, 5)
        assert [(r.role, r.media_id, r.field_index) for r in rows] == [
            (None, None, None), ("media", "m1", None), ("media", "m2", None), ("question", None, None),
            ("field_prompt", None, 0)]
        assert {r.priority for r in rows} == {2}
        person = flow.person_row(member)
        base = f"notice:{notice['id']}:p{person}"
        assert [keys[r.message_id] for r in rows[1:]] == [f"{base}:media:m1", f"{base}:media:m2", f"{base}:question",
                                                          f"{base}:field_prompt:0"]
        assert keys[rows[0].message_id].startswith(base)
        received = [flow.received(member, r) for r in rows]
        assert [m.call_seq for m in received] == sorted(m.call_seq for m in received)
        for index, (media_id, method, data, alt, filename) in enumerate(expected_media, start=1):
            msg = received[index]
            assert msg.method == method
            assert msg.upload is not None
            assert (msg.upload.sha256, msg.upload.filename, msg.upload.content_type) == (
                hashlib.sha256(data).hexdigest(), filename, "image/png")
            assert msg.caption == _caption(_figure_number(media_id, index), alt)
        assert _lines(received[0].plain_text) == flow.expected_header(d)


def test_missing_media_and_engine_warnings_by_kind_in_the_header(make_flow: Callable[..., Flow]) -> None:
    """A questão com a figura ausente (aviso `missing_media` do motor) e o alvo vazio
    (aviso `empty_target` do núcleo) → no cabeçalho, as linhas fixas pelo tipo (`PREVIEW_ENGINE_WARNINGS` e
    `CORE_WARNING_MESSAGES`), nunca a mensagem do motor; a mídia ausente não vira mensagem (só cabeçalho e
    questão, sem mensagem parcial), e a questão, igual ao `_render` da entrega, traz a linha de mídia
    indisponível."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    d = flow.prepare("missing_media", preset("missing_media"))
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None
    kinds = [w["kind"] for w in get_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)["preview"]["warnings"]]
    assert "missing_media" in kinds and "empty_target" in kinds
    [member] = team.active
    rows = flow.wait_sent(member, 3)
    assert [r.role for r in rows] == [None, "question", "field_prompt"]
    header = _lines(flow.received(member, rows[0]).plain_text)
    assert header == flow.expected_header(d)
    assert noticetexts.PREVIEW_ENGINE_WARNINGS["missing_media"] in header
    assert CORE_WARNING_MESSAGES["empty_target"] in header
    assert not any("Uma imagem do enunciado não foi encontrada." in line for line in header)
    question = flow.received(member, rows[1])
    assert _differences(flow.expected_question(d, sample), (question.text, _keyboard(question))) == []
    assert deliverytexts.MEDIA_UNAVAILABLE.format(n=1) in question.plain_text


def test_immediate_without_closing_uses_the_immediate_header_and_no_deadline(make_flow: Callable[..., Flow]) -> None:
    """O imediato sem fechamento informado → o cabeçalho
    `PREVIEW_IMMEDIATE` ("abre na confirmação e fecha 45 minutos depois"); a questão sem a linha "Prazo:"
    (uma diferença declarada), igual ao `_render` da entrega com o fechamento desconhecido."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.student()
    d = flow.prepare("single_choice", preset("single_choice"), opens_in=None, window=None)
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None
    [member] = team.active
    header_row, question_row = flow.wait_sent(member, 2)
    header = _lines(flow.received(member, header_row).plain_text)
    assert header == flow.expected_header(d)
    assert header[1] == f"Abre na confirmação e fecha {WINDOW_MINUTES} minutos depois."
    question = flow.received(member, question_row)
    assert _differences(flow.expected_question(d, sample), (question.text, _keyboard(question))) == []
    assert "Prazo:" not in (question.plain_text or "")


def test_long_title_is_truncated_only_in_the_variable_field(make_flow: Callable[..., Flow]) -> None:
    """O título com 3000 caracteres (com `<`, `&`, acentos e um emoji) → o cabeçalho cabe em
    4096 caracteres de markup; a 1ª linha leva só o começo do título (truncado, escapado no markup e lido de
    volta igual pela Bot API falsa); as demais linhas (horários, alunos-alvo, o comando inteiro) exatas."""
    title = ("Título <longo> & difícil — ação, reação, ênfase 😀 " * 60)[:3000]
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.student()
    d = flow.prepare("single_choice", dataclasses.replace(preset("single_choice"), title=title))
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    [member] = team.active
    header_row, _ = flow.wait_sent(member, 2)
    assert header_row.text is not None and len(header_row.text) <= TEXT_LIMIT
    lines = _lines(flow.received(member, header_row).plain_text)
    expected = flow.expected_header(d)
    prefix = "Prévia do disparo: "
    assert lines[0].startswith(prefix)
    shown = lines[0][len(prefix):]
    assert 0 < len(shown) < len(title)
    assert title.startswith(shown.rstrip("…").rstrip())
    assert lines[1:] == expected[1:]
    assert lines[-1] == f"Para confirmar o disparo: cq dispatches confirm {flow.code} {d.id}"


def test_preview_never_shows_the_answer_key(make_flow: Callable[..., Flow]) -> None:
    """A questão com `show_correct_answer` e explicação → nenhuma mensagem
    da prévia (texto no banco e texto lido pela Bot API falsa) leva a sentinela do gabarito do motor falso
    nem a explicação; controle positivo: a sentinela está na amostra gravada (`dispatch_preview_contents.ir`,
    a exceção nomeada)."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.student()
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    flow.wait_notice(d, "queued")
    sample = flow.sample(d)
    assert sample is not None
    assert ANSWER_SENTINEL in str(sample["ir"]), "controle positivo: a amostra guarda a IR com o gabarito"
    [member] = team.active
    rows = flow.wait_sent(member, 2)
    for row in rows:
        assert ANSWER_SENTINEL not in (row.text or "")
        msg = flow.received(member, row)
        for text in (msg.text, msg.plain_text, msg.caption):
            assert ANSWER_SENTINEL not in (text or "")
            assert "espalhamento de Rayleigh" not in (text or "")


# ---------------------------------------------------------------------------------------------
# Reexecução, desistências e concorrência
# ---------------------------------------------------------------------------------------------


def test_reexecution_does_not_duplicate_and_preview_stays_out_of_target(make_flow: Callable[..., Flow]) -> None:
    """Depois da prévia `queued`, a tarefa `preview.send` volta a `pending` e
    roda de novo → nenhuma chamada ao motor, nenhum aviso, mensagem, seed ou token novos; nenhuma entrega,
    submissão ou seleção para a equipe; a contagem do disparo continua a dos alunos; confirmado e aberto, as
    entregas são só dos 2 alunos (nenhuma a um membro da equipe)."""
    flow = make_flow()
    team = flow.link()
    flow.student()
    flow.student()
    d = flow.prepare("single_choice", preset("single_choice"), opens_in=timedelta(minutes=30))
    flow.wait_state(d, "scheduled")
    notice = flow.wait_notice(d, "queued")
    for member in team.active:
        flow.wait_sent(member, 2)
    sample = flow.sample(d)
    calls = len(flow.variant_calls("single_choice"))
    messages = flow.preview_messages()
    [task] = flow.preview_tasks()
    assert task["state"] == "done"

    flow.sql.execute("UPDATE tasks SET state = 'pending', available_at = %s, lease_owner = NULL, lease_until = NULL "
                     "WHERE id = %s", (flow.clock.now(), task["id"]))
    flow.wait(lambda: flow.preview_tasks()[0]["state"] == "done")
    time_module.sleep(QUIET)
    assert len(flow.variant_calls("single_choice")) == calls
    assert flow.notices(d) == [notice]
    assert flow.preview_messages() == messages
    assert flow.sample(d) == sample
    assert flow.sql.execute("SELECT count(*) FROM deliveries WHERE dispatch_id = %s", (d.row,)).fetchone()[0] == 0
    body = get_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    assert (body["state"], body["preview"]["target_count"]) == ("scheduled", 2)

    confirm_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    assert d.opens_at is not None
    flow.clock.set(d.opens_at + timedelta(seconds=1))
    flow.advance_until(lambda: flow.state(d) == "open", step=15.0)
    flow.wait(lambda: flow.sql.execute("SELECT count(*) FROM deliveries WHERE dispatch_id = %s",
                                       (d.row,)).fetchone()[0] == 2)
    team_people = [flow.person_row(m) for m in team.members]
    rows = flow.sql.execute("SELECT e.person_id, e.role FROM deliveries v JOIN enrollments e ON e.id = v.enrollment_id "
                            "WHERE v.dispatch_id = %s", (d.row,)).fetchall()
    assert sorted(role for _, role in rows) == ["student", "student"]
    assert not {person for person, _ in rows} & set(team_people)
    assert get_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)["target_count"] == 2
    assert flow.sql.execute("SELECT count(*) FROM submissions s JOIN deliveries v ON v.id = s.delivery_id "
                            "WHERE v.dispatch_id = %s", (d.row,)).fetchone()[0] == 0
    assert len(flow.notices(d)) == 1


def test_engine_down_fails_rounds_then_gives_up_without_partial_message(make_flow: Callable[..., Flow]) -> None:
    """O motor responde 500 a toda `create_variant` da prévia → as
    rodadas `preview.send` terminam `failed` e, depois de 5 (`PREVIEW_MAX_ROUNDS`), o aviso fica `gave_up`
    com o `notice.recorded` (`recipient_count` 0); nenhuma mensagem à equipe, nem parcial; o disparo continua
    `scheduled`."""
    flow = make_flow()
    flow.link((ACTIVE,))
    # A falha é programada antes do preparo: a 1ª `create_variant` da questão (a da validação) passa.
    flow.engine_fake.fail("create_variant", error="internal_error", times=None, when=_after_first("single_choice"))
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    assert flow.sample(d) is not None
    notice = flow.advance_until(lambda: (rows := flow.notices(d)) and rows[0]["state"] == "gave_up" and rows[0],
                                step=120.0, tries=120)
    tasks = flow.preview_tasks()
    assert [t["state"] for t in tasks] == ["failed"] * 5
    assert flow.preview_messages() == []
    assert (notice["recipient_count"], notice["finished_at"] is not None) == (0, True)
    assert flow.preview_events(d) == [flow.recorded(d, "gave_up", 0, notice["finished_at"])]
    preview_calls = flow.variant_calls("single_choice")[1:]
    assert len(preview_calls) >= 5 and all(c.outcome == "error" for c in preview_calls)
    # Uma `create_variant` por execução: tantas chamadas quanto tentativas somadas das
    # rodadas (o número de tentativas por rodada depende de a falha ser temporária ou definitiva, que nada no
    # produto fixa; por isso a contagem exata é pela soma, e não "5").
    assert len(preview_calls) == sum(t["attempts"] for t in tasks)
    assert flow.state(d) == "scheduled"


def test_preview_that_only_runs_after_opening_gives_up(make_flow: Callable[..., Flow]) -> None:
    """Mídias e desistência da prévia: a 1ª rodada da prévia falha (motor 500); o disparo é
    confirmado e abre (alvo vazio); com o motor de volta, a rodada seguinte vê o disparo fora de `scheduled`
    → `gave_up` com o evento, sem chamar o motor e sem mensagem."""
    flow = make_flow()
    flow.link((ACTIVE,))
    flow.engine_fake.fail("create_variant", error="internal_error", times=None, when=_after_first("single_choice"))
    d = flow.prepare("single_choice", preset("single_choice"), opens_in=timedelta(minutes=10))
    flow.wait_state(d, "scheduled")
    assert flow.sample(d) is not None
    flow.wait(lambda: len(flow.variant_calls("single_choice")) >= 2)
    confirm_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    assert d.opens_at is not None
    flow.clock.set(d.opens_at + timedelta(seconds=1))
    flow.advance_until(lambda: flow.state(d) == "open", step=15.0)
    flow.engine_fake.clear_failures()
    calls = len(flow.variant_calls("single_choice"))
    notice = flow.advance_until(lambda: (rows := flow.notices(d)) and rows[0]["state"] == "gave_up" and rows[0],
                                step=120.0, tries=60)
    assert len(flow.variant_calls("single_choice")) == calls
    assert flow.preview_messages() == []
    assert flow.preview_events(d) == [flow.recorded(d, "gave_up", 0, notice["finished_at"])]


def test_no_recipient_records_without_calling_the_engine(make_flow: Callable[..., Flow]) -> None:
    """Sem membro da equipe com endereço ativo (um membro sem endereço) → o aviso
    `no_recipient` com o evento (`recipient_count` 0), sem mensagem e sem nenhuma `create_variant` além da
    validação."""
    flow = make_flow()
    flow.link((NO_ENDPOINT,))
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    notice = flow.wait_notice(d, "no_recipient")
    assert (notice["recipient_count"], notice["subject_key"]) == (0, f"dispatch:{d.row}")
    time_module.sleep(QUIET)
    assert len(flow.variant_calls("single_choice")) == 1
    assert flow.preview_messages() == []
    assert flow.preview_events(d) == [flow.recorded(d, "no_recipient", 0, notice["finished_at"])]


def test_engine_not_found_enqueues_the_refetch_and_fails_the_round(make_flow: Callable[..., Flow]) -> None:
    """`EngineNotFound` na prévia: a `create_variant` da prévia recebe 404 `not_found`
    (`EngineNotFound`) → o `question.refetch` do disparo é enfileirado para a referência (payload
    `question_ref_row_id`) e a rodada da prévia termina `failed`, sem mensagem; com o motor de volta, uma
    rodada seguinte entrega a prévia (`queued`)."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.engine_fake.fail("create_variant", error="not_found", times=None, when=_after_first("single_choice"))
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    assert flow.sample(d) is not None
    ref_row = flow.sql.execute("SELECT question_ref_id FROM dispatches WHERE id = %s", (d.row,)).fetchone()[0]
    flow.advance_until(lambda: (tasks := flow.preview_tasks()) and tasks[0]["state"] == "failed", step=30.0)
    refetch = flow.sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'question.refetch' AND payload @> %s::jsonb",
                               (f'{{"question_ref_row_id": {ref_row}}}',)).fetchone()[0]
    assert refetch >= 1
    # O refetch só é enfileirado sem tarefa viva para a referência: nunca dois vivos.
    live_refetch = flow.sql.execute(
        "SELECT count(*) FROM tasks WHERE task_type = 'question.refetch' AND state IN ('pending', 'leased') "
        "AND payload @> %s::jsonb", (f'{{"question_ref_row_id": {ref_row}}}',)).fetchone()[0]
    assert live_refetch <= 1
    assert flow.preview_messages() == []
    assert flow.notices(d) == []
    flow.engine_fake.clear_failures()
    flow.advance_until(lambda: (rows := flow.notices(d)) and rows[0]["state"] == "queued", step=120.0)
    flow.wait_sent(team.active[0], 2)


def test_sweep_revives_a_due_preview_without_live_task_exactly_once(make_flow: Callable[..., Flow]) -> None:
    """Prévia devida reanimada pela varredura: a 1ª rodada
    `preview.send` falha de vez (motor 500 em toda tentativa) e termina `failed`; o disparo continua
    `scheduled`, sem aviso `dispatch_preview` e sem tarefa viva: a prévia fica **devida**.
    Com o motor de volta e o relógio adiante do intervalo de rodada, a varredura reanima a prévia
    **exatamente uma vez** (`preview.send:<id da amostra>:r2`, com o mesmo payload da r1, o id da
    amostra), e a rodada conclui numa tentativa, com uma `create_variant` a mais: um aviso `queued` com os 2
    destinatários, um `notice.recorded`, 2 mensagens por membro (cabeçalho e questão), cada uma recebida uma
    vez pela Bot API falsa; depois, com o relógio adiante de novo, nenhuma rodada, mensagem ou evento
    novos."""
    flow = make_flow()
    team = flow.link()
    flow.student()
    flow.engine_fake.fail("create_variant", error="internal_error", times=None, when=_after_first("single_choice"))
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    sample = flow.sample(d)
    assert sample is not None
    # Passos de 20 s: as esperas entre tentativas (2 s e 4 s no teste) passam, e o relógio para logo que a r1
    # termina, longe do intervalo de rodada (`backoff_max_seconds` = 60 s no teste), antes de o motor voltar.
    flow.advance_until(lambda: (tasks := flow.preview_tasks()) and tasks[0]["state"] == "failed", step=20.0)
    assert [t["state"] for t in flow.preview_tasks()] == ["failed"]
    # Devida: sem aviso, sem tarefa viva, disparo agendado e nada enviado nem registrado.
    assert flow.notices(d) == []
    assert flow.state(d) == "scheduled"
    assert flow.preview_messages() == []
    assert flow.preview_events(d) == []
    received_before = {m.pseudo_id: len(flow.rich.messages(flow.bot, m.chat_id)) for m in team.active}
    flow.engine_fake.clear_failures()
    calls_before = len(flow.variant_calls("single_choice"))

    notice = flow.advance_until(lambda: (rows := flow.notices(d)) and rows[0]["state"] == "queued" and rows[0],
                                step=20.0)
    for member in team.active:
        flow.wait_sent(member, 2)
    # Nenhuma reanimação a mais: o relógio passa de novo o intervalo depois da rodada concluída.
    flow.clock.advance(120.0)
    time_module.sleep(QUIET)

    rows = flow._rows("SELECT idempotency_key, state, payload, attempts FROM tasks WHERE task_type = %s ORDER BY id",
                      (PREVIEW_TASK,))
    assert [(r["idempotency_key"], r["state"]) for r in rows] == [
        (f"{PREVIEW_TASK}:{sample['id']}:r1", "failed"), (f"{PREVIEW_TASK}:{sample['id']}:r2", "done")]
    # O payload leva o id da linha do assunto (`dispatch_previews.id`), igual nas duas rodadas.
    assert rows[0]["payload"] == rows[1]["payload"]
    assert isinstance(rows[1]["payload"], dict) and list(rows[1]["payload"].values()) == [sample["id"]]
    assert rows[1]["attempts"] == 1
    calls = flow.variant_calls("single_choice")
    assert len(calls) == calls_before + 1 and calls[-1].outcome == "ok"
    assert flow.notices(d) == [notice]
    assert (notice["recipient_count"], notice["subject_key"]) == (2, f"dispatch:{d.row}")
    assert flow.preview_events(d) == [flow.recorded(d, "queued", 2, notice["finished_at"])]
    assert len(flow.preview_messages()) == 4
    assert len(set(flow.keys(notice["id"]).values())) == 4
    for member in team.active:
        assert len(flow.preview_messages(member)) == 2
        assert len(flow.rich.messages(flow.bot, member.chat_id)) == received_before[member.pseudo_id] + 2
    after = flow.sample(d)
    columns = ("id", "seed", "button_token", "created_at", "ir")
    assert {k: after[k] for k in columns} == {k: sample[k] for k in columns}
    assert after["rendered"] is not None
    assert flow.state(d) == "scheduled"


def test_slow_engine_does_not_block_a_concurrent_confirmation(make_flow: Callable[..., Flow]) -> None:
    """Motor fora de transação: o motor segura a `create_variant`
    da prévia por 6 s; enquanto ela está pendente, a confirmação pela API (outra conexão) termina em menos de
    3 s, com a chamada ao motor ainda aberta; depois, a prévia sai (`queued`) e o disparo continua
    `scheduled` e confirmado."""
    flow = make_flow()
    team = flow.link((ACTIVE,))
    flow.engine_fake.slow("create_variant", 6.0, when=_after_first("single_choice"))
    d = flow.prepare("single_choice", preset("single_choice"))
    flow.wait_state(d, "scheduled")
    assert flow.sample(d) is not None
    flow.wait(lambda: len(calls := flow.variant_calls("single_choice")) >= 2 and calls[1].outcome == "pending")
    started = time_module.monotonic()
    confirmed = confirm_dispatch(flow.client, ADMIN_TOKEN, flow.code, d.id)
    elapsed = time_module.monotonic() - started
    assert (confirmed["state"], confirmed["confirmed"]) == ("scheduled", True)
    assert elapsed < 3.0, f"a confirmação esperou o motor: {elapsed:.1f} s"
    assert flow.variant_calls("single_choice")[1].outcome == "pending"
    flow.wait_notice(d, "queued")
    flow.wait_sent(team.active[0], 2)
    assert flow.state(d) == "scheduled"
