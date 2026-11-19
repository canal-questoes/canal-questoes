"""Perfil do CLI: endereço da API e token."""

from __future__ import annotations

import pathlib

import click
import pytest

from cq_client.profile import Profile, default_config_path, load_profile

DEFAULT_URL = "http://127.0.0.1:8000"
FILE_URL = "https://api.arquivo.example:8443"
FILE_TOKEN = "tok-do-arquivo-Q7x2Lm9"
ENV_URL = "http://api.ambiente.example:9000"
ENV_TOKEN = "tok-do-ambiente-Zp4Wc8"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixa `APPDATA`, `XDG_CONFIG_HOME`, `HOME` e `USERPROFILE` em `tmp_path`, para não ler o perfil
    real da máquina, e tira `CQ_*` do ambiente do processo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("CQ_API_URL", "CQ_TOKEN", "CQ_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _expected_default_path() -> pathlib.Path:
    return pathlib.Path(click.get_app_dir("cq")) / "config.toml"


def _write_toml(path: pathlib.Path, *, api_url: str | None = FILE_URL,
                token: str | None = FILE_TOKEN) -> pathlib.Path:
    lines = []
    if api_url is not None:
        lines.append(f'api_url = "{api_url}"')
    if token is not None:
        lines.append(f'token = "{token}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_nothing_configured_gives_local_api_without_token(tmp_path: pathlib.Path) -> None:
    """nada configurado → `Profile("http://127.0.0.1:8000", None)`; o arquivo padrão
    (dentro de `tmp_path`, pela fixture) não existe."""
    assert tmp_path in _expected_default_path().parents
    assert not _expected_default_path().exists()
    assert load_profile({}) == Profile(DEFAULT_URL, None)


def test_toml_from_cq_config(tmp_path: pathlib.Path) -> None:
    """o TOML indicado por `CQ_CONFIG` dá `api_url` e `token`."""
    path = _write_toml(tmp_path / "perfis" / "cq.toml")
    assert load_profile({"CQ_CONFIG": str(path)}) == Profile(FILE_URL, FILE_TOKEN)


def test_toml_from_default_path() -> None:
    """sem `CQ_CONFIG`, o perfil é lido de
    `Path(click.get_app_dir("cq")) / "config.toml"`."""
    _write_toml(_expected_default_path())
    assert load_profile({}) == Profile(FILE_URL, FILE_TOKEN)


@pytest.mark.parametrize(
    ("env_extra", "expected"),
    [
        ({}, Profile(FILE_URL, FILE_TOKEN)),
        ({"CQ_API_URL": ENV_URL}, Profile(ENV_URL, FILE_TOKEN)),
        ({"CQ_TOKEN": ENV_TOKEN}, Profile(FILE_URL, ENV_TOKEN)),
        ({"CQ_API_URL": ENV_URL, "CQ_TOKEN": ENV_TOKEN}, Profile(ENV_URL, ENV_TOKEN)),
    ],
    ids=["so_arquivo", "url_do_ambiente", "token_do_ambiente", "os_dois_do_ambiente"],
)
def test_environment_overrides_file_field_by_field(tmp_path: pathlib.Path, env_extra: dict[str, str],
                                                   expected: Profile) -> None:
    """`CQ_API_URL` e `CQ_TOKEN` sobrepõem o arquivo, campo a campo."""
    path = _write_toml(tmp_path / "cq.toml")
    assert load_profile({"CQ_CONFIG": str(path), **env_extra}) == expected


@pytest.mark.parametrize(
    ("file_fields", "env_extra", "expected"),
    [
        ({"api_url": None}, {}, Profile(DEFAULT_URL, FILE_TOKEN)),
        ({"token": None}, {}, Profile(FILE_URL, None)),
        ({"api_url": None}, {"CQ_API_URL": ENV_URL}, Profile(ENV_URL, FILE_TOKEN)),
        ({"token": None}, {"CQ_TOKEN": ENV_TOKEN}, Profile(FILE_URL, ENV_TOKEN)),
    ],
    ids=["arquivo_so_token", "arquivo_so_url", "arquivo_so_token_url_do_ambiente",
         "arquivo_so_url_token_do_ambiente"],
)
def test_partial_file_combines_with_environment_and_defaults(
        tmp_path: pathlib.Path, file_fields: dict, env_extra: dict[str, str], expected: Profile) -> None:
    """um arquivo com um só campo; o outro vem do ambiente ou do padrão."""
    path = _write_toml(tmp_path / "cq.toml", **file_fields)
    assert load_profile({"CQ_CONFIG": str(path), **env_extra}) == expected


def test_empty_variables_count_as_absent(tmp_path: pathlib.Path) -> None:
    """variável vazia vale como ausente (`CQ_API_URL`, `CQ_TOKEN` e `CQ_CONFIG`)."""
    path = _write_toml(tmp_path / "cq.toml")
    assert load_profile({"CQ_CONFIG": str(path), "CQ_API_URL": "", "CQ_TOKEN": ""}) == Profile(
        FILE_URL, FILE_TOKEN)
    assert load_profile({"CQ_API_URL": "", "CQ_TOKEN": "", "CQ_CONFIG": ""}) == Profile(DEFAULT_URL, None)
    assert default_config_path({"CQ_CONFIG": ""}) == _expected_default_path()
    _write_toml(_expected_default_path(), api_url=ENV_URL, token=ENV_TOKEN)
    assert load_profile({"CQ_CONFIG": ""}) == Profile(ENV_URL, ENV_TOKEN)


def test_default_config_path_uses_cq_config_when_not_empty(tmp_path: pathlib.Path) -> None:
    """com `CQ_CONFIG` não vazio, `default_config_path` devolve esse caminho."""
    path = tmp_path / "outro-lugar" / "perfil.toml"
    result = default_config_path({"CQ_CONFIG": str(path)})
    assert isinstance(result, pathlib.Path)
    assert result == path


def test_default_config_path_uses_click_app_dir(tmp_path: pathlib.Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """sem `CQ_CONFIG`, `Path(click.get_app_dir("cq")) / "config.toml"`, com `APPDATA` e
    `XDG_CONFIG_HOME` fixados; o diretório vem do ambiente do **processo**, não do `env` recebido."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-fixado"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-fixado"))
    expected = _expected_default_path()
    assert tmp_path in expected.parents
    result = default_config_path({})
    assert isinstance(result, pathlib.Path)
    assert result == expected
    other = str(tmp_path / "nao-usar")
    assert default_config_path({"APPDATA": other, "XDG_CONFIG_HOME": other, "HOME": other}) == expected


@pytest.mark.parametrize(
    ("env_extra", "expected"),
    [({}, Profile(DEFAULT_URL, None)), ({"CQ_TOKEN": ENV_TOKEN}, Profile(DEFAULT_URL, ENV_TOKEN)),
     ({"CQ_API_URL": ENV_URL}, Profile(ENV_URL, None))],
    ids=["sem_ambiente", "token_do_ambiente", "url_do_ambiente"],
)
def test_missing_file_gives_defaults(tmp_path: pathlib.Path, env_extra: dict[str, str],
                                     expected: Profile) -> None:
    """`CQ_CONFIG` apontando para um arquivo inexistente → padrões (mais o ambiente)."""
    missing = tmp_path / "nao-existe.toml"
    assert load_profile({"CQ_CONFIG": str(missing), **env_extra}) == expected
    assert not missing.exists()


def test_process_environment_is_not_the_source(tmp_path: pathlib.Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """`CQ_API_URL`, `CQ_TOKEN` e `CQ_CONFIG` vêm só do `env` recebido; os do
    ambiente do processo são ignorados."""
    decoy = _write_toml(tmp_path / "isca.toml", api_url="http://isca.example:1", token="tok-isca-000")
    monkeypatch.setenv("CQ_API_URL", "http://processo.example:2")
    monkeypatch.setenv("CQ_TOKEN", "tok-do-processo-111")
    monkeypatch.setenv("CQ_CONFIG", str(decoy))
    assert load_profile({}) == Profile(DEFAULT_URL, None)
    assert default_config_path({}) == _expected_default_path()
