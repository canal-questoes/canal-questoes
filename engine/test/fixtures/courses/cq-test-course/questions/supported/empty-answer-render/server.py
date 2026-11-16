def generate(data):
    # "sem" não tem resposta correta: o painel de resposta do pl-integer-input sai vazio
    # (pl-integer-input.py, linhas 202-205).
    data["correct_answers"]["v"] = 1
