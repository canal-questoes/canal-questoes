"""Pessoas, identidades e revelação.

Núcleo sem HTTP. As funções que tocam o banco recebem a conexão do chamador e abrem **uma**
transação (ou um SAVEPOINT, se o chamador já tiver uma) com a mudança e o evento dela juntos
(atômicos): se o evento não confirma, a mudança também não vale.

- **Pseudônimo**: `pseudo_id` é um UUID v4 sorteado por `generate_pseudo_id` (atributo do
  módulo, que os testes substituem) e nunca derivado da identidade. O apelido é o primeiro prefixo
  do hex, com 4 a 32 caracteres, que tenha uma letra `a`–`f` e esteja livre.
- **Identidade**: fica só em `identities`; sai só pela revelação. A unicidade de RA
  e e-mail é conferida por consulta **antes** do `INSERT`, e a transação do cadastro termina em
  COMMIT mesmo no conflito, para que o log do banco não registre valores. A corrida
  entre dois cadastros iguais cai no `ON CONFLICT DO NOTHING` dentro de um SAVEPOINT.
- **Ordem do cadastro**: o cadastro toma o lock consultivo do log de eventos
  (`EVENTS_LOCK_KEY`) no início da transação, antes de alocar o `id` de `people`, para que a ordem
  de `people.id` seja a de confirmação e o cursor de `people.list` não perca ninguém.
- **Revelação**: a concedida devolve a identidade e grava `identity.revealed` com a
  justificativa redigida; a negada grava `identity.reveal_denied` sem a justificativa e sem ler
  `identities`. Os itens não encontrados só entram pela contagem.

As validações (`normalize_full_name`, `validate_ra`, `validate_email`, `normalize_reason`) levantam
`InvalidIdentity` com o motivo do contrato e nunca com o valor recebido. Nome e
justificativa com um caractere que o banco não guarda (U+0000 ou substituto UTF-16 isolado) são
`invalid_format`, depois das checagens de tamanho (a precedência do contrato da API).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import email_validator
import psycopg

from cq_server.events import EVENTS_LOCK_KEY, EventType, append_event

# Formatos de entrada.
ALIAS_PATTERN = re.compile(r"(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}")
PSEUDO_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
ALIAS_MIN_LENGTH = 4
ALIAS_MAX_LENGTH = 32
_HAS_LETTER = re.compile(r"[a-f]")

# Limites da identidade.
NAME_MAX_LENGTH = 200
RA_MAX_LENGTH = 10
RA_PATTERN = re.compile(r"[0-9]{1,10}")
EMAIL_MAX_LENGTH = 254
_WHITESPACE_RUN = re.compile(r"\s+")

# Caracteres que o banco não guarda: U+0000, que `text` e `jsonb` recusam, e os substitutos UTF-16
# (U+D800 a U+DFFF), que chegam isolados pelo escape JSON (`"\ud800"`) e não têm UTF-8. Um par
# válido já chega como um só caractere (`json.loads` junta os dois escapes).
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")

# Justificativa da revelação.
REASON_MAX_LENGTH = 500

# Sorteios de `pseudo_id` por cadastro antes de desistir (um UUID v4 sem letra nos prefixos, ou
# repetido, é raríssimo: o limite só evita um laço infinito com um gerador defeituoso).
MAX_PSEUDO_ID_DRAWS = 64


def generate_pseudo_id() -> uuid.UUID:
    """Sorteia um `pseudo_id` (UUID v4 de `os.urandom`, não semeável)."""
    return uuid.uuid4()


class InvalidIdentity(ValueError):
    """Valor de entrada fora das regras; `reason` é o motivo do contrato."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------------------------------
# Formatos e normalização
# --------------------------------------------------------------------------------------------------


def is_alias(value: object) -> bool:
    return isinstance(value, str) and ALIAS_PATTERN.fullmatch(value) is not None


def is_pseudo_id(value: object) -> bool:
    return isinstance(value, str) and PSEUDO_ID_PATTERN.fullmatch(value) is not None


def is_pseudonym(value: object) -> bool:
    """Apelido ou identificador canônico; qualquer outra forma não é."""
    return is_alias(value) or is_pseudo_id(value)


def normalize_full_name(value: str) -> str:
    """Tira os espaços nas pontas e troca cada sequência de espaços em branco por um espaço.

    Depois do tamanho, um caractere que o banco não guarda é `invalid_format`.
    """
    normalized = _WHITESPACE_RUN.sub(" ", value).strip()
    if not normalized:
        raise InvalidIdentity("too_short")
    if len(normalized) > NAME_MAX_LENGTH:
        raise InvalidIdentity("too_long")
    if _UNSTORABLE.search(normalized):
        raise InvalidIdentity("invalid_format")
    return normalized


def validate_ra(value: str) -> str:
    """Só dígitos, de 1 a 10, sem normalização (vazio ou só espaços é `too_short`)."""
    if not value.strip():
        raise InvalidIdentity("too_short")
    if len(value) > RA_MAX_LENGTH:
        raise InvalidIdentity("too_long")
    if RA_PATTERN.fullmatch(value) is None:
        raise InvalidIdentity("invalid_format")
    return value


@dataclass(frozen=True)
class Email:
    address: str      # como enviado, sem espaços nas pontas
    normalized: str   # `casefold()` do endereço inteiro (unicidade)


def validate_email(value: str) -> Email:
    """E-mail aparado, até 254 caracteres, validado sem DNS e sem nome de exibição."""
    address = value.strip()
    if not address:
        raise InvalidIdentity("too_short")
    if len(address) > EMAIL_MAX_LENGTH:
        raise InvalidIdentity("too_long")
    try:
        email_validator.validate_email(
            address,
            check_deliverability=False,
            allow_display_name=False,
            allow_quoted_local=False,
            allow_domain_literal=False,
        )
    except email_validator.EmailNotValidError:
        # A mensagem do validador pode trazer o endereço: só o motivo segue.
        raise InvalidIdentity("invalid_format") from None
    return Email(address=address, normalized=address.casefold())


def normalize_reason(value: str) -> str:
    """Justificativa da revelação sem espaços nas pontas, de 1 a 500 caracteres.

    Depois do tamanho, um caractere que o banco não guarda é `invalid_format`.
    """
    reason = value.strip()
    if not reason:
        raise InvalidIdentity("too_short")
    if len(reason) > REASON_MAX_LENGTH:
        raise InvalidIdentity("too_long")
    if _UNSTORABLE.search(reason):
        raise InvalidIdentity("invalid_format")
    return reason


def alias_candidates(pseudo_id: uuid.UUID) -> list[str]:
    """Prefixos do hex (sem hífens) com 4 a 32 caracteres que têm pelo menos uma letra."""
    hexed = pseudo_id.hex
    return [hexed[:size] for size in range(ALIAS_MIN_LENGTH, ALIAS_MAX_LENGTH + 1)
            if _HAS_LETTER.search(hexed[:size])]


# --------------------------------------------------------------------------------------------------
# Registros
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    pseudo_id: uuid.UUID
    alias: str
    created_at: datetime


@dataclass(frozen=True)
class PeoplePage:
    items: list[Person]
    next_cursor: int  # cursor interno do último item, ou `after` se não houver itens


@dataclass(frozen=True)
class Duplicate:
    """Cadastro recusado: os campos repetidos, na ordem do esquema (`ra`, `email`)."""

    fields: tuple[str, ...]


@dataclass(frozen=True)
class RevealedPerson:
    pseudo_id: uuid.UUID
    alias: str
    full_name: str
    ra: str | None
    email: str | None


@dataclass(frozen=True)
class Revelation:
    people: list[RevealedPerson]  # uma vez por pessoa, na ordem da primeira referência
    not_found: list[str]          # itens sem pessoa, sem repetição, na ordem do pedido


class _PseudonymTaken(Exception):
    """`pseudo_id` ou apelido tomado por um cadastro concorrente: sorteia de novo."""


class _IdentityTaken(Exception):
    """RA ou e-mail gravado por um cadastro concorrente depois da checagem."""


# --------------------------------------------------------------------------------------------------
# Cadastro
# --------------------------------------------------------------------------------------------------


def _duplicate_fields(conn: psycopg.Connection, ra: str | None, email: Email | None) -> tuple[str, ...]:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM identities WHERE ra = %s), "
        "EXISTS (SELECT 1 FROM identities WHERE email_normalized = %s)",
        (ra, email.normalized if email is not None else None),
    ).fetchone()
    assert row is not None
    ra_taken, email_taken = bool(row[0]), bool(row[1])
    return tuple(field for field, taken in (("ra", ra_taken), ("email", email_taken)) if taken)


def _pseudo_id_exists(conn: psycopg.Connection, pseudo_id: uuid.UUID) -> bool:
    return conn.execute("SELECT 1 FROM people WHERE pseudo_id = %s", (pseudo_id,)).fetchone() is not None


def _choose_alias(conn: psycopg.Connection, pseudo_id: uuid.UUID) -> str | None:
    candidates = alias_candidates(pseudo_id)
    if not candidates:
        return None
    taken = {row[0] for row in conn.execute(
        "SELECT alias FROM people WHERE alias = ANY(%s::text[])", (candidates,)).fetchall()}
    return next((candidate for candidate in candidates if candidate not in taken), None)


def _insert_person(conn: psycopg.Connection, *, pseudo_id: uuid.UUID, alias: str, full_name: str,
                   ra: str | None, email: Email | None, now: datetime) -> None:
    """Grava `people` e `identities` num SAVEPOINT; a corrida desfaz só o SAVEPOINT."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO people (pseudo_id, alias, created_at) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (pseudo_id, alias, now),
        ).fetchone()
        if row is None:
            raise _PseudonymTaken
        stored = conn.execute(
            "INSERT INTO identities (person_id, full_name, ra, email, email_normalized) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING person_id",
            (row[0], full_name, ra, email.address if email else None,
             email.normalized if email else None),
        ).fetchone()
        if stored is None:
            raise _IdentityTaken


def register_person(conn: psycopg.Connection, *, full_name: str, ra: str | None, email: str | None,
                    now: datetime, actor_token_id: str | None) -> Person | Duplicate:
    """Cadastra uma pessoa e grava `person.registered`, na mesma transação.

    `full_name`, `ra` e `email` passam de novo pelas regras de cadastro da identidade (`InvalidIdentity`
    antes de qualquer SQL). RA ou e-mail já cadastrados devolvem `Duplicate` **sem erro no banco**:
    a transação só leu e termina em COMMIT.
    """
    name = normalize_full_name(full_name)
    ra_value = validate_ra(ra) if ra is not None else None
    email_value = validate_email(email) if email is not None else None

    with conn.transaction():
        # O lock do log de eventos vem primeiro, e não só no INSERT de `person.registered` (onde o
        # gatilho `events_serialize_writers` o tomaria): o `id` de `people` sai da sequência no
        # INSERT, antes do evento. Sem isso, o cadastro A aloca `id = 1` e demora; o B aloca 2 e
        # confirma; o leitor encadeia `next_cursor` até 2; A confirma e nunca é lido (o mesmo
        # argumento vale para `seq` nos eventos). Seguro até o COMMIT, que fica visível antes da
        # liberação, o lock faz a ordem de `people.id` ser a de confirmação. De quebra, em READ
        # COMMITTED a checagem abaixo já vê todo cadastro confirmado antes; o SAVEPOINT com
        # `ON CONFLICT` fica para quem grava `identities` sem o lock (ex.: SQL cru).
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK_KEY,))
        duplicates = _duplicate_fields(conn, ra_value, email_value)
        if duplicates:
            return Duplicate(duplicates)
        for _ in range(MAX_PSEUDO_ID_DRAWS):
            # Nome global resolvido a cada sorteio: os testes substituem o gerador do módulo.
            pseudo_id = generate_pseudo_id()
            if _pseudo_id_exists(conn, pseudo_id):
                continue
            alias = _choose_alias(conn, pseudo_id)
            if alias is None:
                continue
            try:
                _insert_person(conn, pseudo_id=pseudo_id, alias=alias, full_name=name, ra=ra_value,
                               email=email_value, now=now)
            except _PseudonymTaken:
                continue
            except _IdentityTaken:
                duplicates = _duplicate_fields(conn, ra_value, email_value)
                if duplicates:
                    return Duplicate(duplicates)
                continue
            append_event(conn, EventType.PERSON_REGISTERED, {"pseudo_id": pseudo_id, "alias": alias},
                         occurred_at=now, actor_token_id=actor_token_id)
            return Person(pseudo_id=pseudo_id, alias=alias, created_at=now)
    raise RuntimeError("não foi possível sortear um pseudônimo livre")


# --------------------------------------------------------------------------------------------------
# Listagem e consulta
# --------------------------------------------------------------------------------------------------


def list_people(conn: psycopg.Connection, *, after: int, limit: int) -> PeoplePage:
    """Pessoas com cursor interno maior que `after`, em ordem de cadastro, no máximo `limit`."""
    rows = conn.execute(
        "SELECT id, pseudo_id, alias, created_at FROM people WHERE id > %s ORDER BY id LIMIT %s",
        (after, limit),
    ).fetchall()
    items = [Person(pseudo_id=pseudo_id, alias=alias, created_at=created_at)
             for _, pseudo_id, alias, created_at in rows]
    next_cursor = int(rows[-1][0]) if rows else after
    return PeoplePage(items=items, next_cursor=next_cursor)


def get_person(conn: psycopg.Connection, ref: str) -> Person | None:
    """A pessoa do pseudônimo `ref` (apelido ou identificador, igualdade exata), ou `None`."""
    if is_pseudo_id(ref):
        row = conn.execute("SELECT pseudo_id, alias, created_at FROM people WHERE pseudo_id = %s",
                           (uuid.UUID(ref),)).fetchone()
    elif is_alias(ref):
        row = conn.execute("SELECT pseudo_id, alias, created_at FROM people WHERE alias = %s",
                           (ref,)).fetchone()
    else:
        return None
    if row is None:
        return None
    return Person(pseudo_id=row[0], alias=row[1], created_at=row[2])


# --------------------------------------------------------------------------------------------------
# Revelação
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    person_id: int
    pseudo_id: uuid.UUID
    alias: str
    identity: tuple[str, str | None, str | None] | None  # (full_name, ra, email)


def _resolve(conn: psycopg.Connection, refs: Sequence[str], *, with_identity: bool,
             ) -> tuple[list[_Resolved], list[str]]:
    """Pessoas referenciadas (uma vez, na ordem da primeira referência) e itens sem pessoa."""
    aliases = sorted({ref for ref in refs if is_alias(ref)})
    pseudo_ids = sorted({uuid.UUID(ref) for ref in refs if is_pseudo_id(ref)})
    if with_identity:
        rows = conn.execute(
            "SELECT p.id, p.pseudo_id, p.alias, i.full_name, i.ra, i.email FROM people p "
            "JOIN identities i ON i.person_id = p.id "
            "WHERE p.alias = ANY(%s::text[]) OR p.pseudo_id = ANY(%s::uuid[])",
            (aliases, pseudo_ids),
        ).fetchall()
        found = [_Resolved(row[0], row[1], row[2], (row[3], row[4], row[5])) for row in rows]
    else:
        rows = conn.execute(
            "SELECT p.id, p.pseudo_id, p.alias FROM people p "
            "WHERE p.alias = ANY(%s::text[]) OR p.pseudo_id = ANY(%s::uuid[])",
            (aliases, pseudo_ids),
        ).fetchall()
        found = [_Resolved(row[0], row[1], row[2], None) for row in rows]

    by_alias = {person.alias: person for person in found}
    by_pseudo_id = {str(person.pseudo_id): person for person in found}
    people: list[_Resolved] = []
    seen: set[int] = set()
    not_found: list[str] = []
    missing: set[str] = set()
    for ref in refs:
        person = by_alias.get(ref) if is_alias(ref) else by_pseudo_id.get(ref)
        if person is None:
            if ref not in missing:
                missing.add(ref)
                not_found.append(ref)
        elif person.person_id not in seen:
            seen.add(person.person_id)
            people.append(person)
    return people, not_found


def _check_refs(refs: Sequence[str]) -> None:
    """Pseudônimos válidos, antes de qualquer SQL (a validação da entrada é da camada de cima)."""
    if not refs or any(not is_pseudonym(ref) for ref in refs):
        raise ValueError("lista de pseudônimos vazia ou fora do formato")


def reveal_identities(conn: psycopg.Connection, refs: Sequence[str], *, reason: str, client: str,
                      now: datetime, actor_token_id: str | None,
                      secrets: Iterable[str | None] = ()) -> Revelation:
    """Revela as identidades e grava `identity.revealed` (justificativa redigida), juntos.

    `refs` são pseudônimos válidos (com repetição); `reason` passa por `normalize_reason`.
    """
    _check_refs(refs)
    normalized_reason = normalize_reason(reason)
    with conn.transaction():
        people, not_found = _resolve(conn, refs, with_identity=True)
        append_event(
            conn, EventType.IDENTITY_REVEALED,
            {"client": client, "revealed": [person.pseudo_id for person in people],
             "not_found_count": len(not_found), "reason": normalized_reason},
            occurred_at=now, actor_token_id=actor_token_id, secrets=secrets,
        )
    revealed = []
    for person in people:
        assert person.identity is not None
        full_name, ra, email = person.identity
        revealed.append(RevealedPerson(pseudo_id=person.pseudo_id, alias=person.alias,
                                       full_name=full_name, ra=ra, email=email))
    return Revelation(people=revealed, not_found=not_found)


def record_denied_reveal(conn: psycopg.Connection, refs: Sequence[str], *, client: str,
                         now: datetime, actor_token_id: str | None) -> None:
    """Grava `identity.reveal_denied`: só os pseudônimos existentes e a contagem dos demais.

    Não recebe a justificativa e não lê `identities`.
    """
    _check_refs(refs)
    with conn.transaction():
        people, not_found = _resolve(conn, refs, with_identity=False)
        append_event(
            conn, EventType.IDENTITY_REVEAL_DENIED,
            {"client": client, "requested": [person.pseudo_id for person in people],
             "not_found_count": len(not_found)},
            occurred_at=now, actor_token_id=actor_token_id,
        )


# --------------------------------------------------------------------------------------------------
# O SQL sobre `identities` do expurgo (o escopo, a trava e o apagamento das identidades do
# oferecimento). Fica aqui, e não em `cq_server.purge`, porque só `events`, `people` e `students` podem
# citar `identities` em SQL (restrição (D) de `tests/contract/test_boundaries.py`).


def purge_scope_rows(conn: psycopg.Connection, offering_row_id: int) -> list[tuple[int, bool]]:
    """As pessoas com matrícula no oferecimento e linha em `identities`, em ordem de `person_id`,
    com `kept` verdadeiro quando há matrícula noutro oferecimento não expurgado."""
    rows = conn.execute(
        "SELECT e.person_id, "
        "EXISTS (SELECT 1 FROM enrollments e2 JOIN offerings o2 ON o2.id = e2.offering_id "
        "WHERE e2.person_id = e.person_id AND e2.offering_id <> %(o)s "
        "AND o2.purged_at IS NULL) AS kept "
        "FROM enrollments e JOIN identities i ON i.person_id = e.person_id "
        "WHERE e.offering_id = %(o)s "
        "ORDER BY e.person_id",
        {"o": offering_row_id},
    ).fetchall()
    return [(int(person_id), bool(kept)) for person_id, kept in rows]


def lock_identities(conn: psycopg.Connection, person_ids: Sequence[int]) -> None:
    """`FOR UPDATE` nas identidades de `person_ids`, em ordem de `person_id`."""
    ids = sorted(set(person_ids))
    if not ids:
        return
    # `identities` não é referenciada por FK: `FOR UPDATE` não barra nenhuma checagem de FK. A ordem fixa de
    # `person_id` evita impasse entre transações que travem conjuntos sobrepostos.
    conn.execute(
        "SELECT person_id FROM identities WHERE person_id = ANY(%s::bigint[]) "
        "ORDER BY person_id FOR UPDATE",
        (ids,),
    ).fetchall()


def delete_identities(conn: psycopg.Connection, person_ids: Sequence[int]) -> int:
    """Apaga as identidades de `person_ids` e devolve quantas linhas saíram."""
    ids = sorted(set(person_ids))
    if not ids:
        return 0
    cur = conn.execute("DELETE FROM identities WHERE person_id = ANY(%s::bigint[])", (ids,))
    return cur.rowcount
