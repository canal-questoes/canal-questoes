# Fixture de mídias: mídias estáticas, geradas, com falha e ausente.
import base64
import random

# PNG 1x1 fixo devolvido pelo file() de "gerada.png". O teste (engine/test/integration/ir.test.ts)
# compara a mídia com estes mesmos bytes.
GERADA_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mMQqTgBAAH4AVX3uUk4AAAAAElFTkSuQmCC"
)


def generate(data):
    # Primeiro sorteio depois da semeadura. O file() de "aleatorio.png" repete o primeiro sorteio
    # num executor recém-reiniciado, e o teste exige que os dois valores coincidam.
    data["params"]["r0"] = random.random()


def file(data):
    if data["filename"] == "gerada.png":
        return GERADA_PNG
    if data["filename"] == "aleatorio.png":
        return str(random.random()).encode()
    if data["filename"] == "falha.png":
        raise RuntimeError("falha proposital no file()")
    return None
