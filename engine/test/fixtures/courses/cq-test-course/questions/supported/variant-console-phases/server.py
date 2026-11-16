# Fixture de create-variant.test.ts: o código da questão imprime no console nas fases
# prepare e file, e não no generate. A variante sai "ok", com um aviso console_output por fase, e o
# texto impresso (ECO-*) não entra na mensagem do aviso.
import base64

# PNG 1x1 fixo (o mesmo de supported/media). O teste compara a mídia x.png com estes bytes.
X_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mMQqTgBAAH4AVX3uUk4AAAAAElFTkSuQmCC"
)


def generate(data):
    data["correct_answers"]["v"] = 1


def prepare(data):
    print("ECO-PREPARE-3b7")


def file(data):
    if data["filename"] == "x.png":
        print("ECO-FILE-3b7")
        return X_PNG
    return None
