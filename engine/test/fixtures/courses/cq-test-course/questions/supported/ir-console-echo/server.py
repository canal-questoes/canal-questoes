# Fixture de ir.test.ts (avisos de saída no console e a mensagem deles): o generate
# imprime um marcador único. A variante sai "ok" com o aviso console_output na fase generate, e a
# mensagem do aviso é uma frase funcional: não repete o que a questão imprimiu.


def generate(data):
    print("depuração ECO-GENERATE-3b7")
    data["correct_answers"]["v"] = 1
