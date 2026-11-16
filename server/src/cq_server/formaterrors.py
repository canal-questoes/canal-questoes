"""Tradução dos erros de formato do PL para mensagens fixas em português, sem ecoar o texto do PL
nem o do aluno.

`translate(field_type, text, *, hint)` sobre o `format_errors[name].text` do motor (nunca o `html`),
por `re.match` no início; os modelos interpolam só números capturados; nada do texto do PL é ecoado
(nem a chave inválida). Sem regra: o recuo com a dica do campo. O original fica na submissão.

A tabela cobre as mensagens de formato do PL fixado, mais a regra "You must select
at least one option.", mantida por defesa. As regras são por tipo de campo: uma regra de
outro tipo não vale. Todo texto sem regra, inclusive o texto personalizado do `server.py` do autor
(até em português), cai no recuo. Chave fora de `fields[]` (`field_type = None`): a frase fixa
`OUTSIDE_FIELDS`.

Núcleo puro.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Recuos.
FALLBACK = "Não consegui aceitar a resposta deste campo."
FALLBACK_WITH_HINT = FALLBACK + " Formato esperado: {hint}."
OUTSIDE_FIELDS = "A questão recusou a resposta. Confira o formato e envie de novo."

_COMPLEX = " real ou complexo"
_SKIP_PT = " (ou deixe em branco)"
_SKIP_PL = r"( \(or skip this question by leaving it blank\))?"


def _fixed(text: str) -> Callable[[re.Match[str]], str]:
    return lambda _match: text


def _number_invalid(match: re.Match[str]) -> str:
    complex_ = _COMPLEX if match.group(1) else ""
    return f"Não consegui ler a resposta como número{complex_}. Não use variáveis nem outros símbolos."


def _fraction_part(match: re.Match[str]) -> str:
    part = "numerador" if match.group(1) == "numerator" else "denominador"
    complex_ = _COMPLEX if match.group(2) else ""
    return f"O {part} da fração não é um número{complex_}."


def _integer_invalid(match: re.Match[str]) -> str:
    base = f" na base {int(match.group(2))}" if match.group(2) else ""
    return (f"Não consegui ler a resposta como número inteiro{base}. Não use símbolos, frações nem "
            "notação científica.")


def _select_between(match: re.Match[str]) -> str:
    skip = _SKIP_PT if match.group(3) else ""
    return f"Marque de {int(match.group(1))} a {int(match.group(2))} alternativas{skip}."


def _select_exactly(match: re.Match[str]) -> str:
    skip = _SKIP_PT if match.group(2) else ""
    return f"Marque exatamente {int(match.group(1))} alternativa(s){skip}."


_BLANK = "A resposta está em branco."

# (tipos, regex ancorada no início do `text`, modelo). Os modelos só leem
# números capturados (`\d+`) e marcas opcionais; nunca repassam texto do PL nem do aluno.
_RULES: tuple[tuple[frozenset[str], re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (frozenset({"number", "integer", "text"}), re.compile(r"No submitted answer\."),
     _fixed("Você não respondeu este campo.")),
    # number
    (frozenset({"number"}), re.compile(r"The submitted answer was blank\."), _fixed(_BLANK)),
    (frozenset({"number"}),
     re.compile(r"Invalid format: The submitted answer could not be interpreted as a decimal "
                r"(\(or complex\) )?number\."),
     _number_invalid),
    (frozenset({"number"}),
     re.compile(r"Invalid format: The (numerator|denominator) could not be interpreted as a decimal "
                r"(\(or complex\) )?number\."),
     _fraction_part),
    (frozenset({"number"}), re.compile(r"Invalid format: The submitted answer is not a finite number\."),
     _fixed("A resposta não é um número finito.")),
    (frozenset({"number"}), re.compile(r"Your expression resulted in a division by zero\."),
     _fixed("A fração tem divisão por zero.")),
    (frozenset({"number"}), re.compile(r"Fractional answers are not allowed in this input\."),
     _fixed("Este campo não aceita frações.")),
    # integer
    (frozenset({"integer"}), re.compile(r"Invalid format: the submitted answer was blank\."),
     _fixed(_BLANK)),
    (frozenset({"integer"}),
     re.compile(r"Invalid format: the submitted answer could not be interpreted as an integer"
                r"( in base (\d+))?\."),
     _integer_invalid),
    # text
    (frozenset({"text"}), re.compile(r"Invalid format\. The submitted answer was left blank\."),
     _fixed(_BLANK)),
    # single_choice
    (frozenset({"single_choice"}), re.compile(r"No answer was submitted\."),
     _fixed("Escolha uma alternativa.")),
    (frozenset({"single_choice"}), re.compile(r"Invalid choice: "),
     _fixed("Essa alternativa não existe nesta questão.")),
    # multiple_choice
    (frozenset({"multiple_choice"}),
     re.compile(r"You must select between (\d+) and (\d+) options" + _SKIP_PL + r"\."),
     _select_between),
    (frozenset({"multiple_choice"}),
     re.compile(r"You must select exactly (\d+) options?" + _SKIP_PL + r"\."),
     _select_exactly),
    (frozenset({"multiple_choice"}), re.compile(r"You selected an invalid option"),
     _fixed("Uma das alternativas marcadas não existe nesta questão.")),
    (frozenset({"multiple_choice"}), re.compile(r"You must select at least one option\."),
     _fixed("Marque pelo menos uma alternativa.")),
)


def translate(field_type: str | None, text: str, *, hint: str | None) -> str:
    """A explicação ao aluno, em português, de um erro de formato do PL."""
    if field_type is None:
        return OUTSIDE_FIELDS
    for types, pattern, template in _RULES:
        if field_type not in types:
            continue
        match = pattern.match(text)
        if match is not None:
            return template(match)
    if hint:
        return FALLBACK_WITH_HINT.format(hint=hint)
    return FALLBACK
