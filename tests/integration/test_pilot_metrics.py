"""Gerador de métricas do piloto contra a API dos testes (implantação e piloto).


**Montagem: um piloto fictício pelo caminho real, só com dublês** (no molde de
`tests/integration/test_console_e2e_reports.py`):
- oferecimento e bot pela API de oferecimentos (bot da Bot API falsa só para o registro e o perfil, processado antes do worker
  de console); lista de 6 alunos pela importação da lista (A, B, C e D são os alunos canário `STUDENT_A`, `STUDENT_B`,
  `DISPATCH_A` e `DISPATCH_B`; E e F são neutros); convites em duas fases, mandados pelo worker de console dos relatórios
  (`report_worker(console=True, verification=mail_channel)`) ao SMTP falso; o código vem do link do e-mail;
- vínculos pelo console (`/start <código>` e `/aceito`): A em +1 h, B em +2 h, C em +5 h e D em +10 h depois do envio
  dos convites; F recusa o termo (`/recuso`) em +3 h; E não abre o link e recebe um reenvio (`invites.resend`, pelo
  token inicial) em +4 h;
- quatro disparos imediatos com o fechamento informado (o `opens_at` é o instante da confirmação), validados, abertos
  e fechados pelo worker e pelo relógio falso, com a questão de cada preset do motor falso (`program_presets`):
  - **D0** (diário, `text`, `pl-string-input`), em +10 min, antes de qualquer vínculo: alvo 0;
  - **D1** (diário, `number`, `pl-number-input`), em +11 h: A responde `6.5` em +30 s; B manda `/naosei` em +40 s;
    C manda `abc` (erro de formato) em +50 s e `6,5` (vírgula decimal) em +60 s; D não responde no prazo e manda
    `6.5` depois do fechamento (atrasada);
  - antes do D2, o endereço de D fica `blocked` por SQL (o console não simula o 403 do Telegram; os vínculos e os
    endereços por SQL são o precedente de `tests/integration/test_reports_api.py`);
  - **D2** (intensivo, `number`, `pl-number-input`), em +12 h: a correção de A recebe o `broken` injetado (falha de
    questão na correção); B responde `13` em +20 s (incorreta, mas corrigida); C não responde;
  - **D3** (intensivo, `integer`, `pl-integer-input`), em +13 h, **preparado e confirmado pelo token do agente** com
    `X-CQ-Client: mcp`: A responde `42` em +10 s; B manda `1.5` (erro de formato) em +15 s e não responde de novo;
    o motor falso para (`engine_fake.stop()`) e C responde `77` em +20 s: a correção fica pendente (submissão
    `grading`; **[F]** o `EngineUnavailable` mantém a submissão `grading` e repete sem limite, docstring de
    `server/src/cq_server/grading.py`; o fechamento não espera a correção, `test_dispatch_closing.py`,
    `test_grading_pending_at_close_outcome_comes_after_closed_with_reveal`), e o motor não volta até o gerador
    rodar: ao fim, a entrega de C é `delivered` com `grading_pending` 1.
- um plano TOML de teste com as 4 vagas, os horários no fuso do oferecimento (`-03:00`) e as
  referências exatas dos disparos.

Cada número esperado foi calculado à mão a partir do roteiro, e a mesma função confere, antes de rodar o gerador, que
as leituras da API (`dispatches.list`, `reports.get`, `events.list`) dizem o mesmo (pré-condições do roteiro): o
esperado não é copiado da API.

**Definições [I] (minhas; os campos têm nome, mas nada no produto fixa a fonte de cada um):**
- um disparo casa a vaga do plano pela referência (`repo`, `commit`, `qid`) e pelo `opens_at`; o `kind` vem da vaga;
  a lista sai na ordem do `opens_at`;
- `target`, `delivered` e `blocked` vêm do alcance do relatório (`reach.target`, `reach.delivered`,
  `reach.not_delivered_by_reason.endpoint_blocked`); `answered_on_time`, `answered_late`, `dont_know`, `no_answer`,
  `question_failed` e `grading_pending_at_end` vêm das contagens do `dispatches.list` ao fim (**[F]** são estados
  correntes: `answered` só no prazo, `answered_late` à parte, `delivered` é "entregue ainda sem resposta", por isso
  não serve para a taxa de entrega; `tests/integration/test_console_e2e_reports.py`, passos 7 a 9);
  `grading_pending_at_end` é o `deliveries.grading_pending` do `dispatches.list` (no roteiro, 1 no D3 e 0 nos
  outros; a entrega de C no D3 conta em `delivered`, não em `no_answer`);
- `delivery_rate = delivered / target`, `block_rate = blocked / target`, `response_rate_on_time = answered_on_time /
  delivered`, `response_rate_with_late = (answered_on_time + answered_late) / delivered`; denominador 0 → `null`;
  no total, as mesmas razões sobre as somas;
- `response_time_ms`: a mediana e os quartis do relatório (`response_time.median_ms`, `q1_ms`, `q3_ms`), `null` sem
  observação; no total, só a faixa (mín., máx.) das medianas por disparo, sem combinar medianas, na forma
  `response_time_median_range: [mín, máx]` (o `tuple` do esqueleto);
- `format_errors_by_field_type`: por disparo, a soma dos `friction.fields[].format_errors` sob o `field_type` da vaga
  do plano (os 5 elementos do PL); no total, a soma por tipo. A comparação ignora as entradas com 0 (uma
  implementação pode omiti-las). `decimal_comma`: a soma de `friction.fields[].decimal_comma`. No roteiro, todo erro
  de formato e toda vírgula são no prazo, então o par (no prazo, com atrasadas) tem os dois lados iguais;
- `graded_without_internal_error`: as respostas que chegaram a nota (os `submission.graded` do disparo, no prazo e
  atrasadas); no roteiro, cada aluno tem no máximo uma resposta válida, então é também `answered_on_time +
  answered_late`;
- `invite_to_link_hours`: por pessoa vinculada, as horas do `student.invite_sent` ao `student.joined` (no roteiro,
  cada vinculado teve um envio só), com a mediana e os quartis de `statistics.quantiles(..., n=4,
  method="inclusive")`, o método do relatório, e `n`; `resends`: os `student.invite_sent` que não são do lote
  (`origin` ≠ `batch`); no roteiro, o único reenvio deu certo, então contar os enfileirados dá o mesmo número;
- `agent_tasks`: os eventos cujo `actor_token_id` é o `--agent-token-id`; `ok` = os que não são recusa,
  `failed` = as recusas registradas (`identity.reveal_denied`); no roteiro, o agente prepara e confirma o D3 →
  `{ok: 2, failed: 0}`; sem `--agent-token-id` → `null`;
- **`terms_refused` → `null` e uma métrica `manual` `{name: "terms_refused", reason: "not_exposed"}`.** **[F]** A
  recusa do termo não gera evento (`tests/integration/test_student_refusal.py`, docstring) e o
  `students.summary` não a conta (`terms_presented` junta quem recusou com quem ainda não decidiu; esquema
  `students.summary.response.schema.json`); a métrica que a leitura não expõe sai "manual"
  com motivo, sem operação nova. Isso diverge do tipo `int` do `TotalMetrics` do esqueleto (divergência
  declarada de propósito);
- `manual`: as do diário (`MANUAL_METRICS`, reais no esqueleto) com `reason: "diary"`, e as não expostas com
  `not_exposed`; outras entradas `manual` são aceitas, com motivo de um dos dois e sem nome repetido;
- a forma do JSON: as chaves dos dataclasses do esqueleto (`dataclasses.asdict`), horários em ISO 8601 com fuso
  (comparados como instantes), `offering` = o código.

**Só leituras:** um gancho de pedido do cliente HTTP da API dos testes (`client.event_hooks["request"]`) registra
cada pedido do gerador: só `GET` das rotas de `dispatches.list`, `reports.get`, `students.summary` e `events.list`
(as três primeiras, ao menos uma vez cada uma que o cálculo usa: lista, relatório e eventos); nenhum método de
escrita do `ApiClient` chamado (trocados por um gravador). O perfil do gerador (arquivo `CQ_CONFIG` no `tmp_path`)
usa um token só de leitura (`offerings:read`, `events:read`).

**Saídas:** o JSON (`--json`) no stdout e, com `--out`, só no arquivo (o mesmo conteúdo, fora o `generated_at`); o
Markdown (padrão) no stdout e em `--out`. **[I]** Do Markdown, só se confere que começa por um título, cita o código
do oferecimento, cada `qid` e a palavra "manual"; o resto da forma é livre.

**Sem identidade:** a saída (JSON e Markdown, no stdout e no `--out`) e o stderr não têm nome, RA,
e-mail (`find_canary` com `STUDENT_PREFIXES` e `DISPATCH_PREFIXES`, com controle positivo), pseudônimo, apelido,
`chat_id` nem texto livre de aluno (as respostas `abc` e `1.5` só aparecem como contagem).

No esqueleto, o roteiro (o que já existe) roda inteiro e as pré-condições passam; a função falha no
`main` do gerador, que lança `NotImplementedError`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
import statistics
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server.adapters.console import ConsoleInbox
from cq_server.bots import profile_task_handler
from cq_server.tasks import TaskQueue
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_token,
    get_dispatch,
    get_report,
    import_students,
    list_dispatches,
    list_events,
    prepare_invites,
    register_bot,
    resend_invites,
    send_invites,
)
from tests.helpers.canary import (
    DISPATCH_A,
    DISPATCH_B,
    DISPATCH_PREFIXES,
    STUDENT_A,
    STUDENT_B,
    STUDENT_PREFIXES,
    find_canary,
)
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent, read_lines
from tests.helpers.ops import load_ops

SCRIPT = "ops/pilot/pilot_metrics.py"

COURSE = "Física Experimental"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
CODE = "piloto-metricas"
BOT_USERNAME = "cq_piloto_metricas_bot"
WORKER_ID = "worker-piloto-metricas"
PREP_WORKER_ID = "preparacao-piloto-metricas"
REPO = "https://repos.cq.test/disciplina/questoes-piloto.git"
COMMIT = "ca01df00085b0172af68253aeba93e76cd7f6ecb"
PROFILE_NAME = "piloto-teste"
WAIT = 30.0
PROFILE = "bot.sync_profile"
# O `broken` da correção (o contrato `grade_response` do motor; o mesmo de `test_console_e2e_reports.py`).
GRADE_BROKEN = {"outcome": "broken",
                "reason": {"code": "question_code_error", "message": "O código da questão falhou ao corrigir.",
                           "phase": "grade"},
                "version_mismatch": {"engine": False, "pl": False}, "warnings": []}

# Todos os tipos de evento do `events.list` (o `oneOf` do esquema congelado), para as pré-condições.
EVENT_TYPES = tuple(
    item["properties"]["type"]["const"]
    for item in json.loads((Path(__file__).resolve().parents[2] / "contracts" / "api" / "v1" / "schemas"
                            / "events.list.response.schema.json").read_text(encoding="utf-8"))
    ["properties"]["items"]["items"]["oneOf"])

# Os métodos de escrita do `ApiClient` (contracts/api/v1/catalog.json: tudo o que não é GET).
WRITE_METHODS = ("register_person", "reveal", "create_token", "revoke_token", "create_offering",
                 "update_offering_settings", "close_offering", "prepare_archive", "archive_offering", "prepare_purge",
                 "purge_offering", "register_bot", "activate_bot", "create_team_link", "import_students",
                 "prepare_send_invites", "send_invites", "resend_invites", "prepare_dispatch", "confirm_dispatch",
                 "cancel_dispatch", "resend_report_summary", "update_offering_digest")
# As rotas das 4 leituras permitidas.
READ_ROUTES = {
    "dispatches.list": re.compile(r"^/v1/offerings/[^/]+/dispatches$"),
    "reports.get": re.compile(r"^/v1/offerings/[^/]+/dispatches/[^/]+/report$"),
    "students.summary": re.compile(r"^/v1/offerings/[^/]+/students/summary$"),
    "events.list": re.compile(r"^/v1/events$"),
}

DISPATCH_KEYS = {"dispatch_id", "qid", "kind", "opens_at", "closes_at", "target", "delivered", "blocked",
                 "answered_on_time", "answered_late", "dont_know", "no_answer", "delivery_rate", "block_rate",
                 "response_rate_on_time", "response_rate_with_late", "response_time_ms",
                 "format_errors_by_field_type", "decimal_comma", "graded_without_internal_error", "question_failed",
                 "grading_pending_at_end"}
TOTAL_KEYS = {"target", "delivered", "blocked", "answered_on_time", "answered_late", "dont_know", "no_answer",
              "delivery_rate", "block_rate", "response_rate_on_time", "response_rate_with_late",
              "response_time_median_range", "format_errors_by_field_type", "decimal_comma",
              "graded_without_internal_error", "question_failed", "grading_pending_at_end", "invite_to_link_hours",
              "resends", "terms_refused", "agent_tasks"}

LINK_HOURS = {"A": 1, "B": 2, "C": 5, "D": 10}
REFUSE_HOURS = 3
RESEND_HOURS = 4


# --- montagem ------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Pupil:
    key: str
    name: str
    ra: str
    email: str
    chat: int
    pseudo: str = ""
    alias: str = ""
    person_row: int = 0
    console: ConsoleStudent | None = None

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}

    @property
    def io(self) -> ConsoleStudent:
        assert self.console is not None
        return self.console


def _pupils() -> list[_Pupil]:
    """A, B, C e D: os alunos canário do vínculo do aluno e do disparo (os alvos de `find_canary`); E e F: neutros."""
    canary = {"A": STUDENT_A, "B": STUDENT_B, "C": DISPATCH_A, "D": DISPATCH_B}
    pupils = [_Pupil(key=key, name=s.name, ra=s.ra, email=s.email, chat=5_771_000_000 + n)
              for n, (key, s) in enumerate(canary.items(), start=1)]
    pupils += [_Pupil(key=key, name=f"Pessoa Piloto Metricas {n:02d}", ra=f"59700009{n:02d}",
                      email=f"piloto.metricas{n:02d}@turma.example.org", chat=5_771_000_000 + n)
               for n, key in ((5, "E"), (6, "F"))]
    return pupils


@dataclasses.dataclass
class _Dispatch:
    label: str
    qid: str
    kind: str
    field_type: str
    dispatch_id: str = ""
    row: int = 0
    opens_at: datetime | None = None
    closes_at: datetime | None = None


@dataclasses.dataclass
class _W:
    client: Any
    sql: psycopg.Connection
    clock: Any
    engine_fake: Any
    stream: Any
    inbox: ConsoleInbox
    offering: dict
    bot_row: int
    pupils: list[_Pupil]
    dispatches: list[_Dispatch] = dataclasses.field(default_factory=list)

    def pupil(self, key: str) -> _Pupil:
        return next(p for p in self.pupils if p.key == key)


def _one(sql: psycopg.Connection, query: str, params: tuple = ()) -> dict | None:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _all(sql: psycopg.Connection, query: str, params: tuple = ()) -> list[dict]:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _busy(w: _W) -> list[str]:
    now = w.clock.now()
    busy = [f"tarefa {t}" for (t,) in w.sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"mensagem {m}" for (m,) in w.sql.execute(
        "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"update {u}" for (u,) in w.sql.execute(
        "SELECT id FROM telegram_updates WHERE state = 'pending'").fetchall()]
    return busy


def _settle(w: _W) -> None:
    """Espera o sistema parar (nenhuma tarefa, mensagem ou update disponível), duas vezes, com mais de uma rodada
    da varredura (1 s real) entre elas; só então o relógio pode saltar (o molde de `test_console_e2e_reports.py`)."""
    for _ in range(10):
        _wait(lambda: not _busy(w), "o sistema parar")
        time.sleep(1.2)
        if not _busy(w):
            return
    raise AssertionError(f"o sistema não parou: {_busy(w)}")


def _process_profile_tasks(db_url: str, clock: Any, settings: Any, bots: Any) -> list[tuple[str, str]]:
    """A `bot.sync_profile` do registro e da ativação, pela fila de tarefas com o tratador do perfil, antes do worker de
    console (que não tem tratador de perfil)."""
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    handler = profile_task_handler(db_url, clock=clock, runtime=bots, settings=settings)
    processed: list[tuple[str, str]] = []
    try:
        while (task_id := queue.work_once(PREP_WORKER_ID, {PROFILE: handler})) is not None:
            record = queue.get(task_id)
            processed.append((record.task_type, record.state))
    finally:
        close = getattr(handler, "close", None)
        if close is not None:
            close()
        queue.close()
    return processed


def _say(w: _W, chat: int, act: Callable[[], None], *, outcome: str | None = None) -> dict:
    """Uma entrada simulada: espera a linha gravada e processada (com o desfecho dado, se dado)."""
    mark = w.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates").fetchone()[0]
    act()

    def done() -> list[dict] | None:
        rows = _all(w.sql, "SELECT * FROM telegram_updates WHERE id > %s AND bot_id = %s AND chat_id = %s "
                    "ORDER BY id", (mark, w.bot_row, chat))
        return rows if rows and all(r["state"] == "processed" for r in rows) else None

    rows = _wait(done, f"a entrada no chat {chat} processada")
    assert len(rows) == 1, f"a entrada gravou {len(rows)} linhas"
    if outcome is not None:
        assert rows[0]["outcome"] == outcome, (rows[0]["outcome"], outcome)
    return rows[0]


def _delivery(w: _W, d: _Dispatch, pupil: _Pupil) -> dict:
    row = _one(w.sql, "SELECT d.id, d.state, d.failure, d.principal_message_id, d.sent_at FROM deliveries d "
               "JOIN enrollments e ON e.id = d.enrollment_id WHERE d.dispatch_id = %s AND e.person_id = %s",
               (d.row, pupil.person_row))
    assert row is not None, f"sem entrega de {pupil.key} em {d.label}"
    return row


def _line_of(w: _W, message_id: int) -> ConsoleLine:
    found = [line for line in read_lines(w.stream.getvalue()) if line.message_id == message_id]
    assert len(found) == 1, f"linhas do console da mensagem {message_id}: {len(found)}"
    return found[0]


def _prompts(w: _W, d: _Dispatch, pupil: _Pupil) -> list[int]:
    delivery = _delivery(w, d, pupil)
    return [int(r[0]) for r in w.sql.execute(
        "SELECT id FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' "
        "AND field_index = 0 AND state = 'sent' ORDER BY id", (delivery["id"],)).fetchall()]


def _submissions(w: _W, d: _Dispatch, pupil: _Pupil) -> list[dict]:
    return _all(w.sql, "SELECT outcome, valid, late, response_ms FROM submissions WHERE delivery_id = %s ORDER BY id",
                (_delivery(w, d, pupil)["id"],))


def _answer(w: _W, d: _Dispatch, key: str, text: str, *, valid: bool = True) -> dict:
    """Responde ao último pedido do campo e espera a correção (`graded`); com `valid=False`, também o novo pedido."""
    pupil = w.pupil(key)
    prompts = _wait(lambda: _prompts(w, d, pupil), f"o pedido do campo de {key} em {d.label}")
    before = len(_submissions(w, d, pupil))
    _say(w, pupil.chat, lambda: pupil.io.reply(_line_of(w, prompts[-1]), text))

    def graded() -> dict | None:
        subs = _submissions(w, d, pupil)
        return subs[-1] if len(subs) == before + 1 and subs[-1]["outcome"] == "graded" else None

    sub = _wait(graded, f"a correção da resposta de {key} em {d.label}")
    assert sub["valid"] is valid, f"{key}: validade {sub['valid']}"
    if not valid:
        _wait(lambda: len(_prompts(w, d, pupil)) == len(prompts) + 1, f"o novo pedido para {key}")
    return sub


def _open(w: _W, d: _Dispatch, *, window: timedelta, delivered: list[str], blocked: list[str] = (),
          token: str = ADMIN_TOKEN, client_kind: str | None = None) -> None:
    """Prepara o imediato com o fechamento informado, espera a validação, confirma no instante do relógio (o
    `opens_at`) e espera as entregas; o relógio não anda até elas."""
    t_open = w.clock.now()
    closes_at = t_open + window
    headers = auth(token, client_kind)
    resp = w.client.post(f"/v1/offerings/{CODE}/dispatches",
                         json={"repo": REPO, "commit": COMMIT, "qid": d.qid, "closes_at": iso(closes_at)},
                         headers=headers)
    assert resp.status_code == 200, resp.text[:300]
    d.dispatch_id = resp.json()["dispatch_id"]
    d.row = int(w.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s::uuid",
                              (d.dispatch_id,)).fetchone()[0])
    _wait(lambda: get_dispatch(w.client, ADMIN_TOKEN, CODE, d.dispatch_id)["state"] != "validating",
          f"a validação de {d.label}")
    _settle(w)
    resp = w.client.post(f"/v1/offerings/{CODE}/dispatches/{d.dispatch_id}/confirm", headers=headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["opens_at"] == iso(t_open), resp.json()
    d.opens_at, d.closes_at = t_open, closes_at
    expected = sorted(["delivered"] * len(delivered) + ["not_delivered"] * len(blocked))
    _wait(lambda: sorted(r[0] for r in w.sql.execute("SELECT state FROM deliveries WHERE dispatch_id = %s",
                                                     (d.row,)).fetchall()) == expected,
          f"as entregas de {d.label}")
    _settle(w)
    assert get_dispatch(w.client, ADMIN_TOKEN, CODE, d.dispatch_id)["state"] == "open"
    for key in delivered:
        assert _delivery(w, d, w.pupil(key))["sent_at"] == t_open, f"{key}: a questão saiu em outro instante"
    w.dispatches.append(d)


def _close(w: _W, d: _Dispatch, timeout: float = 3 * WAIT) -> None:
    """Leva o relógio além de `closes_at` e espera o disparo `closed` (avança mais 6 s se for preciso)."""
    assert d.closes_at is not None
    w.clock.set(d.closes_at + timedelta(seconds=1))
    deadline = time.monotonic() + timeout
    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if get_dispatch(w.client, ADMIN_TOKEN, CODE, d.dispatch_id)["state"] == "closed":
                _settle(w)
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            raise AssertionError(f"{d.label} não fechou em {timeout} s")
        w.clock.advance(6)


def _at(t0: datetime, *, hours: float = 0, minutes: float = 0, seconds: float = 0) -> datetime:
    return t0 + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _scenario(w: _W, codes: dict[str, str], t_send: datetime, agent_secret: str) -> None:
    """O roteiro do docstring do módulo (worker no ar do começo ao fim)."""
    # D0: antes de qualquer vínculo (alvo 0).
    w.clock.set(_at(t_send, minutes=10))
    d0 = _Dispatch("D0", "text", "daily", "pl-string-input")
    _open(w, d0, window=timedelta(minutes=10), delivered=[])
    _close(w, d0)

    # Vínculos, a recusa e o reenvio, nos horários do roteiro.
    steps = sorted([(h, "link", k) for k, h in LINK_HOURS.items()]
                   + [(REFUSE_HOURS, "refuse", "F"), (RESEND_HOURS, "resend", "E")])
    for hours, action, key in steps:
        pupil = w.pupil(key)
        w.clock.set(_at(t_send, hours=hours))
        if action == "resend":
            sent_before = w.sql.execute("SELECT count(*) FROM invite_sends WHERE state = 'sent'").fetchone()[0]
            assert resend_invites(w.client, ADMIN_TOKEN, CODE, [pupil.pseudo])["queued"] == 1
            _wait(lambda: w.sql.execute("SELECT count(*) FROM invite_sends WHERE state = 'sent'").fetchone()[0]
                  == sent_before + 1, "o reenvio de E")
        else:
            _say(w, pupil.chat, lambda p=pupil: p.io.say(f"/start {codes[p.key]}"), outcome="student_terms_presented")
            final = "/aceito" if action == "link" else "/recuso"
            outcome = "student_joined" if action == "link" else "student_terms_refused"
            _say(w, pupil.chat, lambda p=pupil, f=final: p.io.say(f), outcome=outcome)
        _settle(w)
    linked = _all(w.sql, "SELECT p.id FROM enrollments e JOIN people p ON p.id = e.person_id "
                  "WHERE e.role = 'student' AND e.state = 'active' ORDER BY p.id")
    assert sorted(r["id"] for r in linked) == sorted(w.pupil(k).person_row for k in LINK_HOURS)

    # D1 (diário).
    w.clock.set(_at(t_send, hours=11))
    d1 = _Dispatch("D1", "number", "daily", "pl-number-input")
    _open(w, d1, window=timedelta(minutes=30), delivered=["A", "B", "C", "D"])
    assert d1.opens_at is not None
    w.clock.set(_at(d1.opens_at, seconds=30))
    _answer(w, d1, "A", "6.5")
    w.clock.set(_at(d1.opens_at, seconds=40))
    b = w.pupil("B")
    _say(w, b.chat, lambda: b.io.dont_know(_line_of(w, int(_delivery(w, d1, b)["principal_message_id"]))),
         outcome="answer_dont_know")
    w.clock.set(_at(d1.opens_at, seconds=50))
    _answer(w, d1, "C", "abc", valid=False)
    w.clock.set(_at(d1.opens_at, seconds=60))
    _answer(w, d1, "C", "6,5")
    _settle(w)
    _close(w, d1)
    w.clock.advance(60)
    late = _answer(w, d1, "D", "6.5")
    assert late["late"] is True
    _settle(w)

    # O endereço de D bloqueado (o 403 do Telegram não existe no console).
    now = w.clock.now()
    w.sql.execute("UPDATE delivery_endpoints SET state = 'blocked', changed_at = %s WHERE person_id = %s "
                  "AND bot_id = %s AND state = 'active'", (now, w.pupil("D").person_row, w.bot_row))

    # D2 (intensivo): a falha de questão na correção de A.
    w.clock.set(_at(t_send, hours=12))
    d2 = _Dispatch("D2", "number", "intensive", "pl-number-input")
    _open(w, d2, window=timedelta(minutes=10), delivered=["A", "B", "C"], blocked=["D"])
    assert d2.opens_at is not None
    w.clock.set(_at(d2.opens_at, seconds=10))
    w.engine_fake.inject("grade", body=GRADE_BROKEN, times=1)
    a = w.pupil("A")
    prompts = _wait(lambda: _prompts(w, d2, a), "o pedido do campo de A em D2")
    _say(w, a.chat, lambda: a.io.reply(_line_of(w, prompts[-1]), "6.5"))
    _wait(lambda: _delivery(w, d2, a)["state"] == "question_failed", "a falha de questão de A em D2")
    w.clock.set(_at(d2.opens_at, seconds=20))
    _answer(w, d2, "B", "13")
    _settle(w)
    _close(w, d2)

    # D3 (intensivo), preparado e confirmado pelo token do agente, pelo MCP.
    w.clock.set(_at(t_send, hours=13))
    d3 = _Dispatch("D3", "integer", "intensive", "pl-integer-input")
    _open(w, d3, window=timedelta(minutes=10), delivered=["A", "B", "C"], blocked=["D"], token=agent_secret,
          client_kind="mcp")
    assert d3.opens_at is not None
    w.clock.set(_at(d3.opens_at, seconds=10))
    _answer(w, d3, "A", "42")
    w.clock.set(_at(d3.opens_at, seconds=15))
    _answer(w, d3, "B", "1.5", valid=False)
    _settle(w)
    # O motor para: a resposta de C fica aguardando correção ao fim (`grading_pending_at_end`).
    w.engine_fake.stop()
    w.clock.set(_at(d3.opens_at, seconds=20))
    c = w.pupil("C")
    prompts = _wait(lambda: _prompts(w, d3, c), "o pedido do campo de C em D3")
    _say(w, c.chat, lambda: c.io.reply(_line_of(w, prompts[-1]), "77"))
    _wait(lambda: [s["outcome"] for s in _submissions(w, d3, c)] == ["grading"], "a correção pendente de C em D3")
    _settle(w)
    _close(w, d3)
    assert [(s["outcome"], s["late"]) for s in _submissions(w, d3, c)] == [("grading", False)], (
        "a correção de C em D3 não ficou pendente")


# --- o esperado, à mão -----------------------------------------------------------------------------------------------


def _quartiles(values: list[float]) -> dict[str, float]:
    q1, median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"median": median, "q1": q1, "q3": q3}


def _rate(num: int, den: int) -> Any:
    return None if den == 0 else pytest.approx(num / den, rel=1e-12, abs=0.0)


def _expected_dispatch(d: _Dispatch, *, target: int, delivered: int, blocked: int, on_time: int, late: int,
                       dont_know: int, no_answer: int, times_ms: list[int], format_errors: int, decimal_comma: int,
                       graded: int, question_failed: int, grading_pending: int = 0) -> dict:
    times = None if not times_ms else (
        {"median": times_ms[0], "q1": times_ms[0], "q3": times_ms[0]} if len(times_ms) == 1 else _quartiles(times_ms))
    return {
        "dispatch_id": d.dispatch_id, "qid": d.qid, "kind": d.kind, "opens_at": d.opens_at, "closes_at": d.closes_at,
        "target": target, "delivered": delivered, "blocked": blocked, "answered_on_time": on_time,
        "answered_late": late, "dont_know": dont_know, "no_answer": no_answer,
        "delivery_rate": _rate(delivered, target), "block_rate": _rate(blocked, target),
        "response_rate_on_time": _rate(on_time, delivered), "response_rate_with_late": _rate(on_time + late, delivered),
        "response_time_ms": times,
        "format_errors_by_field_type": {d.field_type: format_errors} if format_errors else {},
        "decimal_comma": decimal_comma, "graded_without_internal_error": graded, "question_failed": question_failed,
        "grading_pending_at_end": grading_pending,
    }


def _expected(w: _W, *, agent: bool) -> tuple[list[dict], dict]:
    d0, d1, d2, d3 = w.dispatches
    dispatches = [
        _expected_dispatch(d0, target=0, delivered=0, blocked=0, on_time=0, late=0, dont_know=0, no_answer=0,
                           times_ms=[], format_errors=0, decimal_comma=0, graded=0, question_failed=0),
        _expected_dispatch(d1, target=4, delivered=4, blocked=0, on_time=2, late=1, dont_know=1, no_answer=0,
                           times_ms=[30_000, 60_000], format_errors=1, decimal_comma=1, graded=3, question_failed=0),
        _expected_dispatch(d2, target=4, delivered=2, blocked=1, on_time=1, late=0, dont_know=0, no_answer=1,
                           times_ms=[20_000], format_errors=0, decimal_comma=0, graded=1, question_failed=1),
        _expected_dispatch(d3, target=4, delivered=3, blocked=1, on_time=1, late=0, dont_know=0, no_answer=1,
                           times_ms=[10_000], format_errors=1, decimal_comma=0, graded=1, question_failed=0,
                           grading_pending=1),
    ]
    total = {
        "target": 12, "delivered": 9, "blocked": 2, "answered_on_time": 4, "answered_late": 1, "dont_know": 1,
        "no_answer": 2, "delivery_rate": _rate(9, 12), "block_rate": _rate(2, 12),
        "response_rate_on_time": _rate(4, 9), "response_rate_with_late": _rate(5, 9),
        "response_time_median_range": [10_000, 45_000],
        "format_errors_by_field_type": {"pl-number-input": 1, "pl-integer-input": 1},
        "decimal_comma": 1, "graded_without_internal_error": 5, "question_failed": 1, "grading_pending_at_end": 1,
        "invite_to_link_hours": {**_quartiles([float(h) for h in sorted(LINK_HOURS.values())]), "n": 4},
        "resends": 1, "terms_refused": None,
        "agent_tasks": {"ok": 2, "failed": 0} if agent else None,
    }
    return dispatches, total


def _check_preconditions(w: _W, t_send: datetime, agent_token_id: str) -> None:
    """As leituras da API dizem o mesmo que o roteiro (ver o docstring do módulo): sem isto, o esperado à mão não
    vale."""
    d0, d1, d2, d3 = w.dispatches
    items = {i["dispatch_id"]: i for i in list_dispatches(w.client, ADMIN_TOKEN, CODE)["items"]}
    assert set(items) == {d.dispatch_id for d in w.dispatches}
    zero = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0, "no_answer": 0,
            "answered_late": 0, "question_failed": 0, "grading_pending": 0}
    expected_counts = {
        d0.dispatch_id: dict(zero),
        d1.dispatch_id: {**zero, "answered": 2, "dont_know": 1, "answered_late": 1},
        d2.dispatch_id: {**zero, "not_delivered": 1, "answered": 1, "no_answer": 1, "question_failed": 1},
        d3.dispatch_id: {**zero, "not_delivered": 1, "answered": 1, "no_answer": 1, "delivered": 1,
                         "grading_pending": 1},
    }
    for d in w.dispatches:
        item = items[d.dispatch_id]
        assert (item["state"], item["question"]["qid"]) == ("closed", d.qid), (d.label, item["state"])
        assert item["deliveries"] == expected_counts[d.dispatch_id], (d.label, item["deliveries"])
        assert (item["opens_at"], item["closes_at"]) == (iso(d.opens_at), iso(d.closes_at)), d.label
    assert items[d3.dispatch_id]["origin"] == "mcp"

    reach = {d.label: get_report(w.client, CODE, d.dispatch_id) for d in w.dispatches}
    for label, (target, delivered, blocked) in {"D0": (0, 0, 0), "D1": (4, 4, 0), "D2": (4, 2, 1),
                                                "D3": (4, 3, 1)}.items():
        r = reach[label]["reach"]
        assert (r["target"], r["delivered"], r["not_delivered_by_reason"]["endpoint_blocked"]) == (
            target, delivered, blocked), (label, r)
        assert r["not_delivered"] == blocked, (label, r)
    assert {label: reach[label]["grading_pending"] for label in reach} == {"D0": 0, "D1": 0, "D2": 0, "D3": 1}
    times = {label: reach[label]["response_time"] for label in reach}
    assert times["D0"]["median_ms"] is None
    assert (times["D1"]["median_ms"], times["D1"]["q1_ms"], times["D1"]["q3_ms"]) == (45_000, 37_500, 52_500)
    assert (times["D2"]["median_ms"], times["D3"]["median_ms"]) == (20_000, 10_000)
    friction = {label: reach[label]["friction"]["fields"] for label in reach}
    assert [f["format_errors"] for f in friction["D1"]] == [{"on_time": 1, "with_late": 1}]
    assert [f["decimal_comma"] for f in friction["D1"]] == [{"on_time": 1, "with_late": 1}]
    assert [f["format_errors"] for f in friction["D3"]] == [{"on_time": 1, "with_late": 1}]
    for label in ("D0", "D2"):
        assert all(f["format_errors"]["with_late"] == 0 and f["decimal_comma"]["with_late"] == 0
                   for f in friction[label]), label

    events = list_events(w.client, ADMIN_TOKEN, *EVENT_TYPES, limit=500)
    assert events["items"] and len(events["items"]) < 500
    items_ev = events["items"]
    graded = [e for e in items_ev if e["type"] == "submission.graded"]
    assert sorted(e["data"]["dispatch_id"] for e in graded) == sorted(
        [d1.dispatch_id] * 3 + [d2.dispatch_id, d3.dispatch_id])
    by_pseudo = {p.pseudo: p for p in w.pupils}
    sent = [(by_pseudo[e["data"]["person"]].key, e["data"]["origin"], e["occurred_at"])
            for e in items_ev if e["type"] == "student.invite_sent"]
    assert sorted(sent) == sorted([(k, "batch", iso(t_send)) for k in "ABCDEF"]
                                  + [("E", "coordination", iso(_at(t_send, hours=RESEND_HOURS)))]), sent
    joined = sorted((by_pseudo[e["data"]["person"]].key, e["occurred_at"]) for e in items_ev
                    if e["type"] == "student.joined")
    assert joined == sorted((k, iso(_at(t_send, hours=h))) for k, h in LINK_HOURS.items()), joined
    agent = [e["type"] for e in items_ev if e["actor_token_id"] == agent_token_id]
    assert sorted(agent) == ["dispatch.confirmed", "dispatch.prepared"], agent


# --- o gerador -------------------------------------------------------------------------------------------------------


def _plan_toml(w: _W) -> str:
    zone = ZoneInfo(ZONE)
    lines = [f'timezone = "{ZONE}"', f'offering = "{CODE}"', ""]
    for d in w.dispatches:
        assert d.opens_at is not None and d.closes_at is not None
        session = d.opens_at.astimezone(zone).date().isoformat() if d.kind == "intensive" else ""
        lines += ["[[slot]]", f'kind = "{d.kind}"', f'session = "{session}"',
                  f'opens_at = "{d.opens_at.astimezone(zone).isoformat()}"',
                  f'closes_at = "{d.closes_at.astimezone(zone).isoformat()}"',
                  f'repo = "{REPO}"', f'commit = "{COMMIT}"', f'qid = "{d.qid}"', f'field_type = "{d.field_type}"',
                  'source = "demo"', ""]
    return "\n".join(lines)


@dataclasses.dataclass
class _Run:
    code: int
    stdout: str
    stderr: str
    requests: list[tuple[str, str]]


def _run_generator(client: Any, env: dict[str, str], argv: list[str], monkeypatch: pytest.MonkeyPatch) -> _Run:
    """Roda o `main` do gerador com a API dos testes (`http=client`), o ambiente dado e os fluxos capturados;
    registra os pedidos pelo gancho do cliente HTTP e troca os métodos de escrita do `ApiClient` por um gravador."""
    from cq_client.api import ApiClient

    module = load_ops(SCRIPT)
    requests: list[tuple[str, str]] = []
    writes: list[str] = []

    def record(request: Any) -> None:
        requests.append((request.method, request.url.path))

    def forbid(name: str) -> Callable[..., Any]:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            writes.append(name)
            raise AssertionError(f"o gerador chamou o método de escrita {name}")
        return call

    with monkeypatch.context() as patch:
        for name in WRITE_METHODS:
            assert hasattr(ApiClient, name), f"o ApiClient não tem {name}"
            patch.setattr(ApiClient, name, forbid(name))
        hooks = client.event_hooks["request"]
        hooks.append(record)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module.main(argv, http=client, env=env)
        finally:
            hooks.remove(record)
    assert writes == [], writes
    return _Run(code=code, stdout=out.getvalue(), stderr=err.getvalue(), requests=requests)


def _assert_reads_only(run: _Run) -> None:
    assert run.requests, "o gerador não leu nada da API"
    used: set[str] = set()
    for method, path in run.requests:
        assert method == "GET", f"pedido de escrita do gerador: {method} {path}"
        matched = [op for op, route in READ_ROUTES.items() if route.fullmatch(path)]
        assert matched, f"rota fora das 4 leituras permitidas: {path}"
        used.update(matched)
    assert {"dispatches.list", "reports.get", "events.list"} <= used, used


def _assert_no_identity(w: _W, text: str) -> None:
    assert find_canary(text, prefixes=STUDENT_PREFIXES + DISPATCH_PREFIXES) == [], "identidade de aluno na saída"
    folded = text.lower()
    for pupil in w.pupils:
        assert pupil.pseudo.lower() not in folded, f"pseudônimo de {pupil.key} na saída"
        assert not re.search(rf"(?<![0-9a-f-]){re.escape(pupil.alias.lower())}(?![0-9a-f-])", folded), (
            f"apelido de {pupil.key} na saída")
        assert str(pupil.chat) not in text, f"chat_id de {pupil.key} na saída"
        assert pupil.email.lower() not in folded and pupil.ra not in text
    for free_text in ('"abc"', '"1.5"', '"6,5"', '"13"', '"77"', "/naosei", "/recuso"):
        assert free_text not in text, f"texto livre de aluno na saída: {free_text}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"horário sem fuso: {value}"
    return parsed


def _normalized(item: dict) -> dict:
    out = dict(item)
    for key in ("opens_at", "closes_at"):
        if key in out and isinstance(out[key], str):
            out[key] = _instant(out[key])
    if isinstance(out.get("format_errors_by_field_type"), dict):
        out["format_errors_by_field_type"] = {k: v for k, v in out["format_errors_by_field_type"].items() if v}
    return out


def _assert_report(w: _W, body: dict, *, agent: bool) -> None:
    from_module = load_ops(SCRIPT)
    dispatches, total = _expected(w, agent=agent)
    assert set(body) == {"generated_at", "offering", "dispatches", "total", "manual"}, sorted(body)
    _instant(body["generated_at"])
    assert body["offering"] == CODE
    assert len(body["dispatches"]) == len(dispatches)
    for got, want in zip(body["dispatches"], dispatches, strict=True):
        assert set(got) == DISPATCH_KEYS, (want["qid"], sorted(set(got) ^ DISPATCH_KEYS))
        assert _normalized(got) == want, (want["dispatch_id"], got)
    assert set(body["total"]) == TOTAL_KEYS, sorted(set(body["total"]) ^ TOTAL_KEYS)
    assert _normalized(body["total"]) == total, body["total"]
    manual = body["manual"]
    names = [m["name"] for m in manual]
    assert len(names) == len(set(names)), names
    assert all(set(m) == {"name", "reason"} and m["reason"] in ("diary", "not_exposed") for m in manual), manual
    for metric in from_module.MANUAL_METRICS:
        assert {"name": metric.name, "reason": "diary"} in manual, metric
    assert {"name": "terms_refused", "reason": "not_exposed"} in manual, manual
    measured = set(TOTAL_KEYS) - {"terms_refused"}
    assert not measured & set(names), "métrica medida marcada como manual"


def test_pilot_metrics_from_a_fictitious_pilot_by_reads_only_and_without_identity(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, db_url: str, bot_api: Any, bots: Any,
        smtp: Any, mail_channel: Any, engine_fake: Any, repo_fetcher: Any, report_worker: Any, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """o piloto fictício do docstring do módulo; o gerador em JSON (com e sem
    `--agent-token-id`), em Markdown (padrão), no stdout e em `--out`, com os números esperados à mão, só leituras e
    sem identidade."""
    pupils = _pupils()
    engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)

    # Oferecimento, bot e o perfil do bot antes do worker de console.
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, course=COURSE, term=TERM, timezone=ZONE)
    tg_bot = bot_api.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, CODE, bot_api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, CODE)
    bot_row = sql.execute("SELECT b.id FROM offerings o JOIN bots b ON b.offering_id = o.id "
                          "WHERE o.offering_id = %s::uuid", (offering["offering_id"],)).fetchone()[0]
    processed = _process_profile_tasks(db_url, clock, settings, bots)
    assert processed and processed == [(PROFILE, "done")] * len(processed), processed

    # Os tokens: o do agente (escrita e leitura) e o do gerador (só leitura).
    agent_secret, agent_body = create_token(client, ADMIN_TOKEN, ["offerings:read", "offerings:write"],
                                            label="agente do piloto")
    reader_secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "events:read"], label="leitura do piloto")

    # Lista e convites (duas fases); os e-mails saem pelo worker de console.
    body = import_students(client, ADMIN_TOKEN, CODE, [p.row() for p in pupils])
    assert (body["received"], body["enrollments_created"]) == (6, 6), body
    for pupil in pupils:
        pupil.person_row, pupil.pseudo, pupil.alias = sql.execute(
            "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
            "WHERE i.ra = %s", (pupil.ra,)).fetchone()
    prepared = prepare_invites(client, ADMIN_TOKEN, CODE)
    assert prepared["eligible"] == 6
    t_send = clock.now()
    assert send_invites(client, ADMIN_TOKEN, CODE, prepared["confirmation_code"])["queued"] == 6

    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        worker = report_worker(console=True, verification=mail_channel,
                           settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        w = _W(client=client, sql=sql, clock=clock, engine_fake=engine_fake, stream=worker.stream, inbox=inbox,
               offering=offering, bot_row=int(bot_row), pupils=pupils)
        _wait(lambda: len(smtp.messages()) == 6 and sql.execute(
            "SELECT count(*) FROM invite_sends WHERE state = 'sent'").fetchone()[0] == 6, "os seis convites")
        _settle(w)
        codes: dict[str, str] = {}
        for pupil in pupils:
            links = [link for link in smtp.invite_links()
                     if tuple(r.lower() for r in link.rcpt_to) == (pupil.email.lower(),)]
            assert len(links) == 1, f"e-mails de convite para {pupil.key}: {len(links)}"
            codes[pupil.key] = links[0].code
            pupil.console = ConsoleStudent(inbox, bot_id=w.bot_row, chat_id=pupil.chat, recipient=pupil.pseudo,
                                           source=worker.stream)
        _scenario(w, codes, t_send, agent_secret)
    finally:
        report_worker.stop()
        inbox.close()

    _check_preconditions(w, t_send, agent_body["token_id"])

    # O gerador: o perfil remoto num arquivo de configuração do teste e o plano TOML.
    config = tmp_path / "cq-config.toml"
    config.write_text(f'[profiles.{PROFILE_NAME}]\nkind = "remote"\napi_url = "http://testserver"\n'
                      f'token = "{reader_secret}"\n', encoding="utf-8")
    plan = tmp_path / "plano-de-disparos.toml"
    plan.write_text(_plan_toml(w), encoding="utf-8")
    env = {"CQ_CONFIG": str(config)}
    base = ["--profile", PROFILE_NAME, "--offering", CODE, "--plan", str(plan)]

    with_agent = _run_generator(client, env, base + ["--agent-token-id", agent_body["token_id"], "--json"],
                                monkeypatch)
    assert with_agent.code == 0, with_agent.stderr
    _assert_reads_only(with_agent)
    _assert_no_identity(w, with_agent.stdout + with_agent.stderr)
    report = json.loads(with_agent.stdout)
    _assert_report(w, report, agent=True)

    without_agent = _run_generator(client, env, base + ["--json"], monkeypatch)
    assert without_agent.code == 0, without_agent.stderr
    _assert_reads_only(without_agent)
    _assert_report(w, json.loads(without_agent.stdout), agent=False)

    out_json = tmp_path / "metricas.json"
    to_file = _run_generator(client, env, base + ["--agent-token-id", agent_body["token_id"], "--json", "--out",
                                                  str(out_json)], monkeypatch)
    assert to_file.code == 0, to_file.stderr
    assert report["dispatches"] and report["dispatches"][0]["dispatch_id"] not in to_file.stdout, (
        "com --out, o relatório não vai ao stdout")
    from_file = json.loads(out_json.read_text(encoding="utf-8"))
    assert {k: v for k, v in from_file.items() if k != "generated_at"} == {
        k: v for k, v in report.items() if k != "generated_at"}

    markdown = _run_generator(client, env, base, monkeypatch)
    assert markdown.code == 0, markdown.stderr
    _assert_reads_only(markdown)
    _assert_no_identity(w, markdown.stdout + markdown.stderr)
    text = markdown.stdout
    assert text.lstrip().startswith("#"), "o Markdown começa por um título"
    assert CODE in text
    for d in w.dispatches:
        assert d.qid in text, f"o Markdown sem o disparo {d.label}"
    assert "manual" in text.lower(), "o Markdown marca as métricas manuais"

    out_md = tmp_path / "metricas.md"
    md_file = _run_generator(client, env, base + ["--out", str(out_md)], monkeypatch)
    assert md_file.code == 0, md_file.stderr
    written = out_md.read_text(encoding="utf-8")
    assert written.lstrip().startswith("#") and CODE in written
    _assert_no_identity(w, written)
    _assert_no_identity(w, out_json.read_text(encoding="utf-8"))

    # Controle positivo do detector: o nome canário de A seria achado.
    assert find_canary(STUDENT_A.name, prefixes=STUDENT_PREFIXES)
    assert find_canary(DISPATCH_B.email, prefixes=DISPATCH_PREFIXES)
