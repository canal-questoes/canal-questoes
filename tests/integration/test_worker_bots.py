"""Workers com bots: concorrência, aviso, parada, queda e estado.

Cobre a concorrência, o aviso, a parada, a queda e o estado dos workers com bots; o
runtime dos bots (`run_worker(…, bots=…)`: threads `cq-bots` e `cq-poll-<id>`, parada com
`abort()`, queda sem escrita, modo sem chave-mestra); a consulta
(`bots.get`) e os registros `worker.bots_disabled` e `bot.lease`.

Os workers rodam em threads (`bot_worker_thread`) contra a Bot API falsa; a queda sem aviso usa um
subprocesso `cq-server worker` morto com `kill()` (`spawn_worker`) e `SystemClock`. As
esperas reais são sempre por outra thread ou processo, com `bot_api.wait_for` e prazo folgado; os
tempos medidos (`started`/`ended` das chamadas e o instante da parada) usam `bot_api.monotonic()`.

Convenções: contagens exatas (linhas, tarefas, envios por chat); eventos lidos pela API
e conferidos por inteiro onde o relógio é o `FakeClock`; ids internos só lidos do banco.
"""

from __future__ import annotations

import dataclasses
import threading
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cq_server.bots import LeasedBot, acquire_leases
from cq_server.clock import SystemClock
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    offering_ref,
    register_bot,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.channels import Crash
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.logs import capture_logs
from tests.helpers.processes import WORKER_BOT_SETTINGS, spawn_worker

# O texto `HELP`, escrito aqui para o teste não depender da implementação.
HELP = ("Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite "
        "que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
LEASE_SECONDS = 30  # o `bot_settings` do conftest
EXPIRE = LEASE_SECONDS + 1  # avanço que vence a posse (a fronteira é estrita)


@pytest.fixture(autouse=True)
def _harness_bot_settings(bot_settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `bot_settings` do conftest."""
    assert (bot_settings.lease_seconds, bot_settings.renew_seconds, bot_settings.reconcile_seconds,
            bot_settings.poll_timeout_seconds, bot_settings.telegram_timeout_seconds) == (
        LEASE_SECONDS, 10, 0.05, 1, 2)


@dataclasses.dataclass(frozen=True)
class _Bot:
    offering: dict
    fake: int
    row: int

    @property
    def offering_id(self) -> str:
        return self.offering["offering_id"]


# --- auxiliares ----------------------------------------------------------------------------------


def _setup(client: Any, bot_api: Any, sql: psycopg.Connection, *, activate: bool = True) -> _Bot:
    offering = create_offering(client, ADMIN_TOKEN)
    fake = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), bot_api.token_of(fake))
    row = sql.execute(
        "SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.offering_id = %s",
        (uuid.UUID(offering["offering_id"]),)).fetchone()
    assert row is not None, "o bot registrado não está no banco"
    bot = _Bot(offering=offering, fake=fake, row=row[0])
    if activate:
        activate_bot(client, ADMIN_TOKEN, offering_ref(offering))
    return bot


def _help(bot: _Bot) -> str:
    return HELP.replace("{course}", bot.offering["course"]).replace("{term}", bot.offering["term"])


def _wait(bot_api: Any, predicate: Callable[[], Any], what: str, *, timeout: float = 15.0,
          workers: tuple[Any, ...] = (), every: float = 0.0) -> Any:
    """`bot_api.wait_for` com prazo; se um worker morrer antes, falha na hora com a causa.
    `every` > 0 limita a frequência com que o predicado (ex.: uma contagem no banco) é avaliado."""
    last = [float("-inf")]

    def check() -> Any:
        for worker in workers:
            if not worker.is_alive() and not worker.returned:
                raise AssertionError(f"{what}: o worker {worker.name} terminou antes") from worker.error
        now = monotonic()
        if now - last[0] < every:
            return None
        last[0] = now
        return predicate()

    try:
        return bot_api.wait_for(check, timeout=timeout)
    except AssertionError as exc:
        if exc.__cause__ is not None:
            raise
        raise AssertionError(f"{what}: {exc}") from None


def _polls(bot_api: Any, bot: _Bot) -> list[Any]:
    return bot_api.calls("getUpdates", bot_id=bot.fake)


def _lease(sql: psycopg.Connection, bot: _Bot) -> tuple[str | None, datetime | None]:
    row = sql.execute("SELECT lease_owner, lease_until FROM bots WHERE id = %s", (bot.row,)).fetchone()
    return row[0], row[1]


def _count(sql: psycopg.Connection, query: str, *params: Any) -> int:
    return sql.execute(query, params).fetchone()[0]


def _lease_events(client: Any) -> list[dict]:
    items = all_events(client, ADMIN_TOKEN, "bot.lease_changed")
    seqs = [e["seq"] for e in items]
    assert all(a < b for a, b in zip(seqs, seqs[1:])), seqs
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _lease_event(bot: _Bot, worker: str, change: str, reason: str, at: datetime) -> dict:
    return {"type": "bot.lease_changed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": bot.offering_id, "worker_id": worker, "change": change,
                     "reason": reason}}


def _bot_body(client: Any, bot: _Bot) -> dict:
    resp = client.get(f"/v1/offerings/{urllib.parse.quote(bot.offering['code'], safe='')}/bot",
                      headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("bots.get", 200, body)
    return body


def _bot_threads() -> list[str]:
    """As threads do runtime dos bots vivas neste processo (`cq-bots`, `cq-poll-<id>`)."""
    return [t.name for t in threading.enumerate()
            if t.is_alive() and (t.name == "cq-bots" or t.name.startswith("cq-poll-"))]


def _never_within(predicate: Callable[[], Any], seconds: float) -> None:
    """Controle negativo com janela definida: o predicado continua falso durante `seconds`."""
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        assert not predicate(), "a condição aconteceu dentro da janela do controle"
        threading.Event().wait(0.05)


# --- concorrência ------------------------------------------------------------------------------

N_WORKERS = 4
N_BOTS = 10
PER_BOT = 100  # 1.000 updates distintos, um chat por update
REDELIVER_PER_PHASE = 5  # por bot e por fase: 100 reentregas no total


def test_four_workers_ten_bots_each_update_once_and_one_poller_per_bot(
        client: Any, bot_api: Any, sql: psycopg.Connection, settings: Any,
        bot_worker_thread: Any, stop_one: Any) -> None:
    """Concorrência; a mesma mensagem repetida entre
    workers: 4 workers e 10 bots ativos recebem 1.000 `/ajuda` distintos (100 chats por bot) e 100
    reentregas (metade antes, metade depois da troca de dono), com a parada ordenada, no meio, do
    worker que tem mais bots. Resultado: exatamente 1.000 linhas `processed` (`help`) e 1.000 tarefas
    `telegram.update` concluídas; exatamente um `sendMessage` por chat, com o `HELP` do bot certo;
    `max_concurrency(b, exclude_abandoned=True) ≤ 1` por bot; `overlaps()` e `conflicts()` só com pares
    cuja chamada anterior terminou `abandoned` depois da parada, num bot que trocou de dono, com
    sobreposição ≤ 0,1 s; os `bot.lease_changed` mostram pelo menos 2 donos, e cada bot do worker
    parado sai com `released/shutdown` e é assumido por outro; as reentregas depois da troca chegam
    ao novo dono e não geram linha nem tarefa."""
    bots = [_setup(client, bot_api, sql, activate=False) for _ in range(N_BOTS)]
    workers = {f"worker-revezamento-{k}": bot_worker_thread(
        settings=dataclasses.replace(settings, worker_id=f"worker-revezamento-{k}")) for k in range(N_WORKERS)}
    alive = tuple(workers.values())
    for bot in bots:
        activate_bot(client, ADMIN_TOKEN, offering_ref(bot.offering))
    _wait(bot_api, lambda: all(_polls(bot_api, bot) for bot in bots), "todos os bots consultados",
          workers=alive, timeout=30)

    users = {bot.fake: [make_user(i * PER_BOT + k) for k in range(PER_BOT)]
             for i, bot in enumerate(bots)}
    pushed: dict[int, list[int]] = {bot.fake: [] for bot in bots}
    half = PER_BOT // 2
    for bot in bots:
        for user in users[bot.fake][:half]:
            pushed[bot.fake].append(bot_api.push_text(bot.fake, user, "/ajuda", bot_command=True))
    _wait(bot_api, lambda: _count(sql, "SELECT count(*) FROM telegram_updates") == N_BOTS * half,
          "1ª metade gravada", workers=alive, timeout=120, every=0.2)
    for bot in bots:
        for uid in pushed[bot.fake][:REDELIVER_PER_PHASE]:
            bot_api.redeliver(bot.fake, uid)
    _wait(bot_api, lambda: all(bot_api.state(bot.fake).redeliveries == () for bot in bots),
          "reentregas antes da troca de dono", workers=alive, timeout=30)

    owners = {bot.fake: _lease(sql, bot)[0] for bot in bots}
    victim_id, _n = Counter(o for o in owners.values() if o is not None).most_common(1)[0]
    moved = [bot for bot in bots if owners[bot.fake] == victim_id]
    victim = workers[victim_id]
    remaining = tuple(w for name, w in workers.items() if name != victim_id)
    asked = monotonic()
    elapsed = stop_one(victim)
    assert elapsed <= 5, f"a parada ordenada levou {elapsed:.1f} s"
    assert victim.returned and victim.error is None

    for bot in bots:
        for user in users[bot.fake][half:]:
            pushed[bot.fake].append(bot_api.push_text(bot.fake, user, "/ajuda", bot_command=True))
    redelivered_at = monotonic()
    late = {bot.fake: pushed[bot.fake][REDELIVER_PER_PHASE:2 * REDELIVER_PER_PHASE] for bot in bots}
    for bot in bots:
        for uid in late[bot.fake]:
            bot_api.redeliver(bot.fake, uid)
    total = N_BOTS * PER_BOT
    _wait(bot_api, lambda: _count(sql, "SELECT count(*) FROM telegram_updates WHERE state = "
                                       "'processed'") == total
          and _count(sql, "SELECT count(*) FROM outbox_messages WHERE state = 'sent'") == total
          and _count(sql, "SELECT count(*) FROM tasks WHERE state <> 'done'") == 0,
          "1.000 updates processados e respondidos", workers=remaining, timeout=240, every=0.25)

    def late_redeliveries_confirmed() -> bool:
        """Cada reentrega da 2ª fase chegou numa resposta posterior a ela (nos bots que trocaram de
        dono, a uma consulta do novo dono) e uma consulta seguinte já começou (lote confirmado)."""
        for bot in bots:
            polls = _polls(bot_api, bot)
            for uid in late[bot.fake]:
                got = [c for c in polls if c.outcome == "ok" and c.ended > redelivered_at
                       and any(u["update_id"] == uid for u in c.result)]
                if bot in moved:
                    got = [c for c in got if c.started > asked]
                if not got or not any(c.started > got[-1].ended for c in polls):
                    return False
        return all(bot_api.state(bot.fake).redeliveries == () for bot in bots)

    _wait(bot_api, late_redeliveries_confirmed, "reentregas da 2ª fase confirmadas",
          workers=remaining, timeout=30)
    bot_worker_thread.stop()
    bot_api.wait_for(lambda: all(c.ended is not None for c in bot_api.calls("getUpdates")), timeout=5)

    by_row = {bot.row: bot for bot in bots}
    rows = sql.execute("SELECT id, bot_id, update_id, state, outcome FROM telegram_updates").fetchall()
    assert len(rows) == total
    assert sorted((by_row[r[1]].fake, r[2]) for r in rows) == sorted(
        (fake, uid) for fake, ids in pushed.items() for uid in ids)
    assert {(r[3], r[4]) for r in rows} == {("processed", "help")}
    tasks = sql.execute("SELECT payload, state FROM tasks WHERE task_type = 'telegram.update'").fetchall()
    assert len(tasks) == total
    assert sorted(t[0]["update_row_id"] for t in tasks) == sorted(r[0] for r in rows)
    assert {t[1] for t in tasks} == {"done"}

    sent = bot_api.sent_messages()
    per_chat = Counter(m.chat_id for m in sent)
    assert per_chat == Counter({u.user_id: 1 for us in users.values() for u in us})
    fake_bot = {bot.fake: bot for bot in bots}
    for message in sent:
        assert message.text == _help(fake_bot[message.bot_id])
        assert message.chat_id in {u.user_id for u in users[message.bot_id]}
    assert [c.outcome for c in bot_api.calls("sendMessage")] == ["ok"] * total

    moved_fakes = {bot.fake for bot in moved}
    for bot in bots:
        assert bot_api.max_concurrency(bot.fake, exclude_abandoned=True) <= 1, bot_api.transcript()
    for earlier, later in bot_api.overlaps():
        overlap = min(earlier.ended, later.ended) - later.started
        assert earlier.outcome == "abandoned" and earlier.ended >= asked, (earlier, later)
        assert earlier.bot_id in moved_fakes and overlap <= 0.1, (earlier, later, overlap)
    for ended, by in bot_api.conflicts():
        overlap = min(ended.ended, by.ended) - by.started
        assert ended.outcome == "abandoned" and ended.ended >= asked, (ended, by)
        assert ended.bot_id in moved_fakes and overlap <= 0.1, (ended, by, overlap)

    events = _lease_events(client)
    owners_seen = {e["data"]["worker_id"] for e in events if e["data"]["change"] == "acquired"}
    assert len(owners_seen) >= 2, owners_seen
    for bot in moved:
        mine = [(e["data"]["worker_id"], e["data"]["change"], e["data"]["reason"]) for e in events
                if e["data"]["offering_id"] == bot.offering_id]
        assert mine[:2] == [(victim_id, "acquired", "acquired"), (victim_id, "released", "shutdown")]
        assert len(mine) >= 3 and mine[2][1:] == ("acquired", "acquired"), mine
        assert mine[2][0] in workers and mine[2][0] != victim_id, mine


# --- aviso e parada ordenada --------------------------------------------------------------------


def test_notice_wakes_idle_worker_and_orderly_stop_hands_over(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        bot_settings: Any, bot_worker_thread: Any, stop_one: Any) -> None:
    """Aviso ao worker ocioso e parada ordenada (um
    ciclo): com `reconcile_seconds=10`, depois que o worker ocioso assumiu um bot sentinela na
    partida, a ativação de outro bot pela API faz a 1ª `getUpdates` sair em até 5 s da resposta.
    Controle: a mesma transição feita por SQL direto, sem aviso, não gera `deleteWebhook` nem
    `getUpdates` em 5 s (e o bot só é consultado depois, pela reconciliação periódica). Com os mesmos
    tempos, a parada ordenada do dono solta cada posse (`released/shutdown`) e o outro worker faz a
    1ª `getUpdates` de cada bot em até 5 s do pedido de parada."""
    slow = dataclasses.replace(bot_settings, reconcile_seconds=10)
    sentinel = _setup(client, bot_api, sql)
    target = _setup(client, bot_api, sql, activate=False)
    control = _setup(client, bot_api, sql, activate=False)
    t0 = clock.now()

    first = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-sucessao-1"),
                              bot_settings=slow)
    _wait(bot_api, lambda: _polls(bot_api, sentinel), "consulta do sentinela na partida",
          workers=(first,), timeout=30)

    activate_bot(client, ADMIN_TOKEN, offering_ref(target.offering))
    answered = monotonic()
    polled = _wait(bot_api, lambda: _polls(bot_api, target), "1ª consulta depois do aviso",
                   workers=(first,), timeout=15)
    assert polled[0].started - answered <= 5, f"{polled[0].started - answered:.2f} s"
    takeover = [c for c in bot_api.calls(bot_id=target.fake)
                if c.method in ("deleteWebhook", "getUpdates")]
    assert [c.method for c in takeover[:2]] == ["deleteWebhook", "getUpdates"]

    sql.execute("UPDATE offerings SET state = 'active', activated_at = %s WHERE offering_id = %s",
                (clock.now(), uuid.UUID(control.offering_id)))
    _never_within(lambda: [c for c in bot_api.calls(bot_id=control.fake)
                           if c.method in ("deleteWebhook", "getUpdates")], 5.0)
    second = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-sucessao-2"),
                               bot_settings=slow)
    _wait(bot_api, lambda: _polls(bot_api, control), "o controle é consultado depois",
          workers=(first, second), timeout=20)

    owned = [bot for bot in (sentinel, target, control) if _lease(sql, bot)[0] == "worker-sucessao-1"]
    assert sentinel in owned and target in owned
    asked = monotonic()
    elapsed = stop_one(first)
    assert elapsed <= 5, f"a parada ordenada levou {elapsed:.1f} s"
    assert first.returned and first.error is None
    stopped_at = first.ended_at
    for bot in owned:
        after = _wait(bot_api, lambda b=bot: [c for c in _polls(bot_api, b) if c.started > stopped_at],
                      "o outro worker assume", workers=(second,), timeout=15)
        assert after[0].started - asked <= 5, f"{after[0].started - asked:.2f} s"
        assert _lease(sql, bot)[0] == "worker-sucessao-2"

    events = _lease_events(client)
    for bot in owned:
        assert [e for e in events if e["data"]["offering_id"] == bot.offering_id][-2:] == [
            _lease_event(bot, "worker-sucessao-1", "released", "shutdown", t0),
            _lease_event(bot, "worker-sucessao-2", "acquired", "acquired", t0),
        ]


# --- queda sem aviso ---------------------------------------------------------------------------


def test_crashed_worker_bot_is_taken_over_after_the_lease_without_losing_updates(
        bot_api: Any, bots: Any, sql: psycopg.Connection, db_url: str, settings: Any,
        bot_settings: Any, bot_worker_thread: Any) -> None:
    """Queda sem aviso: um subprocesso `cq-server worker`
    (posse 4 s, renovação 1 s, reconciliação 0,2 s, consulta 1 s, timeout 0,5 s; `SystemClock`)
    assume o bot, grava e processa 3 updates e é morto com `kill()`; o sucessor, uma thread com os
    mesmos tempos e `SystemClock`, faz a 1ª `getUpdates` com `lease_until ≤ clock_at ≤ lease_until +
    reconcile_seconds + 2 s` (o `lease_until` gravado pelo morto); os 3 updates injetados na janela
    são gravados e processados uma vez, e os anteriores não se repetem; no máximo um par de consultas
    sobrepostas, só com uma chamada anterior à queda; os eventos de posse são a assunção do morto, a
    do sucessor (sem soltura do morto) e a soltura do sucessor na parada. Nenhum passo desta fase usa
    o `FakeClock`."""
    from cq_server.api import create_app

    system_clock = SystemClock()
    bot_api.reset(clock=system_clock)
    same_times = dataclasses.replace(bot_settings, **WORKER_BOT_SETTINGS)
    app = create_app(settings, clock=system_clock, bots=bots)
    with TestClient(app, raise_server_exceptions=False) as api:
        bot = _setup(api, bot_api, sql)
        proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-caido"})
        try:
            _wait(bot_api, lambda: _lease(sql, bot)[0] == "worker-caido" and _polls(bot_api, bot),
                  "o subprocesso assume e consulta", timeout=60)
            before = [bot_api.push_text(bot.fake, make_user(900 + k), f"Antes da queda {k}")
                      for k in range(3)]
            _wait(bot_api, lambda: _count(sql, "SELECT count(*) FROM telegram_updates WHERE state = "
                                               "'processed'") == 3
                  and _count(sql, "SELECT count(*) FROM tasks WHERE state <> 'done'") == 0
                  and bot_api.calls("setMyCommands", bot_id=bot.fake),
                  "o subprocesso processa os updates e o perfil", timeout=30)

            successor = bot_worker_thread(
                clock=system_clock, bot_settings=same_times,
                settings=dataclasses.replace(settings, worker_id="worker-sucessor"))
            proc.stop()
            killed_at = monotonic()  # depois da morte: toda chamada que começa depois é do sucessor
            owner, lease_until = _lease(sql, bot)
            assert owner == "worker-caido"
            window = [bot_api.push_text(bot.fake, make_user(910 + k), f"Durante a queda {k}")
                      for k in range(3)]

            polls_after = _wait(bot_api, lambda: [c for c in _polls(bot_api, bot)
                                                  if c.started > killed_at],
                                "1ª consulta do sucessor", workers=(successor,), timeout=30)
            first_after = polls_after[0]
            limit = timedelta(seconds=same_times.reconcile_seconds + 2)
            assert lease_until <= first_after.clock_at <= lease_until + limit, (
                lease_until, first_after.clock_at)
            _wait(bot_api, lambda: _count(sql, "SELECT count(*) FROM telegram_updates WHERE state = "
                                               "'processed'") == 6
                  and _count(sql, "SELECT count(*) FROM tasks WHERE state <> 'done'") == 0,
                  "updates da janela processados", workers=(successor,), timeout=30)
            bot_worker_thread.stop()
            assert successor.returned and successor.error is None
        finally:
            proc.stop()

        rows = sql.execute("SELECT update_id, state, outcome FROM telegram_updates WHERE bot_id = %s",
                           (bot.row,)).fetchall()
        assert sorted(rows) == sorted((uid, "processed", "not_handled") for uid in before + window)
        assert _count(sql, "SELECT count(*) FROM tasks WHERE task_type = 'telegram.update'") == 6
        pairs = bot_api.overlaps(bot.fake)
        assert len(pairs) <= 1, bot_api.transcript()
        for earlier, _later in pairs:
            assert earlier.started < killed_at, bot_api.transcript()

        events = _lease_events(api)
        assert [(e["data"]["worker_id"], e["data"]["change"], e["data"]["reason"])
                for e in events] == [("worker-caido", "acquired", "acquired"),
                                     ("worker-sucessor", "acquired", "acquired"),
                                     ("worker-sucessor", "released", "shutdown")]
        assert {e["data"]["offering_id"] for e in events} == {bot.offering_id}
        assert {e["actor_token_id"] for e in events} == {None}
        acquired_by_successor = datetime.fromisoformat(
            events[1]["occurred_at"].replace("Z", "+00:00"))
        assert acquired_by_successor > lease_until


# --- parada com consulta presa e registros de posse ---------------------------------------------------


def test_stop_aborts_a_stuck_get_updates_and_releases_the_lease(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        bot_settings: Any, bot_worker_thread: Any, stop_one: Any) -> None:
    """Parada (`run_worker`: na parada, `abort()` e soltura numa transação); o registro
    (`bot.lease` com `offering_id`, `change` e `reason`, por inclusão): com a leitura do
    `getUpdates` em até 11 s (`telegram_timeout_seconds=10`) e a resposta presa no dublê, a
    `run_worker` volta em até 5 s depois do `stop` (só o `abort()` explica), sem exceção; a chamada
    presa termina do lado do dublê; a posse fica solta (`bots.get` exato, com `lease: null`, sem
    consulta bem-sucedida e sem problema: a interrupção é da parada, não do Telegram), com os
    eventos `acquired` e `released/shutdown` exatos e os registros `bot.lease` correspondentes."""
    stuck_read = dataclasses.replace(bot_settings, telegram_timeout_seconds=10)
    bot = _setup(client, bot_api, sql)
    t0 = clock.now()
    bot_api.fail_network("getUpdates", kind="hang", times=None, bot_id=bot.fake)
    with capture_logs() as cap:
        worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-preso"),
                                   bot_settings=stuck_read)
        stuck = _wait(bot_api, lambda: [c for c in _polls(bot_api, bot) if c.ended is None],
                      "getUpdates preso", workers=(worker,), timeout=15)[0]
        _wait(bot_api, lambda: sql.execute("SELECT profile_state FROM bots WHERE id = %s",
                                           (bot.row,)).fetchone()[0] == "configured",
              "perfil configurado", workers=(worker,), timeout=15)
        asked = monotonic()
        elapsed = stop_one(worker)
    assert elapsed <= 5, f"run_worker levou {elapsed:.1f} s para voltar"
    assert worker.returned and worker.error is None
    ended = bot_api.wait_for(lambda: next((c for c in _polls(bot_api, bot)
                                           if c.seq == stuck.seq and c.ended is not None), None),
                             timeout=5)
    assert ended.outcome == "hang" and ended.ended - asked <= 5
    assert len(_polls(bot_api, bot)) == 1

    assert _lease(sql, bot) == (None, None)
    assert _bot_body(client, bot) == {
        "offering": {"offering_id": bot.offering_id, "code": bot.offering["code"]},
        "username": bot_api.state(bot.fake).username, "telegram_bot_id": bot.fake,
        "state": "registered", "profile_state": "configured", "registered_at": iso(t0),
        "token_replaced_at": None, "lease": None, "last_poll_ok_at": None, "problem": None,
    }
    assert _lease_events(client) == [
        _lease_event(bot, "worker-preso", "acquired", "acquired", t0),
        _lease_event(bot, "worker-preso", "released", "shutdown", t0),
    ]
    records = cap.with_event("bot.lease")
    for record in records:
        assert record.get("logger") == "cq_server.worker", record
    changes = [(r.get("offering_id"), r.get("change"), r.get("reason")) for r in records]
    assert changes == [(bot.offering_id, "acquired", "acquired"),
                       (bot.offering_id, "released", "shutdown")]


# --- queda por BaseException ------------------------------------------------------------------------


def test_crash_in_a_handler_propagates_stops_bot_threads_and_keeps_the_lease(
        client: Any, bot_api: Any, sql: psycopg.Connection, db_url: str, clock: Any,
        settings: Any, bot_worker_thread: Any) -> None:
    """Queda (`run_worker`: numa `BaseException`, para as threads sem soltar posse
    nem escrever no banco, chama `abort()` e relança): um `Crash` no tratador `telegram.update` (o do
    chamador tem precedência) faz a `run_worker` relançar em até 5 s; nenhuma thread `cq-bots` ou
    `cq-poll-*` continua viva 3 s depois; a posse fica com o worker caído, com o mesmo `lease_until`
    e sem evento de soltura, e só é assumida por outro depois de vencer (fronteira estrita)."""
    bot = _setup(client, bot_api, sql)
    t0 = clock.now()
    crashed_at: list[float] = []

    def crash(task: Any) -> None:
        crashed_at.append(monotonic())
        raise Crash()

    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-queda"),
                               handlers={"telegram.update": crash})
    _wait(bot_api, lambda: _polls(bot_api, bot), "posse e consulta", workers=(worker,))
    bot_api.push_text(bot.fake, make_user(950), "Mensagem que derruba o worker")
    worker.join(15)
    assert not worker.is_alive(), "o worker com Crash no tratador não caiu em 15 s"
    assert isinstance(worker.error, Crash), f"esperada a queda por Crash, veio {worker.error!r}"
    assert len(crashed_at) == 1 and worker.ended_at - crashed_at[0] <= 5
    bot_api.wait_for(lambda: not _bot_threads(),
                     timeout=max(0.0, worker.ended_at + 3 - monotonic()) + 0.01)
    assert _bot_threads() == []

    assert _lease(sql, bot) == ("worker-queda", t0 + timedelta(seconds=LEASE_SECONDS))
    assert _lease_events(client) == [_lease_event(bot, "worker-queda", "acquired", "acquired", t0)]

    with psycopg.connect(db_url) as conn:
        assert acquire_leases(conn, worker_id="worker-outro", now=clock.now(),
                              lease_seconds=LEASE_SECONDS) == []
        conn.commit()
    clock.set(t0 + timedelta(seconds=LEASE_SECONDS))
    with psycopg.connect(db_url) as conn:
        assert acquire_leases(conn, worker_id="worker-outro", now=clock.now(),
                              lease_seconds=LEASE_SECONDS) == []
        conn.commit()
    t1 = clock.advance(0.000001)
    with psycopg.connect(db_url) as conn:
        assert acquire_leases(conn, worker_id="worker-outro", now=t1,
                              lease_seconds=LEASE_SECONDS) == [LeasedBot(bot_id=bot.row, token_version=1)]
        conn.commit()
    assert _lease_events(client) == [
        _lease_event(bot, "worker-queda", "acquired", "acquired", t0),
        _lease_event(bot, "worker-outro", "acquired", "acquired", t1),
    ]


# --- sem chave-mestra --------------------------------------------------------------------------------


def test_without_master_key_the_worker_takes_no_lease_and_warns_once(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        bot_settings: Any, queue: Any, bot_worker_thread: Any) -> None:
    """Sem chave-mestra (`run_worker` sem chave-mestra: não assume posse de nenhum bot e registra uma
    única vez `worker.bots_disabled`, WARNING, sem marcar bots): com um bot ativo e o
    worker sem `master_key`, o laço da fila continua (5 tarefas de teste concluídas, uma de cada vez),
    o registro sai exatamente uma vez no logger `cq_server.worker`, o bot fica sem dono e
    `registered`, nenhum `bot.lease_changed` sai e a Bot API falsa não recebe chamada nenhuma do
    worker (só o `getMe` do registro)."""
    bot = _setup(client, bot_api, sql)
    no_key = dataclasses.replace(bot_settings, master_key=None)
    handled: list[Any] = []
    with capture_logs() as cap:
        worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-sem-chave"),
                                   bot_settings=no_key, handlers={"teste.eco": handled.append})
        for n in range(5):
            task_id = queue.enqueue("teste.eco", {"n": n})
            _wait(bot_api, lambda t=task_id: queue.get(t).state == "done", f"tarefa {n}",
                  workers=(worker,))
        bot_worker_thread.stop()
    assert worker.returned and worker.error is None

    disabled = cap.with_event("worker.bots_disabled")
    assert len(disabled) == 1, disabled
    assert {"logger": "cq_server.worker", "level": "WARNING"}.items() <= disabled[0].items()
    assert [task.payload for task in handled] == [{"n": n} for n in range(5)]
    assert sql.execute("SELECT state, access_problem, lease_owner, lease_until FROM bots WHERE id = %s",
                       (bot.row,)).fetchone() == ("registered", None, None, None)
    assert _lease_events(client) == []
    assert [c.method for c in bot_api.calls(bot_id=bot.fake)] == ["getMe"]


# --- consulta do bot com posse ----------------------------------------------------------------


def test_bots_get_shows_lease_owner_and_last_successful_poll(
        client: Any, bot_api: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """com o bot ativo e consultado, `bots.get` devolve o corpo
    inteiro exato: nome de usuário, identificador, `registered`, perfil `configured` (a tarefa do
    perfil rodou), `registered_at` do registro, `lease` com o dono (`worker-consulta`) e a validade
    (agora + 30 s), `last_poll_ok_at` da última resposta `ok` e `problem: null`."""
    t_reg = clock.now()
    bot = _setup(client, bot_api, sql, activate=False)
    t1 = clock.advance(60)
    activate_bot(client, ADMIN_TOKEN, offering_ref(bot.offering))
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id="worker-consulta"))

    expected = {
        "offering": {"offering_id": bot.offering_id, "code": bot.offering["code"]},
        "username": bot_api.state(bot.fake).username, "telegram_bot_id": bot.fake,
        "state": "registered", "profile_state": "configured", "registered_at": iso(t_reg),
        "token_replaced_at": None,
        "lease": {"worker_id": "worker-consulta", "until": iso(t1 + timedelta(seconds=LEASE_SECONDS))},
        "last_poll_ok_at": iso(t1), "problem": None,
    }
    _wait(bot_api, lambda: _bot_body(client, bot) == expected, "posse, consulta e perfil visíveis",
          workers=(worker,), timeout=15)
    assert _bot_body(client, bot) == expected
    assert [c.outcome for c in _polls(bot_api, bot) if c.ended is not None][:1] == ["ok"]
