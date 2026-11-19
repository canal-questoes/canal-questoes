# Fixture de ir.test.ts (mídias da IR):
# - "com espaço.png" (gerada): o pl-figure monta a URL com quote(file-name), e o file() só devolve os
#   bytes se receber o nome decodificado;
# - "figura dois.png" (estática, em clientFilesQuestion): o filename da IR é o nome decodificado;
# - "img-gerada.png": <img> comum com options.client_files_question_dynamic_url, fora do pl-figure;
# - "vazia.png" (gerada): o file() devolve vazio (status "missing" e aviso media_error);
# - "explicacao.png" (estática): só aparece no pl-answer-panel, depois das mídias do enunciado.
# O teste compara as mídias geradas com estes mesmos bytes.
COM_ESPACO = b"CQ-IR-MIDIA-COM-ESPACO"
IMG_GERADA = b"CQ-IR-MIDIA-IMG-GERADA"


def generate(data):
    data["correct_answers"]["v"] = 1


def file(data):
    if data["filename"] == "com espaço.png":
        return COM_ESPACO
    if data["filename"] == "img-gerada.png":
        return IMG_GERADA
    if data["filename"] == "vazia.png":
        return b""
    return None
