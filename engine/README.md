# Motor de questões (engine)

O motor executa questões PrairieLearn com a mesma semântica do PL e expõe três operações sem estado:
**inspecionar**, **criar variante** e **corrigir**. A variante sai numa representação intermediária
(IR) independente de canal, que o núcleo usa para entregar a questão pelo Telegram. O contrato está na
seção [Contrato](#contrato), abaixo.

O motor roda **dentro da imagem oficial do PL**, fixada por hash, e reutiliza o código dele: os
elementos e o código Python da questão, sem alteração, e as peças de orquestração `CodeCallerNative`,
`markdown.processQuestion` e `mustache`, por fidelidade ao PL. A única divergência deliberada é
a vírgula decimal, tratada fora do motor, no núcleo.

## Contrato

- **HTTP** (`src/main.ts`): `GET /v1/health`, `POST /v1/inspect`, `POST /v1/variants`,
  `POST /v1/grade`. Corpos e respostas em
  [`contracts/engine/v1/engine-api.schema.json`](../contracts/engine/v1/engine-api.schema.json); a IR
  em [`ir.schema.json`](../contracts/engine/v1/ir.schema.json).
- **Linha de comando em lote** (`src/cli.ts`): lê `{"requests": [...]}` da stdin e escreve
  `{"results": [...]}` na stdout; é o que a suíte de conformidade usa com `--network none`.
- Semântica, estados, HTML canônico e hash: as descrições dos dois esquemas e os comentários do código
  (`src/engine.ts`, `src/ir/canonical-html.ts` e `src/core/content-hash.ts`).

## Implantação: só pelo Compose

A **única** implantação do motor é o serviço `engine` do [`compose.yaml`](../compose.yaml) da raiz:
só a rede interna `engine` (sem saída e sem o gateway do host), sem porta publicada,
sistema de arquivos só de leitura com `/tmp` em memória, usuário 65534, sem capacidades, sem novos
privilégios, limites de processos, memória e CPU, o volume `repos` só para leitura, sem segredos e com
reinício automático. O teste de contrato `tests/contract/test_engine_isolation.py` falha se qualquer uma
dessas restrições sumir.

Os comandos `docker run` deste README são **só de desenvolvimento local**: servem para construir,
testar e experimentar a imagem na própria
máquina, com questões conhecidas, e nunca para rodar questões de terceiros num servidor.
A suíte da imagem (`--network none`) e o oráculo da conformidade são testes do motor.

## Construir, testar e rodar (só desenvolvimento local)

Comandos para o Git Bash no Windows (no Linux, dispense o `MSYS_NO_PATHCONV=1`), a partir da raiz:

```bash
docker build -f engine/Dockerfile -t canal-questoes/engine:dev .
docker build -f engine/test/Dockerfile -t canal-questoes/engine:test .
MSYS_NO_PATHCONV=1 docker run --rm --network none --user 65534:65534 --read-only --tmpfs /tmp canal-questoes/engine:test
```

A última linha roda os testes de unidade, integração e contrato com o motor restrito. A pilha do
[início rápido](../docs/operacao/inicio-rapido.md) sobe o motor junto, pelo serviço `engine` do Compose.

## Conformidade contra o PL

Com o oráculo (um PL local em modo de desenvolvimento) no ar em `127.0.0.1:3000`:

```bash
node --test "engine/conformance/*.test.ts"
```

A suíte compara, questão a questão, as variantes e a correção de uma mesma resposta crua no motor e no
PL, e falha com relatório em `engine/conformance/resultados/` se houver divergência. Para checar que a
própria suíte detecta diferenças, rode também com `CQ_CONFORMANCE_INJECT=score`.

## Configuração

| Variável | Padrão | Uso |
| --- | --- | --- |
| `PL_COMMIT` | definido no `Dockerfile` | Commit do PL da imagem (40 hexadecimais); vai para a saúde e para o snapshot |
| `ENGINE_REPOS_ROOT` | `/repos` | Área dos repositórios de questões, em `<sha256(repo)>/<commit>/`, montada somente para leitura |
| `ENGINE_WORKERS` | nº de CPUs | Executores Python simultâneos; os pedidos excedentes esperam em fila |
| `ENGINE_QUESTION_TIMEOUT_MS` | `10000` (10 s) | Tempo máximo de cada chamada do código da questão |
| `ENGINE_HOST`, `ENGINE_PORT` | `0.0.0.0`, `8080` | Escuta da API HTTP |
| `ENGINE_CONTRACTS_DIR` | `contracts/engine/v1` | Esquemas usados para validar pedidos e respostas |

## Restrições de execução

O motor executa código de terceiros, inclusive gerado por LLM. Por isso ele roda **sem rede**, como
**usuário sem privilégios** e com o **sistema de arquivos somente leitura** (`--network none --user
65534:65534 --read-only --tmpfs /tmp`). Não guarda estado entre pedidos. O registro (stdout na API,
stderr na CLI) é uma linha JSON por pedido, sem respostas cruas nem identidade (regra de
privacidade do projeto).

**O código da questão roda com o mesmo usuário (65534) do Node e dos outros executores**.
Ele pode, portanto:
- escrever no log do contêiner e derrubar o motor (o Compose o reinicia, e o worker trata a queda como
  falha repetível);
- ler todo o `/repos` (questões de outros oferecimentos), o `/proc` dos pedidos simultâneos (com as
  respostas cruas de outros alunos) e o `/tmp` comum, e devolver isso pelos canais legítimos do motor
  (enunciado, feedback, erros de formato).

Por isso os **logs do contêiner do motor são sensíveis**: têm rotação curta no Compose (1 MiB × 2) e não
são coletados por nenhuma saída do sistema. A rede interna fecha só a exfiltração pela rede e pelo DNS.
Esses riscos residuais são aceitos no TCC **só com questões de autores confiáveis**, pela lista de hosts
permitidos (`CQ_REPO_ALLOWED_HOSTS`). **Questões de autores não confiáveis exigem um usuário
separado para o código da questão** (trabalho futuro, que muda o isolamento do motor). Esta exigência
fica declarada aqui e no cabeçalho do `compose.yaml`. Os limites de memória e CPU do motor na VM estão
no [guia de implantação](../docs/operacao/implantacao-vm.md),
seção 8.

## Trocar a versão do PL

1. Troque o `FROM` do `engine/Dockerfile` pela nova tag **com** o digest (`@sha256:…`) e o `PL_COMMIT`.
2. Atualize os hashes de proveniência que os testes conferem (`engine/test/unit/pl-provenance.test.ts`
   e `engine/conformance/provenance.test.ts`); como isso muda os próprios testes, a mudança pede
   uma revisão à parte.
3. Suba o oráculo na mesma versão e rode a suíte da imagem restrita e a conformidade. A troca só vale
   com **0 divergências** (fidelidade ao PL).
