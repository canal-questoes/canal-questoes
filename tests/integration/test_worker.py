"""Laço do worker.

`run_worker` roda numa thread com o `FakeClock` do teste. As esperas
reais aqui são só as que o teste exige (outra thread trabalhando), sempre com prazo. A thread de cada
worker guarda a exceção que escapar de `run_worker`: a queda simulada (`Crash`) termina a thread, e um
`run_worker` saudável volta normalmente depois do `stop`.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.outbox import OutboxRecord
from cq_server.ports.delivery import OutgoingMessage
from cq_server.tasks import ClaimedTask, TaskRecord
from tests.helpers.api import ADMIN_TOKEN, all_events, register_person
from tests.helpers.channels import Crash, ScriptedChannel
from tests.helpers.logs import capture_logs

LEASE_SECONDS = 30
LEASE = timedelta(seconds=LEASE_SECONDS)
EXPIRE = LEASE_SECONDS + 1  # avanço que vence a posse (a fronteira é estrita: lease_until < agora)
CONSOLE_REF = re.compile(r"^console-[0-9a-f]{32}$")
PASSWORD = "senha-que-nao-pode-vazar"  # a senha sentinela de `unreachable_db_url` (conftest)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.poll_interval_seconds) == (
        LEASE_SECONDS, 3, 0.05)


class _WorkerRun:
    """`run_worker` numa thread; guarda a exceção que escapar e se a função voltou normalmente."""

    def __init__(self, settings: Any, clock: Any, kwargs: dict[str, Any]) -> None:
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.returned = False
        self.thread = threading.Thread(target=self._run, args=(settings, clock, kwargs), daemon=True)

    def _run(self, settings: Any, clock: Any, kwargs: dict[str, Any]) -> None:
        from cq_server.worker import run_worker

        try:
            run_worker(settings, stop=self.stop, clock=clock, **kwargs)
            self.returned = True
        except BaseException as exc:  # a thread nunca deixa exceção escapar; os testes conferem
            self.error = exc

    def stop_and_join(self, timeout: float) -> float:
        started = time.monotonic()
        self.stop.set()
        self.thread.join(timeout)
        return time.monotonic() - started


@pytest.fixture
def start_worker(settings: Any, clock: Any) -> Iterator[Callable[..., _WorkerRun]]:
    """`start(cfg=None, **kwargs)` sobe um `run_worker`; no fim, `stop` + `join(10)` de todos."""
    runs: list[_WorkerRun] = []

    def start(cfg: Any = None, **kwargs: Any) -> _WorkerRun:
        run = _WorkerRun(settings if cfg is None else cfg, clock, kwargs)
        runs.append(run)
        run.thread.start()
        return run

    yield start
    for run in runs:
        run.stop.set()
    for run in runs:
        run.thread.join(10)
    assert not [r for r in runs if r.thread.is_alive()], "run_worker não terminou em 10 s depois de stop"


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str, run: _WorkerRun) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if not run.thread.is_alive():
            raise AssertionError(f"{what}: o worker terminou antes (erro: {run.error!r})")
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what}: não aconteceu em {timeout} s")
        time.sleep(0.02)  # espera real por outra thread, com prazo


def _register(client: Any, name: str) -> uuid.UUID:
    return uuid.UUID(register_person(client, ADMIN_TOKEN, name=name)["pseudo_id"])


def _enqueue_message(outbox: Any, db_url: str, recipient: uuid.UUID, text: str) -> int:
    with psycopg.connect(db_url) as conn:
        return outbox.enqueue(conn, recipient=recipient, text=text)


def _task(task_id: int, task_type: str, *, state: str, attempts: int, available_at: datetime,
          lease_owner: str | None = None, lease_until: datetime | None = None,
          finished_at: datetime | None = None) -> TaskRecord:
    return TaskRecord(id=task_id, task_type=task_type, state=state, attempts=attempts,
                      available_at=available_at, lease_owner=lease_owner, lease_until=lease_until,
                      last_error=None, finished_at=finished_at)


def _message(message_id: int, recipient: uuid.UUID, text: str, *, state: str, attempts: int,
             available_at: datetime, lease_owner: str | None = None, lease_until: datetime | None = None,
             channel_ref: str | None = None, sent_at: datetime | None = None) -> OutboxRecord:
    return OutboxRecord(id=message_id, recipient=recipient, text=text, state=state, attempts=attempts,
                        available_at=available_at, lease_owner=lease_owner, lease_until=lease_until,
                        channel_ref=channel_ref, last_error=None, sent_at=sent_at)


def _console_line(line: str, message_id: int, recipient: uuid.UUID, text: str) -> str:
    """Confere a linha do canal de console e devolve o `channel_ref` dela."""
    parsed = json.loads(line)
    ref = parsed.get("channel_ref")
    assert isinstance(ref, str) and CONSOLE_REF.fullmatch(ref), parsed
    assert parsed == {"channel_ref": ref, "message_id": message_id, "recipient": str(recipient),
                      "text": text}
    return ref


def _processed(cap: Any, event: str) -> list[tuple[Any, Any]]:
    records = cap.with_event(event)
    for record in records:
        assert {"logger": "cq_server.worker", "event": event}.items() <= record.items(), record
    return [(r.get("id"), r.get("state")) for r in records]


def test_worker_processes_task_and_message(client: Any, queue: Any, outbox: Any, db_url: str,
                                           clock: Any, start_worker: Any) -> None:
    """`run_worker` numa thread, com tratador de teste e
    `ConsoleChannel(StringIO)`, leva a tarefa a `done` e a mensagem a `sent` em até 5 s, com
    `task.processed` (`state: done`) e `outbox.dispatched` (`state: sent`); com `stop`, a thread
    termina em até 5 s e `run_worker` volta sem exceção."""
    pid = _register(client, "Pessoa Destinataria Worker")
    handled: list[ClaimedTask] = []
    stream = io.StringIO()
    text = "Olá do worker ✓"
    t0 = clock.now()

    with capture_logs() as cap:
        run = start_worker(channel=ConsoleChannel(stream), handlers={"teste.eco": handled.append})
        tid = queue.enqueue("teste.eco", {"n": 1, "texto": "olá"})
        mid = _enqueue_message(outbox, db_url, pid, text)
        _wait_until(lambda: queue.get(tid).state == "done" and outbox.get(mid).state == "sent", 5,
                    "tarefa e mensagem processadas", run)
        assert run.thread.is_alive()
        elapsed = run.stop_and_join(5)
    assert not run.thread.is_alive() and elapsed <= 5, f"o worker levou {elapsed:.1f} s para parar"
    assert run.error is None and run.returned, f"run_worker não voltou normalmente: {run.error!r}"

    assert handled == [ClaimedTask(id=tid, task_type="teste.eco", payload={"n": 1, "texto": "olá"},
                                   attempts=1)]
    assert queue.get(tid) == _task(tid, "teste.eco", state="done", attempts=1, available_at=t0,
                                   finished_at=t0)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, lines
    ref = _console_line(lines[0], mid, pid, text)
    assert outbox.get(mid) == _message(mid, pid, text, state="sent", attempts=1, available_at=t0,
                                       channel_ref=ref, sent_at=t0)
    assert _processed(cap, "task.processed") == [(tid, "done")]
    assert _processed(cap, "outbox.dispatched") == [(mid, "sent")]
    assert cap.with_event("exception") == []


def test_worker_console_output_file(client: Any, outbox: Any, db_url: str, clock: Any, settings: Any,
                                    start_worker: Any, tmp_path: Any) -> None:
    """Arquivo do console (`run_worker` sem `channel`: `console_output` é o caminho do arquivo, em modo
    acréscimo, fechado ao sair): a linha JSON está no arquivo, lida depois de `stop` e `join(5)`."""
    pid = _register(client, "Pessoa Destinataria Arquivo")
    path = tmp_path / "console.jsonl"
    previous = '{"linha": "de uma execucao anterior"}'
    path.write_text(previous + "\n", encoding="ascii")
    text = "Mensagem para o arquivo do console"  # só ASCII: nada no produto fixa a codificação
    t0 = clock.now()

    run = start_worker(dataclasses.replace(settings, console_output=str(path)), handlers={})
    mid = _enqueue_message(outbox, db_url, pid, text)
    _wait_until(lambda: outbox.get(mid).state == "sent", 5, "mensagem enviada ao arquivo", run)
    elapsed = run.stop_and_join(5)
    assert not run.thread.is_alive() and elapsed <= 5, f"o worker levou {elapsed:.1f} s para parar"
    assert run.error is None and run.returned, f"run_worker não voltou normalmente: {run.error!r}"

    lines = path.read_text(encoding="ascii").splitlines()
    assert len(lines) == 2 and lines[0] == previous, lines
    ref = _console_line(lines[1], mid, pid, text)
    assert outbox.get(mid) == _message(mid, pid, text, state="sent", attempts=1, available_at=t0,
                                       channel_ref=ref, sent_at=t0)
    path.unlink()  # no Windows, um arquivo que o worker deixou aberto não pode ser removido


def test_worker_console_output_dash_is_stdout(client: Any, outbox: Any, db_url: str, clock: Any,
                                             settings: Any, start_worker: Any, capsys: Any) -> None:
    """`console_output = "-"` é a saída padrão: a linha JSON do console sai na saída
    padrão, e só ela."""
    pid = _register(client, "Pessoa Destinataria Saida")
    text = "Mensagem para a saida padrao"
    t0 = clock.now()
    capsys.readouterr()

    run = start_worker(dataclasses.replace(settings, console_output="-"), handlers={})
    mid = _enqueue_message(outbox, db_url, pid, text)
    _wait_until(lambda: outbox.get(mid).state == "sent", 5, "mensagem enviada à saída padrão", run)
    elapsed = run.stop_and_join(5)
    assert not run.thread.is_alive() and elapsed <= 5, f"o worker levou {elapsed:.1f} s para parar"
    assert run.error is None and run.returned, f"run_worker não voltou normalmente: {run.error!r}"

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    ref = _console_line(lines[0], mid, pid, text)
    assert outbox.get(mid) == _message(mid, pid, text, state="sent", attempts=1, available_at=t0,
                                       channel_ref=ref, sent_at=t0)


def test_worker_waits_for_schema_when_database_unreachable(settings: Any, unreachable_db_url: str,
                                                           start_worker: Any, capsys: Any) -> None:
    """Espera do esquema (`run_worker` espera o esquema, registrando `event=exception` no logger
    `cq_server.worker` a cada tentativa que falha, e só o `stop` encerra): com o banco inalcançável, o
    primeiro registro sai em até 10 s, a espera continua tentando, a senha sentinela não aparece, e o
    `stop` encerra a thread (`join(10)`)."""
    cfg = dataclasses.replace(settings, database_url=unreachable_db_url)

    def failures(cap: Any) -> list[dict]:
        return [r for r in cap.records
                if r.get("event") == "exception" and r.get("logger") == "cq_server.worker"]

    with capture_logs() as cap:
        run = start_worker(cfg, channel=ScriptedChannel([]), handlers={})
        _wait_until(lambda: len(failures(cap)) >= 1, 10, "primeiro registro de exceção", run)
        _wait_until(lambda: len(failures(cap)) >= 2, 10, "segunda tentativa de ler o esquema", run)
        assert run.thread.is_alive()
        elapsed = run.stop_and_join(10)
    assert not run.thread.is_alive() and elapsed <= 10, f"o worker levou {elapsed:.1f} s para parar"
    assert run.error is None and run.returned, f"run_worker não voltou normalmente: {run.error!r}"

    captured = capsys.readouterr()
    for name, text in (("logs", cap.text), ("stdout", captured.out), ("stderr", captured.err)):
        assert PASSWORD not in text, f"a senha do banco vazou em {name}"
        assert "Traceback" not in text, f"rastreamento de pilha em {name}"


def test_worker_crash_then_takeover_completes_everything(client: Any, queue: Any, outbox: Any,
                                                         db_url: str, clock: Any, settings: Any,
                                                         start_worker: Any) -> None:
    """um primeiro `run_worker` cai com `Crash` no
    tratador e outro com `Crash` no canal, deixando uma tarefa e uma mensagem em posse; depois de
    `EXPIRE`, um novo `run_worker` leva todas as tarefas a `done` e todas as mensagens a `sent`, com
    `attempts` exatos, sem intervenção manual e sem eventos de falha."""
    pid = _register(client, "Pessoa Destinataria Retomada")
    t0 = clock.now()
    task_type = "retomada.item"
    tasks = [queue.enqueue(task_type, {"n": n}) for n in range(4)]
    assert tasks == sorted(tasks)

    # 1) Queda no tratador: só há tarefas; o worker cai na primeira (a de menor id).
    crashed_on: list[ClaimedTask] = []

    def crash_handler(task: ClaimedTask) -> None:
        crashed_on.append(task)
        raise Crash()

    w1 = start_worker(dataclasses.replace(settings, worker_id="worker-queda-1"),
                      channel=ScriptedChannel([]), handlers={task_type: crash_handler})
    w1.thread.join(5)
    assert not w1.thread.is_alive(), "o worker com Crash no tratador não caiu em 5 s"
    assert isinstance(w1.error, Crash), f"esperada a queda por Crash, veio {w1.error!r}"
    assert crashed_on == [ClaimedTask(id=tasks[0], task_type=task_type, payload={"n": 0}, attempts=1)]
    assert queue.get(tasks[0]) == _task(tasks[0], task_type, state="leased", attempts=1, available_at=t0,
                                        lease_owner="worker-queda-1", lease_until=t0 + LEASE)
    for tid in tasks[1:]:
        assert queue.get(tid) == _task(tid, task_type, state="pending", attempts=0, available_at=t0)

    # 2) Queda no canal: o worker cai no primeiro envio (a mensagem de menor id). Antes disso, ele
    # pode ter concluído parte das outras tarefas (a ordem entre as filas no laço não é contrato).
    texts = [f"Mensagem {n} da retomada" for n in range(4)]
    messages = [_enqueue_message(outbox, db_url, pid, text) for text in texts]
    handled: list[ClaimedTask] = []
    crash_channel = ScriptedChannel([Crash()])
    w2 = start_worker(dataclasses.replace(settings, worker_id="worker-queda-2"),
                      channel=crash_channel, handlers={task_type: handled.append})
    w2.thread.join(5)
    assert not w2.thread.is_alive(), "o worker com Crash no canal não caiu em 5 s"
    assert isinstance(w2.error, Crash), f"esperada a queda por Crash, veio {w2.error!r}"
    assert crash_channel.calls == [OutgoingMessage(message_id=messages[0], recipient=pid, text=texts[0])]
    assert outbox.get(messages[0]) == _message(messages[0], pid, texts[0], state="leased", attempts=1,
                                               available_at=t0, lease_owner="worker-queda-2",
                                               lease_until=t0 + LEASE)
    for mid, text in zip(messages[1:], texts[1:], strict=True):
        assert outbox.get(mid) == _message(mid, pid, text, state="pending", attempts=0, available_at=t0)
    assert queue.get(tasks[0]).state == "leased"
    before = {tid: queue.get(tid) for tid in tasks[1:]}
    done_by_w2 = {tid for tid, record in before.items() if record.state == "done"}
    for tid, record in before.items():
        assert record in (_task(tid, task_type, state="pending", attempts=0, available_at=t0),
                          _task(tid, task_type, state="done", attempts=1, available_at=t0,
                                finished_at=t0)), record
    assert sorted(task.id for task in handled) == sorted(done_by_w2)

    # 3) A posse vence; um novo worker retoma tudo.
    clock.advance(EXPIRE)
    t1 = clock.now()
    stream = io.StringIO()
    w3 = start_worker(dataclasses.replace(settings, worker_id="worker-retomada"),
                      channel=ConsoleChannel(stream), handlers={task_type: handled.append})
    _wait_until(lambda: all(queue.get(t).state == "done" for t in tasks)
                and all(outbox.get(m).state == "sent" for m in messages), 10, "retomada completa", w3)
    elapsed = w3.stop_and_join(5)
    assert not w3.thread.is_alive() and elapsed <= 5, f"o worker levou {elapsed:.1f} s para parar"
    assert w3.error is None and w3.returned, f"run_worker não voltou normalmente: {w3.error!r}"

    assert queue.get(tasks[0]) == _task(tasks[0], task_type, state="done", attempts=2, available_at=t0,
                                        finished_at=t1)
    for tid in tasks[1:]:
        expected = before[tid] if tid in done_by_w2 else _task(
            tid, task_type, state="done", attempts=1, available_at=t0, finished_at=t1)
        assert queue.get(tid) == expected
    expected_calls = [ClaimedTask(id=tid, task_type=task_type, payload={"n": n}, attempts=2 if n == 0 else 1)
                      for n, tid in enumerate(tasks)]
    assert sorted(handled, key=lambda task: task.id) == expected_calls, "cada tarefa concluída uma vez"

    lines = stream.getvalue().splitlines()
    assert len(lines) == len(messages), lines
    by_id = {json.loads(line).get("message_id"): line for line in lines}
    assert sorted(by_id) == sorted(messages), "cada mensagem enviada exatamente uma vez"
    for n, (mid, text) in enumerate(zip(messages, texts, strict=True)):
        ref = _console_line(by_id[mid], mid, pid, text)
        assert outbox.get(mid) == _message(mid, pid, text, state="sent", attempts=2 if n == 0 else 1,
                                           available_at=t0, channel_ref=ref, sent_at=t1)
    assert all_events(client, ADMIN_TOKEN, "task.failed", "outbox.failed") == []
