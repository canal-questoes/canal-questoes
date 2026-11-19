# Fixture de ir.test.ts (mídias da IR, caminho que escapa do diretório e fase do aviso): o <img>
# aponta para "../fora.png" a partir de clientFilesQuestion. O arquivo existe (fora.png, na raiz desta
# questão), mas o caminho escapa do diretório-base: a mídia fica "missing", sem os bytes, com o aviso
# missing_media na fase render, e a variante sai "ok". Um <img> comum evita o prepare do pl-figure,
# que exigiria o arquivo dentro de clientFilesQuestion.


def generate(data):
    data["correct_answers"]["v"] = 1
