"""CLI do expurgo e do filtro de eventos por pessoa.

Preparar e confirmar: `cq offerings purge prepare REF` e `cq offerings purge
confirm REF --confirmation CÓDIGO` com o texto exato do contrato e o `--json` igual ao corpo da API;
`--confirmation` obrigatório (código 2, sem eco e sem chamar a API); cada erro da API (`invalid_state`,
`purge_pending` com a linha `  pendente: …`, `invalid_confirmation`, `purge_changed`, 403 e 404) com `Erro:
<mensagem>` no stderr, stdout vazio, código 1 e sem rastreamento (com `--json`, o corpo de erro da API no
stderr); `offerings show` com a linha `Expurgado em <purged_at>.` só quando o oferecimento foi expurgado.

Filtro por pessoa: `cq events list --person REF`, com e sem `--type`, igual à API
(texto e `--json`); `cq events list` sem `--type` e sem `--person` → código 2 com `Uso incorreto: Informe --type
ou --person.`, sem chamar a API (o perfil aponta para a API inalcançável `http://127.0.0.1:9`: uma chamada daria
o código 3).

O CLI roda em processo pelo `cli_invoke` do conftest (a API dos testes por `http=`); a montagem usa a semente do
expurgo (`tests/helpers/purge.py`) e os atalhos da API. Horários pelo `FakeClock` e `iso(dt)`; o código de
confirmação vem da própria saída.

No esqueleto, as folhas `offerings purge prepare|confirm` e a opção
`--person` não existem (código 2, comando ou opção desconhecidos), e o `--type` continua obrigatório: cada função
falha na primeira invocação delas. As asserções do `offerings show` sem a linha do expurgo ficam na mesma
função de um caso que falha no esqueleto.

Interrupção: o Ctrl+C
durante a chamada à API de `offerings purge prepare|confirm` sai com 130, `Interrompido.` no stderr e o stdout
vazio. Sem esse tratamento, o `KeyboardInterrupt` vira `click.Abort` e o grupo raiz sai com o código 1.
"""

from __future__ import annotations

import json
import re
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
    create_team_link,
    create_token,
    import_students,
    register_bot,
    register_person,
)
from tests.helpers.canary import PURGE_A
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_matches
from tests.helpers.purge import make_pending, seed_purge_scenario, snapshot_purge_tables

ADMIN = auth(ADMIN_TOKEN)
CODE_TTL = timedelta(minutes=10)
PREPARE_RE = re.compile(r"Código de confirmação \(válido até (\S+)\): ([A-Za-z0-9_-]{22})\n$")
UNREACHABLE = "http://127.0.0.1:9"


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


def _failed(result: Any, code: int) -> None:
    _no_traceback(result)
    assert result.exit_code == code, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""


def _api_ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _get_offering(client: Any, ref: str) -> dict:
    """O `Offering` pela API, tolerando só a ausência de `purged_at` (a montagem; o objeto do teste é o CLI)."""
    resp = client.get(f"/v1/offerings/{ref}", headers=ADMIN)
    assert resp.status_code == 200, f"offerings.get: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches_tolerant("offerings.get", 200, body)
    return body


def _prepare_line(scenario: Any) -> str:
    impact = scenario.impact
    return (f"Expurgar {scenario.code}: {impact.identities_to_delete} identidades a apagar, {impact.identities_kept} "
            f"mantidas por outro oferecimento, {impact.endpoints} endereços de entrega, {impact.updates} updates, "
            f"e-mail de contato {'sim' if impact.contact_email_set else 'não'}.")


def _prepare_via_cli(cli_invoke: Any, scenario: Any) -> str:
    text = _ok(cli_invoke(["offerings", "purge", "prepare", scenario.code]))
    match = PREPARE_RE.search(text)
    assert match is not None, f"saída fora do formato esperado: {text!r}"
    return match.group(2)


def _pending_line(pending: dict) -> str:
    return (f"  pendente: {pending['outbox']} na fila de saída, {pending['updates']} updates, {pending['tasks']} "
            f"tarefas, {pending['freeze']} relatórios por congelar, {pending['due']} envios devidos (revelações e "
            "avisos).")


def _error_body(code: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": MESSAGES[code], **extra}}


# --------------------------------------------------------------------------------------------------
# Preparar e confirmar


def test_text_purge_prepare_and_confirm(cli_invoke: Any, client: Any, sql: psycopg.Connection, clock: Any,
                                        bot_api: Any) -> None:
    """Contrato: `offerings purge prepare` imprime `Expurgar <code>: <a> identidades a apagar, <k> mantidas
    por outro oferecimento, <e> endereços de entrega, <u> updates, e-mail de contato <sim|não>.` e `Código de
    confirmação (válido até <expires_at>): <código>` (o código de 22 caracteres; `expires_at = now + 10 min`);
    `offerings purge confirm` imprime `Oferecimento expurgado: <code> em <purged_at>; <a> identidades apagadas,
    <k> mantidas.`. Código 0, stderr vazio."""
    scenario = seed_purge_scenario(client, sql)
    t0 = clock.now()
    text = _ok(cli_invoke(["offerings", "purge", "prepare", scenario.code]))
    match = PREPARE_RE.search(text)
    assert match is not None, f"saída fora do formato esperado: {text!r}"
    code = match.group(2)
    assert text == _out(_prepare_line(scenario), f"Código de confirmação (válido até {iso(t0 + CODE_TTL)}): {code}")
    clock.advance(45)
    t1 = clock.now()

    confirmed = _ok(cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation", code]))
    outcome = scenario.outcome
    assert confirmed == _out(f"Oferecimento expurgado: {scenario.code} em {iso(t1)}; {outcome.identities_deleted} "
                             f"identidades apagadas, {outcome.identities_kept} mantidas.")
    # Repetida, com um código qualquer: o resultado gravado, o mesmo texto.
    again = _ok(cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation", "codigo-qualquer"]))
    assert again == confirmed


def test_json_purge_prepare_and_confirm_equal_the_api_body(cli_invoke: Any, client: Any, sql: psycopg.Connection,
                                                           clock: Any, bot_api: Any) -> None:
    """O `--json` igual ao corpo da API: a preparação com `--json` imprime o corpo de
    `offerings.prepare_purge` (o esquema congelado; o impacto exato da semente) e a confirmação com `--json`, o
    corpo de `offerings.purge`, igual ao que a API devolve na repetição."""
    scenario = seed_purge_scenario(client, sql)
    t0 = clock.now()
    prepared = json.loads(_ok(cli_invoke(["offerings", "purge", "prepare", scenario.code], json=True)))
    assert_matches("offerings.prepare_purge", 200, prepared)
    assert prepared == {"offering": {"offering_id": scenario.offering_id, "code": scenario.code},
                        "impact": {"identities_to_delete": scenario.impact.identities_to_delete,
                                   "identities_kept": scenario.impact.identities_kept,
                                   "endpoints": scenario.impact.endpoints, "updates": scenario.impact.updates,
                                   "contact_email_set": scenario.impact.contact_email_set},
                        "confirmation_code": prepared["confirmation_code"], "expires_at": iso(t0 + CODE_TTL)}
    confirmed = json.loads(_ok(cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation",
                                           prepared["confirmation_code"]], json=True)))
    assert_matches("offerings.purge", 200, confirmed)
    api = _api_ok(client.post(f"/v1/offerings/{scenario.code}/purge", json={"confirmation_code": "qualquer-codigo"},
                              headers=ADMIN), "offerings.purge")
    assert confirmed == api
    assert confirmed["purged_at"] == iso(t0)


def test_confirm_requires_the_confirmation_option(cli_invoke: Any, client: Any, sql: psycopg.Connection,
                                                  clock: Any, bot_api: Any) -> None:
    """`--confirmation` obrigatório, como em `offerings archive confirm`, e uso incorreto: sem a
    opção → código 2 e `Uso incorreto: falta a opção --confirmation.`; com a opção sem valor → `Uso incorreto:
    falta o valor da opção --confirmation.`; nos dois modos, sem eco e sem chamar a API (o código preparado
    continua o mesmo e nada muda)."""
    scenario = seed_purge_scenario(client, sql)
    _prepare_via_cli(cli_invoke, scenario)
    before = snapshot_purge_tables(sql)
    for json_mode in (False, True):
        result = cli_invoke(["offerings", "purge", "confirm", scenario.code], json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out("Uso incorreto: falta a opção --confirmation.")
        result = cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation"], json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out("Uso incorreto: falta o valor da opção --confirmation.")
    assert snapshot_purge_tables(sql) == before


# --------------------------------------------------------------------------------------------------
# Erros


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_invalid_state_and_not_found(json_mode: bool, cli_invoke: Any, client: Any, sql: psycopg.Connection,
                                     clock: Any) -> None:
    """Mensagens exatas e erros: preparar e confirmar num rascunho → código 1, `Erro: <mensagem>` e
    `Estado atual: draft`; num oferecimento inexistente → `Erro: Recurso não encontrado.`; com `--json`, o corpo
    de erro da API no stderr. Stdout vazio, sem rastreamento."""
    draft = create_offering(client, ADMIN_TOKEN)
    for args in (["offerings", "purge", "prepare", draft["code"]],
                 ["offerings", "purge", "confirm", draft["code"], "--confirmation", "codigo-qualquer"]):
        result = cli_invoke(args, json=json_mode)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == _error_body("invalid_state", state="draft")
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['invalid_state']}", "Estado atual: draft")
    for args in (["offerings", "purge", "prepare", "codigo-inexistente"],
                 ["offerings", "purge", "confirm", "codigo-inexistente", "--confirmation", "codigo-qualquer"]):
        result = cli_invoke(args, json=json_mode)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == _error_body("not_found")
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['not_found']}")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_purge_pending_shows_the_pending_line(json_mode: bool, cli_invoke: Any, client: Any,
                                              sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Regra (`purge_pending` no CLI: `Erro: <mensagem>` e uma linha `  pendente: <n> na fila de saída,
    <n> updates, <n> tarefas, <n> relatórios por congelar, <n> envios devidos (revelações e avisos).` no stderr,
    código 1): com uma resposta na fila do bot e a revelação devida, a preparação e a confirmação são recusadas
    com a linha exata; com `--json`, o corpo de erro da API com o `pending` exato e sem a linha."""
    scenario = seed_purge_scenario(client, sql)
    code = _prepare_via_cli(cli_invoke, scenario)
    make_pending(sql, "outbox_pending", scenario)
    make_pending(sql, "reveal_without_task", scenario)
    pending = {"outbox": 1, "updates": 0, "tasks": 0, "freeze": 0, "due": 1}
    for args in (["offerings", "purge", "prepare", scenario.code],
                 ["offerings", "purge", "confirm", scenario.code, "--confirmation", code]):
        result = cli_invoke(args, json=json_mode)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == _error_body("purge_pending", pending=pending)
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['purge_pending']}", _pending_line(pending))


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_invalid_confirmation_and_purge_changed(json_mode: bool, cli_invoke: Any, client: Any,
                                                sql: psycopg.Connection, clock: Any, bot_api: Any) -> None:
    """Mensagens exatas: um código inventado → `Erro: O código de confirmação é inválido ou venceu.`, sem eco
    do código; depois da preparação, a matrícula de `aluno_a` noutro oferecimento ativo muda as contagens e a
    confirmação com o código válido → `Erro: <mensagem de purge_changed>`. Código 1, stdout vazio; com `--json`,
    o corpo de erro da API. Nada é expurgado."""
    scenario = seed_purge_scenario(client, sql)
    other = scenario.other
    assert other is not None
    code = _prepare_via_cli(cli_invoke, scenario)
    result = cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation", "codigosentinela-errado"],
                        json=json_mode)
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == _error_body("invalid_confirmation")
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['invalid_confirmation']}")
    assert "codigosentinela" not in result.stderr

    import_students(client, ADMIN_TOKEN, other.code, [PURGE_A.row()])
    result = cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation", code], json=json_mode)
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == _error_body("purge_changed")
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['purge_changed']}")
    assert sql.execute("SELECT purged_at FROM offerings WHERE id = %s", (scenario.offering_row,)).fetchone()[0] is None


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_forbidden_without_people_write(json_mode: bool, cli_invoke: Any, client: Any, sql: psycopg.Connection,
                                        clock: Any, bot_api: Any) -> None:
    """Permissões e forma dos erros: com um token sem `people:write`, as duas folhas → código 1, `Erro:
    <mensagem>` e `Permissão necessária: people:write`; com `--json`, o corpo de erro da API; nada muda."""
    scenario = seed_purge_scenario(client, sql)
    weak, _ = create_token(client, ADMIN_TOKEN, ["offerings:write", "offerings:read"])
    before = snapshot_purge_tables(sql)
    for args in (["offerings", "purge", "prepare", scenario.code],
                 ["offerings", "purge", "confirm", scenario.code, "--confirmation", "codigo-qualquer"]):
        result = cli_invoke(args, json=json_mode, token=weak)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == _error_body("forbidden", permission="people:write")
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: people:write")
        assert weak not in result.stderr
    assert snapshot_purge_tables(sql) == before


def test_offerings_show_has_the_purged_line_only_when_purged(cli_invoke: Any, client: Any, sql: psycopg.Connection,
                                                             clock: Any, bot_api: Any) -> None:
    """Regra (`offerings show` tem a linha `Expurgado em <purged_at>.` só quando
    preenchido): antes do expurgo, só a linha do oferecimento; depois, a mesma linha e `Expurgado em
    <purged_at>.`; outro oferecimento continua com uma linha só."""
    scenario = seed_purge_scenario(client, sql)
    shown = _get_offering(client, scenario.code)
    line = (f"{scenario.code}  archived  {shown['course']}  {shown['term']}  bot: {shown['bot']['username']}")
    assert _ok(cli_invoke(["offerings", "show", scenario.code])) == _out(line)
    code = _prepare_via_cli(cli_invoke, scenario)
    clock.advance(12)
    t1 = clock.now()
    _ok(cli_invoke(["offerings", "purge", "confirm", scenario.code, "--confirmation", code]))
    assert _ok(cli_invoke(["offerings", "show", scenario.code])) == _out(line, f"Expurgado em {iso(t1)}.")
    other = scenario.other
    assert other is not None
    other_shown = _get_offering(client, other.code)
    assert _ok(cli_invoke(["offerings", "show", other.code])) == _out(
        f"{other.code}  active  {other_shown['course']}  {other_shown['term']}  bot: {other_shown['bot']['username']}")


# --------------------------------------------------------------------------------------------------
# Ctrl+C nas folhas do expurgo


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("leaf", ["prepare", "confirm"])
def test_interrupt_in_purge_leaves(leaf: str, json_mode: bool, cli_invoke: Any, monkeypatch: Any) -> None:
    """Os comandos do expurgo capturam o `KeyboardInterrupt` (→ 130), como manda o
    contrato (interrupção → `Interrompido.`, código 130): o Ctrl+C durante a chamada à API de `offerings
    purge prepare` e de `offerings purge confirm`, nos modos texto e `--json`, sai com o código 130, a mensagem
    fixa `Interrompido.` (a mesma dos comandos locais, `INTERRUPTED_MESSAGE`) como a única linha não vazia do
    stderr, o stdout vazio e sem rastreamento. O `KeyboardInterrupt` vem de um dublê do método do `ApiClient`,
    que registra a chamada (a interrupção acontece mesmo na chamada à API)."""
    from cq_client.api import ApiClient
    from cq_client.local_commands import INTERRUPTED_MESSAGE

    calls: list[tuple[str, ...]] = []

    def interrupted_prepare(self: Any, ref: str) -> dict:
        calls.append((ref,))
        raise KeyboardInterrupt

    def interrupted_purge(self: Any, ref: str, *, confirmation_code: str) -> dict:
        calls.append((ref, confirmation_code))
        raise KeyboardInterrupt

    monkeypatch.setattr(ApiClient, "prepare_purge", interrupted_prepare)
    monkeypatch.setattr(ApiClient, "purge_offering", interrupted_purge)
    ref = "OFERTA-INTERROMPIDA"
    if leaf == "prepare":
        args, expected = ["offerings", "purge", "prepare", ref], [(ref,)]
    else:
        code = "c" * 22
        args, expected = ["offerings", "purge", "confirm", ref, "--confirmation", code], [(ref, code)]
    result = cli_invoke(args, json=json_mode)
    assert calls == expected
    _failed(result, 130)
    assert result.stderr.endswith("\n")
    assert [line for line in result.stderr.splitlines() if line] == [INTERRUPTED_MESSAGE]


# --------------------------------------------------------------------------------------------------
# `events list --person`


def _setup_filter(client: Any, bot_api: Any) -> tuple[dict, dict]:
    """Duas pessoas cadastradas e um link de equipe de cada uma num oferecimento ativo."""
    first = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra Do Filtro")
    second = register_person(client, ADMIN_TOKEN, name="Pessoa Neutra De Fora")
    offering = create_offering(client, ADMIN_TOKEN)
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot_api.add_bot()))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    create_team_link(client, ADMIN_TOKEN, offering["code"], first["alias"])
    create_team_link(client, ADMIN_TOKEN, offering["code"], second["alias"])
    return first, second


def _citing(events: list[dict], pseudo_id: str) -> list[dict]:
    return [e for e in events if pseudo_id in (e["data"].get("pseudo_id"), e["data"].get("person"))]


def _event_lines(items: list[dict], next_cursor: int) -> str:
    return _out(*(f"{e['seq']}  {e['occurred_at']}  {e['type']}" for e in items), f"Próximo cursor: {next_cursor}")


@pytest.mark.parametrize("by", ["alias", "pseudo_id"])
def test_events_list_by_person(by: str, cli_invoke: Any, client: Any, clock: Any, bot_api: Any) -> None:
    """Contrato (`cq events list [--type T]... [--person REF]`, `--type` não obrigatório): com
    `--person` (pelo apelido e pelo pseudônimo), só os eventos que citam a pessoa, em ordem de `seq`, no texto
    (`<seq>  <occurred_at>  <type>` e `Próximo cursor: <n>`) e no `--json` igual ao corpo da API; com
    `--type team.link_created`, a interseção; a outra pessoa não aparece."""
    first, second = _setup_filter(client, bot_api)
    events = all_events(client, ADMIN_TOKEN, "person.registered", "team.link_created")
    mine = _citing(events, first["pseudo_id"])
    assert [e["type"] for e in mine] == ["person.registered", "team.link_created"]
    ref = first[by]

    body = json.loads(_ok(cli_invoke(["events", "list", "--person", ref], json=True)))
    assert_matches("events.list", 200, body)
    assert body == {"items": mine, "next_cursor": mine[-1]["seq"]}
    assert _ok(cli_invoke(["events", "list", "--person", ref])) == _event_lines(mine, mine[-1]["seq"])
    only_links = [e for e in mine if e["type"] == "team.link_created"]
    body = json.loads(_ok(cli_invoke(["events", "list", "--person", ref, "--type", "team.link_created"], json=True)))
    assert body == {"items": only_links, "next_cursor": only_links[-1]["seq"]}
    assert _ok(cli_invoke(["events", "list", "--type", "team.link_created", "--person", ref])) == _event_lines(
        only_links, only_links[-1]["seq"])
    assert second["pseudo_id"] not in json.dumps(body)


def test_events_list_requires_type_or_person(cli_invoke: Any, client: Any, clock: Any) -> None:
    """Validação antes de qualquer pedido: `events list` sem `--type` e sem `--person` →
    código 2 e `Uso incorreto: Informe --type ou --person.`, nos dois modos, sem chamar a API (o perfil aponta
    para a API inalcançável; uma chamada daria o código 3); com `--type` só, o comando chama a API
    (controle: a mesma API inalcançável dá o código 3)."""
    env = {"CQ_API_URL": UNREACHABLE}
    for json_mode in (False, True):
        result = cli_invoke(["events", "list"], json=json_mode, http=None, env=env)
        _failed(result, 2)
        assert result.stderr == _out("Uso incorreto: Informe --type ou --person.")
        result = cli_invoke(["events", "list", "--after", "0", "--limit", "5"], json=json_mode, http=None, env=env)
        _failed(result, 2)
        assert result.stderr == _out("Uso incorreto: Informe --type ou --person.")
    control = cli_invoke(["events", "list", "--type", "person.registered"], http=None, env=env)
    _failed(control, 3)
