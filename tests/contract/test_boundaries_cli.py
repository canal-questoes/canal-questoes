"""Fronteiras do CLI (princípio "Portas e contratos";
núcleo sem HTTP e sem adaptadores; o SQL do expurgo sobre identidades; o import preguiçoso de `click`
no perfil do cliente).

Arquivo à parte: `tests/contract/test_boundaries.py` (e os outros de fronteira) ficam congelados e sem edição, e os
verificadores deles são reusados aqui. Cobre:
- os 2 módulos do núcleo (`cq_server.purge` e `cq_server.doctor`), em duas checagens separadas: (1)
  direta, por AST, sem HTTP, `subprocess`, `socket` nem `cq_server.adapters` (o `FORBIDDEN` congelado mais
  `socket`, como nas outras fronteiras); (2) transitiva, num processo novo, só contra o `TRANSITIVE_FORBIDDEN`
  congelado, **sem** `subprocess` nem `socket` (**[F]** importar `cq_server.offerings`, `cq_server.scheduler` e
  `cq_server.people` já põe os dois em `sys.modules`, e o verificador congelado deixa a biblioteca padrão fora
  da transitiva de propósito); nenhum dos dois cita `identities` em SQL (o alcance, o `FOR UPDATE` e o
  `DELETE` ficam em `cq_server.people`), com controle positivo (o mesmo verificador acha `identities` em
  `cq_server.people`);
- os módulos do cliente (`profile`, `hint`, `stack`, `stack_compose`, `local_commands`, `doctor`, `api`) sem
  `cq_server` e, exceto o `cli`, sem `click`, em duas formas: (1) estática, sem `import click` no nível
  do módulo (o import preguiçoso dentro de `profile.default_config_path` é o único permitido); (2)
  transitiva, por sonda de `sys.modules` num subprocesso que importa `cq_client.profile`, `cq_client.hint` e
  `cq_client.doctor` (os que recebem o `EffectiveProfile`), com o controle positivo da sonda (importar
  `cq_client.cli` põe `click` em `sys.modules`); só `stack_compose` importa `subprocess` no pacote cliente;
- a **ligação**: `confirm_purge` apaga e trava as identidades só por `people.lock_identities` e
  `people.delete_identities`; a rota `system.doctor` chama `doctor.collect` com o `DoctorSettings` injetado em
  `create_app`; e `cq up` chama `local_commands.run_up` com o `Stack` do `ctx.obj`.

Falha esperada no esqueleto (nenhuma função passa no esqueleto): as checagens
estruturais já valem no esqueleto, e por isso ficam na **mesma função** de uma ligação, que
falha até a implementação: a rota de preparação do expurgo não existe (404) e `confirm_purge` lança
`NotImplementedError`; a rota `system.doctor` não existe (404); e o `cq` não tem a folha
`up`.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from tests.contract.test_boundaries import (
    FORBIDDEN,
    REPO,
    TRANSITIVE_FORBIDDEN,
    _identities_sql,
    _imported_names,
    _is_forbidden,
    _module_files,
)
from tests.contract.test_catalog import _imports_server

# O `FORBIDDEN` congelado mais `socket` (a rede só nos adaptadores; como nas outras fronteiras).
CLI_FORBIDDEN = FORBIDDEN + ("socket",)
DOCTOR_PURGE_CORE_MODULES = ("purge", "doctor")

CLIENT_DIR = REPO / "client" / "src" / "cq_client"
# Perfis, pilha e `doctor` no cliente: os módulos do cliente que não podem depender de
# `cq_server` nem (exceto o `cli`) de `click`.
CLIENT_MODULES = ("profile", "hint", "stack", "stack_compose", "local_commands", "doctor", "api")
# o único `import click` permitido fora do `cli`, preguiçoso, dentro desta função.
LAZY_CLICK = {"cq_client.profile": ["default_config_path"]}
# Os módulos que recebem o `EffectiveProfile` (a sonda transitiva do `click`).
PROFILE_RECEIVERS = ("cq_client.profile", "cq_client.hint", "cq_client.doctor")


def _cli_violations(source: str, modname: str, is_pkg: bool = False) -> list[tuple[int, str]]:
    return sorted({(line, name) for line, name in _imported_names(source, modname, is_pkg)
                   if _is_forbidden(name, CLI_FORBIDDEN)})


def _loaded_by(modules: list[str]) -> list[str]:
    """`sys.modules` depois de importar `modules` num processo novo (sem o `PYTHONSTARTUP` do desenvolvedor)."""
    code = ("import importlib, json, sys\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    importlib.import_module(m)\n"
            "print(json.dumps(sorted(sys.modules)))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"}
    result = subprocess.run([sys.executable, "-c", code, json.dumps(modules)], cwd=REPO, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _core_source(module: str) -> tuple[str, str]:
    files = _module_files(module)
    assert [modname for modname, _, _ in files] == [f"cq_server.{module}"], (
        f"módulo do núcleo cq_server.{module} não encontrado como arquivo único (a estrutura do repositório)")
    modname, path, _ = files[0]
    return modname, path.read_text(encoding="utf-8")


def _assert_core_module_boundaries(module: str) -> None:
    """As duas checagens separadas do núcleo (direta por AST e transitiva num processo novo) e o SQL de
    `identities` fora do módulo, com os controles positivos."""
    modname, source = _core_source(module)
    assert _cli_violations(source, modname) == [], f"{modname} importa o proibido"
    for snippet, expected in (("import socket", "socket"), ("from subprocess import run", "subprocess"),
                              ("from cq_server.adapters.console import ConsoleChannel", "cq_server.adapters.console"),
                              ("def f():\n    import httpx2\n    return httpx2", "httpx2"),
                              ("from .api import errors", "cq_server.api.errors")):
        assert expected in {name for _, name in _cli_violations(snippet, modname)}, snippet

    loaded = _loaded_by([modname])
    assert modname in loaded
    assert [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)] == []
    # A transitiva não cobra a biblioteca padrão (o verificador congelado a deixa de fora de propósito).
    assert not _is_forbidden("subprocess", TRANSITIVE_FORBIDDEN) and not _is_forbidden("socket", TRANSITIVE_FORBIDDEN)

    assert _identities_sql(source) == [], f"{modname} cita identities em SQL"
    _, people_source = _core_source("people")
    assert _identities_sql(people_source), "controle positivo: o verificador não achou identities em cq_server.people"


def _click_imports(source: str) -> list[str | None]:
    """Cada `import click` (ou `from click …`) do código, com o nome da função que o contém (`None` no nível do
    módulo, inclusive sob `if TYPE_CHECKING:` e dentro de classes fora de função)."""
    found: list[str | None] = []

    def walk(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Import) and any(a.name.split(".")[0] == "click" for a in child.names):
                found.append(function)
            elif (isinstance(child, ast.ImportFrom) and child.level == 0
                  and (child.module or "").split(".")[0] == "click"):
                found.append(function)
            walk(child, function)

    walk(ast.parse(source), None)
    return found


def _client_sources() -> dict[str, str]:
    """Nome qualificado → código de todo módulo `.py` do pacote `cq_client`."""
    sources: dict[str, str] = {}
    for path in sorted(CLIENT_DIR.rglob("*.py")):
        parts = ["cq_client", *path.relative_to(CLIENT_DIR).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        sources[".".join(parts)] = path.read_text(encoding="utf-8")
    return sources


# ---------------------------------------------------------------------------------------------
# `purge`: fronteiras e a ligação com `people`
# ---------------------------------------------------------------------------------------------


def test_purge_core_imports_nothing_forbidden_and_deletes_identities_only_through_people(
        monkeypatch: pytest.MonkeyPatch, client: Any, sql: Any, clock: Any, bot_api: Any) -> None:
    """Portas e contratos; o SQL do expurgo sobre identidades.

    Estrutura (já vale no esqueleto): `cq_server.purge` não importa HTTP, `subprocess`, `socket`, a API nem os
    adaptadores (AST), não os carrega num processo novo (só o `TRANSITIVE_FORBIDDEN` congelado) e não cita
    `identities` em SQL; controles positivos: a checagem acusa cada forma de importação proibida no módulo, e o
    mesmo verificador de SQL acha `identities` em `cq_server.people`.

    A ligação: num oferecimento da semente do expurgo (`tests/helpers/purge.py`), com dublês de
    `people.lock_identities` e `people.delete_identities` por `monkeypatch` (registram a chamada e chamam a
    função real), a confirmação pela API trava as identidades de todas as pessoas afetadas (matrícula no
    oferecimento e identidade), antes de apagar, e apaga, numa única chamada, exatamente as identidades a
    apagar (as da semente, `deleted`), e nenhuma outra linha de `identities` some.

    Vermelho no esqueleto: a rota de preparação do expurgo não existe (404), e `confirm_purge`
    lança `NotImplementedError`."""
    _assert_core_module_boundaries("purge")

    from cq_server import people
    from tests.helpers.api import ADMIN_TOKEN, auth
    from tests.helpers.purge import seed_purge_scenario

    scenario = seed_purge_scenario(client, sql)
    affected = [row[0] for row in sql.execute(
        "SELECT DISTINCT e.person_id FROM enrollments e JOIN identities i ON i.person_id = e.person_id "
        "WHERE e.offering_id = %s ORDER BY 1", (scenario.offering_row,)).fetchall()]
    to_delete = sorted(p.person_row for p in scenario.people.values() if p.deleted)
    assert to_delete and len(to_delete) == scenario.outcome.identities_deleted
    assert set(to_delete) < set(affected), "a semente precisa de pessoas mantidas e apagadas"
    others = [row[0] for row in sql.execute(
        "SELECT person_id FROM identities WHERE person_id <> ALL(%s) ORDER BY person_id", (to_delete,)).fetchall()]

    prepared = client.post(f"/v1/offerings/{scenario.code}/purge/prepare", headers=auth(ADMIN_TOKEN))
    assert prepared.status_code == 200, f"preparo do expurgo falhou: {prepared.status_code} {prepared.text[:300]}"

    calls: list[tuple[str, list[int]]] = []
    real_lock, real_delete = people.lock_identities, people.delete_identities

    def lock_double(conn: Any, person_ids: Any) -> Any:
        calls.append(("lock", sorted(person_ids)))
        return real_lock(conn, person_ids)

    def delete_double(conn: Any, person_ids: Any) -> Any:
        calls.append(("delete", sorted(person_ids)))
        return real_delete(conn, person_ids)

    monkeypatch.setattr(people, "lock_identities", lock_double)
    monkeypatch.setattr(people, "delete_identities", delete_double)
    resp = client.post(f"/v1/offerings/{scenario.code}/purge",
                       json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"confirmação do expurgo falhou: {resp.status_code} {resp.text[:300]}"

    kinds = [kind for kind, _ in calls]
    assert kinds.count("delete") == 1 and kinds[-1] == "delete", calls
    assert sorted(i for kind, ids in calls if kind == "lock" for i in ids) == affected, calls
    assert calls[-1] == ("delete", to_delete)
    assert sql.execute("SELECT count(*) FROM identities WHERE person_id = ANY(%s)", (to_delete,)).fetchone()[0] == 0
    assert [row[0] for row in sql.execute("SELECT person_id FROM identities ORDER BY person_id").fetchall()] == others


# ---------------------------------------------------------------------------------------------
# `doctor`: fronteiras e a ligação com a rota
# ---------------------------------------------------------------------------------------------


def test_doctor_core_imports_nothing_forbidden_and_route_uses_injected_settings(
        monkeypatch: pytest.MonkeyPatch, settings: Any, clock: Any, bots: Any) -> None:
    """Portas e contratos; o diagnóstico na API (`create_app(...,
    doctor_settings=...)`, que não lê o ambiente).

    Estrutura (já vale no esqueleto): `cq_server.doctor` não importa HTTP, `subprocess`, `socket`, a API nem os
    adaptadores (AST), não os carrega num processo novo e não cita `identities` em SQL, com os mesmos controles
    positivos do expurgo.

    A ligação: com um `DoctorSettings` próprio injetado em `create_app` e um dublê de `doctor.collect` por
    `monkeypatch` (registra os argumentos e devolve nenhuma verificação), `GET /v1/system/doctor` chama
    `collect` uma vez, com o mesmo objeto de configuração, o horário do relógio da API e `master_key_configured`
    booleano, e responde 200 com `{checked_at: <o mesmo horário>, checks: []}`, válido contra o esquema.

    Vermelho no esqueleto: a rota `system.doctor` não existe (404)."""
    _assert_core_module_boundaries("doctor")

    from fastapi.testclient import TestClient

    from cq_server import doctor
    from cq_server.api import create_app
    from tests.helpers.api import ADMIN_TOKEN, auth
    from tests.helpers.calls import TEST_REPO_SETTINGS
    from tests.helpers.clock import iso
    from tests.helpers.contract import assert_matches

    injected = doctor.DoctorSettings(bot_poll_seconds=120, task_overdue_seconds=240, clock_skew_seconds=2.5)
    recorded: list[dict[str, Any]] = []

    def collect_double(conn: Any, *, now: Any, master_key_configured: Any, settings: Any) -> list:
        recorded.append({"now": now, "master_key_configured": master_key_configured, "settings": settings})
        return []

    monkeypatch.setattr(doctor, "collect", collect_double)
    app = create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS, doctor_settings=injected)
    with TestClient(app, raise_server_exceptions=False) as api:
        resp = api.get("/v1/system/doctor", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"system.doctor: {resp.status_code} {resp.text[:300]}"
    assert len(recorded) == 1, "a rota system.doctor não chamou doctor.collect"
    call = recorded[0]
    assert call["settings"] is injected
    assert call["now"] == clock.now()
    assert isinstance(call["master_key_configured"], bool)
    body = resp.json()
    assert_matches("system.doctor", 200, body)
    assert body == {"checked_at": iso(clock.now()), "checks": []}


# ---------------------------------------------------------------------------------------------
# O cliente: sem `cq_server`, sem `click` fora do `cli`, e a ligação do `up`
# ---------------------------------------------------------------------------------------------


def test_client_modules_avoid_server_and_click_and_up_runs_through_local_commands(
        monkeypatch: pytest.MonkeyPatch, client: Any, tmp_path: pathlib.Path) -> None:
    """Portas e contratos; pilha, cliente da API e CLI; o import preguiçoso de `click` no perfil.

    Estrutura (já vale no esqueleto): os módulos `profile`, `hint`, `stack`, `stack_compose`, `local_commands`,
    `doctor` e `api` do cliente não importam `cq_server` (o verificador de `test_catalog.py`); nenhum deles tem
    `import click` no nível do módulo (o único `click` fora do `cli` é o preguiçoso de
    `profile.default_config_path`), com o controle positivo no `cli`, que o importa no nível do módulo; no
    pacote cliente inteiro, só `stack_compose` importa `subprocess`; num subprocesso, importar
    `cq_client.profile`, `cq_client.hint` e `cq_client.doctor` não carrega `click` nem `cq_server`, e o controle
    positivo da sonda (importar `cq_client.cli`) carrega `click`.

    A ligação: com o perfil local de teste (o endereço de loopback com porta explícita) e um `FakeStack`
    no `ctx.obj`, `cq up` chama `local_commands.run_up` uma vez com esse `Stack` (um dublê por `monkeypatch`
    registra a chamada e devolve um `UpSummary` saudável) e termina com o código 0.

    Vermelho no esqueleto: o `cq` não tem a folha `up`."""
    sources = _client_sources()
    assert {f"cq_client.{m}" for m in CLIENT_MODULES} | {"cq_client.cli"} <= set(sources)
    for module in CLIENT_MODULES:
        modname = f"cq_client.{module}"
        tree = ast.parse(sources[modname])
        assert _imports_server(tree) == [], f"{modname} importa cq_server"
        assert _click_imports(sources[modname]) == LAZY_CLICK.get(modname, []), (
            f"{modname} importa click fora do permitido: {_click_imports(sources[modname])}")
    assert None in _click_imports(sources["cq_client.cli"]), "controle positivo: o cli importa click no módulo"
    assert _click_imports("import click\n") == [None]
    assert _click_imports("def f():\n    import click\n    return click\n") == ["f"]
    assert _click_imports("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from click import Context\n") == [
        None]
    with_subprocess = sorted(modname for modname, source in sources.items()
                             if any(name.split(".")[0] == "subprocess"
                                    for _, name in _imported_names(source, modname, modname == "cq_client")))
    assert with_subprocess == ["cq_client.stack_compose"]

    loaded = _loaded_by(list(PROFILE_RECEIVERS))
    assert set(PROFILE_RECEIVERS) <= set(loaded)
    assert "click" not in loaded, "importar os módulos que recebem o EffectiveProfile carregou click"
    assert [m for m in loaded if m == "cq_server" or m.startswith("cq_server.")] == []
    assert "click" in _loaded_by(["cq_client.cli"]), "controle positivo da sonda: o cli não carregou click"

    # A ligação.
    import cq_client.cli as cq_cli
    from cq_client import local_commands
    from tests.helpers.api import ADMIN_TOKEN
    from tests.helpers.cli_runner import invoke_cli, make_stack_dir, write_profile
    from tests.helpers.clock import FakeClock
    from tests.helpers.stack_fake import TEST_API_PORT, TEST_API_URL, FakeSleep, FakeStack, healthy

    stack_dir = make_stack_dir(tmp_path)
    config = write_profile(tmp_path, (
        'default_profile = "local"\n\n'
        "[profiles.local]\n"
        'kind = "local"\n'
        f'api_url = "{TEST_API_URL}"\n'
        f'token = "{ADMIN_TOKEN}"\n'
        f'stack_dir = "{stack_dir.as_posix()}"\n'
        'compose_project = "cq"\n'))
    stack = FakeStack(["stopped", "healthy"])
    sleep = FakeSleep(FakeClock())
    recorded: list[dict[str, Any]] = []

    def run_up_double(*args: Any, **kwargs: Any) -> Any:
        recorded.append({"args": args, "kwargs": kwargs})
        return local_commands.UpSummary(project="cq", services=healthy().services, api_port=TEST_API_PORT)

    monkeypatch.setattr(local_commands, "run_up", run_up_double)
    # Se o `cli` guardar o nome, o dublê também vale lá (o contrato é "chama `local_commands.run_up`").
    monkeypatch.setattr(cq_cli, "run_up", run_up_double, raising=False)
    result = invoke_cli(["up"], obj={"http": client, "stack": stack, "clock": sleep.monotonic, "sleep": sleep},
                       config=config, tmp_path=tmp_path)
    assert len(recorded) == 1, f"cq up não chamou local_commands.run_up (código {result.exit_code})"
    call = recorded[0]
    given = call["kwargs"].get("stack", call["args"][0] if call["args"] else None)
    assert given is stack, "cq up não passou o Stack do ctx.obj a run_up"
    assert result.exit_code == 0, (result.exit_code, result.stderr[-500:])
