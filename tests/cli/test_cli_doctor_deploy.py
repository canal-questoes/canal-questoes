"""O `cq doctor` da implantação (volume, disco e as globais de backup, SMTP e retenção).


Arquivo à parte: `tests/cli/test_cli_doctor.py` roda o `invoke_cli` congelado, que não aceita
`host_probe`; aqui o `cq doctor` roda pelo `invoke_doctor`, com a API dos testes por `http=`, o
`FakeStack`/`FakeRunner` do CLI e o `FakeHostProbe` no `ctx.obj`:
- em texto e em `--json`, a lista completa pelos grupos (as 4 do cliente, as do servidor, as 10 locais do CLI, as 2
  do host e as 2 do perfil), **nenhuma** "não verificado" por reserva; a parte de cada uma igual a `CHECK_PARTS`; o
  JSON válido contra o esquema atual do CLI;
- a linha `"[{label}] {id}{offering}: {message}{extra}"` com `retention.overdue` em aviso saindo `" (N)"` e o código
  do oferecimento no lugar do `offering`; código 1 só com "falha";
- perfil remoto (o perfil do túnel): volume e disco "não se aplica" com a mensagem da VM, sem
  chamar o probe; as da API vindas da API;
- com `stack` ou `runner` injetado e **sem** `host_probe`: as duas "não verificado" `no_host_probe`, nenhum argv
  novo no `FakeRunner` e o código de saída inalterado;
- `CQ_DOCTOR_DISK_WARNING_PERCENT`, `CQ_DOCTOR_DISK_FAILURE_PERCENT` ou `CQ_BACKUP_DIR` inválidos → erro de uso (2)
  com mensagem fixa, sem eco;
- diretório da pilha não achado, `DOCKER_HOST` remoto e `.env` ausente → volume e disco "não verificado" com o
  motivo das locais e **zero** chamadas ao `FakeHostProbe`.

Escolhas deste teste ([I]):
- o banco de teste não tem bot registrado: as `bot.*` não saem, e a lista tem as 27 menos as 3 `bot.*` (24 linhas);
  a contagem é calculada pelos grupos, não fixada;
- o estado de backup, SMTP e retenção é o do banco de teste (a API dos testes sem `CQ_BACKUP_STATUS_FILE`, sem linha em
  `mail_probe`): `backup.age` e `smtp.server` em aviso, o que não muda o código 0;
- o oferecimento vencido é arquivado com o relógio da API 31 dias antes do do banco e, depois, o relógio volta ao do
  banco (`align_clock_to_db`), para `clock.skew` ficar "ok";
- o erro de uso das variáveis do disco tem a **mesma** mensagem para valores inválidos diferentes da mesma variável
  (texto fixo), nada no stdout e nenhuma chamada ao probe.
Vermelho no esqueleto: o `cli.py` ignora o `host_probe`, emite as 4 reservadas, não emite as 2
do host nem lê as variáveis do disco.
"""

from __future__ import annotations

import os
import pathlib
from datetime import timedelta
from typing import Any

import pytest

from cq_client.doctor import (
    API_CLIENT_CHECK_IDS,
    CHECK_IDS,
    CHECK_PARTS,
    CLIENT_MESSAGES,
    DISK_SPACE_TEMPLATE,
    HOST_CHECK_IDS,
    LOCAL_CHECK_IDS,
    NOT_APPLICABLE_REMOTE,
    NOT_APPLICABLE_REMOTE_VM,
    NOT_VERIFIED_MESSAGES,
    POSTGRES_VOLUME_MESSAGES,
    PROFILE_CHECK_IDS,
    SERVER_CHECK_IDS,
)
from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

from tests.cli.test_cli_doctor import (
    ENGINE_DETAIL,
    LOCAL_HEADER,
    LOCAL_VIEW,
    _assert_clean,
    _check,
    _line,
    _new_globals,
    _permissions_result,
    _reader_token,
    _text,
)
from tests.helpers.api import ADMIN_TOKEN, auth, create_offering
from tests.helpers.cli_runner import UNREACHABLE_API_URL, align_clock_to_db, make_stack_dir, write_profile
from tests.helpers.contract import assert_cli_json
from tests.helpers.docker_fake import FakeRunner, assert_allowed
from tests.helpers.host_probe_fake import FakeHostProbe, invoke_doctor, usage, volume_anonymous, volume_ok
from tests.helpers.stack_fake import TEST_API_URL, FakeSleep, FakeStack

ROOT_DIR = "/var/lib/docker"
TUNNEL_URL = "http://127.0.0.1:18000"
BOT_IDS = ("bot.access", "bot.poll", "bot.lease")
# O ramo por plataforma do CLI: no Windows, `profile.permissions` sai "não verificado" (as ACLs).
UNVERIFIED_BY_PLATFORM = ["profile.permissions"] if os.name == "nt" else []


# --- montagem ------------------------------------------------------------------------------------------------


def _profile_file(tmp_path: pathlib.Path, *, token: str, stack_dir: pathlib.Path | None = None) -> pathlib.Path:
    """O arquivo de perfil de teste: o local `local` (com `stack_dir` quando dado) e o remoto `piloto`, o perfil do
    túnel (sem `stack_dir` nem `compose_project`)."""
    lines = [f'api_url = "{UNREACHABLE_API_URL}"', "",
             "[profiles.local]", 'kind = "local"', f'api_url = "{TEST_API_URL}"', f'token = "{token}"']
    if stack_dir is not None:
        lines.append(f"stack_dir = '{stack_dir}'")
    lines += ["", "[profiles.piloto]", 'kind = "remote"', f'api_url = "{TUNNEL_URL}"', f'token = "{token}"', ""]
    path = write_profile(tmp_path, "\n".join(lines))
    # o mesmo critério do `_profile_file` de `tests/cli/test_cli_doctor.py`, de que sai o
    # `_permissions_result` esperado: nos sistemas POSIX o arquivo fica restrito ao dono (0o600), então
    # `profile.permissions` sai "ok"; sem isso, o `write_profile` deixa o modo da umask (0644 na validação do CI no
    # Linux) e sai "aviso". No Windows o resultado esperado continua "não verificado" (as ACLs).
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _probe(*, mounts: Any = None, percent: float = 50) -> FakeHostProbe:
    return FakeHostProbe(mounts=volume_ok("cq") if mounts is None else mounts, root_dir=ROOT_DIR,
                         usage_by_path={ROOT_DIR: usage(percent, 100)})


def _api_part(*, retention: dict | None = None) -> list[dict]:
    """As da API no banco de teste sem bot: as 4 do cliente "ok", as 3 globais de base "ok" e as outras 3."""
    checks = [_check(i, "api", "ok", CLIENT_MESSAGES[(i, "ok")]) for i in API_CLIENT_CHECK_IDS]
    checks += [_check("bots.master_key", "api", "ok", DOCTOR_MESSAGES[("bots.master_key", "ok")]),
               _check("tasks.overdue", "api", "ok", DOCTOR_MESSAGES[("tasks.overdue", "ok")], count=0),
               _check("clock.skew", "api", "ok", DOCTOR_MESSAGES[("clock.skew", "ok")])]
    news = _new_globals()
    if retention is not None:
        news = [retention if c["id"] == "retention.overdue" else c for c in news]
    return checks + news


def _local_ok() -> list[dict]:
    return [_check(i, "local", "ok", CLIENT_MESSAGES[(i, "ok")], detail=ENGINE_DETAIL if i == "engine.image" else None)
            for i in LOCAL_CHECK_IDS]


def _profile_part() -> list[dict]:
    permissions = _permissions_result()
    return [_check("profile.permissions", "profile", permissions,
                   CLIENT_MESSAGES[("profile.permissions", permissions)]),
            _check("profile.git", "profile", "ok", CLIENT_MESSAGES[("profile.git", "ok")])]


def _host(volume: tuple[str, str] = ("ok", "ok"), disk: tuple[str, int] = ("ok", 50)) -> list[dict]:
    disk_result, percent = disk
    return [_check("postgres.volume", "local", volume[0], POSTGRES_VOLUME_MESSAGES[volume[1]]),
            _check("disk.space", "local", disk_result, DISK_SPACE_TEMPLATE.format(percent=percent, gib=f"{percent}.0"))]


def _host_rows(payload: dict) -> list[dict]:
    return [c for c in payload["checks"] if c["id"] in HOST_CHECK_IDS]


def _archive_overdue(client: Any, sql: Any, clock: Any, code: str) -> None:
    """Um oferecimento de rascunho arquivado com o relógio da API 31 dias antes do banco (fora do prazo padrão de
    30 dias), sem expurgo; o relógio volta depois ao do banco."""
    clock.set(align_clock_to_db(clock, sql) - timedelta(days=31))
    create_offering(client, ADMIN_TOKEN, strict_schema=False, code=code)
    prepared = client.post(f"/v1/offerings/{code}/archive/prepare", headers=auth(ADMIN_TOKEN))
    assert prepared.status_code == 200, prepared.text[:300]
    archived = client.post(f"/v1/offerings/{code}/archive", headers=auth(ADMIN_TOKEN),
                           json={"confirmation_code": prepared.json()["confirmation_code"]})
    assert archived.status_code == 200 and archived.json()["state"] == "archived", archived.text[:300]
    align_clock_to_db(clock, sql)


# --- casos -----------------------------------------------------------------------------------------------------


def test_doctor_full_report_with_host_probe_text_and_json(client: Any, sql: Any, clock: Any,
                                                          tmp_path: pathlib.Path) -> None:
    """com o `FakeHostProbe` (volume certo, 50% livre), o relatório completo pelos grupos, sem nenhuma
    "não verificado"; a parte de cada linha é a de `CHECK_PARTS`; o JSON é válido
    contra o esquema atual do CLI e igual ao texto linha a linha; código 0 (só avisos)."""
    token = _reader_token(client)
    stack_dir = make_stack_dir(tmp_path)
    config = _profile_file(tmp_path, token=token, stack_dir=stack_dir)
    expected = [*_api_part(), *_local_ok(), *_host(), *_profile_part()]
    # O número pelos grupos: os 27 ids menos as 3 `bot.*` (nenhum bot no banco de teste).
    assert len(expected) == len(API_CLIENT_CHECK_IDS) + len(SERVER_CHECK_IDS) - len(BOT_IDS) + len(LOCAL_CHECK_IDS) \
        + len(HOST_CHECK_IDS) + len(PROFILE_CHECK_IDS) == len(CHECK_IDS) - len(BOT_IDS) == 24
    for json_mode in (False, True):
        probe = _probe()
        align_clock_to_db(clock, sql)
        result = invoke_doctor(["doctor"], json=json_mode,
                               obj={"http": client, "stack": FakeStack(["healthy"]), "host_probe": probe},
                               env={"CQ_PROFILE": "local"}, config=config)
        assert (result.exit_code, result.stderr) == (0, ""), result.stderr
        if json_mode:
            payload = assert_cli_json("doctor", result.stdout)
            assert payload == {"profile": LOCAL_VIEW, "checks": expected}
            assert [c["part"] for c in payload["checks"]] == [CHECK_PARTS[c["id"]] for c in payload["checks"]]
            assert {c["id"] for c in payload["checks"]} == set(CHECK_IDS) - set(BOT_IDS)
            # Só o ramo por plataforma do CLI (as permissões do perfil no Windows) fica "não verificado".
            assert [c["id"] for c in payload["checks"] if c["result"] == "not_verified"] == UNVERIFIED_BY_PLATFORM
        else:
            assert result.stdout == _text(LOCAL_HEADER, expected)
            assert len([line for line in result.stdout.splitlines() if line.startswith("[não verificado]")]) == len(
                UNVERIFIED_BY_PLATFORM)
        assert ("postgres_mounts", "cq") in probe.calls
        _assert_clean(result, token, str(stack_dir), ROOT_DIR, "cq_pgdata")


def test_doctor_retention_overdue_line_and_exit_code_only_with_failure(client: Any, sql: Any, clock: Any,
                                                                       tmp_path: pathlib.Path) -> None:
    """`retention.overdue` em aviso: a linha `[aviso] retention.overdue (<código>): <mensagem> (1)` (o código no
    lugar do `offering`, a contagem no `extra`), o mesmo no JSON; só avisos → código 0; com o volume anônimo e 5% de
    disco (duas "falha") → código 1, o relatório no stdout."""
    token = _reader_token(client)
    stack_dir = make_stack_dir(tmp_path)
    config = _profile_file(tmp_path, token=token, stack_dir=stack_dir)
    _archive_overdue(client, sql, clock, "retencao-vencida")
    retention = _check("retention.overdue", "api", "warning",
                       DOCTOR_REASON_MESSAGES[("retention.overdue", "warning", "overdue")],
                       offering="retencao-vencida", count=1)
    obj = {"http": client, "stack": FakeStack(["healthy"]), "host_probe": _probe()}
    text = invoke_doctor(["doctor"], obj=obj, env={"CQ_PROFILE": "local"}, config=config)
    assert text.exit_code == 0, text.stderr
    assert _line("retention.overdue", "warning", retention["message"], offering="retencao-vencida", count=1) \
        in text.stdout.splitlines()
    assert "[aviso] retention.overdue (retencao-vencida): " in text.stdout

    align_clock_to_db(clock, sql)
    failing = {"http": client, "stack": FakeStack(["healthy"]),
               "host_probe": _probe(mounts=volume_anonymous(), percent=5)}
    as_json = invoke_doctor(["doctor"], json=True, obj=failing, env={"CQ_PROFILE": "local"}, config=config)
    assert as_json.exit_code == 1, as_json.stderr
    assert as_json.stderr.strip() != ""
    payload = assert_cli_json("doctor", as_json.stdout)
    assert payload["checks"] == [*_api_part(retention=retention), *_local_ok(),
                                 *_host(("failure", "anonymous"), ("failure", 5)), *_profile_part()]


def test_doctor_tunnel_profile_host_checks_not_applicable(client: Any, sql: Any, clock: Any,
                                                          tmp_path: pathlib.Path) -> None:
    """O perfil do túnel (remoto): as da API vêm da API; as 10 locais "não se aplica" com a
    mensagem do perfil remoto e volume e disco "não se aplica" com `NOT_APPLICABLE_REMOTE_VM`; o probe e a pilha não são
    chamados; código 0."""
    token = _reader_token(client)
    config = _profile_file(tmp_path, token=token)
    probe = _probe()
    stack = FakeStack(["healthy"])
    align_clock_to_db(clock, sql)
    result = invoke_doctor(["doctor"], json=True, obj={"http": client, "stack": stack, "host_probe": probe},
                           env={"CQ_PROFILE": "piloto"}, config=config)
    assert (result.exit_code, result.stderr) == (0, ""), result.stderr
    payload = assert_cli_json("doctor", result.stdout)
    assert payload["profile"]["kind"] == "remote" and payload["profile"]["api_url"] == TUNNEL_URL
    assert payload["checks"] == [
        *_api_part(),
        *(_check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE) for i in LOCAL_CHECK_IDS),
        *(_check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE_VM) for i in HOST_CHECK_IDS),
        *_profile_part()]
    assert probe.calls == [] and stack.calls == []
    text = invoke_doctor(["doctor"], obj={"http": client, "stack": stack, "host_probe": probe},
                         env={"CQ_PROFILE": "piloto"}, config=config)
    assert [line for line in text.stdout.splitlines() if line.startswith("[não se aplica]")][-2:] == [
        _line(i, "not_applicable", NOT_APPLICABLE_REMOTE_VM) for i in HOST_CHECK_IDS]


@pytest.mark.parametrize("injected", ["stack", "runner"])
def test_doctor_with_injected_double_and_no_host_probe(client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path,
                                                       injected: str) -> None:
    """com `stack` ou `runner` injetado e **sem** `host_probe`, o CLI não monta o `DockerHostProbe`: volume e
    disco "não verificado" `no_host_probe`, depois das locais do CLI; nenhum argv fora da lista fechada do CLI no
    `FakeRunner`; o código de saída é o das outras verificações (0 com a pilha saudável)."""
    token = _reader_token(client)
    stack_dir = make_stack_dir(tmp_path)
    config = _profile_file(tmp_path, token=token, stack_dir=stack_dir)
    runner = FakeRunner()
    sleep = FakeSleep(clock)
    obj: dict[str, Any] = {"http": client, "clock": sleep.monotonic, "sleep": sleep}
    obj[injected] = FakeStack(["healthy"]) if injected == "stack" else runner
    align_clock_to_db(clock, sql)
    result = invoke_doctor(["doctor"], json=True, obj=obj, env={"CQ_PROFILE": "local"}, config=config)
    assert result.exit_code == 0, (result.exit_code, result.stderr)
    payload = assert_cli_json("doctor", result.stdout)
    assert _host_rows(payload) == [_check(i, "local", "not_verified", NOT_VERIFIED_MESSAGES["no_host_probe"])
                                   for i in HOST_CHECK_IDS]
    ids = [c["id"] for c in payload["checks"]]
    assert ids.index("postgres.volume") == ids.index("api.owner") + 1
    assert_allowed(runner.calls, "cq", stack_dir)
    assert None not in runner.operations(), "o doctor rodou um argv fora da lista fechada do CLI"


@pytest.mark.parametrize(("variable", "value", "other_value"), [
    ("CQ_DOCTOR_DISK_WARNING_PERCENT", "abc-sentinela-Wd3", "outra-sentinela"),
    ("CQ_DOCTOR_DISK_WARNING_PERCENT", "0", "100"),
    ("CQ_DOCTOR_DISK_WARNING_PERCENT", "-1", "150"),
    ("CQ_DOCTOR_DISK_FAILURE_PERCENT", "100", "0"),
    ("CQ_DOCTOR_DISK_FAILURE_PERCENT", "30", "40"),
    ("CQ_BACKUP_DIR", "backups-sentinela/cq", "outro-relativo"),
], ids=["aviso-texto", "aviso-zero", "aviso-negativo", "falha-cem", "falha-acima-do-aviso", "backup-relativo"])
def test_doctor_invalid_disk_settings_is_usage_error(client: Any, tmp_path: pathlib.Path, variable: str, value: str,
                                                     other_value: str) -> None:
    """variável do disco inválida no ambiente do `cq` → erro de uso (2), com mensagem fixa (a
    mesma para outro valor inválido da mesma variável e do mesmo tipo de erro, **[I]**), sem o valor, nada no
    stdout e sem chamar o probe."""
    env = {variable: value}
    token = _reader_token(client)
    config = _profile_file(tmp_path, token=token, stack_dir=make_stack_dir(tmp_path))
    probe = _probe()
    result = invoke_doctor(["doctor"], obj={"http": client, "stack": FakeStack(["healthy"]), "host_probe": probe},
                           env={"CQ_PROFILE": "local", **env}, config=config)
    assert result.exit_code == 2, (result.exit_code, result.stdout[-300:])
    assert result.stdout == ""
    assert result.stderr.strip() != ""
    assert "sentinela" not in result.stderr and "Wd3" not in result.stderr and "Traceback" not in result.stderr
    assert probe.calls == []
    other = invoke_doctor(["doctor"], obj={"http": client, "stack": FakeStack(["healthy"]), "host_probe": _probe()},
                          env={"CQ_PROFILE": "local", variable: other_value}, config=config)
    assert (other.exit_code, other.stderr) == (2, result.stderr)


def test_doctor_local_early_exit_blocks_host_checks_without_probe_calls(client: Any, sql: Any, clock: Any,
                                                                        tmp_path: pathlib.Path) -> None:
    """diretório da pilha não achado (`stack_directory`), `DOCKER_HOST` remoto (`docker_target`) e `.env`
    ausente (`env_file`) → volume e disco "não verificado" com o motivo das locais e **zero** chamadas ao
    `FakeHostProbe`."""
    token = _reader_token(client)
    cases: list[tuple[str, pathlib.Path | None, dict[str, str], dict[str, Any]]] = []
    # O `stack_dir` do perfil inexistente: `StackDirNotFound` sem procurar no diretório atual.
    cases.append(("stack_directory", tmp_path / "pilha-inexistente", {}, {}))
    cases.append(("docker_target", make_stack_dir(tmp_path, name="pilha-alvo"), {"DOCKER_HOST": "tcp://10.1.2.3:2376"},
                  {}))
    cases.append(("env_file", make_stack_dir(tmp_path, name="pilha-sem-env", env_file=False), {},
                  {"stack": FakeStack(["healthy"])}))
    for reason, stack_dir, extra_env, extra_obj in cases:
        case_dir = tmp_path / f"perfil-{reason}"
        case_dir.mkdir()
        config = _profile_file(case_dir, token=token, stack_dir=stack_dir)
        probe = _probe()
        align_clock_to_db(clock, sql)
        result = invoke_doctor(["doctor"], json=True, obj={"http": client, "host_probe": probe, **extra_obj},
                               env={"CQ_PROFILE": "local", **extra_env}, config=config)
        payload = assert_cli_json("doctor", result.stdout)
        assert _host_rows(payload) == [_check(i, "local", "not_verified", NOT_VERIFIED_MESSAGES[reason])
                                       for i in HOST_CHECK_IDS], reason
        assert probe.calls == [], f"{reason}: o doctor chamou o probe"
        assert result.exit_code == 1, (reason, result.stderr)
