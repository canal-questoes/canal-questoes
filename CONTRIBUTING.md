# Como contribuir com o canal-questoes

Obrigado pelo interesse! Este guia explica como preparar o ambiente, rodar os testes e enviar uma mudança. Ele vale
para quem escreve à mão e para quem trabalha com agentes de código; o [`AGENTS.md`](AGENTS.md) resume as mesmas regras
para os agentes.

- **Idioma:** o projeto escreve em português do Brasil (documentação, mensagens e comentários), com os identificadores
  de código em inglês. Relatos em inglês também são aceitos. *Issues, discussions and pull requests in English are
  welcome.*
- **Conduta:** quem participa segue o [código de conduta](CODE_OF_CONDUCT.md).
- **Dúvidas:** o [`SUPPORT.md`](SUPPORT.md) diz onde perguntar.
- **Vulnerabilidades** não vão em issue pública: siga a [política de segurança](SECURITY.md).

## Antes de começar

- Para relatar um defeito ou propor uma melhoria, abra uma issue pelos formulários do repositório. Para uma mudança
  grande, combine a ideia numa issue antes de escrever o código.
- O escopo é contido de propósito: o que fica para depois está nos próximos passos do [`ROADMAP.md`](ROADMAP.md).
- Para conhecer o sistema, comece pelo [índice da documentação](docs/README.md). O
  [tutorial](docs/tutorial/primeiro-uso.md) leva da pilha local a um relatório: ele pede um bot do Telegram criado
  por você e uma conta de e-mail sua, com envio por SMTP; o aluno é você mesmo, com nome e RA fictícios e o seu
  e-mail.

## Ambiente

Pré-requisitos:

- git e o [`uv`](https://docs.astral.sh/uv/), que instala o Python 3.12 usado pelo projeto;
- Docker com Compose, nas versões que o [tutorial](docs/tutorial/primeiro-uso.md) pede, para o Postgres dos testes,
  os testes de sistema, a suíte do motor e a pilha local;
- Node.js 22.6 ou mais novo, o primeiro que lê TypeScript, para a conformidade do motor de questões; com o Node 22
  ou 23, exporte `NODE_OPTIONS=--experimental-strip-types`, como faz o workflow
  [`engine.yml`](.github/workflows/engine.yml);
- no Windows, trabalhe dentro do WSL 2, para o Docker e as suítes; se rodar algo com o Python nativo do Windows,
  exporte `PYTHONUTF8=1` antes.

```bash
git clone <endereço do seu fork>
cd canal-questoes
uv sync --locked            # as dependências fixadas no uv.lock, com as de desenvolvimento
uv tool install ./client    # opcional: o comando cq, para operar uma pilha local
```

O [guia de início rápido](docs/operacao/inicio-rapido.md) e o tutorial explicam a pilha local, o `.env` (criado a
partir do `.env.example`) e o arquivo de perfil do `cq`. O `.env` e o arquivo de perfil guardam segredos: ficam fora
do git e fora de qualquer mensagem.

## Testes

```bash
uv run python scripts/fetch_age.py --platform linux-amd64 --dest <pasta fora do repositório>
export CQ_TEST_AGE_DIR=<a mesma pasta>  # os testes do age falham sem ela (estão na suíte rápida)
bash scripts/test-postgres.sh up       # o Postgres descartável dos testes, em 127.0.0.1:55432
uv run pytest -q tests/unit            # uma parte da suíte Python
uv run python ops/ci/ci_suite.py fast  # a suíte rápida do CI (sem os testes de sistema)
docker pull "$(awk 'toupper($1) == "FROM" { print $2; exit }' engine/Dockerfile)"  # a imagem base do PL
bash scripts/run-suite.sh              # a suíte Python inteira, por diretório, com os testes de sistema
bash scripts/run-engine-suite.sh       # o motor e a conformidade com o PrairieLearn
bash scripts/test-postgres.sh down     # derruba o Postgres de teste
uv run ruff check server/src client/src ops scripts   # o lint, com a versão do ruff fixada no projeto
```

- A suíte Python fica em `tests/`: `unit/`, `contract/`, `cli/`, `integration/`, `privacy/` (o teste canário, que
  confere que a identidade dos alunos não vaza) e `system/` (sobe o Docker Compose de verdade).
- `SYSTEM=0 bash scripts/run-suite.sh` pula os testes de sistema; `CQ_SUITE_WORKERS` muda o número de processos
  (padrão: 4) e `CQ_SUITE_OUT`, a pasta das saídas.
- Os testes de sistema do motor (`tests/system/test_engine_system.py`) exigem a imagem base do PrairieLearn já no
  host e não a baixam: sem ela, eles falham (não são pulados). O `docker pull` do bloco acima baixa a imagem da
  primeira linha `FROM` do `engine/Dockerfile`, fixada pelo commit e pelo digest, como o CI faz antes da suíte
  inteira. Faça isso uma vez, antes da primeira suíte inteira, e de novo quando o `engine/Dockerfile` trocar a imagem.
  Com `SYSTEM=0`, esses testes ficam de fora com os outros de sistema.
- Alguns testes usam o binário `age`, e sem `CQ_TEST_AGE_DIR` eles falham (não são pulados). Eles só rodam em
  x86-64, no Linux ou no Windows; noutras máquinas (um Mac ou uma máquina ARM, por exemplo) esses testes falham. O
  `scripts/fetch_age.py` baixa a versão fixada do `age` e confere o SHA-256 dela; no Python nativo do Windows, use
  `--platform windows-amd64`. Faça isso uma vez, numa pasta fora do repositório, e exporte a variável em cada
  terminal.
- A suíte do motor constrói as imagens, roda os testes do motor dentro da imagem dele, sem rede, só de leitura e
  sem root, e compara o motor com o oráculo, um
  PrairieLearn local em `127.0.0.1:3000` na mesma versão da imagem do motor. O script não cria o oráculo: ele inicia
  um container de nome `pl-oracle` que já precisa existir. O workflow [`engine.yml`](.github/workflows/engine.yml)
  mostra o comando que o cria, com a imagem fixada. Rode a suíte do motor sem outras suítes em paralelo.

O CI roda a suíte rápida em todo push e em toda pull request, a suíte inteira nos pushes da `main` e a do motor nos
pushes que mudam o motor ou os contratos dele. Uma pull request precisa da suíte rápida verde.

## Política de testes

- **Testes primeiro.** Uma mudança de comportamento chega com os testes que a descrevem, escritos antes do código;
  a correção de um defeito traz o teste que o reproduz.
- **Não altere um teste existente para o código passar.** Se um teste parecer errado, diga isso na issue ou na pull
  request. A correção do teste é uma mudança própria, separada da mudança do código, explicada e revisada como tal.
- **Dados fictícios sempre.** Os testes usam identidades fictícias; nunca dados reais de alunos, tokens reais ou
  segredos. Não enfraqueça os testes de privacidade.
- **Fidelidade ao PrairieLearn.** Uma mudança na correção das respostas é conferida contra o oráculo, pela suíte do
  motor.
- **Contratos primeiro.** Uma operação nova entra no catálogo único (`contracts/api/v1/catalog.json`), que liga a
  rota da API, o comando `cq` e, quando a operação é exposta ao agente, a ferramenta do MCP; os testes de contrato
  conferem o catálogo contra a API, o CLI e o MCP.

O porquê dessas regras está nos [princípios do projeto](docs/explicacao/principios.md) e na
[descrição do método](docs/explicacao/metodo.md).

## Commits

Os commits seguem o [Conventional Commits](https://www.conventionalcommits.org/pt-br/v1.0.0/):
`<tipo>(<escopo opcional>): <descrição>`, com a descrição em português.

- Tipos mais usados: `feat`, `fix`, `docs`, `test`, `refactor`, `ci` e `chore`.
- Exemplos de forma: `feat(cli): ...`, `fix(motor): ...`, `test(api): ...` e `docs: ...`.
- Uma mudança incompatível leva `!` depois do tipo ou do escopo e o rodapé `BREAKING CHANGE:`.
- Prefira commits pequenos, cada um com um assunto. Nunca commite o `.env`, tokens, chaves, dumps de banco nem
  dados de alunos. Se um segredo real entrou num commit por engano, revogue-o primeiro e avise pelo canal privado da
  [política de segurança](SECURITY.md).

## Pull requests

1. Faça um fork e crie uma branch a partir da `main`.
2. Mantenha a mudança pequena e focada; cite a issue que ela resolve.
3. Rode localmente os testes que a mudança toca e o lint.
4. Preencha o modelo da pull request: o que muda, como foi testado e o checklist dos princípios.
5. Se a mudança altera algo visível para quem usa, atualize a documentação e a seção `[Unreleased]` do
   [`CHANGELOG.md`](CHANGELOG.md).

O mantenedor revisa a pull request e pode pedir ajustes antes do merge.

## Contribuições feitas com IA

Contribuições feitas com a ajuda de ferramentas de IA (assistentes de código, agentes, geradores de texto) são
aceitas, nestas condições:

- **Declare o uso na pull request:** qual ferramenta e em que parte (código, testes, documentação). O modelo da pull
  request tem um campo para isso.
- **Você responde pelo que envia.** Leia, entenda e teste todo o conteúdo como se o tivesse escrito à mão; "a
  ferramenta gerou" não justifica um defeito.
- **As regras de teste e de revisão são as mesmas.** Testes primeiro, nenhum teste existente alterado para o código
  passar e a mesma revisão de qualquer outra pull request.
- **Nenhum segredo nem dado pessoal vai a um modelo.** Não ponha no contexto de uma ferramenta de IA o `.env`, o
  arquivo de perfil do `cq`, tokens, chaves, dumps de banco nem dados reais de alunos.

Os agentes de código seguem o [`AGENTS.md`](AGENTS.md); o [`CLAUDE.md`](CLAUDE.md) acrescenta o que é próprio do
Claude Code.

## Licença

O projeto é distribuído sob a licença AGPL-3.0-or-later ([`LICENSE`](LICENSE)). Ao contribuir, você concorda que a sua
contribuição seja distribuída sob a mesma licença.
