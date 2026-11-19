"""Logs estruturados em JSON."""

from __future__ import annotations

import json
import logging
import pathlib
import re
from collections.abc import Iterator

import pytest

from cq_server.logs import JsonFormatter, configure_logging
from tests.helpers.canary import OLIMPIO, URSULA, ZEFIRO, find_canary

HARNESS_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")
WATCHED_LOGGERS = ("cq_server", "cq_server.api", "cq_server.worker", "uvicorn", "uvicorn.error",
                   "uvicorn.access", "fastapi", *HARNESS_LOGGERS)


def _snapshot() -> dict[str, tuple[int, list[logging.Handler]]]:
    snap = {"": (logging.getLogger().level, list(logging.getLogger().handlers))}
    for name in WATCHED_LOGGERS:
        logger = logging.getLogger(name)
        snap[name] = (logger.level, list(logger.handlers))
    return snap


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Guarda e restaura os handlers e o nível da raiz (e dos loggers vigiados)."""
    saved = _snapshot()
    yield
    for name, (level, handlers) in saved.items():
        logger = logging.getLogger(name or None)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:  # pragma: no cover - só limpeza
                    pass
        logger.handlers[:] = handlers
        logger.setLevel(level)


class _ListHandler(logging.Handler):
    """Guarda o texto formatado de cada registro; um erro do formatter sobe para o teste."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def json_logger() -> Iterator[tuple[logging.Logger, _ListHandler]]:
    """Um logger `cq_server.*` isolado, com o `JsonFormatter` real num handler que não engole erros."""
    logger = logging.getLogger("cq_server.test_unit_logs")
    handler = _ListHandler()
    handler.setFormatter(JsonFormatter())
    saved = (logger.level, logger.propagate)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


def _single_json_line(text: str) -> dict:
    assert "\n" not in text and "\r" not in text, f"o registro não é uma linha só: {text!r}"
    record = json.loads(text)
    assert isinstance(record, dict)
    return record


# ---------------------------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------------------------


def test_json_formatter_line_with_extra(json_logger) -> None:
    """uma linha JSON ⊇ `{level: "INFO", logger, event}` e os campos do `extra`,
    com os tipos JSON preservados."""
    logger, handler = json_logger
    extra = {"event": "request", "method": "GET", "route": None, "status": 200, "id": 7,
             "state": "done"}
    logger.info("mensagem ignorada quando há event", extra=extra)
    assert len(handler.lines) == 1
    record = _single_json_line(handler.lines[0])
    expected = {"level": "INFO", "logger": "cq_server.test_unit_logs", **extra}
    assert expected.items() <= record.items(), record
    assert type(record["status"]) is int and type(record["id"]) is int


def test_json_formatter_event_defaults_to_formatted_message(json_logger) -> None:
    """sem `extra` `event`, o `event` é a mensagem formatada; `level` em
    maiúsculas para cada nível."""
    logger, handler = json_logger
    logger.info("olá %s, %d itens", "mundo", 3)
    logger.debug("depuração %s", "ligada")
    logger.warning("aviso simples")
    logger.error("erro sem exceção", extra={"id": 3})
    records = [_single_json_line(line) for line in handler.lines]
    assert len(records) == 4
    assert {"level": "INFO", "logger": "cq_server.test_unit_logs",
            "event": "olá mundo, 3 itens"}.items() <= records[0].items()
    assert {"level": "DEBUG", "event": "depuração ligada"}.items() <= records[1].items()
    assert {"level": "WARNING", "event": "aviso simples"}.items() <= records[2].items()
    assert {"level": "ERROR", "event": "erro sem exceção", "id": 3}.items() <= records[3].items()


def test_json_formatter_keeps_multiline_message_in_one_line(json_logger) -> None:
    """uma linha por registro, mesmo com quebra de linha e Unicode na mensagem."""
    logger, handler = json_logger
    message = "primeira linha\nsegunda linha\r\nação ✓"
    logger.info(message)
    assert len(handler.lines) == 1
    record = _single_json_line(handler.lines[0])
    assert {"level": "INFO", "event": message}.items() <= record.items()


def _raise_canary() -> None:
    raise RuntimeError(f"{ZEFIRO.name} {ZEFIRO.ra} {ZEFIRO.email}")


_RAISE_LINE = _raise_canary.__code__.co_firstlineno + 1


def _call_raise_canary() -> None:
    _raise_canary()


def _raise_chained() -> None:
    try:
        {}[OLIMPIO.email]
    except KeyError as exc:
        raise ValueError(f"{URSULA.name} {URSULA.ra}") from exc


def _call_raise_chained() -> None:
    _raise_chained()


def _innermost(exc: BaseException) -> tuple[str, int]:
    tb = exc.__traceback__
    assert tb is not None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code.co_filename, tb.tb_lineno


@pytest.mark.parametrize(
    ("raiser", "exc_type"),
    [(_call_raise_canary, "RuntimeError"), (_call_raise_chained, "ValueError")],
    ids=["simples", "encadeada"],
)
def test_exception_record_has_only_type_and_where(json_logger, raiser, exc_type: str) -> None:
    """`logger.exception("mensagem qualquer")` sobre uma exceção com nome,
    RA e e-mail canário → `event: exception` (sobrepondo a mensagem e o `extra`), `exc_type` e
    `where` = arquivo:linha do quadro mais interno; sem mensagem, sem `Traceback` e sem canário."""
    logger, handler = json_logger
    try:
        raiser()
    except Exception as exc:  # noqa: BLE001 - o teste captura de propósito
        caught = exc
        logger.exception("mensagem qualquer", extra={"event": "evento.sobreposto"})
    assert len(handler.lines) == 1
    line = handler.lines[0]
    record = _single_json_line(line)
    assert {"level": "ERROR", "logger": "cq_server.test_unit_logs", "event": "exception",
            "exc_type": exc_type}.items() <= record.items(), record
    filename, lineno = _innermost(caught)
    if exc_type == "RuntimeError":
        assert lineno == _RAISE_LINE  # o quadro mais interno é o do `raise`, não o do teste
    assert re.fullmatch(r"^.+\.py:\d+$", record["where"]), record["where"]
    assert record["where"].endswith(f"{pathlib.Path(filename).name}:{lineno}"), record["where"]
    assert "mensagem qualquer" not in line
    assert "evento.sobreposto" not in line
    assert "Traceback" not in line
    assert "direct cause" not in line
    assert str(caught) not in line
    assert find_canary(line) == []


def test_exc_info_true_behaves_like_exception(json_logger) -> None:
    """qualquer registro com `exc_info` (não só `logger.exception`) sai como
    `event: exception`, sem a mensagem da exceção."""
    logger, handler = json_logger
    try:
        _call_raise_canary()
    except RuntimeError:
        logger.warning("falhou", exc_info=True)
    record = _single_json_line(handler.lines[0])
    assert {"level": "WARNING", "event": "exception", "exc_type": "RuntimeError"}.items() <= record.items()
    assert record["where"].endswith(f"{pathlib.Path(__file__).name}:{_RAISE_LINE}")
    assert find_canary(handler.lines[0]) == []
    assert "Traceback" not in handler.lines[0]
    assert "falhou" not in handler.lines[0]


# ---------------------------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------------------------


def _json_records(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def test_configure_logging_is_idempotent(capfd: pytest.CaptureFixture[str]) -> None:
    """`configure_logging("INFO")` duas vezes → **um** handler na raiz, com o
    `JsonFormatter`, no lugar dos anteriores; `httpx`, `httpx2`, `httpcore` e `httpcore2` em WARNING; um registro
    INFO de `cq_server` sai uma vez só, e DEBUG não sai."""
    root = logging.getLogger()
    stale = logging.NullHandler()
    root.addHandler(stale)
    for name in HARNESS_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    configure_logging("INFO")
    configure_logging("INFO")

    assert len(root.handlers) == 1, root.handlers
    assert stale not in root.handlers
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    for name in HARNESS_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name

    capfd.readouterr()
    app_logger = logging.getLogger("cq_server.test_configure")
    app_logger.info("configurado", extra={"event": "configurado.teste", "id": 1})
    app_logger.debug("nao deve sair", extra={"event": "depuracao.teste"})
    logging.getLogger("httpx2").info("GET http://127.0.0.1/v1/people?x=1")
    out, err = capfd.readouterr()
    records = _json_records(out + "\n" + err)
    matching = [r for r in records if r.get("event") == "configurado.teste"]
    assert len(matching) == 1, records
    assert {"level": "INFO", "logger": "cq_server.test_configure", "id": 1}.items() <= matching[0].items()
    assert not [r for r in records if r.get("event") == "depuracao.teste"]
    assert "127.0.0.1/v1/people" not in out + err


def test_configure_logging_honors_level(capfd: pytest.CaptureFixture[str]) -> None:
    """o nível pedido vale: com `WARNING`, um INFO de `cq_server` não sai e um
    WARNING sai (em JSON)."""
    configure_logging("WARNING")
    capfd.readouterr()
    app_logger = logging.getLogger("cq_server.test_configure")
    app_logger.info("informativo", extra={"event": "info.suprimido"})
    app_logger.warning("atenção", extra={"event": "aviso.emitido"})
    out, err = capfd.readouterr()
    records = _json_records(out + "\n" + err)
    assert not [r for r in records if r.get("event") == "info.suprimido"]
    assert [r["level"] for r in records if r.get("event") == "aviso.emitido"] == ["WARNING"]


# ---------------------------------------------------------------------------------------------
# create_app não configura o logging
# ---------------------------------------------------------------------------------------------


def test_create_app_does_not_touch_logging(unreachable_db_url: str) -> None:
    """`create_app`, o lifespan e uma requisição não alteram os handlers nem os
    níveis da raiz e dos loggers da aplicação, do uvicorn e do arnês."""
    from fastapi.testclient import TestClient

    from cq_server.api import create_app
    from cq_server.config import Settings

    before = _snapshot()
    app = create_app(Settings(database_url=unreachable_db_url, admin_token=None))
    assert _snapshot() == before
    with TestClient(app, raise_server_exceptions=False) as client:
        assert _snapshot() == before
        client.get("/health")
        assert _snapshot() == before
    assert _snapshot() == before
