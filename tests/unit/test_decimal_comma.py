"""Vírgula decimal e resposta crua ao motor.


- `answers.normalize_decimal_comma(field_type, literal) -> (texto, normalizado)`: só num campo
  `number`, e só com **exatamente uma** vírgula (U+002C) e **nenhum** ponto (U+002E), a vírgula vira
  ponto e o segundo elemento é `True`; nada é aparado (o PL recusa depois o que não é número). Toda
  outra entrada, e todo campo de outro tipo, volta igual, com `False`.
- `answers.raw_for_engine(ir, raw_input, normalized_fields) -> dict`: o corpo
  `submitted_answers` que o PL gravaria, refeito deterministicamente da entrada literal por campo
  (`raw_input`, o `parts` da entrega) e da lista dos campos normalizados (a vírgula é a única
  transformação). Escolha única → a chave (pelo botão) ou o texto digitado literal; múltipla pelo
  botão (lista) → as chaves na ordem das opções, uma marcação como texto, nenhuma como campo
  **ausente**; múltipla digitada (texto; canal degradado ou lista com chaves) → o texto literal
  digitado, **sem nenhuma mudança** (fidelidade ao PL na
  adaptação de canal da escolha múltipla digitada): é o valor cru do campo, como o PL o
  receberia de um formulário com um único valor, e o `pl-checkbox` faz `set(submitted_key)`
  (`pl-checkbox.py@a193f72:726-752`): `ab` vale como {a, b}; `b,a` ou `a b` voltam com o erro de
  formato do próprio PL. Sem divisão, sem reordenação, sem descarte. `number`, `integer` e `text` → o
  texto literal, salvo a vírgula do `number` normalizado. A entrada literal não muda.

A IR vem do motor falso (`tests/helpers/engine.py`, `build_variant`), a mesma que o motor falso
confere contra `contracts/engine/v1/ir.schema.json`. No esqueleto, as duas funções lançam
`NotImplementedError`: todo caso falha por esse motivo.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from cq_server import answers
from tests.helpers.engine import (
    FakeQuestion,
    build_variant,
    integer_field,
    multiple_choice_field,
    number_field,
    schema_errors,
    single_choice_field,
    text_field,
)

# --- normalize_decimal_comma ------------------------------------------------------------------------

NUMBER_CASES = [
    ("simple", "3,14", ("3.14", True)),
    ("comma-only", ",", (".", True)),
    ("leading-space", " 3,14", (" 3.14", True)),
    ("trailing-space", "3,14 ", ("3.14 ", True)),
    ("negative-exponent", "-2,5e3", ("-2.5e3", True)),
    ("fraction-like", "1/2,5", ("1/2.5", True)),
    ("thousands-and-point", "1,234.5", ("1,234.5", False)),
    ("two-commas", "1,2,3", ("1,2,3", False)),
    ("point-only", "3.14", ("3.14", False)),
    ("comma-and-point", "3,1.4", ("3,1.4", False)),
    ("no-separator", "42", ("42", False)),
    ("empty", "", ("", False)),
    ("fullwidth-comma", "3，14", ("3，14", False)),
    ("arabic-comma", "3،14", ("3،14", False)),
]


@pytest.mark.parametrize(("literal", "expected"), [case[1:] for case in NUMBER_CASES],
                         ids=[case[0] for case in NUMBER_CASES])
def test_number_field_normalizes_exactly_one_comma_and_no_point(literal: str,
                                                                 expected: tuple[str, bool]) -> None:
    """num campo `number`, exatamente uma vírgula U+002C e nenhum ponto → a
    vírgula vira ponto e `True` (`,` → `.` e ` 3,14` → ` 3.14`: só a contagem, nada é aparado); toda
    outra entrada (`1,234.5`, `1,2,3`, sem vírgula, vazia, outra vírgula Unicode) volta igual, com
    `False`."""
    result = answers.normalize_decimal_comma("number", literal)
    assert result == expected
    assert type(result) is tuple and type(result[1]) is bool


@pytest.mark.parametrize("field_type", ["integer", "text", "string", "single_choice", "multiple_choice"])
@pytest.mark.parametrize("literal", ["3,14", ",", "1,5", "a,b", " 2,0 "])
def test_other_field_types_never_normalize(field_type: str, literal: str) -> None:
    """`integer`, `text` (a `string` do PL) e os de escolha nunca normalizam: a entrada
    volta igual, com `False`, mesmo com uma só vírgula e nenhum ponto."""
    assert answers.normalize_decimal_comma(field_type, literal) == (literal, False)


# --- raw_for_engine -----------------------------------------------------------------------------------


def _ir(*fields: Any) -> dict[str, Any]:
    """A IR de uma variante do motor falso com estes campos, validada contra o esquema do motor de questões."""
    question = FakeQuestion(title="Questão de teste", statement="Responda.", fields=tuple(fields))
    _snapshot, ir, _warnings = build_variant(question, "1abc")
    assert schema_errors("ir", ir) == []
    return ir


def _raw(ir: dict[str, Any], raw_input: dict[str, Any], normalized: list[str]) -> dict[str, Any]:
    """`raw_for_engine`, conferindo que a entrada literal e a lista de normalizados não mudam."""
    before_input = copy.deepcopy(raw_input)
    before_normalized = list(normalized)
    result = answers.raw_for_engine(ir, raw_input, normalized)
    assert raw_input == before_input, "a entrada literal mudou"
    assert normalized == before_normalized
    assert type(result) is dict
    return result


# A múltipla usa as opções `a` a `d` (PRIME_OPTIONS), nessa ordem.
MULTIPLE = multiple_choice_field("marcas", min_select=0, max_select=4, allow_blank=True)
SINGLE = single_choice_field("cor")

BUTTON_MULTIPLE_CASES = [
    ("two-in-option-order", ["a", "c"], ["a", "c"]),
    ("toggled-out-of-order", ["c", "a"], ["a", "c"]),
    ("all-reversed", ["d", "c", "b", "a"], ["a", "b", "c", "d"]),
    ("one-mark-as-text", ["b"], "b"),
]


@pytest.mark.parametrize(("selection", "expected"), [case[1:] for case in BUTTON_MULTIPLE_CASES],
                         ids=[case[0] for case in BUTTON_MULTIPLE_CASES])
def test_multiple_choice_by_taps_goes_in_option_order(selection: list[str], expected: Any) -> None:
    """a múltipla pelos botões (a seleção, uma lista) vai como a lista de chaves na
    ordem das opções; uma só marcação vai como texto (o formato do formulário do PL)."""
    ir = _ir(MULTIPLE)
    assert _raw(ir, {"marcas": selection}, []) == {"marcas": expected}


def test_multiple_choice_with_empty_selection_is_absent() -> None:
    """a seleção vazia vai como campo **ausente** (o PL grava assim o
    formulário sem marcação), e os outros campos continuam."""
    ir = _ir(MULTIPLE, SINGLE)
    assert _raw(ir, {"marcas": [], "cor": "b"}, []) == {"cor": "b"}


TYPED_MULTIPLE_CASES = [
    # O que o PL aceita: `set("ab") = {a, b}` e `set("ca") = {a, c}` (sem reordenar para `ac`).
    ("glued-keys", "ab"),
    ("glued-reversed", "ca"),
    ("single-key", "a"),
    ("glued-repeated", "aa"),
    # O que o PL recusa com a própria mensagem ("You selected an invalid option"): vai como veio.
    ("comma", "b,a"),
    ("space", "a b"),
    ("comma-and-spaces", " c ,  a "),
    ("unknown-key", "z"),
    ("uppercase", "A"),
    ("key-and-unknown", "az"),
    # Sem nenhuma chave: também literal, nunca campo ausente (sem descarte).
    ("empty", ""),
    ("space-only", " "),
    ("comma-only", ","),
]


@pytest.mark.parametrize("typed", [case[1] for case in TYPED_MULTIPLE_CASES],
                         ids=[case[0] for case in TYPED_MULTIPLE_CASES])
def test_typed_multiple_choice_goes_literal_to_the_engine(typed: str) -> None:
    """Fidelidade ao PL (na adaptação de canal
    da escolha múltipla): a múltipla digitada (texto) vai ao motor **sem nenhuma mudança**, como o valor único de
    um formulário do PL: nem dividida (`b,a` e `a b` ficam como estão), nem reordenada (`ca` não vira
    `ac` nem `["a","c"]`), nem convertida em lista (`ab` não vira `["a","b"]` nem `["ab"]`), nem
    descartada (`""`, `" "` e `","` vão presentes, e não como campo ausente). Os outros campos
    continuam. O `pl-checkbox` do PL decide: `set("ab")` é {a, b}; `set("b,a")` tem `,`."""
    ir = _ir(MULTIPLE, SINGLE)
    result = _raw(ir, {"marcas": typed, "cor": "a"}, [])
    assert result == {"marcas": typed, "cor": "a"}
    assert type(result["marcas"]) is str


def test_typed_multiple_choice_is_never_touched_by_the_decimal_comma() -> None:
    """Vírgula decimal (controle): `b,a` tem exatamente uma vírgula e nenhum ponto, a forma que a
    normalização da vírgula decimal muda num `number`; num campo de escolha múltipla, a vírgula fica (`b,a`, e não `b.a`), e
    a normalização não se aplica a ele (`False`)."""
    assert answers.normalize_decimal_comma("multiple_choice", "b,a") == ("b,a", False)
    ir = _ir(number_field("x"), MULTIPLE)
    assert _raw(ir, {"x": "6,5", "marcas": "b,a"}, ["x"]) == {"x": "6.5", "marcas": "b,a"}


def test_typed_and_tapped_multiple_choice_in_the_same_question() -> None:
    """Controle: numa questão com duas múltiplas, a digitada (texto) vai literal
    (`db`, na ordem digitada) e a dos botões (lista) vai na ordem das opções (`["d","b"]` →
    `["b","d"]`): só a seleção pelos botões é refeita no formato do formulário do PL."""
    ir = _ir(MULTIPLE, multiple_choice_field("outras", min_select=0, max_select=4, allow_blank=True))
    assert _raw(ir, {"marcas": "db", "outras": ["d", "b"]}, []) == {"marcas": "db", "outras": ["b", "d"]}


@pytest.mark.parametrize("value", ["b", "zzz", " B ", "a,b", "Azul"],
                         ids=["button-key", "unknown-key", "spaced-uppercase", "two-keys", "label"])
def test_single_choice_goes_as_key_or_typed_literal(value: str) -> None:
    """a escolha única vai como veio: a chave pelo botão, ou o
    texto digitado literal (uma chave inexistente, com espaço ou maiúscula, vai como está, e o PL a
    recusa)."""
    ir = _ir(SINGLE)
    assert _raw(ir, {"cor": value}, []) == {"cor": value}


def test_typed_fields_go_literal_except_the_normalized_comma() -> None:
    """`number`, `integer` e `text` vão com o texto literal
    (nada aparado nem convertido); só o `number` listado em `normalized_fields` tem a vírgula trocada
    por ponto; a vírgula de um `integer` ou de um `text` vai como está; nenhum campo fora da IR."""
    ir = _ir(number_field("velocidade"), integer_field("quantidade"), text_field("cor"),
             number_field("massa"))
    raw_input = {"velocidade": " 3,14", "quantidade": "1,5", "cor": " Azul, claro ", "massa": "1,234.5"}
    literal, normalized = answers.normalize_decimal_comma("number", raw_input["velocidade"])
    assert (literal, normalized) == (" 3.14", True)
    assert answers.normalize_decimal_comma("number", raw_input["massa"]) == ("1,234.5", False)
    assert _raw(ir, raw_input, ["velocidade"]) == {
        "velocidade": " 3.14", "quantidade": "1,5", "cor": " Azul, claro ", "massa": "1,234.5"}


def test_multipart_body_is_exact_and_deterministic() -> None:
    """numa questão de várias partes, o corpo tem exatamente os campos da IR
    com resposta, cada um no formato do PL (a vírgula normalizada, a escolha única pela chave, a
    múltipla pelos botões na ordem das opções), e a mesma entrada dá sempre o mesmo corpo."""
    ir = _ir(number_field("x"), SINGLE, MULTIPLE, text_field("nome"))
    raw_input = {"x": "6,5", "cor": "c", "marcas": ["d", "b"], "nome": "Ana"}
    expected = {"x": "6.5", "cor": "c", "marcas": ["b", "d"], "nome": "Ana"}
    assert _raw(ir, raw_input, ["x"]) == expected
    assert _raw(ir, raw_input, ["x"]) == expected
    assert _raw(ir, dict(reversed(list(raw_input.items()))), ["x"]) == expected
