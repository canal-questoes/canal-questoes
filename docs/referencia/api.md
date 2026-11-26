# API HTTP

A API HTTP é a porta de entrada de todas as operações do catálogo: o `cq` e o servidor MCP são clientes dela. Esta
página resume as regras comuns a todas as operações e lista cada uma. O formato exato de cada pedido e de cada
resposta está nos esquemas versionados e na especificação OpenAPI que a própria API publica; esta página não os copia.

## Endereço

Na pilha local, a API escuta em `http://127.0.0.1:8000`. A porta do host muda com a variável `CQ_API_PORT` do
arquivo `.env` da pilha, e a API só é publicada no endereço local (`127.0.0.1`).

As operações ficam sob `/v1`. Fora dele, só `/health` (a saúde da API) e `/openapi.json` (a especificação). Um caminho
com barra no fim é desconhecido (404), e a API não responde com redirecionamento. Não há `/docs` nem `/redoc`.

## Autenticação

Toda rota sob `/v1` exige o cabeçalho `Authorization: Bearer <segredo>`. `/health` e `/openapi.json` não exigem
token. Sem token, ou com um token inválido ou revogado, a resposta é 401.

Há dois tipos de token:

- o **token inicial**, o valor de `CQ_ADMIN_TOKEN` no `.env` da pilha (opcional; com pelo menos 32 caracteres): não
  fica no banco e tem todas as permissões. Serve para criar os outros tokens;
- os **tokens criados** pela operação `tokens.create` (no CLI, `cq tokens create`), cada um com um rótulo e uma lista
  de permissões. Um token só pode conceder permissões que ele mesmo tem. O segredo começa por `cq_` e só aparece na
  resposta da criação: o banco guarda só um resumo SHA-256 dele. A revogação (`tokens.revoke`) é definitiva.

### Permissões

| Permissão | O que libera |
| --- | --- |
| `people:read` | consultar pessoas, só pelos pseudônimos |
| `people:write` | cadastrar pessoas, importar a lista da turma e as duas fases do expurgo |
| `identity:reveal` | revelar a identidade por trás de um pseudônimo |
| `events:read` | ler o log de eventos |
| `offerings:read` | consultar oferecimentos, bots, alunos, disparos, mensagens, relatórios, o resumo diário, o diagnóstico e os bancos de questões enviados |
| `offerings:write` | criar, configurar, encerrar e arquivar oferecimentos; links de equipe; convites; disparos; reenvio do resumo do relatório; envio de bancos de questões |
| `bots:manage` | registrar e ativar o bot do oferecimento |
| `tokens:manage` | criar, listar e revogar tokens |

A tabela das operações, abaixo, diz a permissão de cada uma. Sem a permissão, a resposta é 403, com o nome da
permissão que faltou. A revelação é a única operação que registra a tentativa negada no log de eventos.

## O cabeçalho `X-CQ-Client`

Opcional. Declara o cliente que fez o pedido: `cli`, `mcp` ou `other` (o padrão, quando ausente). O `cq` manda `cli`,
e o servidor MCP manda `mcp`. Um valor fora desses três é recusado com 400.

## Ordem das verificações

Depois da autenticação e do roteamento (401, 404 e 405), cada rota sob `/v1` confere, nesta ordem:

1. o pedido inteiro (o cabeçalho `X-CQ-Client`, o caminho, a consulta e o corpo): 400, com todos os campos com problema
   de uma vez;
2. a permissão do token: 403;
3. as regras da operação (o recurso existe, o estado permite): 404 ou 409 (e, nas operações que falam com o
   Telegram, 503, ou 400 para o token de bot recusado).

A exceção é o envio de um banco de questões (`banks.upload`), cujo corpo pode ter centenas de MB: ele confere o
cabeçalho `X-CQ-Client` e a consulta, depois a permissão (403, sem ler o corpo) e só então o corpo (veja
[o envio de um banco de questões](#o-envio-de-um-banco-de-questões)).

## Envelope de erro

Todo erro sai no mesmo envelope JSON, descrito em
[`error.schema.json`](../../contracts/api/v1/schemas/error.schema.json):

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Pedido inválido.",
    "fields": [{"field": "code", "reason": "invalid_format"}]
  }
}
```

- `code` e `message` sempre vêm; cada código tem uma mensagem fixa, em português, que nunca repete o valor enviado;
- `fields` lista os campos com problema e o motivo de cada um (`missing`, `unknown_field`, `invalid_format`,
  `too_short`, `too_long`, `too_few`, `too_many`, `out_of_range`, `invalid_value`, `duplicate`, `too_large`,
  `path_not_allowed`, `entry_not_allowed`, `not_a_course` ou `mismatch`; os cinco últimos só aparecem no envio de
  um banco de questões, descritos [na seção dele](#o-envio-de-um-banco-de-questões));
- `permission` diz a permissão que faltou (no 403);
- `state` diz o estado atual do oferecimento (no `invalid_state`): `draft`, `active`, `closed` ou `archived`;
- `pending` traz as contagens do trabalho pendente (no `purge_pending`).

| Código | HTTP | Quando |
| --- | --- | --- |
| `invalid_request` | 400 | o pedido está fora do formato |
| `unauthenticated` | 401 | token ausente, inválido ou revogado |
| `forbidden` | 403 | o token não tem a permissão |
| `not_found` | 404 | o recurso não existe |
| `method_not_allowed` | 405 | o método não vale para a rota |
| `conflict` | 409 | já existe uma pessoa com esse RA ou e-mail |
| `already_exists` | 409 | já existe um oferecimento com esse código |
| `invalid_state` | 409 | a operação não é permitida no estado atual do oferecimento |
| `bot_conflict` | 409 | esse bot não pode ser usado neste oferecimento |
| `bot_required` | 409 | o oferecimento não tem um bot registrado com acesso |
| `invalid_confirmation` | 409 | o código de confirmação é inválido ou venceu |
| `list_conflict` | 409 | há linhas da lista em conflito com pessoas cadastradas ou com a equipe; nada foi gravado |
| `role_conflict` | 409 | a mesma pessoa não pode ser equipe e aluno do mesmo oferecimento |
| `invalid_dispatch_state` | 409 | a operação não é permitida no estado atual do disparo |
| `window_expired` | 409 | a janela do disparo não é mais válida; é preciso preparar outro |
| `grading_pending` | 409 | há respostas aguardando correção ou mensagens ainda não processadas no oferecimento |
| `purge_pending` | 409 | há trabalho pendente no oferecimento; o expurgo espera ele terminar |
| `purge_changed` | 409 | as contagens do expurgo mudaram desde o preparo; é preciso preparar de novo |
| `internal` | 500 | erro interno |
| `unavailable` | 503 | o Telegram não respondeu |
| `not_configured` | 503 | o servidor não tem a chave-mestra dos bots (`CQ_MASTER_KEY`) configurada |

## Listas e paginação

As listas paginadas (`people.list`, `events.list`, `offerings.list`, `dispatches.list`, `messages.list`,
`reports.list_versions` e `banks.list`) aceitam, na consulta, `after` (o cursor: só os itens depois dele; padrão 0) e `limit` (de 1 a
500; padrão 100). A resposta traz `items` e `next_cursor`, o valor a passar em `after` para buscar a página seguinte:
o cursor do último item da página ou, numa página sem itens, o próprio `after` recebido, o que marca o fim da lista.
No esquema da resposta de `reports.list_versions`, `next_cursor` também admite nulo. O `tokens.list` não é paginado:
não aceita `after` nem `limit`, e a resposta traz só `items`.

## Horários

Nos pedidos, os horários (a abertura e o fechamento de um disparo) vão em ISO 8601 com o deslocamento explícito, como
`2031-01-02T09:00:00-03:00`; um horário sem fuso é recusado. Nas respostas, os horários saem em UTC, terminados em
`Z`.

## Operações

`{ref}` é o código ou o identificador do oferecimento (nas rotas de pessoas, o apelido ou o identificador da pessoa);
`{dispatch_id}` é o identificador do disparo; `{token_id}` é o identificador do token. A coluna "Esquemas" diz quais
arquivos a operação tem em [`contracts/api/v1/schemas/`](../../contracts/api/v1/schemas/): o do pedido se chama
`<operação>.request.schema.json`, e o da resposta, `<operação>.response.schema.json` (por exemplo,
`offerings.create.request.schema.json`). O comando do `cq` de cada operação está na [página do CLI](cli.md).

| Operação | Método e rota | Permissão | Esquemas |
| --- | --- | --- | --- |
| `system.health` | `GET /health` | nenhuma | resposta |
| `people.register` | `POST /v1/people` | `people:write` | pedido e resposta |
| `people.list` | `GET /v1/people` | `people:read` | resposta |
| `people.get` | `GET /v1/people/{ref}` | `people:read` | resposta |
| `identity.reveal` | `POST /v1/identity/reveal` | `identity:reveal` | pedido e resposta |
| `tokens.create` | `POST /v1/tokens` | `tokens:manage` | pedido e resposta |
| `tokens.list` | `GET /v1/tokens` | `tokens:manage` | resposta |
| `tokens.revoke` | `POST /v1/tokens/{token_id}/revoke` | `tokens:manage` | resposta |
| `events.list` | `GET /v1/events` | `events:read` | resposta |
| `offerings.create` | `POST /v1/offerings` | `offerings:write` | pedido e resposta |
| `offerings.list` | `GET /v1/offerings` | `offerings:read` | resposta |
| `offerings.get` | `GET /v1/offerings/{ref}` | `offerings:read` | resposta |
| `offerings.close` | `POST /v1/offerings/{ref}/close` | `offerings:write` | resposta |
| `offerings.prepare_archive` | `POST /v1/offerings/{ref}/archive/prepare` | `offerings:write` | resposta |
| `offerings.archive` | `POST /v1/offerings/{ref}/archive` | `offerings:write` | pedido e resposta |
| `bots.register` | `PUT /v1/offerings/{ref}/bot` | `bots:manage` | pedido e resposta |
| `bots.activate` | `POST /v1/offerings/{ref}/bot/activate` | `bots:manage` | resposta |
| `bots.get` | `GET /v1/offerings/{ref}/bot` | `offerings:read` | resposta |
| `team.link` | `POST /v1/offerings/{ref}/team-links` | `offerings:write` | pedido e resposta |
| `students.import` | `POST /v1/offerings/{ref}/students` | `people:write` | pedido e resposta |
| `students.summary` | `GET /v1/offerings/{ref}/students/summary` | `offerings:read` | resposta |
| `invites.prepare_send` | `POST /v1/offerings/{ref}/invites/prepare` | `offerings:write` | pedido e resposta |
| `invites.send` | `POST /v1/offerings/{ref}/invites` | `offerings:write` | pedido e resposta |
| `invites.resend` | `POST /v1/offerings/{ref}/invites/resend` | `offerings:write` | pedido e resposta |
| `dispatches.prepare` | `POST /v1/offerings/{ref}/dispatches` | `offerings:write` | pedido e resposta |
| `dispatches.get` | `GET /v1/offerings/{ref}/dispatches/{dispatch_id}` | `offerings:read` | resposta |
| `dispatches.list` | `GET /v1/offerings/{ref}/dispatches` | `offerings:read` | resposta |
| `dispatches.confirm` | `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/confirm` | `offerings:write` | resposta |
| `dispatches.cancel` | `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/cancel` | `offerings:write` | resposta |
| `messages.list` | `GET /v1/offerings/{ref}/messages` | `offerings:read` | resposta |
| `offerings.update_settings` | `PATCH /v1/offerings/{ref}/settings` | `offerings:write` | pedido e resposta |
| `reports.get` | `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report` | `offerings:read` | resposta |
| `reports.list_versions` | `GET /v1/offerings/{ref}/dispatches/{dispatch_id}/report/versions` | `offerings:read` | resposta |
| `reports.resend_summary` | `POST /v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend` | `offerings:write` | resposta |
| `offerings.get_digest` | `GET /v1/offerings/{ref}/digest` | `offerings:read` | resposta |
| `offerings.update_digest` | `PATCH /v1/offerings/{ref}/digest` | `offerings:write` | pedido e resposta |
| `system.doctor` | `GET /v1/system/doctor` | `offerings:read` | resposta |
| `offerings.prepare_purge` | `POST /v1/offerings/{ref}/purge/prepare` | `people:write` | resposta |
| `offerings.purge` | `POST /v1/offerings/{ref}/purge` | `people:write` | pedido e resposta |
| `banks.upload` | `POST /v1/question-banks` | `offerings:write` | pedido e resposta |
| `banks.list` | `GET /v1/question-banks` | `offerings:read` | resposta |

Algumas consultas próprias:

- `events.list`: `types` (repetível: um ou mais tipos de evento) e `person` (o apelido ou o identificador da pessoa);
  ao menos um dos dois é obrigatório;
- `reports.get`: `format` (`json`, o padrão, ou `csv`) e `version` (um número ou `latest`; sem ela, o relatório é
  calculado na hora). No formato CSV, os erros continuam saindo no envelope JSON;
- `banks.list`: `name` (opcional, um só: o nome exato do banco, no mesmo padrão do envio; fora dele,
  `invalid_format`). A lista traz uma linha por versão enviada, em ordem do primeiro envio.

## O envio de um banco de questões

`banks.upload` recebe um banco de questões local, uma pasta de curso do PrairieLearn empacotada pelo cliente, e o
guarda para os disparos. O `cq banks upload` e a ferramenta `upload_question_bank` do MCP leem a pasta, conferem as
regras, calculam a versão e montam o pacote antes do pedido; o guia
[Banco de questões local](../operacao/banco-local.md) trata do uso.

O corpo é JSON, com no máximo 188743680 bytes (180 MiB), conferido pelo `Content-Length` antes da leitura e pelo
contador durante ela (acima do teto, `body` com `too_long`; um `Content-Length` repetido ou não numérico, `body` com
`invalid_format`). A API trata um envio por vez; o segundo espera. Os campos:

| Campo | O que é |
| --- | --- |
| `name` | o nome do banco: de 3 a 40 caracteres, letras minúsculas, dígitos e hífen, começando por letra ou dígito |
| `version` | a versão declarada: o SHA-256 do conteúdo, em 64 dígitos hexadecimais minúsculos |
| `source` | a origem: `folder` (a pasta como está) ou `git` (um commit de um repositório git local) |
| `git_commit` | o commit de origem (40 ou 64 dígitos hexadecimais): obrigatório com `git`; ausente ou nulo com `folder` |
| `archive` | o pacote `.tar.gz` em base64 padrão, com preenchimento; decodificado, no máximo 134217728 bytes (128 MiB) |

A API confere o pacote inteiro: um `.tar.gz` só com arquivos regulares, de até 20000 arquivos e 100 MiB de conteúdo,
com caminhos relativos e o `infoCourse.json` na raiz. Depois recalcula a versão pelo conteúdo recebido. Cada recusa
sai como `invalid_request`, com um único campo:

| Campo e motivo | Quando |
| --- | --- |
| `archive` com `invalid_format` | o base64 é inválido ou o pacote está malformado |
| `archive` com `too_long` | o pacote decodificado passa de 128 MiB |
| `archive` com `too_large` | o conteúdo passa de 100 MiB (ou o pacote descomprimido, ou um cabeçalho estendido, passa do teto) |
| `archive` com `too_many` | o pacote passa de 20000 arquivos |
| `archive` com `entry_not_allowed` | um membro do pacote não é um arquivo regular (link, diretório, dispositivo), ou um cabeçalho estendido tem uma chave fora da lista aceita |
| `archive` com `path_not_allowed` | um caminho está fora da regra: absoluto, com um componente vazio, `.`, `..` ou `.git` (em qualquer caixa), com barra invertida ou caractere de controle, fora de UTF-8, longo demais (mais de 255 bytes num componente ou 4096 no caminho), ou um arquivo com o nome de uma pasta |
| `archive` com `duplicate` | o mesmo caminho aparece duas vezes |
| `archive` com `not_a_course` | falta o `infoCourse.json` na raiz do banco |
| `version` com `mismatch` | a versão recalculada pelo conteúdo difere da declarada |

Os demais campos seguem as regras de sempre: `missing`, `invalid_format`, `too_short` e `too_long` no `name`;
`missing` e `invalid_format` na `version`; `missing` e `invalid_value` na `source`; no `git_commit`, `missing` com a
origem `git`, `invalid_format` fora da forma e `invalid_value` com a origem `folder` e um valor não nulo.

O sucesso é sempre 200. A resposta traz o nome, a versão, a origem, o commit git (ou nulo), as contagens (arquivos,
bytes e questões), o horário do primeiro envio, `repo` (a referência `bank:<nome>` a usar no preparo do disparo) e
`created`: `true` no primeiro envio desse nome com essa versão; `false` num reenvio, que devolve o primeiro envio
(a origem e o commit dele) sem gravar nada de novo. Uma versão enviada não muda depois.

O primeiro envio registra o evento `bank.uploaded` no log de eventos, com o nome, a versão, a origem, o commit e as
contagens.

No `dispatches.prepare`, o `repo` pode ser essa referência `bank:<nome>`, com a versão do banco (64 dígitos
hexadecimais) no `commit`; ela não passa pela lista de hosts permitidos. Uma referência `bank:` fora do padrão do
nome é `invalid_format`. O preparo não confere se o banco existe: quem confere é a validação do disparo, no worker.

## Duas fases

As operações destrutivas da API são feitas em duas fases:

| Preparo | Confirmação | O que a confirmação exige |
| --- | --- | --- |
| `offerings.prepare_archive` | `offerings.archive` | o código de confirmação devolvido pelo preparo |
| `invites.prepare_send` | `invites.send` | o código de confirmação devolvido pelo preparo |
| `offerings.prepare_purge` | `offerings.purge` | o código de confirmação devolvido pelo preparo; se as contagens mudaram, `purge_changed` |
| `dispatches.prepare` | `dispatches.confirm` | o disparo validado, pelo `dispatch_id` |

Nas três primeiras, um preparo novo troca o código anterior, e um código vencido ou errado dá
`invalid_confirmation`.

## Marcas nos esquemas

Os esquemas marcam, por tipo de campo, o que exige cuidado:

- `x-identity`: um dado de identidade (nome, RA ou e-mail): no cadastro de pessoas, na importação da lista, na
  resposta da revelação e no e-mail de contato do professor, na criação e na configuração do oferecimento;
- `x-secret`: um segredo (o do token criado, o token do bot no registro, o link de equipe);
- `x-free-text`: texto livre;
- `x-untrusted`: texto livre que vem de terceiros ou de alunos (sempre junto de `x-free-text`), a ser tratado como
  dado, nunca como instrução.

## A especificação OpenAPI

Com a pilha no ar, a API publica a especificação OpenAPI 3.1 em `GET /openapi.json`, sem token. Na pilha local:

```bash
curl http://127.0.0.1:8000/openapi.json
```

Ela traz os parâmetros e os corpos de cada operação, com as mesmas marcas dos esquemas, e é gerada pela própria API
que está rodando; por isso ela é a referência exata da versão que você tem.
