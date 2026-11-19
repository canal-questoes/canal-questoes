"""Subprocesso `cq-server worker` para a queda sem aviso.

`spawn_worker(env)` roda `python -m cq_server worker` (o mesmo `cq-server worker` do pacote, com o
Python da `.venv` dos testes) apontado para a Bot API falsa da sessão, com a chave-mestra dos testes
e tempos curtos dentro do invariante da posse (`WORKER_TIMES`). O teste mata o processo com
`kill()` (TerminateProcess no Windows: queda sem aviso, sem soltar posse) e o sucessor sobe numa
thread com os mesmos tempos (`WORKER_BOT_SETTINGS`).

A saída do processo (fd 1 e 2) é drenada por threads para a memória desde o início (um pipe cheio
travaria o worker) e fica em `WorkerProcess.output()` para as varreduras de identidade e segredo.

Ambiente: o do processo dos testes **sem** nenhuma variável `CQ_*` (as do desenvolvedor não vazam
para o teste), mais os padrões abaixo, mais `env` (que prevalece; `None` remove a variável).
Nenhum arquivo `.env` é lido.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
from collections.abc import Mapping

from tests.helpers.bot_api import session_fake
from tests.helpers.secrets import MASTER_KEY

# Tempos curtos do worker morto: posse 4 s, renovação 1 s, reconciliação 0,2 s,
# consulta 1 s e timeout 0,5 s. Invariante: 4 − 1 ≥ 1 + 0,5 e 0,2 ≤ 1.
WORKER_TIMES: dict[str, str] = {
    "CQ_BOT_LEASE_SECONDS": "4",
    "CQ_BOT_RENEW_SECONDS": "1",
    "CQ_BOT_RECONCILE_SECONDS": "0.2",
    "CQ_POLL_TIMEOUT_SECONDS": "1",
    "CQ_TELEGRAM_TIMEOUT_SECONDS": "0.5",
}

# Os mesmos tempos como campos de `BotSettings`, para o sucessor em thread
# (`bot_worker_thread(bot_settings=dataclasses.replace(bot_settings, **WORKER_BOT_SETTINGS), …)`).
WORKER_BOT_SETTINGS: dict[str, float | int] = {
    "lease_seconds": 4,
    "renew_seconds": 1,
    "reconcile_seconds": 0.2,
    "poll_timeout_seconds": 1,
    "telegram_timeout_seconds": 0.5,
}

# Laço das tarefas e da fila de saída também curto (o padrão é 1 s).
_CORE_LOOP_TIMES: dict[str, str] = {"CQ_POLL_INTERVAL_SECONDS": "0.05"}

_SPAWNED: list[WorkerProcess] = []
_SPAWNED_LOCK = threading.Lock()


class WorkerProcess(subprocess.Popen):
    """`Popen` do worker com a saída drenada para a memória (`output()`)."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        super().__init__(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
        self._out = bytearray()
        self._err = bytearray()
        self._drainers = [
            threading.Thread(target=self._drain, args=(self.stdout, self._out),
                             name="cq-test-worker-stdout", daemon=True),
            threading.Thread(target=self._drain, args=(self.stderr, self._err),
                             name="cq-test-worker-stderr", daemon=True),
        ]
        for thread in self._drainers:
            thread.start()

    @staticmethod
    def _drain(stream: object, sink: bytearray) -> None:
        read = getattr(stream, "read1", None) or getattr(stream, "read")
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    return
                sink.extend(chunk)
        except (OSError, ValueError):
            return

    def output(self, timeout: float = 10.0) -> tuple[str, str]:
        """(stdout, stderr) em UTF-8 (bytes inválidos trocados por U+FFFD). Depois que o processo
        terminou, espera os drenos esvaziarem os pipes (até `timeout`)."""
        if self.poll() is not None:
            for thread in self._drainers:
                thread.join(timeout)
        return (bytes(self._out).decode("utf-8", "replace"),
                bytes(self._err).decode("utf-8", "replace"))

    def stop(self, timeout: float = 10.0) -> int:
        """Mata o processo se ainda vive (sem parada ordenada) e espera o fim; devolve o código."""
        if self.poll() is None:
            self.kill()
        return self.wait(timeout)


def worker_env(env: Mapping[str, str | None]) -> dict[str, str]:
    """O ambiente completo do subprocesso (ver o docstring do módulo)."""
    full = {k: v for k, v in os.environ.items() if not k.upper().startswith("CQ_")}
    full.update({
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "CQ_MASTER_KEY": MASTER_KEY,
        "CQ_TELEGRAM_API_URL": session_fake().base_url,
        **WORKER_TIMES,
        **_CORE_LOOP_TIMES,
    })
    for key, value in env.items():
        if value is None:
            full.pop(key, None)
        else:
            full[key] = value
    assert full.get("CQ_DATABASE_URL"), "spawn_worker precisa de CQ_DATABASE_URL em env"
    return full


def spawn_worker(env: Mapping[str, str | None]) -> WorkerProcess:
    """Sobe `cq-server worker` num subprocesso com o ambiente de `worker_env(env)`.

    `env` precisa trazer `CQ_DATABASE_URL`; o resto tem padrão (`CQ_MASTER_KEY` dos testes,
    `CQ_TELEGRAM_API_URL` da Bot API falsa da sessão e `WORKER_TIMES`). Os processos que o teste
    não matar são mortos na saída da sessão.
    """
    proc = WorkerProcess([sys.executable, "-m", "cq_server", "worker"], worker_env(env))
    with _SPAWNED_LOCK:
        _SPAWNED.append(proc)
    return proc


def stop_all_workers() -> None:
    """Mata e espera todos os subprocessos criados por `spawn_worker` que ainda vivem."""
    with _SPAWNED_LOCK:
        procs = list(_SPAWNED)
        _SPAWNED.clear()
    for proc in procs:
        try:
            proc.stop(10)
        except (OSError, subprocess.TimeoutExpired):
            pass


atexit.register(stop_all_workers)
