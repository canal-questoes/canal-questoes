"""Registro dos segredos de token criados durante um teste.

`create_token`, `VALID_CALLS["tokens.create"]` e a leitura de um `--secret-file` acrescentam o
segredo aqui; uma fixture autouse do conftest zera o registro a cada teste. O canário e o teste de
sistema procuram `all_secrets()` (inclui o token inicial) em todas as saídas.

**Bots:** os tokens de bot da Bot API falsa (inteiros e a parte
secreta) e os links/códigos de convite de `create_team_link` também entram por `remember_secret`. As
chaves-mestras de teste ficam **fora** de `all_secrets()` e saem por
`bot_secrets()`: `MASTER_KEY` (a chave dos testes) e `OTHER_MASTER_KEY` (outra chave válida, para o
caso "chave trocada"), geradas uma vez por sessão (processo), 32 bytes aleatórios em base64url sem
`=` (43 caracteres, forma canônica), como `CQ_MASTER_KEY` exige. Só o
teste de vetor conhecido da cifra usa uma chave fixa, própria.

**Vínculo do aluno:** os códigos de convite de aluno lidos do SMTP falso
(`FakeSmtp.invite_links()`) entram por `remember_secret`, como os tokens.
As credenciais do SMTP falso ficam **fora** de `all_secrets()`, como as
chaves-mestras: `mail_secrets(smtp)` lê o atributo `credentials` (usuário e senha sorteados no
`reset`) e devolve a senha e as suas formas no fio (o base64 do AUTH PLAIN `"\\0usuário\\0senha"` e o
base64 da senha, do AUTH LOGIN). O detector genérico de código de convite
(`find_invite_codes(texto, resumos)`, no molde do `team._candidate_codes` dos bots, sem importar
`cq_server`) testa **toda janela de 22 caracteres** de cada sequência maximal `[A-Za-z0-9_-]{22,}`, em
todas as formas de `_forms` do canário (bruta, sem percent-encoding e strings de JSON), contra o
conjunto de resumos SHA-256 hex dado (os de `invites.code_sha256` dos convites de aluno); acha também
os códigos nunca entregues.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets as _random
from collections.abc import Collection

from tests.helpers.canary import _forms

SECRETS: list[str] = []


def _new_master_key() -> str:
    key = _random.token_urlsafe(32)
    assert len(key) == 43 and "=" not in key
    return key


MASTER_KEY: str = _new_master_key()
OTHER_MASTER_KEY: str = _new_master_key()
while OTHER_MASTER_KEY == MASTER_KEY:  # pragma: no cover - probabilidade desprezível
    OTHER_MASTER_KEY = _new_master_key()


def remember_secret(secret: str) -> None:
    if secret not in SECRETS:
        SECRETS.append(secret)


def reset_secrets() -> None:
    SECRETS.clear()


def all_secrets(*extra: str) -> list[str]:
    from tests.helpers.api import ADMIN_TOKEN

    return [ADMIN_TOKEN, *SECRETS, *extra]


def bot_secrets(*extra: str) -> list[str]:
    """As chaves-mestras de teste (a da sessão e a "trocada"), mais `extra`, para as varreduras."""
    return [MASTER_KEY, OTHER_MASTER_KEY, *extra]


# --------------------------------------------------------------------------------------------------
# credenciais do SMTP falso
# --------------------------------------------------------------------------------------------------


def mail_secrets(smtp: object, *extra: str) -> list[str]:
    """A senha do SMTP falso e as suas formas no fio, mais `extra`, para as varreduras.

    Lê `smtp.credentials` (a tupla `(usuário, senha)` sorteada pelo último `reset` do `FakeSmtp`, de
    `tests/helpers/smtp.py`) na hora da chamada; depois de outro `reset`, chame de novo. Devolve
    `[senha, base64("\\0usuário\\0senha"), base64(senha)]`: o AUTH PLAIN e o AUTH LOGIN do `smtplib`
    mandam essas formas em base64 padrão, com `=` (o usuário sozinho não é segredo). Nada entra em
    `SECRETS`: fica fora de `all_secrets()`, como as chaves-mestras de `bot_secrets()`. Sem
    credenciais, é erro do teste (senão a varredura passaria no vazio).
    """
    credentials = getattr(smtp, "credentials", None)
    if credentials is None:
        raise ValueError("o SMTP falso não tem credenciais (chame `reset` antes de `mail_secrets`)")
    username, password = credentials
    if not isinstance(username, str) or not isinstance(password, str) or not password:
        raise ValueError("`credentials` deve ser um par (usuário, senha) de textos, com senha não vazia")
    plain = base64.b64encode(f"\0{username}\0{password}".encode("utf-8")).decode("ascii")
    login = base64.b64encode(password.encode("utf-8")).decode("ascii")
    return [password, plain, login, *extra]


# --------------------------------------------------------------------------------------------------
# detector genérico de código de convite
# --------------------------------------------------------------------------------------------------

INVITE_CODE_LENGTH = 22
_CODE_RUN = re.compile(r"[A-Za-z0-9_-]{22,}")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def invite_code_digest(code: str) -> str:
    """SHA-256 (hex minúsculo) dos bytes UTF-8 de um código, a forma de `invites.code_sha256`."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def invite_code_label(digest: str) -> str:
    """O rótulo que `find_invite_codes` devolve para o código cujo resumo é `digest`."""
    return f"convite:{digest.lower()[:8]}…"


def _code_windows(text: str) -> set[str]:
    """Toda janela de 22 caracteres de cada sequência maximal `[A-Za-z0-9_-]{22,}`, em cada forma de
    `_forms` (bruta, sem percent-encoding e strings de JSON): candidatos a código de convite."""
    windows: set[str] = set()
    for form in _forms(text):
        for run in _CODE_RUN.finditer(form):
            chunk = run.group()
            for start in range(len(chunk) - INVITE_CODE_LENGTH + 1):
                windows.add(chunk[start:start + INVITE_CODE_LENGTH])
    return windows


def find_invite_codes(text: str, digests: Collection[str]) -> list[str]:
    """Rótulos (`invite_code_label`) dos códigos de convite achados em `text`: toda janela de 22
    caracteres cujo SHA-256 hex está em `digests` (lista vazia se nenhum).

    `digests` é a coleção dos resumos (64 dígitos hex, ex.: os de `invites.code_sha256` dos convites
    de aluno); um texto no lugar da coleção, uma coleção vazia ou um item que não é resumo é erro do
    teste (senão a varredura passaria no vazio), mesmo com texto vazio. O código colado a outras
    letras, em percent-encoding (`%3D<código>`) ou escapado dentro de JSON também é achado.
    """
    if isinstance(digests, str) or not digests:
        raise ValueError("digests deve ser uma coleção não vazia de resumos SHA-256 hex")
    items = list(digests)
    if any(not isinstance(d, str) or _HEX_DIGEST.fullmatch(d.lower()) is None for d in items):
        raise ValueError("digests deve ter só resumos SHA-256 hex (64 dígitos hex)")
    wanted = {digest.lower() for digest in items}
    if not text:
        return []
    hits = {digest for digest in map(invite_code_digest, _code_windows(text)) if digest in wanted}
    return sorted(invite_code_label(digest) for digest in hits)
