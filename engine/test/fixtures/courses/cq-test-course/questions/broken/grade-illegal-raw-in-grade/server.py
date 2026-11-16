def generate(data):
    data["correct_answers"]["v"] = "a"


def grade(data):
    # Proibido no grade: o checkData do PL só deixa raw_submitted_answers mudar na fase test.
    data["raw_submitted_answers"]["v"] = "NOVO-VALOR-5d2a"
