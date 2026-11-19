"""Resolução dos perfis nomeados do CLI.


`resolve_profile(env, selector=None)` devolve o `EffectiveProfile`. O perfil implícito (sem nome escolhido)
é exatamente o `load_profile(env)` original (`tests/unit/test_profile.py` tem todos os casos
repetidos aqui por `resolve_profile(env).as_profile()`); os perfis nomeados ficam em `profiles.<nome>`, escolhidos
por `--profile` (o `selector`) > `CQ_PROFILE` > `default_profile` > o implícito.

As asserções sobre o que o esqueleto já tem (os tipos, as mensagens fixas dos erros, o `repr` seguro e o
`display_url` do `EffectiveProfile`) ficam na mesma função de um caso de `resolve_profile`.
"""

from __future__ import annotations

import pathlib

import click
import pytest

from cq_client.profile import (
    DEFAULT_PROJECT,
    INSECURE_TRANSPORT_TEMPLATE,
    EffectiveProfile,
    InvalidApiUrl,
    InvalidComposeProject,
    InvalidProfileField,
    InvalidProfileFile,
    InvalidProfileKind,
    InvalidToken,
    Profile,
    ProfileError,
    UnknownProfile,
    UnknownProfileKey,
    UnreadableProfileFile,
    default_config_path,
    insecure_transport_warning,
    load_profile,
    resolve_profile,
)

DEFAULT_URL = "http://127.0.0.1:8000"
FILE_URL = "https://api.arquivo.example:8443"
FILE_TOKEN = "tok-do-arquivo-Q7x2Lm9"
ENV_URL = "http://api.ambiente.example:9000"
ENV_TOKEN = "tok-do-ambiente-Zp4Wc8"

# Endereços dos perfis nomeados (nenhum é chamado: a resolução não faz rede).
LOCAL_URL = "http://127.0.0.1:18765"
VM_URL = "https://vm.cq.test"
VM_TOKEN = "tok-da-vm-Hc5Rt2"
LOCAL_TOKEN = "tok-local-Pw8Nd3"
OUTRO_URL = "https://outro.cq.test"
OUTRO_TOKEN = "tok-outro-Lm4Xs6"

# Canários: nenhum erro pode ecoar o nome digitado, o conteúdo do arquivo, credenciais nem o token.
CANARY_NAME = "perfil-canario-Kq3Vz8"
CANARY_TOKEN = "cq_tokcanario-Wt6Yb1-" + "k" * 20
CANARY_PASSWORD = "senha-sentinela-Rm2Gh7"

# Os motivos fixos (os dos perfis nomeados e os do `load_profile`).
REASONS = {
    UnknownProfile: "o perfil escolhido não existe.",
    UnknownProfileKey: "o perfil tem uma chave desconhecida.",
    InvalidProfileField: "stack_dir e compose_project só valem no perfil local.",
    InvalidProfileKind: "o tipo do perfil deve ser local ou remote.",
    InvalidComposeProject: "o nome do projeto Compose do perfil é inválido ou reservado.",
    InvalidProfileFile: "o arquivo de perfil não é um TOML válido.",
    UnreadableProfileFile: "não foi possível ler o arquivo de perfil.",
    InvalidApiUrl: "o endereço da API é inválido.",
    InvalidToken: "o token do perfil é inválido.",
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Como em `tests/unit/test_profile.py`: o diretório padrão do perfil em `tmp_path` e nenhuma `CQ_*` no
    ambiente do processo (a resolução só lê o `env` recebido)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("CQ_API_URL", "CQ_TOKEN", "CQ_CONFIG", "CQ_PROFILE", "CQ_STACK_DIR"):
        monkeypatch.delenv(var, raising=False)


def _expected_default_path() -> pathlib.Path:
    return pathlib.Path(click.get_app_dir("cq")) / "config.toml"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _top(*, api_url: str | None = FILE_URL, token: str | None = FILE_TOKEN) -> str:
    lines = []
    if api_url is not None:
        lines.append(f'api_url = "{api_url}"')
    if token is not None:
        lines.append(f'token = "{token}"')
    return "\n".join(lines) + "\n"


def _env(path: pathlib.Path, **extra: str) -> dict[str, str]:
    return {"CQ_CONFIG": str(path), **extra}


# Arquivo com o implícito no topo e três perfis nomeados.
NAMED_FILE = f"""\
api_url = "{FILE_URL}"
token = "{FILE_TOKEN}"

[profiles.local]
kind = "local"
api_url = "{LOCAL_URL}"
token = "{LOCAL_TOKEN}"
compose_project = "cq-local"

[profiles.vm]
kind = "remote"
api_url = "{VM_URL}"
token = "{VM_TOKEN}"

[profiles.outro]
api_url = "{OUTRO_URL}"
token = "{OUTRO_TOKEN}"
"""


def _assert_no_echo(exc: BaseException, *needles: str) -> None:
    """O erro tem só o motivo fixo: nada de `needles` em `str`, `repr` nem `args`."""
    texts = [str(exc), repr(exc), *(str(a) for a in exc.args)]
    for needle in needles:
        assert all(needle not in text for text in texts), f"o erro ecoou {needle!r}: {texts}"


# --------------------------------------------------------------------------------------------------
# O implícito é o `load_profile` original (todos os casos de tests/unit/test_profile.py)


# (arquivo: "both" | "url" | "token" | None = ausente, env extra, Profile esperado, origem do endereço,
#  origem do token)
_IMPLICIT_CASES = [
    (None, {}, Profile(DEFAULT_URL, None), "default", "default"),
    ("both", {}, Profile(FILE_URL, FILE_TOKEN), "file", "file"),
    ("both", {"CQ_API_URL": ENV_URL}, Profile(ENV_URL, FILE_TOKEN), "env", "file"),
    ("both", {"CQ_TOKEN": ENV_TOKEN}, Profile(FILE_URL, ENV_TOKEN), "file", "env"),
    ("both", {"CQ_API_URL": ENV_URL, "CQ_TOKEN": ENV_TOKEN}, Profile(ENV_URL, ENV_TOKEN), "env", "env"),
    ("token", {}, Profile(DEFAULT_URL, FILE_TOKEN), "default", "file"),
    ("url", {}, Profile(FILE_URL, None), "file", "default"),
    ("token", {"CQ_API_URL": ENV_URL}, Profile(ENV_URL, FILE_TOKEN), "env", "file"),
    ("url", {"CQ_TOKEN": ENV_TOKEN}, Profile(FILE_URL, ENV_TOKEN), "file", "env"),
    (None, {"CQ_TOKEN": ENV_TOKEN}, Profile(DEFAULT_URL, ENV_TOKEN), "default", "env"),
    (None, {"CQ_API_URL": ENV_URL}, Profile(ENV_URL, None), "env", "default"),
    ("both", {"CQ_API_URL": "", "CQ_TOKEN": ""}, Profile(FILE_URL, FILE_TOKEN), "file", "file"),
]


@pytest.mark.parametrize(("file_kind", "env_extra", "expected", "url_origin", "token_origin"), _IMPLICIT_CASES,
                         ids=["nada", "so_arquivo", "url_do_ambiente", "token_do_ambiente", "os_dois_do_ambiente",
                              "arquivo_so_token", "arquivo_so_url", "arquivo_so_token_url_do_ambiente",
                              "arquivo_so_url_token_do_ambiente", "sem_arquivo_token_do_ambiente",
                              "sem_arquivo_url_do_ambiente", "variaveis_vazias"])
def test_implicit_profile_equals_load_profile(tmp_path, file_kind, env_extra, expected, url_origin,
                                              token_origin) -> None:
    """Passo 3 da resolução: sem nome escolhido, `resolve_profile(env).as_profile()` é
    exatamente `load_profile(env)`, campo a campo, com `CQ_API_URL`/`CQ_TOKEN` (vazias = ausentes) vencendo o
    topo do arquivo; o perfil é o implícito (`name = None`, `named = False`), sem `stack_dir`, com o projeto
    `cq`, o tipo inferido do host e as origens esperadas."""
    path = tmp_path / "cq.toml"
    if file_kind == "both":
        _write(path, _top())
    elif file_kind == "url":
        _write(path, _top(token=None))
    elif file_kind == "token":
        _write(path, _top(api_url=None))
    env = _env(path, **env_extra)
    profile = resolve_profile(env)
    assert isinstance(profile, EffectiveProfile)
    assert profile.as_profile() == load_profile(env) == expected
    assert profile.name is None
    assert profile.named is False
    assert profile.api_url == expected.api_url
    assert profile.token == expected.token
    assert profile.stack_dir is None
    assert profile.compose_project == DEFAULT_PROJECT == "cq"
    assert profile.kind == ("local" if expected.api_url == DEFAULT_URL else "remote")
    assert profile.config_path == default_config_path(env) == path
    assert dict(profile.origins) == {"name": "default", "kind": "inferred", "api_url": url_origin,
                                     "token": token_origin, "compose_project": "default"}
    assert insecure_transport_warning(profile) is None
    # o `repr` real do esqueleto, sem o token.
    if expected.token is not None:
        assert expected.token not in repr(profile)
        assert "<definido>" in repr(profile)


def test_implicit_profile_from_default_path_and_process_environment_ignored(tmp_path, monkeypatch) -> None:
    """Caminho padrão: sem `CQ_CONFIG` (ou vazia), o arquivo é o de `click.get_app_dir("cq")`; o
    ambiente do **processo** nunca é a fonte (`CQ_*` só do `env` recebido), como no `load_profile`."""
    assert tmp_path in _expected_default_path().parents
    assert resolve_profile({}).as_profile() == load_profile({}) == Profile(DEFAULT_URL, None)
    _write(_expected_default_path(), _top())
    assert resolve_profile({}).as_profile() == load_profile({}) == Profile(FILE_URL, FILE_TOKEN)
    assert resolve_profile({"CQ_CONFIG": ""}).as_profile() == Profile(FILE_URL, FILE_TOKEN)
    assert resolve_profile({}).config_path == _expected_default_path()
    decoy = _write(tmp_path / "isca.toml", _top(api_url="http://isca.example:1", token="tok-isca-000")
                   + '\ndefault_profile = "isca"\n[profiles.isca]\napi_url = "http://isca.example:2"\n')
    monkeypatch.setenv("CQ_API_URL", "http://processo.example:2")
    monkeypatch.setenv("CQ_TOKEN", "tok-do-processo-111")
    monkeypatch.setenv("CQ_CONFIG", str(decoy))
    monkeypatch.setenv("CQ_PROFILE", "isca")
    profile = resolve_profile({})
    assert profile.as_profile() == load_profile({}) == Profile(FILE_URL, FILE_TOKEN)
    assert profile.name is None


@pytest.mark.parametrize(
    ("text", "error"),
    [("api_url = [\n", InvalidProfileFile),
     ('api_url = 3\n', InvalidProfileFile),
     ('token = true\n', InvalidProfileFile),
     ('api_url = "ftp://api.arquivo.example"\n', InvalidApiUrl),
     ('api_url = "http://"\n', InvalidApiUrl),
     (f'token = " {FILE_TOKEN}"\n', InvalidToken),
     (f'token = "{FILE_TOKEN} "\n', InvalidToken)],
    ids=["toml_invalido", "url_nao_texto", "token_nao_texto", "esquema_ftp", "sem_host",
         "token_espaco_inicial", "token_espaco_final"])
def test_implicit_profile_errors_equal_load_profile(tmp_path, text, error) -> None:
    """no implícito, os erros são os originais, com o mesmo tipo e o mesmo motivo fixo."""
    path = _write(tmp_path / "cq.toml", text)
    with pytest.raises(error) as resolved:
        resolve_profile(_env(path))
    with pytest.raises(error) as loaded:
        load_profile(_env(path))
    assert str(resolved.value) == str(loaded.value) == REASONS[error] == error.reason
    assert type(resolved.value) is type(loaded.value)


def test_unreadable_profile_file_equals_load_profile(tmp_path) -> None:
    """um diretório no lugar do arquivo dá o `UnreadableProfileFile` original."""
    path = tmp_path / "um-diretorio.toml"
    path.mkdir()
    with pytest.raises(UnreadableProfileFile) as exc:
        resolve_profile(_env(path))
    assert str(exc.value) == REASONS[UnreadableProfileFile]
    with pytest.raises(UnreadableProfileFile):
        load_profile(_env(path))


# --------------------------------------------------------------------------------------------------
# Precedência e perfis nomeados


@pytest.mark.parametrize(
    ("selector", "env_extra", "default_profile", "expected_name", "name_origin"),
    [("outro", {"CQ_PROFILE": "local"}, "vm", "outro", "option"),
     ("vm", {}, None, "vm", "option"),
     (None, {"CQ_PROFILE": "local"}, "vm", "local", "env"),
     (None, {"CQ_PROFILE": ""}, "vm", "vm", "file"),
     (None, {}, "outro", "outro", "file"),
     (None, {"CQ_PROFILE": ""}, None, None, "default")],
    ids=["opcao_vence_variavel_e_arquivo", "opcao_sozinha", "variavel_vence_arquivo", "variavel_vazia_e_ausente",
         "default_profile", "nenhum_seletor_implicito"])
def test_precedence_option_env_default_profile_implicit(tmp_path, selector, env_extra, default_profile,
                                                        expected_name, name_origin) -> None:
    """o nome escolhido é a opção `--profile` > `CQ_PROFILE` (vazia = ausente) >
    `default_profile` > o implícito; `origins["name"]` diz de onde veio."""
    text = NAMED_FILE if default_profile is None else f'default_profile = "{default_profile}"\n' + NAMED_FILE
    path = _write(tmp_path / "cq.toml", text)
    profile = resolve_profile(_env(path, **env_extra), selector)
    expected = {"local": (LOCAL_URL, LOCAL_TOKEN), "vm": (VM_URL, VM_TOKEN), "outro": (OUTRO_URL, OUTRO_TOKEN),
                None: (FILE_URL, FILE_TOKEN)}[expected_name]
    assert profile.name == expected_name
    assert profile.named is (expected_name is not None)
    assert (profile.api_url, profile.token) == expected
    assert profile.origins["name"] == name_origin
    if expected_name is not None:
        assert profile.origins["api_url"] == "profile"
        assert profile.origins["token"] == "profile"
    assert profile.as_profile() == Profile(*expected)


def test_named_profile_fields_and_origins(tmp_path) -> None:
    """o perfil nomeado `local` com tipo declarado, projeto próprio e `stack_dir`; o
    `vm` remoto declarado; o `outro` sem `kind`, inferido pelo host; as origens de cada campo."""
    stack = tmp_path / "pilha"
    text = NAMED_FILE.replace('compose_project = "cq-local"\n',
                              f'compose_project = "cq-local"\nstack_dir = "{stack.as_posix()}"\n')
    path = _write(tmp_path / "cq.toml", text)
    local = resolve_profile(_env(path), "local")
    assert (local.name, local.kind, local.api_url, local.token) == ("local", "local", LOCAL_URL, LOCAL_TOKEN)
    assert local.stack_dir == pathlib.Path(stack.as_posix())
    assert local.compose_project == "cq-local"
    assert local.named is True
    assert local.config_path == path
    assert dict(local.origins) == {"name": "option", "kind": "profile", "api_url": "profile", "token": "profile",
                                   "compose_project": "profile"}
    vm = resolve_profile(_env(path), "vm")
    assert (vm.kind, vm.stack_dir, vm.compose_project) == ("remote", None, DEFAULT_PROJECT)
    assert dict(vm.origins) == {"name": "option", "kind": "profile", "api_url": "profile", "token": "profile",
                                "compose_project": "default"}
    outro = resolve_profile(_env(path), "outro")
    assert outro.kind == "remote"
    assert outro.origins["kind"] == "inferred"
    # o `repr` e o `display_url` reais do esqueleto.
    assert LOCAL_TOKEN not in repr(local) and VM_TOKEN not in repr(vm)
    assert vm.display_url == VM_URL


def test_top_level_keys_never_complete_a_named_profile(tmp_path) -> None:
    """as chaves de topo (`api_url`, `token`) só formam o implícito; um perfil nomeado sem `token`
    fica sem token (e não herda o do topo), e sem `api_url` fica com o endereço padrão (origem `default`)."""
    text = _top() + f"""
[profiles.sem_token]
api_url = "{VM_URL}"

[profiles.so_token]
token = "{VM_TOKEN}"
"""
    path = _write(tmp_path / "cq.toml", text)
    sem_token = resolve_profile(_env(path), "sem_token")
    assert (sem_token.api_url, sem_token.token) == (VM_URL, None)
    assert sem_token.origins["token"] == "default"
    so_token = resolve_profile(_env(path), "so_token")
    assert (so_token.api_url, so_token.token) == (DEFAULT_URL, VM_TOKEN)
    assert so_token.origins["api_url"] == "default"
    assert so_token.kind == "local"
    assert FILE_TOKEN not in (sem_token.token, so_token.token)
    assert FILE_URL not in (sem_token.api_url, so_token.api_url)


def test_named_local_only_when_chosen(tmp_path) -> None:
    """um perfil nomeado `local` no arquivo não é usado sem ser escolhido (sem opção, sem
    `CQ_PROFILE` e sem `default_profile`, vale o implícito do topo)."""
    path = _write(tmp_path / "cq.toml", NAMED_FILE)
    profile = resolve_profile(_env(path))
    assert profile.name is None
    assert (profile.api_url, profile.token) == (FILE_URL, FILE_TOKEN)
    assert profile.as_profile() == load_profile(_env(path))
    assert profile.origins["name"] == "default"


def test_environment_overrides_named_profile_field_by_field(tmp_path) -> None:
    """`CQ_API_URL` e `CQ_TOKEN` não vazias vencem o campo correspondente do perfil nomeado,
    campo a campo, com a origem `env`; o nome e o projeto continuam os do perfil."""
    path = _write(tmp_path / "cq.toml", NAMED_FILE)
    only_url = resolve_profile(_env(path, CQ_API_URL=ENV_URL), "vm")
    assert (only_url.api_url, only_url.token) == (ENV_URL, VM_TOKEN)
    assert (only_url.origins["api_url"], only_url.origins["token"]) == ("env", "profile")
    only_token = resolve_profile(_env(path, CQ_TOKEN=ENV_TOKEN), "vm")
    assert (only_token.api_url, only_token.token) == (VM_URL, ENV_TOKEN)
    assert (only_token.origins["api_url"], only_token.origins["token"]) == ("profile", "env")
    both = resolve_profile(_env(path, CQ_API_URL=ENV_URL, CQ_TOKEN=ENV_TOKEN, CQ_PROFILE="vm"))
    assert (both.name, both.api_url, both.token) == ("vm", ENV_URL, ENV_TOKEN)
    assert both.origins["name"] == "env"
    empty = resolve_profile(_env(path, CQ_API_URL="", CQ_TOKEN=""), "local")
    assert (empty.api_url, empty.token, empty.compose_project) == (LOCAL_URL, LOCAL_TOKEN, "cq-local")


@pytest.mark.parametrize(
    ("api_url", "declared", "expected_kind", "kind_origin"),
    [("http://127.0.0.1:18765", None, "local", "inferred"),
     ("http://[::1]:18765", None, "local", "inferred"),
     ("http://localhost:18765", None, "local", "inferred"),
     ("https://cq.exemplo.br", None, "remote", "inferred"),
     ("http://10.0.0.5:8000", None, "remote", "inferred"),
     ("http://127.0.0.1:18765", "remote", "remote", "profile"),
     ("https://cq.exemplo.br", "local", "local", "profile")],
    ids=["ipv4_loopback", "ipv6_loopback", "localhost", "host_remoto", "ip_da_rede", "declarado_vence_remote",
         "declarado_vence_local"])
def test_kind_declared_or_inferred(tmp_path, api_url, declared, expected_kind, kind_origin) -> None:
    """o tipo é o declarado (`kind`); sem ele, `local` só com o host `127.0.0.1`, `::1` ou
    `localhost`, e `remote` nos demais; o declarado vence a inferência."""
    kind_line = f'kind = "{declared}"\n' if declared else ""
    path = _write(tmp_path / "cq.toml", f'[profiles.p]\n{kind_line}api_url = "{api_url}"\n')
    profile = resolve_profile(_env(path), "p")
    assert profile.kind == expected_kind
    assert profile.origins["kind"] == kind_origin


def test_env_remote_url_over_local_profile_with_stack_dir(tmp_path) -> None:
    """Passos 1, 4 e 5 da resolução: `CQ_API_URL` remota sobre um perfil local inferido com
    `stack_dir` não é erro (as chaves da pilha são validadas pelo tipo do próprio perfil, antes das
    variáveis); o tipo efetivo, inferido do endereço efetivo, vira `remote`."""
    path = _write(tmp_path / "cq.toml", f'[profiles.p]\napi_url = "{LOCAL_URL}"\nstack_dir = "{tmp_path.as_posix()}"\n'
                                        'compose_project = "cq-p"\n')
    assert resolve_profile(_env(path), "p").kind == "local"
    profile = resolve_profile(_env(path, CQ_API_URL=VM_URL), "p")
    assert profile.kind == "remote"
    assert profile.api_url == VM_URL
    assert (profile.origins["api_url"], profile.origins["kind"]) == ("env", "inferred")


@pytest.mark.parametrize(
    "profile_text",
    [f'kind = "remote"\napi_url = "{LOCAL_URL}"\nstack_dir = "C:/pilha"\n',
     f'api_url = "{VM_URL}"\ncompose_project = "cq"\n',
     f'kind = "remote"\napi_url = "{VM_URL}"\nstack_dir = "/srv/pilha"\ncompose_project = "cq"\n'],
    ids=["stack_dir_no_remoto_declarado", "projeto_no_remoto_inferido", "os_dois_no_remoto"])
def test_stack_keys_in_remote_profile_are_refused(tmp_path, profile_text) -> None:
    """`stack_dir` ou `compose_project` num perfil cujo tipo (declarado ou inferido do
    próprio `api_url`) é `remote` → `InvalidProfileField`, mesmo com `CQ_API_URL` de loopback (a validação
    é anterior às variáveis), sem eco."""
    path = _write(tmp_path / "cq.toml", f"[profiles.{CANARY_NAME}]\n" + profile_text
                  + f'token = "{CANARY_TOKEN}"\n')
    for env in (_env(path), _env(path, CQ_API_URL=LOCAL_URL)):
        with pytest.raises(InvalidProfileField) as exc:
            resolve_profile(env, CANARY_NAME)
        assert str(exc.value) == REASONS[InvalidProfileField] == exc.value.reason
        assert isinstance(exc.value, ProfileError)
        _assert_no_echo(exc.value, CANARY_NAME, CANARY_TOKEN, "pilha")


@pytest.mark.parametrize("project", ["CQ", "-cq", "_cq", "cq demo", "cq.demo", "", "cq-demo", "cq-systest"],
                         ids=["maiuscula", "hifen_inicial", "sublinhado_inicial", "espaco", "ponto", "vazio",
                              "reservado_demo", "reservado_systest"])
def test_compose_project_invalid_or_reserved(tmp_path, project) -> None:
    """`compose_project` fora de `^[a-z0-9][a-z0-9_-]*$`, `cq-demo` ou
    `cq-systest` → `InvalidComposeProject`, sem eco do valor."""
    path = _write(tmp_path / "cq.toml", f'[profiles.local]\nkind = "local"\napi_url = "{LOCAL_URL}"\n'
                                        f'compose_project = "{project}"\n')
    with pytest.raises(InvalidComposeProject) as exc:
        resolve_profile(_env(path), "local")
    assert str(exc.value) == REASONS[InvalidComposeProject]
    if project:
        _assert_no_echo(exc.value, project)


@pytest.mark.parametrize("project", ["cq", "cq2", "meu_cq-1", "0cq"])
def test_compose_project_accepted(tmp_path, project) -> None:
    """um nome no padrão e fora dos reservados é aceito, com a origem `profile`."""
    path = _write(tmp_path / "cq.toml", f'[profiles.local]\nkind = "local"\napi_url = "{LOCAL_URL}"\n'
                                        f'compose_project = "{project}"\n')
    profile = resolve_profile(_env(path), "local")
    assert profile.compose_project == project
    assert profile.origins["compose_project"] == "profile"


@pytest.mark.parametrize("kind", ["Local", "docker", ""], ids=["caixa", "outro", "vazio"])
def test_invalid_kind(tmp_path, kind) -> None:
    """`kind` diferente de `local` e `remote` → `InvalidProfileKind`, sem eco."""
    path = _write(tmp_path / "cq.toml", f'[profiles.p]\nkind = "{kind}"\napi_url = "{LOCAL_URL}"\n')
    with pytest.raises(InvalidProfileKind) as exc:
        resolve_profile(_env(path), "p")
    assert str(exc.value) == REASONS[InvalidProfileKind]
    if kind:
        _assert_no_echo(exc.value, kind)


@pytest.mark.parametrize("how", ["option", "env", "default_profile"])
def test_unknown_profile_without_echo(tmp_path, how) -> None:
    """o perfil escolhido (pela opção, por `CQ_PROFILE` ou por `default_profile`) que não
    existe em `profiles` → `UnknownProfile`, com o motivo fixo, sem o nome digitado nem o conteúdo."""
    text = NAMED_FILE.replace(f'token = "{VM_TOKEN}"', f'token = "{CANARY_TOKEN}"')
    if how == "default_profile":
        text = f'default_profile = "{CANARY_NAME}"\n' + text
    path = _write(tmp_path / "cq.toml", text)
    env = _env(path, CQ_PROFILE=CANARY_NAME) if how == "env" else _env(path)
    with pytest.raises(UnknownProfile) as exc:
        resolve_profile(env, CANARY_NAME if how == "option" else None)
    assert str(exc.value) == REASONS[UnknownProfile] == UnknownProfile.reason
    assert isinstance(exc.value, ProfileError)
    _assert_no_echo(exc.value, CANARY_NAME, CANARY_TOKEN, VM_URL, LOCAL_TOKEN)


def test_unknown_profile_without_profiles_table(tmp_path) -> None:
    """sem a tabela `profiles` (ou sem arquivo), qualquer nome escolhido é desconhecido."""
    path = _write(tmp_path / "cq.toml", _top())
    with pytest.raises(UnknownProfile):
        resolve_profile(_env(path), "vm")
    with pytest.raises(UnknownProfile):
        resolve_profile(_env(tmp_path / "nao-existe.toml"), "vm")


@pytest.mark.parametrize("key", ["senha", "API_URL", "default_profile", "stackdir"])
def test_unknown_key_in_named_profile(tmp_path, key) -> None:
    """uma chave fora de `kind`, `api_url`, `token`, `stack_dir` e `compose_project` num perfil
    nomeado → `UnknownProfileKey`, sem eco da chave, do valor nem do token."""
    path = _write(tmp_path / "cq.toml", f'[profiles.vm]\nkind = "remote"\napi_url = "{VM_URL}"\n'
                                        f'token = "{CANARY_TOKEN}"\n{key} = "{CANARY_PASSWORD}"\n')
    with pytest.raises(UnknownProfileKey) as exc:
        resolve_profile(_env(path), "vm")
    assert str(exc.value) == REASONS[UnknownProfileKey]
    _assert_no_echo(exc.value, CANARY_PASSWORD, CANARY_TOKEN)


def test_unknown_top_level_key_is_ignored(tmp_path) -> None:
    """uma chave desconhecida no topo é ignorada, no implícito e com um perfil nomeado."""
    path = _write(tmp_path / "cq.toml", 'editor = "vim"\n' + NAMED_FILE + "\n[outra]\nchave = 1\n")
    implicit = resolve_profile(_env(path))
    assert implicit.as_profile() == load_profile(_env(path)) == Profile(FILE_URL, FILE_TOKEN)
    assert resolve_profile(_env(path), "vm").api_url == VM_URL


@pytest.mark.parametrize(
    "text",
    [f'api_url = "https://usuario:{CANARY_PASSWORD}@cq.test"\ntoken = "{CANARY_TOKEN}"\n[[[ {CANARY_NAME}\n',
     f'default_profile = 3\ntoken = "{CANARY_TOKEN}"\n',
     f'default_profile = ["{CANARY_NAME}"]\n',
     f'profiles = "{CANARY_NAME}"\ntoken = "{CANARY_TOKEN}"\n'],
    ids=["toml_invalido", "default_profile_numero", "default_profile_lista", "profiles_texto"])
def test_invalid_file_and_wrong_types_in_new_top_keys(tmp_path, text) -> None:
    """TOML inválido e tipos errados nas chaves novas de topo (`default_profile`, `profiles`) →
    a mensagem original (`InvalidProfileFile`), sem o conteúdo, as credenciais nem o token."""
    path = _write(tmp_path / "cq.toml", text)
    with pytest.raises(InvalidProfileFile) as exc:
        resolve_profile(_env(path))
    assert str(exc.value) == REASONS[InvalidProfileFile]
    _assert_no_echo(exc.value, CANARY_PASSWORD, CANARY_TOKEN, CANARY_NAME)


# --------------------------------------------------------------------------------------------------
# Endereço sem credenciais, `repr` seguro e o aviso de `http://`


def test_display_url_and_repr_without_credentials(tmp_path) -> None:
    """com `api_url = "https://usuario:senha@host"`, o `display_url` sai sem o
    `user:senha@`, e o `repr` não mostra a senha nem o token; o `api_url` efetivo é o do arquivo (as
    credenciais nunca vão à API, regra do `ApiClient`)."""
    url = f"https://usuario:{CANARY_PASSWORD}@vm.cq.test/base"
    path = _write(tmp_path / "cq.toml", f'[profiles.vm]\nkind = "remote"\napi_url = "{url}"\n'
                                        f'token = "{CANARY_TOKEN}"\n')
    profile = resolve_profile(_env(path), "vm")
    assert profile.api_url == url
    assert profile.display_url == "https://vm.cq.test/base"
    text = repr(profile)
    assert CANARY_PASSWORD not in text and "usuario" not in text
    assert CANARY_TOKEN not in text
    assert "token='<definido>'" in text
    assert profile.as_profile() == Profile(url, CANARY_TOKEN)
    assert CANARY_PASSWORD not in repr(profile.as_profile())


@pytest.mark.parametrize(
    ("profile_text", "selector", "warns"),
    [(f'[profiles.vm]\nkind = "remote"\napi_url = "http://vm.cq.test"\n', "vm", True),
     (f'[profiles.vm]\napi_url = "http://vm.cq.test:8080"\n', "vm", True),
     (f'[profiles.vm]\nkind = "remote"\napi_url = "https://vm.cq.test"\n', "vm", False),
     (f'[profiles.vm]\nkind = "remote"\napi_url = "http://127.0.0.1:18765"\n', "vm", False),
     (f'[profiles.vm]\nkind = "remote"\napi_url = "http://localhost:18765"\n', "vm", False),
     (f'[profiles.vm]\nkind = "remote"\napi_url = "http://[::1]:18765"\n', "vm", False),
     (f'[profiles.vm]\nkind = "local"\napi_url = "http://vm.cq.test"\n', "vm", False),
     ('api_url = "http://api.remota.cq.test"\n', None, False)],
    ids=["nomeado_remoto_http", "nomeado_inferido_http", "nomeado_https", "remoto_declarado_loopback_ipv4",
         "remoto_declarado_localhost", "remoto_declarado_loopback_ipv6", "nomeado_local", "implicito_http_remoto"])
def test_insecure_transport_warning(tmp_path, profile_text, selector, warns) -> None:
    """o aviso de `http://` só com perfil nomeado, `remote`, esquema
    `http` e host fora do loopback (um `remote` declarado com `http://127.0.0.1:…`, um túnel SSH, não avisa;
    o implícito com `http://` remoto também não)."""
    path = _write(tmp_path / "cq.toml", profile_text)
    profile = resolve_profile(_env(path), selector)
    warning = insecure_transport_warning(profile)
    if warns:
        assert warning == INSECURE_TRANSPORT_TEMPLATE.format(name="vm")
        assert warning == 'Aviso: o perfil "vm" usa http:// fora do loopback; o token vai sem cifragem.'
    else:
        assert warning is None


def test_insecure_transport_warning_without_credentials(tmp_path) -> None:
    """o aviso de um perfil com `http://usuario:senha@host` não mostra a senha nem o token."""
    path = _write(tmp_path / "cq.toml", f'[profiles.vm]\nkind = "remote"\n'
                                        f'api_url = "http://usuario:{CANARY_PASSWORD}@vm.cq.test"\n'
                                        f'token = "{CANARY_TOKEN}"\n')
    profile = resolve_profile(_env(path), "vm")
    warning = insecure_transport_warning(profile)
    assert warning == INSECURE_TRANSPORT_TEMPLATE.format(name="vm")
    assert CANARY_PASSWORD not in warning and CANARY_TOKEN not in warning and "usuario" not in warning


def test_named_local_profile_with_missing_stack_dir_resolves(tmp_path) -> None:
    """A metade negativa: um perfil nomeado `kind = "local"` com
    `stack_dir` inexistente resolve **sem erro**, com o `stack_dir` preservado; só os comandos da pilha o
    recusam (tests/unit/test_stack_locate.py, tests/cli/test_cli_stack.py)."""
    missing = tmp_path / "pilha-que-nao-existe"
    path = _write(tmp_path / "cq.toml", f'[profiles.local]\nkind = "local"\napi_url = "{LOCAL_URL}"\n'
                                        f'token = "{LOCAL_TOKEN}"\nstack_dir = "{missing.as_posix()}"\n')
    profile = resolve_profile(_env(path, CQ_PROFILE="local"))
    assert not missing.exists()
    assert profile.stack_dir == pathlib.Path(missing.as_posix())
    assert (profile.kind, profile.api_url, profile.token) == ("local", LOCAL_URL, LOCAL_TOKEN)
    assert profile.origins["name"] == "env"
