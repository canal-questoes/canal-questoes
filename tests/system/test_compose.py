"""Teste de sistema: o Compose de verdade.


Sobe api, worker e postgres pelo projeto Compose `cq-systest`, com segredos e porta gerados no
teste, exercita a API com uma pessoa canário, põe uma tarefa sem tratador e uma mensagem ao canário
direto nas filas do banco para o `cq-server worker` do Compose processar, e
confere que os logs dos três serviços não têm identidade nem segredo. Remove só o que criou (o
projeto `cq-systest`, com `down -v`). Nunca roda `docker compose config`, que imprime os segredos
interpolados.

Mensagens livres opcionais: o
`Offering` traz `free_messages` e `contact_email_set`; o corpo exato esperado do oferecimento cadastrado sem
esses campos traz os dois, falsos (o padrão). Sem as chaves na resposta, a comparação falha.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import pathlib
import re
import secrets
import socket
import subprocess
import time
import unicodedata
import urllib.parse
from typing import Any

import httpx2
import pytest
import yaml

from cq_server.migrate import EXPECTED_VERSION
from tests.helpers.api import auth
from tests.helpers.canary import ZEFIRO, find_canary, find_secrets
from tests.helpers.contract import assert_error, assert_matches
from tests.helpers.secrets import all_secrets, remember_secret

# oferecimentos e bots
import hashlib

from tests.helpers.api import marker

pytestmark = pytest.mark.system

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"
PROJECT = "cq-systest"
HEALTH_DEADLINE_SECONDS = 120  # saudável em até 2 min depois das imagens prontas
COMMAND_TIMEOUT = 300
LOG_DEADLINE_SECONDS = 15
WORKER_DEADLINE_SECONDS = 30
NO_HANDLER_ERROR = "sem tratador para o tipo de tarefa"  # a mensagem de `cq_server.tasks`
# A versão do esquema: a da migração mais recente; o nome da constante fica. As migrações em ordem:
# `0003`, a do vínculo do aluno (`0003_student_enrollment.sql`);
# `0004`, a do disparo e da resposta;
# `0005`, a dos relatórios;
# `0006`, a do expurgo e do CLI;
# `0007`, a da implantação (`0007_mail_probe.sql`).
CURRENT_SCHEMA_VERSION = "0007"
SYSTEM_OFFERING = "sistema-oferta"
# Registros do worker com bots que não podem aparecer com a chave-mestra no ar e
# sem nenhum bot: sem chave (`worker.bots_disabled`), posse ou problema de consulta.
IDLE_BOT_EVENTS = ("worker.bots_disabled", "bot.lease", "bot.poll_problem")
# Vínculo do aluno: as 7 variáveis do e-mail, que o worker recebe
# do Compose como `${…:-}` (vazias sem configuração) e a api não recebe.
SMTP_VARS = ("CQ_SMTP_FROM", "CQ_SMTP_HOST", "CQ_SMTP_PASSWORD", "CQ_SMTP_PORT", "CQ_SMTP_SECURITY",
             "CQ_SMTP_TIMEOUT_SECONDS", "CQ_SMTP_USERNAME")
# Vínculo do aluno: o aviso único do worker que sobe sem servidor de e-mail.
MAIL_DISABLED = {"level": "WARNING", "logger": "cq_server.worker", "event": "worker.mail_disabled"}


class _Stack:
    """Chamadas ao `docker compose` do projeto de teste, com a saída sem os segredos gerados."""

    def __init__(self, env: dict[str, str], env_file: pathlib.Path, hidden: list[str]) -> None:
        self.env = env
        self.env_file = env_file
        self.hidden = hidden

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
                f"`docker compose {' '.join(args)}` falhou ({result.returncode}):\n"
                f"{self.scrub(result.stderr[-3000:])}")
        return result

    def logs(self, service: str) -> str:
        """A saída bruta do container do serviço, pelo `docker logs`. O `docker compose logs` não
        serve: o Compose 2.26.1 escreve ESC[2K antes de cada linha, mesmo com --no-color
        (docker/compose, cmd/formatter/logs.go@v2.26.1), e o formato JSON é conferido aqui."""
        cmd = ["docker", "logs", self.container(service)]
        result = subprocess.run(cmd, cwd=REPO, env=self.env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
        assert result.returncode == 0, (
            f"`docker logs` de {service} falhou ({result.returncode}):\n"
            f"{self.scrub(result.stderr[-3000:])}")
        return result.stdout + "\n" + result.stderr

    def container(self, service: str) -> str:
        ids = self.run("ps", "-q", service, timeout=60).stdout.split()
        assert len(ids) == 1, f"esperava um container de {service}, achei {len(ids)}"
        return ids[0]

    def psql(self, sql: str) -> str:
        """Roda `sql` (pela entrada padrão) no banco do Compose, com o `psql` do container `postgres`
        (socket local, usuário e banco do próprio container); devolve a saída sem cabeçalhos."""
        result = self.run(
            "exec", "-T", "postgres", "sh", "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -qtA -U "${POSTGRES_USER:-postgres}" '
            '-d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"',
            stdin="SET client_encoding = 'UTF8';\n" + sql + "\n", timeout=120)
        return result.stdout.strip()


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _surface(resp: Any) -> str:
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    return resp.text + "\n" + headers


def _wait_health(http: Any, started: float) -> Any:
    deadline = started + HEALTH_DEADLINE_SECONDS
    last: str = "sem resposta"
    while time.monotonic() < deadline:
        try:
            resp = http.get("/health")
        except Exception as exc:  # conexão recusada enquanto os serviços sobem
            last = type(exc).__name__
        else:
            last = f"{resp.status_code} {resp.text[:300]}"
            if resp.status_code == 200:
                return resp
        time.sleep(1)
    pytest.fail(f"/health não ficou ok em {HEALTH_DEADLINE_SECONDS} s depois do `up` (último: {last})")


def _json_records(text: str) -> tuple[list[dict], list[str]]:
    records, bad = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad.append(line[:200])
            continue
        if not isinstance(record, dict) or not {"level", "logger", "event"} <= set(record):
            bad.append(line[:200])
            continue
        records.append(record)
    return records, bad


def _includes(records: list[dict], expected: dict) -> bool:
    return any(expected.items() <= r.items() for r in records)


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ascii(text: str) -> str:
    """O texto sem acentos: o arquivo do console só recebe ASCII (nada fixa a codificação dele),
    e o detector de canário acha o nome do mesmo jeito (ele compara sem acentuação)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def test_compose_stack_end_to_end(tmp_path: pathlib.Path) -> None:
    """um ambiente limpo sobe com api, worker e postgres e fica saudável em até
    120 s; com uma pessoa canário cadastrada, revelada e em conflito (400, 404, 409),
    os logs de api, worker e postgres não têm identidade, segredos nem `duplicate key`; logs de api e
    worker em JSON; o `cq-server worker` do Compose processa uma tarefa sem tratador
    (`failed`, `task.failed` sem ator) e entrega ao canário uma mensagem no arquivo
    `CQ_CONSOLE_OUTPUT`, fora do stdout dos logs técnicos, com
    `task.processed`/`outbox.dispatched` e sem `event: exception`; o worker fica no ar; os dados
    persistem depois de `down`/`up` (volume pgdata).

    Oferecimentos e bots, com as variáveis do Compose
    no teste de sistema:
    o teste gera `CQ_MASTER_KEY` e a esconde das saídas (`hidden`); a api e o
    worker recebem a mesma chave (conferida por resumo, sem imprimi-la); o esquema no ar é o da
    migração `0007`; um oferecimento com o nome canário na disciplina é cadastrado e consultado pelo
    código e pelo identificador (disciplina redigida), com o evento `offering.created` exato, e continua
    lá depois de `down`/`up`; o worker, com a chave e sem bots, fica ocioso (sem `worker.bots_disabled`,
    posse nem problema de consulta), sem exceção, e nenhum dos três serviços registra a chave-mestra.
    O Telegram fica de fora: nenhum bot é registrado.

    Vínculo do aluno:
    o esquema no ar é o da migração `0007`; o ambiente passado ao Compose não leva as
    `CQ_SMTP_*` do shell de quem roda (o teste não depende dele); o worker recebe as 7 variáveis do
    e-mail vazias, e a api nenhuma; o worker sobe sem servidor de e-mail configurado, registra
    exatamente um `worker.mail_disabled` (WARNING, logger `cq_server.worker`) antes do `worker.started`,
    sem exceção e sem reiniciar, e nenhum dos três serviços registra segredo. O e-mail fica de fora:
    nenhum servidor SMTP é configurado."""
    assert COMPOSE.exists(), "compose.yaml ausente na raiz do repositório"
    _require_docker()

    db_password = secrets.token_urlsafe(24)
    admin_token = "cq_" + secrets.token_urlsafe(32)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_") and not k.startswith("CQ_SMTP_")}
    env.update({"CQ_DB_PASSWORD": db_password, "CQ_ADMIN_TOKEN": admin_token, "CQ_API_PORT": str(port),
                "MSYS_NO_PATHCONV": "1"})
    # chave-mestra dos bots gerada no teste (32 bytes em base64url sem "=", 43 caracteres).
    master_key = secrets.token_urlsafe(32)
    assert len(master_key) == 43 and "=" not in master_key
    env["CQ_MASTER_KEY"] = master_key
    env_file = tmp_path / "vazio.env"  # não lê o `.env` local de quem roda o teste
    env_file.write_text("", encoding="utf-8")
    stack = _Stack(env, env_file, [db_password, admin_token])
    stack.hidden.append(master_key)  # oculta em toda saída do `docker compose` que o teste mostra
    version = importlib.metadata.version("cq-server")
    expected_health = {"status": "ok", "version": version, "database": "ok",
                       "schema_version": EXPECTED_VERSION, "expected_schema_version": EXPECTED_VERSION}
    admin = auth(admin_token)

    stack.run("down", "-v", "--remove-orphans")  # ambiente limpo: só o projeto do próprio teste
    try:
        stack.run("build", timeout=None)
        pg_image = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["postgres"]["image"]
        if subprocess.run(["docker", "image", "inspect", pg_image], capture_output=True,
                          timeout=60).returncode != 0:
            subprocess.run(["docker", "pull", pg_image], capture_output=True, check=True)

        started = time.monotonic()
        stack.run("up", "-d")
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as http:
            health = _wait_health(http, started)
            assert health.json() == expected_health
            assert_matches("system.health", 200, health.json())
            # O esquema no ar tem a migração mais recente.
            assert (health.json()["schema_version"], health.json()["expected_schema_version"]) == (
                CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)

            responses: list[tuple[str, Any]] = []

            # Cadastro de uma pessoa canário e criação de um token.
            resp = http.post("/v1/people", json=ZEFIRO.payload(), headers=admin)
            assert resp.status_code == 201, resp.text[:300]
            person = resp.json()
            assert_matches("people.register", 201, person)
            assert set(person) == {"pseudo_id", "alias", "created_at"}
            responses.append(("people.register 201", resp))

            resp = http.post("/v1/tokens", json={"label": "token do teste de sistema",
                                                  "permissions": ["people:read"]}, headers=admin)
            assert resp.status_code == 201, resp.text[:300]
            created = resp.json()
            assert_matches("tokens.create", 201, created)
            remember_secret(created["secret"])
            stack.hidden.append(created["secret"])
            assert find_canary(_surface(resp)) == []

            resp = http.get("/v1/people", headers=auth(created["secret"]))
            assert resp.status_code == 200 and resp.json()["items"] == [person]
            responses.append(("people.list 200 (token criado)", resp))

            # Revelação (a única resposta que pode ter a identidade: controle positivo).
            resp = http.post("/v1/identity/reveal", json={
                "pseudonyms": [person["alias"]], "reason": "conferencia do teste de sistema"}, headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            assert resp.json() == {"people": [{"pseudo_id": person["pseudo_id"], "alias": person["alias"],
                                               "name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": ZEFIRO.email}],
                                   "not_found": []}
            assert {f"nome:{ZEFIRO.name}", f"ra:{ZEFIRO.ra}", f"email:{ZEFIRO.email}"} <= set(
                find_canary(resp.text))

            # Revogação: o token para de funcionar na hora.
            resp = http.post(f"/v1/tokens/{created['token_id']}/revoke", headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            revoked = resp.json()
            assert_matches("tokens.revoke", 200, revoked)
            assert revoked == {"token_id": created["token_id"], "label": "token do teste de sistema",
                               "permissions": ["people:read"], "created_at": created["created_at"],
                               "revoked_at": revoked["revoked_at"]}
            assert isinstance(revoked["revoked_at"], str)
            responses.append(("tokens.revoke 200", resp))
            resp = http.get("/v1/people", headers=auth(created["secret"]))
            assert_error(resp, 401, "unauthenticated")
            responses.append(("people.list 401 (revogado)", resp))

            # 400 com a identidade canário no corpo e na chave da consulta.
            resp = http.post("/v1/people", json={"name": "Pessoa Sistema Invalida",
                                                 "email": f"{ZEFIRO.name} <{ZEFIRO.email}>"}, headers=admin)
            assert_error(resp, 400, "invalid_request", fields=[("email", "invalid_format")])
            responses.append(("people.register 400", resp))
            resp = http.get("/v1/people", params={ZEFIRO.name: "1"}, headers=admin)
            assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
            responses.append(("people.list 400", resp))

            # 404 com o RA canário no caminho e rota inexistente com o nome canário.
            resp = http.get(f"/v1/people/{ZEFIRO.ra}a", headers=admin)
            assert_error(resp, 404, "not_found")
            responses.append(("people.get 404", resp))
            resp = http.get(f"/v1/{urllib.parse.quote(ZEFIRO.name)}", headers=admin)
            assert_error(resp, 404, "not_found")
            responses.append(("rota inexistente 404", resp))

            # 409 com o RA e o e-mail canário, sem o pseudônimo existente.
            resp = http.post("/v1/people", json=ZEFIRO.payload(), headers=admin)
            assert_error(resp, 409, "conflict", fields=[("ra", "duplicate"), ("email", "duplicate")])
            assert person["alias"] not in resp.text and person["pseudo_id"] not in resp.text
            responses.append(("people.register 409", resp))

            # Oferecimentos e bots: cadastro e consulta de um oferecimento, com o nome canário
            # na disciplina (redigido na gravação e na saída). Sem bot: o Telegram fica de fora.
            resp = http.post("/v1/offerings", json={
                "code": SYSTEM_OFFERING, "course": f"Seminário de {ZEFIRO.name}", "term": "2031s1",
                "timezone": "America/Sao_Paulo"}, headers=admin)
            assert resp.status_code == 201, resp.text[:300]
            offering = resp.json()
            assert_matches("offerings.create", 201, offering)
            assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                                offering["offering_id"]), offering
            assert offering == {
                "offering_id": offering["offering_id"], "code": SYSTEM_OFFERING,
                "course": f"Seminário de {marker(person['alias'])}", "term": "2031s1",
                "timezone": "America/Sao_Paulo", "response_window_minutes": 1440, "state": "draft",
                "created_at": offering["created_at"], "activated_at": None, "closed_at": None,
                "archived_at": None, "bot": None, "free_messages": False, "contact_email_set": False,
                # Expurgo: o `Offering` traz `purged_at`, nulo sem expurgo.
                "purged_at": None}
            responses.append(("offerings.create 201", resp))
            for ref in (SYSTEM_OFFERING, offering["offering_id"]):
                resp = http.get(f"/v1/offerings/{ref}", headers=admin)
                assert resp.status_code == 200, resp.text[:300]
                assert_matches("offerings.get", 200, resp.json())
                assert resp.json() == offering
                responses.append((f"offerings.get 200 ({'código' if ref == SYSTEM_OFFERING else 'id'})", resp))
            resp = http.get("/v1/offerings", headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            assert_matches("offerings.list", 200, resp.json())
            assert resp.json()["items"] == [offering]
            responses.append(("offerings.list 200", resp))
            resp = http.get("/v1/events", params={"types": "offering.created"}, headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            assert_matches("events.list", 200, resp.json())
            assert [(e["type"], e["actor_token_id"], e["data"]) for e in resp.json()["items"]] == [
                ("offering.created", "bootstrap", {"offering_id": offering["offering_id"], "code": SYSTEM_OFFERING})]
            responses.append(("events.list 200 (offering.created)", resp))

            # Auditoria: os eventos, só com pseudônimos, na ordem das operações.
            resp = http.get("/v1/events", params=[("types", t) for t in (
                "person.registered", "token.created", "token.revoked", "identity.revealed",
                "identity.reveal_denied")], headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            assert_matches("events.list", 200, resp.json())
            events = resp.json()["items"]
            assert [(e["type"], e["actor_token_id"], e["data"]) for e in events] == [
                ("person.registered", "bootstrap", {"pseudo_id": person["pseudo_id"], "alias": person["alias"]}),
                ("token.created", "bootstrap", {"token_id": created["token_id"],
                                                "label": "token do teste de sistema",
                                                "permissions": ["people:read"]}),
                ("identity.revealed", "bootstrap", {"client": "other", "revealed": [person["pseudo_id"]],
                                                    "not_found_count": 0,
                                                    "reason": "conferencia do teste de sistema"}),
                ("token.revoked", "bootstrap", {"token_id": created["token_id"]}),
            ]
            seqs = [e["seq"] for e in events]
            assert seqs == sorted(set(seqs))
            responses.append(("events.list 200", resp))

            # Worker: uma tarefa sem tratador e uma mensagem ao canário, gravadas direto
            # nas filas (as tabelas e o enfileiramento do produto), para o `cq-server worker` do Compose.
            text = f"Mensagem do teste de sistema para {_ascii(ZEFIRO.name)}"
            assert f"nome:{ZEFIRO.name}" in find_canary(text)  # controle positivo do detector
            queued = json.loads(stack.psql(
                "WITH t AS (INSERT INTO tasks (task_type, payload, state, attempts, available_at, created_at) "
                "VALUES ('sistema.sem_tratador', '{}'::jsonb, 'pending', 0, now() - interval '1 minute', now()) "
                "RETURNING id), "
                "m AS (INSERT INTO outbox_messages (recipient_person_id, text, state, attempts, available_at, "
                "created_at) "
                f"SELECT id, {_sql_text(text)}, 'pending', 0, now() - interval '1 minute', now() "
                f"FROM people WHERE pseudo_id = {_sql_text(person['pseudo_id'])}::uuid RETURNING id) "
                "SELECT json_build_object('task_id', (SELECT id FROM t), 'message_id', (SELECT id FROM m));"))
            task_id, message_id = queued["task_id"], queued["message_id"]
            assert isinstance(task_id, int) and isinstance(message_id, int), (
                f"tarefa e mensagem não entraram nas filas (a pessoa cadastrada está no banco?): {queued}")
            status_sql = (
                "SELECT json_build_object("
                "'task', (SELECT json_build_object('state', state, 'attempts', attempts, 'last_error', last_error, "
                "'lease_owner', lease_owner, 'lease_until', lease_until, 'finished', finished_at IS NOT NULL) "
                f"FROM tasks WHERE id = {task_id}), "
                "'message', (SELECT json_build_object('state', state, 'attempts', attempts, 'channel_ref', "
                "channel_ref, 'last_error', last_error, 'lease_owner', lease_owner, 'lease_until', lease_until, "
                f"'sent', sent_at IS NOT NULL) FROM outbox_messages WHERE id = {message_id}));")
            expected_worker = [
                {"logger": "cq_server.worker", "event": "task.processed", "id": task_id, "state": "failed"},
                {"logger": "cq_server.worker", "event": "outbox.dispatched", "id": message_id, "state": "sent"},
            ]
            deadline = time.monotonic() + WORKER_DEADLINE_SECONDS
            while True:
                status = json.loads(stack.psql(status_sql))
                worker_records, _ = _json_records(stack.logs("worker"))
                processed = (status["task"]["state"] == "failed" and status["message"]["state"] == "sent"
                             and all(_includes(worker_records, e) for e in expected_worker))
                if processed or time.monotonic() > deadline:
                    break
                time.sleep(1)
            ref = status["message"]["channel_ref"]
            assert isinstance(ref, str) and re.fullmatch(r"console-[0-9a-f]{32}", ref), (
                f"o worker não entregou a mensagem em {WORKER_DEADLINE_SECONDS} s: {status}")
            assert status == {
                "task": {"state": "failed", "attempts": 1, "last_error": NO_HANDLER_ERROR,
                         "lease_owner": None, "lease_until": None, "finished": True},
                "message": {"state": "sent", "attempts": 1, "channel_ref": ref, "last_error": None,
                            "lease_owner": None, "lease_until": None, "sent": True},
            }

            resp = http.get("/v1/events", params={"types": "task.failed"}, headers=admin)
            assert resp.status_code == 200, resp.text[:300]
            assert_matches("events.list", 200, resp.json())
            assert [(e["type"], e["actor_token_id"], e["data"]) for e in resp.json()["items"]] == [
                ("task.failed", None, {"task_id": task_id, "task_type": "sistema.sem_tratador", "attempts": 1,
                                       "error": NO_HANDLER_ERROR}),
            ]
            responses.append(("events.list 200 (task.failed)", resp))

            # A entrega vai para o arquivo `CQ_CONSOLE_OUTPUT` do container (uma linha JSON, pelo contrato).
            console = stack.run(
                "exec", "-T", "worker", "sh", "-c",
                'test -n "$CQ_CONSOLE_OUTPUT" && test "$CQ_CONSOLE_OUTPUT" != - && cat -- "$CQ_CONSOLE_OUTPUT"',
                timeout=120).stdout
            assert [json.loads(line) for line in console.splitlines()] == [
                {"channel_ref": ref, "message_id": message_id, "recipient": person["pseudo_id"], "text": text}]

            for label, r in responses:
                assert find_canary(_surface(r)) == [], f"identidade canário em {label}"
                assert find_secrets(_surface(r), all_secrets(admin_token, db_password, master_key)) == [], (
                    f"segredo em {label}")

            # Logs dos três serviços (a api registra cada requisição; esperamos o último 409 aparecer).
            expected_requests = [
                {"event": "request", "logger": "cq_server.api", "method": "GET", "route": "/v1/people/{ref}",
                 "status": 404},
                {"event": "request", "logger": "cq_server.api", "method": "GET", "route": None, "status": 404},
                {"event": "request", "logger": "cq_server.api", "method": "POST", "route": "/v1/identity/reveal",
                 "status": 200},
                {"event": "request", "logger": "cq_server.api", "method": "POST", "route": "/v1/people",
                 "status": 409},
            ]
            deadline = time.monotonic() + LOG_DEADLINE_SECONDS
            while True:
                api_logs = stack.logs("api")
                api_records, api_bad = _json_records(api_logs)
                if all(_includes(api_records, e) for e in expected_requests) or time.monotonic() > deadline:
                    break
                time.sleep(0.5)
            worker_logs = stack.logs("worker")
            postgres_logs = stack.logs("postgres")
            worker_records, worker_bad = _json_records(worker_logs)

            missing = [e for e in expected_requests if not _includes(api_records, e)]
            assert missing == [], f"registros de requisição ausentes no log da api: {missing}"
            assert api_bad == [], f"linhas da api fora de JSON: {[stack.scrub(b) for b in api_bad[:5]]}"
            assert worker_bad == [], f"linhas do worker fora de JSON: {[stack.scrub(b) for b in worker_bad[:5]]}"
            # um registro por item processado, e nenhum `exception` numa pilha saudável.
            assert sorted(((r["event"], r.get("id"), r.get("state")) for r in worker_records
                           if r["event"] in ("task.processed", "outbox.dispatched")), key=repr) == [
                ("outbox.dispatched", message_id, "sent"), ("task.processed", task_id, "failed")]
            assert all(_includes(worker_records, e) for e in expected_worker), worker_records
            exceptions = [r for r in worker_records if r["event"] == "exception"]
            assert exceptions == [], f"exceções no log do worker: {exceptions[:5]}"
            # Bots: com a chave-mestra e sem bots, a reconciliação fica ociosa.
            idle = [r["event"] for r in worker_records if r["event"] in IDLE_BOT_EVENTS]
            assert idle == [], f"o worker com a chave-mestra e sem bots não ficou ocioso: {idle}"
            # Vínculo do aluno: sem servidor de e-mail, um único aviso ao subir, antes do
            # `worker.started`, e nenhum envio (nenhum registro `invite.email`).
            disabled = [r for r in worker_records if r["event"] == "worker.mail_disabled"]
            assert len(disabled) == 1, f"o worker sem e-mail não registrou um único aviso: {len(disabled)}"
            assert MAIL_DISABLED.items() <= disabled[0].items(), disabled[0]
            events = [r["event"] for r in worker_records]
            assert events.count("worker.started") == 1, events
            assert events.index("worker.mail_disabled") < events.index("worker.started"), events
            assert [e for e in events if e.startswith("invite.email")] == [], events
            # Sem o nome canário da mensagem entregue: a entrega não foi para o stdout dos logs técnicos.
            for service, logs_text in (("api", api_logs), ("worker", worker_logs), ("postgres", postgres_logs)):
                assert find_canary(logs_text) == [], f"identidade canário nos logs de {service}"
                assert find_secrets(logs_text, all_secrets(admin_token, db_password, master_key)) == [], (
                    f"segredo (ou resumo) nos logs de {service}")
            lowered = postgres_logs.lower()
            assert "duplicate key" not in lowered and "unique constraint" not in lowered, (
                "o conflito de cadastro gerou erro no banco")

            # O worker está no ar e não reiniciou.
            for service in ("worker", "api"):
                state = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}}", stack.container(service)],
                    capture_output=True, text=True, timeout=60)
                assert state.stdout.split() == ["running", "0"], f"{service}: {state.stdout.strip()}"
            # Bots (as variáveis do Compose): a api e o worker receberam a chave gerada no teste,
            # conferida pelo resumo SHA-256, sem imprimir a chave em lugar nenhum.
            key_digest = hashlib.sha256(master_key.encode("ascii")).hexdigest()
            for service in ("api", "worker"):
                seen = stack.run(
                    "exec", "-T", service, "python", "-c",
                    "import hashlib, os; "
                    "print(hashlib.sha256(os.environ.get('CQ_MASTER_KEY', '').encode('ascii')).hexdigest())",
                    timeout=120).stdout.strip()
                assert seen == key_digest, f"CQ_MASTER_KEY não chegou ao serviço {service}"
            # Vínculo do aluno: o worker recebe as 7 `CQ_SMTP_*` vazias (e-mail desligado) e a
            # api nenhuma; só o nome e se o valor é vazio saem do container (nunca o valor).
            smtp_env = {}
            for service in ("api", "worker"):
                smtp_env[service] = json.loads(stack.run(
                    "exec", "-T", service, "python", "-c",
                    "import json, os; "
                    "print(json.dumps(sorted([k, v == ''] for k, v in os.environ.items() "
                    "if k.startswith('CQ_SMTP_'))))",
                    timeout=120).stdout.strip())
            assert smtp_env == {"api": [], "worker": [[name, True] for name in SMTP_VARS]}, smtp_env

            # Persistência: depois de `down` (sem -v) e `up`, a pessoa e a revogação continuam.
            stack.run("down")
            restarted = time.monotonic()
            stack.run("up", "-d")
            health = _wait_health(http, restarted)
            assert health.json() == expected_health
            resp = http.get(f"/v1/people/{person['alias']}", headers=admin)
            assert resp.status_code == 200 and resp.json() == person
            resp = http.get(f"/v1/people/{person['pseudo_id']}", headers=admin)
            assert resp.status_code == 200 and resp.json() == person
            assert_error(http.get("/v1/people", headers=auth(created["secret"])), 401, "unauthenticated")
            # o oferecimento também persiste.
            resp = http.get(f"/v1/offerings/{SYSTEM_OFFERING}", headers=admin)
            assert resp.status_code == 200 and resp.json() == offering

        stack.run("down", "-v", "--remove-orphans")
        volumes = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
                                 capture_output=True, text=True, timeout=60)
        assert volumes.stdout.split() == [], "o `down -v` não removeu os volumes do projeto de teste"
    finally:
        stack.run("down", "-v", "--remove-orphans", check=False)
