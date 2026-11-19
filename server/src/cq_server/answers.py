"""Entrada do aluno numa entrega: toques, textos, partes, "Não sei" e recusas.
Recebe os toques nos botões e os textos digitados, grava as partes e as submissões e responde ao aluno
com textos fixos; a correção em si fica com `grading`, por uma tarefa enfileirada.

- `reception._apply` despacha para cá, sem mudar os fluxos da equipe e dos alunos: `process_tap` (um `callback_query`
  com dado `cq1:`) e `process_message` (texto sem comando, comando desconhecido, mensagem sem texto,
  `/naosei`); `process_message` devolve `None` quando o remetente não é aluno vinculado (a recepção segue).
- Tudo na transação do update, com as travas numa ordem fixa: o endereço
  (`team.chat_endpoint`, `FOR NO KEY UPDATE`) → o disparo (`FOR SHARE`) → a entrega (`FOR NO KEY UPDATE`);
  o lock dos eventos por último. Duas saídas sem gravar nada, pela exceção (a transação do chamador volta atrás):
  `AwaitTransition` (entrada depois do horário com o disparo ainda aberto: o tratador termina `done`
  sem efeito e o update continua `pending`) e `RetryTaskLater(2)` (entrega `pending`, com a
  questão ainda em envio).
- A renderização do estado corrente é da `deliveries.render_state` (que grava o `current_render`), com o
  formatador do dialeto gravado na abertura (`channel.formatter_for(rendered.format)`) e as
  capacidades do canal; as mensagens da entrega saem pela fila (`outbox.enqueue_delivery`,
  `enqueue_delivery_edit`, `cancel_delivery_messages`) e as respostas fixas ao aluno, como texto simples
  (`Outbox.enqueue`), sem texto do aluno.
- `normalize_decimal_comma` (a única normalização da resposta, a vírgula decimal) e `raw_for_engine` (a resposta
  crua ao motor, refeita de `raw_input` e `normalized_fields`).
- Auxiliares da entrega usados também por `grading` (`load_delivery`, `load_contents`, `refresh`,
  `enqueue_field_prompt`, `field_typeable`, `reveal_now`), para que a resposta e a correção mexam na
  entrega do mesmo jeito.

`Inbound` guarda o `chat_id`, o `sender_id` e o texto fora do `repr` (a identidade só sai pela revelação). Importa
`deliveries`, `buttons`, `deliverytexts`, `freemessages`, `outbox`, `team`, `events` e `tasks`; nunca
`reception`, `grading` nem `worker`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server import buttons, deliveries, deliverytexts, freemessages, offerings, outbox, team
from cq_server._leasing import utc
from cq_server.buttons import CallbackAction
from cq_server.config import DeliverySettings
from cq_server.events import EventType, append_event, load_write_identities
from cq_server.outbox import Outbox
from cq_server.ports.delivery import ChannelCapabilities, DeliveryChannel
from cq_server.tasks import RetryTaskLater, enqueue_in_transaction


class SubmissionOutcome(StrEnum):
    """Desfecho de uma submissão."""

    GRADING = "grading"
    GRADED = "graded"
    BROKEN = "broken"
    INTERNAL_FAILURE = "internal_failure"
    DONT_KNOW = "dont_know"
    REFUSED = "refused"


class Refusal(StrEnum):
    """Recusa de uma tentativa."""

    ALREADY_ANSWERED = "already_answered"
    GRADING_PENDING = "grading_pending"
    UNAVAILABLE = "unavailable"
    WINDOW_CLOSED = "window_closed"


# Os desfechos da recepção definidos aqui (`telegram_updates.outcome`).
ANSWER_RECORDED = "answer_recorded"
ANSWER_PART_RECORDED = "answer_part_recorded"
ANSWER_TOGGLED = "answer_toggled"
ANSWER_DONT_KNOW = "answer_dont_know"
ANSWER_REFUSED = "answer_refused"
ANSWER_STALE = "answer_stale"
ANSWER_IGNORED = "answer_ignored"
ANSWER_NOT_TEXT = "answer_not_text"
FREE_MESSAGE = "free_message"
FREE_MESSAGE_AMBIGUOUS = "free_message_ambiguous"
ABANDONED = "abandoned"
# Os desfechos do modo desligado das mensagens livres.
FREE_MESSAGE_MASKED_REPLIED = "free_message_masked_replied"
FREE_MESSAGE_MASKED = "free_message_masked"
FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED = "free_message_ambiguous_masked_replied"
FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED_PLAIN = "free_message_ambiguous_masked_replied_plain"
FREE_MESSAGE_AMBIGUOUS_MASKED = "free_message_ambiguous_masked"
# Só estes cinco mascaram o texto do update, no mesmo `UPDATE` que o marca `processed`.
MASKED_OUTCOMES = frozenset({FREE_MESSAGE_MASKED_REPLIED, FREE_MESSAGE_MASKED, FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED,
                             FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED_PLAIN, FREE_MESSAGE_AMBIGUOUS_MASKED})
# O toque num botão da prévia e a resposta a uma mensagem da prévia.
PREVIEW_TAPPED = "preview_tapped"
PREVIEW_REPLIED = "preview_replied"
OUTCOMES = (ANSWER_RECORDED, ANSWER_PART_RECORDED, ANSWER_TOGGLED, ANSWER_DONT_KNOW, ANSWER_REFUSED,
            ANSWER_STALE, ANSWER_IGNORED, ANSWER_NOT_TEXT, FREE_MESSAGE, FREE_MESSAGE_AMBIGUOUS, ABANDONED,
            FREE_MESSAGE_MASKED_REPLIED, FREE_MESSAGE_MASKED, FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED,
            FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED_PLAIN, FREE_MESSAGE_AMBIGUOUS_MASKED, PREVIEW_TAPPED,
            PREVIEW_REPLIED)
# Os limitadores do modo desligado, sobre os desfechos dos updates. (a) O do contato:
# as respostas que levam a frase do contato (ou, sem contato, a resposta ao texto livre). (b) O do pedido de
# usar "responder" (o `USE_REPLY`, com ou sem a frase do contato).
_CONTACT_LIMITER = (FREE_MESSAGE_MASKED_REPLIED, FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED)
_USE_REPLY_LIMITER = (FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED, FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED_PLAIN)

# O "Não sei" digitado.
DONT_KNOW_COMMAND = deliverytexts.DONT_KNOW_COMMAND
# O texto exato `/naosei` ou `/naosei@<bot>` (a recepção só repassa o comando dirigido a este bot).
_DONT_KNOW_TEXT = re.compile(re.escape(DONT_KNOW_COMMAND) + r"(?:@[A-Za-z0-9_]{1,64})?")

# A tarefa da correção, como texto: `answers` não importa `grading` (que o importa).
GRADE_TASK_TYPE = "submission.grade"
# A entrega ainda `pending` (a questão em envio) repete o update depois de 2 s.
PENDING_RETRY_SECONDS = 2

# Estados da entrega.
_CLOSED_STATES = frozenset({"answered", "dont_know", "answered_late"})
_UNAVAILABLE_STATES = frozenset({"not_delivered", "question_failed"})
_OPEN_STATES = frozenset({"delivered", "no_answer"})
# Papéis das mensagens da entrega que levam os controles.
_CONTROL_ROLES = frozenset({"question", "fallback"})
_CHOICE_TYPES = frozenset({"single_choice", "multiple_choice"})


class AwaitTransition(Exception):  # noqa: N818 - é um sinal, não um erro
    """A entrada foi gravada depois do horário com o disparo ainda aberto:
    nada é gravado (o chamador desfaz a transação), o update continua `pending` e a tarefa termina
    `done` sem efeito; a varredura o reenfileira até a transição do fechamento."""


@dataclass(frozen=True)
class Inbound:
    """Um update do aluno, como o tratador `telegram.update` o lê."""

    update_row_id: int
    bot_id: int
    kind: str
    chat_id: int | None = field(repr=False)
    sender_id: int | None = field(repr=False)
    text: str | None = field(repr=False)
    callback_id: str | None
    data: str | None
    message_id: int | None
    reply_to_message_id: int | None
    received_at: datetime


@dataclass(frozen=True)
class TapResult:
    """O desfecho de um toque (`telegram_updates.outcome`) e o texto da confirmação (ou nenhum)."""

    outcome: str
    answer_text: str | None


# --------------------------------------------------------------------------------------------------
# A entrega vista pela resposta e pela correção
# --------------------------------------------------------------------------------------------------


@dataclass
class DeliveryView:
    """A linha da entrega com o disparo, a pessoa e o bot (lida sob a trava, quando `lock=True`)."""

    id: int
    delivery_uuid: uuid.UUID
    dispatch_row: int
    dispatch_uuid: uuid.UUID
    dispatch_state: str
    closes_at: datetime
    offering_row: int
    offering_uuid: uuid.UUID
    person_id: int
    pseudo_id: uuid.UUID
    bot_row: int
    endpoint_row: int
    state: str
    current_field: int | None
    render_version: int
    controls_version: int
    sent_at: datetime | None
    revealed_at: datetime | None
    question_ref_uuid: uuid.UUID
    question_ref_row: int


@dataclass
class Contents:
    """O conteúdo da entrega que a resposta usa."""

    ir: dict
    rendered: dict
    parts: dict
    selection: list

    @property
    def fields(self) -> list[dict]:
        return list(self.ir.get("fields") or [])

    def typeable(self, index: int, capabilities: ChannelCapabilities) -> bool:
        """O campo `index` recebe texto digitado: o que a abertura gravou em `rendered.fields[i]`
        (`typeable`, decidido pelas capacidades do canal da abertura); sem isso, `field_typeable`."""
        fields = self.fields
        if not 0 <= index < len(fields):
            return False
        specs = self.rendered.get("fields")
        if (isinstance(specs, list) and index < len(specs) and isinstance(specs[index], Mapping)
                and isinstance(specs[index].get("typeable"), bool)):
            return bool(specs[index]["typeable"])
        return field_typeable(fields[index], capabilities)


_VIEW_SQL = (
    "SELECT d.id, d.delivery_uuid, d.dispatch_id, x.dispatch_uuid, x.state, x.closes_at, x.offering_id, "
    "o.offering_id, e.person_id, p.pseudo_id, b.id, d.delivery_endpoint_id, d.state, d.current_field, "
    "d.render_version, d.controls_version, d.sent_at, d.revealed_at, q.ref_uuid, q.id "
    "FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id JOIN offerings o ON o.id = x.offering_id "
    "JOIN bots b ON b.offering_id = o.id JOIN enrollments e ON e.id = d.enrollment_id "
    "JOIN people p ON p.id = e.person_id JOIN question_refs q ON q.id = x.question_ref_id "
    "WHERE d.id = %s"
)


def load_delivery(conn: psycopg.Connection, delivery_row_id: int, *, lock: bool) -> DeliveryView | None:
    """A entrega `delivery_row_id`; com `lock`, o disparo `FOR SHARE` e depois a entrega `FOR NO KEY UPDATE`
    (nesta ordem), e a leitura sai depois das travas (o estado novo, em READ COMMITTED)."""
    if lock:
        found = conn.execute("SELECT dispatch_id FROM deliveries WHERE id = %s", (delivery_row_id,)).fetchone()
        if found is None:
            return None
        conn.execute("SELECT 1 FROM dispatches WHERE id = %s FOR SHARE", (found[0],))
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        conn.execute("SELECT 1 FROM deliveries WHERE id = %s FOR NO KEY UPDATE", (delivery_row_id,))
    row = conn.execute(_VIEW_SQL, (delivery_row_id,)).fetchone()
    if row is None:
        return None
    return DeliveryView(id=int(row[0]), delivery_uuid=row[1], dispatch_row=int(row[2]), dispatch_uuid=row[3],
                        dispatch_state=row[4], closes_at=utc(row[5]), offering_row=int(row[6]),
                        offering_uuid=row[7], person_id=int(row[8]), pseudo_id=row[9], bot_row=int(row[10]),
                        endpoint_row=int(row[11]), state=row[12],
                        current_field=None if row[13] is None else int(row[13]),
                        render_version=int(row[14]), controls_version=int(row[15]), sent_at=utc(row[16]),
                        revealed_at=utc(row[17]), question_ref_uuid=row[18], question_ref_row=int(row[19]))


def load_contents(conn: psycopg.Connection, delivery_row_id: int) -> Contents | None:
    """O conteúdo da entrega (existe desde a variante; `None` antes dela)."""
    row = conn.execute(
        "SELECT ir, rendered, parts, selection FROM delivery_contents WHERE delivery_id = %s",
        (delivery_row_id,),
    ).fetchone()
    if row is None:
        return None
    return Contents(ir=dict(row[0] or {}), rendered=dict(row[1] or {}), parts=dict(row[2] or {}),
                    selection=list(row[3] or []))


def has_grading(conn: psycopg.Connection, delivery_row_id: int) -> bool:
    """A marca de correção pendente: há submissão `grading` da entrega."""
    row = conn.execute("SELECT 1 FROM submissions WHERE delivery_id = %s AND outcome = 'grading'",
                       (delivery_row_id,)).fetchone()
    return row is not None


def field_typeable(field_ir: Mapping[str, Any], capabilities: ChannelCapabilities) -> bool:
    """O campo recebe texto digitado: `number`, `integer` e `text`, e a
    escolha degradada, sem botões ou com mais alternativas mais controles ("Enviar" e "Não sei", nas duas
    escolhas) que `max_buttons` (`deliveries.choice_has_buttons`, a regra única)."""
    kind = field_ir.get("type")
    if kind not in _CHOICE_TYPES:
        return True
    return not deliveries.choice_has_buttons(str(kind), len(field_ir.get("options") or ()), capabilities)


def _formatter(channel: DeliveryChannel, contents: Contents) -> Any:
    """O formatador do dialeto gravado na abertura; sem dialeto gravado, o do canal."""
    dialect = contents.rendered.get("format")
    return channel.formatter_for(dialect) if isinstance(dialect, str) else channel.formatter


def _route(conn: psycopg.Connection, view: DeliveryView) -> tuple[int, bool] | None:
    """O endereço das mensagens assíncronas da entrega: o da abertura, se ainda é o vigente
    (`True`: a original pode ser editada); senão, o vigente da pessoa no bot (`False`: mensagem nova,
    papel `fallback`); sem vigente, `None` (só o registro)."""
    row = conn.execute("SELECT state FROM delivery_endpoints WHERE id = %s", (view.endpoint_row,)).fetchone()
    if row is not None and row[0] in ("active", "blocked"):
        return view.endpoint_row, True
    current = conn.execute(
        "SELECT id FROM delivery_endpoints WHERE person_id = %s AND bot_id = %s AND channel = 'telegram' "
        "AND state IN ('active', 'blocked')",
        (view.person_id, view.bot_row),
    ).fetchone()
    return None if current is None else (int(current[0]), False)


def refresh(conn: psycopg.Connection, view: DeliveryView, contents: Contents, *, channel: DeliveryChannel,
            now: datetime, markup_only: bool = False, notice_key: str | None = None) -> None:
    """A renderização completa do estado já gravado (`deliveries.render_state`, que sobe a
    `render_version` e grava o `current_render`) e a edição da mensagem com os controles, com essa
    versão; num canal sem edição, ou com o endereço trocado, uma mensagem nova (papel `fallback`) com a
    renderização completa. Se o estado não cabe (`fits = False`)
    e `notice_key` vem, o feedback curto sai também no papel `notice`."""
    rendered = deliveries.render_state(conn, view.id, formatter=_formatter(channel, contents),
                                       capabilities=channel.capabilities, now=now)
    view.render_version = int(rendered.render_version)
    route = _route(conn, view)
    if route is None:
        return
    endpoint, same = route
    if same and channel.capabilities.can_edit:
        outbox.enqueue_delivery_edit(conn, delivery_row_id=view.id, recipient=view.pseudo_id, bot_id=view.bot_row,
                                     endpoint_row_id=endpoint, role="question", markup_only=markup_only,
                                     version=view.render_version, now=now)
    else:
        outbox.enqueue_delivery(conn, delivery_row_id=view.id, recipient=view.pseudo_id, bot_id=view.bot_row,
                                endpoint_row_id=endpoint, role="fallback",
                                idempotency_key=f"delivery.fallback:{view.id}:{view.render_version}", now=now)
    if notice_key is not None and not rendered.fits:
        outbox.enqueue_delivery(conn, delivery_row_id=view.id, recipient=view.pseudo_id, bot_id=view.bot_row,
                                endpoint_row_id=endpoint, role="notice",
                                idempotency_key=f"delivery.notice:{view.id}:{notice_key}", now=now)


def enqueue_field_prompt(conn: psycopg.Connection, view: DeliveryView, field_index: int, *, key: str,
                         now: datetime | None = None) -> None:
    """O pedido do campo `field_index`, em resposta à questão, pela fila.
    `now` (aditivo, só-nomeado): o horário do `Clock` do chamador, gravado na mensagem."""
    route = _route(conn, view)
    if route is None:
        return
    outbox.enqueue_delivery(conn, delivery_row_id=view.id, recipient=view.pseudo_id, bot_id=view.bot_row,
                            endpoint_row_id=route[0], role="field_prompt", field_index=field_index,
                            idempotency_key=f"delivery.field_prompt:{view.id}:{field_index}:{key}", now=now)


def notify(conn: psycopg.Connection, box: Outbox, view: DeliveryView, text: str, *, key: str) -> None:
    """Um texto fixo (sem texto do aluno) ao endereço vigente da pessoa no bot, pela fila de saída; sem
    endereço ativo, só o registro (a fila recusa com `ValueError` antes de gravar)."""
    try:
        box.enqueue(conn, recipient=view.pseudo_id, bot_id=view.bot_row, text=text, idempotency_key=key)
    except ValueError:
        pass


def move_to_field(conn: psycopg.Connection, view: DeliveryView, contents: Contents, field_index: int, *,
                  parts: Mapping[str, Any], channel: DeliveryChannel, prompt_key: str,
                  now: datetime | None = None) -> None:
    """Troca o campo corrente (avanço ou reabertura): grava as partes, zera a
    seleção, sobe a `controls_version` (a `render_version` sobe no `refresh`), cancela os
    pedidos pendentes de outros campos e enfileira o pedido do novo campo, se digitável. Não
    edita a questão (quem chama faz o `refresh`). `now` (aditivo, só-nomeado): o horário do `Clock`,
    repassado ao pedido enfileirado."""
    contents.parts = dict(parts)
    contents.selection = []
    conn.execute("UPDATE delivery_contents SET parts = %s, selection = %s WHERE delivery_id = %s",
                 (Jsonb(contents.parts), Jsonb([]), view.id))
    row = conn.execute(
        "UPDATE deliveries SET current_field = %s, controls_version = controls_version + 1 "
        "WHERE id = %s RETURNING controls_version",
        (field_index, view.id),
    ).fetchone()
    view.current_field = field_index
    view.controls_version = int(row[0])
    outbox.cancel_delivery_messages(conn, delivery_row_id=view.id, role="field_prompt",
                                    keep_field_index=field_index)
    if contents.typeable(field_index, channel.capabilities):
        enqueue_field_prompt(conn, view, field_index, key=prompt_key, now=now)


def reveal_now(conn: psycopg.Connection, view: DeliveryView, now: datetime) -> None:
    """A revelação devida na transação do desfecho: com o disparo já `closed` e `revealed_at`
    nulo, grava `revealed_at` (a renderização mostra o gabarito permitido)."""
    if view.dispatch_state == "closed" and view.revealed_at is None:
        conn.execute("UPDATE deliveries SET revealed_at = %s WHERE id = %s", (now, view.id))
        view.revealed_at = now


def _normalized_fields(fields: Sequence[Mapping[str, Any]], parts: Mapping[str, Any]) -> list[str]:
    """Os campos `number` cuja vírgula decimal é trocada (a submissão guarda a marca)."""
    names = []
    for field_ir in fields:
        name = field_ir.get("name")
        value = parts.get(name)
        if field_ir.get("type") == "number" and isinstance(value, str):
            if normalize_decimal_comma("number", value)[1]:
                names.append(name)
    return names


def _insert_submission(conn: psycopg.Connection, view: DeliveryView, inbound: Inbound, *, outcome: str,
                       now: datetime, raw_input: Mapping[str, Any], refusal: str | None = None,
                       normalized_fields: Sequence[str] = (), late: bool | None = None) -> tuple[int, uuid.UUID]:
    """Uma submissão da entrega, ligada ao update (um update gera no máximo uma)."""
    submission_uuid = uuid.uuid4()
    if late is None:
        late = inbound.received_at > view.closes_at
    row = conn.execute(
        "INSERT INTO submissions (submission_uuid, delivery_id, telegram_update_id, outcome, refusal, late, "
        "normalized_fields, raw_input, received_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (submission_uuid, view.id, inbound.update_row_id, outcome, refusal, late, list(normalized_fields),
         Jsonb(dict(raw_input)), inbound.received_at, now),
    ).fetchone()
    return int(row[0]), submission_uuid


# --------------------------------------------------------------------------------------------------
# Aplicação a um campo, "Não sei" e recusas
# --------------------------------------------------------------------------------------------------


def _apply_value(conn: psycopg.Connection, view: DeliveryView, contents: Contents, inbound: Inbound, *,
                 value: Any, now: datetime, channel: DeliveryChannel) -> str:
    """Grava `value` no campo corrente e avança: com todos os campos preenchidos,
    a submissão `grading` e a tarefa de correção (`answer_recorded`); senão, o próximo campo, o pedido
    dele e a edição do progresso (`answer_part_recorded`). O aluno não volta a um campo preenchido."""
    fields = contents.fields
    assert view.current_field is not None
    parts = dict(contents.parts)
    parts[fields[view.current_field]["name"]] = value
    following = next((i for i, f in enumerate(fields) if f.get("name") not in parts), None)
    if following is None:
        contents.parts = parts
        conn.execute("UPDATE delivery_contents SET parts = %s WHERE delivery_id = %s", (Jsonb(parts), view.id))
        submission_id, _ = _insert_submission(conn, view, inbound, outcome=SubmissionOutcome.GRADING, now=now,
                                              raw_input=parts,
                                              normalized_fields=_normalized_fields(fields, parts))
        enqueue_in_transaction(conn, GRADE_TASK_TYPE, {"submission_row_id": submission_id}, now=now,
                               idempotency_key=f"{GRADE_TASK_TYPE}:{submission_id}")
        # Com todos os campos preenchidos, nenhum pedido pendente da entrega sai mais.
        outbox.cancel_delivery_messages(conn, delivery_row_id=view.id, role="field_prompt")
        return ANSWER_RECORDED
    move_to_field(conn, view, contents, following, parts=parts, channel=channel,
                  prompt_key=f"u{inbound.update_row_id}", now=now)
    refresh(conn, view, contents, channel=channel, now=now)
    return ANSWER_PART_RECORDED


def _toggle(conn: psycopg.Connection, view: DeliveryView, contents: Contents, *, key: str, mark: bool,
            now: datetime, channel: DeliveryChannel) -> str:
    """`m`/`u`: marcar e desmarcar são absolutos; a mudança sobe só a `render_version` (pela
    renderização) e edita os marcadores; sem mudança, nada é gravado nem editado."""
    selection = list(contents.selection)
    if mark == (key in selection):
        return ANSWER_TOGGLED
    selection = [*selection, key] if mark else [k for k in selection if k != key]
    contents.selection = selection
    conn.execute("UPDATE delivery_contents SET selection = %s WHERE delivery_id = %s", (Jsonb(selection), view.id))
    refresh(conn, view, contents, channel=channel, now=now, markup_only=True)
    return ANSWER_TOGGLED


def _mark_single(conn: psycopg.Connection, view: DeliveryView, contents: Contents, *, key: str,
                 now: datetime, channel: DeliveryChannel) -> str:
    """`m` na escolha única: a marcação passa a `[key]`, trocando a
    anterior (no máximo uma, como o rádio do PL). Marcar a já marcada é absoluto: nada é gravado nem
    editado. A troca sobe só a `render_version` (pela renderização) e edita os botões, sem
    correção nem submissão; a `controls_version` não muda (o "Enviar" da mesma versão manda a marcação
    corrente)."""
    if list(contents.selection) == [key]:
        return ANSWER_TOGGLED
    selection = [key]
    contents.selection = selection
    conn.execute("UPDATE delivery_contents SET selection = %s WHERE delivery_id = %s", (Jsonb(selection), view.id))
    refresh(conn, view, contents, channel=channel, now=now, markup_only=True)
    return ANSWER_TOGGLED


def _dont_know(conn: psycopg.Connection, view: DeliveryView, contents: Contents, inbound: Inbound, *,
               now: datetime, channel: DeliveryChannel) -> str:
    """"Não sei" válido, numa entrega `delivered` ou `no_answer` (o "Não sei"
    atrasado): submissão `dont_know` com as partes preenchidas, sem motor, com `late = received_at >
    closes_at`; a entrega `dont_know`, as versões + 1, os pedidos pendentes cancelados,
    com o disparo já `closed` a revelação na mesma transação (`reveal_now`), a edição sem
    controles (com "Janela fechada" e o gabarito permitido, depois do fechamento) e o
    `delivery.dont_know`."""
    submission_id, submission_uuid = _insert_submission(conn, view, inbound, outcome=SubmissionOutcome.DONT_KNOW,
                                                        now=now, raw_input=contents.parts)
    row = conn.execute(
        "UPDATE deliveries SET state = 'dont_know', answered_at = %s, current_field = NULL, "
        "controls_version = controls_version + 1 WHERE id = %s AND state IN ('delivered', 'no_answer') "
        "RETURNING controls_version",
        (inbound.received_at, view.id),
    ).fetchone()
    view.state, view.current_field = "dont_know", None
    view.controls_version = int(row[0])
    outbox.cancel_delivery_messages(conn, delivery_row_id=view.id, role="field_prompt")
    reveal_now(conn, view, now)
    refresh(conn, view, contents, channel=channel, now=now)
    append_event(conn, EventType.DELIVERY_DONT_KNOW,
                 {"offering_id": view.offering_uuid, "dispatch_id": view.dispatch_uuid,
                  "delivery_id": view.delivery_uuid, "submission_id": submission_uuid, "person": view.pseudo_id},
                 occurred_at=now, actor_token_id=None)
    del submission_id
    return ANSWER_DONT_KNOW


_REFUSAL_TEXTS = {
    Refusal.UNAVAILABLE: deliverytexts.UNAVAILABLE,
    Refusal.ALREADY_ANSWERED: deliverytexts.ALREADY_ANSWERED,
    Refusal.GRADING_PENDING: deliverytexts.GRADING_IN_PROGRESS,
    Refusal.WINDOW_CLOSED: deliverytexts.WINDOW_CLOSED,
}


def _refusal(conn: psycopg.Connection, view: DeliveryView) -> Refusal | None:
    """As recusas da tentativa (na ordem): indisponível → encerrada → correção
    pendente; `None` se nenhuma vale. O `window_closed` não tem gatilho (o "Não sei" depois
    do horário espera a transição e vale como atrasado)."""
    if view.state in _UNAVAILABLE_STATES:
        return Refusal.UNAVAILABLE
    if view.state in _CLOSED_STATES:
        return Refusal.ALREADY_ANSWERED
    if has_grading(conn, view.id):
        return Refusal.GRADING_PENDING
    return None


def _refuse(conn: psycopg.Connection, view: DeliveryView, inbound: Inbound, refusal: Refusal, *,
            now: datetime, raw_input: Mapping[str, Any]) -> None:
    """A tentativa recusada: submissão `refused` com a recusa, sem mudar a entrega."""
    _insert_submission(conn, view, inbound, outcome=SubmissionOutcome.REFUSED, now=now, raw_input=raw_input,
                       refusal=refusal.value)


def _waits_for_close(view: DeliveryView, inbound: Inbound) -> bool:
    """Entrada gravada depois do horário com o disparo ainda aberto: espera a transição."""
    return view.dispatch_state == "open" and view.closes_at < inbound.received_at


def _reply(conn: psycopg.Connection, box: Outbox, inbound: Inbound, endpoint: team.Endpoint, text: str, *,
           priority: int = 0) -> None:
    """Um texto fixo (sem texto do aluno) ao aluno, pela fila de saída: à pessoa no bot (o endereço
    vigente, o chat do update; a rota que o console também entrega pelo `recipient`)."""
    pseudo = conn.execute("SELECT pseudo_id FROM people WHERE id = %s", (endpoint.person_id,)).fetchone()
    try:
        box.enqueue(conn, recipient=pseudo[0], bot_id=inbound.bot_id, text=text, priority=priority)
    except ValueError:
        # Sem endereço `active` (não acontece depois da reativação): ao chat do update.
        box.enqueue(conn, text=text, bot_id=inbound.bot_id, reply_to_update_id=inbound.update_row_id,
                    priority=priority)


def _linked_student(conn: psycopg.Connection, inbound: Inbound) -> tuple[team.Endpoint, int] | None:
    """O endereço vigente (travado) do chat e o oferecimento do bot, se o remetente é aluno vinculado:
    endereço `active`/`blocked` no bot **e** matrícula `student` `active`."""
    if inbound.chat_id is None:
        return None
    endpoint = team.chat_endpoint(conn, bot_id=inbound.bot_id, chat_id=inbound.chat_id)
    if endpoint is None:
        return None
    row = conn.execute(
        "SELECT b.offering_id FROM bots b JOIN enrollments e ON e.offering_id = b.offering_id "
        "WHERE b.id = %s AND e.person_id = %s AND e.role = 'student' AND e.state = 'active'",
        (inbound.bot_id, endpoint.person_id),
    ).fetchone()
    return None if row is None else (endpoint, int(row[0]))


# --------------------------------------------------------------------------------------------------
# Toque
# --------------------------------------------------------------------------------------------------


def parse_callback_data(data: str) -> CallbackAction | None:
    """O dado do botão decifrado (`buttons.decode_callback`), ou `None` fora de `cq1:`."""
    if not isinstance(data, str):
        return None
    return buttons.decode_callback(data)


def _tap_target(conn: psycopg.Connection, inbound: Inbound, action: CallbackAction
                ) -> tuple[int, team.Endpoint] | None:
    """A entrega do toque e o endereço do chat, pelas validações sem efeito: token
    desconhecido (ou de outro bot), chat que não é o endereço vigente da pessoa, mensagem conhecida de
    outra entrega ou de papel sem controles. A mensagem desconhecida no endereço vale como cópia não
    registrada da questão."""
    found = conn.execute(
        "SELECT d.id, e.person_id, d.delivery_endpoint_id FROM deliveries d "
        "JOIN dispatches x ON x.id = d.dispatch_id JOIN bots b ON b.offering_id = x.offering_id "
        "JOIN enrollments e ON e.id = d.enrollment_id WHERE d.button_token = %s AND b.id = %s",
        (action.token, inbound.bot_id),
    ).fetchone()
    if found is None or inbound.chat_id is None or inbound.message_id is None:
        return None
    delivery_row, person_id, endpoint_row = int(found[0]), int(found[1]), int(found[2])
    linked = _linked_student(conn, inbound)
    if linked is None:
        return None
    endpoint, _offering = linked
    if endpoint.person_id != person_id:
        return None
    rows = conn.execute(
        "SELECT delivery_id, delivery_role FROM outbox_messages WHERE delivery_endpoint_id = %s "
        "AND channel_ref = %s AND (operation IS NULL OR operation = 'send')",
        (endpoint.id, str(inbound.message_id)),
    ).fetchall()
    controls = any(r[0] is not None and int(r[0]) == delivery_row and r[1] in _CONTROL_ROLES for r in rows)
    if rows and not controls:
        return None
    # A cópia não registrada só vale no endereço da abertura; noutro chat da pessoa (troca de conta),
    # só uma mensagem com controles desta entrega enviada a ele (o `fallback`).
    if endpoint.id != endpoint_row and not controls:
        return None
    return delivery_row, endpoint


def _tap_effect(conn: psycopg.Connection, view: DeliveryView, contents: Contents, inbound: Inbound,
                action: CallbackAction, *, now: datetime, channel: DeliveryChannel) -> TapResult:
    """Depois das recusas: o `n` numa `no_answer` é o "Não sei" atrasado, de qualquer campo e versão dos
    controles; nos demais, versão, ação e chave conferidas, e o efeito.


    Ações aceitas por tipo do campo corrente: `single_choice` → `m` (marca,
    `_mark_single`) e `s` (envia a chave marcada como `str`, ou `None` sem marcação: o campo vai ausente
    ao motor); `multiple_choice` → `m`, `u` (`_toggle`) e `s` (a seleção, lista); `n` em todo
    campo. Qualquer outra (o `c`, reservado, e o `u` na única) é ação inexistente, sem
    efeito (`answer_ignored`)."""
    if action.action == "n" and view.state == "no_answer":
        return TapResult(_dont_know(conn, view, contents, inbound, now=now, channel=channel), None)
    if (view.current_field is None or action.field != view.current_field
            or action.version != view.controls_version % buttons.VERSION_MODULUS):
        return TapResult(ANSWER_STALE, deliverytexts.PART_ALREADY_ANSWERED)
    if action.action == "n":
        if view.state not in _OPEN_STATES:
            return TapResult(ANSWER_IGNORED, None)
        return TapResult(_dont_know(conn, view, contents, inbound, now=now, channel=channel), None)
    fields = contents.fields
    if not 0 <= view.current_field < len(fields):
        return TapResult(ANSWER_IGNORED, None)
    field_ir = fields[view.current_field]
    kind = field_ir.get("type")
    keys = [option.get("key") for option in (field_ir.get("options") or ())]
    allowed = {"single_choice": {"m", "s"}, "multiple_choice": {"m", "u", "s"}}.get(kind, set())
    if action.action not in allowed or (action.key is not None and action.key not in keys):
        return TapResult(ANSWER_IGNORED, None)
    if kind == "single_choice":
        if action.action == "m":
            assert action.key is not None
            outcome = _mark_single(conn, view, contents, key=action.key, now=now, channel=channel)
        else:
            chosen = [k for k in keys if k in set(contents.selection)]
            outcome = _apply_value(conn, view, contents, inbound, value=chosen[0] if chosen else None, now=now,
                                   channel=channel)
    elif action.action in ("m", "u"):
        assert action.key is not None
        outcome = _toggle(conn, view, contents, key=action.key, mark=action.action == "m", now=now,
                          channel=channel)
    else:
        chosen = [k for k in keys if k in set(contents.selection)]
        outcome = _apply_value(conn, view, contents, inbound, value=chosen, now=now, channel=channel)
    return TapResult(outcome, None)


def _preview_tap(conn: psycopg.Connection, inbound: Inbound, action: CallbackAction) -> TapResult:
    """Sem entrega com o token no bot, o token de uma prévia de
    disparo do oferecimento do bot → `preview_tapped` com `PREVIEW_TAP`, sem travar nem gravar nada além
    do update (a confirmação sai depois do commit); senão, o `answer_ignored`."""
    delivery = conn.execute(
        "SELECT 1 FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "JOIN bots b ON b.offering_id = x.offering_id WHERE d.button_token = %s AND b.id = %s LIMIT 1",
        (action.token, inbound.bot_id),
    ).fetchone()
    if delivery is None:
        from cq_server import noticetexts, previews  # importação local, só neste caminho

        if previews.is_preview_token(conn, bot_id=inbound.bot_id, token=action.token):
            return TapResult(PREVIEW_TAPPED, noticetexts.PREVIEW_TAP)
    return TapResult(ANSWER_IGNORED, None)


def process_tap(conn: psycopg.Connection, inbound: Inbound, *, now: datetime, outbox: Outbox,
                channel: DeliveryChannel) -> TapResult:
    """Um toque com dado `cq1:`, na transação do update: devolve o desfecho e o
    texto da confirmação, que o tratador manda depois do commit. `AwaitTransition` no toque
    `c`/`m`/`u`/`s`/`n` gravado depois do horário com o disparo ainda aberto;
    `RetryTaskLater(2)` com a entrega ainda `pending` (a questão em envio). A submissão `refused` de um
    toque guarda `{}` em `raw_input`: o dado literal fica no update."""
    del outbox  # as confirmações não passam pela fila
    action = parse_callback_data(inbound.data or "")
    if action is None:
        return TapResult(ANSWER_IGNORED, None)
    target = _tap_target(conn, inbound, action)
    if target is None:
        return _preview_tap(conn, inbound, action)
    delivery_row, endpoint = target
    team.reactivate_endpoint(conn, endpoint, now)
    view = load_delivery(conn, delivery_row, lock=True)
    if view is None:
        return TapResult(ANSWER_IGNORED, None)
    if _waits_for_close(view, inbound):
        raise AwaitTransition()
    if view.state == "pending":
        raise RetryTaskLater(PENDING_RETRY_SECONDS)
    refusal = _refusal(conn, view)
    contents = load_contents(conn, view.id)
    if refusal is not None:
        _refuse(conn, view, inbound, refusal, now=now, raw_input={})
        return TapResult(ANSWER_REFUSED, _REFUSAL_TEXTS[refusal])
    if contents is None or view.state not in _OPEN_STATES:
        return TapResult(ANSWER_IGNORED, None)
    return _tap_effect(conn, view, contents, inbound, action, now=now, channel=channel)


# --------------------------------------------------------------------------------------------------
# Texto e mensagem sem texto
# --------------------------------------------------------------------------------------------------


def _replied_delivery(conn: psycopg.Connection, inbound: Inbound, endpoint: team.Endpoint
                      ) -> tuple[int, str | None, int | None] | None:
    """Ligação por resposta: a mensagem respondida é uma mensagem conhecida de uma entrega da pessoa no endereço
    (`outbox_messages_endpoint_ref_idx`), de qualquer papel: (entrega, papel, índice do campo)."""
    if inbound.reply_to_message_id is None:
        return None
    row = conn.execute(
        "SELECT m.delivery_id, m.delivery_role, m.field_index FROM outbox_messages m "
        "JOIN deliveries d ON d.id = m.delivery_id JOIN enrollments e ON e.id = d.enrollment_id "
        "WHERE m.delivery_endpoint_id = %s AND m.channel_ref = %s AND m.delivery_id IS NOT NULL "
        "AND m.operation = 'send' AND e.person_id = %s ORDER BY m.id DESC LIMIT 1",
        (endpoint.id, str(inbound.reply_to_message_id), endpoint.person_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), row[1], None if row[2] is None else int(row[2])


def _open_candidates(conn: psycopg.Connection, inbound: Inbound, endpoint: team.Endpoint, *,
                     typeable_only: bool) -> list[int]:
    """Ligação pelo campo aberto (`deliveries.open_fields`): as entregas `delivered` da pessoa no bot, de disparo `open`
    com `closes_at > received_at`, sem correção pendente e com campo corrente (digitável, no texto;
    qualquer, no `/naosei`). A de um disparo aberto com o horário já passado não conta."""
    found = deliveries.open_fields(conn, bot_id=inbound.bot_id, person_id=endpoint.person_id,
                                   received_at=inbound.received_at)
    return sorted({f.delivery_row_id for f in found if f.typeable or not typeable_only})


def _free_message(conn: psycopg.Connection, inbound: Inbound, endpoint: team.Endpoint, offering_row: int, *,
                  ambiguous: bool, now: datetime, box: Outbox, delivery: DeliverySettings) -> str:
    """A mensagem livre, pelo modo do oferecimento lido no processamento
    (`offerings.free_message_settings`). Ligado: o texto redigido na gravação e a resposta fixa do
    tipo, se o limitador deixa, pela fila com `priority = 1`. Desligado (`_masked_free_message`): nada é
    gravado em `free_messages` e o desfecho é um dos `MASKED_OUTCOMES` (a recepção mascara o texto)."""
    assert inbound.text is not None
    settings = offerings.free_message_settings(conn, offering_row)
    if not settings.enabled:
        return _masked_free_message(conn, inbound, endpoint, contact=settings.contact_email, ambiguous=ambiguous,
                                    now=now, box=box, delivery=delivery)
    reply = freemessages.store_free_message(
        conn, offering_row_id=offering_row, endpoint_row_id=endpoint.id, update_row_id=inbound.update_row_id,
        text=inbound.text, ambiguous=ambiguous, received_at=inbound.received_at, now=now,
        identities=load_write_identities(conn), reply_interval_seconds=delivery.free_reply_seconds)
    if reply is not None:
        _reply(conn, box, inbound, endpoint, reply, priority=1)
    return FREE_MESSAGE_AMBIGUOUS if ambiguous else FREE_MESSAGE


def _buttons_open(conn: psycopg.Connection, inbound: Inbound, endpoint: team.Endpoint) -> bool:
    """A pessoa tem aberta, pela mesma regra de `open_fields`, ao menos uma entrega com
    escolha com botões como campo corrente (o campo não digitável) e nenhum campo digitável."""
    found = deliveries.open_fields(conn, bot_id=inbound.bot_id, person_id=endpoint.person_id,
                                   received_at=inbound.received_at)
    return any(not f.typeable for f in found) and not any(f.typeable for f in found)


def _masked_free_message(conn: psycopg.Connection, inbound: Inbound, endpoint: team.Endpoint, *,
                         contact: str | None, ambiguous: bool, now: datetime, box: Outbox,
                         delivery: DeliverySettings) -> str:
    """O modo desligado: nenhuma linha em
    `free_messages` e os dois limitadores sobre os updates. O texto não ambíguo recebe
    `answers_only_reply(contato, buttons_open=…)` só com o limitador do contato livre; o ambíguo recebe o
    `USE_REPLY` só com o limitador do pedido livre, seguido da frase do contato só se houver contato e o
    limitador do contato também estiver livre (assim o e-mail sai no máximo uma vez por intervalo). As
    respostas saem pela fila com `priority = 1`, como texto puro, sem evento."""
    interval = delivery.free_reply_seconds

    def due(outcomes: Sequence[str]) -> bool:
        return freemessages.masked_reply_due(conn, bot_id=inbound.bot_id, chat_id=inbound.chat_id,
                                             outcomes=outcomes, now=now, reply_interval_seconds=interval)

    if not ambiguous:
        if not due(_CONTACT_LIMITER):
            return FREE_MESSAGE_MASKED
        text = deliverytexts.answers_only_reply(contact, buttons_open=_buttons_open(conn, inbound, endpoint))
        _reply(conn, box, inbound, endpoint, text, priority=1)
        return FREE_MESSAGE_MASKED_REPLIED
    if not due(_USE_REPLY_LIMITER):
        return FREE_MESSAGE_AMBIGUOUS_MASKED
    if contact is not None and due(_CONTACT_LIMITER):
        _reply(conn, box, inbound, endpoint, deliverytexts.use_reply_masked(contact), priority=1)
        return FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED
    _reply(conn, box, inbound, endpoint, deliverytexts.use_reply_masked(None), priority=1)
    return FREE_MESSAGE_AMBIGUOUS_MASKED_REPLIED_PLAIN


def process_message(conn: psycopg.Connection, inbound: Inbound, *, now: datetime, outbox: Outbox,
                    channel: DeliveryChannel, delivery: DeliverySettings) -> str | None:
    """Um texto (ou mensagem sem texto) de chat privado dirigido ao bot, na transação do update
    (resposta, senão campo aberto): devolve o desfecho, ou `None` se o remetente não é aluno vinculado (a recepção
    segue). `AwaitTransition` no texto por "responder" (inclusive o `/naosei`) gravado depois do horário
    com o disparo ainda aberto; `RetryTaskLater(2)` no texto ligado a uma entrega
    `pending`. O `/naosei` por "responder" a uma mensagem de uma entrega `no_answer` é o "Não
    sei" atrasado."""
    linked = _linked_student(conn, inbound)
    if linked is None:
        return None
    endpoint, offering_row = linked
    team.reactivate_endpoint(conn, endpoint, now)
    text = inbound.text
    dont_know = text is not None and _DONT_KNOW_TEXT.fullmatch(text) is not None

    replied = _replied_delivery(conn, inbound, endpoint)
    role: str | None = None
    prompt_field: int | None = None
    if replied is not None:
        delivery_row, role, prompt_field = replied
    else:
        candidates = _open_candidates(conn, inbound, endpoint, typeable_only=not dont_know)
        if len(candidates) != 1:
            if text is None:
                return None
            return _free_message(conn, inbound, endpoint, offering_row, ambiguous=len(candidates) > 1,
                                 now=now, box=outbox, delivery=delivery)
        delivery_row = candidates[0]

    if text is None:
        _reply(conn, outbox, inbound, endpoint, deliverytexts.REPLY_WITH_TEXT)
        return ANSWER_NOT_TEXT
    view = load_delivery(conn, delivery_row, lock=True)
    if view is None:  # pragma: no cover - a entrega achada acima existe
        return None
    if replied is not None and _waits_for_close(view, inbound):
        raise AwaitTransition()
    if view.state == "pending":
        raise RetryTaskLater(PENDING_RETRY_SECONDS)
    contents = load_contents(conn, view.id)
    refusal = _refusal(conn, view)
    if refusal is not None:
        raw: dict[str, Any] = {}
        if not dont_know and contents is not None and view.current_field is not None \
                and 0 <= view.current_field < len(contents.fields):
            raw = {contents.fields[view.current_field]["name"]: text}
        _refuse(conn, view, inbound, refusal, now=now, raw_input=raw)
        _reply(conn, outbox, inbound, endpoint, _REFUSAL_TEXTS[refusal])
        return ANSWER_REFUSED
    if dont_know and view.state == "no_answer" and contents is not None:
        # O "Não sei" atrasado por "responder" a qualquer mensagem da entrega.
        return _dont_know(conn, view, contents, inbound, now=now, channel=channel)
    if role == "field_prompt" and prompt_field != view.current_field:
        _reply(conn, outbox, inbound, endpoint, deliverytexts.PART_ALREADY_ANSWERED)
        return ANSWER_STALE
    if contents is None or view.current_field is None or not 0 <= view.current_field < len(contents.fields):
        _reply(conn, outbox, inbound, endpoint, deliverytexts.PART_ALREADY_ANSWERED)
        return ANSWER_STALE
    if dont_know:
        return _dont_know(conn, view, contents, inbound, now=now, channel=channel)
    return _apply_value(conn, view, contents, inbound, value=text, now=now, channel=channel)


# --------------------------------------------------------------------------------------------------
# Resposta crua ao motor
# --------------------------------------------------------------------------------------------------


def normalize_decimal_comma(field_type: str, literal: str) -> tuple[str, bool]:
    """Num campo `number`, exatamente uma vírgula e nenhum ponto → a vírgula vira ponto.

    Só a vírgula U+002C e o ponto U+002E contam; nada é aparado (o PL recusa depois o que não é
    número). Toda outra entrada, e todo campo de outro tipo, volta igual, com `False`."""
    if field_type == "number" and literal.count(",") == 1 and "." not in literal:
        return literal.replace(",", "."), True
    return literal, False


def _button_selection(options: Sequence[Mapping[str, Any]] | None, selection: Sequence[str]) -> list[str]:
    """A seleção pelos botões na ordem das opções da IR. Uma chave fora das opções (que os
    botões validados não produzem) vai depois, na ordem em que veio: nada é descartado."""
    order = {option["key"]: index for index, option in enumerate(options or ())}
    return sorted(selection, key=lambda key: order.get(key, len(order)))


def raw_for_engine(ir: Mapping[str, Any], raw_input: Mapping[str, Any],
                   normalized_fields: Sequence[str]) -> dict:
    """A resposta crua enviada ao motor, refeita deterministicamente.

    Por campo da IR com resposta em `raw_input`, na ordem da IR:
    - escolha única → a chave marcada (pelo "Enviar", `str`) ou o texto digitado, literal; sem
      marcação (`None`), campo ausente, como o PL grava o formulário sem rádio marcado;
    - múltipla pelos botões (lista) → as chaves na ordem das opções; uma só como texto; nenhuma, campo
      ausente (como o PL grava o formulário sem marcação);
    - múltipla digitada (texto) → o texto literal, sem nenhuma mudança: o `pl-checkbox` decide;
    - `number`, `integer` e `text` → o texto literal, salvo a vírgula do `number` listado em
      `normalized_fields`, a única transformação.
    Nenhum campo fora da IR; a entrada literal não muda."""
    normalized = set(normalized_fields)
    body: dict[str, Any] = {}
    for field_ir in ir["fields"]:
        name = field_ir["name"]
        if name not in raw_input or raw_input[name] is None:
            continue
        value = raw_input[name]
        field_type = field_ir["type"]
        if field_type == "multiple_choice" and isinstance(value, (list, tuple)):
            keys = _button_selection(field_ir.get("options"), list(value))
            if not keys:
                continue
            body[name] = keys[0] if len(keys) == 1 else keys
        elif field_type == "number" and name in normalized and isinstance(value, str):
            body[name] = normalize_decimal_comma(field_type, value)[0]
        else:
            body[name] = value
    return body
