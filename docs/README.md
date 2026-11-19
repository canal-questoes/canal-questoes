# Documentação do canal-questoes

O canal-questoes entrega questões do PrairieLearn aos alunos por bots do Telegram, corrige as respostas com a
semântica do PrairieLearn e produz relatórios descritivos por questão para o professor. Esta página é o índice da
documentação. Os textos seguem o [Diátaxis](https://diataxis.fr/) e se dividem em quatro tipos, cada um para uma
necessidade de quem lê:

| Tipo | Serve para | Comece por |
| --- | --- | --- |
| Tutorial | aprender fazendo, do zero até o primeiro relatório | [Primeiro uso](tutorial/primeiro-uso.md) |
| Guias de operação | cumprir uma tarefa concreta de quem opera a pilha | [Início rápido](operacao/inicio-rapido.md) |
| Referência | consultar comandos, operações da API, ferramentas do MCP e tipos de questão | [Referência](referencia/README.md) |
| Explicação | entender a arquitetura, o método, os princípios e as decisões de projeto | [Explicação](explicacao/README.md) |

A visão geral do projeto está no [README](../README.md), e o que vem depois desta versão, no [roadmap](../ROADMAP.md).
Nesta versão não há site de documentação: os textos são Markdown, lidos no próprio GitHub.

## Tutorial

Um roteiro guiado, para quem chega de fora e quer ver o sistema funcionando.

- [Primeiro uso](tutorial/primeiro-uso.md): da cópia do repositório até um relatório, passando pela pilha local, por
  um oferecimento de teste com dados fictícios e por um disparo. Exige Linux, ou Windows com WSL 2, com Docker, git e
  `uv`; um bot do Telegram criado por você no BotFather (você mesmo faz o papel do aluno); um servidor de e-mail (SMTP)
  seu, para o convite; e uma questão do PrairieLearn num repositório git público. No fim, mostra como limpar tudo.

## Guias de operação

Guias práticos, cada um para uma tarefa de quem opera o sistema.

> **Para quem foram escritos.** Os guias que já existiam antes desta versão (início rápido, implantação na VM, backup e
> restauração, política de retenção, CI e exportação) e os materiais do piloto foram escritos para o operador do piloto
> do sistema na Unicamp. Por isso, alguns passos vêm marcados como "[passo do Pedro]" (o mantenedor) e citam a VM,
> os backups e as datas previstas desse piloto, que ainda não começou. A reescrita para um operador qualquer está no
> [roadmap](../ROADMAP.md).

Pilha local e agente:

- [Início rápido](operacao/inicio-rapido.md): instalar o `cq`, criar o `.env` da pilha e o arquivo de perfil, subir,
  acompanhar e parar a pilha local, diagnosticar com `cq doctor`, usar um perfil remoto e rodar a suíte de testes.
- [Operação pelo agente](operacao/plugin-e-agente.md): o plugin do Claude Code, o token próprio do agente, o que pede
  a sua aprovação e como validar a instalação.
- [Observabilidade](operacao/observabilidade.md): o que existe hoje para acompanhar o sistema: o diagnóstico do
  `cq doctor`, os registros redigidos dos contêineres, o log de eventos e as métricas do piloto.

Implantação e dados:

- [Implantação na VM](operacao/implantacao-vm.md): montar a VM do piloto, do zero até a pilha no ar, com backup,
  cópia no Windows e túnel para o agente.
- [Backup e restauração](operacao/backup-e-restauracao.md): como a rotina de backup funciona, as chaves do age (a
  ferramenta que cifra os backups), a restauração numa pilha descartável, a recuperação numa VM nova e o que fazer se
  uma chave se perder.
- [Política de retenção](operacao/politica-de-retencao.md): por quanto tempo cada lugar guarda os dados do piloto,
  quem os apaga e o que fazer depois de uma restauração.

Publicação:

- [CI e exportação](operacao/ci-e-exportacao.md): exportar a árvore pública, revisar o que vai sair, validar os passos
  do CI num contêiner local e conferir a primeira execução no GitHub Actions.
- [Publicação de versões](operacao/publicacao-de-versoes.md): os passos para publicar uma versão nova, cada um com o
  seu comando e a sua conferência.

Os materiais do piloto ficam em [`operacao/piloto/`](operacao/piloto/): o
[roteiro do ensaio interno](operacao/piloto/roteiro-ensaio.md), o
[plano de disparos](operacao/piloto/plano-de-disparos.md), o modelo do [diário do piloto](operacao/piloto/diario.md) e
o [questionário de experiência](operacao/piloto/questionario.md).

### A exportação roda só no repositório de origem

Este repositório público é uma exportação do repositório de origem do projeto, onde o desenvolvimento acontece. A
exportação (`ops/export/export_public.py`, descrita nos guias de CI e exportação e de publicação de versões) copia só
os arquivos da lista positiva, varre segredos e dados pessoais e cria um repositório novo, com um único commit. Ela
roda só no repositório de origem: numa árvore exportada, como esta, ela recusa com o código de saída 65, porque a
pasta com as versões públicas de `AGENTS.md`, `CLAUDE.md` e `ROADMAP.md`, que ela copia para a raiz, só existe na
origem.

## Referência

Descrições secas e completas, para consultar. A referência é escrita a partir dos contratos versionados em
[`contracts/`](../contracts/), e um teste confere que toda operação do catálogo, todo comando e toda ferramenta
aparecem nela; os comandos locais, que não são operações do catálogo, vêm à parte.

- [Visão geral da referência](referencia/README.md): o mapa das páginas e os contratos de onde cada uma sai.
- [CLI](referencia/cli.md): os comandos do `cq`: os do catálogo, um por operação da API, e os locais, que operam a
  pilha ou iniciam o servidor MCP.
- [API](referencia/api.md): as operações da API, com os esquemas versionados e a especificação OpenAPI que a pilha
  local serve em `/openapi.json`.
- [MCP](referencia/mcp.md): as ferramentas do servidor MCP, inclusive as destrutivas, que passam por duas fases.
- [Tipos de questão](referencia/tipos-de-questao.md): os elementos de resposta do PrairieLearn que o sistema suporta,
  as recusas da inspeção de uma questão e a vírgula decimal.

## Explicação

Textos para entender o porquê do sistema. O índice da parte está em [Explicação](explicacao/README.md).

- [Arquitetura](explicacao/arquitetura.md): como o sistema se organiza, das pessoas e dos sistemas externos aos
  contêineres, às portas e aos adaptadores, ao motor de questões e à representação intermediária entre o motor e o
  canal, com o caminho de uma questão pelo sistema e os diagramas.
- [Método de desenvolvimento](explicacao/metodo.md): como o sistema foi construído, em fatias, com a especificação
  primeiro, os testes antes do código, a revisão adversarial e os testes congelados.
- [Princípios](explicacao/principios.md): os sete princípios do projeto e as restrições técnicas.
- [Decisões de projeto](explicacao/decisoes.md): os requisitos e as principais decisões, com o motivo de cada uma.

## Diagramas

Os diagramas ficam em [`diagramas/`](diagramas/README.md): os fontes em D2, com o SVG renderizado ao lado de cada um.
Eles cobrem o contexto e os contêineres (no modelo C4), a implantação, as camadas do motor, a operação por agente, os
estados de uma entrega, o modelo de dados e o ciclo de desenvolvimento. Os textos de explicação e o README os usam.
