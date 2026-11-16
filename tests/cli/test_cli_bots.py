"""CLI `cq bots register` e `cq bots show`, e os erros de `cq bots activate`.

Cobre o registro, a consulta e a ativação do bot pelo CLI: o operador registra o token pelo CLI, que lê o token de um
prompt mascarado ou de um arquivo, nunca da linha de comando (nenhuma opção ou argumento recebe o token); os
comandos dos bots (a leitura do token, o texto para pessoas, `--json` e
erros, com a linha `Estado atual: <state>` no `invalid_state`) e as regras gerais do CLI (códigos de saída, uso
incorreto sem eco, nenhuma saída com rastreamento de pilha).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API). A
entrada do token é substituída nos pontos expostos para os testes, `cli.stdin_is_terminal()`
e `cli.prompt_secret(label)`; um teste roda o CLI num subprocesso com a entrada
padrão em pipe, sem substituição, e outro usa o `cli.prompt_secret` real (só `cli.stdin_is_terminal`
substituído), com o token na entrada do `CliRunner`. Nenhuma saída pode conter o token (inteiro ou a
parte secreta).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import cq_client.cli as cq_cli
from tests.helpers.api import ADMIN_TOKEN, activate_bot, auth, create_offering, register_bot
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_matches
from tests.helpers.secrets import all_secrets, remember_secret

ADMIN = auth(ADMIN_TOKEN)
PROMPT_LABEL = "Token do bot"
USAGE_NO_FILE = "Uso incorreto: fora de um terminal, informe --token-file."
USAGE_UNREADABLE = "Uso incorreto: não foi possível ler o arquivo de --token-file."
# Uma opção `--token` (sem ser a `--token-file`) na ajuda.
TOKEN_OPTION = re.compile(r"--token(?!-file)")

# Mensagens fixas de `problem`.
PROBLEM_MESSAGES = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "rate_limited": "O Telegram pediu uma pausa; o sistema vai tentar de novo depois do tempo indicado.",
}


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


def _patch_input(monkeypatch: pytest.MonkeyPatch, *, terminal: bool, prompt: str | None = None) -> list[str]:
    """Substitui `cli.stdin_is_terminal` e `cli.prompt_secret` (os pontos que os testes
    substituem). Devolve a lista dos rótulos pedidos ao prompt; sem `prompt`, o prompt não deveria
    ser chamado (o teste confere a lista vazia)."""
    asked: list[str] = []

    def fake_prompt(label: str) -> str:
        asked.append(label)
        return prompt if prompt is not None else "valor-inesperado-do-prompt"

    monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: terminal)
    monkeypatch.setattr(cq_cli, "prompt_secret", fake_prompt)
    return asked


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


def _path(ref: str, suffix: str = "/bot") -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}{suffix}"


def _get_bot(client: Any, ref: str) -> dict:
    resp = client.get(_path(ref), headers=ADMIN)
    assert resp.status_code == 200, f"bots.get: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("bots.get", 200, body)
    return body


def _post_ok(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _archive(client: Any, ref: str) -> None:
    prepared = _post_ok(client, "offerings.prepare_archive", _path(ref, "/archive/prepare"))
    archived = _post_ok(client, "offerings.archive", _path(ref, "/archive"),
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived"


def _bot_body(offering: dict, telegram_bot_id: int, username: str, *, registered_at: str,
              state: str = "registered", profile_state: str = "pending", lease: dict | None = None,
              last_poll_ok_at: str | None = None, problem: str | None = None) -> dict:
    """O `Bot` exato do contrato."""
    return {
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
        "username": username, "telegram_bot_id": telegram_bot_id, "state": state,
        "profile_state": profile_state, "registered_at": registered_at, "token_replaced_at": None,
        "lease": lease, "last_poll_ok_at": last_poll_ok_at,
        "problem": None if problem is None else {"code": problem, "message": PROBLEM_MESSAGES[problem]},
    }


def _token_forms(*tokens: str) -> list[str]:
    forms: list[str] = []
    for token in tokens:
        forms.append(token)
        if ":" in token:
            forms.append(token.split(":", 1)[1])
    return forms


def _new_bot(bot_api: Any) -> tuple[int, str, str]:
    bot = bot_api.add_bot()
    return bot, bot_api.token_of(bot), bot_api.state(bot).username


def _token_file(tmp_path: Any, content: str, name: str = "token-do-bot.txt") -> Any:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


def _assert_register_help(cli_invoke: Any) -> None:
    """`cq bots register --help` sai com 0 e só oferece `--token-file` (nenhuma opção `--token`)."""
    result = cli_invoke(["bots", "register", "--help"])
    _ok(result)
    assert "--token-file" in result.stdout
    assert TOKEN_OPTION.search(result.stdout) is None, result.stdout


def _internal_bot_id(sql: psycopg.Connection, telegram_bot_id: int) -> int:
    rows = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (telegram_bot_id,)).fetchall()
    assert len(rows) == 1
    return int(rows[0][0])


# --------------------------------------------------------------------------------------------------
# `bots register`: de onde vem o token


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_bots_register_prompts_on_terminal(json_mode: bool, cli_invoke: Any, client: Any, clock: Any,
                                           bot_api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leitura do token (num terminal, sem `--token-file` →
    `cli.prompt_secret("Token do bot")`; texto `Bot registrado: @<username> no oferecimento <code>
    (perfil: <profile_state>).`; `--json` = corpo da API): o prompt é pedido uma vez com o rótulo
    exato; um único pedido `PUT /v1/offerings/<code>/bot`, com o token só no corpo (nada na URL) e
    `X-CQ-Client: cli`; a saída é exata e não contém o token; o bot fica registrado."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    asked = _patch_input(monkeypatch, terminal=True, prompt=token)

    with _recorded_requests(client) as seen:
        result = cli_invoke(["bots", "register", offering["code"]], json=json_mode)

    _ok(result)
    assert asked == [PROMPT_LABEL]
    assert [(r.method, str(r.url), r.headers.get("x-cq-client")) for r in seen] == [
        ("PUT", f"http://testserver/v1/offerings/{offering['code']}/bot", "cli")]
    assert json.loads(seen[0].content) == {"token": token}
    expected = _bot_body(offering, bot, username, registered_at=registered_at)
    assert _get_bot(client, offering["code"]) == expected
    if json_mode:
        assert json.loads(result.stdout) == expected
    else:
        assert result.stdout == _out(f"Bot registrado: @{username} no oferecimento {offering['code']} "
                                     "(perfil: pending).")
    assert find_secrets(result.stdout + result.stderr, _token_forms(token)) == []
    assert [c.method for c in bot_api.calls()] == ["getMe"]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
def test_bots_register_reads_token_file(terminal: bool, json_mode: bool, cli_invoke: Any, client: Any,
                                        clock: Any, bot_api: Any, monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Any) -> None:
    """Leitura do token (com `--token-file`, dentro ou fora de um terminal,
    o CLI lê o arquivo em UTF-8 e tira os espaços e as quebras de linha das pontas): o prompt não é
    chamado; o corpo do pedido é exatamente `{"token": <token>}`; a saída é exata e sem o token."""
    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    path = _token_file(tmp_path, "  \r\n" + token + " \r\n\n")
    asked = _patch_input(monkeypatch, terminal=terminal)

    with _recorded_requests(client) as seen:
        result = cli_invoke(["bots", "register", offering["code"], "--token-file", str(path)], json=json_mode)

    _ok(result)
    assert asked == []
    assert [(r.method, str(r.url)) for r in seen] == [
        ("PUT", f"http://testserver/v1/offerings/{offering['code']}/bot")]
    assert json.loads(seen[0].content) == {"token": token}
    expected = _bot_body(offering, bot, username, registered_at=registered_at)
    assert _get_bot(client, offering["code"]) == expected
    if json_mode:
        assert json.loads(result.stdout) == expected
    else:
        assert result.stdout == _out(f"Bot registrado: @{username} no oferecimento {offering['code']} "
                                     "(perfil: pending).")
    assert find_secrets(result.stdout + result.stderr, _token_forms(token)) == []


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_bots_register_outside_terminal_requires_token_file(json_mode: bool, cli_invoke: Any, client: Any,
                                                            bot_api: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Leitura do token (fora de um terminal e sem `--token-file` → código
    2, `Uso incorreto: fora de um terminal, informe --token-file.`, nenhuma chamada à API): saída
    padrão vazia, o stderr exato, nenhum pedido à API nem ao Telegram e nenhum prompt; na mesma
    função, `cq bots register --help` sai com 0 e só oferece `--token-file`."""
    asked = _patch_input(monkeypatch, terminal=False)
    with _recorded_requests(client) as seen:
        result = cli_invoke(["bots", "register", "oferta-qualquer"], json=json_mode)
    _no_traceback(result)
    assert (result.exit_code, result.stdout, result.stderr) == (2, "", _out(USAGE_NO_FILE))
    assert seen == [] and asked == [] and bot_api.calls() == []
    _assert_register_help(cli_invoke)


def test_bots_register_real_prompt_is_masked(client: Any, clock: Any, bot_api: Any,
                                            monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """O CLI lê o token de um prompt mascarado (só num terminal); o token do bot nunca vem por
    argumento (ele vem de um prompt mascarado); nenhum segredo à mostra; a leitura do token
    (`cli.prompt_secret("Token do bot")`, mascarado, sem eco); a implementação
    (`prompt_secret` = `click.prompt(label, hide_input=True)`): com o
    `cli.prompt_secret` **de verdade** (só `cli.stdin_is_terminal` substituído, por `True`) e o token
    digitado na entrada do `CliRunner` com `echo_stdin=True` (toda leitura da entrada que não seja o
    prompt oculto do click é ecoada na saída, como num terminal: `input()`, `sys.stdin.readline()` ou
    `click.prompt` sem `hide_input` mostram o token) → código 0; a saída tem só a linha do rótulo
    `Token do bot` e a linha exata do registro; o único pedido leva o token no corpo; o bot fica
    registrado com esse token; nenhuma forma do token (inteiro ou a parte secreta) no stdout, no
    stderr nem na saída combinada. Controle positivo do detector."""
    from click.testing import CliRunner

    offering = create_offering(client, ADMIN_TOKEN)
    registered_at = iso(clock.now())
    bot, token, username = _new_bot(bot_api)
    monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: True)
    env = {"CQ_API_URL": "http://testserver", "CQ_TOKEN": ADMIN_TOKEN,
           "CQ_CONFIG": str(tmp_path / "config-inexistente.toml")}

    with _recorded_requests(client) as seen:
        result = CliRunner(echo_stdin=True).invoke(cq_cli.cli, ["bots", "register", offering["code"]],
                                                   input=token + "\n", env=env, obj={"http": client})

    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; saída: {result.output!r}"
    registered = f"Bot registrado: @{username} no oferecimento {offering['code']} (perfil: pending)."
    assert [line.rstrip() for line in result.output.splitlines()] == [f"{PROMPT_LABEL}:", registered]
    assert result.stdout.endswith(_out(registered))
    assert [(r.method, str(r.url), r.headers.get("x-cq-client")) for r in seen] == [
        ("PUT", f"http://testserver/v1/offerings/{offering['code']}/bot", "cli")]
    assert json.loads(seen[0].content) == {"token": token}
    assert _get_bot(client, offering["code"]) == _bot_body(offering, bot, username, registered_at=registered_at)
    assert [(c.method, c.token_state) for c in bot_api.calls()] == [("getMe", "valid")]
    forms = _token_forms(token)
    assert find_secrets(f"saída com {token}", forms) != [], "controle positivo do detector"
    for text in (result.stdout, result.stderr, result.output):
        assert find_secrets(text, forms) == []


def test_real_stdin_check_outside_terminal(tmp_path: Any) -> None:
    """Nenhum segredo à mostra; a leitura do token: o `cli.stdin_is_terminal()` de
    verdade, sem substituição, num processo com a entrada padrão em pipe (como quando um agente roda
    o comando) → código 2 e a mensagem exata. A API aponta para `http://127.0.0.1:9` (inalcançável):
    o código 2, e não 3, mostra que a checagem vem antes de qualquer conexão. Na mesma função,
    `cq bots register --help` no mesmo ambiente sai com 0."""
    env = {**os.environ, "CQ_API_URL": "http://127.0.0.1:9", "CQ_TOKEN": ADMIN_TOKEN,
           "CQ_CONFIG": str(tmp_path / "config-inexistente.toml"), "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-c", "from cq_client.cli import main; main()", "bots", "register"]
    proc = subprocess.run([*command, "oferta-qualquer"], env=env, input="", capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert "Traceback" not in proc.stdout and "Traceback" not in proc.stderr, proc.stderr
    assert (proc.returncode, proc.stdout, proc.stderr) == (2, "", _out(USAGE_NO_FILE))
    assert find_secrets(proc.stdout + proc.stderr, all_secrets()) == []

    help_proc = subprocess.run([*command, "--help"], env=env, input="", capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
    assert (help_proc.returncode, help_proc.stderr) == (0, "")
    assert "--token-file" in help_proc.stdout and TOKEN_OPTION.search(help_proc.stdout) is None


@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "fora-de-terminal"])
@pytest.mark.parametrize("case", ["inexistente", "diretorio", "nao-utf8"])
def test_bots_register_unreadable_token_file(case: str, terminal: bool, cli_invoke: Any, client: Any,
                                             bot_api: Any, monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Any) -> None:
    """Leitura do token (arquivo inexistente ou ilegível em UTF-8 → código
    2, `Uso incorreto: não foi possível ler o arquivo de --token-file.`): com e sem `--json`, dentro
    ou fora de um terminal, saída padrão vazia, o stderr exato, nenhum pedido à API nem ao Telegram e
    nenhum prompt; na mesma função, `cq bots register --help` sai com 0."""
    if case == "inexistente":
        path = tmp_path / "nao-existe.txt"
    elif case == "diretorio":
        path = tmp_path / "pasta-do-token"
        path.mkdir()
    else:
        path = tmp_path / "token-nao-utf8.txt"
        path.write_bytes(b"\xff\xfe1234567:" + b"Ab0_-Zy9" * 5 + b"\n")
    asked = _patch_input(monkeypatch, terminal=terminal)

    with _recorded_requests(client) as seen:
        results = [cli_invoke(["bots", "register", "oferta-qualquer", "--token-file", str(path)], json=json_mode)
                   for json_mode in (False, True)]

    for result in results:
        _no_traceback(result)
        assert (result.exit_code, result.stdout, result.stderr) == (2, "", _out(USAGE_UNREADABLE))
    assert seen == [] and asked == [] and bot_api.calls() == []
    _assert_register_help(cli_invoke)


def test_bots_register_has_no_token_option(cli_invoke: Any, client: Any, bot_api: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """O token do bot nunca vem por argumento (nenhuma opção ou argumento recebe o
    token); uso incorreto sem eco: `--token <valor>`, `--token=<valor>` →
    `Uso incorreto: opção desconhecida.`; o token como argumento a mais → `Uso incorreto: há
    argumentos a mais.`; código 2, sem o valor no stderr, nenhum pedido e nenhum prompt; na mesma
    função, a ajuda sai com 0 e não tem opção `--token`."""
    sentinel = "7654321:" + "Sentinela_do_token-" * 2
    remember_secret(sentinel)
    asked = _patch_input(monkeypatch, terminal=True)
    cases = [
        (["bots", "register", "oferta-qualquer", "--token", sentinel], "Uso incorreto: opção desconhecida."),
        (["bots", "register", "oferta-qualquer", f"--token={sentinel}"], "Uso incorreto: opção desconhecida."),
        (["bots", "register", "oferta-qualquer", sentinel], "Uso incorreto: há argumentos a mais."),
    ]
    for args, message in cases:
        with _recorded_requests(client) as seen:
            result = cli_invoke(args)
        _no_traceback(result)
        assert (result.exit_code, result.stdout, result.stderr) == (2, "", _out(message)), args
        assert seen == []
        assert find_secrets(result.stderr, _token_forms(sentinel)) == []
    assert asked == [] and bot_api.calls() == []
    _assert_register_help(cli_invoke)


# --------------------------------------------------------------------------------------------------
# `bots register`: erros da API

API_ERROR_CASES = ["token-recusado", "token-vazio", "oferecimento-inexistente", "arquivado", "outro-bot",
                   "telegram-fora", "sem-chave-mestra"]


def _error_envelope(code: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": MESSAGES[code], **extra}}


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("case", API_ERROR_CASES)
def test_bots_register_api_errors(case: str, json_mode: bool, cli_invoke: Any, client: Any, bot_api: Any,
                                  app_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Erros (na forma comum do CLI; no 409 `invalid_state`,
    depois de `Erro: <message>`, a linha `Estado atual: <state>`) e mensagens exatas: token
    recusado e token vazio (400 com os campos), oferecimento inexistente (404), arquivado (409
    `invalid_state`), outro bot (409 `bot_conflict`), Telegram fora (503 `unavailable`) e API sem
    chave-mestra (503 `not_configured`) → código 1, saída padrão vazia, o stderr exato (texto, ou o
    envelope da API com `--json`) e nenhum token nas saídas."""
    _patch_input(monkeypatch, terminal=False)
    offering = create_offering(client, ADMIN_TOKEN)
    ref = offering["code"]
    bot, token, _ = _new_bot(bot_api)
    content = token
    http: Any = client
    with contextlib.ExitStack() as stack:
        if case == "token-recusado":
            content = "1234567:" + "Ab0_-Zy9" * 5
            remember_secret(content)
            error = _error_envelope("invalid_request", fields=[{"field": "token", "reason": "invalid_value"}])
            text = ["Erro: Pedido inválido.", "  - token: invalid_value"]
        elif case == "token-vazio":
            content = "   \r\n"
            error = _error_envelope("invalid_request", fields=[{"field": "token", "reason": "too_short"}])
            text = ["Erro: Pedido inválido.", "  - token: too_short"]
        elif case == "oferecimento-inexistente":
            ref = "oferta-inexistente"
            error = _error_envelope("not_found")
            text = ["Erro: Recurso não encontrado."]
        elif case == "arquivado":
            _archive(client, ref)
            error = _error_envelope("invalid_state", state="archived")
            text = ["Erro: A operação não é permitida no estado atual do oferecimento.", "Estado atual: archived"]
        elif case == "outro-bot":
            first, first_token, _ = _new_bot(bot_api)
            register_bot(client, ADMIN_TOKEN, ref, first_token)
            error = _error_envelope("bot_conflict", fields=[{"field": "token", "reason": "invalid_value"}])
            text = ["Erro: Esse bot não pode ser usado neste oferecimento.", "  - token: invalid_value"]
        elif case == "telegram-fora":
            bot_api.fail("getMe", error="server_restart", bot_id=bot)
            error = _error_envelope("unavailable")
            text = ["Erro: O Telegram não respondeu. Tente de novo em instantes."]
        else:
            http = stack.enter_context(TestClient(app_factory(bots=None), raise_server_exceptions=False))
            error = _error_envelope("not_configured")
            text = ["Erro: O servidor não tem a chave-mestra dos bots configurada."]
        path = _token_file(tmp_path, content)
        result = cli_invoke(["bots", "register", ref, "--token-file", str(path)], json=json_mode, http=http)

    _no_traceback(result)
    assert result.exit_code == 1, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == error
    else:
        assert result.stderr == _out(*text)
    assert find_secrets(result.stdout + result.stderr, _token_forms(token, content.strip() or token)) == []


# --------------------------------------------------------------------------------------------------
# `bots show`

SHOW_CASES = ["recem-registrado", "com-posse", "sem-acesso", "problema-temporario"]


@pytest.mark.parametrize("case", SHOW_CASES)
def test_bots_show_text(case: str, cli_invoke: Any, client: Any, clock: Any, bot_api: Any,
                        sql: psycopg.Connection) -> None:
    """Contrato (texto de `bots show`: `@<username>  <state>  perfil:
    <profile_state>  posse: <worker_id> até <until>` ou `posse: nenhuma`, `última consulta:
    <last_poll_ok_at ou "-">` e, se houver problema, a linha `Problema: <message>`; horários como a
    API os devolve): para cada estado, o `bots.get` da API é o `Bot` exato, e `cq bots show` pelo
    código e pelo identificador imprime exatamente as linhas esperadas."""
    offering = create_offering(client, ADMIN_TOKEN)
    t0 = clock.now()
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    internal = _internal_bot_id(sql, bot)
    until = t0 + timedelta(seconds=30)
    polled = t0 + timedelta(seconds=2)
    lease = {"worker_id": "worker-cli-exemplo", "until": iso(until)}
    lease_line = f"posse: worker-cli-exemplo até {iso(until)}  última consulta: {iso(polled)}"
    if case in ("com-posse", "problema-temporario"):
        activate_bot(client, ADMIN_TOKEN, offering["code"])
        sql.execute("UPDATE bots SET profile_state = 'configured', lease_owner = 'worker-cli-exemplo', "
                    "lease_until = %s, last_poll_ok_at = %s WHERE id = %s", (until, polled, internal))
    if case == "recem-registrado":
        expected = _bot_body(offering, bot, username, registered_at=iso(t0))
        lines = [f"@{username}  registered  perfil: pending  posse: nenhuma  última consulta: -"]
    elif case == "com-posse":
        expected = _bot_body(offering, bot, username, registered_at=iso(t0), profile_state="configured",
                             lease=lease, last_poll_ok_at=iso(polled))
        lines = [f"@{username}  registered  perfil: configured  {lease_line}"]
    elif case == "sem-acesso":
        sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected', "
                    "profile_state = 'failed' WHERE id = %s", (internal,))
        expected = _bot_body(offering, bot, username, registered_at=iso(t0), state="no_access",
                             profile_state="failed", problem="token_rejected")
        lines = [f"@{username}  no_access  perfil: failed  posse: nenhuma  última consulta: -",
                 f"Problema: {PROBLEM_MESSAGES['token_rejected']}"]
    else:
        sql.execute("UPDATE bots SET last_problem = 'rate_limited', last_problem_at = %s WHERE id = %s",
                    (t0 + timedelta(seconds=3), internal))
        expected = _bot_body(offering, bot, username, registered_at=iso(t0), profile_state="configured",
                             lease=lease, last_poll_ok_at=iso(polled), problem="rate_limited")
        lines = [f"@{username}  registered  perfil: configured  {lease_line}",
                 f"Problema: {PROBLEM_MESSAGES['rate_limited']}"]

    assert _get_bot(client, offering["code"]) == expected
    for ref in (offering["code"], offering["offering_id"]):
        result = cli_invoke(["bots", "show", ref])
        _ok(result)
        assert result.stdout == _out(*lines)
        assert find_secrets(result.stdout, _token_forms(token)) == []


def test_bots_show_json(cli_invoke: Any, client: Any, clock: Any, bot_api: Any, sql: psycopg.Connection) -> None:
    """Contrato (`--json`: o corpo da API): `cq --json bots show REF` imprime
    exatamente o corpo de `bots.get`, com posse, última consulta e problema."""
    offering = create_offering(client, ADMIN_TOKEN)
    t0 = clock.now()
    bot, token, username = _new_bot(bot_api)
    register_bot(client, ADMIN_TOKEN, offering["code"], token)
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    until = t0 + timedelta(seconds=30)
    polled = t0 + timedelta(seconds=2)
    sql.execute("UPDATE bots SET lease_owner = 'worker-cli-json', lease_until = %s, last_poll_ok_at = %s, "
                "last_problem = 'rate_limited', last_problem_at = %s WHERE telegram_bot_id = %s",
                (until, polled, polled, bot))
    expected = _bot_body(offering, bot, username, registered_at=iso(t0),
                         lease={"worker_id": "worker-cli-json", "until": iso(until)},
                         last_poll_ok_at=iso(polled), problem="rate_limited")
    assert _get_bot(client, offering["code"]) == expected
    result = cli_invoke(["bots", "show", offering["code"]], json=True)
    _ok(result)
    assert json.loads(result.stdout) == expected
    assert find_secrets(result.stdout, _token_forms(token)) == []


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_bots_show_not_found(json_mode: bool, cli_invoke: Any, client: Any) -> None:
    """404 se o oferecimento não existe **ou** não tem bot; erros na
    forma comum do CLI: código 1, saída padrão vazia e `Erro: Recurso não encontrado.` (ou o envelope, com
    `--json`), nos dois casos."""
    without_bot = create_offering(client, ADMIN_TOKEN)
    for ref in ("oferta-inexistente", without_bot["code"]):
        result = cli_invoke(["bots", "show", ref], json=json_mode)
        _no_traceback(result)
        assert result.exit_code == 1, ref
        assert result.stdout == ""
        if json_mode:
            assert json.loads(result.stderr) == _error_envelope("not_found")
        else:
            assert result.stderr == _out("Erro: Recurso não encontrado.")


# --------------------------------------------------------------------------------------------------
# `bots activate`: erros da API

ACTIVATE_ERROR_CASES = ["sem-bot", "bot-sem-acesso", "encerrado", "arquivado"]


def _get_offering(client: Any, ref: str) -> dict:
    resp = client.get(_path(ref, ""), headers=ADMIN)
    assert resp.status_code == 200, f"offerings.get: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("case", ACTIVATE_ERROR_CASES)
def test_bots_activate_errors(case: str, json_mode: bool, cli_invoke: Any, client: Any, bot_api: Any,
                              sql: psycopg.Connection) -> None:
    """A ativação exige um bot registrado e com acesso (ativação,
    nesta ordem: `closed`/`archived` → 409 `invalid_state`; … `draft` sem bot ou com bot `no_access` →
    409 `bot_required`); mensagens exatas e erros (na forma comum do CLI; no 409
    `invalid_state`, depois de `Erro: <message>`, a linha `Estado atual: <state>`): `cq bots activate`
    num rascunho sem bot e num rascunho com o bot sem acesso → `Erro: O oferecimento não tem um bot
    registrado com acesso.`; num oferecimento encerrado e num arquivado → `Erro: A operação não é
    permitida no estado atual do oferecimento.` e `Estado atual: closed`/`archived`; com `--json`, o
    envelope exato da API no stderr. Em todos, código 1, saída padrão vazia, um único pedido `POST
    /v1/offerings/<code>/bot/activate`, o oferecimento sem mudança e nenhuma chamada nova à Bot API
    falsa."""
    offering = create_offering(client, ADMIN_TOKEN)
    ref = offering["code"]
    summary = None
    if case != "sem-bot":
        bot, token, username = _new_bot(bot_api)
        register_bot(client, ADMIN_TOKEN, ref, token)
        without_access = case == "bot-sem-acesso"
        summary = {"username": username, "telegram_bot_id": bot,
                   "state": "no_access" if without_access else "registered",
                   "profile_state": "failed" if without_access else "pending"}
    if case == "sem-bot":
        error = _error_envelope("bot_required")
        text = ["Erro: O oferecimento não tem um bot registrado com acesso."]
    elif case == "bot-sem-acesso":
        sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected', "
                    "profile_state = 'failed' WHERE id = %s", (_internal_bot_id(sql, bot),))
        error = _error_envelope("bot_required")
        text = ["Erro: O oferecimento não tem um bot registrado com acesso."]
    elif case == "encerrado":
        activate_bot(client, ADMIN_TOKEN, ref)
        closed = _post_ok(client, "offerings.close", _path(ref, "/close"))
        assert closed["state"] == "closed"
        error = _error_envelope("invalid_state", state="closed")
        text = ["Erro: A operação não é permitida no estado atual do oferecimento.", "Estado atual: closed"]
    else:
        _archive(client, ref)
        error = _error_envelope("invalid_state", state="archived")
        text = ["Erro: A operação não é permitida no estado atual do oferecimento.", "Estado atual: archived"]
    before = _get_offering(client, ref)
    assert before["state"] == {"sem-bot": "draft", "bot-sem-acesso": "draft", "encerrado": "closed",
                               "arquivado": "archived"}[case]
    assert before["bot"] == summary
    telegram_calls = len(bot_api.calls())

    with _recorded_requests(client) as seen:
        result = cli_invoke(["bots", "activate", ref], json=json_mode)

    _no_traceback(result)
    assert result.exit_code == 1, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""
    if json_mode:
        assert json.loads(result.stderr) == error
    else:
        assert result.stderr == _out(*text)
    assert [(r.method, str(r.url)) for r in seen] == [
        ("POST", f"http://testserver/v1/offerings/{ref}/bot/activate")]
    assert _get_offering(client, ref) == before
    assert len(bot_api.calls()) == telegram_calls


# --------------------------------------------------------------------------------------------------
# O token nunca sai


def test_bot_token_never_in_cli_outputs(cli_invoke: Any, client: Any, bot_api: Any,
                                        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Saídas do CLI sem segredo: registro pelo prompt, troca pelo arquivo,
    registro recusado, `bots show` e erro de uso, com e sem `--json`; nenhuma saída (stdout e stderr)
    contém um token (inteiro ou a parte secreta) nem outro segredo conhecido. Controle positivo: o
    detector acha o token num texto que o contém. Cada saída também é conferida por valor (texto
    exato, ou o corpo da API com `--json`)."""
    offering = create_offering(client, ADMIN_TOKEN)
    code = offering["code"]
    bot, token, username = _new_bot(bot_api)
    refused = "1234567:" + "Ab0_-Zy9" * 5
    remember_secret(refused)
    registered_line = _out(f"Bot registrado: @{username} no oferecimento {code} (perfil: pending).")
    show_line = _out(f"@{username}  registered  perfil: pending  posse: nenhuma  última consulta: -")
    refused_text = _out("Erro: Pedido inválido.", "  - token: invalid_value")
    refused_json = _error_envelope("invalid_request", fields=[{"field": "token", "reason": "invalid_value"}])
    usage = _out("Uso incorreto: opção desconhecida.")
    results: list[Any] = []

    def check(result: Any, code_expected: int, stdout: str | dict, stderr: str | dict) -> None:
        _no_traceback(result)
        assert result.exit_code == code_expected, (result.exit_code, result.stderr)
        assert (json.loads(result.stdout) if isinstance(stdout, dict) else result.stdout) == stdout
        assert (json.loads(result.stderr) if isinstance(stderr, dict) else result.stderr) == stderr
        results.append(result)

    for json_mode in (False, True):
        _patch_input(monkeypatch, terminal=True, prompt=bot_api.token_of(bot))
        result = cli_invoke(["bots", "register", code], json=json_mode)
        check(result, 0, _get_bot(client, code) if json_mode else registered_line, "")
        _patch_input(monkeypatch, terminal=False)
        rotated = bot_api.rotate_token(bot)
        path = _token_file(tmp_path, rotated + "\n", name=f"token-{int(json_mode)}.txt")
        result = cli_invoke(["bots", "register", code, "--token-file", str(path)], json=json_mode)
        check(result, 0, _get_bot(client, code) if json_mode else registered_line, "")
        bad = _token_file(tmp_path, refused, name=f"recusado-{int(json_mode)}.txt")
        result = cli_invoke(["bots", "register", code, "--token-file", str(bad)], json=json_mode)
        check(result, 1, "", refused_json if json_mode else refused_text)
        result = cli_invoke(["bots", "show", code], json=json_mode)
        check(result, 0, _get_bot(client, code) if json_mode else show_line, "")
        result = cli_invoke(["bots", "register", code, "--token", rotated], json=json_mode)
        check(result, 2, "", usage)
    assert len(results) == 10
    secrets = _token_forms(*bot_api.all_tokens(), refused) + all_secrets()
    assert find_secrets(f"saída com {token}", secrets) != [], "controle positivo do detector"
    for result in results:
        _no_traceback(result)
        assert find_secrets(result.stdout + "\n" + result.stderr, secrets) == []
