"""Parte da API do `doctor`.


Núcleo sem HTTP e sem adaptadores. `collect` lê o banco numa transação só de leitura, sem evento e sem
chamada externa (o `getMe` do Telegram nunca é chamado), e devolve as verificações do servidor
na ordem fixa: as globais na ordem de `SERVER_CHECK_IDS`, depois as por oferecimento ordenadas pelo
código e pelo `id`. O oferecimento é identificado só pelo código. As mensagens são fixas por
`(id, result)` (`DOCTOR_MESSAGES`).

Os limites vêm de `DoctorSettings`: o `__main__` os lê do ambiente com `from_env`; o
`create_app` só os recebe (não lê o ambiente).

Backup, e-mail e retenção:
três globais, `backup.age`, `smtp.server` e `retention.overdue`, com as mensagens por
`(id, result, motivo)` em `DOCTOR_REASON_MESSAGES` (à parte do `DOCTOR_MESSAGES`, por
`(id, result)`); o registro do backup lido por `read_backup_status`; os limites deles no fim de `DoctorSettings`.
O `from_env` lê as variáveis `CQ_DOCTOR_*`, `CQ_RETENTION_DAYS` e `CQ_BACKUP_STATUS_FILE`; `read_backup_status` valida o
registro do backup; o `collect` emite as 3 globais depois de `clock.skew`, na mesma transação e só com
`SELECT` (o `smtp.server` pela linha de `mail_probe`, lida por `cq_server.mail_probe.read_probe`).

Chave-mestra: `bots.master_key` testa a chave, decifrando os tokens gravados com o
`DoctorSettings.token_cipher` (preenchido pela composição, nunca pela rota); a falha tem a mensagem do motivo
`token_unreadable` em `DOCTOR_REASON_MESSAGES` (o "ok" e o "aviso" ficam em `DOCTOR_MESSAGES`). Por
padrão, todo token gravado é testado, e o texto decifrado é descartado na hora.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, get_args

import psycopg

from cq_server.mail_probe import read_probe
from cq_server.secretbox import TokenCipher, UnreadableToken

# Os identificadores do servidor e os 3 resultados que ele devolve (os resultados
# `not_verified` e `not_applicable` são só do cliente). As 3 globais de backup, e-mail e
# retenção vêm no fim.
ServerCheckId = Literal["bots.master_key", "bot.access", "bot.poll", "bot.lease", "tasks.overdue",
                        "clock.skew", "backup.age", "smtp.server", "retention.overdue"]
ServerCheckResult = Literal["ok", "warning", "failure"]

SERVER_CHECK_IDS: tuple[str, ...] = get_args(ServerCheckId)
SERVER_CHECK_RESULTS: tuple[str, ...] = get_args(ServerCheckResult)

# As variáveis opcionais que o `__main__` lê (números positivos finitos).
DOCTOR_ENV_BOT_POLL = "CQ_DOCTOR_BOT_POLL_SECONDS"
DOCTOR_ENV_TASK_OVERDUE = "CQ_DOCTOR_TASK_OVERDUE_SECONDS"
DOCTOR_ENV_CLOCK_SKEW = "CQ_DOCTOR_CLOCK_SKEW_SECONDS"

# As variáveis do backup, do e-mail e da retenção, lidas pelo `from_env`.
DOCTOR_ENV_BACKUP_WARNING = "CQ_DOCTOR_BACKUP_WARNING_HOURS"
DOCTOR_ENV_BACKUP_FAILURE = "CQ_DOCTOR_BACKUP_FAILURE_HOURS"
DOCTOR_ENV_MAIL_STALE = "CQ_DOCTOR_MAIL_STALE_HOURS"
DOCTOR_ENV_RETENTION_DAYS = "CQ_RETENTION_DAYS"
DOCTOR_ENV_BACKUP_STATUS_FILE = "CQ_BACKUP_STATUS_FILE"

# Os motivos das mensagens das 3 globais de backup, e-mail e retenção e o da falha de
# `bots.master_key` (`token_unreadable`).
ServerCheckReason = Literal["not_configured", "unreadable", "age", "never", "connection", "tls", "auth",
                            "stale", "ok", "overdue", "token_unreadable"]
SERVER_CHECK_REASONS: tuple[str, ...] = get_args(ServerCheckReason)


@dataclass(frozen=True)
class DoctorSettings:
    """Os limites do `doctor`: 5 min para a consulta do bot e as tarefas atrasadas, 5 s para o
    desvio do relógio. Nenhum é segredo.

    `token_cipher` não é limite nem vem do ambiente (o `from_env` o deixa `None`): é a
    cifra dos tokens dos bots, posta pela composição (`__main__` e o `create_app` sem `doctor_settings`), com que o
    `collect` testa a chave-mestra. Fica fora do `repr`; o `TokenCipher` guarda só as chaves derivadas."""

    bot_poll_seconds: float = 300
    task_overdue_seconds: float = 300
    clock_skew_seconds: float = 5.0
    # No fim e com padrão. `backup_status_file` `None` = backup não
    # configurado nesta pilha.
    backup_status_file: str | None = None
    backup_warning_hours: float = 26
    backup_failure_hours: float = 50
    mail_stale_hours: float = 13
    retention_days: int = 30
    # No fim e com padrão; `None` = a chave não é testada (o resultado "ok").
    token_cipher: TokenCipher | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DoctorSettings:
        """Lê `CQ_DOCTOR_*`, `CQ_RETENTION_DAYS` e `CQ_BACKUP_STATUS_FILE` de `env` (ausente ou vazia → padrão;
        cada uma por si); inválido → `ValueError` de texto fixo, que cita a variável e nunca o valor.
        `token_cipher` fica `None`: não vem do ambiente."""
        defaults = cls()
        warning_hours = _positive_number(env, DOCTOR_ENV_BACKUP_WARNING, defaults.backup_warning_hours, "horas")
        failure_hours = _positive_number(env, DOCTOR_ENV_BACKUP_FAILURE, defaults.backup_failure_hours, "horas")
        if not warning_hours < failure_hours:
            raise ValueError(f"{DOCTOR_ENV_BACKUP_WARNING} deve ser menor que {DOCTOR_ENV_BACKUP_FAILURE}")
        return cls(
            bot_poll_seconds=_positive_seconds(env, DOCTOR_ENV_BOT_POLL, defaults.bot_poll_seconds),
            task_overdue_seconds=_positive_seconds(env, DOCTOR_ENV_TASK_OVERDUE,
                                                   defaults.task_overdue_seconds),
            clock_skew_seconds=_positive_seconds(env, DOCTOR_ENV_CLOCK_SKEW, defaults.clock_skew_seconds),
            backup_status_file=_absolute_path(env, DOCTOR_ENV_BACKUP_STATUS_FILE),
            backup_warning_hours=warning_hours,
            backup_failure_hours=failure_hours,
            mail_stale_hours=_positive_number(env, DOCTOR_ENV_MAIL_STALE, defaults.mail_stale_hours, "horas"),
            retention_days=_retention_days(env, DOCTOR_ENV_RETENTION_DAYS, defaults.retention_days),
        )


def _positive_seconds(env: Mapping[str, str], name: str, default: float) -> float:
    """Um número positivo finito de segundos; ausente ou vazio → `default` (como `config._optional`).
    A mensagem do erro traz só o nome da variável, nunca o valor (nem pela exceção de origem)."""
    return _positive_number(env, name, default, "segundos")


def _positive_number(env: Mapping[str, str], name: str, default: float, unit: str) -> float:
    """Um número positivo e finito (de `unit`); ausente ou vazio → `default`. Inválido → `ValueError` com o mesmo
    texto para todo valor, só com o nome da variável."""
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} deve ser um número de {unit} positivo e finito")
    return value


# `CQ_RETENTION_DAYS` é um inteiro decimal (só dígitos ASCII) de 1 a 3650.
RETENTION_DAYS_MIN = 1
RETENTION_DAYS_MAX = 3650
_DECIMAL_DIGITS = re.compile(r"[0-9]+")


def _retention_days(env: Mapping[str, str], name: str, default: int) -> int:
    """Os dias do prazo de retenção; ausente ou vazio → `default`. `30.0`, `0`, `-1`, `abc`, `1e2` e fora da faixa
    → `ValueError` de texto fixo, sem o valor."""
    raw = env.get(name)
    if not raw:
        return default
    text = raw.strip()
    value = int(text) if _DECIMAL_DIGITS.fullmatch(text) else 0
    if not RETENTION_DAYS_MIN <= value <= RETENTION_DAYS_MAX:
        raise ValueError(f"{name} deve ser um número inteiro de dias, de {RETENTION_DAYS_MIN} a {RETENTION_DAYS_MAX}")
    return value


def _absolute_path(env: Mapping[str, str], name: str) -> str | None:
    """Um caminho absoluto; ausente ou vazio → `None` (backup não configurado). Relativo → `ValueError` sem o
    valor. Aceita o absoluto do sistema em que roda e o POSIX (o da `api` no contêiner, `/run/cq-backup/…`)."""
    raw = env.get(name)
    if not raw:
        return None
    if not (os.path.isabs(raw) or posixpath.isabs(raw)):
        raise ValueError(f"{name} deve ser um caminho absoluto")
    return raw


@dataclass(frozen=True)
class DoctorCheck:
    """Uma verificação do servidor: `id` entre os 9 de `ServerCheckId`,
    `result` entre os 3 de `ServerCheckResult` e a mensagem fixa (`DOCTOR_MESSAGES` ou `DOCTOR_REASON_MESSAGES`).
    `offering` (o código) em `bot.*` e no "aviso" de `retention.overdue`; `count` em `tasks.overdue`, `backup.age`
    (as horas, só com registro legível) e `retention.overdue`; nulos nas demais. Quem confere as regras por id na
    resposta é o `DoctorCheckOut` da API."""

    id: str
    result: str
    message: str
    offering: str | None
    count: int | None


# As mensagens fixas por `(id, result)`; em português, sem detalhe técnico.
DOCTOR_MESSAGES: Mapping[tuple[str, str], str] = {
    ("bots.master_key", "ok"): "Chave-mestra dos bots configurada.",
    ("bots.master_key", "warning"):
        "Chave-mestra dos bots ausente: os bots não podem ser registrados nem consultados.",
    ("bot.access", "ok"): "Bot com acesso ao Telegram.",
    ("bot.access", "failure"): 'Bot sem acesso ao Telegram: troque o token com "cq bots register".',
    ("bot.poll", "ok"): "Bot consultado recentemente.",
    ("bot.poll", "warning"): "Bot sem consulta recente: o worker pode estar parado ou sem rede.",
    ("bot.lease", "ok"): "Bot atendido por um worker.",
    ("bot.lease", "warning"): "Nenhum worker está atendendo o bot.",
    ("tasks.overdue", "ok"): "Nenhuma tarefa atrasada.",
    ("tasks.overdue", "warning"): "Há tarefas atrasadas: o worker pode estar parado.",
    ("clock.skew", "ok"): "Relógio da API em sincronia com o banco.",
    ("clock.skew", "warning"): "Relógio da API fora de sincronia com o banco.",
}


# As mensagens fixas das 3 globais de backup, e-mail e retenção por `(id, result, motivo)`. As de
# `backup.age` com motivo `age` são o modelo `BACKUP_AGE_TEMPLATE` (formatado com as horas inteiras). E
# também a falha de `bots.master_key` (um token gravado não decifra); o "ok" e o "aviso" dela
# ficam em `DOCTOR_MESSAGES`, e os pares `(id, result)` das duas tabelas não se repetem.
BACKUP_AGE_TEMPLATE = "Último backup há {hours} h."

DOCTOR_REASON_MESSAGES: Mapping[tuple[str, str, str], str] = {
    ("bots.master_key", "failure", "token_unreadable"):
        "Chave-mestra dos bots não decifra os tokens gravados: use a chave com que os bots foram registrados.",
    ("backup.age", "warning", "not_configured"): "Backup não configurado nesta pilha.",
    ("backup.age", "failure", "unreadable"): "Sem registro legível do último backup.",
    ("backup.age", "ok", "age"): BACKUP_AGE_TEMPLATE,
    ("backup.age", "warning", "age"): BACKUP_AGE_TEMPLATE,
    ("backup.age", "failure", "age"): BACKUP_AGE_TEMPLATE,
    ("smtp.server", "warning", "never"): "O servidor de e-mail ainda não foi testado.",
    ("smtp.server", "warning", "not_configured"): "E-mail não configurado no worker.",
    ("smtp.server", "failure", "connection"): "Sem conexão com o servidor de e-mail.",
    ("smtp.server", "failure", "tls"): "Falha na negociação TLS com o servidor de e-mail.",
    ("smtp.server", "failure", "auth"): "O servidor de e-mail recusou a autenticação.",
    ("smtp.server", "warning", "stale"): "O último teste do servidor de e-mail é antigo.",
    ("smtp.server", "ok", "ok"): "O servidor de e-mail aceitou a conexão e a autenticação.",
    ("retention.overdue", "ok", "ok"): "Nenhum oferecimento arquivado com prazo de retenção vencido.",
    ("retention.overdue", "warning", "overdue"):
        "Há oferecimentos arquivados com prazo de retenção vencido sem expurgo.",
}


# O estado do registro do backup, como o `collect` o lê.
@dataclass(frozen=True)
class Unreadable:
    """Backup configurado, mas sem registro legível (ausente, ilegível, fora do formato ou no futuro)."""


@dataclass(frozen=True)
class Finished:
    """Registro legível: a hora de término do último backup (UTC)."""

    finished_at: datetime


BackupStatus = Literal["not_configured"] | Unreadable | Finished

# O tamanho máximo do registro lido pela API.
BACKUP_RECORD_MAX_BYTES = 4096


def read_backup_status(path: str | None, *, now: datetime, clock_skew_seconds: float,
                       max_bytes: int = BACKUP_RECORD_MAX_BYTES) -> BackupStatus:
    """O estado do registro do backup em `path`: `None` ou vazio → `"not_configured"`;
    qualquer desvio, inclusive `finished_at` mais de `clock_skew_seconds` depois de `now` → `Unreadable()`;
    válido → `Finished(finished_at)`. Nunca levanta exceção para o chamador.

    Só arquivo regular, lido até `max_bytes` (um byte a mais = ilegível); UTF-8 estrito; objeto JSON com exatamente
    as chaves do registro e os tipos exatos (`bool` não conta como inteiro). A fronteira do futuro é estrita: exatamente
    `clock_skew_seconds` depois de `now` ainda é legível.

    A mesma leitura do `read_record` da rotina (`ops/backup/cq_backup.py`):
    chave JSON repetida em qualquer objeto e carimbo de calendário impossível no `file` também são ilegíveis."""
    if not path:
        return "not_configured"
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return Unreadable()
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            return Unreadable()
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        finished_at = _valid_record_finished_at(record)
    except (OSError, ValueError, RecursionError):
        # `ValueError` cobre o `UnicodeDecodeError` e o `JSONDecodeError`; `RecursionError`, um JSON aninhado demais.
        return Unreadable()
    if finished_at is None or finished_at - now > timedelta(seconds=clock_skew_seconds):
        return Unreadable()
    return Finished(finished_at=finished_at)


# Os formatos dos campos do registro (só dígitos ASCII; `fullmatch`, sem o `$` que aceita `\n`).
_RECORD_KEYS = frozenset({"version", "finished_at", "file", "size_bytes", "sha256"})
_RECORD_FINISHED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_RECORD_FILE = re.compile(r"cq-[0-9]{8}T[0-9]{6}Z\.dump\.age")
_RECORD_SHA256 = re.compile(r"[0-9a-f]{64}")


def _valid_record_finished_at(record: object) -> datetime | None:
    """O `finished_at` (UTC, com fuso) de um registro no formato do backup; `None` com qualquer desvio."""
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        return None
    version, finished, file, size, digest = (record[key] for key in
                                             ("version", "finished_at", "file", "size_bytes", "sha256"))
    if type(version) is not int or version != 1:
        return None
    if type(size) is not int or size <= 0:
        return None
    if not (isinstance(file, str) and _RECORD_FILE.fullmatch(file)):
        return None
    if not (isinstance(digest, str) and _RECORD_SHA256.fullmatch(digest)):
        return None
    if not (isinstance(finished, str) and _RECORD_FINISHED_AT.fullmatch(finished)):
        return None
    # Data inexistente (30 de fevereiro) no carimbo do `file` ou no `finished_at` → `ValueError`, tratado por quem
    # chama. O carimbo do `file` é lido como no `parse_dump_name` da rotina.
    datetime.strptime(file[3:18], "%Y%m%dT%H%M%S")
    return datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """O `object_pairs_hook` do registro: chave repetida → `ValueError` de texto fixo, sem a chave (como
    o `_no_duplicate_keys` da rotina)."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("chave repetida")
        result[key] = value
    return result


def collect(conn: psycopg.Connection, *, now: datetime, master_key_configured: bool,
            settings: DoctorSettings) -> list[DoctorCheck]:
    """As verificações do servidor, na ordem fixa (só leitura; o `clock_timestamp()` do banco
    na mesma conexão).

    Só `SELECT`: nenhuma trava, nenhum evento, nenhuma escrita e nenhuma chamada externa. A
    transação é a de quem chama (a rota abre `in_transaction`)."""
    # Globais, na ordem de `SERVER_CHECK_IDS` restrita às globais, e as 3 de backup, e-mail e retenção depois
    # de `clock.skew`.
    checks = [_master_key_check(conn, master_key_configured, settings.token_cipher)]

    overdue_before = now - timedelta(seconds=settings.task_overdue_seconds)
    count_row = conn.execute(
        "SELECT count(*) FROM tasks WHERE state = 'pending' AND available_at < %s",
        (overdue_before,)).fetchone()
    overdue = int(count_row[0]) if count_row is not None else 0
    checks.append(_check("tasks.overdue", "warning" if overdue > 0 else "ok", count=overdue))

    # O relógio do banco lido na mesma conexão, no instante da verificação (não o início da
    # transação, que `now()` daria).
    db_row = conn.execute("SELECT clock_timestamp()").fetchone()
    skew = abs((now - db_row[0]).total_seconds()) if db_row is not None else math.inf
    checks.append(_check("clock.skew", "ok" if skew <= settings.clock_skew_seconds else "warning"))

    # O registro do backup, a linha de `mail_probe` e a retenção.
    checks.append(_backup_age_check(read_backup_status(
        settings.backup_status_file, now=now, clock_skew_seconds=settings.clock_skew_seconds), now, settings))
    checks.append(_smtp_server_check(read_probe(conn), now, settings))
    checks.append(_retention_check(conn, now, settings))

    # Por oferecimento: só `active` e `closed` com bot (rascunho e arquivado fora); identificado só pelo
    # código. A ordem pelo código é feita aqui, e não pelo `ORDER BY`, para não depender da collation.
    rows = conn.execute(
        "SELECT o.code, o.state, b.state, b.last_poll_ok_at, b.lease_owner, b.lease_until "
        "FROM offerings o JOIN bots b ON b.offering_id = o.id "
        "WHERE o.state IN ('active', 'closed')").fetchall()
    poll_since = now - timedelta(seconds=settings.bot_poll_seconds)
    per_offering: list[DoctorCheck] = []
    for code, offering_state, bot_state, last_poll_ok_at, lease_owner, lease_until in rows:
        registered = bot_state == "registered"
        per_offering.append(_check("bot.access", "ok" if registered else "failure", offering=code))
        if offering_state != "active" or not registered:
            # Sem acesso não há consulta nem posse a verificar; o encerrado não é atendido.
            continue
        leased = lease_owner is not None and lease_until is not None and lease_until > now
        per_offering.append(_check("bot.lease", "ok" if leased else "warning", offering=code))
        polled = last_poll_ok_at is not None and last_poll_ok_at >= poll_since
        per_offering.append(_check("bot.poll", "ok" if polled else "warning", offering=code))
    per_offering.sort(key=lambda check: (check.offering or "", check.id))
    return checks + per_offering


def _check(check_id: str, result: str, *, offering: str | None = None,
           count: int | None = None) -> DoctorCheck:
    """Uma verificação com a mensagem fixa de `(id, result)`."""
    return DoctorCheck(id=check_id, result=result, message=DOCTOR_MESSAGES[(check_id, result)],
                       offering=offering, count=count)


def _reason_check(check_id: str, result: str, reason: str, *, offering: str | None = None,
                  count: int | None = None) -> DoctorCheck:
    """Uma verificação com a mensagem fixa de `(id, result, motivo)`; a idade do backup formata as horas."""
    message = DOCTOR_REASON_MESSAGES[(check_id, result, reason)]
    if (check_id, reason) == ("backup.age", "age"):
        message = message.format(hours=count)
    return DoctorCheck(id=check_id, result=result, message=message, offering=offering, count=count)


def _master_key_check(conn: psycopg.Connection, master_key_configured: bool,
                      cipher: TokenCipher | None) -> DoctorCheck:
    """`bots.master_key`: sem a chave, "aviso"; sem a cifra no `DoctorSettings`,
    "ok" (a chave não é testada); senão decifra o token de cada linha de `bots` (`ORDER BY id`) e sai
    "falha" no primeiro `UnreadableToken`. O texto decifrado é descartado na hora; o motivo da exceção não sai."""
    if not master_key_configured:
        return _check("bots.master_key", "warning")
    if cipher is not None:
        rows = conn.execute("SELECT bot_uuid, telegram_bot_id, token_ciphertext FROM bots ORDER BY id").fetchall()
        for bot_uuid, telegram_bot_id, blob in rows:
            try:
                cipher.decrypt(bytes(blob), bot_uuid=bot_uuid, telegram_bot_id=int(telegram_bot_id))
            except UnreadableToken:
                return _reason_check("bots.master_key", "failure", "token_unreadable")
    return _check("bots.master_key", "ok")


_HOUR = timedelta(hours=1)


def _backup_age_check(status: BackupStatus, now: datetime, settings: DoctorSettings) -> DoctorCheck:
    """`backup.age`: a idade exata comparada com os limites (`> aviso`, `> falha`); o `count` é
    o piso das horas (0 com o término até `clock_skew_seconds` no futuro, que `read_backup_status` aceita)."""
    if status == "not_configured":
        return _reason_check("backup.age", "warning", "not_configured")
    if not isinstance(status, Finished):
        return _reason_check("backup.age", "failure", "unreadable")
    age = max(now - status.finished_at, timedelta(0))
    if age <= timedelta(hours=settings.backup_warning_hours):
        result = "ok"
    elif age <= timedelta(hours=settings.backup_failure_hours):
        result = "warning"
    else:
        result = "failure"
    return _reason_check("backup.age", result, "age", count=age // _HOUR)


# O resultado gravado pelo worker → `(result, motivo)`; o `ok` depende da idade.
_SMTP_RESULTS: Mapping[str, tuple[str, str]] = {
    "not_configured": ("warning", "not_configured"),
    "connection": ("failure", "connection"),
    "tls": ("failure", "tls"),
    "auth": ("failure", "auth"),
}


def _smtp_server_check(row: tuple[str, datetime] | None, now: datetime, settings: DoctorSettings) -> DoctorCheck:
    """`smtp.server`: sem linha, "aviso" `never`; `ok` mais velho que `mail_stale_hours`
    (estrito), "aviso" `stale`; os demais pelo resultado gravado."""
    if row is None:
        return _reason_check("smtp.server", "warning", "never")
    probe_result, checked_at = row
    if probe_result == "ok":
        if now - checked_at > timedelta(hours=settings.mail_stale_hours):
            return _reason_check("smtp.server", "warning", "stale")
        return _reason_check("smtp.server", "ok", "ok")
    result, reason = _SMTP_RESULTS[probe_result]
    return _reason_check("smtp.server", result, reason)


def _retention_check(conn: psycopg.Connection, now: datetime, settings: DoctorSettings) -> DoctorCheck:
    """`retention.overdue`: os arquivados sem expurgo com `archived_at` antes de `now −
    retention_days × 24 h`; `count` e o código do de menor `(archived_at, id)`. Só `SELECT`; nenhuma ação."""
    row = conn.execute(
        "SELECT code, count(*) OVER () FROM offerings "
        "WHERE state = 'archived' AND purged_at IS NULL AND archived_at < %s "
        "ORDER BY archived_at, id LIMIT 1",
        (now - timedelta(hours=24 * settings.retention_days),)).fetchone()
    if row is None:
        return _reason_check("retention.overdue", "ok", "ok", count=0)
    return _reason_check("retention.overdue", "warning", "overdue", offering=row[0], count=int(row[1]))
