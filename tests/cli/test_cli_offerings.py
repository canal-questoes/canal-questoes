"""CLI `cq offerings create/list/show`.

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`). As saídas são comparadas com o corpo da API (`--json`) ou com o texto exato
para pessoas; os erros seguem a forma comum do CLI (`Erro:
<message>`, uma linha por campo, `Permissão necessária: …`; uso incorreto com código 2 e sem eco).
Identificadores e cursores nunca são afirmados por valor literal: vêm da própria API.

Com as mensagens livres opcionais por oferecimento (desligadas por padrão), o
`Offering` tem `free_messages` e `contact_email_set`; o `_draft` esperado (um rascunho cadastrado sem os
campos) traz os dois, falsos. As linhas de texto não mostram esses campos. Sem as chaves na resposta, os testes
que comparam o corpo inteiro falham.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.helpers.api import ADMIN_TOKEN, auth, create_offering, create_token
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error, assert_matches

ADMIN = auth(ADMIN_TOKEN)
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    """Saída esperada: uma linha por item, cada uma terminada por quebra de linha."""
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


def _api_get(client: Any, path: str, params: Any = None) -> dict:
    """`GET` direto na API com o token inicial; afirma 200 e o esquema congelado."""
    resp = client.get(path, params=params, headers=ADMIN)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.list" if path == "/v1/offerings" else "offerings.get", 200, body)
    return body


def _draft(offering_id: str, code: str, created_at: str, *, course: str = COURSE, term: str = TERM,
           tz: str = TZ, window: int = 1440) -> dict:
    return {"offering_id": offering_id, "code": code, "course": course, "term": term, "timezone": tz,
            "response_window_minutes": window, "state": "draft", "created_at": created_at,
            "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
            # As mensagens livres desligadas por padrão e nenhum contato do professor.
            "free_messages": False, "contact_email_set": False,
            # O `Offering` tem `purged_at`, nulo sem expurgo.
            "purged_at": None}


def _create_args(code: str, *extra: str, course: str = COURSE, term: str = TERM, tz: str = TZ) -> list[str]:
    return ["offerings", "create", code, "--course", course, "--term", term, "--timezone", tz, *extra]


def _assert_nothing_created(client: Any) -> None:
    assert _api_get(client, "/v1/offerings") == {"items": [], "next_cursor": 0}


# --------------------------------------------------------------------------------------------------
# --json: a saída padrão é o corpo da API


def test_json_offerings_create(cli_invoke, client, clock):
    """Contrato (`--json`: o corpo da API): `cq --json offerings create` imprime o
    `Offering` criado, igual ao da consulta; `--response-window` chega à API como inteiro, e sem ele
    vale a janela padrão (1440)."""
    result = cli_invoke(_create_args("mc404-2031s1", "--response-window", "90"), json=True)
    _ok(result)
    api = _api_get(client, "/v1/offerings/mc404-2031s1")
    assert api == _draft(api["offering_id"], "mc404-2031s1", iso(clock.now()), window=90)
    assert json.loads(result.stdout) == api

    clock.advance(5)
    default = cli_invoke(_create_args("mc404-2031s2"), json=True)
    _ok(default)
    api_default = _api_get(client, "/v1/offerings/mc404-2031s2")
    assert api_default == _draft(api_default["offering_id"], "mc404-2031s2", iso(clock.now()))
    assert json.loads(default.stdout) == api_default


def test_json_offerings_list(cli_invoke, client):
    """`cq --json offerings list` (com e sem `--after`/`--limit`) é o corpo da
    API."""
    created = [create_offering(client, ADMIN_TOKEN) for _ in range(3)]
    full = cli_invoke(["offerings", "list"], json=True)
    _ok(full)
    api_full = _api_get(client, "/v1/offerings")
    assert api_full["items"] == created
    assert json.loads(full.stdout) == api_full

    cursor = _api_get(client, "/v1/offerings", {"limit": 1})["next_cursor"]
    paged = cli_invoke(["offerings", "list", "--after", str(cursor), "--limit", "1"], json=True)
    _ok(paged)
    api_paged = _api_get(client, "/v1/offerings", {"after": cursor, "limit": 1})
    assert api_paged["items"] == [created[1]]
    assert json.loads(paged.stdout) == api_paged


def test_json_offerings_show(cli_invoke, client):
    """`cq --json offerings show REF` pelo código e pelo identificador."""
    offering = create_offering(client, ADMIN_TOKEN)
    for ref in (offering["code"], offering["offering_id"]):
        result = cli_invoke(["offerings", "show", ref], json=True)
        _ok(result)
        assert json.loads(result.stdout) == _api_get(client, f"/v1/offerings/{ref}") == offering


# --------------------------------------------------------------------------------------------------
# Texto para pessoas


def test_text_offerings_create(cli_invoke, client, clock):
    """`cq offerings create` → `Oferecimento cadastrado: <code>
    (<offering_id>), em rascunho.`, e o oferecimento chega à API com os campos dados."""
    result = cli_invoke(_create_args("mc404-2031s1", course="Cálculo Numérico", term="2031 s1",
                                     tz="UTC"))
    _ok(result)
    api = _api_get(client, "/v1/offerings/mc404-2031s1")
    assert api == _draft(api["offering_id"], "mc404-2031s1", iso(clock.now()), course="Cálculo Numérico",
                         term="2031 s1", tz="UTC")
    assert result.stdout == _out(f"Oferecimento cadastrado: mc404-2031s1 ({api['offering_id']}), em rascunho.")


def test_text_offerings_list(cli_invoke, client, clock):
    """`cq offerings list` → `<code>  <state>  <course>  <term>` por item e
    `Próximo cursor: <next_cursor>`; com a lista vazia, só o cursor; com `--after`/`--limit`, a página."""
    empty = cli_invoke(["offerings", "list"])
    _ok(empty)
    assert _api_get(client, "/v1/offerings") == {"items": [], "next_cursor": 0}
    assert empty.stdout == _out("Próximo cursor: 0")

    created = []
    for course, term in (("Algoritmos", "2031s1"), ("Cálculo Numérico", "2031 s2"), ("Física", "verão")):
        created.append(create_offering(client, ADMIN_TOKEN, course=course, term=term))
        clock.advance(60)
    result = cli_invoke(["offerings", "list"])
    _ok(result)
    api = _api_get(client, "/v1/offerings")
    assert api["items"] == created
    assert result.stdout == _out(*(f"{o['code']}  draft  {o['course']}  {o['term']}" for o in created),
                                 f"Próximo cursor: {api['next_cursor']}")

    cursor = _api_get(client, "/v1/offerings", {"limit": 1})["next_cursor"]
    paged = cli_invoke(["offerings", "list", "--after", str(cursor), "--limit", "1"])
    _ok(paged)
    api_paged = _api_get(client, "/v1/offerings", {"after": cursor, "limit": 1})
    o = created[1]
    assert api_paged["items"] == [o]
    assert paged.stdout == _out(f"{o['code']}  draft  Cálculo Numérico  2031 s2",
                                f"Próximo cursor: {api_paged['next_cursor']}")


def test_text_offerings_show(cli_invoke, client):
    """`cq offerings show REF` → `<code>  <state>  <course>  <term>  bot: -` (sem
    bot), pelo código e pelo identificador."""
    offering = create_offering(client, ADMIN_TOKEN, course="Estruturas de Dados", term="2031s2")
    assert offering["bot"] is None
    for ref in (offering["code"], offering["offering_id"]):
        result = cli_invoke(["offerings", "show", ref])
        _ok(result)
        assert result.stdout == _out(f"{offering['code']}  draft  Estruturas de Dados  2031s2  bot: -")


# --------------------------------------------------------------------------------------------------
# Uso incorreto (só `--after`, `--limit`, `--response-window` e `--valid-hours` são
# tipados; código 2, sem eco)

_USAGE_CASES = [
    ("janela-texto", _create_args("mc404-2031s1", "--response-window", "abcdefgh"),
     "Uso incorreto: valor inválido na opção --response-window.", "abcdefgh"),
    ("janela-fracao", _create_args("mc404-2031s1", "--response-window", "90.5"),
     "Uso incorreto: valor inválido na opção --response-window.", "90.5"),
    ("sem-disciplina", ["offerings", "create", "mc404-2031s1", "--term", TERM, "--timezone", TZ],
     "Uso incorreto: falta a opção --course.", None),
    ("sem-fuso", ["offerings", "create", "mc404-2031s1", "--course", COURSE, "--term", TERM],
     "Uso incorreto: falta a opção --timezone.", None),
    ("sem-codigo", ["offerings", "create", "--course", COURSE, "--term", TERM, "--timezone", TZ],
     "Uso incorreto: falta um argumento obrigatório.", None),
    ("limit-texto", ["offerings", "list", "--limit", "abcdefgh"],
     "Uso incorreto: valor inválido na opção --limit.", "abcdefgh"),
    ("after-texto", ["offerings", "list", "--after", "abcdefgh"],
     "Uso incorreto: valor inválido na opção --after.", "abcdefgh"),
    ("show-sem-ref", ["offerings", "show"], "Uso incorreto: falta um argumento obrigatório.", None),
    ("argumento-a-mais", ["offerings", "show", "mc404-2031s1", "argumentosentinela"],
     "Uso incorreto: há argumentos a mais.", "argumentosentinela"),
]


@pytest.mark.parametrize(("args", "message", "sentinel"), [(a, m, s) for _, a, m, s in _USAGE_CASES],
                         ids=[c[0] for c in _USAGE_CASES])
def test_usage_error_is_exit_2_with_exact_message(args, message, sentinel, cli_invoke, client):
    """Uso incorreto: `--response-window` não inteiro (e `--limit`/`--after` não
    inteiros, opção ou argumento faltando, argumento a mais) → código 2, o stderr exato, sem eco do
    valor digitado e sem chamar a API; na mesma função, `cq offerings create --help` sai com 0."""
    for json_mode in (False, True):
        result = cli_invoke(args, json=json_mode)
        _failed(result, 2)
        assert result.stderr == _out(message)
        if sentinel is not None:
            assert sentinel not in result.stderr
    _assert_nothing_created(client)

    help_result = cli_invoke(["offerings", "create", "--help"])
    _ok(help_result)
    assert "--response-window" in help_result.stdout


# --------------------------------------------------------------------------------------------------
# Erros da API (código 1; `Erro: <message>` e os campos; `--json`: o corpo)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_invalid_request(json_mode, cli_invoke, client):
    """400 → código 1, `Erro: Pedido inválido.` e uma linha `  - <field>: <reason>` por
    campo (o código e o fuso não são validados no CLI), sem eco; `--json`: o corpo de erro da API."""
    args = _create_args("AB", course="   ", tz="America/Campinas")
    result = cli_invoke(args, json=json_mode)
    _failed(result, 1)
    fields = [("code", "too_short"), ("course", "too_short"), ("timezone", "invalid_value")]
    api = client.post("/v1/offerings", json={"code": "AB", "course": "   ", "term": TERM,
                                             "timezone": "America/Campinas"}, headers=ADMIN)
    assert_error(api, 400, "invalid_request", fields=fields)
    if json_mode:
        assert json.loads(result.stderr) == api.json()
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['invalid_request']}",
                                     *(f"  - {f}: {r}" for f, r in fields))
    assert "America/Campinas" not in result.stderr
    _assert_nothing_created(client)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_already_exists(json_mode, cli_invoke, client):
    """código repetido → código 1, `Erro: Já existe um oferecimento com
    esse código.` e `  - code: duplicate`, sem o identificador existente."""
    existing = create_offering(client, ADMIN_TOKEN, code="mc404-2031s1")
    result = cli_invoke(_create_args("mc404-2031s1", course="Outra Disciplina"), json=json_mode)
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == {"error": {
            "code": "already_exists", "message": MESSAGES["already_exists"],
            "fields": [{"field": "code", "reason": "duplicate"}]}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['already_exists']}", "  - code: duplicate")
    assert existing["offering_id"] not in result.stderr
    assert _api_get(client, "/v1/offerings")["items"] == [existing]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_show_not_found_and_malformed_ref(json_mode, cli_invoke, client):
    """`offerings show` com código inexistente → código 1 e `Erro: Recurso não
    encontrado.`; com `ref` malformado → `Erro: Pedido inválido.` e `  - ref: invalid_format`, sem
    eco; `offerings list --limit 0` → `  - limit: out_of_range`."""
    missing = cli_invoke(["offerings", "show", "codigo-inexistente"], json=json_mode)
    _failed(missing, 1)
    if json_mode:
        assert json.loads(missing.stderr) == {"error": {"code": "not_found", "message": MESSAGES["not_found"]}}
    else:
        assert missing.stderr == _out(f"Erro: {MESSAGES['not_found']}")

    malformed = cli_invoke(["offerings", "show", "REFSENTINELA"], json=json_mode)
    _failed(malformed, 1)
    if json_mode:
        assert json.loads(malformed.stderr) == {"error": {
            "code": "invalid_request", "message": MESSAGES["invalid_request"],
            "fields": [{"field": "ref", "reason": "invalid_format"}]}}
    else:
        assert malformed.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - ref: invalid_format")
    assert "refsentinela" not in malformed.stderr.casefold()

    limit = cli_invoke(["offerings", "list", "--limit", "0"], json=json_mode)
    _failed(limit, 1)
    if json_mode:
        assert json.loads(limit.stderr) == {"error": {
            "code": "invalid_request", "message": MESSAGES["invalid_request"],
            "fields": [{"field": "limit", "reason": "out_of_range"}]}}
    else:
        assert limit.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - limit: out_of_range")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_forbidden(json_mode, cli_invoke, client):
    """403 → código 1 e a linha `Permissão necessária: <permissão>` (`offerings:write`
    para cadastrar, `offerings:read` para listar e consultar); nada é cadastrado."""
    offering = create_offering(client, ADMIN_TOKEN)
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="so pessoas")
    cases = [
        (_create_args("mc404-2031s9"), "offerings:write"),
        (["offerings", "list"], "offerings:read"),
        (["offerings", "show", offering["code"]], "offerings:read"),
    ]
    for args, permission in cases:
        result = cli_invoke(args, json=json_mode, token=secret)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == {"error": {
                "code": "forbidden", "message": MESSAGES["forbidden"], "permission": permission}}
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['forbidden']}", f"Permissão necessária: {permission}")
    assert _api_get(client, "/v1/offerings")["items"] == [offering]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_unauthenticated(json_mode, cli_invoke, client):
    """sem token → código 1 e `Erro: Token ausente, inválido ou revogado.` nos três comandos."""
    offering = create_offering(client, ADMIN_TOKEN)
    for args in (_create_args("mc404-2031s9"), ["offerings", "list"], ["offerings", "show", offering["code"]]):
        result = cli_invoke(args, json=json_mode, token=None)
        _failed(result, 1)
        if json_mode:
            assert json.loads(result.stderr) == {"error": {"code": "unauthenticated",
                                                           "message": MESSAGES["unauthenticated"]}}
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['unauthenticated']}")
    assert _api_get(client, "/v1/offerings")["items"] == [offering]
