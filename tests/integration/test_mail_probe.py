"""O teste do servidor de e-mail sem envio, gravado pelo worker.


Com o SMTP falso (`tests/helpers/smtp.py`, fixture `smtp`, estrita: nenhum desvio de protocolo nem segredo
nas mensagens) e o banco de teste:
- `SmtpVerificationChannel.probe()` (a porta `MailServerProbe`): o falso que aceita conexão, TLS e AUTH → `"ok"`,
  **zero** mensagens capturadas, a sessão termina com `QUIT` e nunca manda `MAIL`; a falha classificada pela
  **etapa**: conexão recusada, `421` na saudação, `554` no `EHLO` e o prazo vencido no `EHLO` →
  `"connection"`; STARTTLS não anunciado no modo `starttls` e o certificado com o nome errado → `"tls"`; AUTH
  recusada e AUTH não anunciado → `"auth"`; o prazo é o `timeout_seconds` do `MailSettings`
  (`CQ_SMTP_TIMEOUT_SECONDS`);
- `record_probe` e `read_probe` (uma linha; `checked_at` do relógio injetado no laço);
- `MailProbeLoop`: `run_once` grava o resultado com a hora do `FakeClock`; com `probe=None` (e-mail não
  configurado) grava `not_configured` sem conexão; `run_forever(stop_event)` testa na partida e a cada
  intervalo (com `sleep` falso que anda o relógio) e para no evento; o laço não cria linha em `tasks`;
- `run_worker`: sem `mail_probe`, nada conecta ao falso nem escreve em `mail_probe`; com ele, a sonda roda na
  partida; a sonda presa não atrasa uma tarefa de envio enfileirada ao mesmo tempo;
- `build_mail_probe(env, settings, probe)`: padrão 21600, o valor válido no laço, e `0`, `-1`, `abc`,
  `1.5` → `ConfigError` sem o valor; a recusa da partida do worker com esses valores por subprocesso (código 2,
  um `config.invalid`, sem eco), no molde de `tests/unit/test_repo_policy.py` (l. 806-834);
- a partida real do worker (`python -m cq_server worker`) liga a sonda, que testa na partida:
  sem `CQ_SMTP_*` → a linha `not_configured`; com as `CQ_SMTP_*` apontando o SMTP falso → a
  linha `ok`, sem nenhuma mensagem capturada; o intervalo fica no padrão e o prazo da linha é explícito;
- nenhum usuário, senha, host ou resposta crua no banco, nos registros nem no stderr (`mail_secrets`).

**Vermelho no esqueleto:** `probe`, `record_probe`, `read_probe`, `run_once`, `run_forever` e `build_mail_probe`
lançam `NotImplementedError`; o `run_worker` ainda não usa `mail_probe`; a partida do worker
ainda não lê `CQ_MAIL_PROBE_INTERVAL_SECONDS` nem liga a sonda (a linha em `mail_probe` não aparece no prazo).

Escolhas deste teste ([I]):
- o convite enfileirado ao mesmo tempo é uma tarefa de teste cujo tratador manda um e-mail pelo
  adaptador real (`mail_channel.send`), com a sonda presa num dublê da porta: o que se mede é que o laço da sonda
  não ocupa o laço das tarefas (a montagem inteira do convite é coberta pelos testes do vínculo do aluno);
- o teto de tempo real dos casos com prazo é `timeout_seconds + 1 s` (o dos testes do adaptador é + 0,5 s);
- o `sleep` dado ao laço nos casos com o worker dorme de verdade por no máximo 50 ms (nenhuma suposição sobre
  como o laço divide a espera).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from cq_server.adapters.smtp import SmtpVerificationChannel
from cq_server.config import ConfigError
from cq_server.ports.verification import VerificationMessage
from tests.helpers.canary import find_secrets
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import capture_logs
from tests.helpers.processes import spawn_worker
from tests.helpers.secrets import mail_secrets
from tests.helpers.smtp import HOSTNAME, FakeSmtp, monotonic
from tests.helpers.smtp import SENDER as SMTP_SENDER  # o mesmo remetente da fixture `smtp_settings`

INTERVAL_DEFAULT = 21600
MARKER = "probe.envio_teste"
ADDRESS = "aluno.neutro@example.org"
SUBJECT = "Mensagem de teste da fila"
BODY = "Texto neutro da tarefa de envio.\n"
SUBPROCESS_TIMEOUT = 60.0
RAW_SENTINEL = "resposta-crua-sentinela"


# --- montagem ------------------------------------------------------------------------------------------------


def _settings(base: Any, smtp: FakeSmtp, **changes: Any) -> Any:
    """O `MailSettings` da fixture com a porta e as credenciais do `reset` mais recente do dublê."""
    creds = smtp.credentials
    values: dict[str, Any] = {"port": smtp.port,
                              "username": creds.username if creds is not None else None,
                              "password": creds.password if creds is not None else None}
    values.update(changes)
    return dataclasses.replace(base, **values)


def _channel(base: Any, smtp: FakeSmtp, **changes: Any) -> SmtpVerificationChannel:
    """O adaptador real, com o contexto TLS que confia só na CA do dublê (por injeção)."""
    return SmtpVerificationChannel(_settings(base, smtp, **changes), ssl_context=smtp.client_context())


def _probe(channel: SmtpVerificationChannel) -> tuple[str, float]:
    started = monotonic()
    result = channel.probe()
    return result, monotonic() - started


def _assert_never_sent(smtp: FakeSmtp) -> None:
    """Nenhuma mensagem capturada, e nenhum `MAIL`, `RCPT` ou `DATA` em nenhuma conexão."""
    assert smtp.messages() == []
    for conn in smtp.connections():
        assert not {"MAIL", "RCPT", "DATA"} & set(conn.verbs), smtp.transcript()


def _rows(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute("SELECT id, result, checked_at FROM mail_probe").fetchall()


def _task_count(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0])


def _short_sleep(seconds: float) -> None:
    time.sleep(min(seconds, 0.05))


def _wait(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(0.05)


class _CountingProbe:
    """Dublê da porta `MailServerProbe`: conta as chamadas e guarda a hora do relógio em cada uma."""

    def __init__(self, clock: Any, result: str = "ok") -> None:
        self.clock = clock
        self.result = result
        self.times: list[datetime] = []

    def probe(self) -> str:
        self.times.append(self.clock.now())
        return self.result


# --- probe(): o resultado por etapa --------------------------------------------------------------------------


@pytest.mark.parametrize("security", ["none", "starttls", "tls"])
def test_probe_ok_never_sends_and_ends_with_quit(smtp: FakeSmtp, clock: Any, smtp_settings: Any,
                                                  security: str) -> None:
    """conexão, TLS (conforme o modo) e AUTH aceitos → `"ok"`; uma conexão, autenticada, terminada com
    `QUIT`; nenhuma mensagem e nenhum `MAIL`."""
    smtp.reset(clock, security=security)
    result, elapsed = _probe(_channel(smtp_settings, smtp, security=security))
    assert result == "ok"
    smtp.wait_for(lambda: all(c.ended is not None for c in smtp.connections()))
    [conn] = smtp.connections()
    assert conn.quit and conn.outcome == "quit", smtp.transcript()
    assert conn.verbs[-1] == "QUIT"
    assert [a.code for a in conn.auth] == [235]
    assert conn.tls == (security != "none")
    if security == "starttls":
        assert "STARTTLS" in conn.verbs
    _assert_never_sent(smtp)
    assert elapsed < smtp_settings.timeout_seconds + 1.0


def test_probe_auth_refused_is_auth(smtp: FakeSmtp, smtp_settings: Any) -> None:
    """AUTH recusada (535) → `"auth"`, sem `MAIL`."""
    smtp.fail("auth", 535)
    result, _ = _probe(_channel(smtp_settings, smtp))
    assert result == "auth"
    _assert_never_sent(smtp)


def test_probe_auth_not_advertised_is_auth(smtp: FakeSmtp, clock: Any, smtp_settings: Any) -> None:
    """o servidor não anuncia AUTH (`credentials=None`) e o worker tem credenciais → `"auth"` (a etapa
    decide), sem mandar AUTH nem `MAIL`."""
    smtp.reset(clock, credentials=None)
    channel = SmtpVerificationChannel(
        dataclasses.replace(smtp_settings, port=smtp.port, username="cq-sonda-usuario",
                            password="senha-da-sonda-que-nao-sai"), ssl_context=smtp.client_context())
    assert channel.probe() == "auth"
    for conn in smtp.connections():
        assert "AUTH" not in conn.verbs
    _assert_never_sent(smtp)


def test_probe_starttls_not_advertised_is_tls(smtp: FakeSmtp, clock: Any, smtp_settings: Any) -> None:
    """Modo `starttls` (`CQ_SMTP_SECURITY=starttls`) sem STARTTLS anunciado → `"tls"`, sem AUTH em claro."""
    smtp.reset(clock, security="starttls", advertise_starttls=False)
    result, _ = _probe(_channel(smtp_settings, smtp, security="starttls"))
    assert result == "tls"
    for conn in smtp.connections():
        assert "AUTH" not in conn.verbs
    _assert_never_sent(smtp)


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_probe_wrong_certificate_name_is_tls(smtp: FakeSmtp, clock: Any, smtp_settings: Any,
                                             security: str) -> None:
    """`cert="wrong_host"` (o nome do certificado não confere, com a CA confiável) → `"tls"`."""
    smtp.reset(clock, security=security, cert="wrong_host")
    result, _ = _probe(_channel(smtp_settings, smtp, security=security))
    assert result == "tls"
    _assert_never_sent(smtp)


def test_probe_connection_refused_is_connection(smtp: FakeSmtp, smtp_settings: Any) -> None:
    """Conexão recusada (porta sem ouvinte) → `"connection"`; o falso não vê conexão."""
    result, elapsed = _probe(_channel(smtp_settings, smtp, port=smtp.closed_port()))
    assert result == "connection"
    assert smtp.connections() == []
    assert elapsed < smtp_settings.timeout_seconds + 1.0


@pytest.mark.parametrize("program", [
    pytest.param(lambda s: s.fail("connect", 421), id="saudacao-421"),
    pytest.param(lambda s: s.fail("connect", 554), id="saudacao-554"),
    pytest.param(lambda s: s.fail("ehlo", 554), id="ehlo-554"),
    pytest.param(lambda s: s.fail_network("connect", "reset"), id="connect-rst"),
])
def test_probe_greeting_or_ehlo_failure_is_connection(smtp: FakeSmtp, smtp_settings: Any,
                                                      program: Callable[[FakeSmtp], None]) -> None:
    """A saudação 4xx/5xx, a conexão derrubada e o `EHLO` recusado → `"connection"`."""
    program(smtp)
    result, _ = _probe(_channel(smtp_settings, smtp))
    assert result == "connection"
    _assert_never_sent(smtp)


def test_probe_deadline_at_ehlo_is_connection_within_the_timeout(smtp: FakeSmtp, smtp_settings: Any) -> None:
    """O servidor não responde ao `EHLO` (`hang`): o prazo `timeout_seconds` vence → `"connection"`, dentro do
    teto de tempo real."""
    smtp.fail_network("ehlo", "hang")
    try:
        result, elapsed = _probe(_channel(smtp_settings, smtp))
    finally:
        smtp.release_hangs()
    assert result == "connection"
    assert elapsed < smtp_settings.timeout_seconds + 1.0, elapsed
    _assert_never_sent(smtp)


def test_probe_deadline_at_auth_is_connection(smtp: FakeSmtp, smtp_settings: Any) -> None:
    """o prazo vencido em qualquer etapa (aqui, o AUTH) é `"connection"`, não `"auth"`."""
    smtp.fail_network("auth", "hang")
    try:
        result, elapsed = _probe(_channel(smtp_settings, smtp))
    finally:
        smtp.release_hangs()
    assert result == "connection"
    assert elapsed < smtp_settings.timeout_seconds + 1.0, elapsed
    _assert_never_sent(smtp)


# --- gravação e laço -----------------------------------------------------------------------------------------


def test_record_probe_keeps_one_row_and_read_probe_returns_the_last(db_url: str, clock: Any) -> None:
    """`record_probe` idempotente: uma linha só, com o último `(result, checked_at)`; `read_probe` sem linha →
    `None`."""
    from cq_server.mail_probe import read_probe, record_probe

    t0 = clock.now()
    with psycopg.connect(db_url) as conn:
        assert read_probe(conn) is None
        for i, result in enumerate(("connection", "tls", "auth", "not_configured", "ok")):
            record_probe(conn, result, t0 + timedelta(minutes=i))
            conn.commit()
            assert read_probe(conn) == (result, t0 + timedelta(minutes=i))
            assert len(_rows(conn)) == 1
        record_probe(conn, "ok", t0 + timedelta(minutes=4))
        conn.commit()
        assert _rows(conn) == [(True, "ok", t0 + timedelta(minutes=4))]


def test_run_once_records_with_the_injected_clock(smtp: FakeSmtp, smtp_settings: Any, db_url: str,
                                                  sql: psycopg.Connection, clock: Any) -> None:
    """`run_once` com o adaptador real: devolve e grava o resultado com a hora do `FakeClock`; de novo, mais
    tarde, a mesma linha com a hora nova; nenhuma tarefa criada."""
    from cq_server.mail_probe import MailProbeLoop

    tasks_before = _task_count(sql)
    loop = MailProbeLoop(_channel(smtp_settings, smtp), db_url, 600, clock, _short_sleep)
    assert loop.run_once() == "ok"
    assert _rows(sql) == [(True, "ok", clock.now())]
    clock.advance(3600)
    smtp.fail("auth", 535)
    assert loop.run_once() == "auth"
    assert _rows(sql) == [(True, "auth", clock.now())]
    assert _task_count(sql) == tasks_before
    _assert_never_sent(smtp)


def test_loop_without_mail_configuration_records_not_configured_without_connecting(
        smtp: FakeSmtp, db_url: str, sql: psycopg.Connection, clock: Any) -> None:
    """`MailProbeLoop(None, …)` (e-mail não configurado no worker): grava `not_configured` sem conexão."""
    from cq_server.mail_probe import MailProbeLoop

    loop = MailProbeLoop(None, db_url, 600, clock, _short_sleep)
    assert loop.run_once() == "not_configured"
    assert _rows(sql) == [(True, "not_configured", clock.now())]
    assert smtp.connections() == []


def test_run_forever_probes_at_start_and_every_interval_and_stops_on_the_event(
        db_url: str, sql: psycopg.Connection, clock: Any) -> None:
    """`run_forever(stop_event)`: o primeiro teste na partida (antes de qualquer espera) e os seguintes a cada
    `interval_seconds` do relógio; o `sleep` falso anda o `FakeClock`; o evento ligado durante a espera depois do
    terceiro teste encerra o laço sem um quarto; o laço não cria linha em `tasks`."""
    from cq_server.mail_probe import MailProbeLoop

    interval = 900
    probe = _CountingProbe(clock)
    stop = threading.Event()
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        assert seconds > 0
        slept.append(seconds)
        clock.advance(seconds)
        if len(probe.times) >= 3 or len(slept) > 10_000:
            stop.set()

    tasks_before = _task_count(sql)
    start = clock.now()
    loop = MailProbeLoop(probe, db_url, interval, clock, fake_sleep)
    errors: list[BaseException] = []

    def target() -> None:
        try:
            loop.run_forever(stop)
        except BaseException as exc:  # noqa: BLE001 - devolvido ao teste
            errors.append(exc)

    thread = threading.Thread(target=target, name="cq-test-mail-probe", daemon=True)
    thread.start()
    thread.join(10)
    stop.set()
    thread.join(5)
    if errors:
        raise errors[0]
    assert not thread.is_alive(), "run_forever não parou no evento"
    assert probe.times[:1] == [start], "o primeiro teste não foi na partida"
    assert len(probe.times) == 3, probe.times
    assert [b - a for a, b in zip(probe.times, probe.times[1:])] == [timedelta(seconds=interval)] * 2
    assert _rows(sql) == [(True, "ok", probe.times[-1])]
    assert _task_count(sql) == tasks_before


def test_run_forever_with_the_event_already_set_returns(db_url: str, clock: Any) -> None:
    """Com o evento já ligado, `run_forever` volta sem esperar o intervalo."""
    from cq_server.mail_probe import MailProbeLoop

    stop = threading.Event()
    stop.set()
    slept: list[float] = []
    loop = MailProbeLoop(_CountingProbe(clock), db_url, INTERVAL_DEFAULT, clock, slept.append)
    started = time.monotonic()
    loop.run_forever(stop)
    assert time.monotonic() - started < 5.0
    assert sum(slept) < INTERVAL_DEFAULT


# --- no worker -----------------------------------------------------------------------------------------------


def _send_handler(mail_channel: Any, results: list[Any]) -> Callable[[Any], None]:
    def handle(task: Any) -> None:
        results.append(mail_channel.send(VerificationMessage(address=ADDRESS, subject=SUBJECT, body=BODY)))

    return handle


def test_run_worker_probes_only_when_injected(worker_thread: Any, queue: Any, smtp: FakeSmtp,
                                              smtp_settings: Any, db_url: str, sql: psycopg.Connection,
                                              clock: Any) -> None:
    """o `run_worker` **sem** `mail_probe` não conecta ao falso nem escreve em `mail_probe` (a tarefa de
    controle termina, então o worker estava no ar); com o laço injetado, a sonda roda na partida e grava `"ok"`
    com a hora do `FakeClock`, sem mensagem."""
    from cq_server.mail_probe import MailProbeLoop

    seen: list[int] = []
    start = worker_thread
    start(handlers={MARKER: lambda task: seen.append(task.id)})
    tid = queue.enqueue(MARKER, {})
    assert _wait(lambda: queue.get(tid).state == "done"), "o worker sem sonda não processou a tarefa"
    start.stop()
    assert smtp.connections() == []
    assert _rows(sql) == []

    loop = MailProbeLoop(_channel(smtp_settings, smtp), db_url, INTERVAL_DEFAULT, clock, _short_sleep)
    start(mail_probe=loop)
    rows = _wait(lambda: _rows(sql))
    start.stop()
    assert rows == [(True, "ok", clock.now())], "o worker com a sonda injetada não gravou o teste"
    assert smtp.connections(), "o worker com a sonda não conectou ao servidor"
    _assert_never_sent(smtp)


def test_probe_does_not_hold_the_send_path(worker_thread: Any, queue: Any, smtp: FakeSmtp, mail_channel: Any,
                                           db_url: str, sql: psycopg.Connection, clock: Any) -> None:
    """com a sonda presa (um dublê da porta que só volta quando o teste solta), uma tarefa de envio
    enfileirada ao mesmo tempo é entregue pelo adaptador real bem antes do prazo da posse das tarefas (30 s), com
    a sonda ainda presa; solta, a sonda grava o resultado."""
    from cq_server.mail_probe import MailProbeLoop

    entered = threading.Event()
    release = threading.Event()

    class _BlockingProbe:
        def probe(self) -> str:
            entered.set()
            release.wait(30)
            return "ok"

    results: list[Any] = []
    loop = MailProbeLoop(_BlockingProbe(), db_url, INTERVAL_DEFAULT, clock, _short_sleep)
    worker_thread(handlers={MARKER: _send_handler(mail_channel, results)}, mail_probe=loop)
    try:
        assert entered.wait(10), "a sonda não partiu com o worker"
        started = monotonic()
        tid = queue.enqueue(MARKER, {})
        smtp.wait_for(lambda: smtp.messages(), timeout=20.0)
        elapsed = monotonic() - started
        assert not release.is_set()
        assert elapsed < 10.0, f"a tarefa de envio esperou a sonda ({elapsed:.1f} s)"
        assert _wait(lambda: queue.get(tid).state == "done")
        assert [type(r).__name__ for r in results] == ["Delivered"]
        assert _rows(sql) == []
    finally:
        release.set()
    assert _wait(lambda: _rows(sql)) == [(True, "ok", clock.now())]


# --- configuração --------------------------------------------------------------------------------------------


def test_build_mail_probe_interval(settings: Any, clock: Any) -> None:
    """`build_mail_probe(env, settings, probe)` lê só o `env` dado: padrão 21600; o valor válido vai ao
    laço; a porta dada (ou `None`) vai ao laço; nada conecta."""
    from cq_server.__main__ import build_mail_probe

    probe = _CountingProbe(clock)
    loop = build_mail_probe({}, settings, probe)
    assert (loop.interval_seconds, loop.probe) == (INTERVAL_DEFAULT, probe)
    assert build_mail_probe({"CQ_MAIL_PROBE_INTERVAL_SECONDS": ""}, settings, probe).interval_seconds \
        == INTERVAL_DEFAULT
    loop = build_mail_probe({"CQ_MAIL_PROBE_INTERVAL_SECONDS": "600"}, settings, None)
    assert (loop.interval_seconds, loop.probe) == (600, None)
    assert isinstance(loop.conninfo, str) and loop.conninfo
    assert probe.times == []


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", "intervalo-sentinela"])
def test_build_mail_probe_refuses_invalid_interval_without_echo(settings: Any, raw: str) -> None:
    """`0`, `-1`, `abc` e `1.5` (não é inteiro positivo) → `ConfigError` com texto fixo que não traz o valor."""
    from cq_server.__main__ import build_mail_probe

    with pytest.raises(ConfigError) as info:
        build_mail_probe({"CQ_MAIL_PROBE_INTERVAL_SECONDS": raw}, settings, None)
    message = str(info.value)
    assert "CQ_MAIL_PROBE_INTERVAL_SECONDS" in message
    if raw in ("1.5", "intervalo-sentinela"):
        assert raw not in message
    with pytest.raises(ConfigError) as other:
        build_mail_probe({"CQ_MAIL_PROBE_INTERVAL_SECONDS": "-17"}, settings, None)
    assert str(other.value) == message, "a mensagem depende do valor"


def _json_records(out: str) -> list[dict]:
    import json

    records: list[dict] = []
    for line in out.splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"_raw": line})
    return records


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", "intervalo-sentinela"])
def test_worker_process_refuses_invalid_interval(db_url: str, bot_api: Any, raw: str) -> None:
    """A partida do worker (`python -m cq_server worker`) com `CQ_MAIL_PROBE_INTERVAL_SECONDS` inválido sai com
    código 2 sem subir o laço, com um único `config.invalid` (ERROR, `cq_server.main`); o valor distintivo não
    aparece em fd 1 e 2 (os registros sem o campo `time`)."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-sonda-invalida",
                         "CQ_MAIL_PROBE_INTERVAL_SECONDS": raw})
    try:
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0], SUBPROCESS_TIMEOUT)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o worker não saiu com código 2 (saiu: {code}; registros: {events})"
    assert "worker.started" not in events, events
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    assert {"level": "ERROR", "logger": "cq_server.main"}.items() <= invalid[0].items(), invalid[0]
    assert "CQ_MAIL_PROBE_INTERVAL_SECONDS" in str(invalid[0].get("detail"))
    if raw in ("1.5", "intervalo-sentinela"):
        without_time = "\n".join(str({k: v for k, v in r.items() if k != "time"}) for r in records)
        for surface, text in (("saída padrão", without_time), ("saída de erro", err)):
            assert raw not in text, f"eco do valor na {surface}"


# --- a partida real liga a sonda --------------------------------------------------------------------------------

# Prazo da linha em `mail_probe` depois do `worker.started` ([I], escolha deste teste): o teste da partida, com o prazo de 2 s
# do SMTP, cabe com folga. O intervalo fica no padrão (21600 s), então só o teste da partida cabe nesse prazo.
PROBE_ROW_TIMEOUT = 15.0


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _worker_until_probe_row(db_url: str, sql: psycopg.Connection, worker_id: str,
                            extra_env: dict[str, str]) -> tuple[list[tuple], str, str]:
    """Sobe a partida real do worker (`python -m cq_server worker`: `_cmd_worker` → `build_mail_probe` →
    `run_worker(mail_probe=…)`) com o banco do teste e sem `CQ_MAIL_PROBE_INTERVAL_SECONDS` (o padrão); o
    ambiente do subprocesso não herda nenhuma `CQ_*` do processo dos testes (`worker_env`). Espera o
    `worker.started` (`SUBPROCESS_TIMEOUT`) e, depois dele, a linha em `mail_probe` (`PROBE_ROW_TIMEOUT`), com o
    processo no ar; mata o processo no `finally` (sem processo órfão). Devolve as linhas, o stdout e o stderr."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": worker_id, **extra_env})
    try:
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0], SUBPROCESS_TIMEOUT)
        if proc.poll() is None and '"worker.started"' in proc.output()[0]:
            _wait(lambda: proc.poll() is not None or _rows(sql), PROBE_ROW_TIMEOUT)
        code = proc.poll()
        rows = _rows(sql)
    finally:
        proc.stop()
    out, err = proc.output()
    events = [r.get("event") for r in _json_records(out)]
    assert "worker.started" in events, f"o worker não subiu (código {code}; registros: {events})"
    assert code is None, f"o worker saiu durante a espera (código {code}; registros: {events})"
    assert rows, (f"a partida real do worker não gravou o teste do e-mail em {PROBE_ROW_TIMEOUT:.0f} s depois do "
                  f"worker.started (registros: {events})")
    return rows, out, err


def test_worker_process_without_mail_probes_at_start_as_not_configured(db_url: str, bot_api: Any,
                                                                        sql: psycopg.Connection) -> None:
    """A sonda testa o servidor de e-mail ao partir: a partida real do worker sem nenhuma
    `CQ_SMTP_*` e com o intervalo padrão liga a sonda, que testa na partida: com o processo no ar, uma linha em
    `mail_probe` com `not_configured` e a hora do relógio do worker, dentro do prazo explícito."""
    not_before = _real_now()
    rows, _, _ = _worker_until_probe_row(db_url, sql, "worker-sonda-sem-email", {})
    not_after = _real_now()
    [(row_id, result, checked_at)] = rows
    assert (row_id, result) == (True, "not_configured"), rows
    assert not_before - timedelta(seconds=1) <= checked_at <= not_after + timedelta(seconds=1), checked_at


def test_worker_process_with_mail_probes_the_fake_smtp_at_start_without_sending(
        db_url: str, bot_api: Any, sql: psycopg.Connection, smtp: FakeSmtp) -> None:
    """a partida real do worker com as `CQ_SMTP_*` apontando o SMTP falso (`none` no
    loopback, as credenciais do `reset` da fixture) e o intervalo padrão: com o processo no ar, uma linha em
    `mail_probe` com `ok` e a hora do relógio do worker; o falso viu a conexão autenticada e terminada com
    `QUIT`, sem nenhuma mensagem capturada nem `MAIL`, `RCPT` ou `DATA` (a sonda não envia e-mail); a senha e as
    suas formas do fio não aparecem em fd 1 e 2."""
    assert smtp.credentials is not None
    env = {"CQ_SMTP_HOST": "127.0.0.1", "CQ_SMTP_PORT": str(smtp.port), "CQ_SMTP_SECURITY": "none",
           "CQ_SMTP_USERNAME": smtp.credentials.username, "CQ_SMTP_PASSWORD": smtp.credentials.password,
           "CQ_SMTP_FROM": SMTP_SENDER, "CQ_SMTP_TIMEOUT_SECONDS": "2"}
    not_before = _real_now()
    rows, out, err = _worker_until_probe_row(db_url, sql, "worker-sonda-com-email", env)
    not_after = _real_now()
    [(row_id, result, checked_at)] = rows
    assert (row_id, result) == (True, "ok"), rows
    assert not_before - timedelta(seconds=1) <= checked_at <= not_after + timedelta(seconds=1), checked_at
    conns = smtp.wait_for(lambda: (cs := smtp.connections()) and all(c.ended is not None for c in cs) and cs)
    assert all(c.outcome == "quit" and [a.code for a in c.auth] == [235] for c in conns), smtp.transcript()
    _assert_never_sent(smtp)
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert find_secrets(text, mail_secrets(smtp)) == [], f"senha do SMTP na {surface}"


# --- nada de segredo nem de resposta crua ----------------------------------------------------------------------


def test_no_user_password_host_or_raw_reply_in_database_logs_or_stderr(
        smtp: FakeSmtp, clock: Any, smtp_settings: Any, db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    """depois de testes com sucesso, AUTH recusada com texto próprio, saudação recusada e prazo vencido,
    nem o banco (todas as tabelas), nem os registros, nem o stderr trazem o usuário, a senha (e as formas do fio),
    o `host:porta`, o nome do servidor da saudação ou o texto cru das respostas."""
    from cq_server.mail_probe import MailProbeLoop

    channel = _channel(smtp_settings, smtp)
    loop = MailProbeLoop(channel, db_url, 600, clock, _short_sleep)
    assert smtp.credentials is not None
    forbidden = mail_secrets(smtp, smtp.credentials.username, f"127.0.0.1:{smtp.port}", HOSTNAME, RAW_SENTINEL)
    with capture_logs() as logs:
        assert loop.run_once() == "ok"
        smtp.fail("auth", 535, f"5.7.8 {RAW_SENTINEL}")
        assert loop.run_once() == "auth"
        smtp.fail("connect", 554, f"5.3.2 {RAW_SENTINEL}")
        assert loop.run_once() == "connection"
        smtp.fail_network("ehlo", "hang")
        try:
            assert loop.run_once() == "connection"
        finally:
            smtp.release_hangs()
    err = capsys.readouterr().err
    stored = "\n".join(text for _, _, text in scan_all_tables(db_url))
    for surface, text in (("banco", stored), ("registros", logs.text), ("stderr", err)):
        assert find_secrets(text, forbidden) == [], f"segredo ou resposta crua no {surface}"
