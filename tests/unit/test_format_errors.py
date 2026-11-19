"""Tradução dos erros de formato do PL (a tabela de traduções, o erro de formato fora dos
campos e os erros não reconhecidos).

`formaterrors.translate(field_type, text, *, hint)` recebe o `format_errors[name].text` do motor (o
HTML sem tags, entidades decodificadas e espaços colapsados; nunca o `html`), casa por `re.match` no
início do texto e devolve só a explicação em português (o prefixo `FORMAT_ERROR` é de quem compõe a
mensagem). A tabela é a das regras de tradução, mais a regra
"You must select at least one option.". Os modelos só interpolam números capturados:
nada do texto do PL é ecoado (nem a chave inválida). Sem regra: "Não consegui aceitar a resposta
deste campo. Formato esperado: {hint}." (sem dica, a frase termina no primeiro ponto). Chave fora de
`fields[]` (`field_type = None`): "A questão recusou a resposta. Confira o formato e envie de novo."
Todo texto sem regra (inclusive o texto do `server.py` do autor, até em português) cai no recuo.


Os textos do PL vêm dos textos reais capturados no motor, os mesmos do motor falso
(`tests/helpers/engine.py`), e dos padrões da tabela quando a captura não trouxe o exemplo. Os
textos esperados são literais da tabela. Leituras dos moldes da tabela fixadas aqui [I]: `{ real ou
complexo}` entra só quando o PL diz "(or complex)"; `{numerador|denominador}` segue o PL;
`{ na base N}` só quando o PL diz a base; `{ (ou deixe em branco)}` só com "(or skip this question by
leaving it blank)"; `alternativa(s)` é literal.

No esqueleto, `translate` lança `NotImplementedError`: todo caso falha por esse motivo.
"""

from __future__ import annotations

import inspect
import re

import pytest

from cq_server import formaterrors
from tests.helpers.engine import (
    INTEGER_BLANK,
    NO_SUBMITTED_ANSWER,
    NUMBER_BLANK,
    NUMBER_DIVISION_BY_ZERO,
    NUMBER_INVALID_TEXT,
    SERVER_FORMAT_ERROR,
    SINGLE_NO_ANSWER,
    TEXT_BLANK,
    html_to_text,
    integer_invalid_error,
    invalid_choice_error,
    invalid_option_error,
    select_count_error,
)

# Dicas da IR (em português, 1 a 64 caracteres).
NUMBER_HINT = "Número; ex.: 4.25, -17 ou 3/7"
INTEGER_HINT = "Número inteiro na base 16; ex.: 2A ou -11"
CHOICE_HINT = "Marque de 1 a 3 alternativas"

# O que o molde do `pl-number-input` acrescenta depois da frase (o `text` real capturado no motor).
_NUMBER_TAIL = (" Do not write variables or other symbols. Example valid inputs: 5-174.25e73/7 Example "
                "invalid inputs: 6+94*10^73*pi3.6i")
_SKIP = " (or skip this question by leaving it blank)"

FALLBACK_HEAD = "Não consegui aceitar a resposta deste campo."
OUTSIDE_FIELDS = "A questão recusou a resposta. Confira o formato e envie de novo."

# (id, tipo do campo, texto do PL, tradução exata) — uma linha por regra da tabela, por
# tipo, mais a regra de nenhuma opção marcada.
TABLE_CASES = [
    # number, integer, text: `No submitted answer\.`
    ("number-no-submitted", "number", NO_SUBMITTED_ANSWER, "Você não respondeu este campo."),
    ("integer-no-submitted", "integer", NO_SUBMITTED_ANSWER, "Você não respondeu este campo."),
    ("text-no-submitted", "text", NO_SUBMITTED_ANSWER, "Você não respondeu este campo."),
    # number
    ("number-blank", "number", NUMBER_BLANK, "A resposta está em branco."),
    ("number-invalid", "number", NUMBER_INVALID_TEXT,
     "Não consegui ler a resposta como número. Não use variáveis nem outros símbolos."),
    ("number-invalid-complex", "number",
     "Invalid format: The submitted answer could not be interpreted as a decimal (or complex) number."
     + _NUMBER_TAIL,
     "Não consegui ler a resposta como número real ou complexo. Não use variáveis nem outros símbolos."),
    ("number-numerator", "number",
     "Invalid format: The numerator could not be interpreted as a decimal number." + _NUMBER_TAIL,
     "O numerador da fração não é um número."),
    ("number-denominator-complex", "number",
     "Invalid format: The denominator could not be interpreted as a decimal (or complex) number."
     + _NUMBER_TAIL,
     "O denominador da fração não é um número real ou complexo."),
    ("number-not-finite", "number",
     "Invalid format: The submitted answer is not a finite number." + _NUMBER_TAIL,
     "A resposta não é um número finito."),
    ("number-division-by-zero", "number", NUMBER_DIVISION_BY_ZERO + _NUMBER_TAIL,
     "A fração tem divisão por zero."),
    ("number-fractions-not-allowed", "number", "Fractional answers are not allowed in this input.",
     "Este campo não aceita frações."),
    # integer
    ("integer-blank", "integer", INTEGER_BLANK, "A resposta está em branco."),
    ("integer-invalid", "integer", integer_invalid_error(10)["text"],
     "Não consegui ler a resposta como número inteiro. Não use símbolos, frações nem notação "
     "científica."),
    ("integer-invalid-base-2", "integer", integer_invalid_error(2)["text"],
     "Não consegui ler a resposta como número inteiro na base 2. Não use símbolos, frações nem "
     "notação científica."),
    ("integer-invalid-base-16", "integer", integer_invalid_error(16)["text"],
     "Não consegui ler a resposta como número inteiro na base 16. Não use símbolos, frações nem "
     "notação científica."),
    # text
    ("text-blank", "text", TEXT_BLANK, "A resposta está em branco."),
    # single_choice
    ("single-no-answer", "single_choice", SINGLE_NO_ANSWER, "Escolha uma alternativa."),
    ("single-invalid-choice", "single_choice", invalid_choice_error("zzz")["text"],
     "Essa alternativa não existe nesta questão."),
    # multiple_choice
    ("multiple-between", "multiple_choice", select_count_error(2, 4)["text"],
     "Marque de 2 a 4 alternativas."),
    ("multiple-between-two-digits", "multiple_choice", select_count_error(3, 12)["text"],
     "Marque de 3 a 12 alternativas."),
    ("multiple-between-skip", "multiple_choice",
     html_to_text(f"You must select between <b>1</b> and <b>3</b> options{_SKIP}."),
     "Marque de 1 a 3 alternativas (ou deixe em branco)."),
    ("multiple-exactly", "multiple_choice", select_count_error(2, 2)["text"],
     "Marque exatamente 2 alternativa(s)."),
    ("multiple-exactly-one", "multiple_choice", select_count_error(1, 1)["text"],
     "Marque exatamente 1 alternativa(s)."),
    ("multiple-exactly-skip", "multiple_choice",
     html_to_text(f"You must select exactly <b>3</b> options{_SKIP}."),
     "Marque exatamente 3 alternativa(s) (ou deixe em branco)."),
    ("multiple-invalid-option", "multiple_choice", invalid_option_error("zzz")["text"],
     "Uma das alternativas marcadas não existe nesta questão."),
    ("multiple-at-least-one", "multiple_choice", "You must select at least one option.",
     "Marque pelo menos uma alternativa."),
]

# Palavras do PL (e a chave ecoada) que nunca podem chegar ao aluno. "zero" e "base" ficam
# de fora: existem também em português ("divisão por zero", "na base 2").
_ENGLISH = re.compile(
    r"\b(invalid|format|submitted|answer|blank|number|integer|options?|choice|select(ed)?|must|could|"
    r"interpreted|decimal|complex|numerator|denominator|finite|fractional|allowed|division|expression|"
    r"skip|question|leaving|example|variables|symbols|between|exactly|least|input|zzz|please|enter|"
    r"positive|value|failed|server|check)\b",
    re.IGNORECASE)


def _assert_no_english(original: str, translated: str) -> None:
    """Nenhuma palavra em inglês do PL, nem a chave inválida, na saída."""
    assert not _ENGLISH.search(translated), (translated, _ENGLISH.findall(translated))
    assert "<" not in translated and "&" not in translated, translated
    if original:
        assert original not in translated


@pytest.mark.parametrize(("field_type", "text", "expected"),
                         [case[1:] for case in TABLE_CASES], ids=[case[0] for case in TABLE_CASES])
def test_each_table_row_translates_the_real_pl_text(field_type: str, text: str, expected: str) -> None:
    """cada linha da tabela, com o texto real do PL do tipo
    do campo, vira a frase exata em português, com os números capturados interpolados; a dica não
    muda nada quando há regra; nenhuma palavra do PL (nem a chave `zzz` ecoada pelo PL) na saída."""
    assert formaterrors.translate(field_type, text, hint=None) == expected
    assert formaterrors.translate(field_type, text, hint=NUMBER_HINT) == expected
    _assert_no_english(text, expected)


def test_translate_reads_only_the_text_and_never_the_html() -> None:
    """a assinatura é `translate(field_type, text, *, hint)`, sem o `html`
    (que pode ecoar a entrada do aluno); o `text` real do `Invalid choice` (com a chave) vira a frase
    sem eco, e o `html` do mesmo erro não é aceito como argumento."""
    error = invalid_choice_error("<b>&zzz</b>")
    assert formaterrors.translate("single_choice", error["text"], hint=None) == (
        "Essa alternativa não existe nesta questão.")
    parameters = inspect.signature(formaterrors.translate).parameters
    assert list(parameters) == ["field_type", "text", "hint"]
    assert parameters["hint"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        formaterrors.translate("single_choice", error["text"], hint=None,  # type: ignore[call-arg]
                               html=error["html"])


FALLBACK_CASES = [
    # O texto personalizado do `server.py` do autor, em português e com instrução.
    ("number-author-portuguese", "number", "Use o formato 1.23, com ponto decimal."),
    # O texto personalizado em inglês.
    ("text-author-english", "text", "Please enter a positive value."),
    # Regras de outro tipo não valem (a tabela é por tipo de campo).
    ("multiple-no-submitted", "multiple_choice", NO_SUBMITTED_ANSWER),
    ("multiple-invalid-choice", "multiple_choice", invalid_choice_error("zzz")["text"]),
    ("single-invalid-option", "single_choice", invalid_option_error("zzz")["text"]),
    ("text-number-blank", "text", NUMBER_BLANK),
    ("integer-number-invalid", "integer", NUMBER_INVALID_TEXT),
    # As mensagens do `test()` dos elementos (fora da correção) e o casamento só no início.
    ("single-test-invalid", "single_choice", "INVALID choice"),
    ("number-test-invalid", "number", "invalid"),
    ("number-not-at-start", "number", "Note: " + NO_SUBMITTED_ANSWER),
    ("text-empty", "text", ""),
]


@pytest.mark.parametrize(("field_type", "text"), [case[1:] for case in FALLBACK_CASES],
                         ids=[case[0] for case in FALLBACK_CASES])
def test_text_without_rule_falls_back_to_the_hint(field_type: str, text: str) -> None:
    """um texto sem regra para o tipo do campo vira "Não
    consegui aceitar a resposta deste campo. Formato esperado: {hint}."; sem dica, a frase termina no
    primeiro ponto; nada do original é ecoado (nem a instrução em português do autor)."""
    with_hint = formaterrors.translate(field_type, text, hint=INTEGER_HINT)
    assert with_hint == f"{FALLBACK_HEAD} Formato esperado: {INTEGER_HINT}."
    without_hint = formaterrors.translate(field_type, text, hint=None)
    assert without_hint == FALLBACK_HEAD
    for translated in (with_hint, without_hint):
        _assert_no_english(text, translated)
    assert formaterrors.translate(field_type, text, hint=CHOICE_HINT) == (
        f"{FALLBACK_HEAD} Formato esperado: {CHOICE_HINT}.")


OUTSIDE_CASES = [
    ("server-check", SERVER_FORMAT_ERROR.html),
    ("rule-text", NO_SUBMITTED_ANSWER),
    ("portuguese-instruction", "Digite o valor em metros, sem a unidade."),
    ("empty", ""),
]


@pytest.mark.parametrize("text", [case[1] for case in OUTSIDE_CASES],
                         ids=[case[0] for case in OUTSIDE_CASES])
def test_key_outside_fields_gets_the_fixed_sentence(text: str) -> None:
    """uma chave de `format_errors` fora de `fields[]` (o
    `grade()` do `server.py`) chega com `field_type = None` e vira sempre "A questão recusou a
    resposta. Confira o formato e envie de novo.", com ou sem dica, mesmo que o texto case com uma
    regra de algum tipo; nada do original é ecoado."""
    assert formaterrors.translate(None, text, hint=None) == OUTSIDE_FIELDS
    assert formaterrors.translate(None, text, hint=NUMBER_HINT) == OUTSIDE_FIELDS
    _assert_no_english(text, OUTSIDE_FIELDS)
