# Fixture de ir.test.ts:
# - o label do pl-number-input tem marcação (<b>) e fórmula; o PL o renderiza cru ({{{label}}} no
#   template), e o motor só o canonicaliza: <b> vira <strong> e $A$ vira cq-math;
# - um <textarea> solto no painel da questão: no enunciado, o conteúdo dele sai.


def generate(data):
    data["correct_answers"]["x"] = 2
