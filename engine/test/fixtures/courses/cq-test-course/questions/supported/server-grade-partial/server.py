def generate(data):
    data["correct_answers"]["v"] = 7


def grade(data):
    data["partial_scores"]["v"]["score"] = 0
