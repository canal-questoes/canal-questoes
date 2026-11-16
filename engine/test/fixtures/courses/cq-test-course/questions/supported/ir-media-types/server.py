# Fixture de ir.test.ts (mídias da IR, com o content_type pela
# extensão): foto.jpg (estática, JPEG 2x2 em clientFilesQuestion) e grafico.svg (gerada). O teste
# compara a mídia gerada com estes mesmos bytes.
GRAFICO_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="4" height="4" fill="#2a7"/></svg>'


def generate(data):
    data["correct_answers"]["v"] = 1


def file(data):
    if data["filename"] == "grafico.svg":
        return GRAFICO_SVG
    return None
