def generate(data):
    data["correct_answers"]["v"] = 1


def parse(data):
    data["params"]["parsed"] = True
    data["correct_answers"]["v"] = 2
