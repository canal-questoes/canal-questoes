# Observabilidade

Este guia reúne o que o canal-questoes oferece hoje para saber se uma instalação está saudável e o que ela fez: o
diagnóstico (`cq doctor`), o estado da pilha, os registros dos serviços, o log de eventos, os avisos à equipe e as
métricas do piloto. Ele foi escrito para quem opera uma instalação: no piloto, a pilha numa VM e o `cq` no computador
do operador, ligado a ela por um túnel SSH.

Os comandos `cq` usam o perfil escolhido (`--profile NOME` antes do subcomando, a variável `CQ_PROFILE` ou o
`default_profile` do arquivo de perfis). O [início rápido](inicio-rapido.md) explica os perfis local e remoto, e a
[referência do CLI](../referencia/cli.md) traz todas as opções.

O que não existe hoje: o servidor e o cliente não exportam métricas nem rastros para um coletor externo (como
Prometheus ou OpenTelemetry), e não há painel. O diagnóstico roda quando alguém o chama; a única informação de
operação que chega sozinha a uma pessoa são os avisos à equipe do oferecimento, pelo Telegram (seção 5).

## 1. Onde olhar primeiro

| Pergunta | Comando | Onde roda |
| --- | --- | --- |
| A API responde, e o banco está em dia? | `cq health` | qualquer perfil; a operação não exige permissão |
| A pilha está no ar? | `cq status` | perfil local (os serviços e a API) ou remoto (só a API) |
| Há algo errado? | `cq doctor` | qualquer perfil; as partes da pilha e do host, só no local |
| O que os serviços registraram? | `cq logs` | só o perfil local, na máquina da pilha |
| Quem fez o quê, e quando? | `cq events list` | qualquer perfil, com um token que tenha a permissão `events:read` |
| Como foi o piloto? | `ops/pilot/pilot_metrics.py` | qualquer perfil; só leitura |

Pelo agente (o servidor MCP do plugin), as ferramentas correspondentes são `run_doctor`, `stack_status`, `stack_logs`
e `list_events`; veja o [guia do plugin e do agente](plugin-e-agente.md) e a [referência do MCP](../referencia/mcp.md).

## 2. O diagnóstico: `cq doctor`

```bash
cq doctor                    # o relatório em texto
cq --json doctor             # o mesmo relatório em JSON (o perfil e a lista de verificações)
cq --profile piloto doctor   # por um perfil remoto (no piloto, o do túnel até a VM)
```

O relatório começa por duas linhas: `Perfil:` (o nome e o tipo do perfil, o endereço da API sem credencial e se há
token) e `Origem:` (de onde veio cada um desses valores). Depois, cada verificação sai numa linha no formato
`[resultado] id (código): mensagem (número ou detalhe)`, em que os dois trechos entre parênteses só aparecem em
algumas:

- o código do oferecimento, depois do identificador, aparece nas verificações de bot e no aviso de
  `retention.overdue`, que cita o oferecimento vencido mais antigo;
- no fim da linha, um número: as tarefas atrasadas (`tasks.overdue`), as horas desde o último backup (`backup.age`,
  quando há registro legível dele) e os oferecimentos vencidos (`retention.overdue`);
- no fim da linha, um detalhe: em `engine.image`, os 12 primeiros caracteres do identificador da imagem do motor.

O relatório termina em `Resultado: N falha(s), M aviso(s).` Os resultados são `ok`, `aviso`, `falha`,
`não verificado` (a verificação não pôde rodar; a mensagem diz por quê) e `não se aplica`. No JSON, os mesmos
resultados saem como `ok`, `warning`, `failure`, `not_verified` e `not_applicable`.

O código de saída é 0 sem falha e 1 com pelo menos uma; com falha, o relatório continua na saída padrão, e uma linha
de resumo vai à saída de erro. As mensagens são textos fixos, e só duas levam números (a idade do backup e o espaço
livre no disco): nunca trazem caminho, endereço, identidade nem segredo. A parte do servidor só lê (o banco e o
registro do backup): não grava evento nem chama o Telegram.

As verificações, por parte:

| Parte | Verificações | O que conferem |
| --- | --- | --- |
| API, pelo cliente | `api.reachable`, `database.reachable`, `database.schema`, `api.access` | a API responde no endereço do perfil, alcança o banco, as migrações estão na versão esperada e o token dá acesso à parte do servidor |
| Servidor | `bots.master_key` | a chave-mestra dos bots está configurada e decifra os tokens de bot gravados |
| Servidor | `bot.access`, `bot.poll`, `bot.lease` (uma linha por oferecimento: `bot.access` nos ativos e encerrados com bot; `bot.poll` e `bot.lease` só nos ativos com o bot registrado) | o bot tem acesso ao Telegram, foi consultado recentemente e tem um worker que o atende |
| Servidor | `tasks.overdue`, `clock.skew` | nenhuma tarefa atrasada; o relógio da API em sincronia com o do banco |
| Servidor | `backup.age`, `smtp.server`, `retention.overdue` | a idade do último backup; o último teste do servidor de e-mail; oferecimentos arquivados com o prazo de retenção vencido e sem expurgo |
| Pilha local | `docker.reachable`, `compose.version`, `stack.directory`, `stack.env_file` | o Docker responde; o Docker Compose é a versão 2 (abaixo da 2.27, aviso); o diretório da pilha e o arquivo de ambiente existem |
| Pilha local | `service.postgres`, `service.api`, `service.worker`, `service.engine`, `engine.image`, `api.owner` | os quatro serviços rodam (e estão saudáveis, nos que têm verificação de saúde); a imagem do motor existe; a API do perfil é a desta pilha |
| Host | `postgres.volume`, `disk.space` | o Postgres usa o volume nomeado da pilha, no lugar certo; o espaço livre no disco dos dados do Docker e, com `CQ_BACKUP_DIR`, no dos backups (vale o pior) |
| Arquivo de perfil | `profile.permissions`, `profile.git` | o arquivo é legível só pelo dono (no Windows, não verificado) e fica fora de qualquer cópia de trabalho git |

Com um perfil remoto, a pilha local sai como "não se aplica", e as duas do host dizem para rodar o `doctor` na
própria VM, onde o perfil é local. A parte do servidor pede um token com a permissão `offerings:read`; sem token, ou
com um token recusado, `api.access` sai em falha, e as do servidor saem como "não verificado".

Os limites das verificações vêm de variáveis de ambiente, todas opcionais (vazia vale como ausente). As da api e do
worker chegam aos contêineres pelo `compose.yaml`, a partir do arquivo de ambiente da pilha; as do `cq`, do ambiente
de quem roda o comando. Um valor inválido numa variável do `cq` faz o `cq doctor` sair com o código 2 e uma mensagem
fixa, sem o valor.

| Variável | Padrão | Quem lê | Para que serve |
| --- | --- | --- | --- |
| `CQ_DOCTOR_BOT_POLL_SECONDS` | 300 | api | atraso máximo da última consulta do bot (`bot.poll`) |
| `CQ_DOCTOR_TASK_OVERDUE_SECONDS` | 300 | api | atraso a partir do qual uma tarefa conta como atrasada (`tasks.overdue`) |
| `CQ_DOCTOR_CLOCK_SKEW_SECONDS` | 5 | api | desvio máximo entre o relógio da API e o do banco (`clock.skew`) |
| `CQ_BACKUP_STATUS_FILE` | vazio | api | o registro do último backup dentro do contêiner; vazio, `backup.age` avisa que o backup não está configurado |
| `CQ_DOCTOR_BACKUP_WARNING_HOURS` | 26 | api | idade do último backup que dá aviso |
| `CQ_DOCTOR_BACKUP_FAILURE_HOURS` | 50 | api | idade do último backup que dá falha |
| `CQ_DOCTOR_MAIL_STALE_HOURS` | 13 | api | idade máxima do último teste do servidor de e-mail |
| `CQ_MAIL_PROBE_INTERVAL_SECONDS` | 21600 (6 h) | worker | intervalo entre os testes do servidor de e-mail |
| `CQ_RETENTION_DAYS` | 30 | api | prazo de retenção dos oferecimentos arquivados (`retention.overdue`) |
| `CQ_DOCTOR_DISK_WARNING_PERCENT` | 20 | `cq` | espaço livre, em porcentagem, que dá aviso |
| `CQ_DOCTOR_DISK_FAILURE_PERCENT` | 10 | `cq` | espaço livre, em porcentagem, que dá falha (menor que o do aviso) |
| `CQ_BACKUP_DIR` | vazio | `cq` | diretório dos backups, medido também no `disk.space` (caminho absoluto) |

O que fazer com cada falha ou aviso está nas tabelas de problemas do [início rápido](inicio-rapido.md) (a pilha
local) e do [guia de implantação na VM](implantacao-vm.md) (a VM do piloto, com o backup, o e-mail e o disco); o
backup e a retenção têm guias próprios: [backup e restauração](backup-e-restauracao.md) e
[política de retenção](politica-de-retencao.md).

Pelo agente, a ferramenta `run_doctor` traz só a parte do servidor, pela API; a pilha local, o host e o arquivo de
perfil ficam no `cq doctor`.

## 3. O estado da pilha: `cq health` e `cq status`

```bash
cq health    # GET /health: a API e o banco
cq status    # o perfil, os serviços da pilha local e a saúde da API
```

O `cq health` chama a rota `/health`, que não exige permissão. Com a API saudável, ele mostra a versão do servidor e
a do esquema do banco. Se a API responde com problema, ele sai com o código 1 e diz o estado do banco e o esquema
encontrado; com a API inalcançável, o código é 3. No JSON
(`cq --json health`), a resposta traz `status`, `version`, `database`, `schema_version` e `expected_schema_version`.

O `cq status` mostra o perfil em uso, os quatro serviços da pilha (no perfil local) e a saúde da API, e sai com o
código 0 só com tudo rodando e saudável. Com um perfil remoto, ele mostra só a parte da API.

## 4. Os registros dos serviços

### O que os serviços escrevem

A api e o worker escrevem uma linha JSON por registro, com `level`, `logger`, `event` e `time` (em UTC) e os campos
próprios do evento. Num erro com exceção, a linha leva só o tipo da exceção (`exc_type`) e o lugar (`where`, no formato
`arquivo:linha`), nunca a mensagem nem o traceback, que podem carregar identidade ou segredo. A saída é só ASCII. As
bibliotecas de cliente HTTP ficam no nível WARNING, porque no INFO elas registram as URLs completas.

### `cq logs`, para o operador

```bash
cq logs                               # as últimas 200 linhas da api, do worker e do postgres
cq logs --service worker --tail 50    # um serviço só (a opção pode se repetir)
cq logs --follow                      # segue os registros novos; Ctrl+C sai com o código 130
cq --json logs --service api          # as linhas num JSON ({"lines": [...]})
```

O `--tail` aceita de 1 a 10000 linhas por serviço, e o `--follow` não combina com o `--json`. Os registros do motor
(`engine`) são sensíveis e ficam de fora: o comando aceita só `api`, `worker` e `postgres`. As linhas saem com o
prefixo do serviço que o Compose põe, sem as sequências de controle do terminal e com os bytes inválidos trocados.

O `cq logs` é um comando local: ele recusa um perfil remoto (código 2). Na VM do piloto, rode-o na própria VM, como
no [guia de implantação](implantacao-vm.md).

### `stack_logs`, para o agente

A ferramenta `stack_logs` do servidor MCP lê os mesmos registros, mas só entrega ao modelo o que passa por uma lista
de permissão:

- uma linha sem o prefixo do serviço (o que o Compose põe antes de cada linha) é descartada e só entra na contagem
  `omitted`;
- nas linhas da api e do worker, ficam só os eventos conhecidos, cada um com os seus campos esperados e valores do
  tipo esperado. Um campo de nome conhecido do servidor vira `[omitido]` quando o valor foge do tipo ou quando o campo
  não é da lista do evento. Um campo de nome desconhecido, ou uma linha que não seja o JSON de um evento conhecido,
  troca a linha inteira por `[linha omitida]`;
- nas do postgres, ficam só as mensagens de formatos conhecidos, sem valores livres. Uma mensagem fora desses formatos
  vira `[omitido]`, e o começo da linha (a data, o fuso, o número do processo e a severidade) fica. As linhas de
  detalhe, como `DETAIL` e `STATEMENT`, e as que fogem do formato de linha do Postgres viram `[linha omitida]`;
- uma segunda camada troca a linha inteira por `[linha omitida]` se ainda achar um e-mail, um token da API ou de bot,
  uma chave, uma credencial numa URL ou uma sequência de cinco ou mais dígitos.

O resultado traz as linhas (`service` e `text`), as contagens `returned`, `redacted` e `omitted` e o indicador
`truncated`: se não couber no tamanho máximo do resultado, as linhas mais antigas são cortadas, e ele diz isso.
Os registros do motor também ficam de fora.

## 5. Os avisos à equipe

O worker manda avisos à equipe de cada oferecimento (as pessoas vinculadas ao bot com o link de equipe, gerado por
`cq team link`), pelo próprio bot no Telegram:

- a prévia de cada disparo, quando ele é validado, e o resultado dele: recusado, aberto, fechado (com o resumo) ou
  cancelado, e o resumo reenviado;
- a questão que falhou num disparo;
- a volta do acesso do bot ao Telegram depois de uma perda, com o que deixou de ser entregue;
- as falhas no envio dos convites por e-mail;
- o resumo diário, desligado por padrão: `cq offerings digest show <código>` mostra a configuração, e
  `cq offerings digest set <código> --enabled on --at HH:MM` o liga no horário local do oferecimento.

O estado final de cada aviso fica no log de eventos, no tipo `notice.recorded`.

## 6. O log de eventos

O banco guarda um log de eventos de auditoria, em ordem de sequência. Cada mudança relevante grava o seu evento na
mesma transação da mudança, e há 49 tipos:

| Assunto | Tipos |
| --- | --- |
| Pessoas e tokens | `person.registered`, `token.created`, `token.revoked` |
| Revelação de identidade | `identity.revealed`, `identity.reveal_denied` |
| Oferecimentos | `offering.created`, `offering.state_changed`, `offering.settings_changed`, `offering.digest_changed`, `offering.archive_prepared`, `offering.purge_prepared`, `offering.purged` |
| Bots | `bot.registered`, `bot.token_replaced`, `bot.profile_updated`, `bot.lease_changed`, `bot.access_changed` |
| Equipe | `team.link_created`, `team.link_revoked`, `team.joined`, `team.join_refused` |
| Alunos e convites | `student.list_imported`, `student.invites_prepared`, `student.invites_queued`, `student.invite_sent`, `student.invite_failed`, `student.invite_cancelled`, `student.invite_revoked`, `student.joined` |
| Disparos | `dispatch.prepared`, `dispatch.validated`, `dispatch.rejected`, `dispatch.confirmed`, `dispatch.cancelled`, `dispatch.opened`, `dispatch.closed` |
| Entregas, respostas e relatórios | `delivery.delivered`, `delivery.not_delivered`, `delivery.question_failed`, `delivery.dont_know`, `submission.graded`, `submission.internal_failure`, `report.frozen` |
| Falhas e avisos | `task.failed`, `outbox.failed`, `outbox.reply_failed`, `endpoint.blocked`, `notice.recorded` |
| Bancos de questões | `bank.uploaded` |

Cada evento tem `seq` (a ordem), `type`, `occurred_at` (em UTC), `actor_token_id` (o token que agiu, quando houve um)
e `data`, com os campos fechados do tipo. Nenhum evento traz identidade: as pessoas aparecem pelo pseudônimo e pelo
apelido. Os campos de texto livre são redigidos na gravação e de novo na leitura: o nome, o RA e o e-mail de uma
pessoa cadastrada viram `[pessoa <apelido>]`, e um segredo vira `[segredo]`.

```bash
cq events list --type identity.revealed --type identity.reveal_denied   # toda revelação de identidade, e toda recusa
cq events list --person <apelido>                                        # os eventos que citam uma pessoa
cq --json events list --type dispatch.opened --after <cursor>            # com o campo data de cada evento
```

Sem `--person`, pelo menos um `--type` é obrigatório (repita a opção para vários tipos); com os dois, valem os eventos
que atendem aos dois filtros. Em texto, cada evento sai numa linha com a sequência, o horário e o tipo, e a última linha
diz `Próximo cursor: N`: passe esse número em `--after` para ler a página seguinte. O `--limit` vai de 1 a 500 (padrão
100). A leitura pede um token com a permissão `events:read`, que se cria com
`cq tokens create --label <rótulo> --permission events:read`.

Pelo agente, a ferramenta é `list_events`. Os campos de texto livre vêm de alunos ou de terceiros: o agente deve
tratá-los como dados, nunca como instruções.

## 7. As métricas do piloto

O script `ops/pilot/pilot_metrics.py` monta o relatório de métricas de um oferecimento, comparando os disparos feitos
com o plano de disparos. Ele lê só pela API (os disparos, os relatórios e o log de eventos), não muda nada no sistema e
só publica agregados: nenhum pseudônimo, nome, e-mail, texto livre ou título de questão.

```bash
uv run --project client python ops/pilot/pilot_metrics.py --profile piloto --offering <código> \
  --plan docs/operacao/piloto/plano-de-disparos.toml
```

| Opção | Para que serve |
| --- | --- |
| `--profile`, `--offering`, `--plan` | obrigatórias: o perfil, o código do oferecimento e o plano de disparos (TOML), que tem de ser do mesmo oferecimento |
| `--json` | o relatório em JSON, em vez de Markdown |
| `--out <arquivo>` | grava o relatório no arquivo, em vez da saída padrão |
| `--agent-token-id <uuid>` | conta as tarefas feitas com o token do agente: as com sucesso e as recusas de revelação de identidade |

O token do perfil precisa das permissões `offerings:read` (os disparos e os relatórios) e
`events:read` (o log de eventos). O código de saída é 0 com o relatório, 1 numa falha (da API, do perfil ou do plano)
e 2 com argumentos inválidos.

Contam só os disparos abertos ou fechados que casam uma vaga do plano (pelo repositório, o commit, a questão e o
horário de abertura). Os abertos ou fechados que não casam com o plano ficam de fora, e a saída de erro diz quantos;
os de outros estados não contam. O relatório traz, para cada disparo:

- o alvo, as entregas e os bloqueios, com as taxas de entrega e de bloqueio (a de bloqueio, no Markdown, só no
  total);
- as respostas no prazo e as atrasadas, o "não sei" e a falta de resposta, com as taxas de resposta (só no prazo, e
  com as atrasadas);
- a mediana e os quartis do tempo até responder;
- os erros de formato por tipo de campo e os casos de vírgula decimal;
- as respostas com nota sem erro interno, as falhas de questão e as correções pendentes ao fim.

No total do oferecimento, saem as mesmas somas e taxas, com estas diferenças:

- o tempo até responder sai como a faixa das medianas dos disparos (a menor e a maior), e não como mediana e quartis;
- só no total: o tempo do convite ao vínculo, em horas (mediana, quartis e o tamanho da amostra), e os reenvios de
  convite;
- só no total, e só com `--agent-token-id`: as tarefas do agente.

Algumas métricas o script não mede, e o relatório as marca como manuais: as que vêm do
[diário do piloto](piloto/diario.md) (incidentes, tempo de recuperação, teste de restauração, confirmações pedidas e
a ausência de vazamento) e as recusas do termo, que a leitura da API não expõe. O formato do plano está no
[plano de disparos](piloto/plano-de-disparos.md).

Para uma olhada rápida sem o script, `cq students summary <código>` mostra a adesão dos alunos (só contagens por
estado), e `cq reports show <código> <disparo>` mostra o relatório de um disparo.
