"""Posse dos bots pelos workers.

Cobre a posse disputada e o relógio que recua; as regras da posse
(assumir, renovar, soltar, elegibilidade, aviso) e da perda de acesso; o runtime
dos bots (`acquire_leases`, `renew_leases`, `release_leases`, `poll_allowed`,
`mark_no_access`, `LeasedBot`).

As funções de posse rodam **na transação do chamador**: cada chamada aqui abre uma conexão, chama a
função e confirma (`_run`). Dois `worker_id` (`worker-a` e `worker-b`) e o `FakeClock` do teste fazem
o papel de dois workers; os horários passados em `now` são os do relógio falso, e a fronteira de
vencimento é estrita (`lease_until < agora`). Os oferecimentos e os bots são preparados pela API
(cadastro, registro na Bot API falsa, ativação, encerramento, arquivamento); nenhum worker roda,
exceto no teste do relógio que recua, que usa a `run_worker` com dois `FakeClock` próprios, e no da
posse perdida durante uma espera, com um worker no `FakeClock` do teste e a posse passada ao
`worker-b` por SQL.

Convenções: eventos `bot.lease_changed` e `bot.access_changed` lidos pela API e
conferidos por inteiro (sem o `seq`); ids internos só lidos do banco, nunca como literal; esperas
reais só onde há outra thread, sempre com prazo.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.bots import (
    LeasedBot,
    acquire_leases,
    mark_no_access,
    poll_allowed,
    release_leases,
    renew_leases,
)
from cq_server.notices import ConfigNotice, NoticeListener
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    offering_ref,
    register_bot,
)
from tests.helpers.bot_api import monotonic
from tests.helpers.clock import FakeClock, iso
from tests.helpers.contract import assert_matches

A = "worker-a"
B = "worker-b"
LEASE_SECONDS = 30  # o `bot_settings` do conftest
LEASE = timedelta(seconds=LEASE_SECONDS)
MARGIN = timedelta(seconds=1 + 2)  # poll_timeout_seconds + telegram_timeout_seconds do conftest
TICK = timedelta(microseconds=1)
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
# Janela sem chamadas depois da perda da posse: passos de 0,5 s reais (4 s, mais que um ciclo de
# consulta de 3 s), com o `FakeClock` andando 1 s por passo (sem vencer a posse do `worker-b`).
QUIET_STEPS = 8
QUIET_STEP_SECONDS = 0.5
# Avanço do relógio do novo dono que solta uma espera de pelo menos 3 s depois de um 409,
# com 1 s de folga para quem espera estritamente mais de 3 s.
RELEASE_409_SECONDS = 4
TOKEN_REJECTED_MESSAGE = ("O Telegram recusou o token do bot. Gere um token novo no BotFather e "
                          "registre-o.")


@pytest.fixture(autouse=True)
def _harness_bot_settings(bot_settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `bot_settings` do conftest."""
    assert (bot_settings.lease_seconds, bot_settings.poll_timeout_seconds,
            bot_settings.telegram_timeout_seconds) == (LEASE_SECONDS, 1, 2)


@dataclasses.dataclass(frozen=True)
class _Bot:
    offering: dict
    fake: int  # id do bot na Bot API falsa (= `telegram_bot_id`)
    row: int  # `bots.id`, lido do banco

    @property
    def offering_id(self) -> str:
        return self.offering["offering_id"]


# --- auxiliares ----------------------------------------------------------------------------------


def _path(ref: str, suffix: str = "") -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}{suffix}"


def _post_ok(client: Any, path: str, op_id: str, json: Any = None) -> dict:
    kwargs = {} if json is None else {"json": json}
    resp = client.post(path, headers=auth(ADMIN_TOKEN), **kwargs)
    assert resp.status_code == 200, f"{op_id} falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _make(client: Any, bot_api: Any, sql: psycopg.Connection, state: str) -> _Bot:
    """Oferecimento com bot registrado no estado pedido (`draft`, `active`, `closed`, `archived`)."""
    offering = create_offering(client, ADMIN_TOKEN)
    bot = bot_api.add_bot()
    ref = offering_ref(offering)
    register_bot(client, ADMIN_TOKEN, ref, bot_api.token_of(bot))
    if state in ("active", "closed"):
        activate_bot(client, ADMIN_TOKEN, ref)
    if state == "closed":
        _post_ok(client, _path(ref, "/close"), "offerings.close")
    if state == "archived":
        code = _post_ok(client, _path(ref, "/archive/prepare"),
                        "offerings.prepare_archive")["confirmation_code"]
        _post_ok(client, _path(ref, "/archive"), "offerings.archive", {"confirmation_code": code})
    row = sql.execute(
        "SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.offering_id = %s",
        (uuid.UUID(offering["offering_id"]),)).fetchone()
    assert row is not None, "o bot registrado não está no banco"
    return _Bot(offering=offering, fake=bot, row=row[0])


def _run(db_url: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Chama a função de posse numa transação própria e confirma (a função não confirma)."""
    with psycopg.connect(db_url) as conn:
        result = fn(conn, **kwargs)
        conn.commit()
    return result


def _acquire(db_url: str, worker: str, now: datetime, held: Any = ()) -> list[LeasedBot]:
    got = _run(db_url, acquire_leases, worker_id=worker, now=now, lease_seconds=LEASE_SECONDS,
               held=held)
    assert isinstance(got, list), f"acquire_leases devolve lista, veio {type(got).__name__}"
    return sorted(got, key=lambda leased: leased.bot_id)


def _renew(db_url: str, worker: str, bots: list[_Bot], now: datetime) -> set[int]:
    return _run(db_url, renew_leases, worker_id=worker, bot_ids=[b.row for b in bots], now=now,
                lease_seconds=LEASE_SECONDS)


def _leased(*bots: _Bot) -> list[LeasedBot]:
    return sorted((LeasedBot(bot_id=b.row, token_version=1) for b in bots),
                  key=lambda leased: leased.bot_id)


def _lease(sql: psycopg.Connection, bot: _Bot) -> tuple[str | None, datetime | None]:
    row = sql.execute("SELECT lease_owner, lease_until FROM bots WHERE id = %s", (bot.row,)).fetchone()
    return row[0], row[1]


def _access(sql: psycopg.Connection, bot: _Bot) -> tuple[str, str | None]:
    row = sql.execute("SELECT state, access_problem FROM bots WHERE id = %s", (bot.row,)).fetchone()
    return row[0], row[1]


def _events(client: Any, *types: str) -> list[dict]:
    items = all_events(client, ADMIN_TOKEN, *types)
    seqs = [e["seq"] for e in items]
    assert all(a < b for a, b in zip(seqs, seqs[1:])), seqs
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _lease_event(bot: _Bot, worker: str, change: str, reason: str, at: datetime) -> dict:
    return {"type": "bot.lease_changed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": bot.offering_id, "worker_id": worker, "change": change,
                     "reason": reason}}


def _access_event(bot: _Bot, problem: str, at: datetime) -> dict:
    return {"type": "bot.access_changed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": bot.offering_id, "access": "lost", "problem": problem}}


def _by_offering(events: list[dict]) -> list[dict]:
    """Eventos da mesma transação sem ordem definida entre si, ordenados pelo oferecimento."""
    return sorted(events, key=lambda e: e["data"]["offering_id"])


def _poll_params(timeout: int) -> dict:
    """Parâmetros exatos de um `getUpdates` sem `offset`, com o `timeout`
    = `poll_timeout_seconds` de quem consulta."""
    return {"timeout": timeout, "limit": 100, "allowed_updates": ALLOWED_UPDATES}


def _poll_state(sql: psycopg.Connection, bot: _Bot) -> tuple[str | None, str]:
    row = sql.execute("SELECT last_problem, profile_state FROM bots WHERE id = %s",
                      (bot.row,)).fetchone()
    return row[0], row[1]


def _wait(bot_api: Any, predicate: Callable[[], Any], what: str, *, timeout: float,
          workers: tuple[Any, ...]) -> Any:
    """`bot_api.wait_for` com prazo; se um worker morrer antes, falha na hora com a causa."""
    def check() -> Any:
        for worker in workers:
            if not worker.is_alive() and not worker.returned:
                raise AssertionError(f"{what}: o worker {worker.name} terminou antes") from worker.error
        return predicate()

    try:
        return bot_api.wait_for(check, timeout=timeout)
    except AssertionError as exc:
        if exc.__cause__ is not None:
            raise
        raise AssertionError(f"{what}: {exc}") from None


def _collect(listener: NoticeListener, want: int, within: float) -> list[ConfigNotice]:
    received: list[ConfigNotice] = []
    deadline = time.monotonic() + within
    while len(received) < want:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        received.extend(listener.wait(remaining))
    return received


# --- assumir e elegibilidade -----------------------------------------------------------------------


def test_acquire_takes_only_eligible_bots_with_exact_events(client: Any, bot_api: Any, clock: Any,
                                                            sql: psycopg.Connection,
                                                            db_url: str) -> None:
    """Elegibilidade (bot `registered` de oferecimento `active` ou
    `closed`): entre um ativo, um encerrado, um rascunho, um arquivado e um ativo `no_access`, o
    `worker-a` assume só o ativo e o encerrado (`LeasedBot` com `token_version` 1), com
    `lease_until` = agora + posse e um `bot.lease_changed {acquired, acquired}` exato por bot; os
    outros ficam sem dono. Depois, nada mais a assumir: o `worker-b` recebe `[]` (posse alheia
    vigente) e o `worker-a` com `held` também, sem evento novo."""
    active = _make(client, bot_api, sql, "active")
    closed = _make(client, bot_api, sql, "closed")
    draft = _make(client, bot_api, sql, "draft")
    archived = _make(client, bot_api, sql, "archived")
    lost = _make(client, bot_api, sql, "active")
    assert _run(db_url, mark_no_access, bot_id=lost.row, token_version=1, problem="token_rejected",
                now=clock.now()) is True
    t0 = clock.advance(10)

    assert _acquire(db_url, A, t0) == _leased(active, closed)
    for bot in (active, closed):
        assert _lease(sql, bot) == (A, t0 + LEASE)
    for bot in (draft, archived, lost):
        assert _lease(sql, bot) == (None, None)
    expected = _by_offering([_lease_event(active, A, "acquired", "acquired", t0),
                             _lease_event(closed, A, "acquired", "acquired", t0)])
    assert _by_offering(_events(client, "bot.lease_changed")) == expected

    t1 = clock.advance(5)
    assert _acquire(db_url, B, t1) == []
    assert _acquire(db_url, A, t1, held={active.row, closed.row}) == []
    for bot in (active, closed):
        assert _lease(sql, bot) == (A, t0 + LEASE)
    assert _by_offering(_events(client, "bot.lease_changed")) == expected


def test_lease_boundary_is_strict_and_only_the_owner_renews(client: Any, bot_api: Any, clock: Any,
                                                            sql: psycopg.Connection,
                                                            db_url: str) -> None:
    """Posse disputada: no instante exato de
    `lease_until` ninguém assume (fronteira estrita) e o não dono não renova; 1 µs depois, com a
    posse vencida e ninguém tendo assumido, o dono ainda renova (`lease_until` = agora + posse), sem
    evento; vencida de novo, o `worker-b` assume 1 µs depois da fronteira, com evento; quem perdeu
    não renova mais (`set()`) nem reassume a posse alheia vigente, e a posse do novo dono fica
    intacta."""
    x = _make(client, bot_api, sql, "active")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x)

    until0 = clock.set(t0 + LEASE)
    assert _acquire(db_url, B, until0) == []
    assert _renew(db_url, B, [x], until0) == set()
    assert _lease(sql, x) == (A, t0 + LEASE)

    t1 = clock.set(until0 + TICK)
    assert _renew(db_url, A, [x], t1) == {x.row}
    assert _lease(sql, x) == (A, t1 + LEASE)

    until1 = clock.set(t1 + LEASE)
    assert _acquire(db_url, B, until1) == []
    t2 = clock.set(until1 + TICK)
    assert _acquire(db_url, B, t2) == _leased(x)
    assert _lease(sql, x) == (B, t2 + LEASE)

    assert _renew(db_url, A, [x], t2) == set()
    assert _acquire(db_url, A, t2, held={x.row}) == []
    assert _acquire(db_url, A, t2) == []
    assert _lease(sql, x) == (B, t2 + LEASE)
    assert _events(client, "bot.lease_changed") == [
        _lease_event(x, A, "acquired", "acquired", t0),
        _lease_event(x, B, "acquired", "acquired", t2),
    ]


def test_own_expired_lease_in_held_is_not_reacquired_but_a_new_process_adopts_it(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Assumir (`held`): uma posse própria em `held`, mesmo vencida, não é
    reassumida (`[]`, sem evento) e segue pela renovação; um processo novo com o mesmo `worker_id`
    (sem `held`) adota a posse própria que o anterior deixou, vigente, com `lease_until` novo e um
    `bot.lease_changed {acquired}`; com `held`, de novo `[]`."""
    x = _make(client, bot_api, sql, "active")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x)

    t1 = clock.advance(LEASE_SECONDS + 1)
    assert _acquire(db_url, A, t1, held={x.row}) == []
    assert _events(client, "bot.lease_changed") == [_lease_event(x, A, "acquired", "acquired", t0)]
    assert _renew(db_url, A, [x], t1) == {x.row}
    assert _lease(sql, x) == (A, t1 + LEASE)

    t2 = clock.advance(5)
    assert _acquire(db_url, A, t2) == _leased(x)
    assert _lease(sql, x) == (A, t2 + LEASE)
    assert _acquire(db_url, A, t2, held={x.row}) == []
    assert _acquire(db_url, B, t2) == []
    assert _events(client, "bot.lease_changed") == [
        _lease_event(x, A, "acquired", "acquired", t0),
        _lease_event(x, A, "acquired", "acquired", t2),
    ]


def test_concurrent_acquisition_is_disjoint(client: Any, bot_api: Any, clock: Any,
                                            sql: psycopg.Connection, db_url: str) -> None:
    """No máximo um dono (`FOR UPDATE OF b SKIP LOCKED`): o `worker-a`
    tem `x1`; com `x2` e `x3` ativos, o `worker-b` assume numa transação ainda aberta e o `worker-a`
    tenta assumir ao mesmo tempo (outra thread): os conjuntos são disjuntos e cobrem os três bots
    (`worker-a` fica só com `x1`, `worker-b` com `x2` e `x3`), com um evento por posse nova."""
    x1 = _make(client, bot_api, sql, "active")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x1)
    x2 = _make(client, bot_api, sql, "active")
    x3 = _make(client, bot_api, sql, "active")
    t1 = clock.advance(5)

    result: dict[str, Any] = {}

    def worker_a() -> None:
        try:
            result["a"] = _acquire(db_url, A, t1, held={x1.row})
        except BaseException as exc:  # noqa: BLE001 - conferida pela thread principal
            result["error"] = exc

    conn_b = psycopg.connect(db_url)
    try:
        got_b = sorted(acquire_leases(conn_b, worker_id=B, now=t1, lease_seconds=LEASE_SECONDS),
                       key=lambda leased: leased.bot_id)
        thread = threading.Thread(target=worker_a, name="cq-test-lease-a", daemon=True)
        thread.start()
        thread.join(1.0)  # com SKIP LOCKED, A não precisa esperar a transação de B
        conn_b.commit()
        thread.join(10)
        assert not thread.is_alive(), "a assunção do worker-a não terminou em 10 s"
    finally:
        conn_b.close()

    assert "error" not in result, f"a assunção do worker-a falhou: {result.get('error')!r}"
    assert got_b == _leased(x2, x3)
    assert result["a"] == []
    assert _lease(sql, x1) == (A, t0 + LEASE)
    assert _lease(sql, x2) == (B, t1 + LEASE)
    assert _lease(sql, x3) == (B, t1 + LEASE)
    events = _events(client, "bot.lease_changed")
    assert events[0] == _lease_event(x1, A, "acquired", "acquired", t0)
    assert _by_offering(events[1:]) == _by_offering([
        _lease_event(x2, B, "acquired", "acquired", t1),
        _lease_event(x3, B, "acquired", "acquired", t1)])


# --- soltar ----------------------------------------------------------------------------------------


def test_release_only_own_leases_with_events_and_notice(client: Any, bot_api: Any, clock: Any,
                                                        sql: psycopg.Connection,
                                                        db_url: str) -> None:
    """Soltar (só as próprias; `bot.lease_changed {released, <motivo>}`;
    aviso `bot`, com a chave = `offering_id`, só depois do commit): o `worker-a` pede para soltar o
    seu `x` e o `y` do `worker-b` com `shutdown` → só `[x]`, um evento e um aviso; o `y` continua
    do `worker-b`; soltar de novo → `[]`; o `worker-b` solta o `y` com `not_eligible`."""
    x = _make(client, bot_api, sql, "active")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x)
    y = _make(client, bot_api, sql, "active")
    assert _acquire(db_url, B, t0) == _leased(y)
    t1 = clock.advance(5)

    with NoticeListener(db_url) as listener:
        released = _run(db_url, release_leases, worker_id=A, bot_ids=[x.row, y.row], now=t1,
                        reason="shutdown")
        assert released == [x.row]
        assert _collect(listener, 1, 5) == [ConfigNotice(topic="bot", key=x.offering_id)]
        assert listener.wait(0.5) == []
    assert _lease(sql, x) == (None, None)
    assert _lease(sql, y) == (B, t0 + LEASE)

    assert _run(db_url, release_leases, worker_id=A, bot_ids=[x.row, y.row], now=t1,
                reason="shutdown") == []
    t2 = clock.advance(5)
    assert _run(db_url, release_leases, worker_id=B, bot_ids=[y.row], now=t2,
                reason="not_eligible") == [y.row]
    assert _lease(sql, y) == (None, None)
    assert _events(client, "bot.lease_changed") == [
        _lease_event(x, A, "acquired", "acquired", t0),
        _lease_event(y, B, "acquired", "acquired", t0),
        _lease_event(x, A, "released", "shutdown", t1),
        _lease_event(y, B, "released", "not_eligible", t2),
    ]


# --- perda de acesso -------------------------------------------------------------------------------


def test_losing_access_releases_the_lease_first_and_opens_one_episode(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Um evento de acesso por episódio; perda de acesso (a mesma
    `token_version` da chamada) e posse: `mark_no_access` com uma versão de token que não é a atual
    → `False`, sem mudança; com a versão atual → `True`, bot `no_access` (`token_rejected`), posse
    limpa, `bot.lease_changed {released, no_access, worker_id: <dono anterior>}` **antes** de
    `bot.access_changed {lost, token_rejected}`, e aviso `bot`; repetir → `False`, sem evento; num
    bot sem dono, só o evento de acesso (`token_unreadable`); o bot sem acesso não é assumido; a
    consulta do bot mostra o estado e o problema exatos."""
    t_reg = clock.now()
    x = _make(client, bot_api, sql, "active")
    y = _make(client, bot_api, sql, "draft")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x)
    t1 = clock.advance(5)

    assert _run(db_url, mark_no_access, bot_id=x.row, token_version=2, problem="token_rejected",
                now=t1) is False
    assert _access(sql, x) == ("registered", None)
    assert _lease(sql, x) == (A, t0 + LEASE)

    with NoticeListener(db_url) as listener:
        assert _run(db_url, mark_no_access, bot_id=x.row, token_version=1,
                    problem="token_rejected", now=t1) is True
        assert _collect(listener, 1, 5) == [ConfigNotice(topic="bot", key=x.offering_id)]
    assert _access(sql, x) == ("no_access", "token_rejected")
    assert _lease(sql, x) == (None, None)

    t2 = clock.advance(5)
    assert _run(db_url, mark_no_access, bot_id=x.row, token_version=1, problem="token_rejected",
                now=t2) is False
    assert _run(db_url, mark_no_access, bot_id=y.row, token_version=1, problem="token_unreadable",
                now=t2) is True
    assert _access(sql, y) == ("no_access", "token_unreadable")
    assert _acquire(db_url, B, t2) == []

    assert _events(client, "bot.lease_changed", "bot.access_changed") == [
        _lease_event(x, A, "acquired", "acquired", t0),
        _lease_event(x, A, "released", "no_access", t1),
        _access_event(x, "token_rejected", t1),
        _access_event(y, "token_unreadable", t2),
    ]

    resp = client.get(_path(offering_ref(x.offering), "/bot"), headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.get", 200, resp.json())
    assert resp.json() == {
        "offering": {"offering_id": x.offering_id, "code": x.offering["code"]},
        "username": bot_api.state(x.fake).username, "telegram_bot_id": x.fake,
        "state": "no_access", "profile_state": "pending", "registered_at": iso(t_reg),
        "token_replaced_at": None, "lease": None, "last_poll_ok_at": None,
        "problem": {"code": "token_rejected", "message": TOKEN_REJECTED_MESSAGE},
    }


# --- conferência antes de cada getUpdates ----------------------------------------------------------


def test_poll_allowed_needs_owner_version_eligibility_and_margin(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str,
        bot_settings: Any) -> None:
    """O worker para de consultar assim que perceber que perdeu a posse; regra
    (antes de cada `getUpdates`: dona, elegível, mesma `token_version` e `lease_until − agora ≥
    poll_timeout_seconds + telegram_timeout_seconds`): com a folga exata, `True`; 1 µs a menos,
    `False`, e a renovação devolve a folga; outro worker ou outra versão do token → `False`; o
    oferecimento encerrado continua elegível; o bot sem acesso não."""
    x = _make(client, bot_api, sql, "active")
    t0 = clock.advance(10)
    assert _acquire(db_url, A, t0) == _leased(x)

    def allowed(worker: str, version: int, now: datetime) -> bool:
        return _run(db_url, poll_allowed, bot_id=x.row, worker_id=worker, token_version=version,
                    now=now, settings=bot_settings)

    edge = t0 + LEASE - MARGIN
    assert allowed(A, 1, t0) is True
    assert allowed(A, 1, edge) is True
    assert allowed(A, 1, edge + TICK) is False
    assert allowed(B, 1, t0) is False
    assert allowed(A, 2, t0) is False

    clock.set(edge + TICK)
    assert _renew(db_url, A, [x], edge + TICK) == {x.row}
    assert allowed(A, 1, edge + TICK) is True

    _post_ok(client, _path(offering_ref(x.offering), "/close"), "offerings.close")
    assert allowed(A, 1, edge + TICK) is True
    assert _run(db_url, mark_no_access, bot_id=x.row, token_version=1, problem="token_rejected",
                now=edge + TICK) is True
    assert allowed(A, 1, edge + TICK) is False


# --- posse disputada e relógio que recua, pela run_worker -----------------------------------------


def test_worker_that_lost_the_lease_during_a_wait_makes_no_call_for_the_bot(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, settings: Any,
        bot_worker_thread: Any) -> None:
    """O worker para de consultar um bot assim que perceber que perdeu a posse; com a posse
    disputada, ele para de consultar antes de fazer qualquer outra chamada pelo bot;
    antes de cada `getUpdates`, a thread confere no banco que é a dona e, sem posse, sai
    sem chamar o Telegram; regras da posse (renovar: sem soltar; assumir: posse alheia vigente)
    e da recepção: o `worker-a` assume e consulta `x`; um 500 programado no 1º `getUpdates`
    deixa a thread de consulta na espera crescente (2 s no `FakeClock`), com o perfil já
    configurado; com ela esperando, o teste passa a posse ao `worker-b` por SQL, com `lease_until`
    vigente; o relógio vai ao fim da espera e anda 1 s por passo durante uma janela real maior que
    um ciclo de consulta (`poll_timeout_seconds + telegram_timeout_seconds`), sem vencer a posse do
    `worker-b`. O `worker-a` não faz nenhuma chamada por `x`, não grava `bot.lease_changed` (nem de
    soltura) e não reassume `x`. Controle positivo: um bot `y` ativado depois disso é assumido e
    consultado pelo mesmo `worker-a` (a reconciliação dele seguiu rodando), e ainda nada saiu por
    `x`; na parada, o `worker-a` solta só `y`, e a posse de `x` continua a do `worker-b`."""
    x = _make(client, bot_api, sql, "active")
    t0 = clock.now()
    bot_api.fail("getUpdates", error="server_restart", bot_id=x.fake)
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=A))
    workers = (worker,)
    _wait(bot_api, lambda: _poll_state(sql, x) == ("telegram_unavailable", "configured")
          and all(c.ended is not None for c in bot_api.calls("getUpdates", bot_id=x.fake)),
          "500 na consulta e perfil configurado", timeout=20, workers=workers)
    assert [(c.status, c.programmed, c.clock_at, c.params)
            for c in bot_api.calls("getUpdates", bot_id=x.fake)] == [
        (500, True, t0, _poll_params(1))], bot_api.transcript()
    assert _lease(sql, x) == (A, t0 + LEASE)

    until = t0 + timedelta(seconds=25)  # vigente durante toda a janela (o relógio vai até t0 + 10 s)
    sql.execute("UPDATE bots SET lease_owner = %s, lease_until = %s WHERE id = %s", (B, until, x.row))
    assert _lease(sql, x) == (B, until)
    seen = bot_api.calls()[-1].seq

    def calls_for_x() -> list[Any]:
        return [c for c in bot_api.calls(bot_id=x.fake) if c.seq > seen]

    clock.set(t0 + timedelta(seconds=2))  # fim da 1ª espera crescente (2 s)
    for _ in range(QUIET_STEPS):
        try:
            early = bot_api.wait_for(calls_for_x, timeout=QUIET_STEP_SECONDS)
        except AssertionError:
            clock.advance(1)
            continue
        raise AssertionError(f"chamada por x depois de perder a posse: {early[0].method} "
                             f"#{early[0].seq}\n{bot_api.transcript()}")
    t_end = clock.now()
    assert t_end == t0 + timedelta(seconds=2 + QUIET_STEPS)
    assert QUIET_STEPS * QUIET_STEP_SECONDS > MARGIN.total_seconds()
    assert worker.is_alive()
    assert calls_for_x() == []
    assert _lease(sql, x) == (B, until)
    assert _events(client, "bot.lease_changed") == [_lease_event(x, A, "acquired", "acquired", t0)]

    y = _make(client, bot_api, sql, "active")
    _wait(bot_api, lambda: _lease(sql, y)[0] == A and bot_api.calls("getUpdates", bot_id=y.fake),
          "posse e consulta de y pelo worker-a", timeout=20, workers=workers)
    assert calls_for_x() == []
    assert _lease(sql, x) == (B, until)
    bot_worker_thread.stop()
    assert not worker.is_alive()
    assert worker.error is None
    assert calls_for_x() == []
    assert _lease(sql, x) == (B, until)
    assert _lease(sql, y) == (None, None)
    assert _events(client, "bot.lease_changed") == [
        _lease_event(x, A, "acquired", "acquired", t0),
        _lease_event(y, A, "acquired", "acquired", t_end),
        _lease_event(y, A, "released", "shutdown", t_end),
    ]


def test_owner_clock_going_back_60s_overlaps_at_most_one_poll_cycle(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, settings: Any,
        bot_settings: Any, bot_worker_thread: Any) -> None:
    """Relógio que recua e posse disputada: dois
    workers com `FakeClock` próprios e `BotSettings(lease_seconds=12, renew_seconds=4,
    reconcile_seconds=0.5, telegram_timeout_seconds=2)`, com `poll_timeout_seconds=1` no
    `worker-dono` e `poll_timeout_seconds=2` no `worker-outro` (os dois dentro do invariante,
    12 − 4 ≥ 2 + 2), o que identifica o autor de cada `getUpdates` pelo `timeout` dos parâmetros
    exatos; o `worker-dono` assume e consulta; o relógio dele recua 60 s, a renovação grava uma
    posse que, no relógio do `worker-outro`, já venceu, e o `worker-outro` assume. Cada par de
    `overlaps()` do bot dura no máximo um ciclo do dono (`poll_timeout_seconds +
    telegram_timeout_seconds`), e a consulta dupla (pares sobrepostos e 409 de conflito) só
    acontece até um ciclo de consulta depois da tomada. O perdedor para de consultar (a regra
    de conferir antes de cada `getUpdates`): no máximo uma consulta dele começa depois da tomada (a que já
    tinha passado pela conferência) e nenhuma depois de um ciclo; o novo dono só consulta depois do
    `deleteWebhook` da posse nova. Passado o ciclo do dono, o relógio do `worker-outro` anda 4 s, o
    que solta uma eventual espera de pelo menos 3 s depois de um 409,
    e as consultas do novo dono continuam depois disso. Os eventos de posse são exatamente a
    assunção de cada um e a soltura do novo dono na parada, no relógio dele (4 s depois)."""
    recede = dataclasses.replace(bot_settings, lease_seconds=12, renew_seconds=4,
                                 reconcile_seconds=0.5, poll_timeout_seconds=1,
                                 telegram_timeout_seconds=2)
    recede_other = dataclasses.replace(recede, poll_timeout_seconds=2)
    cycle = recede.poll_timeout_seconds + recede.telegram_timeout_seconds
    owner_params = _poll_params(recede.poll_timeout_seconds)
    other_params = _poll_params(recede_other.poll_timeout_seconds)
    assert owner_params != other_params
    x = _make(client, bot_api, sql, "active")
    start = clock.now()
    clock_a = FakeClock(start)
    clock_b = FakeClock(start)

    def polls(params: dict) -> list[Any]:
        return [c for c in bot_api.calls("getUpdates", bot_id=x.fake) if c.params == params]

    owner = bot_worker_thread(clock=clock_a, bot_settings=recede,
                              settings=dataclasses.replace(settings, worker_id="worker-dono"))
    _wait(bot_api, lambda: _lease(sql, x)[0] == "worker-dono"
          and bot_api.calls("getUpdates", bot_id=x.fake), "posse e consulta do dono", timeout=20,
          workers=(owner,))
    other = bot_worker_thread(clock=clock_b, bot_settings=recede_other,
                              settings=dataclasses.replace(settings, worker_id="worker-outro"))
    assert owner.is_alive() and other.is_alive()
    assert _lease(sql, x)[0] == "worker-dono"

    clock_a.set(start - timedelta(seconds=60))
    workers = (owner, other)
    _wait(bot_api, lambda: _lease(sql, x)[0] == "worker-outro", "tomada pelo outro", timeout=20,
          workers=workers)
    deletes = _wait(bot_api, lambda: (lambda d: d if len(d) >= 2 else None)(
        bot_api.calls("deleteWebhook", bot_id=x.fake)), "deleteWebhook da posse nova", timeout=10,
        workers=workers)
    takeover = deletes[1].started
    # Passado um ciclo do dono, a consulta que ele já tinha conferido acabou, e um 409 que ela
    # tenha causado ao novo dono já chegou; só então o relógio do novo dono solta a espera.
    _wait(bot_api, lambda: monotonic() > takeover + cycle + 0.5
          and all(c.ended is not None for c in polls(owner_params)),
          "um ciclo do dono depois da tomada", timeout=15, workers=workers)
    moved = clock_b.advance(RELEASE_409_SECONDS)
    advanced_at = monotonic()
    _wait(bot_api, lambda: any(c.started > advanced_at and c.ended is not None
                               for c in polls(other_params)),
          "o novo dono segue consultando", timeout=15, workers=workers)
    bot_worker_thread.stop()
    assert not owner.is_alive() and not other.is_alive()
    assert owner.error is None and other.error is None
    bot_api.wait_for(lambda: all(c.ended is not None
                                 for c in bot_api.calls("getUpdates", bot_id=x.fake)), timeout=5)

    assert len(bot_api.calls("deleteWebhook", bot_id=x.fake)) == 2
    assert [c.seq for c in bot_api.calls("getUpdates", bot_id=x.fake)
            if c.params not in (owner_params, other_params)] == [], bot_api.transcript()
    assert polls(owner_params)[0].started < takeover, bot_api.transcript()
    assert len([c for c in polls(owner_params) if c.started > takeover]) <= 1, bot_api.transcript()
    assert all(c.started <= takeover + cycle for c in polls(owner_params)), bot_api.transcript()
    assert all(c.started > takeover for c in polls(other_params)), bot_api.transcript()
    assert any(c.started > advanced_at for c in polls(other_params)), bot_api.transcript()
    for earlier, later in bot_api.overlaps(x.fake):
        overlap = min(earlier.ended, later.ended) - later.started
        assert overlap <= cycle, f"consulta dupla por {overlap:.3f} s\n{bot_api.transcript()}"
        assert later.started <= takeover + cycle, bot_api.transcript()
    for _ended, by in bot_api.conflicts(x.fake):
        assert by.started <= takeover + cycle, bot_api.transcript()
    assert moved == start + timedelta(seconds=RELEASE_409_SECONDS)
    assert _events(client, "bot.lease_changed") == [
        _lease_event(x, "worker-dono", "acquired", "acquired", start),
        _lease_event(x, "worker-outro", "acquired", "acquired", start),
        _lease_event(x, "worker-outro", "released", "shutdown", moved),
    ]
