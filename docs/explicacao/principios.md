# Princípios do canal-questoes

Os sete princípios que guiam o desenvolvimento do canal-questoes e as restrições técnicas do projeto. Este documento é
derivado do documento interno de princípios do projeto (a versão 1.1.0, ratificada em 24/09/2026 e emendada pela
última vez em 25/11/2026), que continua sendo a fonte, e é conferido contra ele antes de cada versão publicada, como
manda o guia de [publicação de versões](../operacao/publicacao-de-versoes.md).

Dois princípios são marcados como **não negociáveis**: o segundo, sobre os testes, e o terceiro, sobre a identidade
dos alunos. Para o resumo em uma linha de cada princípio, veja o [`AGENTS.md`](../../AGENTS.md); para as regras
práticas de quem contribui, o [`CONTRIBUTING.md`](../../CONTRIBUTING.md). O método que aplica estes princípios está
descrito em [O método de desenvolvimento](metodo.md).

## Princípios fundamentais

### I. Conhecimento verificável e rastreável

- O projeto mantém um registro de fatos verificados e de decisões. Toda sessão de trabalho, humana ou com agente,
  parte dele e o atualiza com o que verificar ou decidir.
- Nos documentos de trabalho do repositório de origem, toda afirmação relevante é marcada como **[F]** fato com
  fonte, **[I]** inferência ou **[D]** decisão (com autor e data). Resposta de modelo de linguagem não é fonte.
- Nenhuma proposta de arquitetura ou de integração com tecnologia externa é feita sem antes verificar a tecnologia na
  documentação oficial ou no código-fonte.
- Toda decisão de escopo, de produto ou de arquitetura é registrada. Um registro nunca é apagado: uma revisão ganha
  registro novo, que aponta o anterior.
- A arquitetura é descrita primeiro pela intenção e depois pela implementação.

Justificativa: o registro é a matéria-prima da monografia do trabalho, e cada decisão precisa ser defensável.

Nesta versão, o registro fica no repositório de origem e não é publicado; as principais decisões estão resumidas em
[Decisões de projeto](decisoes.md).

### II. Testes primeiro, com revisão adversarial independente (não negociável)

- Toda especificação inclui tarefas de teste. Os testes são escritos **antes** da implementação e falham antes dela.
- Antes da implementação, os testes passam por uma **revisão adversarial independente**, em modo somente leitura e
  com veredito estruturado em arquivo. Hoje são duas revisões em paralelo, feitas por agentes de modelos diferentes,
  como descreve [o método](metodo.md). Só os **bloqueios** fazem os testes voltarem para ajuste; as sugestões ficam
  registradas. Há um limite de iterações e, ao atingi-lo, o caso vai para decisão humana.
- Depois de aprovados, os testes ficam **congelados**: a implementação não os altera. Se quem implementa julgar que um
  teste está errado, a única via é reabrir a revisão dos testes.
- Uma especificação só termina quando os testes passam e a convergência (a comparação do código com a especificação e
  as tarefas) não acha mais trabalho faltando.

Justificativa: testes aprovados antes do código e protegidos contra mudança garantem que a implementação faz o que
foi especificado, mesmo sem revisão humana a cada passo.

Para quem contribui, isso se traduz numa regra simples: não altere um teste existente para o código passar.

### III. Identidade só sai pela revelação (não negociável)

- Todo o modelo de dados referencia as pessoas pelo `pseudo_id`, que é aleatório e nunca derivado do RA nem de outro
  dado pessoal.
- A identidade (nome, RA, e-mail, `telegram_id`, `@username`) pode **entrar** na API, por exemplo na importação da
  lista da turma, mas só **sai** pela operação de revelação (o identificador do Telegram, na prática, nunca sai: a
  revelação devolve só o nome, o RA e o e-mail). Essa operação:
  - exige a permissão `identity:reveal` no token;
  - recebe um escopo mínimo;
  - registra a auditoria com **pseudônimos**, nunca com nomes.
- Os esquemas de saída da API e das ferramentas MCP são estritos, com os campos de identidade marcados **por tipo**.
  Um **teste canário** com identidades fictícias falha se qualquer saída, fora a revelação, contiver identidade.
- Todo texto livre que sai da API passa por redação automática das identidades de todas as pessoas cadastradas (o
  nome completo, o RA e o e-mail).
- Segredos (tokens de bot, chaves, credenciais) não entram no git nem passam pelo modelo de linguagem. Os tokens de bot
  ficam cifrados no banco.

Justificativa: os dados pessoais dos alunos, a LGPD, a ética em pesquisa e a operação do sistema por agentes baseados
em modelos de linguagem.

### IV. Fidelidade ao PrairieLearn

- A correção reproduz a semântica do PrairieLearn:
  - o código Python do PrairieLearn (os elementos, o pacote `prairielearn` e o zygote) é reutilizado sem alteração, na
    imagem oficial fixada por hash;
  - a orquestração, em TypeScript, reutiliza o `mustache`, o `markdown.processQuestion` e o `CodeCallerNative` do
    próprio PrairieLearn.
- Toda divergência deliberada em relação ao PrairieLearn é registrada como decisão. A única vigente é a normalização
  da vírgula decimal: num campo de número decimal (o tipo `number`, do elemento `pl-number-input`), uma entrada com
  uma vírgula e nenhum ponto tem a vírgula trocada por ponto; a entrada literal fica preservada, e a submissão,
  marcada.
- Mudanças no motor e trocas de versão do PrairieLearn passam na suíte de conformidade contra o **oráculo**, um
  PrairieLearn local na mesma versão da imagem do motor.

Justificativa: a correção é o núcleo de valor do sistema, e divergências silenciosas invalidariam os dados.

### V. Fronteiras, portas e contratos

- O núcleo não depende diretamente de Telegram, PrairieLearn, git, e-mail ou HTTP. Toda dependência externa entra por
  uma porta, como `QuestionEngine` (o motor de questões), `DeliveryChannel` (a entrega ao aluno),
  `VerificationChannel` (o convite por e-mail) e `ReportRenderer` (o relatório).
  - O provisionamento de bots é hoje parte da porta do Telegram: o operador cola à mão o token criado no BotFather, e
    o sistema o confere e configura o bot por essa porta. Uma porta própria, `BotProvisioner`, é a evolução prevista
    para os bots gerenciados do Telegram.
- Os canais declaram as suas capacidades; o núcleo nunca testa "qual canal é".
- O banco de questões é opaco: o sistema não gere nem interpreta o conteúdo dele; só recebe referências (o banco, a
  versão fixada e o identificador da questão) e entrega o conteúdo ao motor. O banco chega de duas formas:
  - um repositório git remoto num commit, que o `worker` obtém;
  - um banco enviado pelo operador: o CLI ou o servidor MCP leem a pasta do curso ou um commit de um git local,
    empacotam e enviam à API, e o servidor guarda o pacote, imutável, identificado pelo hash SHA-256 do conteúdo, que
    faz o papel do commit. Só o cliente lê o git local; o núcleo continua sem git.
- A representação intermediária entre o motor e o canal e a API são **contratos versionados**, em
  [`contracts/`](../../contracts/), e mudanças neles são deliberadas.
- Toda operação nasce no **catálogo único**, com endpoint, comando do CLI e, quando faz sentido, ferramenta MCP. As
  operações destrutivas usam duas fases e a anotação de destrutiva.

Justificativa: as evoluções previstas (novos canais, os bots gerenciados do Telegram, um modo de identidade só local)
precisam ser mudanças de fachada.

### VI. Nenhuma resposta perdida

- A entrada literal do aluno é gravada **antes** de qualquer processamento.
- As tarefas assíncronas são idempotentes e retomáveis. A recepção do Telegram deduplica as atualizações pelo
  `update_id`, e cada bot tem um único worker dono de cada vez.
- Todo passo relevante gera um evento no log de eventos, que só recebe acréscimos.
- As falhas relevantes para o professor são comunicadas em linguagem funcional, no chat do professor, sem detalhes
  técnicos.

Justificativa: o critério de sucesso do piloto inclui zero respostas perdidas.

### VII. Escopo contido

- Só entra o que está no escopo registrado.
- As evoluções ficam documentadas como trabalho futuro, não implementadas (YAGNI); as previstas estão no
  [roadmap](../../ROADMAP.md).
- A escala é projetada, mas o sistema roda com uma réplica.
- Se o cronograma apertar, os cortes seguem a ordem registrada: o relatório formatado, os desenhos de tela de uma
  futura interface web, as questões com várias partes e a formatação rica.

Justificativa: o prazo de um trabalho de conclusão de curso, com cerca de seis semanas de desenvolvimento previstas até
o piloto.

## Restrições técnicas

- **Linguagens:** Python, gerenciado com o `uv`, no núcleo, na API, no worker, no CLI e no servidor MCP. TypeScript
  **apenas** no motor.
- **Execução:** Docker Compose com quatro contêineres (`api`, `worker`, `engine` e `postgres`). PostgreSQL com volume
  persistente e backup cifrado fora da VM. O motor fica numa rede interna, sem saída, e roda como usuário não-root.
- **Telegram:** long polling, Rich Messages e respeito ao `retry_after` e aos limites documentados.
- **Ambiente Windows:** o desenvolvimento é recomendado dentro do WSL 2, que o PrairieLearn exige para rodar
  localmente, com `PYTHONUTF8=1` exportado ao rodar as suítes (o `scripts/run-suite.sh` já o exporta).
- **Idioma:** documentação, mensagens para pessoas e monografia em português do Brasil. Identificadores de código em
  inglês.
- **Diagramas:** C4 em D2, com o SVG gerado versionado ao lado do fonte, em [`docs/diagramas/`](../diagramas/README.md).

As ferramentas do processo de desenvolvimento estão descritas em [O método de desenvolvimento](metodo.md).

## Como este documento muda

O documento interno de princípios prevalece sobre as outras práticas do projeto. As emendas seguem o versionamento
semântico (MAJOR para remover ou redefinir um princípio, MINOR para um princípio ou uma seção nova, PATCH para a
redação), e cada emenda é registrada como decisão. Depois de uma emenda, este documento é atualizado e conferido de
novo na publicação da versão seguinte.
