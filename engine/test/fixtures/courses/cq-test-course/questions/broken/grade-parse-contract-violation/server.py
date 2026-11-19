def generate(data):
    data["correct_answers"]["v"] = 1


def parse(data):
    # O checkData do PL só exige que format_errors seja objeto; o esquema do motor exige html em texto.
    data["format_errors"]["v"] = {"x": 1}
