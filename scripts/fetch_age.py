"""Obtenção do age v1.3.2 oficial para os testes, o CI e a validação local.
Com rede.

Uso:

    uv run python scripts/fetch_age.py --platform windows-amd64|linux-amd64 --dest <diretório>

Baixa `https://github.com/FiloSottile/age/releases/download/v1.3.2/<asset>` para um temporário no destino,
confere o SHA-256 fixado (`AGE_ASSETS`) **antes** de extrair, extrai com `zipfile`/`tarfile` (sem membros
fora do destino) no leiaute que o `tests/helpers/age_bin.py` exige (o arquivo e a extração lado a lado).
Mensagens fixas (`FETCH_MESSAGES`), sem caminho.

Só biblioteca padrão. Importável sem efeito colateral. Nos testes, só com o `opener` falso (nenhuma rede).

Como funciona:

1. O destino é recusado (`invalid_arguments`, antes de qualquer pedido) se estiver dentro deste repositório
   (binário fora do git) ou se existir sem ser diretório. Se não existir, é criado; numa falha, o
   diretório criado aqui é removido de novo.
2. Tudo o que é baixado e extraído fica num diretório temporário oculto dentro do destino (`.fetch-age-*`), que
   some ao fim, com sucesso ou não: numa falha, o destino fica como estava.
3. O download é lido em blocos, com o SHA-256 calculado no caminho e um teto de tamanho (`MAX_DOWNLOAD_BYTES`).
4. Conferido o SHA-256, **todos** os membros do arquivo são validados antes de extrair qualquer um: só arquivos
   regulares e diretórios (nada de link simbólico, link físico ou dispositivo), nomes relativos sem `..`, sem
   barra invertida, sem `:` e sem duplicata (comparados sem distinção de caixa, como no Windows). Um membro ruim
   recusa o arquivo inteiro (`unsafe_archive`). A entrada de diretório `age/` do arquivo real é aceita.
5. Extraído no temporário, o resultado entra no destino: o arquivo com o nome do asset e as entradas de topo da
   extração (`age/`), substituindo as de uma obtenção anterior.

`fetch` escreve a mensagem fixa da chave final (a de `ok` no stdout; as de falha, uma linha no stderr, como os
scripts de `ops/`) e devolve o código dela. **[I]** Escolha minha: a mensagem sai em `fetch`, e não só em `main`,
porque o código sozinho não distingue as chaves que o compartilham (65, 64), e a assinatura de `fetch` é fixa.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

AGE_VERSION = "v1.3.2"

# **[F]** Plataforma → (nome do arquivo da release, SHA-256).
AGE_ASSETS: Mapping[str, tuple[str, str]] = {
    "windows-amd64": ("age-v1.3.2-windows-amd64.zip",
                      "f48d8f8f9ebe903ab5027ed067652f2cc1db94bc206976430133b905dcd8e8c7"),
    "linux-amd64": ("age-v1.3.2-linux-amd64.tar.gz",
                    "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10"),
}

AGE_RELEASE_URL = "https://github.com/FiloSottile/age/releases/download/{version}/{asset}"

# Chave → (código de saída, mensagem). [I] Chaves, códigos e textos da implementação.
FETCH_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "age v1.3.2 pronto."),
    "unknown_platform": (64, "Plataforma não suportada."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "checksum_mismatch": (65, "O arquivo baixado não confere com o SHA-256 fixado; nada foi extraído."),
    "unsafe_archive": (65, "O arquivo baixado tem membros fora do destino; nada foi extraído."),
    "download_failed": (69, "Não foi possível baixar o age."),
    "internal": (1, "Erro interno ao obter o age."),
}

# A forma do `urllib.request.urlopen` (`opener(url, *, timeout)` → objeto com `read()`).
Opener = Callable[..., Any]

# [I] Limites escolhidos: os arquivos oficiais têm poucos MiB; o teto só impede um download ou uma extração sem fim.
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
STAGING_PREFIX = ".fetch-age-"

# A raiz deste repositório (o script vive em `scripts/`).
REPO_ROOT = Path(__file__).resolve().parents[1]

_Plan = list[tuple[tuple[str, ...], bool, int]]


class _Refusal(Exception):
    """Interrompe a obtenção com uma chave de `FETCH_MESSAGES` (nunca com texto de entrada)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _emit(key: str) -> int:
    code, text = FETCH_MESSAGES[key]
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write(text + "\n")
    stream.flush()
    return code


def _is_inside_repository(dest: Path) -> bool:
    resolved = os.path.normcase(str(dest.resolve()))
    root = os.path.normcase(str(REPO_ROOT))
    return resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------------------


def _download(url: str, target: Path, opener: Opener) -> str:
    """Grava a resposta de `url` em `target` e devolve o SHA-256 hexadecimal."""
    digest = hashlib.sha256()
    total = 0
    try:
        with opener(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise _Refusal("download_failed")
            with open(target, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise _Refusal("download_failed")
                    digest.update(chunk)
                    handle.write(chunk)
    except _Refusal:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # `urllib.error.URLError` e `HTTPError` são `OSError`; nada da exceção vai para a saída.
        raise _Refusal("download_failed") from exc
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------------
# Validação e extração dos membros
# --------------------------------------------------------------------------------------------------


def _safe_parts(name: str) -> tuple[str, ...]:
    """As partes de um nome de membro seguro; `_Refusal("unsafe_archive")` se ele puder sair do destino."""
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise _Refusal("unsafe_archive")
    parts = tuple(part for part in PurePosixPath(name).parts if part not in ("", "."))
    if not parts:
        raise _Refusal("unsafe_archive")
    for part in parts:
        if part == ".." or ":" in part or part != part.strip():
            raise _Refusal("unsafe_archive")
    return parts


def _check_plan(plan: _Plan) -> None:
    """Recusa duplicata, conflito arquivo/diretório e extração grande demais, antes de extrair."""
    files: set[tuple[str, ...]] = set()
    dirs: set[tuple[str, ...]] = set()
    total = 0
    for parts, is_dir, size in plan:
        key = tuple(part.casefold() for part in parts)
        if is_dir:
            if key in files:
                raise _Refusal("unsafe_archive")
            dirs.add(key)
            continue
        if key in files or key in dirs:
            raise _Refusal("unsafe_archive")
        files.add(key)
        total += max(size, 0)
        if total > MAX_EXTRACTED_BYTES:
            raise _Refusal("unsafe_archive")
    for key in files:
        for depth in range(1, len(key)):
            if key[:depth] in files:
                raise _Refusal("unsafe_archive")


def _write_member(root: Path, parts: tuple[str, ...], source: Any, mode: int | None) -> None:
    target = root.joinpath(*parts)
    # Defesa em profundidade: o caminho final continua dentro do temporário de extração.
    if root.resolve() not in target.resolve().parents:
        raise _Refusal("unsafe_archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as handle:
        shutil.copyfileobj(source, handle, CHUNK_BYTES)
    if mode is not None and os.name == "posix":
        os.chmod(target, mode & 0o755)


def _extract_zip(archive: Path, root: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        plan: _Plan = []
        for info in infos:
            unix_mode = (info.external_attr >> 16) & 0o177777
            if unix_mode and stat.S_IFMT(unix_mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise _Refusal("unsafe_archive")  # link simbólico ou especial gravado no zip
            plan.append((_safe_parts(info.filename), info.is_dir(), info.file_size))
        _check_plan(plan)
        for info, (parts, is_dir, _) in zip(infos, plan):
            if is_dir:
                root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                continue
            unix_mode = (info.external_attr >> 16) & 0o777
            with bundle.open(info) as source:
                _write_member(root, parts, source, unix_mode or None)


def _extract_tar(archive: Path, root: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        plan: _Plan = []
        for member in members:
            if not (member.isreg() or member.isdir()):
                raise _Refusal("unsafe_archive")  # links, dispositivos, FIFOs
            plan.append((_safe_parts(member.name), member.isdir(), member.size))
        _check_plan(plan)
        for member, (parts, is_dir, _) in zip(members, plan):
            if is_dir:
                root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise _Refusal("unsafe_archive")
            with source:
                _write_member(root, parts, source, member.mode)


def _extract(archive: Path, root: Path) -> None:
    root.mkdir()
    try:
        if archive.name.endswith(".zip"):
            _extract_zip(archive, root)
        else:
            _extract_tar(archive, root)
    except _Refusal:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise _Refusal("internal") from exc


# --------------------------------------------------------------------------------------------------
# Instalação no destino
# --------------------------------------------------------------------------------------------------


def _install(dest: Path, staging: Path, archive: Path, extracted: Path) -> None:
    """Põe o arquivo e as entradas de topo da extração no destino, trocando as de uma obtenção anterior (que vão
    para o temporário e somem com ele)."""
    previous = staging / "anterior"
    previous.mkdir()
    for entry in [*sorted(extracted.iterdir()), archive]:
        final = dest / entry.name
        if final.exists() or final.is_symlink():
            os.replace(final, previous / entry.name)
        os.replace(entry, final)


def fetch(platform: str, dest: Path, *, opener: Opener = urllib.request.urlopen) -> int:
    """Baixa, confere e extrai o age de `platform` em `dest`; escreve a mensagem fixa e devolve o código de
    `FETCH_MESSAGES` (ver o docstring do módulo)."""
    if platform not in AGE_ASSETS:
        return _emit("unknown_platform")
    dest = Path(dest)
    try:
        if not str(dest) or _is_inside_repository(dest) or (dest.exists() and not dest.is_dir()):
            return _emit("invalid_arguments")
    except (OSError, ValueError, RuntimeError):
        return _emit("invalid_arguments")

    asset, expected_sha = AGE_ASSETS[platform]
    url = AGE_RELEASE_URL.format(version=AGE_VERSION, asset=asset)
    created_dest = False
    staging: Path | None = None
    key = "internal"
    try:
        if not dest.exists():
            dest.mkdir(parents=True)
            created_dest = True
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest))
        archive = staging / asset
        if _download(url, archive, opener) != expected_sha:
            raise _Refusal("checksum_mismatch")
        extracted = staging / "extracao"
        _extract(archive, extracted)
        _install(dest, staging, archive, extracted)
        key = "ok"
    except _Refusal as refusal:
        key = refusal.key
    except Exception:  # noqa: BLE001 - mensagem fixa, sem eco da exceção
        key = "internal"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if key != "ok" and created_dest:
            try:
                dest.rmdir()  # só se ficou vazio
            except OSError:
                pass
    return _emit(key)


# --------------------------------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------------------------------


def _parse(argv: Sequence[str]) -> dict[str, str] | None:
    """`{"platform": …, "dest": …}` de `--platform X --dest Y` (também `--opção=valor`); `None` se inválido."""
    values: dict[str, str] = {}
    items = list(argv)
    index = 0
    while index < len(items):
        item = items[index]
        if item.startswith("--") and "=" in item:
            name, value = item[2:].split("=", 1)
            index += 1
        elif item in ("--platform", "--dest") and index + 1 < len(items):
            name, value = item[2:], items[index + 1]
            index += 2
        else:
            return None
        if name not in ("platform", "dest") or name in values:
            return None
        values[name] = value
    if set(values) != {"platform", "dest"} or not values["dest"]:
        return None
    return values


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        values = _parse(sys.argv[1:] if argv is None else argv)
        if values is None or values["platform"] not in AGE_ASSETS:
            return _emit("invalid_arguments")
        return fetch(values["platform"], Path(values["dest"]))
    except Exception:  # noqa: BLE001 - mensagem fixa, sem eco da exceção
        return _emit("internal")


if __name__ == "__main__":
    sys.exit(main())
