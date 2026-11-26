# Referência

Esta parte da documentação descreve as interfaces do canal-questoes como elas são hoje: os comandos do `cq`, a API
HTTP, as ferramentas do servidor MCP e os tipos de questão do PrairieLearn que o sistema aceita. Ela foi escrita à mão
a partir dos contratos versionados em [`contracts/`](../../contracts/), e um teste automatizado do repositório confere
que toda operação do catálogo, todo comando `cq` do catálogo, toda ferramenta do MCP, todo comando local e todo
elemento de resposta suportado aparecem nestas páginas.

Para aprender a usar o sistema desde o começo, siga o [tutorial](../tutorial/primeiro-uso.md). O
[índice da documentação](../README.md) mostra as outras partes.

## Páginas

| Página | O que tem |
| --- | --- |
| [CLI (`cq`)](cli.md) | como rodar o `cq`; as opções globais; o arquivo de perfis e as variáveis de ambiente do cliente; os códigos de saída; cada comando do catálogo e cada comando local |
| [API HTTP](api.md) | o endereço, a autenticação por token e as permissões; o envelope de erro; a paginação; a tabela das 41 operações, com o método, a rota, a permissão e os esquemas; o `/openapi.json` |
| [Servidor MCP](mcp.md) | como o `cq mcp` serve as ferramentas a um agente; as 38 ferramentas; as que pedem a aprovação do operador; as 8 operações que ficam fora do MCP e por quê |
| [Tipos de questão](tipos-de-questao.md) | as questões do PrairieLearn aceitas; os 5 elementos de resposta e os elementos auxiliares; as recusas da inspeção; a vírgula decimal |

## Uma operação, três portas de entrada

O sistema tem um catálogo único de operações, [`catalog.json`](../../contracts/api/v1/catalog.json), com 41
operações. Cada operação tem:

- um identificador, como `offerings.create`;
- uma rota HTTP e a permissão que o token precisa ter para chamá-la;
- um comando do `cq`, como `cq offerings create`;
- uma ferramenta do servidor MCP, em 33 das 41. As outras 8 ficam fora do MCP de propósito: passariam ao modelo um
  segredo, uma credencial, uma identidade ou texto livre de aluno; são a gestão de tokens, que fica fora do agente (só
  no CLI e na API); ou são um diagnóstico, que no MCP fica com a `run_doctor`. A
  [página do MCP](mcp.md#operações-fora-do-mcp) diz o motivo de cada uma.

Além do catálogo, o `cq` tem cinco comandos locais, que não são operações do catálogo: `cq up`, `cq down`, `cq status`
e `cq logs` operam a pilha Docker Compose da sua máquina, e `cq mcp` inicia o servidor MCP. Da API, o `cq up` e o
`cq status` só consultam a saúde. Os quatro primeiros também viram ferramentas do MCP (cinco, porque a parada da pilha
tem duas fases).

## Onde ficam os contratos

| Caminho | Conteúdo |
| --- | --- |
| [`contracts/api/v1/catalog.json`](../../contracts/api/v1/catalog.json) | o catálogo: para cada operação, o identificador, o método, a rota, a permissão, o comando do `cq`, a exposição no MCP e se ela é destrutiva |
| [`contracts/api/v1/schemas/`](../../contracts/api/v1/schemas/) | os esquemas JSON dos pedidos e das respostas de cada operação e o do envelope de erro |
| [`contracts/cli/v1/schemas/`](../../contracts/cli/v1/schemas/) | as saídas `--json` de `cq doctor`, `cq down`, `cq logs`, `cq status` e `cq up` |
| [`contracts/mcp/v1/tools.json`](../../contracts/mcp/v1/tools.json) | as ferramentas do MCP: o nome, a origem (a operação do catálogo ou o comando local), o título, a descrição, as anotações, a marca de aprovação e os caminhos dos esquemas de entrada e de saída |
| [`contracts/mcp/v1/schemas/`](../../contracts/mcp/v1/schemas/) | os parâmetros de cada ferramenta e as saídas da `prepare_stack_down` e da `stack_logs` (as outras ferramentas locais usam as saídas de `contracts/cli/v1/schemas/`) |
| [`contracts/engine/v1/`](../../contracts/engine/v1/) | a API interna do motor de questões e a representação da questão que o motor entrega ao resto do sistema |

A especificação OpenAPI da API não é um arquivo do repositório: a própria API a publica em `/openapi.json`, sem token
(veja a [página da API](api.md#a-especificação-openapi)).

## Termos usados nestas páginas

- **Oferecimento**: uma disciplina num período, com a sua turma e o seu bot do Telegram. Nos comandos de
  oferecimento, `REF` é o código do oferecimento (de 3 a 40 caracteres: letras minúsculas, dígitos e hífen, começando
  por letra ou dígito) ou o identificador dele; nos de pessoa (`cq people show`, `cq identity reveal` e a opção
  `--person`), `REF` é o apelido ou o pseudônimo da pessoa (veja abaixo).
- **Pseudônimo**: o identificador aleatório de uma pessoa, sorteado no cadastro e nunca derivado da identidade. O
  **apelido** é um prefixo curto dele (de 4 a 32 caracteres hexadecimais, com ao menos uma letra). Os comandos aceitam
  os dois. A identidade (nome, RA, e-mail) só sai pela revelação, que fica registrada.
- **Disparo**: o envio de uma questão do PrairieLearn aos alunos de um oferecimento, numa janela de tempo. Nos
  comandos, `DISPATCH_ID` é o identificador do disparo.
- **Duas fases**: as operações destrutivas (o arquivamento, o envio dos convites, a confirmação do disparo, o expurgo
  e, no MCP, a parada da pilha) são feitas em dois passos: um preparo, que mostra o efeito e devolve um código de
  confirmação (no disparo, o identificador a confirmar), e uma confirmação, que exige esse código.
