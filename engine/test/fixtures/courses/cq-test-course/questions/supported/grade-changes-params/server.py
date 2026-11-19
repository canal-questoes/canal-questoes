def generate(data):
    data["correct_answers"]["v"] = 1


def grade(data):
    # Alteração permitida na fase grade (checkData do PL): volta no resultado da correção.
    data["params"]["graded"] = True
    data["correct_answers"]["v"] = 3
