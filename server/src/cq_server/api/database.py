"""Conexões da API com o banco, sob demanda (`create_app` sobe sem banco).

- Nenhuma conexão é aberta na criação do app nem na subida do lifespan: a primeira operação conecta,
  com o `connect_timeout` de 3 s de `cq_server.db`, e um banco inalcançável vira erro dessa
  operação (500 `internal`), não da subida.
- Enquanto o lifespan está ativo (`open` … `close`), as conexões que terminam **ociosas** (sem
  transação aberta) ficam guardadas para a próxima operação, até `max_idle`; a saída do lifespan as
  fecha. Uma conexão com transação pendente, quebrada ou usada numa operação que levantou exceção é
  fechada, nunca reaproveitada.
- As conexões são `autocommit`: cada operação abre a própria transação (`with conn.transaction()`),
  que termina em COMMIT, ou em ROLLBACK só quando algo falha. Devolver uma conexão ao estoque não
  emite ROLLBACK (o conflito de cadastro não pode terminar em ROLLBACK).
- Uma conexão parada há mais de `recheck_after` segundos é conferida (`SELECT 1`) antes do reuso;
  se o servidor a encerrou, ela é descartada e outra é aberta.
- Cada conexão é usada por uma thread de cada vez; o estoque é protegido por um lock.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

import psycopg
from psycopg.pq import TransactionStatus

from cq_server import db


class Database:
    def __init__(self, conninfo: str, *, max_idle: int = 8, recheck_after: float = 30.0) -> None:
        self._conninfo = conninfo
        self._max_idle = max_idle
        self._recheck_after = recheck_after
        self._idle: list[tuple[psycopg.Connection, float]] = []
        self._lock = threading.Lock()
        self._pooling = False

    def open(self) -> None:
        """Passa a guardar as conexões ociosas (subida do lifespan)."""
        with self._lock:
            self._pooling = True

    def close(self) -> None:
        """Fecha as conexões guardadas e para de guardar (saída do lifespan). Idempotente."""
        with self._lock:
            self._pooling = False
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            _close_quietly(conn)

    @contextlib.contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        conn = self._acquire()
        try:
            yield conn
        except BaseException:
            _close_quietly(conn)
            raise
        self._release(conn)

    def _acquire(self) -> psycopg.Connection:
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return db.connect(self._conninfo, autocommit=True)
            conn, since = item
            if conn.closed or conn.broken:
                _close_quietly(conn)
                continue
            if time.monotonic() - since > self._recheck_after:
                try:
                    conn.execute("SELECT 1")
                except psycopg.Error:
                    _close_quietly(conn)
                    continue
            return conn

    def _release(self, conn: psycopg.Connection) -> None:
        reusable = (not conn.closed and not conn.broken
                    and conn.info.transaction_status == TransactionStatus.IDLE)
        if reusable:
            with self._lock:
                if self._pooling and len(self._idle) < self._max_idle:
                    self._idle.append((conn, time.monotonic()))
                    return
        _close_quietly(conn)


def _close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - só limpeza; a conexão já pode estar quebrada
        pass
