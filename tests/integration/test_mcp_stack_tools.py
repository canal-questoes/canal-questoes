"""Ferramentas locais da pilha pelo agente: o roteiro das cinco, o `up` sem reconstruir imagens, uma operação da pilha
por vez no servidor (a trava) e o perfil remoto recusado nas ferramentas locais.

O servidor MCP roda em processo (`tests/helpers/mcp_client.py`: `make_deps`, `in_process`, `call`), com o ambiente
explícito (o `CQ_CONFIG` num arquivo de perfil do `tmp_path`), a API dos testes por `http=` (só a sonda de saúde do
`status` e do `up`) e o Docker sempre dublê:
- **nível 1**: o `FakeStack` (`tests/helpers/stack_fake.py`) em `ServerDeps.stack`;
- **nível 2**: o `ComposeStack` real sobre o `FakeRunner` (`tests/helpers/docker_fake.py`) em `ServerDeps.runner`,
  com o `assert_allowed` da lista fechada do CLI.
Cada ferramenta é comparada com o comando do CLI equivalente (`cq status`, `cq up`, `cq down`, `cq logs`, pelo
`invoke_cli`) sobre um dublê gêmeo (mesma configuração): o mesmo corpo e as mesmas chamadas ao Docker (o mesmo argv, no
nível 2). As amostras de registros são as de `tests/helpers/log_samples.py`.

O relógio monotônico (`ServerDeps.clock`) e o `sleep` são o `FakeSleep` sobre um `FakeClock`: a espera do `up` e o
vencimento do código do `down` rodam sem esperar de verdade. O relógio de parede é outro `FakeClock` (o `expires_at`
informativo). O `new_code` é determinístico (`CodeSequence`). O `deps.stderr` é um `StringIO`: nenhuma ferramenta
escreve nele, em sucesso ou em erro. O stdout e o stderr reais do processo (o `capfd`, esvaziado antes
da sessão e lido logo depois dela, antes de qualquer comando do CLI) também ficam vazios: no `cq mcp` a saída padrão
é o canal do protocolo, e uma linha de progresso do `run_up` (ou qualquer `print`) ali corromperia a sessão.


As falhas da pilha viram o erro de execução `[stack.<Classe>] <mensagem>`
(`stack.StackFailure.<kind>`, `stack.StackVariableMissing[.unnamed]`), e o arquivo de perfil quebrado vira
`[profile.InvalidProfileFile] <reason>` nas cinco, nos dois níveis do dublê. Cada falha é provocada também no
comando do CLI sobre um gêmeo: o controle de que o dublê chega à mesma classe (o código de saída e a linha fixa
do CLI) e de que a ferramenta pede ao Docker o mesmo que o comando.

A trava usa a `BlockingStack(FakeStack)` deste arquivo, com um `threading.Event` no `up` e outro no `down`,
sem tocar o congelado `stack_fake.py`; as chamadas simultâneas saem do mesmo cliente em processo, em tarefas do
`anyio`.

No esqueleto, `build_server` lança `NotImplementedError` (cada função falha já ao abrir o cliente). As comparações com
o `cq` do CLI (que já funciona) ficam na mesma função de uma chamada MCP; o `cq logs` sem redação fica
na mesma função da `stack_logs` redigida sobre as mesmas amostras.
"""

from __future__ import annotations

import io
import json
import pathlib
import threading
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import anyio
import anyio.to_thread
import jsonschema
import pytest

from cq_client.mcp_server import (
    DOWN_CODE_TTL_SECONDS,
    DOWN_NOTICE,
    ERROR_TEXT_TEMPLATE,
    INVALID_PARAMS_CODE,
    LOGS_TAIL_MAX,
    MCP_MESSAGES,
    PROTOCOL_MESSAGES,
    ServerDeps,
)
from cq_client.stack import (
    LOG_SERVICES,
    SERVICES,
    STACK_ENV_FILE,
    DockerUnavailable,
    StackDirNotFound,
    StackEnvMissing,
    StackError,
    StackFailure,
    StackTargetRefused,
    StackVariableMissing,
)

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.canary import find_canary, find_secrets
from tests.helpers.cli_runner import ENV_SENTINEL, ENV_SENTINEL_TOKEN, invoke_cli, make_stack_dir, write_profile
from tests.helpers.clock import FakeClock
from tests.helpers.contract import assert_cli_json
from tests.helpers.docker_fake import DOCKER_MISSING, SENTINEL_SECRET, FakeRunner, assert_allowed, failure_samples
from tests.helpers.log_samples import (
    ALL_SAMPLES,
    BULK_DEFAULT,
    CANARY_PREFIXES,
    DROPPED,
    RESULT_LIMIT_CHARS,
    SENTINELS,
    LogSample,
    bulk,
    lines,
)
from tests.helpers.mcp_client import MODES, Outcome, call, in_process, make_deps, run, scan
from tests.helpers.stack_fake import TEST_API_PORT, TEST_API_URL, FakeSleep, FakeStack, port_mismatch, snapshot

REPO = pathlib.Path(__file__).resolve().parents[2]
MCP_SCHEMAS = REPO / "contracts" / "mcp" / "v1" / "schemas"

PROJECT = "cq"
MODE = MODES[0]
# Teto de cada espera da trava (abaixo do `CALL_TIMEOUT_SECONDS` do cliente em processo).
BLOCK_TIMEOUT = 5.0
# O `expires_at` do primeiro preparo: o relógio de parede (`FakeClock`, início em 2031-01-01) + 5 minutos.
FIRST_EXPIRES_AT = "2031-01-01T00:05:00Z"

LOCAL_TOOLS = ("stack_status", "stack_up", "prepare_stack_down", "stack_down", "stack_logs")
# Um código no formato do `down` que nunca é emitido nos testes (a `CodeSequence` usa outro prefixo).
UNISSUED_CODE = "codigoNuncaEmitido_001"
# O `tail` padrão da `stack_logs` (200, no contrato do MCP) como literal, e não a constante `LOGS_TAIL_DEFAULT`
# do código de produção, que não é congelado. O teto do resultado é o `RESULT_LIMIT_CHARS` de `log_samples`.
SPEC_TAIL_DEFAULT = 200

SAMPLE_LIST: list[LogSample] = list(ALL_SAMPLES.values())
RAW_LINES: list[str] = lines(SAMPLE_LIST)
RAW_BYTES: bytes = b"".join(line.encode("utf-8") + b"\n" for line in RAW_LINES)


def _local_profile(*, token: bool = True) -> str:
    return f'api_url = "{TEST_API_URL}"\n' + (f'token = "{ADMIN_TOKEN}"\n' if token else "")


REMOTE_PROFILE = (f'default_profile = "vm"\n\n[profiles.vm]\nkind = "remote"\napi_url = "https://vm.cq.test"\n'
                  f'token = "{ADMIN_TOKEN}"\n')
OTHER_PROJECT_PROFILE = (f'default_profile = "dev"\n\n[profiles.dev]\nkind = "local"\napi_url = "{TEST_API_URL}"\n'
                         f'token = "{ADMIN_TOKEN}"\ncompose_project = "cq-dev"\n')


def _reserved_profile(project: str) -> str:
    return (f'default_profile = "demo"\n\n[profiles.demo]\nkind = "local"\napi_url = "{TEST_API_URL}"\n'
            f'token = "{ADMIN_TOKEN}"\ncompose_project = "{project}"\n')


class CodeSequence:
    """O `new_code` injetável: códigos determinísticos no formato do `token_urlsafe(16)` (22 caracteres)."""

    def __init__(self) -> None:
        self.issued: list[str] = []

    def __call__(self) -> str:
        code = f"codigoDeTesteDown{len(self.issued) + 1:05d}"
        self.issued.append(code)
        return code


class Rig:
    """O arquivo de perfil, o diretório da pilha (com os marcadores e o `.env`), os relógios, o `new_code`, o
    `stderr` do servidor e a captura da saída real do processo (`capfd`) de um teste."""

    def __init__(self, tmp_path: pathlib.Path, capfd: Any, profile: str | None = None) -> None:
        self.tmp_path = tmp_path
        self.stack_dir = make_stack_dir(tmp_path)
        self.config = write_profile(tmp_path, profile if profile is not None else _local_profile())
        self.sleep = FakeSleep(FakeClock())
        self.wall = FakeClock()
        self.codes = CodeSequence()
        self.stderr = io.StringIO()
        self.capfd = capfd
        self.process_output: tuple[str, str] | None = None

    def write(self, text: str) -> None:
        """Troca o perfil (entre as fases, ou com uma chamada em andamento)."""
        self.config.write_bytes(text.encode("utf-8"))

    def deps(self, client: Any, *, stack: Any = None, runner: Any = None,
             env: dict[str, str] | None = None) -> ServerDeps:
        return make_deps(self.tmp_path, env={"CQ_CONFIG": str(self.config), **(env or {})}, http=client,
                         stack=stack, runner=runner, cwd=self.stack_dir, clock=self.sleep.monotonic,
                         sleep=self.sleep, wall_clock=self.wall.now, new_code=self.codes, stderr=self.stderr)

    def session(self, deps: ServerDeps, steps: Callable[[Any], Awaitable[Any]], mode: str = MODE) -> Any:
        """`_session`, com a saída real do processo esvaziada antes e guardada logo depois (antes de qualquer
        comando do CLI, cujo `CliRunner` troca o `sys.stdout`)."""
        self.capfd.readouterr()
        try:
            return _session(deps, steps, mode)
        finally:
            captured = self.capfd.readouterr()
            self.process_output = (captured.out, captured.err)

    def assert_quiet(self) -> None:
        """Nenhuma ferramenta escreve no `stderr` do servidor, e nada sai no stdout
        nem no stderr reais do processo durante a sessão (nenhuma linha de progresso)."""
        assert self.stderr.getvalue() == "", self.stderr.getvalue()
        assert self.process_output is not None, "a sessão não passou por Rig.session"
        out, err = self.process_output
        assert out == "", f"saída no stdout do processo: {out[:200]!r}"
        assert err == "", f"saída no stderr do processo: {err[:200]!r}"


class BlockingStack(FakeStack):
    """O `FakeStack` com o `up` e o `down` presos até o teste soltar (`up_release`, `down_release`); `*_entered`
    avisa que a chamada chegou ao Docker dublê; `*_started` conta as chegadas (antes do registro em `calls`)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.up_entered = threading.Event()
        self.up_release = threading.Event()
        self.down_entered = threading.Event()
        self.down_release = threading.Event()
        self.up_started = 0
        self.down_started = 0

    def up(self, *, rebuild: bool) -> None:
        self.up_started += 1
        self.up_entered.set()
        self.up_release.wait(BLOCK_TIMEOUT)
        super().up(rebuild=rebuild)

    def down(self) -> None:
        self.down_started += 1
        self.down_entered.set()
        self.down_release.wait(BLOCK_TIMEOUT)
        super().down()


# --------------------------------------------------------------------------------------------------
# Apoio


def _session(deps: ServerDeps, steps: Callable[[Any], Awaitable[Any]], mode: str = MODE) -> Any:
    """Abre o cliente em processo sobre `build_server(deps)` e roda `steps(client)`."""

    async def main() -> Any:
        async with in_process(deps, mode) as client:
            return await steps(client)

    return run(main)


def _ok(outcome: Outcome) -> dict:
    """Sucesso com `structuredContent` e o texto igual ao JSON dele."""
    assert not outcome.is_protocol_error, outcome.raw
    assert outcome.is_error is False, outcome.text
    assert isinstance(outcome.structured, dict), outcome.raw
    assert outcome.text is not None and json.loads(outcome.text) == outcome.structured
    return outcome.structured


def _error_text(key: str, **values: Any) -> str:
    message = MCP_MESSAGES[key]
    if values:
        message = message.format(**values)
    return ERROR_TEXT_TEMPLATE.format(code=key, message=message)


def _assert_error(outcome: Outcome, key: str, **values: Any) -> None:
    """Erro de execução, sem `structuredContent`, com o texto fixo da chave."""
    assert not outcome.is_protocol_error, outcome.raw
    assert outcome.is_error is True, outcome.raw
    assert outcome.structured is None
    assert outcome.text == _error_text(key, **values)


def _assert_invalid_params(outcome: Outcome) -> None:
    """Recusa de protocolo: `-32602`, a mensagem fixa, sem `data`."""
    assert outcome.protocol_code == INVALID_PARAMS_CODE, outcome.raw
    assert outcome.protocol_message == PROTOCOL_MESSAGES["invalid_arguments"]
    assert "data" not in outcome.raw


def _validate(name: str, body: Any) -> None:
    schema = json.loads((MCP_SCHEMAS / f"{name}.output.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(body)


def _compact(value: Any) -> str:
    """O texto de um sucesso, o JSON compacto do corpo."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _services_json(snap: Any) -> list[dict]:
    return [{"service": s.service, "state": s.state, "health": s.health} for s in snap.services]


def _up_body() -> dict:
    return {"project": PROJECT, "services": _services_json(snapshot("healthy")), "api_port": TEST_API_PORT}


def _cli(rig: Rig, args: list[str], client: Any, *, json_mode: bool = False, stack: Any = None,
         runner: Any = None, config: pathlib.Path | None = None, env: dict[str, str] | None = None) -> Any:
    """O comando do CLI sobre o dublê gêmeo, com o mesmo perfil e o mesmo diretório da pilha (e, com `env`,
    as mesmas variáveis a mais do servidor)."""
    sleep = FakeSleep(FakeClock())
    obj: dict[str, Any] = {"clock": sleep.monotonic, "sleep": sleep, "cwd": rig.stack_dir, "http": client}
    if stack is not None:
        obj["stack"] = stack
    if runner is not None:
        obj["runner"] = runner
    result = invoke_cli(args, json=json_mode, obj=obj, config=config if config is not None else rig.config,
                       env=env)
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    return result


def _expected_counts(samples: list[LogSample], requested: tuple[str, ...] = LOG_SERVICES) -> dict[str, int]:
    kept = [s for s in samples if s.outcome != DROPPED and s.service in requested]
    dropped = len(samples) - len(kept)
    return {"returned": len(kept), "redacted": sum(1 for s in kept if s.outcome == "redacted"),
            "omitted": sum(1 for s in kept if s.outcome == "omitted") + dropped}


def _assert_logs_body(body: dict, samples: list[LogSample], requested: tuple[str, ...] = LOG_SERVICES) -> None:
    """Corpo da `stack_logs`: o esquema, as linhas na ordem (sem as descartadas), as contagens e nenhum canário nem
    sentinela."""
    _validate("stack_logs", body)
    kept = [s for s in samples if s.outcome != DROPPED and s.service in requested]
    assert [line["service"] for line in body["lines"]] == [s.service for s in kept]
    counts = _expected_counts(samples, requested)
    assert {k: body[k] for k in counts} == counts
    assert body["truncated"] is False
    text = json.dumps(body, ensure_ascii=False)
    assert find_canary(text, prefixes=CANARY_PREFIXES) == []
    assert find_secrets(text, list(SENTINELS)) == []


def _assert_positive_control(raw: str) -> None:
    """O controle positivo: as amostras cruas têm as identidades canário e cada segredo-sentinela."""
    assert find_canary(raw, prefixes=CANARY_PREFIXES), "controle: as amostras têm identidades canário"
    for secret in SENTINELS:
        assert find_secrets(raw, [secret]), f"controle: o sentinela {secret[:6]}… não está nas amostras"


# --------------------------------------------------------------------------------------------------
# Roteiro da pilha: o roteiro das cinco, nos dois níveis e nas duas eras


@pytest.mark.parametrize("mode", MODES)
def test_stack_walkthrough_level1(tmp_path, client, capfd, mode) -> None:
    """Roteiro da pilha: pelo `FakeStack`, `stack_status` com a pilha
    saudável → o mesmo corpo do `cq status --json` (válido em `status.output`); `stack_up` da pilha parada → o corpo do
    `cq up --json`, sem reconstruir; `stack_logs` sem argumentos → os três serviços e o `tail` padrão (200, o
    literal), redigido, sem identidade nem segredo, com as contagens;
    `prepare_stack_down` → projeto, serviços, aviso, código e `expires_at`, sem mudar nada; `stack_down` com o código →
    o corpo do `cq down --json`, e o código não vale mais. Cada uma com as mesmas chamadas ao dublê que o comando do
    CLI sobre um gêmeo; o `cq logs` do CLI continua sem redação sobre as mesmas amostras; nada no stderr do
    servidor nem no stdout e no stderr reais do processo."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"], logs=RAW_LINES)
    marks: dict[str, int] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        out["status"] = await call(c, "stack_status", {})
        marks["status"] = len(stack.calls)
        stack.set_timeline(["stopped", "starting", "healthy"])
        out["up"] = await call(c, "stack_up", {})
        marks["up"] = len(stack.calls)
        out["logs"] = await call(c, "stack_logs", {})
        marks["logs"] = len(stack.calls)
        out["prepare"] = await call(c, "prepare_stack_down", {})
        marks["prepare"] = len(stack.calls)
        code = _ok(out["prepare"])["confirmation_code"]
        out["down"] = await call(c, "stack_down", {"confirmation_code": code})
        marks["down"] = len(stack.calls)
        out["down_again"] = await call(c, "stack_down", {"confirmation_code": code})
        marks["down_again"] = len(stack.calls)
        return out

    out = rig.session(rig.deps(client, stack=stack), steps, mode)
    calls = stack.calls

    # stack_status = cq status --json
    status = _ok(out["status"])
    status_twin = FakeStack(["healthy"], logs=RAW_LINES)
    cli_status = _cli(rig, ["status"], client, json_mode=True, stack=status_twin)
    assert cli_status.exit_code == 0, cli_status.stderr
    assert status == assert_cli_json("status", cli_status.stdout)
    assert calls[:marks["status"]] == status_twin.calls

    # stack_up = cq up --json (sem reconstruir)
    up = _ok(out["up"])
    assert assert_cli_json("up", up) == _up_body()
    up_twin = FakeStack(["stopped", "starting", "healthy"])
    cli_up = _cli(rig, ["up"], client, json_mode=True, stack=up_twin)
    assert cli_up.exit_code == 0, cli_up.stderr
    assert up == json.loads(cli_up.stdout)
    assert calls[marks["status"]:marks["up"]] == up_twin.calls
    assert [c for c in up_twin.calls if c[0] == "up"] == [("up", {"rebuild": False})]

    # stack_logs redigido; cq logs sem redação
    logs = _ok(out["logs"])
    _assert_logs_body(logs, SAMPLE_LIST)
    logs_twin = FakeStack(["healthy"], logs=RAW_LINES)
    cli_logs = _cli(rig, ["logs"], client, stack=logs_twin)
    assert cli_logs.exit_code == 0, cli_logs.stderr
    assert cli_logs.stdout == "".join(f"{line}\n" for line in RAW_LINES)
    _assert_positive_control(cli_logs.stdout)
    assert calls[marks["up"]:marks["logs"]] == logs_twin.calls
    assert logs_twin.calls == [("logs", {"services": LOG_SERVICES, "tail": SPEC_TAIL_DEFAULT, "follow": False})]

    # prepare_stack_down: nada muda
    prepare = _ok(out["prepare"])
    _validate("prepare_stack_down", prepare)
    assert prepare == {"project": PROJECT, "services": list(SERVICES), "notice": DOWN_NOTICE,
                       "confirmation_code": rig.codes.issued[-1], "expires_at": FIRST_EXPIRES_AT}
    assert {name for name, _ in calls[marks["logs"]:marks["prepare"]]} <= {"snapshot"}

    # stack_down = cq down --json; o código não vale mais
    down = _ok(out["down"])
    down_twin = FakeStack(["healthy"])
    cli_down = _cli(rig, ["down"], client, json_mode=True, stack=down_twin)
    assert cli_down.exit_code == 0, cli_down.stderr
    assert down == assert_cli_json("down", cli_down.stdout) == {"project": PROJECT}
    assert calls[marks["prepare"]:marks["down"]] == down_twin.calls == [("down", {})]
    _assert_error(out["down_again"], "down_code_invalid")
    assert calls[marks["down"]:marks["down_again"]] == []
    assert [name for name, _ in calls].count("up") == 1 and [name for name, _ in calls].count("down") == 1
    rig.assert_quiet()


@pytest.mark.parametrize("mode", MODES)
def test_stack_walkthrough_level2(tmp_path, client, capfd, mode) -> None:
    """Roteiro da pilha: o mesmo roteiro pelo `ComposeStack` real sobre o `FakeRunner`: cada ferramenta
    pede ao Docker exatamente o argv do comando do CLI equivalente sobre um executor gêmeo (o `cq status`, o `cq up`, o
    `cq logs`, o `cq down`); o preparo do `down` só consulta (sem `up`, `build` nem `down`); o `down` nunca pede volumes
    (`assert_allowed`); a `stack_logs` sai redigida e o `cq logs` do CLI, sobre as mesmas amostras, sai sem redação;
    nada no stdout nem no stderr reais do processo."""
    rig = Rig(tmp_path, capfd)
    runner = FakeRunner({"logs": RAW_BYTES})
    marks: dict[str, int] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        out["status"] = await call(c, "stack_status", {})
        marks["status"] = len(runner.calls)
        runner.set_sample("ps", ["stopped", "healthy"])
        out["up"] = await call(c, "stack_up", {})
        marks["up"] = len(runner.calls)
        runner.set_sample("ps", "healthy")
        out["logs"] = await call(c, "stack_logs", {})
        marks["logs"] = len(runner.calls)
        out["prepare"] = await call(c, "prepare_stack_down", {})
        marks["prepare"] = len(runner.calls)
        code = _ok(out["prepare"])["confirmation_code"]
        out["down"] = await call(c, "stack_down", {"confirmation_code": code})
        marks["down"] = len(runner.calls)
        out["down_again"] = await call(c, "stack_down", {"confirmation_code": code})
        marks["down_again"] = len(runner.calls)
        return out

    out = rig.session(rig.deps(client, runner=runner), steps, mode)
    argvs = runner.argvs()
    assert_allowed(runner.calls, PROJECT, rig.stack_dir)

    status_twin = FakeRunner({"logs": RAW_BYTES})
    cli_status = _cli(rig, ["status"], client, json_mode=True, runner=status_twin)
    assert cli_status.exit_code == 0, cli_status.stderr
    assert _ok(out["status"]) == assert_cli_json("status", cli_status.stdout)
    assert argvs[:marks["status"]] == status_twin.argvs()

    up_twin = FakeRunner({"ps": ["stopped", "healthy"]})
    cli_up = _cli(rig, ["up"], client, json_mode=True, runner=up_twin)
    assert cli_up.exit_code == 0, cli_up.stderr
    assert _ok(out["up"]) == assert_cli_json("up", cli_up.stdout)
    assert argvs[marks["status"]:marks["up"]] == up_twin.argvs()

    logs_twin = FakeRunner({"logs": RAW_BYTES})
    cli_logs = _cli(rig, ["logs"], client, runner=logs_twin)
    assert cli_logs.exit_code == 0, cli_logs.stderr
    _assert_positive_control(cli_logs.stdout)
    _assert_logs_body(_ok(out["logs"]), SAMPLE_LIST)
    assert argvs[marks["up"]:marks["logs"]] == logs_twin.argvs()

    prepare_ops = runner.operations()[marks["logs"]:marks["prepare"]]
    assert prepare_ops and not set(prepare_ops) & {"up", "build", "down", "logs"}, prepare_ops
    _validate("prepare_stack_down", _ok(out["prepare"]))

    down_twin = FakeRunner()
    cli_down = _cli(rig, ["down"], client, json_mode=True, runner=down_twin)
    assert cli_down.exit_code == 0, cli_down.stderr
    assert _ok(out["down"]) == assert_cli_json("down", cli_down.stdout) == {"project": PROJECT}
    assert argvs[marks["prepare"]:marks["down"]] == down_twin.argvs()
    assert [argv[-1] for argv in down_twin.argvs()] == ["down"]
    _assert_error(out["down_again"], "down_code_invalid")
    assert argvs[marks["down"]:marks["down_again"]] == []
    for twin in (status_twin, up_twin, logs_twin, down_twin):
        assert_allowed(twin.calls, PROJECT, rig.stack_dir)
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# Recusas antes de tudo


_REFUSALS = [("remote", REMOTE_PROFILE, "stack.RemoteProfileRefused"),
             ("cq-demo", _reserved_profile("cq-demo"), "profile.InvalidComposeProject"),
             ("cq-systest", _reserved_profile("cq-systest"), "profile.InvalidComposeProject")]


@pytest.mark.parametrize("level", ["stack", "runner"])
@pytest.mark.parametrize(("case", "profile", "key"), _REFUSALS, ids=[r[0] for r in _REFUSALS])
def test_remote_and_reserved_refused_in_all_five_without_docker(tmp_path, client, capfd, level, case, profile,
                                                                key) -> None:
    """com o perfil remoto (inclusive na `stack_status`) ou com
    um projeto reservado (`cq-demo`, `cq-systest`, recusado na resolução do perfil, como no CLI), as cinco falham com
    a mensagem fixa, sem nenhuma chamada ao Docker (nos dois níveis) e antes da trava e do código: o `stack_down` com
    um código nunca emitido dá a recusa do perfil, não `down_code_invalid`; nenhum código é emitido; nada no stderr."""
    rig = Rig(tmp_path, capfd, profile)
    stack = FakeStack(["healthy"], logs=RAW_LINES) if level == "stack" else None
    runner = FakeRunner() if level == "runner" else None
    arguments = {"stack_down": {"confirmation_code": UNISSUED_CODE}}

    async def steps(c: Any) -> dict[str, Outcome]:
        return {name: await call(c, name, arguments.get(name, {})) for name in LOCAL_TOOLS}

    out = rig.session(rig.deps(client, stack=stack, runner=runner), steps)
    for name in LOCAL_TOOLS:
        _assert_error(out[name], key)
    if stack is not None:
        assert stack.calls == []
    if runner is not None:
        assert runner.calls == []
    assert rig.codes.issued == []
    rig.assert_quiet()


def test_remote_stack_down_keeps_code(tmp_path, client, capfd) -> None:
    """o código emitido com o perfil local continua válido depois de um
    `stack_down` com o perfil trocado para o remoto (a recusa vem antes da conferência e não gasta o código); de volta
    ao perfil local, o mesmo código para a pilha."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"])

    seen: dict[str, int] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out = {"prepare": await call(c, "prepare_stack_down", {})}
        code = _ok(out["prepare"])["confirmation_code"]
        seen["before_remote"] = len(stack.calls)
        rig.write(REMOTE_PROFILE)
        out["remote"] = await call(c, "stack_down", {"confirmation_code": code})
        seen["after_remote"] = len(stack.calls)
        rig.write(_local_profile())
        out["local"] = await call(c, "stack_down", {"confirmation_code": code})
        return out

    out = rig.session(rig.deps(client, stack=stack), steps)
    _assert_error(out["remote"], "stack.RemoteProfileRefused")
    assert seen["after_remote"] == seen["before_remote"], "a recusa do remoto chamou o Docker"
    assert _ok(out["local"]) == {"project": PROJECT}
    assert [name for name, _ in stack.calls].count("down") == 1
    rig.assert_quiet()


def test_local_tools_work_without_token(tmp_path, client, capfd) -> None:
    """as cinco funcionam com um perfil local sem token (nenhuma passa pela API autenticada; a sonda de
    saúde do `status` e do `up` não pede token): o `status` mostra `token_set` falso; nada no stderr."""
    rig = Rig(tmp_path, capfd, _local_profile(token=False))
    stack = FakeStack(["healthy"], logs=RAW_LINES)

    async def steps(c: Any) -> dict[str, Outcome]:
        out = {"status": await call(c, "stack_status", {}), "up": await call(c, "stack_up", {}),
               "logs": await call(c, "stack_logs", {}), "prepare": await call(c, "prepare_stack_down", {})}
        code = _ok(out["prepare"])["confirmation_code"]
        out["down"] = await call(c, "stack_down", {"confirmation_code": code})
        return out

    out = rig.session(rig.deps(client, stack=stack), steps)
    status = assert_cli_json("status", _ok(out["status"]))
    assert status["profile"]["token_set"] is False
    assert _ok(out["up"]) == _up_body()
    _assert_logs_body(_ok(out["logs"]), SAMPLE_LIST)
    _validate("prepare_stack_down", _ok(out["prepare"]))
    assert _ok(out["down"]) == {"project": PROJECT}
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# stack_status e stack_up


@pytest.mark.parametrize("state", ["stopped", "absent", "docker_unavailable"])
def test_stack_status_report_is_not_an_error(tmp_path, client, capfd, state) -> None:
    """com a pilha parada, nunca criada ou com o Docker fora, a `stack_status` devolve o
    relatório normal (não erro), igual ao `cq status --json` (que sai com o código 1 e o mesmo corpo)."""
    rig = Rig(tmp_path, capfd)

    def make() -> FakeStack:
        return FakeStack.unavailable() if state == "docker_unavailable" else FakeStack([state])

    stack = make()

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_status", {})

    body = _ok(rig.session(rig.deps(client, stack=stack), steps))
    twin = make()
    cli = _cli(rig, ["status"], client, json_mode=True, stack=twin)
    assert cli.exit_code == 1
    assert body == assert_cli_json("status", cli.stdout)
    assert stack.calls == twin.calls
    if state == "docker_unavailable":
        assert body["warnings"] == ["docker_unavailable"]
    rig.assert_quiet()


def test_stack_up_idempotent(tmp_path, client, capfd) -> None:
    """com a pilha já saudável, `stack_up` devolve o corpo do `up` duas vezes, só pede o `up` sem
    reconstruir (idempotente) e nunca o `down`."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"])

    async def steps(c: Any) -> list[Outcome]:
        return [await call(c, "stack_up", {}), await call(c, "stack_up", {})]

    first, second = rig.session(rig.deps(client, stack=stack), steps)
    assert _ok(first) == _ok(second) == _up_body()
    assert [c for c in stack.calls if c[0] in ("up", "down")] == [("up", {"rebuild": False})] * 2
    rig.assert_quiet()


def test_stack_up_wait_exhausted(tmp_path, client, capfd) -> None:
    """a api em laço de reinício até esgotar a espera de 180 s (no relógio e no `sleep`
    falsos) → `[stack.UpTimeout]` com o tempo e os serviços, citando `stack_logs` e `run_doctor`, nunca `cq logs` nem
    `cq doctor`; nada derrubado; nenhuma linha de progresso do `run_up` no stdout nem no stderr reais do processo.
    """
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["stopped", "restart_loop"])

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_up", {})

    outcome = rig.session(rig.deps(client, stack=stack), steps)
    _assert_error(outcome, "stack.UpTimeout", seconds=180, services="api")
    assert "stack_logs" in outcome.text and "run_doctor" in outcome.text
    assert "cq logs" not in outcome.text and "cq doctor" not in outcome.text
    assert 179 <= rig.sleep.total <= 181
    assert "down" not in stack.names()
    rig.assert_quiet()


def test_stack_up_port_mismatch(tmp_path, client, capfd) -> None:
    """os quatro serviços "ok" com a api publicada noutra porta que a do perfil →
    `[stack.PortMismatch]`, nada derrubado."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["stopped", port_mismatch()])

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_up", {})

    _assert_error(rig.session(rig.deps(client, stack=stack), steps), "stack.PortMismatch")
    assert "down" not in stack.names()
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# Falhas da pilha e do perfil nas ferramentas locais


LEVELS = ["stack", "runner"]
# Os segredos que nenhuma falha pode ecoar: o do stderr do Docker dublê (nível 2) e os do `.env` de teste.
SECRETS = [SENTINEL_SECRET, ENV_SENTINEL, ENV_SENTINEL_TOKEN]
# Os métodos do `FakeStack` que uma falha "de toda chamada ao Compose" atinge (o escopo do `failure_samples`).
STACK_METHODS = ("snapshot", "up", "down", "logs")
MISSING_STACK_DIR = "pilha-sumida-Wv3"
REMOTE_DOCKER_HOST = "192.0.2.10"
REMOTE_DOCKER_ENV = {"DOCKER_HOST": f"tcp://{REMOTE_DOCKER_HOST}:2375"}
BROKEN_PROFILE_SENTINEL = "perfil-quebrado-sentinela-Jt4Rw8"
BROKEN_PROFILE = f'api_url = "{BROKEN_PROFILE_SENTINEL}\n[[[ isto não é TOML\n'


class ComposeFailure(NamedTuple):
    """Uma falha do Docker nos dois níveis: a amostra do `FakeRunner` (`failure_samples`, com o escopo realista),
    a exceção que o `FakeStack` levanta nos métodos do mesmo escopo, a chave do MCP com os valores
    do modelo, e se o `cq status` a trata como erro (código 2) em vez do relatório."""

    sample: str
    error: Callable[[], StackError]
    methods: tuple[str, ...]
    key: str
    values: dict[str, Any]
    status_is_error: bool


COMPOSE_FAILURES: dict[str, ComposeFailure] = {
    "docker_unavailable": ComposeFailure(DOCKER_MISSING, DockerUnavailable, STACK_METHODS,
                                         "stack.DockerUnavailable", {}, False),
    "variable_missing": ComposeFailure("variable_missing", lambda: StackVariableMissing("CQ_DB_PASSWORD"),
                                       STACK_METHODS, "stack.StackVariableMissing", {"name": "CQ_DB_PASSWORD"},
                                       True),
    # O nome fora de `^CQ_[A-Z0-9_]+$` não sai (o CLI o descarta).
    "variable_missing_unnamed": ComposeFailure("variable_missing_unnamed",
                                               lambda: StackVariableMissing("POSTGRES_PASSWORD"), STACK_METHODS,
                                               "stack.StackVariableMissing.unnamed", {}, True),
    "port_in_use": ComposeFailure("port_allocated", lambda: StackFailure("port_in_use"), ("up",),
                                  "stack.StackFailure.port_in_use", {}, False),
    "build_failed": ComposeFailure("build_failed", lambda: StackFailure("build_failed"), ("up",),
                                   "stack.StackFailure.build_failed", {}, False),
    "other": ComposeFailure("generic", lambda: StackFailure("other"), STACK_METHODS, "stack.StackFailure.other", {},
                            False),
}
# As falhas que atingem toda chamada ao Compose (e não só o `up`): alcançam também `status`, `logs`, o preparo e o
# `down`.
EVERY_CALL_FAILURES = [case for case, failure in COMPOSE_FAILURES.items() if failure.methods == STACK_METHODS]


def _doubles(level: str) -> tuple[FakeStack | None, FakeRunner | None]:
    """O dublê do nível: o `FakeStack` saudável (nível 1) ou o `FakeRunner` saudável (nível 2), com as amostras."""
    if level == "stack":
        return FakeStack(["healthy"], logs=RAW_LINES), None
    return None, FakeRunner({"logs": RAW_BYTES})


def _double(level: str) -> Any:
    stack, runner = _doubles(level)
    return stack if stack is not None else runner


def _calls(double: Any) -> list[Any]:
    """As chamadas ao Docker dublê: as do `FakeStack` (nível 1) ou os argv do `FakeRunner` (nível 2)."""
    return list(double.calls) if isinstance(double, FakeStack) else double.argvs()


def _operations(double: Any) -> list[Any]:
    return double.names() if isinstance(double, FakeStack) else double.operations()


def _inject(double: Any, failure: ComposeFailure) -> None:
    """Provoca a falha no dublê: a exceção nos métodos do escopo (nível 1) ou as amostras de falha (nível 2)."""
    if isinstance(double, FakeStack):
        for method in failure.methods:
            double.fail(method, failure.error())
    else:
        for operation, value in failure_samples(failure.sample).items():
            double.set_sample(operation, value)


def _failing_twin(level: str, failure: ComposeFailure) -> Any:
    twin = _double(level)
    _inject(twin, failure)
    return twin


def _assert_cli_failed(result: Any, error: StackError) -> None:
    """Controle no comando do CLI: o código de saída e a linha fixa da mesma classe, e o stdout vazio."""
    assert result.exit_code == error.exit_code, (result.exit_code, result.stderr)
    assert error.message in result.stderr, result.stderr
    assert result.stdout == ""


def _assert_no_echo(outcome: Outcome, *values: str) -> None:
    """Nenhum dos `values` no texto nem no resultado bruto (também na forma escapada do JSON)."""
    texts = [outcome.text or "", json.dumps(outcome.raw, ensure_ascii=False)]
    for value in values:
        for form in {value, json.dumps(value, ensure_ascii=False)[1:-1]}:
            assert all(form not in text for text in texts), f"eco de {value[:16]}…"


def _missing_dir_profile(path: pathlib.Path) -> str:
    """O perfil local nomeado (padrão) com um `stack_dir` que não existe (o mesmo projeto `cq`)."""
    return (f'default_profile = "local"\n\n[profiles.local]\nkind = "local"\napi_url = "{TEST_API_URL}"\n'
            f'token = "{ADMIN_TOKEN}"\nstack_dir = "{path.as_posix()}"\n')


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("case", list(COMPOSE_FAILURES))
def test_stack_up_failures_map_to_fixed_messages(tmp_path, client, capfd, level, case) -> None:
    """cada falha do Docker que o `cq up` alcança (o Docker indisponível, a variável
    ausente com e sem nome, e uma `StackFailure` de cada `kind`: porta ocupada, construção falha e outra) → na
    `stack_up`, o erro de execução `[stack.<Classe>] <mensagem fixa>` (`stack.StackFailure.<kind>`,
    `stack.StackVariableMissing[.unnamed]`), nunca `mcp_internal` nem a exceção crua, sem o stderr do Docker nem o
    `.env`, nos dois níveis do dublê, com as mesmas chamadas ao Docker que o `cq up` sobre um gêmeo com a mesma falha
    (controle: o CLI sai com o código e a linha fixa da mesma classe); nada derrubado; nenhuma linha de progresso no
    `deps.stderr` nem na saída real do processo."""
    failure = COMPOSE_FAILURES[case]
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner
    _inject(double, failure)

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_up", {})

    outcome = rig.session(rig.deps(client, stack=stack, runner=runner), steps)
    _assert_error(outcome, failure.key, **failure.values)
    assert scan(outcome, SECRETS) == []
    twin = _failing_twin(level, failure)
    _assert_cli_failed(_cli(rig, ["up"], client, json_mode=True, **{level: twin}), failure.error())
    assert _calls(double) == _calls(twin)
    assert "down" not in _operations(double)
    if runner is not None:
        assert_allowed(runner.calls, PROJECT, rig.stack_dir)
    rig.assert_quiet()


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("case", EVERY_CALL_FAILURES)
def test_stack_failures_in_status_logs_prepare_and_down(tmp_path, client, capfd, level, case) -> None:
    """com um código válido em mãos, o Docker
    passa a falhar em toda chamada (indisponível, variável ausente com e sem nome, outra falha do Compose):
    `stack_logs`, `prepare_stack_down` (sem emitir código novo; só consulta) e `stack_down` com o código falham com
    `[stack.<Classe>] <mensagem fixa>`, com as mesmas chamadas ao Docker que o `cq logs` e o `cq down` sobre gêmeos
    com a mesma falha; a `stack_status` segue o `cq status --json`: a variável ausente (código 2 no CLI) é erro, e o
    Docker indisponível e a outra falha (código 1) dão o relatório normal, igual ao do CLI. Nada ecoa o stderr do
    Docker nem o `.env`; nada no `deps.stderr` nem na saída real do processo."""
    failure = COMPOSE_FAILURES[case]
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner
    segments: dict[str, list[Any]] = {}
    operations: dict[str, list[Any]] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        code = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        _inject(double, failure)
        for name in ("stack_status", "stack_logs", "prepare_stack_down", "stack_down"):
            before = len(double.calls)
            out[name] = await call(c, name, {"confirmation_code": code} if name == "stack_down" else {})
            segments[name] = _calls(double)[before:]
            operations[name] = _operations(double)[before:]
        return out

    out = rig.session(rig.deps(client, stack=stack, runner=runner), steps)
    error = failure.error()
    for name in ("stack_logs", "prepare_stack_down", "stack_down"):
        _assert_error(out[name], failure.key, **failure.values)
    for outcome in out.values():
        assert scan(outcome, SECRETS) == []
    assert rig.codes.issued == ["codigoDeTesteDown00001"], "o preparo com a falha emitiu código"
    assert operations["prepare_stack_down"] and set(operations["prepare_stack_down"]) <= {"snapshot", "ps", "port"}

    status_twin = _failing_twin(level, failure)
    cli_status = _cli(rig, ["status"], client, json_mode=True, **{level: status_twin})
    if failure.status_is_error:
        _assert_cli_failed(cli_status, error)
        _assert_error(out["stack_status"], failure.key, **failure.values)
    else:
        assert cli_status.exit_code == 1, cli_status.stderr
        assert _ok(out["stack_status"]) == assert_cli_json("status", cli_status.stdout)
    assert segments["stack_status"] == _calls(status_twin)
    for name, command in (("stack_logs", ["logs"]), ("stack_down", ["down"])):
        twin = _failing_twin(level, failure)
        _assert_cli_failed(_cli(rig, command, client, json_mode=True, **{level: twin}), error)
        assert segments[name] == _calls(twin), name
    if runner is not None:
        assert_allowed(runner.calls, PROJECT, rig.stack_dir)
    rig.assert_quiet()


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("case", ["env_missing", "dir_not_found"])
def test_env_missing_and_dir_not_found_refused_without_docker(tmp_path, client, capfd, level, case) -> None:
    """depois do preparo (um código válido em mãos), sem o `.env` no
    diretório da pilha, ou com o perfil trocado para um `stack_dir` que não existe, `stack_up`, `stack_logs`,
    `prepare_stack_down` (sem emitir código) e `stack_down` com o código falham com `[stack.StackEnvMissing]` ou
    `[stack.StackDirNotFound]`, sem nenhuma chamada ao Docker; a `stack_status` sem o `.env` devolve o relatório do
    `cq status --json` (aviso `env_file_missing`; não é erro) e sem o diretório falha com `[stack.StackDirNotFound]`,
    como o `cq status` (código 2). Nenhuma mensagem cita o caminho. Controle: os comandos do CLI no mesmo
    estado saem com o mesmo código e a linha fixa, sem chamar o Docker."""
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner
    missing = tmp_path / MISSING_STACK_DIR
    seen: dict[str, int] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        code = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        seen["before"] = len(double.calls)
        if case == "env_missing":
            (rig.stack_dir / STACK_ENV_FILE).unlink()
        else:
            rig.write(_missing_dir_profile(missing))
        out = {name: await call(c, name, {"confirmation_code": code} if name == "stack_down" else {})
               for name in LOCAL_TOOLS}
        seen["after"] = len(double.calls)
        return out

    out = rig.session(rig.deps(client, stack=stack, runner=runner), steps)
    error_class = StackEnvMissing if case == "env_missing" else StackDirNotFound
    for name in LOCAL_TOOLS:
        if not (name == "stack_status" and case == "env_missing"):
            _assert_error(out[name], f"stack.{error_class.__name__}")
        _assert_no_echo(out[name], str(missing), missing.as_posix(), str(rig.stack_dir), rig.stack_dir.as_posix())
        assert scan(out[name], SECRETS) == []
    assert seen["after"] == seen["before"], "uma recusa antes do Docker chamou o Docker"
    assert rig.codes.issued == ["codigoDeTesteDown00001"], "o preparo recusado emitiu código"

    status_twin = _double(level)
    cli_status = _cli(rig, ["status"], client, json_mode=True, **{level: status_twin})
    if case == "env_missing":
        assert cli_status.exit_code == 1, cli_status.stderr
        body = _ok(out["stack_status"])
        assert body == assert_cli_json("status", cli_status.stdout)
        assert "env_file_missing" in body["warnings"]
    else:
        _assert_cli_failed(cli_status, StackDirNotFound())
    assert _calls(status_twin) == []
    for command in ("up", "logs", "down"):
        twin = _double(level)
        _assert_cli_failed(_cli(rig, [command], client, json_mode=True, **{level: twin}), error_class())
        assert _calls(twin) == [], command
    rig.assert_quiet()


@pytest.mark.parametrize("level", LEVELS)
def test_docker_target_refused_without_docker(tmp_path, client, capfd, level) -> None:
    """com o `DOCKER_HOST` do ambiente do servidor num host remoto,
    `stack_status`, `stack_up`, `prepare_stack_down` e `stack_logs` falham com `[stack.StackTargetRefused]` (a
    mensagem reescrita para ferramentas, sem o valor da variável), sem nenhuma chamada ao Docker e sem emitir código;
    o `stack_down` confere o código antes da pilha (só o perfil remoto e o reservado vêm antes da
    trava e do código), e um código nunca emitido dá `down_code_invalid`. Controle: os comandos do CLI com a
    mesma variável saem com o código 2 e a linha fixa, sem chamar o Docker."""
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner

    async def steps(c: Any) -> dict[str, Outcome]:
        return {name: await call(c, name, {"confirmation_code": UNISSUED_CODE} if name == "stack_down" else {})
                for name in LOCAL_TOOLS}

    out = rig.session(rig.deps(client, stack=stack, runner=runner, env=REMOTE_DOCKER_ENV), steps)
    for name in LOCAL_TOOLS:
        _assert_error(out[name], "down_code_invalid" if name == "stack_down" else "stack.StackTargetRefused")
        _assert_no_echo(out[name], REMOTE_DOCKER_HOST)
    assert _calls(double) == []
    assert rig.codes.issued == []
    for command in ("status", "up", "logs", "down"):
        twin = _double(level)
        _assert_cli_failed(_cli(rig, [command], client, json_mode=True, env=REMOTE_DOCKER_ENV, **{level: twin}),
                           StackTargetRefused())
        assert _calls(twin) == [], command
    rig.assert_quiet()


@pytest.mark.parametrize("level", LEVELS)
def test_broken_profile_file_in_all_five(tmp_path, client, capfd, level) -> None:
    """Perfil inválido ou arquivo de configuração quebrado:
    com o arquivo de perfil trocado por um TOML quebrado depois do preparo, as cinco (inclusive a
    `stack_status` e o `stack_down` com o código válido) falham com `[profile.InvalidProfileFile] <reason do CLI>`,
    sem eco do conteúdo nem do caminho do arquivo, sem chamar o Docker e sem emitir código. A resolução do perfil vem
    antes da trava e da conferência: o código fica intacto e, com o perfil local de volta, para a pilha uma vez."""
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner
    seen: dict[str, int] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        code = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        rig.write(BROKEN_PROFILE)
        seen["before"] = len(double.calls)
        out = {name: await call(c, name, {"confirmation_code": code} if name == "stack_down" else {})
               for name in LOCAL_TOOLS}
        seen["after"] = len(double.calls)
        rig.write(_local_profile())
        out["down_after"] = await call(c, "stack_down", {"confirmation_code": code})
        return out

    out = rig.session(rig.deps(client, stack=stack, runner=runner), steps)
    for name in LOCAL_TOOLS:
        _assert_error(out[name], "profile.InvalidProfileFile")
        _assert_no_echo(out[name], BROKEN_PROFILE_SENTINEL, "isto não é TOML", str(rig.config), rig.config.as_posix())
        assert scan(out[name], SECRETS) == []
    assert seen["after"] == seen["before"], "a falha do perfil chamou o Docker"
    assert rig.codes.issued == ["codigoDeTesteDown00001"]
    assert _ok(out["down_after"]) == {"project": PROJECT}
    assert _operations(double).count("down") == 1
    if runner is not None:
        assert_allowed(runner.calls, PROJECT, rig.stack_dir)
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# O down em duas fases


def test_prepare_stack_down_summary_and_nothing_to_stop(tmp_path, client, capfd) -> None:
    """o preparo traz os serviços presentes do `snapshot()` (não `absent`), na ordem
    de `SERVICES` (a pilha parcial: só postgres e worker), o aviso fixo, o código e o `expires_at` (o relógio de
    parede + 5 minutos), sem mudar nada; com a pilha já parada (nenhum serviço presente) → `down_nothing_to_stop`,
    sem emitir código."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["partial"])

    async def steps(c: Any) -> list[Outcome]:
        first = await call(c, "prepare_stack_down", {})
        stack.set_timeline(["absent"])
        return [first, await call(c, "prepare_stack_down", {})]

    partial, absent = rig.session(rig.deps(client, stack=stack), steps)
    body = _ok(partial)
    _validate("prepare_stack_down", body)
    assert body == {"project": PROJECT, "services": ["postgres", "worker"], "notice": DOWN_NOTICE,
                    "confirmation_code": "codigoDeTesteDown00001", "expires_at": FIRST_EXPIRES_AT}
    _assert_error(absent, "down_nothing_to_stop")
    assert rig.codes.issued == ["codigoDeTesteDown00001"]
    assert not {name for name, _ in stack.calls} & {"up", "down", "logs"}
    rig.assert_quiet()


def test_stack_down_refuses_invalid_codes_without_docker(tmp_path, client, capfd) -> None:
    """`stack_down` com o código vencido (depois de `DOWN_CODE_TTL_SECONDS` no relógio
    monotônico falso), inventado, já usado e de outro projeto (o perfil trocado entre as fases) →
    `down_code_invalid`, sem pedir nada ao Docker; sem código, com o código fora do padrão ou com campo a mais →
    `-32602` sem Docker. O código válido (controle positivo) para a pilha uma vez."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"])
    downs: dict[str, int] = {}

    def count() -> int:
        return [name for name, _ in stack.calls].count("down")

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        expired = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        rig.sleep.clock.advance(DOWN_CODE_TTL_SECONDS)
        out["expired"] = await call(c, "stack_down", {"confirmation_code": expired})
        downs["expired"] = count()
        out["invented"] = await call(c, "stack_down", {"confirmation_code": UNISSUED_CODE})
        downs["invented"] = count()
        valid = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        out["valid"] = await call(c, "stack_down", {"confirmation_code": valid})
        out["used"] = await call(c, "stack_down", {"confirmation_code": valid})
        downs["used"] = count()
        stack.set_timeline(["healthy"])
        other = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
        rig.write(OTHER_PROJECT_PROFILE)
        out["other_project"] = await call(c, "stack_down", {"confirmation_code": other})
        downs["other_project"] = count()
        rig.write(_local_profile())
        out["missing"] = await call(c, "stack_down", {})
        out["pattern"] = await call(c, "stack_down", {"confirmation_code": "curto"})
        out["extra"] = await call(c, "stack_down", {"confirmation_code": UNISSUED_CODE, "force": True})
        downs["protocol"] = count()
        return out

    out = rig.session(rig.deps(client, stack=stack), steps)
    for case in ("expired", "invented", "used", "other_project"):
        _assert_error(out[case], "down_code_invalid")
    assert (downs["expired"], downs["invented"]) == (0, 0)
    assert _ok(out["valid"]) == {"project": PROJECT}
    assert (downs["used"], downs["other_project"], downs["protocol"]) == (1, 1, 1)
    for case in ("missing", "pattern", "extra"):
        _assert_invalid_params(out[case])
    rig.assert_quiet()


@pytest.mark.parametrize("level", LEVELS)
def test_down_code_valid_only_in_the_server_that_issued_it(tmp_path, client, capfd, level) -> None:
    """O código só na memória do servidor; com o servidor reiniciado entre o
    preparo e a confirmação do `down` e com dois servidores MCP do projeto ao mesmo tempo. Cada `build_server` faz as
    vezes de um processo `cq mcp` (**[I]** do teste; o esqueleto declara um `DownCodeStore` por servidor), todos com o
    mesmo perfil local, o mesmo diretório da pilha e o mesmo Docker dublê, nos dois níveis:
    - reinício: o servidor A prepara e fecha; um servidor B novo recebe o código de A → `down_code_invalid`, sem
      nenhuma chamada ao Docker;
    - dois ao mesmo tempo: com B e C abertos juntos, cada um prepara o seu código; o de B em C e o de C em B →
      `down_code_invalid`, sem Docker;
    - controle positivo: o código de B, depois do preparo de C, para a pilha em B (um registro comum por projeto teria
      sido trocado pelo de C), e o de C, em C; um `down` por confirmação válida.
    Nenhum arquivo novo no `tmp_path`, que contém o diretório de trabalho dos servidores (o da pilha); nada no stderr
    dos servidores nem na saída real do processo."""
    rig = Rig(tmp_path, capfd)
    stack, runner = _doubles(level)
    double = stack if stack is not None else runner
    files_before = sorted(tmp_path.rglob("*"))
    docker: dict[str, list[Any]] = {}

    async def confirm(c: Any, case: str, code: str) -> Outcome:
        before = len(double.calls)
        outcome = await call(c, "stack_down", {"confirmation_code": code})
        docker[case] = _operations(double)[before:]
        return outcome

    async def server_a(a: Any) -> str:
        return _ok(await call(a, "prepare_stack_down", {}))["confirmation_code"]

    code_a = rig.session(rig.deps(client, stack=stack, runner=runner), server_a)
    rig.assert_quiet()
    deps_c = rig.deps(client, stack=stack, runner=runner)

    async def servers_b_and_c(b: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"a_in_b": await confirm(b, "a_in_b", code_a)}
        async with in_process(deps_c, MODE) as c:
            out["code_b"] = _ok(await call(b, "prepare_stack_down", {}))["confirmation_code"]
            out["code_c"] = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]
            out["b_in_c"] = await confirm(c, "b_in_c", out["code_b"])
            out["c_in_b"] = await confirm(b, "c_in_b", out["code_c"])
            out["b_in_b"] = await confirm(b, "b_in_b", out["code_b"])
            out["c_in_c"] = await confirm(c, "c_in_c", out["code_c"])
        return out

    out = rig.session(rig.deps(client, stack=stack, runner=runner), servers_b_and_c)
    assert rig.codes.issued == [code_a, out["code_b"], out["code_c"]]
    assert len(set(rig.codes.issued)) == 3
    for case in ("a_in_b", "b_in_c", "c_in_b"):
        _assert_error(out[case], "down_code_invalid")
        assert docker[case] == [], f"{case}: a conferência do código de outro servidor chamou o Docker"
    for case in ("b_in_b", "c_in_c"):
        assert _ok(out[case]) == {"project": PROJECT}, case
        assert docker[case] == ["down"], case
    assert _operations(double).count("down") == 2
    if runner is not None:
        assert_allowed(runner.calls, PROJECT, rig.stack_dir)
    assert sorted(tmp_path.rglob("*")) == files_before, "um servidor criou arquivo no tmp_path"
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# A trava


def test_lock_while_stack_up_in_progress(tmp_path, client, capfd) -> None:
    """Com um `stack_up` em andamento (preso no Docker dublê), `stack_down` (com um
    código válido) e outro `stack_up` falham na hora com `stack_busy`, sem chamar o Docker e sem gastar o código;
    com o perfil trocado para o remoto, `stack_up` e `stack_down` dão a recusa do remoto, não `stack_busy` (a recusa
    vem antes da trava); `stack_status` e `stack_logs` seguem livres. Solto o `up`, ele termina com o corpo do `up`
    e o código continua valendo."""
    rig = Rig(tmp_path, capfd)
    stack = BlockingStack(["healthy"], logs=RAW_LINES)
    stack.down_release.set()
    seen: dict[str, Any] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        code = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]

        async def first_up() -> None:
            out["up"] = await call(c, "stack_up", {})

        async with anyio.create_task_group() as tg:
            tg.start_soon(first_up)
            try:
                seen["entered"] = await anyio.to_thread.run_sync(stack.up_entered.wait, BLOCK_TIMEOUT)
                before = len(stack.calls)
                out["down_busy"] = await call(c, "stack_down", {"confirmation_code": code})
                out["up_busy"] = await call(c, "stack_up", {})
                seen["busy_calls"] = len(stack.calls) - before
                seen["started_during"] = (stack.up_started, stack.down_started)
                rig.write(REMOTE_PROFILE)
                out["up_remote"] = await call(c, "stack_up", {})
                out["down_remote"] = await call(c, "stack_down", {"confirmation_code": code})
                rig.write(_local_profile())
                out["status"] = await call(c, "stack_status", {})
                out["logs"] = await call(c, "stack_logs", {})
                seen["up_running"] = not stack.up_release.is_set()
            finally:
                rig.write(_local_profile())
                stack.up_release.set()
        out["down_after"] = await call(c, "stack_down", {"confirmation_code": code})
        return out

    out = rig.session(rig.deps(client, stack=stack), steps)
    assert seen["entered"] is True, "o stack_up não chegou ao Docker dublê"
    _assert_error(out["down_busy"], "stack_busy")
    _assert_error(out["up_busy"], "stack_busy")
    assert seen["busy_calls"] == 0, "uma chamada recusada pela trava chamou o Docker"
    assert seen["started_during"] == (1, 0)
    _assert_error(out["up_remote"], "stack.RemoteProfileRefused")
    _assert_error(out["down_remote"], "stack.RemoteProfileRefused")
    assert seen["up_running"] is True
    assert_cli_json("status", _ok(out["status"]))
    _validate("stack_logs", _ok(out["logs"]))
    assert _ok(out["up"]) == _up_body()
    assert _ok(out["down_after"]) == {"project": PROJECT}
    assert (stack.up_started, stack.down_started) == (1, 1)
    rig.assert_quiet()


def test_lock_while_stack_down_in_progress(tmp_path, client, capfd) -> None:
    """Com um `stack_down` em andamento, `stack_up` e outro `stack_down` (com um código
    válido novo, emitido durante o `down`) falham na hora com `stack_busy`, sem chamar o Docker e com o segundo
    código intacto; `stack_status` e `stack_logs` seguem livres. Solto o `down`, ele termina com `{"project": …}`, e
    o segundo código ainda para a pilha."""
    rig = Rig(tmp_path, capfd)
    stack = BlockingStack(["healthy"], logs=RAW_LINES)
    stack.up_release.set()
    seen: dict[str, Any] = {}

    async def steps(c: Any) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        first_code = _ok(await call(c, "prepare_stack_down", {}))["confirmation_code"]

        async def first_down() -> None:
            out["down"] = await call(c, "stack_down", {"confirmation_code": first_code})

        async with anyio.create_task_group() as tg:
            tg.start_soon(first_down)
            try:
                seen["entered"] = await anyio.to_thread.run_sync(stack.down_entered.wait, BLOCK_TIMEOUT)
                out["prepare"] = await call(c, "prepare_stack_down", {})
                second_code = _ok(out["prepare"])["confirmation_code"]
                seen["second_code"] = second_code
                before = len(stack.calls)
                out["down_busy"] = await call(c, "stack_down", {"confirmation_code": second_code})
                out["up_busy"] = await call(c, "stack_up", {})
                seen["busy_calls"] = len(stack.calls) - before
                seen["started_during"] = (stack.up_started, stack.down_started)
                out["status"] = await call(c, "stack_status", {})
                out["logs"] = await call(c, "stack_logs", {})
                seen["down_running"] = not stack.down_release.is_set()
            finally:
                stack.down_release.set()
        out["down_after"] = await call(c, "stack_down", {"confirmation_code": seen.get("second_code",
                                                                                         UNISSUED_CODE)})
        return out

    out = rig.session(rig.deps(client, stack=stack), steps)
    assert seen["entered"] is True, "o stack_down não chegou ao Docker dublê"
    assert seen["second_code"] != rig.codes.issued[0]
    _assert_error(out["down_busy"], "stack_busy")
    _assert_error(out["up_busy"], "stack_busy")
    assert seen["busy_calls"] == 0, "uma chamada recusada pela trava chamou o Docker"
    assert seen["started_during"] == (0, 1)
    assert seen["down_running"] is True
    assert_cli_json("status", _ok(out["status"]))
    _validate("stack_logs", _ok(out["logs"]))
    assert _ok(out["down"]) == {"project": PROJECT}
    assert _ok(out["down_after"]) == {"project": PROJECT}
    assert (stack.up_started, stack.down_started) == (0, 2)
    rig.assert_quiet()


# --------------------------------------------------------------------------------------------------
# stack_logs


@pytest.mark.parametrize("arguments", [{"services": ["engine"]}, {"services": ["API"]}, {"services": ["Postgres"]},
                                       {"services": ["api", "api"]}, {"services": []}, {"tail": 0},
                                       {"tail": LOGS_TAIL_MAX + 1}, {"tail": "200"}, {"follow": True}],
                         ids=["engine", "caixa_api", "caixa_postgres", "repetido", "vazio", "tail_zero", "tail_acima",
                              "tail_texto", "campo_a_mais"])
def test_stack_logs_invalid_arguments_without_docker(tmp_path, client, capfd, arguments) -> None:
    """`services` com o `engine`, com variação de caixa, repetido ou vazio,
    `tail` 0, acima de 1.000 ou texto, e campo a mais → `-32602` sem eco e sem chamar o Docker. Controle positivo na
    mesma sessão: `services=["api"]` com `tail` 1 e o `tail` 1.000 são aceitos e chegam ao dublê."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"], logs=[])
    seen: dict[str, int] = {}

    async def steps(c: Any) -> list[Outcome]:
        refused = await call(c, "stack_logs", arguments)
        seen["after_refused"] = len(stack.calls)
        return [refused, await call(c, "stack_logs", {"services": ["api"], "tail": 1}),
                await call(c, "stack_logs", {"tail": LOGS_TAIL_MAX})]

    refused, one, most = rig.session(rig.deps(client, stack=stack), steps)
    _assert_invalid_params(refused)
    assert seen["after_refused"] == 0
    assert _ok(one)["lines"] == [] and _ok(most)["lines"] == []
    assert stack.calls == [("logs", {"services": ("api",), "tail": 1, "follow": False}),
                           ("logs", {"services": LOG_SERVICES, "tail": LOGS_TAIL_MAX, "follow": False})]
    rig.assert_quiet()


def test_stack_logs_services_filter(tmp_path, client, capfd) -> None:
    """com `services=["worker", "postgres"]` e sem `tail`, o dublê recebe exatamente
    esses serviços e o `tail` padrão (200, o literal), e as linhas de outro serviço que vierem na saída são
    descartadas e somadas em `omitted`."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"], logs=RAW_LINES)

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_logs", {"services": ["worker", "postgres"]})

    body = _ok(rig.session(rig.deps(client, stack=stack), steps))
    _assert_logs_body(body, SAMPLE_LIST, ("worker", "postgres"))
    assert {line["service"] for line in body["lines"]} <= {"worker", "postgres"}
    assert stack.calls == [("logs", {"services": ("worker", "postgres"), "tail": SPEC_TAIL_DEFAULT,
                                     "follow": False})]
    rig.assert_quiet()


def test_stack_logs_truncated_above_limit(tmp_path, client, capfd) -> None:
    """registros que passam do teto no texto → o resultado (não um erro)
    com as linhas mais recentes que cabem e `truncated` verdadeiro. O teto é o literal do teste (`RESULT_LIMIT_CHARS`,
    50.000 caracteres do texto, o JSON compacto do corpo), conferido dos dois lados: o texto cabe nele, e o corte não
    tira mais que o necessário (o texto fica a menos de uma linha do teto; mais longe, a linha seguinte caberia)."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["healthy"], logs=bulk())

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_logs", {"tail": LOGS_TAIL_MAX})

    outcome = rig.session(rig.deps(client, stack=stack), steps)
    body = _ok(outcome)
    _validate("stack_logs", body)
    assert body["truncated"] is True
    assert 0 < body["returned"] == len(body["lines"]) < BULK_DEFAULT
    assert outcome.text == _compact(body)
    assert len(outcome.text) <= RESULT_LIMIT_CHARS
    # A linha seguinte (a mais antiga cortada) só difere das mantidas no `id`, menor: somaria no máximo a maior entrada
    # de `lines`, a vírgula e um dígito a mais em `returned`.
    widest = max(len(_compact(line)) for line in body["lines"]) + 2
    assert len(outcome.text) > RESULT_LIMIT_CHARS - widest, "o corte tirou linhas que cabiam no teto"
    ids = [json.loads(line["text"])["id"] for line in body["lines"]]
    assert ids == list(range(BULK_DEFAULT - len(ids) + 1, BULK_DEFAULT + 1)), "não são as linhas mais recentes"
    rig.assert_quiet()


def test_stack_logs_empty(tmp_path, client, capfd) -> None:
    """O dublê sem linhas (serviço parado ou sem registros) → `lines: []` e contagens 0."""
    rig = Rig(tmp_path, capfd)
    stack = FakeStack(["stopped"], logs=[])

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_logs", {})

    body = _ok(rig.session(rig.deps(client, stack=stack), steps))
    assert body == {"lines": [], "returned": 0, "redacted": 0, "omitted": 0, "truncated": False}
    rig.assert_quiet()


def test_stack_logs_samples_private_and_cli_logs_unredacted(tmp_path, client, capfd) -> None:
    """sobre as amostras com identidades canário e segredos-sentinela, a `stack_logs`
    não devolve nenhum deles (no estruturado, no texto nem no resultado bruto), com as contagens esperadas; o
    `cq logs` do CLI, pelo mesmo `FakeRunner` gêmeo, continua sem redação (as identidades e os sentinelas saem nele)
    e pede o mesmo argv."""
    rig = Rig(tmp_path, capfd)
    runner = FakeRunner({"logs": RAW_BYTES})

    async def steps(c: Any) -> Outcome:
        return await call(c, "stack_logs", {})

    outcome = rig.session(rig.deps(client, runner=runner), steps)
    _assert_logs_body(_ok(outcome), SAMPLE_LIST)
    assert scan(outcome, list(SENTINELS), prefixes=CANARY_PREFIXES) == []
    twin = FakeRunner({"logs": RAW_BYTES})
    cli = _cli(rig, ["logs"], client, runner=twin)
    assert cli.exit_code == 0, cli.stderr
    _assert_positive_control(cli.stdout)
    assert scan(cli.stdout, list(SENTINELS), prefixes=CANARY_PREFIXES) != []
    assert runner.argvs() == twin.argvs()
    assert_allowed(runner.calls, PROJECT, rig.stack_dir)
    rig.assert_quiet()

