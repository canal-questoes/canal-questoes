"""Teste de sistema: backup cifrado e restauração entre dois projetos Compose descartáveis.



Docker real **só** nos projetos `cq-systest-bkp-src` e `cq-systest-bkp-dst` (porta livre, segredos gerados e
ocultos, a sobreposição `tests/system/compose.backup-test.yaml` que publica o Postgres no loopback,
prazo explícito por etapa e `down -v` só desses dois no `finally`). Nunca roda `docker compose config` (imprime
os segredos interpolados) e nunca sobe o `worker` (com um bot registrado, ele chamaria o Telegram).

Fluxo:
1. sobe `postgres` e `api` na origem (a API aplica as migrações) e semeia: pelas rotas, um oferecimento e a
   lista com duas pessoas canário; por DSN (como as sementes de integração, com os ajudantes de SQL de
   `tests/helpers/purge.py`), um bot com token fictício cifrado pela chave-mestra do teste (`secretbox`), as
   matrículas ativas, os endereços, um disparo fechado com duas respostas e uma mensagem livre; os eventos
   vêm das rotas;
2. gera a identidade do age no `tmp_path` (`age_paths()`, `keypair`) e roda o **`cq_backup.py` real**
   (`run_main` do `load_ops`, `--age-bin` do cache); confere o arquivo, o cabeçalho, a ausência de `PGDMP`
   em todo o `tmp_path` e o registro;
3. sobe **só** o `postgres` do destino e restaura com o **`ops/backup/restore.py` real**; confere o código 0 e
   a ausência de `errors ignored on restore`;
4. compara as contagens de todas as tabelas de `public`, os resumos `md5(string_agg(t::text, '|' ORDER BY <chave
   primária>))` de `people`, `identities`, `enrollments`, `events`, `submissions` e `free_messages`, e o
   `schema_migrations` (igual e em `EXPECTED_VERSION`);
5. sobe a `api` do destino com a mesma chave-mestra (`/health` 200 com o esquema em dia; `GET
   /v1/system/doctor` com `bots.master_key` "ok"; o token decifrável pelo `secretbox`) e depois com outra. Com
   a outra chave (o `doctor` tenta decifrar o token), `bots.master_key` sai "falha", uma vez,
   exatamente com a mensagem fixa de `DOCTOR_REASON_MESSAGES[("bots.master_key", "failure", "token_unreadable")]`,
   sem `offering` nem `count`, e a resposta não traz o token; o token gravado também **não** decifra pelo
   `secretbox` (`UnreadableToken` com `key_mismatch`);
6. o token, as chaves-mestras, a identidade do age, o token inicial e as senhas do banco não aparecem em
   nenhuma saída (scripts, respostas, registros dos contêineres e do Compose), varridas por `find_secrets`.

Sem o age no cache: falha com a mensagem de ambiente. O item 5 também depende do
`doctor` do destino.

**[I]** Escolhas minhas: a `api` da origem e a do destino usam o mesmo token inicial (ele não fica no banco);
cada projeto tem a sua senha do banco (o dump não leva papéis); a chave primária de cada tabela comparada vem
do catálogo (`pg_index`), não de uma lista no teste.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import psycopg
import pytest
import yaml
from psycopg import sql as pgsql

from cq_server.doctor import DOCTOR_REASON_MESSAGES
from cq_server.migrate import EXPECTED_VERSION
from cq_server.secretbox import TokenCipher, UnreadableToken
from tests.helpers.age_bin import age_paths, keypair
from tests.helpers.api import auth, create_offering
from tests.helpers.canary import PURGE_A, PURGE_B, find_canary, find_secrets
from tests.helpers.engine import preset
from tests.helpers.ops import REPO, Result, clean_env, load_ops, run_main
from tests.helpers.process_fake import AGE_HEADER
from tests.helpers.purge import (_activate_student, _answered, _dispatch, _endpoint, _free_message, _part,
                                 _person_by_ra, _question, _update)

pytestmark = pytest.mark.system

COMPOSE = REPO / "compose.yaml"
OVERLAY = REPO / "tests" / "system" / "compose.backup-test.yaml"
SOURCE = "cq-systest-bkp-src"
DESTINATION = "cq-systest-bkp-dst"

# Prazos por etapa (segundos).
BUILD_TIMEOUT = 1800
PULL_TIMEOUT = 900
UP_TIMEOUT = 300
COMMAND_TIMEOUT = 180
HEALTH_DEADLINE_SECONDS = 180
DB_DEADLINE_SECONDS = 120

# as tabelas comparadas pelo resumo (as de identidade, eventos e texto do aluno).
DIGEST_TABLES = ("people", "identities", "enrollments", "events", "submissions", "free_messages")
OFFERING_CODE = "bkp-sistema-origem"
# `bots.master_key` com outra chave-mestra, exatamente (o `doctor` tenta decifrar o token).
MASTER_KEY_FAILURE = {"id": "bots.master_key", "result": "failure",
                      "message": DOCTOR_REASON_MESSAGES[("bots.master_key", "failure", "token_unreadable")],
                      "offering": None, "count": None}


# --------------------------------------------------------------------------------------------------
# Compose dos projetos descartáveis
# --------------------------------------------------------------------------------------------------


class _Project:
    """Chamadas ao `docker compose` de um projeto descartável, com a saída guardada e os segredos ocultos."""

    def __init__(self, name: str, env: dict[str, str], env_file: pathlib.Path, hidden: list[str],
                 outputs: list[str]) -> None:
        assert name in (SOURCE, DESTINATION), "o teste só toca os seus dois projetos"
        self.name = name
        self.env = env
        self.env_file = env_file
        self.hidden = hidden
        self.outputs = outputs

    def scrub(self, text: str) -> str:
        for value in self.hidden:
            text = text.replace(value, "[segredo]")
        return text

    def run(self, *args: str, timeout: float = COMMAND_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess:
        assert "config" not in args, "o teste nunca roda `docker compose config` (imprime segredos)"
        cmd = ["docker", "compose", "--env-file", str(self.env_file), "-f", str(COMPOSE), "-f", str(OVERLAY),
               "-p", self.name, *args]
        result = subprocess.run(cmd, cwd=REPO, env=self.env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        self.outputs.append(result.stdout + "\n" + result.stderr)
        if check:
            assert result.returncode == 0, (
                f"`docker compose -p {self.name} {' '.join(args)}` falhou ({result.returncode}):\n"
                f"{self.scrub(result.stderr[-3000:])}")
        return result

    def container(self, service: str) -> str:
        ids = self.run("ps", "-q", service).stdout.split()
        assert len(ids) == 1, f"esperava um contêiner de {service} em {self.name}, achei {len(ids)}"
        return ids[0]

    def logs(self, service: str) -> str:
        result = subprocess.run(["docker", "logs", self.container(service)], cwd=REPO, env=self.env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
        assert result.returncode == 0, f"`docker logs` de {service} falhou:\n{self.scrub(result.stderr[-3000:])}"
        text = result.stdout + "\n" + result.stderr
        self.outputs.append(text)
        return text

    @property
    def dsn(self) -> str:
        return (f"host=127.0.0.1 port={self.env['CQ_TEST_PG_PORT']} dbname=cq user=cq "
                f"password={self.env['CQ_DB_PASSWORD']} connect_timeout=5")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.env['CQ_API_PORT']}"


def _require_docker() -> None:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, stdin=subprocess.DEVNULL)
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


def _project_env(*, db_password: str, admin_token: str, master_key: str, api_port: int, pg_port: int) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(("COMPOSE_", "CQ_"))}
    env.update({"CQ_DB_PASSWORD": db_password, "CQ_ADMIN_TOKEN": admin_token, "CQ_MASTER_KEY": master_key,
                "CQ_API_PORT": str(api_port), "CQ_TEST_PG_PORT": str(pg_port), "MSYS_NO_PATHCONV": "1"})
    return env


def _wait_health(http: Any) -> Any:
    deadline = time.monotonic() + HEALTH_DEADLINE_SECONDS
    last = "sem resposta"
    while time.monotonic() < deadline:
        try:
            resp = http.get("/health")
        except Exception as exc:  # conexão recusada enquanto a api sobe
            last = type(exc).__name__
        else:
            last = f"{resp.status_code}"
            if resp.status_code == 200:
                return resp
        time.sleep(1)
    pytest.fail(f"/health não ficou ok em {HEALTH_DEADLINE_SECONDS} s (último: {last})")


def _connect(dsn: str) -> psycopg.Connection:
    deadline = time.monotonic() + DB_DEADLINE_SECONDS
    last = "sem resposta"
    while time.monotonic() < deadline:
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError as exc:
            last = type(exc).__name__
            time.sleep(1)
    pytest.fail(f"o Postgres publicado no loopback não respondeu em {DB_DEADLINE_SECONDS} s (último: {last})")


# --------------------------------------------------------------------------------------------------
# Semente e comparação
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    token: str
    bot_uuid: uuid.UUID
    telegram_bot_id: int


def _seed(http: Any, dsn: str, admin_token: str, master_key: str) -> Seed:
    """A semente da origem (ver o docstring do módulo, item 1)."""
    create_offering(http, admin_token, strict_schema=False, code=OFFERING_CODE, course="Algoritmos e Programação",
                    term="2031s1", timezone="America/Sao_Paulo")
    resp = http.post(f"/v1/offerings/{OFFERING_CODE}/students", json={"rows": [PURGE_A.row(), PURGE_B.row()]},
                     headers=auth(admin_token))
    assert resp.status_code == 200, f"students.import falhou na semente: {resp.status_code}"

    now = datetime.now(UTC).replace(microsecond=0)
    early = now - timedelta(hours=3)
    opens, closes = now - timedelta(hours=2), now - timedelta(hours=1)
    telegram_bot_id = 7_100_000_000 + secrets.randbelow(10_000_000)
    token = f"{telegram_bot_id}:{secrets.token_urlsafe(26)}"
    bot_uuid = uuid.uuid4()
    blob = TokenCipher(master_key).encrypt(token, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)
    with _connect(dsn) as conn:
        offering_row = int(conn.execute("SELECT id FROM offerings WHERE code = %s", (OFFERING_CODE,)).fetchone()[0])
        bot_row = int(conn.execute(
            "INSERT INTO bots (bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, token_version, "
            "state, profile_state, registered_at) VALUES (%s, %s, %s, %s, %s, 1, 'registered', 'configured', %s) "
            "RETURNING id",
            (bot_uuid, offering_row, telegram_bot_id, "bkp_sistema_bot", blob, early)).fetchone()[0])
        a_row = _person_by_ra(conn, PURGE_A.ra)[0]
        b_row = _person_by_ra(conn, PURGE_B.ra)[0]
        a_enrollment = _activate_student(conn, a_row, offering_row, early)
        b_enrollment = _activate_student(conn, b_row, offering_row, early)
        a_chat, b_chat = PURGE_A.accounts[0].user_id, PURGE_B.accounts[0].user_id
        a_endpoint = _endpoint(conn, a_row, bot_row, a_chat, "active", early)
        b_endpoint = _endpoint(conn, b_row, bot_row, b_chat, "active", early)
        question = _question(conn, "text", early)
        dispatch_row, _ = _dispatch(conn, offering_row, question, state="closed", opens=opens, closes=closes)
        for enrollment, person, endpoint, chat, answer, score in (
                (a_enrollment, a_row, a_endpoint, a_chat, "azul", 1.0),
                (b_enrollment, b_row, b_endpoint, b_chat, f"a mesma de {PURGE_A.name}", 0.0)):
            _answered(conn, dispatch_row=dispatch_row, enrollment_row=enrollment, person_row=person, bot_row=bot_row,
                      endpoint_row=endpoint, chat_id=chat, question=preset("text"), raw={"resposta": answer},
                      text=answer, score=score, part_derived=[_part(0)], opens=opens,
                      answered=opens + timedelta(minutes=30), revealed=closes + timedelta(minutes=1))
        text = "Quando sai a próxima questão?"
        update = _update(conn, bot_row, chat_id=b_chat, text=text, outcome="free_message",
                         at=early + timedelta(minutes=5))
        _free_message(conn, offering_row, b_endpoint, update, text, early + timedelta(minutes=5), replied=False)
    return Seed(token=token, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)


def _summary(dsn: str) -> dict[str, Any]:
    """Contagens de todas as tabelas de `public`, resumos das tabelas de `DIGEST_TABLES` e o `schema_migrations`."""
    with _connect(dsn) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
        counts = {table: int(conn.execute(pgsql.SQL("SELECT count(*) FROM {}").format(
            pgsql.Identifier(table))).fetchone()[0]) for table in tables}
        digests = {}
        for table in DIGEST_TABLES:
            keys = [row[0] for row in conn.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)", (f"public.{table}",)).fetchall()]
            assert keys, f"{table} sem chave primária"
            order = pgsql.SQL(", ").join(pgsql.SQL("t.{}").format(pgsql.Identifier(key)) for key in keys)
            query = pgsql.SQL("SELECT md5(string_agg(t::text, '|' ORDER BY {})) FROM {} AS t").format(
                order, pgsql.Identifier(table))
            digests[table] = conn.execute(query).fetchone()[0]
        migrations = [tuple(row) for row in conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()]
    return {"tables": tables, "counts": counts, "digests": digests, "migrations": migrations}


def _bot_blob(dsn: str, seed: Seed) -> bytes:
    with _connect(dsn) as conn:
        row = conn.execute("SELECT token_ciphertext FROM bots WHERE bot_uuid = %s", (seed.bot_uuid,)).fetchone()
    assert row is not None, "o bot semeado não está no destino"
    return bytes(row[0])


def _master_key_check(http: Any, admin_token: str) -> tuple[dict, str]:
    resp = http.get("/v1/system/doctor", headers=auth(admin_token))
    assert resp.status_code == 200, f"GET /v1/system/doctor no destino: {resp.status_code}"
    matches = [check for check in resp.json()["checks"] if check["id"] == "bots.master_key"]
    assert len(matches) == 1, "bots.master_key deve aparecer uma vez no doctor do destino"
    return matches[0], resp.text


def _secret_key(identity: pathlib.Path) -> str:
    for line in identity.read_text(encoding="ascii").splitlines():
        if line.startswith("AGE-SECRET-KEY-1"):
            return line.strip()
    raise AssertionError("a identidade gerada não tem a linha AGE-SECRET-KEY-1")


def _assert_quiet(result: Result, *, ok_line: str) -> None:
    assert (result.code, result.stdout, result.stderr) == (0, ok_line + "\n", "")
    assert find_canary(result.stdout + result.stderr) == []


# --------------------------------------------------------------------------------------------------
# O teste
# --------------------------------------------------------------------------------------------------


def test_backup_restore_round_trip_between_disposable_projects(tmp_path: pathlib.Path) -> None:
    """Ver o docstring do módulo."""
    assert COMPOSE.is_file() and OVERLAY.is_file()
    _require_docker()
    age, _ = age_paths()
    keys_dir = tmp_path / "chave"
    keys_dir.mkdir()
    identity, recipient = keypair(keys_dir)
    age_secret = _secret_key(identity)

    admin_token = "cq_" + secrets.token_urlsafe(32)
    master_key, other_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    assert len(master_key) == len(other_key) == 43 and master_key != other_key
    src_password, dst_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    hidden = [admin_token, master_key, other_key, src_password, dst_password, age_secret]
    outputs: list[str] = []
    env_file = tmp_path / "vazio.env"  # não lê o `.env` local de quem roda o teste
    env_file.write_text("", encoding="utf-8")
    src = _Project(SOURCE, _project_env(db_password=src_password, admin_token=admin_token, master_key=master_key,
                                        api_port=_free_port(), pg_port=_free_port()), env_file, hidden, outputs)
    dst = _Project(DESTINATION, _project_env(db_password=dst_password, admin_token=admin_token,
                                             master_key=master_key, api_port=_free_port(), pg_port=_free_port()),
                   env_file, hidden, outputs)
    responses: list[str] = []
    script_outputs: list[str] = []

    for project in (src, dst):
        project.run("down", "-v", "--remove-orphans", timeout=UP_TIMEOUT)
    try:
        # 1. Origem: postgres e api; a semente.
        src.run("build", "api", timeout=BUILD_TIMEOUT)
        dst.run("build", "api", timeout=BUILD_TIMEOUT)
        pg_image = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["postgres"]["image"]
        if subprocess.run(["docker", "image", "inspect", pg_image], capture_output=True, timeout=60,
                          stdin=subprocess.DEVNULL).returncode != 0:
            subprocess.run(["docker", "pull", pg_image], capture_output=True, check=True, timeout=PULL_TIMEOUT,
                           stdin=subprocess.DEVNULL)
        src.run("up", "-d", "--wait", "postgres", "api", timeout=UP_TIMEOUT)
        with httpx2.Client(base_url=src.base_url, timeout=10, trust_env=False) as http:
            health = _wait_health(http)
            assert health.json()["schema_version"] == EXPECTED_VERSION
            seed = _seed(http, src.dsn, admin_token, master_key)
        hidden.append(seed.token)
        source_summary = _summary(src.dsn)
        for table, minimum in (("people", 2), ("identities", 2), ("enrollments", 2), ("events", 1),
                               ("submissions", 2), ("free_messages", 1), ("bots", 1)):
            assert source_summary["counts"][table] >= minimum, f"a semente não gravou {table}"

        # 2. O backup real da origem.
        vm = tmp_path / "vm"
        dumps, state = vm / "dumps", vm / "state"
        dumps.mkdir(parents=True)
        state.mkdir(parents=True)
        recipients = vm / "recipients.txt"
        recipients.write_text(f"# destinatario do teste de sistema\n{recipient}\n", encoding="utf-8")
        cq_backup = load_ops("ops/backup/cq_backup.py")
        backup = run_main(cq_backup, ["run", "--project", SOURCE, "--recipients", str(recipients), "--dumps-dir",
                                      str(dumps), "--state-dir", str(state), "--age-bin", str(age)],
                          env=clean_env())
        script_outputs.append(backup.stdout + backup.stderr)
        _assert_quiet(backup, ok_line="Backup concluído.")
        finals = sorted(path.name for path in dumps.iterdir() if path.name.endswith(".dump.age"))
        assert len(finals) == 1 and sorted(path.name for path in dumps.iterdir()) == sorted([*finals, "SHA256SUMS"])
        final = dumps / finals[0]
        assert final.read_bytes().startswith(AGE_HEADER)
        record = json.loads((state / "last-backup.json").read_text(encoding="utf-8"))
        assert record["file"] == final.name and record["size_bytes"] == final.stat().st_size
        assert cq_backup.read_record(state / "last-backup.json") is not None
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert b"PGDMP" not in path.read_bytes(), f"dump em claro em disco: {path.name}"

        # 3. Destino: só o postgres; a restauração real.
        dst.run("up", "-d", "--wait", "postgres", timeout=UP_TIMEOUT)
        assert dst.run("ps", "-q").stdout.split() == [dst.container("postgres")]
        restore_mod = load_ops("ops/backup/restore.py")
        restored = run_main(restore_mod, ["--dump", str(final), "--identity", str(identity), "--project",
                                          DESTINATION, "--age-bin", str(age)], env=clean_env())
        script_outputs.append(restored.stdout + restored.stderr)
        _assert_quiet(restored, ok_line="Restauração concluída.")
        assert restore_mod.IGNORED_ERRORS_MARKER not in restored.stderr

        # 4. Comparação.
        target_summary = _summary(dst.dsn)
        assert target_summary["tables"] == source_summary["tables"]
        assert target_summary["counts"] == source_summary["counts"]
        assert target_summary["digests"] == source_summary["digests"]
        assert target_summary["migrations"] == source_summary["migrations"]
        assert target_summary["migrations"][-1][0] == EXPECTED_VERSION

        # 5a. A api do destino com a mesma chave-mestra.
        dst.run("up", "-d", "--wait", "api", timeout=UP_TIMEOUT)
        with httpx2.Client(base_url=dst.base_url, timeout=10, trust_env=False) as http:
            health = _wait_health(http)
            responses.append(health.text)
            assert (health.json()["schema_version"], health.json()["expected_schema_version"]) == (
                EXPECTED_VERSION, EXPECTED_VERSION)
            check, text = _master_key_check(http, admin_token)
            responses.append(text)
            assert check["result"] == "ok"
        blob = _bot_blob(dst.dsn, seed)
        assert TokenCipher(master_key).decrypt(blob, bot_uuid=seed.bot_uuid,
                                               telegram_bot_id=seed.telegram_bot_id) == seed.token
        dst.logs("api")

        # 5b. A api do destino com outra chave-mestra: `bots.master_key` "falha", uma vez
        # (`_master_key_check`), com a mensagem fixa nova, e a resposta sem o token; o token gravado também não
        # decifra pelo `secretbox`.
        dst.env["CQ_MASTER_KEY"] = other_key
        dst.run("up", "-d", "--wait", "api", timeout=UP_TIMEOUT)
        with httpx2.Client(base_url=dst.base_url, timeout=10, trust_env=False) as http:
            responses.append(_wait_health(http).text)
            check, text = _master_key_check(http, admin_token)
            responses.append(text)
            assert check == MASTER_KEY_FAILURE
            assert find_secrets(text, hidden) == [], "segredo na resposta do doctor com a outra chave-mestra"
        with pytest.raises(UnreadableToken) as caught:
            TokenCipher(other_key).decrypt(_bot_blob(dst.dsn, seed), bot_uuid=seed.bot_uuid,
                                           telegram_bot_id=seed.telegram_bot_id)
        assert caught.value.reason == "key_mismatch"
        dst.logs("api")
        src.logs("api")

        # 6. Nenhum segredo em saída nenhuma.
        for label, text in (("scripts", "\n".join(script_outputs)), ("respostas", "\n".join(responses)),
                            ("compose e registros", "\n".join(outputs))):
            assert find_secrets(text, hidden) == [], f"segredo nas saídas: {label}"
    finally:
        for project in (src, dst):
            project.run("down", "-v", "--remove-orphans", timeout=UP_TIMEOUT, check=False)
