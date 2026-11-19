"""Restauração de um dump cifrado num projeto Compose.


Uso:

    python3 ops/backup/restore.py --dump <arquivo .age> --identity <arquivo da chave privada> --project <p>
            [--production] [--age-bin age] [--docker-bin docker]

Recusa o `cq-demo` sempre e o `cq` sem `--production` (`--production` só vale com `--project cq`). Em
fluxo: `age -d -i <identidade> <dump>` (stdout) → `docker exec -i <id> pg_restore --single-transaction
--no-owner --exit-on-error -d cq -U cq` (stdin), dois processos encadeados, os dois códigos conferidos;
nenhum arquivo decifrado em disco. A identidade nunca é copiada, impressa nem gravada.

Só biblioteca padrão. Importável sem efeito colateral. O `spawn` tem a forma do `ProcessLike`/`Spawn` de
`cq_backup.py` (o mesmo diretório; os scripts de `ops/` não se importam por nome de pacote).

Ordem das recusas, todas antes do processo que abre a identidade: argumentos, projeto reservado, contêiner
do `postgres` (exatamente um), outro serviço do projeto no ar, banco não vazio. O stderr do `pg_restore` é
lido só para procurar `errors ignored on restore`, e o do `age` só para reconhecer o pipe fechado
(`AGE_BROKEN_PIPE_MARKER`); os dois são descartados em seguida, e nenhuma saída de processo é repetida.

Prazos: nenhuma espera de processo filho é sem prazo. As três checagens do Docker têm
`DOCKER_CHECK_TIMEOUT_SECONDS` cada (`communicate` com prazo); o par `age -d` → `pg_restore`, junto,
`RESTORE_TIMEOUT_SECONDS`, vigiado por um `threading.Timer` que mata os dois ao vencer (o pai lê o
stderr do `pg_restore` aos pedaços, e só a morte do filho desbloqueia essa leitura). Vencido o
prazo, os filhos são mortos e esperados por `KILL_GRACE_SECONDS`, e a saída é a falha do passo.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

# Chave → (código de saída, mensagem). A `ok` vai ao stdout; as demais, uma linha no
# stderr, sem caminho, nome de projeto, conteúdo da identidade nem saída de processo.
RESTORE_MESSAGES: dict[str, tuple[int, str]] = {
    "ok": (0, "Restauração concluída."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "decrypt_failed": (65, "A decifração do dump falhou."),
    "container_not_found": (69, "O contêiner do Postgres do projeto não foi encontrado em execução."),
    "restore_failed": (70, "A restauração do banco falhou; nada foi gravado."),
    "not_empty": (73, "O banco de destino não está vazio."),
    "stack_running": (75, "Outro serviço do projeto está no ar; deixe só o Postgres."),
    "reserved_project": (77, "Este projeto não pode receber restauração."),
    "internal": (1, "Erro interno da restauração."),
}

# O `cq-demo` é recusado sempre; o `cq`, só com `production=True`.
RESERVED = frozenset({"cq", "cq-demo"})
PRODUCTION_PROJECT = "cq"

# O marcador, no stderr do `pg_restore`, de erro ignorado (lido e descartado, nunca repetido).
IGNORED_ERRORS_MARKER = "errors ignored on restore"

# O erro do `age -d` quando o leitor do pipe (o `pg_restore`) fecha a ponta antes do fim do fluxo decifrado
# (lido e descartado, nunca repetido). **[F]** Conferido no age v1.3.2 oficial do Windows (2026-10-30, dump de
# 8 MiB ligado a um leitor que lê 1000 bytes e sai com 1): o `age` sai com 1 e escreve
# `age: error: write /dev/stdout: The pipe has been ended.`; com a identidade errada, sai com 1 e escreve
# `age: error: no identity matched any of the recipients` antes de qualquer byte no stdout.
AGE_BROKEN_PIPE_MARKER = b"age: error: write /dev/stdout: "

# A forma do `cq_backup.Spawn` (`spawn(argv, *, env, stdin, stdout, stderr) -> ProcessLike`).
Spawn = Callable[..., Any]

# A consulta de banco vazio (um elemento só do argv, sem shell).
PG_TABLES_SQL = "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"

# **[I]** A regra de nome de projeto do Compose (minúsculas, dígitos, `-` e `_`, começando por letra ou dígito),
# de memória, não conferida na fonte: um nome fora dela é argumento inválido, e nada estranho vai
# parar no filtro de rótulo do `docker ps`.
_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Tamanho de cada leitura do stderr do `pg_restore` (a procura do marcador é por janelas, sem guardar tudo).
_CHUNK = 65536

# Prazos das esperas dos processos filhos. Um filho preso não pode deixar a restauração
# sem retornar: ao vencer o prazo, o filho é morto e a saída é a mensagem fixa do passo.
# **[I]** O `docker ps` e a contagem do `psql` respondem em menos de um segundo num daemon e num banco saudáveis;
# 2 min cobrem um daemon lento. Vencido, vale o mesmo que o comando em erro: `container_not_found` no `docker ps` do
# `postgres`, `internal` no `docker ps` do projeto e na contagem de tabelas.
DOCKER_CHECK_TIMEOUT_SECONDS = 120
# **[I]** O banco do piloto (uma turma) deve ficar na casa das dezenas de MB; o `pg_restore` dele, com os índices,
# leva segundos ou poucos minutos. 1 h para o par inteiro dá folga de mais de dez vezes. Vencido: `restore_failed`
# (a restauração não terminou; o `--single-transaction` desfaz o que o `pg_restore` gravou quando a entrada se
# interrompe, **[I]** sem conferência com o Postgres real neste caminho).
RESTORE_TIMEOUT_SECONDS = 3600
# **[I]** Depois do `kill`, um filho morre em milissegundos; 10 s de espera bastam para recolhê-lo sem que a própria
# limpeza fique presa.
KILL_GRACE_SECONDS = 10


class _Refusal(Exception):
    """Uma saída prevista da restauração (a chave de `RESTORE_MESSAGES`); nunca carrega texto de processo."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _child_env() -> dict[str, str]:
    """O ambiente dos filhos: o do processo sem as `CQ_*` (como na rotina de backup)."""
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("CQ_")}


def _finish(key: str, stdout: TextIO, stderr: TextIO) -> int:
    code, text = RESTORE_MESSAGES[key]
    target = stdout if key == "ok" else stderr
    target.write(text + "\n")
    target.flush()
    return code


def _capture(spawn: Spawn, argv: list[str], env: dict[str, str], *, on_timeout: str) -> tuple[int, bytes]:
    """Roda `argv` com o stdout capturado e o stderr descartado; devolve (código, stdout). O `communicate` com
    `DOCKER_CHECK_TIMEOUT_SECONDS` lê e espera sem ficar preso no pipe; vencido, o filho é morto e a saída é
    `_Refusal(on_timeout)`, a mesma do comando em erro naquele passo."""
    process = spawn(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        out, _ = process.communicate(timeout=DOCKER_CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(process, drain=True)
        raise _Refusal(on_timeout) from None
    except BaseException:
        _stop(process, drain=True)
        raise
    return process.returncode, out if isinstance(out, bytes) else b""


def _ids(output: bytes) -> list[str]:
    return [line.strip() for line in output.decode("ascii", errors="replace").splitlines() if line.strip()]


def _has_marker(stream: Any, marker: bytes = IGNORED_ERRORS_MARKER.encode("ascii")) -> bool:
    """Lê o stream até o fim procurando o marcador; o conteúdo é descartado (nunca repetido)."""
    if stream is None:
        return False
    found = False
    tail = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        window = tail + chunk
        if not found and marker in window:
            found = True
        tail = window[-(len(marker) - 1):]
    return found


def _check_arguments(project: str, production: bool) -> None:
    """Argumentos e projeto reservado, antes de qualquer processo."""
    if not isinstance(project, str) or not isinstance(production, bool) or not _PROJECT_NAME.fullmatch(project):
        raise _Refusal("invalid_arguments")
    if project in RESERVED and not (production and project == PRODUCTION_PROJECT):
        raise _Refusal("reserved_project")
    if production and project != PRODUCTION_PROJECT:
        raise _Refusal("invalid_arguments")


def _postgres_container(spawn: Spawn, project: str, docker_bin: str, env: dict[str, str]) -> str:
    """Exatamente um contêiner do `postgres` do projeto em execução."""
    code, out = _capture(spawn, [docker_bin, "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                                 "--filter", "label=com.docker.compose.service=postgres", "--filter",
                                 "status=running"], env, on_timeout="container_not_found")
    ids = _ids(out)
    if code != 0 or len(ids) != 1:
        raise _Refusal("container_not_found")
    return ids[0]


def _only_postgres_running(spawn: Spawn, project: str, container: str, docker_bin: str,
                           env: dict[str, str]) -> None:
    """Nenhum outro contêiner do projeto em execução."""
    code, out = _capture(spawn, [docker_bin, "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                         env, on_timeout="internal")
    if code != 0:
        raise _Refusal("internal")
    if set(_ids(out)) != {container}:
        raise _Refusal("stack_running")


def _database_empty(spawn: Spawn, container: str, docker_bin: str, env: dict[str, str]) -> None:
    """Nenhuma tabela em `public` no banco de destino."""
    code, out = _capture(spawn, [docker_bin, "exec", container, "psql", "-U", "cq", "-d", "cq", "-tAc",
                                 PG_TABLES_SQL], env, on_timeout="internal")
    if code != 0:
        raise _Refusal("internal")
    text = out.decode("ascii", errors="replace").strip()
    if not text.isdigit():
        raise _Refusal("internal")
    if int(text) != 0:
        raise _Refusal("not_empty")


def _stop(process: Any, *, drain: bool = False) -> None:
    """Encerra um filho que passou do prazo ou que uma falha deixou para trás: `kill` se ainda roda e uma espera
    curta (`KILL_GRACE_SECONDS`). Com `drain`, a espera é antes um `communicate` com prazo, que recolhe e fecha os
    pipes de um `communicate` vencido. Melhor esforço: nunca levanta nem fica preso."""
    try:
        if process.poll() is None:
            process.kill()
    except Exception:  # noqa: BLE001 - limpeza de melhor esforço; a saída já é a do passo
        pass
    if drain:
        try:
            process.communicate(timeout=KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - idem
            pass
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 - idem
        pass


class _Watchdog:
    """O prazo de um encadeamento de filhos: um `threading.Timer` que, ao vencer, mata (`kill`) os filhos da lista
    ainda em execução, o que fecha os pipes deles e desbloqueia a leitura e as esperas do pai. `expired` fica
    verdadeiro só se o vigia chegou a matar algum filho (um encadeamento que terminou no limite não vira falha).
    Serve a qualquer objeto com `poll()` e `kill()`, como o `subprocess.Popen`; a lista pode crescer depois da
    partida (cada filho entra nela logo depois do `spawn`)."""

    def __init__(self, seconds: float, processes: list[Any]) -> None:
        self._deadline = time.monotonic() + seconds
        self._processes = processes
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self.expired = False

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._timer.cancel()
        # Um disparo em curso termina antes da decisão do pai (o `kill` é imediato).
        self._timer.join(KILL_GRACE_SECONDS)

    def wait_budget(self) -> float:
        """O prazo de um `wait` dentro do encadeamento: o que resta, mais a folga do encerramento pelo vigia."""
        return max(0.0, self._deadline - time.monotonic()) + KILL_GRACE_SECONDS

    def _fire(self) -> None:
        for process in list(self._processes):
            try:
                if process.poll() is None:
                    process.kill()
                    self.expired = True
            except Exception:  # noqa: BLE001 - o vigia não pode morrer no meio; o pai decide pela saída
                self.expired = True


def _pipe(spawn: Spawn, dump: Path, identity: Path, container: str, age_bin: str, docker_bin: str,
          env: dict[str, str]) -> None:
    """`age -d -i <identidade> <dump>` (stdout) → `docker exec -i <id> pg_restore …` (stdin): os dois códigos
    conferidos e o marcador de erro ignorado procurado no stderr do `pg_restore`; nada decifrado em disco.

    A causa de uma falha: `age` em erro → `decrypt_failed`, salvo quando o `age` falhou só pelo pipe fechado
    (o `pg_restore` saiu antes, com `--exit-on-error`, e o `age` levou a escrita no stdout fechado): com o
    `pg_restore` também em erro e o `age` morto por SIGPIPE (POSIX) ou com `AGE_BROKEN_PIPE_MARKER` no stderr
    (Windows, conferido), a causa é o banco → `restore_failed`. A identidade errada faz o `age` falhar antes
    de escrever qualquer byte (o `pg_restore` também falha, com a entrada vazia), com outra mensagem: segue
    `decrypt_failed`. O stderr do `age` só é varrido pelo marcador e descartado.

    **[I]** No Linux, o runtime do Go encerra o processo com SIGPIPE ao escrever num pipe fechado no fd 1
    (documentação do `os/signal`, de memória, não conferida na fonte); o `Popen` dá então `-SIGPIPE`.

    Prazo: o par inteiro tem `RESTORE_TIMEOUT_SECONDS`, vigiado pelo `_Watchdog`; vencido, os dois são mortos e
    recolhidos e a saída é `restore_failed` (a restauração não terminou)."""
    started: list[Any] = []
    with _Watchdog(RESTORE_TIMEOUT_SECONDS, started) as watchdog:
        try:
            decrypt = spawn([age_bin, "-d", "-i", str(identity), str(dump)], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            started.append(decrypt)
            load = spawn([docker_bin, "exec", "-i", container, "pg_restore", "--single-transaction", "--no-owner",
                          "--exit-on-error", "-d", "cq", "-U", "cq"], env=env, stdin=decrypt.stdout,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            started.append(load)
            # docs.python.org/3.12, "Replacing shell pipeline": o pai fecha a sua ponta para o `age` receber SIGPIPE
            # se o `pg_restore` sair antes.
            if decrypt.stdout is not None:
                decrypt.stdout.close()
            try:
                ignored = _has_marker(load.stderr)
            finally:
                if load.stderr is not None:
                    load.stderr.close()
            load_code = load.wait(timeout=watchdog.wait_budget())
            # O stderr do `age` é curto (uma mensagem de erro) e só é lido depois do `pg_restore`: cabe no pipe.
            try:
                broken_pipe = _has_marker(decrypt.stderr, AGE_BROKEN_PIPE_MARKER)
            finally:
                if decrypt.stderr is not None:
                    decrypt.stderr.close()
            decrypt_code = decrypt.wait(timeout=watchdog.wait_budget())
        except Exception as error:
            for process in started:
                _stop(process)
            # Uma espera vencida ou um erro depois do disparo do vigia: a restauração não terminou no prazo.
            if isinstance(error, subprocess.TimeoutExpired) or watchdog.expired:
                raise _Refusal("restore_failed") from None
            raise
        except BaseException:
            for process in started:
                _stop(process)
            raise
    if watchdog.expired:
        for process in started:
            _stop(process)
        raise _Refusal("restore_failed")
    sigpipe = getattr(signal, "SIGPIPE", None)
    killed_by_pipe = sigpipe is not None and decrypt_code == -sigpipe
    if decrypt_code != 0 and not (load_code != 0 and (killed_by_pipe or broken_pipe)):
        raise _Refusal("decrypt_failed")
    if load_code != 0 or ignored:
        raise _Refusal("restore_failed")


def restore(dump: Path, identity: Path, project: str, *, production: bool = False, age_bin: str = "age",
            docker_bin: str = "docker", spawn: Spawn | None = None, stdout: TextIO | None = None,
            stderr: TextIO | None = None) -> int:
    """Restaura `dump` no Postgres do `project`; devolve o código de `RESTORE_MESSAGES`.
    `spawn=None` → `subprocess.Popen`; `stdout`/`stderr` `None` → os do processo, resolvidos na chamada."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    run = spawn if spawn is not None else subprocess.Popen
    try:
        _check_arguments(project, production)
        if not age_bin or not docker_bin:
            raise _Refusal("invalid_arguments")
        env = _child_env()
        container = _postgres_container(run, project, docker_bin, env)
        _only_postgres_running(run, project, container, docker_bin, env)
        _database_empty(run, container, docker_bin, env)
        _pipe(run, Path(dump), Path(identity), container, age_bin, docker_bin, env)
    except _Refusal as refusal:
        return _finish(refusal.key, out, err)
    except Exception:  # noqa: BLE001 - mensagem fixa; nada da exceção (caminhos, saídas) vai ao stderr
        return _finish("internal", out, err)
    return _finish("ok", out, err)


class _ArgumentError(Exception):
    """Linha de comando inválida (sem o `usage` do `argparse` nem eco da entrada)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # noqa: ARG002 - a mensagem do argparse ecoa a entrada; descartada
        raise _ArgumentError()

    def exit(self, status: int = 0, message: str | None = None) -> Any:  # noqa: ARG002
        raise _ArgumentError()


def _parser() -> _Parser:
    parser = _Parser(prog="restore.py", add_help=False, allow_abbrev=False)
    parser.add_argument("--dump", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--age-bin", default="age")
    parser.add_argument("--docker-bin", default="docker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Linha de comando; devolve o código de saída."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        return _finish("invalid_arguments", sys.stdout, sys.stderr)
    return restore(Path(args.dump), Path(args.identity), args.project, production=args.production,
                   age_bin=args.age_bin, docker_bin=args.docker_bin)


if __name__ == "__main__":
    sys.exit(main())
