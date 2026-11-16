"""Tokens de API.

Núcleo sem HTTP:
- **Segredo:** `cq_` + `secrets.token_urlsafe(32)` (43 caracteres, 256 bits). Só o SHA-256 em hex
  dos bytes UTF-8 do segredo completo é gravado; a autenticação busca pelo resumo.
- **Token inicial** (`CQ_ADMIN_TOKEN`): não fica no banco, tem todas as permissões e aparece nos
  eventos como `actor_token_id = "bootstrap"`. É comparado por `hmac.compare_digest` entre resumos
  (bytes), nunca entre `str` crus (um cabeçalho não ASCII daria `TypeError`).
- **Subconjunto**: um token só concede permissões que tem; `missing_permissions` diz quais
  faltam, em ordem alfabética.
- **Revogação** idempotente: `revoked_at` é gravado uma única vez, e só a primeira gera
  `token.revoked`.
- **Rótulo:** texto livre, gravado redigido e redigido de novo na saída, com as identidades do
  momento; na gravação, elas são lidas depois do lock dos eventos, para que um
  cadastro concorrente confirmado antes não escape. U+0000 ou substituto UTF-16 isolado é
  `invalid_format`.

As funções que mudam o banco abrem uma transação (ou um SAVEPOINT) com a mudança e o evento juntos.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets as _secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import psycopg

from cq_server.events import EventType, append_event, load_known_identities, load_write_identities
from cq_server.permissions import Permission
from cq_server.redaction import redact

SECRET_PREFIX = "cq_"
SECRET_PATTERN = re.compile(rb"cq_[A-Za-z0-9_-]{43}")
TOKEN_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
BOOTSTRAP_ACTOR = "bootstrap"
ALL_PERMISSIONS: frozenset[str] = frozenset(permission.value for permission in Permission)

LABEL_MAX_LENGTH = 100
MIN_PERMISSIONS = 1
MAX_PERMISSIONS = len(ALL_PERMISSIONS)

# Caracteres que o banco não guarda (o mesmo critério do nome e da justificativa em `people`):
# U+0000, que `text` e `jsonb` recusam, e os substitutos UTF-16 (U+D800 a U+DFFF), que chegam
# isolados pelo escape JSON (`"\ud800"`) e não têm UTF-8.
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")


class InvalidLabel(ValueError):
    """Rótulo fora das regras; `reason` é o motivo do contrato."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    """Quem fez o pedido: `actor` vai para `actor_token_id` dos eventos."""

    actor: str                     # `token_id` em texto ou "bootstrap"
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions


BOOTSTRAP = Principal(actor=BOOTSTRAP_ACTOR, permissions=ALL_PERMISSIONS)


@dataclass(frozen=True)
class Token:
    token_id: uuid.UUID
    label: str
    permissions: tuple[str, ...]  # em ordem alfabética
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class CreatedToken:
    token: Token
    secret: str


# --------------------------------------------------------------------------------------------------
# Segredo e autenticação
# --------------------------------------------------------------------------------------------------


def generate_secret() -> str:
    return SECRET_PREFIX + _secrets.token_urlsafe(32)


def secret_digest(secret: str | bytes) -> str:
    """SHA-256 em hex minúsculo dos bytes do segredo (`str` vira UTF-8)."""
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hashlib.sha256(raw).hexdigest()


def is_bootstrap_secret(presented: bytes, admin_token: str | None) -> bool:
    """O segredo apresentado é o token inicial configurado (comparação em tempo constante)."""
    if not admin_token:
        return False
    expected = hashlib.sha256(admin_token.encode("utf-8")).digest()
    return hmac.compare_digest(hashlib.sha256(presented).digest(), expected)


def may_be_secret(presented: bytes) -> bool:
    """Tem a forma de um segredo gerado aqui (só esses podem estar no banco)."""
    return SECRET_PATTERN.fullmatch(presented) is not None


def find_principal(conn: psycopg.Connection, digest: str) -> Principal | None:
    """O token ativo com o resumo dado, ou `None` (inexistente ou revogado)."""
    row = conn.execute(
        "SELECT token_id, permissions, revoked_at FROM api_tokens WHERE secret_sha256 = %s",
        (digest,),
    ).fetchone()
    if row is None or row[2] is not None:
        return None
    return Principal(actor=str(row[0]), permissions=frozenset(row[1]) & ALL_PERMISSIONS)


def authenticate(conn: psycopg.Connection | None, presented: bytes, *,
                 admin_token: str | None) -> Principal | None:
    """Autentica o segredo apresentado: o token inicial (sem banco) ou um token ativo do banco.

    `conn` pode ser `None` quando o chamador só quer saber do token inicial; um segredo que não tem
    a forma de `generate_secret` nunca consulta o banco.
    """
    if is_bootstrap_secret(presented, admin_token):
        return BOOTSTRAP
    if conn is None or not may_be_secret(presented):
        return None
    return find_principal(conn, secret_digest(presented))


# --------------------------------------------------------------------------------------------------
# Regras de entrada
# --------------------------------------------------------------------------------------------------


def is_token_id(value: object) -> bool:
    """UUID v4 canônico minúsculo."""
    return isinstance(value, str) and TOKEN_ID_PATTERN.fullmatch(value) is not None


def normalize_label(value: str) -> str:
    """Rótulo sem espaços nas pontas, de 1 a 100 caracteres.

    Depois do tamanho, um caractere que o banco não guarda é `invalid_format`.
    """
    label = value.strip()
    if not label:
        raise InvalidLabel("too_short")
    if len(label) > LABEL_MAX_LENGTH:
        raise InvalidLabel("too_long")
    if _UNSTORABLE.search(label):
        raise InvalidLabel("invalid_format")
    return label


def missing_permissions(principal: Principal, requested: Iterable[str]) -> list[str]:
    """As permissões pedidas que o token atual não tem, em ordem alfabética."""
    return sorted(set(requested) - principal.permissions)


# --------------------------------------------------------------------------------------------------
# Operações
# --------------------------------------------------------------------------------------------------


def _token(row: tuple, label: str) -> Token:
    token_id, _, permissions, created_at, revoked_at = row
    return Token(token_id=token_id, label=label, permissions=tuple(sorted(permissions)),
                 created_at=created_at, revoked_at=revoked_at)


def create_token(conn: psycopg.Connection, *, label: str, permissions: Iterable[str], now: datetime,
                 actor_token_id: str | None, secrets: Iterable[str | None] = ()) -> CreatedToken:
    """Cria um token e grava `token.created`, juntos; devolve o segredo **uma** vez.

    `permissions`: subconjunto não vazio de `Permission` (a checagem de que o criador as tem é de
    quem chama, com `missing_permissions`). O rótulo é gravado aparado e redigido.
    """
    clean_label = normalize_label(label)
    granted = sorted(set(permissions))
    if not granted or not set(granted) <= ALL_PERMISSIONS:
        raise ValueError("permissões vazias ou desconhecidas")
    secret_list = list(secrets)
    secret = generate_secret()
    token_id = uuid.uuid4()
    with conn.transaction():
        # Lock dos eventos antes das identidades: um cadastro concorrente confirmado antes de
        # `token.created` entra na redação do rótulo (ver `events.load_write_identities`).
        identities = load_write_identities(conn)
        stored_label = redact(clean_label, identities, secrets=secret_list)
        conn.execute(
            "INSERT INTO api_tokens (token_id, label, secret_sha256, permissions, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (token_id, stored_label, secret_digest(secret), granted, now),
        )
        append_event(conn, EventType.TOKEN_CREATED,
                     {"token_id": token_id, "label": stored_label, "permissions": granted},
                     occurred_at=now, actor_token_id=actor_token_id, secrets=secret_list,
                     identities=identities)
    token = Token(token_id=token_id, label=stored_label, permissions=tuple(granted), created_at=now,
                  revoked_at=None)
    return CreatedToken(token=token, secret=secret)


def list_tokens(conn: psycopg.Connection, *, secrets: Iterable[str | None] = ()) -> list[Token]:
    """Os tokens do banco em ordem de criação, com o rótulo redigido de novo na saída."""
    secret_list = list(secrets)
    with conn.transaction():
        identities = load_known_identities(conn)
        rows = conn.execute(
            "SELECT token_id, label, permissions, created_at, revoked_at FROM api_tokens ORDER BY id"
        ).fetchall()
    return [_token(row, redact(row[1], identities, secrets=secret_list)) for row in rows]


def revoke_token(conn: psycopg.Connection, token_id: uuid.UUID, *, now: datetime,
                 actor_token_id: str | None, secrets: Iterable[str | None] = ()) -> Token | None:
    """Revoga o token (idempotente); `None` se não existe.

    A primeira revogação grava `revoked_at = now` e `token.revoked`, juntos; as seguintes devolvem o
    mesmo `revoked_at` e não gravam nada.
    """
    secret_list = list(secrets)
    with conn.transaction():
        row = conn.execute(
            "SELECT token_id, label, permissions, created_at, revoked_at FROM api_tokens "
            "WHERE token_id = %s FOR UPDATE",
            (token_id,),
        ).fetchone()
        if row is None:
            return None
        if row[4] is None:
            row = conn.execute(
                "UPDATE api_tokens SET revoked_at = %s WHERE token_id = %s "
                "RETURNING token_id, label, permissions, created_at, revoked_at",
                (now, token_id),
            ).fetchone()
            assert row is not None
            append_event(conn, EventType.TOKEN_REVOKED, {"token_id": token_id}, occurred_at=now,
                         actor_token_id=actor_token_id)
        identities = load_known_identities(conn)
    return _token(row, redact(row[1], identities, secrets=secret_list))
