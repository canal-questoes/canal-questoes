"""Perfis nomeados pelo CLI
(o contrato dos perfis e dos comandos que os usam).

O `cq` roda em processo pelo `invoke_cli` (tests/helpers/cli_runner.py), com o ambiente limpo e o arquivo de perfil de
teste (`write_profile`) por `CQ_CONFIG`; a API dos testes chega por `http=` no `ctx.obj` (o `TestClient` atende
qualquer host), e os pedidos são observados pelo gancho `event_hooks["request"]`, que mostra o endereço e o token
de fato usados. O topo de todo arquivo de perfil aponta para a API inalcançável (`http://127.0.0.1:9`) com um token
que a API recusa: escolher o perfil errado nunca dá certo por acaso.

Os casos de `status` e `doctor` aqui usam perfis **remotos** (só a parte da API); os perfis locais e a pilha estão em
`tests/cli/test_cli_stack.py`.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

import pytest

import cq_server
from cq_server.migrate import EXPECTED_VERSION

from tests.helpers.api import ADMIN_TOKEN, auth, register_person
from tests.helpers.cli_runner import UNREACHABLE_API_URL, invoke_cli, write_profile
from tests.helpers.contract import assert_cli_json
from tests.helpers.stack_fake import TEST_API_PORT, TEST_API_URL

TOP_TOKEN = "cq_tokendotoporecusado_" + "t" * 24
OTHER_TOKEN = "cq_tokendooutrorecusado_" + "o" * 24
CANARY_NAME = "perfil-canario-Jm4Ux9"
CANARY_PASSWORD = "senha-sentinela-Pz6Wq3"
VM_URL = "https://vm.cq.test"
OUTRO_URL = "https://outro.cq.test"
ENV_URL = "https://api.ambiente.cq.test"

UNKNOWN_PROFILE = "Uso incorreto: o perfil escolhido não existe.\n"
WARNING_VM = 'Aviso: o perfil "vm" usa http:// fora do loopback; o token vai sem cifragem.\n'

PROFILE_FILE = f"""\
api_url = "{UNREACHABLE_API_URL}"
token = "{TOP_TOKEN}"
{{default}}
[profiles.vm]
kind = "remote"
api_url = "{VM_URL}"
token = "{ADMIN_TOKEN}"

[profiles.outro]
api_url = "{OUTRO_URL}"

[profiles.recusado]
kind = "remote"
api_url = "{OUTRO_URL}"
token = "{OTHER_TOKEN}"
"""


def _file(tmp_path: Any, default: str | None = None, extra: str = "") -> Any:
    line = f'default_profile = "{default}"\n' if default else ""
    return write_profile(tmp_path, PROFILE_FILE.replace("{default}", line) + extra)


@contextlib.contextmanager
def _recorded(http: Any):
    seen: list[Any] = []
    hooks = http.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _targets(seen: list[Any]) -> list[tuple[str, str, int | None, str | None]]:
    """(esquema, host, porta, Authorization) de cada pedido."""
    return [(r.url.scheme, r.url.host, r.url.port, r.headers.get("Authorization")) for r in seen]


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _people(client: Any) -> dict:
    resp = client.get("/v1/people", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200
    return resp.json()


def _health(client: Any) -> dict:
    resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json()


def _api_line() -> str:
    return f"API: saudável, versão {cq_server.__version__}, esquema {EXPECTED_VERSION} (esperado {EXPECTED_VERSION})."


# --------------------------------------------------------------------------------------------------
# Escolha do perfil: opção, variável, default_profile


@pytest.mark.parametrize(
    ("args", "env", "default"),
    [(["--profile", "vm"], {}, None),
     ([], {"CQ_PROFILE": "vm"}, None),
     ([], {}, "vm"),
     (["--profile", "vm"], {"CQ_PROFILE": "recusado"}, "recusado"),
     ([], {"CQ_PROFILE": "vm"}, "recusado")],
    ids=["opcao", "variavel", "default_profile", "opcao_vence_variavel_e_arquivo", "variavel_vence_arquivo"])
def test_profile_selection_chooses_url_and_token(tmp_path, client, args, env, default) -> None:
    """`--profile` (antes do subcomando) > `CQ_PROFILE` > `default_profile`
    escolhem o endereço e o token usados na chamada de uma folha do catálogo; a saída é a da API."""
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    config = _file(tmp_path, default)
    with _recorded(client) as seen:
        result = invoke_cli([*args, "people", "list"], json=True, obj={"http": client}, env=env, config=config)
    _no_traceback(result)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == _people(client)
    assert result.stderr == ""
    assert _targets(seen) == [("https", "vm.cq.test", None, f"Bearer {ADMIN_TOKEN}")]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("how", ["option", "env", "default_profile"])
def test_unknown_profile_is_usage_error_without_echo(tmp_path, client, how, json_mode) -> None:
    """o perfil escolhido que não existe → 2, `Uso incorreto:` com o motivo fixo, sem
    eco do nome, stdout vazio e nenhum pedido à API."""
    config = _file(tmp_path, CANARY_NAME if how == "default_profile" else None)
    args = ["--profile", CANARY_NAME] if how == "option" else []
    env = {"CQ_PROFILE": CANARY_NAME} if how == "env" else {}
    with _recorded(client) as seen:
        result = invoke_cli([*args, "people", "list"], json=json_mode, obj={"http": client}, env=env, config=config)
    _no_traceback(result)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == UNKNOWN_PROFILE
    assert CANARY_NAME not in result.stderr
    assert seen == []


@pytest.mark.parametrize(
    ("profile_text", "reason"),
    [(f'[profiles.p]\nkind = "remote"\napi_url = "{VM_URL}"\nsenha = "{CANARY_PASSWORD}"\n',
      "o perfil tem uma chave desconhecida."),
     (f'[profiles.p]\nkind = "docker"\napi_url = "{VM_URL}"\n', "o tipo do perfil deve ser local ou remote."),
     (f'[profiles.p]\nkind = "local"\napi_url = "{TEST_API_URL}"\ncompose_project = "cq-demo"\n',
      "o nome do projeto Compose do perfil é inválido ou reservado."),
     (f'[profiles.p]\nkind = "remote"\napi_url = "{VM_URL}"\nstack_dir = "C:/{CANARY_PASSWORD}"\n',
      "stack_dir e compose_project só valem no perfil local.")],
    ids=["chave_desconhecida", "tipo_invalido", "projeto_reservado", "stack_dir_no_remoto"])
def test_invalid_named_profile_is_usage_error(tmp_path, client, profile_text, reason) -> None:
    """Os erros dos perfis nomeados → 2, `Uso incorreto:` com o motivo fixo, sem eco do conteúdo,
    stdout vazio e nenhum pedido."""
    config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n' + profile_text)
    with _recorded(client) as seen:
        result = invoke_cli(["--profile", "p", "people", "list"], obj={"http": client}, config=config)
    _no_traceback(result)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == f"Uso incorreto: {reason}\n"
    assert CANARY_PASSWORD not in result.stderr
    assert seen == []


# --------------------------------------------------------------------------------------------------
# O aviso de http://


def test_insecure_transport_warning_only_on_stderr(tmp_path, client) -> None:
    """o perfil nomeado remoto com `http://` fora do loopback dá o aviso fixo
    no stderr, sem mudar o stdout nem o código; com `https://`, com um `remote` declarado em
    `http://127.0.0.1:…` (túnel) e no implícito com `http://` remoto, nenhum aviso."""
    register_person(client, ADMIN_TOKEN, name="Bento Fictício Lage")
    extra = f"""
[profiles.vmhttp]
kind = "remote"
api_url = "http://vm.cq.test"
token = "{ADMIN_TOKEN}"

[profiles.tunel]
kind = "remote"
api_url = "http://127.0.0.1:{TEST_API_PORT}"
token = "{ADMIN_TOKEN}"
"""
    config = _file(tmp_path, extra=extra)
    baseline = invoke_cli(["--profile", "vm", "people", "list"], obj={"http": client}, config=config)
    assert (baseline.exit_code, baseline.stderr) == (0, "")
    with _recorded(client) as seen:
        warned = invoke_cli(["--profile", "vmhttp", "people", "list"], obj={"http": client}, config=config)
    _no_traceback(warned)
    assert warned.exit_code == 0
    assert warned.stdout == baseline.stdout
    assert warned.stderr == WARNING_VM.replace('"vm"', '"vmhttp"')
    assert _targets(seen) == [("http", "vm.cq.test", None, f"Bearer {ADMIN_TOKEN}")]
    warned_json = invoke_cli(["--profile", "vmhttp", "people", "list"], json=True, obj={"http": client},
                            config=config)
    assert warned_json.exit_code == 0
    assert json.loads(warned_json.stdout) == _people(client)
    tunnel = invoke_cli(["--profile", "tunel", "people", "list"], obj={"http": client}, config=config)
    assert (tunnel.exit_code, tunnel.stdout, tunnel.stderr) == (0, baseline.stdout, "")
    implicit = write_profile(tmp_path, f'api_url = "http://api.remota.cq.test"\ntoken = "{ADMIN_TOKEN}"\n',
                             name="implicito.toml")
    plain = invoke_cli(["people", "list"], obj={"http": client}, config=implicit)
    assert (plain.exit_code, plain.stdout, plain.stderr) == (0, baseline.stdout, "")
    # `--profile` com um perfil http remoto: o nome sai no aviso, a senha de um `user:senha@` não.
    secret = write_profile(tmp_path, f'[profiles.vm]\nkind = "remote"\n'
                                     f'api_url = "http://usuario:{CANARY_PASSWORD}@vm.cq.test"\n'
                                     f'token = "{ADMIN_TOKEN}"\n', name="com-senha.toml")
    with_password = invoke_cli(["--profile", "vm", "people", "list"], obj={"http": client}, config=secret)
    assert with_password.exit_code == 0
    assert with_password.stderr == WARNING_VM
    assert CANARY_PASSWORD not in with_password.stderr + with_password.stdout


# --------------------------------------------------------------------------------------------------
# `status` e `doctor` com as linhas Perfil: e Origem: (perfis remotos)


def _view(name: str | None, url: str, token_set: bool, origins: dict) -> dict:
    return {"name": name, "kind": "remote", "api_url": url, "token_set": token_set, "compose_project": None,
            "origins": {**origins, "compose_project": None}}


_STATUS_CASES = {
    "opcao": (["--profile", "vm"], {}, None,
              "Perfil: vm (remoto), API em https://vm.cq.test, token definido.",
              "Origem: nome opção --profile, tipo declarado, endereço perfil, token perfil.",
              _view("vm", VM_URL, True, {"name": "option", "kind": "profile", "api_url": "profile",
                                         "token": "profile"})),
    "variavel_tipo_inferido_token_do_ambiente": (
        [], {"CQ_PROFILE": "outro", "CQ_TOKEN": ADMIN_TOKEN}, None,
        "Perfil: outro (remoto), API em https://outro.cq.test, token definido.",
        "Origem: nome CQ_PROFILE, tipo inferido do endereço, endereço perfil, token CQ_TOKEN.",
        _view("outro", OUTRO_URL, True, {"name": "env", "kind": "inferred", "api_url": "profile", "token": "env"})),
    "default_profile_endereco_do_ambiente": (
        [], {"CQ_API_URL": ENV_URL}, "vm",
        "Perfil: vm (remoto), API em https://api.ambiente.cq.test, token definido.",
        "Origem: nome default_profile, tipo declarado, endereço CQ_API_URL, token perfil.",
        _view("vm", ENV_URL, True, {"name": "file", "kind": "profile", "api_url": "env", "token": "profile"})),
    "sem_token": (
        ["--profile", "outro"], {}, None,
        "Perfil: outro (remoto), API em https://outro.cq.test, sem token.",
        "Origem: nome opção --profile, tipo inferido do endereço, endereço perfil, token ausente.",
        _view("outro", OUTRO_URL, False, {"name": "option", "kind": "inferred", "api_url": "profile",
                                          "token": "default"})),
}


@pytest.mark.parametrize("case", list(_STATUS_CASES))
def test_status_shows_profile_and_origin(tmp_path, client, case) -> None:
    """com perfil remoto, o `status` mostra só o perfil e a API: as linhas
    `Perfil:` e `Origem:` com os rótulos fixos e a linha `API:`, sem o token; no `--json`, o `ProfileView` exato,
    `services = null`, sem avisos (validado contra `status.output`)."""
    args, env, default, perfil, origem, view = _STATUS_CASES[case]
    config = _file(tmp_path, default)
    text = invoke_cli([*args, "status"], obj={"http": client}, env=env, config=config)
    _no_traceback(text)
    assert text.exit_code == 0, text.stderr
    assert text.stdout == f"{perfil}\n{origem}\n{_api_line()}\n"
    as_json = invoke_cli([*args, "status"], json=True, obj={"http": client}, env=env, config=config)
    _no_traceback(as_json)
    assert as_json.exit_code == 0, as_json.stderr
    body = assert_cli_json("status", as_json.stdout)
    assert body == {"profile": view, "services": None, "api_port": None,
                    "api": {"reachable": True, "health": _health(client), "owned_by_stack": None}, "warnings": []}
    for result in (text, as_json):
        assert ADMIN_TOKEN not in result.stdout + result.stderr
        assert TOP_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("case", list(_STATUS_CASES))
def test_doctor_shows_profile_and_origin(tmp_path, client, case) -> None:
    """o `doctor` começa pelas mesmas linhas `Perfil:` e `Origem:` do
    `status`, sem o token; no `--json`, o `profile` é o mesmo `ProfileView` (validado contra `doctor.output`).
    Sem token, `api.access` falha (código 1); com o token, nenhuma falha (código 0)."""
    args, env, default, perfil, origem, view = _STATUS_CASES[case]
    config = _file(tmp_path, default)
    expected_code = 0 if view["token_set"] else 1
    text = invoke_cli([*args, "doctor"], obj={"http": client}, env=env, config=config)
    _no_traceback(text)
    assert text.exit_code == expected_code, text.stderr
    assert text.stdout.splitlines()[:2] == [perfil, origem]
    as_json = invoke_cli([*args, "doctor"], json=True, obj={"http": client}, env=env, config=config)
    _no_traceback(as_json)
    assert as_json.exit_code == expected_code, as_json.stderr
    body = assert_cli_json("doctor", as_json.stdout)
    assert body["profile"] == view
    for result in (text, as_json):
        assert ADMIN_TOKEN not in result.stdout + result.stderr


def test_credentials_in_api_url_never_shown(tmp_path, client) -> None:
    """com `api_url = "https://usuario:senha@host"` no perfil, a senha (e o
    usuário) ficam fora do texto e do `--json` de `status` e `doctor`; o endereço sai como `https://host`."""
    config = write_profile(tmp_path, f'[profiles.vm]\nkind = "remote"\n'
                                     f'api_url = "https://usuario:{CANARY_PASSWORD}@vm.cq.test"\n'
                                     f'token = "{ADMIN_TOKEN}"\n')
    view = _view("vm", VM_URL, True, {"name": "option", "kind": "profile", "api_url": "profile", "token": "profile"})
    for command in ("status", "doctor"):
        text = invoke_cli(["--profile", "vm", command], obj={"http": client}, config=config)
        _no_traceback(text)
        assert text.stdout.splitlines()[:1] == ["Perfil: vm (remoto), API em https://vm.cq.test, token definido."]
        as_json = invoke_cli(["--profile", "vm", command], json=True, obj={"http": client}, config=config)
        _no_traceback(as_json)
        assert assert_cli_json(command, as_json.stdout)["profile"] == view
        for result in (text, as_json):
            output = result.stdout + result.stderr
            assert CANARY_PASSWORD not in output and "usuario" not in output
            assert ADMIN_TOKEN not in output


# --------------------------------------------------------------------------------------------------
# Ajuda da raiz


def test_root_help_describes_profiles_without_token_option(tmp_path) -> None:
    """a ajuda da raiz descreve os perfis, `--profile`, as variáveis (`CQ_PROFILE`,
    `CQ_API_URL`, `CQ_TOKEN`, `CQ_CONFIG`, `CQ_STACK_DIR`) e os comandos locais e o `doctor`, sem citar a opção de
    token."""
    result = invoke_cli(["--help"], tmp_path=tmp_path)
    _no_traceback(result)
    assert result.exit_code == 0
    help_text = result.stdout
    for needle in ("--profile", "CQ_PROFILE", "CQ_API_URL", "CQ_TOKEN", "CQ_CONFIG", "CQ_STACK_DIR", "perfi"):
        assert needle in help_text, needle
    for command in ("up", "down", "status", "logs", "doctor"):
        assert re.search(rf"^\s+{command}\b", help_text, re.MULTILINE), command
    assert "--token" not in help_text


# --------------------------------------------------------------------------------------------------
# Roteiro de perfis e o perfil local sem a pilha


def test_profiles_walkthrough(tmp_path, client) -> None:
    """Roteiro de perfis: o perfil local com o token configurado (o padrão do arquivo) e um perfil remoto
    apontando para a mesma API; a mesma folha do catálogo por um e pelo outro dá a mesma saída, cada uma no seu
    endereço; `cq --profile vm status` mostra só a parte da API (código 0)."""
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    config = write_profile(tmp_path, f"""\
default_profile = "local"

[profiles.local]
kind = "local"
api_url = "{TEST_API_URL}"
token = "{ADMIN_TOKEN}"

[profiles.vm]
kind = "remote"
api_url = "{VM_URL}"
token = "{ADMIN_TOKEN}"
""")
    with _recorded(client) as seen:
        local = invoke_cli(["people", "list"], json=True, obj={"http": client}, config=config)
        remote = invoke_cli(["--profile", "vm", "people", "list"], json=True, obj={"http": client}, config=config)
    assert (local.exit_code, remote.exit_code) == (0, 0), local.stderr + remote.stderr
    assert json.loads(local.stdout) == json.loads(remote.stdout) == _people(client)
    assert _targets(seen) == [("http", "127.0.0.1", TEST_API_PORT, f"Bearer {ADMIN_TOKEN}"),
                              ("https", "vm.cq.test", None, f"Bearer {ADMIN_TOKEN}")]
    status = invoke_cli(["--profile", "vm", "status"], json=True, obj={"http": client}, config=config)
    assert status.exit_code == 0, status.stderr
    body = assert_cli_json("status", status.stdout)
    assert body["services"] is None and body["api_port"] is None
    assert body["profile"]["name"] == "vm" and body["api"]["reachable"] is True


def test_catalog_leaf_with_local_profile_whose_stack_dir_is_missing(tmp_path, client) -> None:
    """Uma folha do catálogo com um perfil nomeado `kind = "local"`
    cujo `stack_dir` não existe funciona (código 0) contra a API dos testes; só `up`, `down`, `status` e `logs`
    recusam o diretório inexistente."""
    missing = tmp_path / "pilha-que-nao-existe"
    config = write_profile(tmp_path, f"""\
api_url = "{UNREACHABLE_API_URL}"
token = "{TOP_TOKEN}"

[profiles.local]
kind = "local"
api_url = "{TEST_API_URL}"
token = "{ADMIN_TOKEN}"
stack_dir = "{missing.as_posix()}"
""")
    with _recorded(client) as seen:
        result = invoke_cli(["people", "list"], json=True, obj={"http": client}, env={"CQ_PROFILE": "local"},
                           config=config)
    _no_traceback(result)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == _people(client)
    assert result.stderr == ""
    assert _targets(seen) == [("http", "127.0.0.1", TEST_API_PORT, f"Bearer {ADMIN_TOKEN}")]
    assert not missing.exists()
