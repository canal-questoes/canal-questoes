"""Ativação do oferecimento pela API, `bots.activate`.

Cobre a ativação; a transição repetida; a ordem
das verificações (`bot_required`; `invalid_state` com `state`; `invalid_confirmation`) e as respostas;
os efeitos da ativação na mesma transação (`activated_at`, código de arquivamento
pendente limpo, evento `offering.state_changed` e aviso `bot`, entregue só no COMMIT).

Convenções: corpo inteiro com `==` e `assert_matches` em toda resposta de sucesso;
envelope de erro exato por `assert_error`; horários pelo `FakeClock` e `iso(dt)`; ids internos e
`seq` nunca como literal. Nenhum worker roda aqui (a posse e a consulta estão em
`test_bot_lease.py`, `test_reception.py` e `test_worker_bots.py`): a Bot API falsa só atende o
`getMe` do registro, e o perfil do bot fica `pending`. O bot sem acesso é produzido pela função de
posse (`bots.mark_no_access`), o mesmo caminho da perda de acesso.
"""

from __future__ import annotations

import time
import urllib.parse
import uuid
from typing import Any

import psycopg
from psycopg import sql as pgsql

from cq_server.notices import ConfigNotice, NoticeListener
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_token,
    offering_ref,
    register_bot,
)
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches

# Gatilho de restrição adiado do teste do aviso: o COMMIT de um `offering.state_changed` falha.
FAIL_TRIGGER = "cq_test_fail_activation_commit"
FORCED_FAILURE = "falha forcada no commit da ativacao"


# --- auxiliares ----------------------------------------------------------------------------------


def _path(ref: str, suffix: str = "") -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}{suffix}"


def _activate(client: Any, ref: str, token: str = ADMIN_TOKEN) -> Any:
    return client.post(_path(ref, "/bot/activate"), headers=auth(token))


def _get(client: Any, ref: str) -> dict:
    resp = client.get(_path(ref), headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"consulta falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


def _close(client: Any, ref: str) -> dict:
    resp = client.post(_path(ref, "/close"), headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"encerramento falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.close", 200, body)
    return body


def _prepare(client: Any, ref: str) -> dict:
    resp = client.post(_path(ref, "/archive/prepare"), headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"preparo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.prepare_archive", 200, body)
    return body


def _confirm(client: Any, ref: str, code: str) -> Any:
    return client.post(_path(ref, "/archive"), json={"confirmation_code": code},
                       headers=auth(ADMIN_TOKEN))


def _archive(client: Any, ref: str) -> dict:
    """Arquivamento em duas fases (preparar e confirmar com o código recebido)."""
    code = _prepare(client, ref)["confirmation_code"]
    resp = _confirm(client, ref, code)
    assert resp.status_code == 200, f"arquivamento falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.archive", 200, body)
    return body


def _with_bot(client: Any, bot_api: Any, **fields: Any) -> tuple[dict, int]:
    """Oferecimento em rascunho com um bot registrado na Bot API falsa: (corpo do cadastro, bot)."""
    offering = create_offering(client, ADMIN_TOKEN, **fields)
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), bot_api.token_of(bot))
    return offering, bot


def _summary(bot_api: Any, bot: int, *, state: str = "registered") -> dict:
    """O resumo `bot` do `Offering`, com o perfil ainda `pending` (sem worker)."""
    return {"username": bot_api.state(bot).username, "telegram_bot_id": bot, "state": state,
            "profile_state": "pending"}


def _bot_row(sql: psycopg.Connection, offering: dict) -> int:
    """O id interno do bot do oferecimento (só para chamar a função de posse; nunca comparado)."""
    row = sql.execute(
        "SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.offering_id = %s",
        (uuid.UUID(offering["offering_id"]),)).fetchone()
    assert row is not None, "o oferecimento não tem bot no banco"
    return row[0]


def _lose_access(db_url: str, sql: psycopg.Connection, clock: Any, offering: dict) -> None:
    """Deixa o bot `no_access` (`token_rejected`) pela função de posse."""
    from cq_server.bots import mark_no_access

    bot_id = _bot_row(sql, offering)
    with psycopg.connect(db_url) as conn:
        opened = mark_no_access(conn, bot_id=bot_id, token_version=1, problem="token_rejected",
                                now=clock.now())
        conn.commit()
    assert opened is True


def _state_changes(client: Any) -> list[dict]:
    """Os eventos `offering.state_changed`, sem o `seq` (conferido por crescimento)."""
    items = all_events(client, ADMIN_TOKEN, "offering.state_changed")
    seqs = [e["seq"] for e in items]
    assert all(a < b for a, b in zip(seqs, seqs[1:])), seqs
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _state_change(offering: dict, from_state: str, to_state: str, at: Any) -> dict:
    return {"type": "offering.state_changed", "occurred_at": iso(at), "actor_token_id": "bootstrap",
            "data": {"offering_id": offering["offering_id"], "from_state": from_state,
                     "to_state": to_state}}


def _collect(listener: NoticeListener, want: int, within: float) -> list[ConfigNotice]:
    """Soma chamadas de `wait` até ter `want` avisos ou passar `within` segundos (espera real)."""
    received: list[ConfigNotice] = []
    deadline = time.monotonic() + within
    while len(received) < want:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        received.extend(listener.wait(remaining))
    return received


def _plpgsql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _install_commit_failure(sql: psycopg.Connection, event_type: str) -> None:
    """Gatilho de restrição adiado: o COMMIT de toda transação com um evento `event_type` falha."""
    body = (f"BEGIN IF NEW.type = {_plpgsql_literal(event_type)} THEN "
            f"RAISE EXCEPTION '%', {_plpgsql_literal(FORCED_FAILURE)}; END IF; RETURN NULL; END")
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}")
                .format(name, pgsql.Literal(body)))
    sql.execute(pgsql.SQL("CREATE CONSTRAINT TRIGGER {} AFTER INSERT ON events DEFERRABLE "
                          "INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {}()").format(name, name))
    sql.execute(pgsql.SQL("ALTER TABLE events ENABLE ALWAYS TRIGGER {}").format(name))


def _remove_commit_failure(sql: psycopg.Connection) -> None:
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("DROP TRIGGER {} ON events").format(name))
    sql.execute(pgsql.SQL("DROP FUNCTION {}()").format(name))


# --- rascunho → ativo ------------------------------------------------------------------------------


def test_activate_draft_with_registered_bot(client: Any, bot_api: Any, clock: Any) -> None:
    """`draft` com bot `registered` → 200 com o `Offering`
    inteiro exato (`state: active`, `activated_at` = agora, `closed_at`/`archived_at` nulos, resumo do
    bot); a consulta devolve o mesmo corpo; um único evento `offering.state_changed`
    `{draft → active}` com `occurred_at` exato e `actor_token_id` `bootstrap`. O `ref` pelo
    `offering_id` ativa do mesmo jeito que pelo código."""
    offering, bot = _with_bot(client, bot_api, code="ativa-por-codigo")
    other, other_bot = _with_bot(client, bot_api, code="ativa-por-id")
    t_act = clock.advance(90)

    resp = _activate(client, offering_ref(offering))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches("bots.activate", 200, body)
    expected = {**offering, "state": "active", "activated_at": iso(t_act), "closed_at": None,
                "archived_at": None, "bot": _summary(bot_api, bot)}
    assert body == expected
    assert _get(client, offering_ref(offering)) == expected
    assert _get(client, offering_ref(offering, by="id")) == expected

    t_other = clock.advance(30)
    by_id = activate_bot(client, ADMIN_TOKEN, offering_ref(other, by="id"))
    other_expected = {**other, "state": "active", "activated_at": iso(t_other), "closed_at": None,
                      "archived_at": None, "bot": _summary(bot_api, other_bot)}
    assert by_id == other_expected
    assert _get(client, offering_ref(other)) == other_expected

    assert _state_changes(client) == [_state_change(offering, "draft", "active", t_act),
                                      _state_change(other, "draft", "active", t_other)]


def test_repeated_activation_changes_nothing_even_without_access(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Transição repetida (`active` → 200
    sem mudança nem evento, mesmo com o bot `no_access`): ativar de novo, com o relógio adiantado,
    devolve o mesmo `Offering` (o `activated_at` não muda); depois da perda de acesso, a ativação
    repetida continua 200, agora com o resumo `bot.state = no_access`; em todo o fluxo sai um só
    `offering.state_changed`."""
    offering, bot = _with_bot(client, bot_api)
    t_act = clock.advance(10)
    first = activate_bot(client, ADMIN_TOKEN, offering_ref(offering))
    expected = {**offering, "state": "active", "activated_at": iso(t_act), "closed_at": None,
                "archived_at": None, "bot": _summary(bot_api, bot)}
    assert first == expected

    clock.advance(3600)
    again = _activate(client, offering_ref(offering))
    assert again.status_code == 200, again.text[:300]
    assert_matches("bots.activate", 200, again.json())
    assert again.json() == expected

    _lose_access(db_url, sql, clock, offering)
    clock.advance(60)
    without_access = _activate(client, offering_ref(offering, by="id"))
    assert without_access.status_code == 200, without_access.text[:300]
    assert_matches("bots.activate", 200, without_access.json())
    assert without_access.json() == {**expected, "bot": _summary(bot_api, bot, state="no_access")}
    assert _get(client, offering_ref(offering)) == without_access.json()

    assert _state_changes(client) == [_state_change(offering, "draft", "active", t_act)]


# --- recusas ---------------------------------------------------------------------------------------


def test_draft_without_bot_or_without_access_requires_bot(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str) -> None:
    """`draft` sem bot → 409 `bot_required` (sem
    `fields` nem `state`); `draft` com o bot `no_access` → 409 `bot_required`; nos dois, o
    `Offering` continua igual e nenhum `offering.state_changed` sai. Ordem das verificações
    : um token sem `bots:manage` recebe 403 com `permission: bots:manage` antes da
    regra, e nada muda."""
    bare = create_offering(client, ADMIN_TOKEN)
    lost, lost_bot = _with_bot(client, bot_api)
    _lose_access(db_url, sql, clock, lost)
    lost_before = _get(client, offering_ref(lost))
    assert lost_before == {**lost, "bot": _summary(bot_api, lost_bot, state="no_access")}
    secret, _token = create_token(client, ADMIN_TOKEN, ["offerings:read", "offerings:write"],
                                  label="sem bots manage")
    clock.advance(10)

    assert_error(_activate(client, offering_ref(bare)), 409, "bot_required")
    assert_error(_activate(client, offering_ref(bare, by="id")), 409, "bot_required")
    assert_error(_activate(client, offering_ref(lost)), 409, "bot_required")
    assert_error(_activate(client, offering_ref(bare), token=secret), 403, "forbidden",
                 permission="bots:manage")
    assert_error(_activate(client, offering_ref(lost), token=secret), 403, "forbidden",
                 permission="bots:manage")

    assert _get(client, offering_ref(bare)) == bare
    assert _get(client, offering_ref(lost)) == lost_before
    assert _state_changes(client) == []


def test_closed_and_archived_offerings_are_invalid_state(client: Any, bot_api: Any,
                                                         clock: Any) -> None:
    """Nenhuma transição volta atrás (ordem: `closed` e
    `archived` → 409 `invalid_state` com o estado atual **antes** de `bot_required`): encerrado →
    `state: closed`; arquivado depois de encerrado → `state: archived`; arquivado a partir do
    rascunho **sem bot** → `invalid_state` (não `bot_required`); o `Offering` não muda e nenhuma
    transição nova é gravada pela ativação."""
    closed, b1 = _with_bot(client, bot_api)
    t_act = clock.advance(10)
    activate_bot(client, ADMIN_TOKEN, offering_ref(closed))
    t_close = clock.advance(10)
    closed_body = _close(client, offering_ref(closed))
    assert closed_body == {**closed, "state": "closed", "activated_at": iso(t_act),
                           "closed_at": iso(t_close), "archived_at": None,
                           "bot": _summary(bot_api, b1)}

    archived, b2 = _with_bot(client, bot_api)
    t_act2 = clock.advance(10)
    activate_bot(client, ADMIN_TOKEN, offering_ref(archived))
    t_close2 = clock.advance(10)
    _close(client, offering_ref(archived))
    t_arch2 = clock.advance(10)
    archived_body = _archive(client, offering_ref(archived))
    assert archived_body == {**archived, "state": "archived", "activated_at": iso(t_act2),
                             "closed_at": iso(t_close2), "archived_at": iso(t_arch2),
                             "bot": _summary(bot_api, b2)}

    bare = create_offering(client, ADMIN_TOKEN)
    t_arch3 = clock.advance(10)
    bare_archived = _archive(client, offering_ref(bare))
    assert bare_archived == {**bare, "state": "archived", "archived_at": iso(t_arch3)}
    before = _state_changes(client)
    clock.advance(60)

    assert_error(_activate(client, offering_ref(closed)), 409, "invalid_state", state="closed")
    assert_error(_activate(client, offering_ref(archived)), 409, "invalid_state", state="archived")
    assert_error(_activate(client, offering_ref(archived, by="id")), 409, "invalid_state",
                 state="archived")
    assert_error(_activate(client, offering_ref(bare)), 409, "invalid_state", state="archived")

    assert _get(client, offering_ref(closed)) == closed_body
    assert _get(client, offering_ref(archived)) == archived_body
    assert _get(client, offering_ref(bare)) == bare_archived
    assert before == [
        _state_change(closed, "draft", "active", t_act),
        _state_change(closed, "active", "closed", t_close),
        _state_change(archived, "draft", "active", t_act2),
        _state_change(archived, "active", "closed", t_close2),
        _state_change(archived, "closed", "archived", t_arch2),
        _state_change(bare, "draft", "archived", t_arch3),
    ]
    assert _state_changes(client) == before


def test_activation_invalidates_the_pending_archive_code(client: Any, bot_api: Any,
                                                         clock: Any) -> None:
    """Preparar de novo invalida (a ativação limpa o código de
    arquivamento pendente, que deixa de valer): preparar no rascunho, ativar,
    encerrar e confirmar com o código antigo, ainda dentro dos 10 minutos → 409
    `invalid_confirmation`, sem mudança nem evento; controle positivo: uma preparação nova no
    encerrado arquiva."""
    offering, bot = _with_bot(client, bot_api)
    old_code = _prepare(client, offering_ref(offering))["confirmation_code"]
    t_act = clock.advance(60)
    activate_bot(client, ADMIN_TOKEN, offering_ref(offering))
    t_close = clock.advance(60)
    closed_body = _close(client, offering_ref(offering))
    assert closed_body == {**offering, "state": "closed", "activated_at": iso(t_act),
                           "closed_at": iso(t_close), "archived_at": None,
                           "bot": _summary(bot_api, bot)}
    clock.advance(60)  # 3 minutos depois do preparo: o código antigo ainda estaria na validade

    assert_error(_confirm(client, offering_ref(offering), old_code), 409, "invalid_confirmation")
    assert _get(client, offering_ref(offering)) == closed_body
    assert _state_changes(client) == [_state_change(offering, "draft", "active", t_act),
                                      _state_change(offering, "active", "closed", t_close)]

    t_arch = clock.advance(5)
    archived = _archive(client, offering_ref(offering))
    assert archived == {**closed_body, "state": "archived", "archived_at": iso(t_arch)}
    assert _state_changes(client)[-1] == _state_change(offering, "closed", "archived", t_arch)


# --- aviso aos workers -----------------------------------------------------------------------------


def test_bot_notice_is_delivered_only_after_the_activation_commits(
        client: Any, bot_api: Any, clock: Any, sql: psycopg.Connection, db_url: str) -> None:
    """Avisar os workers depois de gravar (aviso `bot` com a
    chave = `offering_id`, só depois do commit): com o COMMIT da ativação falhando (gatilho adiado
    no evento `offering.state_changed`), a resposta é 500 `internal`, o oferecimento continua em
    rascunho e nenhum aviso chega; sem a falha, a ativação responde 200 e chega exatamente
    `ConfigNotice(topic="bot", key=<offering_id>)`."""
    offering, bot = _with_bot(client, bot_api)
    draft = _get(client, offering_ref(offering))
    assert draft == {**offering, "bot": _summary(bot_api, bot)}
    notice = ConfigNotice(topic="bot", key=offering["offering_id"])
    clock.advance(10)

    with NoticeListener(db_url) as listener:
        _install_commit_failure(sql, "offering.state_changed")
        try:
            failed = _activate(client, offering_ref(offering))
        finally:
            _remove_commit_failure(sql)
        assert_error(failed, 500, "internal")
        assert listener.wait(1.0) == [], "o aviso saiu de uma ativação que não foi confirmada"
        assert _get(client, offering_ref(offering)) == draft
        assert _state_changes(client) == []

        t_act = clock.advance(10)
        assert activate_bot(client, ADMIN_TOKEN, offering_ref(offering)) == {
            **draft, "state": "active", "activated_at": iso(t_act)}
        assert _collect(listener, 1, 5) == [notice]
        assert listener.wait(0.5) == []

    assert _state_changes(client) == [_state_change(offering, "draft", "active", t_act)]
