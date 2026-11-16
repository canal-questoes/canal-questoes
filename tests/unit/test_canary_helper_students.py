"""Autoteste do detector canário para os alunos canário e dos detectores de segredo do vínculo do
aluno.

O detector (`tests/helpers/canary.py`) é o oráculo do canário do vínculo do aluno.
Se ele não achar uma grafia de um aluno canário, a varredura passa no vazio; se os alvos de
aluno se cruzarem com os já existentes, as exceções de um viram ruído no outro e os testes já existentes mudam
de sentido. Por isso, além das grafias, este arquivo confere a **detecção cruzada**: nenhum texto-alvo
de aluno é acusado sem `prefixes` nem com `ALL_PREFIXES`, e nenhum alvo já existente é acusado com
`STUDENT_PREFIXES`. A segunda parte confere `mail_secrets()` e o detector genérico
de código de convite (`find_invite_codes`). Estes testes conferem o próprio arnês e passam antes da
implementação por natureza. Os autotestes das pessoas canário (`test_canary_helper.py`) e dos
remetentes do Telegram (`test_canary_helper_telegram.py`) continuam valendo sem edição.
"""

from __future__ import annotations

import base64
import email.base64mime
import hashlib
import json
import re
import smtplib
import unicodedata
import urllib.parse
from dataclasses import dataclass

import email_validator
import pytest

from tests.helpers.api import ADMIN_TOKEN
from tests.helpers.bot_api import FIRST_BOT_ID, UPDATE_ID_BASE, make_user
from tests.helpers.canary import (
    ALL_PREFIXES,
    CANARY_PEOPLE,
    CANARY_STUDENT_ACCOUNTS,
    CANARY_STUDENTS,
    CANARY_TELEGRAM_USERS,
    BASE_PREFIXES,
    STUDENT_A,
    STUDENT_B,
    STUDENT_PREFIXES,
    STUDENT_TARGETS,
    TARGETS,
    TG_PREFIXES,
    TG_STUDENT_A1,
    TG_STUDENT_A2,
    TG_STUDENT_B,
    TG_TARGETS,
    CanaryStudent,
    CanaryTelegramUser,
    find_canary,
    find_secrets,
    fold,
)
from tests.helpers.secrets import (
    INVITE_CODE_LENGTH,
    SECRETS,
    all_secrets,
    find_invite_codes,
    invite_code_digest,
    invite_code_label,
    mail_secrets,
)

# =============================================================================================
# Parte 1: alunos canário
# =============================================================================================

# (rótulo esperado, grafia original a procurar), escritos à mão (independentes de `_student_targets`).
STUDENT_EXPECTED: list[tuple[str, str]] = [
    # Aluno A: a linha da lista.
    ("aluno-nome:Leocádia Jurubeba Tabatinga", "Leocádia Jurubeba Tabatinga"),
    ("aluno-palavra:leocadia", "Leocádia"),
    ("aluno-palavra:jurubeba", "Jurubeba"),
    ("aluno-palavra:tabatinga", "Tabatinga"),
    ("aluno-ra:8812047356", "8812047356"),
    ("aluno-email:Leocadia.JURUBEBA@Jequitiba.Example.ORG", "Leocadia.JURUBEBA@Jequitiba.Example.ORG"),
    ("aluno-email:Leocadia.JURUBEBA@Jequitiba.Example.ORG", "leocadia.jurubeba@jequitiba.example.org"),
    ("aluno-email-local:Leocadia.JURUBEBA@Jequitiba.Example.ORG", "Leocadia.JURUBEBA"),
    ("aluno-email-dominio:Leocadia.JURUBEBA@Jequitiba.Example.ORG", "Jequitiba.Example.ORG"),
    # Aluno A: 1ª conta do Telegram.
    ("aluno-tg-nome:Ubirajara Coivara Jacarandá", "Ubirajara Coivara Jacarandá"),
    ("aluno-tg-nome:Ubirajara", "Ubirajara"),
    ("aluno-tg-nome:Coivara Jacarandá", "Coivara Jacarandá"),
    ("aluno-tg-palavra:ubirajara", "Ubirajara"),
    ("aluno-tg-palavra:coivara", "Coivara"),
    ("aluno-tg-palavra:jacaranda", "Jacarandá"),
    ("aluno-tg-username:ubirajara_coivara_tg", "ubirajara_coivara_tg"),
    ("aluno-tg-username:ubirajara_coivara_tg", "@ubirajara_coivara_tg"),
    ("aluno-tg-id:7561203948571", "7561203948571"),
    # Aluno A: 2ª conta do Telegram.
    ("aluno-tg-nome:Anacleta Mandacaru Piracema", "Anacleta Mandacaru Piracema"),
    ("aluno-tg-nome:Anacleta", "Anacleta"),
    ("aluno-tg-nome:Mandacaru Piracema", "Mandacaru Piracema"),
    ("aluno-tg-palavra:anacleta", "Anacleta"),
    ("aluno-tg-palavra:mandacaru", "Mandacaru"),
    ("aluno-tg-palavra:piracema", "Piracema"),
    ("aluno-tg-username:anacleta_piracema_tg", "anacleta_piracema_tg"),
    ("aluno-tg-username:anacleta_piracema_tg", "@anacleta_piracema_tg"),
    ("aluno-tg-id:7568837201946", "7568837201946"),
    # Aluno B: a linha da lista.
    ("aluno-nome:Genésio Urutau Paraguaçu", "Genésio Urutau Paraguaçu"),
    ("aluno-palavra:genesio", "Genésio"),
    ("aluno-palavra:urutau", "Urutau"),
    ("aluno-palavra:paraguacu", "Paraguaçu"),
    ("aluno-ra:8826193407", "8826193407"),
    ("aluno-email:GENESIO.Urutau@Buritizal.Example.NET", "GENESIO.Urutau@Buritizal.Example.NET"),
    ("aluno-email:GENESIO.Urutau@Buritizal.Example.NET", "genesio.urutau@buritizal.example.net"),
    ("aluno-email-local:GENESIO.Urutau@Buritizal.Example.NET", "GENESIO.Urutau"),
    ("aluno-email-dominio:GENESIO.Urutau@Buritizal.Example.NET", "Buritizal.Example.NET"),
    # Aluno B: a conta do Telegram.
    ("aluno-tg-nome:Bartolomeu Guaxinim Caraúna", "Bartolomeu Guaxinim Caraúna"),
    ("aluno-tg-nome:Bartolomeu", "Bartolomeu"),
    ("aluno-tg-nome:Guaxinim Caraúna", "Guaxinim Caraúna"),
    ("aluno-tg-palavra:bartolomeu", "Bartolomeu"),
    ("aluno-tg-palavra:guaxinim", "Guaxinim"),
    ("aluno-tg-palavra:carauna", "Caraúna"),
    ("aluno-tg-username:bartolomeu_carauna_tg", "bartolomeu_carauna_tg"),
    ("aluno-tg-username:bartolomeu_carauna_tg", "@bartolomeu_carauna_tg"),
    ("aluno-tg-id:7574419302658", "7574419302658"),
]

# Prefixos que vêm da lista da turma (exceção em `identities`) e os que vêm das contas do Telegram.
ROSTER_PREFIXES = ("aluno-nome", "aluno-palavra", "aluno-ra", "aluno-email", "aluno-email-local",
                   "aluno-email-dominio")
ACCOUNT_PREFIXES = ("aluno-tg-nome", "aluno-tg-palavra", "aluno-tg-username", "aluno-tg-id")


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
    "espacos_extras": lambda s: "\n  " + s.replace(" ", " \t  ") + "\t ",
    "nfd": lambda s: unicodedata.normalize("NFD", s),
    "percent_encoding": lambda s: urllib.parse.quote(s, safe=""),
    "percent_encoding_plus": lambda s: urllib.parse.quote_plus(s, safe=""),
    "url_com_consulta": lambda s: f"/v1/offerings?nome={urllib.parse.quote(s, safe='')}&limit=1",
    "json_escapado": lambda s: json.dumps({"detalhe": s}),
    "json_aninhado": lambda s: json.dumps({"a": {"b": [1, None, {"c": [s]}]}}),
    "json_aninhado_utf8": lambda s: json.dumps({"a": [{"b": s}]}, ensure_ascii=False),
    "json_como_chave": lambda s: json.dumps({s: 1}),
    "json_em_linha_de_log": lambda s: (
        "linha qualquer\n" + json.dumps({"event": "invite.email", "extra": {"k": [s]}}) + "\nfim"),
    "combinado_nfd_caixa_chave_json": lambda s: json.dumps(
        {"k": {unicodedata.normalize("NFD", s.swapcase()): 0}}),
}


def _student_texts() -> dict[str, str]:
    """Todo texto-alvo de aluno, na grafia original: nome, palavras de 5+ letras, RA, e-mail, parte
    local, domínio, e de cada conta o nome, o sobrenome, os dois juntos, as palavras, o `username`, o
    `@username`, o id (também `json.dumps(id)`) e os objetos `User` e `Chat` da Bot API; e a linha
    da lista em JSON."""
    texts: dict[str, str] = {}
    for tag, student in (("a", STUDENT_A), ("b", STUDENT_B)):
        local, domain = student.email.split("@")
        texts[f"{tag}_nome"] = student.name
        for word in student.name.split(" "):
            texts[f"{tag}_palavra_{fold(word)}"] = word
        texts[f"{tag}_ra"] = student.ra
        texts[f"{tag}_email"] = student.email
        texts[f"{tag}_email_local"] = local
        texts[f"{tag}_email_dominio"] = domain
        texts[f"{tag}_linha_json"] = json.dumps(student.row())
        texts[f"{tag}_linha_json_utf8"] = json.dumps(student.row(), ensure_ascii=False)
        for index, account in enumerate(student.accounts, start=1):
            key = f"{tag}{index}_tg"
            assert account.last_name is not None and account.username is not None
            texts[f"{key}_nome_completo"] = account.full_name
            texts[f"{key}_nome"] = account.first_name
            texts[f"{key}_sobrenome"] = account.last_name
            for word in account.full_name.split(" "):
                texts[f"{key}_palavra_{fold(word)}"] = word
            texts[f"{key}_username"] = account.username
            texts[f"{key}_arroba_username"] = f"@{account.username}"
            texts[f"{key}_id"] = str(account.user_id)
            texts[f"{key}_id_json"] = json.dumps(account.user_id)
            texts[f"{key}_id_json_objeto"] = json.dumps({"chat_id": account.user_id})
            texts[f"{key}_user"] = json.dumps(account.as_user(), ensure_ascii=False)
            texts[f"{key}_chat"] = json.dumps(account.as_private_chat())
    return texts


STUDENT_TEXTS = _student_texts()


def _existing_target_texts() -> dict[str, str]:
    """Todo alvo já existente, na grafia original e na forma dobrada de `TARGETS` e `TG_TARGETS`."""
    texts: dict[str, str] = {}
    for label, target in TARGETS + TG_TARGETS:
        texts[f"alvo|{label}"] = target
    for index, person in enumerate(CANARY_PEOPLE):
        texts[f"pessoa{index}_nome"] = person.name
        texts[f"pessoa{index}_json"] = json.dumps(person.payload())
        if person.ra is not None:
            texts[f"pessoa{index}_ra"] = person.ra
        if person.email is not None:
            texts[f"pessoa{index}_email"] = person.email
            texts[f"pessoa{index}_email_local"] = person.email.split("@")[0]
            texts[f"pessoa{index}_email_dominio"] = person.email.split("@")[1]
    for index, user in enumerate(CANARY_TELEGRAM_USERS):
        texts[f"tg{index}_user"] = json.dumps(user.as_user(), ensure_ascii=False)
        texts[f"tg{index}_chat"] = json.dumps(user.as_private_chat())
        texts[f"tg{index}_id_json"] = json.dumps(user.user_id)
        if user.username is not None:
            texts[f"tg{index}_arroba_username"] = f"@{user.username}"
    return texts


EXISTING_TARGET_TEXTS = _existing_target_texts()


# ---------------------------------------------------------------------------------------------
# Forma dos alunos canário
# ---------------------------------------------------------------------------------------------


def test_canary_students_shape() -> None:
    """2 alunos canário, A com 2 contas do Telegram e B com 1; nome com
    acento e maiúsculas, RA de 10 dígitos, e-mail com maiúsculas na parte local e no domínio, em
    `example.*` (fora de `canario.example.com`), aceito pelas regras da API base e ASCII (o adaptador SMTP
    não tem caminho SMTPUTF8); cada conta com nome, sobrenome e `@username`, id de 13 dígitos na faixa
    `75…`, fora das faixas da Bot API falsa."""
    assert CANARY_STUDENTS == (STUDENT_A, STUDENT_B)
    assert STUDENT_A == CanaryStudent("Leocádia Jurubeba Tabatinga", "8812047356",
                                      "Leocadia.JURUBEBA@Jequitiba.Example.ORG",
                                      (TG_STUDENT_A1, TG_STUDENT_A2))
    assert STUDENT_B == CanaryStudent("Genésio Urutau Paraguaçu", "8826193407",
                                      "GENESIO.Urutau@Buritizal.Example.NET", (TG_STUDENT_B,))
    assert TG_STUDENT_A1 == CanaryTelegramUser(7561203948571, "Ubirajara", "Coivara Jacarandá",
                                               "ubirajara_coivara_tg")
    assert TG_STUDENT_A2 == CanaryTelegramUser(7568837201946, "Anacleta", "Mandacaru Piracema",
                                               "anacleta_piracema_tg")
    assert TG_STUDENT_B == CanaryTelegramUser(7574419302658, "Bartolomeu", "Guaxinim Caraúna",
                                              "bartolomeu_carauna_tg")
    assert CANARY_STUDENT_ACCOUNTS == (TG_STUDENT_A1, TG_STUDENT_A2, TG_STUDENT_B)
    assert STUDENT_A.row() == {"name": "Leocádia Jurubeba Tabatinga", "ra": "8812047356",
                               "email": "Leocadia.JURUBEBA@Jequitiba.Example.ORG"}
    assert STUDENT_B.row() == {"name": "Genésio Urutau Paraguaçu", "ra": "8826193407",
                               "email": "GENESIO.Urutau@Buritizal.Example.NET"}
    for student in CANARY_STUDENTS:
        assert student.name != _strip_accents(student.name), "nome de aluno canário sem acento"
        assert any(c.isupper() for c in student.name)
        assert re.fullmatch(r"\d{10}", student.ra)
        local, domain = student.email.split("@")
        assert any(c.isupper() for c in local) and any(c.isupper() for c in domain)
        assert re.fullmatch(r"[a-z]+\.example\.[a-z]+", domain.casefold())
        assert "canario" not in fold(student.email)
        email_validator.validate_email(student.email, check_deliverability=False,
                                       allow_display_name=False, allow_quoted_local=False,
                                       allow_domain_literal=False)
        assert email_validator.validate_email(student.email, check_deliverability=False,
                                              allow_smtputf8=False).ascii_email is not None
    ids = [account.user_id for account in CANARY_STUDENT_ACCOUNTS]
    assert len(set(ids)) == 3
    for account in CANARY_STUDENT_ACCOUNTS:
        assert re.fullmatch(r"75\d{11}", str(account.user_id))
        assert account.last_name is not None and account.username is not None
        assert re.fullmatch(r"[a-z0-9_]{5,32}", account.username)
        assert not str(account.user_id).startswith(str(FIRST_BOT_ID)[:2])
        assert not str(account.user_id).startswith(str(UPDATE_ID_BASE)[:2])
        assert not str(account.user_id).startswith(str(make_user(0).user_id)[:2])


def test_student_ids_neither_contain_nor_are_contained_in_existing_ids_and_ras() -> None:
    """Os 3 ids numéricos de aluno não contêm nem estão contidos nos ids e RAs canário
    já existentes; nem nos RAs dos próprios alunos (o id no `chat_id` não pode acusar o RA)."""
    student_ids = [str(account.user_id) for account in CANARY_STUDENT_ACCOUNTS]
    others = [person.ra for person in CANARY_PEOPLE if person.ra is not None]
    others += [str(user.user_id) for user in CANARY_TELEGRAM_USERS]
    others += [student.ra for student in CANARY_STUDENTS]
    assert len(others) == 7
    for student_id in student_ids:
        for other in others:
            assert student_id not in other and other not in student_id, (student_id, other)
    for index, student_id in enumerate(student_ids):
        for other_id in student_ids[index + 1:]:
            assert student_id not in other_id and other_id not in student_id


def test_student_prefix_constants() -> None:
    """Os prefixos `aluno-*` ficam fora de `ALL_PREFIXES`, que não muda (nem os das pessoas
    nem os do Telegram); cada alvo de aluno pertence a um único prefixo de `STUDENT_PREFIXES`; nenhum alvo de aluno
    entra em `TARGETS` nem em `TG_TARGETS`."""
    assert STUDENT_PREFIXES == ("aluno-nome", "aluno-palavra", "aluno-ra", "aluno-email",
                                "aluno-email-local", "aluno-email-dominio", "aluno-tg-nome",
                                "aluno-tg-palavra", "aluno-tg-username", "aluno-tg-id")
    assert STUDENT_PREFIXES == ROSTER_PREFIXES + ACCOUNT_PREFIXES
    assert BASE_PREFIXES == ("nome", "palavra", "ra", "email", "email-local")
    assert TG_PREFIXES == ("tg-nome", "tg-palavra", "tg-username", "tg-id")
    assert ALL_PREFIXES == BASE_PREFIXES + TG_PREFIXES
    assert not set(STUDENT_PREFIXES) & set(ALL_PREFIXES)
    assert all(prefix.startswith("aluno-") for prefix in STUDENT_PREFIXES)
    assert {label.split(":", 1)[0] for label, _ in STUDENT_TARGETS} == set(STUDENT_PREFIXES)
    assert not any(label.startswith("aluno-") for label, _ in TARGETS + TG_TARGETS)
    assert {label.split(":", 1)[0] for label, _ in TARGETS} == set(BASE_PREFIXES)
    assert {label.split(":", 1)[0] for label, _ in TG_TARGETS} == set(TG_PREFIXES)
    assert [person.name for person in CANARY_PEOPLE] == [
        "Zéfiro Canário Quintela", "Úrsula Pintassilgo Bethânia", "Olímpio Sanhaçu Tavares"]
    assert [user.user_id for user in CANARY_TELEGRAM_USERS] == [7300112233445, 7300556677889,
                                                                7300998877665]


def test_student_targets_are_exactly_the_hand_written_ones() -> None:
    """os rótulos dos alvos de aluno são exatamente os escritos à mão acima, com o alvo dobrado
    de cada um igual ao `fold` da grafia (sem `@`), e nenhum se repete."""
    labels = [label for label, _ in STUDENT_TARGETS]
    assert len(labels) == len(set(labels)) == 40
    assert set(labels) == {label for label, _ in STUDENT_EXPECTED}
    folded = dict(STUDENT_TARGETS)
    for label, original in STUDENT_EXPECTED:
        assert folded[label] == fold(original.removeprefix("@")), label


def test_student_prefixes_are_known_and_invalid_ones_are_errors() -> None:
    """Os prefixos `aluno-*` são conhecidos (sozinhos, juntos e somados a
    `ALL_PREFIXES`); prefixo de aluno desconhecido, só `aluno` ou um texto no lugar da coleção é erro
    do teste, mesmo com texto vazio."""
    assert find_canary("", prefixes=STUDENT_PREFIXES) == []
    assert find_canary("Urutau", prefixes=("aluno-palavra",)) == ["aluno-palavra:urutau"]
    assert find_canary("Urutau e Tavares", prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == [
        "aluno-palavra:urutau", "palavra:tavares"]
    for prefixes in (("aluno-nome", "aluno-desconhecido"), ("aluno",), ("aluno-tg",), "aluno-tg-id"):
        with pytest.raises(ValueError):
            find_canary("Urutau", prefixes=prefixes)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            find_canary("", prefixes=prefixes)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# O detector acha cada alvo de aluno em cada grafia
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize(("label", "original"), STUDENT_EXPECTED,
                         ids=[f"{lab}|{orig}" for lab, orig in STUDENT_EXPECTED])
def test_detector_finds_each_student_target_in_each_spelling(label: str, original: str,
                                                             variant: str) -> None:
    """Com `STUDENT_PREFIXES` e com `ALL_PREFIXES + STUDENT_PREFIXES`, o detector acha
    cada alvo dos alunos canário (inclusive o nome, o sobrenome e o `@username` do perfil do Telegram)
    em caixa trocada, sem acento, com acento a mais, com espaços extras, em NFD, em percent-encoding,
    dentro de JSON (escapado, aninhado, como chave) e numa linha JSON de log."""
    text = VARIANTS[variant](original)
    for prefixes in (STUDENT_PREFIXES, ALL_PREFIXES + STUDENT_PREFIXES):
        hits = find_canary(text, prefixes=prefixes)
        assert label in hits, f"{variant}: {label!r} não achado em {text!r} (achados: {hits})"


@pytest.mark.parametrize(
    ("text", "prefixes", "expected"),
    [
        ("Leocádia Jurubeba Tabatinga", STUDENT_PREFIXES, [
            "aluno-nome:Leocádia Jurubeba Tabatinga", "aluno-palavra:jurubeba", "aluno-palavra:leocadia",
            "aluno-palavra:tabatinga"]),
        ("Leocadia.JURUBEBA@Jequitiba.Example.ORG", STUDENT_PREFIXES, [
            "aluno-email-dominio:Leocadia.JURUBEBA@Jequitiba.Example.ORG",
            "aluno-email-local:Leocadia.JURUBEBA@Jequitiba.Example.ORG",
            "aluno-email:Leocadia.JURUBEBA@Jequitiba.Example.ORG",
            "aluno-palavra:jurubeba", "aluno-palavra:leocadia"]),
        ("RCPT TO:<GENESIO.Urutau@buritizal.example.net>", STUDENT_PREFIXES, [
            "aluno-email-dominio:GENESIO.Urutau@Buritizal.Example.NET",
            "aluno-email-local:GENESIO.Urutau@Buritizal.Example.NET",
            "aluno-email:GENESIO.Urutau@Buritizal.Example.NET",
            "aluno-palavra:genesio", "aluno-palavra:urutau"]),
        ("buritizal.example.net", STUDENT_PREFIXES,
         ["aluno-email-dominio:GENESIO.Urutau@Buritizal.Example.NET"]),
        ("RA 8826193407.", STUDENT_PREFIXES, ["aluno-ra:8826193407"]),
        ("@BARTOLOMEU_CARAUNA_TG", STUDENT_PREFIXES, [
            "aluno-tg-nome:Bartolomeu", "aluno-tg-palavra:bartolomeu", "aluno-tg-palavra:carauna",
            "aluno-tg-username:bartolomeu_carauna_tg"]),
        ("Anacleta Mandacaru Piracema", STUDENT_PREFIXES, [
            "aluno-tg-nome:Anacleta", "aluno-tg-nome:Anacleta Mandacaru Piracema",
            "aluno-tg-nome:Mandacaru Piracema", "aluno-tg-palavra:anacleta", "aluno-tg-palavra:mandacaru",
            "aluno-tg-palavra:piracema"]),
        ("chat 7568837201946 e Anacleta", ("aluno-tg-id",), ["aluno-tg-id:7568837201946"]),
        ("Genésio e Tavares e Teodorico", ALL_PREFIXES + STUDENT_PREFIXES, [
            "aluno-palavra:genesio", "palavra:tavares", "tg-nome:Teodorico", "tg-palavra:teodorico"]),
    ],
    ids=["nome_a", "email_a", "rcpt_b", "dominio_b", "ra_b", "username_b", "nome_conta_a2",
         "so_aluno_tg_id", "todos_os_prefixos"],
)
def test_detector_exact_hits_with_student_prefixes(text: str, prefixes: tuple[str, ...],
                                                   expected: list[str]) -> None:
    """saídas exatas e ordenadas, escritas à mão, com cada seleção de prefixos."""
    assert find_canary(text, prefixes=prefixes) == expected


def _update(sender: CanaryTelegramUser, text: str) -> dict:
    """Um update `message` com o remetente em `from`, `chat` e numa resposta citada."""
    return {
        "update_id": UPDATE_ID_BASE,
        "message": {
            "message_id": 1, "from": sender.as_user(), "chat": sender.as_private_chat(),
            "date": 1924992000, "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split(" ")[0])}],
            "reply_to_message": {"message_id": 2, "from": sender.as_user(), "date": 1924991000,
                                 "chat": sender.as_private_chat(), "text": "o termo"},
        },
    }


@pytest.mark.parametrize("ensure_ascii", [True, False], ids=["json_ascii", "json_utf8"])
def test_update_payload_of_each_student_account_is_flagged(ensure_ascii: bool) -> None:
    """O JSON de um update de cada conta de aluno (como a Bot API o entregaria) é
    acusado com o nome, o sobrenome, as palavras, o `username` e o id da conta; o `/convite` com o
    e-mail de B no texto também acusa o e-mail; sem `prefixes` e com `ALL_PREFIXES`, nada."""
    a1 = json.dumps(_update(TG_STUDENT_A1, "/aceito"), ensure_ascii=ensure_ascii)
    assert find_canary(a1, prefixes=STUDENT_PREFIXES) == [
        "aluno-tg-id:7561203948571", "aluno-tg-nome:Coivara Jacarandá", "aluno-tg-nome:Ubirajara",
        "aluno-tg-palavra:coivara", "aluno-tg-palavra:jacaranda", "aluno-tg-palavra:ubirajara",
        "aluno-tg-username:ubirajara_coivara_tg"]
    a2 = json.dumps(_update(TG_STUDENT_A2, "/recuso"), ensure_ascii=ensure_ascii)
    assert find_canary(a2, prefixes=ACCOUNT_PREFIXES) == [
        "aluno-tg-id:7568837201946", "aluno-tg-nome:Anacleta", "aluno-tg-nome:Mandacaru Piracema",
        "aluno-tg-palavra:anacleta", "aluno-tg-palavra:mandacaru", "aluno-tg-palavra:piracema",
        "aluno-tg-username:anacleta_piracema_tg"]
    b = json.dumps(_update(TG_STUDENT_B, f"/convite {STUDENT_B.email}"), ensure_ascii=ensure_ascii)
    assert find_canary(b, prefixes=STUDENT_PREFIXES) == [
        "aluno-email-dominio:GENESIO.Urutau@Buritizal.Example.NET",
        "aluno-email-local:GENESIO.Urutau@Buritizal.Example.NET",
        "aluno-email:GENESIO.Urutau@Buritizal.Example.NET", "aluno-palavra:genesio",
        "aluno-palavra:urutau", "aluno-tg-id:7574419302658", "aluno-tg-nome:Bartolomeu",
        "aluno-tg-nome:Guaxinim Caraúna", "aluno-tg-palavra:bartolomeu", "aluno-tg-palavra:carauna",
        "aluno-tg-palavra:guaxinim", "aluno-tg-username:bartolomeu_carauna_tg"]
    for text in (a1, a2, b):
        assert find_canary(text) == []
        assert find_canary(text, prefixes=ALL_PREFIXES) == []


def test_synthetic_profile_echo_is_flagged() -> None:
    """Controle positivo sintético no molde do `synthetic_tg` dos remetentes do Telegram: um texto com o nome
    e o `@username` de uma conta de aluno é acusado com `STUDENT_PREFIXES`."""
    for account in CANARY_STUDENT_ACCOUNTS:
        synthetic = f"X-Eco: {account.full_name} @{account.username}"
        assert {f"aluno-tg-nome:{account.full_name}", f"aluno-tg-username:{account.username}"} <= set(
            find_canary(synthetic, prefixes=STUDENT_PREFIXES))


def test_json_escape_needs_decoding_for_student_names() -> None:
    """controle das variantes JSON: `Paraguaçu` escapado (`Paragua\\u00e7u`) não casa no texto
    cru, só depois de decodificado."""
    raw = json.dumps(["Paraguaçu"])
    assert "\\u00e7" in raw
    assert "paraguacu" not in fold(raw)
    assert find_canary(raw, prefixes=STUDENT_PREFIXES) == ["aluno-palavra:paraguacu"]


# ---------------------------------------------------------------------------------------------
# Detecção cruzada
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(STUDENT_TEXTS))
def test_student_texts_are_not_flagged_by_existing_targets(name: str) -> None:
    """Nenhum texto-alvo de aluno é acusado sem `prefixes` nem com `ALL_PREFIXES` (os
    testes já existentes não mudam de sentido); com `STUDENT_PREFIXES`, é (controle positivo)."""
    text = STUDENT_TEXTS[name]
    assert find_canary(text) == []
    assert find_canary(text, prefixes=ALL_PREFIXES) == []
    assert find_canary(text, prefixes=STUDENT_PREFIXES) != []


@pytest.mark.parametrize("name", sorted(EXISTING_TARGET_TEXTS))
def test_existing_targets_are_not_flagged_by_student_targets(name: str) -> None:
    """Nenhum alvo já existente (grafia original e forma dobrada) é acusado com
    `STUDENT_PREFIXES`; com `ALL_PREFIXES`, é (controle positivo)."""
    text = EXISTING_TARGET_TEXTS[name]
    assert find_canary(text, prefixes=STUDENT_PREFIXES) == []
    assert find_canary(text, prefixes=ALL_PREFIXES) != []


@pytest.mark.parametrize("name", sorted(STUDENT_TEXTS) + sorted(EXISTING_TARGET_TEXTS))
def test_all_plus_student_prefixes_is_the_union(name: str) -> None:
    """com `ALL_PREFIXES + STUDENT_PREFIXES`, o resultado é a união ordenada dos dois (os
    conjuntos de alvos não interferem um no outro)."""
    text = STUDENT_TEXTS.get(name) or EXISTING_TARGET_TEXTS[name]
    assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == sorted(
        find_canary(text, prefixes=ALL_PREFIXES) + find_canary(text, prefixes=STUDENT_PREFIXES))


def _student_of(label: str) -> str:
    """O aluno (`a` ou `b`) dono de um rótulo de aluno."""
    value = label.split(":", 1)[1]
    owners = [tag for tag, student in (("a", STUDENT_A), ("b", STUDENT_B))
              if fold(value) in {fold(student.name), fold(student.ra), fold(student.email)}
              or fold(value) in fold(student.name).split(" ")
              or any(fold(value) in {fold(account.full_name), fold(account.first_name),
                                     fold(account.last_name or ""), fold(account.username or ""),
                                     str(account.user_id)}
                     or fold(value) in fold(account.full_name).split(" ")
                     for account in student.accounts)]
    assert len(owners) == 1, (label, owners)
    return owners[0]


@pytest.mark.parametrize("name", sorted(STUDENT_TEXTS))
def test_roster_and_account_targets_do_not_cross(name: str) -> None:
    """Um texto da lista (nome, RA, e-mail) não acusa alvo de conta do Telegram, e um
    texto de conta não acusa alvo da lista (as contas não têm exceção em `identities`, e a lista
    tem); um texto do aluno A não acusa alvo do aluno B, e vice-versa."""
    text = STUDENT_TEXTS[name]
    tag = name[0]
    from_account = "_tg_" in name
    hits = find_canary(text, prefixes=STUDENT_PREFIXES)
    assert hits
    wrong_kind = find_canary(text, prefixes=ROSTER_PREFIXES if from_account else ACCOUNT_PREFIXES)
    assert wrong_kind == [], f"{name}: {wrong_kind}"
    assert {_student_of(label) for label in hits} == {tag}, f"{name}: {hits}"


# ---------------------------------------------------------------------------------------------
# Texto limpo não é acusado
# ---------------------------------------------------------------------------------------------

_NEUTRAL = make_user(7)

CLEAN_TEXTS = {
    "vazio": "",
    "remetente_neutro": json.dumps({"from": _NEUTRAL.as_user(), "chat": _NEUTRAL.as_private_chat()}),
    "bot_e_update_do_duble": json.dumps({"id": FIRST_BOT_ID, "is_bot": True, "first_name": "Bot de teste",
                                         "username": "cq_teste_0000_bot", "update_id": UPDATE_ID_BASE}),
    "quase_ids": "756120394857 e 7561203948570 e 7568837201940 e 7574419302650",
    "quase_ras": "881204735 e 8812047357 e 882619340 e 8826193408",
    "quase_nomes": "Leocádi, Jurubeb, Tabating, Genési, Uruta, Paraguaç.",
    "quase_perfis": "Ubiraja, Coivar, Jacarand, Anaclet, Mandacar, Piracem, Bartolome, Guaxini, Caraún.",
    "quase_usernames": "ubirajar_coivar_tg e anaclet_piracem_tg e bartolome_caraun_tg",
    "quase_dominios": "jequitiba.example.com e buritizal.example.org e example.org e example.net",
    "remetente_do_email": "From: Canal de Questões <convites@example.org>\r\nMessage-ID: <x@example.org>",
    "dominio_canario": "canario.example.com",
    "link_de_convite": "https://t.me/cq_teste_0003_bot?start=AbCdEfGhIjKlMnOpQrStUv",
    "marca_de_pessoa": "Disciplina de [pessoa a3f9] no período 2031s1",
    "comandos": "/aceito /recuso /convite /start /ajuda",
}


@pytest.mark.parametrize("name", sorted(CLEAN_TEXTS))
def test_detector_does_not_flag_clean_text_with_student_prefixes(name: str) -> None:
    """com `STUDENT_PREFIXES`, o detector não acusa texto limpo: remetentes neutros e ids do
    dublê, quase-ids (12 dígitos ou outro último dígito), quase-RAs, quase-nomes, quase-usernames,
    domínios vizinhos, o remetente do e-mail e o link de convite."""
    assert find_canary(CLEAN_TEXTS[name], prefixes=STUDENT_PREFIXES) == []


# =============================================================================================
# Parte 2: credenciais do SMTP falso e detector genérico de código de convite
# =============================================================================================


@dataclass
class _SmtpStub:
    """Só o atributo que `mail_secrets` lê do `FakeSmtp`: `credentials`."""

    credentials: tuple[str, str] | None


USERNAME = "cq-smtp-teste"
PASSWORD = "Senha-Falsa_7Qx2Lm9Pz"
# Escritos à mão: base64 de "\0cq-smtp-teste\0Senha-Falsa_7Qx2Lm9Pz" e de "Senha-Falsa_7Qx2Lm9Pz".
PLAIN_B64 = "AGNxLXNtdHAtdGVzdGUAU2VuaGEtRmFsc2FfN1F4MkxtOVB6"
LOGIN_B64 = "U2VuaGEtRmFsc2FfN1F4MkxtOVB6"


class _RecordingSmtp(smtplib.SMTP):
    """`smtplib.SMTP` sem rede: `docmd` grava as linhas que iriam ao fio e responde pelo roteiro."""

    def __init__(self, username: str, password: str, replies: list[tuple[int, bytes]]) -> None:
        super().__init__(local_hostname="localhost")  # sem `host`: não conecta nem consulta DNS
        self.user, self.password = username, password
        self.sent: list[str] = []
        self._replies = list(replies)

    def docmd(self, cmd: str, args: str = "") -> tuple[int, bytes]:
        self.sent.append(f"{cmd} {args}" if args else cmd)
        return self._replies.pop(0)


def _smtplib_wire_forms(username: str, password: str) -> tuple[str, str]:
    """As formas que o próprio `smtplib.SMTP.auth` põe no fio, sem conectar: o argumento do
    `AUTH PLAIN` (resposta inicial) e a resposta à pergunta da senha no `AUTH LOGIN`."""
    plain = _RecordingSmtp(username, password, [(235, b"2.7.0 ok")])
    login = _RecordingSmtp(username, password, [(334, base64.b64encode(b"Password:")),
                                                (235, b"2.7.0 ok")])
    try:
        plain.auth("PLAIN", plain.auth_plain)
        login.auth("LOGIN", login.auth_login)
    finally:
        plain.close()
        login.close()
    assert len(plain.sent) == 1 and plain.sent[0].startswith("AUTH PLAIN ")
    user_b64 = email.base64mime.body_encode(username.encode("ascii"), eol="")
    assert login.sent[0] == f"AUTH LOGIN {user_b64}" and len(login.sent) == 2
    return plain.sent[0].removeprefix("AUTH PLAIN "), login.sent[1]


def test_mail_secrets_are_the_password_and_its_wire_forms() -> None:
    """`mail_secrets(smtp)` lê `credentials` e devolve a senha, o base64 do AUTH PLAIN e
    o base64 da senha (AUTH LOGIN), iguais aos do `smtplib`, mais `extra`; relê a cada chamada."""
    stub = _SmtpStub((USERNAME, PASSWORD))
    assert mail_secrets(stub) == [PASSWORD, PLAIN_B64, LOGIN_B64]
    assert _smtplib_wire_forms(USERNAME, PASSWORD) == (PLAIN_B64, LOGIN_B64)
    assert base64.b64decode(PLAIN_B64) == f"\0{USERNAME}\0{PASSWORD}".encode()
    assert mail_secrets(stub, "extra-sentinela") == [PASSWORD, PLAIN_B64, LOGIN_B64, "extra-sentinela"]
    # Senha cujo base64 tem `=` e usuário longo (o `smtplib` quebra em blocos de 57 bytes).
    stub.credentials = ("u" * 70, "p@ss")
    plain, login = _smtplib_wire_forms("u" * 70, "p@ss")
    assert login.endswith("=") and len(plain) > 76
    assert mail_secrets(stub) == ["p@ss", plain, login]


def test_mail_secrets_stay_out_of_all_secrets() -> None:
    """As credenciais do SMTP ficam fora de `all_secrets()` (que continua igual) e do
    registro `SECRETS`, como as chaves-mestras de `bot_secrets()`."""
    assert SECRETS == []
    mail_secrets(_SmtpStub((USERNAME, PASSWORD)))
    assert SECRETS == []
    assert all_secrets() == [ADMIN_TOKEN]


@pytest.mark.parametrize("stub", [_SmtpStub(None), object(), _SmtpStub((USERNAME, ""))],
                         ids=["sem_credenciais", "sem_atributo", "senha_vazia"])
def test_mail_secrets_without_credentials_is_a_test_error(stub: object) -> None:
    """sem credenciais (antes do `reset`) ou com senha vazia, é erro do teste (senão a varredura
    passaria no vazio)."""
    with pytest.raises(ValueError):
        mail_secrets(stub)


@pytest.mark.parametrize(
    ("text", "expected_index"),
    [
        (f"AUTH PLAIN {PLAIN_B64}", 1),
        (f"334 VXNlcm5hbWU6\r\n{base64.b64encode(USERNAME.encode()).decode()}\r\n334 UGFzc3dvcmQ6\r\n"
         f"{LOGIN_B64}", 2),
        (json.dumps({"detail": f"senha {PASSWORD}"}), 0),
        (urllib.parse.quote(f"x={PLAIN_B64}", safe=""), 1),
    ],
    ids=["auth_plain", "auth_login", "senha_em_json", "plain_em_percent_encoding"],
)
def test_find_secrets_finds_each_mail_secret(text: str, expected_index: int) -> None:
    """`find_secrets` com `mail_secrets()` acha cada forma no texto em que ela aparece
    (controle positivo das varreduras de credenciais)."""
    secrets = mail_secrets(_SmtpStub((USERNAME, PASSWORD)))
    assert f"segredo:{secrets[expected_index][:6]}…" in find_secrets(text, secrets)


@pytest.mark.parametrize(
    "text",
    ["", base64.b64encode(USERNAME.encode()).decode(), f"AUTH LOGIN {USERNAME}", PASSWORD[:-1],
     LOGIN_B64[:-2]],
    ids=["vazio", "usuario_em_base64", "usuario_cru", "prefixo_da_senha", "prefixo_do_login"],
)
def test_find_secrets_does_not_flag_username_or_prefixes(text: str) -> None:
    """o usuário sozinho (cru ou em base64) não é segredo; prefixos das formas não casam."""
    assert find_secrets(text, mail_secrets(_SmtpStub((USERNAME, PASSWORD)))) == []


# ---------------------------------------------------------------------------------------------
# Detector genérico de código de convite
# ---------------------------------------------------------------------------------------------

CODE = "Q9x-Lm_2PzR7tKc4VwYb0N"
OTHER_CODE = "Zk3_Rt8-WqPn5Hs1YcVb6D"
DIGEST = hashlib.sha256(CODE.encode("utf-8")).hexdigest()
OTHER_DIGEST = hashlib.sha256(OTHER_CODE.encode("utf-8")).hexdigest()


def test_invite_code_fixture_has_contract_format() -> None:
    """os códigos de teste têm a forma do convite (22 caracteres base64url), e o resumo é o
    SHA-256 hex dos bytes UTF-8, como em `invites.code_sha256`."""
    assert INVITE_CODE_LENGTH == 22
    for code in (CODE, OTHER_CODE):
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", code)
    assert invite_code_digest(CODE) == DIGEST and re.fullmatch(r"[0-9a-f]{64}", DIGEST)
    assert invite_code_label(DIGEST) == f"convite:{DIGEST[:8]}…"
    assert invite_code_label(DIGEST.upper()) == invite_code_label(DIGEST)


def _all_percent(text: str) -> str:
    return "".join(f"%{byte:02X}" for byte in text.encode("utf-8"))


def _json_unicode_escaped(text: str) -> str:
    return '{"text": "' + "".join(f"\\u{ord(c):04x}" for c in text) + '"}'


FOUND_TEXTS = {
    "cru": lambda c: c,
    "colado_a_letras_dos_dois_lados": lambda c: f"abc{c}xyz",
    "colado_a_uma_sequencia_longa": lambda c: f"{'A' * 30}{c}{'z9_-' * 5}",
    "percent_3d": lambda c: f"%3D{c}",
    "percent_3d_em_consulta": lambda c: f"/x?start%3D{c}%26y%3D1",
    "link": lambda c: f"https://t.me/cq_teste_0003_bot?start={c}",
    "link_em_percent_encoding": lambda c: urllib.parse.quote(f"https://t.me/cq_teste_0003_bot?start={c}",
                                                             safe=""),
    "todo_em_percent_encoding": _all_percent,
    "start_gravado": lambda c: f"/start {c}",
    "json_valor": lambda c: json.dumps({"text": f"/start {c}"}),
    "json_chave": lambda c: json.dumps({c: 1}),
    "json_com_escape_unicode": _json_unicode_escaped,
    "json_em_linha_de_log": lambda c: (
        "linha qualquer\n" + json.dumps({"event": "x", "extra": {"k": [f"?start={c}"]}}) + "\nfim"),
}


@pytest.mark.parametrize("name", sorted(FOUND_TEXTS))
def test_find_invite_codes_finds_the_code_in_every_form(name: str) -> None:
    """O detector acha o código em toda janela de 22 caracteres de cada sequência
    maximal `[A-Za-z0-9_-]{22,}`, em todas as formas de `_forms`: colado a letras dos dois lados, em
    percent-encoding (`%3D<código>` e inteiro), no link, no `/start`, e dentro de JSON (valor,
    chave, escape `\\uXXXX`, linha de log); resumos em maiúsculas também valem."""
    text = FOUND_TEXTS[name](CODE)
    assert find_invite_codes(text, {DIGEST}) == [invite_code_label(DIGEST)]
    assert find_invite_codes(text, [DIGEST.upper(), OTHER_DIGEST]) == [invite_code_label(DIGEST)]


def test_encoded_forms_actually_need_decoding() -> None:
    """controle das formas: no percent-encoding inteiro e no escape `\\uXXXX` de JSON, o código
    não aparece no texto cru; o detector o acha só pelas outras formas de `_forms`."""
    for name in ("todo_em_percent_encoding", "json_com_escape_unicode"):
        text = FOUND_TEXTS[name](CODE)
        assert CODE not in text
        assert find_invite_codes(text, {DIGEST}) == [invite_code_label(DIGEST)]


def test_find_invite_codes_reports_each_code_once_and_only_the_given_ones() -> None:
    """dois códigos no mesmo texto dão dois rótulos, ordenados, cada um uma vez, mesmo repetidos
    em várias formas; só os códigos cujo resumo foi dado são acusados."""
    text = json.dumps({"a": f"/start {CODE}", "b": [OTHER_CODE, f"x{CODE}y", urllib.parse.quote(CODE)]})
    both = sorted([invite_code_label(DIGEST), invite_code_label(OTHER_DIGEST)])
    assert find_invite_codes(text, {DIGEST, OTHER_DIGEST}) == both
    assert find_invite_codes(text, [OTHER_DIGEST]) == [invite_code_label(OTHER_DIGEST)]


@pytest.mark.parametrize(
    "text",
    ["", CODE[:-1], CODE[1:], CODE[:11] + " " + CODE[11:], CODE[:11] + "." + CODE[11:],
     CODE[:-1] + ("A" if CODE[-1] != "A" else "B"), f"%3D{CODE[:-1]}", DIGEST,
     json.dumps({"code_sha256": DIGEST}), OTHER_CODE, "https://t.me/cq_teste_0003_bot?start=AbCdEfGhIjKlMnOpQrStUv"],
    ids=["vazio", "sem_o_ultimo", "sem_o_primeiro", "partido_por_espaco", "partido_por_ponto",
         "ultimo_trocado", "percent_sem_o_ultimo", "resumo_cru", "resumo_em_json", "outro_codigo",
         "outro_link"],
)
def test_find_invite_codes_does_not_flag_clean_text(text: str) -> None:
    """o detector não acusa texto sem o código: pedaços de 21 caracteres, o código partido ou
    com um caractere trocado, o próprio resumo (a coluna `code_sha256`) e outros códigos."""
    assert find_invite_codes(text, {DIGEST}) == []


@pytest.mark.parametrize(
    "digests",
    [DIGEST, set(), [], [CODE], ["abc"], [DIGEST, "z" * 64], [DIGEST, None]],
    ids=["texto", "conjunto_vazio", "lista_vazia", "codigo_no_lugar_do_resumo", "hex_curto",
         "nao_hex", "nao_texto"],
)
def test_find_invite_codes_invalid_digests_are_test_errors(digests: object) -> None:
    """um texto no lugar da coleção, uma coleção vazia ou um item que não é resumo SHA-256 hex é
    erro do teste (senão a varredura passaria no vazio), mesmo com texto vazio."""
    with pytest.raises(ValueError):
        find_invite_codes(CODE, digests)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        find_invite_codes("", digests)  # type: ignore[arg-type]
