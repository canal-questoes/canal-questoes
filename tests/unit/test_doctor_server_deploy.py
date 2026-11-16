"""O `doctor` do servidor no piloto: o registro do backup, a regra da idade, a configuração nova e as mensagens por
motivo.


Cobre, sem banco onde possível:
- `read_backup_status(path, *, now, clock_skew_seconds)`: `None` ou vazio → `"not_configured"`; arquivo
  inexistente, diretório, vazio, maior que 4096 bytes, bytes que não são UTF-8, JSON inválido, objeto com chave a
  mais ou a menos, tipo errado, valor fora do formato do registro e `finished_at` mais de
  `clock_skew_seconds` no futuro → `Unreadable()`, sem exceção para o chamador; válido → `Finished(finished_at)`;
- a regra da idade, pelo `collect` com o banco de teste (o único lugar onde ela é montada):
  `floor((now − finished_at) / 1 h)` no `count`, os limites comparando a idade
  **exata** (2 h ok, 26 h exatas ok, 26 h + 1 s aviso, 30 h aviso, 50 h exatas aviso, 50 h + 1 s falha, 60 h
  falha), a mensagem `BACKUP_AGE_TEMPLATE.format(hours=count)`; sem configuração, "aviso" `not_configured`; com
  registro ilegível, "falha" `unreadable`; `count` nulo sem registro legível;
- `DoctorSettings.from_env`: padrões 26, 50, 13 e 30; os números positivos e finitos das horas;
  `warning ≥ failure` recusado; `CQ_RETENTION_DAYS` inteiro decimal de 1 a 3650; `CQ_BACKUP_STATUS_FILE` vazio =
  ausente e relativo recusado; toda recusa com `ValueError` de texto fixo (o mesmo para todo valor
  inválido da variável), sem o valor;
- `DOCTOR_REASON_MESSAGES` com exatamente as chaves `(id, result, motivo)` esperadas para as 3
  globais novas e, na verificação da chave-mestra, a falha de `bots.master_key` (motivo
  `token_unreadable`), sem repetir nenhum par `(id, result)` de `DOCTOR_MESSAGES`, e `retention.overdue` sem nenhuma
  chave com `failure`; o campo `token_cipher` do `DoctorSettings` com padrão `None`, fora do `repr` e fora
  do `from_env`. O comportamento da chave pelo `collect` fica em `tests/integration/test_doctor_api_deploy.py`.

**No esqueleto:** `read_backup_status` lança `NotImplementedError`; o `collect` ainda não emite
`backup.age` e o `from_env` ainda não lê as variáveis novas (até a implementação). **O que já passa:** as
tabelas reais e os padrões do `DoctorSettings()`, que já valem no esqueleto, ficam na mesma função de um caso
que falha no esqueleto (uma chamada a `read_backup_status` ou ao `from_env` com valor).

Escolhas deste teste ([I]): a fronteira do futuro é estrita como a da idade (`finished_at` exatamente
`clock_skew_seconds` depois de `now` ainda é legível; um segundo além, ilegível); os registros válidos dos testes
usam horários em segundos inteiros, como a rotina os grava.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cq_server.doctor import (
    BACKUP_AGE_TEMPLATE,
    DOCTOR_MESSAGES,
    DOCTOR_ENV_BACKUP_FAILURE,
    DOCTOR_ENV_BACKUP_STATUS_FILE,
    DOCTOR_ENV_BACKUP_WARNING,
    DOCTOR_ENV_MAIL_STALE,
    DOCTOR_ENV_RETENTION_DAYS,
    DOCTOR_REASON_MESSAGES,
    DoctorSettings,
    Finished,
    Unreadable,
    read_backup_status,
)
from cq_server.secretbox import TokenCipher
from tests.helpers.secrets import MASTER_KEY

NOW = datetime(2031, 3, 1, 12, 0, tzinfo=timezone.utc)
SKEW = 5.0

VALID_RECORD: dict[str, Any] = {
    "version": 1,
    "finished_at": "2031-03-01T09:30:00Z",
    "file": "cq-20310301T093000Z.dump.age",
    "size_bytes": 123456,
    "sha256": "0123456789abcdef" * 4,
}
VALID_FINISHED_AT = datetime(2031, 3, 1, 9, 30, tzinfo=timezone.utc)

# as chaves `(id, result, motivo)` das 3 globais novas, escritas aqui por extenso, e a falha de
# `bots.master_key` quando a chave-mestra não decifra os tokens.
EXPECTED_REASON_KEYS = {
    ("bots.master_key", "failure", "token_unreadable"),
    ("backup.age", "warning", "not_configured"),
    ("backup.age", "failure", "unreadable"),
    ("backup.age", "ok", "age"),
    ("backup.age", "warning", "age"),
    ("backup.age", "failure", "age"),
    ("smtp.server", "warning", "never"),
    ("smtp.server", "warning", "not_configured"),
    ("smtp.server", "failure", "connection"),
    ("smtp.server", "failure", "tls"),
    ("smtp.server", "failure", "auth"),
    ("smtp.server", "warning", "stale"),
    ("smtp.server", "ok", "ok"),
    ("retention.overdue", "ok", "ok"),
    ("retention.overdue", "warning", "overdue"),
}


def _write(tmp_path: Path, content: bytes | str | dict | list | None, name: str = "last-backup.json") -> str:
    """Grava o registro em `tmp_path/name` (dict e list como JSON) e devolve o caminho absoluto, como texto."""
    path = tmp_path / name
    if isinstance(content, (dict, list)) or content is None:
        path.write_text(json.dumps(content), encoding="utf-8")
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return str(path)


def _status(path: str | None, *, now: datetime = NOW, skew: float = SKEW) -> Any:
    return read_backup_status(path, now=now, clock_skew_seconds=skew)


# --- read_backup_status --------------------------------------------------------------------------------------


def test_reason_message_table_has_exactly_the_data_model_keys() -> None:
    """`DOCTOR_REASON_MESSAGES` tem exatamente as 15 chaves esperadas (as 14 das globais novas e a
    falha de `bots.master_key`); nenhum par `(id, result)` dela está em `DOCTOR_MESSAGES` (o "ok" e o
    "aviso" de `bots.master_key` continuam lá, sem mudança); `retention.overdue` nunca dá `failure`; o modelo da
    idade é o esperado; as mensagens de `backup.age` com motivo `age` são o modelo. Chave-mestra: o `token_cipher` do
    `DoctorSettings` tem padrão `None`, não entra no `repr` (nem a chave) e não vem do `from_env`.
    Na mesma função, o caso que falha no esqueleto: `read_backup_status` sem configuração."""
    assert set(DOCTOR_REASON_MESSAGES) == EXPECTED_REASON_KEYS
    assert {(check_id, result) for check_id, result, _ in DOCTOR_REASON_MESSAGES}.isdisjoint(DOCTOR_MESSAGES)
    assert {("bots.master_key", "ok"), ("bots.master_key", "warning")} <= set(DOCTOR_MESSAGES)
    assert DoctorSettings().token_cipher is None
    assert DoctorSettings.from_env({"CQ_MASTER_KEY": MASTER_KEY}).token_cipher is None
    with_cipher = DoctorSettings(token_cipher=TokenCipher(MASTER_KEY))
    assert "token_cipher" not in repr(with_cipher) and MASTER_KEY not in repr(with_cipher)
    assert not [key for key in DOCTOR_REASON_MESSAGES if key[0] == "retention.overdue" and key[1] == "failure"]
    assert BACKUP_AGE_TEMPLATE == "Último backup há {hours} h."
    for result in ("ok", "warning", "failure"):
        assert DOCTOR_REASON_MESSAGES[("backup.age", result, "age")] == BACKUP_AGE_TEMPLATE
    assert all(isinstance(text, str) and text.strip() for text in DOCTOR_REASON_MESSAGES.values())
    # O caso que falha no esqueleto, na mesma função.
    assert _status(None) == "not_configured"


@pytest.mark.parametrize("path", [None, ""], ids=["none", "vazio"])
def test_unconfigured_status(path: str | None) -> None:
    """`None` ou vazio (a variável ausente ou vazia): `"not_configured"`, sem tocar em arquivo."""
    assert _status(path) == "not_configured"


def test_valid_record_is_finished(tmp_path: Path) -> None:
    """Registro válido: `Finished` com o `finished_at` em UTC, com fuso."""
    status = _status(_write(tmp_path, VALID_RECORD))
    assert status == Finished(finished_at=VALID_FINISHED_AT)
    assert isinstance(status, Finished)
    assert status.finished_at.tzinfo is not None
    assert status.finished_at.utcoffset() == timedelta(0)


def test_record_of_exactly_max_bytes_is_still_read(tmp_path: Path) -> None:
    """O limite é inclusivo: um registro válido com espaços até exatamente 4096 bytes ainda é lido."""
    text = json.dumps(VALID_RECORD)
    padded = text[:-1] + " " * (4096 - len(text.encode("utf-8"))) + "}"
    assert len(padded.encode("utf-8")) == 4096
    assert _status(_write(tmp_path, padded)) == Finished(finished_at=VALID_FINISHED_AT)


def _with(**changes: Any) -> dict[str, Any]:
    record = dict(VALID_RECORD)
    record.update(changes)
    return record


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in VALID_RECORD.items() if k != key}


_TOO_BIG = json.dumps(VALID_RECORD)[:-1] + " " * 4096 + "}"

UNREADABLE_CONTENTS: list[Any] = [
    pytest.param(b"", id="vazio"),
    pytest.param(_TOO_BIG, id="maior-que-4096"),
    pytest.param(b"\xff\xfe{\"version\": 1}", id="nao-utf8"),
    pytest.param("{\"version\": 1,", id="json-invalido"),
    pytest.param("nada de json", id="texto"),
    pytest.param(None, id="null"),
    pytest.param([VALID_RECORD], id="lista"),
    pytest.param("\"cq-20310301T093000Z.dump.age\"", id="texto-json"),
    pytest.param(_with(extra="x"), id="chave-a-mais"),
    *[pytest.param(_without(key), id=f"sem-{key}") for key in VALID_RECORD],
    pytest.param(_with(version=2), id="versao-2"),
    pytest.param(_with(version="1"), id="versao-texto"),
    pytest.param(_with(version=True), id="versao-booleana"),
    pytest.param(_with(version=1.0), id="versao-real"),
    pytest.param(_with(finished_at="2031-03-01T09:30:00+00:00"), id="finished-at-com-offset"),
    pytest.param(_with(finished_at="2031-03-01 09:30:00Z"), id="finished-at-sem-T"),
    pytest.param(_with(finished_at="2031-03-01T09:30:00.123Z"), id="finished-at-com-fracao"),
    pytest.param(_with(finished_at="2031-02-30T09:30:00Z"), id="finished-at-data-inexistente"),
    pytest.param(_with(finished_at=1930000000), id="finished-at-numero"),
    pytest.param(_with(file="cq-20310301T093000Z.dump"), id="arquivo-sem-age"),
    pytest.param(_with(file="../cq-20310301T093000Z.dump.age"), id="arquivo-com-caminho"),
    pytest.param(_with(file=7), id="arquivo-numero"),
    pytest.param(_with(size_bytes=0), id="tamanho-zero"),
    pytest.param(_with(size_bytes=-1), id="tamanho-negativo"),
    pytest.param(_with(size_bytes="123"), id="tamanho-texto"),
    pytest.param(_with(size_bytes=1.5), id="tamanho-real"),
    pytest.param(_with(size_bytes=True), id="tamanho-booleano"),
    pytest.param(_with(sha256="0123456789ABCDEF" * 4), id="sha256-maiusculo"),
    pytest.param(_with(sha256="0123456789abcdef" * 3), id="sha256-curto"),
    pytest.param(_with(sha256=None), id="sha256-nulo"),
]


@pytest.mark.parametrize("content", UNREADABLE_CONTENTS)
def test_unreadable_record_contents(tmp_path: Path, content: Any) -> None:
    """qualquer desvio do formato é "registro ilegível", sem exceção para o chamador."""
    assert _status(_write(tmp_path, content)) == Unreadable()


def test_missing_file_and_directory_are_unreadable(tmp_path: Path) -> None:
    """Configurado e sem arquivo, ou o caminho é um diretório: `Unreadable()`, sem exceção."""
    assert _status(str(tmp_path / "nao-existe" / "last-backup.json")) == Unreadable()
    folder = tmp_path / "um-diretorio"
    folder.mkdir()
    assert _status(str(folder)) == Unreadable()


@pytest.mark.parametrize(("ahead", "readable"), [(0, True), (5, True), (6, False), (3600, False)],
                         ids=["agora", "no-limite", "1s-alem", "1h-alem"])
def test_finished_at_in_the_future_beyond_the_skew_is_unreadable(tmp_path: Path, ahead: int,
                                                                  readable: bool) -> None:
    """`finished_at` mais de `clock_skew_seconds` depois de `now` é ilegível; dentro do desvio, legível."""
    finished = NOW + timedelta(seconds=ahead)
    path = _write(tmp_path, _with(finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  file=finished.strftime("cq-%Y%m%dT%H%M%SZ.dump.age")))
    status = _status(path)
    assert status == (Finished(finished_at=finished) if readable else Unreadable())


def test_skew_comes_from_the_argument(tmp_path: Path) -> None:
    """O desvio aceito é o do argumento: 60 s no futuro é legível com 120 s e ilegível com 5 s."""
    finished = NOW + timedelta(seconds=60)
    path = _write(tmp_path, _with(finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ")))
    assert _status(path, skew=120.0) == Finished(finished_at=finished)
    assert _status(path, skew=5.0) == Unreadable()


# --- a regra da idade, pelo `collect` --------------------------------------------------------------------------


def _backup_check(conn: psycopg.Connection, settings: DoctorSettings, now: datetime) -> Any:
    """A única verificação `backup.age` do `collect`."""
    from cq_server.doctor import collect

    checks = [c for c in collect(conn, now=now, master_key_configured=True, settings=settings)
              if c.id == "backup.age"]
    assert len(checks) == 1, f"o collect devolveu {len(checks)} verificações backup.age"
    return checks[0]


def _record_at(tmp_path: Path, finished: datetime) -> str:
    return _write(tmp_path, _with(finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  file=finished.strftime("cq-%Y%m%dT%H%M%SZ.dump.age")))


@pytest.mark.parametrize(("age", "result", "hours"), [
    (timedelta(hours=2), "ok", 2),
    (timedelta(hours=2, minutes=59, seconds=59), "ok", 2),
    (timedelta(hours=26), "ok", 26),
    (timedelta(hours=26, seconds=1), "warning", 26),
    (timedelta(hours=30), "warning", 30),
    (timedelta(hours=50), "warning", 50),
    (timedelta(hours=50, seconds=1), "failure", 50),
    (timedelta(hours=60), "failure", 60),
    (timedelta(0), "ok", 0),
], ids=["2h", "2h59m59s", "26h", "26h+1s", "30h", "50h", "50h+1s", "60h", "0h"])
def test_backup_age_rule(sql: psycopg.Connection, tmp_path: Path, age: timedelta, result: str,
                         hours: int) -> None:
    """a idade compara exata com os limites (`> 26 h` aviso, `> 50 h` falha); o `count` é o piso
    das horas; a mensagem é o modelo com as horas; nada de `offering`."""
    check = _backup_check(sql, DoctorSettings(backup_status_file=_record_at(tmp_path, NOW - age)), NOW)
    assert (check.result, check.count, check.offering) == (result, hours, None)
    assert check.message == BACKUP_AGE_TEMPLATE.format(hours=hours)
    assert check.message == DOCTOR_REASON_MESSAGES[("backup.age", result, "age")].format(hours=hours)


def test_backup_age_uses_the_configured_limits(sql: psycopg.Connection, tmp_path: Path) -> None:
    """Os limites vêm do `DoctorSettings`: com 1 h e 2 h, 90 min é aviso e 2 h + 1 s é falha."""
    settings = DoctorSettings(backup_warning_hours=1, backup_failure_hours=2,
                              backup_status_file=_record_at(tmp_path, NOW - timedelta(minutes=90)))
    check = _backup_check(sql, settings, NOW)
    assert (check.result, check.count) == ("warning", 1)
    settings = DoctorSettings(backup_warning_hours=1, backup_failure_hours=2,
                              backup_status_file=_record_at(tmp_path, NOW - timedelta(hours=2, seconds=1)))
    check = _backup_check(sql, settings, NOW)
    assert (check.result, check.count) == ("failure", 2)


def test_backup_age_not_configured_and_unreadable(sql: psycopg.Connection, tmp_path: Path) -> None:
    """Sem configuração: "aviso" `not_configured`; configurado e ilegível (ausente, ou no futuro além do desvio):
    "falha" `unreadable`; nos dois, `count` nulo."""
    check = _backup_check(sql, DoctorSettings(), NOW)
    assert (check.result, check.count, check.offering) == ("warning", None, None)
    assert check.message == DOCTOR_REASON_MESSAGES[("backup.age", "warning", "not_configured")]
    unreadable = DOCTOR_REASON_MESSAGES[("backup.age", "failure", "unreadable")]
    for path in (str(tmp_path / "ausente.json"), _record_at(tmp_path, NOW + timedelta(seconds=SKEW + 60))):
        check = _backup_check(sql, DoctorSettings(backup_status_file=path, clock_skew_seconds=SKEW), NOW)
        assert (check.result, check.count, check.message) == ("failure", None, unreadable)


# --- DoctorSettings.from_env -----------------------------------------------------------------------------------


def _new_fields(settings: DoctorSettings) -> dict[str, Any]:
    return {"backup_status_file": settings.backup_status_file,
            "backup_warning_hours": settings.backup_warning_hours,
            "backup_failure_hours": settings.backup_failure_hours,
            "mail_stale_hours": settings.mail_stale_hours,
            "retention_days": settings.retention_days}


NEW_DEFAULTS = {"backup_status_file": None, "backup_warning_hours": 26, "backup_failure_hours": 50,
                "mail_stale_hours": 13, "retention_days": 30}


def test_defaults_and_valid_values(tmp_path: Path) -> None:
    """Os padrões (26, 50, 13, 30 e sem arquivo) e os nomes das variáveis e,
    na mesma função, o `from_env` lendo valores válidos."""
    assert _new_fields(DoctorSettings()) == NEW_DEFAULTS
    assert (DOCTOR_ENV_BACKUP_WARNING, DOCTOR_ENV_BACKUP_FAILURE, DOCTOR_ENV_MAIL_STALE, DOCTOR_ENV_RETENTION_DAYS,
            DOCTOR_ENV_BACKUP_STATUS_FILE) == ("CQ_DOCTOR_BACKUP_WARNING_HOURS", "CQ_DOCTOR_BACKUP_FAILURE_HOURS",
                                               "CQ_DOCTOR_MAIL_STALE_HOURS", "CQ_RETENTION_DAYS",
                                               "CQ_BACKUP_STATUS_FILE")
    assert _new_fields(DoctorSettings.from_env({})) == NEW_DEFAULTS
    status_file = str(tmp_path / "estado" / "last-backup.json")
    settings = DoctorSettings.from_env({"CQ_DOCTOR_BACKUP_WARNING_HOURS": "12",
                                        "CQ_DOCTOR_BACKUP_FAILURE_HOURS": "48.5",
                                        "CQ_DOCTOR_MAIL_STALE_HOURS": "6.5",
                                        "CQ_RETENTION_DAYS": "7",
                                        "CQ_BACKUP_STATUS_FILE": status_file})
    assert _new_fields(settings) == {"backup_status_file": status_file, "backup_warning_hours": 12.0,
                                     "backup_failure_hours": 48.5, "mail_stale_hours": 6.5, "retention_days": 7}
    assert type(settings.retention_days) is int
    # As outras continuam no padrão.
    assert (settings.bot_poll_seconds, settings.task_overdue_seconds, settings.clock_skew_seconds) == (300, 300, 5.0)


@pytest.mark.parametrize("raw", ["1", "3650", "29"])
def test_retention_days_accepts_decimal_integers_from_1_to_3650(raw: str) -> None:
    """`CQ_RETENTION_DAYS` inteiro decimal de 1 a 3650."""
    assert DoctorSettings.from_env({"CQ_RETENTION_DAYS": raw}).retention_days == int(raw)


def test_empty_values_are_absent() -> None:
    """Vazia = ausente: cada variável nova vazia dá o padrão, e o arquivo vazio dá `None`. E,
    junto, o valor válido de cada uma."""
    empty = {name: "" for name in ("CQ_DOCTOR_BACKUP_WARNING_HOURS", "CQ_DOCTOR_BACKUP_FAILURE_HOURS",
                                   "CQ_DOCTOR_MAIL_STALE_HOURS", "CQ_RETENTION_DAYS", "CQ_BACKUP_STATUS_FILE")}
    assert _new_fields(DoctorSettings.from_env(empty)) == NEW_DEFAULTS
    assert DoctorSettings.from_env({"CQ_DOCTOR_MAIL_STALE_HOURS": "1"}).mail_stale_hours == 1.0


# Valores recusados por variável; os marcados com `True` são distintivos o bastante para a conferência do eco.
HOURS_INVALID = [("0", False), ("-1", False), ("abc", False), ("inf", False), ("nan", False),
                 ("-17.25", True), ("horas-sentinela", True), ("Infinity", True)]
RETENTION_INVALID = [("30.0", True), ("0", False), ("-1", False), ("abc", False), ("3651", True),
                     ("dias-sentinela", True), ("1e2", True), ("3650.5", True)]


def _refusal(env: dict[str, str]) -> str:
    with pytest.raises(ValueError) as info:
        DoctorSettings.from_env(env)
    return str(info.value)


@pytest.mark.parametrize("variable", ["CQ_DOCTOR_BACKUP_WARNING_HOURS", "CQ_DOCTOR_BACKUP_FAILURE_HOURS",
                                      "CQ_DOCTOR_MAIL_STALE_HOURS"])
def test_hours_must_be_positive_and_finite_without_echo(variable: str) -> None:
    """As horas: positivas e finitas; a recusa é `ValueError` com texto fixo (o mesmo para todo valor inválido da
    variável), que cita a variável e nunca o valor."""
    messages = set()
    for raw, distinctive in HOURS_INVALID:
        env = {variable: raw}
        if variable == "CQ_DOCTOR_BACKUP_WARNING_HOURS":
            env["CQ_DOCTOR_BACKUP_FAILURE_HOURS"] = "1000"
        message = _refusal(env)
        messages.add(message)
        if distinctive:
            assert raw not in message, f"eco do valor {raw!r}"
    assert len(messages) == 1, messages
    assert variable in messages.pop()


@pytest.mark.parametrize(("warning", "failure"), [("50", "50"), ("60", "50"), ("50", None), ("27", "26")],
                         ids=["iguais", "aviso-maior", "aviso-igual-ao-padrao-da-falha", "aviso-maior-por-1"])
def test_backup_warning_must_be_below_failure(warning: str, failure: str | None) -> None:
    """`warning ≥ failure` recusado, também contra o padrão da outra; sem eco."""
    env = {"CQ_DOCTOR_BACKUP_WARNING_HOURS": warning}
    if failure is not None:
        env["CQ_DOCTOR_BACKUP_FAILURE_HOURS"] = failure
    message = _refusal(env)
    assert f"={warning}" not in message and f"={failure}" not in message
    accepted = DoctorSettings.from_env({"CQ_DOCTOR_BACKUP_WARNING_HOURS": "49.5"})
    assert (accepted.backup_warning_hours, accepted.backup_failure_hours) == (49.5, 50)


def test_retention_days_refusals_without_echo() -> None:
    """`CQ_RETENTION_DAYS`: `30.0`, `0`, `-1`, `abc`, `3651` (e outros fora do inteiro decimal de 1 a 3650)
    recusados com o mesmo texto fixo, sem o valor."""
    messages = set()
    for raw, distinctive in RETENTION_INVALID:
        message = _refusal({"CQ_RETENTION_DAYS": raw})
        messages.add(message)
        if distinctive:
            assert raw not in message, f"eco do valor {raw!r}"
    assert len(messages) == 1, messages
    assert "CQ_RETENTION_DAYS" in messages.pop()


def test_relative_backup_status_file_is_refused_without_echo(tmp_path: Path) -> None:
    """`CQ_BACKUP_STATUS_FILE` relativo recusado, sem o valor; absoluto aceito como texto."""
    message = _refusal({"CQ_BACKUP_STATUS_FILE": "estado-relativo-sentinela/last-backup.json"})
    assert "estado-relativo-sentinela" not in message
    assert "CQ_BACKUP_STATUS_FILE" in message
    absolute = str(tmp_path / "last-backup.json")
    assert DoctorSettings.from_env({"CQ_BACKUP_STATUS_FILE": absolute}).backup_status_file == absolute


def test_new_fields_have_finite_numbers() -> None:
    """Os números lidos são finitos (o `from_env` não aceita `inf` por outra grafia)."""
    for raw in ("1e999", "-1e999", "+inf", "NaN"):
        _refusal({"CQ_DOCTOR_MAIL_STALE_HOURS": raw})
    settings = DoctorSettings.from_env({"CQ_DOCTOR_MAIL_STALE_HOURS": "0.25"})
    assert math.isfinite(settings.mail_stale_hours) and settings.mail_stale_hours == 0.25
