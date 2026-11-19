# Fixture de grade.test.ts. O parse e o grade do server.py imprimem no
# console. Com 13, o grade imprime e lança exceção depois de o parse ter impresso e concluído.
def generate(data):
    data["correct_answers"]["v"] = 1


def parse(data):
    print("depuração no parse: MARCADOR-SAIDA-PARSE-4e2a")


def grade(data):
    print("depuração no grade: MARCADOR-SAIDA-GRADE-4e2a")
    if data["submitted_answers"]["v"] == 13:
        raise ValueError("falha proposital no grade")
