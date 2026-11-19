"""Redação de texto livre.

`redact(text, identities, *, secrets=())` troca, no texto livre:
- todo segredo de token (`cq_[A-Za-z0-9_-]{20,}`), todo texto com a forma de um token de bot do
  Telegram (`BOT_TOKEN_PATTERN`) e cada valor literal de
  `secrets` (ex.: o token inicial configurado) por `[segredo]`;
- o nome completo, o RA e o e-mail de cada pessoa conhecida por `[pessoa <apelido>]`.

Algoritmo, com segredos e pessoas achados no mesmo texto **original**:
1. **Marcas existentes** (`[pessoa <hex 4–32>]` e `[segredo]`) ficam protegidas: não entram na
   busca de pessoas, o que torna a redação idempotente (ela roda na gravação e de novo na saída).
2. **Segredos:** as ocorrências dos dois padrões e dos valores literais, no texto todo.
3. **Normalização** caractere a caractere, guardando o índice de origem de cada caractere
   normalizado: NFKD; remoção das categorias `M*` e `Cf`; `casefold()`; de novo NFKD e a remoção;
   cada sequência de espaços em branco vira um espaço. Os alvos passam pela mesma normalização.
4. **Busca** de todas as ocorrências de todos os alvos, inclusive sobrepostas, em cada trecho entre
   marcas protegidas, também sobre os segredos:
   - nome e e-mail: com fronteira por classe no texto **normalizado** (uma ocorrência que começa ou
     termina em letra não encosta em letra; em dígito, não encosta em dígito);
   - RA: como subcadeia, e a troca cobre toda a sequência de dígitos que o contém;
   - um caractere de segredo não conta como vizinho, nem na fronteira nem na sequência de dígitos
     do RA: na saída ele vira parte de `[segredo]`, que não é letra nem dígito;
   - o intervalo original se estende sobre os caracteres removidos pela normalização (`M*`, `Cf`)
     logo depois do último caractere casado.
5. **União** de todos os intervalos, de segredos e de pessoas, que se sobrepõem ou se encostam. Cada
   intervalo unido do texto original vira `[segredo]` se contiver um segredo (um nome encostado num
   segredo some dentro dele) e, se não, `[pessoa <apelido>]`, com o apelido da ocorrência que começa
   primeiro (em empate, a mais longa; em empate total, a primeira pessoa na ordem de `identities`,
   que os chamadores passam em ordem crescente de `people.id`).

Buscar as pessoas no texto original, e não depois de trocar os segredos, evita que um segredo
esconda um nome: o padrão `cq_…` é guloso e engole um `-` ou `_` e o começo de um nome ou e-mail
colado a ele (em `cq_…-Zéfiro Canário Quintela` ele leva o `-Z`), e o resto sairia sem marca.

Sem nada a trocar, o texto sai idêntico (não normalizado).
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

SECRET_MARKER = "[segredo]"
SECRET_PATTERN = re.compile(r"cq_[A-Za-z0-9_-]{20,}")
# A forma do token de bot (`<id>:<segredo>`), com fronteira nos dois
# lados: nenhum caractere do alfabeto do token encosta nela (os que vêm à direita entram no segredo,
# que é guloso). É a mesma forma que o registro do bot aceita.
BOT_TOKEN_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_-])[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}(?![0-9A-Za-z_-])")
PROTECTED_PATTERN = re.compile(r"\[pessoa [0-9a-f]{4,32}\]|\[segredo\]")


@dataclass(frozen=True)
class KnownIdentity:
    alias: str
    full_name: str
    ra: str | None
    email: str | None


def person_marker(alias: str) -> str:
    """A marca que substitui a identidade de uma pessoa."""
    return f"[pessoa {alias}]"


# --------------------------------------------------------------------------------------------------
# Normalização
# --------------------------------------------------------------------------------------------------


def _dropped(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] == "M" or category == "Cf"


@functools.lru_cache(maxsize=4096)
def _fold_char(ch: str) -> str:
    """Normalização de um caractere: NFKD, sem M*/Cf, casefold, NFKD e sem M*/Cf de novo."""
    out: list[str] = []
    for c in unicodedata.normalize("NFKD", ch):
        if _dropped(c):
            continue
        for c2 in unicodedata.normalize("NFKD", c.casefold()):
            if not _dropped(c2):
                out.append(c2)
    return "".join(out)


def _normalize(text: str, start: int, end: int) -> tuple[str, list[int]]:
    """Normaliza `text[start:end]`; devolve o texto normalizado e o índice de origem de cada
    caractere dele (espaços em branco consecutivos viram um espaço, com a origem do primeiro)."""
    chars: list[str] = []
    origin: list[int] = []
    for index in range(start, end):
        for c in _fold_char(text[index]):
            if c.isspace():
                if chars and chars[-1] == " ":
                    continue
                c = " "
            chars.append(c)
            origin.append(index)
    return "".join(chars), origin


def _normalize_target(value: str) -> str:
    normalized, _ = _normalize(value, 0, len(value))
    return normalized.strip(" ")


# --------------------------------------------------------------------------------------------------
# Busca
# --------------------------------------------------------------------------------------------------


def _char_class(ch: str) -> str | None:
    if ch.isalpha():
        return "L"
    if ch.isdigit():
        return "D"
    return None


def _bounded(normalized: str, neighbor: list[bool], a: int, b: int) -> bool:
    """A ocorrência `normalized[a:b]` não encosta, do lado de fora, num caractere da mesma classe
    (`neighbor[i]` falso: o caractere `i` é de segredo e não conta como vizinho)."""
    first = _char_class(normalized[a])
    if first is not None and a > 0 and neighbor[a - 1] and _char_class(normalized[a - 1]) == first:
        return False
    last = _char_class(normalized[b - 1])
    if (last is not None and b < len(normalized) and neighbor[b]
            and _char_class(normalized[b]) == last):
        return False
    return True


def _occurrences(haystack: str, needle: str) -> Iterable[int]:
    index = haystack.find(needle)
    while index != -1:
        yield index
        index = haystack.find(needle, index + 1)


@dataclass(frozen=True)
class _Target:
    rank: int  # posição da pessoa em `identities` (desempate total)
    alias: str
    normalized: str
    is_ra: bool


def _targets(identities: Iterable[KnownIdentity]) -> list[_Target]:
    targets: list[_Target] = []
    for rank, identity in enumerate(identities):
        name = _normalize_target(identity.full_name) if identity.full_name else ""
        if name:
            targets.append(_Target(rank, identity.alias, name, False))
        if identity.email:
            email = _normalize_target(identity.email)
            if email:
                targets.append(_Target(rank, identity.alias, email, False))
        if identity.ra:
            ra = _normalize_target(identity.ra)
            if ra:
                targets.append(_Target(rank, identity.alias, ra, True))
    return targets


@dataclass(frozen=True)
class _Hit:
    start: int  # no texto original
    end: int
    rank: int
    alias: str


def _removed(text: str, index: int) -> bool:
    return _fold_char(text[index]) == ""


def _segment_hits(text: str, seg_start: int, seg_end: int, targets: list[_Target],
                  in_secret: bytearray | None) -> list[_Hit]:
    normalized, origin = _normalize(text, seg_start, seg_end)
    if not normalized:
        return []
    # Um caractere de segredo não conta como vizinho: na saída ele vira parte de `[segredo]`.
    if in_secret is None:
        neighbor = [True] * len(normalized)
    else:
        neighbor = [not in_secret[index] for index in origin]
    hits: list[_Hit] = []
    for target in targets:
        needle = target.normalized
        for a in _occurrences(normalized, needle):
            b = a + len(needle)
            if target.is_ra:
                while a > 0 and neighbor[a - 1] and normalized[a - 1].isdigit():
                    a -= 1
                while b < len(normalized) and neighbor[b] and normalized[b].isdigit():
                    b += 1
            elif not _bounded(normalized, neighbor, a, b):
                continue
            start = origin[a]
            end = origin[b - 1] + 1
            while end < seg_end and _removed(text, end):
                end += 1
            hits.append(_Hit(start, end, target.rank, target.alias))
    return hits


def _protected_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in PROTECTED_PATTERN.finditer(text)]


def _secret_spans(text: str, secrets: Iterable[str | None]) -> list[tuple[int, int]]:
    """Os intervalos de segredo do texto original: o padrão `cq_…`, a forma do token de bot e cada
    valor literal."""
    spans = [match.span() for match in SECRET_PATTERN.finditer(text)]
    spans.extend(match.span() for match in BOT_TOKEN_PATTERN.finditer(text))
    for secret in secrets:
        if secret:
            spans.extend((index, index + len(secret)) for index in _occurrences(text, secret))
    return spans


def _person_hits(text: str, targets: list[_Target], secret_spans: list[tuple[int, int]]) -> list[_Hit]:
    """As ocorrências de pessoas nos trechos entre marcas protegidas, também sobre os segredos."""
    if not targets or not text:
        return []
    in_secret: bytearray | None = None
    if secret_spans:
        in_secret = bytearray(len(text))
        for start, end in secret_spans:
            in_secret[start:end] = b"\x01" * (end - start)
    hits: list[_Hit] = []
    cursor = 0
    for start, end in [*_protected_spans(text), (len(text), len(text))]:
        if cursor < start:
            hits.extend(_segment_hits(text, cursor, start, targets, in_secret))
        cursor = end
    return hits


# --------------------------------------------------------------------------------------------------
# Troca
# --------------------------------------------------------------------------------------------------


def _replace(text: str, hits: list[_Hit], secret_spans: list[tuple[int, int]]) -> str:
    """Une os intervalos de segredos e de pessoas que se sobrepõem ou se encostam e troca cada
    união: por `[segredo]` se contiver um segredo; se não, pela marca da primeira ocorrência."""
    if not hits and not secret_spans:
        return text
    hits.sort(key=lambda h: (h.start, -(h.end - h.start), h.rank))
    spans = [(hit.start, hit.end, person_marker(hit.alias)) for hit in hits]
    spans.extend((start, end, SECRET_MARKER) for start, end in secret_spans)
    # Ordenação estável: no mesmo início, as pessoas guardam a ordem de desempate acima.
    spans.sort(key=lambda span: span[0])

    # Uma marca protegida que encosta num segredo ou numa pessoa continua como está: ela não entra
    # na união.
    groups: list[tuple[int, int, str]] = []  # (início, fim, marca)
    for start, end, mark in spans:
        if groups and start <= groups[-1][1]:
            group_start, group_end, group_mark = groups[-1]
            merged_mark = SECRET_MARKER if mark == SECRET_MARKER else group_mark
            groups[-1] = (group_start, max(group_end, end), merged_mark)
        else:
            groups.append((start, end, mark))

    pieces: list[str] = []
    cursor = 0
    for start, end, mark in groups:
        pieces.append(text[cursor:start])
        pieces.append(mark)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redact(text: str, identities: Iterable[KnownIdentity], *,
           secrets: Iterable[str | None] = ()) -> str:
    """Redige `text`.

    - `identities`: as pessoas conhecidas, em ordem crescente de `people.id` (a ordem desempata);
      qualquer iterável serve, percorrido uma vez.
    - `secrets`: valores literais a trocar por `[segredo]` além dos padrões `cq_…` e do token de
      bot (ex.: o token inicial configurado, `settings.admin_token`); `None` e vazios são
      ignorados.
    """
    targets = _targets(identities)
    secret_spans = _secret_spans(text, secrets)
    hits = _person_hits(text, targets, secret_spans)
    return _replace(text, hits, secret_spans)
