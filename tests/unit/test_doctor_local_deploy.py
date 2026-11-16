"""As verificações novas do `doctor` no cliente, para o piloto.


`tests/unit/test_doctor_client.py` guarda as listas de ids e os corpos com as
6 globais; aqui ficam os casos completos das verificações novas, sem Click e sem banco:
- `postgres.volume` (`postgres_volume_check`) com o `FakeHostProbe`: "ok" com o volume `<projeto>_pgdata` em
  `/var/lib/postgresql`; "falha" `anonymous` (nome de 64 hex), `bind` e `wrong_path` (nada no destino, inclusive só
  em `/var/lib/postgresql/data`); `postgres_mounts` `None` → "não verificado" `postgres_not_running`; e
  (**[I]**, escolha deste teste), um volume nomeado de outro nome (`outro_pgdata`, nome
  sem 64 hex) no destino → "falha" `other_volume`, sem o nome do volume na mensagem;
- `disk.space` (`disk_space_check`): 50%, 15% e 5% livres → "ok", "aviso", "falha", nas fronteiras exatas de 20% e
  10%; o pior entre o `DockerRootDir` e o `CQ_BACKUP_DIR`; a mensagem `DISK_SPACE_TEMPLATE` com `{percent}` inteiro
  (piso) e `{gib}` com uma casa; `docker_root_dir` `None` → "não verificado" `docker_unavailable`; algum alvo
  inexistente neste host (Docker Desktop) → "não verificado" `unreachable_path`;
- nenhum valor do probe (caminho, nome de volume) na mensagem nem no `detail`;
- `collect_host_checks`: só as 2 do host, na ordem de `HOST_CHECK_IDS`, parte `local`; perfil remoto → "não se
  aplica" com `NOT_APPLICABLE_REMOTE_VM`, sem chamar o probe (e `collect_local_checks` continua com as suas 10, com
  a sua mensagem fixa); `blocked` (`stack_directory`, `docker_target`, `env_file`) → as duas "não verificado" com esse
  motivo e **zero** chamadas ao probe; `host_probe=None` → `no_host_probe`;
- `LocalDoctorSettings.from_env`: padrões 20 e 10; `0`, `100`, `-1`, `abc`, `nan`, `inf` recusados; `failure ≥
  warning` recusado; `CQ_BACKUP_DIR` relativo recusado; vazia = ausente; sem eco do valor;
- o validador do corpo do servidor (`collect_api_checks` com a API falsa do CLI): as 3 globais novas só com
  mensagem de `SERVER_REASON_MESSAGES` do `(id, result)`; em `backup.age` pelo motivo da idade, só com
  `message == BACKUP_AGE_TEMPLATE.format(hours=count)`; `count` só em `tasks.overdue`, `backup.age` e
  `retention.overdue`; `offering` só em `bot.*` e `retention.overdue`; `retention.overdue` nunca "falha"; `count`
  negativo, global ausente ou repetida recusados; na verificação da chave-mestra, `bots.master_key`
  "falha" aceito só com a mensagem nova (`SERVER_REASON_MESSAGES[("bots.master_key", "failure")]`), sem `count` nem
  `offering`, e recusado com outra mensagem (a do "aviso", texto livre) ou com a mensagem nova no "ok"; os pares
  `(id, result)` de `SERVER_MESSAGES` e de `SERVER_REASON_MESSAGES` não se repetem;
- o `DockerHostProbe` com o `ScriptedRunner`: os argv exatos (`docker compose -p … ps -q postgres`, com o
  prefixo do Compose; `docker inspect --format '{{json .Mounts}}' <id>`; `docker info --format '{{.DockerRootDir}}'`), o
  `env` igual a `docker_env(env)` e o `timeout` em cada chamada, as montagens lidas para `Mount`, e `None` com código
  ≠ 0, saída inválida ou exceção do executor; `disk_usage` por `shutil.disk_usage`, trocado por `monkeypatch`.

Escolhas deste teste ([I]; nada no produto as fixa):
- um volume nomeado certo no destino certo dá "ok" mesmo com outras montagens em outros destinos (só o destino
  `/var/lib/postgresql` decide); a lista vazia de montagens é "nada no destino" (`wrong_path`);
- nas fronteiras de 1 byte abaixo de 20% e de 10%, só o resultado e o `{percent}` (piso) são conferidos: o
  arredondamento de `{gib}` (uma casa) não é fixado para valores que não caem numa casa exata;
- `backup.age` com `not_configured` ou `unreadable` leva `count = null` (`null` sem registro
  legível); `retention.overdue` "ok" leva `count = 0` e `offering = null`, "aviso" leva `count ≥ 1` e o código;
- `LocalDoctorSettings.from_env` aceita percentual com casas (`12.5`), como número em (0, 100);
- `ps -q` sem nenhuma linha → `None` (serviço parado ou ausente), sem chamar o `inspect`; uma montagem sem `Name`
  (ex.: `bind`) vira `name=None`; o `DockerRootDir` vem sem espaços em volta, e a saída vazia é inválida;
- o `DockerHostProbe.disk_usage` chama `shutil.disk_usage` pelo atributo do módulo `shutil` (assim o `monkeypatch`
  o alcança) e devolve `None` para `OSError` (inclusive `FileNotFoundError`), sem usar o executor.

**[I]** Do formato do `docker inspect --format '{{json .Mounts}}'` (lista de objetos com `Type`, `Name`, `Source`,
`Destination`, `Driver`, `Mode`, `RW`, `Propagation`; o `bind` sem `Name`), as amostras abaixo são de memória, não
de uma saída real conferida.

No esqueleto, a falha vem de `NotImplementedError` (as funções novas do `doctor`, os métodos do `DockerHostProbe` e o
`from_env`) ou do validador sem as globais novas, que recusa todo corpo com elas.
"""

from __future__ import annotations

import collections
import json
import pathlib
import subprocess
from typing import Any

import pytest

from cq_client import doctor
from cq_client.doctor import (
    API_ACCESS_FAILURE_MESSAGES,
    BACKUP_AGE_TEMPLATE,
    DISK_SPACE_TEMPLATE,
    DOCTOR_ENV_BACKUP_DIR,
    DOCTOR_ENV_DISK_FAILURE,
    DOCTOR_ENV_DISK_WARNING,
    HOST_CHECK_IDS,
    LOCAL_CHECK_IDS,
    NOT_APPLICABLE_REMOTE,
    NOT_APPLICABLE_REMOTE_VM,
    NOT_VERIFIED_MESSAGES,
    POSTGRES_VOLUME_MESSAGES,
    SERVER_REASON_MESSAGES,
    Check,
    LocalDoctorSettings,
)
from cq_client.host_probe import HOST_PROBE_TIMEOUT_SECONDS, DiskUsage, DockerHostProbe, Mount
from cq_client.stack import DockerUnavailable, compose_prefix, docker_env
from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

from tests.helpers.host_probe_fake import (
    ANONYMOUS_VOLUME_NAME,
    GIB,
    FakeHostProbe,
    ScriptedRunner,
    bind,
    result,
    usage,
    volume_anonymous,
    volume_ok,
    wrong_path,
)
from tests.unit.test_doctor_client import (
    BACKUP,
    CLOCK,
    GLOBAL,
    LOCAL,
    OVERDUE,
    REMOTE,
    RETENTION,
    SMTP,
    VALID_SERVER,
    FakeApi,
    _collect,
    _doctor_body,
    _ok,
    _server_passed,
)

ROOT_DIR = "/var/lib/docker"
BACKUP_DIR = "/srv/cq-backups-sentinela"
PROJECT = "cq"


def _volume(result_: str, reason: str) -> Check:
    return Check("postgres.volume", "local", result_, POSTGRES_VOLUME_MESSAGES[reason])


def _not_verified(check_id: str, reason: str) -> Check:
    return Check(check_id, "local", "not_verified", NOT_VERIFIED_MESSAGES[reason])


def _disk(result_: str, percent: int, gib: str) -> Check:
    return Check("disk.space", "local", result_, DISK_SPACE_TEMPLATE.format(percent=percent, gib=gib))


def _assert_no_probe_values(check: Check) -> None:
    """Nenhum valor do probe (nome de volume, caminho do host) na mensagem nem no `detail`."""
    for text in (check.message, check.detail or ""):
        for value in (f"{PROJECT}_pgdata", ANONYMOUS_VOLUME_NAME, ROOT_DIR, BACKUP_DIR, "/data", "sentinela"):
            assert value not in text, f"{check.id}: {value!r} na mensagem"
    assert check.detail is None


# --- postgres.volume ------------------------------------------------------------------------------------------


def test_postgres_volume_ok_named_volume_in_the_right_place() -> None:
    """o volume nomeado `<projeto>_pgdata` em `/var/lib/postgresql` → "ok", com a mensagem fixa; o probe é
    lido pelo projeto dado. **[I]** Outras montagens em outros destinos não mudam o resultado."""
    probe = FakeHostProbe(mounts=volume_ok(PROJECT))
    check = doctor.postgres_volume_check(probe, PROJECT)
    assert check == _volume("ok", "ok")
    assert probe.calls == [("postgres_mounts", PROJECT)]
    _assert_no_probe_values(check)

    other = FakeHostProbe(mounts=(*volume_ok("cq-piloto"), *bind("/backup")))
    assert doctor.postgres_volume_check(other, "cq-piloto") == _volume("ok", "ok")


@pytest.mark.parametrize(("mounts", "reason"), [
    (volume_anonymous(), "anonymous"),
    (bind(), "bind"),
    (wrong_path("/var/lib/postgresql/data", project=PROJECT), "wrong_path"),
    (wrong_path("/data", project=PROJECT), "wrong_path"),
    (bind("/var/lib/postgresql/data"), "wrong_path"),
    ((), "wrong_path"),
], ids=["anonimo", "bind", "so-em-pgdata-data", "outro-destino", "bind-em-outro-destino", "sem-montagem"])
def test_postgres_volume_failures(mounts: tuple[Mount, ...], reason: str) -> None:
    """Volume anônimo (nome de 64 hex) → `anonymous`; diretório do host no destino → `bind`; nada no
    destino `/var/lib/postgresql` (inclusive só em `/var/lib/postgresql/data`) → `wrong_path`; todos "falha", sem o
    nome do volume nem o caminho na mensagem."""
    check = doctor.postgres_volume_check(FakeHostProbe(mounts=mounts), PROJECT)
    assert check == _volume("failure", reason)
    _assert_no_probe_values(check)


def test_postgres_volume_other_named_volume_is_failure() -> None:
    """Volume nomeado com outro nome no destino (**[I]**, escolha deste teste): um volume nomeado de outro nome
    (`outro_pgdata`, nome sem 64 hex) no destino `/var/lib/postgresql` → "falha" `other_volume` com a mensagem fixa,
    nunca "ok" (aceitar qualquer volume nomeado não confirma o volume do projeto); o nome do volume não entra na
    mensagem nem no `detail`."""
    mounts = volume_ok("outro")
    assert [(m.type, m.name, m.destination) for m in mounts] == [
        ("volume", "outro_pgdata", "/var/lib/postgresql")]
    assert POSTGRES_VOLUME_MESSAGES["other_volume"] == "O Postgres usa um volume com outro nome."
    check = doctor.postgres_volume_check(FakeHostProbe(mounts=mounts), PROJECT)
    assert check == _volume("failure", "other_volume")
    _assert_no_probe_values(check)
    for text in (check.message, check.detail or ""):
        assert "outro_pgdata" not in text


def test_postgres_volume_not_running_is_not_verified() -> None:
    """Serviço ausente ou parado (`postgres_mounts` devolve `None`) → "não verificado"
    `postgres_not_running`, nunca "falha"."""
    probe = FakeHostProbe(mounts=None)
    assert doctor.postgres_volume_check(probe, PROJECT) == _not_verified("postgres.volume", "postgres_not_running")
    assert NOT_VERIFIED_MESSAGES["postgres_not_running"] == (
        "Não verificado: o serviço do Postgres não está em execução.")


# --- disk.space -----------------------------------------------------------------------------------------------


def _disk_probe(root: DiskUsage | None, *, backup: DiskUsage | None = None, root_dir: str | None = ROOT_DIR,
                with_backup: bool = False) -> FakeHostProbe:
    usage_by_path: dict[str, DiskUsage] = {}
    if root is not None and root_dir is not None:
        usage_by_path[root_dir] = root
    if with_backup and backup is not None:
        usage_by_path[BACKUP_DIR] = backup
    return FakeHostProbe(root_dir=root_dir, usage_by_path=usage_by_path)


SETTINGS = LocalDoctorSettings()
WITH_BACKUP = LocalDoctorSettings(backup_dir=BACKUP_DIR)


@pytest.mark.parametrize(("percent", "expected"), [
    (50, _disk("ok", 50, "50.0")),
    (15, _disk("warning", 15, "15.0")),
    (5, _disk("failure", 5, "5.0")),
    (20, _disk("ok", 20, "20.0")),
    (10, _disk("warning", 10, "10.0")),
    (15.7, _disk("warning", 15, "15.7")),
], ids=["50", "15", "5", "fronteira-20", "fronteira-10", "piso-15.7"])
def test_disk_space_results_and_message(percent: float, expected: Check) -> None:
    """Com os limites padrão (aviso 20%, falha 10%), 50% livre → "ok", 15% → "aviso", 5% → "falha";
    nas fronteiras exatas, 20% → "ok" (pior livre ≥ aviso) e 10% → "aviso" (falha ≤ pior livre < aviso); a mensagem
    `DISK_SPACE_TEMPLATE` com `{percent}` inteiro pelo piso e `{gib}` livre com uma casa, sem o caminho."""
    probe = _disk_probe(usage(percent, 100))
    check = doctor.disk_space_check(probe, SETTINGS)
    assert check == expected
    assert ("disk_usage", ROOT_DIR) in probe.calls and ("docker_root_dir",) in probe.calls
    _assert_no_probe_values(check)


@pytest.mark.parametrize(("percent", "expected_result", "floor"), [
    (20, "warning", 19),
    (10, "failure", 9),
], ids=["um-byte-abaixo-de-20", "um-byte-abaixo-de-10"])
def test_disk_space_one_byte_below_each_limit(percent: int, expected_result: str, floor: int) -> None:
    """As fronteiras por um byte: 1 byte abaixo de 20% já é "aviso" e 1 byte abaixo de 10% já é "falha"; o
    `{percent}` é o piso (19 e 9). **[I]** O `{gib}` desses valores não é conferido (arredondamento não fixado)."""
    exact = usage(percent, 100)
    check = doctor.disk_space_check(_disk_probe(DiskUsage(total=exact.total, free=exact.free - 1)), SETTINGS)
    assert (check.id, check.part, check.result) == ("disk.space", "local", expected_result)
    assert check.message.startswith(f"Espaço em disco livre: {floor}% (")


def test_disk_space_custom_limits() -> None:
    """Os limites vêm do `LocalDoctorSettings`: com aviso em 60% e falha em 40%, 50% livre já é "aviso"."""
    settings = LocalDoctorSettings(disk_warning_percent=60, disk_failure_percent=40)
    assert doctor.disk_space_check(_disk_probe(usage(50, 100)), settings) == _disk("warning", 50, "50.0")
    assert doctor.disk_space_check(_disk_probe(usage(30, 100)), settings) == _disk("failure", 30, "30.0")


@pytest.mark.parametrize(("root", "backup", "expected"), [
    (usage(50, 100), usage(5, 200), _disk("failure", 5, "10.0")),
    (usage(5, 200), usage(50, 100), _disk("failure", 5, "10.0")),
    (usage(50, 100), usage(15, 100), _disk("warning", 15, "15.0")),
    (usage(30, 100), usage(60, 100), _disk("ok", 30, "30.0")),
], ids=["backup-pior", "docker-pior", "backup-em-aviso", "os-dois-ok"])
def test_disk_space_worst_of_docker_root_and_backup_dir(root: DiskUsage, backup: DiskUsage, expected: Check) -> None:
    """Os alvos são o `DockerRootDir` e o `CQ_BACKUP_DIR` (definido): o resultado e a mensagem saem do pior
    (menor percentual livre), sem nenhum dos dois caminhos na mensagem."""
    probe = _disk_probe(root, backup=backup, with_backup=True)
    check = doctor.disk_space_check(probe, WITH_BACKUP)
    assert check == expected
    assert {("disk_usage", ROOT_DIR), ("disk_usage", BACKUP_DIR)} <= set(probe.calls)
    _assert_no_probe_values(check)


def test_disk_space_docker_unreachable_is_not_verified() -> None:
    """`docker_root_dir` `None` (Docker inalcançável) → "não verificado" `docker_unavailable`, com a
    mensagem de `NOT_VERIFIED_MESSAGES`."""
    probe = _disk_probe(None, root_dir=None)
    assert doctor.disk_space_check(probe, SETTINGS) == _not_verified("disk.space", "docker_unavailable")
    assert doctor.disk_space_check(_disk_probe(None, root_dir=None), WITH_BACKUP) == _not_verified(
        "disk.space", "docker_unavailable")


@pytest.mark.parametrize(("root", "backup", "settings"), [
    (None, None, SETTINGS),
    (None, usage(50, 100), WITH_BACKUP),
    (usage(50, 100), None, WITH_BACKUP),
], ids=["docker-desktop", "docker-desktop-com-backup", "backup-inexistente"])
def test_disk_space_unreachable_path_is_not_verified(root: DiskUsage | None, backup: DiskUsage | None,
                                                     settings: LocalDoctorSettings) -> None:
    """Algum alvo inexistente neste host (o `DockerRootDir` do Docker Desktop, que fica na VM dele, ou o
    `CQ_BACKUP_DIR` ausente) → "não verificado" `unreachable_path`, sem caminho na mensagem."""
    check = doctor.disk_space_check(_disk_probe(root, backup=backup, with_backup=True), settings)
    assert check == _not_verified("disk.space", "unreachable_path")
    assert NOT_VERIFIED_MESSAGES["unreachable_path"] == "O disco dos dados do Docker não é acessível neste computador."
    _assert_no_probe_values(check)


# --- collect_host_checks --------------------------------------------------------------------------------------


def _healthy_probe() -> FakeHostProbe:
    return FakeHostProbe(mounts=volume_ok(PROJECT), root_dir=ROOT_DIR, usage_by_path={ROOT_DIR: usage(50, 100)})


def test_collect_host_checks_local_profile_returns_the_two_in_order() -> None:
    """`collect_host_checks` devolve só as 2 do host, na ordem de `HOST_CHECK_IDS`, parte `local`, pelo
    projeto do perfil (`compose_project`)."""
    probe = _healthy_probe()
    checks = doctor.collect_host_checks(LOCAL, host_probe=probe, settings=SETTINGS)
    assert checks == [_volume("ok", "ok"), _disk("ok", 50, "50.0")]
    assert [c.id for c in checks] == list(HOST_CHECK_IDS)
    assert ("postgres_mounts", LOCAL.compose_project) in probe.calls


@pytest.mark.parametrize("probe", [_healthy_probe(), None], ids=["com-probe", "sem-probe"])
def test_collect_host_checks_remote_profile_not_applicable_without_probe(probe: FakeHostProbe | None) -> None:
    """Perfil remoto (o túnel): as 2 do host "não se aplica" com `NOT_APPLICABLE_REMOTE_VM`, sem chamar o
    probe; as 10 locais continuam com `NOT_APPLICABLE_REMOTE` (`collect_local_checks` não muda)."""
    if probe is not None:
        probe.calls.clear()
    checks = doctor.collect_host_checks(REMOTE, host_probe=probe, settings=SETTINGS)
    assert checks == [Check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE_VM) for i in HOST_CHECK_IDS]
    assert NOT_APPLICABLE_REMOTE_VM == "Não se aplica pelo túnel: rode o doctor na VM."
    if probe is not None:
        assert probe.calls == [], "o doctor chamou o probe com o perfil remoto"
    local = doctor.collect_local_checks(None, REMOTE, stack_dir_error=None, env_file_present=None, api_reachable=True)
    assert local == [Check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE) for i in LOCAL_CHECK_IDS]


@pytest.mark.parametrize("blocked", ["stack_directory", "docker_target", "env_file"])
def test_collect_host_checks_blocked_by_local_early_exit_never_calls_probe(blocked: str) -> None:
    """com a saída antecipada das locais (diretório da pilha não achado, alvo do Docker não local, `.env`
    ausente), as duas "não verificado" com o motivo das locais e **zero** chamadas ao probe."""
    probe = _healthy_probe()
    checks = doctor.collect_host_checks(LOCAL, host_probe=probe, settings=SETTINGS, blocked=blocked)
    assert checks == [_not_verified(i, blocked) for i in HOST_CHECK_IDS]
    assert probe.calls == []


def test_collect_host_checks_without_probe_is_no_host_probe() -> None:
    """sem `host_probe` (dublê injetado no CLI), as duas "não verificado" `no_host_probe`, que não mudam o
    código de saída."""
    checks = doctor.collect_host_checks(LOCAL, host_probe=None, settings=SETTINGS)
    assert checks == [_not_verified(i, "no_host_probe") for i in HOST_CHECK_IDS]
    assert doctor.exit_code(checks) == 0


@pytest.mark.parametrize(("probe", "expected"), [
    (FakeHostProbe(mounts=volume_anonymous(), root_dir=ROOT_DIR, usage_by_path={ROOT_DIR: usage(5, 100)}),
     [_volume("failure", "anonymous"), _disk("failure", 5, "5.0")]),
    (FakeHostProbe(mounts=None, root_dir=None), [_not_verified("postgres.volume", "postgres_not_running"),
                                                 _not_verified("disk.space", "docker_unavailable")]),
], ids=["duas-falhas", "docker-parado"])
def test_collect_host_checks_combines_both_results(probe: FakeHostProbe, expected: list[Check]) -> None:
    """Cada verificação do host segue a sua regra, independentes; "falha" dá o código 1."""
    checks = doctor.collect_host_checks(LOCAL, host_probe=probe, settings=SETTINGS)
    assert checks == expected
    assert doctor.exit_code(checks) == (1 if any(c.result == "failure" for c in expected) else 0)


# --- LocalDoctorSettings.from_env -----------------------------------------------------------------------------


def test_local_settings_defaults_and_valid_values(tmp_path: pathlib.Path) -> None:
    """sem variável (ou vazia) → aviso 20, falha 10, sem `CQ_BACKUP_DIR`; valores válidos
    lidos (inclusive com casas, **[I]**); `CQ_BACKUP_DIR` absoluto aceito."""
    assert LocalDoctorSettings.from_env({}) == LocalDoctorSettings(20, 10, None)
    empty = {DOCTOR_ENV_DISK_WARNING: "", DOCTOR_ENV_DISK_FAILURE: "", DOCTOR_ENV_BACKUP_DIR: ""}
    assert LocalDoctorSettings.from_env(empty) == LocalDoctorSettings(20, 10, None)
    given = {DOCTOR_ENV_DISK_WARNING: "30", DOCTOR_ENV_DISK_FAILURE: "12.5", DOCTOR_ENV_BACKUP_DIR: str(tmp_path)}
    assert LocalDoctorSettings.from_env(given) == LocalDoctorSettings(30, 12.5, str(tmp_path))
    assert LocalDoctorSettings.from_env({"CQ_OUTRA": "1"}) == LocalDoctorSettings()


@pytest.mark.parametrize("name", [DOCTOR_ENV_DISK_WARNING, DOCTOR_ENV_DISK_FAILURE])
@pytest.mark.parametrize("value", ["0", "100", "-1", "150", "abc-sentinela-Pq7", "nan", "inf", "-inf", "1e400"])
def test_local_settings_reject_percent_out_of_range_without_echo(name: str, value: str) -> None:
    """Percentual fora de (0, 100), não numérico ou não finito → `ValueError` com texto fixo, sem o valor (a
    sentinela não aparece; os números curtos podem coincidir com o texto fixo, como "100")."""
    with pytest.raises(ValueError) as caught:
        LocalDoctorSettings.from_env({name: value})
    assert "sentinela" not in str(caught.value) and "Pq7" not in str(caught.value)


@pytest.mark.parametrize(("warning", "failure"), [("20", "20"), ("10", "30"), (None, "25"), ("5", None)],
                         ids=["iguais", "falha-maior", "falha-acima-do-aviso-padrao", "aviso-abaixo-da-falha-padrao"])
def test_local_settings_reject_failure_not_below_warning(warning: str | None, failure: str | None) -> None:
    """`failure < warning`, também contra o padrão da outra variável."""
    env = {k: v for k, v in ((DOCTOR_ENV_DISK_WARNING, warning), (DOCTOR_ENV_DISK_FAILURE, failure)) if v is not None}
    with pytest.raises(ValueError):
        LocalDoctorSettings.from_env(env)


@pytest.mark.parametrize("value", ["backups-sentinela/cq", "./backups-sentinela", "backups-sentinela"])
def test_local_settings_reject_relative_backup_dir_without_echo(value: str) -> None:
    """`CQ_BACKUP_DIR` relativo → `ValueError`, sem o valor na mensagem."""
    with pytest.raises(ValueError) as caught:
        LocalDoctorSettings.from_env({DOCTOR_ENV_BACKUP_DIR: value})
    assert "sentinela" not in str(caught.value)


# --- validador do corpo do servidor (as 3 globais novas) -------------------------------------------------------


def _item(check_id: str, result_: str, message: str, *, offering: str | None = None,
          count: int | None = None) -> dict:
    return {"id": check_id, "result": result_, "message": message, "offering": offering, "count": count}


def _reason(check_id: str, result_: str, reason: str) -> str:
    return DOCTOR_REASON_MESSAGES[(check_id, result_, reason)]


def _replace(item: dict) -> list[dict]:
    """O corpo válido de referência com `item` no lugar da global de mesmo id."""
    return [item if c["id"] == item["id"] else c for c in VALID_SERVER]


ACCEPTED_NEW = {
    "backup-nao-configurado": _item("backup.age", "warning", _reason("backup.age", "warning", "not_configured")),
    "backup-ilegivel": _item("backup.age", "failure", _reason("backup.age", "failure", "unreadable")),
    "backup-ok-0h": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=0), count=0),
    "backup-ok-26h": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=26), count=26),
    "backup-aviso-30h": _item("backup.age", "warning", BACKUP_AGE_TEMPLATE.format(hours=30), count=30),
    "backup-falha-51h": _item("backup.age", "failure", BACKUP_AGE_TEMPLATE.format(hours=51), count=51),
    "smtp-nunca": _item("smtp.server", "warning", _reason("smtp.server", "warning", "never")),
    "smtp-nao-configurado": _item("smtp.server", "warning", _reason("smtp.server", "warning", "not_configured")),
    "smtp-antigo": _item("smtp.server", "warning", _reason("smtp.server", "warning", "stale")),
    "smtp-conexao": _item("smtp.server", "failure", _reason("smtp.server", "failure", "connection")),
    "smtp-tls": _item("smtp.server", "failure", _reason("smtp.server", "failure", "tls")),
    "smtp-auth": _item("smtp.server", "failure", _reason("smtp.server", "failure", "auth")),
    "smtp-ok": _item("smtp.server", "ok", _reason("smtp.server", "ok", "ok")),
    "retencao-ok": _item("retention.overdue", "ok", _reason("retention.overdue", "ok", "ok"), count=0),
    "retencao-1": _item("retention.overdue", "warning", _reason("retention.overdue", "warning", "overdue"),
                        offering="turma-velha", count=1),
    "retencao-12": _item("retention.overdue", "warning", _reason("retention.overdue", "warning", "overdue"),
                         offering="t" * 40, count=12),
    # A chave-mestra que não decifra os tokens gravados.
    "chave-mestra-nao-decifra": _item("bots.master_key", "failure",
                                      _reason("bots.master_key", "failure", "token_unreadable")),
}


@pytest.mark.parametrize("item", list(ACCEPTED_NEW.values()), ids=list(ACCEPTED_NEW))
def test_server_body_with_each_new_global_result_passes(item: dict) -> None:
    """Cada resultado das 3 globais novas, com a mensagem do motivo, o `count` e o `offering` no
    lugar certo, e a "falha" de `bots.master_key` (a chave-mestra que não decifra), passa como veio; "falha" dá o código 1.
    As tabelas dos dois lados batem: o `SERVER_REASON_MESSAGES` do cliente é exatamente o conjunto das
    mensagens do servidor por `(id, result)`, e nenhum par dele está no `SERVER_MESSAGES`."""
    by_pair: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for (check_id, result_, _), message in DOCTOR_REASON_MESSAGES.items():
        by_pair[(check_id, result_)].add(message)
    assert {pair: frozenset(messages) for pair, messages in by_pair.items()} == dict(SERVER_REASON_MESSAGES)
    assert set(doctor.SERVER_MESSAGES).isdisjoint(SERVER_REASON_MESSAGES)
    assert BACKUP_AGE_TEMPLATE == DOCTOR_REASON_MESSAGES[("backup.age", "ok", "age")]

    server = _replace(item)
    checks = _collect(FakeApi(doctor_response=(200, _doctor_body(server))))
    assert checks == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"), _ok("api.access"),
                      *_server_passed(server)]
    assert doctor.exit_code(checks) == (1 if item["result"] == "failure" else 0)


REJECTED_NEW = {
    # backup.age: o modelo só com as horas do `count`, e o `count` só pelo motivo da idade.
    "backup-modelo-sem-formatar": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE, count=3),
    "backup-horas-diferentes-do-count": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=3), count=4),
    "backup-idade-sem-count": _item("backup.age", "warning", BACKUP_AGE_TEMPLATE.format(hours=30)),
    "backup-count-negativo": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=-1), count=-1),
    "backup-count-booleano": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=True), count=True),
    "backup-nao-configurado-com-count": _item("backup.age", "warning",
                                              _reason("backup.age", "warning", "not_configured"), count=5),
    "backup-mensagem-de-outro-resultado": _item("backup.age", "ok", _reason("backup.age", "warning", "not_configured")),
    "backup-com-offering": _item("backup.age", "ok", BACKUP_AGE_TEMPLATE.format(hours=3), count=3,
                                 offering="turma-a"),
    "backup-mensagem-livre": _item("backup.age", "failure", "resposta-da-api-sentinela-Rk42"),
    # smtp.server: só as mensagens do `(id, result)`; nem `count` nem `offering`.
    "smtp-mensagem-de-falha-no-aviso": _item("smtp.server", "warning", _reason("smtp.server", "failure", "auth")),
    "smtp-com-count": _item("smtp.server", "ok", _reason("smtp.server", "ok", "ok"), count=1),
    "smtp-com-offering": _item("smtp.server", "ok", _reason("smtp.server", "ok", "ok"), offering="turma-a"),
    "smtp-resultado-do-cliente": _item("smtp.server", "not_verified", _reason("smtp.server", "warning", "never")),
    # retention.overdue: nunca "falha"; `count` 0 e sem `offering` com "ok"; `count ≥ 1` e o código com "aviso".
    "retencao-falha": _item("retention.overdue", "failure", _reason("retention.overdue", "warning", "overdue"),
                            offering="turma-velha", count=1),
    "retencao-ok-com-vencidos": _item("retention.overdue", "ok", _reason("retention.overdue", "ok", "ok"), count=1),
    "retencao-ok-com-offering": _item("retention.overdue", "ok", _reason("retention.overdue", "ok", "ok"), count=0,
                                      offering="turma-velha"),
    "retencao-ok-sem-count": _item("retention.overdue", "ok", _reason("retention.overdue", "ok", "ok")),
    "retencao-aviso-sem-vencidos": _item("retention.overdue", "warning",
                                         _reason("retention.overdue", "warning", "overdue"), offering="turma-velha",
                                         count=0),
    "retencao-aviso-sem-offering": _item("retention.overdue", "warning",
                                         _reason("retention.overdue", "warning", "overdue"), count=2),
    "retencao-offering-fora-do-padrao": _item("retention.overdue", "warning",
                                              _reason("retention.overdue", "warning", "overdue"),
                                              offering="resposta-da-api-sentinela-Rk42", count=2),
    "retencao-count-negativo": _item("retention.overdue", "warning",
                                     _reason("retention.overdue", "warning", "overdue"), offering="turma-velha",
                                     count=-1),
    "retencao-mensagem-trocada": _item("retention.overdue", "warning", _reason("retention.overdue", "ok", "ok"),
                                       offering="turma-velha", count=2),
    # bots.master_key: a "falha" só com a mensagem nova, sem `count` nem `offering`; a mensagem nova só na
    # "falha".
    "chave-mestra-falha-com-a-mensagem-do-aviso": _item("bots.master_key", "failure",
                                                        DOCTOR_MESSAGES[("bots.master_key", "warning")]),
    "chave-mestra-falha-mensagem-livre": _item("bots.master_key", "failure", "resposta-da-api-sentinela-Rk42"),
    "chave-mestra-ok-com-a-mensagem-da-falha": _item("bots.master_key", "ok",
                                                     _reason("bots.master_key", "failure", "token_unreadable")),
    "chave-mestra-falha-com-count": _item("bots.master_key", "failure",
                                          _reason("bots.master_key", "failure", "token_unreadable"), count=1),
    "chave-mestra-falha-com-offering": _item("bots.master_key", "failure",
                                             _reason("bots.master_key", "failure", "token_unreadable"),
                                             offering="turma-a"),
}

REJECTED_BODIES = {
    **{name: _replace(item) for name, item in REJECTED_NEW.items()},
    # `count` e `offering` continuam fora do lugar nas outras globais.
    "count-em-clock-skew": [dict(c, count=1) if c["id"] == "clock.skew" else c for c in VALID_SERVER],
    "offering-em-tasks-overdue": [dict(c, offering="turma-a") if c["id"] == "tasks.overdue" else c
                                  for c in VALID_SERVER],
    # Cada global exatamente uma vez (as 6).
    "sem-backup-age": [GLOBAL, OVERDUE, CLOCK, SMTP, RETENTION],
    "sem-smtp-server": [GLOBAL, OVERDUE, CLOCK, BACKUP, RETENTION],
    "sem-retention-overdue": [GLOBAL, OVERDUE, CLOCK, BACKUP, SMTP],
    "sem-clock-skew": [GLOBAL, OVERDUE, BACKUP, SMTP, RETENTION],
    "retention-repetida-com-outro-resultado": [
        *VALID_SERVER, _item("retention.overdue", "warning", _reason("retention.overdue", "warning", "overdue"),
                             offering="turma-velha", count=1)],
    "backup-repetido-com-outro-resultado": [*VALID_SERVER, ACCEPTED_NEW["backup-nao-configurado"]],
}


@pytest.mark.parametrize("server", list(REJECTED_BODIES.values()), ids=list(REJECTED_BODIES))
def test_server_body_new_rules_out_of_contract_is_api_error(server: list[dict]) -> None:
    """O corpo fora das regras novas vira "erro da API", com as do servidor
    "não verificado" e o código 1, sem repassar nada do corpo. O controle positivo, na mesma função: o corpo
    válido de referência passa."""
    reference = _collect(FakeApi(doctor_response=(200, _doctor_body(VALID_SERVER))))
    assert reference[3] == _ok("api.access"), "controle positivo: o corpo válido de referência foi recusado"

    checks = _collect(FakeApi(doctor_response=(200, _doctor_body(server))))
    assert checks[:4] == [_ok("api.reachable"), _ok("database.reachable"), _ok("database.schema"),
                          Check("api.access", "api", "failure", API_ACCESS_FAILURE_MESSAGES["api_error"])]
    assert [(c.id, c.result) for c in checks[4:]] == [(i, "not_verified") for i in doctor.SERVER_CHECK_IDS]
    assert doctor.exit_code(checks) == 1
    for check in checks:
        assert "sentinela" not in check.message and "turma-velha" not in check.message


# --- DockerHostProbe (o real, com o ScriptedRunner) -----------------------------------------------------------

CONTAINER_ID = "4f2a9c0d1e3b"
PS_ARGV_TAIL = ["ps", "-q", "postgres"]
INFO_ARGV = ["docker", "info", "--format", "{{.DockerRootDir}}"]
# O ambiente do usuário: o probe passa `docker_env(env)` (sem `COMPOSE_*` nem as variáveis do CLI).
USER_ENV = {"PATH": "/usr/bin", "COMPOSE_PROJECT_NAME": "outro", "CQ_TOKEN": "cq_" + "s" * 43, "LANG": "pt_BR.UTF-8"}
MOUNTS_JSON = json.dumps([
    {"Type": "volume", "Name": "cq_pgdata", "Source": "/var/lib/docker/volumes/cq_pgdata/_data",
     "Destination": "/var/lib/postgresql", "Driver": "local", "Mode": "z", "RW": True, "Propagation": ""},
    {"Type": "bind", "Source": "/srv/cq", "Destination": "/backup", "Mode": "", "RW": True,
     "Propagation": "rprivate"},
]) + "\n"


def _ps_argv(stack_dir: pathlib.Path, project: str = PROJECT) -> list[str]:
    return [*compose_prefix(project, stack_dir), *PS_ARGV_TAIL]


def _inspect_argv(container: str = CONTAINER_ID) -> list[str]:
    return ["docker", "inspect", "--format", "{{json .Mounts}}", container]


def _assert_calls_env_and_timeout(runner: ScriptedRunner, timeout: float) -> None:
    expected_env = docker_env(USER_ENV)
    assert runner.calls, "o probe não chamou o executor"
    for call in runner.calls:
        assert call.env == expected_env, f"env diferente de docker_env(env) em {list(call.argv)}"
        assert call.timeout == timeout


def test_docker_host_probe_postgres_mounts_argv_env_and_parse(tmp_path: pathlib.Path) -> None:
    """`postgres_mounts(project)` roda `docker compose -p <projeto> -f <dir>/compose.yaml --env-file
    <dir>/.env ps -q postgres` (o prefixo comum) e `docker inspect --format '{{json .Mounts}}' <id>`, nessa ordem e só
    eles, cada um com `env=docker_env(env)` e o prazo padrão; lê as montagens para `Mount` (sem `Name` → `None`)."""
    stack_dir = tmp_path / "pilha"
    runner = ScriptedRunner({tuple(_ps_argv(stack_dir)): result(CONTAINER_ID + "\n"),
                             tuple(_inspect_argv()): result(MOUNTS_JSON)})
    probe = DockerHostProbe(runner, stack_dir, env=USER_ENV)
    assert probe.postgres_mounts(PROJECT) == (Mount(type="volume", name="cq_pgdata", destination="/var/lib/postgresql"),
                                              Mount(type="bind", name=None, destination="/backup"))
    assert runner.argvs == [_ps_argv(stack_dir), _inspect_argv()]
    _assert_calls_env_and_timeout(runner, HOST_PROBE_TIMEOUT_SECONDS)
    assert HOST_PROBE_TIMEOUT_SECONDS == 10


def test_docker_host_probe_uses_given_project_and_timeout(tmp_path: pathlib.Path) -> None:
    """O projeto vem do argumento e o prazo do construtor."""
    stack_dir = tmp_path / "pilha"
    runner = ScriptedRunner({tuple(_ps_argv(stack_dir, "cq-piloto")): result(CONTAINER_ID + "\n"),
                             tuple(_inspect_argv()): result("[]\n")})
    assert DockerHostProbe(runner, stack_dir, env=USER_ENV, timeout=3).postgres_mounts("cq-piloto") == ()
    _assert_calls_env_and_timeout(runner, 3)


def test_docker_host_probe_postgres_not_running_skips_inspect(tmp_path: pathlib.Path) -> None:
    """`ps -q` sem nenhuma linha (serviço parado ou ausente) → `None`, sem chamar o `inspect`."""
    stack_dir = tmp_path / "pilha"
    runner = ScriptedRunner({tuple(_ps_argv(stack_dir)): result("")})
    assert DockerHostProbe(runner, stack_dir, env=USER_ENV).postgres_mounts(PROJECT) is None
    assert runner.argvs == [_ps_argv(stack_dir)]


def _failing_inspect(kind: str) -> Any:
    return {
        "codigo": result(b"", returncode=1, stderr=b"Error: No such object"),
        "json-invalido": result("{nao-e-json"),
        "json-nao-lista": result('{"Type": "volume"}'),
        "item-sem-destino": result('[{"Type": "volume", "Name": "cq_pgdata"}]'),
        "docker-indisponivel": DockerUnavailable(),
        "oserror": OSError("sem docker"),
        "prazo": subprocess.TimeoutExpired(["docker"], 10),
    }[kind]


@pytest.mark.parametrize("kind", ["codigo", "json-invalido", "json-nao-lista", "item-sem-destino",
                                  "docker-indisponivel", "oserror", "prazo"])
def test_docker_host_probe_postgres_mounts_none_on_bad_inspect(tmp_path: pathlib.Path, kind: str) -> None:
    """Código ≠ 0, saída inválida ou exceção do executor no `inspect` → `None`, nunca exceção."""
    stack_dir = tmp_path / "pilha"
    runner = ScriptedRunner({tuple(_ps_argv(stack_dir)): result(CONTAINER_ID + "\n"),
                             tuple(_inspect_argv()): _failing_inspect(kind)})
    assert DockerHostProbe(runner, stack_dir, env=USER_ENV).postgres_mounts(PROJECT) is None


@pytest.mark.parametrize("response", [result(b"", returncode=1), DockerUnavailable(), OSError("x"),
                                      subprocess.TimeoutExpired(["docker"], 10)],
                         ids=["codigo", "docker-indisponivel", "oserror", "prazo"])
def test_docker_host_probe_postgres_mounts_none_on_bad_ps(tmp_path: pathlib.Path, response: Any) -> None:
    """Falha do `ps -q` → `None`, sem chamar o `inspect`."""
    stack_dir = tmp_path / "pilha"
    runner = ScriptedRunner({tuple(_ps_argv(stack_dir)): response})
    assert DockerHostProbe(runner, stack_dir, env=USER_ENV).postgres_mounts(PROJECT) is None
    assert runner.argvs == [_ps_argv(stack_dir)]


def test_docker_host_probe_docker_root_dir(tmp_path: pathlib.Path) -> None:
    """`docker_root_dir()` roda só `docker info --format '{{.DockerRootDir}}'`, com `env=docker_env(env)` e o prazo,
    e devolve o caminho sem espaços em volta."""
    runner = ScriptedRunner({tuple(INFO_ARGV): result(ROOT_DIR + "\n")})
    assert DockerHostProbe(runner, tmp_path / "pilha", env=USER_ENV).docker_root_dir() == ROOT_DIR
    assert runner.argvs == [INFO_ARGV]
    _assert_calls_env_and_timeout(runner, HOST_PROBE_TIMEOUT_SECONDS)


@pytest.mark.parametrize("response", [result(b"", returncode=1), result("\n"), DockerUnavailable(), OSError("x"),
                                      subprocess.TimeoutExpired(["docker"], 10)],
                         ids=["codigo", "vazio", "docker-indisponivel", "oserror", "prazo"])
def test_docker_host_probe_docker_root_dir_none_on_failure(tmp_path: pathlib.Path, response: Any) -> None:
    """Docker inalcançável, saída vazia ou exceção do executor → `None`."""
    runner = ScriptedRunner({tuple(INFO_ARGV): response})
    assert DockerHostProbe(runner, tmp_path / "pilha", env=USER_ENV).docker_root_dir() is None


_DiskUsageTuple = collections.namedtuple("_DiskUsageTuple", "total used free")


def test_docker_host_probe_disk_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """`disk_usage(path)` lê `shutil.disk_usage(path)` (trocado aqui por `monkeypatch`), sem o executor;
    `OSError` (o caminho inexistente neste host, como o `DockerRootDir` do Docker Desktop) → `None`."""
    import shutil

    seen: list[str] = []

    def fake_disk_usage(path: Any) -> _DiskUsageTuple:
        seen.append(str(path))
        if str(path) == ROOT_DIR:
            return _DiskUsageTuple(total=100 * GIB, used=40 * GIB, free=60 * GIB)
        if str(path) == BACKUP_DIR:
            raise PermissionError("sem acesso")
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)
    probe = DockerHostProbe(ScriptedRunner({}), tmp_path / "pilha", env=USER_ENV)
    assert probe.disk_usage(ROOT_DIR) == DiskUsage(total=100 * GIB, free=60 * GIB)
    assert probe.disk_usage("/inexistente") is None
    assert probe.disk_usage(BACKUP_DIR) is None
    assert seen == [ROOT_DIR, "/inexistente", BACKUP_DIR]
