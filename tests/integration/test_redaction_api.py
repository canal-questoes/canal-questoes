"""Redação de texto livre na API, na gravação e na saída.

Cobre a redação de texto livre na gravação e na saída. Contratos: a redação, o texto de `last_error` e os
dados dos eventos, e o marcador `x-free-text` dos contratos.

O texto de falha com identidade passa pelos quatro caminhos do texto de `last_error`: `fail(…,
permanent=True)`, `Undeliverable(reason)`, `fail(error=…)` temporário e `RetryLater(reason)`, estes
dois também até esgotar as tentativas.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from cq_server.outbox import OutboxRecord
from cq_server.ports.delivery import OutgoingMessage, RetryLater, Undeliverable
from cq_server.tasks import ClaimedTask, TaskRecord
from tests.helpers.api import (ADMIN_TOKEN, all_events, auth, create_token, marker, register_person,
                               uuid4_with_prefix)
from tests.helpers.calls import SEEDED_FREE_TEXT
from tests.helpers.canary import URSULA, ZEFIRO, CanaryPerson, find_canary, find_secrets
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches

TASK_TYPE = "demo.redaction"
WORKER = "worker-redacao"
FREE_TEXT_TYPES = ("token.created", "identity.revealed", "task.failed", "outbox.failed")

# Homônimo fictício (fora do canário), para o desempate.
HOMONYM = "Teodoro Jabuticaba Lindqvist"

# Espera da falha temporária com o `settings` dos testes (base 2 s, teto 60 s, 3 tentativas):
# `min(2 · 2^(attempts−1), 60)`, ou seja, 2 s e depois 4 s.
MAX_ATTEMPTS = 3


def _backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(2 * 2 ** (attempt - 1), 60))


# Token inicial literal que **não** tem a forma `cq_…`: só a regra do valor literal o redige.
LITERAL_ADMIN = "Chave-Inicial-Literal-Sentinela-Z9y8X7w6V5"
assert len(LITERAL_ADMIN) >= 32 and not LITERAL_ADMIN.startswith("cq_")


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _register(client: Any, person: CanaryPerson, token: str = ADMIN_TOKEN) -> dict:
    return register_person(client, token, **person.payload())


def _strip_seq(events: list[dict]) -> list[dict]:
    """Tira o `seq` (valor de sequência, nunca afirmado por valor), conferindo que cresce."""
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _events(client: Any, *types: str, token: str = ADMIN_TOKEN) -> list[dict]:
    return _strip_seq(all_events(client, token, *types))


def _token_item(created: dict, label: str, revoked_at: str | None = None) -> dict:
    """`Token` esperado em `tokens.list`/`tokens.revoke`."""
    return {"token_id": created["token_id"], "label": label, "permissions": created["permissions"],
            "created_at": created["created_at"], "revoked_at": revoked_at}


def _token_created_event(created: dict, label: str, actor: str = "bootstrap") -> dict:
    return {"type": "token.created", "occurred_at": created["created_at"], "actor_token_id": actor,
            "data": {"token_id": created["token_id"], "label": label,
                     "permissions": created["permissions"]}}


def _list_tokens(client: Any, token: str = ADMIN_TOKEN) -> Any:
    resp = client.get("/v1/tokens", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert_matches("tokens.list", 200, resp.json())
    return resp


def _revoke(client: Any, token_id: str, token: str = ADMIN_TOKEN) -> Any:
    resp = client.post(f"/v1/tokens/{token_id}/revoke", headers=auth(token))
    assert resp.status_code == 200, resp.text
    assert_matches("tokens.revoke", 200, resp.json())
    return resp


def _reveal(client: Any, pseudonyms: list[str], reason: str, token: str = ADMIN_TOKEN) -> Any:
    resp = client.post("/v1/identity/reveal", json={"pseudonyms": pseudonyms, "reason": reason},
                       headers=auth(token, "cli"))
    assert resp.status_code == 200, resp.text
    assert_matches("identity.reveal", 200, resp.json())
    return resp


def _revealed_event(at: str, revealed: list[str], reason: str, actor: str = "bootstrap") -> dict:
    return {"type": "identity.revealed", "occurred_at": at, "actor_token_id": actor,
            "data": {"client": "cli", "revealed": revealed, "not_found_count": 0, "reason": reason}}


def _fail_task(queue: Any, error: str) -> int:
    """Enfileira, pega e falha em definitivo uma tarefa com `fail(error=…)`."""
    task_id = queue.enqueue(TASK_TYPE, {"caso": "redacao"})
    claimed = queue.claim(WORKER)
    assert claimed is not None and claimed.id == task_id
    assert queue.fail(task_id, WORKER, error=error, permanent=True) is True
    return task_id


def _task_failed_event(task_id: int, error: str, at: str, attempts: int = 1) -> dict:
    return {"type": "task.failed", "occurred_at": at, "actor_token_id": None,
            "data": {"task_id": task_id, "task_type": TASK_TYPE, "attempts": attempts, "error": error}}


def _enqueue_message(db_url: str, outbox: Any, recipient: str, text: str) -> int:
    """Enfileira uma mensagem numa transação confirmada."""
    with psycopg.connect(db_url) as conn:
        message_id = outbox.enqueue(conn, recipient=uuid.UUID(recipient), text=text)
        conn.commit()
    return message_id


def _undeliverable(db_url: str, outbox: Any, recipient: str, reason: str) -> int:
    """Enfileira uma mensagem confirmada e a despacha por um canal que devolve `Undeliverable`."""
    message_id = _enqueue_message(db_url, outbox, recipient, "mensagem de teste da redacao")
    channel = ScriptedChannel([Undeliverable(reason)])
    assert outbox.dispatch_once(channel, WORKER) == message_id
    assert len(channel.calls) == 1
    return message_id


def _outbox_failed_event(message_id: int, recipient: str, error: str, at: str, attempts: int = 1) -> dict:
    return {"type": "outbox.failed", "occurred_at": at, "actor_token_id": None,
            "data": {"message_id": message_id, "recipient": recipient, "attempts": attempts,
                     "error": error}}


def _stored_label(sql: Any, token_id: str) -> str:
    rows = sql.execute("SELECT label FROM api_tokens WHERE token_id::text = %s", (token_id,)).fetchall()
    assert len(rows) == 1
    return rows[0][0]


def _stored_free_text_events(sql: Any) -> list[tuple[str, dict]]:
    return [(row[0], row[1]) for row in sql.execute(
        "SELECT type, data FROM events WHERE type IN "
        "('token.created', 'identity.revealed', 'task.failed', 'outbox.failed') ORDER BY seq")]


# ---------------------------------------------------------------------------------------------
# Rótulo do token
# ---------------------------------------------------------------------------------------------

LABEL_CASES = [
    pytest.param(f"token de {ZEFIRO.name}", "token de {m}", id="nome"),
    pytest.param(f"RA {ZEFIRO.ra} do operador", "RA {m} do operador", id="ra"),
    pytest.param(f"contato {ZEFIRO.email}", "contato {m}", id="email"),
    pytest.param("token de zefiro   CANARIO quintela", "token de {m}", id="nome-sem-acento-caixa-espacos"),
    pytest.param(f"contato {ZEFIRO.email.upper()}", "contato {m}", id="email-em-maiusculas"),
]


@pytest.mark.parametrize(("raw_label", "expected_template"), LABEL_CASES)
def test_label_redacted_in_token_outputs(client: Any, clock: Any, raw_label: str,
                                         expected_template: str) -> None:
    """o rótulo com o nome, o RA ou o e-mail de uma pessoa cadastrada (também sem
    acento, em outra caixa e com espaços extras) sai com `marker(alias)` em `tokens.create`,
    `tokens.list`, `tokens.revoke` e no evento `token.created`."""
    z = _register(client, ZEFIRO)
    expected = expected_template.format(m=marker(z["alias"]))

    created_at = iso(clock.now())
    secret, created = create_token(client, ADMIN_TOKEN, ["people:read"], label=raw_label)
    assert created == {"token_id": created["token_id"], "label": expected, "permissions": ["people:read"],
                       "created_at": created_at, "revoked_at": None, "secret": secret}

    listed = _list_tokens(client)
    assert listed.json() == {"items": [_token_item(created, expected)]}

    clock.advance(30)
    revoked = _revoke(client, created["token_id"])
    assert revoked.json() == _token_item(created, expected, revoked_at=iso(clock.now()))

    events = _events(client, "token.created")
    assert events == [_token_created_event(created, expected)]
    # O `secret` (aleatório) fica fora da varredura: só o texto livre e os demais campos importam.
    without_secret = {k: v for k, v in created.items() if k != "secret"}
    for text in (json.dumps(without_secret, ensure_ascii=False), listed.text, revoked.text,
                 json.dumps(events, ensure_ascii=False)):
        assert find_canary(text) == []


# ---------------------------------------------------------------------------------------------
# Justificativa da revelação
# ---------------------------------------------------------------------------------------------

REASON_CASES = [
    pytest.param(SEEDED_FREE_TEXT["reason"], "conferir {u} e {z}", id="nome-e-email"),
    pytest.param("revisar zefiro canario QUINTELA e ursula  pintassilgo bethania", "revisar {z} e {u}",
                 id="nomes-sem-acento-caixa-espacos"),
    pytest.param(f"RA {ZEFIRO.ra} e RA {URSULA.ra}", "RA {z} e RA {u}", id="ras"),
]


@pytest.mark.parametrize(("raw_reason", "expected_template"), REASON_CASES)
def test_reason_redacted_in_reveal_events(client: Any, clock: Any, raw_reason: str,
                                          expected_template: str) -> None:
    """a justificativa com identidades de pessoas cadastradas sai com
    `marker(alias)` de cada uma no evento `identity.revealed`."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    expected = expected_template.format(z=marker(z["alias"]), u=marker(u["alias"]))

    _reveal(client, [z["alias"]], raw_reason)

    events = _events(client, "identity.revealed")
    assert events == [_revealed_event(iso(clock.now()), [z["pseudo_id"]], expected)]
    assert find_canary(json.dumps(events, ensure_ascii=False)) == []


# ---------------------------------------------------------------------------------------------
# Erro das falhas definitivas de tarefas e mensagens
# ---------------------------------------------------------------------------------------------


def test_failure_errors_redacted_in_events(client: Any, clock: Any, queue: Any, outbox: Any,
                                           db_url: str) -> None:
    """o `error` de `task.failed` (vindo de `fail(error=…)`) e o de
    `outbox.failed` (vindo de `Undeliverable(reason)`) saem com `marker(alias)`."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    mz, mu = marker(z["alias"]), marker(u["alias"])

    first_task = _fail_task(queue, SEEDED_FREE_TEXT["task_error"])
    second_task = _fail_task(queue, f"contato {ZEFIRO.email.upper()} indisponível")
    message_id = _undeliverable(db_url, outbox, u["pseudo_id"], SEEDED_FREE_TEXT["outbox_reason"])

    at = iso(clock.now())
    events = _events(client, "task.failed", "outbox.failed")
    assert events == [
        _task_failed_event(first_task, f"falhou para {mz} RA {mz}", at),
        _task_failed_event(second_task, f"contato {mz} indisponível", at),
        _outbox_failed_event(message_id, u["pseudo_id"], f"destinatário {mu} bloqueou (RA {mu})", at),
    ]
    assert find_canary(json.dumps(events, ensure_ascii=False)) == []


# ---------------------------------------------------------------------------------------------
# Na gravação
# ---------------------------------------------------------------------------------------------


def test_free_text_is_stored_redacted(client: Any, sql: Any, queue: Any, outbox: Any,
                                      db_url: str) -> None:
    """Redação na gravação: `api_tokens.label`, `tasks.last_error`,
    `outbox_messages.last_error` e `events.data` guardam o texto já redigido."""
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    mz, mu = marker(z["alias"]), marker(u["alias"])
    exp_label = f"token de {mz} (RA {mz})"
    exp_reason = f"conferir {mu} e {mz}"
    exp_task = f"falhou para {mz} RA {mz}"
    exp_outbox = f"destinatário {mu} bloqueou (RA {mu})"

    _, created = create_token(client, ADMIN_TOKEN, ["people:read"], label=SEEDED_FREE_TEXT["label"])
    _reveal(client, [u["alias"]], SEEDED_FREE_TEXT["reason"])
    task_id = _fail_task(queue, SEEDED_FREE_TEXT["task_error"])
    message_id = _undeliverable(db_url, outbox, u["pseudo_id"], SEEDED_FREE_TEXT["outbox_reason"])

    assert _stored_label(sql, created["token_id"]) == exp_label
    assert sql.execute("SELECT last_error FROM tasks WHERE id = %s", (task_id,)).fetchall() == [(exp_task,)]
    assert sql.execute("SELECT last_error FROM outbox_messages WHERE id = %s",
                       (message_id,)).fetchall() == [(exp_outbox,)]
    assert _stored_free_text_events(sql) == [
        ("token.created", {"token_id": created["token_id"], "label": exp_label,
                           "permissions": ["people:read"]}),
        ("identity.revealed", {"client": "cli", "revealed": [u["pseudo_id"]], "not_found_count": 0,
                               "reason": exp_reason}),
        ("task.failed", {"task_id": task_id, "task_type": TASK_TYPE, "attempts": 1, "error": exp_task}),
        ("outbox.failed", {"message_id": message_id, "recipient": u["pseudo_id"], "attempts": 1,
                           "error": exp_outbox}),
    ]
    stored_text = "\n".join(
        [row[0] for row in sql.execute("SELECT label FROM api_tokens")]
        + [row[0] or "" for row in sql.execute("SELECT last_error FROM tasks")]
        + [row[0] or "" for row in sql.execute("SELECT last_error FROM outbox_messages")]
        + [row[0] for row in sql.execute("SELECT data::text FROM events")])
    assert find_canary(stored_text) == []


# ---------------------------------------------------------------------------------------------
# Falha temporária e esgotamento das tentativas
# ---------------------------------------------------------------------------------------------

# Um texto por tentativa (texto cru, redação esperada); o último é o que fica no esgotamento.
TASK_RETRY_ERRORS = [
    (SEEDED_FREE_TEXT["task_error"], "falhou para {z} RA {z}"),
    (f"contato {ZEFIRO.email.upper()} indisponível", "contato {z} indisponível"),
    (f"sem resposta de {URSULA.name} (RA {URSULA.ra})", "sem resposta de {u} (RA {u})"),
]
OUTBOX_RETRY_REASONS = [
    (SEEDED_FREE_TEXT["outbox_reason"], "destinatário {u} bloqueou (RA {u})"),
    (f"caixa de {ZEFIRO.email} cheia", "caixa de {z} cheia"),
    (f"aguardando {ZEFIRO.name} liberar", "aguardando {z} liberar"),
]
assert len(TASK_RETRY_ERRORS) == len(OUTBOX_RETRY_REASONS) == MAX_ATTEMPTS


def _retry_marks(client: Any) -> dict[str, str]:
    z = _register(client, ZEFIRO)
    u = _register(client, URSULA)
    return {"z": marker(z["alias"]), "u": marker(u["alias"]), "u_pseudo": u["pseudo_id"]}


def test_temporary_task_failures_store_redacted_error_until_exhausted(client: Any, clock: Any, sql: Any,
                                                                      queue: Any) -> None:
    """Redação na gravação e na saída (na gravação:
    `tasks.last_error`) e texto de `last_error` (`fail(error=…)` sem `permanent`; tentativas esgotadas por falha
    temporária → o texto da última falha): a cada falha temporária, o item `pending` guarda em
    `tasks.last_error` o texto já redigido; na 3ª, `failed` com o último texto redigido, e o `error`
    de `task.failed` sai redigido na API e fica redigido em `events.data`."""
    marks = _retry_marks(client)
    payload = {"caso": "falha-temporaria"}
    task_id = queue.enqueue(TASK_TYPE, payload)
    available_at = clock.now()

    for attempt, (raw, template) in enumerate(TASK_RETRY_ERRORS[:-1], start=1):
        expected = template.format(**marks)
        assert find_canary(raw) and find_canary(expected) == []
        assert queue.claim(WORKER) == ClaimedTask(id=task_id, task_type=TASK_TYPE, payload=payload,
                                                  attempts=attempt)
        now = clock.now()
        assert queue.fail(task_id, WORKER, raw) is True
        available_at = now + _backoff(attempt)
        assert queue.get(task_id) == TaskRecord(
            id=task_id, task_type=TASK_TYPE, state="pending", attempts=attempt, available_at=available_at,
            lease_owner=None, lease_until=None, last_error=expected, finished_at=None)
        assert sql.execute("SELECT state, last_error FROM tasks WHERE id = %s",
                           (task_id,)).fetchall() == [("pending", expected)]
        assert _events(client, "task.failed") == []
        clock.advance(60)  # além da espera (2 s e depois 4 s)

    raw, template = TASK_RETRY_ERRORS[-1]
    exp_last = template.format(**marks)
    assert queue.claim(WORKER) == ClaimedTask(id=task_id, task_type=TASK_TYPE, payload=payload,
                                              attempts=MAX_ATTEMPTS)
    failed_at = clock.now()
    assert queue.fail(task_id, WORKER, raw) is True
    assert queue.get(task_id) == TaskRecord(
        id=task_id, task_type=TASK_TYPE, state="failed", attempts=MAX_ATTEMPTS, available_at=available_at,
        lease_owner=None, lease_until=None, last_error=exp_last, finished_at=failed_at)
    assert sql.execute("SELECT state, last_error FROM tasks WHERE id = %s",
                       (task_id,)).fetchall() == [("failed", exp_last)]

    events = _events(client, "task.failed")
    assert events == [_task_failed_event(task_id, exp_last, iso(failed_at), attempts=MAX_ATTEMPTS)]
    assert _stored_free_text_events(sql) == [
        ("task.failed", {"task_id": task_id, "task_type": TASK_TYPE, "attempts": MAX_ATTEMPTS,
                         "error": exp_last}),
    ]
    stored_text = "\n".join(
        [row[0] or "" for row in sql.execute("SELECT last_error FROM tasks")]
        + [row[0] for row in sql.execute("SELECT data::text FROM events")])
    assert find_canary(stored_text) == []
    assert find_canary(json.dumps(events, ensure_ascii=False)) == []


def test_retry_later_reasons_store_redacted_error_until_exhausted(client: Any, clock: Any, sql: Any,
                                                                  outbox: Any, db_url: str) -> None:
    """Redação na gravação e na saída (na gravação:
    `outbox_messages.last_error`) e texto de `last_error` (`RetryLater(reason)`; tentativas esgotadas por falha
    temporária → o texto da última falha): a cada `RetryLater`, a mensagem `pending` guarda em
    `outbox_messages.last_error` o `reason` já redigido; na 3ª, `failed` com o último `reason`
    redigido, e o `error` de `outbox.failed` sai redigido na API e fica redigido em `events.data`."""
    marks = _retry_marks(client)
    recipient = marks["u_pseudo"]
    text = "mensagem de teste da redacao temporaria"
    message_id = _enqueue_message(db_url, outbox, recipient, text)
    channel = ScriptedChannel([RetryLater(raw) for raw, _ in OUTBOX_RETRY_REASONS])
    available_at = clock.now()

    for attempt, (raw, template) in enumerate(OUTBOX_RETRY_REASONS[:-1], start=1):
        expected = template.format(**marks)
        assert find_canary(raw) and find_canary(expected) == []
        now = clock.now()
        assert outbox.dispatch_once(channel, WORKER) == message_id
        available_at = now + _backoff(attempt)
        assert outbox.get(message_id) == OutboxRecord(
            id=message_id, recipient=uuid.UUID(recipient), text=text, state="pending", attempts=attempt,
            available_at=available_at, lease_owner=None, lease_until=None, channel_ref=None,
            last_error=expected, sent_at=None)
        assert sql.execute("SELECT state, last_error FROM outbox_messages WHERE id = %s",
                           (message_id,)).fetchall() == [("pending", expected)]
        assert _events(client, "outbox.failed") == []
        clock.advance(60)  # além da espera (2 s e depois 4 s)

    _, template = OUTBOX_RETRY_REASONS[-1]
    exp_last = template.format(**marks)
    failed_at = clock.now()
    assert outbox.dispatch_once(channel, WORKER) == message_id
    assert channel.calls == [OutgoingMessage(message_id=message_id, recipient=uuid.UUID(recipient),
                                             text=text)] * MAX_ATTEMPTS
    assert outbox.get(message_id) == OutboxRecord(
        id=message_id, recipient=uuid.UUID(recipient), text=text, state="failed", attempts=MAX_ATTEMPTS,
        available_at=available_at, lease_owner=None, lease_until=None, channel_ref=None,
        last_error=exp_last, sent_at=None)
    assert sql.execute("SELECT state, last_error FROM outbox_messages WHERE id = %s",
                       (message_id,)).fetchall() == [("failed", exp_last)]

    events = _events(client, "outbox.failed")
    assert events == [_outbox_failed_event(message_id, recipient, exp_last, iso(failed_at),
                                           attempts=MAX_ATTEMPTS)]
    assert _stored_free_text_events(sql) == [
        ("outbox.failed", {"message_id": message_id, "recipient": recipient, "attempts": MAX_ATTEMPTS,
                           "error": exp_last}),
    ]
    stored_text = "\n".join(
        [row[0] or "" for row in sql.execute("SELECT last_error FROM outbox_messages")]
        + [row[0] for row in sql.execute("SELECT data::text FROM events")])
    assert find_canary(stored_text) == []
    assert find_canary(json.dumps(events, ensure_ascii=False)) == []


# ---------------------------------------------------------------------------------------------
# Na saída: pessoa cadastrada depois da gravação
# ---------------------------------------------------------------------------------------------


def test_output_redacts_people_registered_later(client: Any, clock: Any, queue: Any, outbox: Any,
                                                db_url: str) -> None:
    """Redação na saída, com as identidades do momento da resposta (alcança o texto gravado antes do cadastro): rótulo,
    `reason` de `identity.revealed` e `error` de `task.failed`/`outbox.failed` gravados antes do
    cadastro da pessoa saem com `marker(alias)` dela na API depois do cadastro. Só a saída é
    afirmada; o conteúdo do banco não."""
    u = _register(client, URSULA)
    mu = marker(u["alias"])
    raw_outbox = f"destinatário {URSULA.name} pediu contato por {ZEFIRO.email}"

    written_at = iso(clock.now())
    _, created = create_token(client, ADMIN_TOKEN, ["people:read"], label=SEEDED_FREE_TEXT["label"])
    # Ninguém com essa identidade estava cadastrado: a saída da criação não tinha o que trocar.
    assert created["label"] == SEEDED_FREE_TEXT["label"]
    _reveal(client, [u["alias"]], SEEDED_FREE_TEXT["reason"])
    task_id = _fail_task(queue, SEEDED_FREE_TEXT["task_error"])
    message_id = _undeliverable(db_url, outbox, u["pseudo_id"], raw_outbox)

    clock.advance(60)
    z = _register(client, ZEFIRO)
    mz = marker(z["alias"])
    exp_label = f"token de {mz} (RA {mz})"

    listed = _list_tokens(client)
    assert listed.json() == {"items": [_token_item(created, exp_label)]}
    clock.advance(30)
    revoked = _revoke(client, created["token_id"])
    assert revoked.json() == _token_item(created, exp_label, revoked_at=iso(clock.now()))

    events = _events(client, *FREE_TEXT_TYPES)
    assert events == [
        _token_created_event(created, exp_label),
        _revealed_event(written_at, [u["pseudo_id"]], f"conferir {mu} e {mz}"),
        _task_failed_event(task_id, f"falhou para {mz} RA {mz}", written_at),
        _outbox_failed_event(message_id, u["pseudo_id"], f"destinatário {mu} pediu contato por {mz}",
                             written_at),
    ]
    for text in (listed.text, revoked.text, json.dumps(events, ensure_ascii=False)):
        assert find_canary(text) == []


# ---------------------------------------------------------------------------------------------
# Limites só na entrada
# ---------------------------------------------------------------------------------------------


def test_length_limits_apply_only_to_input(client: Any, sql: Any, clock: Any) -> None:
    """Limites: rótulo com 100 caracteres e justificativa com 500, cada
    um com o RA de uma pessoa, são aceitos (201/200) e saem e ficam gravados redigidos por inteiro,
    mais longos que o limite."""
    z = _register(client, ZEFIRO)
    mz = marker(z["alias"])
    head_label = ("rotulo-" * 20)[:89]
    raw_label = f"{head_label} {ZEFIRO.ra}"
    exp_label = f"{head_label} {mz}"
    head_reason = ("motivo-" * 80)[:489]
    raw_reason = f"{head_reason} {ZEFIRO.ra}"
    exp_reason = f"{head_reason} {mz}"
    assert len(raw_label) == 100 and len(exp_label) > 100
    assert len(raw_reason) == 500 and len(exp_reason) > 500

    _, created = create_token(client, ADMIN_TOKEN, ["people:read"], label=raw_label)
    assert created["label"] == exp_label
    assert _list_tokens(client).json() == {"items": [_token_item(created, exp_label)]}
    reveal = _reveal(client, [z["alias"]], raw_reason)
    assert reveal.json() == {"people": [{"pseudo_id": z["pseudo_id"], "alias": z["alias"],
                                         "name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": ZEFIRO.email}],
                             "not_found": []}

    assert _events(client, "token.created", "identity.revealed") == [
        _token_created_event(created, exp_label),
        _revealed_event(iso(clock.now()), [z["pseudo_id"]], exp_reason),
    ]
    assert _stored_label(sql, created["token_id"]) == exp_label
    assert [data for (_, data) in _stored_free_text_events(sql)] == [
        {"token_id": created["token_id"], "label": exp_label, "permissions": ["people:read"]},
        {"client": "cli", "revealed": [z["pseudo_id"]], "not_found_count": 0, "reason": exp_reason},
    ]


# ---------------------------------------------------------------------------------------------
# Segredos
# ---------------------------------------------------------------------------------------------


def test_token_secrets_redacted_in_label_and_task_error(client: Any, sql: Any, clock: Any,
                                                        queue: Any) -> None:
    """A mesma redação troca por `[segredo]` qualquer segredo de token: um rótulo e um
    `fail(error=…)` com o segredo de um token criado saem e ficam gravados com `[segredo]`, sem o
    segredo nem o resumo dele."""
    leaked, original = create_token(client, ADMIN_TOKEN, ["people:read"], label="token original")
    exp_label = "copia de [segredo] guardada"
    exp_error = "falhou ao usar [segredo]"

    second_secret, created = create_token(client, ADMIN_TOKEN, ["people:read"],
                                          label=f"copia de {leaked} guardada")
    assert created["label"] == exp_label
    listed = _list_tokens(client)
    assert listed.json() == {"items": [_token_item(original, "token original"),
                                       _token_item(created, exp_label)]}
    task_id = _fail_task(queue, f"falhou ao usar {leaked}")

    events = _events(client, "token.created", "task.failed")
    assert events == [
        _token_created_event(original, "token original"),
        _token_created_event(created, exp_label),
        _task_failed_event(task_id, exp_error, iso(clock.now())),
    ]
    assert _stored_label(sql, created["token_id"]) == exp_label
    assert sql.execute("SELECT last_error FROM tasks WHERE id = %s", (task_id,)).fetchall() == [(exp_error,)]
    assert [data for (_, data) in _stored_free_text_events(sql)] == [e["data"] for e in events]

    outputs = [json.dumps(created, ensure_ascii=False), listed.text, json.dumps(events, ensure_ascii=False)]
    stored = [_stored_label(sql, created["token_id"]),
              sql.execute("SELECT last_error FROM tasks WHERE id = %s", (task_id,)).fetchone()[0],
              *[row[0] for row in sql.execute("SELECT data::text FROM events")]]
    for text in outputs + stored:
        assert find_secrets(text, [leaked]) == []
    for text in outputs[1:] + stored:
        assert find_secrets(text, [second_secret]) == []


def test_configured_admin_token_literal_is_redacted(app_factory: Any, sql: Any, clock: Any) -> None:
    """Segredos (o valor literal do token inicial configurado), caso
    à parte porque `redact` não recebe o token inicial: com um token inicial que não tem a
    forma `cq_…`, o rótulo e a justificativa que o contêm saem e ficam gravados com `[segredo]`."""
    from fastapi.testclient import TestClient

    exp_label = "script usa [segredo] hoje"
    exp_reason = "autorizado por [segredo]"
    with TestClient(app_factory(admin_token=LITERAL_ADMIN), raise_server_exceptions=False) as c:
        z = _register(c, ZEFIRO, token=LITERAL_ADMIN)
        _, created = create_token(c, LITERAL_ADMIN, ["people:read"], label=f"script usa {LITERAL_ADMIN} hoje")
        assert created["label"] == exp_label
        listed = _list_tokens(c, token=LITERAL_ADMIN)
        assert listed.json() == {"items": [_token_item(created, exp_label)]}
        _reveal(c, [z["alias"]], f"autorizado por {LITERAL_ADMIN}", token=LITERAL_ADMIN)
        events = _events(c, "token.created", "identity.revealed", token=LITERAL_ADMIN)

    assert events == [
        _token_created_event(created, exp_label),
        _revealed_event(iso(clock.now()), [z["pseudo_id"]], exp_reason),
    ]
    assert _stored_label(sql, created["token_id"]) == exp_label
    assert [data for (_, data) in _stored_free_text_events(sql)] == [e["data"] for e in events]
    for text in (listed.text, json.dumps(events, ensure_ascii=False),
                 *[row[0] for row in sql.execute("SELECT data::text FROM events")],
                 *[row[0] for row in sql.execute("SELECT label FROM api_tokens")]):
        assert find_secrets(text, [LITERAL_ADMIN]) == []


# ---------------------------------------------------------------------------------------------
# Desempate
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("first_prefix", "second_prefix"), [("fedcba", "abcdef"), ("abcdef", "fedcba")],
                         ids=["primeira-com-apelido-maior", "primeira-com-apelido-menor"])
def test_tie_break_uses_first_registered_person(client: Any, sql: Any, clock: Any, monkeypatch: Any,
                                                first_prefix: str, second_prefix: str) -> None:
    """Em empate total, vale a pessoa de menor `people.id`: duas pessoas
    com o mesmo nome → o texto com esse nome sai e fica gravado com o apelido da cadastrada
    primeiro, qualquer que seja a ordem dos apelidos e dos `pseudo_id`."""
    forced = [uuid4_with_prefix(first_prefix), uuid4_with_prefix(second_prefix)]
    pending = iter(forced)
    monkeypatch.setattr("cq_server.people.generate_pseudo_id", lambda: next(pending))
    first = register_person(client, ADMIN_TOKEN, name=HOMONYM, ra="9900770001")
    second = register_person(client, ADMIN_TOKEN, name=HOMONYM, ra="9900770002")
    # Pré-condição: o gerador substituído valeu, e a ordem de cadastro difere da dos apelidos.
    assert [first["pseudo_id"], second["pseudo_id"]] == [str(u) for u in forced]
    assert first["alias"] != second["alias"]

    m_first = marker(first["alias"])
    exp_label = f"token de {m_first}"
    exp_reason = f"homônimo {m_first} conferido"
    _, created = create_token(client, ADMIN_TOKEN, ["people:read"], label=f"token de {HOMONYM}")
    assert created["label"] == exp_label
    assert _list_tokens(client).json() == {"items": [_token_item(created, exp_label)]}
    _reveal(client, [second["alias"]], f"homônimo {HOMONYM} conferido")

    assert _events(client, "token.created", "identity.revealed") == [
        _token_created_event(created, exp_label),
        _revealed_event(iso(clock.now()), [second["pseudo_id"]], exp_reason),
    ]
    assert _stored_label(sql, created["token_id"]) == exp_label
    assert [data for (_, data) in _stored_free_text_events(sql)] == [
        {"token_id": created["token_id"], "label": exp_label, "permissions": ["people:read"]},
        {"client": "cli", "revealed": [second["pseudo_id"]], "not_found_count": 0, "reason": exp_reason},
    ]
