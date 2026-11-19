"""A API dos testes num subprocesso do loopback.


`serve(db_url, *, env=None) -> ApiServer` roda `python -m cq_server serve --host 127.0.0.1 --port <porta>`
(o `cq-server serve` do pacote, com o Python da `.venv` dos testes: o `create_app` do servidor no
`uvicorn`, depois das migrações, que no banco clonado do template já estão aplicadas) numa porta livre do
loopback, resolvida antes (bind na porta 0 e fechamento; a corrida residual é aceita), espera o
`/health` responder (qualquer resposta HTTP: o 503 também é a API no ar) por até `READY_TIMEOUT` segundos
e devolve o `ApiServer(url, stop)`. Se o processo sai antes ou o limite passa, o servidor é parado e o
teste falha com `AssertionError` (nunca `skip`).

Ambiente: o do processo dos testes **sem** nenhuma variável `CQ_*` (as do desenvolvedor não vazam; como
`tests/helpers/processes.py`), mais `CQ_DATABASE_URL`, o `CQ_ADMIN_TOKEN` e a `CQ_MASTER_KEY` dos testes e
a Bot API apontada para o loopback fechado (`http://127.0.0.1:9`; nada chama o Telegram), mais `env`
(que prevalece; `None` remove a variável). Nenhum endereço fora do loopback; nenhum `.env` é lido.

`ApiServer.stop()` termina o processo (e o mata se não sair), espera o fim e confere que ele saiu;
`output()` devolve o stdout e o stderr drenados (para as varreduras). Os servidores que o teste não parar
são parados na saída da sessão.
"""

from __future__ import annotations

import atexit
import http.client
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.processes import WorkerProcess
from tests.helpers.secrets import MASTER_KEY

HOST = "127.0.0.1"
READY_TIMEOUT = 60.0
STOP_TIMEOUT = 15.0
_POLL_SECONDS = 0.1
# Bot API no loopback fechado: a API não fala com o Telegram real em nenhum caso.
_CLOSED_BOT_API = "http://127.0.0.1:9"

_SERVERS: list[ApiServer] = []
_SERVERS_LOCK = threading.Lock()


def free_port() -> int:
    """Uma porta livre do loopback (bind na porta 0 e fechamento)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def server_env(db_url: str, env: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """O ambiente completo do subprocesso (ver o docstring do módulo)."""
    full = {k: v for k, v in os.environ.items() if not k.upper().startswith("CQ_")}
    full.update({
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "CQ_DATABASE_URL": db_url,
        "CQ_ADMIN_TOKEN": ADMIN_TOKEN,
        "CQ_MASTER_KEY": MASTER_KEY,
        "CQ_TELEGRAM_API_URL": _CLOSED_BOT_API,
    })
    for key, value in (env or {}).items():
        if value is None:
            full.pop(key, None)
        else:
            full[key] = value
    return full


class ApiServer:
    """A API num subprocesso: `url` (`http://127.0.0.1:<porta>`, sem barra no fim), `port`, `stop()` e
    `output()`."""

    def __init__(self, process: WorkerProcess, port: int) -> None:
        self.process = process
        self.port = port
        self.url = f"http://{HOST}:{port}"

    def __repr__(self) -> str:
        return f"ApiServer(url={self.url!r}, running={self.process.poll() is None})"

    def output(self, timeout: float = 10.0) -> tuple[str, str]:
        return self.process.output(timeout)

    def stop(self) -> int:
        """Termina o processo (e o mata se preciso) e confere que ele saiu; devolve o código de saída."""
        with _SERVERS_LOCK:
            if self in _SERVERS:
                _SERVERS.remove(self)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(STOP_TIMEOUT)
        code = self.process.poll()
        assert code is not None, "a API do subprocesso não saiu depois do stop"
        return code


def _wait_ready(server: ApiServer) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            break
        conn = http.client.HTTPConnection(HOST, server.port, timeout=2.0)
        try:
            conn.request("GET", "/health")
            conn.getresponse().read()
            return
        except OSError:
            time.sleep(_POLL_SECONDS)
        finally:
            conn.close()
    code = server.process.poll()
    server.stop()
    _, err = server.output()
    reason = (f"saiu com o código {code}" if code is not None
              else f"não respondeu ao /health em {READY_TIMEOUT:.0f} s")
    raise AssertionError(f"a API do subprocesso {reason}; stderr:\n{err[-4000:]}")


def serve(db_url: str, *, env: Mapping[str, str | None] | None = None) -> ApiServer:
    """Sobe a API dos testes num subprocesso do loopback e espera o `/health` responder."""
    port = free_port()
    args = [sys.executable, "-m", "cq_server", "serve", "--host", HOST, "--port", str(port)]
    server = ApiServer(WorkerProcess(args, server_env(db_url, env)), port)
    with _SERVERS_LOCK:
        _SERVERS.append(server)
    _wait_ready(server)
    return server


def stop_all_servers() -> None:
    """Para todos os servidores que ainda vivem."""
    with _SERVERS_LOCK:
        servers = list(_SERVERS)
    for server in servers:
        try:
            server.stop()
        except (OSError, subprocess.TimeoutExpired, AssertionError):
            pass


atexit.register(stop_all_servers)
