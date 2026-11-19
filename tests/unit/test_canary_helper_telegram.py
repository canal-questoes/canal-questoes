"""Autoteste do detector canário para os remetentes do Telegram.

O detector (`tests/helpers/canary.py`) é o oráculo do canário do Telegram.
Se ele não achar uma grafia de um remetente canário, a varredura passa no vazio;
se acusar texto limpo, vira ruído; se os alvos do Telegram entrarem sem `prefixes`, os testes das pessoas canário
mudam de sentido. Estes testes conferem o próprio arnês e passam antes da implementação por
natureza. O autoteste das pessoas canário (`test_canary_helper.py`) continua valendo sem edição.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse

import pytest

from tests.helpers.bot_api import FIRST_BOT_ID, UPDATE_ID_BASE, make_user
from tests.helpers.canary import (
    ALL_PREFIXES,
    CANARY_PEOPLE,
    CANARY_TELEGRAM_USERS,
    BASE_PREFIXES,
    TARGETS,
    TG_FILISBINA,
    TG_PREFIXES,
    TG_TARGETS,
    TG_TEODORICO,
    TG_ZEFIRO,
    ZEFIRO,
    CanaryTelegramUser,
    find_canary,
    fold,
)

# ---------------------------------------------------------------------------------------------
# Alvos esperados, escritos à mão (independentes de `_tg_targets`)
# ---------------------------------------------------------------------------------------------

# (rótulo esperado, grafia original a procurar)
TG_EXPECTED: list[tuple[str, str]] = [
    ("tg-nome:Teodorico Maracanã Albuquerque", "Teodorico Maracanã Albuquerque"),
    ("tg-nome:Teodorico", "Teodorico"),
    ("tg-nome:Maracanã Albuquerque", "Maracanã Albuquerque"),
    ("tg-palavra:teodorico", "Teodorico"),
    ("tg-palavra:maracana", "Maracanã"),
    ("tg-palavra:albuquerque", "Albuquerque"),
    ("tg-username:teodorico_maracana_tg", "teodorico_maracana_tg"),
    ("tg-username:teodorico_maracana_tg", "@teodorico_maracana_tg"),
    ("tg-id:7300112233445", "7300112233445"),
    ("tg-nome:Filisbina", "Filisbina"),
    ("tg-palavra:filisbina", "Filisbina"),
    ("tg-id:7300556677889", "7300556677889"),
    ("tg-nome:Zéfiro Canário Quintela", "Zéfiro Canário Quintela"),
    ("tg-nome:Zéfiro", "Zéfiro"),
    ("tg-nome:Canário Quintela", "Canário Quintela"),
    ("tg-palavra:zefiro", "Zéfiro"),
    ("tg-palavra:canario", "Canário"),
    ("tg-palavra:quintela", "Quintela"),
    ("tg-username:zefiro_quintela_tg", "zefiro_quintela_tg"),
    ("tg-username:zefiro_quintela_tg", "@zefiro_quintela_tg"),
    ("tg-id:7300998877665", "7300998877665"),
]


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def _add_accents(text: str) -> str:
    """Acrescenta um acento agudo a toda vogal sem acento, em NFC."""
    out = []
    for ch in text:
        out.append(ch)
        if ch.lower() in "aeiou":
            out.append("́")
    return unicodedata.normalize("NFC", "".join(out))


VARIANTS = {
    "original": lambda s: s,
    "caixa_trocada": lambda s: s.swapcase(),
    "maiusculas": lambda s: s.upper(),
    "sem_acento": _strip_accents,
    "acento_acrescentado": _add_accents,
    "espacos_extras": lambda s: "\n  " + s.replace(" ", " \t  ") + "\t ",
    "nfd": lambda s: unicodedata.normalize("NFD", s),
    "percent_encoding": lambda s: urllib.parse.quote(s, safe=""),
    "percent_encoding_plus": lambda s: urllib.parse.quote_plus(s, safe=""),
    "url_com_consulta": lambda s: f"/v1/offerings?nome={urllib.parse.quote(s, safe='')}&limit=1",
    "json_escapado": lambda s: json.dumps({"detalhe": s}),
    "json_aninhado": lambda s: json.dumps({"a": {"b": [1, None, {"c": [s]}]}}),
    "json_aninhado_utf8": lambda s: json.dumps({"a": [{"b": s}]}, ensure_ascii=False),
    "json_como_chave": lambda s: json.dumps({s: 1}),
    "json_em_linha_de_log": lambda s: (
        "linha qualquer\n" + json.dumps({"event": "telegram.update", "extra": {"k": [s]}}) + "\nfim"),
    "combinado_nfd_caixa_chave_json": lambda s: json.dumps(
        {"k": {unicodedata.normalize("NFD", s.swapcase()): 0}}),
}


# ---------------------------------------------------------------------------------------------
# Remetentes canário
# ---------------------------------------------------------------------------------------------


def test_canary_telegram_users_shape() -> None:
    """Os três remetentes canário, com ids de 13 dígitos na faixa `73…`, distintos e
    fora das faixas da Bot API falsa; Teodorico completo, Filisbina sem sobrenome nem username,
    Zéfiro com o nome de uma pessoa canário cadastrada."""
    assert CANARY_TELEGRAM_USERS == (TG_TEODORICO, TG_FILISBINA, TG_ZEFIRO)
    assert TG_TEODORICO == CanaryTelegramUser(7300112233445, "Teodorico", "Maracanã Albuquerque",
                                              "teodorico_maracana_tg")
    assert TG_FILISBINA == CanaryTelegramUser(7300556677889, "Filisbina", None, None)
    assert TG_ZEFIRO == CanaryTelegramUser(7300998877665, "Zéfiro", "Canário Quintela",
                                           "zefiro_quintela_tg")
    assert f"{TG_ZEFIRO.first_name} {TG_ZEFIRO.last_name}" == ZEFIRO.name
    ids = [user.user_id for user in CANARY_TELEGRAM_USERS]
    assert len(set(ids)) == 3
    for user_id in ids:
        assert re.fullmatch(r"73\d{11}", str(user_id))
        assert not str(user_id).startswith(str(FIRST_BOT_ID)[:2])
        assert not str(user_id).startswith(str(UPDATE_ID_BASE)[:2])


def test_as_user_and_as_private_chat_have_bot_api_shape() -> None:
    """`as_user()` e `as_private_chat()` têm a forma do `User` e do `Chat` privado da Bot API
    (campos ausentes omitidos; `chat.id == from.id`), escrita à mão."""
    assert TG_TEODORICO.as_user() == {
        "id": 7300112233445, "is_bot": False, "first_name": "Teodorico",
        "last_name": "Maracanã Albuquerque", "username": "teodorico_maracana_tg",
        "language_code": "pt-br"}
    assert TG_TEODORICO.as_private_chat() == {
        "id": 7300112233445, "type": "private", "first_name": "Teodorico",
        "last_name": "Maracanã Albuquerque", "username": "teodorico_maracana_tg"}
    assert TG_FILISBINA.as_user() == {"id": 7300556677889, "is_bot": False, "first_name": "Filisbina",
                                      "language_code": "pt-br"}
    assert TG_FILISBINA.as_private_chat() == {"id": 7300556677889, "type": "private",
                                              "first_name": "Filisbina"}
    assert TG_ZEFIRO.as_user()["username"] == "zefiro_quintela_tg"
    assert TG_ZEFIRO.as_private_chat()["id"] == TG_ZEFIRO.as_user()["id"] == 7300998877665


def test_prefix_constants() -> None:
    """os prefixos das pessoas canário, do Telegram e todos (cada alvo pertence a um único prefixo)."""
    assert BASE_PREFIXES == ("nome", "palavra", "ra", "email", "email-local")
    assert TG_PREFIXES == ("tg-nome", "tg-palavra", "tg-username", "tg-id")
    assert ALL_PREFIXES == BASE_PREFIXES + TG_PREFIXES
    assert {label.split(":", 1)[0] for label, _ in TARGETS} == set(BASE_PREFIXES)
    assert {label.split(":", 1)[0] for label, _ in TG_TARGETS} == set(TG_PREFIXES)


def test_telegram_targets_are_exactly_the_hand_written_ones() -> None:
    """os rótulos dos alvos do Telegram são exatamente os escritos à mão acima, e nenhum deles
    está em `TARGETS` (o conjunto das pessoas canário não muda)."""
    assert {label for label, _ in TG_TARGETS} == {label for label, _ in TG_EXPECTED}
    assert not {label for label, _ in TG_TARGETS} & {label for label, _ in TARGETS}
    r2_people = {person.name for person in CANARY_PEOPLE}
    assert r2_people == {"Zéfiro Canário Quintela", "Úrsula Pintassilgo Bethânia", "Olímpio Sanhaçu Tavares"}


# ---------------------------------------------------------------------------------------------
# Sem `prefixes`: exatamente as pessoas canário
# ---------------------------------------------------------------------------------------------

TG_ONLY_TEXTS = {
    "teodorico": json.dumps({"from": TG_TEODORICO.as_user(), "chat": TG_TEODORICO.as_private_chat()},
                            ensure_ascii=False),
    "filisbina": json.dumps({"from": TG_FILISBINA.as_user()}),
    "username_com_arroba": "mensagem de @teodorico_maracana_tg",
    "ids": "chat 7300112233445, remetente 7300556677889",
    "nome_completo": "Teodorico Maracanã Albuquerque",
}


@pytest.mark.parametrize("name", sorted(TG_ONLY_TEXTS))
def test_without_prefixes_telegram_targets_are_not_searched(name: str) -> None:
    """sem `prefixes`, o detector procura só os alvos das pessoas canário; um texto que só tem dados dos
    remetentes Teodorico e Filisbina não é acusado (e é, com `TG_PREFIXES`: controle positivo)."""
    text = TG_ONLY_TEXTS[name]
    assert find_canary(text) == []
    assert find_canary(text, prefixes=BASE_PREFIXES) == []
    assert find_canary(text, prefixes=TG_PREFIXES) != []


def test_without_prefixes_zefiro_sender_hits_only_base_labels() -> None:
    """o remetente Zéfiro tem o nome de uma pessoa cadastrada; sem `prefixes`, só os rótulos das
    pessoas canário aparecem (as palavras), nunca `tg-*`."""
    text = json.dumps({"from": TG_ZEFIRO.as_user()})
    assert find_canary(text) == ["palavra:canario", "palavra:quintela", "palavra:zefiro"]
    assert find_canary(text, prefixes=BASE_PREFIXES) == find_canary(text)


@pytest.mark.parametrize(("label", "original"), TARGETS, ids=[lab for lab, _ in TARGETS])
def test_base_prefixes_equal_no_prefixes_on_base_targets(label: str, original: str) -> None:
    """`prefixes=BASE_PREFIXES` dá o mesmo resultado que sem `prefixes`, alvo a alvo das pessoas canário."""
    assert find_canary(original, prefixes=BASE_PREFIXES) == find_canary(original)
    assert label in find_canary(original)


# ---------------------------------------------------------------------------------------------
# Com `prefixes`: cada alvo em cada grafia
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize(("label", "original"), TG_EXPECTED,
                         ids=[f"{lab}|{orig}" for lab, orig in TG_EXPECTED])
def test_detector_finds_each_telegram_target_in_each_spelling(label: str, original: str,
                                                              variant: str) -> None:
    """com `TG_PREFIXES` e com `ALL_PREFIXES`, o detector acha cada alvo dos remetentes em caixa
    trocada, sem acento, com acento a mais, com espaços extras (inclusive NBSP), em NFD, em
    percent-encoding, dentro de JSON (escapado, aninhado, como chave) e numa linha JSON de log."""
    text = VARIANTS[variant](original)
    for prefixes in (TG_PREFIXES, ALL_PREFIXES):
        hits = find_canary(text, prefixes=prefixes)
        assert label in hits, f"{variant}: {label!r} não achado em {text!r} (achados: {hits})"


@pytest.mark.parametrize(
    ("text", "prefixes", "expected"),
    [
        ("Teodorico Maracanã Albuquerque", TG_PREFIXES, [
            "tg-nome:Maracanã Albuquerque", "tg-nome:Teodorico", "tg-nome:Teodorico Maracanã Albuquerque",
            "tg-palavra:albuquerque", "tg-palavra:maracana", "tg-palavra:teodorico"]),
        ("@TEODORICO_MARACANA_TG", TG_PREFIXES, [
            "tg-nome:Teodorico", "tg-palavra:maracana", "tg-palavra:teodorico",
            "tg-username:teodorico_maracana_tg"]),
        ("Filisbina", TG_PREFIXES, ["tg-nome:Filisbina", "tg-palavra:filisbina"]),
        ("7300556677889", TG_PREFIXES, ["tg-id:7300556677889"]),
        ("Zefiro", ALL_PREFIXES, ["palavra:zefiro", "tg-nome:Zéfiro", "tg-palavra:zefiro"]),
        ("7300112233445 e Teodorico", ("tg-id",), ["tg-id:7300112233445"]),
        ("@zefiro_quintela_tg", ("tg-username",), ["tg-username:zefiro_quintela_tg"]),
        ("Zéfiro Canário Quintela", ("nome",), ["nome:Zéfiro Canário Quintela"]),
    ],
    ids=["nome_teodorico", "username_teodorico", "filisbina", "id_filisbina", "zefiro_todos",
         "so_tg_id", "so_tg_username", "so_nome"],
)
def test_detector_exact_hits_with_prefixes(text: str, prefixes: tuple[str, ...],
                                           expected: list[str]) -> None:
    """saídas exatas e ordenadas, escritas à mão, com cada seleção de prefixos."""
    assert find_canary(text, prefixes=prefixes) == expected


def _update(sender: CanaryTelegramUser) -> dict:
    """Um update `message` com o remetente em `from`, `chat` e numa resposta citada."""
    return {
        "update_id": UPDATE_ID_BASE,
        "message": {
            "message_id": 1, "from": sender.as_user(), "chat": sender.as_private_chat(),
            "date": 1924992000, "text": "/ajuda",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
            "reply_to_message": {"message_id": 2, "from": sender.as_user(), "date": 1924991000,
                                 "chat": sender.as_private_chat(), "text": "o professor"},
        },
    }


@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["json_ascii", "json_utf8"])
def test_update_payload_of_each_sender_is_flagged(ensure_ascii: bool) -> None:
    """o JSON de um update com o remetente canário (como a Bot API o entregaria) é acusado com
    o nome, o sobrenome, as palavras, o username e o id; o nome inteiro não está contíguo no JSON."""
    teodorico = json.dumps(_update(TG_TEODORICO), ensure_ascii=ensure_ascii)
    assert find_canary(teodorico, prefixes=TG_PREFIXES) == [
        "tg-id:7300112233445", "tg-nome:Maracanã Albuquerque", "tg-nome:Teodorico",
        "tg-palavra:albuquerque", "tg-palavra:maracana", "tg-palavra:teodorico",
        "tg-username:teodorico_maracana_tg"]
    filisbina = json.dumps(_update(TG_FILISBINA), ensure_ascii=ensure_ascii)
    assert find_canary(filisbina, prefixes=ALL_PREFIXES) == [
        "tg-id:7300556677889", "tg-nome:Filisbina", "tg-palavra:filisbina"]
    zefiro = json.dumps(_update(TG_ZEFIRO), ensure_ascii=ensure_ascii)
    assert find_canary(zefiro, prefixes=ALL_PREFIXES) == [
        "palavra:canario", "palavra:quintela", "palavra:zefiro", "tg-id:7300998877665",
        "tg-nome:Canário Quintela", "tg-nome:Zéfiro", "tg-palavra:canario", "tg-palavra:quintela",
        "tg-palavra:zefiro", "tg-username:zefiro_quintela_tg"]


def test_json_escape_needs_decoding_for_telegram_names() -> None:
    """controle das variantes JSON: `Maracanã` escapado (`Maracan\\u00e3`) não casa no texto cru,
    só depois de decodificado."""
    raw = json.dumps(["Maracanã"])
    assert "\\u00e3" in raw
    assert "maracana" not in fold(raw)
    assert find_canary(raw, prefixes=TG_PREFIXES) == ["tg-palavra:maracana"]


# ---------------------------------------------------------------------------------------------
# Texto limpo não é acusado
# ---------------------------------------------------------------------------------------------

_NEUTRAL = make_user(7)

CLEAN_TEXTS = {
    "vazio": "",
    "remetente_neutro": json.dumps({"from": _NEUTRAL.as_user(), "chat": _NEUTRAL.as_private_chat()}),
    "bot_e_update_do_duble": json.dumps({"id": FIRST_BOT_ID, "is_bot": True, "first_name": "Bot de teste",
                                         "username": "cq_teste_0000_bot", "update_id": UPDATE_ID_BASE}),
    "quase_ids": "730011223344 e 7300112233440 e 7300556677880 e 7300998877660",
    "quase_nomes": "Teodoric, Maracan, Albuquerqu, Filisbin, Zefir, Canari e Quintel.",
    "quase_username": "teodoric_maracan_tg e zefir_quintel_tg",
    "textos_do_bot": ("Este é o bot do oferecimento Programação I (2031s1). Para participar, toque no "
                      "link de convite que você recebeu da coordenação. Esta conta do Telegram já está "
                      "vinculada a outra pessoa neste oferecimento. Fale com a coordenação."),
    "link_de_equipe": "https://t.me/cq_teste_0003_bot?start=AbCdEfGhIjKlMnOpQrStUv",
    "marca_de_pessoa": "Disciplina de [pessoa a3f9] no período 2031s1",
}


@pytest.mark.parametrize("name", sorted(CLEAN_TEXTS))
def test_detector_does_not_flag_clean_text_with_all_prefixes(name: str) -> None:
    """com `ALL_PREFIXES`, o detector não acusa texto limpo: remetentes neutros e ids do dublê,
    quase-ids (12 dígitos ou outro último dígito), quase-nomes, textos do bot e o link de equipe."""
    assert find_canary(CLEAN_TEXTS[name], prefixes=ALL_PREFIXES) == []


# ---------------------------------------------------------------------------------------------
# Uso incorreto de `prefixes`
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("prefixes", [(), [], ("tg-nome", "tg-desconhecido"), ("palavras",), "tg-id"],
                         ids=["tupla_vazia", "lista_vazia", "desconhecido", "quase_prefixo", "texto"])
def test_invalid_prefixes_are_test_errors(prefixes: object) -> None:
    """prefixo desconhecido, coleção vazia ou um texto no lugar da coleção é erro do teste
    (senão a varredura passaria no vazio), mesmo com texto vazio."""
    with pytest.raises(ValueError):
        find_canary("Teodorico", prefixes=prefixes)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        find_canary("", prefixes=prefixes)  # type: ignore[arg-type]
