"""CLI `cq invites send prepare|confirm` e `cq invites resend` do vínculo do aluno.

Cobre o envio e o reenvio de convites pelo CLI: os
comandos (só `--valid-hours` é tipado, inteiro), o texto para pessoas (horários como a API os devolve,
inclusive o `expires_at` do código de confirmação), os erros (na forma comum do CLI, com `Estado atual:`
no `invalid_state`) e o `--json` (o corpo da API, sem tradução).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`). Os pedidos que o CLI faz são registrados pelo gancho de pedido do
`TestClient`. Nenhum worker roda nestes testes: a confirmação e o reenvio só enfileiram (`invite_sends`
`pending` e a tarefa `invite.email`), e nenhum e-mail sai. O código de confirmação é aleatório e vem da
própria saída (ele não é segredo). Identificadores, pseudônimos e códigos nunca são
literais: vêm da própria API. Horários pelo `FakeClock` e `iso(dt)`.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    import_students,
    prepare_invites,
    register_bot,
    register_person,
    uuid4_with_prefix,
)
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_matches

ADMIN = auth(ADMIN_TOKEN)
CONFIRMATION = timedelta(minutes=10)  # `expires_at` = agora + 10 min
NOT_FOUND_ALIAS = "ffff0000ffff"  # formato válido; nunca é apelido de alguém (o apelido seria `ffff`)
ANY_CODE = "Aa0_-Aa0_-Aa0_-Aa0_-Zz"  # código de confirmação no formato, que nenhum preparo gerou
PREPARE_RE = re.compile(r"Enviar convites de \S+: \d+ alunos, validade de \d+ h a partir do envio\.\n"
                        r"Código de confirmação \(válido até \S+\): ([A-Za-z0-9_-]{22})\n")

STUDENT_ROWS = [
    {"name": "Aluna Convite Primeira", "ra": "5530000001", "email": "convite.primeira@turma.example.org"},
    {"name": "Aluno Convite Segundo", "ra": "5530000002", "email": "convite.segundo@turma.example.org"},
    {"name": "Aluna Convite Terceira", "ra": "5530000003", "email": "convite.terceira@turma.example.org"},
]


# --------------------------------------------------------------------------------------------------
# Apoio


def _out(*lines: str) -> str:
    """Saída esperada: uma linha por item, cada uma terminada por quebra de linha."""
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _failed(result: Any, code: int) -> None:
    _no_traceback(result)
    assert result.exit_code == code, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""


@contextlib.contextmanager
def _recorded_requests(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


def _sent(seen: list[Any]) -> list[tuple[str, str, Any]]:
    """(método, caminho, corpo JSON ou `None`) de cada pedido registrado."""
    return [(r.method, r.url.path, json.loads(r.content) if r.content else None) for r in seen]


def _ref(offering: dict) -> dict:
    """O `OfferingRef` do contrato."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _prepare_args(ref: str, *extra: str) -> list[str]:
    return ["invites", "send", "prepare", ref, *extra]


def _confirm_args(ref: str, confirmation: str) -> list[str]:
    return ["invites", "send", "confirm", ref, "--confirmation", confirmation]


def _active_offering(client: Any, bot_api: Any, clock: Any, code: str) -> tuple[dict, int]:
    """Um oferecimento ativo, com bot registrado na Bot API falsa; devolve o `Offering` e o id do bot."""
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    telegram_bot_id = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(telegram_bot_id))
    clock.advance(5)
    active = activate_bot(client, ADMIN_TOKEN, code)
    assert (active["offering_id"], active["state"]) == (offering["offering_id"], "active")
    return active, telegram_bot_id


def _post_ok(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    resp = client.post(path, headers=ADMIN, **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _close(client: Any, code: str) -> None:
    assert _post_ok(client, "offerings.close", f"/v1/offerings/{code}/close")["state"] == "closed"


def _archive(client: Any, code: str) -> None:
    prepared = _post_ok(client, "offerings.prepare_archive", f"/v1/offerings/{code}/archive/prepare")
    archived = _post_ok(client, "offerings.archive", f"/v1/offerings/{code}/archive",
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived"


def _set_no_access(sql: Any, telegram_bot_id: int) -> None:
    """Bot sem acesso, gravado direto no banco, como no
    `tests/integration/test_api_errors.py`: o caminho que leva a `no_access` é conferido em `test_bot_failures.py`;
    aqui só importa a saída do CLI para o `bot_required`."""
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                "WHERE telegram_bot_id = %s", (telegram_bot_id,))
    assert sql.execute("SELECT state, access_problem, lease_owner FROM bots WHERE telegram_bot_id = %s",
                       (telegram_bot_id,)).fetchall() == [("no_access", "token_rejected", None)]


def _invite_sends(sql: Any, code: str) -> list[tuple]:
    """(id, enrollment_id, pseudo_id, origin, valid_hours, state, attempts) dos envios do oferecimento."""
    return sql.execute(
        "SELECT s.id, s.enrollment_id, p.pseudo_id::text, s.origin, s.valid_hours, s.state, s.attempts "
        "FROM invite_sends s JOIN enrollments e ON e.id = s.enrollment_id "
        "JOIN offerings o ON o.id = e.offering_id JOIN people p ON p.id = e.person_id "
        "WHERE o.code = %s ORDER BY s.id", (code,)).fetchall()


def _all_invite_sends(sql: Any) -> list[tuple]:
    return sql.execute("SELECT id FROM invite_sends ORDER BY id").fetchall()


def _student_enrollment_ids(sql: Any, code: str) -> list[int]:
    return [row[0] for row in sql.execute(
        "SELECT e.id FROM enrollments e JOIN offerings o ON o.id = e.offering_id "
        "WHERE o.code = %s AND e.role = 'student' ORDER BY e.id", (code,)).fetchall()]


def _invite_email_tasks(sql: Any) -> list[tuple]:
    return sql.execute("SELECT state, payload, idempotency_key FROM tasks WHERE task_type = 'invite.email' "
                       "ORDER BY id").fetchall()


def _prepare_columns(sql: Any, code: str) -> tuple:
    """As colunas do preparo do envio."""
    return sql.execute("SELECT invite_code_sha256, invite_code_expires_at, invite_valid_hours, "
                       "invite_eligible_count FROM offerings WHERE code = %s", (code,)).fetchone()


def _events(client: Any, *types: str) -> list[dict]:
    """Os eventos dos tipos dados, em ordem de `seq` (crescente), sem o `seq`."""
    events = all_events(client, ADMIN_TOKEN, *types)
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _prepared_event(offering: dict, eligible: int, valid_hours: int, expires_at: str, at: str) -> dict:
    """`student.invites_prepared`, pelo token inicial."""
    return {"type": "student.invites_prepared", "occurred_at": at, "actor_token_id": "bootstrap",
            "data": {"offering_id": offering["offering_id"], "eligible": eligible, "valid_hours": valid_hours,
                     "expires_at": expires_at}}


def _queued_event(offering: dict, origin: str, queued: int, at: str) -> dict:
    """`student.invites_queued`, pelo token inicial."""
    return {"type": "student.invites_queued", "occurred_at": at, "actor_token_id": "bootstrap",
            "data": {"offering_id": offering["offering_id"], "origin": origin, "queued": queued}}


def _error(code: str, *, state: str | None = None, fields: list[tuple[str, str]] | None = None
           ) -> tuple[dict, list[str]]:
    """O corpo de erro exato e o texto do CLI (a forma de erros do CLI):
    `Erro: <message>`, `Estado atual: <state>` no `invalid_state` e `  - <field>: <reason>`."""
    error: dict[str, Any] = {"code": code, "message": MESSAGES[code]}
    lines = [f"Erro: {MESSAGES[code]}"]
    if state is not None:
        error["state"] = state
        lines.append(f"Estado atual: {state}")
    if fields is not None:
        error["fields"] = [{"field": f, "reason": r} for f, r in fields]
        lines.extend(f"  - {f}: {r}" for f, r in fields)
    return {"error": error}, lines


def _assert_api_error(result: Any, json_mode: bool, body: dict, lines: list[str]) -> None:
    """Código 1, stdout vazio e o stderr exato: o corpo de erro (`--json`) ou o texto."""
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == body
    else:
        assert result.stderr == _out(*lines)


def _assert_prepared(result: Any, json_mode: bool, offering: dict, *, eligible: int, valid_hours: int,
                     expires_at: str) -> str:
    """o preparo, em texto (`Enviar convites de <code>: <eligible> alunos, validade
    de <valid_hours> h a partir do envio.` e `Código de confirmação (válido até <expires_at>):
    <confirmation_code>`) ou o corpo da API; devolve o código de confirmação."""
    _ok(result)
    if json_mode:
        body = json.loads(result.stdout)
        assert_matches("invites.prepare_send", 200, body)
        confirmation = body["confirmation_code"]
        assert body == {"offering": _ref(offering), "eligible": eligible, "valid_hours": valid_hours,
                        "confirmation_code": confirmation, "expires_at": expires_at}
    else:
        match = PREPARE_RE.fullmatch(result.stdout)
        assert match, f"saída fora do formato: {result.stdout!r}"
        confirmation = match.group(1)
        assert result.stdout == _out(
            f"Enviar convites de {offering['code']}: {eligible} alunos, validade de {valid_hours} h a partir "
            "do envio.",
            f"Código de confirmação (válido até {expires_at}): {confirmation}")
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", confirmation)
    return confirmation


def _assert_confirmed(result: Any, json_mode: bool, offering: dict, *, queued: int, valid_hours: int) -> None:
    """`Convites enfileirados em <code>: <queued>.` ou o corpo da API."""
    _ok(result)
    if json_mode:
        body = json.loads(result.stdout)
        assert_matches("invites.send", 200, body)
        assert body == {"offering": _ref(offering), "queued": queued, "valid_hours": valid_hours}
    else:
        assert result.stdout == _out(f"Convites enfileirados em {offering['code']}: {queued}.")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prepare_via_api(client: Any, clock: Any, offering: dict) -> str:
    """Preparo pela API, sem alunos elegíveis (validade padrão); afirma o corpo exato e devolve o código."""
    body = prepare_invites(client, ADMIN_TOKEN, offering["code"])
    assert body == {"offering": _ref(offering), "eligible": 0, "valid_hours": 168,
                    "confirmation_code": body["confirmation_code"],
                    "expires_at": iso(clock.now() + CONFIRMATION)}
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", body["confirmation_code"])
    return body["confirmation_code"]


# --------------------------------------------------------------------------------------------------
# invites send prepare / confirm


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_send_prepare_and_confirm(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                  bot_api: Any) -> None:
    """Texto para pessoas (`Enviar
    convites de <code>: <eligible> alunos, validade de <valid_hours> h a partir do envio.`, `Código de
    confirmação (válido até <expires_at>): <confirmation_code>` e `Convites enfileirados em <code>:
    <queued>.`; o `expires_at` como a API o devolve; `--json`: o corpo da API): sem alunos, o preparo
    conta 0 com a validade padrão (168 h, corpo `{}`) e a confirmação enfileira 0; com 3 alunos
    importados, `--valid-hours 24` chega à API como inteiro, o preparo conta 3 e a confirmação com o
    código impresso enfileira 3 envios `batch`/`pending` de 24 h, com uma tarefa `invite.email` cada, em
    ordem de `enrollments.id`; os eventos `student.invites_prepared` e `student.invites_queued` saem
    exatos, e nenhum e-mail sai (sem worker)."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-convites-cli")
    code = offering["code"]

    clock.advance(30)
    first_prepared_at = clock.now()
    with _recorded_requests(client) as seen:
        prepare = cli_invoke(_prepare_args(code), json=json_mode)
    first_expires = iso(first_prepared_at + CONFIRMATION)
    empty_code = _assert_prepared(prepare, json_mode, offering, eligible=0, valid_hours=168,
                                  expires_at=first_expires)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/prepare", {})]

    clock.advance(20)
    first_confirmed_at = clock.now()
    with _recorded_requests(client) as seen:
        confirm = cli_invoke(_confirm_args(code, empty_code), json=json_mode)
    _assert_confirmed(confirm, json_mode, offering, queued=0, valid_hours=168)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites", {"confirmation_code": empty_code})]
    assert _invite_sends(sql, code) == []

    imported = import_students(client, ADMIN_TOKEN, code, STUDENT_ROWS)
    assert imported == {"offering": _ref(offering), "received": 3, "people_created": 3, "people_reused": 0,
                        "enrollments_created": 3, "already_enrolled": 0}

    clock.advance(30)
    second_prepared_at = clock.now()
    with _recorded_requests(client) as seen:
        prepare = cli_invoke(_prepare_args(code, "--valid-hours", "24"), json=json_mode)
    second_expires = iso(second_prepared_at + CONFIRMATION)
    batch_code = _assert_prepared(prepare, json_mode, offering, eligible=3, valid_hours=24,
                                  expires_at=second_expires)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/prepare", {"valid_hours": 24})]
    assert batch_code != empty_code

    clock.advance(20)
    second_confirmed_at = clock.now()
    confirm = cli_invoke(_confirm_args(code, batch_code), json=json_mode)
    _assert_confirmed(confirm, json_mode, offering, queued=3, valid_hours=24)

    sends = _invite_sends(sql, code)
    assert [s[3:] for s in sends] == [("batch", 24, "pending", 0)] * 3
    assert [s[1] for s in sends] == _student_enrollment_ids(sql, code)
    assert _invite_email_tasks(sql) == [("pending", {"invite_send_id": s[0]}, f"invite.email:{s[0]}")
                                        for s in sends]
    assert _events(client, "student.invites_prepared", "student.invites_queued") == [
        _prepared_event(offering, 0, 168, first_expires, iso(first_prepared_at)),
        _queued_event(offering, "batch", 0, iso(first_confirmed_at)),
        _prepared_event(offering, 3, 24, second_expires, iso(second_prepared_at)),
        _queued_event(offering, "batch", 3, iso(second_confirmed_at)),
    ]
    for result in (prepare, confirm):
        for row in STUDENT_ROWS:
            assert row["email"] not in result.stdout and row["ra"] not in result.stdout


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_send_prepare_valid_hours_range(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                        bot_api: Any) -> None:
    """Validade ajustável de 1 hora a 30 dias (`valid_hours` de 1 a 720; fora:
    `out_of_range`) e os erros: `--valid-hours 0` e `--valid-hours 721` → código 1,
    `Erro: Pedido inválido.` e `  - valid_hours: out_of_range` (ou o corpo de erro com `--json`), sem
    preparo gravado nem evento; `--valid-hours 1` e `--valid-hours 720` são aceitos, com a validade
    impressa, e o preparo gravado é o do código impresso (resumo SHA-256, validade de 10 min)."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-convites-horas")
    code = offering["code"]
    for hours in ("0", "721"):
        with _recorded_requests(client) as seen:
            result = cli_invoke(_prepare_args(code, "--valid-hours", hours), json=json_mode)
        _assert_api_error(result, json_mode, *_error("invalid_request", fields=[("valid_hours", "out_of_range")]))
        assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/prepare", {"valid_hours": int(hours)})]
    assert _prepare_columns(sql, code) == (None, None, None, None)
    assert _events(client, "student.invites_prepared") == []

    for hours in (1, 720):
        clock.advance(10)
        prepared_at = clock.now()
        result = cli_invoke(_prepare_args(code, "--valid-hours", str(hours)), json=json_mode)
        confirmation = _assert_prepared(result, json_mode, offering, eligible=0, valid_hours=hours,
                                        expires_at=iso(prepared_at + CONFIRMATION))
        assert _prepare_columns(sql, code) == (_sha256(confirmation), prepared_at + CONFIRMATION, hours, 0)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_send_confirm_invalid_confirmation(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                           bot_api: Any) -> None:
    """Código de uso único, válido por 10 minutos; um novo preparo invalida o anterior;
    contrato: confirmar com um código inventado (sem preparo), vencido
    (`FakeClock` além dos 10 min), substituído por outro preparo e já usado → código 1 e `Erro: O código
    de confirmação é inválido ou venceu.`, sem a linha de estado e sem eco do código (ou o corpo de erro
    com `--json`); controle positivo: o código vigente confirma (0 enfileirados), uma vez só; nenhum
    envio é criado, e só a confirmação válida grava `student.invites_queued`."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-convites-codigo")
    code = offering["code"]
    invalid = _error("invalid_confirmation")

    invented = cli_invoke(_confirm_args(code, "codigosentinela-inventado"), json=json_mode)
    _assert_api_error(invented, json_mode, *invalid)
    assert "codigosentinela" not in invented.stderr

    clock.advance(10)
    expired = _prepare_via_api(client, clock, offering)
    clock.advance(601)
    result = cli_invoke(_confirm_args(code, expired), json=json_mode)
    _assert_api_error(result, json_mode, *invalid)
    assert expired not in result.stderr

    replaced = _prepare_via_api(client, clock, offering)
    clock.advance(10)
    current = _prepare_via_api(client, clock, offering)
    result = cli_invoke(_confirm_args(code, replaced), json=json_mode)
    _assert_api_error(result, json_mode, *invalid)
    assert replaced not in result.stderr

    clock.advance(10)
    confirmed_at = clock.now()
    confirmed = cli_invoke(_confirm_args(code, current), json=json_mode)
    _assert_confirmed(confirmed, json_mode, offering, queued=0, valid_hours=168)
    used = cli_invoke(_confirm_args(code, current), json=json_mode)
    _assert_api_error(used, json_mode, *invalid)
    assert current not in used.stderr

    assert _invite_sends(sql, code) == []
    assert _events(client, "student.invites_queued") == [_queued_event(offering, "batch", 0, iso(confirmed_at))]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_send_invalid_state_shows_the_current_state(json_mode: bool, cli_invoke: Any, client: Any, clock: Any,
                                                    sql: Any, bot_api: Any) -> None:
    """Regra (409 `invalid_state` fora de `active`, antes do
    `bot_required` e do `invalid_confirmation`) e os erros (no 409 `invalid_state`, depois de `Erro:
    <message>`, a linha `Estado atual: <state>`): preparar e confirmar num rascunho (sem bot), num
    encerrado e num arquivado → código 1 e o stderr exato (ou o corpo de erro com `state`); nada é
    preparado nem enfileirado."""
    draft = create_offering(client, ADMIN_TOKEN, code="mc102-convites-rascunho")
    closed, _ = _active_offering(client, bot_api, clock, "mc102-convites-encerrado")
    _close(client, closed["code"])
    archived = create_offering(client, ADMIN_TOKEN, code="mc102-convites-arquivado")
    _archive(client, archived["code"])
    cases = [
        (_prepare_args(draft["code"]), "draft"),
        (_confirm_args(draft["code"], ANY_CODE), "draft"),
        (_prepare_args(closed["code"], "--valid-hours", "24"), "closed"),
        (_confirm_args(closed["code"], ANY_CODE), "closed"),
        (_prepare_args(archived["code"]), "archived"),
        (_confirm_args(archived["code"], ANY_CODE), "archived"),
    ]
    for args, state in cases:
        result = cli_invoke(args, json=json_mode)
        _assert_api_error(result, json_mode, *_error("invalid_state", state=state))
    for offering in (draft, closed, archived):
        assert _prepare_columns(sql, offering["code"]) == (None, None, None, None)
    assert _all_invite_sends(sql) == []
    assert _events(client, "student.invites_prepared", "student.invites_queued") == []


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_send_bot_required(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                           bot_api: Any) -> None:
    """Oferecimento ativo com bot registrado e com acesso (409
    `bot_required` com o bot `no_access`, antes do `invalid_confirmation`): preparar e confirmar
    num oferecimento ativo cujo bot perdeu o acesso → código 1 e `Erro: O oferecimento não tem um bot
    registrado com acesso.` (ou o corpo de erro com `--json`); nada é preparado nem enfileirado."""
    offering, telegram_bot_id = _active_offering(client, bot_api, clock, "mc102-convites-sem-acesso")
    code = offering["code"]
    _set_no_access(sql, telegram_bot_id)
    for args in (_prepare_args(code), _prepare_args(code, "--valid-hours", "24"), _confirm_args(code, ANY_CODE)):
        result = cli_invoke(args, json=json_mode)
        _assert_api_error(result, json_mode, *_error("bot_required"))
    assert _prepare_columns(sql, code) == (None, None, None, None)
    assert _all_invite_sends(sql) == []
    assert _events(client, "student.invites_prepared", "student.invites_queued") == []


def test_send_usage_errors_exit_2_without_request(cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                                  bot_api: Any) -> None:
    """Comandos (`cq invites send prepare REF [--valid-hours N]` e `cq invites
    send confirm REF --confirmation CÓDIGO`; só `--valid-hours` é tipado, inteiro) e a forma geral (uso
    incorreto sem eco): `--valid-hours` não inteiro, sem o valor de `--valid-hours`, sem `REF`,
    argumento a mais e confirmação sem `--confirmation` → código 2, a mensagem exata (também com
    `--json`), sem repetir o valor digitado e sem nenhum pedido à API; na mesma função, o `--help` dos
    dois comandos sai com 0 e cita as opções."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-convites-uso")
    code = offering["code"]
    cases = [
        (_prepare_args(code, "--valid-hours", "horas-sentinela"), "valor inválido na opção --valid-hours."),
        (_prepare_args(code, "--valid-hours", "1.5"), "valor inválido na opção --valid-hours."),
        (_prepare_args(code, "--valid-hours"), "falta o valor da opção --valid-hours."),
        (["invites", "send", "prepare"], "falta um argumento obrigatório."),
        (_prepare_args(code, "argumentosentinela"), "há argumentos a mais."),
        (["invites", "send", "confirm", code], "falta a opção --confirmation."),
        (["invites", "send", "confirm", "--confirmation", "codigosentinela"], "falta um argumento obrigatório."),
    ]
    for args, message in cases:
        for json_mode in (False, True):
            with _recorded_requests(client) as seen:
                result = cli_invoke(args, json=json_mode)
            _failed(result, 2)
            assert result.stderr == _out(f"Uso incorreto: {message}"), args
            assert seen == [], args
            assert "sentinela" not in result.stderr
    assert _prepare_columns(sql, code) == (None, None, None, None)

    prepare_help = cli_invoke(["invites", "send", "prepare", "--help"])
    _ok(prepare_help)
    assert "--valid-hours" in prepare_help.stdout
    confirm_help = cli_invoke(["invites", "send", "confirm", "--help"])
    _ok(confirm_help)
    assert "--confirmation" in confirm_help.stdout


# --------------------------------------------------------------------------------------------------
# invites resend


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_resend_text_and_json(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                              bot_api: Any) -> None:
    """Regras do reenvio (por 1 a 100 pseudônimos, uma vez por pessoa, em ordem de matrícula; as pessoas que
    não são alunas na ordem da primeira referência, sem repetição; só a contagem dos itens distintos sem
    pessoa, nunca ecoados); texto para pessoas (`Reenvios
    enfileirados em <code>: <queued>.`, uma linha `Não é aluno do oferecimento: <alias>` por item de
    `not_students`, e `Sem pessoa: <not_found_count>` só se maior que 0; `--json`: o corpo da API):
    antes da importação, só uma pessoa que não é aluna e um item sem pessoa → 0 enfileirados, 200 e
    `student.invites_queued` com 0; depois, com a aluna A citada pelo apelido e pelo
    identificador, o aluno B, as pessoas C e D (não alunas,
    C citada duas vezes) e dois itens sem pessoa (um repetido), `--valid-hours 48` → 2 enfileirados,
    C e D nessa ordem e 2 sem pessoa; sem `--valid-hours` (o campo não vai no pedido), 168 h e só a
    primeira linha; os envios são `coordination`/`pending` com a validade pedida, com tarefa, e saem
    os `student.invites_queued` exatos."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-reenvio-cli")
    code = offering["code"]
    a = register_person(client, ADMIN_TOKEN, name="Aluna Reenvio Alfa", ra="5530000101",
                        email="reenvio.alfa@turma.example.org")
    b = register_person(client, ADMIN_TOKEN, name="Aluno Reenvio Beta", ra="5530000102",
                        email="reenvio.beta@turma.example.org")
    c = register_person(client, ADMIN_TOKEN, name="Pessoa Fora Gama")
    d = register_person(client, ADMIN_TOKEN, name="Pessoa Fora Delta", ra="5530000104")

    clock.advance(30)
    none_at = iso(clock.now())
    with _recorded_requests(client) as seen:
        none = cli_invoke(["invites", "resend", code, c["alias"], NOT_FOUND_ALIAS], json=json_mode)
    _ok(none)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/resend",
                            {"pseudonyms": [c["alias"], NOT_FOUND_ALIAS]})]
    none_body = {"offering": _ref(offering), "queued": 0, "valid_hours": 168,
                 "not_students": [{"pseudo_id": c["pseudo_id"], "alias": c["alias"]}], "not_found_count": 1}
    if json_mode:
        body = json.loads(none.stdout)
        assert_matches("invites.resend", 200, body)
        assert body == none_body
    else:
        assert none.stdout == _out(f"Reenvios enfileirados em {code}: 0.",
                                   f"Não é aluno do oferecimento: {c['alias']}", "Sem pessoa: 1")

    imported = import_students(client, ADMIN_TOKEN, code, [
        {"name": "Aluna Reenvio Alfa", "ra": "5530000101", "email": "reenvio.alfa@turma.example.org"},
        {"name": "Aluno Reenvio Beta", "ra": "5530000102", "email": "reenvio.beta@turma.example.org"}])
    assert imported == {"offering": _ref(offering), "received": 2, "people_created": 0, "people_reused": 2,
                        "enrollments_created": 2, "already_enrolled": 0}
    missing_uuid = str(uuid4_with_prefix("eeee", fill="9"))
    refs = [a["alias"], b["pseudo_id"], c["alias"], NOT_FOUND_ALIAS, d["pseudo_id"], a["pseudo_id"],
            NOT_FOUND_ALIAS, c["pseudo_id"], missing_uuid]

    clock.advance(30)
    first_at = iso(clock.now())
    with _recorded_requests(client) as seen:
        first = cli_invoke(["invites", "resend", code, *refs, "--valid-hours", "48"], json=json_mode)
    _ok(first)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/resend",
                            {"pseudonyms": refs, "valid_hours": 48})]
    first_body = {"offering": _ref(offering), "queued": 2, "valid_hours": 48,
                  "not_students": [{"pseudo_id": c["pseudo_id"], "alias": c["alias"]},
                                   {"pseudo_id": d["pseudo_id"], "alias": d["alias"]}],
                  "not_found_count": 2}
    if json_mode:
        body = json.loads(first.stdout)
        assert_matches("invites.resend", 200, body)
        assert body == first_body
    else:
        assert first.stdout == _out(f"Reenvios enfileirados em {code}: 2.",
                                    f"Não é aluno do oferecimento: {c['alias']}",
                                    f"Não é aluno do oferecimento: {d['alias']}",
                                    "Sem pessoa: 2")
    assert NOT_FOUND_ALIAS not in first.stdout and missing_uuid not in first.stdout

    clock.advance(30)
    second_at = iso(clock.now())
    with _recorded_requests(client) as seen:
        second = cli_invoke(["invites", "resend", code, a["pseudo_id"]], json=json_mode)
    _ok(second)
    assert _sent(seen) == [("POST", f"/v1/offerings/{code}/invites/resend", {"pseudonyms": [a["pseudo_id"]]})]
    second_body = {"offering": _ref(offering), "queued": 1, "valid_hours": 168, "not_students": [],
                   "not_found_count": 0}
    if json_mode:
        body = json.loads(second.stdout)
        assert_matches("invites.resend", 200, body)
        assert body == second_body
    else:
        assert second.stdout == _out(f"Reenvios enfileirados em {code}: 1.")

    sends = _invite_sends(sql, code)
    assert [s[1] for s in sends[:2]] == _student_enrollment_ids(sql, code)
    assert sorted(s[2] for s in sends[:2]) == sorted([a["pseudo_id"], b["pseudo_id"]])
    assert [s[3:] for s in sends[:2]] == [("coordination", 48, "pending", 0)] * 2
    assert [s[2:] for s in sends[2:]] == [(a["pseudo_id"], "coordination", 168, "pending", 0)]
    assert _invite_email_tasks(sql) == [("pending", {"invite_send_id": s[0]}, f"invite.email:{s[0]}")
                                        for s in sends]
    assert _events(client, "student.invites_queued") == [_queued_event(offering, "coordination", 0, none_at),
                                                        _queued_event(offering, "coordination", 2, first_at),
                                                        _queued_event(offering, "coordination", 1, second_at)]
    for result in (none, first, second):
        for value in ("Reenvio Alfa", "Reenvio Beta", "Fora Gama", "Fora Delta", "reenvio.alfa", "5530000104"):
            assert value.casefold() not in result.stdout.casefold()


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_resend_api_errors(json_mode: bool, cli_invoke: Any, client: Any, clock: Any, sql: Any,
                           bot_api: Any) -> None:
    """As condições do oferecimento (ativo, com bot com acesso) e as regras do reenvio; os casos
    (entradas inválidas; erros 404, 409 `invalid_state`, 409 `bot_required`) e os erros
    (na forma comum do CLI): oferecimento inexistente, encerrado (`Estado atual: closed`) e com o bot sem
    acesso; item fora do formato (`  - pseudonyms[1]: invalid_format`, sem eco), 101 itens com
    repetição (`  - pseudonyms: too_many`) e `--valid-hours` 0 e 721 (`  - valid_hours: out_of_range`)
    → código 1 e o stderr exato (ou o corpo de erro com `--json`); nada é enfileirado."""
    active, _ = _active_offering(client, bot_api, clock, "mc102-reenvio-erros")
    closed, _ = _active_offering(client, bot_api, clock, "mc102-reenvio-encerrado")
    _close(client, closed["code"])
    no_access, telegram_bot_id = _active_offering(client, bot_api, clock, "mc102-reenvio-sem-acesso")
    _set_no_access(sql, telegram_bot_id)
    code = active["code"]
    cases = [
        (["invites", "resend", "codigo-inexistente", "abcd"], _error("not_found")),
        (["invites", "resend", closed["code"], "abcd"], _error("invalid_state", state="closed")),
        (["invites", "resend", no_access["code"], "abcd"], _error("bot_required")),
        (["invites", "resend", code, "abcd", "ITEMSENTINELA"],
         _error("invalid_request", fields=[("pseudonyms[1]", "invalid_format")])),
        (["invites", "resend", code, *(["abcd"] * 101)], _error("invalid_request", fields=[("pseudonyms", "too_many")])),
        (["invites", "resend", code, "abcd", "--valid-hours", "0"],
         _error("invalid_request", fields=[("valid_hours", "out_of_range")])),
        (["invites", "resend", code, "abcd", "--valid-hours", "721"],
         _error("invalid_request", fields=[("valid_hours", "out_of_range")])),
    ]
    for args, (body, lines) in cases:
        result = cli_invoke(args, json=json_mode)
        _assert_api_error(result, json_mode, body, lines)
        assert "itemsentinela" not in result.stderr.casefold()
    assert _all_invite_sends(sql) == []
    assert _events(client, "student.invites_queued") == []


def test_resend_usage_errors_exit_2_without_request(cli_invoke: Any, client: Any, clock: Any,
                                                    bot_api: Any) -> None:
    """Contrato (`cq invites resend REF PSEUDONYM... [--valid-hours N]`: o argumento variádico é
    obrigatório e só `--valid-hours` é tipado, inteiro) e a forma geral (uso incorreto sem eco): sem
    pseudônimo, sem `REF` nem pseudônimo, `--valid-hours` não inteiro e sem o valor de `--valid-hours`
    → código 2, a mensagem exata (também com `--json`) e nenhum pedido à API; na mesma função,
    `cq invites resend --help` sai com 0 e cita `--valid-hours`."""
    offering, _ = _active_offering(client, bot_api, clock, "mc102-reenvio-uso")
    code = offering["code"]
    cases = [
        (["invites", "resend", code], "falta um argumento obrigatório."),
        (["invites", "resend"], "falta um argumento obrigatório."),
        (["invites", "resend", code, "abcd", "--valid-hours", "horas-sentinela"],
         "valor inválido na opção --valid-hours."),
        (["invites", "resend", code, "abcd", "--valid-hours"], "falta o valor da opção --valid-hours."),
    ]
    for args, message in cases:
        for json_mode in (False, True):
            with _recorded_requests(client) as seen:
                result = cli_invoke(args, json=json_mode)
            _failed(result, 2)
            assert result.stderr == _out(f"Uso incorreto: {message}"), args
            assert seen == [], args
            assert "sentinela" not in result.stderr

    help_result = cli_invoke(["invites", "resend", "--help"])
    _ok(help_result)
    assert "--valid-hours" in help_result.stdout
