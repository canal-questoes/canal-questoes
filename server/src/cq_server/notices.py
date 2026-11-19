"""Avisos de mudança de configuração.

- `notify_config_change(conn, topic, key)`: `pg_notify('cq_config', <json {topic, key}>)` **na
  transação do chamador**. O PostgreSQL só entrega o aviso no COMMIT; no ROLLBACK, nada chega.
  As validações vêm antes do SQL: um `ValueError` não deixa erro no banco.
- `NoticeListener(conninfo)`: gerenciador de contexto com uma conexão dedicada, em autocommit (sem
  ele o `LISTEN` só valeria no commit); o `LISTEN` já está ativo quando o `__enter__` retorna.
  `wait(timeout)` bloqueia até o primeiro aviso ou o timeout e então devolve todos os avisos já
  recebidos, sem bloquear de novo.

O aviso só **acorda** quem escuta: a fonte de verdade continua sendo o banco.
Um aviso com payload fora do formato (ex.: um `NOTIFY` manual) é ignorado.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import psycopg

from cq_server import db

CHANNEL = "cq_config"

# O payload do NOTIFY precisa ter menos de 8000 bytes.
MAX_PAYLOAD_BYTES = 7999


@dataclass(frozen=True)
class ConfigNotice:
    topic: str
    key: str


def _payload(topic: str, key: str) -> str:
    for name, value in (("topic", topic), ("key", key)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} deve ser um texto não vazio")
        if "\x00" in value:
            raise ValueError(f"{name} não pode conter o caractere NUL")
    payload = json.dumps({"topic": topic, "key": key}, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("aviso grande demais para o NOTIFY")
    return payload


def notify_config_change(conn: psycopg.Connection, topic: str, key: str) -> None:
    """Agenda o aviso `(topic, key)` na transação de `conn` (entregue no COMMIT)."""
    payload = _payload(topic, key)
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))


def _parse(payload: str) -> ConfigNotice | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"topic", "key"}:
        return None
    topic, key = data["topic"], data["key"]
    if not isinstance(topic, str) or not isinstance(key, str):
        return None
    return ConfigNotice(topic=topic, key=key)


class NoticeListener:
    """Ouvinte dos avisos de configuração, numa conexão dedicada (ver o docstring do módulo)."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._conn: psycopg.Connection | None = None

    def __enter__(self) -> NoticeListener:
        conn = db.connect(self._conninfo, autocommit=True)
        try:
            conn.execute(f"LISTEN {CHANNEL}")
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Fecha a conexão do ouvinte (idempotente)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def wait(self, timeout: float) -> list[ConfigNotice]:
        """Espera até `timeout` segundos pelo primeiro aviso e devolve todos os já recebidos."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("NoticeListener fora do bloco with")
        received: list[ConfigNotice] = []
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while not received:
            remaining = max(deadline - time.monotonic(), 0.0)
            for notify in conn.notifies(timeout=remaining, stop_after=1):
                notice = _parse(notify.payload)
                if notice is not None:
                    received.append(notice)
            if time.monotonic() >= deadline:
                break
        # O que já chegou sai junto, sem bloquear de novo (timeout=0 só consulta uma vez).
        for notify in conn.notifies(timeout=0):
            notice = _parse(notify.payload)
            if notice is not None:
                received.append(notice)
        return received
