"""Autoteste do detector canário e do detector de segredos.

O detector (`tests/helpers/canary.py`) é o oráculo do canário. Se ele não achar uma
grafia, o canário passa no vazio; se acusar texto limpo, o canário vira ruído. Estes testes conferem
o próprio arnês e passam antes da implementação por natureza.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.parse

import pytest

from tests.helpers.api import ADMIN_TOKEN, marker
from tests.helpers.canary import (
    CANARY_PEOPLE,
    OLIMPIO,
    URSULA,
    ZEFIRO,
    find_canary,
    find_secrets,
    fold,
)
from tests.helpers.secrets import SECRETS, all_secrets, remember_secret

# ---------------------------------------------------------------------------------------------
# Alvos e variantes
# ---------------------------------------------------------------------------------------------


def _targets() -> list[tuple[str, str]]:
    """(rótulo esperado, grafia original) de cada alvo, pela regra das identidades canário.

    Nome completo, cada palavra do nome com 5 letras ou mais, RA, e-mail e parte local do e-mail.
    """
    out: list[tuple[str, str]] = []
    for person in CANARY_PEOPLE:
        out.append((f"nome:{person.name}", person.name))
        for word in person.name.split(" "):
            if len(fold(word)) >= 5:
                out.append((f"palavra:{fold(word)}", word))
        if person.ra is not None:
            out.append((f"ra:{person.ra}", person.ra))
        if person.email is not None:
            out.append((f"email:{person.email}", person.email))
            out.append((f"email-local:{person.email}", person.email.split("@")[0]))
    return out


TARGETS = _targets()


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def _add_accents(text: str) -> str:
    """Acrescenta um acento agudo a toda vogal (ex.: `Quintela` → `Quíntélá`), em NFC."""
    out = []
    for ch in text:
        out.append(ch)
        if ch.lower() in "aeiou":
            out.append("́")
    return unicodedata.normalize("NFC", "".join(out))


def _extra_spaces(text: str) -> str:
    inner = text.replace(" ", "  \t  ")
    return f"\n   {inner}\t  "


VARIANTS = {
    "original_nfc_com_e_composto": lambda s: s,
    "caixa_trocada": lambda s: s.swapcase(),
    "maiusculas": lambda s: s.upper(),
    "sem_acento": _strip_accents,
    "acento_acrescentado": _add_accents,
    "espacos_extras": _extra_spaces,
    "nfd": lambda s: unicodedata.normalize("NFD", s),
    "percent_encoding": lambda s: urllib.parse.quote(s, safe=""),
    "percent_encoding_plus": lambda s: urllib.parse.quote_plus(s, safe=""),
    "url_com_consulta": lambda s: f"/v1/people?nome={urllib.parse.quote(s, safe='')}&limit=1",
    "json_escapado_u00e9": lambda s: json.dumps({"detalhe": s}),
    "json_aninhado": lambda s: json.dumps({"a": {"b": [1, None, {"c": [s]}]}}),
    "json_aninhado_utf8": lambda s: json.dumps({"a": [{"b": s}]}, ensure_ascii=False),
    "json_como_chave": lambda s: json.dumps({s: 1}),
    "json_chave_aninhada": lambda s: json.dumps({"externo": [{"meio": {s: True}}]}),
    "json_em_linha_de_log": lambda s: (
        "linha de texto qualquer\n" + json.dumps({"event": "x", "extra": {"k": [s]}}) + "\nfim"),
    "combinado_nfd_caixa_chave_json": lambda s: json.dumps(
        {"k": {unicodedata.normalize("NFD", s.swapcase()): 0}}),
}


# ---------------------------------------------------------------------------------------------
# Identidades canário (forma combinada)
# ---------------------------------------------------------------------------------------------


def test_canary_people_shape() -> None:
    """As três pessoas canário têm a forma combinada (Zéfiro completo, Úrsula sem e-mail,
    Olímpio sem RA; RA de 10 dígitos; e-mails em `canario.example.com`; nomes com acento e
    maiúsculas) e `payload()` omite os campos ausentes."""
    assert CANARY_PEOPLE == (ZEFIRO, URSULA, OLIMPIO)
    assert ZEFIRO.ra == "9900112233"
    assert ZEFIRO.email is not None and URSULA.ra is not None and OLIMPIO.email is not None
    assert URSULA.email is None
    assert OLIMPIO.ra is None
    for person in CANARY_PEOPLE:
        assert person.name != _strip_accents(person.name), "nome canário sem acento"
        assert any(c.isupper() for c in person.name)
        if person.ra is not None:
            assert re.fullmatch(r"\d{10}", person.ra)
        if person.email is not None:
            assert person.email.endswith("@canario.example.com")
    assert ZEFIRO.payload() == {"name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": ZEFIRO.email}
    assert URSULA.payload() == {"name": URSULA.name, "ra": URSULA.ra}
    assert OLIMPIO.payload() == {"name": OLIMPIO.name, "email": OLIMPIO.email}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ZEFIRO.name, ["nome:Zéfiro Canário Quintela", "palavra:canario", "palavra:quintela",
                       "palavra:zefiro"]),
        (URSULA.name, ["nome:Úrsula Pintassilgo Bethânia", "palavra:bethania", "palavra:pintassilgo",
                       "palavra:ursula"]),
        (OLIMPIO.name, ["nome:Olímpio Sanhaçu Tavares", "palavra:olimpio", "palavra:sanhacu",
                        "palavra:tavares"]),
        (ZEFIRO.email, ["email-local:zefiro.canario@canario.example.com",
                        "email:zefiro.canario@canario.example.com", "palavra:canario",
                        "palavra:zefiro"]),
    ],
    ids=["nome_zefiro", "nome_ursula", "nome_olimpio", "email_zefiro"],
)
def test_detector_covers_every_target_category(text: str, expected: list[str]) -> None:
    """O detector procura o nome completo, cada palavra de 5 letras ou mais, o e-mail e a
    parte local (saída exata e ordenada, escrita à mão, independente de `_targets`)."""
    assert find_canary(text) == expected


# ---------------------------------------------------------------------------------------------
# O detector acha cada alvo em cada grafia
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize(("label", "original"), TARGETS, ids=[lab for lab, _ in TARGETS])
def test_detector_finds_each_target_in_each_spelling(label: str, original: str, variant: str) -> None:
    """o detector acha cada alvo em caixa trocada, sem acento, com acento a mais, com espaços
    extras (inclusive NBSP), em NFD, em NFC com `é` composto e escapado como `\\u00e9`, em
    percent-encoding, dentro de JSON aninhado, como chave de JSON e numa linha JSON de log."""
    text = VARIANTS[variant](original)
    hits = find_canary(text)
    assert label in hits, f"{variant}: {label!r} não achado em {text!r} (achados: {hits})"


def test_json_escape_actually_needs_decoding() -> None:
    """controle das variantes JSON: o nome escapado (`Z\\u00e9firo`) não casa no texto cru, só
    depois de decodificado; logo, o teste acima exercita a decodificação de JSON."""
    raw = json.dumps(["Zéfiro"])
    assert "\\u00e9" in raw
    assert "zefiro" not in fold(raw)
    assert find_canary(raw) == ["palavra:zefiro"]
    as_key = json.dumps({"Zéfiro": 1})
    assert "zefiro" not in fold(as_key)
    assert find_canary(as_key) == ["palavra:zefiro"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9900112233", ["ra:9900112233"]),
        ("Quintela", ["palavra:quintela"]),
        ("RA: 9900445566.", ["ra:9900445566"]),
        ("PINTASSILGO", ["palavra:pintassilgo"]),
        ("contato: olimpio.sanhacu@canario.example.com.", sorted([
            f"email:{OLIMPIO.email}", f"email-local:{OLIMPIO.email}", "palavra:olimpio",
            "palavra:sanhacu", "palavra:canario"])),
    ],
    ids=["ra", "palavra", "ra_em_frase", "palavra_maiuscula", "email_em_frase"],
)
def test_detector_exact_hits(text: str, expected: list[str]) -> None:
    """saídas exatas do detector em casos escritos à mão (a lista vem ordenada)."""
    assert find_canary(text) == expected


# ---------------------------------------------------------------------------------------------
# Texto limpo não é acusado
# ---------------------------------------------------------------------------------------------

CLEAN_TEXTS = {
    "vazio": "",
    "marca_abcd": "[pessoa abcd]",
    "marca_helper": marker("abcd"),
    "marcas_em_frase": f"Rótulo de {marker('a3f9')} e {marker('b7c1e')}: revisão.",
    "marca_percent": urllib.parse.quote(marker("abcd"), safe=""),
    "mensagens_do_contrato": "Pedido inválido. Token ausente, inválido ou revogado. Recurso não encontrado.",
    "envelope_de_erro": json.dumps({"error": {"code": "invalid_request", "message": "Pedido inválido.",
                                              "fields": [{"field": "email", "reason": "invalid_format"}]}},
                                   ensure_ascii=False),
    "pessoa_publica": json.dumps({"pseudo_id": "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b", "alias": "3f2a",
                                  "created_at": "2031-01-01T00:00:00Z"}),
    "linha_de_log": json.dumps({"level": "INFO", "logger": "cq_server.api", "event": "request",
                                "method": "GET", "route": "/v1/people/{ref}", "status": 200}),
    "quase_ra": "RA 990011223 e 9900112234",
    "quase_nomes": "Zefir, Canari e Quintel; Ursul e Bethan; Olimp, Sanhac e Tavare.",
    "palavras_curtas": "Ana, RA, e-mail, nome, rótulo, tokens, eventos.",
}


@pytest.mark.parametrize("name", sorted(CLEAN_TEXTS))
def test_detector_does_not_flag_clean_text(name: str) -> None:
    """o detector não acusa texto limpo, marcas `[pessoa abcd]` nem quase-alvos."""
    assert find_canary(CLEAN_TEXTS[name]) == []


# ---------------------------------------------------------------------------------------------
# Detector de segredos
# ---------------------------------------------------------------------------------------------

SECRET = "cq_" + "Ab3-_x9QzK" * 4 + "Zz1"


def _secret_label(secret: str) -> str:
    return f"segredo:{secret[:6]}…"


def _digest_label(secret: str) -> str:
    return f"resumo:{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:8]}…"


def test_secret_fixture_has_contract_format() -> None:
    """o segredo de teste tem o formato dos segredos da API (`cq_` + 43 caracteres)."""
    assert re.fullmatch(r"cq_[A-Za-z0-9_-]{43}", SECRET)


@pytest.mark.parametrize(
    ("text_of", "expected_of"),
    [
        (lambda s: f"Authorization: Bearer {s}", lambda s: [_secret_label(s)]),
        (lambda s: json.dumps({"a": [{"secret": s}]}), lambda s: [_secret_label(s)]),
        (lambda s: json.dumps({s: "valor"}), lambda s: [_secret_label(s)]),
        (lambda s: "resumo " + hashlib.sha256(s.encode()).hexdigest(), lambda s: [_digest_label(s)]),
        (lambda s: json.dumps({"x": {"secret_sha256": hashlib.sha256(s.encode()).hexdigest()}}),
         lambda s: [_digest_label(s)]),
        (lambda s: s + " " + hashlib.sha256(s.encode()).hexdigest(),
         lambda s: sorted([_secret_label(s), _digest_label(s)])),
    ],
    ids=["cru", "json_valor", "json_chave", "resumo_cru", "resumo_json", "segredo_e_resumo"],
)
def test_find_secrets_finds_secret_and_digest(text_of, expected_of) -> None:
    """`find_secrets` acha o segredo e o seu SHA-256 hex, no texto cru e dentro de JSON."""
    assert find_secrets(text_of(SECRET), [SECRET]) == expected_of(SECRET)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Token ausente, inválido ou revogado.",
        SECRET[:-1],
        hashlib.sha256(SECRET[:-1].encode()).hexdigest(),
        json.dumps({"token_id": "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b", "label": "rotulo"}),
    ],
    ids=["vazio", "mensagem", "prefixo_do_segredo", "resumo_de_outro_texto", "token_publico"],
)
def test_find_secrets_does_not_flag_clean_text(text: str) -> None:
    """`find_secrets` não acusa texto limpo, um prefixo do segredo nem o resumo de outro texto."""
    assert find_secrets(text, [SECRET]) == []


def test_secret_registry_lists_admin_token_and_remembered() -> None:
    """`all_secrets()` traz o token inicial e os segredos registrados, sem repetição; a
    fixture autouse começa cada teste com o registro vazio."""
    assert SECRETS == []
    assert all_secrets() == [ADMIN_TOKEN]
    remember_secret(SECRET)
    remember_secret(SECRET)
    assert all_secrets() == [ADMIN_TOKEN, SECRET]
    assert all_secrets("extra-sentinela") == [ADMIN_TOKEN, SECRET, "extra-sentinela"]
    assert find_secrets(f"x {ADMIN_TOKEN} y", all_secrets()) == [_secret_label(ADMIN_TOKEN)]
