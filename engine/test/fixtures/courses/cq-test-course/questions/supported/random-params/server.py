import random


def generate(data):
    data["params"]["r"] = random.randint(1, 10**9)
    data["correct_answers"]["v"] = data["params"]["r"]
