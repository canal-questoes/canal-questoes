# Fixture de grade.test.ts: o tempo esgota no parse do server.py, que não imprime nada.
def generate(data):
    data["correct_answers"]["v"] = 1


def parse(data):
    while True:
        pass
