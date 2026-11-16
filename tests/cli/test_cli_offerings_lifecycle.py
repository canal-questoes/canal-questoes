"""CLI `cq offerings close` e `cq offerings archive prepare/confirm`.

Cobre o encerramento e o arquivamento pelo CLI: o texto exato para pessoas
do CLI, o `--json` igual ao corpo da API, e os erros: no 409 `invalid_state`, depois de `Erro: <message>`,
a linha `Estado atual: <state>`; os demais na forma comum do CLI (código 1; `--json`: o
corpo de erro da API no stderr).

O CLI roda em processo pelo `cli_invoke` do conftest. A montagem (cadastro, registro do bot na Bot API
falsa e ativação) usa os atalhos da API; o código de confirmação é aleatório e vem da própria saída.
Horários pelo `FakeClock` e `iso(dt)`.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import pytest

from tests.helpers.api import ADMIN_TOKEN, activate_bot, auth, create_offering, register_bot
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error, assert_matches

ADMIN = auth(ADMIN_TOKEN)
CONFIRMATION = timedelta(minutes=10)
PREPARE_RE = re.compile(r"Arquivar (\S+): (\d+) na equipe, (\d+) links pendentes\.\n"
                        r"Código de confirmação \(válido até (\S+)\): ([A-Za-z0-9_-]{22})\n")


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _failed(result: Any, code: int) -> None:
    _no_traceback(result)
    assert result.exit_code == code, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""


def _api_get(client: Any, ref: str) -> dict:
    resp = client.get(f"/v1/offerings/{ref}", headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


def _active_offering(client: Any, bot_api: Any, clock: Any) -> tuple[dict, str]:
    """Um oferecimento ativo, com bot registrado na Bot API falsa; devolve o `Offering` e o
    `username` do bot."""
    offering = create_offering(client, ADMIN_TOKEN, course="Cálculo Numérico", term="2031 s1")
    telegram_bot_id = bot_api.add_bot()
    bot = register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(telegram_bot_id))
    clock.advance(5)
    active = activate_bot(client, ADMIN_TOKEN, offering["code"])
    assert active == {**offering, "state": "active", "activated_at": iso(clock.now()),
                      "bot": {"username": bot["username"], "telegram_bot_id": telegram_bot_id,
                              "state": "registered", "profile_state": "pending"}}
    return active, bot["username"]


def _offering_line(body: dict) -> str:
    """`<code>  <state>  <course>  <term>  bot: <username ou "-">`."""
    bot = "-" if body["bot"] is None else body["bot"]["username"]
    return f"{body['code']}  {body['state']}  {body['course']}  {body['term']}  bot: {bot}"


def _archive_via_api(client: Any, ref: str) -> dict:
    code = _prepare_via_api(client, ref)["confirmation_code"]
    resp = client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": code}, headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.archive", 200, body)
    return body


def _prepare_via_api(client: Any, ref: str) -> dict:
    resp = client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.prepare_archive", 200, body)
    return body


# --------------------------------------------------------------------------------------------------
# Encerrar (`offerings close` → a linha do oferecimento)


def test_text_offerings_close(cli_invoke, client, clock, bot_api):
    """`cq offerings close REF` → `<code>  closed  <course>
    <term>  bot: <username>`; o oferecimento fica `closed` com o horário; encerrar de novo sai igual
    (transição repetida)."""
    active, username = _active_offering(client, bot_api, clock)
    clock.advance(60)
    closed_at = iso(clock.now())
    result = cli_invoke(["offerings", "close", active["code"]])
    _ok(result)
    assert result.stdout == _out(f"{active['code']}  closed  Cálculo Numérico  2031 s1  bot: {username}")
    api = _api_get(client, active["code"])
    assert api == {**active, "state": "closed", "closed_at": closed_at}

    clock.advance(30)
    again = cli_invoke(["offerings", "close", active["offering_id"]])
    _ok(again)
    assert again.stdout == result.stdout
    assert _api_get(client, active["code"]) == api


def test_json_offerings_close(cli_invoke, client, clock, bot_api):
    """Contrato (`--json`: o corpo da API): `cq --json offerings close` imprime o
    `Offering` encerrado, igual ao da consulta."""
    active, _ = _active_offering(client, bot_api, clock)
    clock.advance(60)
    result = cli_invoke(["offerings", "close", active["code"]], json=True)
    _ok(result)
    api = _api_get(client, active["code"])
    assert api == {**active, "state": "closed", "closed_at": iso(clock.now())}
    assert json.loads(result.stdout) == api


# --------------------------------------------------------------------------------------------------
# Arquivar em duas fases (`archive prepare` e `archive confirm --confirmation`)


def test_text_archive_prepare_and_confirm(cli_invoke, client, clock, bot_api):
    """`cq offerings archive prepare REF` → `Arquivar <code>:
    <team_members_active> na equipe, <pending_links> links pendentes.` e `Código de confirmação (válido
    até <expires_at>): <confirmation_code>`; `cq offerings archive confirm REF --confirmation CÓDIGO`
    → `Oferecimento arquivado: <code> em <archived_at>.`; confirmar de novo, com qualquer código, sai
    igual, com o `archived_at` original (num encerrado com bot e num rascunho sem bot)."""
    active, _ = _active_offering(client, bot_api, clock)
    draft = create_offering(client, ADMIN_TOKEN)
    clock.advance(60)
    _ok(cli_invoke(["offerings", "close", active["code"]]))

    for offering in (active, draft):
        ref = offering["code"]
        before = _api_get(client, ref)
        clock.advance(30)
        prepared_at = clock.now()
        prepare = cli_invoke(["offerings", "archive", "prepare", ref])
        _ok(prepare)
        match = PREPARE_RE.fullmatch(prepare.stdout)
        assert match, f"saída fora do formato: {prepare.stdout!r}"
        code = match.group(5)
        assert prepare.stdout == _out(f"Arquivar {ref}: 0 na equipe, 0 links pendentes.",
                                      f"Código de confirmação (válido até {iso(prepared_at + CONFIRMATION)}): {code}")

        clock.advance(20)
        archived_at = iso(clock.now())
        confirm = cli_invoke(["offerings", "archive", "confirm", ref, "--confirmation", code])
        _ok(confirm)
        assert confirm.stdout == _out(f"Oferecimento arquivado: {ref} em {archived_at}.")
        api = _api_get(client, ref)
        assert api == {**before, "state": "archived", "archived_at": archived_at}

        clock.advance(3600)
        again = cli_invoke(["offerings", "archive", "confirm", offering["offering_id"],
                            "--confirmation", "qualquer-codigo"])
        _ok(again)
        assert again.stdout == confirm.stdout
        assert _api_get(client, ref) == api


def test_json_archive_prepare_and_confirm(cli_invoke, client, clock, bot_api):
    """Contrato (`--json`: o corpo da API): `cq --json offerings archive prepare`
    imprime o resumo exato (`offering`, contagens, código e validade) e `cq --json offerings archive
    confirm` o `Offering` arquivado, igual ao da consulta."""
    active, _ = _active_offering(client, bot_api, clock)
    clock.advance(60)
    close = cli_invoke(["offerings", "close", active["code"]], json=True)
    _ok(close)
    closed = json.loads(close.stdout)
    clock.advance(30)
    prepared_at = clock.now()
    prepare = cli_invoke(["offerings", "archive", "prepare", active["code"]], json=True)
    _ok(prepare)
    body = json.loads(prepare.stdout)
    assert_matches("offerings.prepare_archive", 200, body)
    assert body == {"offering": closed, "team_members_active": 0, "pending_links": 0,
                    "confirmation_code": body["confirmation_code"],
                    "expires_at": iso(prepared_at + CONFIRMATION)}
    assert closed == _api_get(client, active["code"])

    clock.advance(20)
    confirm = cli_invoke(["offerings", "archive", "confirm", active["code"], "--confirmation",
                          body["confirmation_code"]], json=True)
    _ok(confirm)
    api = _api_get(client, active["code"])
    assert api == {**closed, "state": "archived", "archived_at": iso(clock.now())}
    assert json.loads(confirm.stdout) == api


# --------------------------------------------------------------------------------------------------
# Erros (no 409 `invalid_state`, a linha `Estado atual: <state>`)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_invalid_state_shows_the_current_state(json_mode, cli_invoke, client, clock, bot_api):
    """Erro de estado (no 409 `invalid_state`, depois de `Erro: <message>`, a linha `Estado
    atual: <state>`): encerrar um rascunho (`draft`), preparar e confirmar o arquivamento de um ativo
    (`active`) e encerrar e preparar um arquivado (`archived`) → código 1, saída padrão vazia e o
    stderr exato; com `--json`, o corpo de erro da API (com `state`); nada muda."""
    active, _ = _active_offering(client, bot_api, clock)
    draft = create_offering(client, ADMIN_TOKEN)
    archived = create_offering(client, ADMIN_TOKEN)
    archived_body = _archive_via_api(client, archived["code"])
    assert archived_body == {**archived, "state": "archived", "archived_at": iso(clock.now())}
    cases = [
        (["offerings", "close", draft["code"]], "draft"),
        (["offerings", "archive", "prepare", active["code"]], "active"),
        (["offerings", "archive", "confirm", active["code"], "--confirmation", "codigo-qualquer"], "active"),
        (["offerings", "close", archived["code"]], "archived"),
        (["offerings", "archive", "prepare", archived["code"]], "archived"),
    ]
    for args, state in cases:
        result = cli_invoke(args, json=json_mode)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == {"error": {
                "code": "invalid_state", "message": MESSAGES["invalid_state"], "state": state}}
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['invalid_state']}", f"Estado atual: {state}")
    assert_error(client.post(f"/v1/offerings/{draft['code']}/close", headers=ADMIN), 409, "invalid_state",
                 state="draft")
    assert _api_get(client, draft["code"]) == draft
    assert _api_get(client, active["code"]) == active
    assert _api_get(client, archived["code"]) == archived_body


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_invalid_confirmation_error(json_mode, cli_invoke, client):
    """confirmar com um código errado → código 1 e `Erro: O código de
    confirmação é inválido ou venceu.`, sem a linha de estado e sem eco do código; com `--json`, o
    corpo de erro da API; o oferecimento continua em rascunho."""
    draft = create_offering(client, ADMIN_TOKEN)
    _prepare_via_api(client, draft["code"])
    result = cli_invoke(["offerings", "archive", "confirm", draft["code"], "--confirmation",
                         "codigosentinela-errado"], json=json_mode)
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "invalid_confirmation",
                                                       "message": MESSAGES["invalid_confirmation"]}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['invalid_confirmation']}")
    assert "codigosentinela" not in result.stderr
    assert _api_get(client, draft["code"]) == draft


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_lifecycle_not_found_and_malformed_ref(json_mode, cli_invoke, client):
    """Forma geral dos erros: os três comandos com um código inexistente → código 1 e `Erro:
    Recurso não encontrado.`; com `ref` malformado → `Erro: Pedido inválido.` e `  - ref:
    invalid_format`, sem eco."""
    for args in (["offerings", "close"], ["offerings", "archive", "prepare"]):
        missing = cli_invoke([*args, "codigo-inexistente"], json=json_mode)
        _failed(missing, 1)
        if json_mode:
            assert json.loads(missing.stderr) == {"error": {"code": "not_found", "message": MESSAGES["not_found"]}}
        else:
            assert missing.stderr == _out(f"Erro: {MESSAGES['not_found']}")
        malformed = cli_invoke([*args, "REFSENTINELA"], json=json_mode)
        _failed(malformed, 1)
        if json_mode:
            assert json.loads(malformed.stderr) == {"error": {
                "code": "invalid_request", "message": MESSAGES["invalid_request"],
                "fields": [{"field": "ref", "reason": "invalid_format"}]}}
        else:
            assert malformed.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - ref: invalid_format")
        assert "refsentinela" not in malformed.stderr.casefold()
    confirm = cli_invoke(["offerings", "archive", "confirm", "codigo-inexistente", "--confirmation", "x"],
                         json=json_mode)
    _failed(confirm, 1)
    if json_mode:
        assert json.loads(confirm.stderr) == {"error": {"code": "not_found", "message": MESSAGES["not_found"]}}
    else:
        assert confirm.stderr == _out(f"Erro: {MESSAGES['not_found']}")


def test_archive_confirm_requires_confirmation_option(cli_invoke, client):
    """Uso incorreto: `offerings archive confirm REF` sem `--confirmation` → código 2 e
    `Uso incorreto: falta a opção --confirmation.`, sem chamar a API (o rascunho preparado continua);
    na mesma função, `cq offerings archive confirm --help` sai com 0."""
    draft = create_offering(client, ADMIN_TOKEN)
    _prepare_via_api(client, draft["code"])
    for json_mode in (False, True):
        result = cli_invoke(["offerings", "archive", "confirm", draft["code"]], json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out("Uso incorreto: falta a opção --confirmation.")
    assert _api_get(client, draft["code"]) == draft
    help_result = cli_invoke(["offerings", "archive", "confirm", "--help"])
    _ok(help_result)
    assert "--confirmation" in help_result.stdout
