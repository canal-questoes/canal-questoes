# Explicação

Os textos desta parte explicam o porquê do canal-questoes: como o sistema se organiza, como ele foi construído, os
princípios que o guiam e as decisões de projeto que o trouxeram até aqui. Eles não ensinam a usar o sistema nem
listam comandos: para usar, comece pelo [tutorial de primeiro uso](../tutorial/primeiro-uso.md); para consultar
comandos, operações e ferramentas, use a [referência](../referencia/README.md). O índice de toda a documentação está
em [`docs/README.md`](../README.md).

O canal-questoes nasceu como trabalho de conclusão de curso de Ciência da Computação na Unicamp. A arquitetura e as
decisões de projeto são adaptadas da monografia desse trabalho.

| Texto | O que explica |
| --- | --- |
| [Arquitetura](arquitetura.md) | como o sistema se organiza: o contexto, os contêineres e a comunicação entre eles, o núcleo com as portas e os adaptadores, o motor de questões, a representação intermediária entre o motor e o canal, o caminho de uma questão, os dados e a privacidade, a operação por agente e a implantação e a escala; adaptada da monografia e conferida contra o código |
| [Método de desenvolvimento](metodo.md) | como o sistema foi construído: as fatias encadeadas, a especificação primeiro, os testes escritos antes do código, a revisão adversarial dupla, os testes congelados, a convergência e a revisão humana adiada, com o diagrama do ciclo |
| [Princípios](principios.md) | os sete princípios do projeto, com o porquê de cada um, e as restrições técnicas |
| [Decisões de projeto](decisoes.md) | os requisitos e as principais decisões de projeto, com o motivo de cada uma; adaptadas da monografia |

Os diagramas citados nesses textos ficam em [`docs/diagramas/`](../diagramas/README.md), com o fonte em D2 e o SVG
renderizado ao lado de cada um.

## Por onde começar

- Quem quer saber **o que o sistema faz por dentro** começa pela [arquitetura](arquitetura.md).
- Quem quer **contribuir** começa pelos [princípios](principios.md) e pelo [`CONTRIBUTING.md`](../../CONTRIBUTING.md):
  os princípios dizem o que não se negocia, por exemplo que um teste existente não muda para o código passar.
- Quem quer entender **como um sistema deste tamanho foi construído com agentes de código** começa pelo
  [método](metodo.md).
- Quem quer saber **por que uma escolha foi feita** vai às [decisões de projeto](decisoes.md).
