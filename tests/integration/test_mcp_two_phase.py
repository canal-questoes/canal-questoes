"""Duas fases pelo servidor MCP: o roteiro das quatro confirmações, o código vencido, de outro oferecimento ou já
usado nas confirmadoras, e as regras do expurgo pela ferramenta.

Com a API dos testes (o `client` da API base) como `ServerDeps.http`, o perfil pelas variáveis (`CQ_API_URL`, `CQ_TOKEN` e
o `CQ_CONFIG` inexistente do `tmp_path`) e o cliente MCP em processo nas duas eras (`MODES`):

- o **roteiro das duas fases**, numa conexão só: arquivamento (`prepare_archive_offering` →
  `archive_offering`), convites (`prepare_send_invites` → `send_invites`), disparo (`prepare_dispatch` →
  `confirm_dispatch`, com o `dispatch_id` como "código", já que o disparo não tem código de confirmação) e expurgo (`prepare_purge_offering` →
  `purge_offering`); o preparo não muda o estado, a confirmação muda, o resultado é o corpo da API no estruturado e
  no texto, e cada pedido leva `X-CQ-Client: mcp`;
- as 4 confirmadoras destrutivas do catálogo, parametrizadas, com o código vencido (o `FakeClock` além da validade),
  de outro oferecimento e já usado, com o texto exato e o instantâneo do banco inteiro antes e depois: no
  `archive_offering` e no `purge_offering`, o já usado devolve o resultado gravado, sem mudança (**[F]**
  `offerings.py:717-718`, `api/routes.py:36-37`); no `send_invites`, `[invalid_confirmation]` (**[F]**
  `students.py:531-554`); no `confirm_dispatch`, o já confirmado, o cancelado e o de prazo vencido dão
  `[invalid_dispatch_state]`, e o de outro oferecimento `[not_found]` (**[F]** `dispatches.py:664-676`);
- as regras do expurgo pela ferramenta: a recusa com trabalho pendente (com a linha `Pendências: …`) e
  o conflito de estado;
- cada confirmadora sem o código (ausente, vazio ou de outro tipo) → `-32602` sem chegar à API, e o código
  exigido no `inputSchema` de cada uma (nenhuma confirma sem o código do preparo).

Em cada caso, o resultado e os eventos novos são varridos por `scan` (identidades canário e segredos).

**[F]** A origem `mcp` do evento: no catálogo de eventos congelado
(`events.list.response.schema.json`), só `identity.revealed` e `identity.reveal_denied` têm o campo `client`; o
`dispatch.prepared` tem `origin` (que vale `mcp` pelo `X-CQ-Client`); o `offering.state_changed`, o
`student.invites_queued` e o `offering.purged` não têm cliente. Por isso a origem é conferida no cabeçalho de cada
pedido (o gancho de pedido do `TestClient`) e, no disparo, também no `origin` da resposta e do evento.

As conexões usam o portal síncrono do `anyio` (`start_blocking_portal` + `wrap_async_context_manager`), para que a
semente, o relógio e o banco fiquem no código síncrono do teste entre as chamadas. No esqueleto,
`build_server` lança `NotImplementedError` ao abrir a conexão: cada função falha
por ele.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import anyio.from_thread
import psycopg
import pytest
from psycopg import sql as psql

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    cancel_dispatch,
    create_offering,
    import_students,
    prepare_dispatch,
    prepare_invites,
    prepare_purge,
    register_bot,
)
from tests.helpers.calls import SEED_QUESTION, _validate_seeded_dispatches
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.mcp_client import (
    MISSING_PROFILE_FILE,
    MODES,
    SCAN_PREFIXES,
    call,
    in_process,
    list_tools,
    make_deps,
    scan,
)
from tests.helpers.purge import make_pending, seed_purge_scenario, snapshot_purge_tables
from tests.helpers.secrets import all_secrets, bot_secrets

BASE_URL = "http://testserver"
ADMIN = auth(ADMIN_TOKEN)
# A validade dos códigos de arquivamento, de convites e de expurgo: 10 minutos.
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
DEFAULT_VALID_HOURS = 168

# os textos exatos.
INVALID_ARGUMENTS = "Argumentos inválidos para a ferramenta."
INVALID_CONFIRMATION_TEXT = ("[invalid_confirmation] Código de confirmação inválido, vencido ou já usado. Prepare de "
                             "novo com a ferramenta de preparo.")
INVALID_DISPATCH_STATE_TEXT = "[invalid_dispatch_state] A operação não é permitida no estado atual do disparo."
NOT_FOUND_TEXT = "[not_found] Recurso não encontrado."
INVALID_STATE_TEXT = "[invalid_state] A operação não é permitida no estado atual do oferecimento."
PURGE_PENDING_TEXT = ("[purge_pending] Há trabalho pendente neste oferecimento; o expurgo só pode ser feito depois "
                      "que ele terminar.")

# As 4 confirmadoras destrutivas do catálogo e o campo do "código" de cada uma (no disparo, o `dispatch_id`).
CONFIRMERS: dict[str, str] = {
    "archive_offering": "confirmation_code",
    "send_invites": "confirmation_code",
    "confirm_dispatch": "dispatch_id",
    "purge_offering": "confirmation_code",
}

# O cenário mínimo do expurgo: um oferecimento arquivado com dois alunos, sem bot, equipe nem pendências.
MINIMAL_PURGE = {"bot": False, "team": False, "shared": False, "cross_offering": False, "frozen_report": False,
                 "free_message_before_register": False, "contact": None}
PURGE_CODE_COLUMNS = ("purge_code_sha256", "purge_code_expires_at", "purge_prepared_counts")

_counter = itertools.count(1)


# --------------------------------------------------------------------------------------------------
# Apoio


@dataclasses.dataclass
class _H:
    client: Any
    sql: psycopg.Connection
    clock: Any
    bot_api: Any
    request: Any
    tmp_path: Any


@pytest.fixture
def h(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, request: pytest.FixtureRequest,
      tmp_path: Any) -> _H:
    return _H(client=client, sql=sql, clock=clock, bot_api=bot_api, request=request, tmp_path=tmp_path)


@contextlib.contextmanager
def _recorded(client: Any) -> Iterator[list[Any]]:
    """Registra cada pedido que passa pelo `TestClient` (gancho de pedido do httpx2)."""
    seen: list[Any] = []
    hooks = client.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


class _Session:
    """Uma conexão MCP em processo, chamada do código síncrono; cada chamada devolve o `Outcome` e os pedidos que
    ela fez à API."""

    def __init__(self, portal: Any, mcp_client: Any, http: Any) -> None:
        self._portal = portal
        self._client = mcp_client
        self._http = http

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[Any, list[Any]]:
        with _recorded(self._http) as seen:
            outcome = self._portal.call(call, self._client, name, arguments)
        return outcome, list(seen)

    def tools(self) -> list[dict[str, Any]]:
        return self._portal.call(list_tools, self._client)


@contextlib.contextmanager
def _session(h: _H, mode: str) -> Iterator[_Session]:
    env = {"CQ_API_URL": BASE_URL, "CQ_TOKEN": ADMIN_TOKEN, "CQ_CONFIG": str(h.tmp_path / MISSING_PROFILE_FILE)}
    deps = make_deps(h.tmp_path, env=env, http=h.client)
    with anyio.from_thread.start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(in_process(deps, mode)) as mcp_client:
            yield _Session(portal, mcp_client, h.client)


def _secrets() -> list[str]:
    return all_secrets() + bot_secrets()


def _assert_mcp_requests(seen: list[Any], count: int = 1) -> None:
    assert len(seen) == count, [(r.method, r.url.path) for r in seen]
    for request in seen:
        assert request.headers.get("X-CQ-Client") == "mcp"


def _ok(outcome: Any, seen: list[Any], op_id: str) -> dict:
    """Sucesso: o corpo da API no estruturado, validado pelo esquema da operação, e o mesmo JSON no
    texto; um pedido à API com `X-CQ-Client: mcp`; nada de canário nem segredo."""
    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is False, outcome.text
    body = outcome.structured
    assert isinstance(body, dict)
    assert_matches(op_id, 200, body)
    assert outcome.text == json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    _assert_mcp_requests(seen)
    assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []
    return body


def _refused(outcome: Any, seen: list[Any], text: str) -> None:
    """Erro de execução: `isError`, só o texto exato, sem estruturado; a API foi chamada uma
    vez (e recusou); nada de canário nem segredo."""
    assert outcome.protocol_code is None, outcome.raw
    assert outcome.is_error is True
    assert outcome.structured is None
    assert outcome.text == text
    _assert_mcp_requests(seen)
    assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []


def _snapshot(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Todas as linhas de todas as tabelas do esquema `public` (como `jsonb` em texto, ordenadas): dois
    instantâneos iguais = nada mudou no banco."""
    tables = [row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
    query = "SELECT to_jsonb(cq_snapshot_row)::text FROM {} AS cq_snapshot_row ORDER BY 1"
    return {table: [row[0] for row in conn.execute(psql.SQL(query).format(psql.Identifier(table))).fetchall()]
            for table in tables}


def _refused_unchanged(h: _H, s: _Session, tool: str, arguments: dict, text: str) -> None:
    before = _snapshot(h.sql)
    _refused(*s.call(tool, arguments), text)
    assert _snapshot(h.sql) == before


def _repeated_unchanged(h: _H, s: _Session, tool: str, arguments: dict, op_id: str, recorded: dict) -> None:
    """A confirmação repetida devolve o resultado gravado, sem mudança no banco."""
    before = _snapshot(h.sql)
    assert _ok(*s.call(tool, arguments), op_id) == recorded
    assert _snapshot(h.sql) == before


def _cursor(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0])


def _events_after(h: _H, cursor: int, *types: str) -> list[dict]:
    """Os eventos dos tipos dados depois do cursor, pela API, sem o `seq`, varridos por `scan`."""
    events = [{k: v for k, v in e.items() if k != "seq"}
              for e in all_events(h.client, ADMIN_TOKEN, *types) if e["seq"] > cursor]
    assert scan(events, _secrets(), prefixes=SCAN_PREFIXES) == []
    return events


def _event(event_type: str, at: Any, data: dict, actor: str | None = "bootstrap") -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _get(h: _H, path: str, op_id: str) -> dict:
    resp = h.client.get(path, headers=ADMIN)
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _post(h: _H, path: str, body: Any = None) -> dict:
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    resp = h.client.post(path, headers=ADMIN, **kwargs)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def _code(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def _ref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _active(h: _H, prefix: str) -> dict:
    """Um oferecimento ativo, com um bot novo da Bot API falsa registrado e ativado."""
    offering = create_offering(h.client, ADMIN_TOKEN, code=_code(prefix))
    telegram_bot_id = h.bot_api.add_bot()
    register_bot(h.client, ADMIN_TOKEN, offering["code"], h.bot_api.token_of(telegram_bot_id))
    activate_bot(h.client, ADMIN_TOKEN, offering["code"])
    return offering


def _students(h: _H, offering: dict, count: int) -> None:
    """Alunos neutros (fora dos alvos canário) importados pela API."""
    rows = []
    for _ in range(count):
        n = next(_counter)
        rows.append({"name": f"Aluno Duas Fases {n}", "ra": f"771{n:07d}",
                     "email": f"aluno.duas.fases.{n}@turma.example.org"})
    import_students(h.client, ADMIN_TOKEN, offering["code"], rows)


def _archived_empty(h: _H, prefix: str) -> dict:
    """Um oferecimento arquivado direto do rascunho, sem bot nem pessoas (impacto do expurgo zero)."""
    offering = create_offering(h.client, ADMIN_TOKEN, code=_code(prefix))
    ref = offering["code"]
    prepared = _post(h, f"/v1/offerings/{ref}/archive/prepare")
    _post(h, f"/v1/offerings/{ref}/archive", {"confirmation_code": prepared["confirmation_code"]})
    return offering


def _validated_dispatches(h: _H, offering: dict, count: int) -> list[str]:
    """`count` disparos agendados da questão da semente, validados pelos tratadores reais, com aberturas
    distintas a partir de um dia depois; devolve os `dispatch_id` (agendados, sem confirmação)."""
    base = h.clock.now() + timedelta(days=1)
    ids = [prepare_dispatch(h.client, ADMIN_TOKEN, offering["code"], **SEED_QUESTION,
                            opens_at=iso(base + timedelta(minutes=i)))["dispatch_id"] for i in range(count)]
    _validate_seeded_dispatches(h.request, h.clock, expected=count)
    for dispatch_id in ids:
        body = _get(h, f"/v1/offerings/{offering['code']}/dispatches/{dispatch_id}", "dispatches.get")
        assert (body["state"], body["confirmed"]) == ("scheduled", False), body["state"]
    return ids


# --------------------------------------------------------------------------------------------------
# Roteiro das duas fases numa conexão só


@pytest.mark.parametrize("mode", MODES)
def test_two_phase_walkthrough(h: _H, mode: str) -> None:
    """Roteiro das duas fases: numa conexão, as 4 confirmações do catálogo pelo
    preparo e pelo código dele. (1) Arquivamento de um oferecimento **encerrado**: o preparo devolve o resumo e o
    código e o oferecimento fica igual; a confirmação arquiva (o estruturado é o `Offering` da API, igual ao
    `offerings.get` depois) e grava o `offering.state_changed` com o ator. (2) Convites: o preparo com `valid_hours`
    conta os elegíveis e nada enfileira; o envio enfileira e grava o `student.invites_queued`. (3) Disparo: o
    preparo sem `origin` fica com `origin: mcp` (resposta e `dispatch.prepared`); depois da validação, a
    confirmação pelo `dispatch_id` confirma, igual ao `dispatches.get` depois. (4) Expurgo (semente do expurgo): o
    preparo devolve o impacto esperado e não apaga nada; a confirmação devolve só as contagens esperadas e grava o
    `offering.purged`. Cada pedido com `X-CQ-Client: mcp`; resultados e eventos sem canário nem segredo."""
    closed = _active(h, "fases-arquivo")
    _post(h, f"/v1/offerings/{closed['code']}/close")
    invited = _active(h, "fases-convite")
    _students(h, invited, 2)
    dispatched = _active(h, "fases-disparo")
    scenario = seed_purge_scenario(h.client, h.sql, **MINIMAL_PURGE)

    with _session(h, mode) as s:
        # (1) Arquivamento.
        ref = closed["code"]
        before = _get(h, f"/v1/offerings/{ref}", "offerings.get")
        assert before["state"] == "closed"
        t0 = h.clock.now()
        prepared = _ok(*s.call("prepare_archive_offering", {"offering": ref}), "offerings.prepare_archive")
        assert CONFIRMATION_RE.fullmatch(prepared["confirmation_code"])
        assert prepared["expires_at"] == iso(t0 + CONFIRMATION)
        assert _get(h, f"/v1/offerings/{ref}", "offerings.get") == before
        cursor = _cursor(h.sql)
        h.clock.advance(30)
        t1 = h.clock.now()
        archived = _ok(*s.call("archive_offering", {"offering": ref,
                                                    "confirmation_code": prepared["confirmation_code"]}),
                       "offerings.archive")
        assert (archived["state"], archived["archived_at"]) == ("archived", iso(t1))
        assert archived == _get(h, f"/v1/offerings/{ref}", "offerings.get")
        assert _events_after(h, cursor, "offering.state_changed") == [
            _event("offering.state_changed", t1, {"offering_id": closed["offering_id"], "from_state": "closed",
                                                  "to_state": "archived"})]

        # (2) Convites.
        ref = invited["code"]
        t2 = h.clock.now()
        prepared = _ok(*s.call("prepare_send_invites", {"offering": ref, "valid_hours": 72}),
                       "invites.prepare_send")
        assert prepared == {"offering": _ref(invited), "eligible": 2, "valid_hours": 72,
                            "confirmation_code": prepared["confirmation_code"], "expires_at": iso(t2 + CONFIRMATION)}
        assert CONFIRMATION_RE.fullmatch(prepared["confirmation_code"])
        queued_before = h.sql.execute("SELECT count(*) FROM invite_sends").fetchone()[0]
        cursor = _cursor(h.sql)
        sent = _ok(*s.call("send_invites", {"offering": ref, "confirmation_code": prepared["confirmation_code"]}),
                   "invites.send")
        assert sent == {"offering": _ref(invited), "queued": 2, "valid_hours": 72}
        assert h.sql.execute("SELECT count(*) FROM invite_sends").fetchone()[0] == queued_before + 2
        assert _events_after(h, cursor, "student.invites_queued") == [
            _event("student.invites_queued", t2, {"offering_id": invited["offering_id"], "origin": "batch",
                                                  "queued": 2})]

        # (3) Disparo.
        ref = dispatched["code"]
        opens_at = iso(h.clock.now() + timedelta(days=1))
        cursor = _cursor(h.sql)
        prepared = _ok(*s.call("prepare_dispatch", {"offering": ref, **SEED_QUESTION, "opens_at": opens_at}),
                       "dispatches.prepare")
        assert (prepared["state"], prepared["confirmed"], prepared["origin"]) == ("validating", False, "mcp")
        dispatch_id = prepared["dispatch_id"]
        assert [e["data"]["origin"] for e in _events_after(h, cursor, "dispatch.prepared")] == ["mcp"]
        _validate_seeded_dispatches(h.request, h.clock, expected=1)
        path = f"/v1/offerings/{ref}/dispatches/{dispatch_id}"
        assert (_get(h, path, "dispatches.get")["state"], _get(h, path, "dispatches.get")["confirmed"]) == (
            "scheduled", False)
        cursor = _cursor(h.sql)
        confirmed = _ok(*s.call("confirm_dispatch", {"offering": ref, "dispatch_id": dispatch_id}),
                        "dispatches.confirm")
        assert (confirmed["dispatch_id"], confirmed["state"], confirmed["confirmed"]) == (
            dispatch_id, "scheduled", True)
        assert confirmed == _get(h, path, "dispatches.get")
        assert [e["data"]["dispatch_id"] for e in _events_after(h, cursor, "dispatch.confirmed")] == [dispatch_id]

        # (4) Expurgo.
        ref = scenario.code
        kept = snapshot_purge_tables(h.sql)
        prepared = _ok(*s.call("prepare_purge_offering", {"offering": ref}), "offerings.prepare_purge")
        assert prepared["impact"] == dataclasses.asdict(scenario.impact)
        assert CONFIRMATION_RE.fullmatch(prepared["confirmation_code"])
        assert _without_purge_code(snapshot_purge_tables(h.sql)) == _without_purge_code(kept)
        cursor = _cursor(h.sql)
        t4 = h.clock.now()
        purged = _ok(*s.call("purge_offering", {"offering": ref, "confirmation_code": prepared["confirmation_code"]}),
                     "offerings.purge")
        assert purged["result"] == dataclasses.asdict(scenario.outcome)
        assert purged["purged_at"] == iso(t4)
        assert [e["type"] for e in _events_after(h, cursor, "offering.purged")] == ["offering.purged"]


def _without_purge_code(snapshot: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """O instantâneo das tabelas do expurgo sem as três colunas do código pendente (que só o preparo grava)."""
    return {table: [({k: v for k, v in row.items() if k not in PURGE_CODE_COLUMNS} if table == "offerings" else row)
                    for row in rows]
            for table, rows in snapshot.items()}


# --------------------------------------------------------------------------------------------------
# Código vencido, de outro oferecimento e já usado


def _archive_cases(h: _H, s: _Session) -> None:
    x = create_offering(h.client, ADMIN_TOKEN, code=_code("fases-arq-x"))
    y = create_offering(h.client, ADMIN_TOKEN, code=_code("fases-arq-y"))
    px = _post(h, f"/v1/offerings/{x['code']}/archive/prepare")["confirmation_code"]
    _post(h, f"/v1/offerings/{y['code']}/archive/prepare")

    # De outro oferecimento.
    _refused_unchanged(h, s, "archive_offering", {"offering": y["code"], "confirmation_code": px},
                       INVALID_CONFIRMATION_TEXT)
    # Vencido.
    h.clock.advance(CONFIRMATION.total_seconds() + 1)
    _refused_unchanged(h, s, "archive_offering", {"offering": x["code"], "confirmation_code": px},
                       INVALID_CONFIRMATION_TEXT)
    # Já usado: o resultado gravado, sem mudança.
    fresh = _post(h, f"/v1/offerings/{x['code']}/archive/prepare")["confirmation_code"]
    arguments = {"offering": x["code"], "confirmation_code": fresh}
    archived = _ok(*s.call("archive_offering", arguments), "offerings.archive")
    assert archived["state"] == "archived"
    h.clock.advance(60)
    _repeated_unchanged(h, s, "archive_offering", arguments, "offerings.archive", archived)


def _invites_cases(h: _H, s: _Session) -> None:
    a = _active(h, "fases-conv-a")
    b = _active(h, "fases-conv-b")
    _students(h, a, 1)
    _students(h, b, 1)
    code_a = prepare_invites(h.client, ADMIN_TOKEN, a["code"])["confirmation_code"]
    prepare_invites(h.client, ADMIN_TOKEN, b["code"])

    # De outro oferecimento.
    _refused_unchanged(h, s, "send_invites", {"offering": b["code"], "confirmation_code": code_a},
                       INVALID_CONFIRMATION_TEXT)
    # Vencido.
    h.clock.advance(CONFIRMATION.total_seconds() + 1)
    _refused_unchanged(h, s, "send_invites", {"offering": a["code"], "confirmation_code": code_a},
                       INVALID_CONFIRMATION_TEXT)
    # Já usado: recusado (uso único), sem mudança.
    fresh = prepare_invites(h.client, ADMIN_TOKEN, a["code"])["confirmation_code"]
    arguments = {"offering": a["code"], "confirmation_code": fresh}
    sent = _ok(*s.call("send_invites", arguments), "invites.send")
    assert sent == {"offering": _ref(a), "queued": 1, "valid_hours": DEFAULT_VALID_HOURS}
    _refused_unchanged(h, s, "send_invites", arguments, INVALID_CONFIRMATION_TEXT)


def _dispatch_cases(h: _H, s: _Session) -> None:
    a = _active(h, "fases-disp-a")
    b = create_offering(h.client, ADMIN_TOKEN, code=_code("fases-disp-b"))
    first, cancelled, late = _validated_dispatches(h, a, 3)

    # De outro oferecimento: o disparo não existe nele.
    _refused_unchanged(h, s, "confirm_dispatch", {"offering": b["code"], "dispatch_id": first}, NOT_FOUND_TEXT)
    # Cancelado.
    cancel_dispatch(h.client, ADMIN_TOKEN, a["code"], cancelled)
    _refused_unchanged(h, s, "confirm_dispatch", {"offering": a["code"], "dispatch_id": cancelled},
                       INVALID_DISPATCH_STATE_TEXT)
    # Já confirmado.
    arguments = {"offering": a["code"], "dispatch_id": first}
    confirmed = _ok(*s.call("confirm_dispatch", arguments), "dispatches.confirm")
    assert (confirmed["state"], confirmed["confirmed"]) == ("scheduled", True)
    _refused_unchanged(h, s, "confirm_dispatch", arguments, INVALID_DISPATCH_STATE_TEXT)
    # Prazo de confirmação vencido (no agendado, o prazo é a abertura).
    opens_at = _get(h, f"/v1/offerings/{a['code']}/dispatches/{late}", "dispatches.get")["opens_at"]
    assert opens_at is not None
    h.clock.advance(timedelta(days=1, minutes=10).total_seconds())
    assert iso(h.clock.now()) > opens_at
    _refused_unchanged(h, s, "confirm_dispatch", {"offering": a["code"], "dispatch_id": late},
                       INVALID_DISPATCH_STATE_TEXT)


def _purge_cases(h: _H, s: _Session) -> None:
    x = _archived_empty(h, "fases-exp-x")
    y = _archived_empty(h, "fases-exp-y")
    code_x = prepare_purge(h.client, x["code"])["confirmation_code"]
    prepare_purge(h.client, y["code"])

    # De outro oferecimento.
    _refused_unchanged(h, s, "purge_offering", {"offering": y["code"], "confirmation_code": code_x},
                       INVALID_CONFIRMATION_TEXT)
    # Vencido.
    h.clock.advance(CONFIRMATION.total_seconds() + 1)
    _refused_unchanged(h, s, "purge_offering", {"offering": x["code"], "confirmation_code": code_x},
                       INVALID_CONFIRMATION_TEXT)
    # Já usado: o resultado gravado, sem mudança.
    fresh = prepare_purge(h.client, x["code"])["confirmation_code"]
    arguments = {"offering": x["code"], "confirmation_code": fresh}
    purged = _ok(*s.call("purge_offering", arguments), "offerings.purge")
    assert purged["offering"]["code"] == x["code"]
    h.clock.advance(3600)
    _repeated_unchanged(h, s, "purge_offering", arguments, "offerings.purge", purged)


CODE_CASES: dict[str, Callable[[_H, _Session], None]] = {
    "archive_offering": _archive_cases,
    "send_invites": _invites_cases,
    "confirm_dispatch": _dispatch_cases,
    "purge_offering": _purge_cases,
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", sorted(CODE_CASES))
def test_confirmer_with_expired_foreign_or_used_code(h: _H, tool: str, mode: str) -> None:
    """Código já usado nas confirmadoras idempotentes: em cada confirmadora destrutiva do
    catálogo, o código de outro oferecimento e o vencido (o relógio além dos 10 minutos) → `[invalid_confirmation]`
    com a mensagem reescrita para ferramentas, e o banco inteiro igual antes e depois; o já usado → no
    `archive_offering` e no `purge_offering`, o resultado gravado (o mesmo corpo da primeira confirmação), e no
    `send_invites`, `[invalid_confirmation]`, sempre sem mudança. No `confirm_dispatch` (o "código" é o
    `dispatch_id`): de outro oferecimento → `[not_found]`; cancelado, já confirmado e com o prazo de
    confirmação vencido → `[invalid_dispatch_state]`, sem mudança. Cada pedido com `X-CQ-Client: mcp`; nada de
    canário nem segredo."""
    assert set(CODE_CASES) == set(CONFIRMERS)
    with _session(h, mode) as s:
        CODE_CASES[tool](h, s)


# --------------------------------------------------------------------------------------------------
# As regras do expurgo pela ferramenta


@pytest.mark.parametrize("mode", MODES)
def test_purge_rules_of_the_cli_hold(h: _H, mode: str) -> None:
    """As contagens de `pending` em `Pendências: …`: com uma unidade de trabalho pendente
    (um update do Telegram) no oferecimento arquivado da semente do expurgo, o preparo e a confirmação (com um código de
    antes do pendente) falham com `[purge_pending]` e a linha das pendências exata, sem mudança; num oferecimento
    ativo, o preparo e a confirmação falham com `[invalid_state]`, sem mudança."""
    scenario = seed_purge_scenario(h.client, h.sql)
    code = prepare_purge(h.client, scenario.code)["confirmation_code"]
    assert make_pending(h.sql, "update_pending", scenario) == "updates"
    pending_text = (PURGE_PENDING_TEXT + "\n"
                    + "Pendências: fila 0, updates 1, tarefas 0, congelamento 0, devidas 0.")
    active = _active(h, "fases-exp-ativo")

    with _session(h, mode) as s:
        _refused_unchanged(h, s, "prepare_purge_offering", {"offering": scenario.code}, pending_text)
        _refused_unchanged(h, s, "purge_offering", {"offering": scenario.code, "confirmation_code": code},
                           pending_text)
        _refused_unchanged(h, s, "prepare_purge_offering", {"offering": active["code"]}, INVALID_STATE_TEXT)
        _refused_unchanged(h, s, "purge_offering", {"offering": active["code"], "confirmation_code": "c" * 22},
                           INVALID_STATE_TEXT)


# --------------------------------------------------------------------------------------------------
# Confirmadora sem o código


@pytest.mark.parametrize("mode", MODES)
def test_confirmer_without_code_is_refused_by_the_schema(h: _H, mode: str) -> None:
    """Recusa de protocolo (`-32602` com `"Argumentos inválidos para a ferramenta."`, sem `data`): cada
    confirmadora destrutiva do catálogo chamada sem o código, com ele vazio, de outro tipo ou nulo (e o
    `confirm_dispatch` com um `dispatch_id` fora da forma) é recusada pelo esquema de entrada, **sem** pedido à
    API e sem eco; o `inputSchema` de cada uma exige o código (nenhuma confirma sem o código do preparo)."""
    offering = create_offering(h.client, ADMIN_TOKEN, code=_code("fases-sem-codigo"))
    variants: list[Any] = ["<ausente>", "", 12345678, None, ["c" * 22]]
    with _session(h, mode) as s:
        tools = {tool["name"]: tool for tool in s.tools()}
        before = _snapshot(h.sql)
        for tool, field in CONFIRMERS.items():
            assert field in tools[tool]["inputSchema"]["required"], tool
            cases = variants + (["0d0d0d0d-0d0d-0d0d-0d0d-0d0d0d0d0d0d", "nao-e-um-uuid"]
                                if field == "dispatch_id" else [])
            for value in cases:
                arguments: dict[str, Any] = {"offering": offering["code"]}
                if value != "<ausente>":
                    arguments[field] = value
                outcome, seen = s.call(tool, arguments)
                assert (outcome.protocol_code, outcome.protocol_message) == (-32602, INVALID_ARGUMENTS), (
                    tool, value, outcome.raw)
                assert "data" not in outcome.raw
                assert seen == []
                assert scan(outcome, _secrets(), prefixes=SCAN_PREFIXES) == []
                if isinstance(value, str) and value:
                    assert value not in json.dumps(outcome.raw, ensure_ascii=False)
        assert _snapshot(h.sql) == before
