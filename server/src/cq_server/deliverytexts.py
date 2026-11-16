"""Textos fixos do bot nas entregas: pedidos, confirmações, recusas, feedback e respostas às mensagens
livres.

Módulo próprio, fora de `botmessages.compose` (os nomes daqui dão `KeyError` lá; o `/naosei` fica fora do
menu dos bots). Todos em português, sem detalhe técnico; os de confirmação de toque cabem em 200 unidades
UTF-16. Valores de terceiros e o eco do aluno entram pelo `formatter.escape` de quem compõe.

Os textos com valores (`FORMAT_ERROR`, `MEDIA_UNAVAILABLE`, `SCORE`, `CONTACT_HOWTO`) são modelos de
`str.format`; os que dependem de mais regras são funções (`verdict`, `part_mark`, `option_mark`,
`score_text`, `feedback`, `answers_only_reply`, `use_reply_masked`, `progress`, `field_request`,
`deadline`).

Os ecos, rótulos e sufixos chegam às funções já truncados e escapados por quem compõe; aqui eles só
são postos no lugar.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from cq_server.timezones import format_local

# Confirmações de toque e respostas fixas.
ALREADY_ANSWERED = "Você já respondeu a esta questão."
GRADING_DELAYED = "Resposta recebida; a correção chega em instantes."
GRADING_IN_PROGRESS = "Sua resposta está sendo corrigida."
ANSWER_KEPT = "Sua resposta foi guardada."
QUESTION_FAILED = ("Esta questão teve um problema e não conseguiu corrigir a sua resposta. "
                   "A sua resposta foi guardada.")
UNAVAILABLE = "Esta questão não está disponível."
PART_ALREADY_ANSWERED = "Esta parte já foi respondida."
REPLY_WITH_TEXT = "Responda com texto."
WINDOW_CLOSED = "A janela desta questão fechou."
DONT_KNOW_RECORDED = 'Registrado: "Não sei".'
CLOSED_NOTICE = "Janela fechada."
LATE_HOWTO = ('Você ainda pode responder, fora do prazo: use os botões ou toque em "Responder" nesta '
              'mensagem.')
FREE_MESSAGE_REPLY = ('Recebi a sua mensagem, mas ela não foi tomada como resposta. Para responder, use os '
                      'botões da questão ou toque em "Responder" na mensagem da questão.')
USE_REPLY = ('Há mais de uma questão aberta esperando texto. Toque em "Responder" na mensagem da questão '
             'certa e digite a resposta.')
# Modo desligado das mensagens livres: a resposta fixa, sozinha quando o
# oferecimento não tem contato.
ANSWERS_ONLY_REPLY = "Este canal recebe só respostas às questões."
# Só no modo desligado, a quem tem aberta só escolha com botões, entre `ANSWERS_ONLY_REPLY` e o contato.
USE_BUTTONS_HOWTO = 'Para responder, use os botões da questão ou toque em "Responder" na mensagem dela.'
# Só em canal sem botões.
DONT_KNOW_HOWTO = 'Para responder "Não sei", responda a esta mensagem com /naosei.'
# Só na múltipla degradada, logo depois das alternativas.
TYPED_MULTIPLE_HOWTO = "Digite as letras das alternativas juntas, sem separador (ex.: ab)."

# Modelos com valores.
FORMAT_ERROR = "Resposta não aceita (não conta como tentativa): {explanation}"
MEDIA_UNAVAILABLE = "[Figura {n} indisponível]"
# A nota do feedback, com o número do selo do PL (`score_text`).
SCORE = "nota {n}%"
# O contato do professor no modo desligado. O e-mail entra literal, sem `formatter.escape`: a resposta
# fixa sai como texto puro, sem `parse_mode`.
CONTACT_HOWTO = "Para falar com o professor, escreva para {email}."

# Veredito do feedback nas três faixas do `determine_score_params` do PL:
# ✅ com nota ≥ 1, ◐ com 0 < nota < 1 e ❌ com nota ≤ 0.
VERDICT_CORRECT = "✅ correta"
VERDICT_PARTIAL = "◐ parcialmente correta"
VERDICT_INCORRECT = "❌ incorreta"

# Controles e o comando do "Não sei" digitado.
DONT_KNOW_BUTTON = "Não sei"
SUBMIT_BUTTON = "Enviar"
DONT_KNOW_COMMAND = "/naosei"


# Os sinais do feedback por parte e por alternativa.
_MARK_CORRECT = "✅"
_MARK_PARTIAL = "◐"
_MARK_INCORRECT = "❌"


def _band(score: float) -> int:
    """As três faixas do PL: 1 com nota ≥ 1 (inclusive acima de 1), 0 com 0 < nota < 1 e -1 com
    nota ≤ 0."""
    if score >= 1:
        return 1
    if score > 0:
        return 0
    return -1


def verdict(score: float) -> str:
    """O veredito nas três faixas: "✅ correta", "◐ parcialmente correta" ou "❌ incorreta"."""
    return {1: VERDICT_CORRECT, 0: VERDICT_PARTIAL, -1: VERDICT_INCORRECT}[_band(score)]


def part_mark(score: float) -> str:
    """A marca de uma parte nas mesmas faixas do veredito: ✅, ◐ ou ❌."""
    return {1: _MARK_CORRECT, 0: _MARK_PARTIAL, -1: _MARK_INCORRECT}[_band(score)]


def option_mark(correct: bool) -> str:
    """A marca de uma alternativa escolhida, posta depois do texto dela por quem compõe o eco."""
    return _MARK_CORRECT if correct else _MARK_INCORRECT


def score_text(score: float) -> str:
    """"nota N%", com o número do selo do PL: 100 com nota ≥ 1 (o teto), ⌊nota × 100⌋ na parcial
    (o `math.floor` do `determine_score_params`) e 0 com nota ≤ 0 (o piso)."""
    band = _band(score)
    n = 100 if band == 1 else 0 if band == -1 else math.floor(score * 100)
    return SCORE.format(n=n)


def feedback(echoes: Sequence[str], *, score: float, part_scores: Sequence[float] | None = None) -> str:
    """"Sua resposta: {eco} · {veredito} · {nota}"; nas várias partes, "Parte {k}: {eco} · {✅|◐|❌}" por
    parte e "Resultado: {veredito} · {nota}". A nota sai em toda correção
    válida, só na linha do resultado. Os ecos chegam já compostos (e escapados), com as marcas por
    alternativa, e saem como vieram."""
    echoes = list(echoes)
    if len(echoes) == 1:
        return f"Sua resposta: {echoes[0]} · {verdict(score)} · {score_text(score)}"
    if part_scores is None or len(part_scores) != len(echoes):
        raise ValueError("feedback de várias partes exige uma nota por parte")
    lines = [f"Parte {k}: {echo} · {part_mark(part)}"
             for k, (echo, part) in enumerate(zip(echoes, part_scores, strict=True), start=1)]
    lines.append(f"Resultado: {verdict(score)} · {score_text(score)}")
    return "\n".join(lines)


def _with_contact(text: str, email: str | None) -> str:
    """Acrescenta a frase do contato quando o oferecimento tem e-mail (literal, sem escape)."""
    return f"{text} {CONTACT_HOWTO.format(email=email)}" if email else text


def answers_only_reply(email: str | None, *, buttons_open: bool = False) -> str:
    """A resposta do modo desligado: `ANSWERS_ONLY_REPLY`, mais `USE_BUTTONS_HOWTO`
    com `buttons_open`, mais `CONTACT_HOWTO` com contato."""
    text = f"{ANSWERS_ONLY_REPLY} {USE_BUTTONS_HOWTO}" if buttons_open else ANSWERS_ONLY_REPLY
    return _with_contact(text, email)


def use_reply_masked(email: str | None) -> str:
    """O ambíguo no modo desligado: `USE_REPLY`, mais `CONTACT_HOWTO` com contato."""
    return _with_contact(USE_REPLY, email)


def progress(k: int, n: int) -> str:
    """"Parte {k} de {n}"."""
    return f"Parte {k} de {n}"


def field_request(k: int, n: int, label: str | None, suffix: str | None = None) -> str:
    """"Campo {k} de {n}: {rótulo} {sufixo} — digite a resposta." (rótulo e sufixo já escapados; sem
    sufixo, só o rótulo; sem os dois, "Campo {k} de {n} — digite a resposta.")."""
    text = " ".join(part for part in (label, suffix) if part)
    if not text:
        return f"Campo {k} de {n} — digite a resposta."
    return f"Campo {k} de {n}: {text} — digite a resposta."


def deadline(moment: datetime, zone: str) -> str:
    """"Prazo: {data e hora no fuso do oferecimento}" (o formato de `timezones.format_local`)."""
    return "Prazo: " + format_local(moment, zone)
