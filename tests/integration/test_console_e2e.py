"""Ponta a ponta pelo console, do oferecimento à resposta atrasada.


O roteiro automatizado de validação do sistema, só com dublês: **oferecimento → lista → vínculo → disparo →
resposta → fechamento → gabarito → atrasada**, com os estados e os eventos esperados em cada passo.

- **Oferecimento e bot** pela API (o bot na Bot API falsa, `bot_api`, que só serve ao registro
  e ao perfil). A tarefa `bot.sync_profile` do registro é processada **na preparação**, antes de o worker de
  console subir, pela fila real (`TaskQueue.work_once`) com o tratador de perfil dos bots
  (`bots.profile_task_handler`) sobre a `bot_api`: o worker de console não tem tratador
  de perfil, e nenhuma tarefa pode terminar com `NO_HANDLER_ERROR`.
- **Lista** pela importação (`students.import`); **convites** pelo envio em duas fases, com os
  e-mails mandados pelo próprio worker de console (`verification=` o adaptador SMTP real sobre o SMTP
  falso) e o código lido do link do e-mail (`smtp.invite_links()`).
- **Vínculo pelo console**: cada aluno manda `/start <código>` e `/aceito`
  pela `ConsoleInbox` (a entrada simulada, gravada por `reception.store_console_updates`: linha literal e
  tarefa na mesma transação, `update_id` negativo), e o vínculo do aluno cria a matrícula e o endereço sem mudança
  de código (o `channel` do endereço continua `telegram`: limitação conhecida). O termo sai numa linha do
  console sem campo do disparo e da resposta; a boas-vindas, na linha do aluno (pelo `recipient`).
- **Worker de console**: `run_worker(…, console_routes=True)` sem `bots`, com o `ConsoleChannel` num
  `StringIO` como canal das mensagens com rota, o motor falso estrito pelo adaptador HTTP real
  (`HttpQuestionEngine`) e a obtenção falsa (`repo_fetcher`). As capacidades do console (sem botões e sem
  edição) exercitam a degradação para canais sem botões: as alternativas saem listadas com as chaves, o "Não sei" vira o
  `/naosei` anunciado na mensagem, a múltipla degradada traz a dica `TYPED_MULTIPLE_HOWTO` logo
  depois das alternativas (e só ela: a escolha única degradada não a traz),
  e toda edição da entrega (feedback, progresso, "Não sei", revelação, atrasada) chega como **linha nova**
  em resposta à questão
  (papel `fallback`).
- **Disparos**: seis, imediatos com `closes_at` informado, um por questão do curso de teste (escolha única,
  escolha múltipla, número, inteiro, texto e a de várias partes, os presets do motor falso): preparar
  (`validating`), ver agendado com a prévia (`scheduled`, a prévia exata), confirmar (a marca), e cada aluno
  recebe a sua variante (seeds distintas, o snapshot guardado é o emitido pelo motor).
- **Respostas**, pela chave e pelo `channel_ref`: a escolha pela chave em resposta à questão
  (`choose`), a múltipla digitada (`ca`, `a` e `ac`: as chaves juntas, sem separador, e o texto literal
  vai ao motor sem nenhuma mudança, como o valor único de um formulário do PL),
  o número com vírgula em resposta ao pedido do campo,
  o inteiro, o texto, as partes da questão de várias partes (as digitáveis em resposta ao pedido de
  cada campo, as de escolha pela chave em resposta à questão) e o `/naosei` em resposta à questão, antes
  e no meio das partes. Cada entrada: a linha literal gravada, o desfecho, a resposta crua exata recebida
  pelo motor, a submissão como o motor devolveu, a entrega, a linha nova do feedback e o
  evento exato.
- **Fechamento** pelo relógio (`FakeClock` além de `closes_at`; a varredura fecha): `delivered` → `no_answer`;
  a revelação na mesma transação (`revealed_at`) só às entregas com resposta ou com "Não sei", como
  linha nova com "Janela fechada." e o gabarito permitido (a sentinela do motor falso, controle positivo;
  antes do fechamento, ela nunca aparece); nas `no_answer`, a linha de fechamento com "Janela fechada.", o
  aviso de como responder atrasado e o `/naosei` anunciado, sem gabarito, sem explicação e sem
  `revealed_at`; o `dispatch.closed {trigger: window, counts}` exato.
- **Atrasada**: a escolha pela chave numa entrega `no_answer` → `answered_late` com `late = true` e o
  gabarito revelado com o feedback, na mesma linha (`revealed_at` = o horário da correção).

Os textos esperados são os textos fixos do disparo e da resposta e os do vínculo do aluno, escritos aqui por extenso (não vêm do
código). Horários pelo `FakeClock`, que só anda com o sistema parado: cada fase acontece num instante fixo
(o preparo e a validação em `t_prep`, a confirmação, a abertura e a entrega em `t_conf`, as respostas em
`t_ans`), então os horários gravados e os dos eventos são exatos. Ids internos, `seq`, seeds, tokens,
`update_id` e pseudônimos nunca como literal: vêm do banco, do dublê ou da API.

Leituras [I] deste arquivo (inferências):
- o aviso `too_many_options` sai na prévia de toda questão com campo de escolha quando o canal com rota é o
  console (`max_buttons = 0`): sem botões (ou com mais alternativas + controles que
  `max_buttons`), a questão degrada: alternativas listadas com as chaves e resposta digitada, com o aviso
  `too_many_options` na prévia. A mensagem do aviso não tem texto fixado (é conferida não vazia),
  e a lista de `kind` é comparada como conjunto;
- o oferecimento fica no padrão (mensagens livres desligadas e sem contato do professor), então toda
  prévia traz também o aviso `no_teacher_contact`, com texto fixo;
- a mensagem da questão no console é a linha com `channel_ref` numérico e sem chave do disparo (a mensagem de
  entrega só com `answerable`); o pedido de campo leva `reply_request` e `reply_to`; toda edição, só
  `reply_to`;
- o pedido do campo `k` (base 0) de `n` começa por "Campo {k+1} de {n}:" (`FIELD_REQUEST`);
- a linha que o "Não sei" e a revelação de uma entrega com resposta ou com "Não sei" produzem não traz mais
  o `DONT_KNOW_HOWTO` (entrega sem os controles do "Não sei"); a linha de
  fechamento de uma `no_answer` o traz, porque o "Não sei" continua valendo depois do fechamento (o gabarito
  só vai a quem já respondeu; `DONT_KNOW_HOWTO`);
- nas partes do `submission.graded`, `ratio` e `residual` das escolhas são nulos, e os das demais partes são
  os gravados em `submissions.part_derived` (o valor numérico não é fixado pela
  documentação); `choices` são os `content_hash` das alternativas marcadas, na ordem das opções (na
  múltipla digitada, as chaves presentes no texto, como o `set(submitted_key)` do `pl-checkbox`);
- o eco de uma escolha no feedback (`FEEDBACK`: "Sua resposta: {echo} · {verdict} ·
  {score}") traz a chave e o texto de cada alternativa escolhida, e nenhuma não escolhida,
  também na escolha pela chave digitada; a forma não está fixada: conferem-se só a presença e a ausência
  entre "Sua resposta:" e o veredito;
- o resultado tem três faixas (✅ com nota ≥ 1, ◐ com 0 < nota < 1, ❌ com nota ≤ 0) e a nota "nota N%" sai em
  toda correção válida, com N = 100 (nota ≥ 1), ⌊nota × 100⌋ (0 < nota < 1) ou 0 (nota ≤ 0): "{verdict} ·
  {score}" (feedback de nota parcial; `SCORE`); na múltipla, cada alternativa
  escolhida do eco termina em " ✅" (entre as corretas) ou " ❌", logo depois do texto dela;
- `/start <código>` gera o desfecho `student_terms_presented`, e `/aceito`, `student_joined`.

No esqueleto, a preparação (oferecimento, bot, perfil, lista,
convites e e-mails pelo worker, que no esqueleto já trata `invite.email`) passa, e a função falha na
primeira entrada simulada: `ConsoleInbox.send_text` lança `NotImplementedError`.
Passa só com a implementação completa (a ponta a ponta usa
todas as partes do fluxo).
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.bots import profile_task_handler
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.tasks import NO_HANDLER_ERROR, TaskQueue
from cq_server.telegramrender import PlainFormatter
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_events,
    prepare_dispatch,
    prepare_invites,
    register_bot,
    send_invites,
)
from tests.helpers.clock import iso
from tests.helpers.console_student import PLAIN_KEYS, ConsoleLine, ConsoleStudent, read_lines
from tests.helpers.engine import ANSWER_SENTINEL

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

COURSE = "Programação de Computadores"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
CODE = "console-ponta-a-ponta"
BOT_USERNAME = "cq_console_ponta_bot"
WORKER_ID = "worker-console-ponta"
PREP_WORKER_ID = "preparacao-console-ponta"
REPO = "https://repos.cq.test/disciplina/questoes-ponta-console.git"
COMMIT = "5e1f" * 10
ORIGIN = "integration"
WINDOW = timedelta(hours=2)
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
WAIT = 20.0  # segundos reais de espera por uma condição
RESPONSE_SECONDS = 30  # de `t_conf` (entrega) a `t_ans` (respostas): `response_ms = 30000`
CHAT_BASE = 5_560_000_000  # chats sintéticos do console (fora dos `message_id` das linhas)
PROFILE = "bot.sync_profile"
QIDS = ("single_choice", "multiple_choice", "number", "integer", "text", "multipart")

# Os 13 tipos de evento do disparo e da resposta, como literais (o esqueleto não os tem).
DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                  "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
                  "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                  "submission.graded", "submission.internal_failure")

# Textos por extenso: o termo e as boas-vindas do vínculo do aluno, e os do disparo e da resposta.
TERMS_HEAD = f"Termo de uso do bot do oferecimento {COURSE} ({TERM}), versão 1"
TERMS_ACCEPT = "Para aceitar, toque em /aceito"
WELCOME = (f"Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {COURSE} ({TERM}). As "
           "questões da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação.")
DONT_KNOW_HOWTO = 'Para responder "Não sei", responda a esta mensagem com /naosei.'
TYPED_MULTIPLE_HOWTO = "Digite as letras das alternativas juntas, sem separador (ex.: ab)."
CLOSED_NOTICE = "Janela fechada."
LATE_HOWTO = 'Você ainda pode responder, fora do prazo: use os botões ou toque em "Responder" nesta mensagem.'
FEEDBACK_HEAD = "Sua resposta:"
RESULT_HEAD = "Resultado:"
DEADLINE_HEAD = "Prazo:"
CORRECT = "✅ correta"
PARTIAL = "◐ parcialmente correta"  # `VERDICT_PARTIAL`
INCORRECT = "❌ incorreta"
# O aviso da prévia de um oferecimento no padrão (mensagens livres
# desligadas e sem contato do professor), como este.
NO_TEACHER_CONTACT = "no_teacher_contact"
NO_TEACHER_CONTACT_MESSAGE = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                              "o aluno que escrever fora das questões não saberá a quem se dirigir.")

# As questões do curso de teste (tests/helpers/engine.py, presets), para as conferências de valor.
NUMBER = {"type": "number", "label": "Velocidade:", "suffix": "m/s", "hint": "Número; ex.: 4.25, -17 ou 3/7"}
INTEGER = {"type": "integer", "label": "Quantidade:", "suffix": None, "hint": "Número inteiro; ex.: 42 ou -17"}
TEXT = {"type": "text", "label": "Cor:", "suffix": None, "hint": "Texto"}
SINGLE = {"type": "single_choice", "label": None, "suffix": None, "hint": "Escolha uma alternativa"}
MULTIPLE = {"type": "multiple_choice", "label": None, "suffix": None, "hint": "Marque de 1 a 4 alternativas"}
COLORS = ("Vermelho", "Azul", "Verde", "Amarelo")
PRIMES = ("Dois", "Nove", "Sete", "Dez")


@dataclasses.dataclass(frozen=True)
class _Spec:
    title: str
    statement: str
    fields: tuple[dict, ...]
    options: tuple[str, ...]
    explanation: str | None = None


SPECS: dict[str, _Spec] = {
    "single_choice": _Spec("Cor do céu", "Qual é a cor do céu num dia limpo?", (SINGLE,), COLORS,
                           "O céu limpo é azul pelo espalhamento de Rayleigh."),
    "multiple_choice": _Spec("Números primos", "Marque os números primos.", (MULTIPLE,), PRIMES),
    "number": _Spec("Velocidade média", "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?", (NUMBER,),
                    ()),
    "integer": _Spec("Contagem", "Quantas unidades há em quatro dezenas e duas unidades?", (INTEGER,), ()),
    "text": _Spec("Cor em uma palavra", "Escreva, em uma palavra minúscula, a cor do céu num dia limpo.",
                  (TEXT,), ()),
    "multipart": _Spec("Várias partes", "Responda às cinco partes.", (NUMBER, INTEGER, TEXT, SINGLE, MULTIPLE),
                       COLORS + PRIMES, "Cada parte vale o mesmo peso."),
}
CHOICE_TYPES = frozenset({"single_choice", "multiple_choice"})
# `FEEDBACK`: "Sua resposta: {echo} · {verdict} · {score}" (três faixas: correta, parcial e errada).
_ECHO = re.compile(r"Sua resposta:\s*(?P<echo>.*?)\s*·\s*(?:✅|◐|❌)", re.S)
# O estado de cada entrega depois do fechamento (antes da atrasada), pelo roteiro das respostas.
AFTER_CLOSE: dict[str, dict[str, str]] = {
    "single_choice": {"A": "answered", "B": "answered", "C": "no_answer"},
    "multiple_choice": {"A": "answered", "B": "dont_know", "C": "answered"},
    "number": {"A": "answered", "B": "answered", "C": "no_answer"},
    "integer": {"A": "answered", "B": "answered", "C": "dont_know"},
    "text": {"A": "answered", "B": "answered", "C": "no_answer"},
    "multipart": {"A": "answered", "B": "dont_know", "C": "no_answer"},
}
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}


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
    """Três alunos neutros (fora de `ALL_PREFIXES`, `STUDENT_PREFIXES` e `DISPATCH_PREFIXES`)."""
    return [_Pupil(key=key, name=f"Pessoa Console Ponta {n:02d}", ra=f"59100004{n:02d}",
                   email=f"console.ponta{n:02d}@turma.example.org", chat=CHAT_BASE + n)
            for n, key in enumerate("ABC", start=1)]


@dataclasses.dataclass
class _W:
    client: Any
    sql: psycopg.Connection
    clock: Any
    engine_fake: Any
    repo_fetcher: Any
    stream: io.StringIO
    offering: dict
    offering_row: int
    bot_row: int
    pupils: list[_Pupil]
    closes: datetime | None = None
    dispatches: dict[str, dict] = dataclasses.field(default_factory=dict)


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


def _ev(type_: str, at: datetime, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _last_seq(w: _W) -> int:
    return w.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events(w: _W, cursor: int, *types: str) -> list[dict]:
    """Os eventos dos tipos depois de `cursor`, sem o `seq` (conferido crescente e sem repetição)."""
    items = list_events(w.client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _same(got: list[dict], expected: list[dict]) -> None:
    """Mesmo multiconjunto de eventos (a ordem entre disparos não é fixada; a de cada caso é conferida à
    parte)."""
    assert sorted(map(_canon, got)) == sorted(map(_canon, expected))


def _counts(**values: int) -> dict[str, int]:
    base = {k: 0 for k in ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                           "answered_late", "question_failed")}
    base.update(values)
    return base


# --- preparação: perfil do bot, lista, convites --------------------------------------------------------


def _process_profile_tasks(db_url: str, clock: Any, settings: Any, bots: Any) -> list[tuple[str, str]]:
    """A tarefa `bot.sync_profile` do registro (e da ativação), processada pela fila com o tratador de
    perfil dos bots sobre a Bot API falsa, antes de o worker de console subir."""
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


def _mail_codes(smtp: Any, pupils: list[_Pupil]) -> dict[str, str]:
    """O código do convite de cada aluno, do link do e-mail que o SMTP falso recebeu (um por aluno)."""
    codes: dict[str, str] = {}
    for pupil in pupils:
        links = [link for link in smtp.invite_links() if link.rcpt_to == (pupil.email,)]
        assert len(links) == 1, f"e-mails de convite para {pupil.key}: {len(links)}"
        assert links[0].bot_username == BOT_USERNAME
        codes[pupil.key] = links[0].code
    return codes


# --- entrada simulada e leitura das linhas -------------------------------------------------------------


_UPDATE_COLUMNS = ("kind", "chat_id", "chat_type", "sender_id", "text", "command", "reply_to_message_id",
                   "received_at", "state", "outcome", "processed_at")


def _max_update(w: _W) -> int:
    return w.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates").fetchone()[0]


def _say(w: _W, pupil: _Pupil, act: Callable[[], None], *, text: str, outcome: str,
         reply_to: ConsoleLine | None = None, command: str | None = None) -> dict:
    """Uma entrada simulada do aluno: espera a linha literal gravada e processada e a
    confere por inteiro; devolve a linha de `telegram_updates`."""
    mark = _max_update(w)
    act()

    def done() -> list[dict] | None:
        rows = _all(w.sql, "SELECT * FROM telegram_updates WHERE id > %s AND bot_id = %s AND chat_id = %s "
                    "ORDER BY id", (mark, w.bot_row, pupil.chat))
        return rows if rows and all(r["state"] == "processed" for r in rows) else None

    rows = _wait(done, f"a entrada {text!r} de {pupil.key} processada")
    assert len(rows) == 1, f"a entrada de {pupil.key} gravou {len(rows)} linhas"
    row = rows[0]
    assert row["update_id"] < 0, "entrada do console com update_id não negativo"
    now = w.clock.now()
    assert {k: row[k] for k in _UPDATE_COLUMNS} == {
        "kind": "message", "chat_id": pupil.chat, "chat_type": "private", "sender_id": pupil.chat, "text": text,
        "command": command, "reply_to_message_id": None if reply_to is None else reply_to.message_id,
        "received_at": now, "state": "processed", "outcome": outcome, "processed_at": now}
    return row


def _line_of(pupil: _Pupil, message_id: int) -> ConsoleLine:
    found = [line for line in pupil.io.lines() if line.message_id == message_id]
    assert len(found) == 1, f"linhas do console de {pupil.key} com a mensagem {message_id}: {len(found)}"
    return found[0]


def _no_chat_in_console(w: _W) -> None:
    """A linha do console nunca traz o `chat_id` nem uma chave `chat` (a identidade fica fora das saídas)."""
    output = w.stream.getvalue()
    for pupil in w.pupils:
        assert str(pupil.chat) not in output, f"chat de {pupil.key} numa linha do console"
    for line in read_lines(output):
        assert not any("chat" in key for key in line.raw), line.raw


# --- disparos, entregas e mensagens -------------------------------------------------------------------


def _dispatch_body(w: _W, qid: str, prepared: dict, *, created: datetime, **changes: Any) -> dict:
    """O `Dispatch` completo esperado, a partir do estado `validating`."""
    body = {
        "offering": {"offering_id": w.offering["offering_id"], "code": CODE},
        "timezone": ZONE,
        "dispatch_id": prepared["dispatch_id"],
        "question": {"question_ref_id": prepared["question"]["question_ref_id"], "repo": REPO, "commit": COMMIT,
                     "qid": qid},
        "state": "validating", "confirmed": False, "origin": ORIGIN,
        "opens_at": None, "closes_at": iso(w.closes), "created_at": iso(created), "validated_at": None,
        "confirmed_at": None, "rejected_at": None, "opened_at": None, "closed_at": None, "cancelled_at": None,
        "rejection": None, "cancel_reason": None, "target_count": None, "deliveries": dict(ZERO_COUNTS),
        "preview": None,
    }
    body.update(changes)
    return body


def _dispatch_row(w: _W, qid: str) -> dict:
    row = _one(w.sql, "SELECT id, state, closed_at FROM dispatches WHERE dispatch_uuid = %s::uuid",
               (w.dispatches[qid]["dispatch_id"],))
    assert row is not None
    return row


_DELIVERY_SQL = """
    SELECT d.id, d.delivery_uuid::text AS delivery_uuid, d.state, d.undelivered_reason, d.failure,
           d.current_field, d.seed, d.variant_at, d.sent_at, d.answered_at, d.revealed_at,
           d.principal_message_id, c.snapshot, c.ir, c.parts
      FROM deliveries d
      JOIN dispatches x ON x.id = d.dispatch_id
      JOIN enrollments e ON e.id = d.enrollment_id
      LEFT JOIN delivery_contents c ON c.delivery_id = d.id
     WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s
"""


def _delivery(w: _W, qid: str, pupil: _Pupil) -> dict:
    row = _one(w.sql, _DELIVERY_SQL, (w.dispatches[qid]["dispatch_id"], pupil.person_row))
    assert row is not None, f"sem entrega de {pupil.key} em {qid}"
    return row


def _submissions(w: _W, delivery: dict) -> list[dict]:
    return _all(w.sql, "SELECT *, submission_uuid::text AS submission_id FROM submissions WHERE delivery_id = %s "
                "ORDER BY id", (delivery["id"],))


def _messages(w: _W, delivery: dict, after: int = 0) -> list[dict]:
    """As mensagens da fila da entrega (id > `after`), em ordem de `id`."""
    return _all(w.sql, "SELECT id, delivery_role, operation, state, channel_ref, field_index, text "
                "FROM outbox_messages WHERE delivery_id = %s AND id > %s ORDER BY id", (delivery["id"], after))


def _max_message(w: _W) -> int:
    return w.sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _question_line(w: _W, qid: str, pupil: _Pupil) -> ConsoleLine:
    """A mensagem da questão (a principal) da entrega, como linha do console: `channel_ref` numérico e
    só as chaves de base."""
    delivery = _delivery(w, qid, pupil)
    principal = delivery["principal_message_id"]
    assert principal is not None, f"entrega de {pupil.key} em {qid} sem mensagem principal"
    line = _line_of(pupil, principal)
    assert (line.channel_ref, set(line.raw)) == (str(principal), set(PLAIN_KEYS)), line.raw
    return line


def _prompt_line(w: _W, qid: str, pupil: _Pupil, index: int) -> ConsoleLine:
    """O pedido do campo `index` (papel `field_prompt`, enviado), como linha do console: em resposta à
    questão, com a dica no `reply_request`."""
    delivery = _delivery(w, qid, pupil)

    def sent() -> list[dict]:
        return [m for m in _messages(w, delivery) if m["delivery_role"] == "field_prompt"
                and m["field_index"] == index and m["state"] == "sent"]

    rows = _wait(sent, f"o pedido do campo {index} de {pupil.key} em {qid}")
    assert len(rows) == 1, f"pedidos enviados do campo {index}: {len(rows)}"
    line = _line_of(pupil, rows[0]["id"])
    question = _question_line(w, qid, pupil)
    fields = SPECS[qid].fields
    field = fields[index]
    assert str(line.reply_to) == question.channel_ref
    assert line.reply_request is not None and field["hint"] in json.dumps(line.reply_request, ensure_ascii=False)
    assert line.text.startswith(f"Campo {index + 1} de {len(fields)}:"), line.text
    if field["label"] is not None:
        assert field["label"] in line.text
    return line


def _new_fallbacks(w: _W, qid: str, pupil: _Pupil, after: int, *, exactly: int | None = 1) -> list[ConsoleLine]:
    """As mensagens novas da entrega depois de `after`, todas `fallback` enviadas por `send` (o console não
    edita), como linhas em resposta à questão."""
    delivery = _delivery(w, qid, pupil)
    question = _question_line(w, qid, pupil)

    def ready() -> list[dict] | None:
        rows = [m for m in _messages(w, delivery, after) if m["delivery_role"] == "fallback"]
        if not rows or any(m["state"] != "sent" for m in rows):
            return None
        return rows if exactly is None or len(rows) >= exactly else None

    rows = _wait(ready, f"a linha nova de {pupil.key} em {qid}")
    if exactly is not None:
        assert len(rows) == exactly, f"linhas novas de {pupil.key} em {qid}: {len(rows)}"
    lines = []
    for row in rows:
        assert (row["operation"], row["text"]) == ("send", None), row
        line = _line_of(pupil, row["id"])
        assert str(line.reply_to) == question.channel_ref, line.raw
        assert line.edit_of is None
        lines.append(line)
    return lines


# --- respostas -------------------------------------------------------------------------------------------


def _option_hash(delivery: dict, index: int, key: str) -> str:
    options = delivery["ir"]["fields"][index]["options"]
    found = [o["content_hash"] for o in options if o["key"] == key]
    assert len(found) == 1
    return found[0]


def _derived(stored: Any, index: int) -> dict:
    """A parte `index` de `submissions.part_derived`, indexada pelo índice do campo."""
    item = stored[index] if isinstance(stored, list) else stored[str(index)]
    assert isinstance(item, dict)
    return item


def _parts(delivery: dict, submission: dict, raw: dict, response: dict) -> list[dict]:
    """As partes do `submission.graded`: por índice, a nota da parte como o motor devolveu,
    os `content_hash` das alternativas marcadas (na ordem das opções) e os derivados gravados."""
    parts = []
    for index, field in enumerate(delivery["ir"]["fields"]):
        name = field["name"]
        score = response["partial_scores"][name]["score"]
        if field["type"] in CHOICE_TYPES:
            value = raw.get(name)
            if value is None:
                chosen = []
            elif isinstance(value, str):
                # a múltipla digitada vai literal, e o `pl-checkbox` a lê como `set(submitted_key)`.
                chosen = list(value) if field["type"] == "multiple_choice" else [value]
            else:
                chosen = list(value)
            keys = [o["key"] for o in field["options"]]
            choices = [_option_hash(delivery, index, k) for k in keys if k in chosen]
            ratio = residual = None
        else:
            derived = _derived(submission["part_derived"], index)
            choices, ratio, residual = [], derived.get("ratio"), derived.get("residual")
        parts.append({"index": index, "score": score, "choices": choices, "ratio": ratio, "residual": residual})
    return parts


def _assert_choice_echo(text: str | None, options: Mapping[str, str], chosen: Iterable[str]) -> None:
    """o eco do feedback (entre "Sua resposta:" e o veredito) traz a chave (símbolo
    isolado) e o texto de cada alternativa escolhida, e nem a chave nem o texto de uma não escolhida; sem
    fixar ordem, separador nem pontuação. O enunciado (com todas as alternativas) e o gabarito revelado
    ficam fora da conferência."""
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


def _verdict(score: float) -> str:
    """As três faixas do resultado: ✅ com nota ≥ 1, ◐ com 0 < nota < 1, ❌ com
    nota ≤ 0 (as do `determine_score_params` do PL fixado)."""
    return CORRECT if score >= 1 else PARTIAL if score > 0 else INCORRECT


def _score_text(score: float) -> str:
    """A nota mostrada ao aluno (`SCORE`): "nota N%", com N = 100 (nota
    ≥ 1), ⌊nota × 100⌋ (0 < nota < 1) ou 0 (nota ≤ 0), o número do selo do PL."""
    return f"nota {100 if score >= 1 else math.floor(score * 100) if score > 0 else 0}%"


def _assert_marks(text: str, options: Mapping[str, str], marks: Mapping[str, bool]) -> None:
    """Marcas por alternativa (`FEEDBACK`): na múltipla, cada alternativa escolhida do eco
    termina em " ✅" (chave entre as corretas) ou " ❌", logo depois do texto dela; procurada só no eco (entre
    "Sua resposta:" e o veredito), sem atravessar a vírgula nem a linha (o enunciado lista as alternativas)."""
    match = _ECHO.search(text or "")
    assert match, f"feedback sem 'Sua resposta: {{eco}} · {{veredito}}': {text!r}"
    echo = match.group("echo")
    for key, right in marks.items():
        mark = "✅" if right else "❌"
        pattern = rf"\({re.escape(key)}\) {re.escape(options[key])}[^,·\n]*? {mark}"
        assert re.search(pattern, echo), f"sem a marca {mark} da alternativa ({key}) no eco: {echo!r}"


def _grade_calls(w: _W, delivery: dict) -> list[Any]:
    """As correções pedidas ao motor falso para a variante da entrega (pelo snapshot)."""
    return [c for c in w.engine_fake.calls("grade")
            if c.request is not None and c.request["snapshot"] == delivery["snapshot"]
            and c.request["question_ref"]["qid"] == delivery["ir_qid"]]


def _graded(w: _W, qid: str, pupil: _Pupil, update: dict, *, raw: dict, literal: dict, normalized: list[str],
            late: bool, at: datetime, response_ms: int, echo: str | None, after: int,
            reveal: bool = False, chosen: tuple[str, ...] | None = None,
            marks: Mapping[str, bool] | None = None) -> dict:
    """A correção válida de uma resposta completa: a resposta crua exata no motor, a
    entrada literal guardada por campo (`raw_input`), a submissão como o motor devolveu, a
    entrega, a linha nova com o feedback (o resultado em três faixas com a nota; as marcas por
    alternativa da múltipla, `marks`) e o evento exato."""
    delivery = {**_delivery(w, qid, pupil), "ir_qid": qid}
    subs = _wait(lambda: (s := _submissions(w, delivery)) and s[-1]["outcome"] == "graded" and s,
                 f"a correção de {pupil.key} em {qid}")
    submission = subs[-1]
    calls = _grade_calls(w, delivery)
    assert len(calls) == 1, f"correções no motor para {pupil.key} em {qid}: {len(calls)}"
    call = calls[0]
    assert call.request["raw_submitted_answers"] == raw
    response = call.response
    assert submission["valid"] is True and response["valid"] is True
    assert (submission["score"], submission["partial_scores"], submission["submitted_answers"],
            submission["format_errors"], submission["feedback"], submission["params"],
            submission["correct_answers"]) == (
        response["score"], response["partial_scores"], response["submitted_answers"], response["format_errors"],
        response["feedback"], response["params"], response["correct_answers"])
    assert (submission["outcome"], submission["refusal"], submission["late"], list(submission["normalized_fields"]),
            submission["raw_input"], submission["received_at"], submission["response_ms"],
            submission["telegram_update_id"]) == (
        "graded", None, late, normalized, literal, at, response_ms, update["id"])
    state = "answered_late" if late else "answered"
    now_delivery = _delivery(w, qid, pupil)
    assert (now_delivery["state"], now_delivery["answered_at"], now_delivery["current_field"]) == (state, at, None)
    # O resultado em três faixas e a nota em toda correção válida, "{verdict} ·
    # {score}" (`FEEDBACK`).
    verdict = f"{_verdict(response['score'])} · {_score_text(response['score'])}"
    [line] = _new_fallbacks(w, qid, pupil, after)
    head = RESULT_HEAD if len(SPECS[qid].fields) > 1 else FEEDBACK_HEAD
    assert head in line.text and verdict in line.text, (verdict, line.text)
    if echo is not None:
        assert echo in line.text, line.text
    if chosen is not None:
        _assert_choice_echo(line.text, dict(zip("abcd", SPECS[qid].options)), chosen)
    if marks is not None:
        _assert_marks(line.text, dict(zip("abcd", SPECS[qid].options)), marks)
    assert DONT_KNOW_HOWTO not in line.text
    if reveal:
        assert ANSWER_SENTINEL in line.text, "a atrasada mostra o gabarito, revelado com o feedback"
    else:
        assert ANSWER_SENTINEL not in line.text
    event = _ev("submission.graded", at, offering_id=w.offering["offering_id"],
                dispatch_id=w.dispatches[qid]["dispatch_id"], delivery_id=delivery["delivery_uuid"],
                submission_id=submission["submission_id"],
                question_ref_id=w.dispatches[qid]["question"]["question_ref_id"], person=pupil.pseudo, valid=True,
                late=late, normalized=bool(normalized), score=response["score"],
                parts=_parts(delivery, submission, raw, response))
    return event


def _dont_know(w: _W, qid: str, pupil: _Pupil, update: dict, *, parts: dict, at: datetime, after: int) -> dict:
    """O "Não sei" pelo `/naosei`: submissão `dont_know` com as partes já preenchidas, sem motor;
    entrega `dont_know`; linha nova sem controles; `delivery.dont_know` exato."""
    delivery = {**_delivery(w, qid, pupil), "ir_qid": qid}
    subs = _submissions(w, delivery)
    assert [s["outcome"] for s in subs] == ["dont_know"]
    sub = subs[0]
    assert (sub["valid"], sub["score"], sub["refusal"], sub["raw_input"], sub["received_at"],
            sub["telegram_update_id"]) == (None, None, None, parts, at, update["id"])
    assert _grade_calls(w, delivery) == []
    now_delivery = _delivery(w, qid, pupil)
    assert (now_delivery["state"], now_delivery["answered_at"], now_delivery["current_field"]) == (
        "dont_know", at, None)
    [line] = _new_fallbacks(w, qid, pupil, after)
    assert DONT_KNOW_HOWTO not in line.text and ANSWER_SENTINEL not in line.text
    return _ev("delivery.dont_know", at, offering_id=w.offering["offering_id"],
               dispatch_id=w.dispatches[qid]["dispatch_id"], delivery_id=delivery["delivery_uuid"],
               submission_id=sub["submission_id"], person=pupil.pseudo)


def _part(w: _W, qid: str, pupil: _Pupil, *, parts: dict, current: int, after: int) -> None:
    """Uma parte gravada (`answer_part_recorded`): sem submissão, a entrega `delivered` com o campo corrente
    seguinte e as partes literais; o progresso chega como linha nova."""
    delivery = _delivery(w, qid, pupil)
    assert _submissions(w, delivery) == []
    assert (delivery["state"], delivery["current_field"], delivery["parts"]) == ("delivered", current, parts)
    _new_fallbacks(w, qid, pupil, after, exactly=None)


# --- o roteiro -------------------------------------------------------------------------------------------


def test_console_end_to_end_from_offering_to_late_answer(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, db_url: str, bot_api: Any, bots: Any,
        smtp: Any, mail_channel: Any, engine_fake: Any, repo_fetcher: Any, worker_thread: Any) -> None:
    """Ponta a ponta pelo console.

    oferecimento → lista → vínculo (código do SMTP falso, `/start` e `/aceito` pela `ConsoleInbox`) →
    disparo (preparar, ver agendado com a prévia, confirmar) → cada aluno recebe a sua variante → escolha
    única, múltipla, número com vírgula, inteiro, texto e várias partes (pela chave e pelo `channel_ref`) →
    `/naosei` → feedback como linhas novas (`can_edit = False`) → fechamento → gabarito permitido →
    atrasada, com os estados e os eventos esperados em cada passo; a `bot.sync_profile` processada antes do
    worker de console; nenhuma tarefa com `NO_HANDLER_ERROR`."""
    pupils = _pupils()
    t0 = clock.now()

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
    assert sql.execute("SELECT task_type, state FROM tasks ORDER BY id").fetchall() == processed
    assert sql.execute("SELECT profile_state FROM bots WHERE id = %s", (bot_row,)).fetchone()[0] == "configured"

    # 2. Lista e convites em duas fases; os e-mails saem pelo worker de console.
    body = import_students(client, ADMIN_TOKEN, CODE, [p.row() for p in pupils])
    assert body == {"offering": {"offering_id": offering["offering_id"], "code": CODE}, "received": 3,
                    "people_created": 3, "people_reused": 0, "enrollments_created": 3, "already_enrolled": 0}
    for pupil in pupils:
        pupil.person_row, pupil.pseudo = sql.execute(
            "SELECT p.id, p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
            (pupil.ra,)).fetchone()
    prepared_invites = prepare_invites(client, ADMIN_TOKEN, CODE)
    assert prepared_invites["eligible"] == 3
    assert send_invites(client, ADMIN_TOKEN, CODE, prepared_invites["confirmation_code"])["queued"] == 3

    stream = io.StringIO()
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    inbox = ConsoleInbox(db_url, clock=clock)
    w = _W(client=client, sql=sql, clock=clock, engine_fake=engine_fake, repo_fetcher=repo_fetcher, stream=stream,
           offering=offering, offering_row=offering_row, bot_row=bot_row, pupils=pupils)
    try:
        worker_thread(channel=ConsoleChannel(stream), console_routes=True, verification=mail_channel,
                      engine=engine, fetcher=repo_fetcher, delivery=DeliverySettings(),
                      settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(lambda: len(smtp.messages()) == 3 and sql.execute(
            "SELECT count(*) FROM invite_sends WHERE state = 'sent'").fetchone()[0] == 3, "os três convites")
        codes = _mail_codes(smtp, pupils)
        for pupil in pupils:
            pupil.console = ConsoleStudent(inbox, bot_id=bot_row, chat_id=pupil.chat, recipient=pupil.pseudo,
                                           source=stream)
        _run(w, codes, t0)
    finally:
        worker_thread.stop()
        inbox.close()
        with contextlib.suppress(NotImplementedError):
            engine.close()


def _run(w: _W, codes: dict[str, str], t0: datetime) -> None:
    """O roteiro depois da preparação (ver a docstring do teste)."""
    offering_id = w.offering["offering_id"]
    a, b, c = w.pupils

    # 3. Vínculo pelo console: `/start <código>` → termo; `/aceito` → matrícula, endereço e boas-vindas.
    t_link = w.clock.advance(60)
    cursor = _last_seq(w)
    for number, pupil in enumerate(w.pupils, start=1):
        text = f"/start {codes[pupil.key]}"
        _say(w, pupil, lambda p=pupil, t=text: p.io.say(t), text=text, command="/start",
             outcome="student_terms_presented")
        terms = _wait(lambda p=pupil, n=number: (found := [ln for ln in p.io.all_lines() if TERMS_HEAD in ln.text])
                      and len(found) == n and found, f"o termo de {pupil.key}")
        assert TERMS_ACCEPT in terms[-1].text and set(terms[-1].raw) == set(PLAIN_KEYS)
        assert terms[-1].channel_ref.startswith("console-")
        _say(w, pupil, lambda p=pupil: p.io.say("/aceito"), text="/aceito", command="/aceito",
             outcome="student_joined")
        welcome = pupil.io.wait_for_line(lambda ln: ln.text == WELCOME, new=False, timeout=WAIT)
        assert set(welcome.raw) == set(PLAIN_KEYS)
    linked = _all(w.sql, """
        SELECT p.pseudo_id::text AS pseudo, e.role, e.state, e.terms_version, e.terms_accepted_at, d.channel,
               d.chat_id, d.state AS endpoint_state
          FROM enrollments e JOIN people p ON p.id = e.person_id
          JOIN delivery_endpoints d ON d.person_id = p.id AND d.bot_id = %s
         WHERE e.offering_id = %s ORDER BY p.id""", (w.bot_row, w.offering_row))
    assert linked == [{"pseudo": p.pseudo, "role": "student", "state": "active", "terms_version": 1,
                       "terms_accepted_at": t_link, "channel": "telegram", "chat_id": p.chat,
                       "endpoint_state": "active"} for p in sorted(w.pupils, key=lambda p: p.person_row)]
    assert _events(w, cursor, "student.joined") == [
        _ev("student.joined", t_link, offering_id=offering_id, person=p.pseudo, terms_version=1,
            endpoint_replaced=False) for p in w.pupils]
    _settle(w)

    # 4. Preparo dos seis disparos (imediatos, com `closes_at`) e a validação pelo worker.
    w.engine_fake.program_presets()
    w.repo_fetcher.add(REPO, COMMIT)
    t_prep = w.clock.advance(60)
    w.closes = t_prep + WINDOW
    cursor = _last_seq(w)
    for qid in QIDS:
        prepared = prepare_dispatch(w.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid=qid,
                                    closes_at=iso(w.closes), origin=ORIGIN)
        assert prepared == _dispatch_body(w, qid, prepared, created=t_prep)
        w.dispatches[qid] = prepared
    _wait(lambda: all(_dispatch_row(w, qid)["state"] != "validating" for qid in QIDS), "as seis validações")
    _settle(w)
    shown: dict[str, dict] = {}
    for qid in QIDS:
        body = get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatches[qid]["dispatch_id"])
        [sample] = w.engine_fake.issued_variants(qid)
        spec = SPECS[qid]
        # o oferecimento está no padrão (mensagens livres desligadas, sem contato): `no_teacher_contact` em
        # toda prévia
        kinds = sorted({NO_TEACHER_CONTACT, *(["too_many_options"]
                                              if any(f["type"] in CHOICE_TYPES for f in spec.fields) else [])})
        preview = body["preview"]
        assert preview is not None, f"{qid}: sem prévia ({body['state']}, {body['rejection']})"
        warnings = preview["warnings"]
        assert sorted({wn["kind"] for wn in warnings}) == kinds, (qid, warnings)
        for warning in warnings:
            assert isinstance(warning["message"], str) and warning["message"].strip()
            assert ANSWER_SENTINEL not in warning["message"]
        assert [wn["message"] for wn in warnings if wn["kind"] == NO_TEACHER_CONTACT] == [
            NO_TEACHER_CONTACT_MESSAGE], (qid, warnings)
        statement = PlainFormatter().plain(sample.ir["statement"]["html"])
        assert spec.statement in statement and "<" not in statement
        assert body == _dispatch_body(
            w, qid, w.dispatches[qid], created=t_prep, state="scheduled", validated_at=iso(t_prep), target_count=3,
            preview={"title": spec.title, "statement": statement, "fields": [dict(f) for f in spec.fields],
                     "target_count": 3, "warnings": warnings})
        assert ANSWER_SENTINEL not in json.dumps(body, ensure_ascii=False)
        shown[qid] = body
    fetches = w.repo_fetcher.calls(REPO, COMMIT)
    assert fetches and all(f.outcome == "Fetched" for f in fetches)
    assert [f.reused for f in fetches].count(False) == 1, "a árvore é copiada uma vez e reusada depois"
    expected = []
    for qid in QIDS:
        body = shown[qid]
        expected += [
            _ev("dispatch.prepared", t_prep, "bootstrap", offering_id=offering_id, dispatch_id=body["dispatch_id"],
                question_ref_id=body["question"]["question_ref_id"], origin=ORIGIN),
            _ev("dispatch.validated", t_prep, offering_id=offering_id, dispatch_id=body["dispatch_id"],
                target_count=3, warnings=sorted({wn["kind"] for wn in body["preview"]["warnings"]}))]
    got = _events(w, cursor, *DISPATCH_EVENT_TYPES)
    for event in got:
        if event["type"] == "dispatch.validated":
            event["data"]["warnings"] = sorted(set(event["data"]["warnings"]))
    _same(got, expected)
    for qid in QIDS:
        mine = [e["type"] for e in got if e["data"]["dispatch_id"] == shown[qid]["dispatch_id"]]
        assert mine == ["dispatch.prepared", "dispatch.validated"], (qid, mine)

    # 5. Confirmação (a marca) e a abertura: cada aluno recebe a sua variante.
    t_conf = w.clock.advance(60)
    cursor = _last_seq(w)
    for qid in QIDS:
        body = confirm_dispatch(w.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "confirmed": True, "confirmed_at": iso(t_conf), "opens_at": iso(t_conf)}

    def opened() -> bool:
        states = [s for (s,) in w.sql.execute(
            "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id WHERE x.offering_id = %s",
            (w.offering_row,)).fetchall()]
        queued = w.sql.execute("SELECT count(*) FROM outbox_messages WHERE delivery_id IS NOT NULL AND state IN "
                               "('pending', 'leased')").fetchone()[0]
        return len(states) == 18 and set(states) == {"delivered"} and queued == 0

    _wait(opened, "as dezoito entregas")
    _settle(w)
    expected = []
    variant_calls = [c for c in w.engine_fake.calls("create_variant") if c.outcome == "ok"]
    for qid in QIDS:
        body = get_dispatch(w.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "state": "open", "confirmed": True, "confirmed_at": iso(t_conf),
                        "opens_at": iso(t_conf), "opened_at": iso(t_conf),
                        "deliveries": {**ZERO_COUNTS, "delivered": 3}}
        dispatch_id = body["dispatch_id"]
        expected += [
            _ev("dispatch.confirmed", t_conf, "bootstrap", offering_id=offering_id, dispatch_id=dispatch_id,
                target_count=3, opens_at=iso(t_conf), closes_at=iso(w.closes)),
            _ev("dispatch.opened", t_conf, offering_id=offering_id, dispatch_id=dispatch_id, target_count=3)]
        seeds = []
        for pupil in w.pupils:
            delivery = _delivery(w, qid, pupil)
            assert {k: delivery[k] for k in ("state", "undelivered_reason", "failure", "current_field", "variant_at",
                                             "sent_at", "answered_at", "revealed_at")} == {
                "state": "delivered", "undelivered_reason": None, "failure": None, "current_field": 0,
                "variant_at": t_conf, "sent_at": t_conf, "answered_at": None, "revealed_at": None}
            seeds.append(delivery["seed"])
            issued = [v for v in w.engine_fake.issued_variants(qid) if int(v.seed, 36) == delivery["seed"]]
            assert len(issued) == 1, f"a variante de {pupil.key} em {qid} não foi a emitida pela seed"
            assert delivery["snapshot"] == issued[0].snapshot
            assert [c for c in variant_calls if c.request["question_ref"]["qid"] == qid
                    and int(c.request["seed"], 36) == delivery["seed"]], "create_variant com a seed da entrega"
            expected.append(_ev("delivery.delivered", t_conf, offering_id=offering_id, dispatch_id=dispatch_id,
                                delivery_id=delivery["delivery_uuid"], person=pupil.pseudo))
            _check_question(w, qid, pupil)
        assert len(set(seeds)) == 3, f"seeds repetidas em {qid}"
        shown[qid] = body
    _same(_events(w, cursor, *DISPATCH_EVENT_TYPES), expected)
    assert ANSWER_SENTINEL not in w.stream.getvalue(), "o gabarito chegou ao aluno antes do fechamento"

    # 6. Respostas pelo console, uma por vez, todas em `t_ans`.
    t_ans = w.clock.advance(RESPONSE_SECONDS)
    ms = RESPONSE_SECONDS * 1000
    cursor = _last_seq(w)
    expected = []

    def answer(pupil: _Pupil, qid: str, act: Callable[[], None], *, text: str, target: ConsoleLine,
               outcome: str, command: str | None = None) -> tuple[dict, int]:
        mark = _max_message(w)
        update = _say(w, pupil, act, text=text, outcome=outcome, reply_to=target, command=command)
        return update, mark

    def graded(pupil: _Pupil, qid: str, act: Callable[[], None], *, text: str, target: ConsoleLine,
               raw: dict, literal: dict | None = None, normalized: list[str] | None = None,
               echo: str | None = None, chosen: tuple[str, ...] | None = None,
               marks: Mapping[str, bool] | None = None) -> None:
        """`literal` é a entrada literal por campo (`raw_input`); sem ele, o próprio texto no único campo.
        `chosen`: as alternativas que o eco de uma escolha traz (e só elas); `marks`: a marca esperada de
        cada alternativa escolhida da múltipla (✅ certa, ❌ errada)."""
        update, mark = answer(pupil, qid, act, text=text, target=target, outcome="answer_recorded")
        expected.append(_graded(w, qid, pupil, update, raw=raw, literal=literal or {"resposta": text},
                                normalized=normalized or [], late=False, at=t_ans, response_ms=ms, echo=echo,
                                after=mark, chosen=chosen, marks=marks))

    def dont_know(pupil: _Pupil, qid: str, *, parts: dict) -> None:
        q = _question_line(w, qid, pupil)
        update, mark = answer(pupil, qid, lambda: pupil.io.dont_know(q), text="/naosei", target=q,
                              outcome="answer_dont_know", command="/naosei")
        expected.append(_dont_know(w, qid, pupil, update, parts=parts, at=t_ans, after=mark))

    def part(pupil: _Pupil, qid: str, act: Callable[[], None], *, text: str, target: ConsoleLine, parts: dict,
             current: int) -> None:
        _update, mark = answer(pupil, qid, act, text=text, target=target, outcome="answer_part_recorded")
        _part(w, qid, pupil, parts=parts, current=current, after=mark)

    # escolha única pela chave, em resposta à questão
    q = _question_line(w, "single_choice", a)
    graded(a, "single_choice", lambda: a.io.choose(q, "b"), text="b", target=q, raw={"resposta": "b"},
           chosen=("b",))
    q = _question_line(w, "single_choice", b)
    graded(b, "single_choice", lambda: b.io.choose(q, "a"), text="a", target=q, raw={"resposta": "a"},
           chosen=("a",))
    # escolha múltipla digitada: as chaves juntas, sem separador; o texto literal
    # vai ao motor sem nenhuma mudança (sem divisão, reordenação nem descarte), como o valor único de um
    # formulário do PL, e o `pl-checkbox` o lê como `set("ca") = {a, c}`; o gabarito do preset é (a, c), com
    # `net-correct`: A tem nota 1 (✅ e "nota 100%") e C, só com "a", nota 0,5 (◐ e "nota 50%"); as marcas por
    # alternativa escolhida
    q = _question_line(w, "multiple_choice", a)
    graded(a, "multiple_choice", lambda: a.io.reply(q, "ca"), text="ca", target=q, raw={"resposta": "ca"},
           chosen=("a", "c"), marks={"a": True, "c": True})
    dont_know(b, "multiple_choice", parts={})
    q = _question_line(w, "multiple_choice", c)
    graded(c, "multiple_choice", lambda: c.io.reply(q, "a"), text="a", target=q, raw={"resposta": "a"},
           chosen=("a",), marks={"a": True})
    # número com vírgula, inteiro e texto, em resposta ao pedido do campo (pelo `channel_ref`)
    p = _prompt_line(w, "number", a, 0)
    graded(a, "number", lambda: a.io.reply(p, "6,5"), text="6,5", target=p, raw={"resposta": "6.5"},
           normalized=["resposta"], echo="6,5")
    p = _prompt_line(w, "number", b, 0)
    graded(b, "number", lambda: b.io.reply(p, "7"), text="7", target=p, raw={"resposta": "7"}, echo="7")
    p = _prompt_line(w, "integer", a, 0)
    graded(a, "integer", lambda: a.io.reply(p, "42"), text="42", target=p, raw={"resposta": "42"}, echo="42")
    p = _prompt_line(w, "integer", b, 0)
    graded(b, "integer", lambda: b.io.reply(p, "41"), text="41", target=p, raw={"resposta": "41"}, echo="41")
    dont_know(c, "integer", parts={})
    p = _prompt_line(w, "text", a, 0)
    graded(a, "text", lambda: a.io.reply(p, "azul"), text="azul", target=p, raw={"resposta": "azul"}, echo="azul")
    p = _prompt_line(w, "text", b, 0)
    graded(b, "text", lambda: b.io.reply(p, "verde"), text="verde", target=p, raw={"resposta": "verde"},
           echo="verde")
    # várias partes: as digitáveis pelo pedido de cada campo, as de escolha pela chave na questão
    q = _question_line(w, "multipart", a)
    p = _prompt_line(w, "multipart", a, 0)
    part(a, "multipart", lambda: a.io.reply(p, "6,5"), text="6,5", target=p, parts={"num": "6,5"}, current=1)
    p = _prompt_line(w, "multipart", a, 1)
    part(a, "multipart", lambda: a.io.reply(p, "31"), text="31", target=p, parts={"num": "6,5", "int": "31"},
         current=2)
    p = _prompt_line(w, "multipart", a, 2)
    part(a, "multipart", lambda: a.io.reply(p, "azul"), text="azul", target=p,
         parts={"num": "6,5", "int": "31", "txt": "azul"}, current=3)
    part(a, "multipart", lambda: a.io.choose(q, "b"), text="b", target=q,
         parts={"num": "6,5", "int": "31", "txt": "azul", "mc": "b"}, current=4)
    graded(a, "multipart", lambda: a.io.reply(q, "ac"), text="ac", target=q,
           raw={"num": "6.5", "int": "31", "txt": "azul", "mc": "b", "cb": "ac"},
           literal={"num": "6,5", "int": "31", "txt": "azul", "mc": "b", "cb": "ac"}, normalized=["num"])
    # o "Não sei" no meio das partes guarda o que já foi preenchido
    p = _prompt_line(w, "multipart", b, 0)
    part(b, "multipart", lambda: b.io.reply(p, "1"), text="1", target=p, parts={"num": "1"}, current=1)
    dont_know(b, "multipart", parts={"num": "1"})

    assert _events(w, cursor, *DISPATCH_EVENT_TYPES) == expected
    _settle(w)
    before_close = {
        "single_choice": _counts(answered=2, delivered=1), "multiple_choice": _counts(answered=2, dont_know=1),
        "number": _counts(answered=2, delivered=1), "integer": _counts(answered=2, dont_know=1),
        "text": _counts(answered=2, delivered=1), "multipart": _counts(answered=1, dont_know=1, delivered=1)}
    for qid in QIDS:
        body = get_dispatch(w.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "deliveries": {**before_close[qid], "grading_pending": 0}}
    assert ANSWER_SENTINEL not in w.stream.getvalue(), "o gabarito chegou ao aluno antes do fechamento"
    assert len(w.engine_fake.calls("grade")) == 11

    # 7. Fechamento pelo relógio: `no_answer`, a revelação (só às com resposta ou "Não sei") e a linha de
    # fechamento como linhas novas, e o `dispatch.closed`.
    cursor = _last_seq(w)
    marks = {(qid, p.key): _max_message(w) for qid in QIDS for p in w.pupils}
    w.clock.set(w.closes + timedelta(seconds=1))
    _close_all(w)
    _settle(w)
    closed_at = {qid: _dispatch_row(w, qid)["closed_at"] for qid in QIDS}
    after_close = {qid: {**counts, "no_answer": counts["delivered"], "delivered": 0}
                   for qid, counts in before_close.items()}
    for qid in QIDS:
        for pupil in w.pupils:
            delivery = _delivery(w, qid, pupil)
            state = AFTER_CLOSE[qid][pupil.key]
            # a `no_answer` não é revelada (sem `revealed_at`, sem gabarito e sem explicação); a linha de
            # fechamento dela anuncia o `/naosei` e o aviso da atrasada.
            assert (delivery["state"], delivery["revealed_at"]) == (
                state, None if state == "no_answer" else closed_at[qid]), (qid, pupil.key)
            assert delivery["current_field"] == (0 if state == "no_answer" else None)
            [line] = _new_fallbacks(w, qid, pupil, marks[(qid, pupil.key)])
            assert CLOSED_NOTICE in line.text, (qid, pupil.key, line.text)
            assert (ANSWER_SENTINEL in line.text) is (state != "no_answer"), (qid, pupil.key, line.text)
            assert (DONT_KNOW_HOWTO in line.text) is (state == "no_answer"), (qid, pupil.key, line.text)
            assert (LATE_HOWTO in line.text) is (state == "no_answer"), (qid, pupil.key)
            if SPECS[qid].explanation is not None:
                assert (SPECS[qid].explanation in line.text) is (state != "no_answer"), (
                    "a explicação segue a regra do gabarito: não sai na `no_answer`", qid, pupil.key)
        body = get_dispatch(w.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "state": "closed", "closed_at": iso(closed_at[qid]),
                        "deliveries": {**after_close[qid], "grading_pending": 0}}
        shown[qid] = body
    got = _events(w, cursor, *DISPATCH_EVENT_TYPES)
    _same(got, [_ev("dispatch.closed", closed_at[qid], offering_id=offering_id,
                    dispatch_id=shown[qid]["dispatch_id"], trigger="window", counts=after_close[qid])
                for qid in QIDS])

    # 8. Atrasada: a escolha pela chave na entrega `no_answer` → `answered_late`, com o gabarito revelado junto
    # do feedback, na mesma linha.
    t_late = w.clock.advance(60)
    cursor = _last_seq(w)
    q = _question_line(w, "single_choice", c)
    mark = _max_message(w)
    update = _say(w, c, lambda: c.io.choose(q, "b"), text="b", outcome="answer_recorded", reply_to=q)
    late_event = _graded(w, "single_choice", c, update, raw={"resposta": "b"}, literal={"resposta": "b"},
                         normalized=[], late=True, at=t_late,
                         response_ms=int((t_late - t_conf).total_seconds() * 1000), echo=None, after=mark,
                         reveal=True, chosen=("b",))
    assert _delivery(w, "single_choice", c)["revealed_at"] == t_late, "revelada com o feedback da atrasada"
    late_lines = [ln for ln in _new_fallbacks(w, "single_choice", c, mark) if ANSWER_SENTINEL in ln.text]
    assert late_lines and SPECS["single_choice"].explanation in late_lines[-1].text, (
        "a explicação permitida sai com o gabarito, na linha da atrasada (controle positivo)")
    assert _events(w, cursor, *DISPATCH_EVENT_TYPES) == [late_event]
    _settle(w)
    body = get_dispatch(w.client, ADMIN_TOKEN, CODE, shown["single_choice"]["dispatch_id"])
    assert body == {**shown["single_choice"], "deliveries": {**_counts(answered=2, answered_late=1),
                                                             "grading_pending": 0}}

    # 9. O que vale para o roteiro inteiro.
    _final_checks(w)


def _check_question(w: _W, qid: str, pupil: _Pupil) -> None:
    """A mensagem da questão degradada: enunciado, alternativas pelas chaves, prazo e o
    `/naosei` anunciado, sem gabarito; o pedido do primeiro campo digitável.

    A dica `TYPED_MULTIPLE_HOWTO`: no console (`max_buttons = 0`) toda
    escolha degrada, e a dica sai só na múltipla degradada, uma vez por campo múltiplo, depois das
    alternativas dele (a múltipla e a parte múltipla da de várias partes); a escolha única degradada e as
    questões sem escolha não a trazem. A distância exata até as alternativas não é fixada."""
    spec = SPECS[qid]
    line = _question_line(w, qid, pupil)
    for piece in (spec.statement, DEADLINE_HEAD, DONT_KNOW_HOWTO, *spec.options):
        assert piece in line.text, (qid, pupil.key, piece)
    assert ANSWER_SENTINEL not in line.text
    multiples = [f for f in spec.fields if f["type"] == "multiple_choice"]
    assert line.text.count(TYPED_MULTIPLE_HOWTO) == len(multiples), (qid, pupil.key, "TYPED_MULTIPLE_HOWTO")
    if multiples:
        # Na de várias partes, as alternativas da parte múltipla são as últimas (`PRIMES`).
        hint_at = line.text.index(TYPED_MULTIPLE_HOWTO)
        assert all(line.text.index(option) < hint_at for option in PRIMES), (qid, pupil.key, "dica antes")
    if spec.fields[0]["type"] not in CHOICE_TYPES:
        _prompt_line(w, qid, pupil, 0)


def _close_all(w: _W, timeout: float = 3 * WAIT) -> None:
    """Espera os seis disparos `closed`; sem isso, avança o relógio além de `CLOSE_RECHECK_SECONDS` (5 s),
    para a varredura criar a rodada seguinte."""
    deadline = time.monotonic() + timeout
    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if all(_dispatch_row(w, qid)["state"] == "closed" for qid in QIDS):
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            states = {qid: _dispatch_row(w, qid)["state"] for qid in QIDS}
            raise AssertionError(f"os disparos não fecharam em {timeout} s: {states}")
        w.clock.advance(6)


def _final_checks(w: _W) -> None:
    """O console nunca editou (toda edição virou linha nova), nenhuma linha traz o chat, as mensagens de
    entrega não guardam texto, toda entrada do console foi gravada com `update_id` negativo e
    processada, e toda tarefa terminou `done`, nenhuma com `NO_HANDLER_ERROR`."""
    lines = read_lines(w.stream.getvalue())
    assert [ln.raw for ln in lines if ln.edit_of is not None] == []
    _no_chat_in_console(w)
    rows = _all(w.sql, "SELECT delivery_role, operation, state, text FROM outbox_messages "
                "WHERE delivery_id IS NOT NULL")
    assert rows and {(r["operation"], r["state"], r["text"]) for r in rows} == {("send", "sent", None)}
    assert {r["delivery_role"] for r in rows} == {"question", "field_prompt", "fallback"}
    updates = _all(w.sql, "SELECT update_id, state FROM telegram_updates WHERE bot_id = %s", (w.bot_row,))
    assert updates and all(u["update_id"] < 0 and u["state"] == "processed" for u in updates)
    assert w.sql.execute("SELECT count(*) FROM tasks WHERE last_error = %s", (NO_HANDLER_ERROR,)).fetchone()[0] == 0
    states = dict(w.sql.execute("SELECT state, count(*) FROM tasks GROUP BY state").fetchall())
    assert set(states) == {"done"}, f"tarefas fora de done: {states}"
    assert len(w.engine_fake.calls("grade")) == 12
    assert w.engine_fake.protocol_errors() == []
