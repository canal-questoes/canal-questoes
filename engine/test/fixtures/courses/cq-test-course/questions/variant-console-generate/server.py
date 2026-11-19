# Fixture de create-variant.test.ts (avisos de saída no console e a mensagem deles): o generate
# imprime no console um texto com marcador (ECO-GENERATE-3b7), como variant-console-phases faz no
# prepare e no file. A variante sai "ok", com o aviso console_output na fase generate, e o texto
# impresso não entra na mensagem do aviso. A fixture fica fora de supported/ porque a tabela por
# questão de ir.test.ts exige valores esperados para todo diretório de supported/.


def generate(data):
    print("depuração ECO-GENERATE-3b7")
    data["correct_answers"]["v"] = 1
