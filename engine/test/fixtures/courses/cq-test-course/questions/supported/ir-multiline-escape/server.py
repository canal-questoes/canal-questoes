# Fixture de ir.test.ts (painel de resposta, HTML canônico e <textarea> que vira <pre>): o gabarito do
# pl-string-input multiline tem "<", ">", "&" e aspas. No painel de resposta, o PL a193f72 o escreve
# escapado dentro de um <textarea>; no display_html, o textarea vira <pre> com o mesmo texto, e a
# aspa sai literal (&quot; só nos atributos).


def generate(data):
    data["correct_answers"]["t"] = 'a < b && c > "d"'
