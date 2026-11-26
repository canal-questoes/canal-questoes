# Tipos de questão

O canal-questoes entrega questões do [PrairieLearn](https://github.com/PrairieLearn/PrairieLearn)
escritas no formato dele, sem conversão: o professor aponta um repositório git de curso, um commit e a questão, e o
motor de questões roda o próprio código do PrairieLearn para gerar a variante e corrigir a resposta. Esta página diz
quais questões o sistema aceita, como cada campo de resposta chega ao aluno no Telegram, o que faz uma questão ser
recusada e a única diferença deliberada em relação ao PrairieLearn: a vírgula decimal.

## De onde vem a questão

O disparo de uma questão (no CLI, `cq dispatches prepare`) recebe três dados:

| Dado | Forma |
| --- | --- |
| `repo` | a URL `https` do repositório git do curso, sem porta, sem IP e sem credenciais, com o host em minúsculas e o caminho só com letras, dígitos, `.`, `_`, `~` e `-`; o host tem de estar na lista `CQ_REPO_ALLOWED_HOSTS` do servidor (vazia, ela recusa todo repositório) |
| `commit` | o SHA do commit, com 40 ou 64 dígitos hexadecimais minúsculos |
| `repo` e `commit` de um banco enviado | `bank:<nome>` no `repo` e a versão do banco (64 dígitos hexadecimais minúsculos) no `commit`, para um banco enviado com `cq banks upload` ou pela ferramenta `upload_question_bank`; essa forma não passa pela lista `CQ_REPO_ALLOWED_HOSTS` ([Banco de questões local](../operacao/banco-local.md)) |
| `qid` | o caminho da questão relativo à pasta `questions/` do curso, como no PrairieLearn: segmentos de letras, dígitos, `_`, `.` ou `-`, sem `.` nem `..` |

A pasta da questão segue o formato do PrairieLearn: o `info.json` com os metadados, o `question.html` e, se houver, o
`server.py`. Depois do preparo, o worker obtém o repositório, inspeciona a questão e gera uma variante dela; só então
o disparo fica pronto para a confirmação, com a prévia da questão. A
[página do CLI](cli.md#disparos-e-mensagens) traz os comandos.

## As questões aceitas

Uma questão é aceita quando:

- o `info.json` é válido, no formato do PrairieLearn, e a questão é do tipo `"v3"`;
- a correção é a interna do PrairieLearn (o `gradingMethod` ausente ou `Internal`), sem `externalGradingOptions`;
- ela não usa workspace (`workspaceOptions`);
- o `server.py`, se existir, não define uma função `render` própria;
- o `question.html` tem ao menos um elemento de resposta suportado, e todo elemento do PrairieLearn que ele usa está no
  conjunto suportado, abaixo;
- o curso não redefine, com elementos próprios, um elemento suportado que a questão usa;
- cada nome de campo (`answers-name`) tem de 1 a 200 caracteres, sem espaço, aspas, `<`, `>` nem `&`.

O código Python da questão (`server.py`, com as funções que o PrairieLearn chama) roda como no PrairieLearn, e uma
questão pode ter mais de um campo de resposta.

## Os elementos de resposta

O sistema suporta cinco elementos de resposta do PrairieLearn. Cada um vira um tipo de campo, e o aluno responde a
cada campo no Telegram:

| Elemento | Tipo de campo | Como o aluno responde |
| --- | --- | --- |
| `pl-multiple-choice` | escolha única (`single_choice`) | marca uma alternativa pelos botões e toca em "Enviar" |
| `pl-checkbox` | múltipla escolha (`multiple_choice`) | marca e desmarca as alternativas pelos botões e toca em "Enviar" |
| `pl-number-input` | número (`number`) | digita o número numa mensagem (com a vírgula decimal, veja abaixo) |
| `pl-integer-input` | inteiro (`integer`) | digita o número inteiro numa mensagem |
| `pl-string-input` | texto (`text`) | digita o texto numa mensagem |

As alternativas de `pl-multiple-choice` e de `pl-checkbox` são escritas com `pl-answer`, como no PrairieLearn.
A mensagem sempre lista as alternativas, cada uma com a sua letra entre parênteses. Quando as alternativas, mais
"Enviar" e "Não sei", não cabem nos botões, ou o canal não tem botões, o aluno digita a letra da alternativa; na
múltipla escolha, as letras juntas, sem separador (ex.: `ab`).

Com mais de uma questão aberta esperando texto, o aluno toca em "Responder" na mensagem da questão certa e digita
a resposta. Em todo campo, o aluno pode responder "Não sei", pelo botão ou, num canal sem botões, com o comando
`/naosei`. Uma resposta fora do formato do campo é recusada com a explicação e não conta como tentativa. A correção
devolve o veredito (correta, parcialmente correta ou incorreta) e a nota, como o PrairieLearn a calcula.

### Os elementos auxiliares

Além dos elementos de resposta, a questão pode usar estes elementos do PrairieLearn, com o comportamento descrito na
documentação dele:

- os de painel: `pl-question-panel`, `pl-answer-panel`, `pl-submission-panel` e `pl-hide-in-panel`;
- os de exibição: `pl-card`, `pl-code` e `pl-figure`.

Qualquer outro elemento do PrairieLearn, ou um elemento próprio do curso, faz a questão ser recusada.

## As recusas da inspeção

Na validação de cada disparo, o motor primeiro inspeciona a questão sem executar o código dela; os motivos que
dependem dos elementos usados são conferidos de novo no HTML gerado para a variante. Uma questão fora do conjunto
suportado recebe um ou mais destes motivos, definidos em
[`engine-api.schema.json`](../../contracts/engine/v1/engine-api.schema.json):

| Motivo | Quando |
| --- | --- |
| `question_type_not_v3` | a questão não é do tipo `"v3"` |
| `grading_method_not_internal` | a correção é externa (`External`) ou manual (`Manual`) |
| `external_grading` | o `info.json` tem `externalGradingOptions` |
| `workspace` | a questão usa um workspace |
| `unsupported_elements` | a questão usa elementos fora do conjunto suportado, ou os elementos do curso não carregam no PrairieLearn; o motivo lista os elementos |
| `no_answer_fields` | a questão não tem nenhum campo de resposta suportado (ou o `question.html` não pôde ser lido) |
| `course_element_override` | o curso redefine um elemento suportado que a questão usa; só as versões do PrairieLearn são aceitas |
| `custom_server_render` | o `server.py` define uma função `render` própria (ou liga o nome `render` a outro objeto), que substituiria o HTML da questão; ou ele não pôde ser lido |
| `invalid_answers_name` | um nome de campo (`answers-name`) é vazio, tem mais de 200 caracteres ou tem espaço, aspas, `<`, `>` ou `&` |
| `invalid_question_metadata` | o `info.json` não é JSON válido, está fora do formato do PrairieLearn ou não pôde ser lido |

Cada motivo vem com uma frase em português que explica o caso.

### O que o operador vê

Uma questão recusada deixa o disparo no estado `rejected`, sem enviar nada aos alunos. O `cq dispatches show` (ou o
`dispatches.get` da API, ou a `get_dispatch` do MCP) mostra o motivo da rejeição e, nos detalhes, a frase de cada
motivo da inspeção. Os motivos de rejeição de um disparo são:

| Rejeição | Mensagem |
| --- | --- |
| `repository_unreachable` | Não foi possível obter o repositório da questão. |
| `repository_refused` | O repositório foi recusado pela política de obtenção. |
| `commit_not_found` | O commit informado não existe no repositório. |
| `question_not_found` | A questão não existe nesse commit. |
| `question_unsupported` | A questão usa recursos que o sistema não suporta. |
| `question_broken` | A questão falhou ao gerar uma variante. |
| `engine_unavailable` | O motor de questões ficou indisponível durante a validação. |
| `channel_limits_exceeded` | A questão não cabe nos limites do canal. |
| `internal_failure` | Falha interna na validação. |

`question_unsupported` é a recusa da inspeção. `question_broken` é a questão aceita na inspeção cujo código falhou ao
rodar. Nesse caso, o detalhe traz a frase do motor, que diz o que falhou e em que fase da validação (a geração da
variante, a preparação da variante ou a montagem do enunciado e do gabarito). Uma imagem gerada pelo código da questão
que falha não recusa o disparo: a questão segue sem ela, com um aviso do motor. A interpretação da resposta e a
correção só rodam quando o aluno responde; uma falha nelas acontece na correção da resposta, e não na rejeição do
disparo. No contrato do motor, os motivos são:

| Motivo | Quando |
| --- | --- |
| `question_code_error` | o código da questão, ou o de um dos seus elementos, falhou |
| `timeout` | o código da questão excedeu o tempo limite |
| `invalid_data` | o código da questão alterou dados de um modo que a fase não permite |
| `ir_contract_violation` | o resultado da fase não pode ser representado no formato do motor de questões |

## A vírgula decimal

A correção reproduz a semântica do PrairieLearn, com uma única divergência deliberada: a vírgula decimal, comum para
quem escreve números em português.

- Vale só para os campos `pl-number-input`.
- Se a resposta tem exatamente uma vírgula e nenhum ponto, a vírgula vira ponto antes da correção: `3,14` é corrigido
  como `3.14`. Só a vírgula e o ponto comuns contam, e nada mais é mudado; o que não for número continua sendo recusado
  pelo PrairieLearn.
- A resposta literal do aluno fica guardada como ele a escreveu, e a submissão fica marcada com os campos em que a
  vírgula foi trocada.
- Os campos `pl-integer-input`, `pl-string-input`, `pl-multiple-choice` e `pl-checkbox` não passam por nenhuma troca.

## A versão do PrairieLearn

O motor usa o código do PrairieLearn sem alteração, a partir da imagem oficial fixada por hash em
[`engine/Dockerfile`](../../engine/Dockerfile) (o commit `a193f72` do PrairieLearn). Uma troca de versão passa pela
suíte de conformidade, que compara o motor com um PrairieLearn local. O [README do motor](../../engine/README.md)
descreve o contrato, a conformidade e como trocar a versão.
