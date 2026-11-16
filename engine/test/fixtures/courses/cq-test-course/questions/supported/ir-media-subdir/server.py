# Fixture de ir.test.ts (opções e mídias da IR): três
# figuras estáticas com o mesmo nome de arquivo, fig.png, e bytes diferentes:
# - clientFilesQuestion/sub/fig.png e clientFilesQuestion/fig.png, desta questão;
# - clientFilesCourse/sub/fig.png, na raiz do curso de teste (directory="clientFilesCourse").
# O filename da IR é <diretório-base>/<caminho>, com o subdiretório: as três mídias não se confundem.


def generate(data):
    data["correct_answers"]["v"] = 1
