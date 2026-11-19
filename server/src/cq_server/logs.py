"""Logs estruturados em JSON.

- Uma linha JSON por registro, com pelo menos `level` (maiúsculas), `logger` e `event`.
- `event` é o valor de `extra={"event": …}` ou, sem ele, a mensagem formatada. Os demais campos do
  `extra` entram no objeto (tipos JSON preservados).
- Com `exc_info`, o registro vira `event = "exception"` (sobrepondo o `extra` e a mensagem), com
  `exc_type = type(exc).__name__` e `where = <arquivo>:<linha>` do quadro mais interno do
  traceback. Nem a mensagem do registro, nem a da exceção, nem o traceback saem: eles podem
  carregar identidade ou segredos.
- A saída é só ASCII (`\\uXXXX` para o resto): o JSON continua o mesmo e nenhum console quebra.

`configure_logging` é chamado só pelo `cq-server` (`__main__`); `create_app` e `run_worker` não
mexem no logging.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

# Loggers de transporte HTTP: registram URLs completas em INFO.
QUIET_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")

# Atributos que todo `LogRecord` tem (não são `extra`), mais os que formatters e o uvicorn põem.
_RECORD_ATTRS = frozenset(logging.LogRecord("", logging.INFO, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName", "color_message",
}
_RESERVED = frozenset({"level", "logger", "event", "time", "exc_type", "where"})


def _json_default(value: Any) -> str:
    return str(value)


def _where(exc: BaseException) -> str | None:
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    path = pathlib.PurePath(tb.tb_frame.f_code.co_filename)
    short = "/".join(path.parts[-2:]) if len(path.parts) >= 2 else path.name
    return f"{short}:{tb.tb_lineno}"


class JsonFormatter(logging.Formatter):
    """Formata cada registro como uma linha JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _RECORD_ATTRS or key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value

        exc_info: Any = record.exc_info
        exc = exc_info if isinstance(exc_info, BaseException) else (
            exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None)
        if exc is not None:
            event: Any = "exception"
            payload["exc_type"] = type(exc).__name__
            payload["where"] = _where(exc)
        elif "event" in record.__dict__:
            event = record.__dict__["event"]
        else:
            event = record.getMessage()

        payload["level"] = record.levelname
        payload["logger"] = record.name
        payload["event"] = event
        payload["time"] = (datetime.fromtimestamp(record.created, tz=timezone.utc)
                           .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        return json.dumps(payload, ensure_ascii=True, default=_json_default)


def configure_logging(level: str = "INFO") -> None:
    """Configura o logging do processo (idempotente).

    Substitui os handlers da raiz por um só, na saída padrão, com o `JsonFormatter`; põe a raiz no
    nível pedido e os loggers de transporte HTTP (`httpx`, `httpx2`, `httpcore`, `httpcore2`) em
    WARNING.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - só limpeza
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
