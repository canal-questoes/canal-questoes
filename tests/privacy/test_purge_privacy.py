"""Privacidade depois do expurgo.

Cobre a privacidade depois do expurgo e as exceções declaradas
do expurgo.

Cenário: a semente do expurgo com o contato igual noutro oferecimento ativo (`seed_purge_scenario(...,
shared_contact=True)`), com as pessoas canário do expurgo (`CANARY_PURGE_PEOPLE`, prefixos `PURGE_PREFIXES`):
`aluno_a`, `equipe` e `citada` são apagadas; `aluno_b`, compartilhada com o ativo `O2`, é mantida. Por cima:
- um `task.failed` com texto livre que cita `aluno_a`, gravado **antes** do cadastro dela (o limite da redação no
  log de eventos, append-only);
- uma segunda resposta de texto em `O2` igual à da semente, que cita `citada`, para o item passar do mínimo de
  ocorrências na distribuição `texts` do relatório no momento de `O2`;
- um oferecimento `O4` cuja disciplina (texto de operador) cita `equipe`, cadastrado antes da semente (a redação
  da gravação ainda não a conhece; depois, só a saída a redige).

Depois do expurgo pelas rotas, **toda** operação GET do catálogo que aceita o oferecimento, um disparo ou uma
pessoa (o conjunto derivado do catálogo: as GET com parâmetro de caminho; conferido contra as operações do
canário, `tests.helpers.calls.VALID_CALLS`), as respostas e os eventos do expurgo e o filtro por pessoa não
citam nenhuma identidade canário (`find_canary(..., prefixes=PURGE_PREFIXES)`), salvo as exceções declaradas, cada
uma com controle positivo (a mesma consulta antes do expurgo sai redigida; depois, o texto literal aparece) e
contagem exata (os rótulos achados iguais aos do texto literal, numa única linha ou num único item):
- o log de eventos: o `task.failed` gravado antes do cadastro, no `events.list`;
- o texto de operador: a disciplina de `O4`, no `offerings.get` dele;
- o texto de outro oferecimento: o `raw_input` de `O2` no relatório no momento de `O2` (JSON e CSV).

A varredura do banco vivo percorre as seis tabelas do expurgo **inteiras** (`identities`,
`delivery_endpoints`, `telegram_updates`, `free_messages`, `offering_contacts`, `outbox_messages`) atrás das
identidades apagadas e dos dois e-mails do contato, com a exceção nomeada da retenção de outros oferecimentos, com controle positivo e
contagem exata: (a) o `telegram_updates.text` do bot de `O2` (exatamente as linhas da mensagem livre e das
respostas de texto que citam `citada`); (b) o contato igual em `O3` (exatamente a linha de `offering_contacts` e
a da fila do bot de `O3`). Nas linhas do oferecimento e do bot expurgados, nenhuma ocorrência. Os alvos da
pessoa mantida (`aluno_b`) ficam fora da varredura (a identidade dela continua no banco, por regra).

No esqueleto, as rotas do expurgo não existem (404): cada função falha
na preparação.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import psycopg

from tests.helpers.api import ADMIN_TOKEN, auth, create_offering
from tests.helpers.canary import (
    PURGE_A,
    PURGE_B,
    PURGE_C,
    PURGE_CONTACT_CURRENT,
    PURGE_CONTACT_OLD,
    PURGE_D,
    PURGE_PREFIXES,
    find_canary,
)
from tests.helpers.contract import assert_matches, load_catalog
from tests.helpers.engine import preset
from tests.helpers.purge import (
    _activate_student,
    _answered,
    _endpoint,
    _import,
    _part,
    _person_by_ra,
    seed_purge_scenario,
)

ADMIN = auth(ADMIN_TOKEN)
SIX_TABLES = {"identities": "person_id", "delivery_endpoints": "id", "telegram_updates": "id",
              "free_messages": "id", "offering_contacts": "id", "outbox_messages": "id"}
EARLY_FREE_TEXT_ERROR = f"falha ao tratar a mensagem de {PURGE_A.name}"
OPERATOR_COURSE = f"Monitoria com {PURGE_C.name}"
CITED_ANSWER = f"igual à de {PURGE_D.name}"
CITED_FREE_MESSAGE = f"Alguém sabe se {PURGE_D.name} vai entregar o trabalho?"  # o texto da semente


def _hits(text: str) -> list[str]:
    return find_canary(text, prefixes=PURGE_PREFIXES)


def _kept_labels() -> set[str]:
    """Os rótulos dos alvos da pessoa mantida (`aluno_b`): a identidade dela continua no banco, por regra."""
    parts = [PURGE_B.name, PURGE_B.ra, PURGE_B.email]
    for account in PURGE_B.accounts:
        parts += [account.first_name, account.last_name or "", account.username or "", str(account.user_id),
                  f"{account.first_name} {account.last_name or ''}".strip()]
    return set(_hits("\n".join(parts)))


KEPT = _kept_labels()


def _deleted_hits(text: str) -> list[str]:
    return [label for label in _hits(text) if label not in KEPT]


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _get(client: Any, path: str, params: Any = None) -> Any:
    resp = client.get(path, params=params or {}, headers=ADMIN)
    assert resp.status_code == 200, f"GET {path}: {resp.status_code} {resp.text[:300]}"
    return resp


def _all_types() -> tuple[str, ...]:
    from cq_server.events import EventType

    return tuple(dict.fromkeys([*(t.value for t in EventType), "offering.purge_prepared", "offering.purged"]))


def _append_early_free_text_event(sql: psycopg.Connection, clock: Any) -> None:
    """O `task.failed` com texto livre que cita `aluno_a`, gravado antes do cadastro dela (a redação da gravação
    ainda não a conhece: o limite da redação na gravação)."""
    from cq_server.events import append_event

    assert sql.execute("SELECT count(*) FROM identities WHERE ra = %s", (PURGE_A.ra,)).fetchone()[0] == 0
    with sql.transaction():
        append_event(sql, "task.failed", {"task_id": 1, "task_type": "demo.task", "attempts": 3,
                                          "error": EARLY_FREE_TEXT_ERROR}, occurred_at=clock.now(), actor_token_id=None)


def _second_cited_answer(client: Any, sql: psycopg.Connection, clock: Any, other: Any) -> int:
    """Uma segunda resposta de texto em `O2`, igual à da semente (cita `citada`); devolve a linha do update."""
    neutral = {"name": "Pessoa Neutra Segunda Resposta", "ra": "4470000001",
               "email": "pessoa.segunda.resposta@turma.example.org"}
    _import(client, other.code, [neutral])
    person_row, _, _ = _person_by_ra(sql, neutral["ra"])
    now = clock.now()
    chat = 9_470_000_001
    enrollment = _activate_student(sql, person_row, other.offering_row, now - timedelta(hours=3))
    endpoint = _endpoint(sql, person_row, other.bot_row, chat, "active", now - timedelta(hours=3))
    opens = now - timedelta(hours=2)
    _, _, update = _answered(sql, dispatch_row=other.dispatch_row, enrollment_row=enrollment, person_row=person_row,
                             bot_row=other.bot_row, endpoint_row=endpoint, chat_id=chat, question=preset("text"),
                             raw={"resposta": CITED_ANSWER}, text=CITED_ANSWER, score=0.0, part_derived=[_part(0)],
                             opens=opens, answered=opens + timedelta(minutes=30),
                             revealed=now - timedelta(minutes=59))
    return update


def _world(client: Any, sql: psycopg.Connection, clock: Any) -> dict:
    # Antes da semente (as pessoas canário ainda não cadastradas): o evento gravado antes do cadastro e a disciplina de `O4`, que a
    # redação da gravação ainda não conhece (gravados literais; a saída os redige enquanto a identidade existir).
    _append_early_free_text_event(sql, clock)
    o4 = create_offering(client, ADMIN_TOKEN, course=OPERATOR_COURSE)
    assert sql.execute("SELECT course FROM offerings WHERE code = %s", (o4["code"],)).fetchone()[0] == OPERATOR_COURSE
    scenario = seed_purge_scenario(client, sql, shared_contact=True)
    assert scenario.other is not None and scenario.shared_contact is not None
    extra_update = _second_cited_answer(client, sql, clock, scenario.other)
    return {"scenario": scenario, "extra_update": extra_update, "o4": o4}


def _purge(client: Any, code: str) -> tuple[Any, Any]:
    prepared = client.post(f"/v1/offerings/{code}/purge/prepare", headers=ADMIN)
    body = _ok(prepared, "offerings.prepare_purge")
    confirmed = client.post(f"/v1/offerings/{code}/purge", json={"confirmation_code": body["confirmation_code"]},
                            headers=ADMIN)
    _ok(confirmed, "offerings.purge")
    return prepared, confirmed


def _derived_get_operations() -> set[str]:
    """As operações GET do catálogo com parâmetro de caminho (o oferecimento, o disparo ou a pessoa)."""
    return {op["id"] for op in load_catalog()["operations"] if op["method"] == "GET" and "{" in op["path"]}


def _get_calls(world: dict) -> dict[str, list[tuple[str, dict]]]:
    """As chamadas GET depois do expurgo, por operação: `(caminho, consulta)`."""
    scenario = world["scenario"]
    other = scenario.other
    o, o2, o4 = scenario.code, other.code, world["o4"]["code"]
    report, other_report = (f"/v1/offerings/{o}/dispatches/{scenario.dispatch_id}/report",
                            f"/v1/offerings/{o2}/dispatches/{other.dispatch_id}/report")
    return {
        "offerings.get": [(f"/v1/offerings/{o}", {}), (f"/v1/offerings/{o2}", {}), (f"/v1/offerings/{o4}", {})],
        "bots.get": [(f"/v1/offerings/{o}/bot", {}), (f"/v1/offerings/{o2}/bot", {})],
        "students.summary": [(f"/v1/offerings/{o}/students/summary", {}), (f"/v1/offerings/{o2}/students/summary", {})],
        "dispatches.get": [(f"/v1/offerings/{o}/dispatches/{scenario.dispatch_id}", {}),
                           (f"/v1/offerings/{o2}/dispatches/{other.dispatch_id}", {})],
        "dispatches.list": [(f"/v1/offerings/{o}/dispatches", {}), (f"/v1/offerings/{o2}/dispatches", {})],
        "messages.list": [(f"/v1/offerings/{o}/messages", {}), (f"/v1/offerings/{o2}/messages", {})],
        "reports.get": [(report, {}), (report, {"format": "csv"}), (report, {"version": 1}),
                        (report, {"version": 1, "format": "csv"}), (other_report, {}),
                        (other_report, {"format": "csv"})],
        "reports.list_versions": [(f"{report}/versions", {}), (f"{other_report}/versions", {})],
        "offerings.get_digest": [(f"/v1/offerings/{o}/digest", {}), (f"/v1/offerings/{o2}/digest", {})],
        "people.get": [(f"/v1/people/{person.alias}", {}) for person in scenario.people.values()],
    }


def _reveal(client: Any, pseudonyms: list[str]) -> dict:
    return _ok(client.post("/v1/identity/reveal", json={"pseudonyms": pseudonyms, "reason": "controle do canario"},
                           headers=ADMIN), "identity.reveal")


# --------------------------------------------------------------------------------------------------
# Saídas da API depois do expurgo


def test_api_outputs_after_purge_have_no_canary_identity(client: Any, sql: psycopg.Connection, clock: Any,
                                                         bot_api: Any) -> None:
    """depois do expurgo, toda GET do catálogo que aceita o oferecimento, um
    disparo ou uma pessoa (o conjunto derivado do catálogo, igual ao desta tabela e coberto pelas operações do
    canário), as respostas da preparação e da confirmação, os eventos do expurgo, todo o log e o filtro por
    pessoa saem sem identidade canário, salvo as exceções declaradas, cada uma com
    controle positivo (antes do expurgo, a mesma consulta sai redigida) e contagem exata."""
    from tests.helpers.calls import VALID_CALLS

    world = _world(client, sql, clock)
    scenario = world["scenario"]
    other = scenario.other
    calls = _get_calls(world)
    assert set(calls) == _derived_get_operations()
    assert set(calls) <= set(VALID_CALLS), "operação GET fora da lista do canário"

    # Controles antes do expurgo: as três exceções saem redigidas.
    o2_report = _get(client, f"/v1/offerings/{other.code}/dispatches/{other.dispatch_id}/report")
    assert _hits(o2_report.text) == []
    assert _hits(_get(client, f"/v1/offerings/{world['o4']['code']}").text) == []
    log = _get(client, "/v1/events", [("types", t) for t in ("task.failed",)] + [("limit", 500)])
    assert _hits(log.text) == []

    prepared, confirmed = _purge(client, scenario.code)
    for resp in (prepared, confirmed):
        assert _hits(resp.text) == []

    exceptions: dict[str, list[str]] = {}
    for op_id, items in calls.items():
        for path, params in items:
            text = _get(client, path, params).text
            found = _hits(text)
            if path.startswith(f"/v1/offerings/{other.code}/dispatches/{other.dispatch_id}/report") and \
                    not path.endswith("/versions") and "version" not in params:
                exceptions[f"outro oferecimento {params.get('format', 'json')}"] = found
                lines = [line for line in text.splitlines() if PURGE_D.name in line]
                if params.get("format") == "csv":
                    assert len(lines) == 1, lines
                else:
                    items_with_name = [item for field in json.loads(text)["distribution"]["fields"]
                                       for item in (field.get("texts") or {}).get("items", [])
                                       if PURGE_D.name in item["value"]]
                    assert items_with_name == [{"value": CITED_ANSWER, "count": {"on_time": 2, "with_late": 2}}]
                continue
            if path == f"/v1/offerings/{world['o4']['code']}":
                exceptions["texto de operador"] = found
                assert json.loads(text)["course"] == OPERATOR_COURSE
                continue
            assert found == [], f"{op_id} {path} {params}: {found}"
    assert exceptions == {"outro oferecimento json": _hits(CITED_ANSWER), "outro oferecimento csv": _hits(CITED_ANSWER),
                          "texto de operador": _hits(OPERATOR_COURSE)}
    assert all(exceptions.values()), "exceção declarada sem ocorrência (controle positivo)"

    events = _get(client, "/v1/events", [("types", t) for t in _all_types()] + [("limit", 500)]).json()["items"]
    with_hits = [(e["type"], _hits(json.dumps(e, ensure_ascii=False))) for e in events]
    assert [(event_type, found) for event_type, found in with_hits if found] == [("task.failed", _hits(EARLY_FREE_TEXT_ERROR))]
    assert [e["data"]["error"] for e in events if e["type"] == "task.failed"] == [EARLY_FREE_TEXT_ERROR]
    purge_events = [e for e in events if e["type"] in ("offering.purge_prepared", "offering.purged")]
    assert [e["type"] for e in purge_events] == ["offering.purge_prepared", "offering.purged"]
    for person in scenario.people.values():
        page = _get(client, "/v1/events", [("person", person.alias), ("limit", 500)])
        assert _hits(page.text) == [], person.label


def test_reveal_controls_after_purge(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Controles positivos: antes do expurgo, as quatro pessoas canário são reveladas com a
    identidade inteira; depois, as apagadas saem em `not_found` e a compartilhada (`aluno_b`) continua revelável,
    com a identidade inteira e só ela."""
    world = _world(client, sql, clock)
    scenario = world["scenario"]
    pseudonyms = [person.pseudo_id for person in scenario.people.values()]
    before = _reveal(client, pseudonyms)
    assert {item["name"] for item in before["people"]} == {PURGE_A.name, PURGE_B.name, PURGE_C.name, PURGE_D.name}
    _purge(client, scenario.code)
    after = _reveal(client, pseudonyms)
    kept = scenario.person("aluno_b")
    assert after["people"] == [{"pseudo_id": kept.pseudo_id, "alias": kept.alias, "name": PURGE_B.name,
                                "ra": PURGE_B.ra, "email": PURGE_B.email}]
    assert sorted(after["not_found"]) == sorted(p.pseudo_id for p in scenario.people.values() if p.deleted)
    assert _deleted_hits(json.dumps(after, ensure_ascii=False)) == []


# --------------------------------------------------------------------------------------------------
# Banco vivo


def _scan_six(sql: psycopg.Connection) -> dict[tuple[str, Any], list[str]]:
    """As linhas das seis tabelas do expurgo, inteiras, com alvos das pessoas apagadas ou dos e-mails do
    contato: `(tabela, chave) → rótulos`."""
    found: dict[tuple[str, Any], list[str]] = {}
    for table, key in SIX_TABLES.items():
        for row_key, text in sql.execute(f"SELECT t.{key}, to_jsonb(t)::text FROM {table} t ORDER BY t.{key}"):
            labels = _deleted_hits(text)
            if labels:
                found[(table, row_key)] = labels
    return found


def test_live_database_scan_after_purge(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """antes do expurgo, a varredura das seis tabelas inteiras acha as
    pessoas apagadas e os dois e-mails do contato nas linhas do oferecimento e do bot expurgados (controle
    positivo, inclusive cada identidade apagada em `identities` e o e-mail antigo na fila do bot); depois, só
    restam as exceções nomeadas da retenção de outros oferecimentos, com contagem exata: (a) o `telegram_updates.text` do bot de `O2`,
    exatamente as linhas da mensagem livre e das duas respostas de texto que citam `citada`, com os rótulos do
    texto literal; (b) o contato igual em `O3`, exatamente a linha de `offering_contacts` e a da fila do bot de
    `O3`, com os rótulos do e-mail vigente. Nenhuma ocorrência nas linhas do oferecimento e do bot expurgados."""
    world = _world(client, sql, clock)
    scenario = world["scenario"]
    other, shared = scenario.other, scenario.shared_contact
    before = _scan_six(sql)
    deleted_rows = {p.person_row for p in scenario.people.values() if p.deleted}
    assert {key for table, key in before if table == "identities"} == deleted_rows
    assert {key for table, key in before if table == "outbox_messages"} >= set(scenario.contact_outbox_rows)
    assert any(_hits(PURGE_CONTACT_OLD)[0] in labels for labels in before.values())
    deleted_endpoints = {row[0] for row in sql.execute(
        "SELECT id FROM delivery_endpoints WHERE bot_id = %s AND person_id = ANY(%s)",
        (scenario.bot_row, list(deleted_rows))).fetchall()}
    assert len(deleted_endpoints) == 4  # `aluno_a` (duas contas), `equipe` e `citada`
    assert {key for table, key in before if table == "delivery_endpoints"} == deleted_endpoints

    _purge(client, scenario.code)

    after = _scan_six(sql)
    cited_updates = {row: (CITED_FREE_MESSAGE if row == other.update_rows[0] else CITED_ANSWER)
                     for row in (*other.update_rows, world["extra_update"])}
    expected = {("telegram_updates", row): _hits(text) for row, text in cited_updates.items()}
    contact_rows = [row[0] for row in sql.execute("SELECT id FROM offering_contacts WHERE offering_id = %s",
                                                  (shared.offering_row,)).fetchall()]
    expected.update({("offering_contacts", row): _hits(PURGE_CONTACT_CURRENT) for row in contact_rows})
    expected.update({("outbox_messages", row): _hits(PURGE_CONTACT_CURRENT) for row in shared.outbox_rows})
    assert len(contact_rows) == 1 and len(shared.outbox_rows) == 1 and len(cited_updates) == 3
    assert after == expected
    texts = {row: text for row, text in sql.execute("SELECT id, text FROM telegram_updates WHERE id = ANY(%s)",
                                                    (list(cited_updates),)).fetchall()}
    assert texts == cited_updates
    ours = {("telegram_updates", row) for row in scenario.rows["telegram_updates"]}
    ours |= {("delivery_endpoints", row) for row in scenario.rows["delivery_endpoints"]}
    ours |= {("free_messages", row) for row in scenario.rows["free_messages"]}
    ours |= {("outbox_messages", row) for row in scenario.rows["outbox_messages"]}
    assert ours.isdisjoint(after)
    assert _hits(PURGE_CONTACT_OLD) and not any(_hits(PURGE_CONTACT_OLD)[0] in labels for labels in after.values())
