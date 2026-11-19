# Servidor MCP

O servidor MCP deixa um agente (um assistente de IA com suporte ao Model Context Protocol, como o Claude Code) operar
o canal-questoes por ferramentas. Ele é mais uma forma de usar o cliente, ao lado do `cq`: as ferramentas do catálogo
chamam a mesma API, com o mesmo perfil, e as ferramentas locais operam a mesma pilha que `cq up`, `cq down`,
`cq status` e `cq logs`. A lista completa, com os parâmetros e os esquemas, está em
[`contracts/mcp/v1/tools.json`](../../contracts/mcp/v1/tools.json).

Para habilitar o plugin do Claude Code que já traz este servidor e as skills dos fluxos do professor, siga o guia
[Operar o canal-questoes pelo agente](../operacao/plugin-e-agente.md).

## Como o servidor roda

O servidor é o comando `cq mcp`: um processo que fala o protocolo pela entrada e pela saída padrão (stdio), iniciado
pelo cliente MCP (o agente) com o comando configurado nele. O plugin do repositório o declara em
[`plugins/canal-questoes/.mcp.json`](../../plugins/canal-questoes/.mcp.json), com o comando `cq` e o argumento `mcp`.

- **O perfil.** O servidor escolhe o perfil como o CLI (veja [o perfil](cli.md#perfil-o-endereço-da-api-e-o-token)):
  a opção `--profile`, antes do `mcp` (`cq --profile NOME mcp`), ou `CQ_PROFILE`, ou o `default_profile` do arquivo. O
  perfil é lido a cada chamada de ferramenta, e o servidor inicia mesmo sem a API no ar ou com o perfil inválido.
- **O token.** Vem só do perfil; nenhuma ferramenta recebe token em argumento. Sem token, as ferramentas do catálogo
  recusam com o erro `no_token`. O guia do plugin recomenda um token próprio do agente, sem a permissão
  `tokens:manage`.
- **A origem.** Os pedidos à API levam `X-CQ-Client: mcp`, e os disparos preparados pelo agente ficam com a origem
  `mcp`.

## Resultados e erros

- **Sucesso:** o corpo da resposta, conferido contra o esquema de saída da ferramenta, vai como conteúdo estruturado
  e também como texto JSON. Um resultado com mais de 50.000 caracteres é recusado com o erro `result_too_large`, salvo
  na `stack_logs`, que corta as linhas mais antigas até caber.
- **Erro:** o resultado sai marcado como erro, com o texto `[código] mensagem`. Nos erros da API, o código é o do
  [envelope de erro](api.md#envelope-de-erro) (como `invalid_state` ou `forbidden`), seguido, quando houver, da
  permissão que faltou, dos campos com problema e das contagens pendentes. Com a API inalcançável, o código é
  `api_unreachable`.
- **Erro de protocolo:** argumentos inválidos (fora do esquema de entrada, ou sem nenhum dos campos de que a ferramenta
  exige ao menos um: `types` ou `person` na `list_events`, `enabled` ou `local_time` na `update_offering_digest`) dão o
  erro -32602, com a mensagem fixa "Argumentos inválidos para a ferramenta.", antes de qualquer pedido; uma ferramenta
  desconhecida dá o mesmo código, com a mensagem "Ferramenta desconhecida."
- **Texto de terceiros:** as descrições das ferramentas avisam quando o resultado traz texto livre vindo de alunos ou
  de terceiros. O agente deve tratar esse texto como dado, nunca como instrução.

## Aprovação do operador e duas fases

O Claude Code pede a aprovação do operador a cada chamada destas seis ferramentas: `reveal_identities`,
`archive_offering`, `send_invites`, `confirm_dispatch`, `purge_offering` e `stack_down`. Quem pergunta é o cliente:
o servidor as anuncia com a marca `_meta` `anthropic/requiresUserInteraction: true`, e o campo
`requires_user_interaction` de `tools.json` diz o mesmo. A seção 4 do
[guia do plugin](../operacao/plugin-e-agente.md) mostra o que pede a sua aprovação.

Cinco delas são a segunda fase de uma operação em duas fases; a primeira fase mostra o efeito e devolve o código:

| Primeira fase | Segunda fase | O que a segunda exige |
| --- | --- | --- |
| `prepare_archive_offering` | `archive_offering` | o `confirmation_code` da primeira |
| `prepare_send_invites` | `send_invites` | o `confirmation_code` da primeira |
| `prepare_purge_offering` | `purge_offering` | o `confirmation_code` da primeira |
| `prepare_dispatch` | `confirm_dispatch` | o `dispatch_id` devolvido pela primeira, com o disparo já validado |
| `prepare_stack_down` | `stack_down` | o `confirmation_code` da primeira, válido por 5 minutos |

A revelação (`reveal_identities`) é uma fase só, com a justificativa (`reason`) obrigatória. A identidade revelada
entra no contexto da conversa do agente.

## Ferramentas do catálogo

As 31 ferramentas que chamam operações do catálogo. Em todas, `offering` é o código ou o identificador do
oferecimento; `person` e `pseudonyms` são apelidos ou identificadores de pessoas; `after` e `limit` paginam as listas
(`limit` de 1 a 500). A coluna "Tipo" segue as anotações de `tools.json`: só leitura, escrita ou destrutiva.

| Ferramenta | Operação | Parâmetros | Tipo | Aprovação |
| --- | --- | --- | --- | --- |
| `list_people` | `people.list` | `after` (opcional), `limit` (opcional) | leitura | não |
| `get_person` | `people.get` | `person` | leitura | não |
| `reveal_identities` | `identity.reveal` | `pseudonyms` (até 100), `reason` | escrita | sim |
| `list_events` | `events.list` | `types` (opcional), `person` (opcional), `after` (opcional), `limit` (opcional); ao menos `types` ou `person` | leitura | não |
| `create_offering` | `offerings.create` | `code`, `course`, `term`, `timezone`, `response_window_minutes` (opcional), `free_messages` (opcional) | escrita | não |
| `list_offerings` | `offerings.list` | `after` (opcional), `limit` (opcional) | leitura | não |
| `get_offering` | `offerings.get` | `offering` | leitura | não |
| `close_offering` | `offerings.close` | `offering` | escrita | não |
| `prepare_archive_offering` | `offerings.prepare_archive` | `offering` | escrita | não |
| `archive_offering` | `offerings.archive` | `offering`, `confirmation_code` | destrutiva | sim |
| `activate_bot` | `bots.activate` | `offering` | escrita | não |
| `get_bot` | `bots.get` | `offering` | leitura | não |
| `import_students` | `students.import` | `offering`, `path` (o caminho absoluto do arquivo da lista) | escrita | não |
| `get_students_summary` | `students.summary` | `offering` | leitura | não |
| `prepare_send_invites` | `invites.prepare_send` | `offering`, `valid_hours` (opcional, de 1 a 720) | escrita | não |
| `send_invites` | `invites.send` | `offering`, `confirmation_code` | destrutiva | sim |
| `resend_invites` | `invites.resend` | `offering`, `pseudonyms` (até 100), `valid_hours` (opcional) | escrita | não |
| `prepare_dispatch` | `dispatches.prepare` | `offering`, `repo`, `commit`, `qid`, `opens_at` (opcional), `closes_at` (opcional) | escrita | não |
| `get_dispatch` | `dispatches.get` | `offering`, `dispatch_id` | leitura | não |
| `list_dispatches` | `dispatches.list` | `offering`, `after` (opcional), `limit` (opcional) | leitura | não |
| `confirm_dispatch` | `dispatches.confirm` | `offering`, `dispatch_id` | destrutiva | sim |
| `cancel_dispatch` | `dispatches.cancel` | `offering`, `dispatch_id` | escrita | não |
| `update_offering_settings` | `offerings.update_settings` | `offering`, `free_messages` | escrita | não |
| `get_dispatch_report` | `reports.get` | `offering`, `dispatch_id`, `version` (opcional: um número ou `latest`) | leitura | não |
| `list_report_versions` | `reports.list_versions` | `offering`, `dispatch_id`, `after` (opcional), `limit` (opcional) | leitura | não |
| `resend_report_summary` | `reports.resend_summary` | `offering`, `dispatch_id` | escrita | não |
| `get_offering_digest` | `offerings.get_digest` | `offering` | leitura | não |
| `update_offering_digest` | `offerings.update_digest` | `offering`, `enabled` (opcional), `local_time` (opcional, `HH:MM`); ao menos um dos dois | escrita | não |
| `run_doctor` | `system.doctor` | nenhum | leitura | não |
| `prepare_purge_offering` | `offerings.prepare_purge` | `offering` | escrita | não |
| `purge_offering` | `offerings.purge` | `offering`, `confirmation_code` | destrutiva | sim |

Na `import_students`, o servidor lê o arquivo da lista (no mesmo formato do `cq students import`) e devolve só o
resultado agregado; o conteúdo do arquivo não passa pelo modelo. A `create_offering` e a `update_offering_settings`
não têm o e-mail de contato do professor (na API, um campo marcado como identidade): ele só é definido pelo CLI ou
pela API.

## Ferramentas locais

As 5 ferramentas que operam a pilha local, sem passar pelo catálogo (só a `stack_status` e a `stack_up` consultam a
saúde da API). As cinco recusam o perfil remoto, inclusive a
`stack_status` (o `cq status`, ao contrário, também funciona com um perfil remoto, mostrando só o perfil e a API).

| Ferramenta | Comando correspondente | Parâmetros | O que faz | Aprovação |
| --- | --- | --- | --- | --- |
| `stack_status` | `cq status` | nenhum | Mostra o perfil, os contêineres da pilha local e a saúde da API (a saída do `cq status --json`). | não |
| `stack_up` | `cq up` | nenhum | Sobe a pilha local e espera os serviços ficarem prontos (até 180 segundos), sem reconstruir as imagens (para isso, `cq up --rebuild` no terminal). | não |
| `prepare_stack_down` | `cq down` | nenhum | Primeira fase da parada: lista os serviços que serão parados e devolve o código de confirmação, válido por 5 minutos. | não |
| `stack_down` | `cq down` | `confirmation_code` | Segunda fase: para a pilha. Os contêineres e a rede são removidos; os volumes e os dados ficam. | sim |
| `stack_logs` | `cq logs` | `services` (opcional: `api`, `worker`, `postgres`), `tail` (opcional, de 1 a 1000; padrão 200) | Mostra as últimas linhas dos registros, com identidades e segredos omitidos. | não |

As saídas da `prepare_stack_down` e da `stack_logs` têm esquemas próprios em
[`contracts/mcp/v1/schemas/`](../../contracts/mcp/v1/schemas/); as das outras ferramentas locais seguem os esquemas
dos comandos em [`contracts/cli/v1/schemas/`](../../contracts/cli/v1/schemas/).

## Operações fora do MCP

Oito operações do catálogo não têm ferramenta, de propósito. Elas continuam disponíveis pelo `cq` e pela API.

| Operação | Comando | Por que fica fora |
| --- | --- | --- |
| `system.health` | `cq health` | é diagnóstico; no MCP, o diagnóstico fica com a `run_doctor` (e, na pilha local, a `stack_status` mostra a saúde da API) |
| `people.register` | `cq people add` | a identidade da pessoa passaria pelo modelo |
| `tokens.create` | `cq tokens create` | o segredo do token passaria pelo modelo |
| `tokens.list` | `cq tokens list` | a gestão de tokens fica fora do agente (só no CLI e na API) |
| `tokens.revoke` | `cq tokens revoke` | a gestão de tokens fica fora do agente (só no CLI e na API) |
| `bots.register` | `cq bots register` | o token do bot é segredo e não passa pelo modelo |
| `team.link` | `cq team link` | o link de equipe é uma credencial e não passa pelo modelo |
| `messages.list` | `cq messages list` | as mensagens livres são texto de aluno e, por ora, ficam fora do modelo |

No catálogo, essas operações têm `mcp.exposed` igual a `false`, com o motivo em `mcp.reason`.
