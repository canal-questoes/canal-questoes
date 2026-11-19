# Política de retenção

Política de retenção dos dados do piloto: por quanto tempo cada lugar guarda os dados, quem apaga, o limite dos
backups e o que fazer depois de uma restauração.
Os comandos citados aqui estão no [guia de backup e restauração](backup-e-restauracao.md)
e no [guia da VM](implantacao-vm.md); este documento diz **o que** fica guardado e **até quando**.

## Escopo e base

Vale para o piloto (a pilha da VM, os backups e a cópia no computador do Pedro) e para qualquer pilha
restaurada a partir desses backups.

- **Base.** As identidades dos alunos (nome, contato, vínculo com o Telegram) só existem para entregar as
  questões e para a revelação autorizada; o relatório trabalha com `pseudo_id`. Depois do arquivamento do
  oferecimento, a identidade não tem mais uso e deve ser expurgada dentro do prazo.
- **Prazo provisório.** Até o Rodolfo responder a pergunta sobre o prazo (ainda aberta), o prazo é de
  **30 dias** depois do arquivamento, configurável. **[I]** Pela LGPD, guardar só o necessário; o prazo curto segue
  essa linha.
- **Nada expurga sozinho.** O sistema avisa; quem expurga é o Pedro, ou o agente com a aprovação dele,
  sempre em duas fases (preparar e confirmar o expurgo).
- **Escolhas por padrão.** Os prazos dos backups e o conteúdo desta política são escolhas por padrão
  feitas no desenvolvimento, ainda à espera da confirmação do Pedro.

## Banco vivo

O banco do Postgres da pilha guarda a identidade de cada oferecimento até o expurgo dele.

- **Prazo:** **30 dias** depois do arquivamento, provisório e configurável pela variável `CQ_RETENTION_DAYS`
  do servidor (dias inteiros, padrão 30, mínimo 1; um valor inválido impede a partida da API com mensagem fixa,
  `config.invalid`).
- **Aviso do `doctor`.** A verificação `retention.overdue` do `cq doctor` sai em "aviso" quando há
  oferecimento arquivado, não expurgado, cujo arquivamento passou do prazo, com a contagem e o código do mais antigo;
  sai "ok" quando não há. Ela nunca sai "falha" e nunca dispara ação: é só o lembrete do expurgo. O Pedro confere o
  `doctor` antes de cada disparo e no encerramento do piloto.
- **Expurgo manual em duas fases.** Feito pelo Pedro no CLI (`cq offerings purge prepare <código>` e depois
  `cq offerings purge confirm <código> --confirmation <código-de-confirmação>`), ou pelo agente pela skill
  `encerramento`, que prepara e só confirma com a aprovação explícita do Pedro. Depois do expurgo, o oferecimento sai
  da contagem do `doctor`.
- **Registro do expurgo.** O Pedro anota cada expurgo (código do oferecimento e data) na seção
  `## Expurgos realizados` do diário preenchido do piloto, que fica fora do repositório (ver
  [Repositório público](#repositório-público)). É essa lista que permite refazer os expurgos depois de uma restauração.
- **Limite.** O expurgo é completo só no banco vivo: as cópias físicas que o Postgres ainda não reaproveitou
  ficam fora do alcance, e os backups anteriores ao expurgo continuam com o banco inteiro (ver as duas seções
  seguintes).

## Dumps na VM

A rotina de backup tira um dump cifrado com o age por dia, às 03:30 UTC, e guarda os dumps em `/var/backups/cq/dumps/`
na VM.

- **Prazo:** **7 dias** (configurável). A própria rotina apaga os dumps mais antigos a cada execução.
- Cada dump leva o banco inteiro, inclusive as identidades dos oferecimentos ainda não expurgados no momento
  do dump. Um expurgo não altera os dumps já tirados: a identidade expurgada some da VM quando o último dump anterior
  ao expurgo sai da rotação, em até 7 dias.
- **Perda máxima.** Com um dump por dia, a perda da VM perde **até ~24 h** de respostas (um dia de disparos).
  Para encurtar essa janela, o plano de disparos manda o backup manual e a cópia no Windows depois de cada sessão
  intensiva. Backup contínuo está fora do escopo.

## Cópia no computador do Pedro

A tarefa agendada do Windows traz os dumps novos da VM e confere o resumo de cada um antes de aceitá-lo. Os dumps
continuam cifrados; só a chave privada do age, que fica só com o Pedro, os decifra.

- **Prazo:** **14 dias** de cópia (configurável). A cópia apaga os dumps locais mais antigos e nunca apaga nada
  na VM.
- **Limite dos backups.** Os backups não são expurgados: decifrar e reescrever cada dump exigiria a chave privada do age, que nunca fica na VM.
  Por isso, a identidade expurgada continua dentro dos backups até eles saírem da rotação: ela sai dos backups em até
  **14 dias** depois do expurgo, quando o último dump anterior ao expurgo deixa a cópia do Pedro (a VM já o apagou em
  até 7 dias).
- Se o Pedro guardar uma cópia fora da rotação (num disco externo, por exemplo), ela segue a mesma regra: apagar os
  dumps anteriores a um expurgo em até 14 dias depois dele.

## Registros dos contêineres

Os registros (logs) da `api`, do `worker` e do `postgres` usam o driver `json-file` com rotação de **10m × 3**: até 3
arquivos de 10 MB por contêiner, e o mais antigo é descartado quando o terceiro enche. O motor mantém a rotação dele
(1m × 2).

- **[I]** A rotação é por tamanho, não por tempo: quanto um registro dura depende do volume de mensagens; ela limita
  o tempo de vida do que for registrado.
- Os registros vivem na VM e não entram nos dumps. Ao derrubar a pilha com `-v` (fim do piloto ou pilha descartável),
  eles somem com os contêineres.

## Textos livres de outros oferecimentos

O expurgo de um oferecimento apaga as linhas dele e as do bot dele. Mas a recepção grava o texto literal de cada
mensagem recebida pelo Telegram: se uma pessoa de outro oferecimento, ainda ativo, citar por escrito o nome ou
o contato de alguém expurgado, esse texto fica nas linhas do outro oferecimento.

- Esse texto é retenção no banco, não saída: ele não sai pela API sem a revelação.
- Ele segue o prazo do outro oferecimento: sai no expurgo dele, 30 dias depois do arquivamento dele.
- O mesmo vale para um contato do outro oferecimento igual ao do expurgado: é dado do outro oferecimento e segue o
  prazo dele.

## Repositório público

O repositório público e o pacote do sistema levam só o que casa a lista positiva (`ops/export/allowlist.txt`), e a
exportação (`ops/export/export_public.py`) passa a varredura de identidade e de segredos antes de publicar.

- Nenhum dado de aluno vai ao repositório: nem lista de alunos, nem dump, nem registro, nem `.env`, nem relatório com
  identidade.
- O `docs/operacao/piloto/diario.md` do repositório é só o **modelo**. O diário preenchido do piloto (incidentes,
  pedidos ao agente, códigos expurgados) fica **fora do repositório**, junto da cópia dos backups no Windows:
  o texto livre dele não seria pego pelos detectores da varredura.
- As métricas do piloto que vão à monografia saem agregadas e sem identidade.

## Depois de uma restauração

Um dump anterior a um expurgo traz de volta as identidades expurgadas depois dele. Por isso, logo depois de restaurar
(numa VM nova ou numa pilha descartável que vá continuar no ar), o Pedro precisa refazer os expurgos do diário:

1. abrir o diário preenchido, fora do repositório, e listar os códigos da seção `## Expurgos realizados` com data
   igual ou posterior à do dump restaurado;
2. conferir a verificação `retention.overdue` no `cq doctor` da pilha restaurada;
3. refazer, em duas fases, o expurgo de cada código da lista e de cada código que o `doctor` apontar como vencido;
4. conferir no `cq doctor` que `retention.overdue` voltou a "ok" e anotar no diário preenchido que os expurgos foram
   refeitos.

O passo a passo, com os comandos prontos, está na seção "Depois de restaurar" do
[guia de backup e restauração](backup-e-restauracao.md). Uma pilha descartável de ensaio, derrubada com `-v` logo
depois da conferência, não precisa dos expurgos refeitos.

## Quando o prazo definitivo for fixado

O prazo de 30 dias é provisório. Quando o Rodolfo fixar o prazo definitivo:

1. o Pedro registra a resposta do Rodolfo e o prazo definitivo, que substitui o provisório em toda esta
   política;
2. o Pedro muda `CQ_RETENTION_DAYS` no arquivo de ambiente da VM (editado num editor, nunca impresso no terminal) e
   reinicia a `api`; a partida recusa um valor inválido;
3. o Pedro confere no `cq doctor` a verificação `retention.overdue` com o prazo novo e expurga, em duas fases, o que
   ele apontar como vencido;
4. se o prazo novo for mais curto que o dos backups, o Pedro avalia com o Rodolfo os prazos da VM e da cópia:
   a identidade expurgada continua nos backups até eles saírem da rotação, e esta política é atualizada.
