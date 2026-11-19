# Fixture de mídias e painéis: todo campo numérico ou de texto tem correct_answers, para o painel de resposta do PL não sair vazio.

def generate(data):
    data["correct_answers"]["dentro"] = 1
    data["correct_answers"]["num"] = 1
    data["correct_answers"]["txt"] = "a"
    data["correct_answers"]["visivel"] = 1
