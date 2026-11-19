"""O age v1.3.2 oficial dos testes, do cache em `CQ_TEST_AGE_DIR`.


- `age_paths() -> (age, age_keygen)`: os caminhos dos binários a partir de `CQ_TEST_AGE_DIR`, **a única
  variável do desenvolvedor lida pelos testes**. Exige o leiaute do cache: o arquivo oficial da
  plataforma (`AGE_ASSETS` de `scripts/fetch_age.py`, carregado pelo `load_ops`) e a extração lado a lado,
  em `age/`. Confere o SHA-256 do arquivo contra o fixado e confere que os binários extraídos são **byte a
  byte** os membros do arquivo. Ausente, divergente ou incompleto → `pytest.fail` com a mensagem de ambiente
  fixa `AGE_ENV_MESSAGE` (nunca `skip`), sem eco do caminho. O resultado bom fica em cache no processo.
- `keypair(tmp_path) -> (identity_path, recipient)`: gera a identidade com `age-keygen -o` num arquivo do
  `tmp_path` e obtém o destinatário com `age-keygen -y <arquivo>` (a identidade nunca é lida para a
  memória do teste; só os detectores do canário a leem, por conta própria).

Membros esperados (**[F]** lista do `age-v1.3.2-windows-amd64.zip` do cache, lida com `zipfile` em
2026-10-29: `age/age.exe`, `age/age-keygen.exe` e os demais em `age/`). **[I]** No Linux, `age/age` e
`age/age-keygen` dentro do `.tar.gz`: a lista de membros do arquivo do Linux é uma lacuna ainda aberta, que
pede rede e não foi feita.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.helpers.ops import clean_env, load_ops

AGE_DIR_VARIABLE = "CQ_TEST_AGE_DIR"
EXTRACT_DIR = "age"

AGE_ENV_MESSAGE = (
    "Ambiente de teste incompleto: o age v1.3.2 oficial não está no cache de CQ_TEST_AGE_DIR (arquivo da "
    "release e extração lado a lado, com o SHA-256 fixado). Refaça o cache com scripts/fetch_age.py "
    "(o arquivo oficial da plataforma e a extração lado a lado, em age/)."
)

RECIPIENT_PATTERN = re.compile(r"age1[0-9a-z]+")
KEYGEN_TIMEOUT_SECONDS = 60

_verified: dict[str, tuple[Path, Path]] = {}


def _fail() -> None:
    pytest.fail(AGE_ENV_MESSAGE, pytrace=False)


def _platform() -> str:
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        _fail()
    if sys.platform == "win32":
        return "windows-amd64"
    if sys.platform.startswith("linux"):
        return "linux-amd64"
    _fail()
    raise AssertionError("inalcançável")


def _members(platform_key: str) -> tuple[str, str]:
    suffix = ".exe" if platform_key.startswith("windows") else ""
    return f"{EXTRACT_DIR}/age{suffix}", f"{EXTRACT_DIR}/age-keygen{suffix}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_member(archive: Path, member: str) -> bytes | None:
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                try:
                    return bundle.read(member)
                except KeyError:
                    return None
        with tarfile.open(archive, "r:gz") as bundle:
            try:
                handle = bundle.extractfile(member)
            except KeyError:
                return None
            return handle.read() if handle is not None else None
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None


def age_paths() -> tuple[Path, Path]:
    """`(age, age_keygen)` do cache conferido (ver o docstring do módulo)."""
    root_text = os.environ.get(AGE_DIR_VARIABLE, "")
    if not root_text:
        _fail()
    cached = _verified.get(root_text)
    if cached is not None:
        return cached
    root = Path(root_text)
    platform_key = _platform()
    fetch_age = load_ops("scripts/fetch_age.py")
    asset, expected_sha = fetch_age.AGE_ASSETS[platform_key]
    archive = root / asset
    if not archive.is_file() or _sha256(archive) != expected_sha:
        _fail()
    paths: list[Path] = []
    for member in _members(platform_key):
        binary = root / Path(member)
        if not binary.is_file():
            _fail()
        expected = _archive_member(archive, member)
        if expected is None or binary.read_bytes() != expected:
            _fail()
        paths.append(binary)
    result = (paths[0], paths[1])
    _verified[root_text] = result
    return result


def keypair(tmp_path: Path, *, name: str = "age-identity.txt") -> tuple[Path, str]:
    """Gera uma identidade do age em `tmp_path/<name>` e devolve `(caminho, destinatário age1…)`."""
    _, age_keygen = age_paths()
    identity = Path(tmp_path) / name
    if identity.exists():
        raise FileExistsError(f"a identidade de teste já existe: {name}")
    env = clean_env()
    generated = subprocess.run([str(age_keygen), "-o", str(identity)], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=KEYGEN_TIMEOUT_SECONDS, check=False)
    if generated.returncode != 0 or not identity.is_file():
        raise AssertionError("age-keygen -o falhou no tmp_path")
    public = subprocess.run([str(age_keygen), "-y", str(identity)], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=KEYGEN_TIMEOUT_SECONDS, check=False)
    recipient = public.stdout.decode("ascii", "replace").strip()
    if public.returncode != 0 or not RECIPIENT_PATTERN.fullmatch(recipient):
        raise AssertionError("age-keygen -y não devolveu um destinatário age1…")
    return identity, recipient
