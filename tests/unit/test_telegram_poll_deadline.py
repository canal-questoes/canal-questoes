"""Prazo total do `getUpdates` (regressão de um defeito de concorrência).


O defeito: antes de cada `getUpdates`, o worker (`cq_server.bots.poll_allowed`) exige que a posse
dure pelo menos a **margem** `poll_timeout_seconds + telegram_timeout_seconds`; mas os tempos do
`HttpTelegramBotApi` são **por fase** e, na leitura, **por leitura** (`poll_timeout_seconds +
timeout_seconds` a cada `recv`). Uma resposta que goteja (um pedaço a cada intervalo menor que o tempo
de leitura) mantém a chamada viva muito além da margem, e a posse pode vencer com o `getUpdates` ainda
em andamento.

O comportamento esperado (no molde do vigia do prazo total do SMTP): o `getUpdates`
tem um prazo **total** igual à margem, contado do **início da chamada** (conexão, escrita e leitura
dentro dele); ao estourar, a chamada termina com a falha transitória da porta (`TelegramUnavailable`,
a mesma de rede e de tempo esgotado), a conexão é **fechada pelo cliente** (a troca
HTTP acaba de fato, não só a espera do chamador) e o objeto continua utilizável na chamada seguinte.
O `abort()` permanente fica reservado à parada, e um `abort()` antes do prazo continua saindo
como `Aborted`.

A Bot API falsa (`tests/helpers/bot_api.py`) não goteja respostas; este arquivo tem o servidor mínimo
`_DripServer` (socket cru numa thread, em 127.0.0.1 com porta efêmera), que atende cada conexão num
destes modos: `drip-length` (cabeçalhos 200 com `Content-Length` grande e um byte do corpo a cada
0,3 s), `drip-chunked` (o mesmo com `Transfer-Encoding: chunked`, um pedaço de um byte a cada 0,3 s),
`drip-headers` (a linha de status e um cabeçalho que goteja um byte a cada 0,3 s, sem nunca terminar
os cabeçalhos), `late-drip` (espera `late_seconds` antes do primeiro byte e depois goteja como
`drip-length`) e `normal`/`late` (a resposta JSON inteira de uma vez, na hora ou depois de uma
espera). O gotejamento para sozinho depois de `DRIP_FOR` segundos (o servidor fecha a conexão), para
que o teste nunca fique preso; a chamada roda numa thread com prazo de espera, e a fixture fecha tudo
no fim. O servidor anota, por conexão, **como** ela terminou (`client`: erro de envio ou EOF do
cliente; `server`: `DRIP_FOR` esgotado; `stop`: a parada do próprio servidor; `answered`: resposta
inteira enviada) e o instante monotônico do fim. A guarda de rede (`network_guard`) fica ligada em
todos os testes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import secrets
import select
import socket
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from cq_server.adapters.telegram import HttpTelegramBotApi
from cq_server.ports.telegram import (
    Aborted,
    MinimalUpdate,
    TelegramError,
    TelegramUnavailable,
)
from tests.helpers.bot_api import monotonic
from tests.helpers.secrets import remember_secret

# Os tempos do caso do defeito: margem = 1 + 0,5 = 1,5 s (a de `poll_allowed`).
POLL_TIMEOUT_SECONDS = 1
TIMEOUT_SECONDS = 0.5
MARGIN = POLL_TIMEOUT_SECONDS + TIMEOUT_SECONDS
# Janela aceita para o instante em que `get_updates` devolve o controle, contada do início da
# chamada: não antes da margem (menos 0,25 s) e não depois da margem mais 0,3 s (agendamento de
# threads e granularidade de relógio no Windows; um vigia por `Timer` erra por ~16 ms).
EARLIEST = MARGIN - 0.25
LATEST = MARGIN + 0.3
# O servidor goteja um byte a cada `DRIP_INTERVAL` (bem menos que o tempo de leitura de 1,5 s) e
# desiste depois de `DRIP_FOR` (bem mais que `LATEST` e que `CLOSED_BY`), fechando a conexão.
DRIP_INTERVAL = 0.3
DRIP_FOR = 5.0
# Até quando (do início da chamada) o servidor precisa ver a conexão fechada PELO CLIENTE: a margem,
# mais até dois intervalos de gotejamento para o servidor perceber o fechamento (EOF ou erro de
# envio), mais folga. Bem abaixo de `DRIP_FOR`: uma chamada abandonada numa thread não passa.
CLOSED_BY = MARGIN + 2 * DRIP_INTERVAL + 0.3
# Quanto o teste espera a chamada terminar antes de declará-la presa.
JOIN_SECONDS = DRIP_FOR + 5.0

# Fases anteriores à resposta: o servidor espera `SLOW_FIRST_BYTE` antes do primeiro
# byte (dentro do tempo de leitura de 1,5 s), ou a conexão do cliente, ou o primeiro envio dele, leva
# `SLOW_PHASE` a mais (dentro do tempo de conexão e de escrita de 0,5 s). Um prazo contado a partir
# da resposta (ou do primeiro byte) terminaria em `atraso + MARGIN`, acima de `LATEST`.
SLOW_FIRST_BYTE = 1.0
SLOW_PHASE = 0.45
assert SLOW_PHASE + MARGIN > LATEST and SLOW_FIRST_BYTE + MARGIN > LATEST
assert SLOW_PHASE < TIMEOUT_SECONDS and SLOW_FIRST_BYTE < POLL_TIMEOUT_SECONDS + TIMEOUT_SECONDS
assert LATEST < CLOSED_BY < DRIP_FOR

# `abort()` chega durante o gotejamento, bem antes do prazo; a chamada sai em até `ABORT_SECONDS`.
ABORT_AT = 0.5
ABORT_SECONDS = 0.5
assert ABORT_AT + ABORT_SECONDS < EARLIEST

DRIP_MODES = ("drip-length", "drip-chunked", "drip-headers")

# Um update de mensagem no formato da Bot API e o que a porta devolve dele.
RAW_UPDATE = {
    "update_id": 810999001,
    "message": {
        "message_id": 7, "date": 1700000000,
        "chat": {"id": 5100009001, "type": "private", "first_name": "Teste"},
        "from": {"id": 5100009001, "is_bot": False, "first_name": "Teste"},
        "text": "oi",
    },
}
EXPECTED_UPDATE = MinimalUpdate(
    update_id=810999001, kind="message", chat_id=5100009001, chat_type="private",
    sender_id=5100009001, text="oi", command=None,
    details={"message_id": 7, "date": 1700000000})
OK_BODY = json.dumps({"ok": True, "result": [RAW_UPDATE]}, separators=(",", ":")).encode("ascii")

_LENGTH_HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 1000000\r\n\r\n{")


# ---------------------------------------------------------------------------------------------
# Servidor mínimo que goteja
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Connection:
    """Uma conexão aceita pelo servidor: como terminou (`client`, `server`, `stop`, `answered` ou
    `None` se ainda aberta) e quando (`monotonic()`); `done` é sinalizado no fim."""

    end: str | None = None
    ended_at: float | None = None
    done: threading.Event = dataclasses.field(default_factory=threading.Event)


class _DripServer:
    """Servidor HTTP/1.1 mínimo sobre socket cru, em 127.0.0.1 com porta efêmera.

    Cada conexão é atendida numa thread: lê o pedido inteiro (cabeçalhos e corpo pelo
    `Content-Length`), anota o último segmento do caminho (o método, nunca o token) e responde
    conforme `mode`, lido na chegada do pedido. Durante o gotejamento, o servidor vigia o socket:
    EOF ou erro de leitura ou de envio vindo do cliente encerra a conexão como `client` (se o
    servidor não está parando). `close()` para tudo: fecha o socket de escuta e as conexões abertas
    (`shutdown` + `close`) e espera as threads.
    """

    def __init__(self) -> None:
        self.mode = "normal"
        self.late_seconds = 0.0
        self.methods: list[str] = []
        self.connections: list[_Connection] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._acceptor = threading.Thread(target=self._accept_loop, name="cq-test-drip-accept",
                                          daemon=True)
        self._acceptor.start()

    def threads(self) -> list[threading.Thread]:
        with self._lock:
            return [self._acceptor, *self._threads]

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            conn.settimeout(None)
            record = _Connection()
            thread = threading.Thread(target=self._serve, args=(conn, record),
                                      name="cq-test-drip-conn", daemon=True)
            with self._lock:
                if self._stop.is_set():
                    conn.close()
                    return
                self._conns.append(conn)
                self._threads.append(thread)
                self.connections.append(record)
            thread.start()

    def _read_request(self, conn: socket.socket) -> str | None:
        """Lê um pedido inteiro e devolve o método da Bot API (último segmento do caminho)."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        size = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length":
                size = int(value.strip())
        while len(body) < size:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            body += chunk
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else ""
        return path.rsplit("/", 1)[-1]

    def _finish(self, record: _Connection, end: str) -> None:
        if record.end is None:
            if end == "client" and self._stop.is_set():
                end = "stop"  # o `shutdown` do próprio `close()` não é ação do cliente
            record.end, record.ended_at = end, monotonic()
        record.done.set()

    def _client_gone(self, conn: socket.socket) -> bool:
        """Verdadeiro se o cliente fechou ou derrubou a conexão (o cliente não manda mais nada
        depois do pedido, então socket legível aqui é EOF ou erro)."""
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                return False
            return conn.recv(1) == b""
        except (OSError, ValueError):
            return True

    def _drip(self, conn: socket.socket, record: _Connection, head: bytes, piece: bytes) -> None:
        """Manda `head` e depois `piece` a cada `DRIP_INTERVAL`, até `DRIP_FOR`, a parada do
        servidor ou o cliente sair (vigiado a cada 20 ms)."""
        try:
            conn.sendall(head)
        except OSError:
            self._finish(record, "client")
            return
        started = monotonic()
        ends = started + DRIP_FOR
        next_piece = started + DRIP_INTERVAL
        while True:
            if self._stop.is_set():
                self._finish(record, "stop")
                return
            if monotonic() >= ends:
                self._finish(record, "server")
                return
            if self._client_gone(conn):
                self._finish(record, "client")
                return
            if monotonic() >= next_piece:
                try:
                    conn.sendall(piece)
                except OSError:
                    self._finish(record, "client")
                    return
                next_piece += DRIP_INTERVAL
            self._stop.wait(0.02)

    def _serve(self, conn: socket.socket, record: _Connection) -> None:
        try:
            method = self._read_request(conn)
            if method is None:
                self._finish(record, "client")
                return
            with self._lock:
                self.methods.append(method)
                mode, late = self.mode, self.late_seconds
            if mode in ("late", "late-drip") and late and self._stop.wait(late):
                self._finish(record, "stop")
                return
            if mode in ("drip-length", "late-drip"):
                self._drip(conn, record, _LENGTH_HEAD, b" ")
            elif mode == "drip-chunked":
                self._drip(conn, record, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                         b"Transfer-Encoding: chunked\r\n\r\n1\r\n{\r\n",
                           b"1\r\n \r\n")
            elif mode == "drip-headers":
                self._drip(conn, record, b"HTTP/1.1 200 OK\r\nX-Padding: ", b"a")
            else:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(OK_BODY)).encode("ascii")
                             + b"\r\nConnection: close\r\n\r\n" + OK_BODY)
                self._finish(record, "answered")
        except OSError:
            self._finish(record, "client")  # (ou `stop`, se o servidor está parando)
        finally:
            self._finish(record, "client")  # sem efeito se já anotado
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._acceptor.join(5)
        with self._lock:
            conns, threads = list(self._conns), list(self._threads)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for thread in threads:
            thread.join(5)


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def drip_server(network_guard: Any) -> Iterator[_DripServer]:
    """O servidor que goteja, com a guarda de rede ligada; fechado no fim do teste."""
    server = _DripServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def make_api() -> Iterator[Callable[..., HttpTelegramBotApi]]:
    """Constrói adaptadores com os tempos do caso do defeito e fecha todos no fim do teste."""
    created: list[HttpTelegramBotApi] = []

    def build(token: str, base_url: str) -> HttpTelegramBotApi:
        api = HttpTelegramBotApi(token, base_url=base_url,
                                 poll_timeout_seconds=POLL_TIMEOUT_SECONDS,
                                 timeout_seconds=TIMEOUT_SECONDS)
        created.append(api)
        return api

    yield build
    for api in created:
        api.close()


def _local_token() -> str:
    """Token fictício no formato da Bot API (registrado nas varreduras de segredos)."""
    secret = secrets.token_urlsafe(27)[:35]
    token = f"6100009998:{secret}"
    remember_secret(token)
    remember_secret(secret)
    return token


class _Background:
    """Uma chamada numa thread, com início e fim medidos por `monotonic()`. `join()` espera até
    `JOIN_SECONDS` (o teste nunca fica preso: se a chamada não termina, `stuck` é verdadeiro)."""

    def __init__(self, call: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.elapsed: float | None = None
        self.ended: float | None = None
        self.stuck = False
        self.started = monotonic()

        def run() -> None:
            try:
                self.result = call()
            except BaseException as exc:  # noqa: BLE001 - guardada para o teste conferir
                self.error = exc
            finally:
                self.ended = monotonic()
                self.elapsed = self.ended - self.started

        self.thread = threading.Thread(target=run, name="cq-test-poll-deadline", daemon=True)
        self.thread.start()

    def join(self, seconds: float = JOIN_SECONDS) -> bool:
        self.thread.join(seconds)
        self.stuck = self.thread.is_alive()
        return not self.stuck


def _timed(call: Callable[[], Any]) -> _Background:
    """A chamada numa thread, já esperada (até `JOIN_SECONDS`)."""
    running = _Background(call)
    running.join()
    return running


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    ends = monotonic() + seconds
    while monotonic() < ends:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _dripped_get_updates(api: HttpTelegramBotApi, server: _DripServer, mode: str) -> _Background:
    server.mode = mode
    timed = _timed(lambda: api.get_updates(offset=None))
    assert not timed.stuck, f"get_updates continuou preso mais de {JOIN_SECONDS} s"
    return timed


def _assert_ends_within_the_margin(timed: _Background, what: str) -> None:
    assert timed.elapsed is not None
    assert EARLIEST <= timed.elapsed <= LATEST, (
        f"get_updates ({what}) devolveu o controle em {timed.elapsed:.2f} s desde o início da "
        f"chamada; o prazo total é a margem de {MARGIN} s (aceito de {EARLIEST} a {LATEST} s)")
    assert type(timed.error) is TelegramUnavailable, repr(timed.error)


def _assert_client_closed_the_connection(server: _DripServer, timed: _Background,
                                         what: str) -> None:
    """A troca HTTP acabou de fato no prazo: o servidor viu a conexão terminar por ação
    do cliente (EOF ou erro de envio) em até `CLOSED_BY` desde o início da chamada, e não pelo
    próprio `DRIP_FOR` (uma chamada abandonada numa thread seguiria viva até lá)."""
    [record] = server.connections
    assert record.done.wait(DRIP_FOR + 2.0), "a conexão do servidor não terminou"
    assert record.ended_at is not None
    since = record.ended_at - timed.started
    assert record.end == "client", (
        f"get_updates ({what}): a conexão terminou por {record.end!r} em {since:.2f} s desde o "
        f"início da chamada, não pelo cliente (a troca HTTP seguiu viva depois do prazo)")
    assert since <= CLOSED_BY, (
        f"get_updates ({what}): o cliente fechou a conexão em {since:.2f} s desde o início da "
        f"chamada (aceito até {CLOSED_BY:.2f} s)")


def _leaks(texts: list[str], token: str) -> int:
    secret_part = token.split(":", 1)[1]
    return sum(1 for text in texts
               if token in text or secret_part in text or f"/bot{token}" in text)


def _exception_texts(exc: BaseException) -> list[str]:
    """`str`, `repr`, `args`, notas e traceback formatado da exceção e de toda a cadeia."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts += [str(current), repr(current), repr(current.args),
                  "".join(traceback.format_exception(current))]
        texts += [str(note) for note in getattr(current, "__notes__", ())]
        pending += [current.__cause__, current.__context__]
    return texts


# ---------------------------------------------------------------------------------------------
# Testes do defeito (falham no código com o defeito)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", DRIP_MODES)
def test_dripping_get_updates_ends_within_the_margin(drip_server: _DripServer,
                                                     make_api: Callable[..., HttpTelegramBotApi],
                                                     mode: str) -> None:
    """O defeito: com `poll_timeout_seconds=1` e `timeout_seconds=0.5` (margem de
    1,5 s, a de `poll_allowed`), um `getUpdates` cuja resposta goteja um byte a cada 0,3 s (no corpo
    com `Content-Length`, no corpo `chunked` ou nos próprios cabeçalhos) devolve o controle com
    `TelegramUnavailable` entre margem − 0,25 s e margem + 0,3 s, e o servidor vê a conexão fechada
    pelo cliente em até margem + 2·0,3 + 0,3 s. Com o defeito, cada leitura renova o tempo, e a chamada só
    termina quando o servidor desiste (~5 s)."""
    api = make_api(_local_token(), drip_server.base_url)

    timed = _dripped_get_updates(api, drip_server, mode)

    _assert_ends_within_the_margin(timed, mode)
    assert drip_server.methods == ["getUpdates"]
    _assert_client_closed_the_connection(drip_server, timed, mode)


def test_the_client_closes_the_dripping_connection(drip_server: _DripServer,
                                                   make_api: Callable[..., HttpTelegramBotApi]
                                                   ) -> None:
    """O prazo encerra a troca HTTP, não só a espera do chamador. Uma correção que
    abandona a chamada numa thread (e devolve `TelegramUnavailable` no prazo) deixaria o
    `getUpdates` vivo no servidor até `DRIP_FOR`, que é o próprio defeito (outra consulta do
    mesmo bot, depois do fim da posse, seria simultânea). Aqui só entram o tipo da falha e o fim da
    conexão do lado do servidor: terminou por ação do cliente em até `CLOSED_BY` desde o início da
    chamada (precedente: `test_abort_interrupts_a_stuck_get_updates`, que exige o dublê ver a
    conexão fechada)."""
    api = make_api(_local_token(), drip_server.base_url)

    timed = _dripped_get_updates(api, drip_server, "drip-length")

    _assert_client_closed_the_connection(drip_server, timed, "drip-length")
    assert type(timed.error) is TelegramUnavailable, repr(timed.error)


def test_the_deadline_counts_from_the_call_not_from_the_first_byte(
        drip_server: _DripServer, make_api: Callable[..., HttpTelegramBotApi]) -> None:
    """Primeiro byte lento: o servidor espera 1,0 s antes do primeiro byte (dentro do tempo de leitura
    de 1,5 s) e DEPOIS goteja. O prazo conta do início da chamada: ela devolve o controle até
    margem + 0,3 s. Um prazo contado a partir do primeiro byte terminaria em ~2,5 s."""
    api = make_api(_local_token(), drip_server.base_url)
    drip_server.late_seconds = SLOW_FIRST_BYTE

    timed = _dripped_get_updates(api, drip_server, "late-drip")

    _assert_ends_within_the_margin(timed, "primeiro byte depois de 1,0 s")
    assert drip_server.methods == ["getUpdates"]
    _assert_client_closed_the_connection(drip_server, timed, "primeiro byte depois de 1,0 s")


def test_the_deadline_includes_a_slow_connection(drip_server: _DripServer,
                                                 make_api: Callable[..., HttpTelegramBotApi],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Conexão lenta: a conexão do cliente ao servidor leva 0,45 s a mais (dentro do tempo de
    conexão de 0,5 s) e a resposta goteja. O prazo inclui a conexão: a chamada devolve o controle até
    margem + 0,3 s. Um prazo contado depois da conexão terminaria em ~1,95 s.

    No loopback a conexão é instantânea; por isso o atraso é simulado no socket: o
    `socket.create_connection` (que o `SyncBackend` do `httpcore2` usa) dorme antes de conectar,
    só quando o destino é a porta deste servidor."""
    api = make_api(_local_token(), drip_server.base_url)
    original = socket.create_connection

    def slow_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == drip_server.port:
            time.sleep(SLOW_PHASE)
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", slow_create_connection)

    timed = _dripped_get_updates(api, drip_server, "drip-length")

    _assert_ends_within_the_margin(timed, "conexão lenta")
    assert drip_server.methods == ["getUpdates"]
    _assert_client_closed_the_connection(drip_server, timed, "conexão lenta")


def test_the_deadline_includes_a_slow_write(drip_server: _DripServer,
                                            make_api: Callable[..., HttpTelegramBotApi],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Primeiro envio lento: o primeiro envio do cliente leva 0,45 s a mais (dentro do tempo de escrita de
    0,5 s) e a resposta goteja. O prazo inclui a escrita: a chamada devolve o controle até
    margem + 0,3 s. Um prazo contado depois da escrita terminaria em ~1,95 s.

    No loopback a escrita é instantânea; por isso o atraso é simulado no socket: `send` e `sendall`
    de `socket.socket` dormem no primeiro envio de um socket cujo par é a porta deste servidor (os
    sockets do servidor têm como par a porta efêmera do cliente e não são afetados)."""
    api = make_api(_local_token(), drip_server.base_url)
    delayed = threading.Event()
    lock = threading.Lock()

    def delay_first_client_send(sock: socket.socket) -> None:
        try:
            peer = sock.getpeername()
        except OSError:
            return
        if not (isinstance(peer, tuple) and len(peer) >= 2 and peer[1] == drip_server.port):
            return
        with lock:
            if delayed.is_set():
                return
            delayed.set()
        time.sleep(SLOW_PHASE)

    original_send = socket.socket.send
    original_sendall = socket.socket.sendall

    def slow_send(self: socket.socket, data: Any, *args: Any) -> int:
        delay_first_client_send(self)
        return original_send(self, data, *args)

    def slow_sendall(self: socket.socket, data: Any, *args: Any) -> None:
        delay_first_client_send(self)
        return original_sendall(self, data, *args)

    monkeypatch.setattr(socket.socket, "send", slow_send)
    monkeypatch.setattr(socket.socket, "sendall", slow_sendall)

    timed = _dripped_get_updates(api, drip_server, "drip-length")

    assert delayed.is_set(), "o envio do cliente não passou pelo atraso simulado"
    _assert_ends_within_the_margin(timed, "escrita lenta")
    assert drip_server.methods == ["getUpdates"]
    _assert_client_closed_the_connection(drip_server, timed, "escrita lenta")


# ---------------------------------------------------------------------------------------------
# Proteções do prazo (passam com e sem o defeito)
# ---------------------------------------------------------------------------------------------


def test_the_same_object_polls_again_after_the_deadline(drip_server: _DripServer,
                                                        make_api: Callable[..., HttpTelegramBotApi]
                                                        ) -> None:
    """O prazo não é o `abort()` permanente: depois de um `getUpdates` gotejado terminar com
    `TelegramUnavailable`, o MESMO objeto faz um `getUpdates` normal contra o mesmo servidor e recebe
    os updates minimizados."""
    api = make_api(_local_token(), drip_server.base_url)

    first = _dripped_get_updates(api, drip_server, "drip-length")
    assert type(first.error) is TelegramUnavailable, repr(first.error)

    drip_server.mode = "normal"
    second = _timed(lambda: api.get_updates(offset=810999001))
    assert not second.stuck, "o segundo get_updates ficou preso"
    assert second.error is None, repr(second.error)
    assert second.result == [EXPECTED_UPDATE]
    assert drip_server.methods == ["getUpdates", "getUpdates"]


def test_a_legitimate_answer_within_the_margin_is_returned(drip_server: _DripServer,
                                                           make_api: Callable[...,
                                                                              HttpTelegramBotApi]
                                                           ) -> None:
    """O prazo não corta consulta legítima: o servidor espera `poll_timeout_seconds` (1 s, como o
    long poll do Telegram) e responde de uma vez; a chamada devolve os updates, sem exceção."""
    api = make_api(_local_token(), drip_server.base_url)
    drip_server.mode = "late"
    drip_server.late_seconds = float(POLL_TIMEOUT_SECONDS)

    timed = _timed(lambda: api.get_updates(offset=None))

    assert not timed.stuck, "get_updates ficou preso"
    assert timed.error is None, repr(timed.error)
    assert timed.result == [EXPECTED_UPDATE]
    assert timed.elapsed is not None and timed.elapsed >= POLL_TIMEOUT_SECONDS - 0.1
    assert drip_server.methods == ["getUpdates"]


def test_two_legitimate_polls_in_a_row_on_the_same_object(drip_server: _DripServer,
                                                          make_api: Callable[...,
                                                                             HttpTelegramBotApi]
                                                          ) -> None:
    """O prazo de uma consulta que termina bem não sobra para a seguinte. Duas consultas
    legítimas seguidas no MESMO objeto (o servidor espera 1 s em cada uma e responde de uma vez; a
    segunda começa logo depois da primeira): as duas devolvem os updates sem exceção, e a segunda
    dura pelo menos ~0,9 s (um vigia esquecido da primeira dispararia ~0,5 s depois do início da
    segunda e a mataria). Depois de `close()`, nenhuma thread nova criada pelo adaptador continua
    viva (diferença de `threading.enumerate()`, sem depender de nome)."""
    before = set(threading.enumerate())
    api = make_api(_local_token(), drip_server.base_url)
    drip_server.mode = "late"
    drip_server.late_seconds = float(POLL_TIMEOUT_SECONDS)

    first = _timed(lambda: api.get_updates(offset=None))
    second = _timed(lambda: api.get_updates(offset=810999001))

    for which, timed in (("primeira", first), ("segunda", second)):
        assert not timed.stuck, f"a {which} consulta ficou presa"
        assert timed.error is None, f"{which} consulta: {timed.error!r}"
        assert timed.result == [EXPECTED_UPDATE]
    assert second.elapsed is not None and second.elapsed >= POLL_TIMEOUT_SECONDS - 0.1, (
        f"a segunda consulta durou {second.elapsed:.2f} s; o servidor esperou "
        f"{POLL_TIMEOUT_SECONDS} s")
    assert drip_server.methods == ["getUpdates", "getUpdates"]

    api.close()
    ours = {first.thread, second.thread, *drip_server.threads()}

    def leftover() -> list[threading.Thread]:
        return [t for t in threading.enumerate()
                if t not in before and t not in ours and t.is_alive()]

    assert _wait_until(lambda: not leftover(), 2.0), (
        f"{len(leftover())} thread(s) criada(s) pelo adaptador continuam vivas depois de close()")


def test_the_deadline_failure_never_carries_the_token(drip_server: _DripServer,
                                                      make_api: Callable[..., HttpTelegramBotApi],
                                                      capfd: pytest.CaptureFixture[str],
                                                      caplog: pytest.LogCaptureFixture) -> None:
    """a falha do prazo (mensagem, `repr`, `args`, traceback e cadeia) não contém o token, a
    parte secreta dele nem o caminho `/bot<token>/`; o `repr` do adaptador também não. Nem o que
    saiu em stdout, stderr ou nos logs durante a chamada (o vigia do prazo não imprime o pedido nem
    a URL)."""
    caplog.set_level(logging.DEBUG)
    token = _local_token()
    api = make_api(token, drip_server.base_url)

    timed = _dripped_get_updates(api, drip_server, "drip-length")
    api.close()

    assert isinstance(timed.error, TelegramError), repr(timed.error)
    texts = _exception_texts(timed.error) + [repr(api), str(api)]
    # Só a contagem entra na asserção: a introspecção do pytest mostraria os textos (com o token).
    assert _leaks(texts, token) == 0, "o token apareceu na exceção ou no repr do adaptador"
    captured = capfd.readouterr()
    logged = [caplog.text] + [record.getMessage() for record in caplog.records]
    assert _leaks([captured.out, captured.err], token) == 0, "o token apareceu em stdout ou stderr"
    assert _leaks(logged, token) == 0, "o token apareceu nos logs"


def test_abort_during_a_dripping_get_updates_is_still_aborted(
        drip_server: _DripServer, make_api: Callable[..., HttpTelegramBotApi]) -> None:
    """No sentido inverso: o prazo não substitui o `abort()`. Com o `getUpdates` gotejando,
    um `abort()` de outra thread 0,5 s depois do início (bem antes do prazo) faz a chamada sair com
    `Aborted` em até 0,5 s, e a chamada seguinte do mesmo objeto também sai com `Aborted`, sem tocar
    a rede."""
    api = make_api(_local_token(), drip_server.base_url)
    drip_server.mode = "drip-length"
    waiting = _Background(lambda: api.get_updates(offset=None))
    assert _wait_until(lambda: drip_server.methods == ["getUpdates"], 1.0), (
        "o pedido não chegou ao servidor")
    time.sleep(max(0.0, waiting.started + ABORT_AT - monotonic()))

    asked = monotonic()
    api.abort()
    assert waiting.join(), "getUpdates continuou preso depois de abort()"
    assert type(waiting.error) is Aborted, repr(waiting.error)
    assert waiting.ended is not None and waiting.ended - asked <= ABORT_SECONDS

    again = _timed(lambda: api.get_updates(offset=None))
    assert not again.stuck
    assert type(again.error) is Aborted, repr(again.error)
    assert drip_server.methods == ["getUpdates"]
