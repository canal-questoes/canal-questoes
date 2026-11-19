"""Apoio comum às filas com posse (`tasks` e `outbox_messages`).

Uso interno de `cq_server.tasks` e `cq_server.outbox`:

- `LazyConnection`: a conexão própria de uma instância de fila, aberta sob demanda com
  `connect_timeout` de 3 s (`cq_server.db`), em autocommit (cada operação abre a sua transação com
  `conn.transaction()`), e reaberta se tiver caído. Cada instância é usada por **uma** thread
  (sem compartilhamento), então não há lock aqui.
- `backoff_seconds`: a espera da falha temporária, `min(base · 2^(attempts−1), máximo)`.
- `storable_text`: troca o NUL, que o PostgreSQL não aceita em `text` nem em `jsonb`, e os
  substitutos UTF-16 isolados, que não têm UTF-8, para que o registro de uma falha nunca provoque
  erro no banco nem deixe o item em posse.
- `has_unstorable`: diz se um texto tem um desses caracteres (a validação do `enqueue` o recusa).
- `utc`: horário vindo do banco, em UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import psycopg

from cq_server import db
from cq_server.config import Settings

# O texto fixo de `last_error` quando a posse vence sem conclusão.
LEASE_EXHAUSTED_ERROR = "posse vencida sem conclusão"

# 2^63 já passa de qualquer teto de espera finito que se configure; acima disso, o teto vale.
_MAX_BACKOFF_EXPONENT = 63

# Teto técnico de qualquer espera (10 anos): só evita o estouro de `datetime` com valores absurdos
# (um `retry_after` enorme de um adaptador, um `CQ_BACKOFF_MAX_SECONDS` gigante), que faria o
# registro da falha nunca ser gravado.
MAX_WAIT_SECONDS = 10 * 365 * 24 * 3600.0

# Caracteres que o banco não guarda (o mesmo critério de `people` e `tokens`): U+0000, que `text` e
# `jsonb` recusam, e os substitutos UTF-16 (U+D800 a U+DFFF), que um `str` do Python pode carregar
# soltos (ex.: `"\ud800"`, ou um `reason` montado com `surrogateescape`) e que não têm UTF-8: o
# psycopg levanta `UnicodeEncodeError` ao mandar o texto, e o `jsonb` recusa o escape `\ud800`.
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


class LazyConnection:
    """Conexão própria de uma fila: aberta no primeiro uso, reaberta se caiu, fechada em `close`."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._conn: psycopg.Connection | None = None

    def get(self) -> psycopg.Connection:
        conn = self._conn
        if conn is not None and (conn.closed or conn.broken):
            self._discard()
            conn = None
        if conn is None:
            conn = db.connect(self._conninfo, autocommit=True)
            self._conn = conn
        return conn

    def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - conexão já quebrada; só a soltamos
                pass

    def close(self) -> None:
        """Fecha a conexão, se houver (idempotente)."""
        self._discard()


def backoff_seconds(settings: Settings, attempts: int) -> float:
    """Espera da falha temporária: `min(backoff_base · 2^(attempts−1), backoff_max)`."""
    exponent = max(attempts - 1, 0)
    if exponent > _MAX_BACKOFF_EXPONENT:
        wait = float(settings.backoff_max_seconds)
    else:
        wait = float(min(settings.backoff_base_seconds * (2 ** exponent), settings.backoff_max_seconds))
    return min(wait, MAX_WAIT_SECONDS)


def storable_text(text: str) -> str:
    """O texto sem os caracteres que o banco não guarda: U+0000 e cada substituto UTF-16 isolado
    (U+D800 a U+DFFF) viram U+FFFD."""
    return _UNSTORABLE.sub("\ufffd", text)


def has_unstorable(text: str) -> bool:
    """O texto tem U+0000 ou um substituto UTF-16 isolado (U+D800 a U+DFFF)."""
    return _UNSTORABLE.search(text) is not None


def utc(value: datetime | None) -> datetime | None:
    """Horário do banco convertido para UTC (o fuso da sessão não importa)."""
    return None if value is None else value.astimezone(timezone.utc)
