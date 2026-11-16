"""Roteiro de ponta a ponta do expurgo, pelo CLI.

O roteiro, só com dublês, pelo `cq` em processo (`cli_invoke`, a API dos testes por `http=`): **oferecimento
com contato → bot → lista → (disparo fechado com respostas, um pedido de ajuda e a resposta fixa na fila do
bot) → encerramento → arquivamento → `offerings purge prepare` recusado com `purge_pending` enquanto há
trabalho → os passos do worker, um a um, com a pendência caindo a cada passo → `offerings purge prepare`
aceito → `offerings purge confirm` → revelação (não encontrados), relatório no momento (distribuições
indisponíveis, a linha `purged` no CSV), versão congelada (inalterada) e `offerings show` com `Expurgado em
<purged_at>.`**, com os estados e os eventos esperados em cada passo.

Montagem [I] (escolha deste teste): o oferecimento, o bot, a lista, o encerramento, o arquivamento e o expurgo pelas
folhas do CLI; o que só o Telegram e o worker produzem (o vínculo, as respostas, a mensagem livre e a resposta
fixa com o contato) por SQL, pelos mesmos ajudantes da semente do expurgo (`tests/helpers/purge.py`, no molde do
`World` dos relatórios): o caminho do disparo pelo motor e pelo worker já tem o seu roteiro próprio, e aqui
importa o estado depois do fechamento. Os passos do worker, como na drenagem da semente: a `bot.sync_profile`
(da ativação, do encerramento e do arquivamento) pelo tratador do perfil do bot com a Bot API falsa (`_drain_profiles`); o
congelamento pelo `reports.freeze_report` dos relatórios (o mesmo que a tarefa `report.freeze` chama; `_freeze`); a
revelação devida e a fila do bot pelo estado final que os tratadores do disparo e o worker do bot gravam
(`revealed_at` da entrega e a mensagem `sent`), por SQL, como a semente grava a fila já terminada.

Números do roteiro (calculados à mão): duas pessoas neutras, só deste oferecimento, cada uma com um endereço
`active` no bot; três updates com texto (as duas respostas e a mensagem livre); uma resposta fixa com o contato
(`outbox_redacted = 1`); nenhuma mensagem livre cita identidade (`free_messages_redacted = 0`). Pendências
depois do arquivamento: `outbox = 1` (a resposta fixa ainda na fila), `updates = 0`, `tasks` = as
`bot.sync_profile` vivas do bot (contadas no banco), `freeze = 1` (o disparo fechado sem versão `closing`) e
`due = 1` (a resposta de B sem revelação).

No esqueleto, a folha `offerings purge prepare` não existe: a função
falha no primeiro `purge prepare` (código 2, comando desconhecido, no lugar do 1 com a pendência).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import psycopg

from tests.helpers.api import ADMIN_TOKEN, all_events, write_roster
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_matches
from tests.helpers.purge import (
    _TWO_PARTS,
    _activate_student,
    _answered,
    _dispatch,
    _drain_profiles,
    _endpoint,
    _fixed_reply,
    _free_message,
    _freeze,
    _part,
    _person_by_ra,
    _question,
    _update,
    pending_counts,
)
from tests.helpers.secrets import remember_secret

CODE = "expurgo-roteiro"
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
CONTACT = "contato.roteiro@docente-roteiro.example.org"
PEOPLE = (
    {"name": "Pessoa Roteiro Primeira", "ra": "4450000001", "email": "pessoa.roteiro.primeira@turma.example.org"},
    {"name": "Pessoa Roteiro Segunda", "ra": "4450000002", "email": "pessoa.roteiro.segunda@turma.example.org"},
)
CHATS = (9_500_000_001, 9_500_000_002)
CODE_TTL = timedelta(minutes=10)
PREPARE_RE = re.compile(r"Código de confirmação \(válido até (\S+)\): ([A-Za-z0-9_-]{22})")
PURGE_TYPES = ("offering.purge_prepared", "offering.purged", "report.frozen")


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> str:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""
    return result.stdout


def _ok_json(result: Any) -> dict:
    return json.loads(_ok(result))


def _pending_line(pending: dict) -> str:
    return (f"  pendente: {pending['outbox']} na fila de saída, {pending['updates']} updates, {pending['tasks']} "
            f"tarefas, {pending['freeze']} relatórios por congelar, {pending['due']} envios devidos (revelações e "
            "avisos).")


def _refused(cli_invoke: Any, pending: dict) -> None:
    """`offerings purge prepare` recusado com `purge_pending`: código 1, stdout vazio, a mensagem e a linha da
    pendência; com `--json`, o corpo de erro da API com o `pending` exato."""
    result = cli_invoke(["offerings", "purge", "prepare", CODE])
    _no_traceback(result)
    assert result.exit_code == 1, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    assert result.stderr == _out(f"Erro: {MESSAGES['purge_pending']}", _pending_line(pending))
    result = cli_invoke(["offerings", "purge", "prepare", CODE], json=True)
    _no_traceback(result)
    assert result.exit_code == 1 and result.stdout == ""
    body = json.loads(result.stderr)
    assert_matches("error", 409, body)
    assert body == {"error": {"code": "purge_pending", "message": MESSAGES["purge_pending"], "pending": pending}}


def _events(client: Any, cursor: int) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "seq"}
            for e in all_events(client, ADMIN_TOKEN, *PURGE_TYPES) if e["seq"] > cursor]


def _cursor(sql: psycopg.Connection) -> int:
    return int(sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])


def _pending(**counts: int) -> dict:
    return {"outbox": 0, "updates": 0, "tasks": 0, "freeze": 0, "due": 0, **counts}


# --------------------------------------------------------------------------------------------------
# O roteiro


def test_purge_end_to_end_by_cli(cli_invoke: Any, client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any,
                                 tmp_path: Any) -> None:
    """Roteiro de ponta a ponta do expurgo: o roteiro do comentário do módulo, com o texto
    exato de cada folha nova, a pendência caindo a cada passo do worker e os eventos do
    expurgo (`offering.purge_prepared` só na preparação aceita; um `offering.purged`)."""
    # 1. Oferecimento com contato, bot e lista pelo CLI.
    created = _ok_json(cli_invoke(["offerings", "create", CODE, "--course", COURSE, "--term", TERM, "--timezone",
                                   "America/Sao_Paulo", "--contact-email", CONTACT], json=True))
    assert (created["code"], created["state"], created["contact_email_set"]) == (CODE, "draft", True)
    token = bot_api.token_of(bot_api.add_bot())
    remember_secret(token)
    token_file = tmp_path / "bot-token.txt"
    token_file.write_text(token, encoding="utf-8")
    _ok(cli_invoke(["bots", "register", CODE, "--token-file", str(token_file)]))
    _ok(cli_invoke(["bots", "activate", CODE]))
    roster = write_roster(tmp_path / "lista.csv", PEOPLE)
    imported = _ok(cli_invoke(["students", "import", CODE, "--file", str(roster)]))
    assert imported == _out(f"Lista importada em {CODE}: 2 linhas; 2 pessoas novas, 0 já cadastradas; "
                            "2 matrículas novas, 0 já matriculados.")

    # 2. O que o Telegram e o worker gravariam (por SQL, ajudantes da semente do expurgo).
    offering_row = int(sql.execute("SELECT id FROM offerings WHERE code = %s", (CODE,)).fetchone()[0])
    bot_row = int(sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0])
    now = clock.now()
    early, opens, closes = now - timedelta(hours=3), now - timedelta(hours=2), now - timedelta(hours=1)
    people = [_person_by_ra(sql, person["ra"]) for person in PEOPLE]
    enrollments = [_activate_student(sql, row, offering_row, early) for row, _, _ in people]
    endpoints = [_endpoint(sql, row, bot_row, chat, "active", early) for (row, _, _), chat in zip(people, CHATS)]
    question = _question(sql, "duas-partes", early, ("texto", "numero"))
    dispatch_row, dispatch_id = _dispatch(sql, offering_row, question, state="closed", opens=opens, closes=closes)
    deliveries = []
    answers = ({"texto": "azul", "numero": "6.5"}, {"texto": "verde", "numero": "7"})
    for (row, _, _), enrollment, endpoint, chat, raw, score in zip(people, enrollments, endpoints, CHATS, answers,
                                                                   (1.0, 0.0)):
        delivery, _, _ = _answered(
            sql, dispatch_row=dispatch_row, enrollment_row=enrollment, person_row=row, bot_row=bot_row,
            endpoint_row=endpoint, chat_id=chat, question=_TWO_PARTS, raw=raw, text=raw["texto"], score=score,
            part_derived=[_part(0), _part(1, ratio=float(raw["numero"]) / 6.5, residual=float(raw["numero"]) - 6.5)],
            opens=opens, answered=opens + timedelta(minutes=30), revealed=closes + timedelta(minutes=1))
        deliveries.append(delivery)
    sql.execute("UPDATE deliveries SET revealed_at = NULL WHERE id = %s", (deliveries[1],))
    asked = _update(sql, bot_row, chat_id=CHATS[0], text="Quando sai a próxima questão?", outcome="free_message",
                    at=closes)
    _free_message(sql, offering_row, endpoints[0], asked, "Quando sai a próxima questão?", closes, replied=True)
    reply = _fixed_reply(sql, bot_row, asked, CONTACT, closes, state="pending")

    # 3. Encerramento e arquivamento pelo CLI.
    _ok(cli_invoke(["offerings", "close", CODE]))
    prepared_archive = _ok(cli_invoke(["offerings", "archive", "prepare", CODE]))
    archive_code = PREPARE_RE.search(prepared_archive)
    assert archive_code is not None
    _ok(cli_invoke(["offerings", "archive", "confirm", CODE, "--confirmation", archive_code.group(2)]))
    shown = _ok_json(cli_invoke(["offerings", "show", CODE], json=True))
    assert shown["state"] == "archived"
    cursor = _cursor(sql)

    # 4. Antes dos passos do worker: recusado, com a pendência exata.
    alive = int(sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'bot.sync_profile' "
                            "AND state IN ('pending', 'leased') AND payload->>'bot_id' = %s",
                            (str(bot_row),)).fetchone()[0])
    assert alive >= 1
    expected = _pending(outbox=1, tasks=alive, freeze=1, due=1)
    assert pending_counts(sql, offering_row) == expected
    _refused(cli_invoke, expected)

    # 5. Os passos do worker, um a um, com a pendência caindo.
    assert _drain_profiles(sql, [bot_row]) == alive
    expected = _pending(outbox=1, freeze=1, due=1)
    _refused(cli_invoke, expected)
    _freeze(sql, dispatch_row, clock.now())
    expected = _pending(outbox=1, due=1)
    _refused(cli_invoke, expected)
    sql.execute("UPDATE deliveries SET revealed_at = %s WHERE id = %s", (clock.now(), deliveries[1]))
    expected = _pending(outbox=1)
    _refused(cli_invoke, expected)
    sql.execute("UPDATE outbox_messages SET state = 'sent', attempts = 1, sent_at = %s, channel_ref = 'enviada' "
                "WHERE id = %s", (clock.now(), reply))
    assert pending_counts(sql, offering_row) == _pending()
    shown = _ok_json(cli_invoke(["offerings", "show", CODE], json=True))
    assert (shown["state"], shown.get("purged_at", "<ausente>")) == ("archived", None)
    events = _events(client, cursor)
    assert [e["type"] for e in events] == ["report.frozen"]
    frozen_json = _ok_json(cli_invoke(["reports", "show", CODE, dispatch_id, "--version", "1"], json=True))
    frozen_csv = _ok(cli_invoke(["reports", "show", CODE, dispatch_id, "--version", "1", "--csv"]))
    now_before = _ok_json(cli_invoke(["reports", "show", CODE, dispatch_id], json=True))
    assert [field["unavailable"] for field in now_before["distribution"]["fields"]] == [None, None]
    cursor = _cursor(sql)

    # 6. Preparação aceita (texto e `--json`).
    clock.advance(30)
    t1 = clock.now()
    text = _ok(cli_invoke(["offerings", "purge", "prepare", CODE]))
    match = PREPARE_RE.search(text)
    assert match is not None
    assert text == _out(f"Expurgar {CODE}: 2 identidades a apagar, 0 mantidas por outro oferecimento, 2 endereços "
                        "de entrega, 3 updates, e-mail de contato sim.",
                        f"Código de confirmação (válido até {iso(t1 + CODE_TTL)}): {match.group(2)}")
    as_json = _ok_json(cli_invoke(["offerings", "purge", "prepare", CODE], json=True))
    assert_matches("offerings.prepare_purge", 200, as_json)
    impact = {"identities_to_delete": 2, "identities_kept": 0, "endpoints": 2, "updates": 3,
              "contact_email_set": True}
    assert as_json == {"offering": {"offering_id": created["offering_id"], "code": CODE}, "impact": impact,
                       "confirmation_code": as_json["confirmation_code"], "expires_at": iso(t1 + CODE_TTL)}
    assert as_json["confirmation_code"] != match.group(2)
    prepared_events = [{"type": "offering.purge_prepared", "occurred_at": iso(t1), "actor_token_id": "bootstrap",
                        "data": {"offering_id": created["offering_id"], "expires_at": iso(t1 + CODE_TTL), **impact}}]
    assert _events(client, cursor) == prepared_events * 2

    # 7. O código substituído é recusado; o vigente expurga.
    stale = cli_invoke(["offerings", "purge", "confirm", CODE, "--confirmation", match.group(2)])
    _no_traceback(stale)
    assert (stale.exit_code, stale.stdout, stale.stderr) == (1, "", _out(f"Erro: {MESSAGES['invalid_confirmation']}"))
    clock.advance(60)
    t2 = clock.now()
    confirmed = _ok(cli_invoke(["offerings", "purge", "confirm", CODE, "--confirmation", as_json["confirmation_code"]]))
    assert confirmed == _out(f"Oferecimento expurgado: {CODE} em {iso(t2)}; 2 identidades apagadas, 0 mantidas.")
    outcome = {"identities_deleted": 2, "identities_kept": 0, "endpoints": 2, "updates": 3,
               "free_messages_redacted": 0, "outbox_redacted": 1, "contact_email_deleted": True}
    assert _events(client, cursor) == prepared_events * 2 + [
        {"type": "offering.purged", "occurred_at": iso(t2), "actor_token_id": "bootstrap",
         "data": {"offering_id": created["offering_id"], **outcome}}]
    again = _ok_json(cli_invoke(["offerings", "purge", "confirm", CODE, "--confirmation", "qualquer-codigo"],
                                json=True))
    assert again == {"offering": {"offering_id": created["offering_id"], "code": CODE}, "purged_at": iso(t2),
                     "result": outcome}

    # 8. Depois do expurgo: revelação, relatórios, `offerings show` e o log.
    pseudonyms = [pseudo for _, pseudo, _ in people]
    revealed = _ok(cli_invoke(["identity", "reveal", *pseudonyms, "--reason", "conferencia do roteiro"]))
    assert sorted(revealed.splitlines()) == sorted(f"Não encontrado: {pseudo}" for pseudo in pseudonyms)
    now_after = _ok_json(cli_invoke(["reports", "show", CODE, dispatch_id], json=True))
    assert now_after["distribution"]["fields"] == [
        {**field, "unavailable": "purged", "values": None, "ratios": None, "unrated": None, "texts": None}
        for field in now_before["distribution"]["fields"]]
    assert {k: v for k, v in now_after.items() if k not in ("distribution", "computed_at")} == {
        k: v for k, v in now_before.items() if k not in ("distribution", "computed_at")}
    # O CSV pelos bytes: o `stdout` do click já normalizou o `\r\n` do CSV do relatório para `\n`.
    csv_result = cli_invoke(["reports", "show", CODE, dispatch_id, "--csv"])
    _ok(csv_result)
    csv_after = csv_result.stdout_bytes.decode("utf-8")
    assert [line for line in csv_after.split("\r\n") if line.startswith("distribution,")] == [
        "distribution,0,purged,unavailable,,", "distribution,1,purged,unavailable,,"]
    assert _ok_json(cli_invoke(["reports", "show", CODE, dispatch_id, "--version", "1"], json=True)) == frozen_json
    assert _ok(cli_invoke(["reports", "show", CODE, dispatch_id, "--version", "1", "--csv"])) == frozen_csv
    shown = _ok_json(cli_invoke(["offerings", "show", CODE], json=True))
    assert (shown["state"], shown["purged_at"], shown["contact_email_set"]) == ("archived", iso(t2), False)
    username = shown["bot"]["username"]
    assert _ok(cli_invoke(["offerings", "show", CODE])) == _out(
        f"{CODE}  archived  {COURSE}  {TERM}  bot: {username}", f"Expurgado em {iso(t2)}.")
    listed = _ok(cli_invoke(["events", "list", "--type", "offering.purge_prepared", "--type", "offering.purged",
                             "--after", str(cursor)]))
    lines = listed.splitlines()
    assert [line.split("  ")[1:] for line in lines[:-1]] == [
        [iso(t1), "offering.purge_prepared"], [iso(t1), "offering.purge_prepared"], [iso(t2), "offering.purged"]]
    assert re.fullmatch(r"Próximo cursor: \d+", lines[-1])
    assert sql.execute("SELECT count(*) FROM identities WHERE person_id = ANY(%s)",
                       ([row for row, _, _ in people],)).fetchone()[0] == 0
