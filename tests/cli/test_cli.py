"""CLI `cq` do pacote cliente.

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`). As saídas são comparadas com o corpo da API ou com o texto exato da tabela de
saída do CLI. Sequências (`seq`, cursores, `token_id`) nunca são afirmadas por valor literal: vêm da
própria API.

Oferecimentos e bots: `_SAMPLE_ARGS` tem os
10 comandos dessa área, para que a API fora do ar (código 3) valha também neles.

Vínculo do aluno: `_SAMPLE_ARGS` tem os
5 comandos (`students import`, `students summary`, `invites send prepare`, `invites send
confirm` e `invites resend`), para que a API fora do ar (código 3) valha também neles. O `students
import` lê o arquivo da lista antes de chamar a API: o arquivo de exemplo, válido pelo formato de
arquivo da lista, com uma linha neutra (fora dos alvos canário), fica no mesmo diretório temporário do
token do bot. No esqueleto, sem os comandos, esses casos terminam com o erro de uso do `click`
(código 2), e não com o 3.

Disparo e resposta: `_SAMPLE_ARGS` tem
os 6 comandos (`dispatches prepare`, `dispatches show`, `dispatches list`, `dispatches confirm`,
`dispatches cancel` e `messages list`), com argumentos de formato válido (o `repo`
num host `.test`, que nunca resolve, um commit SHA-1 e horários com deslocamento), para que a API fora
do ar (código 3) valha também neles. No esqueleto, sem os comandos, esses casos terminam com o erro de
uso do `click` (código 2), e não com o 3; os demais casos da mesma função passam.

Mensagens livres opcionais por oferecimento:
`_SAMPLE_ARGS` tem a folha `offerings settings` (`offerings.update_settings`), com `--free-messages on`
(sem opção, a folha é erro de uso antes de chamar a API), para que a API fora do ar (código 3) valha também
nela. Sem o comando, esse caso termina com o erro de uso do `click` (código 2), e não com o 3.

Relatórios e acompanhamento: `_SAMPLE_ARGS` tem os
5 comandos (`reports show`, `reports versions` e `reports resend`, com `REF` e um `DISPATCH_ID` uuid
v4; `offerings digest show`; `offerings digest set` com `--enabled on`), para que a API fora do ar (código
3) valha também neles. No esqueleto, sem os comandos, esses casos terminam com o erro de uso do `click`
(código 2), e não com o 3; os demais casos da mesma função passam.

Expurgo e diagnóstico, com ressalva:
`_SAMPLE_ARGS` tem os 2 comandos do expurgo (`offerings purge prepare REF` e `offerings purge
confirm REF --confirmation CÓDIGO`, com um código de formato válido), para que a API fora do ar (código 3) valha
também neles. **Ressalva:** `CATALOG_COMMANDS` filtra a folha `doctor` (`system.doctor`):
com a API fora do ar, o `doctor` não termina com o código 3 e a mensagem única, e sim com o relatório das
verificações (`api.reachable` em `failure`) e a ajuda, cobertos em `tests/cli/test_cli_doctor.py`;
só ela fica fora do conjunto. No esqueleto, sem os comandos, os casos do expurgo terminam com o erro de uso
do `click` (código 2), e não com o 3; os demais casos da mesma função passam.

Banco de questões local: `_SAMPLE_ARGS` tem os 2 comandos (`banks upload PASTA --name NOME` e `banks list`,
com o filtro e a paginação), para que a API fora do ar (código 3) valha também neles. O `banks upload` lê,
confere e empacota a pasta antes de chamar a API: a pasta de exemplo é uma cópia do curso de fixture, válida
pelas regras do banco, no mesmo diretório temporário do token do bot. No esqueleto, a leitura da pasta e a
listagem lançam `NotImplementedError`, e esses casos não terminam com o 3.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import cq_client.cli as cq_cli
import cq_server
from cq_server.migrate import EXPECTED_VERSION
from tests.helpers.api import (
    ADMIN_TOKEN,
    all_events,
    auth,
    create_token,
    register_person,
    uuid4_with_prefix,
)
from tests.helpers.api import write_roster
from tests.helpers.banks import copy_course
from tests.helpers.calls import DUMMY_BOT_TOKEN
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error, assert_matches, load_catalog
from tests.helpers.secrets import all_secrets, remember_secret

ADMIN = auth(ADMIN_TOKEN)
UNREACHABLE_MESSAGE = "Não foi possível conectar à API em http://127.0.0.1:9."
DB_PASSWORD_SENTINEL = "senha-que-nao-pode-vazar"  # a senha de `unreachable_db_url` (conftest)
NOT_FOUND_ALIAS = "ffff0000ffff"  # formato válido; nunca é apelido de alguém (o apelido seria `ffff`)


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    """Saída esperada: uma linha por item, cada uma terminada por quebra de linha."""
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    """Nenhuma exceção escapou do grupo `cli` e nenhuma saída tem rastreamento de pilha."""
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _terminal(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Substitui `cli.stdout_is_terminal` (é o ponto que os testes substituem)."""
    monkeypatch.setattr(cq_cli, "stdout_is_terminal", lambda: value)


def _fix_pseudo_ids(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> list[uuid.UUID]:
    """Fixa os `pseudo_id` dos próximos cadastros (`people.generate_pseudo_id`)."""
    from cq_server import people

    ids = [uuid4_with_prefix(p, fill=str(i + 1)) for i, p in enumerate(prefixes)]
    pending = list(ids)
    monkeypatch.setattr(people, "generate_pseudo_id", lambda: pending.pop(0))
    return ids


def _op_of(path: str) -> str:
    """Operação do catálogo de um caminho `GET` usado nos testes."""
    if path == "/health":
        return "system.health"
    if path.startswith("/v1/people/"):
        return "people.get"
    return {"/v1/people": "people.list", "/v1/tokens": "tokens.list", "/v1/events": "events.list"}[path]


def _api_get(client: Any, path: str, params: Any = None) -> dict:
    """`GET` direto na API com o token inicial; afirma 200 e o esquema congelado."""
    resp = client.get(path, params=params, headers=ADMIN)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(_op_of(path), 200, body)
    return body


def _api_post(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    """`POST` direto na API com o token inicial; afirma 200 e o esquema congelado."""
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _only_token(client: Any) -> dict:
    items = _api_get(client, "/v1/tokens")["items"]
    assert len(items) == 1, f"esperava exatamente um token criado, há {len(items)}"
    return items[0]


def _read_secret_file(path: Any) -> str:
    """O arquivo de `--secret-file` tem exatamente `segredo + "\\n"`."""
    raw = path.read_bytes()
    assert re.fullmatch(rb"cq_[A-Za-z0-9_-]{43}\n", raw), "o arquivo deve ter só o segredo e um \\n"
    secret = raw[:-1].decode("ascii")
    remember_secret(secret)
    return secret


def _assert_secret_of(sql: Any, token_id: str, secret: str) -> None:
    """O segredo é o do token criado: o banco guarda o SHA-256 dele."""
    row = sql.execute("SELECT secret_sha256 FROM api_tokens WHERE token_id::text = %s",
                      (token_id,)).fetchone()
    assert row == (hashlib.sha256(secret.encode("utf-8")).hexdigest(),)


def _assert_no_token_created(client: Any) -> None:
    assert _api_get(client, "/v1/tokens") == {"items": []}
    assert all_events(client, ADMIN_TOKEN, "token.created") == []


def _token_args(label: str = "rotulo do cli", *permissions: str) -> list[str]:
    perms = permissions or ("people:read", "events:read")
    args = ["tokens", "create", "--label", label]
    for perm in perms:
        args += ["--permission", perm]
    return args


@contextlib.contextmanager
def _recorded_requests(http: Any):
    """Registra cada pedido que passa pelo cliente HTTP injetado (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = http.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _health_ok() -> dict:
    return {"status": "ok", "version": cq_server.__version__, "database": "ok",
            "schema_version": EXPECTED_VERSION, "expected_schema_version": EXPECTED_VERSION}


# --------------------------------------------------------------------------------------------------
# --json: a saída padrão é o corpo da API


def test_json_health(cli_invoke, client):
    """`cq --json health` imprime o corpo de `system.health`."""
    result = cli_invoke(["health"], json=True)
    _ok(result)
    api = _api_get(client, "/health")
    assert api == _health_ok()
    assert json.loads(result.stdout) == api


def test_json_people_add(cli_invoke, client, clock, monkeypatch):
    """`cq --json people add` imprime o `Person` criado, e a identidade chega à API."""
    ids = _fix_pseudo_ids(monkeypatch, "abc1")
    result = cli_invoke(["people", "add", "--name", "Helena Fictícia Moraes", "--ra", "1122334455",
                         "--email", "helena.ficticia@exemplo.example.com"], json=True)
    _ok(result)
    expected = {"pseudo_id": str(ids[0]), "alias": "abc1", "created_at": iso(clock.now())}
    assert json.loads(result.stdout) == expected
    assert _api_get(client, "/v1/people/abc1") == expected
    revealed = _api_post(client, "identity.reveal", "/v1/identity/reveal",
                         {"pseudonyms": ["abc1"], "reason": "conferir"})
    assert revealed == {"people": [{"pseudo_id": str(ids[0]), "alias": "abc1",
                                           "name": "Helena Fictícia Moraes", "ra": "1122334455",
                                           "email": "helena.ficticia@exemplo.example.com"}],
                               "not_found": []}


def test_json_people_list(cli_invoke, client):
    """`cq --json people list` (com e sem `--after`/`--limit`) é o corpo da API."""
    for name in ("Ana Inventada Prado", "Bento Fictício Lage", "Clara Imaginária Viana"):
        register_person(client, ADMIN_TOKEN, name=name)
    full = cli_invoke(["people", "list"], json=True)
    _ok(full)
    api_full = _api_get(client, "/v1/people")
    assert len(api_full["items"]) == 3
    assert json.loads(full.stdout) == api_full

    cursor = _api_get(client, "/v1/people", {"limit": 1})["next_cursor"]
    paged = cli_invoke(["people", "list", "--after", str(cursor), "--limit", "1"], json=True)
    _ok(paged)
    api_paged = _api_get(client, "/v1/people", {"after": cursor, "limit": 1})
    assert api_paged["items"] == [api_full["items"][1]]
    assert json.loads(paged.stdout) == api_paged


def test_json_people_show(cli_invoke, client):
    """`cq --json people show REF` pelo apelido e pelo identificador."""
    person = register_person(client, ADMIN_TOKEN, name="Davi Inventado Nunes")
    for ref in (person["alias"], person["pseudo_id"]):
        result = cli_invoke(["people", "show", ref], json=True)
        _ok(result)
        assert json.loads(result.stdout) == _api_get(client, f"/v1/people/{ref}") == person


def test_json_identity_reveal(cli_invoke, client):
    """`cq --json identity reveal` imprime o corpo exato da revelação."""
    a = register_person(client, ADMIN_TOKEN, name="Helena Fictícia Moraes", ra="1122334455",
                        email="Helena.Ficticia@exemplo.example.com")
    b = register_person(client, ADMIN_TOKEN, name="Bruno Inventado Siqueira")
    refs = [a["alias"], b["pseudo_id"], NOT_FOUND_ALIAS, a["pseudo_id"]]
    result = cli_invoke(["identity", "reveal", *refs, "--reason", "conferencia pelo cli"], json=True)
    _ok(result)
    expected = {
        "people": [
            {"pseudo_id": a["pseudo_id"], "alias": a["alias"], "name": "Helena Fictícia Moraes",
             "ra": "1122334455", "email": "Helena.Ficticia@exemplo.example.com"},
            {"pseudo_id": b["pseudo_id"], "alias": b["alias"], "name": "Bruno Inventado Siqueira",
             "ra": None, "email": None},
        ],
        "not_found": [NOT_FOUND_ALIAS],
    }
    assert json.loads(result.stdout) == expected
    api = _api_post(client, "identity.reveal", "/v1/identity/reveal",
                    {"pseudonyms": refs, "reason": "conferencia pelo cli"})
    assert api == expected


def test_json_tokens_create(cli_invoke, client, clock, sql, monkeypatch, tmp_path):
    """`cq --json tokens create` sai sem `secret` e com `secret_file`."""
    _terminal(monkeypatch, False)
    path = tmp_path / "segredo.txt"
    result = cli_invoke([*_token_args(), "--secret-file", str(path)], json=True)
    _ok(result)
    secret = _read_secret_file(path)
    item = _only_token(client)
    assert item == {"token_id": item["token_id"], "label": "rotulo do cli",
                    "permissions": ["events:read", "people:read"], "created_at": iso(clock.now()),
                    "revoked_at": None}
    assert json.loads(result.stdout) == {**item, "secret_file": str(path)}
    assert find_secrets(result.stdout + result.stderr, [secret]) == []
    _assert_secret_of(sql, item["token_id"], secret)
    assert client.get("/v1/people", headers=auth(secret)).status_code == 200


def test_json_tokens_list(cli_invoke, client, clock):
    """`cq --json tokens list` é o corpo da API, na ordem de criação."""
    _, first = create_token(client, ADMIN_TOKEN, ["people:read"], label="primeiro token")
    clock.advance(10)
    _, second = create_token(client, ADMIN_TOKEN, ["events:read", "people:write"], label="segundo token")
    _api_post(client, "tokens.revoke", f"/v1/tokens/{first['token_id']}/revoke")
    result = cli_invoke(["tokens", "list"], json=True)
    _ok(result)
    api = _api_get(client, "/v1/tokens")
    assert [i["token_id"] for i in api["items"]] == [first["token_id"], second["token_id"]]
    assert json.loads(result.stdout) == api


def test_json_tokens_revoke(cli_invoke, client, clock):
    """`cq --json tokens revoke` é o `Token` revogado da API."""
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token a revogar")
    clock.advance(30)
    result = cli_invoke(["tokens", "revoke", token["token_id"]], json=True)
    _ok(result)
    expected = {"token_id": token["token_id"], "label": "token a revogar", "permissions": ["people:read"],
                "created_at": token["created_at"], "revoked_at": iso(clock.now())}
    assert json.loads(result.stdout) == expected
    assert _api_post(client, "tokens.revoke", f"/v1/tokens/{token['token_id']}/revoke") == expected


def test_json_events_list(cli_invoke, client):
    """`cq --json events list` (vários tipos, `--after`, `--limit`) é o corpo da API."""
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    register_person(client, ADMIN_TOKEN, name="Bento Fictício Lage")
    create_token(client, ADMIN_TOKEN, ["people:read"], label="token dos eventos")
    types = [("types", "person.registered"), ("types", "token.created")]
    base_args = ["events", "list", "--type", "person.registered", "--type", "token.created"]

    full = cli_invoke(base_args, json=True)
    _ok(full)
    api_full = _api_get(client, "/v1/events", types)
    assert [e["type"] for e in api_full["items"]] == ["person.registered", "person.registered",
                                                        "token.created"]
    assert json.loads(full.stdout) == api_full

    cursor = api_full["items"][0]["seq"]
    paged = cli_invoke([*base_args, "--after", str(cursor), "--limit", "1"], json=True)
    _ok(paged)
    api_paged = _api_get(client, "/v1/events", [*types, ("after", cursor), ("limit", 1)])
    assert api_paged["items"] == [api_full["items"][1]]
    assert json.loads(paged.stdout) == api_paged


# --------------------------------------------------------------------------------------------------
# Texto para pessoas


def test_text_health(cli_invoke):
    """`cq health` → `API no ar: versão <version>, esquema <schema_version>.`."""
    result = cli_invoke(["health"])
    _ok(result)
    assert result.stdout == _out(f"API no ar: versão {cq_server.__version__}, esquema {EXPECTED_VERSION}.")


def test_text_people_add(cli_invoke, client, monkeypatch):
    """`cq people add` → `Pessoa cadastrada: <alias> (<pseudo_id>).`; RA e e-mail opcionais."""
    ids = _fix_pseudo_ids(monkeypatch, "abc1", "bcd2")
    full = cli_invoke(["people", "add", "--name", "Helena Fictícia Moraes", "--ra", "1122334455",
                       "--email", "helena@exemplo.example.com"])
    _ok(full)
    assert full.stdout == _out(f"Pessoa cadastrada: abc1 ({ids[0]}).")
    bare = cli_invoke(["people", "add", "--name", "Bruno Inventado Siqueira"])
    _ok(bare)
    assert bare.stdout == _out(f"Pessoa cadastrada: bcd2 ({ids[1]}).")
    revealed = _api_post(client, "identity.reveal", "/v1/identity/reveal",
                         {"pseudonyms": ["abc1", "bcd2"], "reason": "conferir"})
    assert revealed == {"people": [
        {"pseudo_id": str(ids[0]), "alias": "abc1", "name": "Helena Fictícia Moraes", "ra": "1122334455",
         "email": "helena@exemplo.example.com"},
        {"pseudo_id": str(ids[1]), "alias": "bcd2", "name": "Bruno Inventado Siqueira", "ra": None,
         "email": None},
    ], "not_found": []}


def test_text_people_list(cli_invoke, client, clock):
    """`cq people list` → `<alias>  <pseudo_id>  <created_at>` e `Próximo cursor: <n>`."""
    empty = cli_invoke(["people", "list"])
    _ok(empty)
    assert _api_get(client, "/v1/people") == {"items": [], "next_cursor": 0}
    assert empty.stdout == _out("Próximo cursor: 0")

    start = clock.now()
    registered = []
    for name in ("Ana Inventada Prado", "Bento Fictício Lage", "Clara Imaginária Viana"):
        registered.append(register_person(client, ADMIN_TOKEN, name=name))
        clock.advance(60)
    result = cli_invoke(["people", "list"])
    _ok(result)
    api = _api_get(client, "/v1/people")
    assert api["items"] == registered
    assert [p["created_at"] for p in registered] == [iso(start + timedelta(seconds=60 * i)) for i in range(3)]
    assert result.stdout == _out(*(f"{p['alias']}  {p['pseudo_id']}  {p['created_at']}" for p in registered),
                                 f"Próximo cursor: {api['next_cursor']}")

    cursor = _api_get(client, "/v1/people", {"limit": 1})["next_cursor"]
    paged = cli_invoke(["people", "list", "--after", str(cursor), "--limit", "1"])
    _ok(paged)
    api_paged = _api_get(client, "/v1/people", {"after": cursor, "limit": 1})
    p = registered[1]
    assert api_paged["items"] == [p]
    assert paged.stdout == _out(f"{p['alias']}  {p['pseudo_id']}  {p['created_at']}",
                                f"Próximo cursor: {api_paged['next_cursor']}")

    past_end = cli_invoke(["people", "list", "--after", str(api["next_cursor"])])
    _ok(past_end)
    assert past_end.stdout == _out(f"Próximo cursor: {api['next_cursor']}")


def test_text_people_show(cli_invoke, client):
    """`cq people show REF` → `<alias>  <pseudo_id>  <created_at>`."""
    person = register_person(client, ADMIN_TOKEN, name="Davi Inventado Nunes")
    for ref in (person["alias"], person["pseudo_id"]):
        result = cli_invoke(["people", "show", ref])
        _ok(result)
        assert result.stdout == _out(f"{person['alias']}  {person['pseudo_id']}  {person['created_at']}")


def test_text_identity_reveal(cli_invoke, client):
    """`cq identity reveal` → uma linha por pessoa (`-` no ausente) e `Não encontrado: <item>`."""
    a = register_person(client, ADMIN_TOKEN, name="Helena Fictícia Moraes", ra="1122334455",
                        email="Helena.Ficticia@exemplo.example.com")
    b = register_person(client, ADMIN_TOKEN, name="Bruno Inventado Siqueira")
    c = register_person(client, ADMIN_TOKEN, name="Clara Imaginária Viana", email="clara@exemplo.example.com")
    d = register_person(client, ADMIN_TOKEN, name="Diego Fictício Barros", ra="5566778899")
    missing_uuid = str(uuid4_with_prefix("eeee", fill="9"))
    refs = [a["alias"], c["pseudo_id"], NOT_FOUND_ALIAS, b["alias"], a["pseudo_id"], missing_uuid, d["alias"]]
    result = cli_invoke(["identity", "reveal", *refs, "--reason", "conferencia pelo cli"])
    _ok(result)
    assert result.stdout == _out(
        f"{a['alias']}  Helena Fictícia Moraes  RA: 1122334455  e-mail: Helena.Ficticia@exemplo.example.com",
        f"{c['alias']}  Clara Imaginária Viana  RA: -  e-mail: clara@exemplo.example.com",
        f"{b['alias']}  Bruno Inventado Siqueira  RA: -  e-mail: -",
        f"{d['alias']}  Diego Fictício Barros  RA: 5566778899  e-mail: -",
        f"Não encontrado: {NOT_FOUND_ALIAS}",
        f"Não encontrado: {missing_uuid}",
    )


@pytest.mark.parametrize("mode", ["terminal", "arquivo"])
def test_text_tokens_create(mode, cli_invoke, client, sql, monkeypatch, tmp_path):
    """`cq tokens create` → `Token criado: …` e o segredo (terminal) ou o caminho."""
    path = tmp_path / "segredo.txt"
    args = _token_args()
    if mode == "terminal":
        _terminal(monkeypatch, True)
    else:
        _terminal(monkeypatch, False)
        args += ["--secret-file", str(path)]
    result = cli_invoke(args)
    _ok(result)
    item = _only_token(client)
    first = f"Token criado: {item['token_id']} (events:read, people:read)."
    if mode == "terminal":
        lines = result.stdout.splitlines()
        assert len(lines) == 2, result.stdout
        match = re.fullmatch(r"Segredo \(mostrado só agora\): (cq_[A-Za-z0-9_-]{43})", lines[1])
        assert match, f"linha do segredo fora do formato: {lines[1]!r}"
        secret = match.group(1)
        remember_secret(secret)
        assert result.stdout == _out(first, f"Segredo (mostrado só agora): {secret}")
        assert not path.exists()
    else:
        secret = _read_secret_file(path)
        assert result.stdout == _out(first, f"Segredo gravado em {path}.")
    _assert_secret_of(sql, item["token_id"], secret)


def test_text_tokens_list(cli_invoke, client, clock):
    """`cq tokens list` → `<token_id>  <label>  <perms com ",">  <created_at>  <revoked_at|ativo>`."""
    empty = cli_invoke(["tokens", "list"])
    _ok(empty)
    assert empty.stdout == ""

    start = clock.now()
    _, first = create_token(client, ADMIN_TOKEN, ["people:read", "events:read"], label="primeiro token")
    clock.advance(10)
    _, second = create_token(client, ADMIN_TOKEN, ["people:write"], label="segundo token")
    clock.advance(10)
    _api_post(client, "tokens.revoke", f"/v1/tokens/{first['token_id']}/revoke")
    result = cli_invoke(["tokens", "list"])
    _ok(result)
    assert result.stdout == _out(
        f"{first['token_id']}  primeiro token  events:read,people:read  {iso(start)}  "
        f"{iso(start + timedelta(seconds=20))}",
        f"{second['token_id']}  segundo token  people:write  {iso(start + timedelta(seconds=10))}  ativo",
    )


def test_text_tokens_revoke(cli_invoke, client, clock):
    """`cq tokens revoke` → `Token revogado: <token_id> em <revoked_at>.`; idempotente."""
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token a revogar")
    clock.advance(30)
    revoked_at = iso(clock.now())
    result = cli_invoke(["tokens", "revoke", token["token_id"]])
    _ok(result)
    assert result.stdout == _out(f"Token revogado: {token['token_id']} em {revoked_at}.")
    clock.advance(30)
    again = cli_invoke(["tokens", "revoke", token["token_id"]])
    _ok(again)
    assert again.stdout == _out(f"Token revogado: {token['token_id']} em {revoked_at}.")


def test_text_events_list(cli_invoke, client, clock):
    """`cq events list` → `<seq>  <occurred_at>  <type>` e `Próximo cursor: <n>`."""
    empty = cli_invoke(["events", "list", "--type", "token.revoked"])
    _ok(empty)
    assert _api_get(client, "/v1/events", {"types": "token.revoked"}) == {"items": [], "next_cursor": 0}
    assert empty.stdout == _out("Próximo cursor: 0")

    start = clock.now()
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    clock.advance(5)
    create_token(client, ADMIN_TOKEN, ["people:read"], label="token dos eventos")
    types = [("types", "person.registered"), ("types", "token.created")]
    base_args = ["events", "list", "--type", "person.registered", "--type", "token.created"]
    result = cli_invoke(base_args)
    _ok(result)
    api = _api_get(client, "/v1/events", types)
    assert [(e["type"], e["occurred_at"]) for e in api["items"]] == [
        ("person.registered", iso(start)), ("token.created", iso(start + timedelta(seconds=5)))]
    assert result.stdout == _out(*(f"{e['seq']}  {e['occurred_at']}  {e['type']}" for e in api["items"]),
                                 f"Próximo cursor: {api['next_cursor']}")

    cursor = api["items"][0]["seq"]
    paged = cli_invoke([*base_args, "--after", str(cursor), "--limit", "1"])
    _ok(paged)
    api_paged = _api_get(client, "/v1/events", [*types, ("after", cursor), ("limit", 1)])
    event = api["items"][1]
    assert api_paged["items"] == [event]
    assert paged.stdout == _out(f"{event['seq']}  {event['occurred_at']}  {event['type']}",
                                f"Próximo cursor: {api_paged['next_cursor']}")


# --------------------------------------------------------------------------------------------------
# Perfil


def test_profile_token_from_env(cli_invoke, client, clock):
    """o token vem de `CQ_TOKEN`; `CQ_TOKEN` vazio vale como ausente."""
    person = register_person(client, ADMIN_TOKEN, name="Carla Imaginária Rocha")
    secret, token = create_token(client, ADMIN_TOKEN, ["identity:reveal", "people:read"], label="token do perfil")
    result = cli_invoke(["identity", "reveal", person["alias"], "--reason", "conferir perfil"], json=True,
                        token=secret)
    _ok(result)
    events = all_events(client, ADMIN_TOKEN, "identity.revealed")
    assert events == [{"seq": events[0]["seq"], "type": "identity.revealed", "occurred_at": iso(clock.now()),
                       "actor_token_id": token["token_id"],
                       "data": {"client": "cli", "revealed": [person["pseudo_id"]], "not_found_count": 0,
                                "reason": "conferir perfil"}}]

    denied = cli_invoke(["people", "add", "--name", "Nome Qualquer Teste"], token=secret)
    _no_traceback(denied)
    assert denied.exit_code == 1
    assert denied.stdout == ""
    assert denied.stderr == _out(f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: people:write")

    empty_token = cli_invoke(["people", "list"], token="")
    _no_traceback(empty_token)
    assert empty_token.exit_code == 1
    assert empty_token.stdout == ""
    assert empty_token.stderr == _out(f"Erro: {MESSAGES['unauthenticated']}")


def test_profile_token_from_file(cli_invoke, client, tmp_path):
    """endereço e token do arquivo de perfil TOML; o ambiente tem precedência."""
    person = register_person(client, ADMIN_TOKEN, name="Carla Imaginária Rocha")
    secret, token = create_token(client, ADMIN_TOKEN, ["identity:reveal"], label="token do arquivo")
    config = tmp_path / "perfil.toml"
    config.write_text(f'api_url = "http://testserver"\ntoken = "{secret}"\n', encoding="utf-8")
    env = {"CQ_CONFIG": str(config), "CQ_API_URL": None}
    args = ["identity", "reveal", person["alias"], "--reason", "conferir perfil"]

    from_file = cli_invoke(args, json=True, token=None, env=env)
    _ok(from_file)
    assert json.loads(from_file.stdout) == {"people": [
        {"pseudo_id": person["pseudo_id"], "alias": person["alias"], "name": "Carla Imaginária Rocha",
         "ra": None, "email": None}], "not_found": []}
    env_wins = cli_invoke(args, token=ADMIN_TOKEN, env=env)
    _ok(env_wins)
    empty_env = cli_invoke(args, token="", env=env)
    _ok(empty_env)
    actors = [e["actor_token_id"] for e in all_events(client, ADMIN_TOKEN, "identity.revealed")]
    assert actors == [token["token_id"], "bootstrap", token["token_id"]]


@pytest.mark.parametrize("content", [
    'token = "conteudosentinela-sem-fim\n',
    '[[[ conteudosentinela\n',
    '# conteudosentinela\ntoken = 123\n',
    '# conteudosentinela\napi_url = 42\n',
], ids=["string-aberta", "tabela-quebrada", "token-inteiro", "api_url-inteiro"])
def test_profile_invalid_toml(content, cli_invoke, tmp_path):
    """TOML malformado ou com tipo errado → 2, mensagem exata, sem o conteúdo."""
    config = tmp_path / "perfil.toml"
    config.write_text(content, encoding="utf-8")
    for json_mode in (False, True):
        result = cli_invoke(["people", "list"], json=json_mode, token=None,
                            env={"CQ_CONFIG": str(config), "CQ_API_URL": None})
        _no_traceback(result)
        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == _out("Uso incorreto: o arquivo de perfil não é um TOML válido.")
        assert "conteudosentinela" not in result.stderr


@pytest.mark.parametrize("source,value", [
    ("env", "ftp://x"),
    ("env", "http://"),
    ("env", "ftp://enderecosentinela.example.com"),
    ("env", "enderecosentinela.example.com"),
    ("arquivo", "ftp://x"),
    ("arquivo", "ftp://enderecosentinela.example.com"),
], ids=["ftp", "sem-host", "ftp-sentinela", "sem-esquema", "arquivo-ftp", "arquivo-ftp-sentinela"])
def test_profile_invalid_api_url(source, value, cli_invoke, tmp_path):
    """endereço que não é http(s) com host → 2, `o endereço da API é inválido.`."""
    if source == "env":
        env = {"CQ_API_URL": value}
    else:
        config = tmp_path / "perfil.toml"
        config.write_text(f'api_url = "{value}"\n', encoding="utf-8")
        env = {"CQ_CONFIG": str(config), "CQ_API_URL": None}
    result = cli_invoke(["health"], env=env)
    _no_traceback(result)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == _out("Uso incorreto: o endereço da API é inválido.")
    assert "enderecosentinela" not in result.stderr


@pytest.mark.parametrize("source,expected_base", [
    ("padrao", "http://127.0.0.1:8000"),
    ("ambiente", "http://testserver"),
    ("barra-final", "http://testserver"),
    ("arquivo", "http://api-do-arquivo.example:8123"),
    ("ambiente-sobre-arquivo", "http://testserver"),
    ("ambiente-vazio", "http://api-do-arquivo.example:8123"),
])
def test_profile_api_url_builds_request_urls(source, expected_base, cli_invoke, client, tmp_path):
    """o endereço do perfil (padrão, ambiente ou arquivo) monta as URLs pedidas."""
    config = tmp_path / "perfil.toml"
    config.write_text('api_url = "http://api-do-arquivo.example:8123"\n', encoding="utf-8")
    env: dict[str, str | None] = {
        "padrao": {"CQ_API_URL": None},
        "ambiente": {"CQ_API_URL": "http://testserver"},
        "barra-final": {"CQ_API_URL": "http://testserver/"},
        "arquivo": {"CQ_API_URL": None, "CQ_CONFIG": str(config)},
        "ambiente-sobre-arquivo": {"CQ_API_URL": "http://testserver", "CQ_CONFIG": str(config)},
        "ambiente-vazio": {"CQ_API_URL": "", "CQ_CONFIG": str(config)},
    }[source]
    with _recorded_requests(client) as seen:
        health = cli_invoke(["health"], json=True, env=env)
        people = cli_invoke(["people", "list"], json=True, env=env)
    _ok(health)
    _ok(people)
    assert [str(r.url).split("?")[0] for r in seen] == [f"{expected_base}/health", f"{expected_base}/v1/people"]
    assert json.loads(health.stdout) == _health_ok()
    assert json.loads(people.stdout) == _api_get(client, "/v1/people")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_profile_url_credentials_are_hidden(json_mode, cli_invoke):
    """`<url>` sai sem credenciais; API inalcançável → 3."""
    result = cli_invoke(["health"], json=json_mode, http=None,
                        env={"CQ_API_URL": "http://usuariosentinela:senhasentinela@127.0.0.1:9"})
    _no_traceback(result)
    assert result.exit_code == 3
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "api_unreachable", "message": UNREACHABLE_MESSAGE}}
    else:
        assert result.stderr == _out(f"Erro: {UNREACHABLE_MESSAGE}")
    assert "usuariosentinela" not in result.stderr and "senhasentinela" not in result.stderr


# --------------------------------------------------------------------------------------------------
# Cliente declarado


def test_cli_declares_client_cli(cli_invoke, client, clock):
    """toda requisição do CLI leva `X-CQ-Client: cli`; a revelação é auditada com `client: cli`."""
    person = register_person(client, ADMIN_TOKEN, name="Carla Imaginária Rocha")
    commands = [
        ["health"],
        ["people", "list"],
        ["people", "show", person["alias"]],
        ["identity", "reveal", person["alias"], "--reason", "conferencia pelo cli"],
        ["events", "list", "--type", "identity.revealed"],
        ["tokens", "list"],
    ]
    with _recorded_requests(client) as seen:
        for args in commands:
            _ok(cli_invoke(args))
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/health"), ("GET", "/v1/people"), ("GET", f"/v1/people/{person['alias']}"),
        ("POST", "/v1/identity/reveal"), ("GET", "/v1/events"), ("GET", "/v1/tokens")]
    assert [r.headers.get("x-cq-client") for r in seen] == ["cli"] * len(commands)
    events = all_events(client, ADMIN_TOKEN, "identity.revealed")
    assert events == [{"seq": events[0]["seq"], "type": "identity.revealed", "occurred_at": iso(clock.now()),
                       "actor_token_id": "bootstrap",
                       "data": {"client": "cli", "revealed": [person["pseudo_id"]], "not_found_count": 0,
                                "reason": "conferencia pelo cli"}}]


# --------------------------------------------------------------------------------------------------
# Token por argumento


def test_token_is_not_accepted_as_argument(cli_invoke):
    """nenhuma opção recebe o token; `--token` é uso incorreto, sem eco."""
    help_result = cli_invoke(["health", "--help"])
    _ok(help_result)
    assert "--token" not in help_result.stdout
    root_help = cli_invoke(["--help"])
    _ok(root_help)
    assert "--token" not in root_help.stdout

    sentinel = "tokensentinela-por-argumento"
    for args in (["--token", "x", "health"], ["health", "--token", "x"],
                 ["--token", sentinel, "health"], ["health", "--token", sentinel],
                 [f"--token={sentinel}", "health"], ["health", f"--token={ADMIN_TOKEN}"]):
        result = cli_invoke(args, token=None)
        _no_traceback(result)
        assert result.exit_code == 2, args
        assert result.stdout == ""
        assert result.stderr.startswith("Uso incorreto:"), result.stderr
        assert sentinel not in result.stderr and ADMIN_TOKEN not in result.stderr
        assert "--token" not in result.stderr


# --------------------------------------------------------------------------------------------------
# Segredo de `tokens create`


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_secret_shown_on_terminal_without_file(json_mode, cli_invoke, client, sql, monkeypatch):
    """terminal sem `--secret-file` → o segredo aparece (texto e JSON)."""
    _terminal(monkeypatch, True)
    result = cli_invoke(_token_args("rotulo no terminal", "people:read"), json=json_mode)
    _ok(result)
    item = _only_token(client)
    if json_mode:
        out = json.loads(result.stdout)
        assert_matches("tokens.create", 201, out)
        secret = out["secret"]
        remember_secret(secret)
        assert out == {**item, "secret": secret}
    else:
        match = re.fullmatch(r"Token criado: (\S+) \(people:read\)\.\n"
                             r"Segredo \(mostrado só agora\): (cq_[A-Za-z0-9_-]{43})\n", result.stdout)
        assert match, result.stdout
        assert match.group(1) == item["token_id"]
        secret = match.group(2)
        remember_secret(secret)
    _assert_secret_of(sql, item["token_id"], secret)
    assert client.get("/v1/people", headers=auth(secret)).status_code == 200


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_secret_goes_to_file(terminal, json_mode, cli_invoke, client, sql, monkeypatch, tmp_path):
    """com `--secret-file` o arquivo (criado antes da chamada) tem `segredo + "\\n"`
    e a saída não tem o segredo."""
    _terminal(monkeypatch, terminal)
    path = tmp_path / "segredo.txt"
    existed_at_request: list[bool] = []

    def check_file(request: Any) -> None:
        if request.url.path == "/v1/tokens":
            existed_at_request.append(path.exists())

    hooks = client.event_hooks["request"]
    hooks.append(check_file)
    try:
        result = cli_invoke([*_token_args("rotulo com arquivo", "people:read"), "--secret-file", str(path)],
                            json=json_mode)
    finally:
        hooks.remove(check_file)
    _ok(result)
    assert existed_at_request == [True], "o arquivo de --secret-file deve existir antes da chamada à API"
    secret = _read_secret_file(path)
    item = _only_token(client)
    _assert_secret_of(sql, item["token_id"], secret)
    if json_mode:
        assert json.loads(result.stdout) == {**item, "secret_file": str(path)}
    else:
        assert result.stdout == _out(f"Token criado: {item['token_id']} (people:read).",
                                     f"Segredo gravado em {path}.")
    assert find_secrets(result.stdout + result.stderr, [secret]) == []
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_secret_file_required_outside_terminal(json_mode, cli_invoke, client, monkeypatch):
    """fora de terminal sem `--secret-file` → 2, mensagem exata, nenhum token."""
    _terminal(monkeypatch, False)
    result = cli_invoke(_token_args("rotulo sem arquivo", "people:read"), json=json_mode)
    _no_traceback(result)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == _out("Uso incorreto: fora de um terminal, informe --secret-file.")
    _assert_no_token_created(client)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_real_terminal_check_outside_terminal(json_mode, tmp_path):
    """Nenhum segredo no modelo: o `cli.stdout_is_terminal()` de verdade, sem substituição, num processo
    com a saída padrão em pipe (como quando um agente roda o comando) → 2 e a mensagem exata.

    A API aponta para `http://127.0.0.1:9` (inalcançável): o código 2, e não 3, mostra que a checagem
    vem antes de qualquer conexão, logo nenhum token é criado."""
    args = (["--json"] if json_mode else []) + _token_args("rotulo fora do terminal", "people:read")
    env = {**os.environ, "CQ_API_URL": "http://127.0.0.1:9", "CQ_TOKEN": ADMIN_TOKEN,
           "CQ_CONFIG": str(tmp_path / "config-inexistente.toml"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", "from cq_client.cli import main; main()", *args],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert "Traceback" not in proc.stdout and "Traceback" not in proc.stderr, proc.stderr
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        2, "", _out("Uso incorreto: fora de um terminal, informe --secret-file."))
    assert find_secrets(proc.stdout + proc.stderr, all_secrets()) == []


@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_secret_file_already_exists(terminal, cli_invoke, client, monkeypatch, tmp_path):
    """`--secret-file` já existente → 2, mensagem exata, arquivo intacto, nenhum token."""
    _terminal(monkeypatch, terminal)
    path = tmp_path / "segredo.txt"
    path.write_bytes(b"conteudo anterior\n")
    for json_mode in (False, True):
        result = cli_invoke([*_token_args("rotulo repetido", "people:read"), "--secret-file", str(path)],
                            json=json_mode)
        _no_traceback(result)
        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == _out("Uso incorreto: o arquivo de --secret-file já existe.")
    assert path.read_bytes() == b"conteudo anterior\n"
    _assert_no_token_created(client)


@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_secret_file_directory_missing(terminal, cli_invoke, client, monkeypatch, tmp_path):
    """Diretório de `--secret-file` inexistente → 2, mensagem exata, nenhum token."""
    _terminal(monkeypatch, terminal)
    path = tmp_path / "pasta-inexistente" / "segredo.txt"
    for json_mode in (False, True):
        result = cli_invoke([*_token_args("rotulo sem pasta", "people:read"), "--secret-file", str(path)],
                            json=json_mode)
        _no_traceback(result)
        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == _out("Uso incorreto: não foi possível criar o arquivo de --secret-file.")
    assert not path.parent.exists()
    _assert_no_token_created(client)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_secret_file_write_failure_revokes_token(terminal, json_mode, cli_invoke, client, clock, sql,
                                                 monkeypatch, tmp_path):
    """O arquivo não pôde ser escrito depois de o token existir → o CLI revoga o token,
    sai com 1 e `Erro: o segredo não pôde ser gravado; o token foi revogado.`, sem o segredo na saída.

    Com `--json`: saída padrão vazia e o envelope `secret_not_saved` exato no stderr."""
    _terminal(monkeypatch, terminal)
    received: list[str] = []

    def failing_write(file: Any, secret: str) -> None:
        received.append(secret)
        remember_secret(secret)
        raise OSError("disco cheio (simulado)")

    monkeypatch.setattr(cq_cli, "write_secret_file", failing_write)
    path = tmp_path / "segredo.txt"
    result = cli_invoke([*_token_args("rotulo sem gravar", "people:read"), "--secret-file", str(path)],
                        json=json_mode)
    _no_traceback(result)
    assert result.exit_code == 1, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {
            "code": "secret_not_saved",
            "message": "O segredo não pôde ser gravado; o token foi revogado."}}
    else:
        assert result.stderr == _out("Erro: o segredo não pôde ser gravado; o token foi revogado.")
    assert len(received) == 1, "write_secret_file deve ser chamada uma vez, com o segredo criado"
    secret = received[0]

    item = _only_token(client)
    now = iso(clock.now())
    assert item == {"token_id": item["token_id"], "label": "rotulo sem gravar",
                    "permissions": ["people:read"], "created_at": now, "revoked_at": now}
    _assert_secret_of(sql, item["token_id"], secret)
    revoked = all_events(client, ADMIN_TOKEN, "token.revoked")
    assert revoked == [{"seq": revoked[0]["seq"] if revoked else None, "type": "token.revoked",
                        "occurred_at": now, "actor_token_id": "bootstrap",
                        "data": {"token_id": item["token_id"]}}]
    assert_error(client.get("/v1/people", headers=auth(secret)), 401, "unauthenticated")
    assert find_secrets(result.stdout + "\n" + result.stderr, all_secrets()) == []


def test_secret_never_appears_in_other_commands(cli_invoke, client, monkeypatch, tmp_path):
    """fora de `tokens create` no terminal, nenhuma saída tem segredo (nem o resumo)."""
    _terminal(monkeypatch, False)
    path = tmp_path / "segredo.txt"
    created = cli_invoke([*_token_args("rotulo do segredo", "people:read"), "--secret-file", str(path)])
    _ok(created)
    secret = _read_secret_file(path)
    token_id = _only_token(client)["token_id"]
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")

    results = [created]
    for json_mode in (False, True):
        for args in (["health"], ["people", "list"], ["tokens", "list"],
                     ["events", "list", "--type", "token.created", "--type", "token.revoked"]):
            result = cli_invoke(args, json=json_mode)
            _ok(result)
            results.append(result)
        with_secret = cli_invoke(["people", "list"], json=json_mode, token=secret)
        _ok(with_secret)
        forbidden = cli_invoke(["tokens", "list"], json=json_mode, token=secret)
        _no_traceback(forbidden)
        assert forbidden.exit_code == 1
        results += [with_secret, forbidden]
    for json_mode in (False, True):
        revoked = cli_invoke(["tokens", "revoke", token_id], json=json_mode)
        _ok(revoked)
        results.append(revoked)
        after_revoke = cli_invoke(["people", "list"], json=json_mode, token=secret)
        _no_traceback(after_revoke)
        assert after_revoke.exit_code == 1
        results.append(after_revoke)
    for result in results:
        assert find_secrets(result.stdout + "\n" + result.stderr, all_secrets()) == []


# --------------------------------------------------------------------------------------------------
# API fora do ar

# Token do bot: `bots register` lê o token de `--token-file` antes de chamar
# a API (o `CliRunner` não é um terminal, e nenhum argumento recebe o token). O arquivo, com um token
# de formato válido que não existe em lugar nenhum (`DUMMY_BOT_TOKEN`), fica num diretório temporário
# próprio, criado na importação e apagado no fim do processo. `team link` roda num terminal (o teste
# troca `stdout_is_terminal`), onde o `--secret-file` é opcional.
_ANY_OFFERING_REF = "oferta-qualquer"
_BOT_TOKEN_DIR = tempfile.mkdtemp(prefix="cq-test-cli-")
atexit.register(shutil.rmtree, _BOT_TOKEN_DIR, True)
_BOT_TOKEN_FILE = os.path.join(_BOT_TOKEN_DIR, "token-do-bot.txt")
with open(_BOT_TOKEN_FILE, "w", encoding="utf-8", newline="\n") as _token_out:
    _token_out.write(DUMMY_BOT_TOKEN + "\n")
# Arquivo da lista: o arquivo que `students import` lê de `--file` antes de
# chamar a API; cabeçalho `ra,nome,email` e uma linha neutra, no formato aceito pelo leitor.
_ROSTER_FILE = str(write_roster(os.path.join(_BOT_TOKEN_DIR, "lista-da-turma.csv"), [
    {"ra": "5512340001", "name": "Nome Inventado Da Lista", "email": "pessoa.da.lista@turma.example.org"}]))
# Pasta do banco: a pasta que `banks upload` lê, confere e empacota antes de chamar a API; uma cópia do
# curso de fixture (a origem não muda).
_BANK_FOLDER = str(copy_course(os.path.join(_BOT_TOKEN_DIR, "banco-local")))

_SAMPLE_ARGS: dict[str, list[str]] = {
    "health": ["health"],
    "people add": ["people", "add", "--name", "Nome Inventado Qualquer"],
    "people list": ["people", "list"],
    "people show": ["people", "show", "abcd"],
    "identity reveal": ["identity", "reveal", "abcd", "--reason", "motivo qualquer"],
    "tokens create": _token_args("rotulo qualquer", "people:read"),
    "tokens list": ["tokens", "list"],
    "tokens revoke": ["tokens", "revoke", "00000000-0000-4000-8000-000000000000"],
    "events list": ["events", "list", "--type", "person.registered"],
    # Oferecimentos e bots
    "offerings create": ["offerings", "create", _ANY_OFFERING_REF, "--course", "Disciplina Qualquer",
                         "--term", "2031s1", "--timezone", "America/Sao_Paulo"],
    "offerings list": ["offerings", "list"],
    "offerings show": ["offerings", "show", _ANY_OFFERING_REF],
    "offerings close": ["offerings", "close", _ANY_OFFERING_REF],
    "offerings archive prepare": ["offerings", "archive", "prepare", _ANY_OFFERING_REF],
    "offerings archive confirm": ["offerings", "archive", "confirm", _ANY_OFFERING_REF,
                                  "--confirmation", "Aa0_-Aa0_-Aa0_-Aa0_-Zz"],
    "bots register": ["bots", "register", _ANY_OFFERING_REF, "--token-file", _BOT_TOKEN_FILE],
    "bots activate": ["bots", "activate", _ANY_OFFERING_REF],
    "bots show": ["bots", "show", _ANY_OFFERING_REF],
    "team link": ["team", "link", _ANY_OFFERING_REF, "--person", "abcd"],
    # Vínculo do aluno
    "students import": ["students", "import", _ANY_OFFERING_REF, "--file", _ROSTER_FILE],
    "students summary": ["students", "summary", _ANY_OFFERING_REF],
    "invites send prepare": ["invites", "send", "prepare", _ANY_OFFERING_REF],
    "invites send confirm": ["invites", "send", "confirm", _ANY_OFFERING_REF,
                             "--confirmation", "Aa0_-Aa0_-Aa0_-Aa0_-Zz"],
    "invites resend": ["invites", "resend", _ANY_OFFERING_REF, "abcd"],
    # Disparo e resposta
    "dispatches prepare": ["dispatches", "prepare", _ANY_OFFERING_REF,
                           "--repo", "https://repos.cq.test/turma/questoes.git",
                           "--commit", "0123456789abcdef0123456789abcdef01234567",
                           "--qid", "velocidade-media",
                           "--opens-at", "2031-03-02T12:00:00-03:00",
                           "--closes-at", "2031-03-02T13:00:00-03:00"],
    "dispatches show": ["dispatches", "show", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "dispatches list": ["dispatches", "list", _ANY_OFFERING_REF, "--after", "0", "--limit", "10"],
    "dispatches confirm": ["dispatches", "confirm", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "dispatches cancel": ["dispatches", "cancel", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "messages list": ["messages", "list", _ANY_OFFERING_REF, "--after", "0", "--limit", "10"],
    # A configuração de mensagens livres do oferecimento.
    "offerings settings": ["offerings", "settings", _ANY_OFFERING_REF, "--free-messages", "on"],
    # Os relatórios e o resumo diário. O `offerings digest set`
    # leva `--enabled on` (sem nenhuma opção, a folha é erro de uso antes de chamar a API).
    "reports show": ["reports", "show", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "reports versions": ["reports", "versions", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "reports resend": ["reports", "resend", _ANY_OFFERING_REF, "00000000-0000-4000-8000-000000000000"],
    "offerings digest show": ["offerings", "digest", "show", _ANY_OFFERING_REF],
    "offerings digest set": ["offerings", "digest", "set", _ANY_OFFERING_REF, "--enabled", "on"],
    # A preparação e a confirmação do expurgo.
    "offerings purge prepare": ["offerings", "purge", "prepare", _ANY_OFFERING_REF],
    "offerings purge confirm": ["offerings", "purge", "confirm", _ANY_OFFERING_REF,
                                "--confirmation", "Aa0_-Aa0_-Aa0_-Aa0_-Zz"],
    # O envio de uma pasta de banco local e a listagem das versões.
    "banks upload": ["banks", "upload", _BANK_FOLDER, "--name", "banco-teste"],
    "banks list": ["banks", "list", "--name", "banco-teste", "--after", "0", "--limit", "10"],
}
# A folha `doctor` (`system.doctor`) fica fora: com a API fora do ar, ela
# relata as verificações em vez de terminar com o código 3 (coberta em `tests/cli/test_cli_doctor.py`).
CATALOG_COMMANDS = [op["cli"] for op in load_catalog()["operations"] if op["cli"] != "doctor"]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("command", CATALOG_COMMANDS)
def test_api_unreachable(command, json_mode, cli_invoke, monkeypatch):
    """sem `http`, API em `http://127.0.0.1:9/` → 3, mensagem exata, sem rastreamento.
    Também os 10 comandos de oferecimentos e bots e os 5 do vínculo do aluno
    (o `students import` lê o arquivo da lista, válido,
    antes de chamar a API), os 6 do disparo e resposta, os 5 dos relatórios
    (`reports show`, `reports versions`,
    `reports resend`, `offerings digest show` e `offerings digest set`) e os
    2 comandos do expurgo (`offerings purge prepare` e `offerings purge confirm`)
    e os 2 do banco local (o `banks upload` lê a pasta, válida, antes de chamar a API);
    o `doctor` fica fora (`tests/cli/test_cli_doctor.py`)."""
    _terminal(monkeypatch, True)
    result = cli_invoke(_SAMPLE_ARGS[command], json=json_mode, http=None,
                        env={"CQ_API_URL": "http://127.0.0.1:9/"})
    _no_traceback(result)
    assert result.exit_code == 3
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "api_unreachable", "message": UNREACHABLE_MESSAGE}}
    else:
        assert result.stderr == _out(f"Erro: {UNREACHABLE_MESSAGE}")


# --------------------------------------------------------------------------------------------------
# Erros da API e códigos de saída


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("kind", ["sem-token", "desconhecido", "revogado"])
def test_api_error_unauthenticated(kind, json_mode, cli_invoke, client):
    """401 → código 1; `--json`: o corpo de erro; texto: `Erro: Token ausente, inválido ou revogado.`."""
    if kind == "sem-token":
        token = None
    elif kind == "desconhecido":
        token = "cq_tokendesconhecidosentinela_" + "z" * 20
    else:
        token, created = create_token(client, ADMIN_TOKEN, ["people:read"], label="token revogado")
        _api_post(client, "tokens.revoke", f"/v1/tokens/{created['token_id']}/revoke")
    result = cli_invoke(["people", "list"], json=json_mode, token=token)
    _no_traceback(result)
    assert result.exit_code == 1
    assert result.stdout == ""
    api = client.get("/v1/people", headers=auth(token))
    assert_error(api, 401, "unauthenticated")
    if json_mode:
        assert json.loads(result.stderr) == api.json()
    else:
        assert result.stderr == _out("Erro: Token ausente, inválido ou revogado.")
    if token is not None:
        assert token not in result.stderr


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_forbidden(json_mode, cli_invoke, client):
    """403 → código 1 e a linha `Permissão necessária: people:write`; nada é cadastrado."""
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="so leitura")
    result = cli_invoke(["people", "add", "--name", "Nome Qualquer Teste"], json=json_mode, token=secret)
    _no_traceback(result)
    assert result.exit_code == 1
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "forbidden", "message": MESSAGES["forbidden"],
                                                       "permission": "people:write"}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: people:write")
    assert _api_get(client, "/v1/people") == {"items": [], "next_cursor": 0}


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_invalid_request(json_mode, cli_invoke, client):
    """400 → código 1 e uma linha `  - <field>: <reason>` por campo, sem eco."""
    add = cli_invoke(["people", "add", "--name", "   ", "--ra", "98765x4321", "--email", "semarroba-sentinela"],
                     json=json_mode)
    _no_traceback(add)
    assert add.exit_code == 1
    assert add.stdout == ""
    fields = [("name", "too_short"), ("ra", "invalid_format"), ("email", "invalid_format")]
    if json_mode:
        assert json.loads(add.stderr) == {"error": {
            "code": "invalid_request", "message": MESSAGES["invalid_request"],
            "fields": [{"field": f, "reason": r} for f, r in fields]}}
    else:
        assert add.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", *(f"  - {f}: {r}" for f, r in fields))
    assert "98765x4321" not in add.stderr and "semarroba-sentinela" not in add.stderr

    reveal = cli_invoke(["identity", "reveal", "abcd", "ITEMSENTINELA", "--reason", "conferir itens"],
                        json=json_mode)
    _no_traceback(reveal)
    assert reveal.exit_code == 1
    assert reveal.stdout == ""
    if json_mode:
        assert json.loads(reveal.stderr) == {"error": {
            "code": "invalid_request", "message": MESSAGES["invalid_request"],
            "fields": [{"field": "pseudonyms[1]", "reason": "invalid_format"}]}}
    else:
        assert reveal.stderr == _out(f"Erro: {MESSAGES['invalid_request']}", "  - pseudonyms[1]: invalid_format")
    assert "itemsentinela" not in reveal.stderr.casefold()
    assert _api_get(client, "/v1/people") == {"items": [], "next_cursor": 0}


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_not_found(json_mode, cli_invoke):
    """404 → código 1 e `Erro: Recurso não encontrado.`."""
    for args in (["people", "show", "abcd"], ["tokens", "revoke", str(uuid4_with_prefix("dddd", fill="7"))]):
        result = cli_invoke(args, json=json_mode)
        _no_traceback(result)
        assert result.exit_code == 1, args
        assert result.stdout == ""
        if json_mode:
            assert json.loads(result.stderr) == {"error": {"code": "not_found", "message": MESSAGES["not_found"]}}
        else:
            assert result.stderr == _out(f"Erro: {MESSAGES['not_found']}")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_conflict(json_mode, cli_invoke, client):
    """409 → código 1, uma linha por campo, sem os valores nem o pseudônimo existente."""
    existing = register_person(client, ADMIN_TOKEN, name="Elisa Inventada Porto", ra="4455667788",
                               email="elisa.inventada@exemplo.example.com")
    result = cli_invoke(["people", "add", "--name", "Outra Pessoa Qualquer", "--ra", "4455667788",
                         "--email", "  ELISA.Inventada@exemplo.example.com "], json=json_mode)
    _no_traceback(result)
    assert result.exit_code == 1
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {
            "code": "conflict", "message": MESSAGES["conflict"],
            "fields": [{"field": "ra", "reason": "duplicate"}, {"field": "email", "reason": "duplicate"}]}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['conflict']}", "  - ra: duplicate", "  - email: duplicate")
    for value in ("4455667788", "elisa.inventada", existing["pseudo_id"], existing["alias"]):
        assert value not in result.stderr.casefold()
    assert [p["pseudo_id"] for p in _api_get(client, "/v1/people")["items"]] == [existing["pseudo_id"]]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_error_internal(json_mode, cli_invoke, app_factory, unreachable_db_url):
    """500 (banco inalcançável) → código 1 e `Erro: Erro interno. …`, sem a senha do banco."""
    app = app_factory(database_url=unreachable_db_url)
    with TestClient(app, raise_server_exceptions=False) as http:
        result = cli_invoke(["people", "list"], json=json_mode, http=http)
    _no_traceback(result)
    assert result.exit_code == 1
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "internal", "message": MESSAGES["internal"]}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES['internal']}")
    assert DB_PASSWORD_SENTINEL not in result.stderr


# --------------------------------------------------------------------------------------------------
# Uso incorreto (código 2, `Uso incorreto:`, sem eco)

_USAGE_CASES = [
    ("subcomando-desconhecido", ["people", "bogus"], "bogus"),
    ("subcomando-sentinela", ["people", "comandosentinela"], "comandosentinela"),
    ("grupo-desconhecido", ["gruposentinela", "list"], "gruposentinela"),
    ("opcao-desconhecida", ["people", "list", "--opcaosentinela", "1"], "opcaosentinela"),
    ("opcao-global-desconhecida", ["--opcaosentinela", "people", "list"], "opcaosentinela"),
    ("opcao-desconhecida-no-cadastro", ["people", "add", "--name", "Nome Inventado Qualquer",
                                        "--apelidosentinela", "valorsentinela"], "sentinela"),
    ("limit-nao-inteiro", ["people", "list", "--limit", "abcdefgh"], "abcdefgh"),
    ("after-nao-inteiro", ["events", "list", "--type", "person.registered", "--after", "abcdefgh"], "abcdefgh"),
    ("argumento-a-mais", ["people", "list", "argumentosentinela"], "argumentosentinela"),
    ("argumento-a-mais-revoke", ["tokens", "revoke", "00000000-0000-4000-8000-000000000000",
                                 "argumentosentinela"], "argumentosentinela"),
    ("argumento-faltando", ["people", "show"], None),
    ("referencias-faltando", ["identity", "reveal", "--reason", "motivosentinela"], "motivosentinela"),
    ("opcao-obrigatoria-faltando", ["people", "add", "--ra", "1234567890"], "1234567890"),
    ("valor-faltando", ["people", "add", "--name"], None),
]


@pytest.mark.parametrize("args,sentinel", [(a, s) for _, a, s in _USAGE_CASES], ids=[c[0] for c in _USAGE_CASES])
def test_usage_error_without_echo(args, sentinel, cli_invoke, client):
    """Uso incorreto → 2, stderr começa por `Uso incorreto:`, sem o valor digitado."""
    result = cli_invoke(args)
    _no_traceback(result)
    assert result.exit_code == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr.startswith("Uso incorreto:"), result.stderr
    if sentinel is not None:
        assert sentinel not in result.stderr.casefold()
    assert _api_get(client, "/v1/people") == {"items": [], "next_cursor": 0}
    assert all_events(client, ADMIN_TOKEN, "person.registered", "token.created", "token.revoked") == []


# --------------------------------------------------------------------------------------------------
# `health` com erro (código 1; JSON na saída padrão; texto no stderr)


@pytest.mark.parametrize("case", ["banco-vazio", "esquema-futuro", "banco-inalcancavel"])
def test_health_error(case, cli_invoke, client, sql, app_factory, empty_db_url, unreachable_db_url):
    """`health` com `status = error` → 1; JSON na saída padrão; texto exato no stderr."""
    with contextlib.ExitStack() as stack:
        if case == "esquema-futuro":
            sql.execute("INSERT INTO schema_migrations (version, applied_at) VALUES ('9999', now())")
            http = client
            database, schema = "ok", "9999"
        else:
            url = empty_db_url if case == "banco-vazio" else unreachable_db_url
            http = stack.enter_context(TestClient(app_factory(database_url=url), raise_server_exceptions=False))
            database = "ok" if case == "banco-vazio" else "unreachable"
            schema = None
        api = http.get("/health")
        assert api.status_code == 503
        assert_matches("system.health", 503, api.json())
        assert api.json() == {"status": "error", "version": cq_server.__version__, "database": database,
                              "schema_version": schema, "expected_schema_version": EXPECTED_VERSION}

        as_json = cli_invoke(["health"], json=True, http=http)
        _no_traceback(as_json)
        assert as_json.exit_code == 1
        assert json.loads(as_json.stdout) == api.json()
        assert as_json.stderr == ""

        as_text = cli_invoke(["health"], http=http)
        _no_traceback(as_text)
        assert as_text.exit_code == 1
        assert as_text.stdout == ""
        assert as_text.stderr == _out(f"Erro: API com problema (banco: {database}, esquema: {schema or 'nenhum'}).")
    for result in (as_json, as_text):
        assert DB_PASSWORD_SENTINEL not in result.stdout + result.stderr


# --------------------------------------------------------------------------------------------------
# Logs do cliente (o grupo põe `httpx2`, `httpcore` e `httpcore2` em WARNING)


def test_cli_raises_http_loggers_to_warning(cli_invoke):
    """depois de invocar o `cli`, `httpx2`, `httpcore` e `httpcore2` ficam em
    WARNING ou acima."""
    names = ("httpx2", "httpcore", "httpcore2")
    saved = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.DEBUG)
        result = cli_invoke(["health"])
        _ok(result)
        levels = {name: logging.getLogger(name).level for name in names}
        assert all(level >= logging.WARNING for level in levels.values()), levels
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


# --------------------------------------------------------------------------------------------------
# Ponto de entrada


def test_help_and_entry_point(cli_invoke):
    """`cq health --help` → 0; o script `cq` aponta para `cq_client.cli:main`."""
    result = cli_invoke(["health", "--help"])
    _ok(result)
    scripts = {ep.name: ep.value for ep in importlib.metadata.distribution("cq-client").entry_points
               if ep.group == "console_scripts"}
    assert scripts == {"cq": "cq_client.cli:main"}
