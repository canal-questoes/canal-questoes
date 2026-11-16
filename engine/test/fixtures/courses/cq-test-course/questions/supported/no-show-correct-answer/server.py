# Fixture de mídias e painéis: todo campo numérico ou de texto tem correct_answers, para o painel de resposta do PL não sair vazio.

def generate(data):
    data["correct_answers"]["v"] = 1
