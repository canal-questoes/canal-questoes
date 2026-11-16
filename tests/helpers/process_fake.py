"""Processos falsos para a rotina de backup, a restauração e a cópia.


O produto chama `spawn(argv, *, env, stdin, stdout, stderr)` **por nome**, na forma do `subprocess.Popen`
(o padrão do produto); os pipes usam `stdin=<stdout do anterior>` e `stdout=PIPE` (docs.python.org/3.12,
"Replacing shell pipeline"). Aqui:

- `FakeProcess(stdout, returncode, stderr=b"")` cumpre o `ProcessLike` de `ops/backup/cq_backup.py`
  (`stdin`, `stdout`, `stderr`, `wait()`, `returncode`), mais `poll()`, `communicate()`, `kill()`,
  `terminate()`, `pid`, `args` e o uso em `with`. Como no `Popen`, `returncode` é `None` até o `wait()`
  (ou `poll()`/`communicate()`), e `stdout`/`stderr` só existem quando o `spawn` recebeu `PIPE`; com um
  arquivo, a saída é escrita nele; com `None` ou `DEVNULL`, descartada (e guardada no registro).
- `FakeSpawn` registra cada chamada (`calls`: argv, `env`, os três fluxos, o processo e os bytes lidos do
  stdin; `argvs`: só os argv, em listas, para conferir com `==`) e responde por **regras por prefixo de
  argv** (`on(...)` e os atalhos abaixo); `ANY` casa qualquer elemento; vale a regra de prefixo mais longo
  e, no empate, a última. Argv sem regra → `pytest.fail` (um `BaseException`: o `except Exception` do
  produto não o engole). Cada regra programa código de saída, stdout e stderr da etapa, ou `raises=` (a
  exceção do `spawn`, como o `FileNotFoundError` de um binário ausente).
- **Pipe:** o `stdout` de um falso é um `io.BytesIO`; o falso seguinte que o recebe como `stdin` o lê
  inteiro **no `spawn`** (assim o `p1.stdout.close()` da receita oficial, logo depois, não atrapalha).
  Com `stdin=PIPE`, o falso só roda quando o produto fecha o stdin, lê o stdout, chama `wait()` ou
  `communicate()` (o lote do `sftp -b -`).
- Atalhos com os argv **exatos** do contrato (`*_argv`, o oráculo dos testes) e as regras:
  - `on_docker_ps(ids, project=…)` (com os rótulos do `postgres` e `status=running`) e
    `on_docker_ps_project(ids, project=…)` (só o rótulo do projeto): 0, 1 ou 2 ids, um por linha;
  - `on_pg_dump(data=DEFAULT_DUMP)`: bytes que começam com `PGDMP` no stdout;
  - `on_psql_tables(count)`: a contagem de `pg_tables` no stdout (`"<n>\\n"`);
  - `on_pg_restore(stderr=…)`: lê o stdin (guardado em `Call.stdin_data`); pode escrever
    `errors ignored on restore` no stderr (`IGNORED_ERRORS_STDERR`);
  - `on_age(...)`: com `-d`, escreve no stdout a decifração (`decrypt_output`, ou o inverso da cifra falsa);
    com `-o <caminho>`, grava **em etapas** o cabeçalho `age-encryption.org/v1` e depois um corpo que
    **não** contém `PGDMP` (o hexadecimal minúsculo da entrada), chamando `hook(etapa)` depois de cada
    etapa (`"header"`, `"body"`) para o teste examinar os diretórios; sem `-o`, a cifra vai ao stdout. Com
    `-R <arquivo>` ausente, sai 1 sem escrever nada, como o age real. Com `omit_header=True` (desligado por
    padrão), a cifra sai **sem** o cabeçalho: só o corpo é gravado (sem a etapa `"header"`) e o código é o
    programado (0 por padrão), para a rotina provar que confere o cabeçalho além do código;
  - `on_sftp(root, remote_dir=…)`: lê o lote do stdin e responde a `ls -1 <dir>` e `get <dir>/<nome>
    <local>` sobre o diretório local `root` (no lugar de `remote_dir`); todo comando do lote fica em
    `Call.batch`; comando desconhecido ou `get` de arquivo ausente → código 1, como o `sftp -b` que para no
    primeiro erro. **[I]** Formato da saída (a conferência não foi feita, por pedir rede): cada
    comando ecoado como `sftp> <comando>` (`echo=True`), o `ls -1 <dir>` com um caminho por linha prefixado
    por `<dir>/` (`prefix=True`), arquivos ocultos omitidos, em ordem de nome, e `get` sem a linha
    `Fetching` (o `-b` liga o modo silencioso). As linhas de cada comando seguem a regra `shlex` POSIX.
- **Modo misto**: `FakeSpawn(passthrough=<caminho do age real>)` registra todo argv (conferido com
  `==`) e entrega um `subprocess.Popen` **real** quando `argv[0]` é um dos caminhos dados (o `stdin` é o
  stdout real do processo anterior; **[F]** `Popen(..., stdin=io.BytesIO(...))` levanta
  `io.UnsupportedOperation: fileno` no Python 3.12.3). A regra do `pg_dump`, nesse modo,
  roda um processo real (`sys.executable -c`) que escreve os bytes programados no stdout e sai com o
  código programado. As demais regras (o `docker ps`) continuam falsas.

Este módulo não importa `cq_server` nem `cq_client`.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
STDOUT = subprocess.STDOUT


class _Any:
    """Curinga de um elemento do argv nas regras."""

    def __repr__(self) -> str:
        return "ANY"


ANY: Any = _Any()

AGE_HEADER = b"age-encryption.org/v1"
# Corpo da cifra falsa: depois do cabeçalho, a marca e o hexadecimal minúsculo da entrada (nunca `PGDMP`).
FAKE_AGE_STANZA = b"-> X25519 cq-fake-stanza\n"
FAKE_AGE_MARKER = b"--- cq-fake\n"

DEFAULT_DUMP = b"PGDMP\x01\x0e\x00\x04\x08\x01\x01" + b"cq-fake-dump;" * 16
IGNORED_ERRORS_STDERR = b"pg_restore: warning: errors ignored on restore: 1\n"
PG_TABLES_SQL = "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"

_pids = itertools.count(40000)


# --------------------------------------------------------------------------------------------------
# Argv exatos: o oráculo dos testes
# --------------------------------------------------------------------------------------------------


def docker_ps_argv(project: str, *, docker: str = "docker") -> list[str]:
    """o contêiner do `postgres` em execução do projeto."""
    return [docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={project}", "--filter",
            "label=com.docker.compose.service=postgres", "--filter", "status=running"]


def docker_ps_project_argv(project: str, *, docker: str = "docker") -> list[str]:
    """todo contêiner do projeto em execução (só o do `postgres` é aceito)."""
    return [docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"]


def pg_dump_argv(container: str, *, docker: str = "docker") -> list[str]:
    """O argv exato do contrato do backup."""
    return [docker, "exec", "-i", container, "pg_dump", "-U", "cq", "-d", "cq", "-Fc"]


def psql_tables_argv(container: str, *, docker: str = "docker") -> list[str]:
    """a contagem de tabelas de `public` (o argv é uma lista; a SQL é um elemento só)."""
    return [docker, "exec", container, "psql", "-U", "cq", "-d", "cq", "-tAc", PG_TABLES_SQL]


def pg_restore_argv(container: str, *, docker: str = "docker") -> list[str]:
    """O argv exato do contrato da restauração."""
    return [docker, "exec", "-i", container, "pg_restore", "--single-transaction", "--no-owner",
            "--exit-on-error", "-d", "cq", "-U", "cq"]


def age_encrypt_argv(recipients: str | Path, output: str | Path, *, age: str = "age") -> list[str]:
    """O argv exato do contrato do backup."""
    return [age, "-R", str(recipients), "-o", str(output)]


def age_decrypt_argv(identity: str | Path, dump: str | Path, *, age: str = "age") -> list[str]:
    """O argv exato do contrato da restauração."""
    return [age, "-d", "-i", str(identity), str(dump)]


def sftp_argv(identity: str | Path, user: str, host: str, *, sftp: str = "sftp") -> list[str]:
    """O argv exato do contrato da cópia."""
    return [sftp, "-b", "-", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", str(identity),
            f"{user}@{host}"]


def fake_age_ciphertext(plaintext: bytes) -> bytes:
    """O que o `age` falso grava para `plaintext` (cabeçalho, marca e hexadecimal minúsculo)."""
    return AGE_HEADER + b"\n" + FAKE_AGE_STANZA + FAKE_AGE_MARKER + plaintext.hex().encode("ascii")


def fake_age_plaintext(ciphertext: bytes) -> bytes | None:
    """O inverso de `fake_age_ciphertext`; `None` se `ciphertext` não veio do `age` falso."""
    prefix = AGE_HEADER + b"\n" + FAKE_AGE_STANZA + FAKE_AGE_MARKER
    if not ciphertext.startswith(prefix):
        return None
    try:
        return bytes.fromhex(ciphertext[len(prefix):].decode("ascii"))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------------------
# Fluxos
# --------------------------------------------------------------------------------------------------


class _LazyOutput(io.BytesIO):
    """O stdout/stderr de um falso com `stdin=PIPE`: a primeira leitura roda o processo."""

    def __init__(self, ensure: Callable[[], None]) -> None:
        super().__init__()
        self._ensure = ensure

    def read(self, size: int | None = -1) -> bytes:
        self._ensure()
        return super().read(size)

    def read1(self, size: int = -1) -> bytes:
        self._ensure()
        return super().read1(size)

    def readline(self, size: int | None = -1) -> bytes:
        self._ensure()
        return super().readline(size)

    def readlines(self, hint: int | None = -1) -> list[bytes]:
        self._ensure()
        return super().readlines(hint)

    def readinto(self, buffer: Any) -> int:
        self._ensure()
        return super().readinto(buffer)

    def readinto1(self, buffer: Any) -> int:
        self._ensure()
        return super().readinto1(buffer)

    def __iter__(self) -> Any:
        self._ensure()
        return super().__iter__()

    def __next__(self) -> bytes:
        self._ensure()
        return super().__next__()


class _PipeInput(io.BytesIO):
    """O stdin de um falso com `stdin=PIPE`: fechar entrega os bytes escritos e roda o processo."""

    def __init__(self, on_close: Callable[[bytes], None]) -> None:
        super().__init__()
        self._on_close = on_close

    def close(self) -> None:
        if not self.closed:
            data = self.getvalue()
            super().close()
            self._on_close(data)


def _read_stdin(stdin: Any) -> bytes:
    """Os bytes de um `stdin` que não é `PIPE`: `None`/`DEVNULL` → vazio; objeto com `read` → lido."""
    if stdin is None or stdin == DEVNULL:
        return b""
    if isinstance(stdin, int):
        raise AssertionError("o FakeSpawn não aceita descritor numérico como stdin")
    data = stdin.read()
    if isinstance(data, str):
        raise AssertionError("o stdin de um processo falso tem de ser de bytes")
    return data


def _deliver(target: Any, data: bytes) -> None:
    """Escreve a saída num arquivo dado ao `spawn` (`stdout=<arquivo>`); `None`/`DEVNULL` descartam."""
    if target is None or target == DEVNULL or target == PIPE or target == STDOUT:
        return
    if isinstance(target, int):
        raise AssertionError("o FakeSpawn não aceita descritor numérico como saída")
    target.write(data)
    flush = getattr(target, "flush", None)
    if flush is not None:
        flush()


# --------------------------------------------------------------------------------------------------
# Processo falso
# --------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """O que uma etapa falsa produziu."""

    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int = 0


Behaviour = Callable[[list[str], bytes, "Call"], Outcome]


class FakeProcess:
    """Um processo falso com a forma do `subprocess.Popen` (ver o docstring do módulo).

    Construído direto (`FakeProcess(stdout, returncode, stderr=b"")`), é um processo já terminado com
    `stdout`/`stderr` em `io.BytesIO` e `stdin` `None`."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, stderr: bytes = b"", *,
                 args: Sequence[str] = ()) -> None:
        self.args = list(args)
        self.pid = next(_pids)
        self.stdin: io.BytesIO | None = None
        self.stdout: io.BytesIO | None = io.BytesIO(stdout)
        self.stderr: io.BytesIO | None = io.BytesIO(stderr)
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._pending: Callable[[], None] | None = None
        self.killed = False

    # -- execução adiada (stdin=PIPE) ---------------------------------------------------------------

    def _ensure_ran(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending()

    # -- a forma do Popen -----------------------------------------------------------------------------

    def poll(self) -> int | None:
        self._ensure_ran()
        self.returncode = self._final_returncode
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._ensure_ran()
        self.returncode = self._final_returncode
        return self.returncode

    def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes | None,
                                                                                               bytes | None]:
        if input is not None:
            if self.stdin is None:
                raise ValueError("communicate(input=...) sem stdin=PIPE")
            self.stdin.write(input)
        if self.stdin is not None and not self.stdin.closed:
            self.stdin.close()
        self._ensure_ran()
        out = self.stdout.read() if self.stdout is not None else None
        err = self.stderr.read() if self.stderr is not None else None
        self.wait()
        return out, err

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.killed = True

    def __enter__(self) -> FakeProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        for stream in (self.stdout, self.stderr, self.stdin):
            if stream is not None and not stream.closed:
                stream.close()
        self.wait()

    def __repr__(self) -> str:
        return f"FakeProcess(args={self.args!r}, returncode={self.returncode!r})"


# --------------------------------------------------------------------------------------------------
# Registro e regras
# --------------------------------------------------------------------------------------------------


@dataclass
class Call:
    """Uma chamada recebida pelo `FakeSpawn`."""

    argv: list[str]
    env: dict[str, str] | None
    stdin: Any
    stdout: Any
    stderr: Any
    kwargs: dict[str, Any]
    rule: str | None = None
    process: Any = None
    stdin_data: bytes | None = None
    batch: list[str] = field(default_factory=list)
    real: bool = False
    stdout_data: bytes | None = None
    stderr_data: bytes | None = None


@dataclass
class Rule:
    name: str
    match: tuple[Any, ...]
    behaviour: Behaviour | None = None
    raises: BaseException | None = None
    reads_stdin: bool = False
    hook: Callable[[str], None] | None = None
    real_stdout: bytes | None = None
    real_returncode: int = 0
    real_stderr: bytes = b""

    def matches(self, argv: Sequence[str]) -> bool:
        if len(argv) < len(self.match):
            return False
        return all(expected is ANY or expected == got for expected, got in zip(self.match, argv))


def _static(stdout: bytes, stderr: bytes, returncode: int) -> Behaviour:
    def behaviour(argv: list[str], stdin: bytes, call: Call) -> Outcome:
        return Outcome(stdout=stdout, stderr=stderr, returncode=returncode)

    return behaviour


class FakeSpawn:
    """O `spawn` falso (ver o docstring do módulo). Os métodos `on*` devolvem o próprio objeto."""

    def __init__(self, *, passthrough: str | Path | Iterable[str | Path] | None = None) -> None:
        if passthrough is None:
            self.passthrough: frozenset[str] = frozenset()
        elif isinstance(passthrough, (str, Path)):
            self.passthrough = frozenset({str(passthrough)})
        else:
            self.passthrough = frozenset(str(item) for item in passthrough)
        self.rules: list[Rule] = []
        self.calls: list[Call] = []

    # -- leitura do registro ----------------------------------------------------------------------------

    @property
    def argvs(self) -> list[list[str]]:
        """Os argv recebidos, em ordem, como listas (para `==`)."""
        return [list(call.argv) for call in self.calls]

    @property
    def envs(self) -> list[dict[str, str] | None]:
        """O `env` de cada chamada, em ordem."""
        return [call.env for call in self.calls]

    def calls_of(self, rule: str) -> list[Call]:
        """As chamadas atendidas pela regra de nome `rule`."""
        return [call for call in self.calls if call.rule == rule]

    # -- programação ----------------------------------------------------------------------------------------

    def on(self, match: Sequence[Any], *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
           raises: BaseException | None = None, reads_stdin: bool = False,
           hook: Callable[[str], None] | None = None, behaviour: Behaviour | None = None,
           name: str | None = None) -> FakeSpawn:
        """Regra genérica por prefixo de argv (`ANY` casa qualquer elemento). `behaviour(argv, stdin,
        call) -> Outcome` substitui a saída estática; `hook("run")` roda antes da saída."""
        self.rules.append(Rule(
            name=name or " ".join("*" if item is ANY else str(item) for item in match),
            match=tuple(match),
            behaviour=behaviour or _static(stdout, stderr, returncode),
            raises=raises,
            reads_stdin=reads_stdin,
            hook=hook,
        ))
        return self

    def on_docker_ps(self, ids: Sequence[str], *, project: str, docker: str = "docker", returncode: int = 0,
                     stderr: bytes = b"", raises: BaseException | None = None) -> FakeSpawn:
        """O `docker ps` do `postgres` em execução do projeto: `ids` (0, 1 ou 2), um por linha."""
        return self.on(docker_ps_argv(project, docker=docker), stdout=_id_lines(ids), stderr=stderr,
                       returncode=returncode, raises=raises, name="docker_ps")

    def on_docker_ps_project(self, ids: Sequence[str], *, project: str, docker: str = "docker",
                             returncode: int = 0) -> FakeSpawn:
        """O `docker ps` de todo o projeto (a restauração exige só o id do `postgres`)."""
        argv = docker_ps_project_argv(project, docker=docker)
        # Prefixo exato e de mesmo comprimento: o argv do `postgres` (mais longo) não cai aqui por engano,
        # porque a regra de prefixo mais longo vence quando as duas existem; sem a do `postgres`, este argv
        # mais curto também casaria o mais longo, então a regra confere o comprimento.
        expected = len(argv)

        def behaviour(got: list[str], stdin: bytes, call: Call) -> Outcome:
            if len(got) != expected:
                pytest.fail(f"argv do docker ps diferente do esperado: {got!r}")
            return Outcome(stdout=_id_lines(ids), returncode=returncode)

        return self.on(argv, behaviour=behaviour, name="docker_ps_project")

    def on_pg_dump(self, data: bytes = DEFAULT_DUMP, *, returncode: int = 0, stderr: bytes = b"",
                   docker: str = "docker", hook: Callable[[str], None] | None = None) -> FakeSpawn:
        """O `docker exec -i <id> pg_dump …`: `data` no stdout. No modo misto, um processo real."""
        rule_index = len(self.rules)
        self.on((docker, "exec", "-i", ANY, "pg_dump"), stdout=data, stderr=stderr, returncode=returncode,
                hook=hook, name="pg_dump")
        rule = self.rules[rule_index]
        rule.real_stdout = data
        rule.real_returncode = returncode
        rule.real_stderr = stderr
        return self

    def on_psql_tables(self, count: int, *, returncode: int = 0, stderr: bytes = b"",
                       docker: str = "docker") -> FakeSpawn:
        """O `docker exec <id> psql … pg_tables`: a contagem programada no stdout."""
        return self.on((docker, "exec", ANY, "psql"), stdout=f"{count}\n".encode("ascii"), stderr=stderr,
                       returncode=returncode, name="psql_tables")

    def on_pg_restore(self, *, returncode: int = 0, stderr: bytes = b"", docker: str = "docker",
                      hook: Callable[[str], None] | None = None) -> FakeSpawn:
        """O `docker exec -i <id> pg_restore …`: lê o stdin (em `Call.stdin_data`); `stderr` programável
        (ex.: `IGNORED_ERRORS_STDERR`)."""
        return self.on((docker, "exec", "-i", ANY, "pg_restore"), stderr=stderr, returncode=returncode,
                       reads_stdin=True, hook=hook, name="pg_restore")

    def on_age(self, *, age: str = "age", returncode: int = 0, stderr: bytes = b"",
               hook: Callable[[str], None] | None = None, decrypt_output: bytes | None = None,
               partial_on_failure: bool = True, raises: BaseException | None = None,
               omit_header: bool = False) -> FakeSpawn:
        """O `age` falso (ver o docstring do módulo). Com `returncode != 0` na cifra, grava só o cabeçalho
        (`partial_on_failure`) ou nada. Com `omit_header=True`, a cifra (no `-o` ou no stdout) sai sem o
        cabeçalho `age-encryption.org/v1`; o padrão (`False`) não muda nada. **[I]** O comportamento do age
        real ao falhar com `-o` não foi conferido."""

        def behaviour(argv: list[str], stdin: bytes, call: Call) -> Outcome:
            if "-d" in argv[1:]:
                if returncode != 0:
                    return Outcome(stderr=stderr, returncode=returncode)
                if decrypt_output is not None:
                    return Outcome(stdout=decrypt_output, stderr=stderr)
                source = Path(argv[-1])
                plain = fake_age_plaintext(source.read_bytes()) if source.is_file() else None
                if plain is None:
                    return Outcome(stderr=b"age: error: fake: not a cq fake ciphertext\n", returncode=1)
                return Outcome(stdout=plain, stderr=stderr)
            recipients = _option(argv, "-R")
            if recipients is not None and not Path(recipients).is_file():
                return Outcome(stderr=b"age: error: failed to open recipient file\n", returncode=1)
            cipher = fake_age_ciphertext(stdin)
            header, body = cipher[:len(AGE_HEADER) + 1], cipher[len(AGE_HEADER) + 1:]
            if omit_header:
                # Só o corpo (sem `PGDMP`); com o padrão, `header + body == cipher`.
                header = b""
            output = _option(argv, "-o")
            if output is None:
                if returncode != 0:
                    return Outcome(stderr=stderr, returncode=returncode)
                return Outcome(stdout=header + body, stderr=stderr)
            if returncode != 0 and not partial_on_failure:
                return Outcome(stderr=stderr, returncode=returncode)
            with open(output, "wb") as handle:
                if header:
                    handle.write(header)
                    handle.flush()
                    if hook is not None:
                        hook("header")
                if returncode != 0:
                    return Outcome(stderr=stderr, returncode=returncode)
                handle.write(body)
                handle.flush()
            if hook is not None:
                hook("body")
            return Outcome(stderr=stderr)

        return self.on((age,), behaviour=behaviour, reads_stdin=True, raises=raises, name="age")

    def on_sftp(self, root: Path, *, remote_dir: str, sftp: str = "sftp", returncode: int = 0,
                echo: bool = True, prefix: bool = True, extra_ls_lines: Sequence[str] = (),
                fail_get: Iterable[str] = (), stderr: bytes = b"") -> FakeSpawn:
        """O `sftp -b -` falso sobre o diretório local `root` (ver o docstring do módulo). `returncode != 0`
        simula a VM inalcançável (nenhum comando executado); `fail_get` lista nomes cujo `get` falha;
        `extra_ls_lines` entram no fim da saída do `ls -1`, como estão."""
        root = Path(root)
        failing = frozenset(fail_get)
        remote = remote_dir.rstrip("/") or "/"

        def behaviour(argv: list[str], stdin: bytes, call: Call) -> Outcome:
            lines = [line for line in stdin.decode("utf-8").splitlines() if line.strip()]
            call.batch.extend(lines)
            if returncode != 0:
                return Outcome(stderr=stderr or b"Connection closed\n", returncode=returncode)
            out: list[str] = []
            for line in lines:
                if echo:
                    out.append(f"sftp> {line}")
                words = shlex.split(line, posix=True)
                if words[:2] == ["ls", "-1"] and len(words) == 3 and words[2].rstrip("/") == remote:
                    names = sorted(entry.name for entry in root.iterdir()
                                   if entry.is_file() and not entry.name.startswith("."))
                    out.extend(f"{remote}/{name}" if prefix else name for name in names)
                    out.extend(extra_ls_lines)
                    continue
                if words[:1] == ["get"] and len(words) == 3:
                    source, local = words[1], words[2]
                    directory, _, name = source.rpartition("/")
                    if directory != remote or not name or name in failing or not (root / name).is_file():
                        return Outcome(stdout=_lines(out),
                                       stderr=f"File \"{source}\" not found.\n".encode("utf-8"), returncode=1)
                    shutil.copyfile(root / name, local)
                    continue
                return Outcome(stdout=_lines(out), stderr=b"Invalid command.\n", returncode=1)
            return Outcome(stdout=_lines(out), stderr=stderr)

        return self.on((sftp,), behaviour=behaviour, reads_stdin=True, name="sftp")

    # -- o spawn ----------------------------------------------------------------------------------------------

    def _rule_for(self, argv: Sequence[str]) -> Rule | None:
        best: Rule | None = None
        for rule in self.rules:
            if rule.matches(argv) and (best is None or len(rule.match) >= len(best.match)):
                best = rule
        return best

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: Any = None,
                 stdout: Any = None, stderr: Any = None, **kwargs: Any) -> Any:
        if isinstance(argv, (str, bytes)):
            pytest.fail("spawn recebeu uma string: o produto deve passar uma lista de argumentos, sem shell")
        argv = [os.fspath(item) for item in argv]
        call = Call(argv=argv, env=dict(env) if env is not None else None, stdin=stdin, stdout=stdout,
                    stderr=stderr, kwargs=dict(kwargs))
        self.calls.append(call)
        if kwargs.get("shell"):
            pytest.fail("spawn recebeu shell=True")
        if argv and argv[0] in self.passthrough:
            call.real = True
            call.rule = "passthrough"
            call.process = subprocess.Popen(argv, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
            return call.process
        rule = self._rule_for(argv)
        if rule is None:
            pytest.fail(f"argv sem regra no FakeSpawn: {argv!r}")
        call.rule = rule.name
        if rule.raises is not None:
            raise rule.raises
        if self.passthrough and rule.real_stdout is not None:
            call.real = True
            call.process = _real_writer(rule, env=env, stdin=stdin, stdout=stdout, stderr=stderr)
            return call.process
        process = FakeProcess(args=argv)
        process.stdout = None
        process.stderr = None
        call.process = process

        def run(data: bytes) -> None:
            call.stdin_data = data
            if rule.hook is not None:
                rule.hook("run")
            assert rule.behaviour is not None
            outcome = rule.behaviour(argv, data, call)
            process._final_returncode = outcome.returncode
            err = outcome.stderr
            out = outcome.stdout
            if stderr == STDOUT:
                out, err = out + err, b""
            for target, data_out, attr in ((stdout, out, "stdout"), (stderr, err, "stderr")):
                if target == PIPE:
                    stream = getattr(process, attr)
                    stream.write(data_out)
                    stream.seek(0)
                else:
                    _deliver(target, data_out)
            call.stdout_data = out
            call.stderr_data = err

        if stdin == PIPE:
            # Adiado até o produto fechar o stdin, ler a saída, esperar ou chamar `communicate`.
            box: dict[str, bytes] = {}

            def on_close(data: bytes) -> None:
                box["data"] = data
                process._ensure_ran()

            pipe_in = _PipeInput(on_close)
            process.stdin = pipe_in

            def pending() -> None:
                if not pipe_in.closed:
                    pipe_in.close()
                run(box.get("data", b""))

            process._pending = pending
            if stdout == PIPE:
                process.stdout = _LazyOutput(process._ensure_ran)
            if stderr == PIPE:
                process.stderr = _LazyOutput(process._ensure_ran)
            return process
        data = _read_stdin(stdin)
        if stdout == PIPE:
            process.stdout = io.BytesIO()
        if stderr == PIPE:
            process.stderr = io.BytesIO()
        run(data)
        return process


def _id_lines(ids: Sequence[str]) -> bytes:
    return "".join(f"{item}\n" for item in ids).encode("ascii")


def _lines(lines: Sequence[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _option(argv: Sequence[str], flag: str) -> str | None:
    for index, item in enumerate(argv[:-1]):
        if item == flag:
            return argv[index + 1]
    return None


def _real_writer(rule: Rule, *, env: Mapping[str, str] | None, stdin: Any, stdout: Any,
                 stderr: Any) -> subprocess.Popen[bytes]:
    """Modo misto: um processo real que escreve os bytes programados no stdout e sai com o código."""
    assert rule.real_stdout is not None
    code = (
        "import sys\n"
        f"sys.stdout.buffer.write(bytes.fromhex({rule.real_stdout.hex()!r}))\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write(bytes.fromhex({rule.real_stderr.hex()!r}))\n"
        f"sys.exit({rule.real_returncode})\n"
    )
    if stdin is not None and stdin not in (DEVNULL, PIPE) and not isinstance(stdin, int):
        if not hasattr(stdin, "fileno"):
            stdin = DEVNULL
    return subprocess.Popen([sys.executable, "-I", "-c", code], env=dict(env) if env is not None else None,
                            stdin=stdin, stdout=stdout, stderr=stderr)


def sha256_hex(data: bytes) -> str:
    """O SHA-256 em hexadecimal minúsculo (conveniência para conferir `SHA256SUMS` e o registro)."""
    return hashlib.sha256(data).hexdigest()
