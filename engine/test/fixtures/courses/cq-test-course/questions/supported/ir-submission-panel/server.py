# Fixture de ir.test.ts (painel de resposta da IR): um pl-integer-input dentro de pl-submission-panel.
# O pl-submission-panel do PL devolve vazio fora do próprio painel, então o painel de resposta não
# mostra "sub". Os dois campos têm correct_answers, para o painel de resposta não sair vazio por falta
# de gabarito (pl-integer-input.py, linhas 202-205).


def generate(data):
    data["correct_answers"]["sub"] = 1
    data["correct_answers"]["v"] = 1
