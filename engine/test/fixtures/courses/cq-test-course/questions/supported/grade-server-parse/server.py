def generate(data):
    data["correct_answers"]["v"] = "A"


def parse(data):
    # Roda depois do parse dos elementos (freeform.js do PL): recusa "proibido" com erro de formato e,
    # nos outros casos, grava a resposta interpretada em maiúsculas (o grade do elemento usa esse valor).
    v = data["submitted_answers"]["v"]
    if v == "proibido":
        data["format_errors"]["v"] = "Recusada no parse"
    elif isinstance(v, str):
        data["submitted_answers"]["v"] = v.upper()
