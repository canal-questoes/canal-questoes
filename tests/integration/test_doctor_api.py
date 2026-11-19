"""Parte da API do `doctor`, `system.doctor`.


`GET /v1/system/doctor`: só leitura, sem evento, sem chamada externa (o
`getMe` nunca é chamado), com a permissão `offerings:read`. Cada verificação provocada isolada, com o
`FakeClock` alinhado ao `clock_timestamp()` do banco de teste imediatamente antes de cada chamada
(`align_clock_to_db`): sem a chave-mestra (`bots.master_key` aviso), bot sem acesso (`bot.access` falha só
para aquele oferecimento), sem consulta recente (`bot.poll` aviso), sem dono e com a posse vencida (`bot.lease`
aviso), tarefa `pending` atrasada (`tasks.overdue` aviso com `count`; `leased` não conta), desvio do relógio
(`clock.set(db_now + 10 s)` → `clock.skew` aviso; com margem folgada → ok); o oferecimento identificado só pelo
código; a ordem fixa; as mensagens exatas de `DOCTOR_MESSAGES`; só oferecimentos `active` e `closed` com bot
(rascunho e arquivado fora); 401 e 403 exatos; sem evento, sem escrita e sem chamada à Bot API falsa; o
`DoctorSettings` injetado em `create_app` muda os limites, e o ambiente do processo não.

As globais são 6, na ordem fixa do
diagnóstico (`backup.age`, `smtp.server` e `retention.overdue` depois das outras 3); estas 3 conferidas pela
tabela `DOCTOR_REASON_MESSAGES`, e as outras por `DOCTOR_MESSAGES[(id, result)]`.

Escolhas deste teste ([I]; fixadas aqui para a implementação):
- a ordem: as globais na ordem fixa do diagnóstico (`bots.master_key`, `tasks.overdue`, `clock.skew`),
  depois as por oferecimento ordenadas **pelo código e pelo `id` da verificação**:
  `bot.access`, `bot.lease`, `bot.poll` de cada código;
- `tasks.overdue` leva a contagem também no "ok" (`count = 0`); as demais, `count = None`;
- `checked_at` é o `now` da API (`ctx.clock.now()`);
- os limites são estritos: `bot.poll` ok com `last_poll_ok_at >= now − bot_poll`; tarefa atrasada com
  `available_at < now − task_overdue`. Os casos de fronteira usam margem folgada (1 s ou mais), porque o `now` da
  chamada é o do `FakeClock`, e o banco não participa dessas comparações.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES, DoctorSettings
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_token,
    offering_ref,
    register_bot,
    system_doctor,
)
from tests.helpers.cli_runner import align_clock_to_db
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches

WORKER = "w-doctor-teste"
DOCTOR = "/v1/system/doctor"


# --- montagem ------------------------------------------------------------------------------------------------


def _check(check_id: str, result: str, offering: str | None = None, count: int | None = None) -> dict:
    return {"id": check_id, "result": result, "message": DOCTOR_MESSAGES[(check_id, result)],
            "offering": offering, "count": count}


def _reason_check(check_id: str, result: str, reason: str, offering: str | None = None,
                  count: int | None = None) -> dict:
    """As verificações com motivo levam a mensagem fixa de `(id, result, motivo)`."""
    return {"id": check_id, "result": result, "message": DOCTOR_REASON_MESSAGES[(check_id, result, reason)],
            "offering": offering, "count": count}


# As 6 globais, na ordem. O banco de teste não tem configuração de
# backup (o `DoctorSettings()` padrão, sem `backup_status_file`) nem linha em `mail_probe`, e nenhum oferecimento
# arquivado nestes casos chega a 30 dias: `backup.age` aviso `not_configured`, `smtp.server` aviso `never` e
# `retention.overdue` ok com `count` 0.
NEW_GLOBAL_IDS = ("backup.age", "smtp.server", "retention.overdue")


def _globals(*, master_key: str = "ok", overdue: int = 0, skew: str = "ok") -> list[dict]:
    return [_check("bots.master_key", master_key),
            _check("tasks.overdue", "warning" if overdue else "ok", count=overdue),
            _check("clock.skew", skew),
            _reason_check("backup.age", "warning", "not_configured"),
            _reason_check("smtp.server", "warning", "never"),
            _reason_check("retention.overdue", "ok", "ok", count=0)]


def _active(code: str, *, access: str = "ok", lease: str = "ok", poll: str = "ok") -> list[dict]:
    return [_check("bot.access", access, code), _check("bot.lease", lease, code), _check("bot.poll", poll, code)]


def _closed(code: str) -> list[dict]:
    return [_check("bot.access", "ok", code)]


def _with_bot(client: Any, bot_api: Any, code: str, *, activate: bool = True) -> dict:
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), bot_api.token_of(bot))
    if activate:
        activate_bot(client, ADMIN_TOKEN, offering_ref(offering))
    return offering


def _post(client: Any, path: str, op_id: str, body: Any = None) -> dict:
    resp = client.post(path, headers=auth(ADMIN_TOKEN), **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def _close(client: Any, code: str) -> None:
    _post(client, f"/v1/offerings/{code}/close", "offerings.close")


def _archive(client: Any, code: str) -> None:
    """Arquivamento em duas fases (só a partir de `draft` ou `closed`, como a API de oferecimentos exige)."""
    prepared = _post(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
    _post(client, f"/v1/offerings/{code}/archive", "offerings.archive",
          {"confirmation_code": prepared["confirmation_code"]})


def _bot_id(sql: psycopg.Connection, code: str) -> int:
    row = sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.code = %s",
                      (code,)).fetchone()
    assert row is not None, f"oferecimento sem bot: {code}"
    return int(row[0])


def _set_bot(sql: psycopg.Connection, code: str, *, poll: datetime | None, lease_until: datetime | None,
             owner: str | None = WORKER) -> None:
    """A última consulta e a posse do bot, por SQL (o que o worker gravaria; nenhum worker roda aqui)."""
    sql.execute("UPDATE bots SET last_poll_ok_at = %s, lease_owner = %s, lease_until = %s WHERE id = %s",
                (poll, owner if lease_until is not None else None, lease_until, _bot_id(sql, code)))


def _healthy_bot(sql: psycopg.Connection, code: str, now: datetime) -> None:
    _set_bot(sql, code, poll=now - timedelta(seconds=10), lease_until=now + timedelta(seconds=30))


def _finish_pending_tasks(sql: psycopg.Connection, now: datetime) -> None:
    """As tarefas da montagem (o perfil do bot da ativação e do arquivamento) concluídas por SQL, para que
    `tasks.overdue` dependa só das tarefas que o caso cria."""
    sql.execute("UPDATE tasks SET state = 'done', finished_at = %s, lease_owner = NULL, lease_until = NULL "
                "WHERE state IN ('pending', 'leased')", (now,))


def _task(sql: psycopg.Connection, *, state: str, available_at: datetime, now: datetime) -> int:
    leased = state == "leased"
    row = sql.execute(
        "INSERT INTO tasks (task_type, payload, state, attempts, available_at, lease_owner, lease_until, "
        "created_at, finished_at) VALUES ('doctor.teste', '{}'::jsonb, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (state, 1 if leased else 0, available_at, WORKER if leased else None,
         now + timedelta(seconds=30) if leased else None, available_at,
         now if state in ("done", "failed") else None)).fetchone()
    return int(row[0])


def _doctor(client: Any, clock: Any, sql: psycopg.Connection, *, shift: float | None = None) -> dict:
    """Alinha o relógio ao banco, aplica o desvio pedido e chama `system.doctor` (200 e o esquema)."""
    now = align_clock_to_db(clock, sql)
    if shift is not None:
        clock.set(now + timedelta(seconds=shift))
    body = system_doctor(client)
    assert body["checked_at"] == iso(clock.now())
    return body


def _fingerprint(sql: psycopg.Connection) -> dict[str, str | None]:
    """O conteúdo de cada tabela do esquema `public`, resumido (nada pode mudar numa leitura do `doctor`)."""
    tables = [row[0] for row in sql.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
    return {table: sql.execute(f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) '
                               f'FROM "{table}" t').fetchone()[0] for table in tables}


@pytest.fixture
def now0(clock: Any, sql: psycopg.Connection) -> datetime:
    """O relógio alinhado ao banco **antes** da montagem: as tarefas e os horários da montagem ficam no tempo do
    banco, e não no 2031 do `FakeClock`."""
    return align_clock_to_db(clock, sql)


# --- casos -----------------------------------------------------------------------------------------------------


def test_healthy_installation_ok_in_fixed_order_and_only_active_or_closed(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, now0: datetime) -> None:
    """Instalação saudável: tudo "ok"; as globais na ordem fixa e as por oferecimento pelo código (criados
    fora de ordem); o encerrado só com `bot.access`; rascunho com bot e arquivado fora; o oferecimento só pelo
    código, nunca pelo `offering_id`; toda mensagem da tabela fixa."""
    b = _with_bot(client, bot_api, "doutor-b")
    a = _with_bot(client, bot_api, "doutor-a")
    _with_bot(client, bot_api, "doutor-c")
    _close(client, "doutor-c")
    _with_bot(client, bot_api, "doutor-rascunho", activate=False)
    _with_bot(client, bot_api, "doutor-arquivado", activate=False)
    _archive(client, "doutor-arquivado")
    _with_bot(client, bot_api, "doutor-encerrado-arquivado")
    _close(client, "doutor-encerrado-arquivado")
    _archive(client, "doutor-encerrado-arquivado")
    create_offering(client, ADMIN_TOKEN, code="doutor-sem-bot")
    for code in ("doutor-a", "doutor-b"):
        _healthy_bot(sql, code, now0)
    _finish_pending_tasks(sql, now0)

    body = _doctor(client, clock, sql)
    assert body["checks"] == [*_globals(), *_active("doutor-a"), *_active("doutor-b"), *_closed("doutor-c")]
    text = json.dumps(body)
    for offering in (a, b):
        assert offering["offering_id"] not in text
    r7_checks = [c for c in body["checks"] if c["id"] not in NEW_GLOBAL_IDS]
    new_checks = [c for c in body["checks"] if c["id"] in NEW_GLOBAL_IDS]
    assert [c["id"] for c in new_checks] == list(NEW_GLOBAL_IDS)
    assert {(c["id"], c["result"]) for c in r7_checks} <= set(DOCTOR_MESSAGES)
    assert all(c["message"] == DOCTOR_MESSAGES[(c["id"], c["result"])] for c in r7_checks)
    assert all(c["message"] in {message for (check_id, result, _), message in DOCTOR_REASON_MESSAGES.items()
                                if (check_id, result) == (c["id"], c["result"])} for c in new_checks)


def test_bot_without_access_fails_only_for_that_offering(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, db_url: str, now0: datetime) -> None:
    """o bot `no_access` de um oferecimento ativo dá `bot.access` falha só para ele, identificado pelo
    código; sem acesso não há posse nem consulta a verificar (o `doctor` só as faz com o bot `registered`)."""
    from cq_server.bots import mark_no_access

    for code in ("acesso-ok", "acesso-perdido"):
        _with_bot(client, bot_api, code)
        _healthy_bot(sql, code, now0)
    with psycopg.connect(db_url) as conn:
        assert mark_no_access(conn, bot_id=_bot_id(sql, "acesso-perdido"), token_version=1,
                              problem="token_rejected", now=now0) is True
        conn.commit()
    _finish_pending_tasks(sql, now0)

    body = _doctor(client, clock, sql)
    assert body["checks"] == [*_globals(), *_active("acesso-ok"), _check("bot.access", "failure", "acesso-perdido")]


def test_bot_access_failure_also_for_closed_offering(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, db_url: str, now0: datetime) -> None:
    """O encerrado também tem `bot.access`: sem acesso, falha (só ele)."""
    from cq_server.bots import mark_no_access

    _with_bot(client, bot_api, "encerrado-sem-acesso")
    with psycopg.connect(db_url) as conn:
        assert mark_no_access(conn, bot_id=_bot_id(sql, "encerrado-sem-acesso"), token_version=1,
                              problem="token_unreadable", now=now0) is True
        conn.commit()
    _close(client, "encerrado-sem-acesso")
    _finish_pending_tasks(sql, now0)

    body = _doctor(client, clock, sql)
    assert body["checks"] == [*_globals(), _check("bot.access", "failure", "encerrado-sem-acesso")]


@pytest.mark.parametrize(("age", "poll"), [(None, "warning"), (301, "warning"), (3600, "warning"), (299, "ok"),
                                           (0, "ok")], ids=["nunca", "301s", "1h", "299s", "agora"])
def test_bot_poll_recent_or_not(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, now0: datetime,
                                age: int | None, poll: str) -> None:
    """A última consulta bem-sucedida nula ou mais antiga que 5 min dá aviso; só `bot.poll` muda."""
    _with_bot(client, bot_api, "consulta")
    _finish_pending_tasks(sql, now0)
    now = align_clock_to_db(clock, sql)
    _set_bot(sql, "consulta", poll=None if age is None else now - timedelta(seconds=age),
             lease_until=now + timedelta(seconds=60))
    clock.set(now)
    body = system_doctor(client)
    assert body["checks"] == [*_globals(), *_active("consulta", poll=poll)]


@pytest.mark.parametrize(("owner", "until", "lease"), [
    (None, None, "warning"), (WORKER, -1, "warning"), (WORKER, -120, "warning"), (WORKER, 30, "ok")],
    ids=["sem-dono", "vencida-1s", "vencida-2min", "valida"])
def test_bot_lease_owner_and_expiry(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                    now0: datetime, owner: str | None, until: int | None, lease: str) -> None:
    """sem dono ou com a posse vencida → `bot.lease` aviso para esse oferecimento; só ela muda."""
    _with_bot(client, bot_api, "posse")
    _finish_pending_tasks(sql, now0)
    now = align_clock_to_db(clock, sql)
    _set_bot(sql, "posse", poll=now - timedelta(seconds=5),
             lease_until=None if until is None else now + timedelta(seconds=until), owner=owner)
    clock.set(now)
    body = system_doctor(client)
    assert body["checks"] == [*_globals(), *_active("posse", lease=lease)]


def test_overdue_tasks_count_only_pending_older_than_limit(
        client: Any, sql: psycopg.Connection, clock: Any, now0: datetime) -> None:
    """Só as tarefas `pending` com `available_at` mais de 5 min no passado contam; `leased` (mesmo
    antiga), `done`, `failed` e a pendente recente não contam; o aviso traz a contagem."""
    _finish_pending_tasks(sql, now0)
    now = align_clock_to_db(clock, sql)
    old = now - timedelta(seconds=301)
    _task(sql, state="pending", available_at=old, now=now)
    _task(sql, state="pending", available_at=now - timedelta(hours=2), now=now)
    _task(sql, state="leased", available_at=now - timedelta(hours=1), now=now)
    _task(sql, state="done", available_at=old, now=now)
    _task(sql, state="failed", available_at=old, now=now)
    _task(sql, state="pending", available_at=now - timedelta(seconds=299), now=now)
    _task(sql, state="pending", available_at=now + timedelta(minutes=10), now=now)
    clock.set(now)
    assert system_doctor(client)["checks"] == _globals(overdue=2)


def test_no_overdue_tasks_is_ok_with_zero(client: Any, sql: psycopg.Connection, clock: Any, now0: datetime) -> None:
    """Sem tarefa atrasada: "ok" com `count = 0`; sem oferecimento com bot, só as globais (6)."""
    _finish_pending_tasks(sql, now0)
    assert _doctor(client, clock, sql)["checks"] == _globals()


@pytest.mark.parametrize(("shift", "skew"), [(10.0, "warning"), (-10.0, "warning"), (3.0, "ok"), (-3.0, "ok"),
                                             (None, "ok")], ids=["+10s", "-10s", "+3s", "-3s", "alinhado"])
def test_clock_skew(client: Any, sql: psycopg.Connection, clock: Any, now0: datetime, shift: float | None,
                    skew: str) -> None:
    """`|now − clock_timestamp()|` acima de 5 s dá aviso; com margem folgada dentro do limite, ok;
    o relógio alinhado ao banco imediatamente antes da chamada."""
    _finish_pending_tasks(sql, now0)
    assert _doctor(client, clock, sql, shift=shift)["checks"] == _globals(skew=skew)


def test_master_key_missing_is_warning(app_factory: Any, client: Any, sql: psycopg.Connection, clock: Any,
                                       bot_api: Any, now0: datetime) -> None:
    """Sem a chave-mestra: `bots.master_key` aviso; as do banco continuam verificadas."""
    _with_bot(client, bot_api, "sem-chave")
    _healthy_bot(sql, "sem-chave", now0)
    _finish_pending_tasks(sql, now0)
    with TestClient(app_factory(bots=None), raise_server_exceptions=False) as other:
        now = align_clock_to_db(clock, sql)
        _healthy_bot(sql, "sem-chave", now)
        body = system_doctor(other)
    assert body["checks"] == [*_globals(master_key="warning"), *_active("sem-chave")]


def test_unauthenticated_and_forbidden(client: Any) -> None:
    """401 sem token e com token inválido (com `WWW-Authenticate: Bearer`); 403 com token sem `offerings:read`."""
    assert_error(client.get(DOCTOR), 401, "unauthenticated")
    assert_error(client.get(DOCTOR, headers=auth("cq_" + "z" * 43)), 401, "unauthenticated")
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read", "events:read"])
    assert_error(client.get(DOCTOR, headers=auth(secret)), 403, "forbidden", permission="offerings:read")
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"])
    resp = client.get(DOCTOR, headers=auth(reader))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("system.doctor", 200, resp.json())


def test_query_and_body_are_refused(client: Any) -> None:
    """Sem parâmetro: qualquer consulta → 400 `[{query, unknown_field}]`."""
    resp = client.get(DOCTOR, params={"offering": "x"}, headers=auth(ADMIN_TOKEN))
    assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert client.get(DOCTOR, headers=auth(ADMIN_TOKEN)).status_code == 200


def test_doctor_is_read_only_without_events_writes_or_telegram(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, db_url: str, now0: datetime) -> None:
    """Só leitura: a contagem de `events` igual antes e depois, nenhuma tabela muda, e a Bot API
    falsa não recebe nenhum pedido (nem `getMe`), com bots ativos, encerrados e sem acesso no banco."""
    from cq_server.bots import mark_no_access

    _with_bot(client, bot_api, "leitura-a")
    _with_bot(client, bot_api, "leitura-b")
    _close(client, "leitura-b")
    with psycopg.connect(db_url) as conn:
        mark_no_access(conn, bot_id=_bot_id(sql, "leitura-a"), token_version=1, problem="token_rejected", now=now0)
        conn.commit()
    _finish_pending_tasks(sql, now0)
    _task(sql, state="pending", available_at=now0 - timedelta(hours=1), now=now0)
    events_before = sql.execute("SELECT count(*), coalesce(max(seq), 0) FROM events").fetchone()
    before = _fingerprint(sql)
    telegram_before = len(bot_api.calls())

    for _ in range(2):
        body = _doctor(client, clock, sql)
        assert body["checks"] == [*_globals(overdue=1), _check("bot.access", "failure", "leitura-a"),
                                  _check("bot.access", "ok", "leitura-b")]

    assert sql.execute("SELECT count(*), coalesce(max(seq), 0) FROM events").fetchone() == events_before
    assert _fingerprint(sql) == before
    assert len(bot_api.calls()) == telegram_before, "o doctor chamou a Bot API"


def test_injected_doctor_settings_change_limits_and_environment_does_not(
        client: Any, settings: Any, sql: psycopg.Connection, clock: Any, bots: Any, bot_api: Any,
        monkeypatch: pytest.MonkeyPatch, now0: datetime) -> None:
    """O `DoctorSettings` dado a `create_app` muda os três limites; o padrão do `create_app` é
    `DoctorSettings()`, e as `CQ_DOCTOR_*` do processo não mudam nada (o `create_app` não lê o ambiente)."""
    from cq_server.api import create_app
    from tests.helpers.calls import TEST_REPO_SETTINGS

    monkeypatch.setenv("CQ_DOCTOR_BOT_POLL_SECONDS", "1")
    monkeypatch.setenv("CQ_DOCTOR_TASK_OVERDUE_SECONDS", "1")
    monkeypatch.setenv("CQ_DOCTOR_CLOCK_SKEW_SECONDS", "0.001")
    _with_bot(client, bot_api, "limites")
    _finish_pending_tasks(sql, now0)

    def scenario() -> datetime:
        now = align_clock_to_db(clock, sql)
        _set_bot(sql, "limites", poll=now - timedelta(seconds=120), lease_until=now + timedelta(seconds=60))
        sql.execute("DELETE FROM tasks WHERE task_type = 'doctor.teste'")
        _task(sql, state="pending", available_at=now - timedelta(seconds=60), now=now)
        clock.set(now + timedelta(seconds=3))
        return now

    # O padrão (o `client` da fixture, sem `doctor_settings`): 120 s e 60 s estão dentro de 5 min; 3 s, de 5 s.
    scenario()
    assert system_doctor(client)["checks"] == [*_globals(), *_active("limites")]

    custom = DoctorSettings(bot_poll_seconds=60, task_overdue_seconds=30, clock_skew_seconds=1.0)
    app = create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS, doctor_settings=custom)
    with TestClient(app, raise_server_exceptions=False) as other:
        scenario()
        assert system_doctor(other)["checks"] == [*_globals(overdue=1, skew="warning"),
                                                  *_active("limites", poll="warning")]
        looser = DoctorSettings(bot_poll_seconds=600, task_overdue_seconds=600, clock_skew_seconds=20.0)
        app_loose = create_app(dataclasses.replace(settings), clock=clock, bots=bots,
                               repo_settings=TEST_REPO_SETTINGS, doctor_settings=looser)
        with TestClient(app_loose, raise_server_exceptions=False) as loose:
            now = scenario()
            clock.set(now + timedelta(seconds=10))
            assert system_doctor(loose)["checks"] == [*_globals(), *_active("limites")]


def test_unknown_offering_ids_never_appear(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                           now0: datetime) -> None:
    """Nenhum identificador interno nem do Telegram na resposta: só o código, os resultados e as contagens."""
    offering = _with_bot(client, bot_api, "so-codigo")
    _healthy_bot(sql, "so-codigo", now0)
    _finish_pending_tasks(sql, now0)
    body = _doctor(client, clock, sql)
    text = json.dumps(body)
    bot_row = sql.execute("SELECT b.telegram_bot_id, b.username, b.bot_uuid FROM bots b JOIN offerings o "
                          "ON o.id = b.offering_id WHERE o.code = 'so-codigo'").fetchone()
    for value in (offering["offering_id"], str(bot_row[0]), bot_row[1], str(bot_row[2]), WORKER):
        assert value not in text, value
    assert uuid.UUID(offering["offering_id"])
