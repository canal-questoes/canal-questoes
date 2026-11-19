"""O adaptador real da pilha sobre o Docker Compose, pelo dublê de nível 2.



`ComposeStack(project, stack_dir, runner=FakeRunner(...), env=...)` só monta os argv da lista fechada da pilha,
sobre o prefixo `docker compose -p <projeto> -f <dir>/compose.yaml --env-file <dir>/.env`, com o ambiente de
`docker_env`, e classifica o stderr do Docker sem repassá-lo. As funções puras (`parse_ps`, `parse_port`,
`parse_compose_version`, `clean_log_line`, `classify_failure`, `docker_env`, `compose_prefix`) são testadas
direto. Nenhum Docker real; o `SubprocessRunner` só com `shutil.which` e o `subprocess` substituídos.

Segredo-sentinela: toda amostra de stderr do `FakeRunner` leva `SENTINEL_SECRET`, e nenhuma
exceção (`str`, `repr`, `args`, `message`) o mostra. As asserções sobre as constantes e a hierarquia real de
`StackError` ficam na mesma função de um caso de `parse_ps`.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from cq_client.stack import (
    LOG_SERVICES,
    MIN_COMPOSE,
    SERVICES,
    DockerUnavailable,
    RemoteProfileRefused,
    ServiceState,
    StackDirNotFound,
    StackEnvMissing,
    StackError,
    StackFailure,
    StackSnapshot,
    StackTargetRefused,
    StackVariableMissing,
    classify_failure,
    clean_log_line,
    compose_prefix,
    docker_env,
    parse_compose_version,
    parse_port,
    parse_ps,
)
from cq_client.stack_compose import ComposeStack, SubprocessRunner

from tests.helpers.cli_runner import make_stack_dir
from tests.helpers.docker_fake import (
    BUILD_FAILURES,
    DAEMON_FAILURES,
    DOCKER_MISSING,
    FAILURE_STDERR,
    LOGS_CLEAN,
    LOGS_RAW,
    PORT_FAILURES,
    PORT_NONE,
    SENTINEL_SECRET,
    SUPPORTED_VERSIONS,
    FakeRunner,
    assert_allowed,
    failure_samples,
    ps_entry,
    ps_output,
    ps_sample,
)
from tests.helpers.stack_fake import ENGINE_IMAGE_ID, OTHER_API_PORT, TEST_API_PORT

PROJECT = "cq"
OTHER_PROJECT = "cq-local"

# O ambiente do processo que o CLI repassaria (com o que o adaptador tem de tirar).
PROCESS_ENV = {
    "PATH": "C:/ferramentas/bin",
    "SystemRoot": "C:/Windows",
    "DOCKER_CONFIG": "C:/docker-config",
    "CQ_DB_PASSWORD": "senha-do-env-que-fica",
    "CQ_TOKEN": "cq_token-do-perfil-Hx8" + "z" * 20,
    "CQ_API_URL": "http://127.0.0.1:18765",
    "CQ_CONFIG": "C:/perfil/cq.toml",
    "CQ_PROFILE": "local",
    "CQ_STACK_DIR": "C:/pilha",
    "COMPOSE_FILE": "outro-compose.yaml",
    "COMPOSE_PROJECT_NAME": "cq-demo",
    "COMPOSE_PROFILES": "depuracao",
    "COMPOSE_ANSI": "always",
    "NO_COLOR": "0",
}
EXPECTED_DOCKER_ENV = {
    "PATH": "C:/ferramentas/bin",
    "SystemRoot": "C:/Windows",
    "DOCKER_CONFIG": "C:/docker-config",
    "CQ_DB_PASSWORD": "senha-do-env-que-fica",
    "COMPOSE_ANSI": "never",
    "NO_COLOR": "1",
}

# Classe esperada de cada amostra de falha (a classificação do stderr).
EXPECTED_FAILURE: dict[str, tuple[type[StackError], dict[str, Any]]] = {
    "variable_missing": (StackVariableMissing, {"name": "CQ_DB_PASSWORD"}),
    "variable_missing_unnamed": (StackVariableMissing, {"name": None}),
    **{name: (DockerUnavailable, {}) for name in DAEMON_FAILURES},
    **{name: (StackFailure, {"kind": "port_in_use"}) for name in PORT_FAILURES},
    **{name: (StackFailure, {"kind": "build_failed"}) for name in BUILD_FAILURES},
    "generic": (StackFailure, {"kind": "other"}),
}


def _prefix(project: str, stack_dir: pathlib.Path) -> list[str]:
    return ["docker", "compose", "-p", project, "-f", str(stack_dir / "compose.yaml"), "--env-file",
            str(stack_dir / ".env")]


def _has(runner: FakeRunner, project: str, stack_dir: pathlib.Path, rest: list[str]) -> list[int]:
    """Os índices das chamadas cujo argv é `<prefixo> + rest` (o caminho comparado como `Path`)."""
    found = []
    for index, argv in enumerate(runner.argvs()):
        if (argv[:5] == _prefix(project, stack_dir)[:5] and len(argv) == 8 + len(rest)
                and pathlib.Path(argv[5]) == stack_dir / "compose.yaml" and argv[6] == "--env-file"
                and pathlib.Path(argv[7]) == stack_dir / ".env" and argv[8:] == rest):
            found.append(index)
    return found


def _stack(tmp_path: pathlib.Path, runner: FakeRunner, *, project: str = PROJECT, env_file: bool = True
           ) -> tuple[ComposeStack, pathlib.Path]:
    stack_dir = make_stack_dir(tmp_path, env_file=env_file)
    return ComposeStack(project, stack_dir, runner, env=dict(PROCESS_ENV)), stack_dir


def _assert_no_sentinel(exc: BaseException) -> None:
    texts = [str(exc), repr(exc), *(repr(a) for a in exc.args)]
    message = getattr(exc, "message", None)
    if message is not None:
        texts.append(str(message))
    if exc.__cause__ is not None:
        texts += [str(exc.__cause__), repr(exc.__cause__)]
    for text in texts:
        assert SENTINEL_SECRET not in text, f"o sentinela vazou numa exceção: {text[:300]}"


def _assert_failure(exc: BaseException, kind: str) -> None:
    cls, attrs = EXPECTED_FAILURE[kind]
    assert type(exc) is cls, f"{kind}: {type(exc).__name__}"
    for name, value in attrs.items():
        assert getattr(exc, name) == value
    _assert_no_sentinel(exc)


# --------------------------------------------------------------------------------------------------
# Funções puras: prefixo, `ps`, porta, versão, registros, classificação, ambiente


def test_compose_prefix(tmp_path) -> None:
    """O prefixo `docker compose -p <projeto> -f <dir>/compose.yaml --env-file <dir>/.env`."""
    stack_dir = make_stack_dir(tmp_path)
    for project in (PROJECT, OTHER_PROJECT):
        prefix = compose_prefix(project, stack_dir)
        assert prefix[:5] == ["docker", "compose", "-p", project, "-f"]
        assert pathlib.Path(prefix[5]) == stack_dir / "compose.yaml"
        assert prefix[6] == "--env-file"
        assert pathlib.Path(prefix[7]) == stack_dir / ".env"
        assert len(prefix) == 8


def _by_service(states: tuple[ServiceState, ...]) -> dict[str, tuple[str, str]]:
    assert len(states) == len(SERVICES)
    assert {s.service for s in states} == set(SERVICES)
    return {s.service: (s.state, s.health) for s in states}


def test_parse_ps_healthy_lines_and_array(tmp_path) -> None:
    """Um JSON por linha (Compose ≥ 2.21) ou um array; o worker sem `Health` → `none`;
    o resultado tem os 4 serviços e nunca os `Labels` (que levam um caminho com o sentinela)."""
    expected = {"postgres": ("running", "healthy"), "api": ("running", "healthy"), "worker": ("running", "none"),
                "engine": ("running", "healthy")}
    for sample in ("healthy", "healthy_array"):
        states = parse_ps(ps_sample(sample))
        assert all(isinstance(s, ServiceState) for s in states)
        assert _by_service(states) == expected
        assert SENTINEL_SECRET not in repr(states)
    # as constantes e a hierarquia reais do esqueleto.
    assert SERVICES == ("postgres", "api", "worker", "engine")
    assert LOG_SERVICES == ("api", "worker", "postgres")
    assert MIN_COMPOSE == (2, 27, 0)
    expected_codes = {DockerUnavailable: 1, StackEnvMissing: 2, StackTargetRefused: 2, StackDirNotFound: 2,
                      RemoteProfileRefused: 2}
    for cls, code in expected_codes.items():
        assert issubclass(cls, StackError)
        assert cls().exit_code == code
    assert StackVariableMissing("CQ_DB_PASSWORD").exit_code == 2
    assert StackFailure("other").exit_code == 1
    assert DockerUnavailable().message == "O Docker não está disponível neste computador (instalado e em execução?)."
    assert StackFailure("port_in_use").message == "A porta da API já está em uso por outro processo ou projeto."
    assert StackFailure("build_failed").message == ("A construção das imagens falhou; rode de novo com o Docker "
                                                    "em ordem.")
    assert StackFailure("other").message == "O Docker Compose falhou ao operar a pilha."
    assert StackVariableMissing("CQ_DB_PASSWORD").message == ("Configuração da pilha incompleta: defina "
                                                              "CQ_DB_PASSWORD no arquivo de ambiente.")
    assert StackVariableMissing("POSTGRES_PASSWORD").name is None
    assert RemoteProfileRefused().message == "Este comando só opera a pilha local; o perfil em uso é remoto."
    assert StackEnvMissing().message == ("Arquivo de ambiente ausente no diretório da pilha: crie-o a partir do "
                                         ".env.example (veja docs/operacao/inicio-rapido.md).")


@pytest.mark.parametrize(
    ("sample", "expected"),
    [("empty", {s: ("absent", "none") for s in SERVICES}),
     ("partial", {"postgres": ("running", "healthy"), "api": ("absent", "none"), "worker": ("stopped", "none"),
                  "engine": ("absent", "none")}),
     ("foreign_service", {"postgres": ("running", "healthy"), "api": ("running", "healthy"),
                          "worker": ("running", "none"), "engine": ("running", "healthy")}),
     ("starting", {"postgres": ("running", "healthy"), "api": ("running", "starting"),
                   "worker": ("running", "none"), "engine": ("running", "starting")}),
     ("unhealthy", {"postgres": ("running", "healthy"), "api": ("running", "unhealthy"),
                    "worker": ("running", "none"), "engine": ("running", "healthy")}),
     ("restart_loop", {"postgres": ("running", "healthy"), "api": ("stopped", "unhealthy"),
                       "worker": ("running", "none"), "engine": ("running", "healthy")}),
     ("stopped", {s: ("stopped", "none") for s in SERVICES}),
     ("stopped_states", {s: ("stopped", "none") for s in SERVICES})],
    ids=["vazio", "parcial", "servico_estranho", "subindo", "com_falha", "laco_de_reinicio", "parados",
         "created_dead_paused_removing"])
def test_parse_ps_state_mapping(sample, expected) -> None:
    """`running` → `running`; `exited`, `created`, `dead`, `paused`, `removing`,
    `restarting` → `stopped` (`restarting` com a saúde `unhealthy`); sem linha → `absent`; `Health` vazio →
    `none`; um serviço estranho ao projeto é ignorado; a saída vazia é aceita."""
    states = parse_ps(ps_sample(sample))
    assert _by_service(states) == expected
    assert "adminer" not in repr(states)
    assert SENTINEL_SECRET not in repr(states)


def test_parse_ps_ignores_unknown_fields_and_other_projects() -> None:
    """Só `Service`, `State` e `Health` contam; um objeto sem `Service` conhecido é ignorado."""
    entries = [ps_entry("postgres", "running", "healthy"), ps_entry("api", "running", "healthy"),
               {**ps_entry("worker", "running", ""), "Labels": f"x={SENTINEL_SECRET}", "Extra": [1, 2]},
               ps_entry("engine", "running", "healthy"), ps_entry("redis", "running", "healthy")]
    states = parse_ps(ps_output(entries, fmt="array"))
    assert _by_service(states)["worker"] == ("running", "none")
    assert SENTINEL_SECRET not in repr(states)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [(f"127.0.0.1:{TEST_API_PORT}\n".encode(), TEST_API_PORT), (b"0.0.0.0:8000\n", 8000),
     (f"0.0.0.0:{OTHER_API_PORT}\n".encode(), OTHER_API_PORT), (f"[::]:{OTHER_API_PORT}\n".encode(), OTHER_API_PORT),
     (b"", None), (b"\n", None), (b"sem porta publicada\n", None)],
    ids=["loopback", "qualquer_8000", "outra", "ipv6", "vazio", "linha_vazia", "lixo"])
def test_parse_port(stdout, expected) -> None:
    """A porta publicada de `P port api 8000` (`<host>:<porta>`), ou `None`."""
    assert parse_port(stdout) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2.27.0", (2, 27, 0)), ("2.26.1\n", (2, 26, 1)), ("2.29.7", (2, 29, 7)), ("", None), ("versão", None)],
    ids=["2_27_0", "2_26_1_com_quebra", "2_29_7", "vazio", "ilegivel"])
def test_parse_compose_version(text, expected) -> None:
    """a versão de `docker compose version --short`, ou `None` se ilegível."""
    assert parse_compose_version(text) == expected


def test_clean_log_line_removes_control_sequences() -> None:
    """`ESC[2K` (Compose 2.26.1, mesmo com `--no-color`), CSI, OSC (com BEL e com ST), C0 e C1 e o
    CR saem; os bytes inválidos viram U+FFFD; o tab fica."""
    raw_lines = LOGS_RAW.split(b"\n")
    assert raw_lines[-1] == b""
    raw_lines = raw_lines[:-1]
    assert len(raw_lines) == len(LOGS_CLEAN)
    for raw, clean in zip(raw_lines, LOGS_CLEAN, strict=True):
        assert clean_log_line(raw) == clean
    assert "\x1b" not in "".join(clean_log_line(r) for r in raw_lines)


@pytest.mark.parametrize("kind", sorted(FAILURE_STDERR))
def test_classify_failure(kind) -> None:
    """O stderr de cada falha vira o erro fixo da classe (variável, indisponível, porta,
    construção, outra), com o nome da variável só se casar `^CQ_[A-Z0-9_]+$`, sem o texto do Docker."""
    exc = classify_failure(FAILURE_STDERR[kind])
    assert isinstance(exc, StackError)
    _assert_failure(exc, kind)


@pytest.mark.parametrize(
    ("first", "second", "winner"),
    [("variable_missing", "port_allocated", "variable_missing"),
     ("port_allocated", "daemon_stopped", "daemon_stopped"),
     ("build_failed", "port_address_in_use", "port_address_in_use"),
     ("generic", "build_failed_target", "build_failed_target")],
    ids=["variavel_antes_de_porta", "indisponivel_antes_de_porta", "porta_antes_de_construcao",
         "construcao_antes_de_outra"])
def test_classify_failure_order(first, second, winner) -> None:
    """A classificação segue a ordem variável → indisponível → porta → construção → outra, qualquer
    que seja a ordem das linhas no stderr."""
    for stderr in (FAILURE_STDERR[first] + FAILURE_STDERR[second], FAILURE_STDERR[second] + FAILURE_STDERR[first]):
        _assert_failure(classify_failure(stderr), winner)


def test_docker_env() -> None:
    """o ambiente do subprocesso é o do processo sem `COMPOSE_*`, `CQ_TOKEN`, `CQ_API_URL`,
    `CQ_CONFIG`, `CQ_PROFILE` e `CQ_STACK_DIR`, com `COMPOSE_ANSI=never` e `NO_COLOR=1`; as demais `CQ_*` (as da
    interpolação do `compose.yaml`) e o resto ficam; o mapa recebido não muda."""
    given = dict(PROCESS_ENV)
    assert docker_env(given) == EXPECTED_DOCKER_ENV
    assert given == PROCESS_ENV
    assert docker_env({}) == {"COMPOSE_ANSI": "never", "NO_COLOR": "1"}


# --------------------------------------------------------------------------------------------------
# ComposeStack sobre o FakeRunner: o argv exato de cada operação


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_snapshot_argv_and_result(tmp_path, version) -> None:
    """O estado por `P ps --all --format json` e a porta por `P port api 8000`, sobre o prefixo do
    projeto; o instantâneo com os 4 serviços e a porta publicada; nada fora da lista fechada, e o ambiente do
    subprocesso em toda chamada."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner)
    snap = stack.snapshot()
    assert isinstance(snap, StackSnapshot)
    assert _by_service(snap.services) == {"postgres": ("running", "healthy"), "api": ("running", "healthy"),
                                          "worker": ("running", "none"), "engine": ("running", "healthy")}
    assert snap.api_port == TEST_API_PORT
    assert _has(runner, PROJECT, stack_dir, ["ps", "--all", "--format", "json"])
    assert _has(runner, PROJECT, stack_dir, ["port", "api", "8000"])
    assert_allowed(runner.calls, PROJECT, stack_dir)
    assert all(call.env == EXPECTED_DOCKER_ENV for call in runner.calls)
    assert SENTINEL_SECRET not in repr(snap)


def test_snapshot_of_absent_and_partial_stack(tmp_path) -> None:
    """A pilha nunca criada (o `ps` vazio, sem porta) e a parcial (sem a api publicada)."""
    runner = FakeRunner({"ps": "empty", "port": PORT_NONE})
    stack, stack_dir = _stack(tmp_path, runner, project=OTHER_PROJECT)
    snap = stack.snapshot()
    assert _by_service(snap.services) == {s: ("absent", "none") for s in SERVICES}
    assert snap.api_port is None
    runner.set_sample("ps", "partial")
    assert _by_service(stack.snapshot().services)["worker"] == ("stopped", "none")
    assert_allowed(runner.calls, OTHER_PROJECT, stack_dir)


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_up_argv(tmp_path, version) -> None:
    """`up` sem reconstruir é `P up -d --no-recreate` (nunca `build`, `down` nem opção de volume)."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner)
    assert stack.up(rebuild=False) is None
    assert _has(runner, PROJECT, stack_dir, ["up", "-d", "--no-recreate"])
    assert not _has(runner, PROJECT, stack_dir, ["up", "-d"])
    assert "build" not in runner.operations() and "down" not in runner.operations()
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_up_rebuild_argv(tmp_path, version) -> None:
    """`--rebuild` é `P build api worker` seguido de `P up -d`, nessa ordem."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner)
    stack.up(rebuild=True)
    build = _has(runner, PROJECT, stack_dir, ["build", "api", "worker"])
    up = _has(runner, PROJECT, stack_dir, ["up", "-d"])
    assert build and up and build[0] < up[0]
    assert not _has(runner, PROJECT, stack_dir, ["up", "-d", "--no-recreate"])
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_down_argv(tmp_path, version) -> None:
    """`down` é `P down`, sem `-v`, `--volumes` nem `--rmi` (os dados ficam)."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner)
    assert stack.down() is None
    assert _has(runner, PROJECT, stack_dir, ["down"])
    assert all(t not in argv for argv in runner.argvs() for t in ("-v", "--volumes", "--rmi"))
    assert "up" not in runner.operations()
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("follow", [False, True], ids=["sem_follow", "follow"])
@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_logs_argv_and_clean_lines(tmp_path, version, follow) -> None:
    """`P logs --no-color --tail <N> [--follow] <serviços>`, com a lista explícita (nunca o
    `engine`), e as linhas já limpas (sem controles, com U+FFFD)."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner)
    lines = list(stack.logs(["api", "worker", "postgres"], tail=200, follow=follow))
    assert lines == list(LOGS_CLEAN)
    rest = ["logs", "--no-color", "--tail", "200"] + (["--follow"] if follow else []) + ["api", "worker", "postgres"]
    assert _has(runner, PROJECT, stack_dir, rest)
    assert all("engine" not in argv[8:] for argv in runner.argvs())
    runner.calls.clear()
    assert list(stack.logs(["worker"], tail=5, follow=False)) == list(LOGS_CLEAN)
    assert _has(runner, PROJECT, stack_dir, ["logs", "--no-color", "--tail", "5", "worker"])
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_versions_image_and_docker_available(tmp_path, version) -> None:
    """`docker compose version --short`, `docker version --format {{.Server.Version}}` e
    `docker image inspect --format {{.Id}} <projeto>-engine`, sem o prefixo do Compose."""
    runner = FakeRunner(version=version)
    stack, stack_dir = _stack(tmp_path, runner, project=OTHER_PROJECT)
    assert stack.compose_version() == tuple(int(p) for p in version.split("."))
    assert stack.docker_available() is True
    assert stack.engine_image_id() == ENGINE_IMAGE_ID
    argvs = runner.argvs()
    assert ["docker", "compose", "version", "--short"] in argvs
    assert ["docker", "version", "--format", "{{.Server.Version}}"] in argvs
    assert ["docker", "image", "inspect", "--format", "{{.Id}}", f"{OTHER_PROJECT}-engine"] in argvs
    runner.set_sample("image_inspect", "absent")
    assert stack.engine_image_id() is None
    assert_allowed(runner.calls, OTHER_PROJECT, stack_dir)


@pytest.mark.parametrize("sample", [*DAEMON_FAILURES, DOCKER_MISSING])
def test_docker_unavailable_is_false(tmp_path, sample) -> None:
    """com o daemon parado ou sem o `docker` no `PATH`, `docker_available()` é falso, sem exceção."""
    runner = FakeRunner(failure_samples(sample))
    stack, stack_dir = _stack(tmp_path, runner)
    assert stack.docker_available() is False
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("method", ["up", "down", "logs"])
def test_env_file_missing_refused_before_any_call(tmp_path, method) -> None:
    """sem o `.env` no diretório da pilha, `up`, `down` e `logs` levantam
    `StackEnvMissing` (2) antes de qualquer chamada ao Docker; o `.env` não é criado."""
    runner = FakeRunner()
    stack, stack_dir = _stack(tmp_path, runner, env_file=False)
    with pytest.raises(StackEnvMissing) as exc:
        if method == "up":
            stack.up(rebuild=False)
        elif method == "down":
            stack.down()
        else:
            list(stack.logs(["api"], tail=200, follow=False))
    assert exc.value.exit_code == 2
    assert runner.calls == []
    assert not (stack_dir / ".env").exists()


# --------------------------------------------------------------------------------------------------
# Falhas classificadas, sem o texto do Docker


def _call(stack: ComposeStack, method: str) -> Any:
    if method == "snapshot":
        return stack.snapshot()
    if method == "up":
        return stack.up(rebuild=False)
    if method == "rebuild":
        return stack.up(rebuild=True)
    if method == "down":
        return stack.down()
    return list(stack.logs(["api", "worker"], tail=200, follow=method == "follow"))


_FAILURE_CASES = (
    [("up", kind) for kind in sorted(FAILURE_STDERR)]
    + [("rebuild", kind) for kind in BUILD_FAILURES]
    + [(method, kind) for method in ("snapshot", "down", "logs", "follow")
       for kind in ("variable_missing", "variable_missing_unnamed", "daemon_stopped", "generic")]
)


@pytest.mark.parametrize(("method", "kind"), _FAILURE_CASES, ids=[f"{m}-{k}" for m, k in _FAILURE_CASES])
def test_compose_failures_are_classified_without_docker_text(tmp_path, method, kind) -> None:
    """cada falha do Compose (no `up`, no `up --rebuild`, no estado, no `down` e nos
    registros) vira o erro fixo da classe, com o seu código fixo, e nenhuma exceção mostra o
    segredo-sentinela do stderr."""
    runner = FakeRunner(failure_samples(kind))
    stack, stack_dir = _stack(tmp_path, runner)
    with pytest.raises(StackError) as exc:
        _call(stack, method)
    _assert_failure(exc.value, kind)
    assert_allowed(runner.calls, PROJECT, stack_dir)


@pytest.mark.parametrize("method", ["snapshot", "up", "down", "logs"])
def test_docker_missing_is_unavailable(tmp_path, method) -> None:
    """o `docker` fora do `PATH` → `DockerUnavailable` (1), sem rastreamento do executor."""
    runner = FakeRunner(failure_samples(DOCKER_MISSING))
    stack, _ = _stack(tmp_path, runner)
    with pytest.raises(DockerUnavailable) as exc:
        _call(stack, method)
    assert exc.value.exit_code == 1
    _assert_no_sentinel(exc.value)


# --------------------------------------------------------------------------------------------------
# SubprocessRunner: sem shell, com o `docker` do `PATH`


class _FakePopen:
    """Um `Popen` que não roda nada: registra os argumentos e devolve a versão do servidor."""

    calls: list[tuple[Any, dict]] = []

    def __init__(self, args: Any, **kwargs: Any) -> None:
        _FakePopen.calls.append((args, kwargs))
        self.args = args
        self.returncode = 0
        self.pid = 4242
        self.stdout = None
        self.stderr = None

    def communicate(self, input: Any = None, timeout: Any = None) -> tuple[bytes, bytes]:
        return b"27.0.3\n", b""

    def wait(self, timeout: Any = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        pass

    def terminate(self) -> None:
        pass

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def test_subprocess_runner_without_docker_on_path(monkeypatch) -> None:
    """sem o `docker` no `PATH` (`shutil.which("docker")` vazio), o executor real
    levanta `DockerUnavailable` sem rodar processo nenhum."""
    import shutil

    seen: list[Any] = []
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(("run", a, k)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: seen.append(("Popen", a, k)))
    runner = SubprocessRunner()
    with pytest.raises(DockerUnavailable) as exc:
        runner.run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10, env={"PATH": "x"})
    assert exc.value.exit_code == 1
    assert seen == []


def test_subprocess_runner_runs_without_shell(monkeypatch) -> None:
    """O executor real usa o `docker` achado no `PATH`, sem shell, com o ambiente recebido,
    e devolve a saída em bytes (o `RunResult` não mostra a saída no `repr`)."""
    import shutil

    docker = str(pathlib.Path("C:/ferramentas/docker/docker.exe"))
    runs: list[tuple[Any, dict]] = []

    def fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        runs.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"27.0.3\n", b"")

    _FakePopen.calls = []
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: docker if name == "docker" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    env = {"PATH": "C:/ferramentas/docker", "COMPOSE_ANSI": "never", "NO_COLOR": "1"}
    result = SubprocessRunner().run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10, env=env)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"27.0.3\n", b"")
    assert "27.0.3" not in repr(result)
    calls = runs + _FakePopen.calls
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert not kwargs.get("shell", False)
    assert not isinstance(args, str)
    assert list(args)[1:] == ["version", "--format", "{{.Server.Version}}"]
    assert list(args)[0] in (docker, "docker")
    assert kwargs.get("env") == env
