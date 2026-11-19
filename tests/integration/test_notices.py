"""Avisos de mudança de configuração.

Interface: `notify_config_change(conn, topic, key)` dentro da transação do chamador;
`NoticeListener(conninfo)` como gerenciador de contexto (o `LISTEN` já vale quando o `__enter__`
retorna), com `wait(timeout)`, que bloqueia até o primeiro aviso ou o timeout e então devolve todos
os avisos já recebidos, sem bloquear de novo. As esperas aqui são reais (o aviso é assíncrono), sempre
com prazo.
"""

from __future__ import annotations

import time

import psycopg
from psycopg.pq import TransactionStatus

from cq_server.notices import ConfigNotice, NoticeListener, notify_config_change


def _collect(listener: NoticeListener, want: int, within: float) -> list[ConfigNotice]:
    """Soma chamadas de `wait` até ter `want` avisos ou passar `within` segundos."""
    received: list[ConfigNotice] = []
    deadline = time.monotonic() + within
    while len(received) < want:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        received.extend(listener.wait(remaining))
    return received


def test_notice_is_delivered_only_after_commit(db_url: str) -> None:
    """com a transação aberta, `wait(0.5)` → `[]`; depois do commit →
    `[ConfigNotice(topic, key)]`, sem esperar o timeout inteiro; o aviso não se repete."""
    notice = ConfigNotice(topic="bot", key="oferecimento-mc102")
    with NoticeListener(db_url) as listener:
        with psycopg.connect(db_url) as conn:
            notify_config_change(conn, notice.topic, notice.key)
            assert conn.info.transaction_status == TransactionStatus.INTRANS
            assert listener.wait(0.5) == []
            conn.commit()
        started = time.monotonic()
        received = listener.wait(5)
        elapsed = time.monotonic() - started
        assert received == [notice]
        assert elapsed < 2.5, f"wait não voltou no primeiro aviso ({elapsed:.2f} s)"
        assert listener.wait(0.5) == []


def test_rolled_back_notice_is_never_delivered(db_url: str) -> None:
    """aviso numa transação desfeita não chega, nem depois; o aviso confirmado em
    seguida chega sozinho."""
    kept = ConfigNotice(topic="bot", key="chave-confirmada")
    with NoticeListener(db_url) as listener:
        with psycopg.connect(db_url) as conn:
            notify_config_change(conn, "bot", "chave-desfeita")
            conn.rollback()
            assert listener.wait(0.5) == []
            notify_config_change(conn, kept.topic, kept.key)
            conn.commit()
        assert _collect(listener, 1, 3) == [kept]
        assert listener.wait(0.5) == []


def test_two_notices_in_consecutive_transactions_arrive_in_order(db_url: str) -> None:
    """dois avisos em transações seguidas → os dois, na ordem, somando chamadas de `wait`
    até ter 2 ou passar 3 s; nenhum aviso repetido."""
    first = ConfigNotice(topic="bot", key="chave-primeira")
    second = ConfigNotice(topic="entrega", key="chave-segunda")
    with NoticeListener(db_url) as listener:
        with psycopg.connect(db_url) as conn:
            notify_config_change(conn, first.topic, first.key)
            conn.commit()
            notify_config_change(conn, second.topic, second.key)
            conn.commit()
        assert _collect(listener, 2, 3) == [first, second]
        assert listener.wait(0.5) == []


def test_every_listening_worker_receives_the_notice(db_url: str) -> None:
    """Os workers que escutam recebem o aviso: dois ouvintes recebem o mesmo aviso
    confirmado."""
    notice = ConfigNotice(topic="bot", key="chave-dois-ouvintes")
    with NoticeListener(db_url) as one, NoticeListener(db_url) as other:
        with psycopg.connect(db_url) as conn:
            notify_config_change(conn, notice.topic, notice.key)
            conn.commit()
        assert _collect(one, 1, 3) == [notice]
        assert _collect(other, 1, 3) == [notice]


def test_wait_without_notice_honors_timeout(db_url: str) -> None:
    """`wait`: sem aviso, `wait(0.5)` devolve `[]` e leva de 0,4 s a 1,5 s."""
    with NoticeListener(db_url) as listener:
        started = time.monotonic()
        received = listener.wait(0.5)
        elapsed = time.monotonic() - started
    assert received == []
    assert 0.4 <= elapsed <= 1.5, f"wait(0.5) levou {elapsed:.2f} s"
