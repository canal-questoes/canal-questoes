"""Teste periódico do servidor de e-mail, gravado pelo worker e lido pelo `doctor` da API.


- `record_probe(conn, result, at)`: grava a linha única de `mail_probe` (`INSERT … ON CONFLICT (id) DO
  UPDATE`), com a hora do relógio do worker;
- `read_probe(conn)`: o `(result, checked_at)` gravado, ou `None` sem linha;
- `MailProbeLoop`: o laço do worker, no molde dos laços periódicos do worker; na partida e a cada
  `interval_seconds`, chama a porta `MailServerProbe` (ou, com `probe=None`, o e-mail não configurado, grava
  `not_configured` sem conexão) e grava o resultado pelo `conninfo` do papel do worker. **Não** cria linha
  em `tasks` e não bloqueia a fila de convites e resumos. Só roda quando injetado no `run_worker`
  (`mail_probe=None` = desligado); só o `__main__` o monta (`build_mail_probe`).

Sem `smtplib` e sem `cq_server.adapters`: quem conversa com o SMTP é o adaptador, pela porta.

Uma falha do laço (o banco fora do ar, uma exceção da porta) fica num registro `worker.iteration_failed` (fase
`mail_probe`; o `JsonFormatter` só põe o tipo e a posição) e o laço segue até o próximo intervalo; a linha anterior
de `mail_probe` fica como estava, e o `doctor` a vê envelhecer.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import psycopg

from cq_server.clock import Clock
from cq_server.ports.verification import MailServerProbe, ProbeResult

__all__ = [
    "MAIL_PROBE_INTERVAL_DEFAULT",
    "MAIL_PROBE_INTERVAL_ENV",
    "MailProbeLoop",
    "ProbeResult",
    "read_probe",
    "record_probe",
]

# O intervalo do laço (inteiro positivo de segundos; padrão 6 h).
MAIL_PROBE_INTERVAL_ENV = "CQ_MAIL_PROBE_INTERVAL_SECONDS"
MAIL_PROBE_INTERVAL_DEFAULT = 21600

# Maior espera de uma vez entre dois testes: a parada (`stop_event`) é conferida entre as esperas, então com um
# `sleep` real ela leva no máximo isso para ser vista ([I], valor escolhido aqui).
WAIT_SLICE_SECONDS = 0.5

# O mesmo logger dos laços do worker (o laço roda numa thread do worker).
logger = logging.getLogger("cq_server.worker")

_UPSERT = ("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s) "
           "ON CONFLICT (id) DO UPDATE SET result = EXCLUDED.result, checked_at = EXCLUDED.checked_at")


def record_probe(conn: psycopg.Connection, result: ProbeResult, at: datetime) -> None:
    """Grava `(result, at)` na linha única de `mail_probe`, na transação de quem chama (sem `commit`)."""
    conn.execute(_UPSERT, (result, at))


def read_probe(conn: psycopg.Connection) -> tuple[ProbeResult, datetime] | None:
    """O último resultado gravado e a hora; `None` sem linha. Só `SELECT`, na transação de quem chama (o `collect`
    do `doctor` o lê na transação de leitura da rota)."""
    row = conn.execute("SELECT result, checked_at FROM mail_probe WHERE id").fetchone()
    if row is None:
        return None
    return row[0], row[1]


class MailProbeLoop:
    """O laço do teste do servidor de e-mail no worker (ver o docstring do módulo)."""

    def __init__(self, probe: MailServerProbe | None, conninfo: str, interval_seconds: float, clock: Clock,
                 sleep: Callable[[float], object]) -> None:
        self.probe = probe
        self.conninfo = conninfo
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.sleep = sleep

    def run_once(self) -> ProbeResult:
        """Um teste e a gravação do resultado; devolve o resultado gravado.

        Sem porta (`probe=None`, o e-mail não configurado), `not_configured` sem conexão nenhuma ao servidor. A
        hora é a do relógio injetado no fim do teste; a gravação usa uma conexão própria ao `conninfo`, fechada
        (com `commit`) antes de voltar. Não cria linha em `tasks`."""
        result: ProbeResult = "not_configured" if self.probe is None else self.probe.probe()
        at = self.clock.now()
        with psycopg.connect(self.conninfo) as conn:
            record_probe(conn, result, at)
        return result

    def run_forever(self, stop_event: threading.Event) -> None:
        """Na partida e a cada `interval_seconds` do relógio, até o `stop_event`.

        O próximo teste é marcado a partir da hora em que o anterior começou; a espera é feita pelo `sleep`
        injetado em pedaços de até `WAIT_SLICE_SECONDS`, conferindo o relógio e o evento entre eles. Uma falha de
        um teste é registrada sem detalhe e não encerra o laço."""
        interval = timedelta(seconds=float(self.interval_seconds))
        while not stop_event.is_set():
            next_at = self.clock.now() + interval
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - registrada sem detalhe; o laço segue no próximo intervalo
                logger.exception("worker.iteration_failed", extra={"phase": "mail_probe"})
            while not stop_event.is_set():
                remaining = (next_at - self.clock.now()).total_seconds()
                if remaining <= 0:
                    break
                self.sleep(min(remaining, WAIT_SLICE_SECONDS))

    def __repr__(self) -> str:
        # O `conninfo` pode levar a senha do papel: fica fora do `repr`.
        return f"MailProbeLoop(interval_seconds={self.interval_seconds!r})"
