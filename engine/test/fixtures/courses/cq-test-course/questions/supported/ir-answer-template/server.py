# Fixture de ir.test.ts (campos e painel de resposta da IR): o rótulo do campo e o
# pl-answer-panel usam {{params.r}}. O PL a193f72 processa o mustache no question.html inteiro antes dos
# elementos, então a explicação, o rótulo e o painel de resposta trazem o r sorteado da variante.
# O sorteio é o mesmo de supported/random-params.
import random


def generate(data):
    r = random.randint(1, 10**9)
    data["params"]["r"] = r
    data["correct_answers"]["v"] = r
