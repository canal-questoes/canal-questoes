# canal-questoes — guia para agentes de código

Este guia vale para qualquer agente de código que trabalhe neste repositório (Claude Code, Codex e outros) e para as
pessoas que os conduzem. As regras para quem contribui estão em [`CONTRIBUTING.md`](CONTRIBUTING.md); a visão geral, no
[`README.md`](README.md); o índice da documentação, em [`docs/README.md`](docs/README.md).

## O que é o projeto

O canal-questoes entrega questões do [PrairieLearn](https://www.prairielearn.com/) aos alunos por bots do Telegram, um
bot por oferecimento de disciplina, corrige as respostas com a mesma semântica do PrairieLearn e produz relatórios
descritivos por questão para o professor. O sistema é operado por uma API com CLI (`cq`) e também por um agente, pelo
servidor MCP e pelo plugin do Claude Code. Os alunos aparecem só por pseudônimo: a identidade sai apenas pela
operação de revelação.

## Mapa das pastas

| Pasta | Conteúdo |
| --- | --- |
| `server/` | o servidor em Python: o núcleo, a API, o worker, as portas e os adaptadores (Telegram, e-mail, motor) |
| `client/` | o pacote cliente: o CLI `cq` e o servidor MCP (`cq mcp`) |
| `engine/` | o motor de questões em TypeScript, na imagem oficial do PrairieLearn, com a suíte de conformidade |
| `contracts/` | os contratos versionados: o catálogo da API, o CLI, as ferramentas do MCP e a representação do motor |
| `tests/` | a suíte Python, por tipo: `unit/`, `contract/`, `cli/`, `integration/`, `privacy/` e `system/` (Compose) |
| `plugins/` | o plugin do Claude Code (a configuração do servidor MCP e as skills) e o marketplace local |
| `ops/` | a operação: cópias de segurança, CI, implantação, exportação pública e as métricas do piloto |
| `scripts/` | os scripts das suítes, do Postgres de teste e da conferência dos testes congelados |
| `docs/` | a documentação: tutorial, operação, referência, explicação e diagramas |
| `.github/` | os workflows do CI e os modelos de issue e de pull request |

## Como rodar as suítes

Pré-requisitos: o git, o [`uv`](https://docs.astral.sh/uv/), Docker com Compose e, para o motor, Node.js 22.6 ou
mais novo (no 22 e no 23, exporte `NODE_OPTIONS=--experimental-strip-types`, como no workflow
`.github/workflows/engine.yml`). No Windows, use o WSL 2; com o Python nativo do Windows, exporte `PYTHONUTF8=1`.

```bash
uv sync --locked                       # as dependências fixadas
uv run python scripts/fetch_age.py --platform linux-amd64 --dest <pasta fora do repositório>
export CQ_TEST_AGE_DIR=<a mesma pasta>  # os testes do age falham sem ela (estão na suíte rápida)
bash scripts/test-postgres.sh up       # o Postgres descartável dos testes, em 127.0.0.1:55432
uv run pytest -q tests/unit            # uma parte da suíte Python
uv run python ops/ci/ci_suite.py fast  # a suíte rápida do CI (sem os testes de sistema)
bash scripts/run-suite.sh              # a suíte Python inteira, por diretório, com os testes de sistema
bash scripts/run-engine-suite.sh       # o motor e a conformidade com o PrairieLearn (veja abaixo)
bash scripts/test-postgres.sh down     # derruba o Postgres de teste
```

- Alguns testes usam o binário `age`, e sem `CQ_TEST_AGE_DIR` eles falham (não são pulados). Eles só rodam em x86-64,
  no Linux ou no Windows. O `scripts/fetch_age.py` baixa a versão fixada numa pasta que precisa ficar fora do
  repositório; no Python nativo do Windows, use `--platform windows-amd64`.
- A conformidade compara o motor com o oráculo, um PrairieLearn local em `127.0.0.1:3000` na mesma versão da imagem do
  motor. O script não cria o oráculo: ele inicia um contêiner de nome `pl-oracle` que já precisa existir; o workflow
  `.github/workflows/engine.yml` mostra como criá-lo. Rode a suíte do motor sem outras suítes em paralelo.
- O último passo de `scripts/run-engine-suite.sh` confere a lista de congelamento dos testes do motor, que fica no
  repositório de origem, fora desta cópia. Aqui o passo não tem o que conferir: o script escreve a linha
  "congelamento: sem lista neste repositório", e o resultado vem da suíte na imagem e da conformidade.
- `SYSTEM=0 bash scripts/run-suite.sh` pula os testes de sistema; `CQ_SUITE_OUT` escolhe onde ficam as saídas.

## Os princípios

Os sete princípios do projeto, em uma linha cada. O texto completo, com o porquê de cada um, está em
[`docs/explicacao/principios.md`](docs/explicacao/principios.md).

1. **Conhecimento verificável e rastreável:** toda afirmação relevante é fato com fonte, inferência declarada ou
   decisão registrada; resposta de modelo de linguagem não é fonte.
2. **Testes primeiro, com revisão adversarial independente:** os testes vêm antes do código, passam por revisão
   independente e, aprovados, não mudam para o código passar.
3. **Identidade só sai pela revelação:** pessoas são referenciadas por pseudônimo aleatório; nome, RA e e-mail só saem
   pela operação de revelação, com auditoria, e o identificador do Telegram nunca sai.
4. **Fidelidade ao PrairieLearn:** a correção reproduz a semântica do PrairieLearn, e toda divergência é deliberada,
   registrada e conferida contra o oráculo.
5. **Fronteiras, portas e contratos:** o núcleo não depende de Telegram, PrairieLearn, git, e-mail nem HTTP, e toda
   operação nasce num catálogo único (API, CLI e, quando faz sentido, MCP).
6. **Nenhuma resposta perdida:** a entrada do aluno é gravada antes de qualquer processamento, e as tarefas são
   idempotentes e retomáveis.
7. **Escopo contido:** só entra o que está no escopo; as evoluções ficam como trabalho futuro.

As restrições técnicas (Python com `uv`, TypeScript só no motor, Docker Compose, português do Brasil nos textos)
estão no mesmo documento.

## Regras para quem contribui

- **Não altere um teste existente para o código passar.** Se um teste parecer errado, abra uma issue ou explique na
  pull request; a correção do teste é uma mudança própria, revisada como tal. Os scripts
  `scripts/check-frozen-tests.sh` e `scripts/check-frozen-engine-tests.sh` conferem o congelamento contra listas de
  hash que ficam no repositório de origem, fora desta cópia.
- **Nenhum segredo nem dado pessoal** no código, nos testes, nos commits, nas issues ou nas mensagens: nada de tokens
  de bot, chaves, senhas, `.env` nem dados reais de alunos. Os testes usam identidades fictícias.
- **Commits no formato [Conventional Commits](https://www.conventionalcommits.org/pt-br/v1.0.0/)**, por exemplo
  `feat(cli): ...`, `fix(motor): ...` ou `docs: ...`.
- **Contribuições feitas com IA** seguem a política do [`CONTRIBUTING.md`](CONTRIBUTING.md): declare o uso na pull
  request; quem envia responde pelo que envia; valem as mesmas regras de teste e de revisão; nenhum segredo nem dado
  pessoal vai a um modelo.
- **Idioma:** documentação, mensagens e comentários em português do Brasil; identificadores de código em inglês.
