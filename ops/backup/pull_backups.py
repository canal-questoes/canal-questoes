"""Cópia dos dumps cifrados da VM para o Windows do Pedro.


Uso:

    python ops/backup/pull_backups.py --host <vm> --identity <chave dedicada> --remote-dir /dumps
           --local-dir <pasta> [--user cqbackup] [--keep-days 14]

Busca o `SHA256SUMS` num temporário; baixa cada `cq-*.dump.age` ausente na cópia para `.part`, confere o SHA-256 e
renomeia (diverge ou fora do resumo → apaga o `.part`); poda a cópia local por carimbo do nome. Nunca apaga
nada na VM: o `SftpTransport` só emite `ls -1 <dir>` e `get <dir>/<nome> <local>`, pelo comando
`sftp -b - -o BatchMode=yes -o IdentitiesOnly=yes -i <chave> <user>@<host>`. O `last-backup.json` não é
copiado.

Saídas: o conjunto fechado `PULL_MESSAGES`; a `ok` vai ao stdout e as demais, uma linha fixa no stderr, sem eco de
argumento, caminho, nome de arquivo nem saída do `sftp`.

Prazos: cada lote do `sftp` tem prazo (`SFTP_LIST_TIMEOUT_SECONDS` no `ls`,
`SFTP_GET_TIMEOUT_SECONDS` em cada `get`), pelo `communicate` com `timeout=`;
vencido, o `sftp` é morto e recolhido (`KILL_GRACE_SECONDS`) e a cópia sai como a VM
que não respondeu (`TransportError` → `unreachable`), com o `.part` da vez apagado.

Só biblioteca padrão. Importável sem efeito colateral.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Protocol

# Chave → (código de saída, mensagem). `{n}` (na `ok`) é o único valor interpolado.
PULL_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Cópia atualizada: {n} arquivo(s) novo(s)."),
    "checksum_mismatch": (65, "Um arquivo copiado não confere com o resumo e foi descartado."),
    "unreachable": (69, "A VM não respondeu pela ligação SSH."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "internal": (1, "Erro interno da cópia."),
}

# Padrões dos parâmetros.
DEFAULT_USER = "cqbackup"
DEFAULT_KEEP_DAYS = 14

# O nome de um dump. Cópia do `cq_backup.DUMP_NAME_PATTERN`: este script roda no Windows só com a
# biblioteca padrão e não importa o `cq_backup` (test_boundaries_deploy.py: os scripts de host só com ela).
DUMP_NAME_PATTERN = r"^cq-\d{8}T\d{6}Z\.dump\.age$"
SUMS_FILE = "SHA256SUMS"
PART_SUFFIX = ".part"

# O comando `sftp` e o prefixo das variáveis do produto, que não passam aos processos filhos.
SFTP_BIN = "sftp"
_PRODUCT_ENV_PREFIX = "CQ_"

# Prazos de cada lote do `sftp`: um `sftp` preso não pode deixar a cópia sem retornar. Vencido o
# prazo, o `sftp` é morto e a cópia sai com `unreachable` (a VM não respondeu pela ligação SSH).
# **[I]** O `ls -1` volta em segundos numa VM saudável, já contando o aperto de mão do SSH; 5 min cobrem uma ligação
# ruim sem prender a cópia.
SFTP_LIST_TIMEOUT_SECONDS = 300
# **[I]** Um `get` traz um dump do piloto (dezenas de MB, estimativa sem medição) ou o `SHA256SUMS`; numa ligação
# doméstica de 1 MB/s, um dump de 100 MB leva menos de 2 min. 30 min por arquivo dão folga larga.
SFTP_GET_TIMEOUT_SECONDS = 1800
# **[I]** Depois do `kill`, o `sftp` morre em milissegundos; 10 s de espera bastam para recolhê-lo sem que a própria
# limpeza fique presa (o `ssh` que ele abriu pode segurar o pipe do stderr mais um pouco: a espera tem prazo).
KILL_GRACE_SECONDS = 10

# Uma linha do `sha256sum` (modo texto `"  "` ou binário `" *"`).
_SUMS_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
# [I] Usuário e máquina sem espaço e sem `-` no início (o `<user>@<host>` não pode virar opção do `sftp`).
_USER = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_HOST = re.compile(r"^[A-Za-z0-9._:\[\]][A-Za-z0-9._:\[\]-]*$")
_KEEP_DAYS = re.compile(r"^[0-9]+$")

# A forma do `cq_backup.Spawn` (`spawn(argv, *, env, stdin, stdout, stderr) -> ProcessLike`).
Spawn = Callable[..., Any]


class Transport(Protocol):
    """A porta da cópia: listar os nomes do diretório remoto e baixar um deles."""

    def list_names(self) -> list[str]: ...

    def fetch(self, name: str, dest: Path) -> None: ...


class TransportError(Exception):
    """A ligação com a VM falhou (o `sftp` saiu com código ≠ 0). A mensagem é fixa: nunca leva a saída do
    `sftp`, caminho, máquina nem chave."""

    def __init__(self) -> None:
        super().__init__("A ligação SFTP falhou.")


@dataclass(frozen=True)
class PullResult:
    """O resultado de uma cópia: os nomes novos, os descartados (resumo divergente) e os podados."""

    new: list[str]
    discarded: list[str]
    pruned: list[str]


def _is_dump_name(name: str) -> bool:
    return re.fullmatch(DUMP_NAME_PATTERN, name) is not None


def _dump_stamp(name: str) -> datetime | None:
    """O carimbo (UTC) de um nome de dump; `None` para nome fora do padrão ou data impossível."""
    if not _is_dump_name(name):
        return None
    try:
        return datetime.strptime(name[3:19], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _plain_name(name: str) -> bool:
    """Um nome-base simples (sem diretório), para nunca sair do diretório remoto nem do local."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _child_env() -> dict[str, str]:
    """O ambiente do `sftp`: o do processo sem as variáveis `CQ_*` do produto."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith(_PRODUCT_ENV_PREFIX)}


def _stop(process: Any) -> None:
    """Encerra o `sftp` que passou do prazo ou que uma falha deixou para trás: `kill` se ainda roda, um `communicate`
    com prazo que recolhe e fecha os pipes do `communicate` vencido (a receita do `Popen.communicate` na
    documentação, aqui com prazo, porque o `ssh` filho do `sftp` pode segurar o stderr) e uma espera curta
    (`KILL_GRACE_SECONDS`). Melhor esforço: nunca levanta nem fica preso."""
    try:
        if process.poll() is None:
            process.kill()
    except Exception:  # noqa: BLE001 - limpeza de melhor esforço; a saída já é a do passo
        pass
    try:
        process.communicate(timeout=KILL_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 - idem
        pass
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 - idem
        pass


class SftpTransport:
    """O `Transport` real, pelo `sftp -b -` com o lote no stdin."""

    def __init__(self, host: str, user: str, identity: Path, remote_dir: str, *, spawn: Spawn | None = None) -> None:
        self.host = host
        self.user = user
        self.identity = identity
        self.remote_dir = remote_dir
        self.spawn = spawn

    def _argv(self) -> list[str]:
        return [SFTP_BIN, "-b", "-", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", str(self.identity),
                f"{self.user}@{self.host}"]

    def _remote_base(self) -> str:
        return self.remote_dir.rstrip("/") or "/"

    def _run(self, command: str, timeout: float) -> bytes:
        """Roda um lote de um comando só e devolve o stdout; código ≠ 0 → `TransportError`. O stderr do `sftp` é
        lido (para não travar o pipe) e descartado. O `communicate` tem o prazo `timeout`; vencido, o `sftp` é morto
        e recolhido e a falha é a mesma da ligação: `TransportError`."""
        spawn = self.spawn if self.spawn is not None else subprocess.Popen
        process = spawn(self._argv(), env=_child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
        try:
            out, _err = process.communicate((command + "\n").encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(process)
            raise TransportError() from None
        except BaseException:
            _stop(process)
            raise
        if process.returncode != 0:
            raise TransportError()
        return out or b""

    def list_names(self) -> list[str]:
        """Os nomes-base que casam `DUMP_NAME_PATTERN` na saída do `ls -1 <dir>` (no lote, cada comando ecoado como
        `sftp> …`, cada nome prefixado por `<dir>/`; linhas fora do padrão ignoradas)."""
        out = self._run(f"ls -1 {shlex.quote(self._remote_base())}", SFTP_LIST_TIMEOUT_SECONDS)
        names: set[str] = set()
        for raw in out.decode("utf-8", errors="replace").splitlines():
            # [I] `strip()` também tira o `\r` que o sftp do Windows pode deixar.
            line = raw.strip()
            if not line or line.startswith("sftp>"):
                continue
            base = line.rsplit("/", 1)[-1]
            if _is_dump_name(base):
                names.add(base)
        return sorted(names)

    def fetch(self, name: str, dest: Path) -> None:
        """Baixa `<dir>/<nome>` para `dest` com um lote só de `get`."""
        if not _plain_name(name):
            raise ValueError("nome de arquivo remoto inválido")
        base = self._remote_base()
        source = f"{base}{name}" if base.endswith("/") else f"{base}/{name}"
        self._run(f"get {shlex.quote(source)} {shlex.quote(str(dest))}", SFTP_GET_TIMEOUT_SECONDS)

    def __repr__(self) -> str:
        # Sem o caminho da chave.
        return f"SftpTransport(host={self.host!r}, user={self.user!r}, remote_dir={self.remote_dir!r})"


class LocalDirTransport:
    """O `Transport` dos testes: um diretório local no lugar da VM."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def list_names(self) -> list[str]:
        """Os nomes dos arquivos do diretório que casam `DUMP_NAME_PATTERN`, como o `SftpTransport`."""
        return sorted(entry.name for entry in Path(self.path).iterdir()
                      if entry.is_file() and _is_dump_name(entry.name))

    def fetch(self, name: str, dest: Path) -> None:
        if not _plain_name(name):
            raise ValueError("nome de arquivo inválido")
        shutil.copyfile(Path(self.path) / name, dest)

    def __repr__(self) -> str:
        return "LocalDirTransport()"


def _read_sums(path: Path) -> dict[str, str]:
    """Nome → SHA-256 (minúsculo) das linhas válidas do `SHA256SUMS`; linhas fora do formato são ignoradas (o dump
    que ficar sem resumo é descartado como divergente)."""
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SUMS_LINE.match(line.rstrip("\r"))
        if match is not None:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prune_local(local_dir: Path, cutoff: datetime) -> list[str]:
    """Apaga da cópia local os dumps com carimbo do nome anterior a `cutoff` (nunca pelo `mtime`; nomes fora do
    padrão ficam)."""
    pruned: list[str] = []
    for entry in sorted(local_dir.iterdir(), key=lambda item: item.name):
        stamp = _dump_stamp(entry.name)
        if stamp is not None and stamp < cutoff and entry.is_file():
            entry.unlink()
            pruned.append(entry.name)
    return pruned


def pull(transport: Transport, local_dir: Path, keep_days: int, now: datetime) -> PullResult:
    """Uma cópia (o algoritmo abaixo).

    Lista os dumps da origem; para cada um ausente na cópia, baixa para `<nome>.part` no diretório local, confere o
    SHA-256 contra o `SHA256SUMS` (buscado num temporário fora da cópia) e renomeia com `os.replace`; diverge ou não
    está no resumo → apaga o `.part` e marca o nome em `discarded`. Por fim, poda a cópia local pelo carimbo do nome
    (mais velho que `keep_days`). Uma falha do transporte sobe (`TransportError`), sem poda; o `.part` da vez é
    removido sempre."""
    local_dir = Path(local_dir)
    cutoff = now - timedelta(days=keep_days)
    # [I] Um dump que a poda local apagaria logo em seguida não é baixado (evita baixar e apagar a cada execução
    # quando a VM guarda mais dias que a cópia).
    candidates = []
    for name in sorted(set(transport.list_names())):
        if not _is_dump_name(name) or (local_dir / name).exists():
            continue
        stamp = _dump_stamp(name)
        if stamp is not None and stamp < cutoff:
            continue
        candidates.append(name)

    new: list[str] = []
    discarded: list[str] = []
    if candidates:
        with tempfile.TemporaryDirectory(prefix="cq-pull-") as scratch:
            sums_path = Path(scratch) / SUMS_FILE
            transport.fetch(SUMS_FILE, sums_path)
            sums = _read_sums(sums_path)
        for name in candidates:
            part = local_dir / (name + PART_SUFFIX)
            try:
                transport.fetch(name, part)
                expected = sums.get(name)
                if expected is not None and _sha256_file(part) == expected:
                    os.replace(part, local_dir / name)
                    new.append(name)
                else:
                    discarded.append(name)
            finally:
                part.unlink(missing_ok=True)

    pruned = _prune_local(local_dir, cutoff)
    return PullResult(new=new, discarded=discarded, pruned=pruned)


class _InvalidArguments(Exception):
    """Argumentos inválidos (nunca ecoados)."""


class _QuietParser(argparse.ArgumentParser):
    """O `argparse` sem impressão: qualquer erro vira `_InvalidArguments`, sem eco do argumento."""

    def error(self, message: str) -> NoReturn:
        raise _InvalidArguments()

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _InvalidArguments()


def _parser() -> _QuietParser:
    parser = _QuietParser(prog="pull_backups.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--keep-days", default=str(DEFAULT_KEEP_DAYS))
    return parser


@dataclass(frozen=True)
class _Args:
    host: str
    user: str
    identity: Path
    remote_dir: str
    local_dir: Path
    keep_days: int


def _parse(argv: Sequence[str]) -> _Args:
    namespace = _parser().parse_args(list(argv))
    keep = namespace.keep_days
    if not _KEEP_DAYS.fullmatch(keep) or int(keep) < 1:
        raise _InvalidArguments()
    if not _HOST.fullmatch(namespace.host) or not _USER.fullmatch(namespace.user):
        raise _InvalidArguments()
    if not namespace.identity or not namespace.remote_dir or not namespace.local_dir:
        raise _InvalidArguments()
    return _Args(host=namespace.host, user=namespace.user, identity=Path(namespace.identity),
                 remote_dir=namespace.remote_dir, local_dir=Path(namespace.local_dir), keep_days=int(keep))


def _fail(key: str) -> int:
    code, message = PULL_MESSAGES[key]
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de `PULL_MESSAGES`."""
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except _InvalidArguments:
        return _fail("invalid_arguments")
    try:
        args.local_dir.mkdir(parents=True, exist_ok=True)
        transport = SftpTransport(args.host, args.user, args.identity, args.remote_dir)
        result = pull(transport, args.local_dir, args.keep_days, datetime.now(UTC))
    except TransportError:
        return _fail("unreachable")
    except Exception:  # noqa: BLE001 - qualquer outra falha sai com a mensagem fixa, sem o texto da exceção
        return _fail("internal")
    if result.discarded:
        return _fail("checksum_mismatch")
    code, message = PULL_MESSAGES["ok"]
    sys.stdout.write(message.format(n=len(result.new)) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
