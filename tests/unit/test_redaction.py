"""Redação de texto livre.

Saídas **exatas**, com a marca montada sempre por `marker(alias)`. A assinatura de `redact`
não recebe o token inicial: o caso do valor literal do token inicial fica em
`tests/integration/test_redaction_api.py`; aqui fica o padrão `cq_[A-Za-z0-9_-]{20,}`.
"""

from __future__ import annotations

import unicodedata

import pytest

from cq_server.redaction import KnownIdentity, redact
from tests.helpers.api import marker
from tests.helpers.canary import OLIMPIO, URSULA, ZEFIRO


def _identity(alias: str, full_name: str, ra: str | None = None, email: str | None = None) -> KnownIdentity:
    return KnownIdentity(alias=alias, full_name=full_name, ra=ra, email=email)


# Pessoas fictícias (tests/helpers/canary.py e nomes inventados).
ZEF = _identity("a1b2c3", ZEFIRO.name, ZEFIRO.ra, ZEFIRO.email)
URS = _identity("d4e5", URSULA.name, URSULA.ra, URSULA.email)
OLI = _identity("f6a7", OLIMPIO.name, OLIMPIO.ra, OLIMPIO.email)
CAIO = _identity("c0ffee", "Caio Prudente", "123456")
ANA = _identity("abc1", "Ana")

# 43 caracteres do alfabeto do segredo, sem dígitos de RA das pessoas acima.
SECRET_43 = "cq_" + ("aZ09_-Qx" * 6)[:43]
assert len(SECRET_43) == 46


# --- acentos, caixa e espaços; NBSP, hífen suave e ZWSP; ß/ss; ﬁ -----------------------------

@pytest.mark.parametrize("variant", [
    "ZÉFIRO CANÁRIO QUINTELA",
    "zefiro canario quintela",
    "Zéfiro   Canário\tQuintela",
    "Zéfiro\nCanário \t Quintela",
], ids=["caixa", "sem-acento", "espacos-e-tab", "quebra-de-linha"])
def test_name_ignores_case_accents_and_whitespace(variant: str) -> None:
    """Na comparação: caixa, acentos e espaços."""
    assert redact(f"Antes {variant} depois.", [ZEF]) == f"Antes {marker(ZEF.alias)} depois."


def test_text_outside_the_match_is_kept_verbatim() -> None:
    """Na troca: só o intervalo casado muda; o resto do original fica como está."""
    text = "Reunião com  ZEFIRO\tcanario\n QUINTELA hoje."
    assert redact(text, [ZEF]) == f"Reunião com  {marker(ZEF.alias)} hoje."


@pytest.mark.parametrize("variant", [
    "Zéfiro Canário Quintela",
    "Zé­firo Canário Quin­tela",
    "Zéfi​ro Cana​rio Quintela",
], ids=["nbsp", "hifen-suave", "zwsp"])
def test_name_with_nbsp_soft_hyphen_and_zero_width_space(variant: str) -> None:
    """Na comparação: NBSP vira espaço; hífen suave e ZWSP (Cf) são removidos."""
    assert redact(f"Antes {variant} depois.", [ZEF]) == f"Antes {marker(ZEF.alias)} depois."


def test_sharp_s_matches_double_s_both_ways() -> None:
    """Na comparação (`STRAUSS` e `Strauß`): `ß` e `ss` casam nos dois sentidos."""
    sharp = _identity("5eed", "Ilse Weißmann")
    double = _identity("5eed", "Ilse Weissmann")
    assert redact("Veio ILSE WEISSMANN.", [sharp]) == f"Veio {marker('5eed')}."
    assert redact("Veio Ilse Weißmann.", [double]) == f"Veio {marker('5eed')}."


def test_ligature_fi_matches() -> None:
    """Na comparação (NFKD): a ligadura `ﬁ` casa com `fi`."""
    assert redact("Veio Zeﬁro Canário Quintela.", [ZEF]) == f"Veio {marker(ZEF.alias)}."


# --- NFD, com o acento solto trocado -------------------------------------------------------------

def test_nfd_text_and_nfd_identity_match() -> None:
    """Na comparação: texto em NFD casa com o nome em NFC, e o contrário."""
    nfd_text = unicodedata.normalize("NFD", "Veio Zéfiro Canário Quintela.")
    assert nfd_text != "Veio Zéfiro Canário Quintela."
    assert redact(nfd_text, [ZEF]) == f"Veio {marker(ZEF.alias)}."
    nfd_identity = _identity(ZEF.alias, unicodedata.normalize("NFD", ZEF.full_name))
    assert redact("Veio Zéfiro Canário Quintela.", [nfd_identity]) == f"Veio {marker(ZEF.alias)}."


def test_nfd_with_swapped_detached_accent_is_covered() -> None:
    """Nas fronteiras: acentos soltos trocados casam, e o acento solto logo depois
    do último caractere casado entra no trecho trocado (não sobra marca solta)."""
    text = "Zèfiro Canãrio Quintelá chegou."
    assert redact(text, [ZEF]) == f"{marker(ZEF.alias)} chegou."
    trailing_cf = "Zéfiro Canário Quintela​́!"
    assert redact(trailing_cf, [ZEF]) == f"{marker(ZEF.alias)}!"


# --- fronteira (`Ana` em `banana`) -------------------------------------------------------------

def test_name_boundary_by_character_class() -> None:
    """Nas fronteiras: `Ana` não casa em `banana` nem em `Anabela`; casa sozinho e
    junto de dígito (classe diferente)."""
    assert redact("banana, Anabela e Ana.", [ANA]) == f"banana, Anabela e {marker('abc1')}."
    assert redact("ANA1 e bANA", [ANA]) == f"{marker('abc1')}1 e bANA"


def test_name_boundary_is_checked_on_normalized_text() -> None:
    """Nas fronteiras: a fronteira vale no texto normalizado (o hífen suave some e
    `Ana` encosta em letra)."""
    assert redact("Ana­bela chegou.", [ANA]) == "Ana­bela chegou."


def test_only_the_full_name_is_a_target() -> None:
    """No alvo: só o nome completo é alvo, não as palavras soltas."""
    text = "Zéfiro chegou; Canário Quintela saiu; Zéfiro Canário ficou."
    assert redact(text, [ZEF]) == text


# --- e-mail em outra caixa; e-mail colado a letra não casa ----------------------------------------

def test_email_in_other_case() -> None:
    """Maiúsculas no e-mail."""
    text = "Escreva para ZEFIRO.Canario@CANARIO.Example.COM."
    assert redact(text, [ZEF]) == f"Escreva para {marker(ZEF.alias)}."


@pytest.mark.parametrize("text", [
    "xzefiro.canario@canario.example.com",
    "zefiro.canario@canario.example.comx",
], ids=["letra-antes", "letra-depois"])
def test_email_glued_to_letter_does_not_match(text: str) -> None:
    """Nas fronteiras: o e-mail não casa encostado em letra."""
    assert redact(text, [ZEF]) == text


# --- `RA 123456.`; `1234567` ------------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("RA 123456.", f"RA {marker('c0ffee')}."),
    ("1234567", marker("c0ffee")),
    ("matrícula 0012345699 ok", f"matrícula {marker('c0ffee')} ok"),
    ("ab123456cd", f"ab{marker('c0ffee')}cd"),
    ("12345 e 23456", "12345 e 23456"),
], ids=["ra-com-ponto", "ra-mais-um-digito", "ra-no-meio", "ra-entre-letras", "so-parte-do-ra"])
def test_ra_expands_to_the_whole_digit_run(text: str, expected: str) -> None:
    """Nas fronteiras: o RA casa como subcadeia e a troca cobre toda a
    sequência de dígitos; parte do RA não casa."""
    assert redact(text, [CAIO]) == expected


# --- sobreposição parcial → uma marca --------------------------------------------------------

def test_partial_overlap_becomes_one_marker() -> None:
    """Na troca: ocorrências sobrepostas são unidas numa marca,
    com o apelido da que começa primeiro, qualquer que seja a ordem da lista."""
    jose = _identity("aaaa1", "José Ávila")
    avila = _identity("bbbb2", "Ávila Conceição")
    text = "Falei com José Ávila Conceição ontem."
    expected = f"Falei com {marker('aaaa1')} ontem."
    assert redact(text, [jose, avila]) == expected
    assert redact(text, [avila, jose]) == expected


def test_touching_occurrences_are_merged() -> None:
    """Na troca: intervalos que se encostam também são unidos (nome de uma pessoa
    colado ao RA de outra)."""
    text = f"{ZEFIRO.name}{URSULA.ra} saiu."
    assert redact(text, [URS, ZEF]) == f"{marker(ZEF.alias)} saiu."


# --- apelido: começa primeiro; a mais longa no empate; a primeira da lista no empate total ------

def test_tie_on_start_uses_the_longest_occurrence() -> None:
    """Na troca: mesmo início → o apelido da ocorrência mais longa."""
    short = _identity("a11a", "Ana Lima")
    long = _identity("b22b", "Ana Lima Souza")
    assert redact("Ana Lima Souza chegou.", [short, long]) == f"{marker('b22b')} chegou."
    assert redact("Ana Lima Souza chegou.", [long, short]) == f"{marker('b22b')} chegou."


def test_total_tie_uses_the_first_identity_in_the_list() -> None:
    """Na troca, `redact` desempata pela ordem de `identities`."""
    first = _identity("cafe01", "Rui Tomé")
    second = _identity("0bad", "Rui Tomé")
    assert redact("Rui Tomé chegou.", [first, second]) == f"{marker('cafe01')} chegou."
    assert redact("Rui Tomé chegou.", [second, first]) == f"{marker('0bad')} chegou."


def test_identities_may_be_any_iterable() -> None:
    """Qualquer iterável (`identities: Iterable[KnownIdentity]`): um gerador, percorrível uma vez, serve, e
    a ordem dele desempata."""
    first = _identity("cafe01", "Rui Tomé")
    second = _identity("0bad", "Rui Tomé")
    gen = (i for i in [second, first, CAIO])
    assert redact("Rui Tomé, RA 123456.", gen) == f"{marker('0bad')}, RA {marker('c0ffee')}."


# --- várias pessoas; `None` ignorado; texto inalterado -----------------------------------------

def test_several_people() -> None:
    """No alvo: nome, RA e e-mail de várias pessoas, cada um com a sua marca."""
    text = (f"{ZEFIRO.name} (RA {ZEFIRO.ra}), {URSULA.name} (RA {URSULA.ra}) e {OLIMPIO.email}.")
    expected = (f"{marker(ZEF.alias)} (RA {marker(ZEF.alias)}), {marker(URS.alias)} "
                f"(RA {marker(URS.alias)}) e {marker(OLI.alias)}.")
    assert redact(text, [ZEF, URS, OLI]) == expected


def test_none_ra_and_email_are_ignored() -> None:
    """`ra`/`email` `None` não viram alvo (nem o texto `None`)."""
    no_contact = _identity("f6a7", OLIMPIO.name)
    text = f"None, RA None; e-mail None; {OLIMPIO.name}."
    assert redact(text, [no_contact, URS]) == f"None, RA None; e-mail None; {marker('f6a7')}."


@pytest.mark.parametrize(("text", "identities"), [
    ("", [ZEF]),
    ("Olá, mundo  —  ﬁm­. Zé e 12345\t\n", [ZEF, CAIO, ANA]),
    (f"{ZEFIRO.name} {ZEFIRO.ra} {ZEFIRO.email}", []),
], ids=["vazio", "unicode-sem-alvo", "sem-identidades"])
def test_text_without_targets_is_unchanged(text: str, identities: list[KnownIdentity]) -> None:
    """sem nada a trocar, o texto sai idêntico (nem normalizado)."""
    assert redact(text, identities) == text


# --- marcas existentes protegidas ----------------------------------------------------------------

def test_existing_markers_are_protected_and_redaction_is_idempotent() -> None:
    """Com marcas existentes: um apelido com os dígitos do RA de outra pessoa
    (`123456beca` e RA `123456`) e um nome só de letras a–f (`Beca`) não são tocados dentro das
    marcas; `redact(redact(x)) == redact(x)`, com saída exata."""
    otavio = _identity("123456beca", "Otávio Ximenes")
    beca = _identity("d00d", "Beca")
    people = [CAIO, otavio, beca]
    text = "Caio Prudente (RA 123456) falou com Otávio Ximenes e Beca."
    first = redact(text, people)
    assert first == (f"{marker('c0ffee')} (RA {marker('c0ffee')}) falou com "
                     f"{marker('123456beca')} e {marker('d00d')}.")
    assert redact(first, people) == first


def test_markers_already_in_the_input_are_protected() -> None:
    """Com marcas existentes: `[pessoa <hex>]` e `[segredo]` do texto recebido não
    entram na busca, mesmo com pessoas chamadas `Pessoa` e `Segredo`; o resto é redigido."""
    people = [_identity("e1e1", "Pessoa"), _identity("e2e2", "Segredo"), CAIO,
              _identity("d00d", "Beca")]
    text = f"{marker('123456beca')} e [segredo] citaram Pessoa."
    assert redact(text, people) == f"{marker('123456beca')} e [segredo] citaram {marker('e1e1')}."


# --- segredos ----------------------------------------------------------------------------------

def test_token_secret_becomes_secret_marker() -> None:
    """Segredo → `[segredo]` (`cq_[A-Za-z0-9_-]{20,}`)."""
    assert redact(f"token {SECRET_43} vazou", []) == "token [segredo] vazou"
    assert redact(f"{ZEFIRO.name} usou {SECRET_43}.", [ZEF]) == f"{marker(ZEF.alias)} usou [segredo]."


def test_secret_pattern_length_boundary() -> None:
    """Nos segredos: `cq_` com 20 caracteres já é segredo; com 19, não."""
    assert redact("a cq_" + "x" * 20 + " b", []) == "a [segredo] b"
    assert redact("a cq_" + "x" * 19 + " b", []) == "a cq_" + "x" * 19 + " b"


# --- idempotência em 5 exemplos fixos -----------------------------------------------------------

@pytest.mark.parametrize(("text", "identities", "expected"), [
    ("Reunião com  ZEFIRO\tcanario\n QUINTELA hoje.", [ZEF],
     f"Reunião com  {marker('a1b2c3')} hoje."),
    ("Falei com José Ávila Conceição ontem.",
     [_identity("bbbb2", "Ávila Conceição"), _identity("aaaa1", "José Ávila")],
     f"Falei com {marker('aaaa1')} ontem."),
    ("RA 1234567 e 123456.", [CAIO], f"RA {marker('c0ffee')} e {marker('c0ffee')}."),
    (f"{URSULA.name}, {OLIMPIO.email}; {ZEFIRO.ra}", [ZEF, URS, OLI],
     f"{marker('d4e5')}, {marker('f6a7')}; {marker('a1b2c3')}"),
    (f"Zèfiro Canário Quintelá enviou {SECRET_43}", [ZEF],
     f"{marker('a1b2c3')} enviou [segredo]"),
], ids=["espacos", "sobreposicao", "ra", "varias-pessoas", "nfd-e-segredo"])
def test_idempotent_on_fixed_examples(text: str, identities: list[KnownIdentity], expected: str) -> None:
    """Com marcas existentes: a redação é idempotente (roda na gravação e na saída)."""
    once = redact(text, identities)
    assert once == expected
    assert redact(once, identities) == once
