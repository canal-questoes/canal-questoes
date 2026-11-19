"""Teste de sistema do motor no Compose (o isolamento efetivo e o adaptador HTTP real).



Sobe o projeto Compose **`cq-systest-engine`** (separado do `cq-systest` do `test_compose.py`
congelado), com segredos gerados no teste e `.env` vazio, no molde do `_Stack` do teste congelado
(reescrito aqui, porque o helper de lá é privado). Nunca roda `docker compose config`, que imprime os
segredos interpolados.

- `test_engine_effective_isolation` (os 7 passos da verificação de isolamento): a configuração
  **efetiva** do contêiner do motor (`docker inspect`), a versão do Docker Engine (≥ 26.0.0, ou ≥ 25.0.5
  na série 25, ou ≥ 23.0.11 na 23: a correção da CVE-2024-29018), uma **sonda local** numa rede não
  interna criada pelo teste (com a imagem do worker do próprio `build`, sem baixar nada) que o worker
  alcança e o motor não; o motor não resolve nem alcança a api, o banco nem o gateway da rede interna;
  sem rota padrão; o DNS embutido devolve SERVFAIL para nome externo (`.invalid`, RFC 6761) e para
  `host.docker.internal`, com o controle positivo `engine` (NOERROR); os logs do motor sem identidade
  nem segredo.
- `test_engine_http_adapter_real_image`: o
  `exampleCourse` do PL (público, no commit fixado da imagem) é copiado para o volume `repos` por um
  contêiner descartável `--network none` e movido com `mv` atômico para `<sha256(repo)>/<commit>`, com o
  `repo` `https://git.test/prairielearn/example-course` (TLD reservado). No worker, o **adaptador HTTP
  real** (`HttpQuestionEngine`, com `EngineSettings.from_env`) inspeciona, cria a variante e corrige as 5
  questões de elemento; os corpos crus das mesmas chamadas passam pelos JSON Schemas do motor de questões
  (`contracts/engine/v1/`); a resposta correta de `element/numberInput` tem nota 1, e a errada, 0; QID
  inexistente → `EngineNotFound`; e **nenhuma** mensagem de erro de formato do PL nos casos de teste dos
  5 tipos cai no recuo da tradução (`formaterrors.translate`): guarda contra mudança nas mensagens do PL.

Nenhum endereço real da internet é contatado (a sonda é local; os nomes consultados são `.invalid`,
`host.docker.internal` e os do próprio projeto). Remove só o que criou, no `finally` da fixture: a
sonda, a rede da sonda e o projeto (`down -v --remove-orphans`). Sem a imagem base do PL no host, falha
com mensagem de ambiente, sem baixar nada.

No esqueleto, o `compose.yaml` ainda não tem o serviço `engine`, e cada função falha
logo no começo por isso (serviço ausente), antes de chamar o Docker.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from cq_server.formaterrors import translate
from tests.helpers.canary import ALL_PREFIXES, DISPATCH_PREFIXES, find_canary, find_secrets
from tests.helpers.engine import schema_errors

pytestmark = pytest.mark.system

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"
ENGINE_DOCKERFILE = REPO / "engine" / "Dockerfile"
PROJECT = "cq-systest-engine"
ENGINE_NETWORK = f"{PROJECT}_engine"
DEFAULT_NETWORK = f"{PROJECT}_default"
REPOS_VOLUME = f"{PROJECT}_repos"
PROBE = "cq-systest-engine-sonda"  # a rede não interna e o contêiner da sonda (o passo 3 da verificação)
PROBE_PORT = 8080
CLOSED_PORT = 1  # porta fechada no gateway (tcpmux): recusa = "alcançou o host"
COMMAND_TIMEOUT = 300
READY_DEADLINE_SECONDS = 240
CONNECT_TIMEOUT_SECONDS = 3
ENGINE_PYTHON = "/PrairieLearn/.venv/bin/python3.13"
ENGINE_USER = "65534:65534"
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
SERVFAIL, NOERROR = 2, 0

# O curso de exemplo do PL, no commit fixado da imagem do motor (engine/Dockerfile), sob um `repo`
# com TLD reservado (RFC 2606): nada é obtido da rede.
EXAMPLE_REPO = "https://git.test/prairielearn/example-course"
PL_COMMIT = "a193f72048530a4499252c454acca963cdf98922"
EXAMPLE_ROOT = "/PrairieLearn/exampleCourse"
SEED = "1abc"
QIDS = ("element/numberInput", "element/integerInput", "element/stringInput", "element/multipleChoice",
        "element/checkbox")
MISSING_QID = "element/naoExisteSonda"
TOKEN = "cqsonda"  # a "resposta" inválida ecoada pelo PL; nunca pode chegar à tradução

# Respostas inválidas por tipo de campo: o campo invalidado → o início exato da mensagem do PL
# a193f72 (conferido contra a imagem). Os demais campos de cada questão ficam
# ausentes e também produzem erros de formato ("No submitted answer." etc.), todos conferidos.
INVALID_ANSWERS: dict[str, dict[str, Any]] = {
    "element/numberInput": {"c_1": TOKEN, "c_2": "", "c_3": "1/0", "c_4": "1/x", "c_5": "inf", "c_b": "2/x"},
    "element/integerInput": {"c_1": "3.5", "c_2": "", "c_7_b2": "12", "c_7_b16": "zz"},
    "element/stringInput": {"ans1": ""},
    "element/multipleChoice": {"sky-color1": TOKEN},
    "element/checkbox": {"group_ans_restricted": ["a"], "group_ans_original": [TOKEN]},
}
EXPECTED_PL_PREFIX: dict[str, dict[str, str]] = {
    "element/numberInput": {
        "c_1": "Invalid format: The submitted answer could not be interpreted as a decimal number.",
        "c_2": "The submitted answer was blank.",
        "c_3": "Your expression resulted in a division by zero.",
        "c_4": "Invalid format: The denominator could not be interpreted as a decimal (or complex) number.",
        "c_5": "Invalid format: The submitted answer is not a finite number.",
        "c_b": "Invalid format: The denominator could not be interpreted as a decimal number.",
        "c_m": "No submitted answer.",
    },
    "element/integerInput": {
        "c_1": "Invalid format: the submitted answer could not be interpreted as an integer.",
        "c_2": "Invalid format: the submitted answer was blank.",
        "c_7_b2": "Invalid format: the submitted answer could not be interpreted as an integer in base 2.",
        "c_7_b16": "Invalid format: the submitted answer could not be interpreted as an integer in base 16.",
        "c_4": "No submitted answer.",
    },
    "element/stringInput": {
        "ans1": "Invalid format. The submitted answer was left blank.",
        "ans2": "No submitted answer.",
    },
    "element/multipleChoice": {
        "sky-color1": f"Invalid choice: {TOKEN}",
        "sky-color2": "No answer was submitted.",
    },
    "element/checkbox": {
        "group_ans_restricted": "You must select between 2 and 4 options.",
        "group_ans_original": f"You selected an invalid option: {TOKEN}",
        "group_ans_v4": "You must select between 1 and 3 options.",
    },
}
# O recuo da tradução: sem regra, e chave fora de `fields[]`.
FALLBACK_PREFIX = "Não consegui aceitar a resposta deste campo."
OUTSIDE_FIELDS = "A questão recusou a resposta. Confira o formato e envie de novo."
ENGLISH_WORDS = re.compile(r"\b(submitted|answer|Invalid|format|option|select|choice|number|blank)\b")


# ---------------------------------------------------------------------------------------------
# Ambiente e o projeto Compose do teste
# ---------------------------------------------------------------------------------------------


def _require_engine_service() -> None:
    """No esqueleto, o `compose.yaml` ainda não tem o motor: a função falha aqui, antes de
    chamar o Docker (serviço ausente)."""
    assert COMPOSE.is_file(), "compose.yaml ausente na raiz do repositório"
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose.get("services") if isinstance(compose, dict) else None
    assert isinstance(services, dict) and isinstance(services.get("engine"), dict), (
        "serviço `engine` ausente do compose.yaml")
    assert isinstance((compose.get("networks") or {}).get("engine"), dict), (
        "rede `engine` ausente do topo do compose.yaml")
    assert "repos" in (compose.get("volumes") or {}), "volume `repos` ausente do compose.yaml"


def _docker(*args: str, timeout: float | None = 120, check: bool = True,
            stdin: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], cwd=REPO, capture_output=True, text=True, input=stdin,
                            encoding="utf-8", errors="replace", timeout=timeout)
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


def _pl_base_image() -> str:
    for line in ENGINE_DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.strip().upper().startswith("FROM "):
            return line.split()[1]
    raise AssertionError("engine/Dockerfile sem FROM")


def _require_pl_base() -> None:
    """a base do PL (4,18 GB, fixada por digest) tem de estar no host; o teste não baixa nada."""
    base = _pl_base_image()
    if _docker("image", "inspect", base, check=False, timeout=60).returncode != 0:
        pytest.fail(f"Ambiente: a imagem base do PL `{base}` não está no host. O teste de sistema do motor "
                    "não baixa imagens (ação externa); obtenha-a antes (`docker pull`) e rode de novo.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _version_has_cve_fix(version: str) -> bool:
    """CVE-2024-29018: corrigida no 26.0.0, no 25.0.5 e no 23.0.11."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return False
    v = tuple(int(x) for x in match.groups())
    return v >= (26, 0, 0) or (v[0] == 25 and v >= (25, 0, 5)) or (v[0] == 23 and v >= (23, 0, 11))


class _Stack:
    """O projeto `cq-systest-engine`, subido sob demanda e removido no `finally` da fixture."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.db_password = secrets.token_urlsafe(24)
        self.admin_token = "cq_" + secrets.token_urlsafe(32)
        self.master_key = secrets.token_urlsafe(32)
        self.port = _free_port()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("COMPOSE_", "CQ_"))}
        self.env.update({"CQ_DB_PASSWORD": self.db_password, "CQ_ADMIN_TOKEN": self.admin_token,
                         "CQ_API_PORT": str(self.port), "CQ_MASTER_KEY": self.master_key,
                         "MSYS_NO_PATHCONV": "1"})
        self.env_file = tmp / "vazio.env"  # não lê o `.env` local de quem roda o teste
        self.env_file.write_text("", encoding="utf-8")
        self.hidden = [self.db_password, self.admin_token, self.master_key]
        self.started = False
        self.error: str | None = None
        self.probe_created = False

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

    def inspect(self, target: str) -> dict:
        return json.loads(_docker("inspect", target).stdout)[0]

    def logs(self, service: str) -> str:
        result = _docker("logs", self.container(service), timeout=120)
        return result.stdout + "\n" + result.stderr

    def py(self, service: str, script: str, *, engine: bool = False) -> Any:
        """Roda `script` (pela entrada padrão) no serviço e devolve o JSON da última linha."""
        interpreter = [ENGINE_PYTHON, "-I", "-S", "-"] if engine else ["python", "-"]
        result = self.run("exec", "-T", service, *interpreter, stdin=script, timeout=COMMAND_TIMEOUT)
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
                states = {s: self.inspect(self.container(s))["State"] for s in ("engine", "worker", "api")}
                ready = (states["engine"].get("Health", {}).get("Status") == "healthy"
                         and states["worker"]["Status"] == "running" and states["api"]["Status"] == "running")
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
        _docker("rm", "-f", PROBE, check=False, timeout=120)
        if self.started:
            self.run("down", "-v", "--remove-orphans", check=False)
        _docker("network", "rm", PROBE, check=False, timeout=120)


@pytest.fixture(scope="module")
def engine_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """O projeto do teste, **sem** subir nada na criação (a fixture nunca falha): cada teste chama
    `start()` depois de exigir o serviço `engine`, o Docker e a base do PL. Remove, no `finally`, só o
    que o teste criou: a sonda, o projeto `cq-systest-engine` (com os volumes) e a rede da sonda."""
    stack = _Stack(tmp_path_factory.mktemp("engine-system"))
    try:
        yield stack
    finally:
        stack.cleanup()


def _prepare(stack: _Stack) -> None:
    _require_engine_service()
    _require_docker()
    _require_pl_base()
    stack.start()


# ---------------------------------------------------------------------------------------------
# Roteiros que rodam dentro dos contêineres (só biblioteca padrão; imprimem uma linha JSON)
# ---------------------------------------------------------------------------------------------

_REACH_SCRIPT = r'''
import json, socket
TIMEOUT = __TIMEOUT__
def resolve(name):
    try:
        return {"ok": True, "addr": socket.gethostbyname(name)}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__}
def connect(host, port):
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return {"reached": True, "error": None}
    except ConnectionRefusedError:
        return {"reached": True, "error": "ConnectionRefusedError"}
    except OSError as exc:
        return {"reached": False, "error": type(exc).__name__}
def http_get(host, port, path):
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            sock.sendall(("GET " + path + " HTTP/1.0\r\nHost: " + host + "\r\n\r\n").encode("ascii"))
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return {"status": int(head.split(b" ")[1]), "body": body.decode("utf-8", "replace")}
    except OSError as exc:
        return {"status": None, "error": type(exc).__name__}
def dns(name):
    import random, struct
    ident = random.randint(0, 65535)
    query = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        query += bytes([len(label)]) + label.encode("ascii")
    query += b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(TIMEOUT)
        try:
            sock.sendto(query, ("127.0.0.11", 53))
            data, _ = sock.recvfrom(4096)
        except OSError as exc:
            return {"rcode": None, "error": type(exc).__name__}
    return {"rcode": struct.unpack(">H", data[2:4])[0] & 0x000F}
def routes():
    with open("/proc/net/route", encoding="ascii") as fh:
        return [line.split()[1] for line in fh.read().splitlines()[1:] if line.split()]
PLAN = __PLAN__
out = {}
for key, (op, *args) in PLAN.items():
    out[key] = {"resolve": resolve, "connect": connect, "http": http_get, "dns": dns, "routes": routes}[op](*args)
print(json.dumps(out))
'''


def _reach(stack: _Stack, service: str, plan: dict[str, list], *, engine: bool = False) -> dict:
    script = _REACH_SCRIPT.replace("__TIMEOUT__", str(CONNECT_TIMEOUT_SECONDS)).replace(
        "__PLAN__", json.dumps(plan))
    return stack.py(service, script, engine=engine)


_ADAPTER_SCRIPT = r'''
import dataclasses, json, os, urllib.error, urllib.request
from collections.abc import Mapping
from cq_server.config import EngineSettings
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.ports.engine import EngineError, QuestionRef

REPO, COMMIT, SEED = __REPO__, __COMMIT__, __SEED__
QIDS, MISSING, INVALID = __QIDS__, __MISSING__, __INVALID__

def conv(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        d = {f.name: conv(getattr(o, f.name)) for f in dataclasses.fields(o)}
        d["__type__"] = type(o).__name__
        return d
    if isinstance(o, Mapping):
        return {str(k): conv(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [conv(v) for v in o]
    return o

def call(fn, *args):
    try:
        return conv(fn(*args))
    except EngineError as exc:
        return {"__type__": type(exc).__name__, "kind": exc.kind}

settings = EngineSettings.from_env(os.environ)
base = settings.engine_url.rstrip("/")
def raw(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read())}

engine = HttpQuestionEngine(settings)
out = {"engine_url": settings.engine_url, "questions": {}}
try:
    for qid in QIDS:
        ref = QuestionRef(REPO, COMMIT, qid)
        wire = {"repo": REPO, "commit": COMMIT, "qid": qid}
        q = {"inspect": call(engine.inspect, ref), "raw_inspect": raw("/v1/inspect", {"question_ref": wire})}
        variant = engine.create_variant(ref, SEED)
        q["variant"] = conv(variant)
        q["variant_again"] = call(engine.create_variant, ref, SEED)
        q["raw_variant"] = raw("/v1/variants", {"question_ref": wire, "seed": SEED})
        snapshot = variant.snapshot
        snapshot_json = conv(snapshot)
        q["invalid"] = call(engine.grade, ref, snapshot, INVALID[qid])
        q["raw_invalid"] = raw("/v1/grade", {"question_ref": wire, "snapshot": snapshot_json,
                                             "raw_submitted_answers": INVALID[qid]})
        if qid == "element/numberInput":
            ca = snapshot_json["correct_answers"]
            fields = [f["name"] for f in conv(variant.ir)["fields"]]
            correct = {n: ("" if ca[n] == "" else repr(ca[n]) if isinstance(ca[n], float) else str(ca[n]))
                       for n in fields}
            wrong = {n: "999" for n in fields}
            q["correct_answers_sent"] = correct
            q["correct"] = call(engine.grade, ref, snapshot, correct)
            q["wrong"] = call(engine.grade, ref, snapshot, wrong)
            q["raw_correct"] = raw("/v1/grade", {"question_ref": wire, "snapshot": snapshot_json,
                                                 "raw_submitted_answers": correct})
        out["questions"][qid] = q
    missing = QuestionRef(REPO, COMMIT, MISSING)
    out["missing_inspect"] = call(engine.inspect, missing)
    out["raw_missing"] = raw("/v1/inspect", {"question_ref": {"repo": REPO, "commit": COMMIT, "qid": MISSING}})
finally:
    engine.close()
print(json.dumps(out))
'''


def _adapter_script() -> str:
    replacements = {"__REPO__": json.dumps(EXAMPLE_REPO), "__COMMIT__": json.dumps(PL_COMMIT),
                    "__SEED__": json.dumps(SEED), "__QIDS__": json.dumps(list(QIDS)),
                    "__MISSING__": json.dumps(MISSING_QID), "__INVALID__": json.dumps(INVALID_ANSWERS)}
    script = _ADAPTER_SCRIPT
    for key, value in replacements.items():
        script = script.replace(key, value)
    return script


# ---------------------------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------------------------


def test_engine_effective_isolation(engine_stack: _Stack) -> None:
    """Os 7 passos da verificação de isolamento: a configuração efetiva do motor, a versão do
    Docker Engine, a sonda local (o worker alcança, o motor não), a api, o banco e o gateway fora do
    alcance do motor, sem rota padrão, DNS sem nome externo (SERVFAIL; controle `engine`), e os logs do
    motor sem identidade nem segredo."""
    stack = engine_stack
    _prepare(stack)

    # 1. Configuração efetiva do contêiner do motor.
    engine_id = stack.container("engine")
    info = stack.inspect(engine_id)
    host = info["HostConfig"]
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Privileged"] is False
    assert host["PidMode"] == ""
    assert host["IpcMode"] != "host"
    assert not host.get("PortBindings"), host.get("PortBindings")
    assert host["PidsLimit"] > 0 and host["Memory"] > 0 and host["NanoCpus"] > 0
    assert host["RestartPolicy"]["Name"] == "unless-stopped"
    assert host["LogConfig"]["Type"] in ("json-file", "local")
    assert {"max-size", "max-file"} <= set(host["LogConfig"]["Config"]), host["LogConfig"]
    assert list(host.get("Tmpfs") or {}) == ["/tmp"] and "size=" in host["Tmpfs"]["/tmp"], host.get("Tmpfs")
    assert info["Config"]["User"] == ENGINE_USER
    mounts = [(m["Type"], m.get("Name"), m["Destination"], m["RW"]) for m in info["Mounts"] if m["Type"] != "tmpfs"]
    assert mounts == [("volume", REPOS_VOLUME, "/repos", False)], mounts
    networks = info["NetworkSettings"]["Networks"]
    assert set(networks) == {ENGINE_NETWORK}, set(networks)
    engine_net = stack.inspect(ENGINE_NETWORK)
    assert engine_net["Internal"] is True
    assert engine_net["Options"].get(INHIBIT_IPV4) == "true", engine_net["Options"]
    # Nenhum segredo exposto: nenhum segredo gerado nem variável de segredo no ambiente do motor.
    env_names = [e.partition("=")[0] for e in info["Config"]["Env"]]
    assert [n for n in env_names if n.startswith("CQ_")] == [], env_names
    assert find_secrets("\n".join(info["Config"]["Env"]), stack.hidden) == []

    # 2. Versão do Docker Engine com a correção da CVE-2024-29018.
    version = _docker("version", "--format", "{{.Server.Version}}").stdout.strip()
    assert _version_has_cve_fix(version), f"Docker Engine {version} sem a correção da CVE-2024-29018"
    assert [_version_has_cve_fix(v) for v in ("26.0.0", "27.3.1", "25.0.5", "25.0.4", "24.0.9", "23.0.11",
                                              "23.0.10")] == [True, True, True, False, False, True, False]

    # 3. A sonda: rede não interna do teste e um servidor HTTP com a imagem do worker do próprio build.
    worker_id = stack.container("worker")
    worker_image = stack.inspect(worker_id)["Image"]
    _docker("rm", "-f", PROBE, check=False)
    _docker("network", "rm", PROBE, check=False)
    _docker("network", "create", PROBE)
    stack.probe_created = True
    _docker("run", "-d", "--name", PROBE, "--network", PROBE, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", worker_image, "python", "-m", "http.server",
            str(PROBE_PORT), "--bind", "0.0.0.0")
    _docker("network", "connect", PROBE, worker_id)
    probe_ip = stack.inspect(PROBE)["NetworkSettings"]["Networks"][PROBE]["IPAddress"]
    api_ip = stack.inspect(stack.container("api"))["NetworkSettings"]["Networks"][DEFAULT_NETWORK]["IPAddress"]
    pg_ip = stack.inspect(stack.container("postgres"))["NetworkSettings"]["Networks"][DEFAULT_NETWORK]["IPAddress"]
    default_gateway = stack.inspect(DEFAULT_NETWORK)["IPAM"]["Config"][0]["Gateway"]
    ipam = engine_net["IPAM"]["Config"][0]
    internal_gateway = ipam.get("Gateway") or re.sub(r"\.\d+/\d+$", ".1", ipam["Subnet"])
    assert all([probe_ip, api_ip, pg_ip, default_gateway, internal_gateway])

    # 4. Controles positivos, no worker: a sonda, o motor e a recusa do gateway da `default`.
    deadline = time.monotonic() + 30
    while True:
        worker = _reach(stack, "worker", {
            "resolve_probe": ["resolve", PROBE],
            "http_probe": ["http", PROBE, PROBE_PORT, "/"],
            "health": ["http", "engine", 8080, "/v1/health"],
            "gateway_closed": ["connect", default_gateway, CLOSED_PORT],
        })
        if worker["http_probe"].get("status") == 200 or time.monotonic() > deadline:
            break
        time.sleep(1)
    assert worker["resolve_probe"] == {"ok": True, "addr": probe_ip}, worker
    assert worker["http_probe"]["status"] == 200, worker
    assert worker["health"]["status"] == 200, worker
    assert schema_errors("health_response", json.loads(worker["health"]["body"])) == []
    assert worker["gateway_closed"] == {"reached": True, "error": "ConnectionRefusedError"}, (
        "controle do método: a porta fechada do gateway da `default` precisa recusar", worker)

    # 5 e 6. Negativos, no motor (o mesmo usuário e o mesmo netns do código da questão), e o DNS.
    external = f"sonda-{secrets.token_hex(4)}.invalid"
    engine = _reach(stack, "engine", {
        "resolve_probe": ["resolve", PROBE],
        "resolve_api": ["resolve", "api"],
        "resolve_postgres": ["resolve", "postgres"],
        "connect_probe": ["connect", probe_ip, PROBE_PORT],
        "connect_api": ["connect", api_ip, 8000],
        "connect_postgres": ["connect", pg_ip, 5432],
        "gateway_api_port": ["connect", internal_gateway, stack.port],
        "gateway_closed": ["connect", internal_gateway, CLOSED_PORT],
        "routes": ["routes"],
        "dns_external": ["dns", external],
        "dns_host": ["dns", "host.docker.internal"],
        "dns_engine": ["dns", "engine"],
    }, engine=True)
    for key in ("resolve_probe", "resolve_api", "resolve_postgres"):
        assert engine[key]["ok"] is False, (key, engine[key])
    for key in ("connect_probe", "connect_api", "connect_postgres", "gateway_api_port", "gateway_closed"):
        assert engine[key]["reached"] is False, (f"o motor alcançou {key}", engine[key])
        assert engine[key]["error"] not in (None, "ConnectionRefusedError"), engine[key]
    assert "00000000" not in engine["routes"], f"o motor tem rota padrão: {engine['routes']}"
    assert engine["dns_external"] == {"rcode": SERVFAIL}, engine["dns_external"]
    assert engine["dns_host"] == {"rcode": SERVFAIL}, engine["dns_host"]
    assert engine["dns_engine"] == {"rcode": NOERROR}, "controle positivo do resolvedor embutido"

    # 7. Logs do motor e do worker: sem identidade canário nem segredo; o worker com o motor configurado
    # não registra `worker.engine_disabled`.
    engine_logs = stack.logs("engine")
    worker_logs = stack.logs("worker")
    for name, text in (("engine", engine_logs), ("worker", worker_logs)):
        assert find_canary(text) == [] and find_canary(text, prefixes=ALL_PREFIXES + DISPATCH_PREFIXES) == [], name
        assert find_secrets(text, stack.hidden) == [], f"segredo nos logs de {name}"
    assert "worker.engine_disabled" not in worker_logs


def test_engine_http_adapter_real_image(engine_stack: _Stack) -> None:
    """O adaptador HTTP real, no worker, contra a imagem real do
    motor, com o `exampleCourse` do PL copiado para o volume `repos`: `inspect` (as 5 questões
    suportadas), `create_variant` (seed fixa, determinística) e `grade` (nota 1 na resposta correta de
    `element/numberInput` e 0 na errada), com os corpos crus validados pelos JSON Schemas do motor de questões; QID
    inexistente → `EngineNotFound`; e toda mensagem de erro de formato do PL nos casos dos 5 tipos é
    traduzida por regra, sem cair no recuo nem ecoar a entrada."""
    stack = engine_stack
    _prepare(stack)

    # O curso de exemplo, copiado sem rede para <sha256(repo)>/<commit>, com `mv` atômico.
    engine_image = stack.inspect(stack.container("engine"))["Image"]
    key = hashlib.sha256(EXAMPLE_REPO.encode("utf-8")).hexdigest()
    token = secrets.token_hex(6)
    staging = f"/repos/.tmp-{token}"
    _docker("run", "--rm", "--name", f"cq-systest-engine-prep-{token}", "--network", "none", "--user", "999:999",
            "-v", f"{REPOS_VOLUME}:/repos", "--entrypoint", "sh", engine_image, "-c",
            f"set -e; mkdir -p {staging} /repos/{key}; cp -R {EXAMPLE_ROOT} {staging}/c; "
            f"mv {staging}/c /repos/{key}/{PL_COMMIT}; rmdir {staging}", timeout=COMMAND_TIMEOUT)

    out = stack.py("worker", _adapter_script())
    assert out["engine_url"] == "http://engine:8080"
    assert set(out["questions"]) == set(QIDS)

    for qid in QIDS:
        q = out["questions"][qid]
        # inspect: suportada, pelo adaptador e no corpo cru (contrato do motor de questões).
        assert q["raw_inspect"]["status"] == 200
        assert schema_errors("inspect_response", q["raw_inspect"]["body"]) == [], qid
        assert q["raw_inspect"]["body"]["outcome"] == "supported", qid
        assert q["inspect"]["__type__"] == "Supported", (qid, q["inspect"])
        question = {k: v for k, v in q["inspect"]["question"].items() if k != "__type__"}
        assert question == q["raw_inspect"]["body"]["question"], qid
        # create_variant: a variante crua e a do adaptador são iguais, e a seed fixa é determinística.
        assert q["raw_variant"]["status"] == 200
        assert schema_errors("create_variant_response", q["raw_variant"]["body"]) == [], qid
        assert q["variant"]["__type__"] == "Variant", (qid, q["variant"].get("__type__"))
        assert schema_errors("snapshot", q["variant"]["snapshot"]) == [], qid
        assert schema_errors("ir", q["variant"]["ir"]) == [], qid
        assert q["variant"]["snapshot"] == q["raw_variant"]["body"]["snapshot"], qid
        assert q["variant"]["ir"] == q["raw_variant"]["body"]["ir"], qid
        assert q["variant_again"] == q["variant"], qid
        assert q["variant"]["snapshot"]["seed"] == SEED and q["variant"]["snapshot"]["pl_commit"] == PL_COMMIT
        # grade inválido: o corpo cru no contrato e o adaptador com os mesmos erros de formato.
        assert q["raw_invalid"]["status"] == 200
        assert schema_errors("grade_response", q["raw_invalid"]["body"]) == [], qid
        assert q["invalid"]["__type__"] == "Graded" and q["invalid"]["valid"] is False, qid
        assert q["invalid"]["score"] is None
        errors = {name: e["text"] for name, e in q["invalid"]["format_errors"].items()}
        assert errors == {name: e["text"] for name, e in q["raw_invalid"]["body"]["format_errors"].items()}, qid
        # os casos esperados aparecem com a mensagem do PL a193f72 ...
        for name, prefix in EXPECTED_PL_PREFIX[qid].items():
            assert errors.get(name, "").startswith(prefix), (qid, name, errors.get(name))
        # ... e toda mensagem de erro de formato do PL chega traduzida por regra (nenhuma no recuo).
        fields = {f["name"]: f for f in q["variant"]["ir"]["fields"]}
        assert set(errors) <= set(fields), (qid, sorted(set(errors) - set(fields)))
        for name, text in errors.items():
            field = fields[name]
            explained = translate(field["type"], text, hint=field["hint"])
            assert isinstance(explained, str) and explained.strip(), (qid, name)
            assert not explained.startswith(FALLBACK_PREFIX), (f"{qid}:{name} caiu no recuo", text)
            assert explained != OUTSIDE_FIELDS, (qid, name, text)
            assert TOKEN not in explained, (f"{qid}:{name} ecoou a entrada", explained)
            assert text.split(".")[0] not in explained and not ENGLISH_WORDS.search(explained), (
                f"{qid}:{name} em inglês", explained)
        # Controle positivo do recuo: a mesma tradução, com um texto sem regra, cai no recuo com a dica.
        first = next(iter(fields.values()))
        assert translate(first["type"], "cq sonda sem regra", hint=first["hint"]).startswith(FALLBACK_PREFIX)

    # A resposta correta do snapshot de element/numberInput tem nota 1; a errada, 0.
    number = out["questions"]["element/numberInput"]
    assert number["correct"]["__type__"] == "Graded", number["correct"]
    assert (number["correct"]["valid"], number["correct"]["score"], number["correct"]["format_errors"]) == (
        True, 1, {})
    assert (number["wrong"]["valid"], number["wrong"]["score"], number["wrong"]["format_errors"]) == (True, 0, {})
    assert number["raw_correct"]["status"] == 200
    assert schema_errors("grade_response", number["raw_correct"]["body"]) == []
    assert number["raw_correct"]["body"]["score"] == number["correct"]["score"] == 1
    assert number["correct"]["correct_answers"] == number["variant"]["snapshot"]["correct_answers"]
    assert "technical_detail" not in json.dumps(out)

    # QID inexistente: 404 `not_found` no contrato e `EngineNotFound` no adaptador.
    assert out["raw_missing"]["status"] == 404
    assert schema_errors("error_response", out["raw_missing"]["body"]) == []
    assert out["raw_missing"]["body"]["error"]["code"] == "not_found"
    assert out["missing_inspect"] == {"__type__": "EngineNotFound", "kind": "not_found"}

    # Nenhum segredo gerado nem identidade nos logs do motor depois das chamadas.
    engine_logs = stack.logs("engine")
    assert find_secrets(engine_logs, stack.hidden) == []
    assert find_canary(engine_logs) == []
