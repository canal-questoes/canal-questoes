def generate(data):
    data["correct_answers"]["v"] = 1


def parse(data):
    # Proibido no parse: o checkData do PL só deixa raw_submitted_answers mudar na fase test.
    data["raw_submitted_answers"]["v"] = "NOVO-VALOR-5d2a"
