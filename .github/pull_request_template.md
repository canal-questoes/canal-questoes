<!-- Obrigado pela contribuição! Preencha as seções abaixo. Pull requests em inglês também são aceitas. -->

## O que muda

<!-- O problema e a solução, em poucas linhas. Cite a issue relacionada, por exemplo: "Resolve #123". -->

## Como foi testado

<!-- Os testes novos e os comandos que você rodou, com o resultado. Por exemplo:
- `uv run python ops/ci/ci_suite.py fast`
- `bash scripts/run-engine-suite.sh`, se a mudança toca o motor
- `uv run ruff check server/src client/src ops scripts`
-->

## Checklist dos princípios

Os princípios estão em `docs/explicacao/principios.md`. Marque o que vale para esta pull request e explique abaixo
o que não vale.

- [ ] **Conhecimento verificável e rastreável:** o que esta pull request afirma tem fonte (um teste, a saída de um
  comando, um documento), e o que é suposição está dito como suposição.
- [ ] **Testes primeiro, com revisão adversarial independente:** a mudança traz os testes que a descrevem, e nenhum
  teste existente foi alterado para o código passar (se algum teste mudou, explico por quê).
- [ ] **Identidade só sai pela revelação:** nenhum nome, RA, e-mail ou conta do Telegram sai fora da operação de
  revelação, e não há segredo nem dado pessoal no código, nos testes, nos registros ou nesta pull request.
- [ ] **Fidelidade ao PrairieLearn:** a correção continua igual à do PrairieLearn; uma divergência nova é deliberada,
  explicada e conferida contra o oráculo.
- [ ] **Fronteiras, portas e contratos:** o núcleo não passou a depender de Telegram, PrairieLearn, git, e-mail nem
  HTTP, e uma operação nova entrou no catálogo único (rota da API, comando `cq` e, se exposta ao agente,
  ferramenta do MCP).
- [ ] **Nenhuma resposta perdida:** a entrada do aluno continua gravada antes de qualquer processamento, e as tarefas
  continuam idempotentes e retomáveis.
- [ ] **Escopo contido:** a mudança está no escopo do projeto ou foi combinada numa issue.
- [ ] Os commits seguem o Conventional Commits, e a documentação e o `CHANGELOG.md` foram atualizados se algo
  visível mudou.

## Uso de IA

- [ ] Esta pull request não usou ferramenta de IA, **ou** declaro abaixo qual ferramenta usei e em que parte, li e
  testei todo o conteúdo e respondo por ele.

<!-- Ferramenta e parte (código, testes, documentação): -->
