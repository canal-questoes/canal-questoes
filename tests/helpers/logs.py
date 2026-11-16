"""Captura dos logs com o formatter **real** da aplicação.

- Um handler com `cq_server.logs.JsonFormatter` vai na raiz e nos loggers que não propagam.
- Os loggers `cq_server*` descem para DEBUG durante a captura.
- Os loggers do **arnês** de teste (`httpx`, `httpx2`, `httpcore`) sobem para WARNING durante a
  captura: eles registram URLs de pedidos feitos pelo próprio teste, que não são log do sistema.
- Tudo volta ao estado anterior no fim.

`capture_transport_logs()` é outra captura, que **não** mexe
em `capture_logs`: um handler próprio em DEBUG nos loggers do transporte HTTP (`httpx2` e
`httpcore2`, que o adaptador do Telegram usa), para provar que nenhum registro deles contém o token
do bot (a URL da Bot API contém o token). Veja o docstring da função.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

HARNESS_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")
TRANSPORT_LOGGERS = ("httpx2", "httpcore2")
SYSTEM_LOGGERS = ("cq_server", "cq_server.api", "cq_server.worker", "uvicorn", "uvicorn.error",
                  "uvicorn.access")


@dataclass
class Captured:
    stream: io.StringIO = field(default_factory=io.StringIO)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line.strip()]

    @property
    def records(self) -> list[dict]:
        out = []
        for line in self.lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"_raw": line})
        return out

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def with_event(self, event: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == event]


class _DedupHandler(logging.StreamHandler):
    """Evita registrar duas vezes o mesmo `LogRecord` (raiz + logger que não propaga)."""

    def __init__(self, stream: io.StringIO) -> None:
        super().__init__(stream)
        self._seen: set[int] = set()
        # Guarda as referências para que o `id` de um registro já visto não seja reutilizado.
        self._keep: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if id(record) in self._seen:
            return
        self._seen.add(id(record))
        self._keep.append(record)
        super().emit(record)


@contextlib.contextmanager
def capture_logs() -> Iterator[Captured]:
    from cq_server.logs import JsonFormatter

    captured = Captured()
    handler = _DedupHandler(captured.stream)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    attached: list[logging.Logger] = [root]
    saved_levels: dict[str, int] = {}

    for name in SYSTEM_LOGGERS:
        logger = logging.getLogger(name)
        saved_levels[name] = logger.level
        if name.startswith("cq_server"):
            logger.setLevel(logging.DEBUG)
        if not logger.propagate:
            attached.append(logger)
    for name in HARNESS_LOGGERS:
        logger = logging.getLogger(name)
        saved_levels[name] = logger.level
        logger.setLevel(logging.WARNING)
    saved_root = root.level
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

    # Avisos de `warnings.warn` (inclusive os do Pydantic) viram registros do logger `py.warnings`,
    # que propaga até a raiz e entra na captura.
    logging.captureWarnings(True)
    for logger in attached:
        logger.addHandler(handler)
    try:
        yield captured
    finally:
        for logger in attached:
            logger.removeHandler(handler)
        logging.captureWarnings(False)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        root.setLevel(saved_root)


class _TransportFormatter(logging.Formatter):
    """Uma linha JSON por registro, com tudo o que um registro pode carregar: logger, nível,
    mensagem formatada (com os argumentos), argumentos crus, exceção e pilha."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception as exc:  # noqa: BLE001 - registro malformado ainda é capturado
            message = f"<mensagem não formatável: {type(exc).__name__}> {record.msg!r}"
        entry: dict = {"logger": record.name, "level": record.levelname, "message": message,
                       "args": repr(record.args)}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        extra = {k: repr(v) for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        if extra:
            entry["extra"] = extra
        return json.dumps(entry, ensure_ascii=False)


_STANDARD_ATTRS = frozenset(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None).__dict__) | {
    "message", "asctime", "taskName"}


@contextlib.contextmanager
def capture_transport_logs() -> Iterator[Captured]:
    """Captura, em DEBUG, tudo o que os loggers `httpx2` e `httpcore2` (e os filhos) registram.

    - Um handler próprio vai nos dois loggers, que descem para DEBUG durante a captura; cada
      registro vira uma linha JSON com a mensagem formatada, os argumentos crus, a exceção, a pilha
      e os campos extras (`Captured.text` é o texto a varrer).
    - Durante a captura, os dois loggers **não propagam**: os registros do transporte não chegam à
      raiz nem a um `capture_logs` aberto ao mesmo tempo.
    - Durante a captura, o nível dos dois loggers fica **preso** em DEBUG: um `capture_logs` aberto
      dentro desta captura sobe `httpx2` e `httpcore2` para WARNING (`HARNESS_LOGGERS`), o que
      esvaziaria a captura sem aviso; com o nível preso, a ordem das duas capturas não importa.
    - Nível, propagação e handlers voltam ao estado anterior no fim.
    """
    captured = Captured()
    handler = _DedupHandler(captured.stream)
    handler.setFormatter(_TransportFormatter())
    handler.setLevel(logging.DEBUG)
    saved: list[tuple[logging.Logger, int, bool, Any]] = []
    for name in TRANSPORT_LOGGERS:
        logger = logging.getLogger(name)
        # `setLevel` próprio da instância, se uma captura de fora já prendeu o nível.
        saved.append((logger, logger.level, logger.propagate, vars(logger).get("setLevel", _UNSET)))
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        logger.setLevel = _keep_debug  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        for logger, level, propagate, own_set_level in reversed(saved):
            logger.removeHandler(handler)
            if own_set_level is _UNSET:
                vars(logger).pop("setLevel", None)
            else:
                logger.setLevel = own_set_level  # type: ignore[method-assign]
            logger.setLevel(level)
            logger.propagate = propagate


_UNSET: Any = object()


def _keep_debug(level: int | str) -> None:
    """`setLevel` dos loggers do transporte durante `capture_transport_logs`: ignora a troca (o
    nível fica em DEBUG até o fim da captura)."""
