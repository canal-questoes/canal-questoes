"""Teste de sistema do banco de questões local no Compose: o envio pelo `cq`, o disparo com o motor real e o
teto do corpo com o `uvicorn` real.

Sobe o projeto Compose descartável **`cq-systest-bank`** (separado dos outros testes de sistema), com segredos e
porta gerados no teste e `.env` vazio, no molde do `_Stack` de `test_engine_system.py` (reescrito aqui, porque
o de lá é privado). Nunca roda `docker compose config`, que imprime os segredos interpolados. Remove só o que
criou, no `finally` da fixture (`down -v --remove-orphans` do próprio projeto); nunca `prune`, nunca outro
projeto.

- `test_bank_uploaded_by_cq_is_dispatched_until_scheduled_with_the_real_engine`: uma cópia do curso de fixture
  (`tests/fixtures/repos/course`) numa pasta temporária é enviada pelo `cq banks upload` (o CLI em processo,
  contra a API do Compose, sem o perfil nem a configuração de quem roda o teste); a saída traz a linha pronta do
  preparo com o `repo` `bank:<nome>` e a versão. Um oferecimento é criado pela API; o bot é registrado e ativado
  por um roteiro no contêiner da api, com uma Bot API de mentira só no roteiro (nenhum pedido ao Telegram) e uma
  chave-mestra gerada pelo teste, que o Compose não recebe (o worker sobe sem bots e não consulta o Telegram). O
  disparo da questão `number` com o `repo` e a versão do banco chega a `scheduled`, com a prévia do motor real.
- `test_body_above_the_cap_is_refused_by_the_real_server_before_the_body`: com o `uvicorn` real, um pedido de
  envio com `Content-Length` um byte acima do teto do corpo (180 MiB) e nenhum byte de corpo recebe 400
  `[{body, too_long}]` sem esperar o corpo.

Nenhum endereço fora do loopback e da rede do próprio projeto é contatado. Sem o Docker, sem o serviço `engine`
no `compose.yaml` ou sem a imagem base do PL no host, falha com mensagem de ambiente, sem baixar nada.

Vermelho esperado no esqueleto: o envio pelo `cq` (o comando lança `NotImplementedError`) e a rota do envio
(500) ainda não existem; fica assim até o comando, o envio, o preparo com `bank:` e a publicação no worker.

Escolhas deste arquivo [I]:
- sem chave-mestra no Compose, a validação do disparo usa o canal de console do worker e ainda chega a
  `scheduled` (a prévia não depende do Telegram);
- o roteiro do contêiner registra o bot com `bots.register_bot` e ativa com `bots.activate`, os mesmos passos da
  API, com uma Bot API de mentira que só responde ao `getMe`.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
import yaml

from tests.helpers import banks as bk
from tests.helpers.contract import MESSAGES

pytestmark = pytest.mark.system

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"
ENGINE_DOCKERFILE = REPO / "engine" / "Dockerfile"
PROJECT = "cq-systest-bank"
COMMAND_TIMEOUT = 300
READY_DEADLINE_SECONDS = 240
DISPATCH_DEADLINE_SECONDS = 180
HTTP_TIMEOUT = 30.0
BANK_NAME = "banco-sistema"
OFFERING_CODE = "sistema-banco"
QID = "number"
MAX_BODY_BYTES = 188743680  # o teto do corpo do envio, do contrato
# Um token de bot fictício da lista de valores fictícios da exportação; só o roteiro do contêiner o vê.
FAKE_BOT_TOKEN = "6100000001:AbCdEfGhIjKlMnOpQrStUvWxYz0123456-_"


# ---------------------------------------------------------------------------------------------
# Ambiente e o projeto Compose do teste
# ---------------------------------------------------------------------------------------------


def _require_engine_service() -> None:
    assert COMPOSE.is_file(), "compose.yaml ausente na raiz do repositório"
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose.get("services") if isinstance(compose, dict) else None
    assert isinstance(services, dict) and isinstance(services.get("engine"), dict), (
        "serviço `engine` ausente do compose.yaml")


def _docker(*args: str, timeout: float | None = 120, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if check:
        assert result.returncode == 0, f"`docker {args[0]}` falhou ({result.returncode}):\n{result.stderr[-3000:]}"
    return result


def _require_docker() -> None:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"Docker indisponível para o teste de sistema ({type(exc).__name__}). "
                    "Instale/ligue o Docker ou rode `pytest -m 'not system'`.")
    if info.returncode != 0:
        pytest.fail("Docker indisponível para o teste de sistema (`docker info` falhou). "
                    "Ligue o Docker ou rode `pytest -m 'not system'`.")


def _require_pl_base() -> None:
    """A base do PL tem de estar no host; o teste não baixa nada."""
    base = next((line.split()[1] for line in ENGINE_DOCKERFILE.read_text(encoding="utf-8").splitlines()
                 if line.strip().upper().startswith("FROM ")), None)
    assert base is not None, "engine/Dockerfile sem FROM"
    if _docker("image", "inspect", base, check=False, timeout=60).returncode != 0:
        pytest.fail(f"Ambiente: a imagem base do PL `{base}` não está no host. O teste de sistema não baixa "
                    "imagens (ação externa); obtenha-a antes (`docker pull`) e rode de novo.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Stack:
    """O projeto `cq-systest-bank`, subido sob demanda e removido no `finally` da fixture."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.db_password = secrets.token_urlsafe(24)
        self.admin_token = "cq_" + secrets.token_urlsafe(32)
        # A chave-mestra só do roteiro do contêiner (o Compose não a recebe).
        self.script_master_key = secrets.token_urlsafe(32)
        self.port = _free_port()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "CQ_"))}
        self.env.update({"CQ_DB_PASSWORD": self.db_password, "CQ_ADMIN_TOKEN": self.admin_token,
                         "CQ_API_PORT": str(self.port), "MSYS_NO_PATHCONV": "1"})
        self.env_file = tmp / "vazio.env"  # não lê o `.env` local de quem roda o teste
        self.env_file.write_text("", encoding="utf-8")
        self.hidden = [self.db_password, self.admin_token, self.script_master_key, FAKE_BOT_TOKEN]
        self.started = False
        self.error: str | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def scrub(self, text: str) -> str:
        for value in self.hidden:
            text = text.replace(value, "[segredo]")
        return text

    def run(self, *args: str, timeout: float | None = COMMAND_TIMEOUT, check: bool = True,
            stdin: str | None = None) -> subprocess.CompletedProcess:
        assert "config" not in args, "o teste nunca roda `docker compose config` (imprime segredos)"
        cmd = ["docker", "compose", "--env-file", str(self.env_file), "-f", str(COMPOSE), "-p", PROJECT, *args]
        result = subprocess.run(cmd, cwd=REPO, env=self.env, capture_output=True, text=True, input=stdin,
                                encoding="utf-8", errors="replace", timeout=timeout)
        if check:
            assert result.returncode == 0, (
                f"`docker compose {' '.join(args[:3])}` falhou ({result.returncode}):\n"
                f"{self.scrub(result.stderr[-3000:])}")
        return result

    def container(self, service: str) -> str:
        ids = self.run("ps", "-q", service, timeout=60).stdout.split()
        assert len(ids) == 1, f"esperava um container de {service}, achei {len(ids)}"
        return ids[0]

    def state(self, service: str) -> dict:
        return json.loads(_docker("inspect", self.container(service)).stdout)[0]["State"]

    def py(self, service: str, script: str) -> Any:
        """Roda `script` (pela entrada padrão) no serviço e devolve o JSON da última linha."""
        result = self.run("exec", "-T", service, "python", "-", stdin=script, timeout=COMMAND_TIMEOUT, check=False)
        assert result.returncode == 0, f"o roteiro em {service} falhou:\n{self.scrub(result.stderr[-3000:])}"
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert lines, f"o roteiro em {service} não imprimiu nada:\n{self.scrub(result.stderr[-2000:])}"
        return json.loads(lines[-1])

    def start(self) -> None:
        if self.error is not None:
            pytest.fail(self.error)
        if self.started:
            return
        self.started = True
        try:
            self.run("down", "-v", "--remove-orphans")  # ambiente limpo: só o projeto do próprio teste
            self.run("build", timeout=None)
            self.run("up", "-d")
            deadline = time.monotonic() + READY_DEADLINE_SECONDS
            while True:
                states = {s: self.state(s) for s in ("engine", "worker", "api")}
                ready = (states["engine"].get("Health", {}).get("Status") == "healthy"
                         and states["api"].get("Health", {}).get("Status") == "healthy"
                         and states["worker"]["Status"] == "running")
                if ready:
                    return
                if time.monotonic() > deadline:
                    seen = {s: (st["Status"], st.get("Health", {}).get("Status")) for s, st in states.items()}
                    raise AssertionError(f"o stack não ficou pronto em {READY_DEADLINE_SECONDS} s: {seen}")
                time.sleep(1)
        except BaseException as exc:
            self.error = f"falha ao subir o projeto {PROJECT}: {self.scrub(str(exc))[:3000]}"
            raise

    def cleanup(self) -> None:
        if self.started:
            self.run("down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def bank_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """O projeto do teste, **sem** subir nada na criação (a fixture nunca falha): cada teste chama `start()`
    depois de exigir o Docker, o serviço `engine` e a base do PL. Remove, no `finally`, só o projeto
    `cq-systest-bank` (com os volumes)."""
    stack = _Stack(tmp_path_factory.mktemp("bank-system"))
    try:
        yield stack
    finally:
        stack.cleanup()


def _prepare(stack: _Stack) -> None:
    _require_docker()
    _require_engine_service()
    _require_pl_base()
    stack.start()


def _headers(stack: _Stack) -> dict[str, str]:
    return {"Authorization": f"Bearer {stack.admin_token}"}


# O registro e a ativação do bot dentro do contêiner da api, com uma Bot API de mentira só aqui (nenhum pedido ao
# Telegram). Imprime uma linha JSON com o estado do oferecimento.
_SEED_BOT_SCRIPT = r'''
import json, os
from datetime import datetime, timezone
import psycopg
from cq_server import bots
from cq_server.config import BotSettings
from cq_server.ports.telegram import BotIdentity

class _FakeApi:
    def get_me(self):
        return BotIdentity(telegram_bot_id=6100000001, username="sistema_banco_bot")
    def close(self):
        pass

runtime = bots.BotRuntime(BotSettings(master_key=__KEY__), api_factory=lambda token: _FakeApi())
now = datetime.now(timezone.utc)
with psycopg.connect(os.environ["CQ_DATABASE_URL"], autocommit=True) as conn:
    registered = bots.register_bot(conn, __CODE__, token=__TOKEN__, runtime=runtime, now=now, actor_token_id=None)
    activated = bots.activate(conn, __CODE__, now=now, actor_token_id=None)
print(json.dumps({"registered": type(registered).__name__, "activated": type(activated).__name__,
                  "state": getattr(activated, "state", None)}))
'''


def _seed_bot(stack: _Stack, code: str) -> dict:
    script = (_SEED_BOT_SCRIPT.replace("__KEY__", json.dumps(stack.script_master_key))
              .replace("__CODE__", json.dumps(code)).replace("__TOKEN__", json.dumps(FAKE_BOT_TOKEN)))
    return stack.py("api", script)


def _cq(stack: _Stack, args: list[str], tmp: pathlib.Path) -> Any:
    """O `cq` em processo contra a API do Compose, sem o perfil, a pilha nem a configuração de quem roda o teste."""
    from click.testing import CliRunner

    from cq_client.cli import cli

    env = {"CQ_API_URL": stack.url, "CQ_TOKEN": stack.admin_token, "CQ_CONFIG": str(tmp / "sem-config.toml"),
           "CQ_PROFILE": None, "CQ_STACK_DIR": None}
    return CliRunner().invoke(cli, args, env=env, obj=None)


# ---------------------------------------------------------------------------------------------
# Os testes
# ---------------------------------------------------------------------------------------------


def test_bank_uploaded_by_cq_is_dispatched_until_scheduled_with_the_real_engine(
        bank_stack: _Stack, tmp_path: pathlib.Path) -> None:
    """O envio de um banco sintético pelo `cq` e um disparo dele até `scheduled` com o motor real (ver o
    docstring do módulo). Vermelho no esqueleto: o comando `cq banks upload` lança `NotImplementedError`."""
    _prepare(bank_stack)
    folder = bk.copy_course(tmp_path / "curso")
    files = bk.course_files()
    version = bk.reference_version(files)

    result = _cq(bank_stack, ["banks", "upload", str(folder), "--name", BANK_NAME], tmp_path)
    output = bank_stack.scrub(result.output)
    assert result.exit_code == 0, f"cq banks upload saiu com {result.exit_code}:\n{output[-2000:]}"
    assert f"--repo bank:{BANK_NAME}" in output and f"--commit {version}" in output, output[-2000:]
    assert str(folder) not in output, "o caminho local da pasta saiu na saída do cq"

    with httpx2.Client(base_url=bank_stack.url, headers=_headers(bank_stack), timeout=HTTP_TIMEOUT) as api:
        listed = api.get("/v1/question-banks", params={"name": BANK_NAME})
        assert listed.status_code == 200, bank_stack.scrub(listed.text[:500])
        [item] = listed.json()["items"]
        assert (item["repo"], item["version"], item["source"], item["file_count"]) == (
            f"bank:{BANK_NAME}", version, "folder", len(files))

        created = api.post("/v1/offerings", json={"code": OFFERING_CODE, "course": "Física Experimental",
                                                   "term": "2031s1", "timezone": "America/Sao_Paulo"})
        assert created.status_code == 201, bank_stack.scrub(created.text[:500])
        seeded = _seed_bot(bank_stack, OFFERING_CODE)
        assert seeded == {"registered": "Bot", "activated": "Offering", "state": "active"}, seeded

        prepared = api.post(f"/v1/offerings/{OFFERING_CODE}/dispatches",
                            json={"repo": item["repo"], "commit": version, "qid": QID})
        assert prepared.status_code == 200, bank_stack.scrub(prepared.text[:500])
        dispatch_id = prepared.json()["dispatch_id"]

        deadline = time.monotonic() + DISPATCH_DEADLINE_SECONDS
        while True:
            body = api.get(f"/v1/offerings/{OFFERING_CODE}/dispatches/{dispatch_id}").json()
            if body["state"] != "validating":
                break
            assert time.monotonic() < deadline, f"o disparo não saiu de validating em {DISPATCH_DEADLINE_SECONDS} s"
            time.sleep(1)

    assert body["state"] == "scheduled", f"o disparo terminou em {body['state']}: {body.get('rejection')}"
    assert body["question"]["repo"] == f"bank:{BANK_NAME}" and body["question"]["commit"] == version
    assert body["preview"] is not None and body["preview"]["fields"], body["preview"]
    assert [f["type"] for f in body["preview"]["fields"]] == ["number"]


def test_body_above_the_cap_is_refused_by_the_real_server_before_the_body(bank_stack: _Stack) -> None:
    """O `uvicorn` real: o envio com `Content-Length` um byte acima do teto do corpo e nenhum byte de corpo
    recebe 400 `[{body, too_long}]`, sem esperar o corpo. Vermelho no esqueleto: a rota responde 500."""
    _prepare(bank_stack)
    head = ("POST /v1/question-banks HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{bank_stack.port}\r\n"
            f"Authorization: Bearer {bank_stack.admin_token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {MAX_BODY_BYTES + 1}\r\n\r\n").encode("ascii")
    data = b""
    with socket.create_connection(("127.0.0.1", bank_stack.port), timeout=HTTP_TIMEOUT) as sock:
        sock.sendall(head)
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            assert chunk, "a conexão fechou sem resposta"
            data += chunk
        header, _, body = data.partition(b"\r\n\r\n")
        lines = header.decode("latin-1").split("\r\n")
        length = next(int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:"))
        while len(body) < length:
            chunk = sock.recv(65536)
            assert chunk, "a conexão fechou no meio da resposta"
            body += chunk

    assert lines[0].split(" ")[1] == "400", bank_stack.scrub(lines[0])
    assert json.loads(body) == {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                                          "fields": [{"field": "body", "reason": "too_long"}]}}
