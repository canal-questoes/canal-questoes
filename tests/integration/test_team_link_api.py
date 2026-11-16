"""Link de equipe pela API (link de equipe
sem pessoa ou sem bot e link antes da ativação).

Contratos: os erros e a ordem das verificações de oferecimentos e bots e
`team.link`; a matrícula, o convite, a geração do link e os eventos (
`team.link_created` e `team.link_revoked`).

Convenções: corpo inteiro por `==` e `assert_matches` em toda resposta de sucesso;
horários pelo `FakeClock` e `iso`; ids internos nunca como literal (as linhas do banco são lidas
por junção com `pseudo_id` e `offering_id`); o link e o código de convite entram em
`remember_secret` antes de qualquer outra conferência. Nenhum teste aqui roda worker: a
Bot API falsa só atende o `getMe` do registro do bot.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    TEAM_LINK_SHAPE,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_token,
    link_code,
    register_bot,
    register_person,
)
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import remember_secret

ADMIN = auth(ADMIN_TOKEN)

# Validade padrão do link: 7 dias.
DEFAULT_HOURS = 168

# Pseudônimos de formato válido que nunca pertencem a alguém (o apelido real teria 4 caracteres).
MISSING_ALIAS = "ffff0000ffff"
MISSING_PSEUDO_ID = "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"
MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"

# Tipos de evento da API base e de oferecimentos e bots, como literais (o esqueleto ainda não os tem).
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
LINK_EVENT_TYPES = ("team.link_created", "team.link_revoked")


# ---------------------------------------------------------------------------------------------
# Apoio


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _context(label: str) -> Iterator[None]:
    """Prefixa a falha com o caso do laço."""
    try:
        yield
    except AssertionError as exc:
        raise AssertionError(f"[{label}] {exc}") from exc


def _offering_with_bot(client: Any, bot_api: Any, code: str, username: str,
                       **fields: Any) -> tuple[dict, int]:
    """Oferecimento em rascunho com bot registrado na Bot API falsa (o único `getMe` do teste)."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, **fields)
    bot_id = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_id))
    return offering, bot_id


def _post_link(client: Any, ref: str, body: Any, *, token: str | None = ADMIN_TOKEN) -> Any:
    """`POST /v1/offerings/{ref}/team-links`; um link devolvido entra logo em `remember_secret`."""
    resp = client.post(f"/v1/offerings/{ref}/team-links", json=body, headers=auth(token))
    if resp.status_code == 201:
        link = resp.json().get("link")
        if isinstance(link, str):
            remember_secret(link)
            match = TEAM_LINK_SHAPE.fullmatch(link)
            if match is not None:
                remember_secret(match.group(2))
    return resp


def _created(resp: Any) -> dict:
    assert resp.status_code == 201, f"link de equipe falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("team.link", 201, body)
    return body


def _expected_body(offering: dict, person: dict, username: str, code: str, expires_at: Any) -> dict:
    return {
        "link": f"https://t.me/{username}?start={code}",
        "expires_at": iso(expires_at),
        "person": {"pseudo_id": person["pseudo_id"], "alias": person["alias"]},
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"],
                     "timezone": offering["timezone"]},
    }


def _enrollments(sql: Any) -> list[tuple]:
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, e.role, e.state, e.created_at, e.activated_at
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         ORDER BY e.id
        """).fetchall()


def _invites(sql: Any) -> list[tuple]:
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, i.code_sha256, i.state, i.expires_at,
               i.created_at, i.used_at, i.revoked_at
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         ORDER BY i.id
        """).fetchall()


def _team_state(sql: Any) -> dict:
    """O que um pedido recusado não pode mudar: matrículas, convites e eventos."""
    return {
        "enrollments": _enrollments(sql),
        "invites": _invites(sql),
        "events": sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall(),
    }


def _link_events(client: Any) -> list[dict]:
    """Os eventos de link pela API, sem o `seq` (conferido só como crescente)."""
    events = all_events(client, ADMIN_TOKEN, *LINK_EVENT_TYPES)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _created_event(offering: dict, person: dict, at: Any, expires_at: Any,
                   actor: str | None = "bootstrap") -> dict:
    return {"type": "team.link_created", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": offering["offering_id"], "person": person["pseudo_id"],
                     "expires_at": iso(expires_at)}}


def _revoked_event(offering: dict, person: dict, at: Any, reason: str) -> dict:
    return {"type": "team.link_revoked", "occurred_at": iso(at), "actor_token_id": "bootstrap",
            "data": {"offering_id": offering["offering_id"], "person": person["pseudo_id"],
                     "revoke_reason": reason}}


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _close(client: Any, ref: str) -> None:
    body = _ok(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")
    assert body["state"] == "closed"


def _archive(client: Any, ref: str) -> None:
    """Arquivamento em duas fases pela API."""
    prepared = _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN),
                   "offerings.prepare_archive")
    body = _ok(client.post(f"/v1/offerings/{ref}/archive",
                           json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN),
               "offerings.archive")
    assert body["state"] == "archived"


# ---------------------------------------------------------------------------------------------
# Sucesso


def test_team_link_returns_exact_body_and_stores_only_the_digest(client: Any, clock: Any, sql: Any,
                                                                 bot_api: Any, db_url: str) -> None:
    """201 com o corpo exato (link
    `https://t.me/<username>?start=<código>`, `expires_at` = agora + 168 h, `person` e `offering` com
    o fuso); o código é aleatório de 128 bits em base64url (22 caracteres); a matrícula `team` nasce
    `pending`; o convite guarda só o SHA-256 do código (nenhuma coluna de nenhuma tabela tem o código
    nem o link, e o resumo só está em `invites.code_sha256`); evento `team.link_created` exato."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-equipe", "cq_equipe_link_bot",
                                     timezone="America/Manaus")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Professora Link", ra="5500000101")
    clock.advance(90)
    at = clock.now()
    expires_at = at + timedelta(hours=DEFAULT_HOURS)

    body = _created(_post_link(client, offering["code"], {"person": person["alias"]}))
    code = link_code(body["link"])
    assert body == _expected_body(offering, person, "cq_equipe_link_bot", code, expires_at)
    assert body["offering"]["timezone"] == "America/Manaus"
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", code)
    assert len(base64.urlsafe_b64decode(code + "==")) == 16

    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "pending", at, None)]
    assert _invites(sql) == [(person["pseudo_id"], offering["offering_id"], _sha256(code), "pending",
                              expires_at, at, None, None)]

    scan = scan_all_tables(db_url)
    assert [(t, c) for t, c, text in scan if code in text] == []
    assert [(t, c) for t, c, text in scan if body["link"] in text] == []
    assert [(t, c) for t, c, text in scan if _sha256(code) in text] == [("invites", "code_sha256")]

    assert _link_events(client) == [_created_event(offering, person, at, expires_at)]

    # Outro oferecimento, outro fuso, outro bot: o link usa o `username` do bot do oferecimento.
    other, _ = _offering_with_bot(client, bot_api, "mc202-equipe", "cq_outro_link_bot",
                                  timezone="Asia/Kolkata")
    body2 = _created(_post_link(client, other["code"], {"person": person["alias"]}))
    code2 = link_code(body2["link"])
    assert code2 != code
    assert body2 == _expected_body(other, person, "cq_outro_link_bot", code2, expires_at)


@pytest.mark.parametrize("hours", [1, 720, DEFAULT_HOURS], ids=["minimo-1h", "maximo-720h", "padrao-explicito"])
def test_valid_hours_limits_are_accepted(hours: int, client: Any, clock: Any, sql: Any,
                                         bot_api: Any) -> None:
    """`valid_hours` 1 e 720 (e o padrão 168 dado de forma explícita) são
    aceitos, com `expires_at` = agora + `valid_hours` horas na resposta, no convite e no evento."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-validade", "cq_validade_link_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Validade Link", ra="5500000102")
    at = clock.now()
    expires_at = at + timedelta(hours=hours)

    body = _created(_post_link(client, offering["code"], {"person": person["alias"], "valid_hours": hours}))
    code = link_code(body["link"])
    assert body == _expected_body(offering, person, "cq_validade_link_bot", code, expires_at)
    assert _invites(sql) == [(person["pseudo_id"], offering["offering_id"], _sha256(code), "pending",
                              expires_at, at, None, None)]
    assert _link_events(client) == [_created_event(offering, person, at, expires_at)]


@pytest.mark.parametrize("value, reason", [
    pytest.param(0, "out_of_range", id="zero"),
    pytest.param(721, "out_of_range", id="721"),
    pytest.param(-5, "out_of_range", id="negativo"),
    pytest.param(True, "invalid_format", id="booleano"),
    pytest.param(1.5, "invalid_format", id="fracionario"),
    pytest.param("24", "invalid_format", id="texto"),
])
def test_valid_hours_out_of_limits_is_refused_without_change(value: Any, reason: str, client: Any,
                                                             sql: Any, bot_api: Any) -> None:
    """`valid_hours` fora de 1..720 → `[{valid_hours, out_of_range}]`; não
    inteiro, inclusive booleano, → `[{valid_hours, invalid_format}]`; nada é gravado (sem matrícula,
    convite nem evento)."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-invalida", "cq_invalida_link_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Validade Recusada", ra="5500000103")
    before = _team_state(sql)

    resp = _post_link(client, offering["code"], {"person": person["alias"], "valid_hours": value})
    assert_error(resp, 400, "invalid_request", fields=[("valid_hours", reason)])
    assert _team_state(sql) == before
    assert _enrollments(sql) == [] and _invites(sql) == []


def test_new_link_revokes_the_pending_one_and_reuses_the_enrollment(client: Any, clock: Any, sql: Any,
                                                                   bot_api: Any) -> None:
    """gerar outro link para a mesma pessoa (agora pelo `pseudo_id`)
    revoga o convite pendente anterior (`revoked`, `revoked_at` = agora) e reusa a matrícula (uma só
    linha, `pending`); eventos, na ordem: `team.link_created`, `team.link_revoked` (`replaced`) e
    `team.link_created`. O link de outra pessoa no mesmo oferecimento não revoga nada."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-troca", "cq_troca_link_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Troca Link", ra="5500000104")
    other = register_person(client, ADMIN_TOKEN, name="Pessoa Outra Troca", ra="5500000105")
    t1 = clock.now()
    first = _created(_post_link(client, offering["code"], {"person": person["alias"]}))
    code1 = link_code(first["link"])

    clock.advance(3600)
    t2 = clock.now()
    second = _created(_post_link(client, offering["code"], {"person": person["pseudo_id"]}))
    code2 = link_code(second["link"])
    assert code2 != code1
    assert second == _expected_body(offering, person, "cq_troca_link_bot", code2,
                                    t2 + timedelta(hours=DEFAULT_HOURS))

    assert _enrollments(sql) == [(person["pseudo_id"], offering["offering_id"], "team", "pending", t1, None)]
    assert _invites(sql) == [
        (person["pseudo_id"], offering["offering_id"], _sha256(code1), "revoked",
         t1 + timedelta(hours=DEFAULT_HOURS), t1, None, t2),
        (person["pseudo_id"], offering["offering_id"], _sha256(code2), "pending",
         t2 + timedelta(hours=DEFAULT_HOURS), t2, None, None),
    ]
    assert _link_events(client) == [
        _created_event(offering, person, t1, t1 + timedelta(hours=DEFAULT_HOURS)),
        _revoked_event(offering, person, t2, "replaced"),
        _created_event(offering, person, t2, t2 + timedelta(hours=DEFAULT_HOURS)),
    ]

    clock.advance(60)
    t3 = clock.now()
    third = _created(_post_link(client, offering["code"], {"person": other["alias"]}))
    code3 = link_code(third["link"])
    assert [row[3] for row in _invites(sql)] == ["revoked", "pending", "pending"]
    assert _invites(sql)[2] == (other["pseudo_id"], offering["offering_id"], _sha256(code3), "pending",
                                t3 + timedelta(hours=DEFAULT_HOURS), t3, None, None)
    assert _enrollments(sql) == [
        (person["pseudo_id"], offering["offering_id"], "team", "pending", t1, None),
        (other["pseudo_id"], offering["offering_id"], "team", "pending", t3, None),
    ]
    assert _link_events(client)[3:] == [_created_event(offering, other, t3, t3 + timedelta(hours=DEFAULT_HOURS))]


@pytest.mark.parametrize("state", ["draft", "active", "closed", "no_access"])
def test_link_is_accepted_in_every_non_archived_state(state: str, client: Any, clock: Any, sql: Any,
                                                      bot_api: Any) -> None:
    """Link antes da ativação: o link sai (201) no
    rascunho, no ativo e no encerrado; um bot `no_access` é aceito (só a falta de bot é
    `bot_required`)."""
    offering, bot_id = _offering_with_bot(client, bot_api, f"mc102-estado-{state.replace('_', '-')}",
                                          "cq_estado_link_bot")
    if state in ("active", "closed"):
        activate_bot(client, ADMIN_TOKEN, offering["code"])
    if state == "closed":
        _close(client, offering["code"])
    if state == "no_access":
        # Pré-condição direta no banco (nenhum worker neste arquivo): o Telegram recusou o token.
        sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                    "WHERE telegram_bot_id = %s", (bot_id,))
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Estado Link", ra="5500000106")
    at = clock.now()

    body = _created(_post_link(client, offering["code"], {"person": person["alias"]}))
    code = link_code(body["link"])
    expires_at = at + timedelta(hours=DEFAULT_HOURS)
    assert body == _expected_body(offering, person, "cq_estado_link_bot", code, expires_at)
    assert _invites(sql) == [(person["pseudo_id"], offering["offering_id"], _sha256(code), "pending",
                              expires_at, at, None, None)]
    assert _link_events(client) == [_created_event(offering, person, at, expires_at)]


# ---------------------------------------------------------------------------------------------
# Recusas


def test_refusals_follow_the_contract_order(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Ordem das verificações (404 do oferecimento → 404 da pessoa → `invalid_state` → `bot_required`);
    link de equipe sem pessoa ou sem bot: oferecimento inexistente
    (por código e por identificador) → 404; pessoa inexistente → 404, inclusive num arquivado e num
    rascunho sem bot; arquivado (com ou sem bot) → 409 `invalid_state` com `state = archived`; sem bot
    → 409 `bot_required`. Nada muda em nenhum caso."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Recusa Link", ra="5500000107")
    archived, _ = _offering_with_bot(client, bot_api, "mc102-arquivado", "cq_arquivado_link_bot")
    _archive(client, archived["code"])
    archived_no_bot = create_offering(client, ADMIN_TOKEN, code="mc102-arquivado-sem-bot")
    _archive(client, archived_no_bot["code"])
    no_bot = create_offering(client, ADMIN_TOKEN, code="mc102-sem-bot")
    before = _team_state(sql)

    cases = [
        ("oferecimento inexistente por código", "mc999-inexistente", person["alias"], 404, "not_found", None),
        ("oferecimento e pessoa inexistentes", "mc999-inexistente", MISSING_ALIAS, 404, "not_found", None),
        ("oferecimento inexistente por id", MISSING_OFFERING_ID, person["alias"], 404, "not_found", None),
        ("arquivado com pessoa inexistente", archived["code"], MISSING_ALIAS, 404, "not_found", None),
        ("arquivado com pseudo_id inexistente", archived["offering_id"], MISSING_PSEUDO_ID, 404, "not_found",
         None),
        ("arquivado", archived["code"], person["alias"], 409, "invalid_state", "archived"),
        ("arquivado sem bot", archived_no_bot["code"], person["alias"], 409, "invalid_state", "archived"),
        ("sem bot com pessoa inexistente", no_bot["code"], MISSING_ALIAS, 404, "not_found", None),
        ("sem bot", no_bot["code"], person["alias"], 409, "bot_required", None),
        ("sem bot por id", no_bot["offering_id"], person["pseudo_id"], 409, "bot_required", None),
    ]
    for label, ref, who, status, code, state in cases:
        with _context(label):
            resp = _post_link(client, ref, {"person": who})
            if state is None:
                assert_error(resp, status, code)
            else:
                assert_error(resp, status, code, state=state)
            assert _team_state(sql) == before


@pytest.mark.parametrize("body, fields", [
    pytest.param({}, [("person", "missing")], id="sem-person"),
    pytest.param({"person": "pessoa-invalida!"}, [("person", "invalid_format")], id="person-fora-do-formato"),
    pytest.param({"person": 12345678}, [("person", "invalid_format")], id="person-nao-texto"),
    pytest.param({"person": "ffff0000ffff", "campo_sentinela": "valor-sentinela-1"},
                 [("body", "unknown_field")], id="campo-extra"),
])
def test_invalid_request_is_400_without_echo_or_change(body: dict, fields: list, client: Any, sql: Any,
                                                       bot_api: Any) -> None:
    """Entrada estrita, como na API base: `person` ausente → `missing`; fora do
    formato de pseudônimo ou não texto → `invalid_format`; campo extra → `[{body, unknown_field}]`;
    sem eco dos valores e sem mudança. Também `ref` fora do formato → `[{ref, invalid_format}]`."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-entrada", "cq_entrada_link_bot")
    before = _team_state(sql)

    resp = _post_link(client, offering["code"], body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    echoes = [v for v in (*body.keys(), *body.values()) if isinstance(v, str) and len(v) >= 8]
    assert_no_echo(resp, *echoes)
    assert _team_state(sql) == before

    resp = _post_link(client, "Oferta_Com_Formato_Invalido", {"person": "abcd"})
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_no_echo(resp, "Oferta_Com_Formato_Invalido")
    assert _team_state(sql) == before


def test_authentication_and_permission(client: Any, clock: Any, sql: Any, bot_api: Any) -> None:
    """Ordem (401 → 400 → 403 → regras): sem token → 401; token sem
    `offerings:write` → 403 com `permission = offerings:write` (também para oferecimento inexistente,
    porque a permissão vem antes das regras), mas o pedido inválido é 400 antes do 403; um token com
    `offerings:write` gera o link, e o evento leva o `token_id` dele como ator."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-permissao", "cq_permissao_link_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Permissao Link", ra="5500000108")
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "bots:manage", "people:write"],
                             label="sem escrita de oferecimento")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escrita de oferecimento")
    before = _team_state(sql)

    assert_error(_post_link(client, offering["code"], {"person": person["alias"]}, token=None),
                 401, "unauthenticated")
    assert_error(_post_link(client, offering["code"], {"person": person["alias"]}, token="cq_" + "z" * 43),
                 401, "unauthenticated")
    assert_error(_post_link(client, offering["code"], {"person": person["alias"]}, token=reader),
                 403, "forbidden", permission="offerings:write")
    assert_error(_post_link(client, "mc999-inexistente", {"person": person["alias"]}, token=reader),
                 403, "forbidden", permission="offerings:write")
    assert_error(_post_link(client, offering["code"], {"person": person["alias"], "valid_hours": 0},
                            token=reader),
                 400, "invalid_request", fields=[("valid_hours", "out_of_range")])
    assert _team_state(sql) == before

    at = clock.now()
    body = _created(_post_link(client, offering["code"], {"person": person["alias"]}, token=writer))
    code = link_code(body["link"])
    expires_at = at + timedelta(hours=DEFAULT_HOURS)
    assert body == _expected_body(offering, person, "cq_permissao_link_bot", code, expires_at)
    assert _link_events(client) == [
        _created_event(offering, person, at, expires_at, actor=writer_body["token_id"])]


def test_the_201_body_is_the_only_api_output_with_the_code(client: Any, clock: Any, sql: Any,
                                                           bot_api: Any) -> None:
    """Credencial (o link é uma credencial; nenhum log leva código nem link): o
    código e o link só aparecem no corpo 201 de `team.link` (controle positivo); nenhuma outra saída
    da API os traz (consultas de oferecimento, bot, pessoas, tokens, todos os eventos da API base e de oferecimentos e bots e
    o resumo do arquivamento), nem o resumo SHA-256 do código; nenhum registro de log do pedido os
    contém."""
    offering, _ = _offering_with_bot(client, bot_api, "mc102-credencial", "cq_credencial_link_bot")
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Credencial Link", ra="5500000109")
    with capture_logs() as cap:
        resp = _post_link(client, offering["code"], {"person": person["alias"]})
    body = _created(resp)
    link = body["link"]
    code = link_code(link)
    digest = _sha256(code)
    assert code in resp.text and link in resp.text

    ref = offering["code"]
    outputs = {
        "offerings.get": _ok(client.get(f"/v1/offerings/{ref}", headers=ADMIN), "offerings.get"),
        "offerings.get(id)": _ok(client.get(f"/v1/offerings/{offering['offering_id']}", headers=ADMIN),
                                 "offerings.get"),
        "offerings.list": _ok(client.get("/v1/offerings", headers=ADMIN), "offerings.list"),
        "bots.get": _ok(client.get(f"/v1/offerings/{ref}/bot", headers=ADMIN), "bots.get"),
        "people.get": _ok(client.get(f"/v1/people/{person['alias']}", headers=ADMIN), "people.get"),
        "people.list": _ok(client.get("/v1/people", headers=ADMIN), "people.list"),
        "tokens.list": _ok(client.get("/v1/tokens", headers=ADMIN), "tokens.list"),
        "events.list": {"items": all_events(client, ADMIN_TOKEN, *CORE_EVENT_TYPES, *OFFERING_BOT_EVENT_TYPES)},
        "offerings.prepare_archive": _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN),
                                         "offerings.prepare_archive"),
    }
    assert outputs["offerings.prepare_archive"]["pending_links"] == 1
    for name, out in outputs.items():
        text = json.dumps(out, ensure_ascii=False)
        with _context(name):
            assert code not in text and link not in text and digest not in text
    assert code not in cap.text and link not in cap.text and digest not in cap.text
    assert uuid.UUID(body["offering"]["offering_id"]).version == 4
