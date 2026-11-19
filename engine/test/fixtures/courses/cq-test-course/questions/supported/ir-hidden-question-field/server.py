# Fixture de ir.test.ts (os campos do enunciado da IR): o conteúdo de
# pl-hide-in-panel question="true" não entra no enunciado, então "h" não tem cq-field (como o campo em
# pl-submission-panel); o painel de resposta mostra "h" normalmente (answer="false", o padrão).


def generate(data):
    data["correct_answers"]["h"] = 1
    data["correct_answers"]["v"] = 1
