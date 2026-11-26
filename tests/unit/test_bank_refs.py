"""A referência de questão de um banco enviado: `bank:<nome>`, com a versão do banco no papel do commit.

O que se confere aqui, sem banco de dados:
- `questionrefs.BANK_REPO` (o texto inteiro: `bank:` e um nome de 3 a 40 caracteres, minúsculas, dígitos e hífen,
  sem começar por hífen);
- `questionrefs.validate_ref_repo(repo, allowed_hosts)`: aceita `bank:<nome>` sem a lista de hosts (também com a
  lista vazia), recusa a forma `bank:` fora do padrão como `bank_name` e delega todo o resto a `validate_repo`, com
  os mesmos códigos; o valor nunca vai para o código devolvido;
- `questionrefs._check_forms`: o `commit` de uma referência `bank:` só com 64 hexadecimais, e o de uma `https` com
  40 ou 64; a recusa é `ValueError` sem o valor;
- o preparo do disparo (o leitor do pedido da rota, `_dispatch_prepare_request`): `bank:<nome>` com a versão é
  aceito também com a lista de hosts vazia; a forma `bank:` fora do padrão → `{repo, invalid_format}`; um commit de
  40 hexadecimais numa referência `bank:` → `{commit, invalid_format}`; o preparo não confere a existência do banco
  (nenhuma conexão, nenhuma leitura da tabela dos bancos).

No esqueleto, `validate_ref_repo` lança `NotImplementedError`, `_check_forms` ainda recusa toda referência `bank:` e o
preparo ainda a recusa como `{repo, invalid_format}`: todo caso falha.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

HEX40 = "0123456789abcdef0123456789abcdef01234567"
HEX64 = "0123456789abcdef" * 4
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
HOSTS = frozenset({"repos.cq.test"})

VALID_NAMES = ("abc", "mc102", "banco-de-questoes", "a-1", "0ab", "a" * 40, "z" + "9" * 39)
INVALID_BANK_FORMS = ("bank:", "bank:ab", "bank:" + "a" * 41, "bank:ABC", "bank:Banco", "bank:-abc",
                      "bank:ab_c", "bank:ab.c", "bank:ab c", "bank:abc/def", "bank:abc\n", "bank:\nabc",
                      "bank:açaí", "bank:abc:def", "bank:abc/", "bank::abc")

# Referências que não são `bank:`, cada uma com o código que `validate_repo` dá com `HOSTS`.
OTHER_REPOS = (
    "https://repos.cq.test/curso/banco.git",
    "https://repos.cq.test/curso/../banco.git",
    "https://outro.cq.test/curso/banco.git",
    "https://127.0.0.1/banco.git",
    "https://repos.cq.test/" + "a" * 2100,
    "bankx:abc",
    "BANK:abc",
    "Bank:abc",
    " bank:abc",
    "git@repos.cq.test:curso/banco.git",
    "",
)


def test_bank_repo_pattern_and_validate_ref_repo() -> None:
    """`BANK_REPO` casa o texto inteiro da forma `bank:<nome>` e nada mais; `validate_ref_repo` aceita essa forma com
    a lista de hosts vazia e com qualquer lista (`None`), recusa a forma `bank:` fora do padrão como `bank_name` (o
    código não traz o valor), e delega o resto a `validate_repo`, com os mesmos códigos, com a lista dos testes e com
    a vazia (que recusa toda `https`)."""
    from cq_server.questionrefs import BANK_REPO, REPO_ERRORS, validate_ref_repo, validate_repo

    for name in VALID_NAMES:
        repo = f"bank:{name}"
        assert BANK_REPO.fullmatch(repo) is not None, repo
        assert BANK_REPO.match(repo) is not None, repo
        assert validate_ref_repo(repo, frozenset()) is None, repo
        assert validate_ref_repo(repo, HOSTS) is None, repo
    for repo in INVALID_BANK_FORMS:
        assert BANK_REPO.fullmatch(repo) is None, repo
        assert BANK_REPO.match(repo) is None, repo
        code = validate_ref_repo(repo, frozenset())
        assert code == "bank_name", repo
        assert validate_ref_repo(repo, HOSTS) == "bank_name", repo
    assert "bank_name" not in REPO_ERRORS

    seen = set()
    for repo in OTHER_REPOS:
        for hosts in (HOSTS, frozenset()):
            expected = validate_repo(repo, hosts)
            assert validate_ref_repo(repo, hosts) == expected, (repo, hosts)
            seen.add(expected)
    assert seen == {None, *REPO_ERRORS}
    assert validate_ref_repo("https://repos.cq.test/curso/banco.git", frozenset()) == "host_not_allowed"


def test_check_forms_requires_64_hex_for_bank_and_accepts_40_or_64_for_https() -> None:
    """`_check_forms` (a forma gravada da referência): `bank:<nome>` só com um commit de 64 hexadecimais (a versão)
    — 40 hexadecimais, maiúsculas e 63 ou 65 caracteres são recusados —, a forma `bank:` fora do padrão recusada com
    qualquer commit, e a `https` com 40 ou 64; toda recusa é `ValueError` sem o valor recebido."""
    from cq_server.questionrefs import _check_forms

    _check_forms("bank:banco-sintetico", HEX64, "q1")
    _check_forms("bank:abc", HEX64, "pasta/q1")
    _check_forms("https://repos.cq.test/curso/banco.git", HEX40, "q1")
    _check_forms("https://repos.cq.test/curso/banco.git", HEX64, "q1")

    refused = [("bank:banco-sintetico", HEX40), ("bank:banco-sintetico", HEX64.upper()),
               ("bank:banco-sintetico", HEX64[:63]), ("bank:banco-sintetico", HEX64 + "0"),
               ("bank:BANCO", HEX64), ("bank:ab", HEX64), ("bank:", HEX64),
               ("https://repos.cq.test/curso/banco.git", HEX40[:39])]
    for repo, commit in refused:
        with pytest.raises(ValueError) as info:
            _check_forms(repo, commit, "q1")
        text = str(info.value) + repr(info.value)
        assert repo not in text and commit not in text, (repo, commit)


def _prepare(obj: dict[str, Any], *, hosts: frozenset[str] = frozenset()) -> tuple[Any, list[Any]]:
    from cq_server.api.routes import _dispatch_prepare_request
    from cq_server.api.validation import RequestChecker

    check = RequestChecker()
    request = _dispatch_prepare_request(check, obj, now=NOW, allowed_hosts=hosts, client="cli")
    return request, check.issues


def test_prepare_accepts_bank_reference_and_refuses_its_wrong_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """O leitor do pedido de preparo: `bank:<nome>` com a versão (64 hexadecimais) é aceito com a lista de hosts
    vazia e com a dos testes, sem nenhum problema; a forma `bank:` fora do padrão → `{repo, invalid_format}`, sem
    outro campo; um commit de 40 hexadecimais numa referência `bank:` → `{commit, invalid_format}`, sem outro campo;
    a `https` segue como antes (`host_not_allowed` com a lista vazia → `{repo, invalid_value}`).

    O preparo não confere a existência do banco: o leitor do pedido não recebe conexão, e com a conexão ao
    PostgreSQL e as funções da tabela dos bancos trocadas por armadilhas, a leitura de um banco que não existe
    termina sem tocá-las; nem o leitor nem `dispatches.prepare_dispatch` citam a tabela ou as funções dos bancos."""
    import psycopg

    from cq_server import banks, dispatches
    from cq_server.api import routes
    from cq_server.api.errors import FieldIssue

    touched: list[str] = []

    def trap(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"o preparo tocou {name}")
        return call

    monkeypatch.setattr(psycopg, "connect", trap("psycopg.connect"))
    for name in ("load_archive", "list_banks", "ensure_bank"):
        monkeypatch.setattr(banks, name, trap(f"banks.{name}"))

    for hosts in (frozenset(), HOSTS):
        request, issues = _prepare({"repo": "bank:banco-inexistente", "commit": HEX64, "qid": "q1"}, hosts=hosts)
        assert issues == []
        assert request is not None
        assert (request.repo, request.commit, request.qid) == ("bank:banco-inexistente", HEX64, "q1")
        assert request.origin == "cli"
        assert request.opens_at is None and request.closes_at is None

    for repo in ("bank:AB", "bank:ab", "bank:", "bank:abc/def"):
        request, issues = _prepare({"repo": repo, "commit": HEX64, "qid": "q1"})
        assert request is None
        assert issues == [FieldIssue("repo", "invalid_format")], repo

    request, issues = _prepare({"repo": "bank:banco-sintetico", "commit": HEX40, "qid": "q1"})
    assert request is None
    assert issues == [FieldIssue("commit", "invalid_format")]

    request, issues = _prepare({"repo": "https://repos.cq.test/curso/banco.git", "commit": HEX40, "qid": "q1"})
    assert request is None
    assert issues == [FieldIssue("repo", "invalid_value")]
    request, issues = _prepare({"repo": "https://repos.cq.test/curso/banco.git", "commit": HEX40, "qid": "q1"},
                               hosts=HOSTS)
    assert issues == [] and request is not None

    assert touched == []
    assert "conn" not in inspect.signature(routes._dispatch_prepare_request).parameters
    for function in (routes._dispatch_prepare_request, dispatches.prepare_dispatch):
        source = inspect.getsource(function)
        for needle in ("question_banks", "load_archive", "list_banks", "ensure_bank"):
            assert needle not in source, (function.__name__, needle)
