"""Ponta a ponta dos relatórios e do acompanhamento pelo console, da equipe ao resumo diário.


O roteiro de validação, só com dublês, pelo adaptador de console: **oferecimento → vínculo da equipe → lista e
vínculo dos alunos → preparo (a equipe recebe o cabeçalho e a questão da prévia como linhas do console; a
resposta ao cabeçalho recebe a `PREVIEW_REPLY`) → confirmação → abertura (o aviso "aberto") → respostas no
prazo, "Não sei" e uma falha de questão na correção (o aviso de falha) → fechamento (a versão 1 congelada e
o resumo de fechamento) → resposta atrasada → relatório sob demanda com o par diferente da versão 1 →
reenvio (a versão 2 e o resumo atualizado) → resumo diário com o `FakeClock`**, com os estados e os eventos
esperados em cada passo e nenhuma tarefa com `NO_HANDLER_ERROR`.

Montagem:
- **Oferecimento e bot** pela API (o bot na Bot API falsa, `bot_api`, só para o registro e o
  perfil); a `bot.sync_profile` processada **antes** do worker de console, pela fila real com o
  tratador de perfil dos bots (o worker de console não tem tratador de perfil; como o `test_console_e2e.py`).
- **Lista** pela importação e **convites** em duas fases, com os e-mails mandados pelo próprio worker
  (`verification=` o adaptador SMTP real sobre o SMTP falso); o código vem do link do e-mail.
- **Worker dos relatórios** pela fixture `report_worker(console=True)`: `run_worker(…, console_routes=True)` com o motor
  falso estrito (`HttpQuestionEngine`), a obtenção falsa e o `ConsoleChannel` num `StringIO`; os tratadores
  dos relatórios entram pelo próprio `run_worker`.
- **Equipe** pela equipe simulada (`tests/helpers/team.py`): dois membros vinculados pelo link de equipe do
  oferecimento, com o `/start` pela `ConsoleInbox` (o worker do teste processa o vínculo); cada membro lê as suas
  linhas pelo `recipient` e responde pela `ConsoleInbox`.
- **Alunos** (quatro, nomes neutros) vinculados pelo console (`/start <código>` e `/aceito`).
- **Disparo**: um imediato com `closes_at` informado da questão `number` do curso de teste (o preset do motor
  falso: um campo de número, rótulo "Velocidade:", correto 6,5).

Roteiro das respostas (os números do relatório esperados abaixo saem daqui, calculados à mão):
- A responde "6,5" no prazo (correta; vírgula normalizada); B manda `/naosei`; a correção de C ("7")
  recebe do motor falso um `broken` injetado (`question_failed/grading_broken`; o aviso de falha de questão
  na correção); D não responde no prazo (`no_answer`) e responde "6.5" atrasado, depois da versão 1.
- Alcance: alvo 4; entregues 3 (A, B, D: as que chegaram ao aluno); falha de questão 1 (C, estágio
  `grading`); nenhum não entregue nem pendente.
- Versão 1 (fechamento): respondidas, corretas (1, 1); "não sei" (1, 1); parciais e incorretas (0, 0);
  taxas sobre as 3 entregues: corretas e resposta (1/3, 1/3), parciais e incorretas (0, 0); nota média
  (1, 1). Tempo até responder: só A no prazo, 30 s depois do envio (n = 1: o próprio valor nos três).
  Distribuição: o valor "6.5" e a razão "1" com uma ocorrência (abaixo do mínimo de 2: só em "outras").
  Fricção: a vírgula de A (1, 1).
- Depois da atrasada de D (cálculo no momento e versão 2): respondidas e corretas (1, 2); corretas e
  resposta (1/3, 2/3); nota média (1, 1); o valor "6.5" e a razão "1" com (1, 2) viram itens; "outras"
  (0, 0). O tempo até responder não muda (a atrasada fica fora).

Leituras [I] deste arquivo (inferências):
- os textos dos avisos e dos resumos são conferidos pelas **linhas fixas** dos modelos de `noticetexts`
  (reais no esqueleto) com os valores calculados aqui; as linhas com horário no fuso, taxa em porcentagem
  ou duração (formatos que nada no produto fixa) ficam de fora, e o par é o texto fixo,
  `"{a} no prazo · {b} com atrasadas"`, escrito por extenso (o `noticetexts.pair` lança no esqueleto);
- no cabeçalho da prévia, conferem-se as linhas comuns a `PREVIEW_HEADER` e `PREVIEW_IMMEDIATE` (a escolha
  entre os dois para o imediato com fechamento informado é do teste da prévia) e o texto fixo do aviso
  `no_teacher_contact` do oferecimento no padrão;
- a mensagem da questão na prévia é igual, em texto, à do aluno A (o mesmo `_render` do estado inicial,
  com o fechamento informado, sem a diferença do imediato sem fechamento, que não tem "Prazo:"); o pedido do campo
  da prévia contém o texto do pedido do aluno e a linha `PREVIEW_FIELD_HINT`, sem `reply_request`;
- o `computed_at` da versão do fechamento é o próprio `frozen_at` (o mesmo `now` do congelamento), entre o
  `closes_at` e o relógio do teste depois do fechamento;
- o resumo diário é ligado para o minuto local seguinte a dois minutos depois do relógio do teste (o mesmo
  dia local), e o relógio salta direto para depois dele; "Disparos abertos ou fechados no dia" é a
  contagem de linhas de disparo; "Vínculos novos no dia" não é conferido (nada no produto fixa se os
  vínculos de equipe contam);
- as taxas fracionárias (1/3, 2/3) são comparadas com `pytest.approx(rel=1e-12)`, por serem divisões de
  ponto flutuante; todas as demais, por igualdade.

No esqueleto, o vínculo da equipe passa, e a função falha na
primeira consulta a `team_notices` (tabela ausente: a migração `0005` ainda não existe); depois da migração,
pelos avisos, pela prévia e pelas rotas dos relatórios ainda não produzidos. Verde só no fim da implementação (a ponta a ponta
usa todas as partes do fluxo).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import noticetexts
from cq_server.adapters.console import ConsoleInbox
from cq_server.bots import profile_task_handler
from cq_server.tasks import NO_HANDLER_ERROR, TaskQueue
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    get_offering_digest,
    get_report,
    import_students,
    list_events,
    list_report_versions,
    prepare_dispatch,
    prepare_invites,
    register_bot,
    resend_report_summary,
    send_invites,
    students_summary,
    update_offering_digest,
)
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent, read_lines
from tests.helpers.engine import ANSWER_SENTINEL
from tests.helpers.team import ACTIVE, Team, TeamMember, link_team, team_lines, team_messages

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

COURSE = "Física Experimental"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
CODE = "console-relatorios-ponta"
BOT_USERNAME = "cq_console_relatorios_bot"
WORKER_ID = "worker-console-relatorios"
PREP_WORKER_ID = "preparacao-console-relatorios"
REPO = "https://repos.cq.test/disciplina/questoes-relatorios-console.git"
COMMIT = "3b7e" * 10
COMMIT_SHORT = COMMIT[:12]
QID = "number"
TITLE = "Velocidade média"  # o título do preset `number` do motor falso
STATEMENT = "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?"
FIELD_LABEL = "Velocidade:"
FIELD_HINT = "Número; ex.: 4.25, -17 ou 3/7"
ORIGIN = "integration"
WINDOW = timedelta(minutes=30)
WAIT = 30.0  # segundos reais de espera por uma condição
RESPONSE_SECONDS = 30  # de `t_conf` (entrega) a `t_ans` (respostas): `response_ms = 30000`
PROFILE = "bot.sync_profile"
REPLY_TEXT = "Conferido, obrigado."
NO_TEACHER_CONTACT_MESSAGE = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                              "o aluno que escrever fora das questões não saberá a quem se dirigir.")
# O `broken` da correção injetado na resposta de C (o contrato `grade_response` do motor).
GRADE_BROKEN = {"outcome": "broken",
                "reason": {"code": "question_code_error", "message": "O código da questão falhou ao corrigir.",
                           "phase": "grade"},
                "version_mismatch": {"engine": False, "pl": False}, "warnings": []}

# Os tipos de evento dos relatórios, como literais (o esqueleto não os tem em `EventType`).
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
DISPATCH_FLOW_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.confirmed", "dispatch.opened",
                     "dispatch.closed", "delivery.delivered", "delivery.not_delivered",
                     "delivery.question_failed", "delivery.dont_know", "submission.graded",
                     "submission.internal_failure")
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}
NO_REASONS = {"endpoint_blocked": 0, "channel_refused": 0, "engine_unavailable": 0, "bot_no_access": 0,
              "channel_unavailable": 0}

# Os textos fixos do vínculo do aluno pelo console, por extenso.
TERMS_ACCEPT = "Para aceitar, toque em /aceito"


# --- montagem ----------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Pupil:
    key: str
    name: str
    ra: str
    email: str
    chat: int
    pseudo: str = ""
    person_row: int = 0
    console: ConsoleStudent | None = None

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}

    @property
    def io(self) -> ConsoleStudent:
        assert self.console is not None
        return self.console


def _pupils() -> list[_Pupil]:
    """Quatro alunos neutros (fora dos prefixos canário)."""
    return [_Pupil(key=key, name=f"Pessoa Console Seis {n:02d}", ra=f"59600006{n:02d}",
                   email=f"console.seis{n:02d}@turma.example.org", chat=5_660_000_000 + n)
            for n, key in enumerate("ABCD", start=1)]


@dataclasses.dataclass
class _W:
    client: Any
    sql: psycopg.Connection
    clock: Any
    engine_fake: Any
    stream: Any
    inbox: ConsoleInbox
    offering: dict
    offering_row: int
    bot_row: int
    pupils: list[_Pupil]
    team: Team | None = None
    members: dict[str, ConsoleStudent] = dataclasses.field(default_factory=dict)
    dispatch: dict | None = None
    closes: datetime | None = None

    @property
    def dispatch_id(self) -> str:
        assert self.dispatch is not None
        return str(self.dispatch["dispatch_id"])

    @property
    def offering_ref(self) -> dict:
        return {"offering_id": self.offering["offering_id"], "code": CODE}


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
    """Espera o sistema parar (nenhuma tarefa, mensagem ou update disponível), duas vezes, com mais de uma
    rodada da varredura (1 s real) entre elas; só então o relógio pode saltar."""
    for _ in range(10):
        _wait(lambda: not _busy(w), "o sistema parar")
        time.sleep(1.2)
        if not _busy(w):
            return
    raise AssertionError(f"o sistema não parou: {_busy(w)}")


def _ev(type_: str, at: datetime | str, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": at if isinstance(at, str) else iso(at), "actor_token_id": actor,
            "data": data}


def _notice_ev(w: _W, kind: str, at: datetime | str, *, dispatch: bool = True, report_version: int | None = None,
               local_date: str | None = None, state: str = "queued", count: int = 2) -> dict:
    """O `notice.recorded` esperado: o assunto do tipo e `null` nos demais campos."""
    return _ev("notice.recorded", at, offering_id=w.offering["offering_id"], notice_type=kind,
               dispatch_id=w.dispatch_id if dispatch else None, report_version=report_version,
               episode_seq=None, batch_id=None, window_started_at=None, local_date=local_date, state=state,
               recipient_count=count)


def _last_seq(w: _W) -> int:
    return w.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events(w: _W, cursor: int, *types: str) -> list[dict]:
    """Os eventos dos tipos depois de `cursor`, sem o `seq` (conferido crescente e sem repetição)."""
    items = list_events(w.client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _pair(on_time: int, with_late: int) -> dict[str, int]:
    return {"on_time": on_time, "with_late": with_late}


def _rate(on_time: float | None, with_late: float | None) -> dict[str, Any]:
    def one(value: float | None) -> Any:
        return None if value is None else pytest.approx(value, rel=1e-12, abs=0.0)
    return {"on_time": one(on_time), "with_late": one(with_late)}


def _pair_text(on_time: int, with_late: int) -> str:
    """O par dos textos (`noticetexts.pair`), por extenso."""
    return f"{on_time} no prazo · {with_late} com atrasadas"


def _fixed_lines(template: str, **values: Any) -> list[str]:
    """As linhas do modelo cujos campos estão todos em `values`, já preenchidas (ver a docstring do módulo)."""
    lines = []
    for line in template.split("\n"):
        try:
            lines.append(line.format(**values))
        except KeyError:
            continue
    return lines


def _contains_lines(text: str, lines: Iterable[str], what: str) -> None:
    for line in lines:
        assert line in text, f"{what}: falta a linha {line!r} em {text!r}"


# --- preparação: perfil do bot, lista, convites --------------------------------------------------------


def _process_profile_tasks(db_url: str, clock: Any, settings: Any, bots: Any) -> list[tuple[str, str]]:
    """A `bot.sync_profile` do registro e da ativação, processada pela fila com o tratador de perfil dos
    bots antes do worker de console (o worker de console não tem tratador de perfil)."""
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


def _say(w: _W, chat: int, act: Callable[[], None], *, outcome: str) -> dict:
    """Uma entrada simulada (aluno ou membro): espera a linha gravada e processada com o desfecho dado e
    devolve a linha de `telegram_updates`."""
    mark = w.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates").fetchone()[0]
    act()

    def done() -> list[dict] | None:
        rows = _all(w.sql, "SELECT * FROM telegram_updates WHERE id > %s AND bot_id = %s AND chat_id = %s "
                    "ORDER BY id", (mark, w.bot_row, chat))
        return rows if rows and all(r["state"] == "processed" for r in rows) else None

    rows = _wait(done, f"a entrada no chat {chat} processada")
    assert len(rows) == 1, f"a entrada gravou {len(rows)} linhas"
    assert rows[0]["update_id"] < 0 and rows[0]["outcome"] == outcome, (rows[0]["outcome"], outcome)
    return rows[0]


# --- disparo, entregas e mensagens -------------------------------------------------------------------


def _dispatch_row(w: _W) -> dict:
    row = _one(w.sql, "SELECT id, state, closed_at FROM dispatches WHERE dispatch_uuid = %s::uuid",
               (w.dispatch_id,))
    assert row is not None
    return row


def _delivery(w: _W, pupil: _Pupil) -> dict:
    row = _one(w.sql, """
        SELECT d.id, d.state, d.failure, d.principal_message_id
          FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id
          JOIN enrollments e ON e.id = d.enrollment_id
         WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s""", (w.dispatch_id, pupil.person_row))
    assert row is not None, f"sem entrega de {pupil.key}"
    return row


def _line_of(w: _W, message_id: int) -> ConsoleLine:
    found = [line for line in read_lines(w.stream.getvalue()) if line.message_id == message_id]
    assert len(found) == 1, f"linhas do console da mensagem {message_id}: {len(found)}"
    return found[0]


def _question_line(w: _W, pupil: _Pupil) -> ConsoleLine:
    principal = _delivery(w, pupil)["principal_message_id"]
    assert principal is not None, f"entrega de {pupil.key} sem mensagem principal"
    return _line_of(w, int(principal))


def _prompt_line(w: _W, pupil: _Pupil) -> ConsoleLine:
    """O pedido do campo 0 enviado ao aluno (o primeiro, da abertura)."""
    delivery = _delivery(w, pupil)
    rows = _wait(lambda: w.sql.execute(
        "SELECT id FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' "
        "AND field_index = 0 AND state = 'sent' ORDER BY id", (delivery["id"],)).fetchall(),
        f"o pedido do campo de {pupil.key}")
    return _line_of(w, int(rows[0][0]))


def _submissions(w: _W, pupil: _Pupil) -> list[dict]:
    return _all(w.sql, "SELECT outcome, valid, score, late, response_ms FROM submissions WHERE delivery_id = %s "
                "ORDER BY id", (_delivery(w, pupil)["id"],))


def _close(w: _W, timeout: float = 3 * WAIT) -> None:
    """Espera o disparo `closed`; sem isso, avança o relógio além de `CLOSE_RECHECK_SECONDS` (5 s)."""
    deadline = time.monotonic() + timeout
    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if _dispatch_row(w)["state"] == "closed":
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            raise AssertionError(f"o disparo não fechou em {timeout} s: {_dispatch_row(w)['state']}")
        w.clock.advance(6)


# --- avisos à equipe ---------------------------------------------------------------------------------


def _member(w: _W, label: str) -> TeamMember:
    assert w.team is not None
    return w.team.member(label)


def _notice_texts(w: _W, kind: str) -> dict[str, str]:
    """O texto do aviso de texto `kind` para cada membro com endereço ativo: exatamente uma mensagem por
    membro, enviada, com prioridade 2, `text_format = "plain"` e o mesmo `notice_id` para todos; o texto da
    linha do console do membro (pelo `recipient`) é o da fila."""
    assert w.team is not None
    texts: dict[str, str] = {}
    notice_ids = set()
    for member in w.team.active:
        messages = _wait(lambda m=member: (found := team_messages(w.sql, kind, m))
                         and all(x.state == "sent" for x in found) and found,
                         f"o aviso {kind} de {member.label} enviado")
        assert len(messages) == 1, f"{kind}: {len(messages)} mensagens a {member.label}"
        message = messages[0]
        assert (message.role, message.text_format, message.priority, message.state) == (None, "plain", 2, "sent")
        assert message.text is not None and ANSWER_SENTINEL not in message.text
        line = _line_of(w, message.message_id)
        assert (line.recipient, line.text) == (member.recipient, message.text)
        texts[member.label] = message.text
        notice_ids.add(message.notice_id)
    assert len(notice_ids) == 1, f"{kind}: um aviso só para a equipe inteira, não {len(notice_ids)}"
    return texts


def _team_message_count(w: _W) -> int:
    return w.sql.execute("SELECT count(*) FROM outbox_messages WHERE notice_id IS NOT NULL").fetchone()[0]


# --- relatório ---------------------------------------------------------------------------------------


def _report(w: _W, *, computed_at: str, frozen: dict | None, late: bool) -> dict:
    """O relatório esperado, com os números calculados à mão na docstring do módulo."""
    assert w.dispatch is not None and w.closes is not None
    answered = _pair(1, 2) if late else _pair(1, 1)
    top = ({"items": [{"value": "6.5", "count": _pair(1, 2)}], "others": _pair(0, 0)} if late
           else {"items": [], "others": _pair(1, 1)})
    ratios = ({"items": [{"value": "1", "count": _pair(1, 2)}], "others": _pair(0, 0)} if late
              else {"items": [], "others": _pair(1, 1)})
    return {
        "report_schema_version": "1",
        "offering": w.offering_ref,
        "timezone": ZONE,
        "dispatch": {"dispatch_id": w.dispatch_id, "question": {"qid": QID, "commit_short": COMMIT_SHORT},
                     "title": TITLE, "state": "closed", "partial": False, "origin": ORIGIN,
                     "opens_at": w.dispatch["opens_at"], "closes_at": iso(w.closes)},
        "computed_at": computed_at,
        "frozen": frozen,
        "grading_pending": 0,
        "reach": {"target": 4, "delivered": 3, "not_delivered": 0, "not_delivered_by_reason": dict(NO_REASONS),
                  "question_failed": 1, "question_failed_by_stage": {"preparation": 0, "grading": 1}, "pending": 0},
        "performance": {
            "answered": answered, "correct": answered, "partially_correct": _pair(0, 0),
            "incorrect": _pair(0, 0), "dont_know": _pair(1, 1),
            "correct_rate": _rate(1 / 3, 2 / 3 if late else 1 / 3), "partially_correct_rate": _rate(0.0, 0.0),
            "incorrect_rate": _rate(0.0, 0.0), "response_rate": _rate(1 / 3, 2 / 3 if late else 1 / 3),
            "mean_score": _rate(1.0, 1.0)},
        "response_time": {"observations": 1, "negative": 0, "median_ms": RESPONSE_SECONDS * 1000,
                          "q1_ms": RESPONSE_SECONDS * 1000, "q3_ms": RESPONSE_SECONDS * 1000},
        "distribution": {"fields": [{"index": 0, "type": "number", "label": FIELD_LABEL, "unavailable": None,
                                     "choices": None, "values": top, "ratios": ratios, "unrated": _pair(0, 0),
                                     "texts": None}]},
        "friction": {"fields": [{"index": 0, "format_errors": _pair(0, 0), "decimal_comma": _pair(1, 1)}],
                     "unmapped_format_errors": _pair(0, 0)},
    }


def _summary_lines(w: _W, template: str, *, late: bool, **extra: Any) -> list[str]:
    answered = (1, 2) if late else (1, 1)
    return _fixed_lines(template, title=TITLE, target=4, delivered=3, not_delivered=0, question_failed=1,
                        answered=_pair_text(*answered), correct=_pair_text(*answered),
                        partially_correct=_pair_text(0, 0), dont_know=_pair_text(1, 1), code=CODE,
                        dispatch_id=w.dispatch_id, **extra)


# --- o roteiro -------------------------------------------------------------------------------------------


def test_console_end_to_end_reports_from_team_link_to_daily_digest(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, db_url: str, bot_api: Any, bots: Any,
        smtp: Any, mail_channel: Any, engine_fake: Any, repo_fetcher: Any, report_worker: Any) -> None:
    """Ponta a ponta dos relatórios pelo console.

    oferecimento → vínculo da equipe (link de equipe pela `ConsoleInbox`) → lista e vínculo dos alunos (o código
    do SMTP falso) → preparo (a equipe recebe o cabeçalho, a questão e o pedido do campo da prévia como
    linhas do console; a resposta ao cabeçalho → `PREVIEW_REPLY`) → confirmação → abertura (o aviso
    "aberto") → respostas no prazo, "Não sei" e uma falha de questão na correção (o aviso de falha) →
    fechamento (versão 1 e o resumo) → resposta atrasada → relatório sob demanda com o par diferente da
    versão 1 → reenvio (versão 2 e o resumo atualizado) → resumo diário com o `FakeClock`; os estados e os
    eventos esperados em cada passo; nenhuma tarefa com `NO_HANDLER_ERROR`."""
    pupils = _pupils()

    # 1. Oferecimento e bot; o perfil do bot processado antes do worker de console.
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, course=COURSE, term=TERM, timezone=ZONE)
    tg_bot = bot_api.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, CODE, bot_api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, CODE)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    processed = _process_profile_tasks(db_url, clock, settings, bots)
    assert processed and processed == [(PROFILE, "done")] * len(processed), processed

    # 2. Lista e convites em duas fases; os e-mails saem pelo worker de console.
    body = import_students(client, ADMIN_TOKEN, CODE, [p.row() for p in pupils])
    assert (body["received"], body["enrollments_created"]) == (4, 4), body
    for pupil in pupils:
        pupil.person_row, pupil.pseudo = sql.execute(
            "SELECT p.id, p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
            (pupil.ra,)).fetchone()
    prepared_invites = prepare_invites(client, ADMIN_TOKEN, CODE)
    assert prepared_invites["eligible"] == 4
    assert send_invites(client, ADMIN_TOKEN, CODE, prepared_invites["confirmation_code"])["queued"] == 4

    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        worker = report_worker(console=True, verification=mail_channel,
                           settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        w = _W(client=client, sql=sql, clock=clock, engine_fake=engine_fake, stream=worker.stream, inbox=inbox,
               offering=offering, offering_row=offering_row, bot_row=bot_row, pupils=pupils)
        _wait(lambda: len(smtp.messages()) == 4 and sql.execute(
            "SELECT count(*) FROM invite_sends WHERE state = 'sent'").fetchone()[0] == 4, "os quatro convites")
        codes: dict[str, str] = {}
        for pupil in pupils:
            links = [link for link in smtp.invite_links() if link.rcpt_to == (pupil.email,)]
            assert len(links) == 1, f"e-mails de convite para {pupil.key}: {len(links)}"
            codes[pupil.key] = links[0].code
            pupil.console = ConsoleStudent(inbox, bot_id=bot_row, chat_id=pupil.chat, recipient=pupil.pseudo,
                                           source=worker.stream)
        _run(w, tg_bot, codes, repo_fetcher)
    finally:
        report_worker.stop()
        inbox.close()


def _run(w: _W, tg_bot: int, codes: dict[str, str], repo_fetcher: Any) -> None:
    offering_id = w.offering["offering_id"]
    a, b, c, d = w.pupils

    # 3. Vínculo da equipe pelo link de equipe, com o `/start` pela `ConsoleInbox`; nenhum aviso ainda.
    w.clock.advance(60)
    w.team = link_team(w.client, w.sql, w.offering, tg_bot, (ACTIVE, ACTIVE), inbox=w.inbox)
    for member in w.team.members:
        assert member.chat_id is not None
        w.members[member.label] = ConsoleStudent(w.inbox, bot_id=w.bot_row, chat_id=member.chat_id,
                                                 recipient=member.pseudo_id, source=w.stream)
    team_rows = _all(w.sql, "SELECT e.role, e.state FROM enrollments e WHERE e.offering_id = %s AND e.role = 'team'",
                     (w.offering_row,))
    assert team_rows == [{"role": "team", "state": "active"}] * 2
    assert w.sql.execute("SELECT count(*) FROM team_notices").fetchone()[0] == 0, "nenhum aviso antes do disparo"
    assert _team_message_count(w) == 0

    # 4. Vínculo dos alunos pelo console: `/start <código>` → termo; `/aceito` → matrícula e endereço.
    w.clock.advance(60)
    for number, pupil in enumerate(w.pupils, start=1):
        _say(w, pupil.chat, lambda p=pupil: p.io.say(f"/start {codes[p.key]}"), outcome="student_terms_presented")
        # O termo vai ao chat do update (sem pessoa ainda): a linha não tem o `recipient` do aluno.
        _wait(lambda n=number: len([ln for ln in read_lines(w.stream.getvalue()) if TERMS_ACCEPT in ln.text]) == n,
              f"o termo de {pupil.key}")
        _say(w, pupil.chat, lambda p=pupil: p.io.say("/aceito"), outcome="student_joined")
    linked = _all(w.sql, "SELECT e.state FROM enrollments e WHERE e.offering_id = %s AND e.role = 'student'",
                  (w.offering_row,))
    assert linked == [{"state": "active"}] * 4
    _settle(w)
    # O resumo diário vem desligado às 20:00.
    assert get_offering_digest(w.client, CODE) == {"offering": w.offering_ref, "timezone": ZONE, "enabled": False,
                                                   "local_time": "20:00"}

    # 5. Preparo e validação; a prévia à equipe (cabeçalho, questão e pedido do campo) e a resposta ao
    # cabeçalho.
    w.engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    t_prep = w.clock.advance(60)
    w.closes = t_prep + WINDOW
    cursor = _last_seq(w)
    w.dispatch = prepare_dispatch(w.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid=QID,
                                  closes_at=iso(w.closes), origin=ORIGIN)
    assert w.dispatch["state"] == "validating"
    _wait(lambda: _dispatch_row(w)["state"] != "validating", "a validação")
    _settle(w)
    scheduled = get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)
    assert (scheduled["state"], scheduled["target_count"], scheduled["preview"]["target_count"]) == (
        "scheduled", 4, 4), scheduled
    preview_lines = _check_preview(w)
    assert [e["type"] for e in _events(w, cursor, *DISPATCH_FLOW_EVENT_TYPES)] == ["dispatch.prepared", "dispatch.validated"]
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(w, "dispatch_preview", t_prep)]
    _check_preview_reply(w, preview_lines["membro-1"][0])
    _settle(w)

    # 6. Confirmação e abertura: cada aluno recebe a sua variante; o aviso "aberto" atrás das entregas.
    t_conf = w.clock.advance(60)
    cursor = _last_seq(w)
    confirmed = confirm_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)
    assert (confirmed["confirmed"], confirmed["opens_at"]) == (True, iso(t_conf))
    w.dispatch = confirmed
    _wait(lambda: [r for (r,) in w.sql.execute(
        "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid",
        (w.dispatch_id,)).fetchall()] == ["delivered"] * 4, "as quatro entregas")
    _settle(w)
    opened = get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)
    assert (opened["state"], opened["opened_at"], opened["deliveries"]) == (
        "open", iso(t_conf), {**ZERO_COUNTS, "delivered": 4})
    texts = _notice_texts(w, "dispatch_opened")
    for text in texts.values():
        _contains_lines(text, _fixed_lines(noticetexts.OPENED, title=TITLE, target=4, delivered=4, not_delivered=0,
                                           question_failed=0, pending=0, code=CODE, dispatch_id=w.dispatch_id),
                        "aviso aberto")
        assert noticetexts.OPENED_PENDING not in text, "sem pendentes, sem a linha das tentativas"
    student_messages = [m for (m,) in w.sql.execute(
        "SELECT o.id FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
        "JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid", (w.dispatch_id,)).fetchall()]
    opened_messages = [m.message_id for m in team_messages(w.sql, "dispatch_opened")]
    assert student_messages and min(opened_messages) > max(student_messages), "o aviso atrás das entregas"
    _check_same_render(w, preview_lines["membro-1"])
    assert sorted(e["type"] for e in _events(w, cursor, *DISPATCH_FLOW_EVENT_TYPES)) == sorted(
        ["dispatch.confirmed", "dispatch.opened"] + ["delivery.delivered"] * 4)
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(w, "dispatch_opened", t_conf)]

    # 7. Respostas no prazo: A acerta com vírgula; B manda "Não sei"; a correção de C falha (`broken`).
    t_ans = w.clock.advance(RESPONSE_SECONDS)
    cursor = _last_seq(w)
    _say(w, a.chat, lambda: a.io.reply(_prompt_line(w, a), "6,5"), outcome="answer_recorded")
    _wait(lambda: [s["outcome"] for s in _submissions(w, a)] == ["graded"], "a correção de A")
    _say(w, b.chat, lambda: b.io.dont_know(_question_line(w, b)), outcome="answer_dont_know")
    w.engine_fake.inject("grade", body=GRADE_BROKEN, times=1)
    _say(w, c.chat, lambda: c.io.reply(_prompt_line(w, c), "7"), outcome="answer_recorded")
    _wait(lambda: _delivery(w, c)["state"] == "question_failed", "a falha de questão de C")
    _settle(w)
    assert _submissions(w, a) == [{"outcome": "graded", "valid": True, "score": 1.0, "late": False,
                                   "response_ms": RESPONSE_SECONDS * 1000}]
    assert (_delivery(w, c)["state"], _delivery(w, c)["failure"]) == ("question_failed", "grading_broken")
    assert get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)["deliveries"] == {
        **ZERO_COUNTS, "delivered": 1, "answered": 1, "dont_know": 1, "question_failed": 1}
    texts = _notice_texts(w, "question_failed")
    for text in texts.values():
        _contains_lines(text, _fixed_lines(noticetexts.QUESTION_FAILED, title=TITLE, code=CODE,
                                           dispatch_id=w.dispatch_id), "aviso de falha de questão")
    assert sorted(e["type"] for e in _events(w, cursor, *DISPATCH_FLOW_EVENT_TYPES)) == sorted(
        ["submission.graded", "delivery.dont_know", "delivery.question_failed"])
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(w, "question_failed", t_ans)]

    # 8. Fechamento pelo relógio: `no_answer` de D, a versão 1 (`closing`) e o resumo de fechamento.
    cursor = _last_seq(w)
    w.clock.set(w.closes + timedelta(seconds=1))
    _close(w)
    _settle(w)
    t_closed = w.clock.now()
    closed = get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)
    assert (closed["state"], closed["deliveries"]) == (
        "closed", {**ZERO_COUNTS, "answered": 1, "dont_know": 1, "no_answer": 1, "question_failed": 1})
    versions = list_report_versions(w.client, CODE, w.dispatch_id)
    assert [(i["version"], i["trigger"]) for i in versions["items"]] == [(1, "closing")], versions
    v1_ref = versions["items"][0]
    assert versions == {"offering": w.offering_ref, "dispatch_id": w.dispatch_id, "items": [v1_ref], "next_cursor": 1}
    frozen_at = datetime.fromisoformat(v1_ref["frozen_at"].replace("Z", "+00:00"))
    assert w.closes < frozen_at <= t_closed, (v1_ref, iso(t_closed))
    v1 = get_report(w.client, CODE, w.dispatch_id, version=1)
    assert v1 == _report(w, computed_at=v1_ref["frozen_at"], frozen=v1_ref, late=False)
    texts = _notice_texts(w, "dispatch_closed")
    for text in texts.values():
        _contains_lines(text, _summary_lines(w, noticetexts.CLOSED_SUMMARY, late=False), "resumo de fechamento")
        assert "Correções pendentes" not in text, "sem correção pendente, sem a linha"
    assert [e["type"] for e in _events(w, cursor, *DISPATCH_FLOW_EVENT_TYPES)] == ["dispatch.closed"]
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [
        _ev("report.frozen", v1_ref["frozen_at"], offering_id=offering_id, dispatch_id=w.dispatch_id, version=1,
            trigger="closing"),
        _notice_ev(w, "dispatch_closed", v1_ref["frozen_at"], report_version=1)]

    # 9. Atrasada: D responde "6.5" ao pedido do campo depois da versão 1.
    t_late = w.clock.advance(60)
    cursor = _last_seq(w)
    _say(w, d.chat, lambda: d.io.reply(_prompt_line(w, d), "6.5"), outcome="answer_recorded")
    _wait(lambda: _delivery(w, d)["state"] == "answered_late", "a atrasada de D")
    _settle(w)
    assert [(s["outcome"], s["valid"], s["score"], s["late"]) for s in _submissions(w, d)] == [
        ("graded", True, 1.0, True)]
    assert get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)["deliveries"] == {
        **ZERO_COUNTS, "answered": 1, "dont_know": 1, "answered_late": 1, "question_failed": 1}
    assert [e["type"] for e in _events(w, cursor, *DISPATCH_FLOW_EVENT_TYPES)] == ["submission.graded"]
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [], "a atrasada não congela nem avisa"

    # 10. Relatório sob demanda: o par com atrasadas muda; a versão 1 não.
    now_report = get_report(w.client, CODE, w.dispatch_id)
    assert now_report == _report(w, computed_at=iso(t_late), frozen=None, late=True)
    assert now_report["performance"]["answered"] != v1["performance"]["answered"], "o par difere da versão 1"
    assert get_report(w.client, CODE, w.dispatch_id, version=1) == v1, "a versão 1 é imutável"

    # 11. Reenvio: a versão 2 (`resend`) e o resumo atualizado.
    t_resend = w.clock.advance(60)
    cursor = _last_seq(w)
    v2_ref = {"version": 2, "trigger": "resend", "frozen_at": iso(t_resend)}
    assert resend_report_summary(w.client, CODE, w.dispatch_id) == {
        "offering": w.offering_ref, "dispatch_id": w.dispatch_id, "frozen": v2_ref}
    _settle(w)
    assert list_report_versions(w.client, CODE, w.dispatch_id) == {
        "offering": w.offering_ref, "dispatch_id": w.dispatch_id, "items": [v1_ref, v2_ref], "next_cursor": 2}
    v2 = get_report(w.client, CODE, w.dispatch_id, version=2)
    assert v2 == _report(w, computed_at=iso(t_resend), frozen=v2_ref, late=True)
    assert get_report(w.client, CODE, w.dispatch_id, version="latest") == v2
    texts = _notice_texts(w, "summary_resent")
    for text in texts.values():
        _contains_lines(text, _summary_lines(w, noticetexts.RESENT_SUMMARY, late=True, version=2), "resumo reenviado")
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [
        _ev("report.frozen", t_resend, "bootstrap", offering_id=offering_id, dispatch_id=w.dispatch_id, version=2,
            trigger="resend"),
        _notice_ev(w, "summary_resent", t_resend, report_version=2)]

    # 12. Resumo diário: ligado para daqui a dois minutos (minuto local seguinte); o relógio salta até ele.
    t_set = w.clock.advance(60)
    cursor = _last_seq(w)
    zone = ZoneInfo(ZONE)
    due_local = (t_set + timedelta(minutes=2)).astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    local_time = due_local.strftime("%H:%M")
    assert due_local.date() == t_set.astimezone(zone).date(), "o roteiro cabe num só dia local"
    expected_digest = {"offering": w.offering_ref, "timezone": ZONE, "enabled": True, "local_time": local_time}
    assert update_offering_digest(w.client, CODE, enabled=True, local_time=local_time) == expected_digest
    assert get_offering_digest(w.client, CODE) == expected_digest
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [
        _ev("offering.digest_changed", t_set, "bootstrap", offering_id=offering_id, enabled=True,
            local_time=local_time)]
    adhesion = students_summary(w.client, ADMIN_TOKEN, CODE)
    _settle(w)
    assert team_messages(w.sql, "daily_digest") == [], "o resumo não sai antes do horário"
    cursor = _last_seq(w)
    t_due = w.clock.set(due_local.astimezone(t_set.tzinfo) + timedelta(seconds=1))
    _settle(w)
    texts = _notice_texts(w, "daily_digest")
    digest_lines = _fixed_lines(noticetexts.DAILY_DIGEST, course=COURSE, late=1, dispatches=1,
                                **{k: adhesion[k] for k in ("linked", "sending", "terms_presented", "invite_valid",
                                                            "send_failed", "invite_lapsed", "no_invite")})
    digest_lines += _fixed_lines(noticetexts.DIGEST_DISPATCH_LINE, title=TITLE, answered=_pair_text(1, 2),
                                 correct=_pair_text(1, 2), partially_correct=_pair_text(0, 0),
                                 dont_know=_pair_text(1, 1))
    for text in texts.values():
        _contains_lines(text, digest_lines, "resumo diário")
    assert _events(w, cursor, *REPORT_EVENT_TYPES) == [
        _notice_ev(w, "daily_digest", t_due, dispatch=False, local_date=due_local.date().isoformat())]

    # 13. O que vale para o roteiro inteiro.
    _final_checks(w)


def _check_preview(w: _W) -> dict[str, list[ConsoleLine]]:
    """A prévia de cada membro com endereço ativo: um aviso
    `dispatch_preview` para a equipe, com, por membro e nesta ordem, o cabeçalho (texto `plain`), a questão
    (papel `question`) e o pedido do campo (papel `field_prompt`, campo 0), todos enviados com prioridade 2 e
    citáveis no console (`channel_ref` numérico); devolve as linhas do console por membro."""
    assert w.team is not None
    by_member: dict[str, list[ConsoleLine]] = {}
    notice_ids = set()
    for member in w.team.active:
        messages = _wait(lambda m=member: (found := team_messages(w.sql, "dispatch_preview", m))
                         and len(found) >= 3 and all(x.state == "sent" for x in found) and found,
                         f"a prévia de {member.label}")
        assert [(x.role, x.field_index, x.text_format, x.priority) for x in messages] == [
            (None, None, "plain", 2), ("question", None, None, 2), ("field_prompt", 0, None, 2)], messages
        assert messages[0].text is not None and messages[1].text is None and messages[2].text is None
        notice_ids |= {x.notice_id for x in messages}
        lines = [_line_of(w, x.message_id) for x in messages]
        assert [ln.recipient for ln in lines] == [member.recipient] * 3
        assert all(ln.channel_ref == str(ln.message_id) for ln in lines), "toda mensagem da prévia é citável"
        header, question, prompt = lines
        assert header.text == messages[0].text
        _contains_lines(header.text, [
            f"Prévia do disparo: {TITLE}", "Alunos-alvo: 4",
            "As mensagens a seguir mostram a questão como os alunos vão recebê-la; os botões não registram resposta.",
            f"Para confirmar o disparo: cq dispatches confirm {CODE} {w.dispatch_id}", NO_TEACHER_CONTACT_MESSAGE],
            "cabeçalho da prévia")
        assert STATEMENT in question.text and FIELD_HINT not in header.text
        assert prompt.text.startswith("Campo 1 de 1:"), prompt.text
        assert noticetexts.PREVIEW_FIELD_HINT.format(hint=FIELD_HINT) in prompt.text
        assert prompt.reply_request is None, "o pedido da prévia não tem a interface de resposta"
        for line in lines:
            assert ANSWER_SENTINEL not in line.text, "a prévia não traz o gabarito"
        by_member[member.label] = lines
    assert len(notice_ids) == 1, "um aviso de prévia para a equipe inteira"
    assert w.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0, "a prévia não cria entrega"
    assert w.sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
    return by_member


def _check_preview_reply(w: _W, header: ConsoleLine) -> None:
    """A resposta ("responder") ao cabeçalho da prévia → `preview_replied` e a `PREVIEW_REPLY` ao chat do
    membro, pela fila, com prioridade 2; nada mais muda."""
    member = _member(w, "membro-1")
    assert member.chat_id is not None
    before = _team_message_count(w)
    update = _say(w, member.chat_id, lambda: w.members["membro-1"].reply(header, REPLY_TEXT),
                  outcome="preview_replied")
    assert update["reply_to_message_id"] == header.message_id
    rows = _wait(lambda: (found := _all(w.sql, "SELECT id, text, state, priority, notice_id, delivery_id FROM "
                                       "outbox_messages WHERE reply_to_update_id = %s", (update["id"],)))
                 and all(r["state"] == "sent" for r in found) and found, "a resposta fixa da prévia")
    assert [{k: r[k] for k in ("text", "state", "priority", "notice_id", "delivery_id")} for r in rows] == [
        {"text": noticetexts.PREVIEW_REPLY, "state": "sent", "priority": 2, "notice_id": None, "delivery_id": None}]
    assert _line_of(w, int(rows[0]["id"])).text == noticetexts.PREVIEW_REPLY
    assert _team_message_count(w) == before, "a resposta à prévia não cria aviso"
    assert w.sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0


def _check_same_render(w: _W, preview: list[ConsoleLine]) -> None:
    """a questão da prévia é igual, em texto, à mensagem da questão do aluno A (a mesma
    renderização do estado inicial); o pedido do campo da prévia traz o texto do pedido do aluno e a dica.
    """
    a = w.pupils[0]
    _header, question, prompt = preview
    assert question.text == _question_line(w, a).text
    assert _prompt_line(w, a).text in prompt.text


def _final_checks(w: _W) -> None:
    """O console nunca editou; cada membro recebeu exatamente as mensagens dos 6 avisos (a prévia em 3
    partes e 5 avisos de texto), todas enviadas com prioridade 2 e sem o gabarito; os 6 avisos ficaram
    `queued` com 2 destinatários; toda tarefa terminou `done`, nenhuma com `NO_HANDLER_ERROR`; o motor falso
    sem desvio de protocolo."""
    assert w.team is not None
    lines = read_lines(w.stream.getvalue())
    assert [ln.raw for ln in lines if ln.edit_of is not None] == []
    kinds = ["dispatch_preview", "dispatch_preview", "dispatch_preview", "dispatch_opened", "question_failed",
             "dispatch_closed", "summary_resent", "daily_digest"]
    for member in w.team.active:
        messages = team_messages(w.sql, recipient=member)
        assert [m.kind for m in messages] == kinds, (member.label, [m.kind for m in messages])
        assert {(m.state, m.priority) for m in messages} == {("sent", 2)}
        assert all(ANSWER_SENTINEL not in ln.text for ln in team_lines(w.stream, member))
    notices = _all(w.sql, "SELECT kind, state, recipient_count FROM team_notices ORDER BY id")
    assert notices == [{"kind": k, "state": "queued", "recipient_count": 2}
                       for k in ("dispatch_preview", "dispatch_opened", "question_failed", "dispatch_closed",
                                 "summary_resent", "daily_digest")]
    assert w.sql.execute("SELECT count(*) FROM tasks WHERE last_error = %s", (NO_HANDLER_ERROR,)).fetchone()[0] == 0
    states = dict(w.sql.execute("SELECT state, count(*) FROM tasks GROUP BY state").fetchall())
    assert set(states) == {"done"}, f"tarefas fora de done: {states}"
    outbox = dict(w.sql.execute("SELECT state, count(*) FROM outbox_messages GROUP BY state").fetchall())
    assert set(outbox) <= {"sent", "cancelled"}, f"mensagens fora de sent/cancelled: {outbox}"
    assert w.engine_fake.protocol_errors() == []
