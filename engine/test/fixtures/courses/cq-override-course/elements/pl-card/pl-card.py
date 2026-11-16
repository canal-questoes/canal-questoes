# Elemento do curso com o nome de um elemento suportado que não é de resposta (fixture do motor).
# A questão que o usa é estaticamente não suportada (course_element_override), e o motor não executa
# código dela: qualquer chamada a este controlador lança.


def prepare(element_html, data):
    raise RuntimeError("o pl-card do curso não deveria executar")


def render(element_html, data):
    raise RuntimeError("o pl-card do curso não deveria executar")
