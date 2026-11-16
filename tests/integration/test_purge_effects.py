"""Efeitos do expurgo, tabela a tabela.

Cobre o que o expurgo apaga, reescreve e mantém, tabela a tabela, e as situações-limite.
Contratos: o resultado do expurgo, o relatório depois dele e os passos do expurgo.

Montagem: a semente do expurgo (`tests/helpers/purge.py`, `seed_purge_scenario`), com as contagens
esperadas calculadas à mão (`scenario.impact`, `scenario.outcome`) e as linhas que ela gravou
(`scenario.rows`, `contact_outbox_rows`, `free_text_limit_message_rows`, `other`). O expurgo é feito pelas rotas
(`offerings.prepare_purge` e `offerings.purge`). "O que fica" é conferido linha a linha (`to_jsonb`) antes e
depois; "o que muda", coluna a coluna, com as demais colunas iguais.

A redação esperada das mensagens livres no expurgo é a de sempre (`redaction.redact`, real no
esqueleto) com as identidades cadastradas antes do expurgo (`events.load_known_identities`): o mesmo texto que
a redação de saída mostrava. O texto esperado na fila do bot depois do expurgo é `deliverytexts.answers_only_reply`
com o contato `[contato expurgado]` (a frase do contato é sempre a última, `deliverytexts.py:130-133`).

No esqueleto, as rotas do expurgo não existem (404): cada função falha
na preparação; as asserções sobre os relatórios (a recusa do reenvio no arquivado, a versão congelada) ficam na mesma
função de um caso vermelho.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    all_events,
    assert_matches_tolerant,
    auth,
    create_offering,
    get_report,
    get_report_csv,
    list_free_messages,
    list_report_versions,
    register_person,
)
from tests.helpers.canary import PURGE_A, PURGE_B, PURGE_C, PURGE_CONTACT_CURRENT, PURGE_D
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches
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
PLACEHOLDER = "[contato expurgado]"  # PURGE_CONTACT_PLACEHOLDER
REVEAL_REASON = "conferencia do expurgo"
CSV_EOL = "\r\n"  # o fim de linha do CSV do relatório (`reportrender.CSV_LINE_TERMINATOR`)

# As tabelas que o expurgo não toca (o que fica), com a chave de ordem.
UNTOUCHED_TABLES = {
    "people": "id", "enrollments": "id", "invites": "id", "dispatches": "id", "deliveries": "id",
    "delivery_contents": "delivery_id", "submissions": "id", "dispatch_reports": "id", "bots": "id",
    "question_refs": "id", "tasks": "id",
}


# --------------------------------------------------------------------------------------------------
# Apoio


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _purge(client: Any, code: str) -> dict:
    """Prepara e confirma o expurgo pelas rotas; devolve o corpo da confirmação."""
    prepared = _ok(client.post(f"/v1/offerings/{code}/purge/prepare", headers=ADMIN), "offerings.prepare_purge")
    return _ok(client.post(f"/v1/offerings/{code}/purge", json={"confirmation_code": prepared["confirmation_code"]},
                           headers=ADMIN), "offerings.purge")


def _rows(sql: psycopg.Connection, table: str, key: str = "id", where: str = "true",
          params: tuple = ()) -> dict[Any, dict]:
    """As linhas inteiras (`to_jsonb`) de `table`, pela chave."""
    return {row[0][key]: row[0] for row in sql.execute(
        f"SELECT to_jsonb(t) FROM {table} t WHERE {where} ORDER BY t.{key}", params).fetchall()}


def _untouched(sql: psycopg.Connection) -> dict[str, dict]:
    return {table: _rows(sql, table, key) for table, key in UNTOUCHED_TABLES.items()}


def _identity_rows(sql: psycopg.Connection) -> dict[int, dict]:
    return _rows(sql, "identities", "person_id")


def _reveal(client: Any, pseudonyms: list[str]) -> dict:
    return _ok(client.post("/v1/identity/reveal", json={"pseudonyms": pseudonyms, "reason": REVEAL_REASON},
                           headers=ADMIN), "identity.reveal")


def _revealed(person: Any) -> dict:
    identity = person.identity
    return {"pseudo_id": person.pseudo_id, "alias": person.alias, "name": identity.name, "ra": identity.ra,
            "email": identity.email}


def _archive_from_draft(client: Any, code: str) -> dict:
    prepared = client.post(f"/v1/offerings/{code}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text[:300]
    resp = client.post(f"/v1/offerings/{code}/archive",
                       json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert_matches_tolerant("offerings.archive", 200, body)
    return body


def _import_rows(client: Any, code: str, rows: list[dict]) -> dict:
    return _import(client, code, rows)


# --------------------------------------------------------------------------------------------------
# Identidades, endereços e updates no expurgo


def test_identities_endpoints_and_updates_after_purge(client: Any, sql: psycopg.Connection, clock: Any,
                                                      bot_api: Any) -> None:
    """O expurgo das identidades, dos endereços e dos updates: as identidades das pessoas a apagar
    (todas as matrículas em expurgados: `aluno_a`, `equipe`, `citada`) somem; a da compartilhada (`aluno_b`)
    fica igual; `delivery_endpoints.chat_id` nulo em todos os estados do bot do oferecimento, com `state`,
    `changed_at` e as demais colunas iguais, e os endereços dos outros bots (o de `aluno_b` no bot de `O2`)
    intactos; `telegram_updates` do bot com `chat_id`, `sender_id` e `text` nulos e `chat_type`, `command`,
    `details`, `kind`, `outcome` e horários iguais; os updates dos outros bots intactos. As contagens da resposta
    batem com as linhas mudadas."""
    scenario = seed_purge_scenario(client, sql)
    identities = _identity_rows(sql)
    endpoints = _rows(sql, "delivery_endpoints")
    updates = _rows(sql, "telegram_updates")

    purged = _purge(client, scenario.code)

    deleted = {p.person_row for p in scenario.people.values() if p.deleted}
    kept = {p.person_row for p in scenario.people.values() if not p.deleted}
    assert {label for label, p in scenario.people.items() if p.deleted} == {"aluno_a", "equipe", "citada"}
    assert kept == {scenario.person("aluno_b").person_row}
    assert _identity_rows(sql) == {row: value for row, value in identities.items() if row not in deleted}
    assert purged["result"]["identities_deleted"] == len(deleted)
    assert purged["result"]["identities_kept"] == len(kept)

    after = _rows(sql, "delivery_endpoints")
    ours = set(scenario.rows["delivery_endpoints"])
    assert {row for row, value in endpoints.items() if value["bot_id"] == scenario.bot_row} == ours
    assert {row: value["state"] for row, value in endpoints.items() if row in ours} == {
        row: value["state"] for row, value in after.items() if row in ours}
    assert {value["state"] for row, value in endpoints.items() if row in ours} == {"active", "inactive", "blocked"}
    assert after == {row: ({**value, "chat_id": None} if row in ours else value) for row, value in endpoints.items()}
    assert purged["result"]["endpoints"] == len(ours)

    after = _rows(sql, "telegram_updates")
    ours = set(scenario.rows["telegram_updates"])
    assert {row for row, value in updates.items() if value["bot_id"] == scenario.bot_row} == ours
    assert after == {row: ({**value, "chat_id": None, "sender_id": None, "text": None} if row in ours else value)
                     for row, value in updates.items()}
    assert purged["result"]["updates"] == len(ours)


# --------------------------------------------------------------------------------------------------
# Mensagens livres e o contato na fila do bot (também o contato antigo)


def test_free_messages_are_redacted_again_in_every_offering(client: Any, sql: psycopg.Connection, clock: Any,
                                                            bot_api: Any) -> None:
    """A nova redação das mensagens livres no expurgo: as mensagens livres gravadas antes do cadastro da
    pessoa citada (que a redação na gravação não alcançou), a de `O` e a de **outro** oferecimento ativo (`O2`), são reescritas com a
    redação de todas as identidades cadastradas (a mesma que a saída aplicava); as demais mensagens não mudam
    (coluna a coluna); `free_messages_redacted` é exato. Depois, a lista de mensagens de `O2` não cita a pessoa
    apagada (a mensagem já está redigida no banco)."""
    from cq_server.events import load_known_identities
    from cq_server.redaction import redact

    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None
    messages = _rows(sql, "free_messages")
    p50 = set(scenario.free_text_limit_message_rows)
    assert len(p50) == 2 and set(other.free_message_rows) <= p50
    assert all(PURGE_D.name in messages[row]["text"] for row in p50)
    identities = load_known_identities(sql)
    expected_text = {row: redact(messages[row]["text"], identities) for row in p50}
    assert all(PURGE_D.name not in text for text in expected_text.values())

    purged = _purge(client, scenario.code)

    after = _rows(sql, "free_messages")
    assert after == {row: ({**value, "text": expected_text[row]} if row in p50 else value)
                     for row, value in messages.items()}
    assert purged["result"]["free_messages_redacted"] == len(p50) == scenario.outcome.free_messages_redacted
    page = list_free_messages(client, ADMIN_TOKEN, other.code)
    assert PURGE_D.name not in str(page)


@pytest.mark.parametrize("contact", ["antigo→vigente", "vigente", "antigo→removido", None],
                         ids=["antigo-vigente", "vigente", "antigo-removido", "sem-contato"])
def test_contact_is_replaced_in_the_bot_queue_and_deleted(contact: str | None, client: Any,
                                                          sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """O contato no expurgo: nas respostas fixas da fila do bot de `O`, o e-mail
    vigente e a frase `CONTACT_HOWTO` de um contato trocado (ou removido) antes do arquivamento viram
    `[contato expurgado]`; as demais linhas da fila não mudam; `outbox_redacted` exato (as linhas mudadas);
    `offering_contacts` de `O` apagada (`contact_email_deleted` só com contato vigente) e `contact_email_set =
    false`. Com o mesmo contato vigente noutro oferecimento ativo (`O3`), a linha de contato e a fila
    dele ficam intactas."""
    from cq_server.deliverytexts import answers_only_reply

    shared_contact = contact in ("antigo→vigente",)
    scenario = seed_purge_scenario(client, sql, contact=contact, shared_contact=shared_contact)
    outbox = _rows(sql, "outbox_messages")
    contacts = _rows(sql, "offering_contacts")
    ours = set(scenario.contact_outbox_rows)
    expected_rows = {"antigo→vigente": 2, "vigente": 1, "antigo→removido": 1, None: 0}[contact]
    assert len(ours) == expected_rows == scenario.outcome.outbox_redacted
    for row in ours:
        assert outbox[row]["bot_id"] == scenario.bot_row and "escreva para" in outbox[row]["text"]

    purged = _purge(client, scenario.code)

    assert _rows(sql, "outbox_messages") == {
        row: ({**value, "text": answers_only_reply(PLACEHOLDER)} if row in ours else value)
        for row, value in outbox.items()}
    assert purged["result"]["outbox_redacted"] == len(ours)
    assert purged["result"]["contact_email_deleted"] is (scenario.contact_current is not None)
    assert _rows(sql, "offering_contacts") == {
        row: value for row, value in contacts.items() if value["offering_id"] != scenario.offering_row}
    shown = _ok(client.get(f"/v1/offerings/{scenario.code}", headers=ADMIN), "offerings.get")
    assert shown["contact_email_set"] is False
    if shared_contact:
        o3 = scenario.shared_contact
        assert o3 is not None
        assert [value["email"] for value in _rows(sql, "offering_contacts").values()
                if value["offering_id"] == o3.offering_row] == [PURGE_CONTACT_CURRENT]
        assert all(PURGE_CONTACT_CURRENT in outbox[row]["text"] for row in o3.outbox_rows)


# --------------------------------------------------------------------------------------------------
# O resultado gravado e o que fica


def test_purge_result_and_what_stays(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """O registro do resultado na confirmação e o que fica (o bot e o token cifrado ficam; `events` não é alterado):
    `offerings.purge_result` igual ao `result` da resposta; ficam linha a linha as pessoas, as matrículas, os
    convites, os disparos, as entregas e o conteúdo delas, as submissões (com o `raw_input` literal), as versões
    congeladas, o bot (com o token cifrado), as questões e as tarefas; os eventos anteriores ficam iguais e só
    os dois do expurgo entram."""
    scenario = seed_purge_scenario(client, sql)
    before = _untouched(sql)
    events_before = sql.execute("SELECT to_jsonb(e) FROM events e ORDER BY seq").fetchall()

    purged = _purge(client, scenario.code)

    stored = sql.execute("SELECT purge_result FROM offerings WHERE id = %s", (scenario.offering_row,)).fetchone()[0]
    assert stored == purged["result"] == dataclasses.asdict(scenario.outcome)
    assert _untouched(sql) == before
    submission = before["submissions"][scenario.submissions["aluno_a"]]
    assert submission["raw_input"] == {"texto": f"a mesma de {PURGE_B.name}", "numero": PURGE_B.ra}
    events_after = sql.execute("SELECT to_jsonb(e) FROM events e ORDER BY seq").fetchall()
    assert events_after[:len(events_before)] == events_before
    assert [row[0]["type"] for row in events_after[len(events_before):]] == ["offering.purge_prepared",
                                                                            "offering.purged"]


# --------------------------------------------------------------------------------------------------
# Revelação


def test_reveal_after_purge(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Na revelação (`identity.reveal`), a pessoa apagada sai entre os não encontrados, numa
    única auditoria com as demais: antes, as quatro pessoas reveladas; depois, só a compartilhada (`aluno_b`)
    com a identidade inteira, as três apagadas em `not_found`, e um único `identity.revealed` com
    `revealed = [aluno_b]` e `not_found_count = 3`."""
    scenario = seed_purge_scenario(client, sql)
    pseudonyms = [scenario.person(label).pseudo_id for label in ("aluno_a", "aluno_b", "equipe", "citada")]
    before = _reveal(client, pseudonyms)
    assert sorted(item["pseudo_id"] for item in before["people"]) == sorted(pseudonyms)
    _purge(client, scenario.code)
    cursor = int(sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])

    after = _reveal(client, pseudonyms)
    assert after["people"] == [_revealed(scenario.person("aluno_b"))]
    assert sorted(after["not_found"]) == sorted(scenario.person(label).pseudo_id
                                                for label in ("aluno_a", "equipe", "citada"))
    audits = [e for e in all_events(client, ADMIN_TOKEN, "identity.revealed", "identity.reveal_denied")
              if e["seq"] > cursor]
    assert [(e["type"], e["data"]["revealed"], e["data"]["not_found_count"], e["data"]["reason"]) for e in audits] == [
        ("identity.revealed", [scenario.person("aluno_b").pseudo_id], 3, REVEAL_REASON)]


# --------------------------------------------------------------------------------------------------
# Situações-limite


def test_person_in_two_archived_offerings(client: Any, sql: psycopg.Connection, clock: Any) -> None:
    """Pessoa em dois arquivados (mantida se há matrícula noutro oferecimento com
    `purged_at IS NULL`): a pessoa matriculada em dois oferecimentos arquivados é mantida no primeiro expurgo
    (`identities_kept = 1`) e apagada no segundo (`identities_to_delete = 1`)."""
    first = create_offering(client, ADMIN_TOKEN)
    second = create_offering(client, ADMIN_TOKEN)
    row = {"name": "Pessoa Neutra Duas Turmas", "ra": "4410000001", "email": "pessoa.duas.turmas@turma.example.org"}
    for offering in (first, second):
        _import_rows(client, offering["code"], [row])
        _archive_from_draft(client, offering["code"])
    person_row, _, _ = _person_by_ra(sql, row["ra"])

    prepared = _ok(client.post(f"/v1/offerings/{first['code']}/purge/prepare", headers=ADMIN),
                   "offerings.prepare_purge")
    assert prepared["impact"] == {"identities_to_delete": 0, "identities_kept": 1, "endpoints": 0, "updates": 0,
                                  "contact_email_set": False}
    first_result = _ok(client.post(f"/v1/offerings/{first['code']}/purge",
                                   json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN),
                       "offerings.purge")
    assert first_result["result"]["identities_kept"] == 1 and first_result["result"]["identities_deleted"] == 0
    assert person_row in _identity_rows(sql)

    prepared = _ok(client.post(f"/v1/offerings/{second['code']}/purge/prepare", headers=ADMIN),
                   "offerings.prepare_purge")
    assert prepared["impact"] == {"identities_to_delete": 1, "identities_kept": 0, "endpoints": 0, "updates": 0,
                                  "contact_email_set": False}
    second_result = _purge_with(client, second["code"], prepared["confirmation_code"])
    assert second_result["result"] == {"identities_deleted": 1, "identities_kept": 0, "endpoints": 0, "updates": 0,
                                       "free_messages_redacted": 0, "outbox_redacted": 0,
                                       "contact_email_deleted": False}
    assert person_row not in _identity_rows(sql)


def _purge_with(client: Any, code: str, confirmation_code: str) -> dict:
    return _ok(client.post(f"/v1/offerings/{code}/purge", json={"confirmation_code": confirmation_code},
                           headers=ADMIN), "offerings.purge")


def test_offering_without_bot_and_team(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Oferecimento sem bot e sem equipe (sem bot, a limpeza dos endereços, dos updates e da
    fila do bot não roda, com contagens 0): o expurgo segue com `endpoints = updates = outbox_redacted = 0` e as
    identidades apagadas pela regra de sempre."""
    scenario = seed_purge_scenario(client, sql, bot=False, team=False, shared=False, cross_offering=False,
                                   contact=None)
    assert scenario.bot_row is None
    assert dataclasses.asdict(scenario.impact) == {"identities_to_delete": 3, "identities_kept": 0, "endpoints": 0,
                                                   "updates": 0, "contact_email_set": False}
    purged = _purge(client, scenario.code)
    assert purged["result"] == {"identities_deleted": 3, "identities_kept": 0, "endpoints": 0, "updates": 0,
                                "free_messages_redacted": scenario.outcome.free_messages_redacted,
                                "outbox_redacted": 0, "contact_email_deleted": False}
    assert purged["result"] == dataclasses.asdict(scenario.outcome)
    rows = {p.person_row for p in scenario.people.values()}
    assert rows.isdisjoint(_identity_rows(sql))


def test_person_without_enrollment_is_not_reached(client: Any, sql: psycopg.Connection, clock: Any,
                                                  bot_api: Any) -> None:
    """Pessoa sem matrícula não é alcançada: uma pessoa cadastrada pela API base, sem
    matrícula, mantém a identidade e a revelação depois do expurgo."""
    loose = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Sem Matricula", ra="4420000001",
                            email="pessoa.sem.matricula@turma.example.org")
    scenario = seed_purge_scenario(client, sql)
    _purge(client, scenario.code)
    revealed = _reveal(client, [loose["pseudo_id"]])
    assert revealed == {"people": [{"pseudo_id": loose["pseudo_id"], "alias": loose["alias"],
                                    "name": "Pessoa Neutra Sem Matricula", "ra": "4420000001",
                                    "email": "pessoa.sem.matricula@turma.example.org"}], "not_found": []}


def test_purged_ra_is_accepted_as_another_person(client: Any, sql: psycopg.Connection, clock: Any,
                                                 bot_api: Any) -> None:
    """`people.register` com o RA ou e-mail de uma identidade apagada é aceito, como **outra**
    pessoa: depois do expurgo, o cadastro com o nome, o RA e o e-mail de `aluno_a` dá 201 com um pseudônimo
    diferente; a pessoa expurgada continua sem identidade."""
    scenario = seed_purge_scenario(client, sql)
    old = scenario.person("aluno_a")
    _purge(client, scenario.code)
    again = register_person(client, ADMIN_TOKEN, name=PURGE_A.name, ra=PURGE_A.ra, email=PURGE_A.email)
    assert again["pseudo_id"] != old.pseudo_id
    assert _person_by_ra(sql, PURGE_A.ra)[0] != old.person_row
    assert old.person_row not in _identity_rows(sql)
    assert _reveal(client, [old.pseudo_id]) == {"people": [], "not_found": [old.pseudo_id]}


# --------------------------------------------------------------------------------------------------
# Relatório depois do expurgo


def _purged_distribution(field: dict) -> dict:
    return {**field, "unavailable": "purged", "values": None, "ratios": None, "unrated": None, "texts": None}


def test_report_now_after_purge_marks_text_and_number_unavailable(client: Any, sql: psycopg.Connection,
                                                                  clock: Any, bot_api: Any) -> None:
    """Relatório depois do expurgo: o relatório calculado no momento do disparo do
    expurgado sai com os campos de texto e de número com `unavailable = "purged"` e `values`, `ratios`,
    `unrated`, `texts` nulos; `choices` e as demais seções iguais às de antes. No CSV, cada distribuição
    indisponível vira uma linha `distribution,<índice>,purged,unavailable,,` na posição do campo (nunca zero),
    e as demais linhas ficam iguais."""
    scenario = seed_purge_scenario(client, sql)
    before = get_report(client, scenario.code, scenario.dispatch_id)
    csv_before = get_report_csv(client, scenario.code, scenario.dispatch_id)
    assert [field["type"] for field in before["distribution"]["fields"]] == ["text", "number"]

    _purge(client, scenario.code)

    after = get_report(client, scenario.code, scenario.dispatch_id)
    expected = {**before, "distribution": {"fields": [_purged_distribution(field)
                                                      for field in before["distribution"]["fields"]]}}
    assert after == expected
    lines = csv_before.split(CSV_EOL)
    distribution = [i for i, line in enumerate(lines) if line.startswith("distribution,")]
    assert [lines[i] for i in distribution] == ["distribution,0,,texts.others,2,2", "distribution,1,,values.others,2,2",
                                                 "distribution,1,,ratios.others,2,2", "distribution,1,,unrated,0,0"]
    expected_lines = (lines[:distribution[0]] + ["distribution,0,purged,unavailable,,",
                                                  "distribution,1,purged,unavailable,,"]
                      + lines[distribution[-1] + 1:])
    assert get_report_csv(client, scenario.code, scenario.dispatch_id) == CSV_EOL.join(expected_lines)


def test_frozen_version_unchanged_and_resend_refused(client: Any, sql: psycopg.Connection, clock: Any,
                                                     bot_api: Any) -> None:
    """a versão congelada antes do expurgo sai byte a byte igual (JSON e CSV) e
    a lista de versões não muda; `reports.resend_summary` continua recusado com 409 `invalid_state`
    (`archived`), sem versão nova."""
    scenario = seed_purge_scenario(client, sql)
    versions = list_report_versions(client, ADMIN_TOKEN, scenario.code, scenario.dispatch_id)
    assert [item["version"] for item in versions["items"]] == [1]
    frozen = get_report(client, scenario.code, scenario.dispatch_id, version=1)
    frozen_csv = get_report_csv(client, scenario.code, scenario.dispatch_id, version=1)

    _purge(client, scenario.code)

    assert get_report(client, scenario.code, scenario.dispatch_id, version=1) == frozen
    assert get_report_csv(client, scenario.code, scenario.dispatch_id, version=1) == frozen_csv
    assert list_report_versions(client, ADMIN_TOKEN, scenario.code, scenario.dispatch_id) == versions
    resp = client.post(f"/v1/offerings/{scenario.code}/dispatches/{scenario.dispatch_id}/report/resend",
                       headers=ADMIN)
    assert_error(resp, 409, "invalid_state", state="archived")
    assert list_report_versions(client, ADMIN_TOKEN, scenario.code, scenario.dispatch_id) == versions


def test_other_offering_raw_input_is_literal_in_report_now(client: Any, sql: psycopg.Connection, clock: Any,
                                                           bot_api: Any) -> None:
    """Exceção conhecida, com contagem exata; `reports.get` no momento de **outro**
    oferecimento: no oferecimento ativo `O2`, duas respostas de texto iguais citam `citada` (a da semente e
    uma segunda, de uma pessoa neutra, para o item passar do mínimo de ocorrências); antes do expurgo, o item da
    distribuição `texts` sai redigido; depois, a identidade apagada deixa de ser conhecida e o item sai literal:
    exatamente um item com o nome, `count = (2, 2)`, e nenhum outro valor muda."""
    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None and other.dispatch_row is not None
    neutral = {"name": "Pessoa Neutra Segunda Resposta", "ra": "4430000001",
               "email": "pessoa.segunda.resposta@turma.example.org"}
    _import_rows(client, other.code, [neutral])
    person_row, _, _ = _person_by_ra(sql, neutral["ra"])
    now = clock.now()
    chat = 9_400_000_001
    enrollment = _activate_student(sql, person_row, other.offering_row, now - timedelta(hours=3))
    endpoint = _endpoint(sql, person_row, other.bot_row, chat, "active", now - timedelta(hours=3))
    answer = f"igual à de {PURGE_D.name}"
    opens = now - timedelta(hours=2)
    _answered(sql, dispatch_row=other.dispatch_row, enrollment_row=enrollment, person_row=person_row,
              bot_row=other.bot_row, endpoint_row=endpoint, chat_id=chat, question=preset("text"),
              raw={"resposta": answer}, text=answer, score=0.0, part_derived=[_part(0)], opens=opens,
              answered=opens + timedelta(minutes=30), revealed=now - timedelta(minutes=59))
    before = get_report(client, other.code, other.dispatch_id)
    [field] = before["distribution"]["fields"]
    [item] = field["texts"]["items"]
    assert PURGE_D.name not in item["value"] and item["count"] == {"on_time": 2, "with_late": 2}

    _purge(client, scenario.code)

    after = get_report(client, other.code, other.dispatch_id)
    literal = [entry for entry in after["distribution"]["fields"][0]["texts"]["items"]
               if PURGE_D.name in entry["value"]]
    assert literal == [{"value": answer, "count": {"on_time": 2, "with_late": 2}}]
    assert after == {**before, "distribution": {"fields": [{**field, "texts": {**field["texts"], "items": literal}}]}}
    for name in (PURGE_A.name, PURGE_B.name, PURGE_C.name):
        assert name not in str(after)
    assert iso(clock.now()) == after["computed_at"]
