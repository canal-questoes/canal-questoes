# CLI (`cq`)

O `cq` é o cliente de linha de comando do canal-questoes. Ele tem dois tipos de comando:

- os **comandos do catálogo**, um por operação da API: cada um faz um pedido HTTP à API do perfil em uso e imprime o
  resultado;
- os **comandos locais** (`cq up`, `cq down`, `cq status`, `cq logs` e `cq mcp`), que operam a pilha Docker Compose
  da sua máquina ou iniciam o servidor MCP, sem passar pelo catálogo.

Todo comando aceita `--help`. Esta página resume o que a ajuda de cada um diz; se houver diferença, vale a ajuda do
`cq` que você tem instalado.

## Como rodar

Dentro do repositório, `uv run cq --help` funciona sem instalar nada. Para ter o comando `cq` em qualquer pasta:

```bash
uv tool install ./client       # instala o pacote do cliente (Python 3.12) e o comando cq
uv tool update-shell           # uma vez: põe o diretório dos executáveis do uv no PATH
```

Depois de atualizar o repositório, reinstale com `uv tool install --reinstall ./client`.

## Opções globais

| Opção | Efeito |
| --- | --- |
| `--json` | imprime o corpo JSON da resposta da API; nos comandos locais e no `cq doctor`, a saída JSON descrita em [`contracts/cli/v1/schemas/`](../../contracts/cli/v1/schemas/) |
| `--profile NOME` | usa o perfil nomeado `NOME` do arquivo de perfis |

As duas vêm **antes** do subcomando, por exemplo: `cq --json --profile meu-perfil offerings list`.

## Perfil: o endereço da API e o token

O endereço da API e o token vêm de um perfil, guardado num arquivo TOML. Nenhuma opção nem argumento recebe o token.

**O arquivo.** É o caminho de `CQ_CONFIG`, se definida e não vazia; senão, o arquivo padrão do `cq`:

| Sistema | Arquivo padrão |
| --- | --- |
| Windows | `%APPDATA%\cq\config.toml` |
| Linux | `~/.config/cq/config.toml` (ou `$XDG_CONFIG_HOME/cq/config.toml`) |
| macOS | `~/Library/Application Support/cq/config.toml` |

**Os perfis.** O arquivo pode ter perfis nomeados em `[profiles.NOME]` e um `default_profile`:

```toml
default_profile = "local"

[profiles.local]
kind = "local"                     # local ou remote; ausente, inferido do endereço
api_url = "http://127.0.0.1:8000"
token = "<segredo do token>"
stack_dir = "<caminho do repositório>"   # só no perfil local
compose_project = "cq"                   # só no perfil local
```

| Chave | O que é |
| --- | --- |
| `kind` | `local` ou `remote`. Ausente, é `local` quando o host do endereço é `127.0.0.1`, `::1` ou `localhost`, e `remote` nos demais |
| `api_url` | o endereço da API |
| `token` | o segredo do token da API |
| `stack_dir` | o diretório da pilha (o repositório com o `compose.yaml`); só no perfil local |
| `compose_project` | o nome do projeto Compose da pilha; só no perfil local; padrão `cq`; letras minúsculas, dígitos, `_` e `-`, começando por letra ou dígito; `cq-demo` e `cq-systest` são reservados |

**A escolha do perfil**, nesta ordem: a opção `--profile`, a variável `CQ_PROFILE`, o `default_profile` do arquivo.
Sem nenhum deles, vale o perfil implícito: as chaves `api_url` e `token` do topo do arquivo. Em qualquer caso,
`CQ_API_URL` e `CQ_TOKEN`, quando não vazias, vencem o campo correspondente do perfil. Sem endereço em lugar nenhum, o
`cq` usa `http://127.0.0.1:8000`. Uma variável vazia vale como ausente.

Um perfil inválido (nome inexistente, chave desconhecida, endereço ou token fora do formato) é uso incorreto (código
2), com uma mensagem fixa que não repete o conteúdo do arquivo. Com um perfil nomeado remoto em `http://` fora do
endereço local, o `cq` imprime um aviso no erro padrão e segue.

**O diretório da pilha**, usado pelos comandos locais: o `stack_dir` do perfil; senão, `CQ_STACK_DIR`; senão, o
diretório atual ou o ancestral mais próximo que tenha `compose.yaml`, `engine/Dockerfile`, `server/pyproject.toml` e
`client/pyproject.toml`.

### Variáveis de ambiente do cliente

| Variável | Efeito |
| --- | --- |
| `CQ_CONFIG` | o caminho do arquivo de perfis |
| `CQ_PROFILE` | o nome do perfil, quando não há `--profile` |
| `CQ_API_URL` | o endereço da API; vence o do perfil |
| `CQ_TOKEN` | o segredo do token; vence o do perfil |
| `CQ_STACK_DIR` | o diretório da pilha, quando o perfil local não tem `stack_dir` |
| `CQ_DOCTOR_DISK_WARNING_PERCENT` | no `cq doctor`, o percentual de disco livre abaixo do qual a verificação do disco dá aviso (padrão 20) |
| `CQ_DOCTOR_DISK_FAILURE_PERCENT` | no `cq doctor`, o percentual abaixo do qual ela dá falha (padrão 10); tem de ser menor que o do aviso |
| `CQ_BACKUP_DIR` | no `cq doctor`, o caminho absoluto de um diretório a mais cujo disco é medido, além do diretório de dados do Docker |

Um valor inválido nas três variáveis do `cq doctor` é uso incorreto (código 2), antes de qualquer chamada.

## Códigos de saída

| Código | Situação |
| --- | --- |
| 0 | sucesso |
| 1 | a API recusou o pedido (resposta 4xx ou 5xx); o `cq health` achou a API com problema; o `cq status` ou o `cq doctor` acharam um problema; uma falha do Docker nos comandos locais; a pilha que não fica saudável no tempo do `cq up` ou publica outra porta que a do perfil |
| 2 | uso incorreto: opção, argumento ou perfil inválido; em `cq up`, `cq down` e `cq logs`, também o perfil remoto e o `.env` ausente no diretório da pilha; neles e no `cq status`, o diretório da pilha não encontrado, uma variável obrigatória ausente no `.env` e o Docker apontando para um alvo que não é local (`DOCKER_HOST` ou `DOCKER_CONTEXT`) |
| 3 | a API está inalcançável |
| 130 | interrompido com Ctrl+C: em `cq up`, `cq down`, `cq status` e `cq logs`, no `cq doctor`, nas duas fases do expurgo e na pergunta para subir a pilha local quando a API está inalcançável (inclusive durante a subida); nos demais comandos, o Ctrl+C sai com 1, e no `cq mcp`, com 0 |

Com código diferente de 0, as mensagens vão para o erro padrão, sem rastreamento de pilha. O `cq status`, o
`cq doctor` e o `cq health` com `--json` imprimem o resultado na saída padrão também quando saem com 1.

Num comando do catálogo, com a API inalcançável, o perfil local, sem `CQ_API_URL` e sem `--json`, o `cq` sugere subir
a pilha local; com a entrada e o erro padrão num terminal, pergunta se deve subi-la. No "sim", sobe a pilha como o
`cq up` e pede para repetir o comando, sem repeti-lo: o código continua 3, salvo se a subida falhar (aí vale o código
do `cq up`). Com `--json`, sai só o erro em JSON no erro padrão, sem a dica nem a pergunta. O `cq doctor` mostra só a
dica, sem a pergunta.

## Comandos do catálogo

Cada comando corresponde a uma operação do [catálogo](../../contracts/api/v1/catalog.json); a coluna "Operação"
traz o identificador dela, o mesmo da [API](api.md) e do [MCP](mcp.md). Nos comandos de lista, `--after` é o cursor
(só os itens depois dele) e `--limit` é o máximo de itens, de 1 a 500.

### Diagnóstico

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq health` | `system.health` | A saúde da API e do banco: a versão da API e a do esquema do banco. Não exige token. |
| `cq doctor` | `system.doctor` | Confere a API e o banco; as verificações do servidor (bots, tarefas atrasadas, relógio, backup, SMTP e retenção); só com o perfil local, a pilha (Docker, Compose, serviços, imagem do motor) e o host (o volume do Postgres e o disco); e o arquivo de perfis (as permissões e se ele está dentro de uma cópia git). Cada verificação sai numa linha `[resultado] id: mensagem`, com o resultado `ok`, `aviso`, `falha`, `não verificado` ou `não se aplica`, e o resumo diz quantas falhas e avisos houve. Sai com 1 se houver ao menos uma falha. O guia de [observabilidade](../operacao/observabilidade.md) trata do uso do `doctor` na operação. |

### Pessoas e identidades

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq people add --name NOME` | `people.register` | Cadastra uma pessoa; `--ra` e `--email` são opcionais. A resposta traz o pseudônimo (o identificador e o apelido), não a identidade. |
| `cq people list` | `people.list` | Lista as pessoas em ordem de cadastro, só pelos pseudônimos; aceita `--after` e `--limit`. |
| `cq people show REF` | `people.get` | Mostra uma pessoa pelo apelido ou pelo identificador. |
| `cq identity reveal REF... --reason TEXTO` | `identity.reveal` | Revela a identidade dos pseudônimos informados (até 100 por pedido). A justificativa é obrigatória e fica registrada no log de eventos. |

### Tokens da API

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq tokens create --label ROTULO --permission PERMISSAO` | `tokens.create` | Cria um token com as permissões dadas (repita `--permission` para várias; um token só concede permissões que ele mesmo tem). O segredo só é mostrado num terminal; fora dele, `--secret-file ARQUIVO` (um arquivo novo) é obrigatório. |
| `cq tokens list` | `tokens.list` | Lista os tokens em ordem de criação, sem os segredos. |
| `cq tokens revoke TOKEN_ID` | `tokens.revoke` | Revoga um token; revogar de novo não muda nada. |

As permissões são: `people:read`, `people:write`, `events:read`, `identity:reveal`, `offerings:read`,
`offerings:write`, `bots:manage` e `tokens:manage`. A [página da API](api.md#permissões) diz qual operação exige qual.

### Log de eventos

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq events list --type TIPO` | `events.list` | Lista os eventos em ordem de sequência. Exige ao menos um `--type` (repetível) ou `--person REF` (só os eventos que citam a pessoa); aceita `--after` e `--limit`. |

### Oferecimentos

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq offerings create CODE --course NOME --term PERIODO --timezone FUSO` | `offerings.create` | Cadastra um oferecimento, em rascunho. `--timezone` é um fuso IANA (ex.: `America/Sao_Paulo`). Opcionais: `--response-window` (a janela de resposta em minutos, de 5 a 43200; padrão 1440), `--free-messages` (`on` ou `off`, o padrão) e `--contact-email` (o contato do professor, indicado aos alunos quando as mensagens livres estão desligadas). |
| `cq offerings list` | `offerings.list` | Lista os oferecimentos em ordem de cadastro; aceita `--after` e `--limit`. |
| `cq offerings show REF` | `offerings.get` | Mostra um oferecimento pelo código ou pelo identificador. |
| `cq offerings settings REF` | `offerings.update_settings` | Altera as mensagens livres: `--free-messages on` ou `off`, `--contact-email` (define ou troca o contato) e `--clear-contact-email` (remove o contato). O e-mail nunca é impresso: a saída diz só se há contato. |
| `cq offerings digest show REF` | `offerings.get_digest` | Mostra a configuração do resumo diário à equipe. |
| `cq offerings digest set REF` | `offerings.update_digest` | Altera o resumo diário: `--enabled on` ou `off` e `--at HH:MM` (no fuso do oferecimento). |
| `cq offerings close REF` | `offerings.close` | Encerra um oferecimento ativo; encerrar de novo não muda nada. |
| `cq offerings archive prepare REF` | `offerings.prepare_archive` | Primeira fase do arquivamento: confere e mostra o código de confirmação. |
| `cq offerings archive confirm REF --confirmation CODIGO` | `offerings.archive` | Segunda fase: arquiva o oferecimento com o código da primeira. |
| `cq offerings purge prepare REF` | `offerings.prepare_purge` | Primeira fase do expurgo das identidades de um oferecimento arquivado: mostra o impacto e o código de confirmação. |
| `cq offerings purge confirm REF --confirmation CODIGO` | `offerings.purge` | Segunda fase: expurga as identidades, de forma irreversível. Repetida num oferecimento já expurgado, mostra o resultado gravado. |

### Bots do Telegram e equipe

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq bots register REF` | `bots.register` | Registra ou troca o bot do oferecimento. O token do bot nunca vai na linha de comando: vem de `--token-file ARQUIVO` (obrigatório fora de um terminal) ou de um prompt mascarado. |
| `cq bots activate REF` | `bots.activate` | Ativa o oferecimento com o bot registrado. |
| `cq bots show REF` | `bots.get` | Mostra o bot do oferecimento (estado, perfil, posse e problema), sem o token. |
| `cq team link REF --person REF` | `team.link` | Gera o link de equipe de uma pessoa, válido por `--valid-hours` horas (de 1 a 720; padrão 168). O link é uma credencial: só aparece num terminal; fora dele, `--secret-file ARQUIVO` (um arquivo novo) é obrigatório. |

### Alunos e convites

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq students import REF --file ARQUIVO` | `students.import` | Importa a lista da turma, tudo ou nada (até 1000 linhas). O arquivo é um CSV em UTF-8 com as colunas `ra`, `nome` e `email` na primeira linha, separadas por vírgula ou por ponto e vírgula. Só as contagens saem. |
| `cq students summary REF` | `students.summary` | Mostra a adesão dos alunos, só com contagens por estado. |
| `cq invites send prepare REF` | `invites.prepare_send` | Primeira fase do envio dos convites por e-mail: mostra o código de confirmação. `--valid-hours` define a validade dos convites, a partir do envio (de 1 a 720; padrão 168). |
| `cq invites send confirm REF --confirmation CODIGO` | `invites.send` | Segunda fase: enfileira os convites da turma. |
| `cq invites resend REF PSEUDONYM...` | `invites.resend` | Reenvia o convite aos alunos informados pelo apelido ou pelo identificador (até 100 por pedido); aceita `--valid-hours`. Cada pedido cria envios novos, e só o último link vale. |

### Disparos e mensagens

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq dispatches prepare REF --repo URL --commit SHA --qid QID` | `dispatches.prepare` | Prepara o disparo de uma questão: `--repo` é a URL `https` do repositório da questão, `--commit` o SHA do commit (40 ou 64 dígitos hexadecimais) e `--qid` o caminho da questão no repositório. `--opens-at` e `--closes-at` (ISO 8601 com deslocamento, ex.: `2031-01-02T09:00:00-03:00`) definem a janela; sem a abertura, ela é imediata; sem o fechamento, vale a janela do oferecimento. A validação da questão roda depois, no worker (o serviço de segundo plano da pilha). |
| `cq dispatches show REF DISPATCH_ID` | `dispatches.get` | Mostra um disparo, com a prévia da questão depois da validação ou o motivo da rejeição. |
| `cq dispatches list REF` | `dispatches.list` | Lista os disparos do oferecimento em ordem de preparo; aceita `--after` e `--limit`. |
| `cq dispatches confirm REF DISPATCH_ID` | `dispatches.confirm` | Confirma um disparo validado: a questão vai à turma no horário de abertura. |
| `cq dispatches cancel REF DISPATCH_ID` | `dispatches.cancel` | Cancela um disparo que ainda não abriu. |
| `cq messages list REF` | `messages.list` | Lista as mensagens livres dos alunos, com o texto redigido; aceita `--after` e `--limit`. |

A [página dos tipos de questão](tipos-de-questao.md) diz quais questões passam pela validação.

### Relatórios

| Comando | Operação | O que faz |
| --- | --- | --- |
| `cq reports show REF DISPATCH_ID` | `reports.get` | Mostra o relatório de um disparo, calculado agora ou, com `--version` (um número ou `latest`), o de uma versão congelada. `--csv` imprime o CSV da API como veio (não combina com `--json`). |
| `cq reports versions REF DISPATCH_ID` | `reports.list_versions` | Lista as versões congeladas do relatório; aceita `--after` e `--limit`. |
| `cq reports resend REF DISPATCH_ID` | `reports.resend_summary` | Congela a versão seguinte do relatório de um disparo fechado e reenvia o resumo à equipe. |

## O ciclo de um oferecimento

Os comandos acima seguem o ciclo do oferecimento. `cq offerings create` o cria em rascunho; `cq bots register` e
`cq bots activate` o deixam ativo. A lista da turma pode ser importada no rascunho ou com o oferecimento ativo; os
convites e os disparos exigem o oferecimento ativo. `cq offerings close` encerra um oferecimento ativo; o
arquivamento vale para um rascunho ou um oferecimento encerrado; e o expurgo apaga as identidades de um oferecimento
arquivado. Uma operação fora do estado certo é recusada pela API (código 1), e o texto diz o estado atual.

## Comandos locais

Os comandos locais não estão no catálogo e não chamam operações da API (salvo a saúde, no `cq up` e no `cq status`).
`cq up`, `cq down` e `cq logs` exigem o perfil local, o Docker e o arquivo `.env` no diretório da pilha (criado a
partir do `.env.example`). Com `--json`, a saída segue os esquemas de
[`contracts/cli/v1/schemas/`](../../contracts/cli/v1/schemas/).

| Comando | O que faz |
| --- | --- |
| `cq up` | Sobe a pilha local e espera a API ficar saudável. `--timeout` é a espera máxima, em segundos (de 1 a 3600; padrão 180); `--rebuild` reconstrói antes as imagens da api e do worker. |
| `cq down` | Para a pilha local: remove os contêineres e a rede do projeto e mantém os volumes, com os dados. Com a pilha já parada, não falha. |
| `cq status` | Mostra o perfil em uso (e a origem de cada campo), a pilha local (só no perfil local) e a saúde da API. Sai com 1 quando acha um problema, com o relatório na saída padrão. |
| `cq logs` | Mostra as últimas linhas dos registros da api, do worker e do postgres como os serviços as escreveram, só sem as sequências de controle do terminal (a redação por lista de permissão é da ferramenta `stack_logs` do MCP). `--service` escolhe o serviço (`api`, `worker` ou `postgres`; repetível; padrão os três), `--tail` o número de linhas finais de cada um (de 1 a 10000; padrão 200), e `--follow` segue os registros novos (não combina com `--json`). Os registros do motor de questões ficam de fora. |
| `cq mcp` | Inicia o servidor MCP, por stdio, para um agente, com o perfil escolhido. Veja a [página do MCP](mcp.md). |
