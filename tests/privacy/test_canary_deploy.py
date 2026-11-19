"""Canário da implantação e do piloto.

Arquivo à parte do canário congelado (`tests/privacy/test_canary.py`); a semente
(`tests/helpers/calls.py`, `tests/helpers/purge.py`) e os detectores (`tests/helpers/canary.py`,
`tests/helpers/secrets.py`, o `scan` de `tests/helpers/mcp_client.py`) são reusados.

Semente (no banco do teste, pelas rotas e pelos ajudantes das fases anteriores; `_world`): `seed_state` (os alvos de toda
operação do catálogo), as pessoas canário da API base (`CANARY_PEOPLE`), o aluno canário `STUDENT_A` importado e a semente
do expurgo (`seed_purge_scenario`: o oferecimento `O` arquivado, as pessoas canário dele e o `O2` com uma resposta de
texto que cita a pessoa apagada); o perfil `local` (o diretório da pilha com o `.env` de teste), o `remoto` e o
`piloto` (o do túnel), todos com o token inicial.

Partes (cada uma um teste):
- `doctor`: 7 casos do servidor (o registro do backup num arquivo do `tmp_path`, ilegível com valores plantados, e
  com 2, 30, 60 e 1 h; a linha de `mail_probe` em cada resultado; a retenção vencida depois de 31 dias), cada um
  emparelhado com um caso do host (`FakeHostProbe` com volume certo, anônimo, `bind`, destino errado, serviço
  parado; perfil remoto; sem `host_probe`), pela API, pelo CLI em texto e em JSON e pelo `run_doctor` do MCP nas
  duas eras. Controle contra o vazio: cada verificação nova com **todos** os resultados possíveis
  (`POSSIBLE_RESULTS`, sem total). Mais um caso, o da falha nova de `bots.master_key` (a chave-mestra posta à prova;
  motivo `token_unreadable`, o único caminho do `doctor` que decifra tokens): um bot registrado pela rota (o token
  cifrado com a chave certa) e a API com `DoctorSettings(token_cipher=TokenCipher(OTHER_MASTER_KEY))`, pelas mesmas
  superfícies, com o token e as duas chaves-mestras entre os segredos.
- gerador de métricas sobre a semente (JSON, Markdown com `--out`, e com erro): nenhum pseudônimo nem apelido de
  pessoa, nenhuma identidade, nenhum segredo.
- scripts de `ops/` e `scripts/fetch_age.py`, cada um com ao menos um sucesso e uma falha, com os dublês
  (`FakeSpawn` e o repositório git de fixture) e valores plantados nas entradas (identidade do age,
  chave-mestra, token, senha de app, usuário do SMTP, nomes e e-mails canário, o caminho do `.env`).
Em toda saída (stdout, stderr, corpo, texto, estruturado, exceção, `repr`): zero identidade e zero segredo
(`mcp_client.scan` com os prefixos das fases anteriores e `find_secrets`) e nenhum dos literais plantados (caminhos com nome
canário, o caminho do `.env`, a identidade do age, o usuário do SMTP). Controles positivos: cada valor plantado é
achado pelo mesmo detector na entrada.

**[I]** Escolhas deste teste:
- os scripts cujo `main` só fala com recurso real (Docker, `sftp`, o repositório real) são exercitados pela função
  injetável que cada script expõe (`run`, `restore`, `pull`, `init`, `build_bundle`, `export`, `check`, `fetch`, o
  `run` do CI e da validação), com o stdout e o stderr capturados (os scripts resolvem `sys.stdout`/`sys.stderr` na
  chamada), e o `main` só com argumentos inválidos (`run_main`), que não toca nada fora do processo;
- o `pull` com o `sftp` inalcançável pode levantar ou devolver: o texto da exceção e o `repr` também são varridos;
- o `check_question_refs.check` roda o `git` real sobre um repositório de fixture local: o `runner` falso troca o
  endereço `https://` do manifesto pelo caminho dele (nenhuma rede);
- o repositório de fixture do pacote e da exportação traz as suas próprias listas em `ops/export/`;
- o `prompt` falso do `init_vm_env` responde pelo texto da pergunta: com "chave", a chave-mestra; com "senha", a
  senha de app; senão, o usuário do Gmail;
- a senha de app falsa e o cabeçalho da chave SSH falsa são montados em tempo de execução (`_join`, como em
  `tests/unit/test_export_public.py`), para o próprio arquivo não ter achado na varredura da exportação e do pacote
  (os valores plantados não mudam).

No esqueleto, o teste falha no `doctor` sem as globais novas e com as fixas (a primeira asserção do primeiro caso);
os scripts e o gerador lançam `NotImplementedError`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import secrets as py_secrets
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from cq_server.doctor import DOCTOR_REASON_MESSAGES, DoctorSettings
from cq_server.secretbox import TokenCipher
from tests.helpers import mcp_client
from tests.helpers.api import (ADMIN_TOKEN, create_offering, import_students, offering_ref, register_bot,
                               register_person, system_doctor)
from tests.helpers.calls import TEST_REPO_SETTINGS, seed_state
from tests.helpers.canary import CANARY_PEOPLE, OLIMPIO, STUDENT_A, ZEFIRO
from tests.helpers.cli_runner import (ENV_SENTINEL, ENV_SENTINEL_TOKEN, UNREACHABLE_API_URL, make_stack_dir,
                                  write_profile)
from tests.helpers.git_fixture import git_env, make_repo
from tests.helpers.host_probe_fake import (FakeHostProbe, bind, invoke_doctor, usage, volume_anonymous, volume_ok,
                                           wrong_path)
from tests.helpers.ops import REPO, clean_env, load_ops, run_main
from tests.helpers.process_fake import (DEFAULT_DUMP, IGNORED_ERRORS_STDERR, FakeSpawn, fake_age_ciphertext,
                                        sha256_hex)
from tests.helpers.secrets import all_secrets, bot_secrets, MASTER_KEY, OTHER_MASTER_KEY
from tests.helpers.stack_fake import TEST_API_URL, FakeStack

MODES = mcp_client.MODES
NEW_SERVER_IDS = ("backup.age", "smtp.server", "retention.overdue")
HOST_IDS = ("postgres.volume", "disk.space")

# os resultados possíveis de cada verificação nova (o controle contra o vazio).
POSSIBLE_RESULTS: dict[str, frozenset[str]] = {
    "backup.age": frozenset({"ok", "warning", "failure"}),
    "smtp.server": frozenset({"ok", "warning", "failure"}),
    "retention.overdue": frozenset({"ok", "warning"}),
    "postgres.volume": frozenset({"ok", "failure", "not_verified", "not_applicable"}),
    "disk.space": frozenset({"ok", "warning", "failure", "not_verified", "not_applicable"}),
}

REMOTE_URL = "https://cq.example.org"
TUNNEL_URL = "http://127.0.0.1:18000"  # o endereço local do túnel
RETENTION_DAYS = 30  # o padrão de `DoctorSettings.retention_days`
KEY_OFFERING = "implantacao-chave-trocada"  # o oferecimento do caso da falha de `bots.master_key`
MASTER_KEY_FAILURE = {"id": "bots.master_key", "result": "failure",
                      "message": DOCTOR_REASON_MESSAGES[("bots.master_key", "failure", "token_unreadable")],
                      "offering": None, "count": None}

def _join(*parts: str) -> str:
    return "".join(parts)


# Valores plantados nas entradas (cada um com o controle positivo do detector). A senha de app e o cabeçalho da
# chave SSH vêm em pedaços, para o arquivo não ter achado na varredura.
FAKE_AGE_IDENTITY = "AGE-SECRET-KEY-1" + ("QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2)[:58]
APP_PASSWORD = _join("wxyz abcd ", "efgh ijkl")
SMTP_USER = OLIMPIO.email
RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"  # o exemplo público do README do age
PLANTED_TEXT = f"{ZEFIRO.name} {OLIMPIO.email} {ADMIN_TOKEN}"  # nome e e-mail canário e o token inicial
CONTAINER = "3f2a1b4c5d6e"
DUMP_NAME = "cq-20261029T033005Z.dump.age"
REMOTE_DIR = "/var/backups/cq/dumps"


def _secrets(*extra: str) -> list[str]:
    """Os segredos da varredura: os tokens das fases anteriores, as chaves-mestras, os sentinelas do `.env` de teste e os
    plantados da implantação (a identidade do age, a senha de app)."""
    return [*all_secrets(), *bot_secrets(), ENV_SENTINEL, ENV_SENTINEL_TOKEN, FAKE_AGE_IDENTITY, APP_PASSWORD,
            *extra]


def _forms(*paths: Path | str) -> list[str]:
    """As formas de um caminho que não podem aparecer numa saída (nativa e POSIX)."""
    out: list[str] = []
    for path in paths:
        out += [str(path), Path(path).as_posix()]
    return out


def _assert_private(texts: Sequence[str], secrets: Sequence[str], literals: Sequence[str], *, where: str) -> None:
    for text in texts:
        assert mcp_client.scan(text, secrets) == [], (where, mcp_client.scan(text, secrets), text[:300])
        assert [lit for lit in literals if lit and lit in text] == [], (where, text[:300])


# ---------------------------------------------------------------------------------------------
# Semente
# ---------------------------------------------------------------------------------------------


@dataclass
class _World:
    state: Any
    scenario: Any
    config: Path
    stack_dir: Path

    def env(self, profile: str, **extra: str) -> dict[str, str]:
        return {"CQ_PROFILE": profile, **extra}


def _world(client: Any, sql: psycopg.Connection, tmp_path: Path) -> _World:
    from tests.helpers.purge import seed_purge_scenario

    state = seed_state(client, revoke_targets=1)
    for person in CANARY_PEOPLE:
        register_person(client, ADMIN_TOKEN, **person.payload())
    import_students(client, ADMIN_TOKEN, state.import_offering["code"], [STUDENT_A.row()])
    scenario = seed_purge_scenario(client, sql)
    folder = tmp_path / "implantacao-perfil"
    stack_dir = make_stack_dir(folder)
    config = write_profile(folder, "\n".join([
        f'api_url = "{UNREACHABLE_API_URL}"', "",
        "[profiles.local]", 'kind = "local"', f'api_url = "{TEST_API_URL}"', f"stack_dir = '{stack_dir}'",
        f'token = "{ADMIN_TOKEN}"', "",
        "[profiles.remoto]", 'kind = "remote"', f'api_url = "{REMOTE_URL}"', f'token = "{ADMIN_TOKEN}"', "",
        "[profiles.piloto]", 'kind = "remote"', f'api_url = "{TUNNEL_URL}"', f'token = "{ADMIN_TOKEN}"', ""]))
    if os.name != "nt":
        os.chmod(config, 0o600)
    return _World(state=state, scenario=scenario, config=config, stack_dir=stack_dir)


# ---------------------------------------------------------------------------------------------
# `doctor`: os casos
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ServerCase:
    backup: str                      # "not_configured", "unreadable" ou "age"
    backup_hours: float | None       # a idade do registro (só com "age")
    mail: tuple[str, float] | None   # (resultado gravado em `mail_probe`, horas atrás); `None` = sem linha
    overdue: bool                    # avança o relógio além do prazo de retenção antes do caso
    expected: tuple[str, str, str]   # (backup.age, smtp.server, retention.overdue)


SERVER_CASES = (
    _ServerCase("not_configured", None, None, False, ("warning", "warning", "ok")),
    _ServerCase("unreadable", None, ("not_configured", 1), False, ("failure", "warning", "ok")),
    _ServerCase("age", 2, ("connection", 1), False, ("ok", "failure", "ok")),
    _ServerCase("age", 30, ("tls", 1), False, ("warning", "failure", "ok")),
    _ServerCase("age", 60, ("auth", 1), False, ("failure", "failure", "ok")),
    _ServerCase("age", 1, ("ok", 20), False, ("ok", "warning", "ok")),   # o teste do e-mail antigo (> 13 h)
    _ServerCase("age", 1, ("ok", 1), True, ("ok", "ok", "warning")),     # 31 dias depois: os arquivados vencidos
)


@dataclass
class _HostCase:
    profile: str                                  # "local" ou "remoto"
    probe: FakeHostProbe | None                   # `None` = sem `host_probe` no `obj`
    expected: tuple[str, str]                     # (postgres.volume, disk.space)
    env: dict[str, str] = field(default_factory=dict)


def _host_cases(tmp_path: Path) -> tuple[list[_HostCase], list[str]]:
    """Os 7 casos do host e os literais plantados neles (caminhos com nome canário)."""
    root_dir = f"/srv/{ZEFIRO.name}/docker"
    backup_dir = str(tmp_path / f"backups {OLIMPIO.name}")
    destination = f"/dados/{ZEFIRO.name}/postgresql"
    cases = [
        _HostCase("local", FakeHostProbe(volume_ok("cq"), root_dir, {root_dir: usage(50, 100),
                                                                     backup_dir: usage(60, 40)}),
                  ("ok", "ok"), {"CQ_BACKUP_DIR": backup_dir}),
        _HostCase("local", FakeHostProbe(volume_anonymous(), root_dir, {root_dir: usage(15, 100)}),
                  ("failure", "warning")),
        _HostCase("local", FakeHostProbe(bind(), root_dir, {root_dir: usage(5, 100)}), ("failure", "failure")),
        _HostCase("local", FakeHostProbe(wrong_path(destination), root_dir, {}), ("failure", "not_verified")),
        _HostCase("local", FakeHostProbe(None, None, {}), ("not_verified", "not_verified")),
        _HostCase("remoto", FakeHostProbe(volume_ok("cq"), root_dir, {root_dir: usage(50, 100)}),
                  ("not_applicable", "not_applicable")),
        _HostCase("local", None, ("not_verified", "not_verified")),
    ]
    return cases, [root_dir, backup_dir, destination]


def _write_record(folder: Path, case: _ServerCase, now: datetime) -> tuple[str | None, str | None]:
    """O registro do backup do caso; devolve (caminho, texto gravado)."""
    if case.backup == "not_configured":
        return None, None
    path = folder / "last-backup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if case.backup == "unreadable":
        # Ilegível (chave a mais, tipos e formatos errados), com valores plantados que nunca podem sair.
        text = json.dumps({"version": 1, "finished_at": ZEFIRO.name, "file": OLIMPIO.email, "size_bytes": "x",
                           "sha256": ADMIN_TOKEN, "token": FAKE_AGE_IDENTITY}, ensure_ascii=False)
    else:
        assert case.backup_hours is not None
        finished = now - timedelta(hours=case.backup_hours)
        text = json.dumps({"version": 1, "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"), "file": DUMP_NAME,
                           "size_bytes": 4321, "sha256": "ab" * 32})
    path.write_text(text, encoding="utf-8")
    return str(path), text


def _write_probe(sql: psycopg.Connection, case: _ServerCase, now: datetime) -> None:
    sql.execute("DELETE FROM mail_probe")
    if case.mail is None:
        return
    result, hours = case.mail
    sql.execute("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s)", (result, now - timedelta(hours=hours)))


def _overdue_oracle(sql: psycopg.Connection, now: datetime) -> tuple[int, str | None]:
    """A regra de retenção sobre as linhas da semente: (vencidos, código do mais antigo)."""
    rows = sql.execute(
        "SELECT code FROM offerings WHERE state = 'archived' AND purged_at IS NULL AND archived_at < %s "
        "ORDER BY archived_at, id", (now - timedelta(days=RETENTION_DAYS),)).fetchall()
    return len(rows), (rows[0][0] if rows else None)


def _by_id(checks: list[dict], check_id: str) -> dict:
    found = [c for c in checks if c["id"] == check_id]
    assert len(found) == 1, f"{check_id}: {len(found)} verificações"
    return found[0]


async def _mcp_doctor(deps: Any, mode: str) -> Any:
    async with mcp_client.in_process(deps, mode) as c:
        return await mcp_client.call(c, "run_doctor", {})


def test_deploy_doctor_sweep_every_new_check_in_every_result(client: Any, sql: psycopg.Connection, clock: Any,
                                                         settings: Any, bots: Any, bot_api: Any,
                                                         tmp_path: Path) -> None:
    """Ver o docstring do módulo: cada verificação nova em todos os seus resultados, pela
    API, pelo CLI em texto e em JSON e pelo `run_doctor` nas duas eras, sobre a semente canário; nenhuma identidade,
    nenhum segredo e nenhum valor plantado (o conteúdo do registro ilegível, os caminhos do probe e do registro, o
    caminho do `.env`) em nenhuma saída; nenhuma das 4 reservadas do CLI como "não verificado" fixa. E a falha nova de
    `bots.master_key` (outra chave-mestra no `DoctorSettings`, um bot registrado), pelas mesmas superfícies,
    sem o token nem as chaves."""
    from cq_server.api import create_app

    world = _world(client, sql, tmp_path)
    secrets = _secrets()
    host_cases, host_literals = _host_cases(tmp_path)
    assert len(host_cases) == len(SERVER_CASES)
    env_path = world.stack_dir / ".env"
    literals = [*host_literals, *_forms(env_path), FAKE_AGE_IDENTITY, SMTP_USER]

    # Controles positivos: o detector acha cada valor plantado na entrada.
    for planted in host_literals:
        assert mcp_client.scan(planted, []), f"o valor plantado não leva o canário: {planted}"
    assert mcp_client.scan(env_path.read_text(encoding="utf-8"), secrets), "o .env de teste sem o sentinela"

    observed: dict[str, set[str]] = {check_id: set() for check_id in POSSIBLE_RESULTS}
    outputs: list[tuple[str, str]] = []
    for index, (case, host) in enumerate(zip(SERVER_CASES, host_cases, strict=True)):
        if case.overdue:
            clock.advance(timedelta(days=RETENTION_DAYS + 1).total_seconds())
        now = clock.now()
        record_dir = tmp_path / f"estado {ZEFIRO.name}" / f"caso-{index}"
        record_path, record_text = _write_record(record_dir, case, now)
        if record_text is not None and case.backup == "unreadable":
            assert mcp_client.scan(record_text, secrets), "o registro ilegível sem os valores plantados"
        case_literals = [*literals, *(_forms(record_path) if record_path else [])]
        _write_probe(sql, case, now)
        first_output = len(outputs)
        overdue_count, oldest = _overdue_oracle(sql, now)
        assert (overdue_count > 0) == case.overdue, "a semente não produz o vencido esperado"

        app = create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS,
                         doctor_settings=DoctorSettings(backup_status_file=record_path))
        with TestClient(app, raise_server_exceptions=False) as api:
            # --- API ---
            body = system_doctor(api)
            outputs.append((f"api {index}", json.dumps(body, ensure_ascii=False)))
            got = tuple(_by_id(body["checks"], check_id)["result"] for check_id in NEW_SERVER_IDS)
            assert got == case.expected, (index, got, case.expected)
            retention = _by_id(body["checks"], "retention.overdue")
            assert (retention["count"], retention["offering"]) == ((overdue_count, oldest) if case.overdue
                                                                   else (0, None)), (index, retention)
            for check_id in NEW_SERVER_IDS:
                observed[check_id].add(_by_id(body["checks"], check_id)["result"])

            # --- CLI, em texto e em JSON ---
            for json_mode in (False, True):
                obj: dict[str, Any] = {"http": api}
                if host.profile == "local":
                    obj["stack"] = FakeStack(["healthy"])
                if host.probe is not None:
                    obj["host_probe"] = host.probe
                result = invoke_doctor(["doctor"], obj=obj, env=world.env(host.profile, **host.env),
                                       config=world.config, json=json_mode)
                assert result.exception is None or isinstance(result.exception, SystemExit), (
                    index, json_mode, type(result.exception).__name__)
                outputs.append((f"cli {index} json={json_mode}", result.stdout + result.stderr))
                if json_mode:
                    checks = json.loads(result.stdout)["checks"]
                    for check_id in NEW_SERVER_IDS:
                        assert _by_id(checks, check_id)["result"] == _by_id(body["checks"], check_id)["result"]
                    got_host = tuple(_by_id(checks, check_id)["result"] for check_id in HOST_IDS)
                    assert got_host == host.expected, (index, got_host, host.expected)
                    assert {c["part"] for c in checks if c["id"] in HOST_IDS} == {"local"}
                    assert {c["part"] for c in checks if c["id"] in NEW_SERVER_IDS} == {"api"}
                    for check_id in HOST_IDS:
                        observed[check_id].add(_by_id(checks, check_id)["result"])
                    failures = any(c["result"] == "failure" for c in checks)
                    assert result.exit_code == (1 if failures else 0), (index, result.exit_code)
                else:
                    for check_id in (*NEW_SERVER_IDS, *HOST_IDS):
                        lines = [line for line in result.stdout.splitlines()
                                 if re.match(rf"^\[[^\]]+\] {re.escape(check_id)}[ :]", line)]
                        assert len(lines) == 1, (index, check_id, result.stdout)
            if host.profile == "remoto" and host.probe is not None:
                assert host.probe.calls == [], "o perfil remoto chamou o probe"

            # --- `run_doctor` pelo MCP, nas duas eras ---
            for mode in MODES:
                stderr = io.StringIO()
                deps = mcp_client.make_deps(tmp_path, env={"CQ_CONFIG": str(world.config), "CQ_PROFILE": "local"},
                                            http=api, stderr=stderr)
                outcome = mcp_client.run(_mcp_doctor, deps, mode)
                assert outcome.protocol_code is None and not outcome.is_error, (index, mode, outcome.text)
                assert isinstance(outcome.structured, dict)
                assert [(c["id"], c["result"]) for c in outcome.structured["checks"]] == [
                    (c["id"], c["result"]) for c in body["checks"]], (index, mode)
                outputs.append((f"mcp {index} {mode}", json.dumps(outcome.raw, ensure_ascii=False)))
                outputs.append((f"mcp {index} {mode} stderr", stderr.getvalue()))
        for where, text in outputs[first_output:]:
            _assert_private([text], secrets, case_literals, where=where)

    # Controle contra o vazio: cada verificação nova em todos os seus resultados (conjunto calculado, sem total).
    assert {check_id: frozenset(results) for check_id, results in observed.items()} == POSSIBLE_RESULTS

    # --- a falha nova de `bots.master_key` (motivo `token_unreadable`) ---
    # Um bot registrado pela rota (o token cifrado com a chave do `bots` da fixture, `MASTER_KEY`) e a API com a cifra
    # de outra chave-mestra no `DoctorSettings` injetado: o `collect` decifra o token gravado e sai "falha". As mesmas
    # superfícies e os mesmos detectores dos casos acima (sem exclusão nova), com o token (inteiro e a parte secreta)
    # e as duas chaves entre os segredos.
    key_token = bot_api.token_of(bot_api.add_bot())
    register_bot(client, ADMIN_TOKEN, offering_ref(create_offering(client, ADMIN_TOKEN, code=KEY_OFFERING)),
                 key_token)
    key_secrets = _secrets(key_token, key_token.split(":", 1)[1], MASTER_KEY, OTHER_MASTER_KEY)
    for planted in (key_token, key_token.split(":", 1)[1], MASTER_KEY, OTHER_MASTER_KEY):
        assert mcp_client.scan(planted, key_secrets), "controle positivo do token e das chaves-mestras"
    row = sql.execute("SELECT b.bot_uuid, b.telegram_bot_id, b.token_ciphertext FROM bots b "
                      "JOIN offerings o ON o.id = b.offering_id WHERE o.code = %s", (KEY_OFFERING,)).fetchone()
    assert row is not None, "o bot do caso da chave não foi gravado"
    # Pré-condição: o token gravado decifra com a chave certa (a falha vem da outra chave, não de um token ruim).
    assert TokenCipher(MASTER_KEY).decrypt(bytes(row[2]), bot_uuid=row[0], telegram_bot_id=int(row[1])) == key_token
    first_output = len(outputs)
    app = create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS,
                     doctor_settings=DoctorSettings(token_cipher=TokenCipher(OTHER_MASTER_KEY)))
    with TestClient(app, raise_server_exceptions=False) as api:
        # --- API ---
        body = system_doctor(api)
        outputs.append(("api chave", json.dumps(body, ensure_ascii=False)))
        assert _by_id(body["checks"], "bots.master_key") == MASTER_KEY_FAILURE

        # --- CLI, em texto e em JSON (perfil local, sem `host_probe`) ---
        for json_mode in (False, True):
            result = invoke_doctor(["doctor"], obj={"http": api, "stack": FakeStack(["healthy"])},
                                   env=world.env("local"), config=world.config, json=json_mode)
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                "chave", json_mode, type(result.exception).__name__)
            outputs.append((f"cli chave json={json_mode}", result.stdout + result.stderr))
            if json_mode:
                checks = json.loads(result.stdout)["checks"]
                assert _by_id(checks, "bots.master_key")["result"] == "failure", checks
                assert result.exit_code == 1, result.exit_code
            else:
                lines = [line for line in result.stdout.splitlines()
                         if re.match(r"^\[[^\]]+\] bots\.master_key[ :]", line)]
                assert len(lines) == 1, result.stdout

        # --- `run_doctor` pelo MCP, nas duas eras ---
        for mode in MODES:
            stderr = io.StringIO()
            deps = mcp_client.make_deps(tmp_path, env={"CQ_CONFIG": str(world.config), "CQ_PROFILE": "local"},
                                        http=api, stderr=stderr)
            outcome = mcp_client.run(_mcp_doctor, deps, mode)
            assert outcome.protocol_code is None and not outcome.is_error, ("chave", mode, outcome.text)
            assert isinstance(outcome.structured, dict)
            assert [(c["id"], c["result"]) for c in outcome.structured["checks"]] == [
                (c["id"], c["result"]) for c in body["checks"]], ("chave", mode)
            outputs.append((f"mcp chave {mode}", json.dumps(outcome.raw, ensure_ascii=False)))
            outputs.append((f"mcp chave {mode} stderr", stderr.getvalue()))
    for where, text in outputs[first_output:]:
        _assert_private([text], key_secrets, literals, where=where)

    assert len(outputs) == (len(SERVER_CASES) + 1) * (1 + 2 + 2 * len(MODES))
    for where, text in outputs:
        _assert_private([text], key_secrets, literals, where=where)


# ---------------------------------------------------------------------------------------------
# Gerador de métricas
# ---------------------------------------------------------------------------------------------


def _capture(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, str, str]:
    """Chama `fn` com o `sys.stdout` e o `sys.stderr` trocados; devolve (valor, stdout, stderr). O
    `NotImplementedError` do esqueleto sobe."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        value = fn(*args, **kwargs)
    return value, out.getvalue(), err.getvalue()


def _pseudonyms(sql: psycopg.Connection) -> list[str]:
    rows = sql.execute("SELECT pseudo_id::text, alias FROM people").fetchall()
    return sorted({value.lower() for row in rows for value in row if value})


def _pseudonyms_in(text: str, pseudonyms: Sequence[str]) -> list[str]:
    lowered = text.lower()
    return [p for p in pseudonyms if re.search(rf"(?<![0-9a-z]){re.escape(p)}(?![0-9a-z])", lowered)]


def test_pilot_metrics_output_has_no_pseudonym_identity_or_secret(client: Any, sql: psycopg.Connection,
                                                                    tmp_path: Path) -> None:
    """o gerador de métricas sobre a semente canário (o `O2` com respostas, entregas e a resposta de
    texto que cita a pessoa apagada), em JSON, em Markdown com `--out` e com erro (oferecimento inexistente): o
    stdout, o stderr e o arquivo sem pseudônimo nem apelido de pessoa, sem identidade e sem segredo."""
    from tests.helpers import purge

    world = _world(client, sql, tmp_path)
    metrics = load_ops("ops/pilot/pilot_metrics.py")
    other = world.scenario.other
    assert other is not None and other.dispatch_id is not None
    plan = tmp_path / "plano.toml"
    plan.write_text("\n".join([
        'timezone = "America/Sao_Paulo"', f'offering = "{other.code}"', "",
        "[[slot]]", 'kind = "daily"', 'session = ""', "opens_at = 2031-03-10T10:00:00-03:00",
        "closes_at = 2031-03-10T22:00:00-03:00", f'repo = "{purge._REPO}"', f'commit = "{purge._COMMIT}"',
        'qid = "text"', 'field_type = "pl-string-input"', 'source = "demo"', ""]), encoding="utf-8")
    env = {"CQ_CONFIG": str(world.config)}
    pseudonyms = _pseudonyms(sql)
    assert pseudonyms and _pseudonyms_in(f"x {pseudonyms[0]} y", pseudonyms), "controle positivo dos pseudônimos"
    secrets = _secrets()
    literals = _forms(world.stack_dir / ".env")

    base = ["--profile", "piloto", "--offering", other.code, "--plan", str(plan)]
    out_file = tmp_path / "metricas.md"
    runs = [
        ("json", [*base, "--json", "--agent-token-id", world.state.token_id], True),
        ("markdown", [*base, "--out", str(out_file)], True),
        ("inexistente", ["--profile", "piloto", "--offering", "implantacao-inexistente", "--plan", str(plan)], False),
    ]
    for label, argv, ok in runs:
        code, stdout, stderr = _capture(metrics.main, argv, http=client, env=env)
        assert (code == 0) == ok, (label, code, stderr[:300])
        texts = [stdout, stderr]
        if label == "markdown":
            assert out_file.is_file(), "o --out não gravou o relatório"
            texts.append(out_file.read_text(encoding="utf-8"))
        if label == "json":
            json.loads(stdout)
        _assert_private(texts, secrets, literals, where=f"métricas {label}")
        for text in texts:
            assert _pseudonyms_in(text, pseudonyms) == [], (label, _pseudonyms_in(text, pseudonyms))


# ---------------------------------------------------------------------------------------------
# Scripts de `ops/` e `scripts/fetch_age.py`
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Out:
    script: str
    outcome: str           # "ok" ou "fail"
    texts: tuple[str, ...]


# Os scripts e o teste que os exercita (conferido contra a árvore em `test_ci_pilot_and_fetch_outputs_are_private`).
SCRIPT_TESTS: dict[str, str] = {
    "ops/backup/cq_backup.py": "backup", "ops/backup/restore.py": "backup", "ops/backup/pull_backups.py": "backup",
    "ops/deploy/init_vm_env.py": "deploy", "ops/deploy/make_bundle.py": "deploy",
    "ops/export/export_public.py": "deploy", "ops/pilot/pilot_metrics.py": "metrics",
    "ops/ci/ci_suite.py": "tools", "ops/ci/validate_local.py": "tools", "ops/pilot/check_question_refs.py": "tools",
    "scripts/fetch_age.py": "tools",
}


def _scripts_with_main() -> set[str]:
    found = {path.relative_to(REPO).as_posix() for path in (REPO / "ops").rglob("*.py")
             if re.search(r"^def main\(", path.read_text(encoding="utf-8"), re.MULTILINE)}
    return found | {"scripts/fetch_age.py"}


def _check_records(records: list[_Out], scripts: set[str], secrets: Sequence[str], literals: Sequence[str]) -> None:
    for record in records:
        _assert_private(record.texts, secrets, literals, where=f"{record.script} {record.outcome}")
    for script in scripts:
        outcomes = {r.outcome for r in records if r.script == script}
        assert outcomes == {"ok", "fail"}, (script, outcomes)


def _invalid_main(rel: str) -> _Out:
    """O `main` com argumentos inválidos (nenhum recurso real): código ≠ 0 e as saídas varridas."""
    module = load_ops(rel)
    result = run_main(module, ["--argumento-inexistente-da-implantacao", ZEFIRO.name], env=clean_env())
    assert result.code != 0, (rel, result.code)
    return _Out(rel, "fail", (result.stdout, result.stderr))


def test_backup_restore_and_pull_outputs_are_private(tmp_path: Path) -> None:
    """a rotina, a restauração e a cópia, com sucesso e com falha, pelo `FakeSpawn` (o dump com o nome
    canário, a identidade do age, o `stderr` dos processos com valores plantados, a chave SSH num caminho com nome
    canário): nenhum valor plantado nas saídas nem no registro e no `SHA256SUMS`."""
    cq_backup = load_ops("ops/backup/cq_backup.py")
    restore = load_ops("ops/backup/restore.py")
    pull_backups = load_ops("ops/backup/pull_backups.py")
    secrets = _secrets()
    planted_dump = DEFAULT_DUMP + PLANTED_TEXT.encode("utf-8")
    planted_stderr = f"erro: {PLANTED_TEXT} {FAKE_AGE_IDENTITY}\n".encode()
    assert mcp_client.scan(planted_stderr.decode(), secrets), "controle positivo do stderr plantado"
    records: list[_Out] = []

    # --- rotina de backup ---
    def backup(name: str, recipients: str, *, dump_code: int = 0) -> tuple[int, str, str, Path]:
        root = tmp_path / f"vm-{name}"
        (root / "dumps").mkdir(parents=True)
        (root / "state").mkdir(parents=True)
        rec = root / "recipients.txt"
        rec.write_text(recipients, encoding="utf-8")
        spawn = FakeSpawn()
        spawn.on_docker_ps([CONTAINER], project="cq")
        spawn.on_pg_dump(planted_dump, returncode=dump_code, stderr=planted_stderr if dump_code else b"")
        spawn.on_age()
        out, err = io.StringIO(), io.StringIO()
        config = cq_backup.BackupConfig(project="cq", recipients=rec, dumps_dir=root / "dumps",
                                        state_dir=root / "state")
        code = cq_backup.run(config, spawn=spawn, now=lambda: datetime(2026, 10, 29, 3, 30, 5, tzinfo=UTC),
                             stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue(), root

    code, out, err, root = backup("ok", f"# chave\n{RECIPIENT}\n")
    assert code == cq_backup.BACKUP_MESSAGES["ok"][0], err
    written = [p.read_text(encoding="utf-8") for p in (root / "state" / cq_backup.RECORD_FILE,
                                                       root / "dumps" / cq_backup.SUMS_FILE)]
    records.append(_Out("ops/backup/cq_backup.py", "ok", (out, err, *written)))
    code, out, err, _ = backup("identidade", f"{RECIPIENT}\n{FAKE_AGE_IDENTITY}\n")
    assert code == cq_backup.BACKUP_MESSAGES["recipients_invalid"][0]
    records.append(_Out("ops/backup/cq_backup.py", "fail", (out, err)))
    code, out, err, _ = backup("dump", f"{RECIPIENT}\n", dump_code=1)
    assert code == cq_backup.BACKUP_MESSAGES["dump_failed"][0]
    records.append(_Out("ops/backup/cq_backup.py", "fail", (out, err)))
    records.append(_invalid_main("ops/backup/cq_backup.py"))

    # --- restauração ---
    dump = tmp_path / "restauracao" / DUMP_NAME
    dump.parent.mkdir()
    dump.write_bytes(fake_age_ciphertext(planted_dump))
    identity = tmp_path / f"chave {ZEFIRO.name}" / "identidade.txt"
    identity.parent.mkdir()
    identity.write_text(f"# chave do teste\n{FAKE_AGE_IDENTITY}\n", encoding="utf-8")

    def restore_run(project: str, *, age_code: int = 0, restore_stderr: bytes = b"") -> tuple[int, str, str]:
        spawn = FakeSpawn()
        spawn.on_docker_ps([CONTAINER], project=project)
        spawn.on_docker_ps_project([CONTAINER], project=project)
        spawn.on_psql_tables(0)
        spawn.on_age(returncode=age_code, stderr=planted_stderr if age_code else b"")
        spawn.on_pg_restore(stderr=restore_stderr)
        out, err = io.StringIO(), io.StringIO()
        code = restore.restore(dump, identity, project, spawn=spawn, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    code, out, err = restore_run("cq-restauracao")
    assert code == restore.RESTORE_MESSAGES["ok"][0], err
    records.append(_Out("ops/backup/restore.py", "ok", (out, err)))
    code, out, err = restore_run("cq-restauracao", age_code=1)
    assert code == restore.RESTORE_MESSAGES["decrypt_failed"][0]
    records.append(_Out("ops/backup/restore.py", "fail", (out, err)))
    code, out, err = restore_run("cq-restauracao", restore_stderr=IGNORED_ERRORS_STDERR + planted_stderr)
    assert code == restore.RESTORE_MESSAGES["restore_failed"][0]
    records.append(_Out("ops/backup/restore.py", "fail", (out, err)))
    code, out, err = restore_run("cq-demo")
    assert code == restore.RESTORE_MESSAGES["reserved_project"][0]
    records.append(_Out("ops/backup/restore.py", "fail", (out, err)))
    records.append(_invalid_main("ops/backup/restore.py"))

    # --- cópia ---
    remote = tmp_path / "vm-remota"
    remote.mkdir()
    data = fake_age_ciphertext(planted_dump)
    (remote / DUMP_NAME).write_bytes(data)
    key = tmp_path / f"ssh {ZEFIRO.name}" / "id_ed25519"
    key.parent.mkdir()
    key.write_text(_join("-----BEGIN ", "OPENSSH ", "PRIVATE KEY-----") + "\nfalsa\n"
                   + _join("-----END ", "OPENSSH ", "PRIVATE KEY-----") + "\n", encoding="utf-8")
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def pull_run(name: str, sums: str, *, sftp_code: int = 0) -> tuple[str, ...]:
        (remote / "SHA256SUMS").write_text(sums, encoding="utf-8")
        spawn = FakeSpawn().on_sftp(remote, remote_dir=REMOTE_DIR, returncode=sftp_code,
                                    stderr=planted_stderr if sftp_code else b"")
        transport = pull_backups.SftpTransport("vm.example.org", pull_backups.DEFAULT_USER, key, REMOTE_DIR,
                                               spawn=spawn)
        local = tmp_path / f"copia-{name}"
        local.mkdir()
        try:
            result, out, err = _capture(pull_backups.pull, transport, local, pull_backups.DEFAULT_KEEP_DAYS, now)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - o texto da exceção também é varrido
            return (str(exc), repr(exc), repr(transport))
        return (repr(result), out, err, repr(transport))

    good = f"{sha256_hex(data)}  {DUMP_NAME}\n"
    records.append(_Out("ops/backup/pull_backups.py", "ok", pull_run("ok", good)))
    records.append(_Out("ops/backup/pull_backups.py", "fail", pull_run("divergente", f"{'0' * 64}  {DUMP_NAME}\n")))
    records.append(_Out("ops/backup/pull_backups.py", "fail", pull_run("inalcancavel", good, sftp_code=255)))
    local_ok = tmp_path / "copia-local"
    local_ok.mkdir()
    result, out, err = _capture(pull_backups.pull, pull_backups.LocalDirTransport(remote), local_ok,
                                pull_backups.DEFAULT_KEEP_DAYS, now)
    assert result.new == [DUMP_NAME], result
    records.append(_Out("ops/backup/pull_backups.py", "ok", (repr(result), out, err)))
    records.append(_invalid_main("ops/backup/pull_backups.py"))

    _check_records(records, {s for s, t in SCRIPT_TESTS.items() if t == "backup"}, secrets,
                   [*_forms(identity, key), FAKE_AGE_IDENTITY, SMTP_USER])


_FIXTURE_FILES: dict[str, bytes] = {
    "app/main.py": b"print('ok')\n",
    "ops/export/allowlist.txt": b"app/**\nops/export/*.txt\n",
    "ops/export/test-needs.txt": b"",
    "ops/export/fictitious.txt": b"",
    "ops/export/binary-allowlist.txt": b"",
    "docs/execucao/instaveis-conhecidos.txt": b"# nenhum\n",
    # A sobreposição pública: sem ela, a exportação recusaria as duas origens antes da varredura.
    "ops/export/publico/AGENTS.md": b"# Projeto de fixture\n",
    "ops/export/publico/CLAUDE.md": b"@AGENTS.md\n",
    "ops/export/publico/ROADMAP.md": b"# Rumo\n",
}


def test_init_env_bundle_and_export_outputs_are_private(tmp_path: Path) -> None:
    """o `.env` da VM (a chave-mestra, o usuário e a senha de app pelo `prompt`, os tokens gerados; o `.env`
    existente com o sentinela; a chave-mestra inválida com o nome canário), o pacote e a exportação num repositório
    de fixture (sucesso; e com uma `CQ_MASTER_KEY` e um e-mail canário plantados, a recusa `arquivo:linha: tipo`):
    nenhum valor plantado nem gerado nas saídas."""
    init_vm_env = load_ops("ops/deploy/init_vm_env.py")
    make_bundle = load_ops("ops/deploy/make_bundle.py")
    export_public = load_ops("ops/export/export_public.py")
    generated = [py_secrets.token_urlsafe(32) for _ in range(8)]
    secrets = _secrets(*generated)
    records: list[_Out] = []

    # --- `.env` da VM ---
    def prompt_for(master_key: str) -> Callable[[str], str]:
        def prompt(text: str) -> str:
            lowered = text.lower()
            if "chave" in lowered:
                return master_key
            if "senha" in lowered:
                return APP_PASSWORD
            return SMTP_USER
        return prompt

    tokens = iter(generated)

    def run_init(stack_dir: Path, master_key: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = init_vm_env.init(stack_dir, repo_hosts="github.com", prompt=prompt_for(master_key),
                                token_factory=lambda: next(tokens), home=tmp_path / "home", stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    fresh = tmp_path / "srv-cq-app"
    fresh.mkdir()
    code, out, err = run_init(fresh, MASTER_KEY)
    assert code == init_vm_env.INIT_ENV_MESSAGES["ok"][0], err
    records.append(_Out("ops/deploy/init_vm_env.py", "ok", (out, err)))
    existing = make_stack_dir(tmp_path / "existente")
    before = (existing / ".env").read_bytes()
    code, out, err = run_init(existing, MASTER_KEY)
    assert code == init_vm_env.INIT_ENV_MESSAGES["exists"][0]
    assert (existing / ".env").read_bytes() == before
    records.append(_Out("ops/deploy/init_vm_env.py", "fail", (out, err)))
    bad = tmp_path / "chave-invalida"
    bad.mkdir()
    code, out, err = run_init(bad, f"curta-{ZEFIRO.name}")
    assert code == init_vm_env.INIT_ENV_MESSAGES["invalid_master_key"][0]
    records.append(_Out("ops/deploy/init_vm_env.py", "fail", (out, err)))
    records.append(_invalid_main("ops/deploy/init_vm_env.py"))

    # --- pacote e exportação, num repositório de fixture ---
    clean = make_repo(tmp_path, _FIXTURE_FILES, name="origem-limpa")
    planted = make_repo(tmp_path, {**_FIXTURE_FILES, "app/config.py": f"CQ_MASTER_KEY={MASTER_KEY}\n".encode(),
                                   "app/contato.txt": f"contato: {OLIMPIO.email}\n".encode()},
                        name="origem-plantada")
    with mock.patch.dict(os.environ, git_env(tmp_path), clear=True):
        for name, repo, ok in (("limpa", clean, True), ("plantada", planted, False)):
            out_file = tmp_path / f"pacote-{name}" / "cq-bundle.tar.gz"
            out_file.parent.mkdir()
            code, out, err = _capture(make_bundle.build_bundle, repo, out_file)
            assert (code == 0) == ok, (name, code, err[:300])
            if not ok:
                assert "app/config.py" in out + err and "env_assignment" in out + err
            records.append(_Out("ops/deploy/make_bundle.py", "ok" if ok else "fail", (out, err)))
            dest = tmp_path / f"destino-{name}"
            dest.mkdir()
            code, out, err = _capture(export_public.export, repo, dest, "Autor Público", "autor@publico.example.org")
            assert (code == 0) == ok, (name, code, err[:300])
            if not ok:
                # O e-mail canário fica num subdomínio de example.com, que a exportação
                # aceita (tests/unit/test_export_public.py); a recusa vem do app/config.py.
                assert "app/config.py" in out + err and "env_assignment" in out + err
            records.append(_Out("ops/export/export_public.py", "ok" if ok else "fail", (out, err)))
    records.append(_invalid_main("ops/deploy/make_bundle.py"))
    records.append(_invalid_main("ops/export/export_public.py"))

    _check_records(records, {s for s, t in SCRIPT_TESTS.items() if t == "deploy"}, secrets,
                   [MASTER_KEY, APP_PASSWORD, SMTP_USER, *_forms(fresh / ".env", existing / ".env", bad / ".env")])


def _junit(path: Path, failure: str | None) -> None:
    case = ('<testcase classname="tests.unit.test_falso" name="test_um" file="tests/unit/test_falso.py">'
            + (f'<failure message="{failure}">{failure}</failure>' if failure else "") + "</testcase>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="1" '
                    f'failures="{1 if failure else 0}" errors="0">{case}</testsuite></testsuites>', encoding="utf-8")


def _fake_pytest(junit_dir: Path, code: int, failure: str | None) -> Callable[[Sequence[str]], int]:
    calls = iter(range(1000))

    def run(args: Sequence[str]) -> int:
        target: Path | None = None
        items = list(args)
        for i, arg in enumerate(items):
            for flag in ("--junitxml", "--junit-xml"):
                if arg.startswith(flag + "="):
                    target = Path(arg.split("=", 1)[1])
                elif arg == flag and i + 1 < len(items):
                    target = Path(items[i + 1])
        _junit(target if target is not None else junit_dir / f"falso-{next(calls)}.xml", failure)
        return code

    return run


class _Opener:
    def __init__(self, data: bytes | None = None, error: BaseException | None = None) -> None:
        self.data, self.error, self.urls = data, error, []

    def __call__(self, url: Any, *args: Any, **kwargs: Any) -> io.BytesIO:
        self.urls.append(str(getattr(url, "full_url", url)))
        if self.error is not None:
            raise self.error
        return io.BytesIO(self.data or b"")


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_ci_pilot_and_fetch_outputs_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """o passo do CI (a falha do JUnit com valores plantados), a validação local (o executor falso), a
    conferência das questões (o `git` real sobre um repositório de fixture; o SHA-256 divergente; a rede caída com o
    `stderr` plantado) e o `fetch_age.py` (o arquivo conferido; o divergente; o download que falha com o texto
    plantado), com sucesso e com falha; e todo script de `ops/` com `main` está em `SCRIPT_TESTS`."""
    assert _scripts_with_main() == set(SCRIPT_TESTS), sorted(_scripts_with_main() ^ set(SCRIPT_TESTS))
    ci_suite = load_ops("ops/ci/ci_suite.py")
    validate_local = load_ops("ops/ci/validate_local.py")
    check_refs = load_ops("ops/pilot/check_question_refs.py")
    fetch_age = load_ops("scripts/fetch_age.py")
    secrets = _secrets()
    records: list[_Out] = []

    # --- CI ---
    for name, code, failure in (("verde", 0, None), ("vermelho", 1, f"falhou com {PLANTED_TEXT}")):
        junit_dir = tmp_path / f"junit-{name}"
        summary = io.StringIO()
        value, out, err = _capture(ci_suite.run, "fast", workers=1, junit_dir=junit_dir, summary=summary,
                                   pytest=_fake_pytest(junit_dir, code, failure))
        assert (value == 0) == (code == 0), (name, value)
        records.append(_Out("ops/ci/ci_suite.py", "ok" if code == 0 else "fail", (out, err, summary.getvalue())))
    records.append(_invalid_main("ops/ci/ci_suite.py"))

    # --- validação local ---
    export_dir = tmp_path / "exportacao"
    export_dir.mkdir()
    (export_dir / "README.md").write_text("exportação de teste\n", encoding="utf-8")
    for name, code in (("verde", 0), ("vermelho", 1)):
        def runner(argv: Sequence[str], *args: Any, _code: int = code, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(list(argv), _code, stdout=PLANTED_TEXT, stderr=PLANTED_TEXT)

        value, out, err = _capture(validate_local.run, export_dir, run_id="canario", runner=runner)
        assert (value == 0) == (code == 0), (name, value)
        records.append(_Out("ops/ci/validate_local.py", "ok" if code == 0 else "fail", (out, err)))
    records.append(_invalid_main("ops/ci/validate_local.py"))

    # --- conferência das questões (o `git` real sobre a fixture local) ---
    info = b'{"uuid": "11111111-2222-4333-8444-555555555555", "title": "Velocidade"}\n'
    questions = make_repo(tmp_path, {"questions/velocidade/info.json": info,
                                     "questions/velocidade/question.html": b'<pl-number-input answers-name="v">'
                                                                           b"</pl-number-input>\n"},
                          name="questoes")
    genv = git_env(tmp_path)
    for option in ("uploadpack.allowAnySHA1InWant", "uploadpack.allowFilter"):
        subprocess.run(["git", "config", option, "true"], cwd=questions, env=genv, check=True, timeout=60)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=questions, env=genv, check=True, timeout=60,
                            capture_output=True).stdout.decode("ascii").strip()
    url = "https://repos.cq.test/apoio/questoes-implantacao.git"

    def local_git(argv: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        kwargs["env"] = genv
        return subprocess.run([questions.as_posix() if a == url else a for a in argv], *args, **kwargs)

    def network_down(argv: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(list(argv), 128, stdout=b"", stderr=planted_bytes)

    planted_bytes = f"fatal: {PLANTED_TEXT}\n".encode()
    for name, sha, runner, ok in (("ok", hashlib.sha256(info).hexdigest(), local_git, True),
                                  ("divergente", "0" * 64, local_git, False),
                                  ("sem-rede", hashlib.sha256(info).hexdigest(), network_down, False)):
        manifest = tmp_path / f"manifesto-{name}.json"
        manifest.write_text(json.dumps([{"repo": url, "commit": commit, "qid": "velocidade",
                                         "field_type": "pl-number-input", "info_json_sha256": sha}]), encoding="utf-8")
        value, out, err = _capture(check_refs.check, manifest, runner=runner)
        assert (value == 0) == ok, (name, value, err[:300])
        records.append(_Out("ops/pilot/check_question_refs.py", "ok" if ok else "fail", (out, err)))
    records.append(_invalid_main("ops/pilot/check_question_refs.py"))

    # --- `fetch_age.py` (nenhuma rede: o `opener` falso) ---
    monkeypatch.setattr(urllib.request, "urlopen", _Opener(error=urllib.error.URLError("rede barrada no teste")))
    archive = _tar_gz({"age/age": b"binario falso do age", "age/age-keygen": b"binario falso do age-keygen",
                       "age/LICENSE": f"licenca falsa {PLANTED_TEXT}\n".encode()})
    asset = fetch_age.AGE_ASSETS["linux-amd64"][0]
    monkeypatch.setitem(fetch_age.AGE_ASSETS, "linux-amd64", (asset, hashlib.sha256(archive).hexdigest()))
    cases = (("ok", _Opener(archive), True),
             ("divergente", _Opener(archive + b"x"), False),
             ("sem-rede", _Opener(error=urllib.error.URLError(f"falha {PLANTED_TEXT}")), False))
    for name, opener, ok in cases:
        dest = tmp_path / f"age-{name}"
        value, out, err = _capture(fetch_age.fetch, "linux-amd64", dest, opener=opener)
        assert (value == 0) == ok, (name, value, err[:300])
        records.append(_Out("scripts/fetch_age.py", "ok" if ok else "fail", (out, err)))
    records.append(_invalid_main("scripts/fetch_age.py"))

    _check_records(records, {s for s, t in SCRIPT_TESTS.items() if t == "tools"}, secrets, [SMTP_USER])
