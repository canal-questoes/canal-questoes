"""Testes de `scripts/fetch_age.py`: o download da release oficial do `age`, conferido pelo SHA-256
fixado, e a extração no leiaute que os testes usam.

Nenhuma rede: o `opener` é falso. Ele entra de dois jeitos, para valer qualquer que seja a forma do `main`:
no padrão nomeado `opener` de `fetch` (`fetch.__kwdefaults__`) e no `urllib.request.urlopen` (os dois
restaurados pelo `monkeypatch`); o `socket.connect` também fica barrado durante cada teste.

O que se confere:
- cada plataforma de `AGE_ASSETS` pede **só** o asset dela, na URL da release oficial `v1.3.2`;
- SHA-256 diferente do fixado → recusa com a mensagem fixa, código ≠ 0 e nada no destino;
- conferido → o arquivo e a extração lado a lado no leiaute dos testes, o mesmo que
  `tests/helpers/age_bin.py` exige (conferido chamando `age_paths()` sobre o destino, com o arquivo oficial do
  cache já preparado);
- destino dentro do repositório recusado, sem pedido nenhum;
- plataforma desconhecida → `invalid_arguments`;
- nenhuma saída com caminho nem URL: toda linha impressa é um texto de `FETCH_MESSAGES`.

**[I]** Escolhas deste teste: a linha de comando (`--platform`, `--dest`) é a do docstring
do esqueleto; a mensagem `ok` sai no stdout e as de falha, uma linha no stderr, como nos scripts de `ops/`;
os arquivos sintéticos (com o SHA-256 fixado trocado pelo `monkeypatch`) servem para a plataforma que não é a
do host e para os casos de recusa; o caso com o arquivo oficial usa o do cache (`CQ_TEST_AGE_DIR`).
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import sys
import tarfile
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.helpers.age_bin import AGE_DIR_VARIABLE, age_paths
from tests.helpers.ops import REPO, Result, clean_env, load_ops, run_main

fetch_age = load_ops("scripts/fetch_age.py")

RELEASE_PREFIX = "https://github.com/FiloSottile/age/releases/download/v1.3.2/"

# **[F]**: os assets e os SHA-256 fixados da release oficial v1.3.2.
EXPECTED_ASSETS = {
    "windows-amd64": ("age-v1.3.2-windows-amd64.zip",
                      "f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7"),
    "linux-amd64": ("age-v1.3.2-linux-amd64.tar.gz",
                    "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"),
}

PLATFORMS = tuple(EXPECTED_ASSETS)


# --------------------------------------------------------------------------------------------------
# Dublês
# --------------------------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """A resposta do `urlopen` falso: bytes com `read`, `status`, `getcode()` e uso em `with`."""

    status = 200

    def getcode(self) -> int:
        return self.status

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Length": str(len(self.getvalue()))}


class FakeOpener:
    """O `opener` falso: registra cada URL pedida e devolve `payload` (ou levanta a exceção dada)."""

    def __init__(self, payload: bytes | BaseException) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: object, *args: object, **kwargs: object) -> FakeResponse:
        self.urls.append(str(getattr(url, "full_url", url)))
        if isinstance(self.payload, BaseException):
            raise self.payload
        return FakeResponse(self.payload)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: socket.socket, *args: object, **kwargs: object) -> None:
        pytest.fail("o teste do fetch_age tentou abrir uma conexão de rede")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _install(monkeypatch: pytest.MonkeyPatch, opener: FakeOpener) -> None:
    """Põe o `opener` falso no padrão de `fetch` e no `urllib.request.urlopen`."""
    defaults = fetch_age.fetch.__kwdefaults__
    assert defaults is not None and "opener" in defaults, "fetch deve ter o parâmetro nomeado opener"
    monkeypatch.setitem(defaults, "opener", opener)
    monkeypatch.setattr(urllib.request, "urlopen", opener)


def _members(platform: str) -> dict[str, bytes]:
    """Os membros de um arquivo sintético no leiaute da release (tudo em `age/`)."""
    suffix = ".exe" if platform.startswith("windows") else ""
    return {
        f"age/age{suffix}": b"binario falso do age " + platform.encode("ascii"),
        f"age/age-keygen{suffix}": b"binario falso do age-keygen " + platform.encode("ascii"),
        "age/LICENSE": b"licenca falsa\n",
    }


def _zip_bytes(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def _tar_gz_bytes(members: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _archive(platform: str, members: Mapping[str, bytes]) -> bytes:
    return _zip_bytes(members) if platform.startswith("windows") else _tar_gz_bytes(members)


def _pin(monkeypatch: pytest.MonkeyPatch, platform: str, data: bytes) -> str:
    """Troca o SHA-256 fixado de `platform` pelo do arquivo sintético `data`; devolve o nome do asset."""
    asset = EXPECTED_ASSETS[platform][0]
    monkeypatch.setitem(fetch_age.AGE_ASSETS, platform, (asset, hashlib.sha256(data).hexdigest()))
    return asset


def _host_platform() -> str:
    return "windows-amd64" if sys.platform == "win32" else "linux-amd64"


def _message(key: str) -> str:
    return fetch_age.FETCH_MESSAGES[key][1]


def _code(key: str) -> int:
    return fetch_age.FETCH_MESSAGES[key][0]


def _main(argv: list[str]) -> Result:
    return run_main(fetch_age, argv, env=clean_env())


def _leftovers(dest: Path) -> list[str]:
    """O que ficou no destino (vazio se ele não existe mais)."""
    if not dest.exists():
        return []
    return sorted(str(path.relative_to(dest)) for path in dest.rglob("*"))


def _assert_only_fixed_lines(result: Result, *paths: Path) -> None:
    """Toda linha impressa é um texto de `FETCH_MESSAGES`; nenhum caminho nem URL."""
    texts = {text for _, text in fetch_age.FETCH_MESSAGES.values()}
    for line in (result.stdout + result.stderr).splitlines():
        assert line in texts, "saída fora das mensagens fixas"
    combined = result.stdout + result.stderr
    assert "http" not in combined.lower()
    for path in paths:
        assert str(path) not in combined
        assert path.as_posix() not in combined


# --------------------------------------------------------------------------------------------------
# Tabela e URL por plataforma
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_assets_table_and_each_platform_requests_only_its_asset(platform: str, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """a tabela fixada é a da release oficial v1.3.2; cada plataforma pede só o asset
    dela, na URL oficial, e o resultado fica no leiaute dos testes (o arquivo e a extração lado a lado, sem
    temporário). A tabela real e a chamada a `fetch` ficam na mesma função."""
    assert fetch_age.AGE_VERSION == "v1.3.2"
    assert dict(fetch_age.AGE_ASSETS) == EXPECTED_ASSETS
    assert fetch_age.AGE_RELEASE_URL.format(version="v1.3.2", asset="x") == RELEASE_PREFIX + "x"

    members = _members(platform)
    data = _archive(platform, members)
    asset = _pin(monkeypatch, platform, data)
    opener = FakeOpener(data)
    dest = tmp_path / "cache"
    dest.mkdir()

    code = fetch_age.fetch(platform, dest, opener=opener)

    assert code == _code("ok") == 0
    assert opener.urls == [RELEASE_PREFIX + asset]
    assert sorted(path.name for path in dest.iterdir()) == sorted([asset, "age"])
    assert (dest / asset).read_bytes() == data
    for name, content in members.items():
        assert (dest / Path(name)).read_bytes() == content, name


def test_official_archive_lands_in_the_layout_age_bin_requires(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """com o arquivo oficial da plataforma do host (o do cache já preparado, servido pelo `opener` falso),
    o `main` deixa o destino no leiaute que `tests/helpers/age_bin.py` exige: `age_paths()` aceita o destino
    (SHA-256 do arquivo e binários byte a byte iguais aos membros)."""
    age_paths()  # o cache já preparado tem de estar íntegro (senão, a mensagem de ambiente)
    platform = _host_platform()
    asset = EXPECTED_ASSETS[platform][0]
    official = (Path(os.environ[AGE_DIR_VARIABLE]) / asset).read_bytes()
    opener = FakeOpener(official)
    _install(monkeypatch, opener)
    dest = tmp_path / "cache-novo"

    result = _main(["--platform", platform, "--dest", str(dest)])

    assert (result.code, result.stdout, result.stderr) == (0, _message("ok") + "\n", "")
    assert opener.urls == [RELEASE_PREFIX + asset]
    assert sorted(path.name for path in dest.iterdir()) == sorted([asset, "age"])
    monkeypatch.setenv(AGE_DIR_VARIABLE, str(dest))
    age, age_keygen = age_paths()
    assert age.parent == age_keygen.parent == dest / "age"
    _assert_only_fixed_lines(result, tmp_path, dest)


# --------------------------------------------------------------------------------------------------
# Recusas
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", PLATFORMS)
def test_checksum_mismatch_is_refused_and_leaves_nothing(platform: str, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """um arquivo cujo SHA-256 não é o fixado (o sintético, sem trocar o fixado) é recusado antes de
    extrair: código 65, a mensagem fixa e nada no destino (nem o arquivo, nem temporário, nem extração)."""
    opener = FakeOpener(_archive(platform, _members(platform)))
    _install(monkeypatch, opener)
    dest = tmp_path / "cache"
    dest.mkdir()

    result = _main(["--platform", platform, "--dest", str(dest)])

    assert result.code == _code("checksum_mismatch") == 65
    assert (result.stdout, result.stderr) == ("", _message("checksum_mismatch") + "\n")
    assert opener.urls == [RELEASE_PREFIX + EXPECTED_ASSETS[platform][0]]
    assert _leftovers(dest) == []
    _assert_only_fixed_lines(result, tmp_path, dest)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_archive_with_member_outside_destination_is_refused(platform: str, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem membros fora do destino: um arquivo conferido com um membro `../` é recusado sem extrair
    nada, nem dentro nem fora do destino."""
    members = {**_members(platform), "../fora-do-destino.txt": b"escapou\n"}
    data = _archive(platform, members)
    _pin(monkeypatch, platform, data)
    opener = FakeOpener(data)
    _install(monkeypatch, opener)
    dest = tmp_path / "cache"
    dest.mkdir()

    result = _main(["--platform", platform, "--dest", str(dest)])

    assert result.code == _code("unsafe_archive") == 65
    assert (result.stdout, result.stderr) == ("", _message("unsafe_archive") + "\n")
    assert _leftovers(dest) == []
    assert not (tmp_path / "fora-do-destino.txt").exists()
    _assert_only_fixed_lines(result, tmp_path, dest)


def test_download_failure_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A falha do download (o `opener` levanta) → `download_failed` (69), nada no destino."""
    platform = _host_platform()
    opener = FakeOpener(urllib.error.URLError("sem rede no teste"))
    _install(monkeypatch, opener)
    dest = tmp_path / "cache"
    dest.mkdir()

    result = _main(["--platform", platform, "--dest", str(dest)])

    assert result.code == _code("download_failed") == 69
    assert (result.stdout, result.stderr) == ("", _message("download_failed") + "\n")
    assert _leftovers(dest) == []
    _assert_only_fixed_lines(result, tmp_path, dest)


def test_destination_inside_repository_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """o destino dentro do repositório é recusado (`invalid_arguments`) antes de qualquer pedido, e
    nada é criado nele."""
    opener = FakeOpener(_archive(_host_platform(), _members(_host_platform())))
    _install(monkeypatch, opener)
    dest = REPO / f".cq-teste-fetch-age-{uuid.uuid4().hex}"
    assert not dest.exists()

    result = _main(["--platform", _host_platform(), "--dest", str(dest)])

    assert result.code == _code("invalid_arguments") == 64
    assert (result.stdout, result.stderr) == ("", _message("invalid_arguments") + "\n")
    assert opener.urls == []
    assert not dest.exists()
    _assert_only_fixed_lines(result, tmp_path, dest, REPO)


@pytest.mark.parametrize("argv", [
    pytest.param(["--platform", "solaris-sparc"], id="plataforma-desconhecida"),
    pytest.param(["--platform", "WINDOWS-AMD64"], id="plataforma-em-maiusculas"),
    pytest.param(["--platform", ""], id="plataforma-vazia"),
    pytest.param([], id="sem-plataforma"),
])
def test_unknown_platform_is_invalid_arguments(argv: list[str], tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Plataforma desconhecida ou ausente → `invalid_arguments` (64), sem eco do valor e sem pedido."""
    opener = FakeOpener(b"nada")
    _install(monkeypatch, opener)
    dest = tmp_path / "cache"

    result = _main([*argv, "--dest", str(dest)])

    assert result.code == _code("invalid_arguments") == 64
    assert (result.stdout, result.stderr) == ("", _message("invalid_arguments") + "\n")
    assert opener.urls == []
    assert _leftovers(dest) == []
    assert "solaris" not in result.stderr.lower()
    _assert_only_fixed_lines(result, tmp_path, dest)
