"""Textos fixos do bot no disparo e na resposta.


`cq_server.deliverytexts`, módulo próprio, fora de `botmessages.compose` (o `compose` e o menu ficam fixos):
- as constantes de texto, exatas, em português e sem detalhe técnico; as que confirmam um
  toque cabem em 200 unidades UTF-16; `FORMAT_ERROR` e `MEDIA_UNAVAILABLE` são modelos de
  `str.format` (`{explanation}` e `{n}`); os controles ("Não sei", "Enviar") e o
  comando `/naosei`; os vereditos do feedback, com o `VERDICT_PARTIAL`; o modelo
  `SCORE` ("nota {n}%", do feedback de nota parcial) e, do modo desligado das mensagens livres, a constante
  `ANSWERS_ONLY_REPLY` e o modelo `CONTACT_HOWTO` (com `{email}`); as composições `answers_only_reply` e
  `use_reply_masked` ficam em `test_delivery_texts_offering_contact.py`;
- `feedback(echoes, *, score, part_scores=None)`: com um eco, "Sua resposta: {eco} · {veredito} ·
  {nota}"; com vários, uma linha "Parte {k}: {eco} · {✅|◐|❌}" por parte (pela nota da parte) e a
  linha "Resultado: {veredito} · {nota}" (pela nota geral), separadas por `\\n` (feedback de nota parcial,
  o modelo `FEEDBACK`). O veredito tem as três faixas
  do `determine_score_params` do PL: "✅ correta" com nota ≥ 1 (inclusive acima de 1), "◐ parcialmente
  correta" com 0 < nota < 1 e "❌ incorreta" com nota ≤ 0; a marca da parte segue as mesmas faixas;
  a nota ("nota N%", o modelo `SCORE`) sai em toda correção válida, com o número do
  selo do PL: 100 com nota ≥ 1, ⌊nota × 100⌋ na parcial e 0 com nota ≤ 0; nunca em decimal.
  Os ecos chegam já truncados e escapados por quem compõe, com as marcas ✅/❌ por
  alternativa já postas depois do texto de cada escolhida, e o `feedback` os leva sem perder
  nem acrescentar marca.
  As auxiliares
  `verdict(score)`, `part_mark(score)`, `score_text(score)` e `option_mark(correct)` (as
  assinaturas de `deliverytexts`) seguem as mesmas regras;
- `progress(k, n)`: "Parte {k} de {n}";
- `field_request(k, n, label, suffix)`: "Campo {k} de {n}: {rótulo} {sufixo} — digite a resposta.",
  com o rótulo e o sufixo já escapados, separados por um espaço ("<rótulo>
  <sufixo>"), e só o rótulo quando não há sufixo;
- `deadline(moment, zone)`: "Prazo: " seguido do instante no fuso do oferecimento, no formato já usado
  (`timezones.format_local`: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`);
- o `{echo}` de um campo de escolha: a chave e o texto de cada alternativa escolhida e de
  nenhuma outra; quem compõe o eco o entrega pronto ao `feedback`, que o leva sem perder nem
  acrescentar alternativa;
- `TYPED_MULTIPLE_HOWTO`, a dica da múltipla degradada, exata. Ela
  não existe no esqueleto: o caso falha pela constante ausente (contrato não cumprido).

As assinaturas de `feedback` e `field_request` são detalhe de implementação
(`feedback(...)`, `field_request(...)`); este teste as fixa [I]. Os textos esperados são literais escritos
aqui, e não as constantes do módulo. As constantes, reais no esqueleto, são
afirmadas na mesma função de uma chamada que lança `NotImplementedError` no esqueleto; o
`botmessages` intocado (os nomes que dão `KeyError` e o menu), na mesma função de um caso
de `feedback`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from cq_server import botmessages, deliverytexts
from cq_server.timezones import format_local

UTC = timezone.utc

# os textos fixos, literais.
CONSTANTS = {
    "ALREADY_ANSWERED": "Você já respondeu a esta questão.",
    "GRADING_DELAYED": "Resposta recebida; a correção chega em instantes.",
    "GRADING_IN_PROGRESS": "Sua resposta está sendo corrigida.",
    "ANSWER_KEPT": "Sua resposta foi guardada.",
    "QUESTION_FAILED": ("Esta questão teve um problema e não conseguiu corrigir a sua resposta. A sua "
                        "resposta foi guardada."),
    "UNAVAILABLE": "Esta questão não está disponível.",
    "PART_ALREADY_ANSWERED": "Esta parte já foi respondida.",
    "REPLY_WITH_TEXT": "Responda com texto.",
    "WINDOW_CLOSED": "A janela desta questão fechou.",
    "DONT_KNOW_RECORDED": 'Registrado: "Não sei".',
    "CLOSED_NOTICE": "Janela fechada.",
    "LATE_HOWTO": ('Você ainda pode responder, fora do prazo: use os botões ou toque em "Responder" '
                   'nesta mensagem.'),
    "FREE_MESSAGE_REPLY": ('Recebi a sua mensagem, mas ela não foi tomada como resposta. Para responder, '
                           'use os botões da questão ou toque em "Responder" na mensagem da questão.'),
    "USE_REPLY": ('Há mais de uma questão aberta esperando texto. Toque em "Responder" na mensagem da '
                  'questão certa e digite a resposta.'),
    "DONT_KNOW_HOWTO": 'Para responder "Não sei", responda a esta mensagem com /naosei.',
    "VERDICT_CORRECT": "✅ correta",
    "VERDICT_INCORRECT": "❌ incorreta",
    # O veredito da nota parcial.
    "VERDICT_PARTIAL": "◐ parcialmente correta",
    # A resposta do modo desligado, sozinha quando o oferecimento não tem contato.
    "ANSWERS_ONLY_REPLY": "Este canal recebe só respostas às questões.",
    "DONT_KNOW_BUTTON": "Não sei",
    "SUBMIT_BUTTON": "Enviar",
    "DONT_KNOW_COMMAND": "/naosei",
}

# As respostas curtas que confirmam um toque ou respondem a uma entrada.
TAP_CONFIRMATIONS = ("ALREADY_ANSWERED", "GRADING_DELAYED", "GRADING_IN_PROGRESS", "ANSWER_KEPT",
                     "QUESTION_FAILED", "UNAVAILABLE", "PART_ALREADY_ANSWERED", "REPLY_WITH_TEXT",
                     "WINDOW_CLOSED", "DONT_KNOW_RECORDED")

# Detalhe técnico que nenhum texto ao aluno pode ter.
_TECHNICAL = re.compile(
    r"\b(HTTP|API|PL|PrairieLearn|motor|engine|servidor|server|erro|error|exce\w*|traceback|null|None|"
    r"JSON|token|id|status|timeout|seed|IR|webhook|callback)\b|[{}<>]", re.IGNORECASE)

# Os nomes novos que `botmessages.compose` não conhece (dão `KeyError`).
# `SCORE` e `CONTACT_HOWTO` são modelos com chaves: ficam fora de `CONSTANTS`, como o
# `FORMAT_ERROR`, e entram aqui à parte.
DELIVERY_TEXT_NAMES = [*CONSTANTS, "FEEDBACK", "PROGRESS", "FIELD_REQUEST", "FORMAT_ERROR", "DEADLINE",
            "MEDIA_UNAVAILABLE", "TYPED_MULTIPLE_HOWTO", "SCORE", "CONTACT_HOWTO"]

# A dica da múltipla degradada, literal.
TYPED_MULTIPLE_HOWTO = "Digite as letras das alternativas juntas, sem separador (ex.: ab)."

# O menu dos bots, sem nenhum comando novo (o `/naosei` não entra no menu).
BOT_COMMANDS = [("start", "Começar ou usar um link de convite"), ("ajuda", "Ver como usar este bot")]


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_fixed_constants_are_exact_portuguese_and_short() -> None:
    """cada constante exata; nenhuma com detalhe
    técnico; as de confirmação de toque em até 200 unidades UTF-16; os modelos `FORMAT_ERROR` e
    `MEDIA_UNAVAILABLE` exatos; afirmadas junto de `progress(k, n)` ("Parte {k} de {n}").
    Nota parcial: `VERDICT_PARTIAL` entre as constantes e o modelo `SCORE` formatado
    ("nota 50%"). Modo desligado: `ANSWERS_ONLY_REPLY` entre as constantes e o
    modelo `CONTACT_HOWTO` formatado, com o e-mail literal."""
    for name, text in CONSTANTS.items():
        assert getattr(deliverytexts, name) == text, name
    for name, text in CONSTANTS.items():
        if name == "DONT_KNOW_COMMAND":
            continue
        assert not _TECHNICAL.search(text), (name, _TECHNICAL.findall(text))
    for name in TAP_CONFIRMATIONS:
        assert _utf16(getattr(deliverytexts, name)) <= 200, name
    assert deliverytexts.FORMAT_ERROR.format(explanation="A resposta está em branco.") == (
        "Resposta não aceita (não conta como tentativa): A resposta está em branco.")
    assert deliverytexts.MEDIA_UNAVAILABLE.format(n=2) == "[Figura 2 indisponível]"
    assert deliverytexts.SCORE.format(n=50) == "nota 50%"
    assert deliverytexts.CONTACT_HOWTO.format(email="prof.livres@docentes.example.org") == (
        "Para falar com o professor, escreva para prof.livres@docentes.example.org.")
    assert deliverytexts.progress(2, 3) == "Parte 2 de 3"
    assert deliverytexts.progress(1, 1) == "Parte 1 de 1"
    assert deliverytexts.progress(10, 12) == "Parte 10 de 12"


@pytest.mark.parametrize(
    ("score", "expected"),
    [(1.0, "Sua resposta: 3,14 · ✅ correta · nota 100%"),
     (1.5, "Sua resposta: 3,14 · ✅ correta · nota 100%"),
     (0.999, "Sua resposta: 3,14 · ◐ parcialmente correta · nota 99%"),
     (0.5, "Sua resposta: 3,14 · ◐ parcialmente correta · nota 50%"),
     (0.29, "Sua resposta: 3,14 · ◐ parcialmente correta · nota 28%"),
     (0.0, "Sua resposta: 3,14 · ❌ incorreta · nota 0%"),
     (-0.25, "Sua resposta: 3,14 · ❌ incorreta · nota 0%")],
    ids=["one", "above-one", "just-below-one", "partial", "floor-like-pl", "zero", "negative"])
def test_single_part_feedback_has_three_bands_and_always_the_score(score: float, expected: str) -> None:
    """Feedback de nota parcial:
    com uma parte, "Sua resposta: {eco} · {veredito} ·
    nota N%", nas três faixas do PL ("✅ correta" com nota ≥ 1, inclusive acima de 1; "◐ parcialmente
    correta" com 0 < nota < 1; "❌ incorreta" com nota ≤ 0, inclusive negativa), e a nota em toda
    correção válida, com o número do selo do PL: ⌊nota × 100⌋ na parcial (0,29 → 28, o `math.floor` do
    `determine_score_params`), 100 com nota ≥ 1 e 0 com nota ≤ 0; nunca a nota em decimal; na mesma
    função, o `botmessages` intocado: `compose` dá `KeyError` com os nomes novos e o menu
    continua só `start` e `ajuda`.
    A nota sai em toda correção válida, não só na parcial.
    """
    text = deliverytexts.feedback(["3,14"], score=score)
    assert text == expected
    assert str(score) not in text and "0,5" not in text
    for name in DELIVERY_TEXT_NAMES:
        with pytest.raises(KeyError):
            botmessages.compose(name, course="Cálculo I", term="2031s1")
    assert [(c.command, c.description) for c in botmessages.COMMANDS] == BOT_COMMANDS


@pytest.mark.parametrize(
    ("echoes", "score", "part_scores", "lines"),
    [(["6,5", "Azul"], 0.5, [1.0, 0.0],
      ["Parte 1: 6,5 · ✅", "Parte 2: Azul · ❌", "Resultado: ◐ parcialmente correta · nota 50%"]),
     (["6,5", "Azul", "Dois, Sete"], 1.0, [1.0, 1.0, 1.0],
      ["Parte 1: 6,5 · ✅", "Parte 2: Azul · ✅", "Parte 3: Dois, Sete · ✅",
       "Resultado: ✅ correta · nota 100%"]),
     (["42", "Não sei"], 0.25, [0.5, 0.0],
      ["Parte 1: 42 · ◐", "Parte 2: Não sei · ❌", "Resultado: ◐ parcialmente correta · nota 25%"]),
     (["1", "2"], 1.2, [1.2, 1.0],
      ["Parte 1: 1 · ✅", "Parte 2: 2 · ✅", "Resultado: ✅ correta · nota 100%"])],
    ids=["one-of-two", "all-correct", "partial-parts", "above-one"])
def test_multipart_feedback_has_one_line_per_part_and_the_result(
        echoes: list[str], score: float, part_scores: list[float], lines: list[str]) -> None:
    """Feedback de nota parcial (o símbolo, as faixas e a forma
    da nota): nas várias partes, uma linha "Parte {k}: {eco} · {✅|◐|❌}" por parte, nas três faixas pela
    nota da parte (a parte com 0,5 traz ◐), e "Resultado: {veredito} · nota N%" pela nota geral
    (a nota sempre, com o teto do selo do PL: 1,2 → "nota 100%"), na ordem das partes, separadas por
    `\\n`; a nota só na linha do resultado, nunca numa linha de parte.
    """
    text = deliverytexts.feedback(echoes, score=score, part_scores=part_scores)
    assert text == "\n".join(lines)
    assert all("%" not in line and "nota" not in line for line in text.split("\n")[:-1])


def test_typed_multiple_howto_is_exact() -> None:
    """a dica da múltipla digitada, exata, em
    português, sem detalhe técnico e sem separador sugerido além do exemplo de letras juntas."""
    text = getattr(deliverytexts, "TYPED_MULTIPLE_HOWTO", None)
    assert text == TYPED_MULTIPLE_HOWTO
    assert not _TECHNICAL.search(text), _TECHNICAL.findall(text)


def _has_key(text: str, key: str) -> bool:
    """A chave como símbolo isolado: nem letra nem dígito dos dois lados."""
    return re.search(rf"(?<![0-9A-Za-zÀ-ÿ]){re.escape(key)}(?![0-9A-Za-zÀ-ÿ])", text) is not None


# As alternativas da variante: chave → texto.
_SINGLE_OPTIONS = {"a": "Vermelho", "b": "Azul", "c": "Verde"}
_MULTIPLE_OPTIONS = {"a": "Dois", "b": "Cinco", "c": "Sete"}


@pytest.mark.parametrize(
    ("echo", "chosen", "options", "score", "expected"),
    [("(b) Azul", ["b"], _SINGLE_OPTIONS, 1.0, "Sua resposta: (b) Azul · ✅ correta · nota 100%"),
     ("(a) Vermelho", ["a"], _SINGLE_OPTIONS, 0.0,
      "Sua resposta: (a) Vermelho · ❌ incorreta · nota 0%"),
     ("(a) Dois, (c) Sete", ["a", "c"], _MULTIPLE_OPTIONS, 1.0,
      "Sua resposta: (a) Dois, (c) Sete · ✅ correta · nota 100%"),
     ("(a) Dois, (c) Sete", ["a", "c"], _MULTIPLE_OPTIONS, 0.5,
      "Sua resposta: (a) Dois, (c) Sete · ◐ parcialmente correta · nota 50%")],
    ids=["single-correct", "single-incorrect", "multiple-a-c-correct", "multiple-a-c-partial"])
def test_choice_echo_in_feedback_has_only_the_chosen_options(
        echo: str, chosen: list[str], options: dict[str, str], score: float, expected: str) -> None:
    """No feedback de um campo de escolha, o eco
    traz a chave (símbolo isolado) e o texto de cada alternativa escolhida e de nenhuma outra; o
    `feedback` leva o eco composto sem perder nem acrescentar alternativa. Nota parcial
    (com o cabeçalho): o veredito nas três faixas e a nota em toda correção válida (a múltipla
    com 0,5 → "◐ parcialmente correta · nota 50%")."""
    text = deliverytexts.feedback([echo], score=score)
    assert text == expected
    for key, label in options.items():
        if key in chosen:
            assert _has_key(text, key), (key, text)
            assert label in text, (label, text)
        else:
            assert not _has_key(text, key), (key, text)
            assert label not in text, (label, text)


def test_choice_echo_in_multipart_feedback_keeps_each_part_apart() -> None:
    """nas várias partes, a linha da parte de escolha traz só as escolhidas
    (a e c), e nenhuma outra linha traz alternativa da escolha. Nota parcial: o
    resultado com a nota 0,5 é "◐ parcialmente correta · nota 50%"."""
    text = deliverytexts.feedback(["6,5", "(a) Dois, (c) Sete", "(b) Azul"], score=0.5,
                                  part_scores=[1.0, 0.0, 1.0])
    assert text == "\n".join(["Parte 1: 6,5 · ✅", "Parte 2: (a) Dois, (c) Sete · ❌",
                               "Parte 3: (b) Azul · ✅",
                               "Resultado: ◐ parcialmente correta · nota 50%"])
    lines = text.split("\n")
    assert _has_key(lines[1], "a") and _has_key(lines[1], "c") and not _has_key(lines[1], "b")
    assert "Dois" in lines[1] and "Sete" in lines[1] and "Cinco" not in text
    assert _has_key(lines[2], "b") and "Azul" in lines[2]
    assert not any(label in lines[2] for label in ("Vermelho", "Verde"))
    assert not _has_key(lines[2], "a") and not _has_key(lines[2], "c")


def test_feedback_shows_the_score_and_keeps_the_option_marks() -> None:
    """A nota e as marcas (sem o parâmetro `show_score`, a nota
    em toda correção válida; teto e piso do selo do PL; marcas por alternativa);
    `FEEDBACK` e `SCORE`: "Sua resposta: (b) Azul · ✅ correta · nota 100%" com nota 1
    e com 1,5 (o teto: nunca "nota 150%"), "❌ incorreta · nota 0%" com 0 e com -0,25 (o piso: nunca
    "nota -25%"); o eco com as marcas por alternativa, composto por quem chama ("(a) Dois ✅, (b) Cinco
    ❌"), sai inteiro, sem perder nem acrescentar marca, seguido de "◐ parcialmente correta · nota 50%";
    nas várias partes, o eco marcado fica na linha da parte, e a nota só no resultado."""
    assert deliverytexts.feedback(["(b) Azul"], score=1.5) == (
        "Sua resposta: (b) Azul · ✅ correta · nota 100%")
    assert deliverytexts.feedback(["(b) Azul"], score=1.0) == (
        "Sua resposta: (b) Azul · ✅ correta · nota 100%")
    assert deliverytexts.feedback(["(b) Azul"], score=0.0) == (
        "Sua resposta: (b) Azul · ❌ incorreta · nota 0%")
    assert deliverytexts.feedback(["(b) Azul"], score=-0.25) == (
        "Sua resposta: (b) Azul · ❌ incorreta · nota 0%")
    for score in (1.5, -0.25):
        text = deliverytexts.feedback(["(b) Azul"], score=score)
        assert not any(bad in text for bad in ("150", "1,5", "1.5", "-25", "0,25", "0.25")), text

    marked = "(a) Dois ✅, (b) Cinco ❌"
    text = deliverytexts.feedback([marked], score=0.5)
    assert text == "Sua resposta: (a) Dois ✅, (b) Cinco ❌ · ◐ parcialmente correta · nota 50%"
    assert text.count("✅") == 1 and text.count("❌") == 1

    lines = deliverytexts.feedback(["6,5", "(a) Dois ✅, (c) Sete ❌"], score=0.5,
                                   part_scores=[1.0, 0.0]).split("\n")
    assert lines == ["Parte 1: 6,5 · ✅", "Parte 2: (a) Dois ✅, (c) Sete ❌ · ❌",
                     "Resultado: ◐ parcialmente correta · nota 50%"]


def test_marks_and_verdicts_follow_the_pl_bands() -> None:
    """As marcas e os vereditos (◐ por parte; teto e piso do
    selo; as assinaturas de `deliverytexts`): `verdict(score)` nas três
    faixas do `determine_score_params` do PL ("✅ correta" com nota ≥ 1, "◐ parcialmente correta" com
    0 < nota < 1, "❌ incorreta" com nota ≤ 0); `part_mark(score)` nas mesmas faixas ("✅", "◐", "❌");
    `option_mark(correct)` ("✅" ou "❌"); `score_text(score)` com o número do selo do PL: ⌊nota × 100⌋
    na parcial (0,29 → "nota 28%"), "nota 100%" com nota ≥ 1 e "nota 0%" com nota ≤ 0. Os textos
    esperados são literais escritos aqui, e não as constantes do módulo."""
    verdict = deliverytexts.verdict
    part_mark = deliverytexts.part_mark
    option_mark = deliverytexts.option_mark
    score_text = deliverytexts.score_text
    for score in (1.0, 1.2):
        assert verdict(score) == "✅ correta", score
    for score in (0.999, 0.5, 0.001):
        assert verdict(score) == "◐ parcialmente correta", score
    for score in (0.0, -1):
        assert verdict(score) == "❌ incorreta", score
    for score in (1.0, 1.2):
        assert part_mark(score) == "✅", score
    for score in (0.999, 0.5, 0.001):
        assert part_mark(score) == "◐", score
    for score in (0.0, -1):
        assert part_mark(score) == "❌", score
    assert option_mark(True) == "✅"
    assert option_mark(False) == "❌"
    assert score_text(0.29) == "nota 28%"
    assert score_text(0.5) == "nota 50%"
    assert score_text(0.999) == "nota 99%"
    assert score_text(0.001) == "nota 0%"
    assert score_text(1.0) == "nota 100%"
    assert score_text(1.5) == "nota 100%"
    assert score_text(0.0) == "nota 0%"
    assert score_text(-0.25) == "nota 0%"


@pytest.mark.parametrize(
    ("args", "expected"),
    [((1, 2, "Velocidade", "m/s"), "Campo 1 de 2: Velocidade m/s — digite a resposta."),
     ((2, 3, "Distância", None), "Campo 2 de 3: Distância — digite a resposta."),
     ((3, 3, "x &lt; 1", "cm"), "Campo 3 de 3: x &lt; 1 cm — digite a resposta."),
     ((10, 12, "Quantidade", None), "Campo 10 de 12: Quantidade — digite a resposta.")],
    ids=["label-and-suffix", "label-only", "escaped-label", "two-digits"])
def test_field_request_is_exact(args: tuple, expected: str) -> None:
    """"Campo {k} de {n}: {rótulo} {sufixo} — digite a
    resposta.", com o rótulo e o sufixo como vieram (já escapados por quem compõe)."""
    assert deliverytexts.field_request(*args) == expected


@pytest.mark.parametrize(
    ("moment", "zone", "expected"),
    [(datetime(2031, 1, 6, 2, 59, tzinfo=UTC), "America/Sao_Paulo",
      "Prazo: 2031-01-05 23:59 -03 (America/Sao_Paulo)"),
     (datetime(2031, 1, 6, 2, 59, 59, tzinfo=UTC), "America/Manaus",
      "Prazo: 2031-01-05 22:59 -04 (America/Manaus)"),
     (datetime(2031, 7, 1, 12, 0, tzinfo=UTC), "UTC", "Prazo: 2031-07-01 12:00 UTC (UTC)")],
    ids=["sao-paulo", "manaus-seconds-truncated", "utc"])
def test_deadline_is_in_the_offering_timezone(moment: datetime, zone: str, expected: str) -> None:
    """"Prazo: " e o instante no fuso do oferecimento, no
    formato já usado (`timezones.format_local`), com os segundos truncados."""
    assert deliverytexts.deadline(moment, zone) == expected
    assert expected == "Prazo: " + format_local(moment, zone)
