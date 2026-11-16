"""Transporte HTTP abortável e vigia do prazo total, comuns aos adaptadores HTTP (o do motor de questões
e o do Telegram).

Comum ao adaptador do Telegram e ao do motor de questões: `_TrackedStream`, `_CallDeadline`,
`_AbortableBackend`, `_ResponseStream` e `_AbortableTransport`. O nome da thread do vigia e as frases
das exceções internas do transporte são parâmetros; os padrões são os do Telegram (o adaptador do
Telegram define os seus e os passa explicitamente). O motor de questões usa os seus
(`"cq-engine-deadline"` e frases próprias, sem URL nem corpo).

As exceções que este módulo levanta são do `httpcore2` e ficam dentro do adaptador: quem chama as
classifica pelo tipo e levanta a exceção da sua porta, sem copiar nada delas. Nada aqui registra em
log.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

import httpcore2  # transporte e backend abortável
import httpx2  # pedido e resposta

# Padrões do Telegram (os mesmos valores de `adapters/telegram.py`, que os passa explicitamente).
DEFAULT_DEADLINE_THREAD_NAME = "cq-telegram-poll-deadline"
DEFAULT_ABORTED_MESSAGE = "chamada à Bot API interrompida por abort()"
DEFAULT_DEADLINE_MESSAGE = "prazo total da chamada à Bot API vencido"


class _TrackedStream(httpcore2.NetworkStream):
    """Um `NetworkStream` do `SyncBackend`, registrado no backend enquanto está aberto."""

    def __init__(self, inner: httpcore2.NetworkStream, backend: _AbortableBackend) -> None:
        self._inner = inner
        self._backend = backend

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._backend._attach(self)
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._backend._attach(self)
        self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._backend._forget(self)
        self._inner.close()

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore2.NetworkStream:
        # O stream TLS embrulha o mesmo socket: durante o aperto de mão este objeto continua
        # registrado (o `abort()` derruba o socket de baixo); depois, o registro passa ao novo.
        try:
            tls = self._inner.start_tls(ssl_context, server_hostname=server_hostname,
                                        timeout=timeout)
        finally:
            self._backend._forget(self)
        return self._backend._track(tls)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)

    def kill(self) -> None:
        """`shutdown(SHUT_RDWR)` + `close()` no socket: acorda um `recv` preso em outra thread
        (no Linux, só `close()` não acorda; no Windows, só `shutdown()` não acorda)."""
        sock = self._inner.get_extra_info("socket")
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
        try:
            self._inner.close()
        except (OSError, ValueError):
            pass


class _CallDeadline:
    """O prazo total de UMA chamada (no molde do vigia do prazo do SMTP).

    O vigia é um `threading.Timer` de nome fixo (`thread_name`), ligado no início da chamada (antes
    da fila do pool, da conexão e da escrita). Cada stream em que a chamada faz E/S se prende a este
    prazo (`attach`); no fim do prazo, o vigia marca `expired` e derruba (`kill()`) os streams
    presos, e a E/S seguinte da chamada é recusada. Não liga o `abort()` permanente do backend: a
    chamada seguinte do mesmo objeto abre conexão nova.

    `finish()` encerra a chamada para o vigia: depois dele, o vigia não derruba mais nada. A trava
    serializa o disparo com o `finish()`, e o `kill()` roda dentro dela; assim, depois que
    `finish()` volta, nenhum stream (que pode voltar ao pool e servir à chamada seguinte) é
    derrubado por este prazo. `stop()` faz `finish()`, cancela o vigia e o espera.
    """

    def __init__(self, seconds: float, *, thread_name: str = DEFAULT_DEADLINE_THREAD_NAME) -> None:
        self._lock = threading.Lock()
        self._streams: set[_TrackedStream] = set()
        self._finished = False
        self._expired = False
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.name = thread_name
        self._timer.daemon = True

    @property
    def expired(self) -> bool:
        return self._expired

    def start(self) -> None:
        self._timer.start()

    def attach(self, stream: _TrackedStream) -> bool:
        """Prende o stream a esta chamada; falso se o prazo já venceu (a E/S não deve seguir)."""
        with self._lock:
            if self._expired:
                return False
            if not self._finished:
                self._streams.add(stream)
            return True

    def _expire(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._expired = True
            streams = list(self._streams)
            self._streams.clear()
            for stream in streams:
                stream.kill()

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            self._streams.clear()

    def stop(self) -> None:
        self.finish()
        self._timer.cancel()
        if self._timer.is_alive() and self._timer is not threading.current_thread():
            self._timer.join()


class _AbortableBackend(httpcore2.NetworkBackend):
    """Backend de rede que embrulha o `SyncBackend` do `httpcore2` e registra cada stream aberto
    (inclusive o do `start_tls`), para que `abort()` os derrube de outra thread.

    Depois de `abort()`, toda conexão nova é recusada (`ConnectError`), e quem chama classifica a
    falha pela sua porta (no Telegram, `Aborted`).

    Enquanto uma chamada com prazo total corre numa thread (`set_deadline`), cada stream aberto ou
    usado nela se prende a esse prazo (no `httpcore2` síncrono, toda a E/S de uma chamada acontece
    na thread de quem chama); depois do prazo vencido, a E/S dela é recusada.
    """

    def __init__(self, *, aborted_message: str = DEFAULT_ABORTED_MESSAGE,
                 deadline_message: str = DEFAULT_DEADLINE_MESSAGE) -> None:
        self._inner = httpcore2.SyncBackend()
        self._lock = threading.Lock()
        self._streams: set[_TrackedStream] = set()
        self._aborted = False
        self._local = threading.local()
        self._aborted_message = aborted_message
        self._deadline_message = deadline_message

    @property
    def aborted(self) -> bool:
        return self._aborted

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None,
                    socket_options: Any = None) -> httpcore2.NetworkStream:
        if self._aborted:
            raise httpcore2.ConnectError(self._aborted_message)
        stream = self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                         socket_options=socket_options)
        return self._track(stream)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)

    def _track(self, stream: httpcore2.NetworkStream) -> _TrackedStream:
        tracked = _TrackedStream(stream, self)
        with self._lock:
            aborted = self._aborted
            if not aborted:
                self._streams.add(tracked)
        if aborted:
            # `abort()` chegou durante a conexão: ela não chega a ser usada.
            tracked.kill()
            raise httpcore2.ConnectError(self._aborted_message)
        deadline = self._current_deadline()
        if deadline is not None and not deadline.attach(tracked):
            # O prazo total venceu durante a conexão: ela também não chega a ser usada.
            self._forget(tracked)
            tracked.kill()
            raise httpcore2.ConnectError(self._deadline_message)
        return tracked

    def _current_deadline(self) -> _CallDeadline | None:
        return getattr(self._local, "deadline", None)

    def _attach(self, stream: _TrackedStream) -> None:
        """Prende o stream ao prazo da chamada desta thread (se houver); recusa a E/S depois dele.

        A recusa é um `ReadError` também quando a E/S recusada é uma escrita, de propósito: com um
        `WriteError`, o `httpcore2` ainda tentaria ler uma resposta depois da falha de escrita."""
        deadline = self._current_deadline()
        if deadline is not None and not deadline.attach(stream):
            raise httpcore2.ReadError(self._deadline_message)

    def set_deadline(self, deadline: _CallDeadline | None) -> None:
        """Liga (ou desliga, com `None`) o prazo total da chamada que corre nesta thread."""
        self._local.deadline = deadline

    def _forget(self, stream: _TrackedStream) -> None:
        with self._lock:
            self._streams.discard(stream)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            streams = list(self._streams)
            self._streams.clear()
        for stream in streams:
            stream.kill()


class _ResponseStream(httpx2.SyncByteStream):
    """O corpo da resposta do `httpcore2`, exposto ao `httpx2.Response`."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class _AbortableTransport(httpx2.BaseTransport):
    """Transporte do `httpx2` chamado direto (sem `httpx2.Client`), sobre um
    `httpcore2.ConnectionPool` de uma conexão com o backend abortável.

    `max_keepalive_connections=0` desliga o reúso (uma conexão nova por chamada; o motor de
    questões); `None` mantém o padrão do `httpcore2` (o Telegram). Não registra nada em log e não
    converte as exceções do `httpcore2`: quem chama as classifica.
    """

    def __init__(self, *, https: bool, max_keepalive_connections: int | None = None,
                 aborted_message: str = DEFAULT_ABORTED_MESSAGE,
                 deadline_message: str = DEFAULT_DEADLINE_MESSAGE) -> None:
        self._backend = _AbortableBackend(aborted_message=aborted_message,
                                          deadline_message=deadline_message)
        self._pool = httpcore2.ConnectionPool(
            ssl_context=httpx2.create_ssl_context() if https else None,
            max_connections=1,
            max_keepalive_connections=max_keepalive_connections,
            network_backend=self._backend,
        )

    @property
    def aborted(self) -> bool:
        return self._backend.aborted

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        core_request = httpcore2.Request(
            method=request.method,
            url=httpcore2.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                              port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = self._pool.handle_request(core_request)
        return httpx2.Response(status_code=response.status, headers=response.headers,
                               stream=_ResponseStream(response.stream),
                               extensions=response.extensions)

    def set_deadline(self, deadline: _CallDeadline | None) -> None:
        """Liga (ou desliga, com `None`) o prazo total da chamada que corre nesta thread."""
        self._backend.set_deadline(deadline)

    def abort(self) -> None:
        self._backend.abort()

    def close(self) -> None:
        self._backend.abort()
        self._pool.close()
