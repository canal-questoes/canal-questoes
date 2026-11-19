"""Integridade do expurgo: repetição, concorrência e falha no meio.

Cobre as sequências de integridade do expurgo: a confirmação repetida, as confirmações concorrentes, a queda
no meio e a matrícula concorrente. Contratos: a ordem das travas e os passos da confirmação.

Cada sequência termina com o oferecimento expurgado **uma única vez** e **um único** `offering.purged`, ou
**sem nenhuma mudança** (`snapshot_purge_tables`, as seis tabelas do expurgo e `offerings`).

O núcleo é chamado direto (`cq_server.purge.prepare_purge` e `confirm_purge`), cada lado com a sua conexão
(`autocommit`, `lock_timeout` de 10 s, 5 s no caso do cadastro do mesmo RA), como os testes de concorrência do
resto do sistema; os casos com duas conexões são **determinísticos**: o lado que chega primeiro segura a trava (uma
transação externa aberta, em que o `with conn.transaction()` interno vira savepoint, ou o `purge._fault`
substituído por um que espera num `threading.Event` no passo `"endpoints"`, o primeiro de `FAULT_STEPS`, já
depois do lock dos eventos), e o outro é **observado esperando** em `pg_locks` (`granted = false`, o `pid` da
conexão dele, por consulta repetida com limite fixo) antes de a trava ser solta. A falha no meio substitui
`purge._fault` por um que levanta `Crash` (o `BaseException` de `tests/helpers/channels.py`) no passo dado.

Os papéis: `students.import_roster` (a importação da lista, `students.import`) e `team.create_link` (o link de
equipe, com o lock dos eventos logo depois de `lock_offering`) no oferecimento ativo `O2` da semente, e
`people.register_person` com o RA de uma pessoa a expurgar.

No esqueleto, `purge.prepare_purge` e `purge.confirm_purge` lançam
`NotImplementedError`: cada função falha na preparação.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.helpers.api import ADMIN_TOKEN, assert_matches_tolerant, auth, create_offering
from tests.helpers.canary import PURGE_A
from tests.helpers.channels import Crash
from tests.helpers.purge import make_pending, seed_purge_scenario, snapshot_purge_tables

CODE_TTL = timedelta(minutes=10)
STEPS = ("endpoints", "updates", "free_messages", "outbox", "identities", "contact", "mark")  # FAULT_STEPS
WAIT = 10.0


# --------------------------------------------------------------------------------------------------
# Apoio


def _connect(db_url: str, lock_timeout: str = "10s") -> psycopg.Connection:
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(f"SET lock_timeout = '{lock_timeout}'")
    conn.execute("SET statement_timeout = '60s'")
    return conn


class _Runner(threading.Thread):
    """Roda `call` numa thread e guarda o resultado ou a exceção (inclusive `BaseException`)."""

    def __init__(self, call: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._call = call
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._call()
        except BaseException as exc:  # noqa: BLE001 - devolvido ao teste
            self.error = exc

    def outcome(self, timeout: float = 3 * WAIT) -> Any:
        self.join(timeout)
        assert not self.is_alive(), "a thread não terminou no prazo (impasse?)"
        if self.error is not None:
            raise self.error
        return self.result


def _wait_lock_wait(sql: psycopg.Connection, pid: int, runner: _Runner, *, locktype: str | None = None) -> None:
    """Espera (por consulta repetida, com limite fixo) até a conexão `pid` aparecer esperando uma trava em
    `pg_locks` (`granted = false`; do tipo `locktype`, quando dado)."""
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        rows = sql.execute("SELECT locktype FROM pg_locks WHERE pid = %s AND NOT granted", (pid,)).fetchall()
        if rows and (locktype is None or any(row[0] == locktype for row in rows)):
            return
        if not runner.is_alive():
            runner.outcome()
            raise AssertionError("a thread terminou sem esperar a trava")
        time.sleep(0.02)
    raise AssertionError(f"a conexão {pid} não esperou a trava {locktype or ''} no prazo")


def _prepare(db_url: str, scenario: Any, clock: Any) -> str:
    from cq_server import purge

    with _connect(db_url) as conn:
        result = purge.prepare_purge(conn, scenario.code, now=clock.now(), actor_token_id=None)
    assert isinstance(result, purge.PurgePreparation), f"preparação recusada: {result!r}"
    return result.confirmation_code


def _confirm(conn: psycopg.Connection, scenario: Any, code: str, clock: Any) -> Any:
    from cq_server import purge

    return purge.confirm_purge(conn, scenario.code, confirmation_code=code, now=clock.now(), actor_token_id=None)


def _purged_events(sql: psycopg.Connection, scenario: Any) -> int:
    return int(sql.execute("SELECT count(*) FROM events WHERE type = 'offering.purged' AND data->>'offering_id' = %s",
                           (scenario.offering_id,)).fetchone()[0])


def _purged_at(sql: psycopg.Connection, scenario: Any) -> Any:
    return sql.execute("SELECT purged_at FROM offerings WHERE id = %s", (scenario.offering_row,)).fetchone()[0]


def _code_columns(sql: psycopg.Connection, scenario: Any) -> tuple:
    return tuple(sql.execute("SELECT purge_code_sha256, purge_code_expires_at, purge_prepared_counts FROM offerings "
                             "WHERE id = %s", (scenario.offering_row,)).fetchone())


def _expected_result(scenario: Any, at: Any) -> Any:
    from cq_server import purge

    return purge.PurgeResult(offering_id=uuid.UUID(scenario.offering_id), code=scenario.code,
                             purged_at=at, outcome=scenario.outcome)


def _identity_of(sql: psycopg.Connection, person_row: int) -> Any:
    return sql.execute("SELECT to_jsonb(i) FROM identities i WHERE person_id = %s", (person_row,)).fetchone()


def _without_offering(snapshot: dict, offering_row: int) -> dict:
    """O instantâneo sem a linha de `offerings` de `offering_row` (a do oferecimento ativo que a importação toca,
    ao gravar a lista)."""
    return {table: [row for row in rows if not (table == "offerings" and row["id"] == offering_row)]
            for table, rows in snapshot.items()}


@dataclasses.dataclass(frozen=True)
class _Ref:
    code: str


def _empty_archived(client: Any) -> _Ref:
    """Outro oferecimento arquivado direto do rascunho (pelas rotas do oferecimento), sem pessoas nem bot."""
    created = create_offering(client, ADMIN_TOKEN)
    headers = auth(ADMIN_TOKEN)
    prepared = client.post(f"/v1/offerings/{created['code']}/archive/prepare", headers=headers)
    assert prepared.status_code == 200, prepared.text[:300]
    resp = client.post(f"/v1/offerings/{created['code']}/archive",
                       json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=headers)
    assert resp.status_code == 200, resp.text[:300]
    assert_matches_tolerant("offerings.archive", 200, resp.json())
    return _Ref(created["code"])


def _roster(person: Any) -> list[Any]:
    from cq_server import students

    rows, problems = students.check_rows([{"name": person.name, "ra": person.ra, "email": person.email}])
    assert problems == []
    return rows


# --------------------------------------------------------------------------------------------------
# Repetição e duas confirmações


def test_repeated_confirmation_purges_once(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                           db_url: str) -> None:
    """O resultado gravado na confirmação: a confirmação repetida (com o mesmo código, um inventado e depois do
    vencimento) devolve o mesmo `PurgeResult` gravado; um único `offering.purged`."""
    from cq_server import purge

    scenario = seed_purge_scenario(client, sql)
    code = _prepare(db_url, scenario, clock)
    t1 = clock.now()
    with _connect(db_url) as conn:
        first = _confirm(conn, scenario, code, clock)
        assert first == _expected_result(scenario, t1)
        after = snapshot_purge_tables(sql)
        clock.advance(2 * CODE_TTL.total_seconds())
        for again in (code, "codigo-inventado-qualquer"):
            assert _confirm(conn, scenario, again, clock) == first
    assert isinstance(first, purge.PurgeResult)
    assert snapshot_purge_tables(sql) == after
    assert _purged_events(sql, scenario) == 1


def test_two_concurrent_confirmations(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                      db_url: str) -> None:
    """Duas confirmações concorrentes serializam pelo `FOR NO KEY UPDATE` do
    oferecimento, e a segunda cai na conferência do resultado já gravado: a conexão A confirma numa transação externa ainda aberta; a B
    confirma com o mesmo código e é observada esperando a trava; depois do COMMIT de A, B devolve o resultado
    gravado (igual ao de A). Um único `offering.purged`."""
    scenario = seed_purge_scenario(client, sql)
    code = _prepare(db_url, scenario, clock)
    t1 = clock.now()
    with _connect(db_url) as conn_a, _connect(db_url) as conn_b:
        with conn_a.transaction():
            first = _confirm(conn_a, scenario, code, clock)
            assert first == _expected_result(scenario, t1)
            second_runner = _Runner(lambda: _confirm(conn_b, scenario, code, clock))
            second_runner.start()
            _wait_lock_wait(sql, conn_b.info.backend_pid, second_runner)
        second = second_runner.outcome()
    assert second == first
    assert _purged_at(sql, scenario) == t1
    assert _purged_events(sql, scenario) == 1


# --------------------------------------------------------------------------------------------------
# Falha no meio


@pytest.mark.parametrize("step", STEPS)
def test_crash_at_each_step_changes_nothing(step: str, client: Any, sql: psycopg.Connection, clock: Any,
                                            bot_api: Any, db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tudo numa transação: uma queda no meio deixa o oferecimento como
    estava: com `purge._fault` levantando `Crash` depois do passo dado (cada um de `FAULT_STEPS`), a
    confirmação cai, nada muda (`purged_at` nulo, o código guardado igual) e nenhum `offering.purged`; o mesmo
    código, sem a falha, expurga uma única vez."""
    from cq_server import purge

    assert purge.FAULT_STEPS == STEPS
    scenario = seed_purge_scenario(client, sql)
    code = _prepare(db_url, scenario, clock)
    before = snapshot_purge_tables(sql)
    stored = _code_columns(sql, scenario)
    seen: list[str] = []

    def crash(name: str) -> None:
        seen.append(name)
        if name == step:
            raise Crash(f"queda simulada depois de {name}")

    monkeypatch.setattr(purge, "_fault", crash)
    with _connect(db_url) as conn, pytest.raises(Crash):
        _confirm(conn, scenario, code, clock)
    assert seen == list(STEPS[:STEPS.index(step) + 1])
    assert snapshot_purge_tables(sql) == before
    assert _purged_at(sql, scenario) is None
    assert _code_columns(sql, scenario) == stored
    assert _purged_events(sql, scenario) == 0

    monkeypatch.setattr(purge, "_fault", lambda name: None)
    with _connect(db_url) as conn:
        result = _confirm(conn, scenario, code, clock)
    assert result == _expected_result(scenario, clock.now())
    assert _purged_events(sql, scenario) == 1


# --------------------------------------------------------------------------------------------------
# Código recusado e trabalho pendente que aparece depois da preparação


@pytest.mark.parametrize("case", ["vencido", "substituido", "de-outro-oferecimento"])
def test_refused_codes_change_nothing(case: str, client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                      db_url: str) -> None:
    """o código vencido (no instante de `expires_at`), o substituído por uma nova preparação e o
    válido de outro oferecimento → `InvalidConfirmation`, sem nenhuma mudança e sem evento."""
    from cq_server import purge

    scenario = seed_purge_scenario(client, sql)
    code = _prepare(db_url, scenario, clock)
    if case == "vencido":
        clock.advance(CODE_TTL.total_seconds())
    elif case == "substituido":
        _prepare(db_url, scenario, clock)
    else:
        code = _prepare(db_url, _empty_archived(client), clock)
    before = snapshot_purge_tables(sql)
    with _connect(db_url) as conn:
        assert _confirm(conn, scenario, code, clock) == purge.InvalidConfirmation()
    assert snapshot_purge_tables(sql) == before
    assert _purged_events(sql, scenario) == 0


@pytest.mark.parametrize("resolution", ["antes-de-vencer", "depois-de-vencer"])
def test_pending_work_after_prepare_blocks_and_keeps_the_code(resolution: str, client: Any, sql: psycopg.Connection,
                                                              clock: Any, bot_api: Any, db_url: str) -> None:
    """A conferência do trabalho pendente na confirmação (soma > 0 → `PurgeBlocked(pending)`; o código continua
    válido): uma resposta na fila do bot que aparece entre a preparação e a confirmação → `PurgeBlocked` com
    `outbox = 1`, sem mudança e com o código guardado igual; terminada a fila, o mesmo código expurga antes de
    vencer e é recusado (`InvalidConfirmation`) depois de vencer."""
    from cq_server import purge

    scenario = seed_purge_scenario(client, sql)
    code = _prepare(db_url, scenario, clock)
    stored = _code_columns(sql, scenario)
    assert make_pending(sql, "outbox_pending", scenario) == "outbox"
    before = snapshot_purge_tables(sql)
    with _connect(db_url) as conn:
        blocked = _confirm(conn, scenario, code, clock)
    assert blocked == purge.PurgeBlocked(purge.PurgePending(outbox=1, updates=0, tasks=0, freeze=0, due=0))
    assert snapshot_purge_tables(sql) == before
    assert _code_columns(sql, scenario) == stored

    sql.execute("UPDATE outbox_messages SET state = 'sent', attempts = 1, sent_at = %s, channel_ref = 'enviada' "
                "WHERE bot_id = %s AND state = 'pending'", (clock.now(), scenario.bot_row))
    if resolution == "depois-de-vencer":
        clock.advance(CODE_TTL.total_seconds() + 1)
        with _connect(db_url) as conn:
            assert _confirm(conn, scenario, code, clock) == purge.InvalidConfirmation()
        assert _purged_events(sql, scenario) == 0
        assert _purged_at(sql, scenario) is None
    else:
        clock.advance(CODE_TTL.total_seconds() - 1)
        with _connect(db_url) as conn:
            assert _confirm(conn, scenario, code, clock) == _expected_result(scenario, clock.now())
        assert _purged_events(sql, scenario) == 1


# --------------------------------------------------------------------------------------------------
# Matrícula concorrente noutro oferecimento ativo


@pytest.mark.parametrize("writer", ["students.import", "team.create_link"])
def test_enrollment_first_then_purge_is_changed(writer: str, client: Any, sql: psycopg.Connection, clock: Any,
                                                bot_api: Any, db_url: str) -> None:
    """Ordem das travas (a matrícula primeiro): a conexão A matricula, numa transação externa
    aberta, no oferecimento ativo `O2` uma pessoa que só está no oferecimento a expurgar (`aluno_a` pela
    importação; `citada` pelo link de equipe), segurando o lock consultivo dos eventos até o COMMIT; a
    confirmação em B é observada esperando o lock consultivo (`locktype = 'advisory'`); depois do COMMIT de A, a
    recontagem acha a pessoa mantida e B recebe `PurgeChanged`, sem nenhuma mudança nas tabelas do expurgo nem no
    oferecimento expurgado (o código continua guardado), e a pessoa mantém a identidade."""
    from cq_server import purge, students, team

    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None
    label = "aluno_a" if writer == "students.import" else "citada"
    person = scenario.person(label)
    assert person.deleted
    code = _prepare(db_url, scenario, clock)
    before = snapshot_purge_tables(sql)
    identity = _identity_of(sql, person.person_row)
    assert identity is not None

    with _connect(db_url) as conn_a, _connect(db_url) as conn_b:
        with conn_a.transaction():
            if writer == "students.import":
                written = students.import_roster(conn_a, other.code, _roster(person.identity), now=clock.now(),
                                                 actor_token_id=None)
                assert isinstance(written, students.ImportResult) and written.enrollments_created == 1
            else:
                written = team.create_link(conn_a, other.code, person=person.pseudo_id, now=clock.now(),
                                           actor_token_id=None)
                assert isinstance(written, team.TeamLink)
            runner = _Runner(lambda: _confirm(conn_b, scenario, code, clock))
            runner.start()
            _wait_lock_wait(sql, conn_b.info.backend_pid, runner, locktype="advisory")
        result = runner.outcome()
    assert result == purge.PurgeChanged()
    assert _without_offering(snapshot_purge_tables(sql), other.offering_row) == _without_offering(
        before, other.offering_row)
    assert _identity_of(sql, person.person_row) == identity
    assert _purged_events(sql, scenario) == 0


def test_purge_first_then_import_registers_another_person(client: Any, sql: psycopg.Connection, clock: Any,
                                                          bot_api: Any, db_url: str,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordem das travas (o expurgo primeiro; só a importação, porque o `team.create_link` recebe o
    pseudônimo, e a pessoa continua em `people`): a confirmação em B para no passo `"endpoints"` (já com o lock
    dos eventos); a importação em A, com o RA de `aluno_a`, é observada esperando o lock consultivo; solto o
    passo, B expurga (um único `offering.purged`) e só então A segue: o RA não está mais em `identities`, e a
    importação cadastra **outra pessoa** (um `person.registered` novo, pseudônimo diferente) com a matrícula nova
    ligada a ela; a pessoa expurgada fica sem identidade e sem matrícula nova."""
    from cq_server import purge, students

    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None
    old = scenario.person("aluno_a")
    code = _prepare(db_url, scenario, clock)
    reached = threading.Event()
    release = threading.Event()

    def hold(name: str) -> None:
        if name == "endpoints":
            reached.set()
            assert release.wait(3 * WAIT), "o teste não soltou o passo"

    monkeypatch.setattr(purge, "_fault", hold)
    with _connect(db_url) as conn_a, _connect(db_url) as conn_b:
        purger = _Runner(lambda: _confirm(conn_b, scenario, code, clock))
        purger.start()
        assert reached.wait(WAIT), "a confirmação não chegou ao passo endpoints"
        importer = _Runner(lambda: students.import_roster(conn_a, other.code, _roster(PURGE_A), now=clock.now(),
                                                          actor_token_id=None))
        importer.start()
        _wait_lock_wait(sql, conn_a.info.backend_pid, importer, locktype="advisory")
        assert importer.is_alive()
        release.set()
        purged = purger.outcome()
        imported = importer.outcome()
    assert purged == _expected_result(scenario, clock.now())
    assert _purged_events(sql, scenario) == 1
    assert isinstance(imported, students.ImportResult)
    assert (imported.people_created, imported.people_reused, imported.enrollments_created) == (1, 0, 1)

    new = sql.execute("SELECT p.id, p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (PURGE_A.ra,)).fetchone()
    assert new is not None and new[0] != old.person_row and new[1] != old.pseudo_id
    assert sql.execute("SELECT count(*) FROM events WHERE type = 'person.registered' AND data->>'pseudo_id' = %s",
                       (new[1],)).fetchone()[0] == 1
    assert sql.execute("SELECT count(*) FROM enrollments WHERE person_id = %s AND offering_id = %s",
                       (new[0], other.offering_row)).fetchone()[0] == 1
    assert _identity_of(sql, old.person_row) is None
    assert [row[0] for row in sql.execute("SELECT offering_id FROM enrollments WHERE person_id = %s ORDER BY id",
                                          (old.person_row,)).fetchall()] == [scenario.offering_row]


@pytest.mark.parametrize("first", ["expurgo", "cadastro"])
def test_no_deadlock_with_register_person_of_the_same_ra(first: str, client: Any, sql: psycopg.Connection,
                                                         clock: Any, bot_api: Any, db_url: str,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordem das travas (antes do `DELETE identities`, fecha o impasse com um `people.register_person`
    concorrente do mesmo RA): com `lock_timeout` curto (5 s) nas duas conexões, as
    duas ordens terminam sem impasse nem estouro de espera. O expurgo primeiro (parado em `"endpoints"`): o
    cadastro espera o lock consultivo e, solto o passo, cadastra a pessoa de novo (`Person`, pseudônimo novo). O
    cadastro primeiro (numa transação externa aberta): o RA ainda existe (`Duplicate`), a confirmação espera o
    lock consultivo e, depois do COMMIT, expurga com as contagens da preparação."""
    from cq_server import people, purge

    scenario = seed_purge_scenario(client, sql)
    old = scenario.person("aluno_a")
    code = _prepare(db_url, scenario, clock)

    def register(conn: psycopg.Connection) -> Any:
        return people.register_person(conn, full_name=PURGE_A.name, ra=PURGE_A.ra, email=PURGE_A.email,
                                      now=clock.now(), actor_token_id=None)

    with _connect(db_url, "5s") as conn_a, _connect(db_url, "5s") as conn_b:
        if first == "expurgo":
            reached = threading.Event()
            release = threading.Event()

            def hold(name: str) -> None:
                if name == "endpoints":
                    reached.set()
                    assert release.wait(3 * WAIT), "o teste não soltou o passo"

            monkeypatch.setattr(purge, "_fault", hold)
            purger = _Runner(lambda: _confirm(conn_b, scenario, code, clock))
            purger.start()
            assert reached.wait(WAIT), "a confirmação não chegou ao passo endpoints"
            registrar = _Runner(lambda: register(conn_a))
            registrar.start()
            _wait_lock_wait(sql, conn_a.info.backend_pid, registrar, locktype="advisory")
            release.set()
            purged = purger.outcome()
            registered = registrar.outcome()
            assert isinstance(registered, people.Person)
            assert str(registered.pseudo_id) != old.pseudo_id
        else:
            with conn_a.transaction():
                registered = register(conn_a)
                assert isinstance(registered, people.Duplicate)
                purger = _Runner(lambda: _confirm(conn_b, scenario, code, clock))
                purger.start()
                _wait_lock_wait(sql, conn_b.info.backend_pid, purger, locktype="advisory")
            purged = purger.outcome()
    assert purged == _expected_result(scenario, clock.now())
    assert _purged_events(sql, scenario) == 1
    assert _identity_of(sql, old.person_row) is None
