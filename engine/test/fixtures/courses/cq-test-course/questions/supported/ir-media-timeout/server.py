# Fixture de ir.test.ts (mídias da IR): o file() de
# lenta.png não termina. No PL, o tempo esgotado do file() afeta só o pedido da imagem, não a variante:
# a mídia fica "missing", com o aviso media_error na fase file, e a variante sai "ok".


def generate(data):
    data["correct_answers"]["v"] = 1


def file(data):
    if data["filename"] == "lenta.png":
        while True:
            pass
    return None
