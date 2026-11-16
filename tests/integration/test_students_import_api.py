"""Importação da lista da turma pela API.

Cobre a importação da lista: as linhas do CSV com
espaços, BOM e maiúsculas no e-mail (a parte da API), a pessoa reaproveitada com nome diferente,
a pessoa já cadastrada com o RA da lista e sem e-mail, o aluno já matriculado, equipe e aluno, o arquivo
grande e a entrada estrita. Contratos: a rota, os erros e a validação da importação, os eventos e
as convenções da API base e de oferecimentos e bots que continuam valendo (ordem das verificações
401 → 404/405 → 400 → 403 →
regras; entrada estrita; sem eco; horários em UTC).

Convenções: corpo inteiro por `==` e `assert_matches` em toda resposta de sucesso; erros
pelo envelope exato (`assert_error`); horários pelo `FakeClock` e `iso`; ids internos, `seq` e
pseudônimos gerados nunca como literal (só relações: as linhas do banco são lidas por junção com
`pseudo_id`, `offering_id` e o RA da área restrita, que o teste lê direto no banco); os eventos são
lidos pela API (`events.list`, esquema congelado) depois de um cursor tirado do banco, por inteiro e
na ordem de `seq`. Os 8 tipos de evento do vínculo do aluno vão como literais de texto (o esqueleto ainda não os
tem). Dados só fictícios: os alunos canário (tests/helpers/canary.py) e linhas neutras inventadas
(RAs `77…`, e-mails em `turma.example.org`).

No esqueleto, a rota `POST /v1/offerings/{ref}/students` não existe (404) e `students.import_roster`
lança `NotImplementedError`: toda função daqui falha pela rota ausente (nenhuma passa já no
esqueleto). As asserções sobre o que o esqueleto já tem (o 401 da API base, que o
middleware dá antes do roteamento; o `students.shuffle` padrão, real no esqueleto) ficam na mesma
função de um caso que falha.
"""

from __future__ import annotations

import contextlib
import inspect
import itertools
import json
import re
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg
import pytest

from cq_server import students as students_core
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_team_link,
    create_token,
    register_bot,
    register_person,
)
from tests.helpers.canary import ALL_PREFIXES, STUDENT_A, STUDENT_B, STUDENT_PREFIXES, find_canary
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.logs import capture_logs

ADMIN = auth(ADMIN_TOKEN)

# Formatos da API base (os mesmos padrões dos esquemas congelados).
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ALIAS_RE = re.compile(r"^(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}$")
_ANY_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Rota de `students.import`, como o registro `request` a mostra.
IMPORT_ROUTE = "/v1/offerings/{ref}/students"

# Todos os tipos de evento (os já existentes e os 8 do vínculo do aluno), para ler o log inteiro depois de
# um cursor. Os 8 novos são literais de texto: o `EventType` do esqueleto ainda é o anterior.
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
STUDENT_LINK_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                  "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                  "student.invite_revoked", "student.joined")
ALL_EVENT_TYPES = CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_LINK_EVENT_TYPES

# As contagens da resposta e do `student.list_imported`.
COUNT_KEYS = ("received", "people_created", "people_reused", "enrollments_created", "already_enrolled")

# Limites de identidade da API base.
NAME_201 = ("Nomeficticio" * 17)[:201]
NAME_RAW_TO_201 = "   " + "  \t ".join(["Nome"] * 40) + "Zy  "
EMAIL_255 = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 50 + ".example.com"
assert len(NAME_201) == 201 and len(" ".join(NAME_RAW_TO_201.split())) == 201
assert len(EMAIL_255) == 255

_DROP: Any = object()


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _context(label: str) -> Iterator[None]:
    """Prefixa a falha com o caso do laço."""
    try:
        yield
    except AssertionError as exc:
        raise AssertionError(f"[{label}] {exc}") from exc


def _row(n: int, **changes: Any) -> dict:
    """Linha neutra e válida da lista (`{name, ra, email}`), única por `n`, com as trocas dadas
    (`_DROP` tira a chave; uma chave nova entra no item)."""
    row: dict[str, Any] = {"name": f"Estudante Ficticio {n:04d}", "ra": f"77{n:08d}",
                           "email": f"estudante.{n:04d}@turma.example.org"}
    for key, value in changes.items():
        if value is _DROP:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def _path(ref: str) -> str:
    return f"/v1/offerings/{urllib.parse.quote(ref, safe='')}/students"


def _post(client: Any, ref: str, body: Any, *, headers: dict[str, str] = ADMIN,
          params: Any = None) -> Any:
    return client.post(_path(ref), json=body, headers=headers, params=params)


def _post_raw(client: Any, ref: str, body: Any, *, headers: dict[str, str] = ADMIN) -> Any:
    """Envia o JSON com escapes ASCII (`\\u0000`, `\\ud800`), para caracteres que o cliente HTTP não
    codifica em UTF-8 (substituto isolado)."""
    return client.post(_path(ref), content=json.dumps(body).encode("ascii"),
                       headers={**headers, "Content-Type": "application/json"})


def _imported(resp: Any) -> dict:
    assert resp.status_code == 200, f"importação falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("students.import", 200, body)
    return body


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _expected(offering: dict, received: int, people_created: int, people_reused: int,
              enrollments_created: int, already_enrolled: int) -> dict:
    """Corpo 200 exato de `students.import`: o `OfferingRef` e só contagens."""
    assert people_created + people_reused == received
    assert enrollments_created + already_enrolled == received
    return {"offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
            "received": received, "people_created": people_created, "people_reused": people_reused,
            "enrollments_created": enrollments_created, "already_enrolled": already_enrolled}


def _imported_event(result: dict, at: Any, actor: str | None = "bootstrap") -> dict:
    """`student.list_imported` exato: o `offering_id` e as contagens da resposta."""
    return {"type": "student.list_imported", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"offering_id": result["offering"]["offering_id"],
                     **{key: result[key] for key in COUNT_KEYS}}}


def _registered_event(pseudo_id: str, alias: str, at: Any, actor: str | None = "bootstrap") -> dict:
    """`person.registered` da API base, com os pseudônimos."""
    return {"type": "person.registered", "occurred_at": iso(at), "actor_token_id": actor,
            "data": {"pseudo_id": pseudo_id, "alias": alias}}


def _last_seq(sql: psycopg.Connection) -> int:
    """Cursor dos eventos tirado do banco (não da API, para não depender dos tipos novos do vínculo do aluno)."""
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Todos os eventos depois do cursor, pela API, em ordem de `seq` (crescente), sem o `seq`."""
    events = [e for e in all_events(client, ADMIN_TOKEN, *ALL_EVENT_TYPES) if e["seq"] > cursor]
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _people(sql: psycopg.Connection) -> list[tuple]:
    """Todas as pessoas em ordem de `people.id`, com a identidade da área restrita:
    `(pseudo_id, alias, created_at, full_name, ra, email, email_normalized)`."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, p.alias, p.created_at, i.full_name, i.ra, i.email, i.email_normalized
          FROM people p
          JOIN identities i ON i.person_id = p.id
         ORDER BY p.id
        """).fetchall()


def _enrollments(sql: psycopg.Connection, offering_id: str) -> list[tuple]:
    """As matrículas do oferecimento em ordem de `enrollments.id`, com as colunas do vínculo do aluno:
    `(pseudo_id, ra, role, state, created_at, activated_at, terms_version, terms_accepted_at)`."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, i.ra, e.role, e.state, e.created_at, e.activated_at,
               e.terms_version, e.terms_accepted_at
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN identities i ON i.person_id = p.id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s
         ORDER BY e.id
        """, (uuid.UUID(offering_id),)).fetchall()


def _snapshot(sql: psycopg.Connection) -> dict[str, list[tuple]]:
    """O que uma importação poderia gravar (só colunas anteriores ao vínculo do aluno): pessoas, identidades,
    matrículas, convites, endereços, tarefas, eventos e oferecimentos."""
    return {
        "people": sql.execute("SELECT id, pseudo_id::text, alias, created_at FROM people ORDER BY id").fetchall(),
        "identities": sql.execute(
            "SELECT person_id, full_name, ra, email, email_normalized FROM identities "
            "ORDER BY person_id").fetchall(),
        "enrollments": sql.execute(
            "SELECT id, person_id, offering_id, role, state, created_at, activated_at FROM enrollments "
            "ORDER BY id").fetchall(),
        "invites": sql.execute("SELECT id, enrollment_id, code_sha256, state FROM invites ORDER BY id").fetchall(),
        "delivery_endpoints": sql.execute("SELECT id, person_id FROM delivery_endpoints ORDER BY id").fetchall(),
        "tasks": sql.execute("SELECT id, task_type, state FROM tasks ORDER BY id").fetchall(),
        "events": sql.execute(
            "SELECT seq, type, occurred_at, actor_token_id, data FROM events ORDER BY seq").fetchall(),
        "offerings": sql.execute(
            "SELECT id, code, state, activated_at, closed_at, archived_at FROM offerings ORDER BY id").fetchall(),
    }


def _count(sql: psycopg.Connection, table: str) -> int:
    return sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _expected_alias(pseudo_id: str, taken: set[str]) -> str | None:
    """Primeiro prefixo do hex (sem hífens) com 4+ caracteres, com letra e livre (a regra do apelido
    da API base)."""
    hexed = uuid.UUID(pseudo_id).hex
    for size in range(4, 33):
        candidate = hexed[:size]
        if re.search("[a-f]", candidate) and candidate not in taken:
            return candidate
    return None


def _assert_pseudonyms(people: Sequence[tuple]) -> None:
    """Cada pessoa (todas, em ordem de `people.id`) tem `pseudo_id` UUID v4 canônico e o apelido
    da regra da API base, calculado com os apelidos das pessoas gravadas antes dela."""
    taken: set[str] = set()
    for pseudo_id, alias, *_ in people:
        parsed = uuid.UUID(pseudo_id)
        assert UUID_RE.fullmatch(pseudo_id) and parsed.version == 4 and str(parsed) == pseudo_id, pseudo_id
        assert ALIAS_RE.fullmatch(alias), alias
        assert alias == _expected_alias(pseudo_id, taken), (pseudo_id, alias)
        taken.add(alias)


def _response_text(resp: Any) -> str:
    """Corpo decodificado (sem os escapes `\\uXXXX`, que o detector de apelido confundiria com hex)
    e cabeçalhos da resposta."""
    try:
        body = json.dumps(resp.json(), ensure_ascii=False)
    except ValueError:
        body = resp.text
    return body + "\n" + "\n".join(f"{k}: {v}" for k, v in resp.headers.items())


def _logs_text(cap: Any) -> str:
    """Os registros capturados, decodificados (a saída do formatter é só ASCII)."""
    return json.dumps(cap.records, ensure_ascii=False)


def _pseudonym_hits(text: str, people: Sequence[tuple]) -> list[str]:
    """Pseudônimos de `people` (`(pseudo_id, alias, …)`) que aparecem em `text`: o `pseudo_id` (com
    e sem hífens) por subcadeia; o apelido como sequência hex inteira (sem hex dos dois lados), depois
    de tirar do texto os UUIDs (ex.: o `offering_id`), cujos grupos não são apelidos."""
    lowered = text.lower()
    hits = [pid for pid, *_ in people if pid in lowered or uuid.UUID(pid).hex in lowered]
    stripped = _ANY_UUID.sub(" ", lowered)
    hits += [alias for _, alias, *_ in people
             if re.search(rf"(?<![0-9a-f]){re.escape(alias)}(?![0-9a-f])", stripped)]
    return hits


def _identity_values(rows: Sequence[dict]) -> list[str]:
    """Os valores de identidade de linhas neutras, como a API os grava (nome normalizado, RA, e-mail
    aparado e a parte local)."""
    values: list[str] = []
    for row in rows:
        email = row["email"].strip()
        values += [" ".join(row["name"].split()), row["ra"], email, email.split("@")[0]]
    return values


def _identity_hits(text: str, rows: Sequence[dict]) -> list[str]:
    """Identidade em `text`: os alvos canário (já existentes e de alunos) e os valores das linhas neutras
    (sem diferenciar maiúsculas)."""
    hits = find_canary(text) + find_canary(text, prefixes=ALL_PREFIXES)
    hits += find_canary(text, prefixes=STUDENT_PREFIXES)
    folded = text.casefold()
    hits += [value for value in _identity_values(rows) if value.casefold() in folded]
    return hits


SCHEMA_FIELDS = frozenset({"rows", "name", "ra", "email"})


def _sentinels(value: Any) -> list[str]:
    """Textos enviados com 8+ caracteres (e as partes em volta de NUL/substitutos), para o eco. As
    chaves que são nomes de campo do esquema ficam de fora (o envelope devolve `rows[i]` em
    `fields`); as desconhecidas continuam sentinelas."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += ([] if key in SCHEMA_FIELDS else _sentinels(key)) + _sentinels(item)
    elif isinstance(value, list):
        for item in value:
            found += _sentinels(item)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        for part in re.split("[\x00\ud800-\udfff]", text):
            for candidate in dict.fromkeys((part, part.strip())):
                if len(candidate) >= 8:
                    found.append(candidate)
    return list(dict.fromkeys(found))


def _assert_no_echo_in_logs(cap: Any, sentinels: Sequence[str]) -> None:
    text = _logs_text(cap).casefold()
    echoed = [value for value in sentinels if value.casefold() in text]
    assert echoed == [], f"os logs ecoam valores enviados: {echoed[:5]}"


def _offering_in_state(client: Any, bot_api: Any, code: str, state: str) -> dict:
    """Um oferecimento no estado dado: `draft` (sem bot), `active` e `closed` (com um bot novo da
    Bot API falsa, ativado e, no `closed`, encerrado) ou `archived` (arquivado a partir do rascunho).
    Devolve o corpo do cadastro (o `offering_id` e o `code` não mudam)."""
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    if state == "draft":
        return offering
    if state == "archived":
        prepared = _ok(client.post(f"/v1/offerings/{code}/archive/prepare", headers=ADMIN),
                       "offerings.prepare_archive")
        archived = _ok(client.post(f"/v1/offerings/{code}/archive",
                                   json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN),
                       "offerings.archive")
        assert archived["state"] == "archived"
        return offering
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_api.add_bot()))
    assert activate_bot(client, ADMIN_TOKEN, code)["state"] == "active"
    if state == "closed":
        closed = _ok(client.post(f"/v1/offerings/{code}/close", headers=ADMIN), "offerings.close")
        assert closed["state"] == "closed"
    else:
        assert state == "active", state
    return offering


def _team_member(client: Any, bot_api: Any, code: str, **identity: Any) -> dict:
    """Cadastra uma pessoa e a põe na equipe do oferecimento `code`, ainda sem bot (registra um bot
    novo da Bot API falsa e gera o link de equipe, que cria a matrícula `team`/`pending`)."""
    person = register_person(client, ADMIN_TOKEN, **identity)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_api.add_bot()))
    create_team_link(client, ADMIN_TOKEN, code, person["alias"])
    return person


# ---------------------------------------------------------------------------------------------
# Importação aceita
# ---------------------------------------------------------------------------------------------


def test_new_rows_return_only_counts_and_record_people_enrollments_and_events(
        client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Linhas novas; linha do CSV com espaços e
    maiúsculas no e-mail (a API apara o nome e o e-mail; o e-mail fica com a caixa enviada e o
    normalizado em `casefold`): N = 5 linhas novas (os dois
    alunos canário e três neutras, uma com espaços) num rascunho → 200 com o corpo exato (só o
    `OfferingRef` e as contagens); N pessoas com `pseudo_id` UUID v4 e o apelido da regra da API base,
    `created_at` do `FakeClock`, e a identidade normalizada na área restrita; N matrículas
    `student`/`pending` (sem `activated_at`, `terms_version` nem `terms_accepted_at`); nenhum convite,
    envio, endereço nem tarefa; exatamente N `person.registered` (com os pseudônimos, na ordem de
    `people.id`, que é a embaralhada) e depois um `student.list_imported` (só contagens, sem
    pseudônimo). Nenhuma identidade nem pseudônimo na resposta nem nos logs do pedido, com controle
    positivo dos dois detectores; o registro `request` sai com a rota e o status."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-turma-nova")
    cursor = _last_seq(sql)
    assert _count(sql, "people") == 0
    rows = [
        STUDENT_A.row(),
        _row(1, name="  Estudante   Ficticio\tUm  ", email="  Estudante.Um@Turma.Example.ORG  "),
        STUDENT_B.row(),
        _row(2),
        _row(3),
    ]
    clock.advance(7.25)
    t1 = clock.now()
    with capture_logs() as cap:
        resp = _post(client, offering["code"], {"rows": rows})
    body = _imported(resp)
    assert body == _expected(offering, 5, 5, 0, 5, 0)

    people = _people(sql)
    _assert_pseudonyms(people)
    assert [p[2] for p in people] == [t1] * 5
    assert {p[4]: (p[3], p[5], p[6]) for p in people} == {
        STUDENT_A.ra: (STUDENT_A.name, STUDENT_A.email, STUDENT_A.email.casefold()),
        STUDENT_B.ra: (STUDENT_B.name, STUDENT_B.email, STUDENT_B.email.casefold()),
        "7700000001": ("Estudante Ficticio Um", "Estudante.Um@Turma.Example.ORG", "estudante.um@turma.example.org"),
        "7700000002": ("Estudante Ficticio 0002", "estudante.0002@turma.example.org",
                       "estudante.0002@turma.example.org"),
        "7700000003": ("Estudante Ficticio 0003", "estudante.0003@turma.example.org",
                       "estudante.0003@turma.example.org"),
    }
    pid_by_ra = {p[4]: p[0] for p in people}
    enrollments = _enrollments(sql, offering["offering_id"])
    assert sorted(enrollments) == sorted(
        (pid_by_ra[ra], ra, "student", "pending", t1, None, None, None) for ra in pid_by_ra)
    for table in ("invites", "invite_sends", "delivery_endpoints", "tasks"):
        with _context(table):
            assert _count(sql, table) == 0

    events = _events_after(client, cursor)
    assert events == [_registered_event(p[0], p[1], t1) for p in people] + [_imported_event(body, t1)]
    pairs = [(p[0], p[1]) for p in people]
    assert _pseudonym_hits(json.dumps(events[-1], ensure_ascii=False), pairs) == []
    assert _identity_hits(json.dumps(events, ensure_ascii=False), rows) == []

    outputs = {"resposta": _response_text(resp), "logs": _logs_text(cap)}
    for name, text in outputs.items():
        with _context(name):
            assert _identity_hits(text, rows) == []
            assert _pseudonym_hits(text, pairs) == []
    assert [(r.get("method"), r.get("route"), r.get("status")) for r in cap.with_event("request")] == [
        ("POST", IMPORT_ROUTE, 200)]
    # Controles positivos: os detectores acham a identidade enviada e os pseudônimos dos eventos.
    sent = json.dumps({"rows": rows}, ensure_ascii=False)
    assert f"aluno-ra:{STUDENT_A.ra}" in find_canary(sent, prefixes=STUDENT_PREFIXES)
    assert "7700000001" in _identity_hits(sent, rows)
    assert sorted(_pseudonym_hits(json.dumps(events, ensure_ascii=False), pairs)) == sorted(
        [p[0] for p in people] + [p[1] for p in people])


def test_reused_person_keeps_identity_and_gets_the_enrollment(client: Any, clock: Any,
                                                              sql: psycopg.Connection) -> None:
    """Pessoa reaproveitada com nome diferente (importação:
    RA e e-mail normalizado apontam a mesma pessoa → reaproveitada; a identidade gravada não
    muda, nem o nome); o `ref` pelo `offering_id`: uma pessoa já cadastrada aparece na
    lista com o mesmo RA, o e-mail com outra caixa e espaços e outro nome → 200 com `people_reused =
    1`; a pessoa, o pseudônimo e a identidade (nome, e-mail com a caixa original) ficam como estavam;
    ela ganha a matrícula de aluno; só a linha nova gera `person.registered`; a outra pessoa
    cadastrada não é tocada."""
    reused = register_person(client, ADMIN_TOKEN, name="Pessoa Reaproveitada Original", ra="7711000001",
                             email="Pessoa.Reaproveitada@Turma.Example.ORG")
    untouched = register_person(client, ADMIN_TOKEN, name="Pessoa Sem Relacao Nenhuma", ra="7711000002",
                                email="sem.relacao@turma.example.org")
    offering = create_offering(client, ADMIN_TOKEN, code="lista-reaproveitada")
    before = _people(sql)
    assert [p[0] for p in before] == [reused["pseudo_id"], untouched["pseudo_id"]]
    cursor = _last_seq(sql)
    clock.advance(30)
    t1 = clock.now()
    rows = [_row(5), {"name": "  Nome   Diferente Qualquer ", "ra": "7711000001",
                      "email": "  pessoa.reaproveitada@TURMA.example.org "}]
    with capture_logs() as cap:
        resp = _post(client, offering["offering_id"], {"rows": rows})
    body = _imported(resp)
    assert body == _expected(offering, 2, 1, 1, 2, 0)

    people = _people(sql)
    assert people[:2] == before
    assert len(people) == 3
    new = people[2]
    assert new[2:] == (t1, "Estudante Ficticio 0005", "7700000005", "estudante.0005@turma.example.org",
                       "estudante.0005@turma.example.org")
    _assert_pseudonyms(people)
    assert sorted(_enrollments(sql, offering["offering_id"])) == sorted([
        (reused["pseudo_id"], "7711000001", "student", "pending", t1, None, None, None),
        (new[0], "7700000005", "student", "pending", t1, None, None, None),
    ])
    assert _events_after(client, cursor) == [_registered_event(new[0], new[1], t1), _imported_event(body, t1)]
    pairs = [(p[0], p[1]) for p in people]
    for name, text in {"resposta": _response_text(resp), "logs": _logs_text(cap)}.items():
        with _context(name):
            assert _pseudonym_hits(text, pairs) == []
            assert _identity_hits(text, rows) == []
            assert "Pessoa Reaproveitada Original".casefold() not in text.casefold()


def test_already_enrolled_is_counted_and_the_import_only_adds(client: Any, clock: Any,
                                                              sql: psycopg.Connection) -> None:
    """Aluno já matriculado (não duplica nada e entra na contagem de já
    matriculados; a importação só acrescenta, nunca remove): a 1ª importação (A, B,
    C) cria 3; a 2ª (B com o e-mail em maiúsculas, C e D novo) → `people_reused = 2`,
    `already_enrolled = 2`, só D ganha pessoa e matrícula, e A continua matriculado; a 3ª (só A) →
    `already_enrolled = 1`, nada muda; a mesma pessoa, aluna de um oferecimento, é importada noutro e
    ganha a matrícula dele. Cada importação grava os `person.registered` das pessoas novas e um
    `student.list_imported` exato."""
    first = create_offering(client, ADMIN_TOKEN, code="lista-ja-matriculados")
    second = create_offering(client, ADMIN_TOKEN, code="lista-outra-turma")
    a, b, c, d = _row(11), _row(12), _row(13), _row(14)
    t0 = clock.now()
    body = _imported(_post(client, first["code"], {"rows": [a, b, c]}))
    assert body == _expected(first, 3, 3, 0, 3, 0)
    people_before = _people(sql)
    enrollments_before = _enrollments(sql, first["offering_id"])
    assert sorted(e[1:] for e in enrollments_before) == sorted(
        (row["ra"], "student", "pending", t0, None, None, None) for row in (a, b, c))

    cursor = _last_seq(sql)
    clock.advance(60)
    t1 = clock.now()
    body = _imported(_post(client, first["code"], {"rows": [dict(b, email=b["email"].upper()), c, d]}))
    assert body == _expected(first, 3, 1, 2, 1, 2)
    people = _people(sql)
    assert people[:3] == people_before and len(people) == 4
    new = people[3]
    assert new[2:] == (t1, d["name"], d["ra"], d["email"], d["email"])
    assert _enrollments(sql, first["offering_id"]) == enrollments_before + [
        (new[0], d["ra"], "student", "pending", t1, None, None, None)]
    assert _events_after(client, cursor) == [_registered_event(new[0], new[1], t1), _imported_event(body, t1)]

    cursor = _last_seq(sql)
    clock.advance(60)
    t2 = clock.now()
    enrollments_now = _enrollments(sql, first["offering_id"])
    body = _imported(_post(client, first["code"], {"rows": [a]}))
    assert body == _expected(first, 1, 0, 1, 0, 1)
    assert _people(sql) == people
    assert _enrollments(sql, first["offering_id"]) == enrollments_now
    assert _events_after(client, cursor) == [_imported_event(body, t2)]

    cursor = _last_seq(sql)
    clock.advance(60)
    t3 = clock.now()
    body = _imported(_post(client, second["offering_id"], {"rows": [a, b]}))
    assert body == _expected(second, 2, 0, 2, 2, 0)
    pid_by_ra = {p[4]: p[0] for p in people}
    assert sorted(_enrollments(sql, second["offering_id"])) == sorted(
        (pid_by_ra[row["ra"]], row["ra"], "student", "pending", t3, None, None, None) for row in (a, b))
    assert _enrollments(sql, first["offering_id"]) == enrollments_now
    assert _people(sql) == people
    assert _events_after(client, cursor) == [_imported_event(body, t3)]


# ---------------------------------------------------------------------------------------------
# Conflitos
# ---------------------------------------------------------------------------------------------


def test_team_member_row_is_invalid_value_and_team_elsewhere_is_reused(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any) -> None:
    """Membro da equipe na lista de alunos; o 409 `list_conflict` (com a
    mensagem exata e `fields` obrigatório); importação (pessoa reaproveitada com
    matrícula de equipe no oferecimento → `[{rows[i], invalid_value}]`): a linha de um membro da
    equipe do próprio oferecimento → 409 `[{rows[1], invalid_value}]`, sem eco (nem os valores, nem o
    pseudônimo do membro) e nada gravado; a matrícula de equipe continua a de antes. A mesma lista noutro
    oferecimento, em que a pessoa não é da equipe, é aceita: ela é reaproveitada e ganha a matrícula
    de aluno lá."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-com-equipe")
    member = _team_member(client, bot_api, offering["code"], name="Pessoa Da Equipe Ficticia",
                          ra="7722000001", email="equipe.ficticia@turma.example.org")
    t_link = clock.now()
    elsewhere = create_offering(client, ADMIN_TOKEN, code="lista-sem-equipe")
    team_row = (member["pseudo_id"], "7722000001", "team", "pending", t_link, None, None, None)
    before = _snapshot(sql)

    rows = [_row(21), {"name": "Pessoa Da Equipe Ficticia", "ra": "7722000001",
                       "email": "Equipe.Ficticia@Turma.Example.org"}]
    clock.advance(5)
    with capture_logs() as cap:
        resp = _post(client, offering["code"], {"rows": rows})
    assert_error(resp, 409, "list_conflict", fields=[("rows[1]", "invalid_value")])
    assert_no_echo(resp, *_sentinels({"rows": rows}), member["pseudo_id"])
    member_pair = [(member["pseudo_id"], member["alias"])]
    assert _pseudonym_hits(_response_text(resp), member_pair) == []
    assert _pseudonym_hits(_logs_text(cap), member_pair) == []
    _assert_no_echo_in_logs(cap, _sentinels({"rows": rows}))
    assert _snapshot(sql) == before

    t1 = clock.now()
    body = _imported(_post(client, elsewhere["code"], {"rows": rows}))
    assert body == _expected(elsewhere, 2, 1, 1, 2, 0)
    new_pid = next(p[0] for p in _people(sql) if p[4] == "7700000021")
    assert sorted(_enrollments(sql, elsewhere["offering_id"])) == sorted([
        (member["pseudo_id"], "7722000001", "student", "pending", t1, None, None, None),
        (new_pid, "7700000021", "student", "pending", t1, None, None, None),
    ])
    assert _enrollments(sql, offering["offering_id"]) == [team_row]


def test_conflicts_with_registered_people_are_listed_in_index_order_and_nothing_is_written(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any) -> None:
    """Conflitos com o cadastro, inclusive a pessoa já cadastrada com o RA da lista e sem e-mail;
    importação: numa lista com linhas novas, uma
    reaproveitável e seis divergentes — RA de uma pessoa com outro e-mail, e-mail (em outra caixa) de
    outra pessoa, membro da equipe, RA de uma pessoa sem e-mail, e-mail de uma pessoa sem RA, RA de
    uma pessoa e e-mail de outra — → 409 `list_conflict` com um item por linha, em ordem de índice
    (`duplicate` e `invalid_value`), sem eco dos valores nem dos pseudônimos das pessoas cadastradas,
    e nada gravado (nem as linhas boas). Só as linhas boas, depois, → 200."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-conflitos")
    full = register_person(client, ADMIN_TOKEN, name="Pessoa Completa Ficticia", ra="7733000001",
                           email="Completa.Ficticia@Turma.Example.org")
    ra_only = register_person(client, ADMIN_TOKEN, name="Pessoa So Com Ra Ficticia", ra="7733000002")
    email_only = register_person(client, ADMIN_TOKEN, name="Pessoa So Com Email Ficticia",
                                 email="so.email.ficticio@turma.example.org")
    reusable = register_person(client, ADMIN_TOKEN, name="Pessoa Reaproveitavel Ficticia", ra="7733000004",
                               email="reaproveitavel.ficticia@turma.example.org")
    x = register_person(client, ADMIN_TOKEN, name="Pessoa Xis Ficticia", ra="7733000006",
                        email="pessoa.xis@turma.example.org")
    y = register_person(client, ADMIN_TOKEN, name="Pessoa Ipsilon Ficticia", ra="7733000007",
                        email="pessoa.ipsilon@turma.example.org")
    member = _team_member(client, bot_api, offering["code"], name="Pessoa Da Equipe Conflito",
                          ra="7733000005", email="equipe.conflito@turma.example.org")
    registered = [full, ra_only, email_only, reusable, x, y, member]
    rows = [
        _row(31),                                                                        # nova
        {"name": "Nome Qualquer Um", "ra": "7733000001",
         "email": "outro.email.um@turma.example.org"},                                  # RA com outro e-mail
        {"name": "Nome Qualquer Dois", "ra": "7733100002",
         "email": "completa.ficticia@turma.example.org"},                               # e-mail de outra pessoa
        {"name": "Pessoa Da Equipe Conflito", "ra": "7733000005",
         "email": "equipe.conflito@turma.example.org"},                                 # equipe
        {"name": "Pessoa So Com Ra Ficticia", "ra": "7733000002",
         "email": "com.ra.sem.email@turma.example.org"},                                # RA de pessoa sem e-mail
        {"name": "Pessoa Reaproveitavel Ficticia", "ra": "7733000004",
         "email": "REAPROVEITAVEL.ficticia@turma.example.org"},                         # reaproveitada
        {"name": "Nome Qualquer Tres", "ra": "7733100006",
         "email": "so.email.ficticio@turma.example.org"},                               # e-mail de pessoa sem RA
        {"name": "Nome Qualquer Quatro", "ra": "7733000006",
         "email": "pessoa.ipsilon@turma.example.org"},                                  # RA de uma, e-mail de outra
        _row(32),                                                                        # nova
    ]
    before = _snapshot(sql)
    clock.advance(5)
    with capture_logs() as cap:
        resp = _post(client, offering["code"], {"rows": rows})
    assert_error(resp, 409, "list_conflict", fields=[
        ("rows[1]", "duplicate"), ("rows[2]", "duplicate"), ("rows[3]", "invalid_value"),
        ("rows[4]", "duplicate"), ("rows[6]", "duplicate"), ("rows[7]", "duplicate")])
    assert_no_echo(resp, *_sentinels({"rows": rows}), *(p["pseudo_id"] for p in registered))
    pairs = [(p["pseudo_id"], p["alias"]) for p in registered]
    assert _pseudonym_hits(_response_text(resp), pairs) == []
    assert _pseudonym_hits(_logs_text(cap), pairs) == []
    _assert_no_echo_in_logs(cap, _sentinels({"rows": rows}))
    assert _snapshot(sql) == before

    t1 = clock.now()
    body = _imported(_post(client, offering["code"], {"rows": [rows[0], rows[5], rows[8]]}))
    assert body == _expected(offering, 3, 2, 1, 3, 0)
    pid_by_ra = {p[4]: p[0] for p in _people(sql)}
    assert pid_by_ra["7733000004"] == reusable["pseudo_id"]
    assert sorted(e for e in _enrollments(sql, offering["offering_id"]) if e[2] == "student") == sorted(
        (pid_by_ra[ra], ra, "student", "pending", t1, None, None, None)
        for ra in ("7700000031", "7733000004", "7700000032"))


def test_format_errors_come_alone_before_the_conflicts(client: Any, clock: Any,
                                                       sql: psycopg.Connection) -> None:
    """Ordem dos erros (os erros de formato vêm antes e sozinhos, os conflitos só aparecem numa lista
    sem erro de formato, e cada resposta aponta todas as linhas do seu tipo): um
    arquivo misto → só o 400, com todas as linhas de formato (inclusive a repetição de uma linha que
    também conflita com o cadastro) e nenhum conflito; sem as linhas de formato, o 409 com todos os
    conflitos (índices da lista nova); sem os conflitos, 200. Os dois pedidos recusados não gravam
    nada."""
    register_person(client, ADMIN_TOKEN, name="Pessoa Cadastrada Formato", ra="7744000001",
                    email="cadastrada.formato@turma.example.org")
    offering = create_offering(client, ADMIN_TOKEN, code="lista-formato-antes")
    rows = [
        _row(41),                                                                         # 0 nova
        {"name": "Nome Conflito Um", "ra": "7744000001",
         "email": "conflito.um@turma.example.org"},                                      # 1 conflito
        _row(42, name=""),                                                                # 2 too_short
        _row(43),                                                                         # 3 nova
        _row(44, ra="7700000041"),                                                        # 4 RA repetido (0)
        {"name": "Nome Conflito Dois", "ra": "7744100005",
         "email": "Cadastrada.Formato@turma.example.org"},                               # 5 conflito
        {"name": "Nome Conflito Tres", "ra": "7744000001",
         "email": "conflito.tres@turma.example.org"},                                    # 6 RA repetido (1)
    ]
    before = _snapshot(sql)
    resp = _post(client, offering["code"], {"rows": rows})
    assert_error(resp, 400, "invalid_request", fields=[
        ("rows[2]", "too_short"), ("rows[4]", "duplicate"), ("rows[6]", "duplicate")])
    assert_no_echo(resp, *_sentinels({"rows": rows}))
    assert _snapshot(sql) == before

    without_format = [rows[i] for i in (0, 1, 3, 5)]
    resp = _post(client, offering["code"], {"rows": without_format})
    assert_error(resp, 409, "list_conflict", fields=[("rows[1]", "duplicate"), ("rows[3]", "duplicate")])
    assert_no_echo(resp, *_sentinels({"rows": without_format}))
    assert _snapshot(sql) == before

    t1 = clock.now()
    body = _imported(_post(client, offering["code"], {"rows": [rows[0], rows[3]]}))
    assert body == _expected(offering, 2, 2, 0, 2, 0)
    assert sorted(e[1:] for e in _enrollments(sql, offering["offering_id"])) == [
        ("7700000041", "student", "pending", t1, None, None, None),
        ("7700000043", "student", "pending", t1, None, None, None)]


# ---------------------------------------------------------------------------------------------
# Validação por item
# ---------------------------------------------------------------------------------------------

INVALID_ITEMS = [
    # 1. item que não é objeto
    pytest.param(["ItemTextoSentinela"], [("rows[0]", "invalid_format")], id="item-texto"),
    pytest.param([12345678], [("rows[0]", "invalid_format")], id="item-inteiro"),
    pytest.param([None], [("rows[0]", "invalid_format")], id="item-null"),
    pytest.param([True], [("rows[0]", "invalid_format")], id="item-booleano"),
    pytest.param([["Nome Em Lista Sentinela", "7700000001", "lista.sentinela@turma.example.org"]],
                 [("rows[0]", "invalid_format")], id="item-lista"),
    pytest.param([_row(1), "ItemTextoSentinela", _row(3)], [("rows[1]", "invalid_format")],
                 id="item-texto-no-meio"),
    # 2. name (regras da API base: tipo → missing → tamanho → formato)
    pytest.param([_row(1, name=_DROP)], [("rows[0]", "missing")], id="name-ausente"),
    pytest.param([_row(1, name=None)], [("rows[0]", "missing")], id="name-null"),
    pytest.param([_row(1, name=12345678)], [("rows[0]", "invalid_format")], id="name-inteiro"),
    pytest.param([_row(1, name=["Nome Sentinela Em Lista"])], [("rows[0]", "invalid_format")], id="name-lista"),
    pytest.param([_row(1, name="")], [("rows[0]", "too_short")], id="name-vazio"),
    pytest.param([_row(1, name=" \t\n  ")], [("rows[0]", "too_short")], id="name-so-espacos"),
    pytest.param([_row(1, name=NAME_201)], [("rows[0]", "too_long")], id="name-201"),
    pytest.param([_row(1, name=NAME_RAW_TO_201)], [("rows[0]", "too_long")], id="name-201-depois-de-normalizar"),
    pytest.param([_row(1, name="NomeSentinela\x00NulNoMeio")], [("rows[0]", "invalid_format")], id="name-nul"),
    pytest.param([_row(1, name="NomeSentinela\ud800SubstitutoIsolado")], [("rows[0]", "invalid_format")],
                 id="name-substituto-isolado"),
    # 2. ra (obrigatório; sem aparar: com espaço é formato inválido)
    pytest.param([_row(1, ra=_DROP)], [("rows[0]", "missing")], id="ra-ausente"),
    pytest.param([_row(1, ra=None)], [("rows[0]", "missing")], id="ra-null"),
    pytest.param([_row(1, ra=7700000001)], [("rows[0]", "invalid_format")], id="ra-inteiro"),
    pytest.param([_row(1, ra="")], [("rows[0]", "too_short")], id="ra-vazio"),
    pytest.param([_row(1, ra="   ")], [("rows[0]", "too_short")], id="ra-so-espacos"),
    pytest.param([_row(1, ra="77000000011")], [("rows[0]", "too_long")], id="ra-11-digitos"),
    pytest.param([_row(1, ra="abcdefghijk")], [("rows[0]", "too_long")], id="ra-11-letras-tamanho-antes"),
    pytest.param([_row(1, ra="77ab000001")], [("rows[0]", "invalid_format")], id="ra-letras"),
    pytest.param([_row(1, ra=" 77000001 ")], [("rows[0]", "invalid_format")], id="ra-espacos-nas-pontas"),
    pytest.param([_row(1, ra="7700 00001")], [("rows[0]", "invalid_format")], id="ra-espaco-no-meio"),
    # 2. email (obrigatório; regras da API base)
    pytest.param([_row(1, email=_DROP)], [("rows[0]", "missing")], id="email-ausente"),
    pytest.param([_row(1, email=None)], [("rows[0]", "missing")], id="email-null"),
    pytest.param([_row(1, email=12345678)], [("rows[0]", "invalid_format")], id="email-inteiro"),
    pytest.param([_row(1, email="")], [("rows[0]", "too_short")], id="email-vazio"),
    pytest.param([_row(1, email="   ")], [("rows[0]", "too_short")], id="email-so-espacos"),
    pytest.param([_row(1, email=EMAIL_255)], [("rows[0]", "too_long")], id="email-255"),
    pytest.param([_row(1, email="email-sem-arroba-sentinela")], [("rows[0]", "invalid_format")],
                 id="email-sem-arroba"),
    pytest.param([_row(1, email="dois@@arrobas-sentinela.example.com")], [("rows[0]", "invalid_format")],
                 id="email-duas-arrobas"),
    pytest.param([_row(1, email="espaco sentinela@turma.example.org")], [("rows[0]", "invalid_format")],
                 id="email-espaco-no-meio"),
    pytest.param([_row(1, email="Fulano Exibido <fulano.exibido@turma.example.org>")],
                 [("rows[0]", "invalid_format")], id="email-nome-de-exibicao"),
    pytest.param([_row(1, email="pessoa.sentinela@turma.test")], [("rows[0]", "invalid_format")],
                 id="email-dominio-de-uso-especial"),
    # precedência dentro do item: name → ra → email → chave desconhecida → repetição
    pytest.param([_row(1, name="", ra="abcsentinela", email="sem-arroba-sentinela")], [("rows[0]", "too_short")],
                 id="precedencia-name-antes-de-ra-e-email"),
    pytest.param([_row(1, ra="", email="sem-arroba-sentinela")], [("rows[0]", "too_short")],
                 id="precedencia-ra-antes-de-email"),
    pytest.param([_row(1, name=12345678, ra=None, email=None)], [("rows[0]", "invalid_format")],
                 id="precedencia-tipo-do-name"),
    pytest.param([_row(1, name=_DROP, campo_extra_sentinela="valor-extra-sentinela")], [("rows[0]", "missing")],
                 id="precedencia-campo-antes-de-chave-desconhecida"),
    pytest.param([_row(1, campo_extra_sentinela="valor-extra-sentinela")], [("rows[0]", "unknown_field")],
                 id="chave-desconhecida"),
    pytest.param([_row(1, extra_um_sentinela=1, extra_dois_sentinela="valor-dois-sentinela")],
                 [("rows[0]", "unknown_field")], id="duas-chaves-desconhecidas-um-motivo"),
    pytest.param([_row(1), _row(2, ra="7700000001", campo_extra_sentinela=1)], [("rows[1]", "unknown_field")],
                 id="precedencia-chave-desconhecida-antes-de-repeticao"),
    # 4. repetição dentro da lista
    pytest.param([_row(1), _row(2, ra="7700000001")], [("rows[1]", "duplicate")], id="ra-repetido"),
    pytest.param([_row(1), _row(2, email="  ESTUDANTE.0001@Turma.Example.ORG ")], [("rows[1]", "duplicate")],
                 id="email-repetido-caixa-e-espacos"),
    pytest.param([_row(1), _row(1)], [("rows[1]", "duplicate")], id="linha-repetida-um-motivo"),
    pytest.param([_row(1), _row(2, ra="7700000001"), _row(3, ra="7700000001")],
                 [("rows[1]", "duplicate"), ("rows[2]", "duplicate")], id="ra-tres-vezes"),
    pytest.param([_row(1, name=_DROP, ra="123"), _row(2, ra="123")],
                 [("rows[0]", "missing"), ("rows[1]", "duplicate")], id="exemplo-nome-ausente"),
    pytest.param([_row(1, name="", ra="123"), _row(2, ra="123")],
                 [("rows[0]", "too_short"), ("rows[1]", "duplicate")], id="nome-vazio"),
    pytest.param([_row(1, email="sem-arroba-sentinela"), _row(2, ra="7700000001")],
                 [("rows[0]", "invalid_format"), ("rows[1]", "duplicate")], id="ra-valido-de-item-invalido"),
    pytest.param([_row(1, ra=None), _row(2, email="estudante.0001@turma.example.org")],
                 [("rows[0]", "missing"), ("rows[1]", "duplicate")], id="email-valido-de-item-invalido"),
    pytest.param([_row(1, campo_extra_sentinela=1), _row(2, ra="7700000001")],
                 [("rows[0]", "unknown_field"), ("rows[1]", "duplicate")],
                 id="ra-de-item-com-chave-desconhecida"),
    pytest.param([_row(1), _row(2, ra="7700000001"), _row(3, email="estudante.0002@turma.example.org")],
                 [("rows[1]", "duplicate"), ("rows[2]", "duplicate")], id="email-de-item-repetido"),
    pytest.param([_row(1, ra=" 77000001 "), _row(2, ra="77000001")], [("rows[0]", "invalid_format")],
                 id="ra-invalido-nao-conta-para-repeticao"),
    pytest.param([_row(1, email="Nome <sentinela.exibido@turma.example.org>"),
                  _row(2, email="sentinela.exibido@turma.example.org")],
                 [("rows[0]", "invalid_format")], id="email-invalido-nao-conta-para-repeticao"),
    pytest.param(["ItemTextoSentinela", _row(2)], [("rows[0]", "invalid_format")],
                 id="item-nao-objeto-nao-conta-para-repeticao"),
    # vários itens, um motivo por item, em ordem de índice
    pytest.param([_row(1, name=_DROP), _row(2), _row(3, ra="77ab0003"), _row(4, email="sem-arroba-sentinela"),
                  _row(5, ra="7700000002"), _row(6, campo_extra_sentinela=1)],
                 [("rows[0]", "missing"), ("rows[2]", "invalid_format"), ("rows[3]", "invalid_format"),
                  ("rows[4]", "duplicate"), ("rows[5]", "unknown_field")], id="varios-itens-em-ordem"),
]


@pytest.mark.parametrize(("rows", "fields"), INVALID_ITEMS)
def test_rows_items_get_one_reason_each_without_echo(client: Any, sql: psycopg.Connection, rows: list,
                                                     fields: list[tuple[str, str]]) -> None:
    """Validação por item; linha do CSV (RA com espaço é formato
    inválido; e-mails comparados sem diferenciar maiúsculas) e entrada estrita; erros
    por item em `rows[i]`, um motivo só; precedência: não objeto → `name`,
    `ra`, `email` com as regras da API base → chave desconhecida → repetição (a repetição compara com
    todos os itens anteriores cujo campo passou nas regras da API base, qualquer que seja a validade dos
    outros campos): 400 com `fields` exatos, sem eco na resposta nem nos logs, e nada gravado."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-validacao-itens")
    before = _snapshot(sql)
    body = {"rows": rows}
    with capture_logs() as cap:
        resp = _post_raw(client, offering["code"], body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    sentinels = _sentinels(body)
    assert_no_echo(resp, *sentinels)
    _assert_no_echo_in_logs(cap, sentinels)
    assert _snapshot(sql) == before


# ---------------------------------------------------------------------------------------------
# Validação da lista e do corpo (arquivo grande)
# ---------------------------------------------------------------------------------------------

INVALID_BODIES = [
    pytest.param({}, [("rows", "missing")], id="rows-ausente"),
    pytest.param({"rows": None}, [("rows", "missing")], id="rows-null"),
    pytest.param({"rows": "lista-texto-sentinela"}, [("rows", "invalid_format")], id="rows-texto"),
    pytest.param({"rows": 12345678}, [("rows", "invalid_format")], id="rows-inteiro"),
    pytest.param({"rows": _row(1)}, [("rows", "invalid_format")], id="rows-objeto"),
    pytest.param({"rows": []}, [("rows", "too_few")], id="rows-vazio"),
    pytest.param({"rows": [_row(n) for n in range(1001)]}, [("rows", "too_many")], id="rows-1001-validas"),
    pytest.param({"rows": ["ItemTextoSentinela"] * 1001}, [("rows", "too_many")],
                 id="rows-1001-itens-nao-validados"),
    pytest.param({"rows": [{}] * 1000}, [(f"rows[{i}]", "missing") for i in range(1000)],
                 id="rows-1000-dentro-do-teto"),
    pytest.param({"rows": [_row(1)], "path": "/home/coordenacao/lista-da-turma.csv"}, [("body", "unknown_field")],
                 id="corpo-com-path"),
    pytest.param({"rows": [_row(1)], "file": "lista-da-turma-sentinela.csv"}, [("body", "unknown_field")],
                 id="corpo-com-file"),
    pytest.param({"path": "/home/coordenacao/lista-da-turma.csv"}, [("rows", "missing"), ("body", "unknown_field")],
                 id="so-path"),
    pytest.param({"rows": [], "campo_extra_sentinela": 1}, [("rows", "too_few"), ("body", "unknown_field")],
                 id="campo-desconhecido-no-fim"),
    pytest.param({"rows": [_row(1, name="")], "path": "/home/coordenacao/lista-da-turma.csv"},
                 [("rows[0]", "too_short"), ("body", "unknown_field")], id="item-e-campo-desconhecido-no-fim"),
    pytest.param({"rows": ["ItemTextoSentinela"] * 1001, "file": "lista-da-turma-sentinela.csv"},
                 [("rows", "too_many"), ("body", "unknown_field")], id="too-many-e-campo-desconhecido"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_BODIES)
def test_rows_list_and_body_are_validated_without_echo(client: Any, sql: psycopg.Connection, body: dict,
                                                       fields: list[tuple[str, str]]) -> None:
    """Lista e corpo (a API não lê caminhos de arquivo: `path` ou `file` no corpo → `[{body,
    unknown_field}]`), o teto de 1.000 linhas por pedido; arquivo
    grande e entrada estrita; validação (`rows` obrigatório, lista de 1 a
    1.000: `missing`, `invalid_format`, `too_few`, `too_many`; fora do limite, os itens não são
    validados; 1.000 itens passam do teto e cada um é validado; campo desconhecido do corpo no fim):
    400 com `fields` exatos, sem eco na resposta nem nos logs, e nada gravado (nem com 1.001 linhas
    válidas)."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-validacao-corpo")
    before = _snapshot(sql)
    with capture_logs() as cap:
        resp = _post_raw(client, offering["code"], body)
    assert_error(resp, 400, "invalid_request", fields=fields)
    sentinels = _sentinels(body)
    assert_no_echo(resp, *sentinels)
    _assert_no_echo_in_logs(cap, sentinels)
    assert _snapshot(sql) == before


RAW_BODIES = [
    pytest.param(None, [("body", "missing")], id="sem-corpo"),
    pytest.param(b"isto nao e json sentinela", [("body", "invalid_format")], id="nao-json"),
    pytest.param(b'[{"name": "Nome Sentinela Em Lista", "ra": "7700000001"}]', [("body", "invalid_format")],
                 id="lista-json"),
    pytest.param(b'"texto-json-sentinela"', [("body", "invalid_format")], id="texto-json"),
]


@pytest.mark.parametrize(("raw", "fields"), RAW_BODIES)
def test_missing_or_non_object_body_is_rejected(client: Any, sql: psycopg.Connection, raw: bytes | None,
                                                fields: list[tuple[str, str]]) -> None:
    """Entrada estrita; as regras do corpo da API base (corpo ausente → `[{body, missing}]`;
    corpo que não é JSON ou não é objeto → `[{body, invalid_format}]`), aplicados a `students.import`:
    sem eco e nada gravado."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-corpo-cru")
    before = _snapshot(sql)
    path = _path(offering["code"])
    if raw is None:
        resp = client.post(path, headers=ADMIN)
    else:
        resp = client.post(path, content=raw, headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "isto nao e json sentinela", "Nome Sentinela Em Lista", "7700000001",
                   "texto-json-sentinela")
    assert _snapshot(sql) == before


# ---------------------------------------------------------------------------------------------
# Referência, autenticação, permissão e ordem das verificações
# ---------------------------------------------------------------------------------------------

INVALID_REFS = [
    pytest.param("ab", id="dois-caracteres"),
    pytest.param("MC404-2031S1", id="maiusculas"),
    pytest.param("-mc404-2031", id="comeca-com-hifen"),
    pytest.param("mc404_2031s1", id="sublinhado"),
    pytest.param("mc404 2031s1", id="espaco"),
    pytest.param("matemática-2031", id="acento"),
    pytest.param("r" * 41, id="41-caracteres"),
    pytest.param("UUID-EXISTENTE-EM-MAIUSCULAS", id="uuid-existente-maiusculo"),
]


@pytest.mark.parametrize("ref", INVALID_REFS)
def test_malformed_ref_is_rejected_without_echo(client: Any, sql: psycopg.Connection, ref: str) -> None:
    """O `ref` (que não é UUID v4 canônico minúsculo nem código → `[{ref,
    invalid_format}]`): com um corpo válido, 400 com `fields` exatos, sem eco do `ref` nem das
    linhas, e nada gravado."""
    existing = create_offering(client, ADMIN_TOKEN, code="lista-ref-valida")
    if ref == "UUID-EXISTENTE-EM-MAIUSCULAS":
        ref = existing["offering_id"].upper()
    before = _snapshot(sql)
    body = {"rows": [_row(1)]}
    resp = _post(client, ref, body)
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_no_echo(resp, *_sentinels(body), *([ref] if len(ref) >= 8 else []))
    assert _snapshot(sql) == before


def test_order_of_checks_authentication_validation_permission_and_not_found(
        client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Ordem das verificações (401 → 404/405 → 400 → 403 → regras; nas regras, o 404 do oferecimento) e
    permissão (`people:write`, sem permissão nova), com as regras da API base: sem token ou com token
    desconhecido → 401; parâmetro de consulta desconhecido → `[{query, unknown_field}]`; o 400 vem
    antes do 403 (mesmo num oferecimento inexistente); sem `people:write` (com todas as permissões de
    oferecimentos) → 403 `people:write`, também num oferecimento inexistente (o 403 antes do 404);
    oferecimento inexistente (código ou UUID v4) → 404; nada é gravado pelos pedidos recusados. Um
    token só com `people:write` importa, e os eventos registram o `token_id` dele como ator."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-ordem-verificacoes")
    code = offering["code"]
    other, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "offerings:write", "people:read"],
                            label="sem escrita de pessoas")
    writer, writer_body = create_token(client, ADMIN_TOKEN, ["people:write"], label="escrita de pessoas")
    valid = {"rows": [_row(1)]}
    missing_uuid = str(uuid.uuid4())
    before = _snapshot(sql)

    # 401 (comportamento da API base, que o middleware já tem; junto dos casos que falham)
    assert_error(client.post(_path(code), json=valid), 401, "unauthenticated")
    assert_error(_post(client, code, valid, headers=auth("cq_tokendesconhecidosentinela_" + "z" * 20)),
                 401, "unauthenticated")

    # 400 antes do 403 e das regras
    resp = _post(client, code, valid, params={"parametro_sentinela": "valor_sentinela"})
    assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert_no_echo(resp, "parametro_sentinela", "valor_sentinela")
    assert_error(_post(client, code, {"rows": []}, headers=auth(other)), 400, "invalid_request",
                 fields=[("rows", "too_few")])
    resp = _post(client, "codigo-inexistente", {"rows": [_row(1, ra="77ab000001")]}, headers=auth(other))
    assert_error(resp, 400, "invalid_request", fields=[("rows[0]", "invalid_format")])
    assert_no_echo(resp, "77ab000001", "Estudante Ficticio 0001")

    # 403 antes do 404
    for ref in (code, offering["offering_id"], "codigo-inexistente", missing_uuid):
        with _context(ref):
            resp = _post(client, ref, valid, headers=auth(other))
            assert_error(resp, 403, "forbidden", permission="people:write")
            assert_no_echo(resp, *_sentinels(valid))

    # 404 do oferecimento
    for token, ref in ((ADMIN_TOKEN, "codigo-inexistente"), (ADMIN_TOKEN, missing_uuid),
                       (writer, "codigo-inexistente")):
        with _context(ref):
            resp = _post(client, ref, valid, headers=auth(token))
            assert_error(resp, 404, "not_found")
            assert_no_echo(resp, *_sentinels(valid))
    assert _snapshot(sql) == before

    cursor = _last_seq(sql)
    clock.advance(12)
    t1 = clock.now()
    body = _imported(_post(client, code, valid, headers=auth(writer)))
    assert body == _expected(offering, 1, 1, 0, 1, 0)
    person = _people(sql)[0]
    assert _events_after(client, cursor) == [
        _registered_event(person[0], person[1], t1, actor=writer_body["token_id"]),
        _imported_event(body, t1, actor=writer_body["token_id"])]


# ---------------------------------------------------------------------------------------------
# Estado do oferecimento
# ---------------------------------------------------------------------------------------------


def test_import_is_accepted_only_in_draft_or_active(client: Any, clock: Any, sql: psycopg.Connection,
                                                    bot_api: Any) -> None:
    """Estado do oferecimento (rascunho ou ativo); nas regras: 404 → `invalid_state` →
    `list_conflict`; importação: num oferecimento encerrado ou arquivado (pelo
    código e pelo identificador) → 409 `invalid_state` com o estado atual, mesmo com uma linha que
    conflitaria com o cadastro (o estado vem antes do conflito); o 400 e o 403 vêm antes do estado; um
    oferecimento inexistente com a mesma linha → 404; nada é gravado. No rascunho e no ativo, a
    importação é aceita, com os eventos exatos (a pessoa da 1ª importação é reaproveitada na 2ª)."""
    draft = _offering_in_state(client, bot_api, "lista-em-rascunho", "draft")
    active = _offering_in_state(client, bot_api, "lista-ativa", "active")
    closed = _offering_in_state(client, bot_api, "lista-encerrada", "closed")
    archived = _offering_in_state(client, bot_api, "lista-arquivada", "archived")
    register_person(client, ADMIN_TOKEN, name="Pessoa Cadastrada Estado", ra="7755000001",
                    email="cadastrada.estado@turma.example.org")
    other, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "offerings:write"], label="sem pessoas")
    conflicting = {"name": "Nome Conflito Estado", "ra": "7755000001", "email": "conflito.estado@turma.example.org"}
    before = _snapshot(sql)

    for offering, state in ((closed, "closed"), (archived, "archived")):
        for ref in (offering["code"], offering["offering_id"]):
            with _context(f"{state} {ref}"):
                assert_error(_post(client, ref, {"rows": [_row(51)]}), 409, "invalid_state", state=state)
                assert_error(_post(client, ref, {"rows": [_row(51), conflicting]}), 409, "invalid_state",
                             state=state)
                assert_error(_post(client, ref, {"rows": []}), 400, "invalid_request", fields=[("rows", "too_few")])
                assert_error(_post(client, ref, {"rows": [_row(51)]}, headers=auth(other)), 403, "forbidden",
                             permission="people:write")
    assert_error(_post(client, "lista-inexistente", {"rows": [_row(51), conflicting]}), 404, "not_found")
    assert _snapshot(sql) == before

    cursor = _last_seq(sql)
    clock.advance(3)
    t1 = clock.now()
    first = _imported(_post(client, draft["code"], {"rows": [_row(51), _row(52)]}))
    assert first == _expected(draft, 2, 2, 0, 2, 0)
    new = _people(sql)[1:]
    assert sorted(p[4] for p in new) == ["7700000051", "7700000052"]
    second = _imported(_post(client, active["offering_id"], {"rows": [_row(51), _row(53)]}))
    assert second == _expected(active, 2, 1, 1, 2, 0)
    third = _people(sql)[3]
    assert third[4] == "7700000053"
    assert _events_after(client, cursor) == (
        [_registered_event(p[0], p[1], t1) for p in new] + [_imported_event(first, t1)]
        + [_registered_event(third[0], third[1], t1), _imported_event(second, t1)])
    pid_by_ra = {p[4]: p[0] for p in _people(sql)}
    assert sorted(_enrollments(sql, draft["offering_id"])) == sorted(
        (pid_by_ra[ra], ra, "student", "pending", t1, None, None, None) for ra in ("7700000051", "7700000052"))
    assert sorted(_enrollments(sql, active["offering_id"])) == sorted(
        (pid_by_ra[ra], ra, "student", "pending", t1, None, None, None) for ra in ("7700000051", "7700000053"))
    assert _enrollments(sql, closed["offering_id"]) == []
    assert _enrollments(sql, archived["offering_id"]) == []


# ---------------------------------------------------------------------------------------------
# Embaralhamento
# ---------------------------------------------------------------------------------------------


def _odd_then_even(seq: list) -> None:
    """Permutação conhecida, no lugar: os índices ímpares e depois os pares (para 7: 1, 3, 5, 0, 2, 4,
    6), diferente da ordem do pedido e da inversa."""
    order = list(range(1, len(seq), 2)) + list(range(0, len(seq), 2))
    seq[:] = [seq[i] for i in order]


def test_known_shuffle_orders_people_enrollments_and_events(client: Any, clock: Any, sql: psycopg.Connection,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Embaralhamento (a importação embaralha a lista das pessoas novas e a das matrículas
    novas com `students.shuffle`; insere pessoas, identidades e um `person.registered` por pessoa e
    as matrículas na ordem embaralhada; `students.shuffle`
    substituível nos testes): com `students.shuffle` trocado por uma permutação conhecida, 7 linhas
    novas → a ordem de `people.id`, a de `enrollments.id` e a de `seq` dos `person.registered` seguem
    a permutação (1, 3, 5, 0, 2, 4, 6), não a do pedido; a substituta é chamada exatamente duas vezes,
    uma com a lista das pessoas novas e outra, à parte, com a das matrículas novas, cada uma com as 7
    linhas e montada na ordem do pedido (a importação trata cada item na ordem do
    pedido)."""
    calls: list[int] = []

    def known(seq: list) -> None:
        calls.append(len(seq))
        _odd_then_even(seq)

    monkeypatch.setattr(students_core, "shuffle", known)
    offering = create_offering(client, ADMIN_TOKEN, code="lista-permutacao")
    rows = [_row(60 + i) for i in range(7)]
    request_ras = [row["ra"] for row in rows]
    expected_ras = [request_ras[i] for i in (1, 3, 5, 0, 2, 4, 6)]
    assert expected_ras != request_ras
    cursor = _last_seq(sql)
    t1 = clock.now()
    body = _imported(_post(client, offering["code"], {"rows": rows}))
    assert body == _expected(offering, 7, 7, 0, 7, 0)
    assert calls == [7, 7]

    people = _people(sql)
    assert [p[4] for p in people] == expected_ras
    assert [e[1] for e in _enrollments(sql, offering["offering_id"])] == expected_ras
    events = _events_after(client, cursor)
    assert events == [_registered_event(p[0], p[1], t1) for p in people] + [_imported_event(body, t1)]
    ra_by_pid = {p[0]: p[4] for p in people}
    assert [ra_by_pid[e["data"]["pseudo_id"]] for e in events[:-1]] == expected_ras


def test_default_shuffle_is_system_random_and_50_rows_leave_the_request_order(
        client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Embaralhamento criptográfico (`secrets.SystemRandom`) por padrão
    (o `students.shuffle` padrão, real no esqueleto, afirmado junto desta importação): o
    `students.shuffle` é o método `shuffle` de uma instância de `secrets.SystemRandom`; sem
    substituição, 50 linhas novas → a ordem de `people.id`, a de `enrollments.id` e a de `seq` dos
    `person.registered` não são a do pedido (cada uma é uma permutação das 50 linhas; a chance de uma
    delas sair igual por acaso é 1/50!); os `person.registered` saem na ordem de `people.id`, com os
    pseudônimos."""
    assert inspect.ismethod(students_core.shuffle)
    assert isinstance(students_core.shuffle.__self__, secrets.SystemRandom)
    assert students_core.shuffle.__func__ is secrets.SystemRandom.shuffle

    offering = create_offering(client, ADMIN_TOKEN, code="lista-cinquenta")
    rows = [_row(100 + i) for i in range(50)]
    request_ras = [row["ra"] for row in rows]
    cursor = _last_seq(sql)
    t1 = clock.now()
    body = _imported(_post(client, offering["code"], {"rows": rows}))
    assert body == _expected(offering, 50, 50, 0, 50, 0)

    people = _people(sql)
    people_ras = [p[4] for p in people]
    enrollment_ras = [e[1] for e in _enrollments(sql, offering["offering_id"])]
    ra_by_pid = {p[0]: p[4] for p in people}
    events = _events_after(client, cursor)
    assert events == [_registered_event(p[0], p[1], t1) for p in people] + [_imported_event(body, t1)]
    event_ras = [ra_by_pid[e["data"]["pseudo_id"]] for e in events[:-1]]
    for name, order in (("people.id", people_ras), ("enrollments.id", enrollment_ras), ("seq", event_ras)):
        with _context(name):
            assert sorted(order) == sorted(request_ras)
            assert order != request_ras


# ---------------------------------------------------------------------------------------------
# Desempenho e privacidade da resposta
# ---------------------------------------------------------------------------------------------


def test_200_rows_are_imported_within_10_seconds_without_linking_rows_to_pseudonyms(
        client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Desempenho (importar uma turma de 200 alunos leva até 10 segundos, e nenhuma resposta liga linha
    do arquivo a pseudônimo): 200 linhas novas → 200 em até 10 s de tempo
    real (medido em volta do pedido), com o corpo exato; 200 pessoas e 200 matrículas; 200
    `person.registered` e um `student.list_imported`; nenhuma identidade nem pseudônimo na resposta
    nem nos logs do pedido."""
    offering = create_offering(client, ADMIN_TOKEN, code="lista-duzentos")
    rows = [_row(200 + i) for i in range(200)]
    cursor = _last_seq(sql)
    t1 = clock.now()
    with capture_logs() as cap:
        started = time.monotonic()
        resp = _post(client, offering["code"], {"rows": rows})
        elapsed = time.monotonic() - started
    body = _imported(resp)
    assert body == _expected(offering, 200, 200, 0, 200, 0)
    assert elapsed <= 10.0, f"a importação de 200 linhas levou {elapsed:.2f} s"

    people = _people(sql)
    assert sorted(p[4] for p in people) == sorted(row["ra"] for row in rows)
    assert sorted(_enrollments(sql, offering["offering_id"])) == sorted(
        (p[0], p[4], "student", "pending", t1, None, None, None) for p in people)
    events = _events_after(client, cursor)
    assert events == [_registered_event(p[0], p[1], t1) for p in people] + [_imported_event(body, t1)]
    pairs = [(p[0], p[1]) for p in people]
    for name, text in {"resposta": _response_text(resp), "logs": _logs_text(cap)}.items():
        with _context(name):
            assert _pseudonym_hits(text, pairs) == []
            assert _identity_hits(text, rows) == []
