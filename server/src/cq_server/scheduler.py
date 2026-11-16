"""Varredura periódica: agendador e reconciliação das tarefas que nascem do estado gravado (validação,
abertura, fechamento, entrega, revelação, correção, recepção, relatórios e avisos).

Nenhuma tarefa tem `available_at` futuro: o "quando" fica na linha do assunto, e `sweep_once`
enfileira o que venceu e reanima o que ficou sem tarefa viva, com a chave `<tipo>:<id>:r<k>` (`k` = 1
+ terminadas) e o intervalo depois de uma rodada terminada (`backoff_max_seconds` depois de uma
`failed`; o `done_gap` da regra depois de uma `done`). Os dois filtros (sem tarefa viva e fora do
intervalo) ficam dentro da `due_sql`, antes do `LIMIT`. `max_rounds` conta só as rodadas `failed`;
esgotado, `on_exhausted` decide (o abandono do update; a rejeição da validação).

Escolha da implementação no `dispatch.close`: o `done_gap`
depois de uma rodada `done` é dispensado enquanto a barreira do fechamento **já vale** no `now` passado
(`_CLOSE_BARRIER_HOLDS`, com a definição do tratador); a rodada que nasce assim fecha o disparo. Sem a
barreira, a rodada seguinte espera o `CLOSE_RECHECK_SECONDS`, o `done_gap` da regra.

**Uma rodada** (`sweep_once`), regra a regra, na ordem de `DISPATCH_RULES`:
1. a `due_sql` lista até `limit` assuntos vencidos em ordem de `id`, já sem os que têm tarefa
   viva e sem os que estão no intervalo;
2. cada assunto numa **transação curta** própria: a mesma `due_sql`, restrita ao assunto, trava a linha
   (`FOR NO KEY UPDATE … SKIP LOCKED`, também no update, como o
   `reception.process_update`) e, num segundo comando (outro instantâneo em READ COMMITTED), reconfere
   os dois filtros sob a trava: a tarefa que outra varredura acabou de confirmar aparece aqui. A trava é
   a primeira defesa contra a duplicata; a chave determinística, a segunda;
3. com o teto de rodadas `failed` esgotado, chama `on_exhausted` na mesma transação e conta o assunto
   em `abandoned`; senão, enfileira `<tipo>:<id>:r<k>` com `tasks.enqueue_in_transaction` e o `now`
   passado (nunca o relógio do banco).

Uma rodada vazia não escreve nem registra; com algo enfileirado ou abandonado, um INFO
`scheduler.swept` (logger `cq_server.worker`) só com as contagens por tipo e o `abandoned`.

O `Sweeper` roda no laço do worker a cada `SWEEP_SECONDS` reais (monotônico), com o `now` do `Clock`,
só com os tipos que têm tratador (`handled_types`), numa conexão própria aberta no primeiro uso.

Os tipos de tarefa ficam aqui como texto (os mesmos de `dispatches`, `deliveries`, `grading` e
`reception`), para o módulo não importar os tratadores; `dispatches` só é importado dentro do
`on_exhausted` da validação. De `offerings` vem só `POLLED_STATES` (a barreira do fechamento).

**Relatórios e avisos**: `REPORT_RULES` separado de
`DISPATCH_RULES`, com o próprio `REPORT_DONE_GAP`; o `Sweeper` itera `DISPATCH_RULES + REPORT_RULES` filtrado pelos
`handled_types`. As 11 regras, nesta ordem: `report.freeze`, `notice.send`, as 3 do disparo, as 4
detecções do oferecimento, `preview.send` e `digest.daily`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.offerings import POLLED_STATES
from cq_server.tasks import enqueue_in_transaction, finished_rounds

logger = logging.getLogger("cq_server.worker")

# As cadências da varredura e os tetos de rodadas.
SWEEP_SECONDS = 1.0
SWEEP_BATCH = 100
IMMEDIATE_CONFIRM_HOURS = 24
SWEEP_MAX_UPDATE_ROUNDS = 3
VALIDATE_MAX_ROUNDS = 3
CLOSE_RECHECK_SECONDS = 5.0
# O intervalo depois de uma rodada `done` das regras dos relatórios e avisos.
REPORT_DONE_GAP = 1.0

# Os tipos das regras (os mesmos valores das constantes dos módulos de domínio).
_VALIDATE = "dispatch.validate"
_EXPIRE = "dispatch.expire"
_OPEN = "dispatch.open"
_CLOSE = "dispatch.close"
_PREPARE = "delivery.prepare"
_GRADE = "submission.grade"
_REVEAL = "delivery.reveal"
_UPDATE = "telegram.update"


@dataclass(frozen=True)
class SweepRule:
    """Uma regra da varredura: o tipo de tarefa, a chave do assunto no payload, a consulta dos
    assuntos vencidos, o teto de rodadas `failed` (`None`: sem teto), o intervalo depois de uma `done`
    e o que fazer ao esgotar o teto (na transação do assunto)."""

    task_type: str
    subject_key: str
    due_sql: str
    max_rounds: int | None
    done_gap: float | None = None
    on_exhausted: Callable[[psycopg.Connection, int, datetime], None] | None = None


@dataclass(frozen=True)
class SweepResult:
    """O que uma rodada fez: tarefas enfileiradas por tipo e assuntos abandonados."""

    enqueued: Mapping[str, int] = field(default_factory=dict)
    abandoned: int = 0


# ---------------------------------------------------------------------------------------------
# As consultas dos assuntos vencidos
# ---------------------------------------------------------------------------------------------


def _due_sql(*, task_type: str, subject_key: str, source: str, condition: str,
             lock: str = "FOR NO KEY UPDATE", done_gap_waived: str | None = None) -> str:
    """A `due_sql` de uma regra. O assunto é `s` (a tabela travada); `condition` é o "vencido", com o
    `now` passado (`%(now)s`). Parâmetros: `now`, `limit`, `subject` (`NULL` = todos; um `id` = só ele),
    `retry_gap` e `done_gap` (segundos). Os dois filtros ficam antes do `ORDER BY … LIMIT`:
    - **sem tarefa viva**: nenhuma `pending`/`leased` do tipo com `payload @> {<chave>: s.id}`;
    - **fora do intervalo**: a última terminada do tipo para o assunto tem `finished_at <= now − gap`,
      com `gap = retry_gap` depois de uma `failed` e `done_gap` depois de uma `done`.

    `done_gap_waived` (opcional; uma condição sobre `s` com o `now` passado) dispensa o `done_gap` depois
    de uma `done` enquanto vale agora; o `retry_gap` depois de uma `failed` nunca é dispensado.

    `task_type`, `subject_key` e `done_gap_waived` são constantes deste módulo, nunca entrada de fora."""
    subject = f"jsonb_build_object('{subject_key}', s.id)"
    waived = "" if done_gap_waived is None else f"    AND NOT (last.state = 'done' AND ({done_gap_waived})) "
    return (
        f"SELECT s.id FROM {source} "
        f"WHERE ({condition}) "
        "AND (%(subject)s::bigint IS NULL OR s.id = %(subject)s::bigint) "
        f"AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.task_type = '{task_type}' "
        f"  AND t.state IN ('pending', 'leased') AND t.payload @> {subject}) "
        "AND NOT EXISTS (SELECT 1 FROM ("
        f"  SELECT t.state, t.finished_at FROM tasks t WHERE t.task_type = '{task_type}' "
        f"    AND t.state IN ('done', 'failed') AND t.payload @> {subject} "
        "    ORDER BY t.finished_at DESC NULLS LAST, t.id DESC LIMIT 1) AS last "
        "  WHERE last.finished_at > %(now)s - make_interval(secs => CASE WHEN last.state = 'failed' "
        "    THEN %(retry_gap)s::float8 ELSE %(done_gap)s::float8 END) "
        f"{waived}) "
        f"ORDER BY s.id LIMIT %(limit)s {lock} OF s SKIP LOCKED"
    )


def _dispatch_rule(task_type: str, condition: str, *, done_gap_waived: str | None = None,
                   **kwargs: Any) -> SweepRule:
    return SweepRule(task_type=task_type, subject_key="dispatch_row_id",
                     due_sql=_due_sql(task_type=task_type, subject_key="dispatch_row_id",
                                      source="dispatches s", condition=condition,
                                      done_gap_waived=done_gap_waived), **kwargs)


def _delivery_rule(task_type: str, condition: str) -> SweepRule:
    return SweepRule(task_type=task_type, subject_key="delivery_row_id",
                     due_sql=_due_sql(task_type=task_type, subject_key="delivery_row_id",
                                      source="deliveries s JOIN dispatches p ON p.id = s.dispatch_id",
                                      condition=condition), max_rounds=None)


# ---------------------------------------------------------------------------------------------
# O que fazer ao esgotar o teto (na transação do assunto, com a linha travada)
# ---------------------------------------------------------------------------------------------


def _reject_exhausted_validation(conn: psycopg.Connection, dispatch_row_id: int, now: datetime) -> None:
    """Esgotadas as `VALIDATE_MAX_ROUNDS` rodadas `failed`, `validating` →
    `rejected/internal_failure` (ou `cancelled/validation_overran` no agendado com `now >= opens_at`)."""
    from cq_server import dispatches  # só aqui: o domínio não é carregado com o agendador

    dispatches.reject_validation_exhausted(conn, dispatch_row_id, now=now)


def _abandon_update(conn: psycopg.Connection, update_row_id: int, now: datetime) -> None:
    """Esgotadas as `SWEEP_MAX_UPDATE_ROUNDS` rodadas `failed`, o update vira `processed` com
    `outcome = 'abandoned'` e `processed_at = now`, sem evento, consultável por (`bot_id`,
    `update_id`). O texto de um `/convite` vira a marca, como no processamento."""
    from cq_server import students  # só aqui: a máscara do `/convite` é do domínio dos alunos

    row = conn.execute("SELECT text, command FROM telegram_updates WHERE id = %s AND state = 'pending'",
                       (update_row_id,)).fetchone()
    if row is None:
        return
    masked = students.invite_request_mask(row[0], row[1])
    conn.execute(
        "UPDATE telegram_updates SET state = 'processed', outcome = 'abandoned', processed_at = %s, "
        "text = coalesce(%s, text) WHERE id = %s AND state = 'pending'",
        (now, masked, update_row_id),
    )


# A validação vencida: `validating`. [I] O agendado com a abertura já passada
# só entra se já teve alguma tarefa de validação (a rodada seguinte o leva a `validation_overran`, ou o
# teto à rejeição): no fluxo real a primeira tarefa nasce no preparo, na mesma transação do
# disparo, então a condição só deixa de fora o agendado vencido montado sem tarefa nenhuma, que
# não tem validação a retomar.
_VALIDATE_DUE = (
    "s.state = 'validating' AND (s.immediate OR s.opens_at > %(now)s OR EXISTS (SELECT 1 FROM tasks v "
    f"WHERE v.task_type = '{_VALIDATE}' AND v.payload @> jsonb_build_object('dispatch_row_id', s.id)))"
)

# A barreira do fechamento já valendo no `now` passado, com a definição do
# tratador (`dispatches._CloseHandler._barrier`, que a reconfere com o bot `FOR SHARE`): nenhum bot do
# oferecimento do disparo, ou (a) nenhum update `pending` do bot com `received_at <= closes_at` e (b)
# `last_poll_ok_at >= closes_at` ou o bot fora de consulta (sem posse viva, `no_access` ou oferecimento
# fora de `offerings.POLLED_STATES`). Com ela, a rodada seguinte não espera o `done_gap`: no horário, a
# primeira rodada pode correr antes da consulta do leitor que grava `last_poll_ok_at >= closes_at` e
# terminar `done` sem efeito; a consulta chega logo depois, e a rodada seguinte, já com a barreira
# valendo, fecha (logo depois do horário). Sem repetição em laço: a rodada que encontra a
# barreira valendo fecha o disparo, que deixa de vencer; se o tratador ainda a vir presa (a posse do
# leitor renovada entre a varredura e o tratador), a varredura também a vê presa e volta ao `done_gap`.
_CLOSE_BARRIER_HOLDS = (
    "NOT EXISTS (SELECT 1 FROM bots b JOIN offerings o ON o.id = b.offering_id "
    "WHERE b.offering_id = s.offering_id AND ("
    "EXISTS (SELECT 1 FROM telegram_updates u WHERE u.bot_id = b.id AND u.state = 'pending' "
    "AND u.received_at <= s.closes_at) "
    "OR (b.state = 'registered' AND b.lease_owner IS NOT NULL AND b.lease_until IS NOT NULL "
    "AND b.lease_until >= %(now)s AND o.state IN (" + ", ".join(f"'{state}'" for state in POLLED_STATES) + ") "
    "AND (b.last_poll_ok_at IS NULL OR b.last_poll_ok_at < s.closes_at))))"
)

# A revelação devida, sobre a entrega `s` e o disparo `p`: nunca `not_delivered`
# nem `question_failed`, nem com correção pendente (essa a `submission.grade` revela no desfecho); nunca
# `no_answer` (ela recebe só a edição de fechamento, e a revelação chega com a atrasada válida ou o
# "Não sei" atrasado). Constante própria, para o `purge` contar
# o assunto devido sem duplicar o SQL; a `due_sql` da regra `delivery.reveal` usa a mesma.
_REVEAL_DUE = (
    "p.state = 'closed' AND s.revealed_at IS NULL "
    "AND s.state IN ('answered', 'dont_know', 'answered_late') "
    "AND NOT EXISTS (SELECT 1 FROM submissions g WHERE g.delivery_id = s.id "
    "AND g.outcome = 'grading')"
)

# As regras da entrega das questões, na ordem em que a varredura as percorre.
DISPATCH_RULES: tuple[SweepRule, ...] = (
    _dispatch_rule(_VALIDATE, _VALIDATE_DUE, max_rounds=VALIDATE_MAX_ROUNDS,
                   on_exhausted=_reject_exhausted_validation),
    _dispatch_rule(_EXPIRE, "s.state = 'scheduled' AND s.confirmed_at IS NULL "
                            "AND s.confirm_deadline <= %(now)s", max_rounds=None),
    _dispatch_rule(_OPEN, "s.state = 'scheduled' AND s.confirmed_at IS NOT NULL AND s.opens_at <= %(now)s",
                   max_rounds=None),
    _dispatch_rule(_CLOSE, "s.state = 'open' AND s.closes_at <= %(now)s", max_rounds=None,
                   done_gap=CLOSE_RECHECK_SECONDS, done_gap_waived=_CLOSE_BARRIER_HOLDS),
    _delivery_rule(_PREPARE, "s.state = 'pending' AND s.variant_at IS NULL AND p.state = 'open'"),
    SweepRule(task_type=_GRADE, subject_key="submission_row_id",
              due_sql=_due_sql(task_type=_GRADE, subject_key="submission_row_id", source="submissions s",
                               condition="s.outcome = 'grading'"), max_rounds=None),
    # A revelação devida (`_REVEAL_DUE`, acima).
    _delivery_rule(_REVEAL, _REVEAL_DUE),
    # O update `pending` de bot de oferecimento com disparo aberto ou fechado; os demais,
    # sem disparo, seguem só com a tarefa da recepção.
    SweepRule(task_type=_UPDATE, subject_key="update_row_id",
              due_sql=_due_sql(task_type=_UPDATE, subject_key="update_row_id", source="telegram_updates s",
                               condition="s.state = 'pending' AND EXISTS (SELECT 1 FROM bots b "
                                         "JOIN dispatches p ON p.offering_id = b.offering_id "
                                         "WHERE b.id = s.bot_id AND p.state IN ('open', 'closed'))",
                               # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs das tabelas que
                               # a referenciam.
                               lock="FOR NO KEY UPDATE"),
              max_rounds=SWEEP_MAX_UPDATE_ROUNDS, done_gap=CLOSE_RECHECK_SECONDS, on_exhausted=_abandon_update),
)


# Os tipos das regras dos relatórios e avisos (os mesmos valores das constantes de `reports` e de
# `teamnotices`).
_REPORT_FREEZE = "report.freeze"
_NOTICE_SEND = "notice.send"
# `reports.FREEZE_WAIT_SECONDS` (o valor repetido aqui, sem importar o domínio dos relatórios).
_FREEZE_WAIT_SECONDS = 600

# O congelamento vence: disparo `closed`, sem versão `closing`, e (nenhuma
# submissão `grading` no prazo nas entregas dele, ou `closed_at + FREEZE_WAIT_SECONDS <= now`). Sem piso:
# todo disparo fechado congela (o piso só decide o `not_sent` do aviso, no tratador).
_FREEZE_DUE = (
    "s.state = 'closed' AND NOT EXISTS (SELECT 1 FROM dispatch_reports r WHERE r.dispatch_id = s.id "
    "AND r.trigger = 'closing') AND (NOT EXISTS (SELECT 1 FROM submissions g "
    "JOIN deliveries d ON d.id = g.delivery_id WHERE d.dispatch_id = s.id AND g.outcome = 'grading' "
    f"AND NOT g.late) OR s.closed_at + make_interval(secs => {_FREEZE_WAIT_SECONDS}) <= %(now)s)"
)

_NOTICE_DISPATCH_REJECTED = "notice.dispatch_rejected"
_NOTICE_DISPATCH_CANCELLED = "notice.dispatch_cancelled"
_NOTICE_DISPATCH_OPENED = "notice.dispatch_opened"
# `teamnotices.OPEN_NOTICE_WAIT_SECONDS` (o valor repetido aqui, sem importar o domínio).
_OPEN_NOTICE_WAIT_SECONDS = 600


def _dispatch_fact(event_type: str) -> str:
    """O fato do disparo depois do deploy (piso por fato): o evento da transição com
    `seq > event_floor`, pelo índice `events_dispatch_fact_idx` (`data->>'dispatch_id'`, `type`, `seq`)."""
    return (f"EXISTS (SELECT 1 FROM events e WHERE e.data->>'dispatch_id' = s.dispatch_uuid::text "
            f"AND e.type = '{event_type}' AND e.seq > (SELECT f.event_floor FROM team_notice_floors f "
            "WHERE f.id = 1))")


def _no_dispatch_notice(kind: str) -> str:
    """"Sem aviso": nenhum aviso do tipo para o disparo, em qualquer estado."""
    return (f"NOT EXISTS (SELECT 1 FROM team_notices n WHERE n.offering_id = s.offering_id "
            f"AND n.kind = '{kind}' AND n.subject_key = 'dispatch:' || s.id::text)")


# O rejeitado: `rejected`, o `dispatch.rejected` acima do piso, sem aviso.
_NOTICE_REJECTED_DUE = (
    f"s.state = 'rejected' AND {_dispatch_fact('dispatch.rejected')} AND {_no_dispatch_notice('dispatch_rejected')}"
)
# O cancelado: `cancelled` por motivo automático (nunca `operator` nem
# `offering_closed`), o `dispatch.cancelled` acima do piso, sem aviso.
_NOTICE_CANCELLED_DUE = (
    "s.state = 'cancelled' AND s.cancel_reason IN ('expired', 'window_missed', 'validation_overran') "
    f"AND {_dispatch_fact('dispatch.cancelled')} AND {_no_dispatch_notice('dispatch_cancelled')}"
)
# O aberto: `open`, o `dispatch.opened` acima do piso, sem aviso, e
# (nenhuma entrega `pending`, o que inclui o alvo vazio, ou `opened_at + 600 s <= now`).
_NOTICE_OPENED_DUE = (
    f"s.state = 'open' AND {_dispatch_fact('dispatch.opened')} AND {_no_dispatch_notice('dispatch_opened')} "
    "AND (NOT EXISTS (SELECT 1 FROM deliveries x WHERE x.dispatch_id = s.id AND x.state = 'pending') "
    f"OR s.opened_at + make_interval(secs => {_OPEN_NOTICE_WAIT_SECONDS}) <= %(now)s)"
)

_NOTICE_QUESTION_FAILED = "notice.question_failed"
_NOTICE_ACCESS_EPISODE = "notice.access_episode"
_NOTICE_INVITE_BATCH = "notice.invite_batch"
_NOTICE_INVITE_WINDOW = "notice.invite_window"
# `teamnotices.INVITE_FAILURE_WINDOW_SECONDS` (o valor repetido aqui, sem importar o domínio).
_INVITE_FAILURE_WINDOW_SECONDS = 900

_EVENT_FLOOR = "(SELECT f.event_floor FROM team_notice_floors f WHERE f.id = 1)"
_INVITE_SEND_FLOOR = "(SELECT f.invite_send_floor FROM team_notice_floors f WHERE f.id = 1)"

# A falha de questão: existe entrega `question_failed/grading_broken` do disparo
# cujo `delivery.question_failed` tem `seq > event_floor` (piso por fato; pelo índice
# `events_dispatch_fact_idx`), sem aviso. A falha de preparação (`variant_broken` e afins) não avisa.
_NOTICE_QUESTION_FAILED_DUE = (
    "EXISTS (SELECT 1 FROM deliveries x JOIN events e ON e.data->>'dispatch_id' = s.dispatch_uuid::text "
    "AND e.type = 'delivery.question_failed' AND e.data->>'delivery_id' = x.delivery_uuid::text "
    f"WHERE x.dispatch_id = s.id AND x.state = 'question_failed' AND x.failure = 'grading_broken' "
    f"AND e.seq > {_EVENT_FLOOR}) AND {_no_dispatch_notice('question_failed')}"
)

# O episódio de bot sem acesso: existe `bot.access_changed` `lost` do
# oferecimento sem aviso `episode:<seq>`, e (o primeiro `restored` posterior com `seq > event_floor`, ou sem
# `restored`, `lost.seq > event_floor` e o oferecimento `archived`). Pelo índice `events_access_changed_idx`.
_ACCESS_OF_OFFERING = ("{a}.type = 'bot.access_changed' AND {a}.data->>'offering_id' = s.offering_id::text "
                       "AND {a}.data->>'access' = '{access}'")
_NOTICE_ACCESS_EPISODE_DUE = (
    f"EXISTS (SELECT 1 FROM events l LEFT JOIN LATERAL (SELECT min(r.seq) AS seq FROM events r "
    f"WHERE {_ACCESS_OF_OFFERING.format(a='r', access='restored')} AND r.seq > l.seq) AS back ON true "
    f"WHERE {_ACCESS_OF_OFFERING.format(a='l', access='lost')} "
    "AND NOT EXISTS (SELECT 1 FROM team_notices n WHERE n.offering_id = s.id AND n.kind = 'bot_access_restored' "
    "AND n.subject_key = 'episode:' || l.seq::text) "
    f"AND (back.seq > {_EVENT_FLOOR} OR (back.seq IS NULL AND l.seq > {_EVENT_FLOOR} AND s.state = 'archived')))"
)

# O lote de convites: sem envio `pending` no lote, ao menos um `failed`, todos
# os envios com `id > invite_send_floor`, sem aviso `batch:<id>`. O envio `pending` cuja tarefa
# `invite.email` (`students.INVITE_EMAIL_TASK_TYPE`, repetido aqui sem importar o domínio) terminou `failed`
# conta como `failed` (o mesmo predicado do `send_failed` da adesão, `students._ADHESION_STATE_SQL`):
# sem isso o lote nunca terminaria e o aviso se perderia.
_INVITE_TASK_FAILED = ("EXISTS (SELECT 1 FROM tasks t WHERE t.idempotency_key = 'invite.email:' || x.id "
                       "AND t.state = 'failed')")
_NOTICE_INVITE_BATCH_DUE = (
    "NOT EXISTS (SELECT 1 FROM invite_sends x WHERE x.batch_id = s.id AND x.state = 'pending' "
    f"AND NOT {_INVITE_TASK_FAILED}) "
    "AND EXISTS (SELECT 1 FROM invite_sends x WHERE x.batch_id = s.id AND (x.state = 'failed' "
    f"OR (x.state = 'pending' AND {_INVITE_TASK_FAILED}))) "
    f"AND NOT EXISTS (SELECT 1 FROM invite_sends x WHERE x.batch_id = s.id AND x.id <= {_INVITE_SEND_FLOOR}) "
    "AND NOT EXISTS (SELECT 1 FROM team_notices n WHERE n.offering_id = s.offering_id "
    "AND n.kind = 'invite_failures' AND n.subject_key = 'batch:' || s.id::text)"
)

# A janela das falhas avulsas: existe falha da coordenação ou do autosserviço do
# oferecimento, com `id > invite_send_floor`, não coberta por uma janela já registrada (`window:<id da
# primeira falha>`, que cobre `[finished_at, finished_at + 900 s)` dela), cuja `finished_at + 900 s <= now`.
_INVITE_WINDOW = f"make_interval(secs => {_INVITE_FAILURE_WINDOW_SECONDS})"
_NOTICE_INVITE_WINDOW_DUE = (
    "EXISTS (SELECT 1 FROM invite_sends f JOIN enrollments e ON e.id = f.enrollment_id "
    "WHERE e.offering_id = s.id AND f.state = 'failed' AND f.origin IN ('coordination', 'self_service') "
    f"AND f.id > {_INVITE_SEND_FLOOR} AND f.finished_at + {_INVITE_WINDOW} <= %(now)s "
    "AND NOT EXISTS (SELECT 1 FROM team_notices n JOIN invite_sends w ON n.subject_key = 'window:' || w.id::text "
    "WHERE n.offering_id = s.id AND n.kind = 'invite_failures' AND f.finished_at >= w.finished_at "
    f"AND f.finished_at < w.finished_at + {_INVITE_WINDOW}))"
)


def _offering_rule(task_type: str, condition: str) -> SweepRule:
    """Uma regra dos relatórios e avisos cujo assunto é o oferecimento. FOR NO KEY UPDATE: `offerings` é
    referenciada por FK."""
    return SweepRule(task_type=task_type, subject_key="offering_row_id",
                     due_sql=_due_sql(task_type=task_type, subject_key="offering_row_id", source="offerings s",
                                      condition=condition),
                     max_rounds=None, done_gap=REPORT_DONE_GAP)


_PREVIEW_SEND = "preview.send"
# `previews.PREVIEW_MAX_ROUNDS` (o valor repetido aqui, sem importar o domínio).
_PREVIEW_MAX_ROUNDS = 5
# A prévia vence: nenhum aviso `dispatch_preview` do disparo da amostra, em
# qualquer estado (pela chave única `(offering_id, kind, subject_key)`). Sem condição de estado do disparo:
# fora de `scheduled`, o tratador registra `gave_up`.
_PREVIEW_DUE = (
    "NOT EXISTS (SELECT 1 FROM dispatches p JOIN team_notices n ON n.offering_id = p.offering_id "
    "AND n.kind = 'dispatch_preview' AND n.subject_key = 'dispatch:' || p.id::text WHERE p.id = s.dispatch_id)"
)


def _give_up_preview(conn: psycopg.Connection, preview_row_id: int, now: datetime) -> None:
    """Esgotadas as `PREVIEW_MAX_ROUNDS` rodadas `failed`, o aviso `gave_up` com o
    `notice.recorded`, sem mensagem."""
    from cq_server import previews  # só aqui: o domínio da prévia não é carregado com o agendador

    previews.give_up(conn, preview_row_id, now)


_DIGEST_DAILY = "digest.daily"
# O resumo diário vence: ligado, com o horário devido passado, e o oferecimento ativo ou
# encerrado (em rascunho e arquivado, nada).
_DIGEST_DUE = "s.enabled AND s.next_due_at <= %(now)s AND o.state IN ('active', 'closed')"


# As regras dos relatórios e avisos, nesta ordem, todas com `done_gap = REPORT_DONE_GAP`; só a
# `preview.send` tem teto. São `report.freeze`, `notice.send`, as 3 regras de disparo, as 4
# detecções do oferecimento, `preview.send` e `digest.daily`.
REPORT_RULES: tuple[SweepRule, ...] = (
    _dispatch_rule(_REPORT_FREEZE, _FREEZE_DUE, max_rounds=None, done_gap=REPORT_DONE_GAP),
    # O aviso `pending` (o índice parcial `team_notices_pending_idx`). FOR NO KEY UPDATE: `team_notices` é
    # referenciada por FK (`outbox_messages.notice_id`).
    SweepRule(task_type=_NOTICE_SEND, subject_key="notice_row_id",
              due_sql=_due_sql(task_type=_NOTICE_SEND, subject_key="notice_row_id", source="team_notices s",
                               condition="s.state = 'pending'"),
              max_rounds=None, done_gap=REPORT_DONE_GAP),
    _dispatch_rule(_NOTICE_DISPATCH_REJECTED, _NOTICE_REJECTED_DUE, max_rounds=None, done_gap=REPORT_DONE_GAP),
    _dispatch_rule(_NOTICE_DISPATCH_CANCELLED, _NOTICE_CANCELLED_DUE, max_rounds=None, done_gap=REPORT_DONE_GAP),
    _dispatch_rule(_NOTICE_DISPATCH_OPENED, _NOTICE_OPENED_DUE, max_rounds=None, done_gap=REPORT_DONE_GAP),
    _dispatch_rule(_NOTICE_QUESTION_FAILED, _NOTICE_QUESTION_FAILED_DUE, max_rounds=None, done_gap=REPORT_DONE_GAP),
    _offering_rule(_NOTICE_ACCESS_EPISODE, _NOTICE_ACCESS_EPISODE_DUE),
    # O lote (`invite_batches`). FOR NO KEY UPDATE: a tabela é referenciada por FK (`invite_sends.batch_id`;
    # por isso não `FOR UPDATE`).
    SweepRule(task_type=_NOTICE_INVITE_BATCH, subject_key="batch_row_id",
              due_sql=_due_sql(task_type=_NOTICE_INVITE_BATCH, subject_key="batch_row_id",
                               source="invite_batches s", condition=_NOTICE_INVITE_BATCH_DUE),
              max_rounds=None, done_gap=REPORT_DONE_GAP),
    _offering_rule(_NOTICE_INVITE_WINDOW, _NOTICE_INVITE_WINDOW_DUE),
    # A amostra (`dispatch_previews`). FOR NO KEY UPDATE: a tabela é referenciada por FK
    # (`dispatch_preview_contents.preview_id`).
    SweepRule(task_type=_PREVIEW_SEND, subject_key="preview_row_id",
              due_sql=_due_sql(task_type=_PREVIEW_SEND, subject_key="preview_row_id", source="dispatch_previews s",
                               condition=_PREVIEW_DUE),
              max_rounds=_PREVIEW_MAX_ROUNDS, done_gap=REPORT_DONE_GAP, on_exhausted=_give_up_preview),
    # A configuração (`offering_digests`), com o oferecimento só lido (a trava é `OF s`). FOR NO KEY UPDATE
    # (por uniformidade).
    SweepRule(task_type=_DIGEST_DAILY, subject_key="digest_row_id",
              due_sql=_due_sql(task_type=_DIGEST_DAILY, subject_key="digest_row_id",
                               source="offering_digests s JOIN offerings o ON o.id = s.offering_id",
                               condition=_DIGEST_DUE),
              max_rounds=None, done_gap=REPORT_DONE_GAP),
)


# ---------------------------------------------------------------------------------------------
# A rodada
# ---------------------------------------------------------------------------------------------


def _failed_rounds(conn: psycopg.Connection, task_type: str, subject: Mapping[str, Any]) -> int:
    """Quantas tarefas do tipo para o assunto terminaram `failed` (o teto conta só essas)."""
    row = conn.execute(
        "SELECT count(*) FROM tasks WHERE task_type = %s AND state = 'failed' AND payload @> %s",
        (task_type, Jsonb(dict(subject))),
    ).fetchone()
    return int(row[0])


def _sweep_subject(conn: psycopg.Connection, rule: SweepRule, subject_id: int, params: dict[str, Any],
                   now: datetime) -> str | None:
    """A transação curta de um assunto: `"enqueued"`, `"abandoned"` ou `None` (já não vence, está
    travado por outra transação, ou ganhou tarefa viva)."""
    one = {**params, "subject": subject_id, "limit": 1}
    with conn.transaction():
        if conn.execute(rule.due_sql, one).fetchone() is None:  # trava (ou pula) a linha
            return None
        # Outro comando, outro instantâneo: reconfere os dois filtros sob a trava.
        if conn.execute(rule.due_sql, one).fetchone() is None:
            return None
        subject = {rule.subject_key: subject_id}
        if rule.max_rounds is not None and _failed_rounds(conn, rule.task_type, subject) >= rule.max_rounds:
            if rule.on_exhausted is None:
                return None
            rule.on_exhausted(conn, subject_id, now)
            return "abandoned"
        finished, _last = finished_rounds(conn, rule.task_type, subject)
        enqueue_in_transaction(conn, rule.task_type, subject, now=now,
                               idempotency_key=f"{rule.task_type}:{subject_id}:r{finished + 1}")
        return "enqueued"


def sweep_once(conn: psycopg.Connection, *, now: datetime, retry_gap: float, rules: Sequence[SweepRule],
               limit: int = SWEEP_BATCH) -> SweepResult:
    """Uma rodada da varredura sobre `conn` (ver o docstring do módulo). `conn` fica fora de transação
    entre os assuntos (a do worker é `autocommit`); `retry_gap` é o `backoff_max_seconds`."""
    enqueued: dict[str, int] = {}
    abandoned = 0
    for rule in rules:
        done_gap = float(rule.done_gap) if rule.done_gap is not None else float(retry_gap)
        params = {"now": now, "retry_gap": float(retry_gap), "done_gap": done_gap}
        due = [int(row[0]) for row in conn.execute(rule.due_sql,
                                                    {**params, "subject": None, "limit": limit}).fetchall()]
        for subject_id in due:
            outcome = _sweep_subject(conn, rule, subject_id, params, now)
            if outcome == "enqueued":
                enqueued[rule.task_type] = enqueued.get(rule.task_type, 0) + 1
            elif outcome == "abandoned":
                abandoned += 1
    if enqueued or abandoned:
        logger.info("scheduler.swept", extra={"event": "scheduler.swept", "enqueued": dict(enqueued),
                                              "abandoned": abandoned})
    return SweepResult(enqueued=enqueued, abandoned=abandoned)


class Sweeper:
    """A varredura no laço do worker (ver o docstring do módulo): só as regras `DISPATCH_RULES + REPORT_RULES` dos tipos
    em `handled_types`; uma rodada a cada `interval` segundos reais (a primeira, na primeira chamada);
    conexão própria, aberta no primeiro uso e reaberta se cair. Usado só pela thread do laço."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, handled_types: Collection[str],
                 interval: float = SWEEP_SECONDS) -> None:
        self._conninfo = conninfo
        self._clock = clock
        self._settings = settings
        self._handled_types = frozenset(handled_types)
        self._interval = interval
        self._rules = tuple(rule for rule in DISPATCH_RULES + REPORT_RULES if rule.task_type in self._handled_types)
        self._db = LazyConnection(conninfo)
        self._last_run: float | None = None

    def maybe_run(self) -> SweepResult | None:
        """Roda uma rodada se já passou `interval` desde a anterior; senão `None`. Uma falha (ex.: o
        banco caiu) sobe ao laço, e a próxima tentativa só vem depois de `interval`."""
        started = time.monotonic()
        if self._last_run is not None and started - self._last_run < self._interval:
            return None
        self._last_run = started
        if not self._rules:
            return SweepResult()
        return sweep_once(self._db.get(), now=self._clock.now(),
                          retry_gap=float(self._settings.backoff_max_seconds), rules=self._rules)

    def close(self) -> None:
        """Fecha a conexão da varredura (idempotente)."""
        self._db.close()
