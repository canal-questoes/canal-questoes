"""API do expurgo: preparar e confirmar.

Cobre a preparação e a confirmação do expurgo pela API, com as recusas, o resultado gravado e o `purged_at`
no `Offering`: os códigos `purge_pending` e `purge_changed`, a ordem das verificações, os pedidos e as
respostas das duas rotas, as invariantes do oferecimento arquivado e as contagens do impacto e do pendente.

Montagem: a semente do expurgo (`tests/helpers/purge.py`, `seed_purge_scenario`) dá um oferecimento
arquivado, sem trabalho pendente, com as contagens esperadas calculadas à mão (`scenario.impact`,
`scenario.outcome`); `make_pending` cria uma unidade de cada classe de trabalho pendente.
Horários pelo `FakeClock` e `iso(dt)`; ids internos, `seq` e códigos de confirmação nunca como literal (só a
forma `^[A-Za-z0-9_-]{22}$` e o resumo SHA-256 guardado). "Nada mudou" é o instantâneo das seis tabelas da
lista do expurgo e de `offerings` (`snapshot_purge_tables`) e nenhum evento novo depois do cursor.

Os eventos são lidos pela API (`events.list`, esquema congelado) depois de um cursor tirado do banco (o maior
`seq`), com todos os tipos de `EventType` mais os dois do expurgo.

No esqueleto, as rotas `offerings.prepare_purge` e `offerings.purge`
não existem (404/405 no lugar do status esperado) e `cq_server.purge` lança `NotImplementedError`: cada função
falha na primeira chamada às rotas ou ao núcleo do expurgo. As asserções sobre o que o esqueleto já tem (o
inventário das regras do varredor, as recusas do arquivado, as contagens do ajudante) ficam na mesma
função de um caso vermelho.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import logging
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    assert_matches_tolerant,
    auth,
    create_offering,
    create_token,
    register_bot,
    register_person,
)
from tests.helpers.clock import iso
from tests.helpers.contract import PENDING_KEYS, assert_error, assert_matches, assert_no_echo
from tests.helpers.db import scan_all_tables
from tests.helpers.purge import (
    FAILED_PROFILE_KIND,
    PENDING_KINDS,
    _drain_profiles,
    make_pending,
    pending_counts,
    seed_purge_scenario,
    snapshot_purge_tables,
)

ADMIN = auth(ADMIN_TOKEN)
CODE_TTL = timedelta(minutes=10)  # PURGE_CODE_TTL
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
PURGE_EVENT_TYPES = ("offering.purge_prepared", "offering.purged")
# As três colunas do código pendente: mudam na preparação, e só elas.
CODE_COLUMNS = ("purge_code_sha256", "purge_code_expires_at", "purge_prepared_counts")

# Inventário das regras do varredor sem teto de rodadas (os assuntos devidos contam como pendência): as que
# contam em `freeze` e em `due` e as impossíveis num arquivado. Uma regra nova sem teto fora destas listas
# precisa ser classificada (e o teste falha até lá).
COUNTED_IN_FREEZE = frozenset({"report.freeze"})
COUNTED_IN_DUE = frozenset({"delivery.reveal", "notice.send", "notice.dispatch_rejected", "notice.dispatch_cancelled",
                            "notice.question_failed", "notice.access_episode", "notice.invite_batch",
                            "notice.invite_window"})
IMPOSSIBLE_WHEN_ARCHIVED = frozenset({"dispatch.expire", "dispatch.open", "dispatch.close", "delivery.prepare",
                                      "notice.dispatch_opened", "submission.grade", "digest.daily"})


# --------------------------------------------------------------------------------------------------
# Apoio


def _prepare_path(ref: str) -> str:
    return f"/v1/offerings/{ref}/purge/prepare"


def _purge_path(ref: str) -> str:
    return f"/v1/offerings/{ref}/purge"


def _prepare(client: Any, ref: str, headers: dict | None = None) -> Any:
    return client.post(_prepare_path(ref), headers=ADMIN if headers is None else headers)


def _confirm(client: Any, ref: str, code: Any, headers: dict | None = None) -> Any:
    return client.post(_purge_path(ref), json={"confirmation_code": code},
                       headers=ADMIN if headers is None else headers)


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _prepared(client: Any, ref: str, headers: dict | None = None) -> dict:
    body = _ok(_prepare(client, ref, headers), "offerings.prepare_purge")
    assert CONFIRMATION_RE.fullmatch(body["confirmation_code"]), "código de confirmação fora da forma"
    return body


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _impact(impact: Any) -> dict:
    return dataclasses.asdict(impact)


def _outcome(outcome: Any) -> dict:
    return dataclasses.asdict(outcome)


def _ref(scenario: Any) -> dict:
    return {"offering_id": scenario.offering_id, "code": scenario.code}


def _all_types() -> tuple[str, ...]:
    from cq_server.events import EventType

    return tuple(dict.fromkeys([*(t.value for t in EventType), *PURGE_EVENT_TYPES]))


def _cursor(sql: psycopg.Connection) -> int:
    return int(sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos depois do cursor, pela API, em ordem de `seq`, sem o `seq`."""
    events = [e for e in all_events(client, ADMIN_TOKEN, *_all_types()) if e["seq"] > cursor]
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _event(event_type: str, at: Any, data: dict, actor: str | None = "bootstrap") -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _prepared_event(scenario: Any, at: Any, impact: dict, actor: str | None = "bootstrap") -> dict:
    return _event("offering.purge_prepared", at,
                  {"offering_id": scenario.offering_id, "expires_at": iso(at + CODE_TTL), **impact}, actor)


def _purged_event(scenario: Any, at: Any, outcome: dict, actor: str | None = "bootstrap") -> dict:
    return _event("offering.purged", at, {"offering_id": scenario.offering_id, **outcome}, actor)


def _stored(sql: psycopg.Connection, code: str) -> dict:
    """As colunas do expurgo de `offerings` e o estado."""
    row = sql.execute("SELECT state, purged_at, purge_result, purge_code_sha256, purge_code_expires_at, "
                      "purge_prepared_counts FROM offerings WHERE code = %s", (code,)).fetchone()
    assert row is not None
    return dict(zip(("state", "purged_at", "purge_result", "purge_code_sha256", "purge_code_expires_at",
                     "purge_prepared_counts"), row))


def _without_code_columns(snapshot: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {table: [({k: v for k, v in row.items() if k not in CODE_COLUMNS} if table == "offerings" else row)
                    for row in rows]
            for table, rows in snapshot.items()}


def _get_offering(client: Any, ref: str) -> dict:
    return _ok(client.get(f"/v1/offerings/{ref}", headers=ADMIN), "offerings.get")


def _with_bot(client: Any, bot_api: Any) -> tuple[dict, dict]:
    offering = create_offering(client, ADMIN_TOKEN)
    telegram_bot_id = bot_api.add_bot()
    bot = register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(telegram_bot_id))
    return offering, bot


def _post_tolerant(client: Any, path: str, op_id: str, body: Any = None) -> dict:
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    resp = client.post(path, headers=ADMIN, **kwargs)
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    result = resp.json()
    assert_matches_tolerant(op_id, 200, result)
    return result


def _archive(client: Any, ref: str) -> dict:
    """Arquiva pelas rotas do oferecimento (a partir de `draft` ou `closed`), tolerando a ausência de `purged_at`."""
    prepared = _post_tolerant(client, f"/v1/offerings/{ref}/archive/prepare", "offerings.prepare_archive")
    return _post_tolerant(client, f"/v1/offerings/{ref}/archive", "offerings.archive",
                          {"confirmation_code": prepared["confirmation_code"]})


def _empty_archived(client: Any) -> dict:
    """Um oferecimento arquivado direto do rascunho, sem bot, pessoas nem contato (impacto zero)."""
    created = create_offering(client, ADMIN_TOKEN)
    return _archive(client, created["code"])


ZERO_IMPACT = {"identities_to_delete": 0, "identities_kept": 0, "endpoints": 0, "updates": 0,
               "contact_email_set": False}


def _zero_pending(**counts: int) -> dict:
    return {**dict.fromkeys(PENDING_KEYS, 0), **counts}


# --------------------------------------------------------------------------------------------------
# Ordem das verificações (como na API base)


def test_check_order_401_route_400_403_then_rules(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Ordem das verificações (401 → 404/405 da rota → 400 (forma de `ref`, consulta e corpo) → 403 → regras): sem
    token → 401 nas duas rotas; método errado no caminho do expurgo → 405; `ref` malformado → 400 `[{ref,
    invalid_format}]` mesmo sem a permissão; corpo na preparação → 400 `[{body, unknown_field}]`; token sem
    `people:write` → 403 antes do 404 do oferecimento; com a permissão, o oferecimento inexistente → 404. Nenhum
    evento e nada muda."""
    archived = _empty_archived(client)
    ref = archived["code"]
    weak, _ = create_token(client, ADMIN_TOKEN, ["offerings:write", "offerings:read"])
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    for path, body in ((_prepare_path(ref), None), (_purge_path(ref), {"confirmation_code": "x" * 22})):
        kwargs = {} if body is None else {"json": body}
        assert_error(client.post(path, **kwargs), 401, "unauthenticated")
        assert_error(client.get(path, headers=ADMIN), 405, "method_not_allowed")
        assert_error(client.put(path, headers=ADMIN), 405, "method_not_allowed")
    for path, body in ((_prepare_path("REF_SENTINELA_MAL"), None),
                       (_purge_path("REF_SENTINELA_MAL"), {"confirmation_code": "x" * 22})):
        kwargs = {} if body is None else {"json": body}
        resp = client.post(path, headers=auth(weak), **kwargs)
        assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
        assert_no_echo(resp, "REF_SENTINELA_MAL")
    resp = client.post(_prepare_path(ref), json={"campo_sentinela_corpo": 1}, headers=auth(weak))
    assert_error(resp, 400, "invalid_request", fields=[("body", "unknown_field")])
    assert_no_echo(resp, "campo_sentinela_corpo")
    for resp in (_prepare(client, ref, auth(weak)), _confirm(client, ref, "x" * 22, auth(weak)),
                 _prepare(client, "codigo-inexistente", auth(weak)),
                 _confirm(client, "codigo-inexistente", "x" * 22, auth(weak))):
        assert_error(resp, 403, "forbidden", permission="people:write")
    assert_error(_prepare(client, "codigo-inexistente"), 404, "not_found")
    assert_error(_confirm(client, "codigo-inexistente", "x" * 22), 404, "not_found")
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []


# --------------------------------------------------------------------------------------------------
# Estado


@pytest.mark.parametrize("state", ["draft", "active", "closed"])
def test_prepare_and_confirm_outside_archived_are_invalid_state(state: str, client: Any, sql: psycopg.Connection,
                                                                clock: Any, bot_api: Any) -> None:
    """Preparar ou confirmar com o oferecimento em `draft`, `active` ou `closed` → 409
    `invalid_state` com `state`: nos três estados, as duas rotas (a confirmação com um código qualquer) → 409
    `invalid_state` com o estado atual; nenhuma coluna do código gravada, nada muda e nenhum evento."""
    offering, _ = _with_bot(client, bot_api)
    ref = offering["code"]
    if state in ("active", "closed"):
        activate_bot(client, ADMIN_TOKEN, ref)
    if state == "closed":
        _post_tolerant(client, f"/v1/offerings/{ref}/close", "offerings.close")
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    assert_error(_prepare(client, ref), 409, "invalid_state", state=state)
    assert_error(_confirm(client, ref, secrets.token_urlsafe(16)), 409, "invalid_state", state=state)
    assert _stored(sql, ref) == {"state": state, "purged_at": None, "purge_result": None, "purge_code_sha256": None,
                                 "purge_code_expires_at": None, "purge_prepared_counts": None}
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []


def test_prepare_on_purged_offering_is_invalid_state_archived(client: Any, sql: psycopg.Connection,
                                                              clock: Any, bot_api: Any) -> None:
    """Preparar com o oferecimento já expurgado → 409 `invalid_state` (`state =
    archived`): depois do expurgo, preparar de novo é recusado com `archived`, sem código novo nem evento."""
    scenario = seed_purge_scenario(client, sql)
    prepared = _prepared(client, scenario.code)
    _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    stored = _stored(sql, scenario.code)
    cursor = _cursor(sql)

    clock.advance(30)
    assert_error(_prepare(client, scenario.code), 409, "invalid_state", state="archived")
    assert _stored(sql, scenario.code) == stored
    assert stored["purge_code_sha256"] is None and stored["purged_at"] is not None
    assert _events_after(client, cursor) == []


def test_rule_order_after_the_route_checks(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Ordem das regras do expurgo: 404 do oferecimento → (confirmação: já expurgado devolve 200 com o
    resultado gravado) → `invalid_state` → `purge_pending` → (confirmação) `invalid_confirmation` →
    `purge_changed`: com trabalho pendente e um código inventado, a confirmação dá `purge_pending` (antes do
    código); no oferecimento ativo com trabalho pendente, `invalid_state` (antes do pendente); no já expurgado
    com trabalho que aparece depois, a confirmação ainda devolve o resultado gravado (200)."""
    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None
    make_pending(sql, "outbox_pending", scenario)
    pending = _zero_pending(outbox=1)
    assert_error(_confirm(client, scenario.code, "codigo-inventado-sentinela"), 409, "purge_pending",
                 pending=pending)
    assert_error(_prepare(client, scenario.code), 409, "purge_pending", pending=pending)
    # O ativo (`O2`), com um update ainda pendente no bot dele: o estado vem antes do pendente.
    as_other = dataclasses.replace(scenario, offering_row=other.offering_row, bot_row=other.bot_row)
    make_pending(sql, "update_pending", as_other)
    assert pending_counts(sql, other.offering_row)["updates"] == 1
    assert_error(_prepare(client, other.code), 409, "invalid_state", state="active")
    assert_error(_confirm(client, other.code, "codigo-inventado-sentinela"), 409, "invalid_state", state="active")

    # Sem o pendente, o expurgo; depois, trabalho novo no bot do expurgado não muda o resultado gravado.
    sql.execute("UPDATE outbox_messages SET state = 'sent', sent_at = %s, attempts = 1, channel_ref = 'enviada' "
                "WHERE bot_id = %s AND state IN ('pending', 'leased')", (clock.now(), scenario.bot_row))
    prepared = _prepared(client, scenario.code)
    purged = _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    make_pending(sql, "update_pending", scenario)
    cursor = _cursor(sql)
    assert _ok(_confirm(client, scenario.code, "codigo-inventado-sentinela"), "offerings.purge") == purged
    assert _events_after(client, cursor) == []


# --------------------------------------------------------------------------------------------------
# Trabalho pendente


@pytest.mark.parametrize("kind", sorted(PENDING_KINDS))
def test_purge_pending_counts_each_class_in_isolation(kind: str, client: Any, sql: psycopg.Connection, clock: Any,
                                                      bot_api: Any) -> None:
    """As cinco classes de pendência, numa única instrução; o contrato (409 `purge_pending` com
    `pending`); a revelação devida com a tarefa `failed` e sem tarefa conta em `due`: cada unidade de
    `make_pending` sozinha dá exatamente `1` na sua classe e `0` nas demais, na preparação e na confirmação (com
    um código válido de antes do pendente), pela API e pelo núcleo (`purge.purge_pending`); o código continua
    guardado e nada muda. O contador próprio do ajudante (`pending_counts`) concorda."""
    from cq_server import purge

    scenario = seed_purge_scenario(client, sql)
    prepared = _prepared(client, scenario.code)
    stored = _stored(sql, scenario.code)
    klass = make_pending(sql, kind, scenario)
    assert klass == PENDING_KINDS[kind]
    expected = _zero_pending(**{klass: 1})
    assert pending_counts(sql, scenario.offering_row) == expected
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    assert_error(_prepare(client, scenario.code), 409, "purge_pending", pending=expected)
    assert_error(_confirm(client, scenario.code, prepared["confirmation_code"]), 409, "purge_pending",
                 pending=expected)
    assert purge.purge_pending(sql, scenario.offering_row) == purge.PurgePending(**expected)
    assert purge.purge_pending(sql, scenario.offering_row).total == 1
    assert _stored(sql, scenario.code) == stored
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []


def test_failed_profile_task_does_not_block(client: Any, sql: psycopg.Connection, clock: Any,
                                            bot_api: Any) -> None:
    """Uma tarefa `failed` sem assunto ainda devido (por exemplo, `bot.sync_profile`)
    não bloqueia: com a `bot.sync_profile` `failed` do bot do oferecimento, as cinco contagens ficam em 0 e a
    preparação é aceita com o impacto exato."""
    from cq_server import purge

    scenario = seed_purge_scenario(client, sql)
    assert make_pending(sql, FAILED_PROFILE_KIND, scenario) == ""
    assert pending_counts(sql, scenario.offering_row) == _zero_pending()
    assert purge.purge_pending(sql, scenario.offering_row) == purge.PurgePending(**_zero_pending())
    prepared = _prepared(client, scenario.code)
    assert prepared["impact"] == _impact(scenario.impact)


def test_sweeper_rule_inventory_and_due_reveal_without_task(client: Any, sql: psycopg.Connection, clock: Any,
                                                            bot_api: Any) -> None:
    """Toda regra do varredor sem teto de rodadas
    (`DISPATCH_RULES` e `REPORT_RULES`, `max_rounds = None`) conta em `freeze` ou em `due`, ou é impossível num arquivado;
    uma regra nova fora das listas falha aqui; as com teto (`telegram.update`, `dispatch.validate`,
    `preview.send`) ficam fora. Na mesma função, o caso vermelho: a revelação devida sem nenhuma tarefa
    (o worker parado ainda não a criou) bloqueia com `due = 1`."""
    from cq_server import scheduler

    rules = scheduler.DISPATCH_RULES + scheduler.REPORT_RULES
    uncapped = {rule.task_type for rule in rules if rule.max_rounds is None}
    capped = {rule.task_type for rule in rules if rule.max_rounds is not None}
    classified = COUNTED_IN_FREEZE | COUNTED_IN_DUE | IMPOSSIBLE_WHEN_ARCHIVED
    outside = uncapped - classified
    assert outside == set(), f"regra sem teto fora do inventário do expurgo: {outside}"
    assert classified - uncapped == set(), f"inventário com regra inexistente ou com teto: {classified - uncapped}"
    assert capped == {"telegram.update", "dispatch.validate", "preview.send"}
    assert not any("purge" in rule.task_type for rule in rules)

    scenario = seed_purge_scenario(client, sql)
    make_pending(sql, "reveal_without_task", scenario)
    assert_error(_prepare(client, scenario.code), 409, "purge_pending", pending=_zero_pending(due=1))


def test_archive_profile_task_counts_while_alive(client: Any, sql: psycopg.Connection, clock: Any,
                                                 bot_api: Any) -> None:
    """A tarefa de perfil do arquivamento (a `bot.sync_profile` `pending` que o arquivamento agenda conta em
    `tasks` enquanto viva e deixa de contar depois de drenada): um oferecimento com bot, ativado, encerrado e
    arquivado pelas rotas do oferecimento, sem worker: a preparação dá `purge_pending` com `tasks` igual ao número exato de
    `bot.sync_profile` vivas do bot (ao menos a do arquivamento) e 0 nas demais classes; drenadas pelo tratador
    do perfil do bot (como a semente), a preparação é aceita com o impacto zero."""
    offering, _ = _with_bot(client, bot_api)
    ref = offering["code"]
    activate_bot(client, ADMIN_TOKEN, ref)
    _post_tolerant(client, f"/v1/offerings/{ref}/close", "offerings.close")
    _archive(client, ref)
    bot_row = int(sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.code = %s",
                              (ref,)).fetchone()[0])
    alive = int(sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'bot.sync_profile' "
                            "AND state IN ('pending', 'leased') AND payload->>'bot_id' = %s",
                            (str(bot_row),)).fetchone()[0])
    assert alive >= 1, "o arquivamento não agendou a bot.sync_profile"

    assert_error(_prepare(client, ref), 409, "purge_pending", pending=_zero_pending(tasks=alive))
    assert _drain_profiles(sql, [bot_row]) == alive
    prepared = _prepared(client, ref)
    assert prepared["impact"] == ZERO_IMPACT


# --------------------------------------------------------------------------------------------------
# Preparação


def test_prepare_returns_the_exact_impact_and_stores_only_the_digest(client: Any, sql: psycopg.Connection,
                                                                     clock: Any, bot_api: Any,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    """a preparação devolve o corpo exato (o
    `OfferingRef`, o impacto calculado à mão pela semente, o código de 22 caracteres do alfabeto urlsafe e
    `expires_at = now + 10 min`); o banco guarda só o resumo (`purge_code_sha256 = sha256(código)`), o vencimento
    e o impacto em `purge_prepared_counts`; o código em claro não aparece em nenhuma coluna de nenhuma tabela
    (controle positivo: a mesma varredura acha um valor-sentinela numa tabela temporária), nem no evento, nem
    nos registros; nada mais muda (instantâneo sem as três colunas do código); um `offering.purge_prepared`
    exato, sem o código nem pseudônimo."""
    scenario = seed_purge_scenario(client, sql)
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)
    clock.advance(42)
    t0 = clock.now()

    with caplog.at_level(logging.DEBUG):
        prepared = _prepared(client, scenario.code)
    code = prepared["confirmation_code"]
    assert prepared == {"offering": _ref(scenario), "impact": _impact(scenario.impact), "confirmation_code": code,
                        "expires_at": iso(t0 + CODE_TTL)}
    assert _stored(sql, scenario.code) == {
        "state": "archived", "purged_at": None, "purge_result": None, "purge_code_sha256": _sha256(code),
        "purge_code_expires_at": t0 + CODE_TTL, "purge_prepared_counts": _impact(scenario.impact)}
    assert _without_code_columns(snapshot_purge_tables(sql)) == _without_code_columns(before)

    assert [entry for entry in scan_all_tables(sql) if code in entry[2]] == []
    sentinel = "sentinela-varredura-" + secrets.token_urlsafe(16)
    sql.execute("CREATE TEMP TABLE cq_controle_varredura (valor text)")
    try:
        sql.execute("INSERT INTO cq_controle_varredura (valor) VALUES (%s)", (sentinel,))
        assert [entry for entry in scan_all_tables(sql) if sentinel in entry[2]] == [
            ("cq_controle_varredura", "valor", sentinel)]
    finally:
        sql.execute("DROP TABLE cq_controle_varredura")
    assert code not in caplog.text

    events = _events_after(client, cursor)
    assert events == [_prepared_event(scenario, t0, _impact(scenario.impact))]
    text = json.dumps(events, ensure_ascii=False)
    assert code not in text and _sha256(code) not in text
    for person in scenario.people.values():
        assert person.pseudo_id not in text and person.alias not in text


def test_prepare_again_invalidates_the_previous_code(client: Any, sql: psycopg.Connection, clock: Any,
                                                     bot_api: Any) -> None:
    """Na preparação (preparar de novo invalida o código anterior): a segunda preparação
    troca o resumo e o vencimento; o primeiro código dá 409 `invalid_confirmation` sem mudança; o segundo
    expurga. Um `offering.purge_prepared` por preparação."""
    scenario = seed_purge_scenario(client, sql)
    cursor = _cursor(sql)
    t1 = clock.now()
    first = _prepared(client, scenario.code)
    clock.advance(60)
    t2 = clock.now()
    second = _prepared(client, scenario.code)
    assert first["confirmation_code"] != second["confirmation_code"]
    assert second["expires_at"] == iso(t2 + CODE_TTL)
    stored = _stored(sql, scenario.code)
    assert stored["purge_code_sha256"] == _sha256(second["confirmation_code"])
    before = snapshot_purge_tables(sql)

    assert_error(_confirm(client, scenario.code, first["confirmation_code"]), 409, "invalid_confirmation")
    assert snapshot_purge_tables(sql) == before
    purged = _ok(_confirm(client, scenario.code, second["confirmation_code"]), "offerings.purge")
    assert purged["result"] == _outcome(scenario.outcome)
    impact = _impact(scenario.impact)
    assert _events_after(client, cursor) == [_prepared_event(scenario, t1, impact),
                                             _prepared_event(scenario, t2, impact),
                                             _purged_event(scenario, t2, _outcome(scenario.outcome))]


@pytest.mark.parametrize("case", ["vencido", "substituido", "de-outro-oferecimento", "inventado",
                                  "sem-preparacao"])
def test_invalid_confirmation_codes_change_nothing(case: str, client: Any, sql: psycopg.Connection, clock: Any,
                                                   bot_api: Any) -> None:
    """Confirmar com código vencido, substituído, de outro oferecimento, inventado ou sem
    preparação → 409 `invalid_confirmation`; a conferência do prazo do código na confirmação (`now < purge_code_expires_at`): o
    vencido no instante exato de `expires_at` (`FakeClock` + 10 min); o substituído por uma nova preparação; o
    código válido de outro oferecimento arquivado; um inventado da mesma forma; e um qualquer sem preparação.
    Nada muda, nenhum evento; o código guardado (quando há) continua o mesmo."""
    scenario = seed_purge_scenario(client, sql)
    code: str
    if case == "sem-preparacao":
        code = secrets.token_urlsafe(16)
    else:
        prepared = _prepared(client, scenario.code)
        code = prepared["confirmation_code"]
        if case == "vencido":
            clock.advance(CODE_TTL.total_seconds())
        elif case == "substituido":
            _prepared(client, scenario.code)
        elif case == "de-outro-oferecimento":
            other = _empty_archived(client)
            code = _prepared(client, other["code"])["confirmation_code"]
        elif case == "inventado":
            code = secrets.token_urlsafe(16)
    stored = _stored(sql, scenario.code)
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    resp = _confirm(client, scenario.code, code)
    assert_error(resp, 409, "invalid_confirmation")
    assert_no_echo(resp, code)
    assert _stored(sql, scenario.code) == stored
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []


INVALID_CONFIRM_BODIES = [
    pytest.param({}, [("confirmation_code", "missing")], id="ausente"),
    pytest.param({"confirmation_code": ""}, [("confirmation_code", "too_short")], id="vazio"),
    pytest.param({"confirmation_code": "c" * 65}, [("confirmation_code", "too_long")], id="65-caracteres"),
    pytest.param({"confirmation_code": "codigo-qualquer", "campo_extra_sentinela": 1},
                 [("body", "unknown_field")], id="campo-extra"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_CONFIRM_BODIES)
def test_confirm_rejects_invalid_body(body: dict, fields: list[tuple[str, str]], client: Any,
                                      sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Contrato do pedido (`{confirmation_code}` de 1 a 64 caracteres, o do arquivamento): ausente,
    vazio, com 65 caracteres e corpo com campo extra → 400 com os `fields` exatos, sem eco; o oferecimento
    preparado não muda. Controle: com 64 caracteres, o pedido passa da validação e dá 409
    `invalid_confirmation`."""
    scenario = seed_purge_scenario(client, sql)
    _prepared(client, scenario.code)
    stored = _stored(sql, scenario.code)
    before = snapshot_purge_tables(sql)

    resp = client.post(_purge_path(scenario.code), content=json.dumps(body).encode("ascii"),
                       headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "campo_extra_sentinela", "c" * 65)
    assert_error(_confirm(client, scenario.code, "c" * 64), 409, "invalid_confirmation")
    assert _stored(sql, scenario.code) == stored
    assert snapshot_purge_tables(sql) == before


# --------------------------------------------------------------------------------------------------
# Confirmação


def test_confirm_purges_and_records_the_result_and_the_events(client: Any, sql: psycopg.Connection, clock: Any,
                                                              bot_api: Any) -> None:
    """a confirmação com o código válido devolve o
    corpo exato (`OfferingRef`, `purged_at` = o relógio da confirmação e as contagens da semente); grava
    `purged_at` e `purge_result` iguais ao corpo e anula as três colunas do código (o registro do resultado); o oferecimento
    continua `archived`; os dois eventos com os campos exatos, sem o código, o resumo nem pseudônimo."""
    scenario = seed_purge_scenario(client, sql)
    cursor = _cursor(sql)
    t0 = clock.now()
    prepared = _prepared(client, scenario.code)
    clock.advance(90)
    t1 = clock.now()

    purged = _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    assert purged == {"offering": _ref(scenario), "purged_at": iso(t1), "result": _outcome(scenario.outcome)}
    assert _stored(sql, scenario.code) == {
        "state": "archived", "purged_at": t1, "purge_result": _outcome(scenario.outcome), "purge_code_sha256": None,
        "purge_code_expires_at": None, "purge_prepared_counts": None}
    events = _events_after(client, cursor)
    assert events == [_prepared_event(scenario, t0, _impact(scenario.impact)),
                      _purged_event(scenario, t1, _outcome(scenario.outcome))]
    text = json.dumps(events, ensure_ascii=False)
    assert prepared["confirmation_code"] not in text and _sha256(prepared["confirmation_code"]) not in text
    for person in scenario.people.values():
        assert person.pseudo_id not in text and person.alias not in text


def test_confirm_again_returns_the_recorded_result_with_any_code(client: Any, sql: psycopg.Connection, clock: Any,
                                                                 bot_api: Any) -> None:
    """Repetida num oferecimento já expurgado, a confirmação devolve o mesmo corpo (o resultado
    gravado), sem conferir o código e sem evento: horas depois, com o código usado, um inventado e um vazio de
    forma válida → 200 com o mesmo corpo; nada muda e nenhum evento."""
    scenario = seed_purge_scenario(client, sql)
    prepared = _prepared(client, scenario.code)
    purged = _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    stored = _stored(sql, scenario.code)
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    clock.advance(3 * 3600)
    for code in (prepared["confirmation_code"], secrets.token_urlsafe(16), "qualquer-codigo"):
        assert _ok(_confirm(client, scenario.code, code), "offerings.purge") == purged
    assert _stored(sql, scenario.code) == stored
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []


def test_people_write_permission(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Contrato das duas rotas (permissão `people:write`): um token só com `offerings:write` e `offerings:read` → 403
    `forbidden` com `permission = people:write` nas duas rotas, sem mudança; controle: um token só com
    `people:write` prepara e confirma, e os eventos levam o `token_id` dele como ator."""
    scenario = seed_purge_scenario(client, sql)
    weak, _ = create_token(client, ADMIN_TOKEN, ["offerings:write", "offerings:read"])
    strong, strong_body = create_token(client, ADMIN_TOKEN, ["people:write"])
    before = snapshot_purge_tables(sql)
    cursor = _cursor(sql)

    assert_error(_prepare(client, scenario.code, auth(weak)), 403, "forbidden", permission="people:write")
    assert_error(_confirm(client, scenario.code, secrets.token_urlsafe(16), auth(weak)), 403, "forbidden",
                 permission="people:write")
    assert snapshot_purge_tables(sql) == before
    assert _events_after(client, cursor) == []

    t0 = clock.now()
    prepared = _prepared(client, scenario.code, auth(strong))
    purged = _ok(_confirm(client, scenario.code, prepared["confirmation_code"], auth(strong)), "offerings.purge")
    assert purged["result"] == _outcome(scenario.outcome)
    actor = strong_body["token_id"]
    assert _events_after(client, cursor) == [_prepared_event(scenario, t0, _impact(scenario.impact), actor),
                                             _purged_event(scenario, t0, _outcome(scenario.outcome), actor)]


# --------------------------------------------------------------------------------------------------
# Depois do expurgo


def test_purged_at_in_offerings_get_and_list(client: Any, sql: psycopg.Connection, clock: Any,
                                             bot_api: Any) -> None:
    """Contrato (`purged_at: horário | null` no `Offering`): antes do expurgo, `purged_at = null`
    em `offerings.get` e no item de `offerings.list`; depois, o horário da confirmação, `contact_email_set =
    false` (o contato apagado) e o resto igual; o estado continua `archived`."""
    scenario = seed_purge_scenario(client, sql)
    before = _get_offering(client, scenario.code)
    assert before["purged_at"] is None and before["state"] == "archived" and before["contact_email_set"] is True
    listed = _ok(client.get("/v1/offerings", params={"limit": 500}, headers=ADMIN), "offerings.list")
    assert [item for item in listed["items"] if item["code"] == scenario.code] == [before]
    prepared = _prepared(client, scenario.code)
    clock.advance(5)
    t1 = clock.now()
    _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")

    after = {**before, "purged_at": iso(t1), "contact_email_set": False}
    assert _get_offering(client, scenario.code) == after
    assert _get_offering(client, scenario.offering_id) == after
    listed = _ok(client.get("/v1/offerings", params={"limit": 500}, headers=ADMIN), "offerings.list")
    assert [item for item in listed["items"] if item["code"] == scenario.code] == [after]
    others = [item for item in listed["items"] if item["code"] != scenario.code]
    assert others and all(item["purged_at"] is None for item in others)


def test_archived_refusals_still_apply_after_purge(client: Any, sql: psycopg.Connection, clock: Any,
                                                   bot_api: Any) -> None:
    """O oferecimento expurgado continua `archived`, com as invariantes do oferecimento: depois do
    expurgo, ativar, encerrar, preparar o arquivamento, registrar o bot, gerar link de equipe, importar a lista
    e alterar a configuração continuam recusados com 409 `invalid_state` (`archived`), sem evento; arquivar de
    novo devolve o `Offering` arquivado (o arquivamento é idempotente) com `purged_at` preenchido."""
    scenario = seed_purge_scenario(client, sql)
    prepared = _prepared(client, scenario.code)
    _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Depois Do Expurgo")
    ref = scenario.code
    new_token = bot_api.token_of(bot_api.add_bot())
    cursor = _cursor(sql)

    assert_error(client.post(f"/v1/offerings/{ref}/bot/activate", headers=ADMIN), 409, "invalid_state",
                 state="archived")
    assert_error(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), 409, "invalid_state", state="archived")
    assert_error(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN), 409, "invalid_state",
                 state="archived")
    resp = client.put(f"/v1/offerings/{ref}/bot", json={"token": new_token}, headers=ADMIN)
    assert_error(resp, 409, "invalid_state", state="archived")
    assert_no_echo(resp, new_token)
    assert_error(client.post(f"/v1/offerings/{ref}/team-links", json={"person": person["alias"]}, headers=ADMIN),
                 409, "invalid_state", state="archived")
    rows = [{"name": "Pessoa Neutra Da Lista Nova", "ra": "4400000001", "email": "pessoa.lista.nova@turma.example.org"}]
    assert_error(client.post(f"/v1/offerings/{ref}/students", json={"rows": rows}, headers=ADMIN), 409,
                 "invalid_state", state="archived")
    assert_error(client.patch(f"/v1/offerings/{ref}/settings", json={"free_messages": True}, headers=ADMIN), 409,
                 "invalid_state", state="archived")
    archived = _ok(client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": "x" * 22},
                               headers=ADMIN), "offerings.archive")
    assert archived == _get_offering(client, ref)
    assert archived["state"] == "archived" and archived["purged_at"] is not None
    assert _events_after(client, cursor) == []


def test_purge_is_manual_without_tasks_or_sweeper_rules(client: Any, sql: psycopg.Connection, clock: Any,
                                                        bot_api: Any) -> None:
    """O expurgo é manual: nenhuma regra do varredor nem tipo de tarefa do expurgo; a preparação, o
    vencimento do código e a confirmação não criam nenhuma tarefa (a tabela `tasks` não muda); o código vencido
    não expurga nada sozinho."""
    from cq_server import scheduler

    assert not any("purge" in rule.task_type for rule in scheduler.DISPATCH_RULES + scheduler.REPORT_RULES)
    scenario = seed_purge_scenario(client, sql)
    tasks_before = sql.execute("SELECT to_jsonb(t) FROM tasks t ORDER BY id").fetchall()

    _prepared(client, scenario.code)
    clock.advance(2 * CODE_TTL.total_seconds())
    assert _stored(sql, scenario.code)["purged_at"] is None
    prepared = _prepared(client, scenario.code)
    _ok(_confirm(client, scenario.code, prepared["confirmation_code"]), "offerings.purge")
    assert sql.execute("SELECT to_jsonb(t) FROM tasks t ORDER BY id").fetchall() == tasks_before
    assert sql.execute("SELECT count(*) FROM tasks WHERE task_type LIKE %s", ("%purge%",)).fetchone()[0] == 0


def test_purge_responses_carry_only_counts(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Só contagens, estados e horários nas respostas do expurgo: o corpo da
    preparação e o da confirmação não citam pseudônimo, apelido, nome, RA, e-mail nem o contato do professor."""
    scenario = seed_purge_scenario(client, sql)
    prepare_resp = _prepare(client, scenario.code)
    prepared = _ok(prepare_resp, "offerings.prepare_purge")
    confirm_resp = _confirm(client, scenario.code, prepared["confirmation_code"])
    _ok(confirm_resp, "offerings.purge")
    values: list[str] = [scenario.contact_current or "", scenario.contact_old or ""]
    for person in scenario.people.values():
        values += [person.pseudo_id, person.identity.name, person.identity.ra, person.identity.email]
    for resp in (prepare_resp, confirm_resp):
        assert_no_echo(resp, *[value for value in values if value])
        text = resp.text
        for person in scenario.people.values():
            assert re.search(rf"\b{re.escape(person.alias)}\b", text) is None
    assert uuid.UUID(prepared["offering"]["offering_id"]).version == 4
