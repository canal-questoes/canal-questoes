"""CLI do `doctor`.


`cq doctor` (o diagnóstico): a composição completa (cliente + API dos testes
+ `FakeStack`), sem Docker real:
- perfil local com um token com `offerings:read`: tudo "ok" (as de backup, SMTP e retenção com o que o banco de teste
  dá), código 0; o `api_url` do perfil é de loopback com porta explícita igual ao `api_port`
  do `FakeStack` (`TEST_API_URL`), e a API dos testes chega por `http=` (`api.owner` depende da regra de
  pertencimento da pilha ao projeto);
- o relatório no stdout em texto (linhas `[<resultado>] <id>[ (<código>)]: <mensagem>[ (<count> | <detail>)]` e
  `Resultado: …`) e em `--json` contra `doctor.output.schema.json`; com o código 1, o relatório
  continua no stdout;
- perfil remoto: as locais "não se aplica", sem tocar a pilha;
- API inalcançável (pelo perfil de teste em `http://127.0.0.1:9`, nunca o padrão `127.0.0.1:8000`): código 1,
  nunca 3, o relatório no stdout, a linha da dica no stderr com o perfil local, **sem** a pergunta
  mesmo num terminal simulado; sem a dica com `--json` ou com `CQ_API_URL`;
- a saída sem identidade, segredo, URL de repositório, conteúdo do `.env` nem ambiente de contêiner.

Nas
saídas esperadas, nenhuma sai "não verificado" por reserva do CLI; as do servidor são 6 globais, com as
3 últimas no estado do banco de teste (a API dos testes sem `CQ_BACKUP_STATUS_FILE`, sem linha em `mail_probe` e sem
oferecimento arquivado): `backup.age` "aviso, não configurado", `smtp.server` "aviso, ainda não testado" e
`retention.overdue` "ok" com `count = 0`; `postgres.volume` e `disk.space` (`HOST_CHECK_IDS`, parte `local`) saem
depois das 10 locais do CLI: aqui sempre com dublê injetado (`stack`) e **sem** `host_probe` (o `invoke_cli` congelado
não o aceita), então "não verificado" pelo motivo `no_host_probe` (os casos com o `FakeHostProbe` ficam em
`test_cli_doctor_deploy.py`); no perfil remoto, as duas "não se aplica" com `NOT_APPLICABLE_REMOTE_VM`, depois das
10 locais com `NOT_APPLICABLE_REMOTE`. O ramo por plataforma de `profile.permissions` (`_permissions_result`) vale aqui:
no Windows, ela sai "não verificado" (as ACLs).

O relógio da API é alinhado ao do banco imediatamente antes de cada invocação que espera `clock.skew = ok`
(`align_clock_to_db`). Os textos esperados saem das tabelas fixas reais (`CLIENT_MESSAGES`,
`DOCTOR_MESSAGES`, `RESULT_LABELS`, `CHECK_LINE_TEMPLATE`, `SUMMARY_TEMPLATE`, `HINT_TEMPLATE`) e das escolhas
fixadas na implementação (as do servidor na ordem da API, `count = 0` em `tasks.overdue` ok). A linha fixa de resumo do
stderr no código 1 não tem texto fixo: aqui só se confere que ela existe, sem a
pergunta e sem dados sensíveis.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest

from cq_client.doctor import (
    CHECK_LINE_TEMPLATE,
    CLIENT_MESSAGES,
    HOST_CHECK_IDS,
    LOCAL_CHECK_IDS,
    NOT_APPLICABLE_REMOTE,
    NOT_APPLICABLE_REMOTE_VM,
    NOT_VERIFIED_MESSAGES,
    RESULT_LABELS,
    SERVER_CHECK_IDS,
    SUMMARY_TEMPLATE,
)
from cq_client.hint import HINT_TEMPLATE
from cq_client.local_commands import ASK_START_TEMPLATE
from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

from tests.helpers.api import ADMIN_TOKEN, create_token
from tests.helpers.cli_runner import (
    ENV_SENTINEL,
    ENV_SENTINEL_TOKEN,
    UNREACHABLE_API_URL,
    align_clock_to_db,
    invoke_cli,
    make_stack_dir,
    write_profile,
)
from tests.helpers.contract import assert_cli_json
from tests.helpers.stack_fake import ENGINE_IMAGE_ID, TEST_API_URL, FakeStack

REMOTE_URL = "https://cq.example.org"
ENGINE_DETAIL = ENGINE_IMAGE_ID.removeprefix("sha256:")[:12]
REPO_URL_MARK = "repos.cq.test"


# --- montagem ------------------------------------------------------------------------------------------------


def _toml_path(path: pathlib.Path) -> str:
    return "'" + str(path) + "'"


def _profile_file(tmp_path: pathlib.Path, *, token: str, api_url: str = TEST_API_URL,
                  stack_dir: pathlib.Path | None = None) -> pathlib.Path:
    """O arquivo de perfil de teste com o perfil local `local` e o remoto `remoto` (nunca o do desenvolvedor);
    restrito ao dono nos sistemas POSIX (o teste pode fazer `chmod`; o `doctor`, não)."""
    # O perfil implícito (chaves de topo) aponta para a API inalcançável: nenhum caminho cai no padrão
    # `127.0.0.1:8000`.
    lines = [f'api_url = "{UNREACHABLE_API_URL}"', "",
             '[profiles.local]', 'kind = "local"', f'api_url = "{api_url}"', f'token = "{token}"']
    if stack_dir is not None:
        lines.append(f"stack_dir = {_toml_path(stack_dir)}")
    lines += ["", "[profiles.remoto]", 'kind = "remote"', f'api_url = "{REMOTE_URL}"', f'token = "{token}"', ""]
    path = write_profile(tmp_path, "\n".join(lines))
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def _reader_token(client: Any) -> str:
    secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"])
    return secret


def _no_git_above(path: pathlib.Path) -> None:
    assert not any((p / ".git").exists() for p in path.resolve().parents), "tmp_path dentro de uma cópia git"


def _line(check_id: str, result: str, message: str, *, offering: str | None = None, count: int | None = None,
          detail: str | None = None) -> str:
    extra = f" ({count})" if count is not None else (f" ({detail})" if detail is not None else "")
    return CHECK_LINE_TEMPLATE.format(label=RESULT_LABELS[result], id=check_id,
                                      offering=f" ({offering})" if offering else "", message=message, extra=extra)


def _check(check_id: str, part: str, result: str, message: str, *, offering: str | None = None,
           count: int | None = None, detail: str | None = None) -> dict:
    return {"id": check_id, "part": part, "result": result, "message": message, "offering": offering,
            "count": count, "detail": detail}


def _permissions_result() -> str:
    return "not_verified" if os.name == "nt" else "ok"


def _new_globals() -> list[dict]:
    """As 3 globais de backup, SMTP e retenção do servidor no banco de teste: sem `CQ_BACKUP_STATUS_FILE`,
    sem linha em `mail_probe` e sem oferecimento arquivado."""
    return [_check("backup.age", "api", "warning", DOCTOR_REASON_MESSAGES[("backup.age", "warning", "not_configured")]),
            _check("smtp.server", "api", "warning", DOCTOR_REASON_MESSAGES[("smtp.server", "warning", "never")]),
            _check("retention.overdue", "api", "ok", DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")],
                   count=0)]


def _host_no_probe() -> list[dict]:
    """Volume e disco com dublê injetado e sem `host_probe`, depois das locais do CLI."""
    return [_check(i, "local", "not_verified", NOT_VERIFIED_MESSAGES["no_host_probe"]) for i in HOST_CHECK_IDS]


def _healthy_checks() -> list[dict]:
    """O relatório esperado da pilha saudável com o perfil local, num banco sem oferecimentos."""
    checks = [_check(i, "api", "ok", CLIENT_MESSAGES[(i, "ok")])
              for i in ("api.reachable", "database.reachable", "database.schema", "api.access")]
    checks += [_check("bots.master_key", "api", "ok", DOCTOR_MESSAGES[("bots.master_key", "ok")]),
               _check("tasks.overdue", "api", "ok", DOCTOR_MESSAGES[("tasks.overdue", "ok")], count=0),
               _check("clock.skew", "api", "ok", DOCTOR_MESSAGES[("clock.skew", "ok")])]
    checks += _new_globals()
    checks += [_check(i, "local", "ok", CLIENT_MESSAGES[(i, "ok")], detail=ENGINE_DETAIL if i == "engine.image" else None)
               for i in LOCAL_CHECK_IDS]
    checks += _host_no_probe()
    permissions = _permissions_result()
    checks += [_check("profile.permissions", "profile", permissions,
                      CLIENT_MESSAGES[("profile.permissions", permissions)]),
               _check("profile.git", "profile", "ok", CLIENT_MESSAGES[("profile.git", "ok")])]
    return checks


def _text(header: list[str], checks: list[dict]) -> str:
    lines = [*header]
    lines += [_line(c["id"], c["result"], c["message"], offering=c["offering"], count=c["count"],
                    detail=c["detail"]) for c in checks]
    failures = sum(1 for c in checks if c["result"] == "failure")
    warnings = sum(1 for c in checks if c["result"] == "warning")
    lines.append(SUMMARY_TEMPLATE.format(failures=failures, warnings=warnings))
    return "\n".join(lines) + "\n"


LOCAL_HEADER = [f"Perfil: local (local), API em {TEST_API_URL}, token definido.",
                "Origem: nome CQ_PROFILE, tipo declarado, endereço perfil, token perfil."]
LOCAL_VIEW = {"name": "local", "kind": "local", "api_url": TEST_API_URL, "token_set": True, "compose_project": "cq",
              "origins": {"name": "env", "kind": "profile", "api_url": "profile", "token": "profile",
                          "compose_project": "default"}}


def _assert_clean(result: Any, *secrets: str) -> None:
    """Nenhuma saída com identidade, segredo, conteúdo do `.env`, ambiente de contêiner, URL de repositório nem
    rastreamento."""
    for text in (result.stdout, result.stderr):
        for value in (ENV_SENTINEL, ENV_SENTINEL_TOKEN, ADMIN_TOKEN, "CQ_DB_PASSWORD", "CQ_ADMIN_TOKEN",
                      "CQ_MASTER_KEY", "POSTGRES_PASSWORD", REPO_URL_MARK, "Traceback", *secrets):
            assert value not in text, f"{value!r} na saída do doctor"


# --- casos -----------------------------------------------------------------------------------------------------


def test_doctor_local_healthy_all_ok_exit_0(client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path) -> None:
    """`doctor` com a pilha saudável, o perfil local e um token com `offerings:read`: todas as locais
    do CLI e as da API "ok" (as de backup, SMTP e retenção como o banco de teste as dá), volume e disco "não verificado"
    (`no_host_probe`), código 0; o texto linha a linha, sem stderr;
    o `FakeStack` só lido."""
    _no_git_above(tmp_path)
    token = _reader_token(client)
    stack_dir = make_stack_dir(tmp_path)
    config = _profile_file(tmp_path, token=token, stack_dir=stack_dir)
    stack = FakeStack(["healthy"])
    align_clock_to_db(clock, sql)
    result = invoke_cli(["doctor"], obj={"http": client, "stack": stack}, env={"CQ_PROFILE": "local"},
                       config=config)
    assert (result.exit_code, result.stderr) == (0, ""), result.stderr
    assert result.stdout == _text(LOCAL_HEADER, _healthy_checks())
    assert set(stack.names()) <= {"snapshot", "compose_version", "engine_image_id", "docker_available"}
    _assert_clean(result, token, str(stack_dir), str(config))


def test_doctor_local_healthy_json(client: Any, sql: Any, clock: Any, tmp_path: pathlib.Path) -> None:
    """O mesmo relatório em `--json`, contra o esquema estrito do CLI, igual campo a campo."""
    token = _reader_token(client)
    config = _profile_file(tmp_path, token=token, stack_dir=make_stack_dir(tmp_path))
    align_clock_to_db(clock, sql)
    result = invoke_cli(["doctor"], json=True, obj={"http": client, "stack": FakeStack(["healthy"])},
                       env={"CQ_PROFILE": "local"}, config=config)
    assert (result.exit_code, result.stderr) == (0, "")
    payload = assert_cli_json("doctor", result.stdout)
    assert payload == {"profile": LOCAL_VIEW, "checks": _healthy_checks()}
    _assert_clean(result, token)


def test_doctor_failure_prints_report_on_stdout_with_exit_1(client: Any, sql: Any, clock: Any,
                                                            tmp_path: pathlib.Path) -> None:
    """Com uma falha (o serviço `engine` parado, a imagem do motor ausente), o código é 1 e o relatório continua no
    stdout, em texto e em JSON; o stderr tem a linha de resumo, sem a pergunta da dica."""
    from cq_client.stack import ServiceState, StackSnapshot

    from tests.helpers.stack_fake import healthy

    token = _reader_token(client)
    config = _profile_file(tmp_path, token=token, stack_dir=make_stack_dir(tmp_path))
    base = healthy()
    stopped = StackSnapshot(services=tuple(ServiceState(s.service, "stopped", "none") if s.service == "engine"
                                           else s for s in base.services),
                            api_port=base.api_port, api_owned=base.api_owned)
    expected = []
    for check in _healthy_checks():
        if check["id"] == "service.engine":
            check = _check("service.engine", "local", "failure", CLIENT_MESSAGES[("service.engine", "failure")])
        elif check["id"] == "engine.image":
            check = _check("engine.image", "local", "failure", CLIENT_MESSAGES[("engine.image", "failure")])
        expected.append(check)
    for json_mode in (False, True):
        align_clock_to_db(clock, sql)
        result = invoke_cli(["doctor"], json=json_mode,
                           obj={"http": client, "stack": FakeStack([stopped], engine_image_id=None)},
                           env={"CQ_PROFILE": "local"}, config=config)
        assert result.exit_code == 1, result.stderr
        if json_mode:
            assert assert_cli_json("doctor", result.stdout) == {"profile": LOCAL_VIEW, "checks": expected}
        else:
            assert result.stdout == _text(LOCAL_HEADER, expected)
        assert result.stderr.strip() != "", "sem a linha de resumo no stderr"
        assert ASK_START_TEMPLATE.format(project="cq").strip() not in result.stderr
        _assert_clean(result, token)


def test_doctor_remote_profile_local_checks_not_applicable(client: Any, sql: Any, clock: Any,
                                                           tmp_path: pathlib.Path) -> None:
    """com o perfil remoto, só a parte da API roda; as dez locais "não se aplica (perfil remoto)" e
    volume e disco "não se aplica" com a mensagem da VM (`NOT_APPLICABLE_REMOTE_VM`); a pilha não é tocada; o
    arquivo de perfil é verificado mesmo assim."""
    token = _reader_token(client)
    config = _profile_file(tmp_path, token=token)
    stack = FakeStack(["healthy"])
    align_clock_to_db(clock, sql)
    result = invoke_cli(["doctor"], json=True, obj={"http": client, "stack": stack}, env={"CQ_PROFILE": "remoto"},
                       config=config)
    assert (result.exit_code, result.stderr) == (0, "")
    expected = [c for c in _healthy_checks() if c["part"] != "local"]
    expected[len(expected) - 2:len(expected) - 2] = [
        *(_check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE) for i in LOCAL_CHECK_IDS),
        *(_check(i, "local", "not_applicable", NOT_APPLICABLE_REMOTE_VM) for i in HOST_CHECK_IDS)]
    assert assert_cli_json("doctor", result.stdout) == {
        "profile": {"name": "remoto", "kind": "remote", "api_url": REMOTE_URL, "token_set": True,
                    "compose_project": None,
                    "origins": {"name": "env", "kind": "profile", "api_url": "profile", "token": "profile",
                                "compose_project": None}},
        "checks": expected}
    assert stack.calls == [], "o doctor tocou a pilha com o perfil remoto"
    text = invoke_cli(["doctor"], obj={"http": client, "stack": stack}, env={"CQ_PROFILE": "remoto"}, config=config)
    assert text.exit_code == 0
    assert text.stdout.splitlines()[:2] == [
        f"Perfil: remoto (remoto), API em {REMOTE_URL}, token definido.",
        "Origem: nome CQ_PROFILE, tipo declarado, endereço perfil, token perfil."]
    assert [line for line in text.stdout.splitlines() if line.startswith("[não se aplica]")] == [
        *(_line(i, "not_applicable", NOT_APPLICABLE_REMOTE) for i in LOCAL_CHECK_IDS),
        *(_line(i, "not_applicable", NOT_APPLICABLE_REMOTE_VM) for i in HOST_CHECK_IDS)]
    _assert_clean(result, token)
    _assert_clean(text, token)


def _unreachable_checks() -> list[dict]:
    checks = [_check("api.reachable", "api", "failure", CLIENT_MESSAGES[("api.reachable", "failure")])]
    checks += [_check(i, "api", "not_verified", NOT_VERIFIED_MESSAGES["api_unreachable"])
               for i in ("database.reachable", "database.schema", "api.access", *SERVER_CHECK_IDS)]
    checks += [_check(i, "local", "ok", CLIENT_MESSAGES[(i, "ok")], detail=ENGINE_DETAIL if i == "engine.image" else None)
               for i in LOCAL_CHECK_IDS[:-1]]
    checks.append(_check("api.owner", "local", "not_verified", NOT_VERIFIED_MESSAGES["api_unreachable"]))
    checks += _host_no_probe()
    permissions = _permissions_result()
    checks += [_check("profile.permissions", "profile", permissions,
                      CLIENT_MESSAGES[("profile.permissions", permissions)]),
               _check("profile.git", "profile", "ok", CLIENT_MESSAGES[("profile.git", "ok")])]
    return checks


@pytest.mark.parametrize("terminal", [False, True], ids=["sem-terminal", "terminal-simulado"])
def test_doctor_api_unreachable_exit_1_with_hint_and_no_question(tmp_path: pathlib.Path, terminal: bool) -> None:
    """A API inalcançável (perfil de teste em `http://127.0.0.1:9`) dá código 1, nunca 3; as
    locais rodam assim mesmo; o relatório no stdout; no stderr, a linha da dica com o projeto do perfil
    local e **nunca** a pergunta, mesmo com o stdin e o stderr num terminal simulado; o stdin não
    é lido."""
    config = _profile_file(tmp_path, token="cq_" + "u" * 43, api_url=UNREACHABLE_API_URL,
                           stack_dir=make_stack_dir(tmp_path))
    stack = FakeStack(["healthy"])
    result = invoke_cli(["doctor"], obj={"stack": stack}, env={"CQ_PROFILE": "local"}, config=config,
                       terminal=terminal, answer="s\n")
    assert result.exit_code == 1, (result.exit_code, result.stderr)
    header = [f"Perfil: local (local), API em {UNREACHABLE_API_URL}, token definido.",
              "Origem: nome CQ_PROFILE, tipo declarado, endereço perfil, token perfil."]
    assert result.stdout == _text(header, _unreachable_checks())
    hint = HINT_TEMPLATE.format(project="cq")
    assert result.stderr.splitlines().count(hint) == 1, result.stderr
    assert "[s/N]" not in result.stderr and ASK_START_TEMPLATE.strip() not in result.stderr
    assert "up" not in stack.names(), "o doctor subiu a pilha"
    _assert_clean(result)


def test_doctor_api_unreachable_without_hint_in_json_or_with_api_url_env(tmp_path: pathlib.Path) -> None:
    """Sem a dica com `--json` (o relatório JSON no stdout, código 1) e com `CQ_API_URL` definida (contrato:
    a dica só com o perfil local, sem `CQ_API_URL` e sem `--json`)."""
    config = _profile_file(tmp_path, token="cq_" + "u" * 43, api_url=UNREACHABLE_API_URL,
                           stack_dir=make_stack_dir(tmp_path))
    hint = HINT_TEMPLATE.format(project="cq")
    as_json = invoke_cli(["doctor"], json=True, obj={"stack": FakeStack(["healthy"])}, env={"CQ_PROFILE": "local"},
                        config=config)
    assert as_json.exit_code == 1
    payload = assert_cli_json("doctor", as_json.stdout)
    assert payload["checks"] == _unreachable_checks()
    assert hint not in as_json.stderr
    with_env = invoke_cli(["doctor"], obj={"stack": FakeStack(["healthy"])},
                         env={"CQ_PROFILE": "local", "CQ_API_URL": UNREACHABLE_API_URL}, config=config)
    assert with_env.exit_code == 1
    assert hint not in with_env.stderr
    assert with_env.stdout.splitlines()[1] == "Origem: nome CQ_PROFILE, tipo declarado, endereço CQ_API_URL, token perfil."


def test_doctor_never_exit_3_and_report_has_no_sensitive_data(client: Any, sql: Any, clock: Any,
                                                              tmp_path: pathlib.Path) -> None:
    """a saída em texto e em JSON não traz o token do perfil, o conteúdo do `.env` (sentinela), os caminhos
    completos do perfil e da pilha, nem o `user:senha@` de um `api_url` com credenciais; o JSON é estrito."""
    token = _reader_token(client)
    stack_dir = make_stack_dir(tmp_path)
    url_with_password = TEST_API_URL.replace("http://", "http://operador:senha-sentinela-Qx8@")
    config = _profile_file(tmp_path, token=token, api_url=url_with_password, stack_dir=stack_dir)
    for json_mode in (False, True):
        align_clock_to_db(clock, sql)
        result = invoke_cli(["doctor"], json=json_mode, obj={"http": client, "stack": FakeStack(["healthy"])},
                           env={"CQ_PROFILE": "local"}, config=config)
        assert result.exit_code == 0, result.stderr
        _assert_clean(result, token, "senha-sentinela-Qx8", "operador:", str(stack_dir), str(config), config.name)
        if json_mode:
            assert assert_cli_json("doctor", result.stdout)["profile"]["api_url"] == TEST_API_URL
        else:
            assert result.stdout.splitlines()[0] == f"Perfil: local (local), API em {TEST_API_URL}, token definido."
            assert json.dumps(result.stdout)  # o texto é serializável (sem bytes soltos)
