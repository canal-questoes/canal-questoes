# Fixture de grade.test.ts. O grade do server.py reescreve a resposta
# interpretada depois de a nota ser calculada (freeform.js), e o PL grava a do fim do grade, também
# quando o grade acusa erro de formato (models/grading-job.js). Fica em broken/ porque o grade quebra
# com 99.
def generate(data):
    data["correct_answers"]["v"] = 7


def grade(data):
    v = data["submitted_answers"]["v"]
    if v == 99:
        raise ValueError("falha proposital no grade")
    if v == 13:
        data["format_errors"]["v"] = "Resposta proibida"
    data["submitted_answers"]["v"] = v * 10
