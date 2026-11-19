"""Código de confirmação do `down` pelo agente.


`cq_client.mcp_server.DownCodeStore(clock, ttl=DOWN_CODE_TTL_SECONDS, new_code=…)` guarda, só na memória, um código
por projeto Compose: `issue(project)` emite (e substitui o anterior do mesmo projeto) e devolve o código em claro;
`consume(code, project)` confere o resumo `sha256` por `hmac.compare_digest`, o projeto, o vencimento no relógio
monotônico e o uso, e marca o código usado na mesma operação. O código em claro nunca fica guardado: nenhum atributo
nem o `repr` o têm, e o `repr` também não tem o resumo.

Relógio monotônico falso (`MonoClock`) e `new_code` injetável (`CodeSequence`); nenhum `sleep` real. No esqueleto,
`issue` e `consume` lançam `NotImplementedError`; o `__repr__` e o construtor são reais e ficam na mesma função de um
`issue`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from cq_client import mcp_server
from cq_client.mcp_server import DOWN_CODE_TTL_SECONDS, DownCodeStore

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")
PROJECT = "cq"
OTHER_PROJECT = "cq-dev"


class MonoClock:
    """O relógio monotônico falso (segundos, `float`), avançado só pelo teste."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CodeSequence:
    """O `new_code` injetável: códigos determinísticos, distintos, no formato do `token_urlsafe(16)`."""

    def __init__(self, prefix: str = "codigoDeTesteDown") -> None:
        assert len(prefix) == 17
        self.prefix = prefix
        self.issued: list[str] = []

    def __call__(self) -> str:
        code = f"{self.prefix}{len(self.issued) + 1:05d}"
        assert CODE_PATTERN.fullmatch(code)
        self.issued.append(code)
        return code


def _store(clock: MonoClock | None = None, **kwargs: Any) -> tuple[DownCodeStore, MonoClock, CodeSequence]:
    clock = clock if clock is not None else MonoClock()
    codes = kwargs.pop("new_code", None) or CodeSequence()
    return DownCodeStore(clock, new_code=codes, **kwargs), clock, codes


def _walk(value: Any, seen: set[int] | None = None, depth: int = 0) -> Iterator[Any]:
    """Todo valor alcançável a partir de `value` pelos atributos, dicionários, sequências e campos de dataclass
    (sem funções nem módulos), para procurar o código em claro e o resumo."""
    seen = seen if seen is not None else set()
    if id(value) in seen or depth > 8:
        return
    seen.add(id(value))
    yield value
    if isinstance(value, (str, bytes, bytearray, int, float, bool, type(None))):
        return
    if callable(value) and not hasattr(value, "__dict__"):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(key, seen, depth + 1)
            yield from _walk(item, seen, depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk(item, seen, depth + 1)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from _walk(getattr(value, f.name, None), seen, depth + 1)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and not isinstance(value, type) and not callable(value):
        for key, item in attrs.items():
            yield from _walk(item, seen, depth + 1)


def _contains(value: Any, needle: str | bytes) -> bool:
    for item in _walk(value):
        if isinstance(needle, str) and isinstance(item, str) and needle in item:
            return True
        if isinstance(needle, bytes) and isinstance(item, (bytes, bytearray)) and needle in bytes(item):
            return True
    return False


# --------------------------------------------------------------------------------------------------
# Emissão, formato e o que fica guardado


def test_issue_format_and_only_digest_stored() -> None:
    """`issue(project)` devolve um código `^[A-Za-z0-9_-]{22}$` (o padrão, com o
    `new_code` real, é `secrets.token_urlsafe(16)`); só o resumo `sha256` fica guardado: nenhum atributo do
    registro tem o código em claro (nem os bytes dele), e o resumo está lá. Na mesma função: o `repr`
    (real no esqueleto) sem o código e sem o resumo."""
    clock = MonoClock()
    real = DownCodeStore(clock)
    first = real.issue(PROJECT)
    second = real.issue(OTHER_PROJECT)
    assert isinstance(first, str) and CODE_PATTERN.fullmatch(first), first
    assert CODE_PATTERN.fullmatch(second) and second != first

    store, _, codes = _store(clock)
    code = store.issue(PROJECT)
    assert code == codes.issued[-1], "o código emitido é o do new_code injetado"
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    assert not _contains(store, code), "o código em claro ficou guardado no registro"
    assert not _contains(store, code.encode("utf-8")), "os bytes do código ficaram guardados no registro"
    assert _contains(store, digest), "o resumo sha256 do código não está no registro"
    for text in (repr(store), str(store)):
        assert code not in text
        assert digest.hex() not in text and digest.hex().upper() not in text
        assert repr(digest) not in text
    assert repr(store) == f"DownCodeStore(ttl={DOWN_CODE_TTL_SECONDS!r})"
    for record_text in (repr(r) for r in _walk(store) if isinstance(r, mcp_server.DownCodeRecord)):
        assert code not in record_text and digest.hex() not in record_text
    assert not _contains(real, first) and not _contains(real, second)


# --------------------------------------------------------------------------------------------------
# Uso único, vencimento, projeto, substituição


def test_consume_once() -> None:
    """`consume` é verdadeiro uma vez e falso na segunda (uso único)."""
    store, _, _ = _store()
    code = store.issue(PROJECT)
    assert store.consume(code, PROJECT) is True
    assert store.consume(code, PROJECT) is False
    assert store.consume(code, PROJECT) is False


def test_consume_after_ttl_is_false() -> None:
    """falso a partir de `DOWN_CODE_TTL_SECONDS` (300 s) no relógio monotônico
    (`clock() >= expires_at_mono`); um instante antes, verdadeiro."""
    assert DOWN_CODE_TTL_SECONDS == 300
    store, clock, _ = _store()
    code = store.issue(PROJECT)
    clock.advance(DOWN_CODE_TTL_SECONDS)
    assert store.consume(code, PROJECT) is False

    before, early_clock, _ = _store()
    early = before.issue(PROJECT)
    early_clock.advance(DOWN_CODE_TTL_SECONDS - 0.5)
    assert before.consume(early, PROJECT) is True

    late, late_clock, _ = _store()
    stale = late.issue(PROJECT)
    late_clock.advance(DOWN_CODE_TTL_SECONDS * 10)
    assert late.consume(stale, PROJECT) is False


def test_consume_respects_custom_ttl() -> None:
    """o `ttl` do construtor vale no lugar do padrão."""
    store, clock, _ = _store(ttl=10)
    code = store.issue(PROJECT)
    clock.advance(9.9)
    assert store.consume(code, PROJECT) is True
    other, other_clock, _ = _store(ttl=10)
    late = other.issue(PROJECT)
    other_clock.advance(10)
    assert other.consume(late, PROJECT) is False


def test_consume_with_other_project_is_false() -> None:
    """o código ligado ao projeto do preparo não vale para outro projeto."""
    store, _, _ = _store()
    code = store.issue(PROJECT)
    assert store.consume(code, OTHER_PROJECT) is False
    fresh, _, _ = _store()
    other = fresh.issue(OTHER_PROJECT)
    assert fresh.consume(other, PROJECT) is False


def test_new_issue_replaces_previous_of_same_project_only() -> None:
    """um novo `issue` do mesmo projeto invalida o anterior; o de outro projeto continua
    valendo (um registro por projeto)."""
    store, _, _ = _store()
    old = store.issue(PROJECT)
    other = store.issue(OTHER_PROJECT)
    new = store.issue(PROJECT)
    assert new != old
    assert store.consume(old, PROJECT) is False
    assert store.consume(new, PROJECT) is True
    assert store.consume(other, OTHER_PROJECT) is True


@pytest.mark.parametrize("invented", ["A" * 22, "codigoInventadoDown_00", "codigoDeTesteDown00002", ""],
                         ids=["letras", "inventado", "proximo_da_sequencia", "vazio"])
def test_invented_code_is_false(invented: str) -> None:
    """um código que nunca foi emitido é falso, com e sem registro do projeto; e não gasta o código
    emitido."""
    empty, _, _ = _store()
    assert empty.consume(invented, PROJECT) is False
    store, _, _ = _store()
    code = store.issue(PROJECT)
    assert invented != code
    assert store.consume(invented, PROJECT) is False
    assert store.consume(code, PROJECT) is True


def test_consume_compares_digests_with_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """a conferência compara os resumos `sha256` por `hmac.compare_digest` (tempo constante),
    qualquer que seja a forma do import (`hmac.compare_digest`, `secrets.compare_digest` ou o nome importado no
    módulo)."""
    seen: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    monkeypatch.setattr(secrets, "compare_digest", spy)
    monkeypatch.setattr(mcp_server, "compare_digest", spy, raising=False)
    store, _, _ = _store()
    code = store.issue(PROJECT)
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    assert store.consume(code, PROJECT) is True
    assert any(digest in (bytes(a), bytes(b)) for a, b in seen if isinstance(a, (bytes, bytearray))
               and isinstance(b, (bytes, bytearray))), "o resumo não passou por compare_digest"
    assert all(code not in (a, b) for a, b in seen), "o código em claro foi comparado no lugar do resumo"


def test_concurrent_consume_succeeds_once() -> None:
    """`consume` confere e marca numa só operação, sob a trava interna: com várias chamadas
    simultâneas do mesmo código, só uma é verdadeira."""
    store, _, _ = _store()
    code = store.issue(PROJECT)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=5)
        outcome = store.consume(code, PROJECT)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 8
    assert results.count(True) == 1
