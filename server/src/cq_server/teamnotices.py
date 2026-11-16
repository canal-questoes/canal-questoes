"""Avisos à equipe: registro, envio, detecção, tratadores e pisos. Cada aviso nasce numa fase (o
registro, com a fotografia em `data`) e é composto e enviado noutra (a entrega aos destinatários da
equipe), sem identidade nem texto livre gravados; os pisos evitam avisar de fatos anteriores ao
deploy.

- **Fase 1** (`record`): `INSERT … ON CONFLICT (offering_id, kind, subject_key) DO NOTHING RETURNING
  id` (`None` = já existia); o aviso nasce `pending`, sem evento.
- **Fase 2** (`deliver`): trava o aviso; fora de `pending` devolve o estado, sem efeito; compõe a
  partir de `data` (e, nos resumos de fechamento e de reenvio, do `content` da versão, pela porta
  `ReportRenderer`: `reportrender.ChannelSummaryRenderer`), nunca de um recálculo; lê os
  destinatários (`team_recipients`); enfileira uma mensagem por destinatário (por parte, no resumo
  diário longo) pela `outbox.enqueue_notice_text`; grava o estado final, `finished_at` e o
  evento `notice.recorded`.
- **Gancho do fechamento** (`record_opened_on_close`): só a fase 1 do "aberto" antecipado.
- **Tratadores** (`task_handlers`): `notice.send` e as 7 regras de detecção.

Importa `noticetexts`, `reportrender` e `outbox`; `reports` e `dispatches` só dentro das funções (sem
ciclo). Nunca HTTP nem `cq_server.adapters`; o canal é lido só na execução da tarefa.

**Fotografia (`data`) por tipo, lida pelo `deliver`** (as chaves que as regras de registro gravam; sem
identidade, sem texto livre): `dispatch_rejected` `{rejection}` (a classe; sem ela, a coluna do disparo);
`dispatch_cancelled` `{reason}` (idem, `cancel_reason`); `dispatch_opened` `{target, delivered,
not_delivered, question_failed, pending, closes_at}` (`opened_data`); `question_failed` `{}`;
`bot_access_restored` `{started_at, ended_at, not_delivered, pending, lost_notices, lost_summaries}`;
`invite_failures` `{sent, failed}` (lote) ou `{failed, window_start, window_end}` (janela). Horários em
UTC (ISO 8601 com `Z`), mostrados no fuso do oferecimento (`timezones.format_local`). O título, a
referência curta, o código e a disciplina são lidos das chaves na composição e redigidos nela.

Partes do módulo: `record`, `team_recipients`, `floors` e `deliver` (os tipos de texto, os resumos
pela porta e o resumo diário); o tratador `notice.send`; `record_opened_on_close` (o registro do
"aberto" antecipado; só a função, e o gancho que a chama fica no
fechamento, em `dispatches._close_locked`); as regras `notice.dispatch_rejected`, `notice.dispatch_cancelled`
e `notice.dispatch_opened` (`_DispatchNoticeHandler`); `finish_without_sending` (o `not_sent` do
resumo de fechamento abaixo do piso; os resumos `dispatch_closed` e `summary_resent` são compostos pelo
`deliver`, pela porta); as detecções `notice.question_failed`, `notice.access_episode`,
`notice.invite_batch` e `notice.invite_window` (`_DetectionHandler`); e a composição do `daily_digest`
(`_digest_parts`, em partes quando não cabe), cuja fotografia é `{adhesion, new_links,
invites_sent, invites_failed, late, dispatches}` (o tratador `digest.daily`, em `digests`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server import noticetexts, outbox, reportrender
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import Settings, redaction_secrets
from cq_server.events import EventType, append_event, load_write_identities
from cq_server.ports.delivery import DeliveryChannel, Formatted, MessageFormatter
from cq_server.redaction import KnownIdentity, redact
from cq_server.tasks import ClaimedTask, PermanentTaskError
from cq_server.timezones import format_local

if TYPE_CHECKING:
    from cq_server.dispatches import DeliveryCounts

# Os tipos de tarefa dos avisos (payloads com o `id` da linha do assunto).
SEND_TASK_TYPE = "notice.send"
DISPATCH_REJECTED_TASK_TYPE = "notice.dispatch_rejected"
DISPATCH_CANCELLED_TASK_TYPE = "notice.dispatch_cancelled"
DISPATCH_OPENED_TASK_TYPE = "notice.dispatch_opened"
QUESTION_FAILED_TASK_TYPE = "notice.question_failed"
ACCESS_EPISODE_TASK_TYPE = "notice.access_episode"
INVITE_BATCH_TASK_TYPE = "notice.invite_batch"
INVITE_WINDOW_TASK_TYPE = "notice.invite_window"
TASK_TYPES = (SEND_TASK_TYPE, DISPATCH_REJECTED_TASK_TYPE, DISPATCH_CANCELLED_TASK_TYPE,
              DISPATCH_OPENED_TASK_TYPE, QUESTION_FAILED_TASK_TYPE, ACCESS_EPISODE_TASK_TYPE,
              INVITE_BATCH_TASK_TYPE, INVITE_WINDOW_TASK_TYPE)

# Constantes configuráveis no código: a espera do aviso de abertura, a janela das falhas e a prioridade.
OPEN_NOTICE_WAIT_SECONDS = 600
INVITE_FAILURE_WINDOW_SECONDS = 900
NOTICE_PRIORITY = 2


class NoticeKind(StrEnum):
    """Os 10 tipos de aviso."""

    DISPATCH_PREVIEW = "dispatch_preview"
    DISPATCH_REJECTED = "dispatch_rejected"
    DISPATCH_OPENED = "dispatch_opened"
    DISPATCH_CLOSED = "dispatch_closed"
    SUMMARY_RESENT = "summary_resent"
    DISPATCH_CANCELLED = "dispatch_cancelled"
    QUESTION_FAILED = "question_failed"
    BOT_ACCESS_RESTORED = "bot_access_restored"
    INVITE_FAILURES = "invite_failures"
    DAILY_DIGEST = "daily_digest"


class NoticeState(StrEnum):
    """Os estados de um aviso: `pending` e os 6 finais."""

    PENDING = "pending"
    QUEUED = "queued"
    NO_RECIPIENT = "no_recipient"
    NO_ACTIVITY = "no_activity"
    MISSED = "missed"
    NOT_SENT = "not_sent"
    GAVE_UP = "gave_up"


# Os estados finais (os do evento `notice.recorded`).
FINAL_STATES = (NoticeState.QUEUED, NoticeState.NO_RECIPIENT, NoticeState.NO_ACTIVITY, NoticeState.MISSED,
                NoticeState.NOT_SENT, NoticeState.GAVE_UP)
# Os prefixos de `subject_key` (`<prefixo>:<número ou data>`).
SUBJECT_PREFIXES = ("dispatch", "report", "episode", "batch", "window", "digest")


@dataclass(frozen=True)
class Recipient:
    """Um destinatário de aviso (matrícula `team` `active` + endereço `active` no bot do
    oferecimento); o `repr` só mostra o `person_row_id` (a identidade só sai pela revelação)."""

    person_row_id: int
    pseudo_id: uuid.UUID = field(repr=False)
    endpoint_row_id: int = field(repr=False)


@dataclass(frozen=True)
class Floors:
    """Os pisos gravados pela migração `0005`."""

    event_floor: int
    invite_send_floor: int



# O limite de markup de uma mensagem de aviso, contado em unidades UTF-16,
# como o Telegram conta (um ponto de código fora do plano básico vale 2).
MAX_MARKUP_UNITS = 4096
# A marca do campo variável cortado para caber.
ELLIPSIS = "…"
_NO_CHANNEL = "canal sem rota"
# O nome da chave do assunto no payload da tarefa `notice.send`.
SEND_SUBJECT_KEY = "notice_row_id"


def _utc_iso(moment: datetime) -> str:
    """O horário em UTC com `Z` (a forma dos horários em `data`)."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_moment(value: Any) -> datetime | None:
    """Um horário de `data` (texto ISO 8601 com fuso) ou `None`."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else None


def _count(data: Mapping[str, Any], key: str) -> int:
    """Uma contagem de `data` (ausente → 0; os números nunca são cortados)."""
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _utf16_units(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


# --------------------------------------------------------------------------------------------------
# Registro, leitura e pisos
# --------------------------------------------------------------------------------------------------


def record(conn: psycopg.Connection, *, offering_row_id: int, kind: NoticeKind, subject_key: str,
           data: Mapping[str, Any], now: datetime, dispatch_row_id: int | None = None,
           report_row_id: int | None = None, local_date: date | None = None) -> int | None:
    """Fase 1: o `id` do aviso novo, `pending`; `None` = já existia.

    `INSERT … ON CONFLICT (offering_id, kind, subject_key) DO NOTHING RETURNING id`, na transação de
    `conn`, sem evento (o `notice.recorded` sai só no estado final, na fase 2). `data` é a fotografia
    agregada (contagens, horários UTC, classe, motivo), sem identidade nem texto livre."""
    row = conn.execute(
        "INSERT INTO team_notices (offering_id, kind, subject_key, state, recipient_count, dispatch_id, "
        "report_id, local_date, data, created_at) VALUES (%s, %s, %s, 'pending', 0, %s, %s, %s, %s, %s) "
        "ON CONFLICT (offering_id, kind, subject_key) DO NOTHING RETURNING id",
        (offering_row_id, NoticeKind(kind).value, subject_key, dispatch_row_id, report_row_id, local_date,
         Jsonb(dict(data)), now),
    ).fetchone()
    return None if row is None else int(row[0])


def team_recipients(conn: psycopg.Connection, offering_row_id: int) -> list[Recipient]:
    """Os destinatários, na ordem de `enrollments.id`: matrícula `team` `active` com
    endereço `active` do canal `telegram` no bot do oferecimento. Leitura sem trava."""
    rows = conn.execute(
        "SELECT p.id, p.pseudo_id, d.id FROM enrollments e "
        "JOIN people p ON p.id = e.person_id "
        "JOIN bots b ON b.offering_id = e.offering_id "
        "JOIN delivery_endpoints d ON d.person_id = e.person_id AND d.bot_id = b.id "
        "AND d.state = 'active' AND d.channel = 'telegram' "
        "WHERE e.offering_id = %s AND e.role = 'team' AND e.state = 'active' "
        "ORDER BY e.id, d.id",
        (offering_row_id,),
    ).fetchall()
    return [Recipient(person_row_id=int(r[0]), pseudo_id=r[1] if isinstance(r[1], uuid.UUID) else uuid.UUID(str(r[1])),
                      endpoint_row_id=int(r[2])) for r in rows]


def floors(conn: psycopg.Connection) -> Floors:
    """A linha única de `team_notice_floors`."""
    row = conn.execute("SELECT event_floor, invite_send_floor FROM team_notice_floors WHERE id = 1").fetchone()
    if row is None:
        raise LookupError("team_notice_floors sem a linha única (migração 0005)")
    return Floors(event_floor=int(row[0]), invite_send_floor=int(row[1]))


def opened_data(counts: DeliveryCounts, closes_at: datetime | None) -> dict[str, Any]:
    """A fotografia do "aberto": alvo, entregues (a entrega que chegou ao aluno, em
    qualquer estado posterior), não entregues, em falha de questão, pendentes e `closes_at` em UTC."""
    delivered = counts.delivered + counts.answered + counts.dont_know + counts.no_answer + counts.answered_late
    target = delivered + counts.pending + counts.not_delivered + counts.question_failed
    return {"target": target, "delivered": delivered, "not_delivered": counts.not_delivered,
            "question_failed": counts.question_failed, "pending": counts.pending,
            "closes_at": None if closes_at is None else _utc_iso(closes_at)}


def record_opened_on_close(conn: psycopg.Connection, *, dispatch_row_id: int, offering_row_id: int,
                           counts: DeliveryCounts, now: datetime) -> bool:
    """O gancho do fechamento: registra o "aberto" antecipado com as contagens do
    instante; verdadeiro se registrou.

    Só a fase 1 (`pending`, sem evento), e só se o `dispatch.opened` do disparo tem `seq > event_floor`
    (abriu depois do deploy; piso por fato) e ainda não há aviso `dispatch_opened` (o
    `ON CONFLICT DO NOTHING` do `record`)."""
    row = conn.execute("SELECT dispatch_uuid, closes_at FROM dispatches WHERE id = %s",
                       (dispatch_row_id,)).fetchone()
    if row is None:
        return False
    floor = floors(conn).event_floor
    opened = conn.execute(
        "SELECT 1 FROM events WHERE type = 'dispatch.opened' AND data->>'dispatch_id' = %s AND seq > %s LIMIT 1",
        (str(row[0]), floor),
    ).fetchone()
    if opened is None:
        return False
    notice = record(conn, offering_row_id=offering_row_id, kind=NoticeKind.DISPATCH_OPENED,
                    subject_key=f"dispatch:{dispatch_row_id}", data=opened_data(counts, row[1]), now=now,
                    dispatch_row_id=dispatch_row_id)
    return notice is not None


# --------------------------------------------------------------------------------------------------
# Fase 2: composição e envio
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Notice:
    """A linha travada do aviso e o que a composição e o evento leem das chaves dele."""

    row_id: int
    offering_row_id: int
    kind: NoticeKind
    subject_key: str
    state: NoticeState
    dispatch_row_id: int | None
    report_row_id: int | None
    local_date: date | None
    data: Mapping[str, Any]


@dataclass(frozen=True)
class _Offering:
    offering_id: uuid.UUID
    code: str
    course: str
    timezone: str
    bot_row_id: int | None


def _lock_notice(conn: psycopg.Connection, notice_row_id: int) -> _Notice | None:
    # FOR NO KEY UPDATE: `team_notices` é referenciada por FK (`outbox_messages.notice_id`); não barra o
    # FOR KEY SHARE das inserções na fila.
    row = conn.execute(
        "SELECT id, offering_id, kind, subject_key, state, dispatch_id, report_id, local_date, data "
        "FROM team_notices WHERE id = %s FOR NO KEY UPDATE",
        (notice_row_id,),
    ).fetchone()
    if row is None:
        return None
    return _Notice(row_id=int(row[0]), offering_row_id=int(row[1]), kind=NoticeKind(row[2]), subject_key=str(row[3]),
                   state=NoticeState(row[4]), dispatch_row_id=None if row[5] is None else int(row[5]),
                   report_row_id=None if row[6] is None else int(row[6]), local_date=row[7],
                   data=row[8] if isinstance(row[8], Mapping) else {})


def _offering(conn: psycopg.Connection, offering_row_id: int) -> _Offering:
    row = conn.execute(
        "SELECT o.offering_id, o.code, o.course, o.timezone, b.id FROM offerings o "
        "LEFT JOIN bots b ON b.offering_id = o.id WHERE o.id = %s",
        (offering_row_id,),
    ).fetchone()
    if row is None:
        raise LookupError("oferecimento do aviso inexistente")
    return _Offering(offering_id=row[0], code=str(row[1]), course=str(row[2]), timezone=str(row[3]),
                     bot_row_id=None if row[4] is None else int(row[4]))


def _subject_number(notice: _Notice) -> int:
    """O número do assunto (`<prefixo>:<número>`); a data do `digest:` não passa por aqui."""
    return int(notice.subject_key.split(":", 1)[1])


def _event_subject(conn: psycopg.Connection, notice: _Notice) -> dict[str, Any]:
    """Os campos do assunto do `notice.recorded`, cada um `None` fora do seu tipo."""
    subject: dict[str, Any] = {"dispatch_id": None, "report_version": None, "episode_seq": None,
                               "batch_id": None, "window_started_at": None, "local_date": None}
    dispatch_row = notice.dispatch_row_id
    if notice.report_row_id is not None:
        report = conn.execute("SELECT dispatch_id, version FROM dispatch_reports WHERE id = %s",
                              (notice.report_row_id,)).fetchone()
        if report is not None:
            dispatch_row = int(report[0]) if dispatch_row is None else dispatch_row
            subject["report_version"] = int(report[1])
    if dispatch_row is not None:
        found = conn.execute("SELECT dispatch_uuid FROM dispatches WHERE id = %s", (dispatch_row,)).fetchone()
        subject["dispatch_id"] = None if found is None else found[0]
    prefix = notice.subject_key.split(":", 1)[0]
    if notice.kind is NoticeKind.BOT_ACCESS_RESTORED and prefix == "episode":
        subject["episode_seq"] = _subject_number(notice)
    elif prefix == "batch":
        found = conn.execute("SELECT batch_uuid FROM invite_batches WHERE id = %s",
                             (_subject_number(notice),)).fetchone()
        subject["batch_id"] = None if found is None else found[0]
    elif prefix == "window":
        found = conn.execute("SELECT finished_at FROM invite_sends WHERE id = %s",
                             (_subject_number(notice),)).fetchone()
        subject["window_started_at"] = None if found is None or found[0] is None else found[0]
    if notice.kind is NoticeKind.DAILY_DIGEST:
        subject["local_date"] = notice.local_date
    return subject


def _finish(conn: psycopg.Connection, notice: _Notice, offering: _Offering, state: NoticeState,
            recipient_count: int, now: datetime) -> NoticeState:
    """O estado final, `finished_at` e o `notice.recorded`, na transação da fase 2."""
    conn.execute("UPDATE team_notices SET state = %s, recipient_count = %s, finished_at = %s WHERE id = %s",
                 (state.value, recipient_count, now, notice.row_id))
    append_event(conn, EventType.NOTICE_RECORDED,
                 {"offering_id": offering.offering_id, "notice_type": notice.kind.value,
                  **_event_subject(conn, notice), "state": state.value, "recipient_count": recipient_count},
                 occurred_at=now)
    return state


def _fits(formatted: Formatted) -> bool:
    return formatted.fits and _utf16_units(formatted.markup) <= MAX_MARKUP_UNITS


def _compose(template: str, formatter: MessageFormatter, values: Mapping[str, str | int],
             free_field: str) -> Formatted:
    """O modelo composto pelo `noticetexts.render`; o campo variável `free_field` (já redigido) é cortado
    (um prefixo dele seguido de "…") até o texto caber no `fits` do formatador e em 4096 unidades de
    markup. Os demais campos (números, horários, códigos, comandos) nunca são cortados."""
    full = noticetexts.render(template, formatter, **values)
    if _fits(full):
        return full
    value = str(values[free_field])

    def attempt(size: int) -> Formatted:
        return noticetexts.render(template, formatter, **{**values, free_field: value[:size] + ELLIPSIS})

    low, high = 0, len(value) - 1
    best = attempt(0)
    if not _fits(best):
        raise ValueError("o aviso não cabe na mensagem nem com o campo variável vazio")
    while low < high:
        middle = (low + high + 1) // 2
        candidate = attempt(middle)
        if _fits(candidate):
            low, best = middle, candidate
        else:
            high = middle - 1
    return best


def _with_optional(template: str, *, before_last: Sequence[str] = (), drop_last: bool = False) -> str:
    """O modelo com as linhas opcionais antes da última (a do comando) ou sem a última (noticetexts)."""
    lines = template.split("\n")
    if drop_last:
        lines = lines[:-1]
    if before_last:
        lines[len(lines) - 1:len(lines) - 1] = list(before_last)
    return "\n".join(lines)


def _local(moment: Any, zone: str) -> str:
    parsed = _parse_moment(moment)
    return noticetexts.EMPTY if parsed is None else format_local(parsed, zone)


def _dispatch_values(conn: psycopg.Connection, dispatch_row_id: int | None, identities: Sequence[KnownIdentity],
                     secrets: Sequence[str | None] = ()) -> dict[str, Any]:
    """Os textos variáveis do disparo que `data` não guarda, redigidos aqui: o título (ou
    "questão sem título"), a referência curta `qid@commit_short` e o `dispatch_id`; e as colunas de estado
    (classe, motivo, `closes_at`) que completam a fotografia."""
    if dispatch_row_id is None:
        raise ValueError("aviso de disparo sem dispatch_id")
    row = conn.execute(
        "SELECT s.dispatch_uuid, q.title, q.qid, q.commit_sha, s.rejection, s.cancel_reason, s.closes_at "
        "FROM dispatches s JOIN question_refs q ON q.id = s.question_ref_id WHERE s.id = %s",
        (dispatch_row_id,),
    ).fetchone()
    if row is None:
        raise ValueError("disparo do aviso inexistente")
    from cq_server import dispatches  # só aqui: o domínio dos disparos não é carregado com o módulo (sem ciclo)

    title = (redact(row[1], identities, secrets=secrets) if isinstance(row[1], str) and row[1]
             else noticetexts.UNTITLED)
    qid = dispatches._redact_operator_text(str(row[2]), identities, secrets)
    return {"dispatch_id": str(row[0]), "title": title, "reference": f"{qid}@{str(row[3])[:12]}",
            "rejection": row[4], "cancel_reason": row[5], "closes_at": row[6]}


def _compose_text(conn: psycopg.Connection, notice: _Notice, offering: _Offering, formatter: MessageFormatter,
                  secrets: Sequence[str | None] = ()) -> Formatted:
    """O texto de um aviso de texto, a partir de `data` e das chaves do aviso. Todo
    texto variável sai redigido com as identidades e os `secrets` configurados (o token inicial;
    regra de privacidade)."""
    data = notice.data
    kind = notice.kind
    if kind in (NoticeKind.DISPATCH_CLOSED, NoticeKind.SUMMARY_RESENT):
        # Pela porta: o `content` da versão, nunca um recálculo.
        from cq_server import reports  # só aqui (sem ciclo)

        found = conn.execute("SELECT content, version, trigger, frozen_at FROM dispatch_reports WHERE id = %s",
                             (notice.report_row_id,)).fetchone()
        if found is None:
            raise ValueError("versão do relatório do aviso inexistente")
        # O `content` guardado tem `frozen` nulo. No fechamento, o resumo é renderizado sobre
        # ele tal como está (`CLOSED_SUMMARY` não usa a versão). Só o reenvio preenche `frozen` com
        # a própria linha da versão (o resumo de reenvio mostra o número da versão, `RESENT_SUMMARY`).
        # Identidades depois do aviso travado (ordem: aviso → destinatários → identidades →
        # composição), e o `content` redigido de novo com elas antes da composição (regra de
        # privacidade), como na releitura pela API: quem se cadastrou entre o congelamento e esta composição (o reenvio
        # congela numa transação e o `notice.send` compõe em outra) não sai em claro no título. No fechamento,
        # o congelamento já tomou o lock dos eventos na mesma transação; o lock é reentrante (sem nova espera).
        identities = load_write_identities(conn)
        report = reports._redact_report(reports.report_from_dict(found[0]), identities, secrets)
        if kind is NoticeKind.SUMMARY_RESENT:
            frozen = reports.FrozenRef(version=int(found[1]), trigger=str(found[2]),
                                       frozen_at=found[3].astimezone(timezone.utc))
            report = replace(report, frozen=frozen)
            renderer = reportrender.ChannelSummaryRenderer(formatter, timezone=offering.timezone,
                                                           offering_code=offering.code, resend=True)
        else:
            renderer = reportrender.ChannelSummaryRenderer(formatter, timezone=offering.timezone,
                                                           offering_code=offering.code)
        body = renderer.render(report).body
        return Formatted(markup=body, text_format=formatter.text_format, length=len(body), fits=True)
    if kind is NoticeKind.DAILY_DIGEST:
        raise ValueError("o resumo diário é composto em partes por _digest_parts")
    if kind is NoticeKind.DISPATCH_PREVIEW:
        raise ValueError("a prévia é composta e enfileirada pela tarefa preview.send")

    # Identidades depois do aviso travado (ordem: aviso → destinatários → identidades).
    identities = load_write_identities(conn)
    common: dict[str, Any] = {"code": offering.code}
    if kind in (NoticeKind.DISPATCH_REJECTED, NoticeKind.DISPATCH_OPENED, NoticeKind.DISPATCH_CANCELLED,
                NoticeKind.QUESTION_FAILED):
        dispatch = _dispatch_values(conn, notice.dispatch_row_id, identities, secrets)
        common.update(title=dispatch["title"], dispatch_id=dispatch["dispatch_id"])
        if kind is NoticeKind.DISPATCH_REJECTED:
            from cq_server import dispatches  # só aqui (sem ciclo)

            rejection = data.get("rejection") or dispatch["rejection"]
            reason = dispatches.REJECTION_MESSAGES[str(rejection)]
            return _compose(noticetexts.REJECTED, formatter,
                            {**common, "reference": dispatch["reference"], "reason": reason}, "title")
        if kind is NoticeKind.DISPATCH_CANCELLED:
            reason = noticetexts.CANCEL_REASONS[str(data.get("reason") or dispatch["cancel_reason"])]
            return _compose(noticetexts.CANCELLED, formatter, {**common, "reason": reason}, "title")
        if kind is NoticeKind.QUESTION_FAILED:
            return _compose(noticetexts.QUESTION_FAILED, formatter, common, "title")
        pending = _count(data, "pending")
        template = _with_optional(noticetexts.OPENED, before_last=(noticetexts.OPENED_PENDING,) if pending else ())
        closes_at = data.get("closes_at") or dispatch["closes_at"]
        return _compose(template, formatter,
                        {**common, "target": _count(data, "target"), "delivered": _count(data, "delivered"),
                         "not_delivered": _count(data, "not_delivered"),
                         "question_failed": _count(data, "question_failed"), "pending": pending,
                         "closes_at": _local(closes_at, offering.timezone)}, "title")

    common["course"] = redact(offering.course, identities, secrets=secrets)
    if kind is NoticeKind.BOT_ACCESS_RESTORED:
        lost_summaries = _count(data, "lost_summaries")
        template = _with_optional(noticetexts.ACCESS_RESTORED, drop_last=lost_summaries == 0)
        return _compose(template, formatter,
                        {**common, "started_at": _local(data.get("started_at"), offering.timezone),
                         "ended_at": _local(data.get("ended_at"), offering.timezone),
                         "not_delivered": _count(data, "not_delivered"), "pending": _count(data, "pending"),
                         "lost_notices": _count(data, "lost_notices"), "lost_summaries": lost_summaries}, "course")
    if kind is NoticeKind.INVITE_FAILURES:
        if notice.subject_key.startswith("batch:"):
            return _compose(noticetexts.INVITE_FAILURES_BATCH, formatter,
                            {**common, "sent": _count(data, "sent"), "failed": _count(data, "failed")}, "course")
        return _compose(noticetexts.INVITE_FAILURES_WINDOW, formatter,
                        {**common, "failed": _count(data, "failed"),
                         "window_start": _local(data.get("window_start"), offering.timezone),
                         "window_end": _local(data.get("window_end"), offering.timezone)}, "course")
    raise ValueError(f"tipo de aviso sem composição: {kind.value}")


# Resumo longo: o menor tamanho a que os títulos e a disciplina são cortados antes de dividir o
# resumo em partes.
DIGEST_TEXT_MIN = 40
# As chaves de `data` do `daily_digest` (a fotografia gravada pelo tratador `digest.daily`, `digests`).
_DIGEST_PAIRS = ("answered", "correct", "partially_correct", "dont_know")


def _clip(value: str, size: int | None) -> str:
    """O valor inteiro, ou os primeiros `size` caracteres seguidos de "…"."""
    if size is None or len(value) <= size:
        return value
    return value[:size] + ELLIPSIS


def _pair_of(value: Any) -> tuple[int, int]:
    """Um par `[no prazo, com atrasadas]` de `data` (inválido → `(0, 0)`)."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(v, int) and not isinstance(v, bool) for v in value):
        return int(value[0]), int(value[1])
    return 0, 0


def _digest_parts(conn: psycopg.Connection, notice: _Notice, offering: _Offering,
                  formatter: MessageFormatter, secrets: Sequence[str | None] = ()) -> list[Formatted]:
    """As mensagens do resumo diário, a partir de `data`: o
    `DAILY_DIGEST` com a adesão, os vínculos novos, as atrasadas e o número de disparos, seguido de uma
    `DIGEST_DISPATCH_LINE` por disparo, na ordem gravada. Os títulos são lidos de `dispatches` pelos
    `dispatch_id` de `data` e, com a disciplina, redigidos aqui. Se o texto não cabe numa mensagem, os
    títulos e a disciplina são cortados (no máximo até `DIGEST_TEXT_MIN` caracteres); se ainda não cabe, o
    resumo sai em partes de linhas inteiras (a primeira com o cabeçalho). Os números nunca são cortados."""
    data = notice.data
    identities = load_write_identities(conn)
    adhesion = data.get("adhesion") if isinstance(data.get("adhesion"), Mapping) else {}
    items = [item for item in data.get("dispatches") or () if isinstance(item, Mapping)]
    ids = [str(item.get("dispatch_id")) for item in items]
    titles: dict[str, str] = {}
    if ids:
        rows = conn.execute(
            "SELECT s.dispatch_uuid::text, q.title FROM dispatches s JOIN question_refs q ON q.id = s.question_ref_id "
            "WHERE s.offering_id = %s AND s.dispatch_uuid::text = ANY(%s)",
            (notice.offering_row_id, ids)).fetchall()
        titles = {str(r[0]): (redact(r[1], identities, secrets=secrets) if isinstance(r[1], str) and r[1]
                              else noticetexts.UNTITLED)
                  for r in rows}
    course = redact(offering.course, identities, secrets=secrets)
    day = notice.local_date.isoformat() if notice.local_date is not None else noticetexts.EMPTY

    def header(size: int | None) -> str:
        values = {state: _count(adhesion, state) for state in ("linked", "sending", "terms_presented",
                                                                "invite_valid", "send_failed", "invite_lapsed",
                                                                "no_invite")}
        return noticetexts.DAILY_DIGEST.format(
            date=day, course=_clip(course, size), **values, new_links=_count(data, "new_links"),
            late=_count(data, "late"), dispatches=len(items))

    def lines(size: int | None) -> list[str]:
        found = []
        for item, dispatch_id in zip(items, ids, strict=True):
            pairs = {name: noticetexts.pair(*_pair_of(item.get(name))) for name in _DIGEST_PAIRS}
            found.append(noticetexts.DIGEST_DISPATCH_LINE.format(
                title=_clip(titles.get(dispatch_id, noticetexts.UNTITLED), size), answered=pairs["answered"],
                correct=pairs["correct"], partially_correct=pairs["partially_correct"], dont_know=pairs["dont_know"]))
        return found

    def single(size: int | None) -> Formatted:
        return formatter.escape("\n".join([header(size), *lines(size)]))

    whole = single(None)
    if _fits(whole):
        return [whole]
    # Primeiro, o corte: o maior tamanho (≥ DIGEST_TEXT_MIN) que faz o resumo caber numa mensagem.
    longest = max([len(course), *(len(t) for t in titles.values()), DIGEST_TEXT_MIN])
    low, high = DIGEST_TEXT_MIN, longest
    if _fits(single(low)):
        while low < high:
            middle = (low + high + 1) // 2
            if _fits(single(middle)):
                low = middle
            else:
                high = middle - 1
        return [single(low)]
    # Depois, as partes, com os textos no menor tamanho: linhas inteiras, na ordem.
    parts: list[Formatted] = []
    current = [header(DIGEST_TEXT_MIN)]
    if not _fits(formatter.escape(current[0])):
        raise ValueError("o cabeçalho do resumo diário não cabe numa mensagem")
    for line in lines(DIGEST_TEXT_MIN):
        attempt = formatter.escape("\n".join([*current, line]))
        if _fits(attempt):
            current.append(line)
            continue
        if not _fits(formatter.escape(line)):
            raise ValueError("uma linha do resumo diário não cabe numa mensagem")
        parts.append(formatter.escape("\n".join(current)))
        current = [line]
    parts.append(formatter.escape("\n".join(current)))
    return parts


def deliver(conn: psycopg.Connection, notice_row_id: int, *, channel: DeliveryChannel,
            now: datetime, secrets: Sequence[str | None] = ()) -> NoticeState:
    """Fase 2: compõe, enfileira por destinatário e grava o estado final.

    Numa transação (ou num ponto de salvamento da transação do chamador, como a do congelamento), na
    ordem de travas dos avisos: trava o aviso (`FOR NO KEY UPDATE`); fora de `pending`, devolve o estado sem
    efeito; lê os destinatários (sem trava); sem destinatário → `no_recipient`; com destinatários,
    compõe o texto com o `channel.formatter` (lido só aqui, na execução) e enfileira uma mensagem
    por destinatário pela `outbox.enqueue_notice_text`, com a chave
    `notice:<id>:p<person_row_id>`, e grava `queued` com `recipient_count`. Em todo estado final,
    `finished_at = now` e o `notice.recorded`. `ValueError` se o aviso não existir. `secrets`: os valores
    literais redigidos nos textos variáveis (o token inicial configurado, que os tratadores montam do
    `Settings`; nenhum segredo sai)."""
    with conn.transaction():
        notice = _lock_notice(conn, notice_row_id)
        if notice is None:
            raise ValueError("aviso inexistente")
        if notice.state is not NoticeState.PENDING:
            return notice.state
        offering = _offering(conn, notice.offering_row_id)
        recipients = team_recipients(conn, notice.offering_row_id)
        if not recipients or offering.bot_row_id is None:
            return _finish(conn, notice, offering, NoticeState.NO_RECIPIENT, 0, now)
        formatter = channel.formatter
        if notice.kind is NoticeKind.DAILY_DIGEST:
            parts = _digest_parts(conn, notice, offering, formatter, secrets)
        else:
            parts = [_compose_text(conn, notice, offering, formatter, secrets)]
        for recipient in recipients:
            base = f"notice:{notice.row_id}:p{recipient.person_row_id}"
            # Uma mensagem por destinatário; no resumo diário longo, uma por parte, na ordem.
            for number, text in enumerate(parts, start=1):
                outbox.enqueue_notice_text(conn, notice_row_id=notice.row_id, recipient=recipient.pseudo_id,
                                           bot_id=offering.bot_row_id, endpoint_row_id=recipient.endpoint_row_id,
                                           text=text.markup, text_format=formatter.text_format,
                                           idempotency_key=base if len(parts) == 1 else f"{base}:part{number}",
                                           now=now)
        return _finish(conn, notice, offering, NoticeState.QUEUED, len(recipients), now)


# Os estados finais sem envio que um tratador decide antes da composição: `not_sent`
# (fechado no piso ou abaixo dele; episódio arquivado sem retorno), `no_activity`, `missed` e
# `gave_up` (resumo diário e prévia).
_UNSENT_STATES = (NoticeState.NOT_SENT, NoticeState.NO_ACTIVITY, NoticeState.MISSED, NoticeState.GAVE_UP)


def finish_without_sending(conn: psycopg.Connection, notice_row_id: int, *, state: NoticeState,
                           now: datetime) -> NoticeState:
    """O estado final sem mensagem (fase 2 sem composição): trava o aviso
    (`FOR NO KEY UPDATE`); fora de `pending`, devolve o estado sem efeito; senão grava `state` com
    `recipient_count = 0`, `finished_at = now` e o `notice.recorded`. `ValueError` se o aviso não existir
    ou se `state` não for um estado final sem envio."""
    state = NoticeState(state)
    if state not in _UNSENT_STATES:
        raise ValueError(f"estado final sem envio inválido: {state.value}")
    with conn.transaction():
        notice = _lock_notice(conn, notice_row_id)
        if notice is None:
            raise ValueError("aviso inexistente")
        if notice.state is not NoticeState.PENDING:
            return notice.state
        return _finish(conn, notice, _offering(conn, notice.offering_row_id), state, 0, now)


# --------------------------------------------------------------------------------------------------
# Tratadores
# --------------------------------------------------------------------------------------------------


class _SendHandler:
    """`notice.send`: a fase 2 do aviso do payload. A conexão abre no primeiro
    uso; o canal só é lido na execução; `channel = None` → `PermanentTaskError("canal sem
    rota")`. A reexecução depois do commit não tem efeito (o aviso já fora de `pending`)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel
        # Os segredos configurados (o token inicial), redigidos na composição (nenhum segredo sai).
        self._secrets = redaction_secrets(settings)

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(SEND_SUBJECT_KEY) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {SEND_SUBJECT_KEY}")
        if self._channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        conn = self._db.get()
        exists = conn.execute("SELECT 1 FROM team_notices WHERE id = %s", (value,)).fetchone()
        if exists is None:
            raise PermanentTaskError("aviso inexistente")
        deliver(conn, value, channel=self._channel, now=self._clock.now(), secrets=self._secrets)


# O nome da chave do assunto no payload das regras de disparo (`scheduler._dispatch_rule`).
DISPATCH_SUBJECT_KEY = "dispatch_row_id"
# Os cancelamentos que avisam; `operator` e `offering_closed` não avisam.
AUTOMATIC_CANCEL_REASONS = ("expired", "window_missed", "validation_overran")


def _fact_above_floor(conn: psycopg.Connection, dispatch_uuid: Any, event_type: str) -> bool:
    """O fato do disparo depois do deploy (piso por fato): o evento da transição com
    `seq > event_floor` (pelo índice `events_dispatch_fact_idx`)."""
    floor = floors(conn).event_floor
    row = conn.execute("SELECT 1 FROM events WHERE data->>'dispatch_id' = %s AND type = %s AND seq > %s LIMIT 1",
                       (str(dispatch_uuid), event_type, floor)).fetchone()
    return row is not None


class _DispatchNoticeHandler:
    """As regras `notice.dispatch_rejected`, `notice.dispatch_cancelled` e `notice.dispatch_opened`
    (os avisos do disparo): numa transação, na ordem de travas dos avisos, o disparo
    (`FOR SHARE`: espera as transições do disparo em curso, inclusive o fechamento com o gancho do aberto) →
    reconferência do estado, do motivo e do fato acima do piso → registro (`record`, `ON CONFLICT DO
    NOTHING`) com a fotografia → entrega (`deliver`: aviso → destinatários → identidades → fila → evento).
    Fora da condição, ou com o aviso já registrado (em qualquer estado; o "aberto" do gancho fica para o
    congelamento ou o `notice.send`), termina sem efeito. O canal só é lido na execução."""

    def __init__(self, conninfo: str, *, task_type: str, clock: Clock, settings: Settings,
                 channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._task_type = task_type
        self._clock = clock
        self._settings = settings
        self._channel = channel
        # Os segredos configurados (o token inicial), redigidos na composição (nenhum segredo sai).
        self._secrets = redaction_secrets(settings)

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(DISPATCH_SUBJECT_KEY) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {DISPATCH_SUBJECT_KEY}")
        if self._channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        conn = self._db.get()
        now = self._clock.now()
        with conn.transaction():
            # FOR SHARE: trava compartilhada; não barra o FOR KEY SHARE das FKs.
            row = conn.execute(
                "SELECT state, offering_id, dispatch_uuid, rejection, cancel_reason, closes_at "
                "FROM dispatches WHERE id = %s FOR SHARE", (value,)).fetchone()
            if row is None:
                raise PermanentTaskError("disparo inexistente")
            state, offering_row, dispatch_uuid = str(row[0]), int(row[1]), row[2]
            found = self._snapshot(conn, value, state=state, rejection=row[3], cancel_reason=row[4],
                                   closes_at=row[5])
            if found is None:
                return
            kind, event_type, data = found
            if not _fact_above_floor(conn, dispatch_uuid, event_type):
                return
            notice_row_id = record(conn, offering_row_id=offering_row, kind=kind, subject_key=f"dispatch:{value}",
                                   data=data, now=now, dispatch_row_id=value)
            if notice_row_id is None:
                return  # já registrado (a reexecução ou o gancho do fechamento): sem efeito novo
            deliver(conn, notice_row_id, channel=self._channel, now=now, secrets=self._secrets)

    def _snapshot(self, conn: psycopg.Connection, dispatch_row_id: int, *, state: str, rejection: Any,
                  cancel_reason: Any, closes_at: datetime | None,
                  ) -> tuple[NoticeKind, str, dict[str, Any]] | None:
        """O tipo, o evento do fato e a fotografia (`data`), ou `None` fora da condição da regra."""
        if self._task_type == DISPATCH_REJECTED_TASK_TYPE:
            if state != "rejected" or not rejection:
                return None
            return NoticeKind.DISPATCH_REJECTED, "dispatch.rejected", {"rejection": str(rejection)}
        if self._task_type == DISPATCH_CANCELLED_TASK_TYPE:
            if state != "cancelled" or cancel_reason not in AUTOMATIC_CANCEL_REASONS:
                return None
            return NoticeKind.DISPATCH_CANCELLED, "dispatch.cancelled", {"reason": str(cancel_reason)}
        if state != "open":
            return None  # fechado: o "aberto" antecipado é do gancho do fechamento
        from cq_server import dispatches  # só aqui (sem ciclo)

        counts = dispatches._counts(conn, dispatch_row_id)
        return NoticeKind.DISPATCH_OPENED, "dispatch.opened", opened_data(counts, closes_at)


# --------------------------------------------------------------------------------------------------
# As detecções do oferecimento
# --------------------------------------------------------------------------------------------------

# Os nomes das chaves do assunto no payload (`scheduler.REPORT_RULES`).
OFFERING_SUBJECT_KEY = "offering_row_id"
BATCH_SUBJECT_KEY = "batch_row_id"
# As origens das falhas avulsas, agrupadas por janela; as do lote têm aviso próprio.
WINDOW_ORIGINS = ("coordination", "self_service")


class _DetectionHandler:
    """A base das 4 detecções do oferecimento: o assunto do payload (`subject_key`), a conexão própria
    (aberta no primeiro uso), o relógio e o canal, lido só na execução; `channel = None` →
    `PermanentTaskError("canal sem rota")`. Cada subclasse faz, numa transação, a reconferência sob a
    trava, o registro (`record`, `ON CONFLICT DO NOTHING`) e a entrega (`deliver`); com o aviso já
    registrado, em qualquer estado, termina sem efeito novo."""

    subject_key = DISPATCH_SUBJECT_KEY

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel
        # Os segredos configurados (o token inicial), redigidos na composição (nenhum segredo sai).
        self._secrets = redaction_secrets(settings)

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(self.subject_key) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {self.subject_key}")
        if self._channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        conn = self._db.get()
        with conn.transaction():
            self._run(conn, value, self._channel, self._clock.now())

    def _run(self, conn: psycopg.Connection, subject_id: int, channel: DeliveryChannel, now: datetime) -> None:
        raise NotImplementedError


def _lock_offering_shared(conn: psycopg.Connection, offering_row_id: int) -> tuple[uuid.UUID, str]:
    """O oferecimento (`FOR SHARE`: espera as transições em curso, como o arquivamento; não barra o
    `FOR KEY SHARE` das FKs): o `offering_id` e o estado. `PermanentTaskError` se não existir."""
    row = conn.execute("SELECT offering_id, state FROM offerings WHERE id = %s FOR SHARE",
                       (offering_row_id,)).fetchone()
    if row is None:
        raise PermanentTaskError("oferecimento inexistente")
    return row[0], str(row[1])


class _QuestionFailedHandler(_DetectionHandler):
    """`notice.question_failed`: um aviso por disparo quando
    alguma entrega dele termina `question_failed/grading_broken` depois do deploy (o
    `delivery.question_failed` com `seq > event_floor`). A falha de preparação não avisa (fica só
    no relatório). Ordem de travas: o disparo (`FOR SHARE`) → registro → entrega. Fotografia: `{}`
    (nenhuma contagem)."""

    def _run(self, conn: psycopg.Connection, subject_id: int, channel: DeliveryChannel, now: datetime) -> None:
        row = conn.execute("SELECT offering_id, dispatch_uuid FROM dispatches WHERE id = %s FOR SHARE",
                           (subject_id,)).fetchone()
        if row is None:
            raise PermanentTaskError("disparo inexistente")
        offering_row, dispatch_uuid = int(row[0]), row[1]
        found = conn.execute(
            "SELECT 1 FROM deliveries x JOIN events e ON e.data->>'dispatch_id' = %s "
            "AND e.type = 'delivery.question_failed' AND e.data->>'delivery_id' = x.delivery_uuid::text "
            "WHERE x.dispatch_id = %s AND x.state = 'question_failed' AND x.failure = 'grading_broken' "
            "AND e.seq > %s LIMIT 1",
            (str(dispatch_uuid), subject_id, floors(conn).event_floor),
        ).fetchone()
        if found is None:
            return
        notice_row_id = record(conn, offering_row_id=offering_row, kind=NoticeKind.QUESTION_FAILED,
                               subject_key=f"dispatch:{subject_id}", data={}, now=now, dispatch_row_id=subject_id)
        if notice_row_id is None:
            return  # já registrado: sem efeito novo
        deliver(conn, notice_row_id, channel=channel, now=now, secrets=self._secrets)


@dataclass(frozen=True)
class _Episode:
    """Um episódio de bot sem acesso: o `lost` e o primeiro `restored` depois dele (ou nenhum)."""

    lost_seq: int
    started_at: datetime
    restored_seq: int | None
    ended_at: datetime | None


def _access_episodes(conn: psycopg.Connection, offering_id: uuid.UUID) -> list[_Episode]:
    """Os episódios do oferecimento em ordem de `seq` do `lost`, cada um com o primeiro `restored`
    posterior (pelo índice `events_access_changed_idx`)."""
    rows = conn.execute(
        "SELECT l.seq, l.occurred_at, back.seq, back.occurred_at FROM events l "
        "LEFT JOIN LATERAL (SELECT r.seq, r.occurred_at FROM events r WHERE r.type = 'bot.access_changed' "
        "AND r.data->>'offering_id' = l.data->>'offering_id' AND r.data->>'access' = 'restored' "
        "AND r.seq > l.seq ORDER BY r.seq LIMIT 1) AS back ON true "
        "WHERE l.type = 'bot.access_changed' AND l.data->>'offering_id' = %s AND l.data->>'access' = 'lost' "
        "ORDER BY l.seq",
        (str(offering_id),),
    ).fetchall()
    return [_Episode(lost_seq=int(r[0]), started_at=r[1], restored_seq=None if r[2] is None else int(r[2]),
                     ended_at=r[3]) for r in rows]


def _episode_data(conn: psycopg.Connection, offering_row_id: int, episode: _Episode) -> dict[str, Any]:
    """A fotografia do episódio: início e fim em UTC; as entregas
    `not_delivered/bot_no_access` dos disparos fechados entre o `lost` e o `restored` (pelo `seq` do
    `dispatch.closed`); as entregas pendentes no retorno; os avisos distintos do oferecimento com alguma
    mensagem cujo `outbox.failed` tem `seq` entre o `lost` e o `restored` e, entre eles, os resumos
    (`dispatch_closed`, `summary_resent`; [I] o reenvio perdido também se refaz por `cq reports resend`).
    Sem `restored` (o arquivado, `not_sent`), o intervalo vai até agora e não há pendentes no retorno."""
    upper = episode.restored_seq
    not_delivered = conn.execute(
        "SELECT count(*) FROM deliveries x JOIN dispatches p ON p.id = x.dispatch_id "
        "WHERE p.offering_id = %s AND x.state = 'not_delivered' AND x.undelivered_reason = 'bot_no_access' "
        "AND EXISTS (SELECT 1 FROM events c WHERE c.data->>'dispatch_id' = p.dispatch_uuid::text "
        "AND c.type = 'dispatch.closed' AND c.seq > %s AND (%s::bigint IS NULL OR c.seq < %s::bigint))",
        (offering_row_id, episode.lost_seq, upper, upper),
    ).fetchone()
    pending = conn.execute(
        "SELECT count(*) FROM deliveries x JOIN dispatches p ON p.id = x.dispatch_id "
        "WHERE p.offering_id = %s AND x.state = 'pending'",
        (offering_row_id,),
    ).fetchone()
    lost = conn.execute(
        "SELECT count(DISTINCT n.id), count(DISTINCT n.id) FILTER (WHERE n.kind IN ('dispatch_closed', "
        "'summary_resent')) FROM events e JOIN outbox_messages m ON m.id = (e.data->>'message_id')::bigint "
        "JOIN team_notices n ON n.id = m.notice_id "
        "WHERE e.type = 'outbox.failed' AND e.seq > %s AND (%s::bigint IS NULL OR e.seq < %s::bigint) "
        "AND n.offering_id = %s",
        (episode.lost_seq, upper, upper, offering_row_id),
    ).fetchone()
    return {"started_at": _utc_iso(episode.started_at),
            "ended_at": None if episode.ended_at is None else _utc_iso(episode.ended_at),
            "not_delivered": int(not_delivered[0]), "pending": 0 if upper is None else int(pending[0]),
            "lost_notices": int(lost[0]), "lost_summaries": int(lost[1])}


class _AccessEpisodeHandler(_DetectionHandler):
    """`notice.access_episode`: para cada episódio
    de bot sem acesso do oferecimento sem aviso `episode:<seq do lost>`, com o retorno depois do deploy
    (`restored.seq > event_floor`) → o aviso `bot_access_restored` pelo bot que voltou; sem retorno, com o
    `lost` depois do deploy e o oferecimento `archived` → o aviso `not_sent`, sem mensagem. Ordem
    de travas: o oferecimento (`FOR SHARE`) → registro → entrega."""

    subject_key = OFFERING_SUBJECT_KEY

    def _run(self, conn: psycopg.Connection, subject_id: int, channel: DeliveryChannel, now: datetime) -> None:
        offering_id, state = _lock_offering_shared(conn, subject_id)
        floor = floors(conn).event_floor
        for episode in _access_episodes(conn, offering_id):
            if episode.restored_seq is not None:
                if episode.restored_seq <= floor:
                    continue
            elif not (episode.lost_seq > floor and state == "archived"):
                continue
            notice_row_id = record(conn, offering_row_id=subject_id, kind=NoticeKind.BOT_ACCESS_RESTORED,
                                   subject_key=f"episode:{episode.lost_seq}",
                                   data=_episode_data(conn, subject_id, episode), now=now)
            if notice_row_id is None:
                continue  # já registrado: sem efeito novo
            if episode.restored_seq is None:
                finish_without_sending(conn, notice_row_id, state=NoticeState.NOT_SENT, now=now)
            else:
                deliver(conn, notice_row_id, channel=channel, now=now, secrets=self._secrets)


class _InviteBatchHandler(_DetectionHandler):
    """`notice.invite_batch`: o lote sem envio
    pendente, com ao menos uma falha e todos os envios depois do deploy (`id > invite_send_floor`) → um
    aviso `invite_failures` (`batch:<id>`) com `{sent, failed}` (os cancelados fora das contagens), sem
    apelido nem pseudônimo. Ordem de travas: o lote (`FOR SHARE`) → registro → entrega."""

    subject_key = BATCH_SUBJECT_KEY

    def _run(self, conn: psycopg.Connection, subject_id: int, channel: DeliveryChannel, now: datetime) -> None:
        row = conn.execute("SELECT offering_id FROM invite_batches WHERE id = %s FOR SHARE",
                           (subject_id,)).fetchone()
        if row is None:
            raise PermanentTaskError("lote de convites inexistente")
        offering_row = int(row[0])
        from cq_server import students  # só aqui (sem ciclo): a chave da tarefa do envio

        # O envio `pending` cuja tarefa `invite.email` terminou `failed` conta como falha (o mesmo
        # predicado do `send_failed` da adesão, `students._ADHESION_STATE_SQL`): a tarefa não volta a rodar,
        # e sem isso o lote nunca terminaria e o aviso se perderia. Espelha
        # `scheduler._NOTICE_INVITE_BATCH_DUE`.
        counts = conn.execute(
            "SELECT count(*) FILTER (WHERE x.state = 'pending' AND NOT x.task_failed), "
            "count(*) FILTER (WHERE x.state = 'sent'), "
            "count(*) FILTER (WHERE x.state = 'failed' OR (x.state = 'pending' AND x.task_failed)), "
            "count(*) FILTER (WHERE x.id <= %(floor)s) "
            "FROM (SELECT s.id, s.state, EXISTS (SELECT 1 FROM tasks t "
            "WHERE t.idempotency_key = %(prefix)s::text || s.id AND t.state = 'failed') AS task_failed "
            "FROM invite_sends s WHERE s.batch_id = %(batch)s) AS x",
            {"floor": floors(conn).invite_send_floor, "batch": subject_id,
             "prefix": f"{students.INVITE_EMAIL_TASK_TYPE}:"},
        ).fetchone()
        pending, sent, failed, before_floor = (int(value) for value in counts)
        if pending or not failed or before_floor:
            return
        notice_row_id = record(conn, offering_row_id=offering_row, kind=NoticeKind.INVITE_FAILURES,
                               subject_key=f"batch:{subject_id}", data={"sent": sent, "failed": failed}, now=now)
        if notice_row_id is None:
            return  # já registrado: sem efeito novo
        deliver(conn, notice_row_id, channel=channel, now=now, secrets=self._secrets)


class _InviteWindowHandler(_DetectionHandler):
    """`notice.invite_window`: as falhas da coordenação e do
    autosserviço do oferecimento (`id > invite_send_floor`) agrupadas em janelas de
    `INVITE_FAILURE_WINDOW_SECONDS` ancoradas na primeira falha não coberta (pelo `finished_at`, e o `id`
    no empate); cada janela já terminada (`início + 900 s <= now`) → um aviso `invite_failures`
    (`window:<id da primeira falha>`) com `{failed, window_start, window_end}`. Ordem de travas: o
    oferecimento (`FOR SHARE`) → registro → entrega, janela a janela."""

    subject_key = OFFERING_SUBJECT_KEY

    def _run(self, conn: psycopg.Connection, subject_id: int, channel: DeliveryChannel, now: datetime) -> None:
        _lock_offering_shared(conn, subject_id)
        floor = floors(conn).invite_send_floor
        window = timedelta(seconds=INVITE_FAILURE_WINDOW_SECONDS)
        while True:
            anchor = conn.execute(
                "SELECT f.id, f.finished_at FROM invite_sends f JOIN enrollments e ON e.id = f.enrollment_id "
                "WHERE e.offering_id = %(o)s AND f.state = 'failed' AND f.origin = ANY(%(origins)s) "
                "AND f.id > %(floor)s AND f.finished_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM team_notices n JOIN invite_sends w "
                "ON n.subject_key = 'window:' || w.id::text WHERE n.offering_id = %(o)s "
                "AND n.kind = 'invite_failures' AND f.finished_at >= w.finished_at "
                "AND f.finished_at < w.finished_at + %(window)s) "
                "ORDER BY f.finished_at, f.id LIMIT 1",
                {"o": subject_id, "origins": list(WINDOW_ORIGINS), "floor": floor, "window": window},
            ).fetchone()
            if anchor is None or anchor[1] + window > now:
                return
            anchor_id, started = int(anchor[0]), anchor[1]
            # A contagem exclui as falhas já cobertas por outra janela registrada (o mesmo filtro da âncora, usado
            # nas duas consultas): com `finished_at` fora da ordem de commit, uma falha pode cair no intervalo de duas
            # janelas, e cada falha conta numa só (sem contagem dupla).
            failed = conn.execute(
                "SELECT count(*) FROM invite_sends f JOIN enrollments e ON e.id = f.enrollment_id "
                "WHERE e.offering_id = %(o)s AND f.state = 'failed' AND f.origin = ANY(%(origins)s) "
                "AND f.id > %(floor)s AND f.finished_at >= %(start)s AND f.finished_at < %(end)s "
                "AND NOT EXISTS (SELECT 1 FROM team_notices n JOIN invite_sends w "
                "ON n.subject_key = 'window:' || w.id::text WHERE n.offering_id = %(o)s "
                "AND n.kind = 'invite_failures' AND f.finished_at >= w.finished_at "
                "AND f.finished_at < w.finished_at + %(window)s)",
                {"o": subject_id, "origins": list(WINDOW_ORIGINS), "floor": floor, "start": started,
                 "end": started + window, "window": window},
            ).fetchone()
            notice_row_id = record(conn, offering_row_id=subject_id, kind=NoticeKind.INVITE_FAILURES,
                                   subject_key=f"window:{anchor_id}",
                                   data={"failed": int(failed[0]), "window_start": _utc_iso(started),
                                         "window_end": _utc_iso(started + window)}, now=now)
            if notice_row_id is None:
                return  # já registrado (corrida com outra execução): sem efeito novo
            deliver(conn, notice_row_id, channel=channel, now=now, secrets=self._secrets)


def task_handlers(conninfo: str, *, clock: Clock, settings: Settings,
                  channel: DeliveryChannel | None) -> dict[str, Callable[..., None]]:
    """Os tratadores dos avisos (`TASK_TYPES`), cada um chamável, com `close()` e a própria conexão
    (aberta no primeiro uso). Nada lê o canal aqui.

    `notice.send`, as 3 regras de disparo e as 4 detecções do oferecimento."""
    built: dict[str, Callable[..., None]] = {
        SEND_TASK_TYPE: _SendHandler(conninfo, clock=clock, settings=settings, channel=channel)}
    for task_type in (DISPATCH_REJECTED_TASK_TYPE, DISPATCH_CANCELLED_TASK_TYPE, DISPATCH_OPENED_TASK_TYPE):
        built[task_type] = _DispatchNoticeHandler(conninfo, task_type=task_type, clock=clock, settings=settings,
                                                  channel=channel)
    detections: tuple[tuple[str, type[_DetectionHandler]], ...] = (
        (QUESTION_FAILED_TASK_TYPE, _QuestionFailedHandler), (ACCESS_EPISODE_TASK_TYPE, _AccessEpisodeHandler),
        (INVITE_BATCH_TASK_TYPE, _InviteBatchHandler), (INVITE_WINDOW_TASK_TYPE, _InviteWindowHandler))
    for task_type, handler in detections:
        built[task_type] = handler(conninfo, clock=clock, settings=settings, channel=channel)
    return built
