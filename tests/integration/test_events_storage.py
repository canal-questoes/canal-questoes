"""Log de eventos no banco: só acréscimo e ordem de `seq` = ordem de confirmação.

Cobre o log só de acréscimo e a ordem de leitura; o lock consultivo num gatilho por comando e os
gatilhos `ENABLE ALWAYS`.

Todo SQL escreve com `INSERT INTO events(type, occurred_at, actor_token_id, data)` e um `data`
válido de `person.registered`, em conexões com `lock_timeout` e `statement_timeout` de 10 s, para
que nenhum bloqueio prenda o teste.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.helpers.api import ADMIN_TOKEN, list_events
from tests.helpers.clock import iso

TIMEOUTS = "-c lock_timeout=10s -c statement_timeout=10s"
OCCURRED_AT = datetime(2031, 1, 1, tzinfo=timezone.utc)
INSERT_EVENT = ("INSERT INTO events(type, occurred_at, actor_token_id, data) "
                "VALUES ('person.registered', %s, NULL, %s)")
APPEND_ONLY_MESSAGE = "events is append-only"  # a mensagem do gatilho do log


def _connect(db_url: str, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(db_url, autocommit=autocommit, options=TIMEOUTS, connect_timeout=5)


def _data(pid: uuid.UUID) -> dict:
    """`data` válido de `person.registered` (pseudo_id v4 e apelido no padrão)."""
    assert pid.version == 4
    return {"pseudo_id": str(pid), "alias": "e" + pid.hex[:7]}


def _insert(conn: psycopg.Connection, data: dict) -> None:
    conn.execute(INSERT_EVENT, (OCCURRED_AT, Jsonb(data)))


def _as_api_item(data: dict) -> dict:
    """O evento como a API deve devolvê-lo, sem o `seq`."""
    return {"type": "person.registered", "occurred_at": iso(OCCURRED_AT), "actor_token_id": None,
            "data": data}


def _without_seq(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "seq"}


def _snapshot(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT seq, type, occurred_at, actor_token_id, data FROM events ORDER BY seq").fetchall()


def _poll(probe, done, timeout: float):
    """Repete `probe()` até `done(valor)` ou o prazo (espera real com prazo: estado de outra thread)."""
    deadline = time.monotonic() + timeout
    value = probe()
    while not done(value) and time.monotonic() < deadline:
        time.sleep(0.02)
        value = probe()
    return value


# ---------------------------------------------------------------------------------------------------
# O banco recusa alterar ou apagar eventos
# ---------------------------------------------------------------------------------------------------

MUTATIONS = {
    "update-one": "UPDATE events SET type = 'token.revoked' WHERE seq = %(seq)s",
    "update-all": "UPDATE events SET data = '{}'::jsonb",
    "delete-one": "DELETE FROM events WHERE seq = %(seq)s",
    "delete-all": "DELETE FROM events",
    "truncate": "TRUNCATE events",
    "truncate-cascade": "TRUNCATE events CASCADE",
    "merge-update": ("MERGE INTO events AS e USING (SELECT %(seq)s::bigint AS seq) AS s "
                     "ON e.seq = s.seq WHEN MATCHED THEN UPDATE SET type = 'token.revoked'"),
    "merge-delete": ("MERGE INTO events AS e USING (SELECT %(seq)s::bigint AS seq) AS s "
                     "ON e.seq = s.seq WHEN MATCHED THEN DELETE"),
}


@pytest.mark.parametrize("replica", [False, True], ids=["origin", "replica"])
@pytest.mark.parametrize("statement", list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_events_reject_update_delete_and_truncate(db_url, statement, replica):
    """`UPDATE`, `DELETE`, `TRUNCATE` (inclusive `CASCADE`)
    e `MERGE` sobre `events` dão erro `events is append-only` e deixam os dados intactos, também com
    `session_replication_role = replica` (gatilhos `ENABLE ALWAYS`)."""
    with _connect(db_url, autocommit=True) as conn:
        _insert(conn, _data(uuid.uuid4()))
        _insert(conn, _data(uuid.uuid4()))
        before = _snapshot(conn)
        assert len(before) == 2
        if replica:
            conn.execute("SET session_replication_role = replica")
            assert conn.execute("SHOW session_replication_role").fetchone()[0] == "replica"

        params = {"seq": before[0][0]} if "%(seq)s" in statement else None
        with pytest.raises(psycopg.Error) as excinfo:
            conn.execute(statement, params)
        assert excinfo.value.diag.message_primary == APPEND_ONLY_MESSAGE

        assert _snapshot(conn) == before
    with _connect(db_url, autocommit=True) as fresh:
        assert _snapshot(fresh) == before


# ---------------------------------------------------------------------------------------------------
# Uma transação de escrita espera a outra; o leitor nunca pula um evento
# ---------------------------------------------------------------------------------------------------


def test_second_writer_waits_on_advisory_lock_and_reader_sees_commit_order(client, db_url):
    """A insere sem confirmar; B, numa thread, fica esperando o lock
    consultivo (`pg_stat_activity`: `Lock`/`advisory`); o leitor pela API com `after=0` vê `[]`;
    depois que A confirma e B termina, o leitor vê A e depois B, com `seq` crescente."""
    data_a = _data(uuid.uuid4())
    data_b = _data(uuid.uuid4())
    b_pid: list[int] = []
    b_ready = threading.Event()
    b_errors: list[str] = []

    def writer_b() -> None:
        try:
            with _connect(db_url) as conn_b:
                b_pid.append(conn_b.info.backend_pid)
                b_ready.set()
                _insert(conn_b, data_b)
                conn_b.commit()
        except BaseException as exc:  # noqa: BLE001 - registrado e afirmado na thread principal
            b_errors.append(repr(exc))
            b_ready.set()

    conn_a = _connect(db_url)
    thread = threading.Thread(target=writer_b, daemon=True)
    # Prazo: se o leitor da API ficasse bloqueado pela transação aberta de A, o teste travaria; o
    # vigia fecha A (desfazendo-a) depois de 30 s, e o teste então falha nas asserções.
    watchdog = threading.Timer(30, conn_a.close)
    watchdog.daemon = True
    watchdog.start()
    try:
        _insert(conn_a, data_a)  # transação de A aberta, sem confirmar

        thread.start()
        assert b_ready.wait(5), "B não conectou em 5 s"
        assert b_errors == []

        with _connect(db_url, autocommit=True) as monitor:
            state = _poll(
                lambda: monitor.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    (b_pid[0],)).fetchone(),
                lambda row: row is not None and tuple(row) == ("Lock", "advisory"),
                timeout=5)
        assert state is not None and tuple(state) == ("Lock", "advisory"), (
            f"B não ficou esperando o lock consultivo: {state}")
        assert thread.is_alive()

        page = list_events(client, ADMIN_TOKEN, "person.registered", after=0)
        assert page == {"items": [], "next_cursor": 0}

        conn_a.commit()
        thread.join(5)
        assert not thread.is_alive(), "B não terminou em 5 s depois do commit de A"
        assert b_errors == []
    finally:
        watchdog.cancel()
        conn_a.close()
        thread.join(15)

    page = list_events(client, ADMIN_TOKEN, "person.registered", after=0)
    items = page["items"]
    assert [_without_seq(i) for i in items] == [_as_api_item(data_a), _as_api_item(data_b)]
    assert items[0]["seq"] < items[1]["seq"]
    assert page["next_cursor"] == items[1]["seq"]


def _consume(page: dict, cursor: int, read: list[str], by_pid: dict[str, dict]) -> int:
    """Confere uma página do leitor e devolve o novo cursor."""
    items = page["items"]
    if not items:
        assert page["next_cursor"] == cursor
        return cursor
    seqs = [i["seq"] for i in items]
    assert seqs[0] > cursor and all(a < b for a, b in zip(seqs, seqs[1:])), (cursor, seqs)
    assert page["next_cursor"] == seqs[-1]
    for item in items:
        pid = item["data"]["pseudo_id"]
        assert pid in by_pid, f"evento lido que nenhum escritor gravou: {pid}"
        assert _without_seq(item) == _as_api_item(by_pid[pid])
        read.append(pid)
    return page["next_cursor"]


def test_stress_concurrent_writers_reader_by_cursor_loses_nothing(client, db_url):
    """Estresse: 8 escritores, 25 inserções cada, 20% desfeitas, com
    `random.Random(20310101)`; o leitor pela API na thread principal encadeia cursores durante a
    escrita e drena no fim; o conjunto lido é igual ao confirmado, sem repetição e em `seq`
    crescente."""
    rng = random.Random(20310101)
    writers, per_writer = 8, 25
    # plano[w] = [(data, desfazer?, pausa com a transação aberta)], sorteado antes das threads.
    plan: list[list[tuple[dict, bool, float]]] = [
        [(_data(uuid.UUID(int=rng.getrandbits(128), version=4)), rng.random() < 0.2,
          rng.uniform(0, 0.004)) for _ in range(per_writer)]
        for _ in range(writers)]
    by_pid = {data["pseudo_id"]: data for row in plan for data, _, _ in row}
    assert len(by_pid) == writers * per_writer
    expected_committed = {data["pseudo_id"] for row in plan for data, rb, _ in row if not rb}
    rolled_back = {data["pseudo_id"] for row in plan for data, rb, _ in row if rb}
    assert rolled_back and expected_committed, "o plano precisa ter confirmações e desfeitas"

    committed: list[str] = []
    committed_lock = threading.Lock()
    errors: list[str] = []
    barrier = threading.Barrier(writers + 1)

    def writer(rows: list[tuple[dict, bool, float]]) -> None:
        try:
            with _connect(db_url) as conn:
                barrier.wait(timeout=10)
                for data, rollback, pause in rows:
                    _insert(conn, data)
                    # Pausa real com a transação aberta: provoca o intercalamento previsto
                    # ("A pega seq=1 e demora; B confirma").
                    time.sleep(pause)
                    if rollback:
                        conn.rollback()
                    else:
                        conn.commit()
                        with committed_lock:
                            committed.append(data["pseudo_id"])
        except BaseException as exc:  # noqa: BLE001 - afirmado na thread principal
            errors.append(repr(exc))
            barrier.abort()

    threads = [threading.Thread(target=writer, args=(rows,), daemon=True) for rows in plan]
    for t in threads:
        t.start()

    read: list[str] = []
    cursor = 0
    concurrent_reads = 0
    try:
        barrier.wait(timeout=10)
        while any(t.is_alive() for t in threads):
            page = list_events(client, ADMIN_TOKEN, "person.registered", after=cursor,
                               limit=rng.randint(1, 20))
            concurrent_reads += 1
            cursor = _consume(page, cursor, read, by_pid)
    finally:
        for t in threads:
            t.join(60)
    assert not any(t.is_alive() for t in threads), "escritores não terminaram em 60 s"
    assert errors == []

    # Drenagem final, depois do join.
    for _ in range(writers * per_writer + 1):
        page = list_events(client, ADMIN_TOKEN, "person.registered", after=cursor, limit=50)
        if not page["items"]:
            assert page["next_cursor"] == cursor
            break
        cursor = _consume(page, cursor, read, by_pid)
    else:
        pytest.fail("a drenagem não terminou")

    assert concurrent_reads >= 2, "o leitor quase não leu durante a escrita; o estresse ficou vazio"
    assert set(committed) == expected_committed
    assert len(read) == len(set(read)), "o leitor recebeu eventos repetidos"
    assert set(read) == expected_committed, (
        f"perdidos: {len(expected_committed - set(read))}; "
        f"desfeitos lidos: {len(set(read) & rolled_back)}")
