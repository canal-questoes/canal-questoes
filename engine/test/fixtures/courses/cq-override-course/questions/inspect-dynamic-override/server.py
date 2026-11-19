# O template injeta dois elementos do curso: pl-number-input (nome de um elemento suportado) e
# custom-thing (nome arbitrário). O question.html estático só tem elementos core.


def generate(data):
    data["params"]["extra"] = '<pl-number-input answers-name="x"></pl-number-input><custom-thing></custom-thing>'
    data["correct_answers"]["v"] = 1
