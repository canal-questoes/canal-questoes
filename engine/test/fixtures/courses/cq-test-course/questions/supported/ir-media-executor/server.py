# Fixture de ir.test.ts (mídias da IR): três figuras geradas.
# - r1.png e r2.png devolvem o primeiro sorteio depois da semeadura e o número de chamadas do file()
#   neste módulo. Com uma chamada por arquivo, cada uma num executor recém-reiniciado (como a rota de
#   arquivos gerados do PL a193f72), as duas devolvem o mesmo sorteio do generate e a contagem 1.
# - p.png devolve dados da variante: params["k"] e correct_answers["v"], que o PL passa ao file()
#   (extractVariantSubmissionData sem submissão).
import random

CHAMADAS = [0]


def generate(data):
    data["params"]["r0"] = random.random()
    data["params"]["k"] = 7
    data["correct_answers"]["v"] = 1


def file(data):
    CHAMADAS[0] += 1
    if data["filename"] in ("r1.png", "r2.png"):
        return f"{random.random()}|{CHAMADAS[0]}".encode()
    if data["filename"] == "p.png":
        return f"{data['params']['k']}|{data['correct_answers']['v']}".encode()
    return None
