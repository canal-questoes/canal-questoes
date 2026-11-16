"""Implementação real da pilha sobre o Docker Compose.


- `SubprocessRunner`: o `CommandRunner` real, por `subprocess`, sem shell, com o `docker` achado por
  `shutil.which`; a saída é capturada em bytes e decodificada depois (`utf-8`, `errors="replace"`, nas
  funções puras de `cq_client.stack`).
- `ComposeStack`: o `Stack` real, que só monta os argv de uma lista fechada sobre o prefixo
  `docker compose -p <projeto> -f <dir>/compose.yaml --env-file <dir>/.env` e classifica as falhas
  (`stack.classify_failure`), nunca repassando o texto do Docker.

É o único módulo do cliente que importa `subprocess`.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from cq_client.stack import (
    LOG_SERVICES,
    STACK_ENV_FILE,
    CommandRunner,
    DockerUnavailable,
    RunResult,
    StackEnvMissing,
    StackError,
    StackFailure,
    StackSnapshot,
    classify_failure,
    clean_log_line,
    compose_prefix,
    docker_env,
    parse_compose_version,
    parse_port,
    parse_ps,
)

# **[I]** Limites de espera do executor: as consultas (versões, estado, porta, imagem) respondem em
# segundos; a subida, a construção e a parada não têm limite aqui (a construção pode levar minutos; o
# Ctrl+C encerra o subprocesso).
QUERY_TIMEOUT = 30.0
OPERATION_TIMEOUT: float | None = None

# Espera pelo término do subprocesso de `stream` depois do `terminate` (antes do `kill`).
_TERMINATE_GRACE = 5.0


# --------------------------------------------------------------------------------------------------
# Nível 2: o executor real
# --------------------------------------------------------------------------------------------------


class SubprocessRunner:
    """O `CommandRunner` real (nível 2): sem shell, com o `docker` do `PATH`."""

    def __init__(self) -> None:
        pass

    @staticmethod
    def _args(argv: Sequence[str]) -> list[str]:
        """O argv com o executável do `docker` achado no `PATH` (`shutil.which` respeita o `PATHEXT` no
        Windows); sem ele, `DockerUnavailable` sem rodar processo nenhum."""
        args = list(argv)
        if not args or args[0] != "docker":
            # Só o `docker` roda por aqui (a lista fechada de argv começa sempre por ele).
            raise ValueError("o executor só roda o docker")
        executable = shutil.which("docker")
        if not executable:
            raise DockerUnavailable()
        return [executable, *args[1:]]

    def run(self, argv: Sequence[str], *, timeout: float | None, env: Mapping[str, str]) -> RunResult:
        args = self._args(argv)
        try:
            # Sem shell; a saída capturada em bytes (a decodificação fica para depois). O `subprocess.run`
            # mata o processo no `TimeoutExpired` e no Ctrl+C antes de propagá-los.
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=dict(env), timeout=timeout, check=False,
                                       shell=False)
        except OSError:
            # O executável sumiu ou não roda: o mesmo que fora do `PATH`, sem o rastreamento.
            raise DockerUnavailable() from None
        return RunResult(completed.returncode, completed.stdout or b"", completed.stderr or b"")

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Iterator[bytes]:
        """As linhas do stdout, uma a uma (cada uma com o `\\n`). Com código ≠ 0, depois do stdout, levanta
        `subprocess.CalledProcessError` com o stderr capturado (a mesma convenção do dublê). Fechar o
        iterador (ou o Ctrl+C) encerra o subprocesso."""
        args = self._args(argv)  # o `docker` fora do `PATH` já aqui, antes de iterar
        return self._lines(args, dict(env))

    @staticmethod
    def _lines(args: list[str], env: dict[str, str]) -> Iterator[bytes]:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, shell=False)
        except OSError:
            raise DockerUnavailable() from None
        assert process.stdout is not None and process.stderr is not None
        stderr_pipe = process.stderr
        # O stderr é drenado numa linha de execução à parte (senão o pipe cheio trava o subprocesso).
        chunks: list[bytes] = []
        drain = threading.Thread(target=lambda: chunks.append(stderr_pipe.read()), daemon=True)
        drain.start()
        returncode: int | None = None
        try:
            for line in iter(process.stdout.readline, b""):
                yield line
            returncode = process.wait()
            drain.join()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
        if returncode:
            raise subprocess.CalledProcessError(returncode, args, output=None, stderr=b"".join(chunks))


# --------------------------------------------------------------------------------------------------
# Nível 1: a pilha sobre o Compose
# --------------------------------------------------------------------------------------------------


class ComposeStack:
    """O `Stack` real (nível 1). `runner=None` usa o `SubprocessRunner`; `env` é o
    ambiente do processo, filtrado por `stack.docker_env` antes de cada comando."""

    def __init__(self, project: str, stack_dir: Path, runner: CommandRunner | None = None, *,
                 env: Mapping[str, str]) -> None:
        self.project = project
        self.stack_dir = stack_dir
        self.runner: CommandRunner = runner if runner is not None else SubprocessRunner()
        self.env = env

    # -- auxiliares --------------------------------------------------------------------------------

    def _require_env_file(self) -> None:
        """Sem o `.env` no diretório, nenhuma chamada ao Docker (o prefixo sempre leva
        `--env-file`). Só a existência é conferida; o arquivo não é lido nem criado."""
        if not (Path(self.stack_dir) / STACK_ENV_FILE).exists():
            raise StackEnvMissing()

    def _compose(self, *rest: str) -> list[str]:
        return [*compose_prefix(self.project, Path(self.stack_dir)), *rest]

    def _run(self, argv: list[str], timeout: float | None) -> RunResult:
        """Um comando pelo executor, com o ambiente filtrado. O tempo esgotado vira a falha genérica fixa
        (a exceção do `subprocess` cita o argv e pode carregar a saída)."""
        error: StackError | None = None
        try:
            return self.runner.run(argv, timeout=timeout, env=docker_env(self.env))
        except subprocess.TimeoutExpired:
            error = StackFailure("other")
        raise error

    def _run_checked(self, argv: list[str], timeout: float | None) -> RunResult:
        """Como `_run`, com o código ≠ 0 classificado pelo stderr, que nunca é repassado."""
        result = self._run(argv, timeout)
        if result.returncode != 0:
            raise classify_failure(result.stderr)
        return result

    # -- Stack -----------------------------------------------------------------------------------

    def snapshot(self) -> StackSnapshot:
        """Estado por `P ps --all --format json` e porta por `P port api 8000`. O `api_owned` fica `None`:
        quem compara a API do perfil com a desta pilha é o chamador."""
        self._require_env_file()
        services = parse_ps(self._run_checked(self._compose("ps", "--all", "--format", "json"),
                                              QUERY_TIMEOUT).stdout)
        port = self._run(self._compose("port", "api", "8000"), QUERY_TIMEOUT)
        # Sem a api publicada (pilha ausente ou parcial), o Compose sai com código ≠ 0: sem porta.
        api_port = parse_port(port.stdout) if port.returncode == 0 else None
        return StackSnapshot(services=services, api_port=api_port, api_owned=None)

    def up(self, *, rebuild: bool) -> None:
        """`P up -d --no-recreate`; com `rebuild`, `P build api worker` e depois `P up -d`."""
        self._require_env_file()
        if rebuild:
            self._run_checked(self._compose("build", "api", "worker"), OPERATION_TIMEOUT)
            self._run_checked(self._compose("up", "-d"), OPERATION_TIMEOUT)
        else:
            self._run_checked(self._compose("up", "-d", "--no-recreate"), OPERATION_TIMEOUT)

    def down(self) -> None:
        """`P down`, sem apagar volumes nem imagens."""
        self._require_env_file()
        self._run_checked(self._compose("down"), OPERATION_TIMEOUT)

    def logs(self, services: Sequence[str], *, tail: int, follow: bool) -> Iterator[str]:
        """`P logs --no-color --tail <N> [--follow] <serviços>`, com a lista explícita (nunca
        o `engine`), linha a linha e já limpa. A recusa sem o `.env` sai já na chamada, antes de iterar."""
        chosen = list(services)
        if (not chosen or any(s not in LOG_SERVICES for s in chosen) or len(set(chosen)) != len(chosen)
                or isinstance(tail, bool) or not isinstance(tail, int) or tail < 0):
            # O CLI valida antes; isto é a trava contra um uso errado do adaptador.
            raise ValueError("registros só de api, worker e postgres, sem repetição, com tail ≥ 0")
        self._require_env_file()
        argv = self._compose("logs", "--no-color", "--tail", str(tail), *(["--follow"] if follow else []),
                             *chosen)
        return self._log_lines(argv)

    def _log_lines(self, argv: list[str]) -> Iterator[str]:
        lines = self.runner.stream(argv, env=docker_env(self.env))
        error: StackError | None = None
        try:
            for raw in lines:
                yield clean_log_line(raw.rstrip(b"\n"))
        except subprocess.CalledProcessError as exc:
            # Só a classe sai; o stderr (que pode citar o `.env`) fica para trás.
            error = classify_failure(exc.stderr or b"")
        finally:
            close = getattr(lines, "close", None)
            if close is not None:
                close()
        if error is not None:
            raise error

    def compose_version(self) -> tuple[int, int, int] | None:
        """`docker compose version --short`; `None` se o Docker não responde ou a saída é ilegível."""
        try:
            result = self._run(["docker", "compose", "version", "--short"], QUERY_TIMEOUT)
        except StackError:
            return None
        if result.returncode != 0:
            return None
        return parse_compose_version(result.stdout.decode("utf-8", errors="replace"))

    def engine_image_id(self) -> str | None:
        """O ID da imagem do motor por `docker image inspect --format {{.Id}} <projeto>-engine`;
        `None` com a imagem ausente ou o Docker indisponível."""
        try:
            result = self._run(["docker", "image", "inspect", "--format", "{{.Id}}", f"{self.project}-engine"],
                               QUERY_TIMEOUT)
        except StackError:
            return None
        if result.returncode != 0:
            return None
        image_id = result.stdout.decode("utf-8", errors="replace").strip()
        return image_id.splitlines()[0].strip() if image_id else None

    def docker_available(self) -> bool:
        """`docker version --format {{.Server.Version}}` responde com código 0 (o daemon atende);
        `docker` fora do `PATH`, daemon parado ou tempo esgotado → falso, sem exceção."""
        try:
            result = self._run(["docker", "version", "--format", "{{.Server.Version}}"], QUERY_TIMEOUT)
        except StackError:
            return False
        return result.returncode == 0
