# Roadmap do canal-questoes

## A visão

O canal-questoes leva a avaliação formativa até o aluno por *push*: a questão chega no aplicativo de mensagens que ele
já usa, sem precisar abrir outra plataforma, e a resposta volta corrigida no mesmo chat. O PrairieLearn é o motor,
usado com fidelidade: as questões no formato do PrairieLearn, sem conversão, e a mesma correção, com cada divergência
deliberada e conferida.
O professor opera o sistema pelo CLI e também por um agente, com as operações destrutivas confirmadas em duas fases.
A privacidade vem por construção: os alunos aparecem só por pseudônimo, e a identidade sai apenas por uma operação de
revelação auditada.

## Os próximos passos

Sem data e sem ordem fixa. Vêm do que ficou fora do escopo da primeira versão e dos itens adiados:

- o relatório formatado, anexo ao resumo do professor, e uma interface web;
- os bots gerenciados do Telegram (Managed Bots) e outros canais de entrega além do Telegram;
- um modo de identidade só local e um *webhook* de feedback;
- uma rota alternativa pelo PrairieLearn web, para o aluno que não usa o Telegram;
- a observabilidade com Prometheus ou OpenTelemetry;
- um site de documentação e guias para um operador qualquer, além do piloto;
- a abertura do kit do processo de desenvolvimento (veja [o método](docs/explicacao/metodo.md)), com o
  desenvolvimento passando a acontecer no repositório público;
- a conformidade com o REUSE e as releases assinadas.

Sugestões e discussões são bem-vindas: veja como contribuir no [`CONTRIBUTING.md`](CONTRIBUTING.md).
