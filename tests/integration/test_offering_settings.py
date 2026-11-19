"""Configuração de mensagens livres do oferecimento.

Cobre o aviso da prévia, a configuração das mensagens livres e o contato do professor, inclusive o contato do
professor que coincide com a identidade de um aluno. Contratos: o
`Offering` estendido, as regras do cadastro e da alteração e `offerings.update_settings`
(`PATCH /v1/offerings/{ref}/settings`); no esquema, o evento
`offering.settings_changed`. Escolhas de projeto [I]: o booleano em `offerings` e o contato em
`offering_contacts`, opcional; o e-mail fora do MCP; o e-mail como
identidade: só os booleanos saem; o contato que a redação alteraria é recusado sem eco; o validador do
e-mail da lista de alunos; o evento só quando algum valor muda; o cadastro gera só o `offering.created`;
o `Offering` com `free_messages` e `contact_email_set`; e o aviso `no_teacher_contact` na prévia
de um oferecimento com as mensagens livres desligadas e sem contato (a função
`test_preview_warns_when_free_messages_off_without_contact`).

Convenções (como nos testes de oferecimentos): corpo inteiro por `==` em toda resposta de sucesso, com `assert_matches`; ids e
cursores lidos da própria API; horários pelo `FakeClock` e `iso(dt)`; o e-mail do contato conferido no banco
(controle positivo) e ausente de toda resposta, evento e log (asserção negativa). Dados só fictícios: o
contato do professor em `docentes.example.org`, e a identidade de aluno das recusas é a do canário da API base
(`ZEFIRO`).

Vermelho sem a implementação (testes primeiro): o cadastro com `free_messages` ou
`contact_email` dá 400 `[{body, unknown_field}]`; a resposta do `Offering` não tem as duas chaves; a coluna
`offerings.free_messages` e a tabela `offering_contacts` não existem; a rota `PATCH .../settings` não existe
(404/405); o tipo `offering.settings_changed` não existe; a validação do disparo não calcula o aviso
`no_teacher_contact`.
"""

from __future__ import annotations

import contextlib
import io
import itertools
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_token,
    get_dispatch,
    list_events,
    prepare_dispatch,
    register_bot,
    register_person,
    update_offering_settings,
)
from tests.helpers.canary import ZEFIRO
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.logs import capture_logs

ADMIN = auth(ADMIN_TOKEN)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"

# Contatos de professor fictícios (guardados como informados, aparados).
PROF = "prof.livres@docentes.example.org"
PROF_OTHER = "prof.outra@docentes.example.org"
PROF_SPACED = "  prof.x@docentes.example.org "
PROF_TRIMMED = "prof.x@docentes.example.org"
assert PROF_SPACED.strip() == PROF_TRIMMED

# Até 254 caracteres depois de aparado; 255 → `too_long` (o tamanho é
# conferido antes do formato pelo validador de e-mail da lista de alunos).
_DOMAIN = "@docentes.example.org"
EMAIL_255 = "p" * (255 - len(_DOMAIN)) + _DOMAIN
assert len(EMAIL_255) == 255

# O aviso da prévia, com a mensagem exata, por último na lista.
NO_TEACHER_CONTACT = "no_teacher_contact"
NO_TEACHER_CONTACT_MESSAGE = (
    "As mensagens livres estão desligadas e o oferecimento não tem contato do professor: o aluno que "
    "escrever fora das questões não saberá a quem se dirigir.")

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def _body(code: str, **extra: Any) -> dict:
    """Corpo de cadastro com os 4 campos obrigatórios e os campos extras dados."""
    return {"code": code, "course": COURSE, "term": TERM, "timezone": TZ, **extra}


def _settings_path(ref: str) -> str:
    return f"/v1/offerings/{ref}/settings"


def _patch(client: Any, ref: str, body: Any = None, *, token: str | None = ADMIN_TOKEN,
           raw: bool = False) -> Any:
    """`PATCH /v1/offerings/{ref}/settings` direto (os casos de erro; o sucesso usa o atalho do arnês)."""
    if raw:
        return client.patch(_settings_path(ref), headers=auth(token))
    return client.patch(_settings_path(ref), json=body, headers=auth(token))


def _offering(created: dict, *, free_messages: bool, contact_email_set: bool, **changes: Any) -> dict:
    """O `Offering` esperado: o do cadastro, com os dois booleanos e as trocas dadas."""
    return {**created, "free_messages": free_messages, "contact_email_set": contact_email_set, **changes}


def _draft(offering_id: str, code: str, created_at: str, *, free_messages: bool = False,
           contact_email_set: bool = False) -> dict:
    """`Offering` exato de um rascunho recém-cadastrado."""
    return {"offering_id": offering_id, "code": code, "course": COURSE, "term": TERM, "timezone": TZ,
            "response_window_minutes": 1440, "state": "draft", "created_at": created_at,
            "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
            "free_messages": free_messages, "contact_email_set": contact_email_set,
            # `purged_at` fica nulo sem expurgo.
            "purged_at": None}


def _created(resp: Any) -> dict:
    assert resp.status_code == 201, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.create", 201, body)
    return body


def _get(client: Any, ref: str) -> dict:
    resp = client.get(f"/v1/offerings/{ref}", headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get", 200, body)
    return body


def _listed(client: Any, code: str) -> tuple[Any, dict]:
    """(resposta, item do oferecimento `code`) de `offerings.list`."""
    resp = client.get("/v1/offerings", params={"limit": 500}, headers=ADMIN)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.list", 200, body)
    items = [item for item in body["items"] if item["code"] == code]
    assert len(items) == 1, f"oferecimento {code} ausente da lista"
    return resp, items[0]


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _row(sql: psycopg.Connection, code: str) -> tuple:
    """(`offerings.free_messages`, `offering_contacts.email`, `offering_contacts.updated_at`) do
    oferecimento `code`; sem contato, os dois últimos são nulos."""
    row = sql.execute(
        "SELECT o.free_messages, c.email, c.updated_at FROM offerings o "
        "LEFT JOIN offering_contacts c ON c.offering_id = o.id WHERE o.code = %s", (code,)).fetchone()
    assert row is not None, f"oferecimento {code} ausente do banco"
    return tuple(row)


def _contacts(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT count(*) FROM offering_contacts").fetchone()[0]


def _event_types(sql: psycopg.Connection) -> list[str]:
    """Os tipos de todos os eventos gravados, em ordem de `seq` (direto no banco: não depende de o tipo novo
    já existir na consulta da API)."""
    return [row[0] for row in sql.execute("SELECT type FROM events ORDER BY seq").fetchall()]


def _events_text(sql: psycopg.Connection) -> str:
    return "\n".join(row[0] for row in sql.execute("SELECT data::text FROM events ORDER BY seq").fetchall())


def _settings_events(client: Any) -> list[dict]:
    """Os `offering.settings_changed` pela API, em ordem de `seq`, sem o `seq`."""
    events = all_events(client, ADMIN_TOKEN, "offering.settings_changed")
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _changed(offering: dict, at: Any, actor: str, *, free_messages: bool, contact_email_set: bool) -> dict:
    """O `offering.settings_changed` exato: sem o e-mail, com o token de quem pediu."""
    return {"type": "offering.settings_changed", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": offering["offering_id"], "free_messages": free_messages,
                     "contact_email_set": contact_email_set}}


def _no_email(resp: Any, *emails: str) -> None:
    """Nenhum e-mail de contato na resposta (corpo e cabeçalhos), nem a chave `contact_email` no corpo."""
    assert_no_echo(resp, *emails)
    assert "contact_email\"" not in resp.text.replace("contact_email_set\"", ""), (
        f"a resposta traz a chave contact_email: {resp.text[:300]}")


def _close(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/close", headers=ADMIN)


def _prepare_archive(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)


def _archive(client: Any, ref: str, confirmation_code: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": confirmation_code},
                       headers=ADMIN)


def _archived(client: Any, ref: str) -> dict:
    prepared = _ok(_prepare_archive(client, ref), "offerings.prepare_archive")
    body = _ok(_archive(client, ref, prepared["confirmation_code"]), "offerings.archive")
    assert body["state"] == "archived"
    return body


def _activated(client: Any, bot_api: Any, ref: str) -> dict:
    register_bot(client, ADMIN_TOKEN, ref, bot_api.token_of(bot_api.add_bot()))
    body = activate_bot(client, ADMIN_TOKEN, ref)
    assert body["state"] == "active"
    return body


# ---------------------------------------------------------------------------------------------
# Cadastro (offerings.create estendido)
# ---------------------------------------------------------------------------------------------


def test_create_defaults_to_off_without_contact(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Desligada por padrão (omitidos os dois campos, a opção fica desligada e sem contato, e os pedidos sem eles
    continuam válidos): o cadastro sem os dois campos → 201 com o `Offering`
    exato, `free_messages: false` e `contact_email_set: false`; a consulta devolve o mesmo corpo;
    `offerings.free_messages` é falso e não há linha em `offering_contacts`; o único evento é o
    `offering.created` exato, só com `{offering_id, code}`."""
    code = _code("padrao")
    resp = client.post("/v1/offerings", json=_body(code), headers=ADMIN)
    created = _created(resp)
    assert created == _draft(created["offering_id"], code, iso(clock.now()))
    assert _get(client, code) == created
    assert _row(sql, code) == (False, None, None)
    assert _contacts(sql) == 0
    events = all_events(client, ADMIN_TOKEN, "offering.created")
    assert [{k: v for k, v in e.items() if k != "seq"} for e in events] == [
        {"type": "offering.created", "occurred_at": iso(clock.now()), "actor_token_id": "bootstrap",
         "data": {"offering_id": created["offering_id"], "code": code}}]
    assert _event_types(sql) == ["offering.created"]


def test_create_accepts_settings_and_never_echoes_the_email(client: Any, clock: Any,
                                                            sql: psycopg.Connection) -> None:
    """O cadastro com os dois campos opcionais, com o e-mail aparado, guardado sem redação, só em
    `offering_contacts`:
    `free_messages: true` e `contact_email` com espaços nas pontas → 201 com `true`/`true`;
    `offering_contacts.email` aparado, com `updated_at` = agora (controle positivo); o e-mail não aparece no
    corpo do cadastro, nos eventos (só o `offering.created`), nos logs nem em `offerings.get/list`.
    Também: `free_messages: false` com `contact_email: null` → `false`/`false`, sem linha; e `free_messages:
    false` com contato → `false`/`true`."""
    code = _code("com-contato")
    with capture_logs() as logs:
        resp = client.post("/v1/offerings", json=_body(code, free_messages=True, contact_email=PROF_SPACED),
                           headers=ADMIN)
        created = _created(resp)
        got = client.get(f"/v1/offerings/{code}", headers=ADMIN)
        listed, item = _listed(client, code)
    assert created == _draft(created["offering_id"], code, iso(clock.now()), free_messages=True,
                             contact_email_set=True)
    _no_email(resp, PROF_TRIMMED)
    assert _ok(got, "offerings.get") == created
    _no_email(got, PROF_TRIMMED)
    assert item == created
    _no_email(listed, PROF_TRIMMED)
    assert _row(sql, code) == (True, PROF_TRIMMED, clock.now())
    assert PROF_TRIMMED not in logs.text, "o e-mail do contato apareceu nos logs"
    assert _event_types(sql) == ["offering.created"]
    assert PROF_TRIMMED not in _events_text(sql), "o e-mail do contato apareceu num evento"

    clock.advance(10)
    plain = _code("sem-contato")
    no_contact = _created(client.post("/v1/offerings", json=_body(plain, free_messages=False, contact_email=None),
                                      headers=ADMIN))
    assert no_contact == _draft(no_contact["offering_id"], plain, iso(clock.now()))
    assert _row(sql, plain) == (False, None, None)

    clock.advance(10)
    off = _code("desligado-com-contato")
    off_body = _created(client.post("/v1/offerings", json=_body(off, free_messages=False, contact_email=PROF),
                                    headers=ADMIN))
    assert off_body == _draft(off_body["offering_id"], off, iso(clock.now()), contact_email_set=True)
    assert _row(sql, off) == (False, PROF, clock.now())
    assert _contacts(sql) == 2
    assert _event_types(sql) == ["offering.created"] * 3


INVALID_CREATE_SETTINGS = [
    pytest.param({"free_messages": "sim"}, [("free_messages", "invalid_format")], id="free-messages-texto"),
    pytest.param({"free_messages": 1}, [("free_messages", "invalid_format")], id="free-messages-inteiro"),
    pytest.param({"free_messages": None}, [("free_messages", "invalid_format")], id="free-messages-null"),
    pytest.param({"contact_email": 5}, [("contact_email", "invalid_format")], id="contato-inteiro"),
    pytest.param({"contact_email": True}, [("contact_email", "invalid_format")], id="contato-booleano"),
    pytest.param({"contact_email": ""}, [("contact_email", "too_short")], id="contato-vazio"),
    pytest.param({"contact_email": "   "}, [("contact_email", "too_short")], id="contato-so-espacos"),
    pytest.param({"contact_email": EMAIL_255}, [("contact_email", "too_long")], id="contato-255"),
    pytest.param({"contact_email": "nao-e-email-sentinela"}, [("contact_email", "invalid_format")],
                 id="contato-sem-arroba"),
    pytest.param({"contact_email": "Nome Sentinela <a@b.org>"}, [("contact_email", "invalid_format")],
                 id="contato-com-nome-de-exibicao"),
    pytest.param({"code": "ab", "term": "", "free_messages": "sim", "contact_email": "   "},
                 [("code", "too_short"), ("term", "too_short"), ("free_messages", "invalid_format"),
                  ("contact_email", "too_short")], id="varios-erros-na-ordem-do-esquema"),
    pytest.param({"free_messages": "sim", "campo_extra_sentinela": 1},
                 [("free_messages", "invalid_format"), ("body", "unknown_field")],
                 id="campo-extra-no-fim"),
]


@pytest.mark.parametrize(("extra", "fields"), INVALID_CREATE_SETTINGS)
def test_create_rejects_invalid_settings_without_echo(client: Any, sql: psycopg.Connection, extra: dict,
                                                      fields: list[tuple[str, str]]) -> None:
    """`offerings.create` estendido: `free_messages` não booleano, inclusive
    `null` → `invalid_format`; `contact_email` não texto → `invalid_format`; vazio ou só espaços → `too_short`;
    mais de 254 depois de aparado → `too_long`; fora do formato, inclusive com nome de exibição →
    `invalid_format`, sem eco; na ordem do esquema, com os erros dos outros campos primeiro e o campo desconhecido
    no fim: 400 com `fields` exatos, sem eco do valor, e nada gravado (nem oferecimento, nem
    contato, nem evento)."""
    body = {**_body(_code("recusa")), **extra}
    resp = client.post("/v1/offerings", json=body, headers=ADMIN)
    assert_error(resp, 400, "invalid_request", fields=fields)
    sentinels = [v for v in extra.values() if isinstance(v, str) and len(v.strip()) >= 8]
    if sentinels:
        assert_no_echo(resp, *sentinels)
    assert sql.execute("SELECT count(*) FROM offerings").fetchone()[0] == 0
    assert _contacts(sql) == 0
    assert _event_types(sql) == []


# ---------------------------------------------------------------------------------------------
# Alteração (offerings.update_settings)
# ---------------------------------------------------------------------------------------------


def test_update_settings_changes_values_with_one_event_each(client: Any, clock: Any,
                                                            sql: psycopg.Connection) -> None:
    """Alteráveis depois: cada
    `PATCH` que muda algum valor devolve 200 com o `Offering` exato e grava um `offering.settings_changed`
    `{offering_id, free_messages, contact_email_set}` (sem o e-mail) com o token de quem pediu: ligar;
    definir o contato (com um token próprio de `offerings:write`, que vira o autor); os dois campos juntos
    (um evento só); trocar o e-mail por outro (os booleanos iguais, mas o valor mudou); remover o contato
    (`null`, linha apagada). Repetir o mesmo valor, ou remover um contato inexistente, devolve 200 sem
    evento e sem gravar (o `updated_at` do contato não muda). O banco acompanha cada passo, e nenhum
    evento traz o e-mail."""
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="escritor da configuracao")
    created = create_offering(client, ADMIN_TOKEN, code=_code("altera"), course=COURSE, term=TERM, timezone=TZ)
    code = created["code"]
    expected_events: list[dict] = []

    t1 = clock.advance(60)
    on = update_offering_settings(client, ADMIN_TOKEN, code, free_messages=True)
    assert on == _offering(created, free_messages=True, contact_email_set=False)
    expected_events.append(_changed(created, t1, "bootstrap", free_messages=True, contact_email_set=False))
    assert _row(sql, code) == (True, None, None)

    t2 = clock.advance(60)
    with_contact = update_offering_settings(client, writer, code, contact_email=PROF)
    assert with_contact == _offering(created, free_messages=True, contact_email_set=True)
    expected_events.append(_changed(created, t2, writer_body["token_id"], free_messages=True,
                                    contact_email_set=True))
    assert _row(sql, code) == (True, PROF, t2)

    clock.advance(60)
    assert update_offering_settings(client, ADMIN_TOKEN, code, free_messages=True) == with_contact
    assert update_offering_settings(client, ADMIN_TOKEN, code, contact_email=PROF) == with_contact
    assert update_offering_settings(client, ADMIN_TOKEN, code, free_messages=True,
                                    contact_email=PROF) == with_contact
    assert _row(sql, code) == (True, PROF, t2)
    assert _settings_events(client) == expected_events

    t3 = clock.advance(60)
    swapped = update_offering_settings(client, ADMIN_TOKEN, code, contact_email=PROF_OTHER)
    assert swapped == with_contact
    expected_events.append(_changed(created, t3, "bootstrap", free_messages=True, contact_email_set=True))
    assert _row(sql, code) == (True, PROF_OTHER, t3)

    t4 = clock.advance(60)
    both = update_offering_settings(client, ADMIN_TOKEN, code, free_messages=False, contact_email=PROF)
    assert both == _offering(created, free_messages=False, contact_email_set=True)
    expected_events.append(_changed(created, t4, "bootstrap", free_messages=False, contact_email_set=True))
    assert _row(sql, code) == (False, PROF, t4)

    t5 = clock.advance(60)
    removed = update_offering_settings(client, ADMIN_TOKEN, code, contact_email=None)
    assert removed == _offering(created, free_messages=False, contact_email_set=False)
    expected_events.append(_changed(created, t5, "bootstrap", free_messages=False, contact_email_set=False))
    assert _row(sql, code) == (False, None, None)
    assert _contacts(sql) == 0

    clock.advance(60)
    assert update_offering_settings(client, ADMIN_TOKEN, code, contact_email=None) == removed
    assert update_offering_settings(client, ADMIN_TOKEN, code, free_messages=False) == removed

    assert _get(client, code) == removed
    assert _settings_events(client) == expected_events
    assert _event_types(sql) == ["token.created", "offering.created"] + ["offering.settings_changed"] * 5
    events_text = _events_text(sql)
    assert PROF not in events_text and PROF_OTHER not in events_text


def test_update_settings_states_and_error_order(client: Any, clock: Any, sql: psycopg.Connection,
                                                bot_api: Any) -> None:
    """Em rascunho, ativo ou encerrado (arquivado: recusada); a
    tabela de entradas inválidas e a ordem da API base: 401 → 404/405 da rota → 400 da forma → 403 → regras: 404 do
    oferecimento → `invalid_state` → `[{contact_email, invalid_value}]`: `draft`, `active` e
    `closed` aceitam (200); `archived` → 409 `invalid_state` com `state: archived`; oferecimento inexistente →
    404; sem corpo → `[{body, missing}]`; `{}` → `[{body, too_few}]`; campo extra → `[{body, unknown_field}]`;
    os valores inválidos do cadastro, sem eco; `ref` fora do formato → `[{ref, invalid_format}]`; sem token →
    401; token só com `offerings:read` → 403 com `permission: offerings:write` (também num oferecimento
    inexistente), mas um e-mail mal formado com esse token dá 400 (a forma antes da permissão); `GET` no
    caminho → 405; num arquivado, um contato que é identidade de aluno dá `invalid_state` (a regra do estado
    antes da do contato), e num inexistente, 404. Nenhum pedido recusado grava valor ou evento."""
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="leitor da configuracao")
    register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())

    draft = create_offering(client, ADMIN_TOKEN, code=_code("estado-rascunho"), timezone=TZ)
    assert update_offering_settings(client, ADMIN_TOKEN, draft["code"], free_messages=True) == _offering(
        draft, free_messages=True, contact_email_set=False)

    active_created = create_offering(client, ADMIN_TOKEN, code=_code("estado-ativo"), timezone=TZ)
    active = _activated(client, bot_api, active_created["code"])
    assert update_offering_settings(client, ADMIN_TOKEN, active["code"], contact_email=PROF) == {
        **active, "free_messages": False, "contact_email_set": True}

    closed_created = create_offering(client, ADMIN_TOKEN, code=_code("estado-encerrado"), timezone=TZ)
    _activated(client, bot_api, closed_created["code"])
    closed = _ok(_close(client, closed_created["code"]), "offerings.close")
    assert closed["state"] == "closed"
    assert update_offering_settings(client, ADMIN_TOKEN, closed["code"], free_messages=True,
                                    contact_email=PROF) == {**closed, "free_messages": True,
                                                            "contact_email_set": True}

    archived_created = create_offering(client, ADMIN_TOKEN, code=_code("estado-arquivado"), timezone=TZ)
    archived = _archived(client, archived_created["code"])

    before_types = _event_types(sql)
    before_rows = {o["code"]: _row(sql, o["code"]) for o in (draft, active, closed, archived)}
    target = draft["code"]
    missing = "oferta-inexistente-cfg"

    assert_error(_patch(client, archived["code"], {"free_messages": True}), 409, "invalid_state",
                 state="archived")
    assert_error(_patch(client, archived["code"], {"contact_email": ZEFIRO.email}), 409, "invalid_state",
                 state="archived")
    assert_error(_patch(client, missing, {"free_messages": True}), 404, "not_found")
    assert_error(_patch(client, missing, {"contact_email": ZEFIRO.email}), 404, "not_found")

    assert_error(_patch(client, target, raw=True), 400, "invalid_request", fields=[("body", "missing")])
    assert_error(_patch(client, target, {}), 400, "invalid_request", fields=[("body", "too_few")])
    extra = _patch(client, target, {"free_messages": True, "campo_extra_sentinela": "valor-extra-sentinela"})
    assert_error(extra, 400, "invalid_request", fields=[("body", "unknown_field")])
    assert_no_echo(extra, "campo_extra_sentinela", "valor-extra-sentinela")
    invalid = [
        ({"free_messages": "sim"}, [("free_messages", "invalid_format")]),
        ({"free_messages": None}, [("free_messages", "invalid_format")]),
        ({"contact_email": 5}, [("contact_email", "invalid_format")]),
        ({"contact_email": ""}, [("contact_email", "too_short")]),
        ({"contact_email": "   "}, [("contact_email", "too_short")]),
        ({"contact_email": EMAIL_255}, [("contact_email", "too_long")]),
        ({"contact_email": "nao-e-email-sentinela"}, [("contact_email", "invalid_format")]),
        ({"contact_email": "Nome Sentinela <a@b.org>"}, [("contact_email", "invalid_format")]),
        ({"free_messages": 1, "contact_email": "nao-e-email-sentinela"},
         [("free_messages", "invalid_format"), ("contact_email", "invalid_format")]),
    ]
    for body, fields in invalid:
        resp = _patch(client, target, body)
        assert_error(resp, 400, "invalid_request", fields=fields)
        for value in body.values():
            if isinstance(value, str) and len(value.strip()) >= 8:
                assert_no_echo(resp, value)
    assert_error(_patch(client, "REF_INVALIDO", {"free_messages": True}), 400, "invalid_request",
                 fields=[("ref", "invalid_format")])

    assert_error(_patch(client, target, {"free_messages": False}, token=None), 401, "unauthenticated")
    assert_error(_patch(client, target, {"free_messages": False}, token=reader), 403, "forbidden",
                 permission="offerings:write")
    assert_error(_patch(client, missing, {"free_messages": False}, token=reader), 403, "forbidden",
                 permission="offerings:write")
    assert_error(_patch(client, archived["code"], {"free_messages": False}, token=reader), 403, "forbidden",
                 permission="offerings:write")
    malformed = _patch(client, missing, {"contact_email": "nao-e-email-sentinela"}, token=reader)
    assert_error(malformed, 400, "invalid_request", fields=[("contact_email", "invalid_format")])
    assert_no_echo(malformed, "nao-e-email-sentinela")
    assert_error(client.get(_settings_path(target), headers=ADMIN), 405, "method_not_allowed")

    assert _event_types(sql) == before_types
    assert {code: _row(sql, code) for code in before_rows} == before_rows
    assert _get(client, archived["code"]) == {**archived, "free_messages": False, "contact_email_set": False}


@pytest.mark.parametrize("operation", ["create", "update"])
def test_contact_that_is_a_student_identity_is_refused_without_echo(operation: str, client: Any, clock: Any,
                                                                    sql: psycopg.Connection) -> None:
    """O e-mail de contato que coincide com a identidade de um aluno cadastrado (a redação o alteraria) é
    recusado sem eco, no cadastro e na alteração, como o código do oferecimento.
    Regra conferida depois do 403. Com uma pessoa cadastrada, o e-mail
    dela (também com espaços nas pontas, aparado antes da conferência) e um e-mail que contém o RA dela → 400
    `[{contact_email, invalid_value}]`, sem eco no corpo, nos eventos e nos logs; nada em `offering_contacts`
    e nenhum `offering.created` nem `offering.settings_changed` pelos pedidos recusados. Controle positivo: um
    e-mail de professor fictício é aceito pela mesma operação."""
    register_person(client, ADMIN_TOKEN, **ZEFIRO.payload())
    assert ZEFIRO.email is not None and ZEFIRO.ra is not None
    with_ra = f"prof.{ZEFIRO.ra}@docentes.example.org"
    refused = [ZEFIRO.email, f"  {ZEFIRO.email}  ", with_ra]
    existing = None
    if operation == "update":
        existing = create_offering(client, ADMIN_TOKEN, code=_code("contato-aluno"), timezone=TZ)
    before_types = _event_types(sql)

    with capture_logs() as logs:
        for email in refused:
            if operation == "create":
                resp = client.post("/v1/offerings", json=_body(_code("contato-aluno"), contact_email=email),
                                   headers=ADMIN)
            else:
                assert existing is not None
                resp = _patch(client, existing["code"], {"contact_email": email})
            assert_error(resp, 400, "invalid_request", fields=[("contact_email", "invalid_value")])
            assert_no_echo(resp, email.strip(), ZEFIRO.ra)
    for email in refused:
        assert email.strip() not in logs.text, "o e-mail recusado apareceu nos logs"
    assert ZEFIRO.ra not in logs.text
    assert _contacts(sql) == 0
    assert _event_types(sql) == before_types
    events_text = _events_text(sql)
    assert ZEFIRO.email not in events_text and with_ra not in events_text
    if operation == "update":
        assert existing is not None
        assert _row(sql, existing["code"]) == (False, None, None)
    else:
        assert sql.execute("SELECT count(*) FROM offerings").fetchone()[0] == 0

    clock.advance(30)
    if operation == "create":
        accepted = _created(client.post("/v1/offerings", json=_body(_code("contato-ok"), contact_email=PROF),
                                        headers=ADMIN))
        assert accepted["contact_email_set"] is True
        assert _row(sql, accepted["code"]) == (False, PROF, clock.now())
    else:
        assert existing is not None
        assert update_offering_settings(client, ADMIN_TOKEN, existing["code"], contact_email=PROF) == _offering(
            existing, free_messages=False, contact_email_set=True)
        assert _row(sql, existing["code"]) == (False, PROF, clock.now())


# ---------------------------------------------------------------------------------------------
# Saídas e arquivamento
# ---------------------------------------------------------------------------------------------


def test_offering_outputs_show_only_booleans(client: Any, clock: Any, sql: psycopg.Connection,
                                             bot_api: Any) -> None:
    """O oferecimento mostra só os booleanos: o `Offering` de
    `offerings.create`, `.list`, `.get`, `.close`, `.prepare_archive` (em `offering`), `.archive`,
    `.update_settings` e `bots.activate`; com as mensagens livres
    ligadas e um contato cadastrado, toda saída mostra `free_messages: true` e `contact_email_set: true`, e
    nenhuma traz o e-mail nem a chave `contact_email`; controle positivo: o e-mail está em
    `offering_contacts`."""
    code = _code("saidas")
    created = _created(client.post("/v1/offerings", json=_body(code, free_messages=True, contact_email=PROF),
                                   headers=ADMIN))
    flags = {"free_messages": True, "contact_email_set": True}
    assert {k: created[k] for k in flags} == flags

    updated = update_offering_settings(client, ADMIN_TOKEN, code, free_messages=True)
    assert updated == created

    responses: list[tuple[str, Any, dict]] = []
    got = client.get(f"/v1/offerings/{code}", headers=ADMIN)
    responses.append(("offerings.get", got, _ok(got, "offerings.get")))
    listed, item = _listed(client, code)
    responses.append(("offerings.list", listed, item))
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_api.add_bot()))
    clock.advance(10)
    activated = client.post(f"/v1/offerings/{code}/bot/activate", headers=ADMIN)
    responses.append(("bots.activate", activated, _ok(activated, "bots.activate")))
    clock.advance(10)
    closed = _close(client, code)
    responses.append(("offerings.close", closed, _ok(closed, "offerings.close")))
    prepared = _prepare_archive(client, code)
    prepared_body = _ok(prepared, "offerings.prepare_archive")
    responses.append(("offerings.prepare_archive", prepared, prepared_body["offering"]))
    clock.advance(10)
    archived = _archive(client, code, prepared_body["confirmation_code"])
    responses.append(("offerings.archive", archived, _ok(archived, "offerings.archive")))

    for label, resp, offering in responses:
        assert {k: offering[k] for k in flags} == flags, label
        assert "contact_email" not in offering, label
        _no_email(resp, PROF)
    assert _row(sql, code)[:2] == (True, PROF)


def test_archive_keeps_settings_and_refuses_changes(client: Any, clock: Any, sql: psycopg.Connection,
                                                    bot_api: Any) -> None:
    """Encerramento e arquivamento não mexem na configuração nem no contato:
    o encerramento e o arquivamento mantêm `free_messages` e o contato (o `Offering` continua com
    `true`/`true`, e a linha de `offering_contacts` continua lá); depois do arquivamento, `PATCH` de qualquer
    campo (inclusive remover o contato) → 409 `invalid_state` com `state: archived`, sem evento e sem mudar o
    banco."""
    code = _code("arquivo")
    created = _created(client.post("/v1/offerings", json=_body(code, free_messages=True, contact_email=PROF),
                                   headers=ADMIN))
    created_at = clock.now()
    _activated(client, bot_api, code)
    closed = _ok(_close(client, code), "offerings.close")
    assert (closed["free_messages"], closed["contact_email_set"]) == (True, True)
    archived = _archived(client, code)
    assert (archived["free_messages"], archived["contact_email_set"]) == (True, True)
    assert _get(client, code) == archived
    assert _row(sql, code) == (True, PROF, created_at)

    before_types = _event_types(sql)
    clock.advance(60)
    for body in ({"free_messages": False}, {"contact_email": None}, {"contact_email": PROF_OTHER},
                 {"free_messages": True}):
        assert_error(_patch(client, code, body), 409, "invalid_state", state="archived")
    assert _event_types(sql) == before_types
    assert "offering.settings_changed" not in before_types
    assert _row(sql, code) == (True, PROF, created_at)
    assert _get(client, code) == archived
    assert created["offering_id"] == archived["offering_id"]


# ---------------------------------------------------------------------------------------------
# O aviso da prévia
# ---------------------------------------------------------------------------------------------

# A questão: um repositório no host de `TEST_REPO_SETTINGS` (`repos.cq.test`, a fixture `app`) e o QID
# do preset `number` do motor falso, que existe na árvore da obtenção falsa (como em test_dispatches_api.py).
_REPO = "https://repos.cq.test/disciplina/questoes-configuracao.git"
_QID = "number"
_WORKER = "teste-configuracao"
_CLAIM_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")


def _drain_validation(db_url: str, clock: Any, settings: Any, bots: Any, engine_fake: Any,
                      repo_fetcher: Any) -> int:
    """Roda a validação real (`dispatch.validate`, com as continuações) de todos os disparos pendentes, com a
    posse por SQL (como a semente) e os tratadores do núcleo montados como no `run_worker`; devolve
    quantas tarefas processou."""
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.bots import TelegramChannel
    from cq_server.config import DeliverySettings, EngineSettings
    from cq_server.dispatches import VALIDATE_TASK_TYPE, validate_task_handler
    from cq_server.tasks import ClaimedTask, TaskQueue

    lease = timedelta(seconds=settings.lease_seconds)
    processed = 0
    with contextlib.ExitStack() as stack:
        queue = TaskQueue(db_url, clock=clock, settings=settings)
        stack.callback(queue.close)
        engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=10.0))
        stack.callback(engine.close)
        channel = TelegramChannel(db_url, clock=clock, runtime=bots, default=ConsoleChannel(io.StringIO()),
                                  delivery=DeliverySettings())
        stack.callback(channel.close)
        handler: Callable[..., Any] = validate_task_handler(db_url, clock=clock, settings=settings, engine=engine,
                                                            fetcher=repo_fetcher, channel=channel)
        stack.callback(handler.close)
        conn = stack.enter_context(psycopg.connect(db_url, autocommit=True))
        while True:
            now = clock.now()
            row = conn.execute(_CLAIM_SQL, {"owner": _WORKER, "until": now + lease, "type": VALIDATE_TASK_TYPE,
                                            "now": now}).fetchone()
            if row is None:
                return processed
            handler(ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3])))
            assert queue.complete(int(row[0]), _WORKER), "dispatch.validate não concluída (posse perdida)"
            processed += 1
            assert processed <= 60, "mais de 60 tarefas dispatch.validate"


def test_preview_warns_when_free_messages_off_without_contact(client: Any, clock: Any, sql: psycopg.Connection,
                                                              db_url: str, settings: Any, bots: Any, bot_api: Any,
                                                              engine_fake: Any, repo_fetcher: Any) -> None:
    """Aviso na prévia:
    a validação do disparo lê a configuração do oferecimento e, com as mensagens livres desligadas e sem
    contato do professor, acrescenta o aviso `no_teacher_contact`, por último, com a mensagem exata
    prevista, na prévia e na lista de `kind` do `dispatch.validated`; o aviso não impede o agendamento.
    Três oferecimentos ativos, sem alunos (o aviso `empty_target` vem antes): (1) no padrão (desligado, sem
    contato) → `["empty_target", "no_teacher_contact"]`; (2) desligado com contato → só `["empty_target"]`;
    (3) ligado sem contato → só `["empty_target"]`. Sem a implementação, a validação não calcula o aviso (o caso
    (1) vem primeiro)."""
    engine_fake.program(_QID, "number")
    cases = [
        ("padrao", {}, ["empty_target", NO_TEACHER_CONTACT]),
        ("com-contato", {"contact_email": PROF}, ["empty_target"]),
        ("ligado-sem-contato", {"free_messages": True}, ["empty_target"]),
    ]
    # Um caso de cada vez, na ordem: o (1) só usa o cadastro padrão e é o que fica vermelho primeiro. O cadastro
    # e a ativação vão direto pelo `client`, sem conferir o `Offering` contra o esquema (os dois booleanos são
    # conferidos nas outras funções deste arquivo): aqui, o que se afirma é o aviso da prévia.
    for index, (prefix, fields, kinds) in enumerate(cases):
        code = _code(f"aviso-{prefix}")
        resp = client.post("/v1/offerings", json=_body(code, **fields), headers=ADMIN)
        assert resp.status_code == 201, f"{resp.status_code} {resp.text[:300]}"
        offering = resp.json()
        register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_api.add_bot()))
        activated = client.post(f"/v1/offerings/{code}/bot/activate", headers=ADMIN)
        assert activated.status_code == 200 and activated.json()["state"] == "active", activated.text[:300]
        dispatch = prepare_dispatch(client, ADMIN_TOKEN, code, repo=_REPO, commit=f"{index + 1:040x}", qid=_QID)
        clock.advance(30)
        assert _drain_validation(db_url, clock, settings, bots, engine_fake, repo_fetcher) >= 1, code

        shown = get_dispatch(client, ADMIN_TOKEN, code, dispatch["dispatch_id"])
        assert shown["state"] == "scheduled", (code, shown["state"])
        assert shown["preview"] is not None, code
        assert shown["preview"]["target_count"] == 0, code
        warnings = shown["preview"]["warnings"]
        assert [w["kind"] for w in warnings] == kinds, code
        for warning in warnings:
            if warning["kind"] == NO_TEACHER_CONTACT:
                assert warning["message"] == NO_TEACHER_CONTACT_MESSAGE
            else:
                assert warning["message"].strip(), (code, warning)
        validated = [e["data"] for e in list_events(client, ADMIN_TOKEN, "dispatch.validated", limit=500)["items"]
                     if e["data"]["dispatch_id"] == dispatch["dispatch_id"]]
        assert validated == [{"offering_id": offering["offering_id"], "dispatch_id": dispatch["dispatch_id"],
                              "target_count": 0, "warnings": kinds}], code
