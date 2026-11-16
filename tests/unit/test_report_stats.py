"""Funções puras do relatório.


- `reports.round_sig(x, digits=3) -> str`: o número com 3 algarismos significativos, escrito como
  `format(x, ".3g")` (as razões de `distribution.fields[].ratios`).
- `reports.normalize_text_answer(text) -> str`: `" ".join(text.split())`, isto é, as pontas aparadas e os
  espaços internos colapsados para todo espaço em branco do Python (tabulação, quebra de linha, espaço não
  separável), com a caixa preservada. É a única definição usada pelo cálculo para agrupar os textos:
  `"abc\\n"`, `"\\tabc"` e `"\\u00a0abc"` caem no grupo de `"abc"`.
- `reports.top_list(counts, *, limit=TOP_ITEMS, min_count=TOP_MIN_COUNT) -> TopList`: até `limit` itens
  com pelo menos `min_count` no lado "com atrasadas", em ordem de contagem com atrasadas decrescente e, no
  empate, pela cadeia (ordem de código do Python); os demais somados em `others`, em par.

Os valores esperados são literais escritos à mão, nunca recalculados pela fórmula do código. Os
quartis e a mediana saem do `percentile_cont` no SQL e não têm função pura: o teste deles fica em
`tests/integration/test_report_compute.py`.

No esqueleto, as três funções lançam `NotImplementedError`: todo caso falha por esse motivo. As
constantes reais do esqueleto (`TOP_ITEMS`, `TOP_MIN_COUNT`, `RATIO_DIGITS`) são
afirmadas na mesma função de um caso de `top_list`.
"""

from __future__ import annotations

import pytest

from cq_server import reports
from cq_server.reports import Pair, TopItem, TopList

# --- round_sig ---------------------------------------------------------------------------------------

# (identificador, valor, texto esperado): literais de `format(x, ".3g")`, escritos à mão.
ROUND_SIG_CASES = [
    ("one", 1, "1"),
    ("one-float", 1.0, "1"),
    ("half", 0.5, "0.5"),
    ("three-digits", 1.23456, "1.23"),
    ("rounds-up", 1.0769230769230769, "1.08"),
    ("two-thirds", 2 / 3, "0.667"),
    ("thousands-exponent", 1234.5, "1.23e+03"),
    ("hundred", 100.0, "100"),
    ("small", 0.000123, "0.000123"),
    ("zero", 0.0, "0"),
    ("negative", -1.23456, "-1.23"),
    ("negative-half", -0.5, "-0.5"),
    ("negative-ratio", -0.46153846153846156, "-0.462"),
    ("negative-small", -0.000987654, "-0.000988"),
]


@pytest.mark.parametrize(("value", "expected"), [c[1:] for c in ROUND_SIG_CASES],
                         ids=[c[0] for c in ROUND_SIG_CASES])
def test_round_sig_writes_three_significant_digits(value: float, expected: str) -> None:
    assert reports.round_sig(value) == expected


def test_round_sig_default_digits_is_three() -> None:
    """O padrão de `digits` é 3: com `digits=3` explícito, o mesmo texto; com outro valor, outro texto."""
    assert reports.round_sig(1.23456, 3) == reports.round_sig(1.23456) == "1.23"
    assert reports.round_sig(1.23456, 2) == "1.2"
    assert reports.round_sig(1.23456, 4) == "1.235"


# --- normalize_text_answer ---------------------------------------------------------------------------

NBSP = " "

# (identificador, entrada, saída esperada)
NORMALIZE_CASES = [
    ("trailing-newline", "abc\n", "abc"),
    ("leading-tab", "\tabc", "abc"),
    ("trailing-space", "abc ", "abc"),
    ("leading-space", " abc", "abc"),
    ("crlf-and-spaces", "  abc\r\n", "abc"),
    ("nbsp-edges", f"{NBSP}abc{NBSP}", "abc"),
    ("inner-double-space", "a  b", "a b"),
    ("inner-tab", "a\tb", "a b"),
    ("inner-newline", "a\nb", "a b"),
    ("inner-nbsp", f"a{NBSP}b", "a b"),
    ("inner-mixed", f"a \t{NBSP}\n b", "a b"),
    ("case-preserved", "Abc", "Abc"),
    ("plain", "abc", "abc"),
    ("empty", "", ""),
    ("only-blanks", f" \t{NBSP}\n", ""),
]


@pytest.mark.parametrize(("text", "expected"), [c[1:] for c in NORMALIZE_CASES],
                         ids=[c[0] for c in NORMALIZE_CASES])
def test_normalize_text_answer_trims_and_collapses_whitespace(text: str, expected: str) -> None:
    assert reports.normalize_text_answer(text) == expected


def test_normalize_text_answer_groups_variants_and_keeps_case() -> None:
    """As variantes de espaço de `"abc"` caem num único grupo; `"Abc"` fica num grupo próprio."""
    variants = ["abc", "abc\n", "\tabc", "abc ", " abc", f"{NBSP}abc", f"abc{NBSP}", "\r\nabc\t"]
    assert {reports.normalize_text_answer(v) for v in variants} == {"abc"}
    assert reports.normalize_text_answer("Abc") == "Abc"
    assert reports.normalize_text_answer("Abc") != reports.normalize_text_answer("abc")
    assert reports.normalize_text_answer(" ABC ") == "ABC"


# --- top_list ----------------------------------------------------------------------------------------


def test_top_list_minimum_is_on_the_with_late_side_and_constants() -> None:
    """O mínimo vale no lado "com atrasadas": um item com 1 no prazo e 2 com atrasadas entra; um com 1 e 1
    não entra e vai para "outras". Com as constantes reais do esqueleto."""
    counts = {
        "x": Pair(1, 2),   # 1 no prazo, 2 com atrasadas: entra
        "y": Pair(1, 1),   # abaixo do mínimo: "outras"
        "z": Pair(0, 3),   # nenhuma no prazo, 3 com atrasadas: entra
        "w": Pair(2, 2),   # 2 e 2: entra
    }
    assert reports.top_list(counts) == TopList(
        items=(TopItem("z", Pair(0, 3)), TopItem("w", Pair(2, 2)), TopItem("x", Pair(1, 2))),
        others=Pair(1, 1))
    # Constantes reais do esqueleto: o padrão de `top_list` e o de `round_sig`.
    assert reports.TOP_ITEMS == 5
    assert reports.TOP_MIN_COUNT == 2
    assert reports.RATIO_DIGITS == 3


def test_top_list_keeps_at_most_five_and_sums_the_rest() -> None:
    """No máximo 5 itens; as demais (inclusive as abaixo do mínimo) somadas em "outras", em par."""
    counts = {
        "a": Pair(7, 9),
        "b": Pair(6, 8),
        "c": Pair(5, 7),
        "d": Pair(4, 6),
        "e": Pair(3, 5),
        "f": Pair(2, 4),   # sexto com o mínimo: "outras"
        "g": Pair(1, 3),   # sétimo: "outras"
        "h": Pair(1, 1),   # abaixo do mínimo: "outras"
    }
    assert reports.top_list(counts) == TopList(
        items=(TopItem("a", Pair(7, 9)), TopItem("b", Pair(6, 8)), TopItem("c", Pair(5, 7)),
               TopItem("d", Pair(4, 6)), TopItem("e", Pair(3, 5))),
        others=Pair(2 + 1 + 1, 4 + 3 + 1))


def test_top_list_breaks_ties_by_string() -> None:
    """Empate na contagem com atrasadas: pela cadeia, em ordem de código (`"-3" < "1e400" < "6.5"`,
    maiúscula antes de minúscula). O lado "no prazo" não desempata."""
    counts = {
        "b": Pair(2, 2),
        "a": Pair(0, 2),
        "c": Pair(1, 3),
        "Abc": Pair(1, 2),
        "6.5": Pair(2, 2),
        "-3": Pair(1, 2),
        "1e400": Pair(1, 2),
    }
    result = reports.top_list(counts, limit=10)
    assert result == TopList(
        items=(TopItem("c", Pair(1, 3)), TopItem("-3", Pair(1, 2)), TopItem("1e400", Pair(1, 2)),
               TopItem("6.5", Pair(2, 2)), TopItem("Abc", Pair(1, 2)), TopItem("a", Pair(0, 2)),
               TopItem("b", Pair(2, 2))),
        others=Pair(0, 0))


def test_top_list_tie_at_the_cut_goes_by_string() -> None:
    """O corte em 5 respeita o desempate pela cadeia: com 6 itens empatados, o maior na ordem de código
    vai para "outras"."""
    counts = {name: Pair(1, 2) for name in ("f", "e", "d", "c", "b", "a")}
    assert reports.top_list(counts) == TopList(
        items=tuple(TopItem(name, Pair(1, 2)) for name in ("a", "b", "c", "d", "e")),
        others=Pair(1, 2))


def test_top_list_empty_has_zero_others() -> None:
    assert reports.top_list({}) == TopList(items=(), others=Pair(0, 0))


def test_top_list_nothing_reaches_the_minimum() -> None:
    """Nenhum item com o mínimo: lista vazia e tudo em "outras"."""
    counts = {"a": Pair(1, 1), "b": Pair(0, 1), "c": Pair(1, 1)}
    assert reports.top_list(counts) == TopList(items=(), others=Pair(2, 3))


def test_top_list_explicit_limit_and_minimum() -> None:
    """`limit` e `min_count` explícitos substituem os padrões."""
    counts = {"a": Pair(3, 4), "b": Pair(1, 3), "c": Pair(2, 2), "d": Pair(0, 5)}
    assert reports.top_list(counts, limit=2, min_count=3) == TopList(
        items=(TopItem("d", Pair(0, 5)), TopItem("a", Pair(3, 4))),
        others=Pair(1 + 2, 3 + 2))
    assert reports.top_list(counts, limit=5, min_count=1) == TopList(
        items=(TopItem("d", Pair(0, 5)), TopItem("a", Pair(3, 4)), TopItem("b", Pair(1, 3)),
               TopItem("c", Pair(2, 2))),
        others=Pair(0, 0))
