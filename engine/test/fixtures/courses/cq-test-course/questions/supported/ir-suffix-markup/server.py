# Fixture de ir.test.ts (os fields[] da IR, sufixo com marcação): sufixos com
# "<", ">" e "&" depois da decodificação do atributo. O PL a193f72 escreve o sufixo de linha única
# escapado ({{suffix}} em pl-number-input, pl-integer-input e pl-string-input), e só o sufixo do
# pl-string-input multiline cru ({{{suffix}}}). Por isso suffix_html de n, i e t é o texto escapado, e o
# de m é o HTML canonicalizado (<b> vira <strong>, <i> vira <em>). Todos os campos têm gabarito, para o
# painel de resposta não sair vazio.


def generate(data):
    data["correct_answers"]["n"] = 2
    data["correct_answers"]["i"] = 3
    data["correct_answers"]["t"] = "x"
    data["correct_answers"]["m"] = "y"
