# Fixture de mídias: figura gerada grande.
# 200 000 bytes fixos: a variante gera uma saída da CLI maior que 64 KiB.
GRANDE = bytes(i % 251 for i in range(200000))


def generate(data):
    data["correct_answers"]["v"] = 1


def file(data):
    if data["filename"] == "grande.png":
        return GRANDE
    return None
