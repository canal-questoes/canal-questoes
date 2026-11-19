# Fixture de grade.test.ts. O parse do server.py grava feedback, que o PL
# devolve com a resposta recusada no parse e leva ao grade na resposta válida (freeform.js: parse()
# devolve o feedback, e grade() começa por ele). Fica em broken/ porque o grade quebra com "quebra".
def generate(data):
    data["correct_answers"]["v"] = "A"


def parse(data):
    data["feedback"]["v"] = "Lido no parse"
    v = data["submitted_answers"]["v"]
    if v == "proibido":
        data["format_errors"]["v"] = "Recusada no parse"
    elif isinstance(v, str):
        data["submitted_answers"]["v"] = v.upper()


def grade(data):
    if data["submitted_answers"]["v"] == "QUEBRA":
        raise ValueError("falha proposital no grade")
