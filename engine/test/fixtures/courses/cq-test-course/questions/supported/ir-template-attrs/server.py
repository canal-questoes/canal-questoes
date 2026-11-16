# Fixture de ir.test.ts (campos da IR com atributos vindos do template): o
# answers-name e a base do pl-integer-input vêm do template. O PL processa o mustache antes dos
# elementos, e por isso vê answers-name="n1" e base="16": o campo da IR se chama "n1", tem base 16 e
# o gabarito 31 (1F em base 16).


def generate(data):
    data["params"]["nome"] = "n1"
    data["params"]["base"] = 16
    data["correct_answers"]["n1"] = 31
