"""A rota do `doctor` com as 3 globais `backup.age`, `smtp.server` e `retention.overdue`.


`GET /v1/system/doctor` com `create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS,
doctor_settings=DoctorSettings(backup_status_file=…))` e `TestClient`, como em `test_doctor_api.py` (o
`app_factory` não aceita `doctor_settings`; **[F]** `tests/conftest.py`), com o registro do backup no `tmp_path`
e a linha de `mail_probe` gravada por SQL (o que o worker gravaria):
- as 6 globais na ordem `bots.master_key, tasks.overdue, clock.skew, backup.age, smtp.server,
  retention.overdue`, cada uma exatamente uma vez, depois as `bot.*`;
- `backup.age` em cada resultado e motivo do diagnóstico (não configurado; configurado e ilegível, inclusive
  `finished_at` no futuro além do desvio; 2 h, 30 h, 60 h), com `count` em horas inteiras e `null` sem registro
  legível;
- `smtp.server` com cada linha de `mail_probe` (sem linha; `not_configured`; `connection`; `tls`; `auth`; `ok`
  recente; `ok` com exatamente 13 h; `ok` mais velho que 13 h);
- `retention.overdue` "ok" com `count` 0 e `offering` `null`;
- cada resposta válida contra o esquema da resposta (estrito; `system_doctor` confere); a leitura não escreve
  (instantâneo de todas as tabelas igual); o `run_doctor` pelo cliente MCP em processo, nas duas eras, com
  essas 3 globais no `structuredContent` e o mesmo JSON no texto; nada do registro além das horas (o `file` e o
  `sha256` fora da resposta);
- a verificação da chave-mestra: `bots.master_key` testa a chave pelos tokens gravados. Sem bot,
  "ok" com a chave, também com outra chave, e "aviso" sem ela; com a chave certa, "ok"; com outra chave
  (o `BotRuntime` montado com ela e o `create_app` sem `doctor_settings`, que põe a cifra do runtime no
  `DoctorSettings`), "falha" com a mensagem fixa de `DOCTOR_REASON_MESSAGES[("bots.master_key", "failure",
  "token_unreadable")]` e nem o token nem as chaves na resposta; "falha" também se **algum** token gravado não
  decifra (o de um oferecimento em rascunho, ou um adulterado com a chave certa num bot `no_access`);
  com `DoctorSettings` injetado, vale a cifra dele (`token_cipher`;
  sem ela, a chave não é testada).

**Vermelho no esqueleto:** o `collect` ainda não emite essas 3 globais nem testa a chave (até a implementação):
com outra chave, `bots.master_key` sai "ok". Os casos de "ok" e "aviso" que já valem no esqueleto ficam na
mesma função de um caso de "falha".

Escolhas deste teste ([I]): o relógio da API é o `FakeClock` sem alinhar ao banco (só essas 3 globais
são conferidas por inteiro; o `clock.skew` não importa aqui); `smtp.server` com o `ok` de exatamente
`mail_stale_hours` ainda é "ok" (a comparação com o limite é estrita, como na idade do
backup).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from cq_server.doctor import BACKUP_AGE_TEMPLATE, DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES, DoctorSettings
from cq_server.secretbox import TokenCipher
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    offering_ref,
    register_bot,
    system_doctor,
)
from tests.helpers.canary import find_secrets
from tests.helpers.cli_runner import write_profile
from tests.helpers.contract import assert_matches
from tests.helpers.mcp_client import MODES, call, in_process, make_deps, run
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY
from tests.helpers.stack_fake import TEST_API_URL

GLOBAL_IDS = ["bots.master_key", "tasks.overdue", "clock.skew", "backup.age", "smtp.server", "retention.overdue"]
NEW_IDS = ("backup.age", "smtp.server", "retention.overdue")
RECORD_FILE_NAME = "cq-20301231T213000Z.dump.age"
RECORD_SHA256 = "fedcba9876543210" * 4


# --- montagem ------------------------------------------------------------------------------------------------


def _app(settings: Any, clock: Any, bots: Any, doctor_settings: DoctorSettings) -> Any:
    from cq_server.api import create_app
    from tests.helpers.calls import TEST_REPO_SETTINGS

    return create_app(settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS,
                      doctor_settings=doctor_settings)


def _doctor(settings: Any, clock: Any, bots: Any, doctor_settings: DoctorSettings | None = None) -> dict:
    """Chama `system.doctor` (200 e o esquema) numa API com o `DoctorSettings` dado."""
    app = _app(settings, clock, bots, doctor_settings if doctor_settings is not None else DoctorSettings())
    with TestClient(app, raise_server_exceptions=False) as client:
        return system_doctor(client)


def _by_id(body: dict, check_id: str) -> dict:
    found = [c for c in body["checks"] if c["id"] == check_id]
    assert len(found) == 1, f"{check_id}: {len(found)} verificações"
    return found[0]


def _expected(check_id: str, result: str, reason: str, *, count: int | None = None,
              offering: str | None = None) -> dict:
    message = DOCTOR_REASON_MESSAGES[(check_id, result, reason)]
    if (check_id, reason) == ("backup.age", "age"):
        message = message.format(hours=count)
    return {"id": check_id, "result": result, "message": message, "offering": offering, "count": count}


def _record(tmp_path: Path, finished: datetime, **changes: Any) -> str:
    record = {"version": 1, "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"), "file": RECORD_FILE_NAME,
              "size_bytes": 4321, "sha256": RECORD_SHA256}
    record.update(changes)
    path = tmp_path / "estado" / "last-backup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def _probe_row(sql: psycopg.Connection, result: str, at: datetime) -> None:
    sql.execute("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE "
                "SET result = EXCLUDED.result, checked_at = EXCLUDED.checked_at", (result, at))


def _fingerprint(sql: psycopg.Connection) -> dict[str, str | None]:
    tables = [row[0] for row in sql.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()]
    return {table: sql.execute(f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) '
                               f'FROM "{table}" t').fetchone()[0] for table in tables}


# --- casos -----------------------------------------------------------------------------------------------------


def test_six_globals_in_order_then_bot_checks(settings: Any, clock: Any, bots: Any, client: Any, bot_api: Any,
                                              tmp_path: Path) -> None:
    """As 6 globais na ordem fixa do diagnóstico, cada uma uma vez, depois as `bot.*` do oferecimento ativo; as
    3 primeiras e as `bot.*` com as mensagens de `DOCTOR_MESSAGES`, as 3 últimas com as de `DOCTOR_REASON_MESSAGES`."""
    offering = create_offering(client, ADMIN_TOKEN, code="doutor-implantacao")
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, offering_ref(offering))

    body = _doctor(settings, clock, bots, DoctorSettings(backup_status_file=_record(
        tmp_path, clock.now() - timedelta(hours=2))))
    ids = [c["id"] for c in body["checks"]]
    assert ids[:6] == GLOBAL_IDS
    assert all(ids.count(check_id) == 1 for check_id in GLOBAL_IDS)
    assert ids[6:] == ["bot.access", "bot.lease", "bot.poll"]
    assert all(c["offering"] == "doutor-implantacao" for c in body["checks"][6:])
    for check in body["checks"]:
        if check["id"] in NEW_IDS:
            assert check["message"] in {text.format(hours=check["count"]) if text == BACKUP_AGE_TEMPLATE else text
                                        for (cid, result, _), text in DOCTOR_REASON_MESSAGES.items()
                                        if (cid, result) == (check["id"], check["result"])}
        else:
            assert check["message"] == DOCTOR_MESSAGES[(check["id"], check["result"])]


BACKUP_CASES = [
    pytest.param("not_configured", None, ("warning", "not_configured", None), id="nao-configurado"),
    pytest.param("missing", None, ("failure", "unreadable", None), id="configurado-sem-arquivo"),
    pytest.param("invalid", None, ("failure", "unreadable", None), id="configurado-ilegivel"),
    pytest.param("record", timedelta(seconds=-3600), ("failure", "unreadable", None), id="no-futuro"),
    pytest.param("record", timedelta(hours=2), ("ok", "age", 2), id="2h"),
    pytest.param("record", timedelta(hours=30), ("warning", "age", 30), id="30h"),
    pytest.param("record", timedelta(hours=60), ("failure", "age", 60), id="60h"),
]


@pytest.mark.parametrize(("kind", "age", "expected"), BACKUP_CASES)
def test_backup_age_results(settings: Any, clock: Any, bots: Any, tmp_path: Path, kind: str,
                            age: timedelta | None, expected: tuple[str, str, int | None]) -> None:
    """`backup.age` em cada resultado e motivo do diagnóstico, com o `count` em horas inteiras (e `null` sem
    registro legível) e nada do registro além das horas: o `file` e o `sha256` fora da resposta."""
    if kind == "not_configured":
        doctor_settings = DoctorSettings()
    elif kind == "missing":
        doctor_settings = DoctorSettings(backup_status_file=str(tmp_path / "estado" / "ausente.json"))
    elif kind == "invalid":
        path = Path(_record(tmp_path, clock.now()))
        path.write_text("{\"version\": 1,", encoding="utf-8")
        doctor_settings = DoctorSettings(backup_status_file=str(path))
    else:
        assert age is not None
        doctor_settings = DoctorSettings(backup_status_file=_record(tmp_path, clock.now() - age))
    body = _doctor(settings, clock, bots, doctor_settings)
    result, reason, count = expected
    assert _by_id(body, "backup.age") == _expected("backup.age", result, reason, count=count)
    text = json.dumps(body)
    for value in (RECORD_FILE_NAME, RECORD_SHA256, "4321", str(tmp_path), json.dumps(str(tmp_path))[1:-1]):
        assert value not in text, value


SMTP_CASES = [
    pytest.param(None, None, ("warning", "never"), id="sem-linha"),
    pytest.param("not_configured", timedelta(minutes=5), ("warning", "not_configured"), id="nao-configurado"),
    pytest.param("connection", timedelta(minutes=5), ("failure", "connection"), id="conexao"),
    pytest.param("tls", timedelta(minutes=5), ("failure", "tls"), id="tls"),
    pytest.param("auth", timedelta(minutes=5), ("failure", "auth"), id="auth"),
    pytest.param("ok", timedelta(hours=1), ("ok", "ok"), id="ok-recente"),
    pytest.param("ok", timedelta(hours=13), ("ok", "ok"), id="ok-13h-exatas"),
    pytest.param("ok", timedelta(hours=13, seconds=1), ("warning", "stale"), id="ok-13h-e-1s"),
    pytest.param("ok", timedelta(days=3), ("warning", "stale"), id="ok-3-dias"),
]


@pytest.mark.parametrize(("row", "age", "expected"), SMTP_CASES)
def test_smtp_server_results(settings: Any, clock: Any, bots: Any, sql: psycopg.Connection, row: str | None,
                             age: timedelta | None, expected: tuple[str, str]) -> None:
    """`smtp.server` com cada linha de `mail_probe`: `count` e `offering` nulos."""
    if row is not None:
        assert age is not None
        _probe_row(sql, row, clock.now() - age)
    body = _doctor(settings, clock, bots)
    result, reason = expected
    assert _by_id(body, "smtp.server") == _expected("smtp.server", result, reason)


def test_smtp_stale_limit_comes_from_the_settings(settings: Any, clock: Any, bots: Any,
                                                  sql: psycopg.Connection) -> None:
    """O limite do "antigo" é o `mail_stale_hours` do `DoctorSettings` injetado: 2 h com o limite de 1 h é aviso."""
    _probe_row(sql, "ok", clock.now() - timedelta(hours=2))
    body = _doctor(settings, clock, bots, DoctorSettings(mail_stale_hours=1))
    assert _by_id(body, "smtp.server") == _expected("smtp.server", "warning", "stale")
    body = _doctor(settings, clock, bots, DoctorSettings(mail_stale_hours=3))
    assert _by_id(body, "smtp.server") == _expected("smtp.server", "ok", "ok")


def test_retention_ok_without_archived_offerings(settings: Any, clock: Any, bots: Any, client: Any) -> None:
    """Sem oferecimento arquivado: `retention.overdue` "ok" com `count` 0 e `offering` nulo."""
    create_offering(client, ADMIN_TOKEN, code="retencao-rascunho")
    body = _doctor(settings, clock, bots)
    assert _by_id(body, "retention.overdue") == _expected("retention.overdue", "ok", "ok", count=0)


def test_doctor_reads_without_writing(settings: Any, clock: Any, bots: Any, sql: psycopg.Connection,
                                      tmp_path: Path) -> None:
    """A leitura não escreve: com o registro do backup e a linha de `mail_probe`, o instantâneo de todas as tabelas
    é igual antes e depois de duas chamadas; o arquivo do registro não muda."""
    _probe_row(sql, "ok", clock.now() - timedelta(hours=1))
    path = _record(tmp_path, clock.now() - timedelta(hours=30))
    content = Path(path).read_bytes()
    before = _fingerprint(sql)
    doctor_settings = DoctorSettings(backup_status_file=path)
    for _ in range(2):
        body = _doctor(settings, clock, bots, doctor_settings)
        assert [c["id"] for c in body["checks"]][:6] == GLOBAL_IDS
    assert _fingerprint(sql) == before
    assert Path(path).read_bytes() == content


@pytest.mark.parametrize("mode", MODES)
def test_run_doctor_through_mcp_carries_the_new_globals(mode: str, settings: Any, clock: Any, bots: Any,
                                                        sql: psycopg.Connection, tmp_path: Path) -> None:
    """O `run_doctor` pelo cliente MCP em processo (nas duas eras): o `structuredContent` é o corpo da API,
    com as 6 globais, e o texto é o mesmo JSON compacto."""
    _probe_row(sql, "auth", clock.now() - timedelta(minutes=10))
    doctor_settings = DoctorSettings(backup_status_file=_record(tmp_path, clock.now() - timedelta(hours=60)))
    app = _app(settings, clock, bots, doctor_settings)
    profile = write_profile(tmp_path, f'api_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
    with TestClient(app, raise_server_exceptions=False) as api:
        expected = system_doctor(api)
        deps = make_deps(tmp_path, env={"CQ_CONFIG": str(profile)}, http=api)

        async def scenario() -> Any:
            async with in_process(deps, mode) as mc:
                return await call(mc, "run_doctor", {})

        outcome = run(scenario)
    assert not outcome.is_protocol_error, (outcome.protocol_code, outcome.protocol_message)
    assert outcome.is_error is False, outcome.text
    assert outcome.structured == expected
    assert outcome.text == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert [c["id"] for c in outcome.structured["checks"]][:6] == GLOBAL_IDS
    assert _by_id(outcome.structured, "backup.age") == _expected("backup.age", "failure", "age", count=60)
    assert _by_id(outcome.structured, "smtp.server") == _expected("smtp.server", "failure", "auth")
    assert _by_id(outcome.structured, "retention.overdue") == _expected("retention.overdue", "ok", "ok", count=0)
    for value in (RECORD_FILE_NAME, RECORD_SHA256):
        assert value not in (outcome.text or "")


# --- bots.master_key testa a chave ------------------------------------------------------

MASTER_KEY_OK = {"id": "bots.master_key", "result": "ok", "message": DOCTOR_MESSAGES[("bots.master_key", "ok")],
                 "offering": None, "count": None}
MASTER_KEY_WARNING = {"id": "bots.master_key", "result": "warning",
                      "message": DOCTOR_MESSAGES[("bots.master_key", "warning")], "offering": None, "count": None}
MASTER_KEY_FAILURE = _expected("bots.master_key", "failure", "token_unreadable")


def _bot_token(client: Any, bot_api: Any, code: str, *, activate: bool = True) -> str:
    """Um oferecimento com bot registrado pela rota (o token cifrado pela chave do `bots` da fixture); o token."""
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    token = bot_api.token_of(bot_api.add_bot())
    register_bot(client, ADMIN_TOKEN, offering_ref(offering), token)
    if activate:
        activate_bot(client, ADMIN_TOKEN, offering_ref(offering))
    return token


def _doctor_text(api: Any) -> tuple[dict, str]:
    """`system.doctor` (200 e o esquema): o corpo e o texto cru da resposta."""
    resp = api.get("/v1/system/doctor", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"system.doctor: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("system.doctor", 200, body)
    return body, resp.text


def _other_key_app(app_factory: Any, bot_settings: Any) -> Any:
    """A API com outra chave-mestra válida (o `BotRuntime` montado com ela; `create_app` sem `doctor_settings`)."""
    return app_factory(bot_settings=dataclasses.replace(bot_settings, master_key=OTHER_MASTER_KEY))


def _bot_row(sql: psycopg.Connection, code: str) -> tuple[Any, int, bytes]:
    row = sql.execute("SELECT b.bot_uuid, b.telegram_bot_id, b.token_ciphertext FROM bots b "
                      "JOIN offerings o ON o.id = b.offering_id WHERE o.code = %s", (code,)).fetchone()
    assert row is not None, f"oferecimento sem bot: {code}"
    return row[0], int(row[1]), bytes(row[2])


def _set_blob(sql: psycopg.Connection, bot_uuid: Any, blob: bytes) -> None:
    sql.execute("UPDATE bots SET token_ciphertext = %s WHERE bot_uuid = %s", (blob, bot_uuid))


def test_master_key_with_another_key_fails_without_showing_the_token(
        client: Any, app_factory: Any, bot_settings: Any, bot_api: Any) -> None:
    """Sem bot ("ok" com a chave, inclusive com outra; "aviso" sem ela); com um
    bot registrado, "ok" com a chave certa (o `client` da fixture: `create_app` sem `doctor_settings`) e "falha" com
    outra chave, com a mensagem fixa, sem o token nem as chaves na resposta."""
    with TestClient(_other_key_app(app_factory, bot_settings), raise_server_exceptions=False) as api:
        assert _by_id(system_doctor(api), "bots.master_key") == MASTER_KEY_OK
    with TestClient(app_factory(bots=None), raise_server_exceptions=False) as api:
        assert _by_id(system_doctor(api), "bots.master_key") == MASTER_KEY_WARNING
    assert _by_id(system_doctor(client), "bots.master_key") == MASTER_KEY_OK

    token = _bot_token(client, bot_api, "chave-mestra-implantacao")
    assert _by_id(system_doctor(client), "bots.master_key") == MASTER_KEY_OK
    with TestClient(_other_key_app(app_factory, bot_settings), raise_server_exceptions=False) as api:
        body, text = _doctor_text(api)
    assert _by_id(body, "bots.master_key") == MASTER_KEY_FAILURE
    assert [c["id"] for c in body["checks"]][:6] == GLOBAL_IDS
    assert find_secrets(text, [token, MASTER_KEY, OTHER_MASTER_KEY]) == []
    assert token.split(":", 1)[1] not in text


def test_master_key_fails_if_any_stored_token_does_not_decrypt(
        client: Any, sql: psycopg.Connection, bot_api: Any) -> None:
    """O `doctor` testa todo token gravado, de qualquer oferecimento e em
    qualquer estado do bot, e sai "falha" se algum não decifra, por qualquer motivo: o do bot de um oferecimento em
    rascunho cifrado com outra chave (o mais recente decifra), e um adulterado com a chave certa num bot
    `no_access`; entre os dois, com o blob original de volta, "ok" de novo. A leitura não regrava o token."""
    old_token = _bot_token(client, bot_api, "chave-rascunho", activate=False)
    _bot_token(client, bot_api, "chave-recente")
    assert _by_id(system_doctor(client), "bots.master_key") == MASTER_KEY_OK

    bot_uuid, telegram_bot_id, blob = _bot_row(sql, "chave-rascunho")
    assert TokenCipher(MASTER_KEY).decrypt(blob, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id) == old_token
    foreign = TokenCipher(OTHER_MASTER_KEY).encrypt(old_token, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id)
    _set_blob(sql, bot_uuid, foreign)
    body, text = _doctor_text(client)
    assert _by_id(body, "bots.master_key") == MASTER_KEY_FAILURE
    assert find_secrets(text, [old_token]) == []
    assert _bot_row(sql, "chave-rascunho")[2] == foreign
    _set_blob(sql, bot_uuid, blob)
    # Com o blob original de volta, o `doctor` volta a "ok" (nenhum estado nem cache do caso anterior): a "falha"
    # seguinte fica atribuída só ao token adulterado.
    assert _by_id(system_doctor(client), "bots.master_key") == MASTER_KEY_OK

    recent_uuid, _, recent_blob = _bot_row(sql, "chave-recente")
    tampered = recent_blob[:-1] + bytes([recent_blob[-1] ^ 1])
    sql.execute("UPDATE bots SET token_ciphertext = %s, state = 'no_access', access_problem = 'token_unreadable', "
                "lease_owner = NULL, lease_until = NULL WHERE bot_uuid = %s", (tampered, recent_uuid))
    assert _by_id(system_doctor(client), "bots.master_key") == MASTER_KEY_FAILURE


def test_master_key_uses_the_cipher_of_the_injected_settings(
        settings: Any, clock: Any, bots: Any, client: Any, bot_api: Any) -> None:
    """com `DoctorSettings` injetado, a rota o passa como veio e o `collect` usa o `token_cipher` dele: sem a
    cifra (o padrão do campo), a chave não é testada ("ok"); com a cifra da chave certa, "ok"; com a de
    outra chave, "falha"."""
    _bot_token(client, bot_api, "chave-injetada")
    assert DoctorSettings().token_cipher is None
    assert _by_id(_doctor(settings, clock, bots, DoctorSettings()), "bots.master_key") == MASTER_KEY_OK
    right = DoctorSettings(token_cipher=TokenCipher(MASTER_KEY))
    assert _by_id(_doctor(settings, clock, bots, right), "bots.master_key") == MASTER_KEY_OK
    wrong = DoctorSettings(token_cipher=TokenCipher(OTHER_MASTER_KEY))
    assert _by_id(_doctor(settings, clock, bots, wrong), "bots.master_key") == MASTER_KEY_FAILURE
