# Registro de mudanças

Todas as mudanças relevantes do canal-questoes ficam registradas neste arquivo, da versão mais nova para a mais
antiga, cada uma com a data em que saiu.

O formato segue o [Keep a Changelog 1.1.0](https://keepachangelog.com/pt-BR/1.1.0/), com as categorias da tradução
em português (Adicionado, Modificado, Obsoleto, Removido, Corrigido e Segurança). As versões seguem o
[Versionamento Semântico 2.0.0](https://semver.org/lang/pt-BR/): na série 0.y.z, de desenvolvimento inicial, qualquer
coisa pode mudar de uma versão para a outra.

## [Unreleased]

## [0.1.2] - 2026-11-24

Versão de manutenção: o Docker Engine mínimo sobe para o 26.0.2, por uma falha de segurança do 26.0.0 e do 26.0.1; o
CI passa a fixar a versão do uv; e mudam o guia de publicação de versões, o documento dos princípios, dois textos dos
contratos e um comentário do workflow do motor. O funcionamento do sistema não muda; muda só o número da versão que
ele mostra.

### Modificado

- **Número da versão:** os pacotes `cq-server` e `cq-client` e o plugin do Claude Code passam a 0.1.2, e é esse o
  número que a saúde da API (`GET /health`, `cq health` e `cq status`) e o servidor MCP mostram.
- **CI:** a ação que instala o uv passa da 10.3.3 para a 10.4.0, e o CI fixa o uv 0.11.16, o mesmo da imagem da api e
  do worker; antes, ele instalava a versão mais recente do uv.
- **Lista de testes instáveis do CI** (`ops/ci/known-flaky.txt`): ganhou um teste de integração, o da recusa do
  cabeçalho de cliente inválido, que falhou uma vez com um erro do banco na preparação dos dados do próprio teste e
  passou isolado.
- **Dependabot:** deixa de propor as versões menores e maiores da imagem do Python, porque o projeto fixa o Python
  3.12, e as do `uv-build`, que acompanha o uv fixado.
- **Guia de publicação de versões:** a seção nova "Entre as versões: as pull requests do Dependabot" diz que nenhuma
  delas recebe merge no repositório público: a atualização aceita entra pela versão seguinte.
- **Guia de publicação de versões, passo 6:** quando um agente faz o commit da versão no clone público, o commit leva
  a linha `Co-Authored-By` com a atribuição do agente, e a conferência do passo passa a pedir essa linha; a mensagem
  continua a do guia, `chore(release): v<versão>`, sem outro texto.
- **Princípios** ([`docs/explicacao/principios.md`](docs/explicacao/principios.md)): o princípio V passa a dizer que
  o provisionamento de bots é hoje parte da porta do Telegram, com o token colado à mão, e que uma porta própria é a
  evolução prevista para os bots gerenciados do Telegram. O documento passa a citar a versão 1.0.1 do documento
  interno de princípios, que ganhou essa emenda de redação.
- **Contratos:** no catálogo, o motivo de a verificação de saúde da API (`system.health`) ficar fora do MCP agora diz
  onde fica o diagnóstico no MCP: na `run_doctor` e, na pilha local, na `stack_status`, que mostra a saúde da API. A
  descrição da ferramenta `stack_status` passa a citar também a saúde da API. Só texto: nenhum comportamento muda.
- **Workflow do motor:** o comentário do topo do `engine.yml` não diz mais que a cópia pública confere o congelamento
  dos testes do motor; essa conferência só roda no repositório de origem.
- **Conferência do congelamento dos testes** (`scripts/check-frozen-tests.sh`): com a lista de hashes dos testes do
  motor, o script para com o código 2 e aponta o `scripts/check-frozen-engine-tests.sh`, que é o conferidor dessa
  lista; antes, ele acusava que a lista de arquivos congelados tinha mudado.

### Segurança

- **Docker Engine mínimo:** sobe de 26.0.0 para 26.0.2, por causa da CVE-2024-32473: no 26.0.0 e no 26.0.1, o IPv6
  não é desativado nas interfaces de redes só IPv4. O cabeçalho do `compose.yaml`, o guia de implantação na VM, o
  tutorial de primeiro uso e o teste de sistema do isolamento do motor passam a pedir o 26.0.2; as séries 25 e 23
  continuam aceitas a partir do 25.0.5 e do 23.0.11, que não têm essa falha.

## [0.1.1] - 2026-11-22

Versão de correção, só de documentação e de testes: o tutorial de primeiro uso, depois de seguido de ponta a ponta, o
guia de contribuição e testes que falhavam de vez em quando por uma corrida ou uma folga de medição do próprio teste.
O funcionamento do sistema não muda; muda só o número da versão que ele mostra.

### Corrigido

- **Tutorial de primeiro uso**, depois de seguido de ponta a ponta com um bot real, uma conta real do Telegram e um
  SMTP real:
  - a tabela "Quando algo dá errado" ganhou a linha da página de senhas de app do Google que recusa a conta (a opção
    indisponível ou a confirmação de identidade que não passa), com os motivos da ajuda do Google e a saída por outra
    conta ou outro provedor de e-mail;
  - a linha do token recusado cita também a mensagem do `cq doctor`, que é outra;
  - a limpeza explica que o `-p cq` é o projeto do Compose que o `cq down` mostra, a trocar quando o perfil tem outro
    `compose_project`, e deixa de marcar como inferência que o `--rmi local` apaga as imagens da `api`, do `worker` e
    do `engine`.
- **Guia de contribuição:** a seção "Testes" manda baixar a imagem base do PrairieLearn, a da primeira linha `FROM`
  do `engine/Dockerfile`, antes da suíte inteira, como o CI já faz. Sem ela, os testes de sistema do motor falham.
- **Testes que falhavam de vez em quando**, sem mudar o que eles conferem:
  - seis testes de integração liam o banco antes de o worker concluir a tarefa, ou andavam o relógio falso além da
    espera de uma nova tentativa; agora esperam o fim da tarefa ou dão um passo menor;
  - os dois casos do erro 429 nos testes de falhas de entrega do Telegram usam a folga de medição dos outros testes
    de ritmo, porque, numa máquina carregada, o agendamento das threads encolhia o intervalo medido.

### Modificado

- **Número da versão:** os pacotes `cq-server` e `cq-client` e o plugin do Claude Code passam a 0.1.1, e é esse o
  número que a saúde da API (`GET /health`, `cq health` e `cq status`) e o servidor MCP mostram.
- **Lista de testes instáveis do CI** (`ops/ci/known-flaky.txt`): ganhou 27 testes: 24 que medem o tempo de parede
  (entre eles, os dois casos do 429) e três com uma corrida do próprio teste, provada ou suspeita (dois deles
  corrigidos acima). Quando todas as falhas de uma suíte são da lista, o CI repete só esses testes uma vez, e a
  repetição decide.

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
