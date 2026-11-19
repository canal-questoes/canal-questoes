# Política de segurança

O canal-questoes lida com dados de alunos e com segredos de operação, como os tokens dos bots do Telegram e as
credenciais do banco e do e-mail. Relatos de vulnerabilidade são bem-vindos e tratados em privado.

## Versões suportadas

Só a versão publicada mais recente recebe correções de segurança.

| Versão | Recebe correções de segurança |
| --- | --- |
| 0.1.x | sim |

## Como relatar

**Não abra issue, discussão nem pull request pública para relatar uma vulnerabilidade.** Use um destes canais
privados:

1. **O relato privado do GitHub (preferido).** Na página do repositório, abra a aba **Security and quality** (se ela
   não aparecer, está no menu de três pontos) e clique em **Report a vulnerability**. O relato chega em privado a
   quem mantém o projeto, e você acompanha a conversa nele.
2. **E-mail.** Se o relato privado não estiver disponível, escreva para <canal.questoes.ic@gmail.com>.

Inclua, se puder:

- a versão (a tag) ou o commit afetado;
- a parte afetada: a API, o worker e os bots, o CLI `cq`, o motor de questões, o servidor MCP e o plugin, ou os
  scripts de operação;
- os passos para reproduzir, com dados fictícios;
- o impacto: o que alguém consegue fazer explorando a falha.

Nunca envie dados reais de alunos nem um segredo em uso. Se o relato envolver um segredo real que vazou (por exemplo,
o token de um bot), revogue-o antes de relatar.

## O que acontece depois

- A **primeira resposta sai em até 14 dias**, com a confirmação do recebimento e os próximos passos.
- Você é mantido informado até a correção. A correção sai numa versão nova, registrada na seção "Segurança" do
  [`CHANGELOG.md`](CHANGELOG.md).
- Combinamos com você quando a vulnerabilidade pode ser divulgada e se você quer o crédito pelo relato.

## Escopo

Vale para o código deste repositório. Uma vulnerabilidade do próprio PrairieLearn, do Telegram ou de uma dependência
deve ser relatada ao projeto responsável; se ela afetar o canal-questoes, avise também por um dos canais acima.

A forma como o sistema protege a identidade dos alunos (o pseudônimo aleatório e a operação de revelação auditada)
está nos [princípios do projeto](docs/explicacao/principios.md).
