"""Helpers de chamada à API."""

# Oferecimentos e bots: atalhos de oferecimentos, bots e equipe (`create_offering`, `register_bot`,
# `activate_bot`, `create_team_link`), `offering_ref` e `BOT_TOKEN_SHAPE`. Cada atalho afirma o
# status de sucesso e o esquema congelado e devolve o corpo; o link de equipe e o seu código entram
# em `remember_secret` (são credenciais).

# Vínculo do aluno: `write_roster` (o arquivo da lista do CLI) e os atalhos de alunos e
# convites (`import_students`, `students_summary`, `prepare_invites`, `send_invites`,
# `resend_invites`). Cada atalho afirma o status 200 e o esquema congelado e devolve o
# corpo. O `confirmation_code` do preparo **não** é segredo e não entra em
# `remember_secret`; o link e o código do convite de aluno só existem no e-mail (SMTP falso).

# Disparo e resposta: os atalhos de disparos e mensagens livres
# (`prepare_dispatch`, `get_dispatch`, `list_dispatches`, `confirm_dispatch`, `cancel_dispatch`,
# `list_free_messages`), com os argumentos do `ApiClient` (a forma que os testes
# usam para chamar a API). Cada atalho afirma o status 200 e o esquema congelado e devolve o corpo;
# nenhum segredo novo.

# Mensagens livres opcionais por oferecimento:
# `update_offering_settings` (`offerings.update_settings`, `PATCH /v1/offerings/{ref}/settings`).
# O `create_offering` já aceita `free_messages=` e `contact_email=` pelos `**fields`.

# Relatórios e acompanhamento: os atalhos das 5 operações
# (`get_report`, com `version=` e `fmt=`, e `get_report_csv`; `list_report_versions`,
# `resend_report_summary`, `get_offering_digest`, `update_offering_digest`), no fim do módulo.


# Mensagens livres e a semente: o `Offering` congelado exige
# `free_messages` e `contact_email_set` (as mensagens livres opcionais), que uma implementação ainda sem esse
# recurso não devolve. Para a semente não encobrir as falhas esperadas de outros recursos do disparo
# (escolha única, gabarito e nota parcial) nem derrubar os casos anteriores por um motivo fora dos esperados,
# o `create_offering` e o `activate_bot` conferem o esquema congelado **inteiro** (tipos, enums, padrões,
# `additionalProperties: false` e todos os campos obrigatórios de oferecimentos e bots) e só toleram a **ausência** das duas chaves
# das mensagens livres (`_FREE_MESSAGE_OFFERING_KEYS`); presentes, continuam conferidas pelo tipo. A conferência estrita (as duas chaves
# obrigatórias) vale quando as mensagens livres são o objeto da chamada: no `create_offering` com `free_messages=` ou
# `contact_email=` nos `**fields`, ou com `strict_schema=True` em qualquer dos dois. Os testes cujo objeto é a
# forma do `Offering` chamam `assert_matches` direto (ex.: test_offerings_api, test_bots_activate_api,
# test_offering_settings, `_case_offering_created` de test_api_errors) e continuam estritos.

# CLI e expurgo: `list_events` aceita
# `person=` no fim (por palavra-chave, depois dos posicionais; sem `person`, a consulta não o leva);
# `_FREE_MESSAGE_OFFERING_KEYS` inclui `purged_at` (só a ausência tolerada na semente, como nas outras); no fim do módulo, os
# atalhos `system_doctor`, `prepare_purge` e `purge_offering` e `assert_matches_tolerant` (a mesma tolerância para
# o `Offering` embutido, usada pelas sementes do expurgo).

from __future__ import annotations

import copy
import csv
import functools
import io
import itertools
import os
import pathlib
import re
import urllib.parse
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

from tests.helpers.contract import assert_matches, load_schema
from tests.helpers.contract import REPORT_CSV_MEDIA, REPORT_JSON_MEDIA, assert_report_response  # relatórios
from tests.helpers.secrets import remember_secret

# Token inicial de administração dos testes: 50 caracteres (a API exige pelo menos 32).
ADMIN_TOKEN = "cq_test_admin_" + "k" * 36
assert len(ADMIN_TOKEN) == 50


def auth(token: str | None, client_kind: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if client_kind is not None:
        headers["X-CQ-Client"] = client_kind
    return headers


def register_person(client: Any, token: str, **identity: Any) -> dict:
    """Cadastra uma pessoa; afirma 201 e o esquema. `identity` usa as chaves `name`, `ra`, `email`."""
    resp = client.post("/v1/people", json=identity, headers=auth(token))
    assert resp.status_code == 201, f"cadastro falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("people.register", 201, body)
    return body


def create_token(client: Any, token: str, permissions: list[str], label: str = "rotulo de teste") -> tuple[str, dict]:
    """Cria um token; afirma 201 e o esquema; devolve (segredo, corpo)."""
    resp = client.post("/v1/tokens", json={"label": label, "permissions": permissions}, headers=auth(token))
    assert resp.status_code == 201, f"criação de token falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("tokens.create", 201, body)
    remember_secret(body["secret"])
    return body["secret"], body


def list_events(client: Any, token: str, *types: str, after: int = 0, limit: int | None = None,
                person: str | None = None) -> dict:
    # O `person` vem por palavra-chave, depois da assinatura
    # posicional. `person` só vai à consulta quando dado; `types` pode vir vazio só com `person` (sem os
    # dois, a API recusa com `[{types, missing}]`, e o 200 afirmado abaixo falha). Com
    # `person=None`, a consulta leva só `types`, `after` e `limit`.
    params: list[tuple[str, Any]] = [("types", t) for t in types] + [("after", after)]
    if limit is not None:
        params.append(("limit", limit))
    if person is not None:
        params.append(("person", person))
    resp = client.get("/v1/events", params=params, headers=auth(token))
    assert resp.status_code == 200, f"consulta de eventos falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("events.list", 200, body)
    return body


def all_events(client: Any, token: str, *types: str) -> list[dict]:
    """Todos os eventos dos tipos dados, encadeando cursores."""
    items: list[dict] = []
    after = 0
    while True:
        page = list_events(client, token, *types, after=after, limit=500)
        items.extend(page["items"])
        if not page["items"]:
            return items
        after = page["next_cursor"]


def uuid4_with_prefix(prefix: str, fill: str = "0") -> uuid.UUID:
    """UUID v4 válido cujo hex (sem hífens) começa com `prefix`.

    As posições 12 (versão, `4`) e 16 (variante, `8`–`b`) são ajustadas se o prefixo não as cobrir;
    se cobrir, precisam já ser válidas.
    """
    prefix = prefix.lower()
    assert all(c in "0123456789abcdef" for c in prefix) and len(prefix) <= 32
    chars = list(prefix + fill * (32 - len(prefix)))[:32]
    if len(prefix) <= 12:
        chars[12] = "4"
    else:
        assert chars[12] == "4", "o prefixo precisa ter '4' na posição 12 (versão)"
    if len(prefix) <= 16:
        chars[16] = "8"
    else:
        assert chars[16] in "89ab", "o prefixo precisa ter 8, 9, a ou b na posição 16 (variante)"
    value = uuid.UUID("".join(chars))
    assert value.version == 4
    return value


def marker(alias: str) -> str:
    """Marca da redação. Único ponto a mudar se a forma da marca mudar."""
    return f"[pessoa {alias}]"


# --- Oferecimentos, bots e equipe ----------------------------------------------------------------

# Forma do token de bot: a mesma que a redação reconhece,
# com as fronteiras; serve a `fullmatch` (formato local) e a `search` (token dentro de um texto).
BOT_TOKEN_SHAPE = re.compile(r"(?<![0-9A-Za-z_-])[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}(?![0-9A-Za-z_-])")

# Padrão do link de equipe.
TEAM_LINK_SHAPE = re.compile(r"https://t\.me/([A-Za-z0-9_]{5,32})\?start=([A-Za-z0-9_-]{22})")

_offering_counter = itertools.count(1)


# Chaves do `Offering` acrescentadas pelas mensagens livres opcionais e entradas do cadastro que as definem.
# Expurgo: + `purged_at`, com a mesma
# regra das duas das mensagens livres: na semente (modo não estrito), só a **ausência** é tolerada; presente, é conferida pelo
# tipo do esquema congelado (horário ou nulo). O modo estrito continua exigindo todas.
_FREE_MESSAGE_OFFERING_KEYS = ("free_messages", "contact_email_set", "purged_at")
_FREE_MESSAGE_CREATE_FIELDS = frozenset({"free_messages", "contact_email"})


@functools.lru_cache(maxsize=None)
def _offering_schema_without_free_message_required(op_id: str) -> Draft202012Validator:
    """O esquema congelado de resposta de `op_id` com as chaves das mensagens livres fora de `required` (só isso muda)."""
    schema = copy.deepcopy(load_schema(op_id, "response"))
    required = schema.get("required")
    assert isinstance(required, list) and "offering_id" in required, (
        f"{op_id}: o esquema de resposta não é o `Offering` esperado")
    schema["required"] = [key for key in required if key not in _FREE_MESSAGE_OFFERING_KEYS]
    return Draft202012Validator(schema)


def _assert_offering_matches(op_id: str, status: int, body: Any, *, strict: bool) -> None:
    """Confere o `Offering` devolvido por `op_id`: estrito com `strict`; senão, o esquema congelado inteiro,
    tolerando só a ausência das chaves das mensagens livres (ver o cabeçalho do módulo)."""
    if strict:
        assert_matches(op_id, status, body)
        return
    errors = sorted(_offering_schema_without_free_message_required(op_id).iter_errors(body), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:10])
        raise AssertionError(
            f"{op_id} {status} não valida contra o esquema congelado (sem exigir {', '.join(_FREE_MESSAGE_OFFERING_KEYS)}): "
            f"{detail}")


def create_offering(client: Any, token: str, *, strict_schema: bool | None = None, **fields: Any) -> dict:
    """Cadastra um oferecimento; afirma 201 e o esquema; devolve o corpo.

    Sem `code`, usa um código único neutro (`oferta-teste-<n>`, sem identidade de pessoa); os demais
    campos têm valores neutros (`course`, `term`, `timezone`) e podem ser trocados por `fields`
    (ex.: `response_window_minutes=60`; mensagens livres: `free_messages=True`, `contact_email=...`).

    O esquema: com `strict_schema=None` (padrão), a conferência é estrita quando `fields` traz
    `free_messages` ou `contact_email` (as mensagens livres são o objeto) e, sem eles, tolera só a ausência de
    `free_messages`/`contact_email_set` na resposta (cadastro de semente; cabeçalho do módulo).
    `strict_schema=True` ou `False` força um dos dois modos. `strict_schema` não vai no corpo.
    """
    strict = bool(_FREE_MESSAGE_CREATE_FIELDS & fields.keys()) if strict_schema is None else strict_schema
    body: dict[str, Any] = {"code": f"oferta-teste-{next(_offering_counter)}",
                            "course": "Algoritmos e Programação", "term": "2031s1",
                            "timezone": "America/Sao_Paulo", **fields}
    resp = client.post("/v1/offerings", json=body, headers=auth(token))
    assert resp.status_code == 201, f"cadastro de oferecimento falhou: {resp.status_code} {resp.text[:300]}"
    created = resp.json()
    _assert_offering_matches("offerings.create", 201, created, strict=strict)
    return created


def offering_ref(offering: dict, by: str = "code") -> str:
    """A referência `{ref}` de um oferecimento: o `code` (padrão) ou, com
    `by="id"`, o `offering_id`. Aceita o corpo de `Offering` ou um resumo com os dois campos."""
    if by == "code":
        return offering["code"]
    if by == "id":
        return offering["offering_id"]
    raise ValueError("by deve ser 'code' ou 'id'")


def _ref_path(ref: str) -> str:
    return urllib.parse.quote(ref, safe="")


def register_bot(client: Any, token: str, ref: str, bot_token: str) -> dict:
    """Registra (ou troca) o token do bot do oferecimento `ref`; afirma 200 e o esquema.

    O token vai só no corpo (`PUT /v1/offerings/{ref}/bot`) e entra em `remember_secret`.
    """
    remember_secret(bot_token)
    resp = client.put(f"/v1/offerings/{_ref_path(ref)}/bot", json={"token": bot_token},
                      headers=auth(token))
    assert resp.status_code == 200, f"registro do bot falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("bots.register", 200, body)
    return body


def activate_bot(client: Any, token: str, ref: str, *, strict_schema: bool = False) -> dict:
    """Ativa o oferecimento `ref` (`bots.activate`); afirma 200 e o esquema; devolve o `Offering`.

    O esquema congelado é conferido inteiro, tolerando só a ausência de `free_messages`/`contact_email_set`
    (cabeçalho do módulo); `strict_schema=True` exige as duas chaves."""
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/bot/activate", headers=auth(token))
    assert resp.status_code == 200, f"ativação falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    _assert_offering_matches("bots.activate", 200, body, strict=strict_schema)
    return body


def link_code(link: str) -> str:
    """O código de convite de um link de equipe (a parte depois de `?start=`)."""
    match = TEAM_LINK_SHAPE.fullmatch(link)
    assert match is not None, "link de equipe fora do padrão esperado"
    return match.group(2)


def create_team_link(client: Any, token: str, ref: str, person: str, *,
                     valid_hours: Any = None) -> dict:
    """Gera o link de equipe de `person` (pseudônimo) no oferecimento `ref`; afirma 201 e o esquema.

    O link e o código de convite entram em `remember_secret` antes de qualquer outra conferência.
    """
    payload: dict[str, Any] = {"person": person}
    if valid_hours is not None:
        payload["valid_hours"] = valid_hours
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/team-links", json=payload, headers=auth(token))
    if resp.status_code == 201 and isinstance(resp.json().get("link"), str):
        link = resp.json()["link"]
        remember_secret(link)
        match = TEAM_LINK_SHAPE.fullmatch(link)
        if match is not None:
            remember_secret(match.group(2))
    assert resp.status_code == 201, f"link de equipe falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("team.link", 201, body)
    return body


# --- Vínculo do aluno: alunos e convites -------------------------------------------------------

# Cabeçalho do arquivo da lista: as colunas `ra`, `nome` e `email`.
ROSTER_HEADER = ("ra", "nome", "email")
_ROSTER_KEYS = ("ra", "name", "email")  # chave de `rows` para cada coluna de `ROSTER_HEADER`


def write_roster(path: str | os.PathLike[str], rows: Iterable[Mapping[str, Any] | Sequence[Any]], *,
                 delimiter: str = ",", bom: bool = False) -> pathlib.Path:
    """Escreve o arquivo da lista em `path` (UTF-8, com `\\ufeff` no início se `bom`) e devolve o caminho.

    A primeira linha é o cabeçalho `ra<d>nome<d>email` (`d` = `delimiter`); cada item de `rows` vira um
    registro, pelo `csv.writer` (dialeto `excel`: aspas só quando precisa, fim de linha `\\r\\n`):
    - um mapeamento com as chaves da API (`ra`, `name`, `email`) sai na ordem do cabeçalho, com os
      valores **sem aparar**;
    - uma sequência sai como está (ex.: `[]` escreve uma linha vazia; 4 campos, um registro com 4
      colunas).
    Os casos de bytes fora do formato (cabeçalho errado, não UTF-8, aspas quebradas) são escritos pelo
    próprio teste, em bytes.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter)
    writer.writerow(ROSTER_HEADER)
    for row in rows:
        if isinstance(row, Mapping):
            writer.writerow([row[key] for key in _ROSTER_KEYS])
        else:
            writer.writerow(list(row))
    target = pathlib.Path(path)
    target.write_bytes(("\ufeff" if bom else "").encode("utf-8") + buffer.getvalue().encode("utf-8"))
    return target


def import_students(client: Any, token: str, ref: str, rows: Iterable[Mapping[str, Any]]) -> dict:
    """Importa a lista `rows` (itens `{name, ra, email}`) no oferecimento `ref` (`students.import`,
    `POST /v1/offerings/{ref}/students`); afirma 200 e o esquema; devolve o corpo (só contagens)."""
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/students", json={"rows": list(rows)},
                       headers=auth(token))
    assert resp.status_code == 200, f"importação da lista falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("students.import", 200, body)
    return body


def students_summary(client: Any, token: str, ref: str) -> dict:
    """A adesão do oferecimento `ref` (`students.summary`, `GET /v1/offerings/{ref}/students/summary`);
    afirma 200 e o esquema; devolve o corpo."""
    resp = client.get(f"/v1/offerings/{_ref_path(ref)}/students/summary", headers=auth(token))
    assert resp.status_code == 200, f"consulta da adesão falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("students.summary", 200, body)
    return body


def prepare_invites(client: Any, token: str, ref: str, *, valid_hours: Any = None) -> dict:
    """Prepara o envio dos convites do oferecimento `ref` (`invites.prepare_send`,
    `POST /v1/offerings/{ref}/invites/prepare`); afirma 200 e o esquema; devolve o corpo.

    Sem `valid_hours`, o corpo é `{}` (a API usa o padrão, 168). O `confirmation_code` da resposta não é
    segredo.
    """
    payload: dict[str, Any] = {}
    if valid_hours is not None:
        payload["valid_hours"] = valid_hours
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/invites/prepare", json=payload,
                       headers=auth(token))
    assert resp.status_code == 200, f"preparo do envio falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("invites.prepare_send", 200, body)
    return body


def send_invites(client: Any, token: str, ref: str, confirmation_code: str) -> dict:
    """Confirma o envio dos convites do oferecimento `ref` com o código do preparo (`invites.send`,
    `POST /v1/offerings/{ref}/invites`); afirma 200 e o esquema; devolve o corpo."""
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/invites",
                       json={"confirmation_code": confirmation_code}, headers=auth(token))
    assert resp.status_code == 200, f"confirmação do envio falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("invites.send", 200, body)
    return body


def resend_invites(client: Any, token: str, ref: str, pseudonyms: Iterable[str], *,
                   valid_hours: Any = None) -> dict:
    """Reenvia os convites dos `pseudonyms` no oferecimento `ref` (`invites.resend`,
    `POST /v1/offerings/{ref}/invites/resend`); afirma 200 e o esquema; devolve o corpo.

    Sem `valid_hours`, o campo fica fora do corpo (a API usa o padrão, 168).
    """
    payload: dict[str, Any] = {"pseudonyms": list(pseudonyms)}
    if valid_hours is not None:
        payload["valid_hours"] = valid_hours
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/invites/resend", json=payload,
                       headers=auth(token))
    assert resp.status_code == 200, f"reenvio dos convites falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("invites.resend", 200, body)
    return body


# --- Disparo e resposta: disparos e mensagens livres ----------------------------------------------
#
# As 6 operações, todas 200, pela interface que os
# testes usam (`ApiClient`, a mesma forma de argumentos). Cada
# atalho afirma o status 200 e o esquema congelado e devolve o corpo. O preparo nunca chama o motor
# : o disparo sai em `validating`, e a validação corre na tarefa `dispatch.validate`. Os casos de
# erro (400, 404, 409) e os horários sem fuso chamam o `client` direto, como antes.


def _dispatch_path(ref: str, dispatch_id: str | None = None) -> str:
    base = f"/v1/offerings/{_ref_path(ref)}/dispatches"
    return base if dispatch_id is None else f"{base}/{urllib.parse.quote(str(dispatch_id), safe='')}"


def _page_params(after: Any, limit: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if after is not None:
        params["after"] = after
    if limit is not None:
        params["limit"] = limit
    return params


def prepare_dispatch(client: Any, token: str, ref: str, *, repo: str, commit: str, qid: str,
                     opens_at: Any = None, closes_at: Any = None, origin: Any = None) -> dict:
    """Prepara um disparo no oferecimento `ref` (`dispatches.prepare`, `POST /v1/offerings/{ref}/dispatches`);
    afirma 200 e o esquema; devolve o `Dispatch` (em `validating`).

    O pedido é plano: `repo`, `commit` e `qid` sempre; `opens_at`, `closes_at` (ISO 8601 com
    deslocamento, ex.: `iso(dt)`) e `origin` só quando dados (ausentes, a API usa o imediato, a janela do
    oferecimento e a origem pelo `X-CQ-Client`).
    """
    payload: dict[str, Any] = {"repo": repo, "commit": commit, "qid": qid}
    if opens_at is not None:
        payload["opens_at"] = opens_at
    if closes_at is not None:
        payload["closes_at"] = closes_at
    if origin is not None:
        payload["origin"] = origin
    resp = client.post(_dispatch_path(ref), json=payload, headers=auth(token))
    assert resp.status_code == 200, f"preparo do disparo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("dispatches.prepare", 200, body)
    return body


def get_dispatch(client: Any, token: str, ref: str, dispatch_id: str) -> dict:
    """Consulta o disparo `dispatch_id` do oferecimento `ref` (`dispatches.get`,
    `GET /v1/offerings/{ref}/dispatches/{dispatch_id}`); afirma 200 e o esquema;
    devolve o `Dispatch`, com a prévia."""
    resp = client.get(_dispatch_path(ref, dispatch_id), headers=auth(token))
    assert resp.status_code == 200, f"consulta do disparo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("dispatches.get", 200, body)
    return body


def list_dispatches(client: Any, token: str, ref: str, *, after: Any = None, limit: Any = None) -> dict:
    """Lista os disparos do oferecimento `ref` (`dispatches.list`, `GET /v1/offerings/{ref}/dispatches`);
    afirma 200 e o esquema; devolve a página. `after` e `limit` só vão na
    consulta quando dados (padrões da API: 0 e 100)."""
    resp = client.get(_dispatch_path(ref), params=_page_params(after, limit), headers=auth(token))
    assert resp.status_code == 200, f"lista de disparos falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("dispatches.list", 200, body)
    return body


def confirm_dispatch(client: Any, token: str, ref: str, dispatch_id: str) -> dict:
    """Confirma o disparo validado `dispatch_id` do oferecimento `ref` (`dispatches.confirm`,
    `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/confirm`, sem corpo); afirma
    200 e o esquema; devolve o `Dispatch` confirmado."""
    resp = client.post(f"{_dispatch_path(ref, dispatch_id)}/confirm", headers=auth(token))
    assert resp.status_code == 200, f"confirmação do disparo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("dispatches.confirm", 200, body)
    return body


def cancel_dispatch(client: Any, token: str, ref: str, dispatch_id: str) -> dict:
    """Cancela o disparo `dispatch_id` do oferecimento `ref` (`dispatches.cancel`,
    `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/cancel`, sem corpo); afirma
    200 e o esquema; devolve o `Dispatch` em `cancelled`."""
    resp = client.post(f"{_dispatch_path(ref, dispatch_id)}/cancel", headers=auth(token))
    assert resp.status_code == 200, f"cancelamento do disparo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("dispatches.cancel", 200, body)
    return body


def list_free_messages(client: Any, token: str, ref: str, *, after: Any = None, limit: Any = None) -> dict:
    """Lista as mensagens livres do oferecimento `ref` (`messages.list`, `GET /v1/offerings/{ref}/messages`);
    afirma 200 e o esquema; devolve a página (texto redigido). `after` e `limit`
    só vão na consulta quando dados."""
    resp = client.get(f"/v1/offerings/{_ref_path(ref)}/messages", params=_page_params(after, limit),
                      headers=auth(token))
    assert resp.status_code == 200, f"lista de mensagens livres falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("messages.list", 200, body)
    return body


def update_offering_settings(client: Any, token: str, ref: str, **fields: Any) -> dict:
    """Altera a configuração de mensagens livres do oferecimento `ref` (`offerings.update_settings`,
    `PATCH /v1/offerings/{ref}/settings`; mensagens livres opcionais); afirma 200 e o esquema; devolve o
    `Offering` (com `free_messages` e `contact_email_set`, nunca o e-mail).

    O corpo é exatamente `fields` (ex.: `free_messages=False`, `contact_email="prof@x.org"` ou
    `contact_email=None`, que vai como `null` e remove o contato); sem `fields`, manda `{}`, que a API
    recusa (`[{body, too_few}]`): os casos de erro chamam o `client` direto, como antes.
    """
    resp = client.patch(f"/v1/offerings/{_ref_path(ref)}/settings", json=dict(fields), headers=auth(token))
    assert resp.status_code == 200, (
        f"alteração da configuração do oferecimento falhou: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    assert_matches("offerings.update_settings", 200, body)
    return body


# --- Relatórios e resumo diário -------------------------------------------------------------------
#
# As 5 operações, todas
# 200). Os atalhos têm esta assinatura, sem o token na lista posicional
# (`get_report(client, ref, dispatch_id, *, version=None, fmt=None)`), com `token=` (padrão: o token inicial)
# por palavra-chave; por compatibilidade com os atalhos anteriores, aceitam também o token na 2ª posição
# (`get_report(client, token, ref, dispatch_id, …)`). Cada atalho afirma o status 200 e a resposta pelo
# esquema congelado (no `reports.get`, pela mídia: `assert_report_response`) e devolve o corpo; os casos de
# erro (400, 404, 409) chamam o `client` direto, como antes. Nenhum segredo novo.


def _positional(args: tuple[Any, ...], names: tuple[str, ...], token: str | None) -> tuple[str, list[Any]]:
    """Os argumentos posicionais de um atalho dos relatórios: `names` sem o token, ou o token antes deles."""
    if len(args) == len(names) + 1:
        assert token is None, "o token vai na 2ª posição ou em token=, não nos dois"
        return str(args[0]), list(args[1:])
    assert len(args) == len(names), f"esperava {', '.join(names)} (e, opcionalmente, o token antes)"
    return (ADMIN_TOKEN if token is None else token), list(args)


def _report_path(ref: str, dispatch_id: Any, suffix: str = "") -> str:
    return f"{_dispatch_path(ref, str(dispatch_id))}/report{suffix}"


def get_report(client: Any, *args: Any, version: Any = None, fmt: str | None = None,
               token: str | None = None) -> Any:
    """O relatório do disparo `dispatch_id` do oferecimento `ref` (`reports.get`,
    `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report`).

    Argumentos: `(ref, dispatch_id)` (ou `(token, ref, dispatch_id)`). `version` (`n` ou `"latest"`) e
    `fmt` (`"json"` ou `"csv"`) só vão na consulta (`version`, `format`) quando dados; sem `version`, o
    cálculo no momento. Afirma o 200 pela mídia (`assert_report_response`): com `fmt="csv"`, exige
    `text/csv; charset=utf-8` e devolve o texto (com o BOM); senão, exige `application/json` e devolve o
    `Report`.
    """
    token_value, (ref, dispatch_id) = _positional(args, ("ref", "dispatch_id"), token)
    params: dict[str, Any] = {}
    if fmt is not None:
        params["format"] = fmt
    if version is not None:
        params["version"] = version
    resp = client.get(_report_path(ref, dispatch_id), params=params, headers=auth(token_value))
    assert resp.status_code == 200, f"consulta do relatório falhou: {resp.status_code} {resp.text[:300]}"
    expected = REPORT_CSV_MEDIA if fmt == "csv" else REPORT_JSON_MEDIA
    media = resp.headers.get("content-type", "")
    assert media == expected, f"relatório com mídia {media!r} != {expected!r}"
    return assert_report_response(resp)


def get_report_csv(client: Any, *args: Any, version: Any = None, token: str | None = None) -> str:
    """O relatório em CSV (`get_report(…, fmt="csv")`): o texto inteiro, com o BOM."""
    return get_report(client, *args, version=version, fmt="csv", token=token)


def list_report_versions(client: Any, *args: Any, after: Any = None, limit: Any = None,
                         token: str | None = None) -> dict:
    """As versões congeladas do relatório (`reports.list_versions`,
    `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report/versions`); afirma 200 e
    o esquema; devolve a página. Argumentos: `(ref, dispatch_id)` (ou com o token antes); `after` e `limit`
    só vão na consulta quando dados."""
    token_value, (ref, dispatch_id) = _positional(args, ("ref", "dispatch_id"), token)
    resp = client.get(_report_path(ref, dispatch_id, "/versions"), params=_page_params(after, limit),
                      headers=auth(token_value))
    assert resp.status_code == 200, f"lista de versões falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("reports.list_versions", 200, body)
    return body


def resend_report_summary(client: Any, *args: Any, token: str | None = None) -> dict:
    """Reenvia o resumo do disparo fechado (`reports.resend_summary`,
    `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend`, sem corpo);
    afirma 200 e o esquema; devolve `{offering, dispatch_id, frozen}`. Cada chamada congela uma versão."""
    token_value, (ref, dispatch_id) = _positional(args, ("ref", "dispatch_id"), token)
    resp = client.post(_report_path(ref, dispatch_id, "/resend"), headers=auth(token_value))
    assert resp.status_code == 200, f"reenvio do resumo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("reports.resend_summary", 200, body)
    return body


def get_offering_digest(client: Any, *args: Any, token: str | None = None) -> dict:
    """A configuração do resumo diário do oferecimento `ref` (`offerings.get_digest`,
    `GET /v1/offerings/{ref}/digest`); afirma 200 e o esquema; devolve
    `{offering, timezone, enabled, local_time}`. Argumentos: `(ref,)` (ou `(token, ref)`)."""
    token_value, (ref,) = _positional(args, ("ref",), token)
    resp = client.get(f"/v1/offerings/{_ref_path(ref)}/digest", headers=auth(token_value))
    assert resp.status_code == 200, f"consulta do resumo diário falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.get_digest", 200, body)
    return body


def update_offering_digest(client: Any, *args: Any, token: str | None = None, **fields: Any) -> dict:
    """Altera o resumo diário do oferecimento `ref` (`offerings.update_digest`,
    `PATCH /v1/offerings/{ref}/digest`); afirma 200 e o esquema; devolve a
    configuração resultante. O corpo é exatamente `fields` (`enabled=`, `local_time=`; um `None` vai como
    `null`, que a API recusa); sem `fields`, manda `{}` (recusado: os casos de erro chamam o `client`
    direto). Argumentos: `(ref,)` (ou `(token, ref)`)."""
    token_value, (ref,) = _positional(args, ("ref",), token)
    resp = client.patch(f"/v1/offerings/{_ref_path(ref)}/digest", json=dict(fields), headers=auth(token_value))
    assert resp.status_code == 200, f"alteração do resumo diário falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.update_digest", 200, body)
    return body


# --- CLI e expurgo: doctor, expurgo e eventos por pessoa ------------------------------------------
#
# As 3 operações, todas 200.
# Os atalhos seguem a convenção dos relatórios: os argumentos sem o token (`system_doctor(client)`, `prepare_purge(client,
# ref)`, `purge_offering(client, ref, code)`), com `token=` (padrão: o token inicial) por palavra-chave ou o token
# na 2ª posição. Cada atalho afirma o status 200 e o esquema congelado e devolve o corpo; os casos de erro (400,
# 404, 409) chamam o `client` direto, como antes. O `confirmation_code` do expurgo não é segredo (o
# mesmo do arquivamento) e não entra em `remember_secret`.


def system_doctor(client: Any, *args: Any, token: str | None = None) -> dict:
    """As verificações da API (`system.doctor`, `GET /v1/system/doctor`, sem consulta nem corpo);
    afirma 200 e o esquema; devolve `{checked_at, checks}`. Argumentos: `()` (ou `(token,)`)."""
    token_value, _ = _positional(args, (), token)
    resp = client.get("/v1/system/doctor", headers=auth(token_value))
    assert resp.status_code == 200, f"system.doctor falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("system.doctor", 200, body)
    return body


def prepare_purge(client: Any, *args: Any, token: str | None = None) -> dict:
    """Prepara o expurgo do oferecimento `ref` (`offerings.prepare_purge`, `POST /v1/offerings/{ref}/purge/prepare`,
    sem corpo); afirma 200 e o esquema; devolve `{offering, impact, confirmation_code,
    expires_at}`. Argumentos: `(ref,)` (ou `(token, ref)`)."""
    token_value, (ref,) = _positional(args, ("ref",), token)
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/purge/prepare", headers=auth(token_value))
    assert resp.status_code == 200, f"preparo do expurgo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.prepare_purge", 200, body)
    return body


def purge_offering(client: Any, *args: Any, token: str | None = None) -> dict:
    """Confirma o expurgo do oferecimento `ref` com o código da preparação (`offerings.purge`,
    `POST /v1/offerings/{ref}/purge`, corpo `{confirmation_code}`); afirma 200 e o esquema;
    devolve `{offering, purged_at, result}`. Argumentos: `(ref, code)` (ou `(token, ref, code)`)."""
    token_value, (ref, code) = _positional(args, ("ref", "code"), token)
    resp = client.post(f"/v1/offerings/{_ref_path(ref)}/purge", json={"confirmation_code": code},
                       headers=auth(token_value))
    assert resp.status_code == 200, f"confirmação do expurgo falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.purge", 200, body)
    return body


def _drop_required(node: Any, keys: frozenset[str]) -> Any:
    """Cópia de um (sub)esquema com `keys` fora de toda lista `required` (inclusive em `$defs` e combinadores)."""
    if isinstance(node, dict):
        copied = {name: _drop_required(value, keys) for name, value in node.items()}
        required = copied.get("required")
        if isinstance(required, list):
            copied["required"] = [key for key in required if key not in keys]
        return copied
    if isinstance(node, list):
        return [_drop_required(item, keys) for item in node]
    return node


@functools.lru_cache(maxsize=None)
def _tolerant_validator(op_id: str) -> Draft202012Validator:
    return Draft202012Validator(_drop_required(load_schema(op_id, "response"), frozenset(_FREE_MESSAGE_OFFERING_KEYS)))


def assert_matches_tolerant(op_id: str, status: int, body: Any) -> None:
    """O esquema congelado de resposta de `op_id` inteiro, tolerando só a **ausência** das chaves
    de `_FREE_MESSAGE_OFFERING_KEYS` em **qualquer** objeto (o `Offering` no topo ou embutido, como em
    `offerings.prepare_archive`); presentes, conferidas pelo tipo. Para as sementes, que não podem falhar por isso
    antes do motivo próprio delas."""
    assert 200 <= status < 300, "assert_matches_tolerant só confere respostas de sucesso"
    errors = sorted(_tolerant_validator(op_id).iter_errors(body), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:10])
        raise AssertionError(
            f"{op_id} {status} não valida contra o esquema congelado (sem exigir {', '.join(_FREE_MESSAGE_OFFERING_KEYS)}): "
            f"{detail}")
