"""Resumo diário à equipe: configuração, agenda e tratador.
Uma vez por dia, no horário local configurado, a equipe recebe as contagens do dia pelos avisos à
equipe (`teamnotices`).

- **Operações** (na transação do chamador): `get_digest` (a linha ou o padrão: desligado às
  `DEFAULT_LOCAL_TIME`) e `update_digest` (arquivado → `InvalidState`; sem mudança → sem evento nem
  escrita; com mudança, `offering.digest_changed`).
- **Agenda** (puras): `next_occurrence` (a próxima ocorrência do horário local no fuso, `fold = 0`) e
  `local_day_bounds` (os limites em UTC de uma data local).
- **Tratador** `digest.daily`: registra o `daily_digest` com a fotografia em `data` (sem texto livre) e o
  entrega pelo `teamnotices.deliver`, que compõe o texto e as partes; `no_activity` e `missed`
  com o evento.

Importa `teamnotices` e `students` (adesão); `reports` só dentro da função.

**Fotografia do `daily_digest`** (`team_notices.data`; sem identidade nem texto livre; lida pelo
`teamnotices.deliver`): `adhesion` (os 7 estados de `students.adhesion` no instante do registro),
`new_links`, `invites_sent`, `invites_failed`, `late` (as atrasadas finais recebidas no dia) e
`dispatches`, a lista dos disparos abertos ou fechados no dia, na ordem de criação, cada um `{dispatch_id,
answered, correct, partially_correct, dont_know}` com os pares `[no prazo, com atrasadas]`.
"""

from __future__ import annotations

import re
import uuid
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

import psycopg

from cq_server import offerings, students, teamnotices
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import Settings, redaction_secrets
from cq_server.events import EventType, append_event
from cq_server.ports.delivery import DeliveryChannel
from cq_server.tasks import PermanentTaskError

if TYPE_CHECKING:
    from cq_server.offerings import InvalidState
    from cq_server.tasks import ClaimedTask

# O horário local padrão do resumo e a forma `HH:MM` aceita (`LocalTime`).
DEFAULT_LOCAL_TIME = "20:00"
LOCAL_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
DIGEST_TASK_TYPE = "digest.daily"
# O nome da chave do assunto no payload da tarefa (`scheduler.REPORT_RULES`).
DIGEST_SUBJECT_KEY = "digest_row_id"
# O tratador só age nos oferecimentos ativos ou encerrados (a regra já filtra; reconferido).
_SENDING_STATES = ("active", "closed")
_UTC = timezone.utc
_NO_CHANNEL = "canal sem rota"


@dataclass(frozen=True)
class DigestView:
    """A configuração do resumo diário de um oferecimento."""

    offering_id: uuid.UUID
    code: str
    timezone: str
    enabled: bool
    local_time: str


# --------------------------------------------------------------------------------------------------
# Agenda (puras)
# --------------------------------------------------------------------------------------------------


def _clock_time(local_time: str) -> time:
    """O `HH:MM` validado como `time`; fora do padrão, `ValueError`."""
    if not isinstance(local_time, str) or not LOCAL_TIME.fullmatch(local_time):
        raise ValueError("horário local fora do formato HH:MM")
    hours, minutes = local_time.split(":")
    return time(int(hours), int(minutes))


def _occurrence(day: date, at: time, zone: zoneinfo.ZoneInfo) -> datetime:
    """O instante (UTC) de `at` na data local `day`; o `fold = 0` padrão dá, no horário inexistente, o
    deslocamento de antes da transição e, no repetido, a primeira passagem."""
    return datetime.combine(day, at, tzinfo=zone).astimezone(_UTC)


def next_occurrence(after: datetime, local_time: str, zone: str, *, strictly_after: bool = False) -> datetime:
    """A primeira ocorrência (UTC) de `local_time` no fuso `zone` em ou depois de `after` (estritamente
    depois com `strictly_after`); horário inexistente ou repetido pelo horário de verão com `fold = 0`.

    Uma ocorrência por data local (a de `fold = 0`): o horário repetido na volta do horário de verão ocorre
    uma vez no dia, e entre as duas passagens a próxima é a do dia seguinte."""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("horário sem fuso")
    at = _clock_time(local_time)
    tz = zoneinfo.ZoneInfo(zone)
    moment = after.astimezone(_UTC)
    # A partir da véspera local de `after` (as ocorrências crescem de um dia para o outro).
    day = moment.astimezone(tz).date() - timedelta(days=1)
    while True:
        candidate = _occurrence(day, at, tz)
        if candidate > moment or (candidate == moment and not strictly_after):
            return candidate
        day += timedelta(days=1)


def local_day_bounds(day: date, zone: str) -> tuple[datetime, datetime]:
    """Os limites `[início, fim)` em UTC da data local `day` no fuso `zone`."""
    tz = zoneinfo.ZoneInfo(zone)
    midnight = time(0, 0)
    return _occurrence(day, midnight, tz), _occurrence(day + timedelta(days=1), midnight, tz)


def _local_date(moment: datetime, zone: str) -> date:
    return moment.astimezone(zoneinfo.ZoneInfo(zone)).date()


# --------------------------------------------------------------------------------------------------
# Operações
# --------------------------------------------------------------------------------------------------


def _current(conn: psycopg.Connection, offering_row_id: int, *, lock: bool) -> tuple[bool, str]:
    """A configuração gravada ou o padrão (desligado às `DEFAULT_LOCAL_TIME`)."""
    # FOR NO KEY UPDATE (por uniformidade: nenhuma chave muda); a linha é travada depois do
    # oferecimento, na ordem fixa das travas.
    suffix = " FOR NO KEY UPDATE" if lock else ""
    row = conn.execute(f"SELECT enabled, local_time FROM offering_digests WHERE offering_id = %s{suffix}",
                       (offering_row_id,)).fetchone()
    if row is None:
        return False, DEFAULT_LOCAL_TIME
    return bool(row[0]), str(row[1])


def get_digest(conn: psycopg.Connection, ref: str) -> DigestView | None:
    """`offerings.get_digest`; `None` = oferecimento inexistente. Em qualquer estado; sem linha, o padrão
    (nenhuma linha é criada pela leitura)."""
    found = offerings.find_offering_row(conn, ref)
    if found is None:
        return None
    enabled, local_time = _current(conn, found.id, lock=False)
    return DigestView(offering_id=found.offering_id, code=found.code, timezone=found.timezone, enabled=enabled,
                      local_time=local_time)


def update_digest(conn: psycopg.Connection, ref: str, *, enabled: bool | None, local_time: str | None,
                  now: datetime, actor_token_id: str | None) -> DigestView | InvalidState | None:
    """`offerings.update_digest`; `None` = oferecimento inexistente.

    Com o oferecimento travado (`FOR NO KEY UPDATE`) e depois a linha da configuração: arquivado →
    `InvalidState`; igual à configuração vigente (a linha ou o padrão) → a configuração, sem escrita nem
    evento; senão grava a linha (criada na primeira alteração) com `next_due_at` = a primeira
    ocorrência ≥ `now` quando ligado, `NULL` quando desligado, e o `offering.digest_changed`.
    `ValueError` sem nenhum campo, com `enabled` não booleano ou com `local_time` fora do formato (a API
    valida antes)."""
    if enabled is None and local_time is None:
        raise ValueError("a alteração precisa de pelo menos um campo")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("enabled não booleano")
    if local_time is not None:
        _clock_time(local_time)
    with conn.transaction():
        row = offerings.lock_offering(conn, ref)
        if row is None:
            return None
        if row.state == "archived":
            return offerings.InvalidState(row.state)
        current_enabled, current_time = _current(conn, row.id, lock=True)
        new_enabled = current_enabled if enabled is None else enabled
        new_time = current_time if local_time is None else local_time
        view = DigestView(offering_id=row.offering_id, code=row.code, timezone=row.timezone, enabled=new_enabled,
                          local_time=new_time)
        if new_enabled == current_enabled and new_time == current_time:
            return view
        next_due = next_occurrence(now, new_time, row.timezone) if new_enabled else None
        conn.execute(
            "INSERT INTO offering_digests (offering_id, enabled, local_time, next_due_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (offering_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "local_time = EXCLUDED.local_time, next_due_at = EXCLUDED.next_due_at, updated_at = EXCLUDED.updated_at",
            (row.id, new_enabled, new_time, next_due, now))
        append_event(conn, EventType.OFFERING_DIGEST_CHANGED,
                     {"offering_id": row.offering_id, "enabled": new_enabled, "local_time": new_time},
                     occurred_at=now, actor_token_id=actor_token_id)
        return view


# --------------------------------------------------------------------------------------------------
# Tratador `digest.daily`
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Activity:
    """A atividade da data local e a fotografia a gravar."""

    data: dict[str, Any]
    active: bool


def _activity(conn: psycopg.Connection, *, offering_row_id: int, offering_id: uuid.UUID, day: date, zone: str,
              now: datetime, channel: DeliveryChannel) -> _Activity:
    """A atividade de `day` pelos limites em UTC calculados em Python: os vínculos novos
    (`activated_at` das matrículas de aluno), os convites enviados ou com falha (`finished_at`), os
    disparos abertos ou fechados (`opened_at`/`closed_at`) e as atrasadas finais (`received_at`).
    O vencimento de convite não é fato gravado e não conta."""
    start, end = local_day_bounds(day, zone)
    bounds = {"o": offering_row_id, "start": start, "end": end}
    new_links = conn.execute(
        "SELECT count(*) FROM enrollments WHERE offering_id = %(o)s AND role = 'student' "
        "AND activated_at >= %(start)s AND activated_at < %(end)s", bounds).fetchone()
    invites = conn.execute(
        "SELECT count(*) FILTER (WHERE f.state = 'sent'), count(*) FILTER (WHERE f.state = 'failed') "
        "FROM invite_sends f JOIN enrollments e ON e.id = f.enrollment_id WHERE e.offering_id = %(o)s "
        "AND f.state IN ('sent', 'failed') AND f.finished_at >= %(start)s AND f.finished_at < %(end)s",
        bounds).fetchone()
    late = conn.execute(
        "SELECT count(*) FROM submissions s JOIN deliveries v ON v.id = s.delivery_id "
        "JOIN dispatches p ON p.id = v.dispatch_id WHERE p.offering_id = %(o)s AND s.late "
        "AND ((s.outcome = 'graded' AND s.valid) OR s.outcome = 'dont_know') "
        "AND s.received_at >= %(start)s AND s.received_at < %(end)s", bounds).fetchone()
    day_dispatches = conn.execute(
        "SELECT id, dispatch_uuid FROM dispatches WHERE offering_id = %(o)s AND state IN ('open', 'closed') "
        "AND ((opened_at >= %(start)s AND opened_at < %(end)s) OR (closed_at >= %(start)s AND closed_at < %(end)s)) "
        "ORDER BY id", bounds).fetchall()

    from cq_server import reports  # só aqui, para não criar ciclo de importação
    from cq_server.dispatches import DispatchStateConflict

    lines: list[dict[str, Any]] = []
    for dispatch_row_id, dispatch_uuid in day_dispatches:
        # Só as contagens do desempenho ficam (nenhum texto do relatório é guardado): sem identidades.
        report = reports.compute_report(conn, int(dispatch_row_id), now=now, identities=(),
                                        formatter=channel.formatter)
        if isinstance(report, DispatchStateConflict):
            continue
        performance = report.performance
        lines.append({"dispatch_id": str(dispatch_uuid),
                      **{name: [getattr(performance, name).on_time, getattr(performance, name).with_late]
                         for name in ("answered", "correct", "partially_correct", "dont_know")}})

    adhesion = students.adhesion(conn, str(offering_id), now=now)
    counts = {state: 0 if adhesion is None else int(getattr(adhesion, state)) for state in students.ADHESION_STATES}
    new_links_count, sent, failed, late_count = int(new_links[0]), int(invites[0]), int(invites[1]), int(late[0])
    data = {"adhesion": counts, "new_links": new_links_count, "invites_sent": sent, "invites_failed": failed,
            "late": late_count, "dispatches": lines}
    active = bool(new_links_count or sent or failed or late_count or day_dispatches)
    return _Activity(data=data, active=active)


class _TaskHandler:
    """O tratador `digest.daily`: numa transação, na ordem fixa das travas, o
    oferecimento (`FOR SHARE`: espera a alteração da configuração e as transições em curso) → a linha da
    configuração (`FOR NO KEY UPDATE`) → a reconferência (ligado, vencido, oferecimento ativo ou encerrado) → o
    registro da data `D` (a data local de `next_due_at` no fuso atual) → a entrega (`teamnotices.deliver`)
    ou o estado sem envio → o avanço de `next_due_at` para a ocorrência estritamente posterior a
    `max(next_due_at, now)`. O registro é único por data (`digest:<D>`): na reexecução ou no
    reagendamento, o `record` devolve `None` e só o avanço acontece. O canal só é lido na
    execução; `channel = None` → `PermanentTaskError("canal sem rota")`."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel
        # Os segredos configurados (o token inicial), redigidos na composição do resumo (nenhum segredo sai,
        # como no `Outbox` e na `TaskQueue`).
        self._secrets = redaction_secrets(settings)

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(DIGEST_SUBJECT_KEY) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {DIGEST_SUBJECT_KEY}")
        if self._channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        conn = self._db.get()
        now = self._clock.now()
        with conn.transaction():
            self._run(conn, value, self._channel, now)

    def _run(self, conn: psycopg.Connection, digest_row_id: int, channel: DeliveryChannel, now: datetime) -> None:
        found = conn.execute("SELECT offering_id FROM offering_digests WHERE id = %s", (digest_row_id,)).fetchone()
        if found is None:
            raise PermanentTaskError("configuração do resumo diário inexistente")
        offering_row_id = int(found[0])
        # FOR SHARE: não barra o FOR KEY SHARE das FKs; espera o `FOR NO KEY UPDATE` da alteração.
        offering = conn.execute("SELECT offering_id, state, timezone, activated_at FROM offerings WHERE id = %s "
                                "FOR SHARE", (offering_row_id,)).fetchone()
        if offering is None:
            raise PermanentTaskError("oferecimento inexistente")
        offering_id, state, zone, activated_at = offering[0], str(offering[1]), str(offering[2]), offering[3]
        config = conn.execute("SELECT enabled, local_time, next_due_at FROM offering_digests WHERE id = %s "
                              "FOR NO KEY UPDATE", (digest_row_id,)).fetchone()
        enabled, local_time, due = bool(config[0]), str(config[1]), config[2]
        if not enabled or due is None or due > now or state not in _SENDING_STATES:
            return  # fora da condição (a reexecução depois do avanço, ou a configuração mudou)
        day = _local_date(due, zone)
        today = _local_date(now, zone)
        if activated_at is not None and day < _local_date(activated_at, zone):
            pass  # ligado em rascunho: a data anterior à ativação só avança
        elif day == today:
            activity = _activity(conn, offering_row_id=offering_row_id, offering_id=offering_id, day=day, zone=zone,
                                 now=now, channel=channel)
            notice_row_id = teamnotices.record(
                conn, offering_row_id=offering_row_id, kind=teamnotices.NoticeKind.DAILY_DIGEST,
                subject_key=f"digest:{day.isoformat()}", data=activity.data, now=now, local_date=day)
            if notice_row_id is not None:
                if activity.active:
                    teamnotices.deliver(conn, notice_row_id, channel=channel, now=now, secrets=self._secrets)
                else:
                    teamnotices.finish_without_sending(conn, notice_row_id, state=teamnotices.NoticeState.NO_ACTIVITY,
                                                       now=now)
        else:
            # A data devida já passou: um único `missed`, para `D`.
            notice_row_id = teamnotices.record(
                conn, offering_row_id=offering_row_id, kind=teamnotices.NoticeKind.DAILY_DIGEST,
                subject_key=f"digest:{day.isoformat()}", data={}, now=now, local_date=day)
            if notice_row_id is not None:
                teamnotices.finish_without_sending(conn, notice_row_id, state=teamnotices.NoticeState.MISSED, now=now)
        following = next_occurrence(max(due, now), local_time, zone, strictly_after=True)
        conn.execute("UPDATE offering_digests SET next_due_at = %s WHERE id = %s", (following, digest_row_id))

    def close(self) -> None:
        self._db.close()


def digest_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                        channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador `digest.daily`."""
    return _TaskHandler(conninfo, clock=clock, settings=settings, channel=channel)
