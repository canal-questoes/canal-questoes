def generate(data):
    data["correct_answers"]["t"] = "a"


def parse(data):
    print(data["raw_submitted_answers"])
