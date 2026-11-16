# Fixture de create-variant.test.ts: a saída e o traceback somam mais de 8000
# caracteres, e o technical_detail tem de sair truncado, sem virar ir_contract_violation.
def generate(data):
    print("x" * 20000)
    raise ValueError("falha longa")
