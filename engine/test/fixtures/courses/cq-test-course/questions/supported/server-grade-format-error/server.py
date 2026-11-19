def generate(data):
    data["correct_answers"]["v"] = 7


def grade(data):
    if data["submitted_answers"]["v"] == 13:
        data["format_errors"]["v"] = "Resposta proibida"
