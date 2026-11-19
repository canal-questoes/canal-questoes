# Diário do piloto (modelo)

Este arquivo é o **modelo** do diário do piloto. Ele não recebe anotações.

A cópia preenchida fica fora do repositório, junto da cópia dos backups no Windows: copie este modelo para lá antes
do primeiro disparo e anote só nessa cópia. O diário preenchido tem texto livre do piloto (incidentes, pedidos ao
agente, códigos de oferecimentos expurgados) que os detectores da exportação não pegam, e o repositório público
recebe tudo o que está em `docs/operacao/`.

Regras de preenchimento:

- nenhum dado que identifique um aluno ou um voluntário: o diário registra o que aconteceu com o sistema, nunca quem
  foi afetado;
- nenhum segredo (chave, senha, token, conteúdo de arquivo de ambiente) e nenhuma saída de comando colada inteira;
- horários no fuso de São Paulo, no formato `AAAA-MM-DD HH:MM`;
- uma entrada por incidente e uma por tarefa pedida ao agente, copiando o bloco da seção.

As métricas que saem daqui (incidentes, tempo de recuperação, teste de restauração, confirmações pedidas) aparecem
como "manual" no relatório de métricas do piloto.

## Incidente

Um incidente é qualquer falha que afetou a entrega, a correção ou a operação (bot parado, e-mail recusado, tarefa
vencida, pilha fora do ar, restauração). Copie o bloco abaixo para cada incidente.

- **Início:** `AAAA-MM-DD HH:MM` (quando a falha começou ou foi percebida, e como foi percebida: doctor, registro,
  aviso de voluntário).
- **Fim:** `AAAA-MM-DD HH:MM` (quando o serviço voltou ao normal).
- **Efeito:** o que deixou de funcionar e para quantos, em contagem (por exemplo, disparo não entregue a 3 alunos),
  sem identificar ninguém.
- **Tempo até recuperar:** em minutos, do início ao fim.
- **Ação:** o que foi feito para recuperar e o que muda para não se repetir.

## Tarefa do agente

Uma entrada por tarefa pedida ao agente pelo servidor MCP (as tarefas do token do agente também
aparecem na auditoria com origem `mcp`). Copie o bloco abaixo para cada tarefa.

- **Pedido:** o que foi pedido, em palavras gerais, sem identidade de aluno.
- **Ferramentas:** as ferramentas MCP que o agente chamou, na ordem.
- **Confirmações:** quantas confirmações o agente pediu e quais (as operações em duas fases).
- **Resultado:** concluída, concluída com ajuste ou não concluída, e o motivo em uma linha.

## Expurgos realizados

Uma linha por expurgo, com a data e o código do oferecimento, nada mais. Depois de uma restauração, esta lista diz
quais oferecimentos expurgar de novo (guia de backup, seção "Depois de restaurar"; política de retenção).

| Data | Código do oferecimento |
| --- | --- |
| `AAAA-MM-DD` | `<código>` |
