# Decisões de projeto

Este texto apresenta os requisitos que guiaram o canal-questoes e as principais decisões de projeto que deram forma
a ele. Para cada decisão, registra o problema, a escolha feita e as alternativas descartadas. A estrutura que resultou
dessas escolhas está em [Arquitetura](arquitetura.md); os princípios que nenhuma decisão pode contrariar, em
[Princípios](principios.md).

O texto foi adaptado do capítulo de requisitos e decisões da monografia do projeto e descreve o que existe no código
desta versão; o que ficou para depois aparece como evolução.

## Requisitos e restrições

O objetivo é um serviço funcional e implantado que:

- entregue questões PrairieLearn por bots do Telegram, um por oferecimento de disciplina;
- corrija as respostas com a semântica do PrairieLearn e devolva ao aluno a correção direta;
- produza um relatório descritivo por disparo;
- seja administrado por API e linha de comando e também operado por um agente de IA.

A validação é um piloto com voluntários. O critério funcional de sucesso tem três partes:

- nenhuma resposta perdida;
- nenhuma divergência de correção nas amostras verificadas contra o PrairieLearn;
- relatório gerado para todos os disparos.

Quatro restrições delimitaram o espaço de soluções:

- **a divisão do trabalho no grupo de pesquisa.** Vários alunos atuam no mesmo tema. A parte deste projeto é o canal
  de comunicação com os alunos, por bot do Telegram, ao lado do relatório por questão e da administração dos bots. A
  geração e a gestão das questões cabem a outros projetos;
- **o formato.** As questões do grupo são escritas no formato do PrairieLearn, que o orientador do trabalho já usa nas
  suas disciplinas;
- **o prazo.** Cerca de seis semanas de desenvolvimento até o piloto, com o sistema validado por volta de 20 de
  novembro de 2026;
- **o ambiente de operação.** Python gerenciado com `uv` e os serviços numa máquina virtual Linux.

O prazo motivou uma regra de **escopo contido**: só entra o que está registrado no escopo, e as evoluções ficam
documentadas como trabalho futuro. Se o cronograma apertasse, os cortes seguiriam uma ordem definida de antemão:
primeiro o relatório formatado, depois os desenhos de tela de uma futura interface web, em seguida as questões com
várias partes e, por último, a formatação rica das mensagens. O primeiro corte foi aplicado: o relatório formatado
ficou como evolução, no [roadmap](../../ROADMAP.md).

## Escopo e fronteiras

### O banco de questões fica de fora

A primeira decisão foi não trazer para o sistema o domínio de geração e gestão das questões. O sistema não tem um
banco próprio: recebe uma referência imutável à questão (o repositório, o *commit* e o QID, o identificador da questão
no repositório), num pedido de disparo imediato ou agendado. O conteúdo e a organização do banco ficam com os
projetos que o produzem.

A alternativa, importar as questões para um repositório interno, acoplaria o canal à forma como cada projeto organiza
o seu acervo. Com a referência, usar as questões de outro projeto do grupo exige só apontar outro repositório, num
host permitido (`CQ_REPO_ALLOWED_HOSTS`).

### Correção direta agora, feedback elaborado depois

O sistema renderiza a questão, envia, recebe a resposta, corrige e persiste tudo. Ao aluno, devolve a correção
direta: certo, parcialmente certo ou errado, nas mesmas faixas do PrairieLearn, com a nota em porcentagem e uma marca
por parte da questão. Um feedback elaborado, como a explicação do erro, ficou para um serviço externo futuro.

O sistema deixa pronta a parte de consulta desse ponto de extensão: um serviço pode ler pela API os eventos de um
tipo, como `submission.graded`, a partir de um cursor (`cq events list --type submission.graded --after <n>`). Ficaram
como evolução, fora desta versão:

- o caminho de volta, um endpoint pelo qual o serviço enviaria o feedback ao aluno;
- o aviso ativo ao serviço, por chamadas HTTP assinadas com fila de reenvio (*webhook*).

### Administração por API e linha de comando, sem interface web

A administração é feita por uma API HTTP e por uma linha de comando (`cq`) que consome essa API, local ou remota. Sem
configuração, a linha de comando usa a API local (`http://127.0.0.1:8000`) e, se ela não responde, oferece subir a
pilha ali mesmo, com a mesma rotina do `cq up`, quando roda num terminal. Isso reduz o atrito de depender de uma API
em execução. Uma interface web ficou fora da implementação.

A mesma API serve ao agente de IA, o que reforça a escolha: uma única porta de entrada atende pessoas, programas e
agentes.

## Motor e fidelidade ao PrairieLearn

### Como reutilizar o PrairieLearn

A correção é o núcleo de valor do sistema, e uma divergência silenciosa em relação ao PrairieLearn invalidaria os
dados coletados. A primeira opção seria reimplementar a semântica dos elementos usados; a segunda, reutilizar o
código do próprio PrairieLearn (PL). O projeto adotou a segunda e manteve a primeira como plano B, atrás da porta do
motor.

Restava decidir *como* reutilizar o PrairieLearn, que não oferece um serviço de questões pronto. Os níveis de reuso
analisados, do mais externo ao mais interno:

| Nível | Forma | Avaliação |
| --- | --- | --- |
| 0 | o PL em execução, pela API pública | inviável: a API não cria variantes nem corrige respostas |
| 0,5 | o PL local em modo de desenvolvimento, pelos formulários web | fidelidade total, mas inadequado em produção: a interface é HTML feito para pessoas, muda com o PL, não expõe os parâmetros da variante e duplica o estado; adotado como oráculo dos testes |
| 1 | a imagem do executor do PL, como processo | não faz a orquestração e fixa um curso por contêiner; criar contêineres sob demanda exigiria dar ao sistema o controle do Docker, que a própria documentação do Docker restringe a usuários confiáveis |
| 2 | o processo Python do PL (*zygote*) na imagem oficial, chamado por um orquestrador próprio | adotado em produção: um contêiner atende vários cursos, porque o *zygote* recebe o diretório do curso a cada chamada; o custo é espelhar a orquestração do PL |
| 3 | os módulos Python do PL importados no processo do sistema | acoplamento mais forte, inclusive de licença |
| 4 | a reimplementação dos elementos | mais código e mais risco de divergência; ainda exigiria executar isolado o `server.py` da questão |

A combinação escolhida usa cada nível onde ele é forte. O nível 2 é o motor de produção. O nível 0,5, fiel por
construção, virou o **oráculo** da suíte de conformidade: gera a mesma questão com a mesma semente e corrige as mesmas
respostas, e os resultados são comparados automaticamente com os do motor.

### O orquestrador em TypeScript

O resto do sistema é escrito em Python, e o natural seria escrever também o orquestrador do motor em Python. A leitura
do código do PrairieLearn mostrou um obstáculo: a biblioteca Mustache que o PL usa, em JavaScript, escapa mais
caracteres do que a biblioteca Python equivalente, de modo que uma reimplementação em Python mudaria o HTML entregue
aos elementos.

Por isso o orquestrador foi escrito em TypeScript e roda dentro da imagem do PrairieLearn, reutilizando a mesma
versão do Mustache, o processamento de Markdown e o componente que conversa com o *zygote*. O código próprio ficou
restrito ao mapa de elementos, à sequência das fases, ao cálculo da nota e à representação intermediária.

Uma consequência é a licença: o PrairieLearn é distribuído sob a AGPL-3.0, e o motor importa código dele. O
repositório adotou a AGPL-3.0 ou posterior, escolhida para ser compatível com esse reuso.

### A vírgula decimal, única divergência deliberada

O PrairieLearn rejeita "3,14" num campo numérico como erro de formato. Para alunos acostumados à vírgula decimal, isso
tenderia a gerar erros de formato frequentes. O sistema troca a vírgula por ponto antes de chamar o motor, mas só
quando não há ambiguidade: num campo de número, exatamente uma vírgula e nenhum ponto. A entrada literal é preservada,
e a submissão guarda quais campos foram normalizados.

É a única divergência em relação ao PrairieLearn, e por isso é explícita, registrada e auditável. A
[referência dos tipos de questão](../referencia/tipos-de-questao.md) descreve a regra do ponto de vista de quem
escreve as questões.

## Infraestrutura

### Monólito modular com o motor isolado

Entre um processo único e um serviço por componente, o projeto ficou no meio. A API e o worker formam um monólito
modular: a mesma imagem e o mesmo código de domínio, em contêineres separados. Isso preserva as transações atômicas
que o domínio exige, como gravar a variante de um aluno e pôr na fila as mensagens dela de uma só vez, sem coordenação
entre serviços.

Só o motor é um serviço à parte, por três razões: outro ambiente de execução, código de terceiros e um ciclo de versão
próprio ([Arquitetura](arquitetura.md#contêineres-e-comunicação)). O conjunto roda com o Docker Compose, em quatro
contêineres.

### PostgreSQL para o estado, as filas e os avisos

A alternativa considerada foi o SQLite. A escolha recaiu sobre o PostgreSQL porque a arquitetura depende de três
coisas:

- vários processos, em contêineres separados, escrevendo no mesmo banco;
- filas de trabalho com vários consumidores;
- avisos entre processos.

O PostgreSQL oferece as duas últimas de forma nativa, com `SKIP LOCKED` e com `LISTEN`/`NOTIFY`, o que dispensa um
intermediário de mensagens separado. O banco usa um volume persistente, e a cópia de segurança é um *dump* cifrado,
copiado para fora da máquina virtual.

### Recepção por long polling, com posse dos bots

O Telegram entrega as atualizações de um bot por *long polling* ou por *webhook*, nunca pelos dois ao mesmo tempo, e o
*webhook* exige um endpoint HTTPS numa das portas 443, 80, 88 ou 8443. No *long polling*, é o próprio sistema que busca
as atualizações, sem expor um endpoint público. Em contrapartida, cada bot precisa de um único consumidor: no servidor
de referência da Bot API, uma consulta nova interrompe a anterior do mesmo bot com um erro de conflito.

A solução foi a **posse**: cada bot tem um worker dono, registrado no banco como uma concessão com prazo. Um worker
atende vários bots, e um laço de reconciliação assume os bots sem dono ou com a posse vencida. Cadastrar um bot,
portanto, não exige subir um contêiner novo, e a recepção escala distribuindo os bots entre os workers.

## Privacidade e operação por agente

### Operação por agente no escopo principal

A operação do sistema por um agente de IA (um servidor MCP com ferramentas curadas, *skills* de procedimento e um
plugin do Claude Code) entrou no escopo principal, e não como extensão opcional. A estimativa inicial era de cerca de
uma semana de trabalho, o que consumia a folga do cronograma.

A mitigação foi um **catálogo único de operações**: cada caso de uso nasce de uma vez como endpoint, comando da linha de
comando e, quando faz sentido, ferramenta MCP. Assim, o servidor MCP cresceu junto com o resto do sistema, em vez de
virar uma fase isolada no fim, e chegou a 36 ferramentas ([referência do MCP](../referencia/mcp.md)).

A decisão tem um efeito sobre a privacidade, tratado a seguir: tudo o que as ferramentas devolvem chega ao provedor do
modelo de linguagem.

### A identidade só sai pela revelação

No uso do dia a dia, os alunos são identificados só por um pseudônimo. A identidade (o nome, o RA e o e-mail) aparece
apenas numa operação de revelação, com escopo mínimo, a pedido do operador ou com a confirmação dele, e registrada numa
trilha de auditoria. O identificador do aluno no Telegram nunca sai do sistema.

A regra vale para toda a API, e não só para o agente: a identidade *entra*, por exemplo na importação da lista, mas só
*sai* por um ponto. Assim, a regra não depende de saber quem é o cliente e fica fácil de testar. Três refinamentos
tornaram a garantia verificável:

- **esquemas estritos e teste canário.** Um teste que só inspeciona os esquemas de saída é necessário, mas não
  suficiente, porque um campo de texto livre ou um dicionário sem tipo escapariam dele. Por isso os esquemas são
  estritos, os campos de identidade são marcados pelo tipo, e não pelo nome, e um teste canário procura identidades
  fictícias em todas as saídas;
- **permissão no token.** A revelação exige uma permissão no token de acesso (`identity:reveal`), de modo que a
  política é garantida pela API mesmo que uma interface esqueça de bloqueá-la;
- **pseudônimo aleatório.** O pseudônimo é sorteado, e nunca um *hash* do RA: como o espaço de RAs é pequeno, um
  *hash* sem segredo seria revertido por força bruta.

Essas escolhas preparam uma evolução descartada para esta versão, mas registrada: um modo em que a identidade nunca
passa pelo modelo e é resolvida só localmente, pela linha de comando. Com a permissão por token, essa evolução se
reduz a retirar a permissão de revelação do token usado pelo agente.

### Convite com código opaco

O vínculo do aluno começa pela lista da turma e por um convite individual enviado por e-mail. A proposta inicial era
um link assinado. A implementação adotou um código aleatório de 128 bits, codificado em 22 caracteres, do qual o
banco guarda só o resumo SHA-256, junto com a matrícula, a validade e o estado.

A intenção da proposta inicial se mantém: um link individual, impossível de adivinhar e de uso único. O estado no
banco permite marcar o convite como usado ou revogado, e a validade faz o convite vencer. O código cabe com folga no
parâmetro do link de início do Telegram, que aceita até 64 caracteres.

### Redação, retenção e expurgo

Dois riscos ficavam fora da regra da revelação.

O primeiro é o **texto livre**: respostas de texto e mensagens espontâneas dos alunos podem conter nomes. Antes de
qualquer texto livre sair da API, as identidades de todas as pessoas cadastradas (o nome completo, o RA e o e-mail)
são trocadas por uma marca com o apelido pseudônimo da pessoa (por exemplo, `[pessoa a1b2]`), com cobertura do teste
canário. A limitação aceita:
nomes informais e apelidos de uso, que não constam da lista da turma, não são detectados.

O segundo é o **tempo**: depois que um oferecimento é arquivado, as identidades não precisam mais existir. O sistema
tem uma política de retenção e uma operação de expurgo, em duas fases (`cq offerings purge prepare` e
`cq offerings purge confirm`). O expurgo apaga as identidades e os endereços de entrega dos alunos do oferecimento
arquivado, redige os textos guardados que ainda as citam e preserva os dados pseudonimizados. O prazo padrão é de 30
dias depois do arquivamento, configurável (`CQ_RETENTION_DAYS`). Nada é expurgado sozinho: o `cq doctor` avisa quando
um oferecimento passa do prazo, e quem expurga é o operador. Os detalhes estão na
[política de retenção](../operacao/politica-de-retencao.md).

## Riscos

Os principais riscos identificados no início do projeto e as mitigações adotadas:

| Risco | Mitigação |
| --- | --- |
| mudança das partes internas do PrairieLearn usadas pelo motor | a versão fixada; a troca de versão deliberada, sempre com a suíte de conformidade |
| divergência entre a orquestração própria e a do PrairieLearn | o reuso das peças do próprio PL; a comparação com o oráculo |
| a licença AGPL do PrairieLearn | a AGPL-3.0 ou posterior em todo o repositório; o motor separado dos demais contêineres por HTTP |
| o suporte às mensagens ricas em aplicativos desatualizados do Telegram | o roteiro de teste nos aplicativos, preparado no início; o modo HTML simples como alternativa (`CQ_TELEGRAM_FORMAT=html`) |
| o cronograma de cerca de seis semanas | o catálogo único de operações; os cortes ordenados; o teste antecipado das premissas técnicas |
| as dependências externas (a máquina virtual, os repositórios de questões, os voluntários) | os pedidos feitos na primeira semana |
| o vazamento de identidade pelo agente | a regra da revelação, o teste canário e a auditoria |
