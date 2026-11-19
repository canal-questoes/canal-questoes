def generate(data):
    data["correct_answers"]["v"] = "a"


def grade(data):
    print(data["raw_submitted_answers"])
    raise ValueError(str(data["raw_submitted_answers"]))
