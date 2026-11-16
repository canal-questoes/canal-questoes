"""A verificação de retenção do `doctor`, `retention.overdue`.


Com o `FakeClock` e o banco de teste, arquivando pela operação de arquivamento (as duas fases, a partir de `draft`;
`archived_at` = o `now` da API) e chamando `GET /v1/system/doctor`:
- vencido = `state = 'archived' AND purged_at IS NULL AND archived_at < now − CQ_RETENTION_DAYS × 24 h` (a
  fronteira por instante): prazo padrão (30 dias) e arquivado há 29 dias → "ok",
  `count` 0, `offering` nulo; 30 dias exatos e 30 dias − 1 s → "ok"; 30 dias + 1 s → "aviso", `count` 1 e o
  código;
- dois vencidos, um expurgado pelas duas fases do expurgo → `count` 1 e o código do restante; com dois vencidos, o
  código do de menor `(archived_at, id)`;
- `DoctorSettings(retention_days=7)` e arquivado há 8 dias → "aviso";
- o expurgo bloqueado por trabalho pendente (409 `purge_pending`) mantém o aviso;
- o `doctor` nunca expurga nem escreve (instantâneo de todas as tabelas igual; `purged_at` continua nulo);
- a partida da API por subprocesso (`python -m cq_server serve`, no molde de `tests/unit/test_repo_policy.py`
  l. 837-880; **[F]** o `_cmd_serve` lê o `DoctorSettings.from_env(os.environ)` antes de migrar,
  `server/src/cq_server/__main__.py`) com `CQ_RETENTION_DAYS` igual a `0`, `-1`, `abc`, `30.0` ou `3651` →
  código 2, um `config.invalid`, sem escutar, sem migrar e sem eco.

**Vermelho no esqueleto:** o `collect` ainda não emite `retention.overdue` e o `from_env` ainda não lê
`CQ_RETENTION_DAYS`; todos os casos dependem da verificação de retenção.

Escolhas deste teste ([I]): os oferecimentos são arquivados direto do rascunho, sem bot nem pessoas
(impacto zero no expurgo); o trabalho pendente é uma tarefa `pending` com o oferecimento no `payload`
(`offering_row_id`, uma das chaves que o expurgo conta); o eco é conferido nos campos dos registros sem o
`time` (o horário pode conter dígitos de qualquer valor) e no stderr.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from cq_server.doctor import DOCTOR_REASON_MESSAGES, DoctorSettings
from tests.helpers.api import ADMIN_TOKEN, auth, create_offering, prepare_purge, purge_offering, system_doctor
from tests.helpers.contract import assert_error
from tests.helpers.processes import WorkerProcess, worker_env

DAY = timedelta(days=1)
SUBPROCESS_TIMEOUT = 60.0
OK = {"id": "retention.overdue", "result": "ok", "offering": None, "count": 0,
      "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")]}


def _warning(code: str, count: int) -> dict:
    return {"id": "retention.overdue", "result": "warning", "offering": code, "count": count,
            "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "warning", "overdue")]}


# --- montagem ------------------------------------------------------------------------------------------------


def _post(client: Any, path: str, op_id: str, body: Any = None) -> dict:
    resp = client.post(path, headers=auth(ADMIN_TOKEN), **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def _archived(client: Any, code: str) -> str:
    """Cria o oferecimento (rascunho) e o arquiva pelas duas fases do arquivamento, no `now` do `FakeClock`."""
    create_offering(client, ADMIN_TOKEN, code=code)
    prepared = _post(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
    _post(client, f"/v1/offerings/{code}/archive", "offerings.archive",
          {"confirmation_code": prepared["confirmation_code"]})
    return code


def _retention(client: Any) -> dict:
    """A verificação `retention.overdue` da resposta (200 e o esquema), exatamente uma."""
    found = [c for c in system_doctor(client)["checks"] if c["id"] == "retention.overdue"]
    assert len(found) == 1, f"retention.overdue: {len(found)} verificações"
    return found[0]


def _archived_at(sql: psycopg.Connection, code: str) -> datetime:
    return sql.execute("SELECT archived_at FROM offerings WHERE code = %s", (code,)).fetchone()[0]


def _fingerprint(sql: psycopg.Connection) -> dict[str, str | None]:
    tables = [row[0] for row in sql.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
    return {table: sql.execute(f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) '
                               f'FROM "{table}" t').fetchone()[0] for table in tables}


def _purge(client: Any, code: str) -> None:
    prepared = prepare_purge(client, code)
    purge_offering(client, code, prepared["confirmation_code"])


# --- casos -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("elapsed", "overdue"), [
    (29 * DAY, False),
    (30 * DAY - timedelta(seconds=1), False),
    (30 * DAY, False),
    (30 * DAY + timedelta(seconds=1), True),
    (400 * DAY, True),
], ids=["29d", "30d-1s", "30d", "30d+1s", "400d"])
def test_default_limit_by_instant(client: Any, sql: psycopg.Connection, clock: Any, elapsed: timedelta,
                                  overdue: bool) -> None:
    """prazo padrão (30 dias) comparado por instante: até 30 dias exatos, "ok" com `count` 0; 30
    dias + 1 s, "aviso" com `count` 1 e o código."""
    code = _archived(client, "retencao-unico")
    assert _archived_at(sql, code) == clock.now()
    clock.advance(elapsed.total_seconds())
    assert _retention(client) == (_warning(code, 1) if overdue else OK)


def test_two_overdue_then_one_purged(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """dois vencidos → `count` 2 e o código do mais antigo; um expurgado pelas duas fases do expurgo → `count` 1
    e o código do restante; os dois expurgados → "ok"."""
    older = _archived(client, "retencao-antigo")
    clock.advance(3600)
    newer = _archived(client, "retencao-novo")
    clock.advance((31 * DAY).total_seconds())
    assert _retention(client) == _warning(older, 2)
    _purge(client, older)
    assert _retention(client) == _warning(newer, 1)
    _purge(client, newer)
    assert _retention(client) == OK


def test_tie_on_archived_at_picks_the_smallest_id(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Dois vencidos arquivados no mesmo instante: o código é o do menor `id` (o criado primeiro), mesmo
    arquivado por último."""
    create_offering(client, ADMIN_TOKEN, code="retencao-empate-a")
    create_offering(client, ADMIN_TOKEN, code="retencao-empate-b")
    for code in ("retencao-empate-b", "retencao-empate-a"):
        prepared = _post(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
        _post(client, f"/v1/offerings/{code}/archive", "offerings.archive",
              {"confirmation_code": prepared["confirmation_code"]})
    assert _archived_at(sql, "retencao-empate-a") == _archived_at(sql, "retencao-empate-b")
    clock.advance((31 * DAY).total_seconds())
    assert _retention(client) == _warning("retencao-empate-a", 2)


def test_only_archived_and_not_purged_count(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """O rascunho antigo não conta (só `archived`), nem o arquivado já expurgado."""
    create_offering(client, ADMIN_TOKEN, code="retencao-so-rascunho")
    purged = _archived(client, "retencao-ja-expurgado")
    _purge(client, purged)
    clock.advance((90 * DAY).total_seconds())
    assert _retention(client) == OK


def test_configured_days(settings: Any, clock: Any, bots: Any, client: Any) -> None:
    """com `DoctorSettings(retention_days=7)`, arquivado há 8 dias → "aviso"; há 6 dias → "ok"; o padrão
    (30) com os mesmos 8 dias → "ok"."""
    from cq_server.api import create_app
    from tests.helpers.calls import TEST_REPO_SETTINGS

    code = _archived(client, "retencao-sete-dias")
    app = create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS,
                     doctor_settings=DoctorSettings(retention_days=7))
    with TestClient(app, raise_server_exceptions=False) as seven:
        clock.advance((6 * DAY).total_seconds())
        assert _retention(seven) == OK
        clock.advance((2 * DAY).total_seconds())
        assert _retention(seven) == _warning(code, 1)
    assert _retention(client) == OK


def test_purge_blocked_by_pending_work_keeps_the_warning(client: Any, sql: psycopg.Connection,
                                                         clock: Any) -> None:
    """Com trabalho pendente do oferecimento, o expurgo é recusado (409 `purge_pending`) e o aviso continua."""
    code = _archived(client, "retencao-pendente")
    row = sql.execute("SELECT id FROM offerings WHERE code = %s", (code,)).fetchone()[0]
    sql.execute("INSERT INTO tasks (task_type, payload, state, attempts, available_at, created_at) "
                "VALUES ('notice.access_episode', %s, 'pending', 0, %s, %s)",
                (Jsonb({"offering_row_id": int(row)}), clock.now(), clock.now()))
    clock.advance((31 * DAY).total_seconds())
    assert _retention(client) == _warning(code, 1)
    resp = client.post(f"/v1/offerings/{code}/purge/prepare", headers=auth(ADMIN_TOKEN))
    assert_error(resp, 409, "purge_pending", pending={"outbox": 0, "updates": 0, "tasks": 1, "freeze": 0, "due": 0})
    assert _retention(client) == _warning(code, 1)


def test_doctor_never_purges_nor_writes(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """com vencidos no banco, duas chamadas ao `doctor` não mudam nenhuma tabela, e `purged_at` continua
    nulo."""
    for code in ("retencao-leitura-a", "retencao-leitura-b"):
        _archived(client, code)
    clock.advance((45 * DAY).total_seconds())
    before = _fingerprint(sql)
    for _ in range(2):
        assert _retention(client) == _warning("retencao-leitura-a", 2)
    assert _fingerprint(sql) == before
    assert sql.execute("SELECT count(*) FROM offerings WHERE purged_at IS NOT NULL").fetchone()[0] == 0


# --- a partida da API por subprocesso --------------------------------------------------------------------------


def _wait(predicate: Callable[[], Any], timeout: float = SUBPROCESS_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _json_records(out: str) -> list[dict]:
    records: list[dict] = []
    for line in out.splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"_raw": line})
    return records


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "30.0", "3651"])
def test_serve_refuses_invalid_retention_days(empty_db_url: str, bot_api: Any, raw: str) -> None:
    """`python -m cq_server serve` com `CQ_RETENTION_DAYS` inválido sai com código 2 sem subir o
    servidor (a porta nunca escuta, nenhum registro do uvicorn) e sem migrar, com um único `config.invalid`
    (ERROR, `cq_server.main`, que cita a variável); o valor não aparece nos registros (sem o `time`) nem no
    stderr."""
    port = _free_port()
    env = worker_env({"CQ_DATABASE_URL": empty_db_url, "CQ_RETENTION_DAYS": raw})
    proc = WorkerProcess([sys.executable, "-m", "cq_server", "serve", "--host", "127.0.0.1", "--port",
                          str(port)], env)
    listened = False
    try:
        def settled() -> bool:
            nonlocal listened
            if proc.poll() is not None:
                return True
            listened = listened or _listening(port)
            return listened

        _wait(settled)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o serve não saiu com código 2 (saiu: {code}; escutou: {listened}; registros: {events})"
    assert not listened
    assert [r for r in records if str(r.get("logger", "")).startswith("uvicorn")] == []
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    assert {"level": "ERROR", "logger": "cq_server.main"}.items() <= invalid[0].items(), invalid[0]
    detail = str(invalid[0].get("detail"))
    assert "CQ_RETENTION_DAYS" in detail
    if raw in ("abc", "30.0", "3651"):
        without_time = "\n".join(json.dumps({k: v for k, v in r.items() if k != "time"}, ensure_ascii=False)
                                 for r in records)
        for surface, text in (("registros", without_time), ("saída de erro", err)):
            assert raw not in text, f"eco do valor nos {surface}"
    else:
        assert f"={raw}" not in detail and f"'{raw}'" not in detail and f'"{raw}"' not in detail
    with psycopg.connect(empty_db_url) as conn:
        tables = conn.execute("SELECT count(*) FROM information_schema.tables "
                              "WHERE table_schema = 'public'").fetchone()[0]
    assert tables == 0, "o serve migrou antes de ler a configuração"
