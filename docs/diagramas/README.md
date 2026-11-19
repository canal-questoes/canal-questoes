# Diagramas

Diagramas da arquitetura e do método em **D2**. A arquitetura segue os níveis do modelo C4: contexto → contêineres →
componentes. Cada fonte `.d2` tem o SVG renderizado ao lado, com o mesmo nome.

| Fonte | SVG | Conteúdo | Visão |
| --- | --- | --- | --- |
| `c4-contexto.d2` | [`c4-contexto.svg`](c4-contexto.svg) | Sistema, pessoas e sistemas externos | C4 nível 1 (contexto) |
| `c4-containers.d2` | [`c4-containers.svg`](c4-containers.svg) | Contêineres e comunicação | C4 nível 2 (contêineres) |
| `implantacao.d2` | [`implantacao.svg`](implantacao.svg) | Contêineres, volumes e backup | implantação |
| `motor-camadas.d2` | [`motor-camadas.svg`](motor-camadas.svg) | O que é deste projeto e o que é do PrairieLearn dentro do engine | componentes do motor |
| `operacao-agentica.d2` | [`operacao-agentica.svg`](operacao-agentica.svg) | CLI, servidor MCP, skills, plugin | operação |
| `estados-entrega.d2` | [`estados-entrega.svg`](estados-entrega.svg) | Estados de uma entrega individual, com a falha da questão e o "não sei" depois do prazo | comportamento |
| `modelo-dados.d2` | [`modelo-dados.svg`](modelo-dados.svg) | As tabelas principais, com uma seleção das colunas | dados |
| `loop-por-spec.d2` | [`loop-por-spec.svg`](loop-por-spec.svg) | O ciclo de cada [fatia](../explicacao/metodo.md): testes primeiro, revisão dupla, congelamento, convergência e revisão do código | método de desenvolvimento |

## Gerar os SVGs

Os SVGs são gerados com o **D2 0.9.0** e o layout **ELK** (`--layout elk`). Com o `d2` no `PATH`, dentro desta pasta:

```bash
for f in *.d2; do d2 --layout elk "$f" "${f%.d2}.svg"; done
```

Se o `d2` não estiver no `PATH`, chame-o pelo caminho absoluto. No ambiente do mantenedor (Windows), ele fica em
`%LOCALAPPDATA%\Programs\d2\d2-v0.9.0\bin\d2.exe`; no Git Bash:

```bash
for f in *.d2; do "$LOCALAPPDATA/Programs/d2/d2-v0.9.0/bin/d2.exe" --layout elk "$f" "${f%.d2}.svg"; done
```

Os SVGs ficam versionados **ao lado** dos fontes, porque o GitHub não renderiza D2. Ao mudar um fonte, gere o SVG de
novo no mesmo commit; o teste de contrato da árvore pública confere que cada fonte tem o seu SVG.
