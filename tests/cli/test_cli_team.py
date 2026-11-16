"""CLI `cq team link` (o link de equipe pelo CLI: comandos, link de
equipe, texto para pessoas, `--json` e erros).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`), como nos outros testes do CLI (`tests/cli/test_cli.py`). O link é uma credencial: aparece só num
terminal (`cli.stdout_is_terminal()`, substituído pelos testes) e, fora dele, só no arquivo de
`--secret-file` (criação exclusiva, `0600`, `link + "\\n"` por `cli.write_secret_file`). Todo link
lido de uma saída entra em `remember_secret` antes de qualquer outra conferência.

A validade no fuso do oferecimento é conferida contra valores escritos aqui (o `FakeClock` começa em
2031-01-01T00:00:00Z; em 2031, `America/Sao_Paulo` é `-03` e `Asia/Kolkata` é `IST`, +05:30).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

import cq_client.cli as cq_cli
from tests.helpers.api import (
    ADMIN_TOKEN,
    TEAM_LINK_SHAPE,
    all_events,
    auth,
    create_offering,
    link_code,
    register_bot,
    register_person,
)
from tests.helpers.canary import find_secrets
from tests.helpers.clock import START, iso
from tests.helpers.contract import assert_matches
from tests.helpers.secrets import all_secrets, remember_secret

ADMIN = auth(ADMIN_TOKEN)
LINK_NOT_SAVED = "O link não pôde ser gravado; gere outro."
LINK_NOT_SAVED_TEXT = "Erro: o link não pôde ser gravado; gere outro."
NOT_FOUND_ALIAS = "ffff0000ffff"


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


def _terminal(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Substitui `cli.stdout_is_terminal` (o ponto que os testes substituem)."""
    monkeypatch.setattr(cq_cli, "stdout_is_terminal", lambda: value)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prepare(client: Any, bot_api: Any, *, code: str = "mc102-cli-equipe", timezone: str = "America/Sao_Paulo",
             username: str = "cq_cli_equipe_bot") -> tuple[dict, dict]:
    """Oferecimento em rascunho com bot registrado e uma pessoa cadastrada."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, timezone=timezone)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Professora Cli", ra="5500000401")
    return offering, person


def _args(ref: str, person: str, *extra: str) -> list[str]:
    return ["team", "link", ref, "--person", person, *extra]


def _remember_link(link: str) -> str:
    """Guarda o link e o código em `remember_secret` e devolve o código."""
    remember_secret(link)
    match = TEAM_LINK_SHAPE.fullmatch(link)
    assert match is not None, "link fora do padrão do link de equipe"
    remember_secret(match.group(2))
    return match.group(2)


def _read_link_file(path: Any) -> str:
    """O arquivo de `--secret-file` tem exatamente `link + "\\n"`."""
    raw = path.read_bytes()
    assert re.fullmatch(rb"https://t\.me/[A-Za-z0-9_]{5,32}\?start=[A-Za-z0-9_-]{22}\n", raw), (
        "o arquivo deve ter só o link e um \\n")
    link = raw[:-1].decode("ascii")
    _remember_link(link)
    return link


def _invites(sql: Any) -> list[tuple]:
    return sql.execute("SELECT code_sha256, state, expires_at FROM invites ORDER BY id").fetchall()


def _public_body(offering: dict, person: dict, expires_at: Any) -> dict:
    """O corpo de `team.link` sem o `link`."""
    return {"expires_at": iso(expires_at),
            "person": {"pseudo_id": person["pseudo_id"], "alias": person["alias"]},
            "offering": {"offering_id": offering["offering_id"], "code": offering["code"],
                         "timezone": offering["timezone"]}}


def _first_line(person: dict, expires_at: Any, local: str) -> str:
    """A primeira linha do texto: `Link de equipe para <alias> (válido até <expires_at>; <local>):`."""
    return f"Link de equipe para {person['alias']} (válido até {iso(expires_at)}; {local}):"


@contextlib.contextmanager
def _recorded_requests(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


class _Response:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class _ZoneUnknownToClient:
    """Cliente HTTP que repassa ao `TestClient` e troca o `offering.timezone` da resposta de
    `team.link` por um fuso que o `tzdata` do cliente não conhece (servidor com base de fusos mais
    nova que a do cliente)."""

    def __init__(self, client: Any, zone: str) -> None:
        self._client = client
        self._zone = zone

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._client.request(method, url, **kwargs)
        if method == "POST" and url.endswith("/team-links") and response.status_code == 201:
            body = response.json()
            _remember_link(body["link"])
            body["offering"]["timezone"] = self._zone
            return _Response(201, body)
        return response


# --------------------------------------------------------------------------------------------------
# No terminal


@pytest.mark.parametrize("case", [
    pytest.param(("America/Sao_Paulo", None, "2031-01-07 21:00 -03 America/Sao_Paulo"), id="sao-paulo-padrao"),
    pytest.param(("Asia/Kolkata", "5", "2031-01-01 10:30 IST Asia/Kolkata"), id="kolkata-5h"),
])
@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_link_on_terminal_shows_the_link_and_the_local_validity(case: tuple, json_mode: bool,
                                                                cli_invoke: Any, client: Any, clock: Any,
                                                                sql: Any, bot_api: Any,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """O CLI mostra o link só num terminal, com a validade em UTC e no fuso do
    oferecimento: no terminal, sem `--secret-file`, o texto é `Link de equipe para
    <alias> (válido até <expires_at>; AAAA-MM-DD HH:MM <tzname> <fuso>):` e o link numa linha
    própria; com `--json`, o corpo da API (com o `link`). O link é o do convite gravado."""
    timezone, hours, local = case
    assert clock.now() == START
    _terminal(monkeypatch, True)
    offering, person = _prepare(client, bot_api, timezone=timezone)
    expires_at = START + timedelta(hours=168 if hours is None else int(hours))
    extra = [] if hours is None else ["--valid-hours", hours]

    result = cli_invoke(_args(offering["code"], person["alias"], *extra), json=json_mode)
    _ok(result)
    if json_mode:
        body = json.loads(result.stdout)
        link = body["link"]
        _remember_link(link)
        assert_matches("team.link", 201, body)
        assert body == {"link": link, **_public_body(offering, person, expires_at)}
    else:
        lines = result.stdout.splitlines()
        assert len(lines) == 2, "esperava a linha da validade e a do link"
        link = lines[1]
        _remember_link(link)
        assert result.stdout == _out(_first_line(person, expires_at, local), link)
    code = _remember_link(link)
    assert link == f"https://t.me/cq_cli_equipe_bot?start={code}"
    assert _invites(sql) == [(_sha256(code), "pending", expires_at)]


# --------------------------------------------------------------------------------------------------
# Fora do terminal: --secret-file


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_outside_terminal_requires_secret_file_without_calling_the_api(json_mode: bool, cli_invoke: Any,
                                                                       client: Any, sql: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """O link de equipe no CLI: fora de um terminal e sem `--secret-file`, código 2,
    `Uso incorreto: fora de um terminal, informe --secret-file.` no stderr, nada no stdout e nenhum
    pedido à API; na mesma função, `cq team link --help` sai com 0 e cita `--secret-file`."""
    _terminal(monkeypatch, False)
    with _recorded_requests(client) as seen:
        result = cli_invoke(_args("mc102-cli-equipe", "abcd"), json=json_mode)
    _no_traceback(result)
    assert (result.exit_code, result.stdout, result.stderr) == (
        2, "", _out("Uso incorreto: fora de um terminal, informe --secret-file."))
    assert seen == []

    help_result = cli_invoke(["team", "link", "--help"], json=json_mode)
    _ok(help_result)
    assert "--secret-file" in help_result.stdout and "--person" in help_result.stdout
    assert "--valid-hours" in help_result.stdout


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_secret_file_receives_the_link(terminal: bool, json_mode: bool, cli_invoke: Any, client: Any,
                                       clock: Any, sql: Any, bot_api: Any, monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Any) -> None:
    """Como o segredo de `tokens create`: com `--secret-file`, o arquivo
    é criado antes do pedido à API, com `link + "\\n"` (permissão `0600` onde o sistema permite); a
    saída não tem o link: `Link de equipe para …:` e `Link gravado em <caminho>.` (texto) ou o corpo
    sem `link` e com `"secret_file": "<caminho>"` (`--json`), no terminal e fora dele."""
    _terminal(monkeypatch, terminal)
    offering, person = _prepare(client, bot_api)
    expires_at = START + timedelta(hours=168)
    path = tmp_path / "link.txt"
    existed_at_request: list[bool] = []

    def check_file(request: Any) -> None:
        if request.url.path.endswith("/team-links"):
            existed_at_request.append(path.exists())

    hooks = client.event_hooks["request"]
    hooks.append(check_file)
    try:
        result = cli_invoke(_args(offering["code"], person["alias"], "--secret-file", str(path)),
                            json=json_mode)
    finally:
        hooks.remove(check_file)
    _ok(result)
    assert existed_at_request == [True], "o arquivo de --secret-file deve existir antes da chamada à API"
    link = _read_link_file(path)
    code = link_code(link)
    assert link == f"https://t.me/cq_cli_equipe_bot?start={code}"
    assert _invites(sql) == [(_sha256(code), "pending", expires_at)]
    if json_mode:
        assert json.loads(result.stdout) == {**_public_body(offering, person, expires_at),
                                             "secret_file": str(path)}
    else:
        assert result.stdout == _out(
            _first_line(person, expires_at, "2031-01-07 21:00 -03 America/Sao_Paulo"),
            f"Link gravado em {path}.")
    assert find_secrets(result.stdout + result.stderr, [link, code]) == []
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_write_failure_after_the_link_exists_exits_1(terminal: bool, json_mode: bool, cli_invoke: Any,
                                                     client: Any, clock: Any, sql: Any, bot_api: Any,
                                                     monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Se o arquivo não pode ser escrito depois de criado o link, código 1, stdout vazio
    e `Erro: o link não pôde ser gravado; gere outro.` (texto) ou o envelope `link_not_saved` exato no
    stderr (`--json`); o link recebido pela escrita é o do convite, que fica pendente (até vencer ou
    ser substituído), e não aparece em nenhuma saída."""
    _terminal(monkeypatch, terminal)
    offering, person = _prepare(client, bot_api)
    received: list[str] = []

    def failing_write(file: Any, secret: str) -> None:
        received.append(secret)
        if TEAM_LINK_SHAPE.fullmatch(secret):
            _remember_link(secret)
        raise OSError("disco cheio (simulado)")

    monkeypatch.setattr(cq_cli, "write_secret_file", failing_write)
    path = tmp_path / "link.txt"
    result = cli_invoke(_args(offering["code"], person["alias"], "--secret-file", str(path)), json=json_mode)
    _no_traceback(result)
    assert result.exit_code == 1, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": "link_not_saved", "message": LINK_NOT_SAVED}}
    else:
        assert result.stderr == _out(LINK_NOT_SAVED_TEXT)
    assert len(received) == 1, "write_secret_file deve ser chamada uma vez, com o link criado"
    code = _remember_link(received[0])
    assert _invites(sql) == [(_sha256(code), "pending", START + timedelta(hours=168))]
    assert find_secrets(result.stdout + "\n" + result.stderr, [received[0], code]) == []


# --------------------------------------------------------------------------------------------------
# Fuso indisponível e erros


def test_zone_unknown_to_the_client_prints_the_line_without_local_validity(
        cli_invoke: Any, client: Any, clock: Any, sql: Any, bot_api: Any,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """se o cliente não carrega o fuso da resposta, a linha vira `Link de equipe
    para <alias> (válido até <expires_at>; fuso indisponível):`, o link sai no terminal e o código é
    0."""
    _terminal(monkeypatch, True)
    offering, person = _prepare(client, bot_api)
    expires_at = START + timedelta(hours=168)
    http = _ZoneUnknownToClient(client, "Etc/Fuso_Que_O_Cliente_Nao_Conhece")

    result = cli_invoke(_args(offering["code"], person["alias"]), http=http)
    _ok(result)
    lines = result.stdout.splitlines()
    assert len(lines) == 2, "esperava a linha da validade e a do link"
    link = lines[1]
    code = _remember_link(link)
    assert result.stdout == _out(_first_line(person, expires_at, "fuso indisponível"), link)
    assert _invites(sql) == [(_sha256(code), "pending", expires_at)]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_api_errors_are_reported_without_a_link(json_mode: bool, cli_invoke: Any, client: Any, clock: Any,
                                                sql: Any, bot_api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Os erros do CLI, como nos outros comandos (no 409 `invalid_state`, a linha `Estado atual: <state>`), e
    os do link de equipe: pessoa inexistente (404), oferecimento sem bot (`bot_required`), oferecimento arquivado
    (`invalid_state`) e `--valid-hours 721` (400 `out_of_range`) saem com código 1, a mensagem da API
    (ou o corpo de erro, com `--json`) e nenhum convite criado."""
    _terminal(monkeypatch, True)
    offering, person = _prepare(client, bot_api)
    no_bot = create_offering(client, ADMIN_TOKEN, code="mc102-cli-sem-bot")
    archived = create_offering(client, ADMIN_TOKEN, code="mc102-cli-arquivado")
    prepared = client.post(f"/v1/offerings/{archived['code']}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text
    confirmed = client.post(f"/v1/offerings/{archived['code']}/archive",
                            json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert confirmed.status_code == 200, confirmed.text

    cases = [
        (_args(offering["code"], NOT_FOUND_ALIAS),
         {"error": {"code": "not_found", "message": "Recurso não encontrado."}},
         ["Erro: Recurso não encontrado."]),
        (_args(no_bot["code"], person["alias"]),
         {"error": {"code": "bot_required", "message": "O oferecimento não tem um bot registrado com acesso."}},
         ["Erro: O oferecimento não tem um bot registrado com acesso."]),
        (_args(archived["code"], person["alias"]),
         {"error": {"code": "invalid_state",
                    "message": "A operação não é permitida no estado atual do oferecimento.",
                    "state": "archived"}},
         ["Erro: A operação não é permitida no estado atual do oferecimento.", "Estado atual: archived"]),
        (_args(offering["code"], person["alias"], "--valid-hours", "721"),
         {"error": {"code": "invalid_request", "message": "Pedido inválido.",
                    "fields": [{"field": "valid_hours", "reason": "out_of_range"}]}},
         ["Erro: Pedido inválido.", "  - valid_hours: out_of_range"]),
    ]
    for args, body, lines in cases:
        result = cli_invoke(args, json=json_mode)
        _no_traceback(result)
        assert (result.exit_code, result.stdout) == (1, ""), (args, result.exit_code, result.stderr)
        if json_mode:
            assert json.loads(result.stderr) == body, args
        else:
            assert result.stderr == _out(*lines), args
    assert _invites(sql) == []
    assert all_events(client, ADMIN_TOKEN, "team.link_created") == []


def test_usage_errors_exit_2_without_echo_or_request(cli_invoke: Any, client: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Só `--valid-hours` é tipado (inteiro), e o uso incorreto sai sem eco: `--valid-hours`
    não inteiro e a falta de `--person` saem com código 2 e a mensagem exata, sem repetir o valor e
    sem pedido à API; na mesma função, `cq team link --help` sai com 0."""
    _terminal(monkeypatch, True)
    sentinel = "horas-sentinela-9Qx"
    with _recorded_requests(client) as seen:
        bad_hours = cli_invoke(_args("mc102-cli-equipe", "abcd", "--valid-hours", sentinel))
        no_person = cli_invoke(["team", "link", "mc102-cli-equipe"])
    for result, message in ((bad_hours, "valor inválido na opção --valid-hours."),
                            (no_person, "falta a opção --person.")):
        _no_traceback(result)
        assert (result.exit_code, result.stdout, result.stderr) == (2, "", _out(f"Uso incorreto: {message}"))
    assert sentinel not in bad_hours.stdout + bad_hours.stderr
    assert seen == []
    _ok(cli_invoke(["team", "link", "--help"]))
    assert find_secrets(bad_hours.stderr + no_person.stderr, all_secrets()) == []
