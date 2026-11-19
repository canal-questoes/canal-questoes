"""Rotina de backup diário cifrado do Postgres da pilha.


Uso (na VM, pela unidade `cq-backup.service`):

    python3 ops/backup/cq_backup.py run --project <p> --recipients <arquivo> --dumps-dir <d> --state-dir <e>
            [--keep-days 7] [--age-bin age] [--docker-bin docker]

Fluxo: acha o contêiner do Postgres do projeto (`docker ps`), faz o dump em fluxo
(`docker exec -i <id> pg_dump -U cq -d cq -Fc`) direto para o `age -R <destinatários> -o <temporário>`,
renomeia o temporário para `cq-<AAAAMMDDTHHMMSSZ>.dump.age`, grava `last-backup.json` e `SHA256SUMS` e poda
os dumps mais velhos que `--keep-days`. Nenhum dump em claro toca o disco.

Só biblioteca padrão; Python ≥ 3.12; roda no POSIX e no Windows. Importável sem efeito colateral: tudo
atrás de `main(argv=None) -> int` e de `if __name__ == "__main__"`.

Sequência de uma execução: trava → órfãos `.tmp-*` apagados → destinatários validados → contêiner
achado → dump cifrado no temporário → cabeçalho conferido → `os.replace` para o nome final →
`SHA256SUMS` e `last-backup.json` atômicos → poda pelo carimbo do nome. Qualquer falha antes do
nome final remove o temporário e não toca no `SHA256SUMS` nem no registro do backup anterior.

Prazos: nenhuma espera de processo filho é sem prazo. O `docker ps` tem
`DOCKER_CHECK_TIMEOUT_SECONDS` e o par `pg_dump` → `age`, junto, `DUMP_TIMEOUT_SECONDS`; ao
vencer, os filhos são mortos (`kill`) e esperados por `KILL_GRACE_SECONDS`, e a execução
sai com a falha do passo (`container_not_found`, `dump_failed` ou `encrypt_failed`), com o
temporário apagado e a trava liberada como em qualquer outra falha.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Protocol, TextIO

# O conjunto fechado das saídas, chave → (código de saída, mensagem). A `ok` vai ao stdout;
# as demais, uma linha no stderr, sem eco de caminho, nome ou saída de processo.
BACKUP_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Backup concluído."),
    "busy": (75, "Outro backup está em andamento."),
    "recipients_invalid": (78, "O arquivo de destinatários do age está ausente ou inválido."),
    "container_not_found": (69, "O contêiner do Postgres do projeto não foi encontrado em execução."),
    "dump_failed": (70, "O dump do banco falhou."),
    "encrypt_failed": (70, "A cifra do dump falhou."),
    "write_failed": (73, "Não foi possível gravar o backup no disco."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "internal": (1, "Erro interno do backup."),
}

# O nome do dump, o cabeçalho de um arquivo do age e o tamanho máximo do registro.
DUMP_NAME_PATTERN = r"^cq-\d{8}T\d{6}Z\.dump\.age$"
AGE_HEADER = b"age-encryption.org/v1"
RECORD_MAX_BYTES = 4096

# Nomes fixos dos arquivos do diretório de estado e do diretório dos dumps.
RECORD_FILE = "last-backup.json"
SUMS_FILE = "SHA256SUMS"
LOCK_FILE = "backup.lock"
RECORD_VERSION = 1

# Padrões dos parâmetros (`--project`, `--keep-days`, `--age-bin`, `--docker-bin`).
DEFAULT_PROJECT = "cq"
DEFAULT_KEEP_DAYS = 7

# Prazos das esperas dos processos filhos. Um filho preso não pode deixar a rotina sem retornar e
# com a trava ocupada: ao vencer o prazo, o filho é morto e a execução sai com a falha do passo.
# **[I]** O `docker ps` responde em menos de um segundo num daemon saudável; 2 min cobrem um daemon lento sem prender
# a rotina. Vencido, vale o mesmo que o `docker ps` em erro: `container_not_found`.
DOCKER_CHECK_TIMEOUT_SECONDS = 120
# **[I]** O banco do piloto (uma turma) deve ficar na casa das dezenas de MB, e o `pg_dump -Fc` cifrado pelo `age`
# leva segundos ou poucos minutos na VM; 1 h para o par inteiro dá folga de mais de dez vezes e ainda solta a trava
# muito antes do timer do dia seguinte. Vencido: `dump_failed` (o dump não terminou), ou `encrypt_failed` quando o
# `age` já saiu em erro ou é o único preso, com o `pg_dump` concluído.
DUMP_TIMEOUT_SECONDS = 3600
# **[I]** Depois do `kill`, um filho morre em milissegundos; 10 s de espera bastam para recolhê-lo sem que a própria
# limpeza fique presa (se nem assim terminar, a rotina segue para a saída de falha mesmo assim).
KILL_GRACE_SECONDS = 10


class RecipientsInvalid(Exception):
    """O arquivo de destinatários do age está ausente, vazio ou com linha fora de `age1…`, `#…` ou vazia
    (a regra do `parse_recipients`). A mensagem nunca traz o conteúdo do arquivo."""


class ProcessLike(Protocol):
    """O que a rotina, a restauração e a cópia usam de um processo filho (a forma do `subprocess.Popen`): as
    esperas sempre com prazo (`timeout=`, que levanta `subprocess.TimeoutExpired`), e `poll`/`kill` para encerrar
    o filho que passou do prazo."""

    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int: ...

    def communicate(self, input: bytes | None = None,
                    timeout: float | None = None) -> tuple[bytes | None, bytes | None]: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


# O lançador de processos: chamado sempre por nome, `spawn(argv, *, env, stdin, stdout, stderr)`; o padrão do
# produto é o `subprocess.Popen`.
Spawn = Callable[..., ProcessLike]


@dataclass(frozen=True)
class BackupConfig:
    """Os parâmetros de uma execução; tudo por argumento, nada do ambiente."""

    project: str
    recipients: Path
    dumps_dir: Path
    state_dir: Path
    keep_days: int = DEFAULT_KEEP_DAYS
    age_bin: str = "age"
    docker_bin: str = "docker"


@dataclass(frozen=True)
class BackupRecord:
    """O registro `last-backup.json`: exatamente estas chaves."""

    version: int
    finished_at: str
    file: str
    size_bytes: int
    sha256: str


def run(config: BackupConfig, *, spawn: Spawn | None = None, now: Callable[[], datetime] | None = None,
        stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Uma execução da rotina (estados `locked → … → ok`); devolve o código de
    `BACKUP_MESSAGES`. `spawn=None` → `subprocess.Popen`; `now=None` → agora em UTC; `stdout`/`stderr`
    `None` → os do processo, resolvidos na chamada."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    spawn_fn: Spawn = spawn if spawn is not None else subprocess.Popen
    clock: Callable[[], datetime] = now if now is not None else _utc_now
    try:
        key = _run_under_lock(config, spawn_fn, clock)
    except Exception:  # noqa: BLE001 - qualquer imprevisto vira a linha fixa `internal`, sem eco
        key = "internal"
    code, message = BACKUP_MESSAGES[key]
    stream = out if key == "ok" else err
    stream.write(message + "\n")
    stream.flush()
    return code


def parse_recipients(text: str) -> tuple[str, ...]:
    """Os destinatários `age1…` do texto, na ordem; `#…` e linhas vazias ignoradas; nenhum ou linha
    inválida → `RecipientsInvalid`.

    Regra do age 1.1.1: quebra de linha universal; linha vazia só se não sobrar
    nada depois de tirar a quebra (uma linha de espaços é inválida); comentário só com `#` na primeira
    coluna; destinatário `age1` seguido só do alfabeto bech32 (que não tem outro `1`). Chave SSH, de plugin
    ou privada é recusada."""
    recipients: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "" or line.startswith("#"):
            continue
        if _RECIPIENT_RE.fullmatch(line) is None:
            raise RecipientsInvalid(_RECIPIENTS_INVALID_TEXT)
        recipients.append(line)
    if not recipients:
        raise RecipientsInvalid(_RECIPIENTS_INVALID_TEXT)
    return tuple(recipients)


def dump_name(at: datetime) -> str:
    """`cq-<AAAAMMDDTHHMMSSZ>.dump.age` do instante `at` (UTC)."""
    return f"cq-{_stamp(at)}.dump.age"


def parse_dump_name(name: str) -> datetime | None:
    """O carimbo (UTC) de um nome que casa `DUMP_NAME_PATTERN`; `None` para qualquer outro nome."""
    if _DUMP_NAME_RE.fullmatch(name) is None:
        return None
    try:
        parsed = datetime.strptime(name[3:18], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def prune(dumps_dir: Path, keep_days: int, now: datetime, keep: str) -> list[str]:
    """Apaga os dumps com carimbo mais velho que `keep_days` (nunca `keep`, o dump recém-gravado) e
    devolve os nomes apagados."""
    try:
        cutoff = now.astimezone(UTC) - timedelta(days=keep_days)
    except OverflowError:
        # Um prazo maior que o calendário: nada é velho o bastante.
        return []
    removed: list[str] = []
    for path in sorted(dumps_dir.iterdir(), key=lambda item: item.name):
        if path.name == keep:
            continue
        stamp = parse_dump_name(path.name)
        if stamp is None or stamp >= cutoff or not path.is_file():
            continue
        path.unlink()
        removed.append(path.name)
    return removed


def write_record(state_dir: Path, record: BackupRecord) -> None:
    """Grava `last-backup.json` por temporário no mesmo diretório + `fsync` + `os.replace` (modo 0644)."""
    data = (json.dumps(dataclasses.asdict(record), ensure_ascii=True) + "\n").encode("ascii")
    _atomic_write(state_dir, RECORD_FILE, data, 0o644)


def write_sums(dumps_dir: Path) -> None:
    """Regrava `SHA256SUMS` (formato do `sha256sum`, uma linha por dump retido, ordem por nome; 0640)."""
    lines = []
    for path in sorted(dumps_dir.iterdir(), key=lambda item: item.name):
        if parse_dump_name(path.name) is None or not path.is_file():
            continue
        digest = _digest_file(path)
        lines.append(f"{digest}  {path.name}\n")
    _atomic_write(dumps_dir, SUMS_FILE, "".join(lines).encode("ascii"), 0o640)


def read_record(path: Path) -> BackupRecord | None:
    """O registro de `path`, validado; `None` em qualquer desvio."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(RECORD_MAX_BYTES + 1)
    except OSError:
        return None
    if not raw or len(raw) > RECORD_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != _RECORD_KEYS:
        return None
    version, finished_at, file, size_bytes, sha256 = (data[key] for key in _RECORD_FIELDS)
    if type(version) is not int or version != RECORD_VERSION:
        return None
    if not isinstance(finished_at, str) or _FINISHED_AT_RE.fullmatch(finished_at) is None:
        return None
    try:
        datetime.strptime(finished_at, _FINISHED_AT_FORMAT)
    except ValueError:
        return None
    if not isinstance(file, str) or parse_dump_name(file) is None:
        return None
    if type(size_bytes) is not int or size_bytes <= 0:
        return None
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        return None
    return BackupRecord(version=version, finished_at=finished_at, file=file, size_bytes=size_bytes,
                        sha256=sha256)


def acquire_lock(state_dir: Path) -> AbstractContextManager[bool]:
    """A trava `backup.lock` (`flock` no POSIX, `msvcrt` no Windows); o contexto dá `False` se ocupada."""
    return _lock(state_dir / LOCK_FILE)


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando (`run …`); devolve o código de saída."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    except _InvalidArguments:
        code, message = BACKUP_MESSAGES["invalid_arguments"]
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        return code
    config = BackupConfig(project=args.project, recipients=args.recipients, dumps_dir=args.dumps_dir,
                          state_dir=args.state_dir, keep_days=args.keep_days, age_bin=args.age_bin,
                          docker_bin=args.docker_bin)
    return run(config)


# --------------------------------------------------------------------------------------------------
# Detalhes
# --------------------------------------------------------------------------------------------------

_DUMP_NAME_RE = re.compile(DUMP_NAME_PATTERN, re.ASCII)
# age 1.1.1: `age1` + alfabeto bech32 (sem `1`, `b`, `i`, `o`).
_RECIPIENT_RE = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]+", re.ASCII)
_RECIPIENTS_INVALID_TEXT = "Arquivo de destinatários do age inválido."
_RECIPIENTS_MAX_BYTES = 1024 * 1024
_FINISHED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
_FINISHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}", re.ASCII)
_RECORD_FIELDS = tuple(field.name for field in dataclasses.fields(BackupRecord))
_RECORD_KEYS = frozenset(_RECORD_FIELDS)
_TMP_PREFIX = ".tmp-"
_CHUNK = 1024 * 1024


class _Failure(Exception):
    """Uma saída de `BACKUP_MESSAGES` por chave (uso interno; nunca carrega texto de fora)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _InvalidArguments(Exception):
    """Linha de comando inválida (o `argparse` não imprime nada)."""


class _Parser(argparse.ArgumentParser):
    """`argparse` sem eco: todo erro vira `_InvalidArguments` (nem `usage`, nem a mensagem do argparse)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _InvalidArguments

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise _InvalidArguments


def _keep_days(value: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError
    days = int(value)
    if days < 1:
        raise ValueError
    return days


def _build_parser() -> _Parser:
    parser = _Parser(prog="cq_backup.py", add_help=False, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", add_help=False, allow_abbrev=False)
    run_parser.add_argument("--project", default=DEFAULT_PROJECT)
    run_parser.add_argument("--recipients", type=Path, required=True)
    run_parser.add_argument("--dumps-dir", type=Path, required=True)
    run_parser.add_argument("--state-dir", type=Path, required=True)
    run_parser.add_argument("--keep-days", type=_keep_days, default=DEFAULT_KEEP_DAYS)
    run_parser.add_argument("--age-bin", default="age")
    run_parser.add_argument("--docker-bin", default="docker")
    return parser


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("chave repetida")
        result[key] = value
    return result


def _child_env() -> dict[str, str]:
    """O ambiente dos filhos: o do processo sem nenhuma `CQ_*`."""
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("CQ_")}


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[bool]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        held = _try_lock(fd)
        try:
            yield held
        finally:
            if held:
                _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _run_under_lock(config: BackupConfig, spawn: Spawn, clock: Callable[[], datetime]) -> str:
    """`locked` e o resto da sequência; devolve a chave de `BACKUP_MESSAGES`."""
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(acquire_lock(config.state_dir))
        except OSError:
            return "write_failed"
        if not held:
            return "busy"
        try:
            _backup(config, spawn, clock)
        except _Failure as failure:
            return failure.key
        return "ok"


def _remove_orphans(directory: Path) -> None:
    """Apaga os `.tmp-*` deixados por uma execução interrompida (sob a trava)."""
    for path in directory.iterdir():
        if path.name.startswith(_TMP_PREFIX) and path.is_file():
            path.unlink()


def _read_recipients(path: Path) -> tuple[str, ...]:
    try:
        with open(path, encoding="utf-8", newline=None) as handle:
            text = handle.read(_RECIPIENTS_MAX_BYTES + 1)
    except (OSError, UnicodeDecodeError, ValueError):
        raise RecipientsInvalid(_RECIPIENTS_INVALID_TEXT) from None
    if len(text) > _RECIPIENTS_MAX_BYTES:
        raise RecipientsInvalid(_RECIPIENTS_INVALID_TEXT)
    return parse_recipients(text)


def _backup(config: BackupConfig, spawn: Spawn, clock: Callable[[], datetime]) -> None:
    try:
        _remove_orphans(config.dumps_dir)
        _remove_orphans(config.state_dir)
    except OSError:
        raise _Failure("write_failed") from None
    try:
        _read_recipients(config.recipients)
    except RecipientsInvalid:
        raise _Failure("recipients_invalid") from None

    env = _child_env()
    container = _find_container(config, spawn, env)

    started = clock()
    final_name = dump_name(started)
    final = config.dumps_dir / final_name
    temporary = config.dumps_dir / f"{_TMP_PREFIX}{_stamp(started)}-{os.getpid()}.age"
    renamed = False
    try:
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            os.close(fd)
        except OSError:
            raise _Failure("write_failed") from None
        _dump_to(config, spawn, env, container, temporary)
        size, digest = _seal(temporary)
        try:
            os.replace(temporary, final)
        except OSError:
            raise _Failure("write_failed") from None
        renamed = True
    finally:
        if not renamed:
            with contextlib.suppress(OSError):
                temporary.unlink()

    # Depois de `renamed`: uma falha deixa o dump novo sem registro (o próximo sucesso o inclui).
    try:
        _fsync_dir(config.dumps_dir)
        write_sums(config.dumps_dir)
        finished = clock().astimezone(UTC)
        write_record(config.state_dir, BackupRecord(version=RECORD_VERSION,
                                                    finished_at=finished.strftime(_FINISHED_AT_FORMAT),
                                                    file=final_name, size_bytes=size, sha256=digest))
        # `pruned`: só depois de `state_written`; o resumo é refeito sem os podados.
        if prune(config.dumps_dir, config.keep_days, finished, final_name):
            write_sums(config.dumps_dir)
    except OSError:
        raise _Failure("write_failed") from None


def _find_container(config: BackupConfig, spawn: Spawn, env: dict[str, str]) -> str:
    """`container_found`: exatamente um contêiner do `postgres` do projeto em execução."""
    argv = [config.docker_bin, "ps", "-q", "--filter", f"label=com.docker.compose.project={config.project}",
            "--filter", "label=com.docker.compose.service=postgres", "--filter", "status=running"]
    try:
        process = spawn(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL)
    except OSError:
        raise _Failure("container_not_found") from None
    # `communicate` com prazo lê o stdout e espera o fim sem ficar preso na leitura do pipe (docs.python.org/3.12,
    # `Popen.communicate`); vencido, o `docker ps` em atraso vale como o `docker ps` em erro.
    try:
        output, _ = process.communicate(timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(process, drain=True)
        raise _Failure("container_not_found") from None
    except BaseException:
        _stop(process, drain=True)
        raise
    output = output if isinstance(output, bytes) else b""
    code = process.returncode
    if code != 0:
        raise _Failure("container_not_found")
    try:
        ids = [line.strip() for line in output.decode("ascii").splitlines() if line.strip()]
    except UnicodeDecodeError:
        raise _Failure("container_not_found") from None
    if len(ids) != 1 or _CONTAINER_ID_RE.fullmatch(ids[0]) is None:
        raise _Failure("container_not_found")
    return ids[0]


def _dump_to(config: BackupConfig, spawn: Spawn, env: dict[str, str], container: str,
             temporary: Path) -> None:
    """`dumped_to_temp`: `pg_dump` → pipe → `age -o <temporário>`, os dois códigos conferidos."""
    dump_argv = [config.docker_bin, "exec", "-i", container, "pg_dump", "-U", "cq", "-d", "cq", "-Fc"]
    age_argv = [config.age_bin, "-R", str(config.recipients), "-o", str(temporary)]
    try:
        dumper = spawn(dump_argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    except OSError:
        raise _Failure("dump_failed") from None
    try:
        encrypter = spawn(age_argv, env=env, stdin=dumper.stdout, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    except OSError:
        if dumper.stdout is not None:
            dumper.stdout.close()
        _stop(dumper)
        raise _Failure("encrypt_failed") from None
    # A receita do pipe (docs.python.org, "Replacing shell pipeline"): o pai fecha a sua ponta, para o
    # `pg_dump` receber SIGPIPE se o `age` sair antes.
    if dumper.stdout is not None:
        dumper.stdout.close()
    # Um prazo só para o par (`DUMP_TIMEOUT_SECONDS`): o pai aqui só espera, sem ler pipe, então o `wait` com o que
    # resta do prazo basta para não ficar preso.
    deadline = time.monotonic() + DUMP_TIMEOUT_SECONDS
    try:
        age_code = encrypter.wait(timeout=_remaining(deadline))
        dump_code = dumper.wait(timeout=_remaining(deadline))
    except subprocess.TimeoutExpired:
        # O estado de cada um antes do `kill`: o `age` já em erro, ou preso sozinho com o `pg_dump` concluído, aponta
        # a cifra; qualquer outro caso é o dump que não terminou.
        age_state, dump_state = encrypter.poll(), dumper.poll()
        _stop(encrypter)
        _stop(dumper)
        if (age_state is not None and age_state != 0) or dump_state == 0:
            raise _Failure("encrypt_failed") from None
        raise _Failure("dump_failed") from None
    except BaseException:
        _stop(encrypter)
        _stop(dumper)
        raise
    # Com o `age` em erro, o `pg_dump` pode ter falhado só pelo pipe fechado: a causa é a cifra.
    if age_code != 0:
        raise _Failure("encrypt_failed")
    if dump_code != 0:
        raise _Failure("dump_failed")


def _remaining(deadline: float) -> float:
    """Os segundos que faltam até `deadline` (relógio monotônico), nunca negativos."""
    return max(0.0, deadline - time.monotonic())


def _stop(process: ProcessLike, *, drain: bool = False) -> None:
    """Encerra um filho que passou do prazo ou que uma falha deixou para trás: `kill` se ainda roda e uma espera
    curta (`KILL_GRACE_SECONDS`). Com `drain`, a espera é antes um `communicate` com prazo, que recolhe e fecha os
    pipes de um `communicate` vencido (a receita do `Popen.communicate` na documentação, aqui com prazo para não
    depender de um neto que segure o pipe). Melhor esforço: nunca levanta nem fica preso."""
    with contextlib.suppress(Exception):
        if process.poll() is None:
            process.kill()
    if drain:
        with contextlib.suppress(Exception):
            process.communicate(timeout=KILL_GRACE_SECONDS)
    with contextlib.suppress(Exception):
        process.wait(timeout=KILL_GRACE_SECONDS)


def _seal(temporary: Path) -> tuple[int, str]:
    """`fsync` do temporário, cabeçalho do age conferido, `0640`; devolve tamanho e SHA-256."""
    try:
        with open(temporary, "r+b") as handle:
            os.fsync(handle.fileno())
            handle.seek(0)
            head = handle.read(len(AGE_HEADER) + 1)
            if head != AGE_HEADER + b"\n":
                raise _Failure("encrypt_failed")
            hasher = hashlib.sha256(head)
            size = len(head)
            while chunk := handle.read(_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
        os.chmod(temporary, 0o640)
    except FileNotFoundError:
        raise _Failure("encrypt_failed") from None
    except OSError:
        raise _Failure("write_failed") from None
    return size, hasher.hexdigest()


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_dir(directory: Path) -> None:
    """`fsync` do diretório depois de um `os.replace` (só no POSIX; no Windows não há o equivalente)."""
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(directory: Path, name: str, data: bytes, mode: int) -> None:
    """Temporário no mesmo diretório + `fsync` + `chmod` + `os.replace` + `fsync` do diretório; o
    temporário não sobrevive a uma falha."""
    temporary = directory / f"{_TMP_PREFIX}{name}-{os.getpid()}"
    replaced = False
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, directory / name)
        replaced = True
        _fsync_dir(directory)
    finally:
        if not replaced:
            with contextlib.suppress(OSError):
                temporary.unlink()


if __name__ == "__main__":
    sys.exit(main())
