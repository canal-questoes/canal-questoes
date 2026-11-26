"""Referência de questão: as formas aceitas de repositório, commit e `qid`, a chave do repositório no
disco e o cadastro da referência, com a recusa sem eco do valor recebido.

- `REPO_URL`, `COMMIT` e `QID`: as formas aceitas de `repo`, `commit` e `qid` (a regex fechada do
  `repo` elimina a divergência de parser entre o Python e o curl: só `https`, host DNS em minúsculas
  com ponto, sem porta, IP, userinfo, `?`, `#`, `%` nem `\\`). Casam o texto inteiro.
- `validate_repo(repo, allowed_hosts)`: `None` = válido; senão `repo_length` · `repo_syntax` ·
  `repo_dot_segment` · `host_not_allowed` (a lista vazia recusa tudo).
- `BANK_REPO`: a forma `bank:<nome>` de um banco de questões enviado (o nome no padrão do código do
  oferecimento); `validate_ref_repo(repo, allowed_hosts)`: como `validate_repo`, mas aceita também a forma
  `bank:`, sem a lista de hosts (`None`, ou um código de `REPO_ERRORS` ou `bank_name`).
- `repo_key(repo)`: o SHA-256 hex do UTF-8 de `repo`, o mesmo cálculo do motor (a chave do diretório
  `<repos>/<repo_key>/<commit>/`). É a **única** definição.
- `ensure_ref` cria ou acha a referência (`INSERT … ON CONFLICT DO NOTHING` + `SELECT`);
  `record_inspection` grava a inspeção uma vez, com o título redigido.

Núcleo sem HTTP, `subprocess` nem `socket`. A tabela `question_refs` não tem CHECK: as formas
de `repo`, `commit` e `qid` são conferidas aqui antes do `INSERT` (`ValueError`, sem o valor), e a
violação do índice único nunca acontece (`ON CONFLICT DO NOTHING`). `ref_row` devolve o id interno e o
`ref_uuid` da referência (uso do preparo do disparo, na mesma transação).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from datetime import datetime

import psycopg

from cq_server.ports.engine import QuestionInfo, QuestionRef
from cq_server.redaction import KnownIdentity, redact

# Tamanhos.
REPO_MAX_LENGTH = 2048
QID_MAX_LENGTH = 512

# A forma do `repo` (o host e o caminho em grupos nomeados).
REPO_URL = re.compile(
    r"\Ahttps://(?P<host>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)"
    r"(?P<path>(?:/[A-Za-z0-9._~-]+)+/?)\Z")
# Contrato do motor, `$defs.question_ref.commit`.
COMMIT = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# Contrato do motor, `$defs.question_ref.qid` (1 a 512; nenhum segmento `.` ou `..`).
QID = re.compile(r"\A(?=.{1,512}\Z)(?!.*(?:^|/)\.{1,2}(?:/|\Z))[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")

# Códigos de `validate_repo`.
REPO_ERRORS = ("repo_length", "repo_syntax", "repo_dot_segment", "host_not_allowed")

# A referência de um banco de questões enviado: `bank:<nome>`, com o nome no padrão do código do oferecimento.
BANK_REPO = re.compile(r"\Abank:[a-z0-9][a-z0-9-]{2,39}\Z")
# O prefixo que separa a forma `bank:` da `https` e a versão do banco no papel do commit (só 64 hexadecimais).
_BANK_PREFIX = "bank:"
_BANK_VERSION = re.compile(r"\A[0-9a-f]{64}\Z")

# Um host cujo último rótulo é só
# número (decimal, ou hexadecimal com `0x`) é um IP literal para o analisador de URL (a regra "ends in a
# number" do WHATWG, que o curl segue): `REPO_URL` o aceita, mas a política o recusa como `repo_syntax`,
# mesmo com ele na lista de hosts.
_NUMERIC_LABEL = re.compile(r"\A(?:[0-9]+|0x[0-9a-f]*)\Z")


def repo_key(repo: str) -> str:
    """O SHA-256 hex (minúsculo) de `repo` em UTF-8: a chave do diretório no motor e na obtenção."""
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()


def validate_repo(repo: str, allowed_hosts: frozenset[str]) -> str | None:
    """`None` se `repo` é válido e o host está em `allowed_hosts`; senão o código (`REPO_ERRORS`).

    A ordem é fixa: `repo_length` (mais de `REPO_MAX_LENGTH` caracteres) · `repo_syntax` (fora de
    `REPO_URL`, ou host que é IP literal) · `repo_dot_segment` (algum segmento do caminho é `.` ou `..`)
    · `host_not_allowed` (comparação exata do host; a lista vazia recusa tudo). O valor nunca vai para
    o código devolvido (recusa sem eco)."""
    if not isinstance(repo, str):
        return "repo_syntax"
    if len(repo) > REPO_MAX_LENGTH:
        return "repo_length"
    match = REPO_URL.fullmatch(repo)
    if match is None:
        return "repo_syntax"
    host = match.group("host")
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        return "repo_syntax"
    if any(segment in (".", "..") for segment in match.group("path").split("/")):
        return "repo_dot_segment"
    if host not in allowed_hosts:
        return "host_not_allowed"
    return None


def validate_ref_repo(repo: str, allowed_hosts: frozenset[str]) -> str | None:
    """O `repo` de uma referência de questão: `None` se é um `bank:<nome>` na forma de `BANK_REPO` (sem
    a lista de hosts), ou o resultado de `validate_repo`; uma forma `bank:` fora do padrão é `bank_name`.
    O valor nunca vai para o código devolvido."""
    if isinstance(repo, str) and repo.startswith(_BANK_PREFIX):
        return None if BANK_REPO.fullmatch(repo) is not None else "bank_name"
    return validate_repo(repo, allowed_hosts)


def _check_forms(repo: str, commit: str, qid: str) -> None:
    """As formas de `repo`, `commit` e `qid` (a tabela não tem CHECK). A lista de hosts é da
    API (o preparo já a conferiu); aqui só a forma: um `bank:<nome>` na forma de `BANK_REPO`, com a versão
    (64 hexadecimais) no `commit`, ou uma `https` com 40 ou 64. `ValueError` sem o valor."""
    if not isinstance(repo, str) or validate_ref_repo(repo, frozenset({_host_of(repo)})) is not None:
        raise ValueError("referência de questão com repo fora da forma")
    is_bank = repo.startswith(_BANK_PREFIX)
    if not isinstance(commit, str) or (_BANK_VERSION if is_bank else COMMIT).fullmatch(commit) is None:
        raise ValueError("referência de questão com commit fora da forma")
    if not isinstance(qid, str) or QID.fullmatch(qid) is None:
        raise ValueError("referência de questão com qid fora da forma")


def _host_of(repo: str) -> str:
    match = REPO_URL.fullmatch(repo) if isinstance(repo, str) and len(repo) <= REPO_MAX_LENGTH else None
    return "" if match is None else match.group("host")


def ref_row(conn: psycopg.Connection, *, repo: str, commit: str, qid: str) -> tuple[int, uuid.UUID] | None:
    """O id interno e o `ref_uuid` da referência `(repo, commit, qid)`, ou `None` (pela chave
    `(repo_sha256, commit_sha, qid)`, como o índice único)."""
    row = conn.execute(
        "SELECT id, ref_uuid FROM question_refs WHERE repo_sha256 = %s AND commit_sha = %s AND qid = %s",
        (repo_key(repo), commit, qid),
    ).fetchone()
    return None if row is None else (int(row[0]), row[1])


def ensure_ref(conn: psycopg.Connection, *, repo: str, commit: str, qid: str, now: datetime) -> QuestionRef:
    """A referência `(repo, commit, qid)`, criada se ainda não existe (na transação de `conn`):
    `INSERT … ON CONFLICT DO NOTHING` + `SELECT`. O `repo` é guardado literal (duas
    grafias do mesmo repositório são referências distintas)."""
    _check_forms(repo, commit, qid)
    conn.execute(
        "INSERT INTO question_refs (ref_uuid, repo, repo_sha256, commit_sha, qid, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (uuid.uuid4(), repo, repo_key(repo), commit, qid, now),
    )
    if ref_row(conn, repo=repo, commit=commit, qid=qid) is None:  # pragma: no cover - só com corrida
        raise LookupError("referência de questão ausente depois do INSERT")
    return QuestionRef(repo=repo, commit=commit, qid=qid)


def record_inspection(conn: psycopg.Connection, ref_row_id: int, question: QuestionInfo, *, now: datetime,
                      identities: Sequence[KnownIdentity]) -> None:
    """Grava a inspeção da referência uma vez (só se ainda não foi inspecionada), com o título
    redigido pelas identidades dadas (as de `events.load_write_identities`, na mesma transação)."""
    title = None if question.title is None else redact(question.title, identities)
    conn.execute(
        "UPDATE question_refs SET supported = true, show_correct_answer = %s, single_variant = %s, "
        "partial_credit = %s, title = %s, answers_names = %s, content_hash = %s, inspected_at = %s "
        "WHERE id = %s AND inspected_at IS NULL",
        (bool(question.show_correct_answer), bool(question.single_variant), bool(question.partial_credit),
         title, list(question.answers_names), question.content_hash, now, ref_row_id),
    )
