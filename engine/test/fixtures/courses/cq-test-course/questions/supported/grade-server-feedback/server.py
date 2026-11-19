def generate(data):
    data["correct_answers"]["v"] = 7


def grade(data):
    # Alterações permitidas na fase grade (checkData do PL). A nota já foi calculada antes daqui.
    data["feedback"]["v"] = "Muito bem"
    data["params"]["graded"] = True
    data["correct_answers"]["v"] = 8
    if data["submitted_answers"]["v"] == 13:
        data["format_errors"]["v"] = "Resposta proibida"
