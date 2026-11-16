# Registro de mudanças

Todas as mudanças relevantes do canal-questoes ficam registradas neste arquivo, da versão mais nova para a mais
antiga, cada uma com a data em que saiu.

O formato segue o [Keep a Changelog 1.1.0](https://keepachangelog.com/pt-BR/1.1.0/), com as categorias da tradução
em português (Adicionado, Modificado, Obsoleto, Removido, Corrigido e Segurança). As versões seguem o
[Versionamento Semântico 2.0.0](https://semver.org/lang/pt-BR/): na série 0.y.z, de desenvolvimento inicial, qualquer
coisa pode mudar de uma versão para a outra.

## [Unreleased]

## [0.1.0] - 2026-11-16

Primeira versão pública.

**O piloto.** O piloto em disciplinas da Unicamp ainda não começou. Quando começar, ele vai rodar o código desta
versão.

### Adicionado

- **Motor de questões.** Executa questões do PrairieLearn dentro da imagem oficial do PrairieLearn, fixada por hash, e
  reutiliza o código dele. Oferece três operações sem estado: inspecionar a questão, criar uma variante e corrigir uma
  resposta. A inspeção diz se a questão é suportada e, se não for, por quê.
- **Cinco elementos de resposta:** `pl-multiple-choice`, `pl-checkbox`, `pl-number-input`, `pl-integer-input` e
  `pl-string-input`.
- **Vírgula decimal.** Num campo `pl-number-input`, uma resposta com uma só vírgula e nenhum ponto, como `3,14`, é
  lida como `3.14`. É a única diferença deliberada em relação à correção do PrairieLearn.
- **Oferecimentos de disciplina:** criar, listar, consultar, ajustar as configurações, encerrar, arquivar e, por fim,
  expurgar as identidades do oferecimento. O arquivamento e o expurgo passam por duas fases: a preparação mostra o
  código de confirmação (no expurgo, também o impacto), e a confirmação exige esse código.
- **Um bot do Telegram por oferecimento:** registrar o bot, ativar o oferecimento com ele e consultar o estado do
  bot. Os alunos recebem as
  questões e respondem no próprio chat.
- **Turma e convites:** importar a lista da turma de um arquivo CSV (colunas `ra`, `nome` e `email`), tudo ou nada;
  enviar os convites por e-mail, também em duas fases, e reenviá-los; acompanhar a adesão. No bot, o aluno entra pelo
  convite, aceita ou recusa os termos e pode pedir um convite novo.
- **Pessoas e equipe:** cadastrar pessoas e depois listá-las e consultá-las só pelo pseudônimo; gerar para uma pessoa
  o link de equipe de um oferecimento. Pelo link, ela entra no bot e passa a receber os avisos, o resumo de cada disparo
  fechado e, se ligado no oferecimento (ele vem desligado), o resumo diário, num horário local configurável.
- **Mensagens livres:** com a opção ligada no oferecimento (ela vem desligada), o texto que um aluno manda ao bot sem
  ser resposta a uma questão fica guardado, com o nome, o RA e o e-mail das pessoas cadastradas trocados pelo
  pseudônimo, e pode ser lido pela API e pelo CLI (`cq messages list`).
- **Disparos:** preparar o disparo de uma questão guardada num repositório Git (o endereço https, o commit e o caminho
  da questão), com abertura e fechamento opcionais; confirmar ou cancelar; listar e consultar. A validação da questão
  roda no worker.
- **Relatórios:** o relatório descritivo de cada disparo, calculado na hora ou numa versão congelada, também em CSV; o
  reenvio do resumo à equipe congela a versão seguinte do relatório.
- **Privacidade por construção:** os alunos aparecem só por pseudônimo, e a identidade sai apenas pela operação de
  revelação, com uma justificativa obrigatória que fica registrada no log de eventos.
- **Nenhuma resposta perdida:** cada atualização recebida do Telegram é gravada antes de ser processada, sem
  duplicata, e o processamento é uma tarefa idempotente.
- **Três formas de operar:** a API HTTP e o CLI `cq` cobrem as mesmas operações, listadas num catálogo único, e o CLI
  tem ainda os comandos da pilha local. O servidor MCP (`cq mcp`) expõe a um agente a maior parte dessas operações e as
  da pilha local. Entre as que ficam só na API e no CLI estão as que lidam com segredos ou com dados pessoais: o
  cadastro de pessoas, o registro do bot, o link de equipe, os tokens de acesso e as mensagens livres. A revelação de
  identidades também está no MCP, e quem opera aprova cada chamada.
- **Plugin do Claude Code** (`canal-questoes`): o servidor MCP e sete skills dos fluxos do professor (oferecimento,
  lista e convites, disparo, relatórios, revelação, encerramento e pilha), com as operações destrutivas em duas fases
  e a confirmação do operador.
- **Acesso à API por tokens:** criar, listar e revogar.
- **Log de eventos**, consultável pela API e pelo CLI.
- **Pilha local com Docker Compose:** `cq up`, `cq down`, `cq status` e `cq logs`; o `cq doctor` confere o perfil, a
  API e a pilha. O CLI lê perfis locais e remotos de um arquivo TOML.
- **Implantação numa máquina virtual**, com cópia de segurança diária cifrada com o age, ensaio de restauração numa
  pilha descartável e uma política de retenção dos dados.
- **Documentação** para quem chega de fora: um tutorial de primeiro uso, a referência das operações, a explicação da
  arquitetura, do método e dos princípios, e os guias de operação. O índice está em [`docs/README.md`](docs/README.md).
- **Licença** AGPL-3.0-or-later.
