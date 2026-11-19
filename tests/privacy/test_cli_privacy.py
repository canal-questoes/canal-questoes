"""Privacidade das saídas dos comandos novos do CLI.


As saídas de `up`, `down`, `status`, `logs` e `doctor` (texto e `--json`), a dica da API inalcançável (provocada pelo perfil
de teste em `http://127.0.0.1:9`, nunca o padrão `127.0.0.1:8000`) e o aviso de `http://` não trazem:
- identidade (os alvos canário da API base, com uma pessoa canário cadastrada no banco da API dos testes);
- o token do perfil (nem o token inicial dos testes);
- o conteúdo do `.env` (o sentinela de `make_stack_dir`), configuração interpolada nem ambiente de contêiner;
- o caminho completo do arquivo de perfil ou do diretório da pilha;
- o segredo-sentinela do Compose dublê (`SENTINEL_SECRET`, em todo stderr do Docker e nos `Labels` do `ps`);
- a senha de um `api_url` com `user:senha@`.

Os comandos rodam pelo dublê de nível 2 (`FakeRunner` no `ctx.obj["runner"]`), para que o stderr do Compose (com
o sentinela) passe pelo adaptador real; a API dos testes chega por `http=` com o perfil local em `TEST_API_URL`
(nunca a porta 8000); o relógio e a espera do `up` são os injetados. Toda chamada ao Docker fica na
lista fechada (`assert_allowed`: nada de `config`, `env`, `exec`, `inspect` de contêiner).

**O que já passa:** `find_secrets` sobre o repositório (nenhum segredo gerado no teste gravado num
arquivo do repositório) já passa no esqueleto; por isso fica na **mesma função** de uma saída real de `cq status` e
`cq doctor` pelos dublês, que falha no esqueleto pela folha ausente e é varrida pelo mesmo
`find_secrets`.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from cq_client.hint import HINT_TEMPLATE
from cq_client.profile import INSECURE_TRANSPORT_TEMPLATE
from cq_client.stack import (STACK_FAILURE_MESSAGES, VARIABLE_MISSING_TEMPLATE, VARIABLE_MISSING_UNNAMED,
                             DockerUnavailable)

from tests.helpers.api import ADMIN_TOKEN, create_token, register_person
from tests.helpers.canary import ZEFIRO, find_canary, find_secrets
from tests.helpers.cli_runner import (
    ENV_SENTINEL,
    ENV_SENTINEL_TOKEN,
    ENV_TEXT,
    UNREACHABLE_API_URL,
    align_clock_to_db,
    invoke_cli,
    make_stack_dir,
    write_profile,
)
from tests.helpers.contract import assert_cli_json
from tests.helpers.docker_fake import SENTINEL_SECRET, FakeRunner, assert_allowed, failure_samples
from tests.helpers.secrets import all_secrets, bot_secrets
from tests.helpers.stack_fake import TEST_API_URL, FakeSleep

REPO = pathlib.Path(__file__).resolve().parents[2]
URL_PASSWORD = "senha-do-endereco-Vb73Lq"
REMOTE_HTTP_URL = f"http://operador:{URL_PASSWORD}@cq.example.org:8080"
# Os nomes das variáveis do `.env` e do ambiente dos contêineres (nenhum aparece numa saída do CLI).
CONTAINER_ENV_NAMES = ("CQ_DB_PASSWORD", "CQ_ADMIN_TOKEN", "CQ_MASTER_KEY", "POSTGRES_PASSWORD", "DATABASE_URL",
                       "CQ_DATABASE_URL")
# Marcas de configuração interpolada do Compose (`docker compose config`), que o CLI nunca roda.
INTERPOLATED_MARKS = ("services:", "environment:", "working_dir", "config_files", "com.docker.compose")
TEXT_SUFFIXES = (".py", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".ini", ".cfg", ".sh", ".sql", ".d2",
                 ".svg", ".example", ".lock", ".html", ".csv", ".jsonl")


# --- montagem ------------------------------------------------------------------------------------------------


class Setup:
    """O perfil local e o remoto de teste, o diretório da pilha e o token de leitura."""

    def __init__(self, client: Any, tmp_path: pathlib.Path) -> None:
        self.token, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "people:read"])
        register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
        self.stack_dir = make_stack_dir(tmp_path)
        assert ENV_SENTINEL in (self.stack_dir / ".env").read_text(encoding="utf-8")
        # O perfil implícito (chaves de topo) também aponta para a API inalcançável: nenhum caminho, nem o da API base
        # (que ignora `CQ_PROFILE`), cai no padrão `127.0.0.1:8000`.
        text = "\n".join([
            f'api_url = "{UNREACHABLE_API_URL}"', "",
            "[profiles.local]", 'kind = "local"', f'api_url = "{TEST_API_URL}"', f'token = "{self.token}"',
            f"stack_dir = '{self.stack_dir}'", "",
            "[profiles.inalcancavel]", 'kind = "local"', f'api_url = "{UNREACHABLE_API_URL}"',
            f'token = "{self.token}"', f"stack_dir = '{self.stack_dir}'", "",
            "[profiles.remoto]", 'kind = "remote"', f'api_url = "{REMOTE_HTTP_URL}"', f'token = "{self.token}"', "",
        ])
        self.config = write_profile(tmp_path, text)

    def secrets(self) -> list[str]:
        return [*all_secrets(), *bot_secrets(), self.token, ENV_SENTINEL, ENV_SENTINEL_TOKEN, SENTINEL_SECRET,
                URL_PASSWORD]


def _obj(client: Any, clock: Any, runner: FakeRunner | None = None, *, http: bool = True) -> dict[str, Any]:
    sleep = FakeSleep(clock)
    obj: dict[str, Any] = {"runner": runner if runner is not None else FakeRunner(), "clock": sleep.monotonic,
                           "sleep": sleep}
    if http:
        obj["http"] = client
    return obj


def _assert_private(result: Any, setup: Setup, label: str, *, allowed: tuple[str, ...] = ()) -> None:
    """Nenhuma saída (stdout, stderr, a saída combinada e o `repr` de uma exceção) com identidade, segredo,
    conteúdo do `.env`, ambiente de contêiner, configuração interpolada ou caminho completo. `allowed`: um nome de
    variável que a mensagem fixa pode citar (a variável obrigatória ausente no padrão `^CQ_[A-Z0-9_]+$`)."""
    secrets = setup.secrets()
    texts = [result.stdout, result.stderr, result.output]
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        texts.append(repr(result.exception))
        texts.append(str(result.exception))
    for text in texts:
        assert find_canary(text) == [], f"{label}: identidade canário: {find_canary(text)}"
        assert find_secrets(text, secrets) == [], f"{label}: segredo: {find_secrets(text, secrets)}"
        for value in (*(n for n in CONTAINER_ENV_NAMES if n not in allowed), *INTERPOLATED_MARKS,
                      str(setup.stack_dir), str(setup.config), str(setup.stack_dir.parent), "Traceback", "operador:",
                      "/home/operador"):
            assert value not in text, f"{label}: {value!r} na saída"
        for line in ENV_TEXT.splitlines():
            assert line not in text, f"{label}: linha do .env na saída"


def _run(args: list[str], setup: Setup, *, json: bool = False, profile: str = "local", obj: dict | None = None,
         **kwargs: Any) -> Any:
    return invoke_cli(args, json=json, obj=obj, env={"CQ_PROFILE": profile}, config=setup.config, **kwargs)


# --- casos -----------------------------------------------------------------------------------------------------


def test_status_and_doctor_outputs_and_repository_have_no_new_secret(
        client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path) -> None:
    """Uma saída real de `cq status` e `cq doctor` (texto e JSON) pelos dublês, com a pilha saudável: os códigos e
    os esquemas esperados, sem nenhum dado sensível; e o repositório (arquivos versionados e não ignorados) sem
    nenhum segredo gerado neste teste (o token do perfil, as chaves-mestras da sessão)."""
    setup = Setup(client, tmp_path)
    runner = FakeRunner()
    for args, schema in ((["status"], "status"), (["doctor"], "doctor")):
        for json_mode in (False, True):
            align_clock_to_db(clock, sql)
            result = _run(args, setup, json=json_mode, obj=_obj(client, clock, runner))
            assert result.exit_code == 0, (args, json_mode, result.exit_code, result.stderr)
            if json_mode:
                assert_cli_json(schema, result.stdout)
            _assert_private(result, setup, f"{args} json={json_mode}")
    assert_allowed(runner.calls, "cq", setup.stack_dir)

    # O repositório: nenhum segredo gerado em tempo de execução (o detector com o controle positivo).
    generated = [setup.token, *bot_secrets()]
    assert find_secrets(f"token = \"{setup.token}\"", generated), "controle positivo do detector"
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                            capture_output=True, timeout=120)
    assert listed.returncode == 0, "git ls-files falhou"
    leaks: list[tuple[str, list[str]]] = []
    scanned = 0
    for name in listed.stdout.decode("utf-8", errors="replace").split("\0"):
        path = REPO / name
        if not name or not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 4_000_000:
            continue
        scanned += 1
        hits = find_secrets(path.read_bytes().decode("utf-8", errors="replace"), generated)
        if hits:
            leaks.append((name, hits))
    assert scanned > 100, f"a varredura do repositório leu poucos arquivos: {scanned}"
    assert leaks == [], f"segredo gerado gravado no repositório: {leaks}"


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_up_down_logs_success_outputs_are_private(client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path,
                                                  json_mode: bool) -> None:
    """`up`, `logs` e `down` com sucesso (texto e JSON): o progresso e o resumo do Compose (com o sentinela no
    stderr) nunca repassados; os registros limpos sem o sentinela; o argv só da lista fechada."""
    setup = Setup(client, tmp_path)
    runner = FakeRunner()
    for args, schema in ((["up"], "up"), (["logs"], "logs"), (["down"], "down")):
        result = _run(args, setup, json=json_mode, obj=_obj(client, clock, runner))
        assert result.exit_code == 0, (args, result.exit_code, result.stderr)
        if json_mode:
            assert_cli_json(schema, result.stdout)
        _assert_private(result, setup, f"{args} json={json_mode}")
    assert_allowed(runner.calls, "cq", setup.stack_dir)


# A mensagem fixa de cada falha, no stderr dos comandos sem relatório.
FAILURE_MESSAGES = {
    "port_allocated": STACK_FAILURE_MESSAGES["port_in_use"],
    "port_only_one_usage": STACK_FAILURE_MESSAGES["port_in_use"],
    "build_failed": STACK_FAILURE_MESSAGES["build_failed"],
    "variable_missing": VARIABLE_MISSING_TEMPLATE.format(name="CQ_DB_PASSWORD"),
    "variable_missing_unnamed": VARIABLE_MISSING_UNNAMED,
    "daemon_stopped": DockerUnavailable.message,
    "daemon_stopped_windows": DockerUnavailable.message,
    "docker_missing": DockerUnavailable.message,
    "generic": STACK_FAILURE_MESSAGES["other"],
}
FAILURES = (
    (["up"], "port_allocated", 1),
    (["up"], "port_only_one_usage", 1),
    (["up", "--rebuild"], "build_failed", 1),
    (["up"], "variable_missing", 2),
    (["up"], "variable_missing_unnamed", 2),
    (["up"], "daemon_stopped", 1),
    (["up"], "daemon_stopped_windows", 1),
    (["up"], "generic", 1),
    (["down"], "generic", 1),
    (["down"], "daemon_stopped", 1),
    (["logs"], "generic", 1),
    (["logs"], "daemon_stopped", 1),
    (["status"], "daemon_stopped", 1),
    (["status"], "generic", 1),
    (["doctor"], "daemon_stopped", 1),
    (["doctor"], "docker_missing", 1),
)


@pytest.mark.parametrize(("args", "kind", "code"), FAILURES,
                         ids=[f"{' '.join(a)}-{k}" for a, k, _ in FAILURES])
@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_failure_outputs_never_repeat_compose_stderr(client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path,
                                                     args: list[str], kind: str, code: int,
                                                     json_mode: bool) -> None:
    """Cada falha do Compose (porta, construção, variável ausente com e sem nome no padrão, daemon parado, Docker
    ausente, outra) sai com a mensagem fixa e o código do contrato, sem o stderr do Docker (o sentinela), sem
    o `.env` e sem caminho; o `status` e o `doctor` com o código 1 trazem o relatório (também varrido)."""
    setup = Setup(client, tmp_path)
    runner = FakeRunner(samples=failure_samples(kind))
    align_clock_to_db(clock, sql)
    result = _run(args, setup, json=json_mode, obj=_obj(client, clock, runner))
    assert result.exit_code == code, (result.exit_code, result.stderr)
    if args[0] in ("status", "doctor"):
        assert result.stdout != "", "o relatório do código 1 sai no stdout"
        if json_mode:
            assert_cli_json(args[0], result.stdout)
    else:
        assert result.stdout == ""
    assert result.stderr.strip() != ""
    if args[0] not in ("status", "doctor"):
        message = FAILURE_MESSAGES[kind]
        assert any(message in line for line in result.stderr.splitlines()), (message, result.stderr)
    allowed = ("CQ_DB_PASSWORD",) if kind == "variable_missing" else ()
    _assert_private(result, setup, f"{args} {kind} json={json_mode}", allowed=allowed)
    assert_allowed(runner.calls, "cq", setup.stack_dir)


@pytest.mark.parametrize("args", [["offerings", "list"], ["doctor"]], ids=["folha-do-catalogo", "doctor"])
def test_hint_line_is_private(client: Any, clock: Any, tmp_path: pathlib.Path, args: list[str]) -> None:
    """A dica da API inalcançável, provocada pelo perfil local de teste em `http://127.0.0.1:9` (nunca o padrão
    `127.0.0.1:8000`): a linha fixa com o projeto, sem o caminho da pilha, sem o token e sem nada do `.env`; a
    folha do catálogo sai com 3 (sem terminal, sem pergunta), o `doctor` com 1."""
    setup = Setup(client, tmp_path)
    result = _run(args, setup, profile="inalcancavel", obj=_obj(client, clock, http=False))
    assert result.exit_code == (1 if args == ["doctor"] else 3), (result.exit_code, result.stderr)
    assert HINT_TEMPLATE.format(project="cq") in result.stderr.splitlines()
    assert "[s/N]" not in result.stderr
    _assert_private(result, setup, f"dica {args}")


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_http_warning_and_status_hide_url_password(client: Any, clock: Any, tmp_path: pathlib.Path,
                                                   json_mode: bool) -> None:
    """O aviso de `http://` do perfil nomeado remoto fora do loopback usa o endereço de exibição:
    a senha do `user:senha@` não aparece no aviso, no texto nem no JSON, nem o `operador:`; o `status` remoto e o
    `doctor` remoto com a API dos testes por `http=`."""
    setup = Setup(client, tmp_path)
    warning = INSECURE_TRANSPORT_TEMPLATE.format(name="remoto")
    for args in (["status"], ["doctor"], ["health"]):
        result = _run(args, setup, json=json_mode, profile="remoto", obj={"http": client})
        assert result.exit_code == 0, (args, result.exit_code, result.stderr)
        if not json_mode:
            assert warning in result.stderr.splitlines(), (args, result.stderr)
        elif args[0] != "health":
            assert assert_cli_json(args[0], result.stdout)["profile"]["api_url"] == "http://cq.example.org:8080"
        _assert_private(result, setup, f"aviso {args} json={json_mode}")
