# Roteiro do ensaio interno (05 e 06/11)

Roteiro do Pedro para o ensaio interno do piloto. Cada
item é uma caixa `- [ ]`: marque-a só depois da conferência dela e anote o resultado no diário. O critério de "pronto
para convidar" está na seção do mesmo nome; a troca para o projeto do piloto vem depois dele e antes de qualquer
convite.

Como o ensaio é montado:

- o ensaio roda num projeto próprio do Compose na VM, o `cq-ensaio`, com banco e volumes próprios, desmontado antes de
  o projeto `cq` do piloto subir. Os dumps do piloto não levam os dados do ensaio, e as métricas não os
  misturam. **[F]** O mesmo bot não entra em dois oferecimentos do mesmo banco (`bots.telegram_bot_id UNIQUE`);
  por isso o bot do piloto pode ser ensaiado no `cq-ensaio` e registrado de novo no `cq`;
- a RAM da VM não comporta duas pilhas: o `cq` fica parado durante o ensaio, e o `cq-ensaio` para antes de o `cq` subir;
- os dois bots do ensaio são o **bot A**, que será o bot do piloto, e o **bot B**, só do ensaio. **[I]** Escolha minha:
  ensaiar o próprio bot do piloto prova o token e o nome que os voluntários vão usar;
- os oferecimentos do ensaio (`ENSAIO-A`, com o bot A, e `ENSAIO-B`, com o bot B) são os oferecimentos de teste do
  envio de e-mail: cada lista tem uma única linha, com um endereço seu. **[I]** Eles substituem, só no ensaio,
  o oferecimento `TESTE-EMAIL` da seção 13 de [`implantacao-vm.md`](../implantacao-vm.md): o banco do `cq-ensaio` some
  na troca, e o bot A não fica preso a um oferecimento do banco do piloto;
- os comandos `cq --profile piloto …` rodam no Windows, com o túnel aberto (seção 12 do guia da VM); os outros rodam
  na VM, por SSH, com o perfil `vm-local`;
- o agente não registra bot nem lida com token: todo bot é registrado **pelo CLI**, pelo Pedro;
- nenhum comando deste roteiro imprime o arquivo de ambiente da pilha ou a configuração interpolada do Compose; o
  arquivo de perfis (`~/.config/cq/config.toml`) tem o token e só é aberto num editor;
- tokens de bot ficam só em arquivos fora do repositório (`C:\cq-ensaio\`), salvos do BotFather direto no arquivo, e
  a pasta é apagada no fim do ensaio.

## 05/11

### Antes do ensaio (até 04/11)

- [ ] Conferir as referências das questões com rede, **até 04/11**, com o `ops/pilot/check_question_refs.py`.
  No Windows, na raiz do repositório:

  ```powershell
  uv run python ops/pilot/check_question_refs.py --manifest docs/operacao/piloto/questoes.lock.json
  ```

  Conferência: a saída é `Todas as referências das questões conferem.` e o código de saída é 0. Qualquer outra
  mensagem para o ensaio até a causa ser entendida (a mensagem diz se faltou a rede, a referência ou o SHA-256).
- [ ] Copiar o modelo do diário para fora do repositório, junto da cópia dos backups no Windows (por exemplo,
  `C:\cq-backups\diario-piloto.md`), e anotar só nessa cópia. O diário preenchido fica fora do repositório
  porque tem texto livre do piloto que os detectores da exportação não pegam; o modelo
  [`diario.md`](diario.md) não recebe anotações.

  Conferência: a cópia existe fora da pasta do repositório, e `git status` no repositório não mostra o diário mudado.

### Pilha do ensaio

- [ ] Trocar o projeto da pilha para `cq-ensaio`: no `[profiles.vm-local]` do arquivo de perfis da VM, `compose_project = "cq-ensaio"`.
  Na VM, pare o `cq` se ele estiver no ar, edite o arquivo **num editor** (ele tem o token) e suba o ensaio:

  ```bash
  cd /srv/cq/app
  cq down
  nano ~/.config/cq/config.toml
  cq up --timeout 600
  cq status
  ```

  No editor, mude só a linha `compose_project` do `[profiles.vm-local]` para `"cq-ensaio"`, sem tocar no token.

  Conferência: o `cq status` mostra o perfil `vm-local` e a linha `Pilha local (projeto cq-ensaio):`, com os quatro
  serviços no ar e a API saudável. **[F]** O `compose_project` só vale num perfil nomeado; fora do
  `[profiles.vm-local]`, ele seria ignorado e a pilha subiria como `cq`.
- [ ] Apontar a unidade do backup para o `cq-ensaio` (o backup segue o projeto pela variável da unidade),
  reinstalando o serviço com o projeto trocado:

  ```bash
  cd /srv/cq/app
  sed -e "s/^User=.*/User=$USER/" -e 's/^Environment="CQ_BACKUP_PROJECT=cq"$/Environment="CQ_BACKUP_PROJECT=cq-ensaio"/' ops/backup/cq-backup.service | sudo tee /etc/systemd/system/cq-backup.service > /dev/null
  sudo systemctl daemon-reload
  systemctl cat cq-backup.service | grep CQ_BACKUP_PROJECT
  sudo systemctl start cq-backup.service
  journalctl -u cq-backup.service -n 20 --no-pager
  ```

  Conferência: o `grep` mostra `CQ_BACKUP_PROJECT=cq-ensaio`; o `journalctl` mostra `Backup concluído.`.
- [ ] `cq doctor` na VM sem `[falha]`, com `backup.age` e `smtp.server` em `[ok]`.

  ```bash
  cq doctor
  ```

  Conferência: nenhuma linha `[falha]`; `backup.age` em `[ok]`, com a idade perto de 0 h; `smtp.server` em `[ok]`.
  **[I]** Se o `smtp.server` aparecer em "aviso" logo depois da subida, o `worker` ainda não fez a primeira sonda:
  espere alguns minutos e rode o `cq doctor` de novo; se continuar, siga a seção 15 do guia da VM.

### Os dois bots e os testes do Telegram

- [ ] Criar os dois bots no BotFather (bot A, o do piloto; bot B, só do ensaio), salvando cada token direto num
  arquivo: `C:\cq-ensaio\bot-a.token` e `C:\cq-ensaio\bot-b.token`.

  Conferência: os dois arquivos existem, e nenhum token passou pelo chat, pelo terminal ou pelo agente.
- [ ] Testes manuais do Telegram **antes** de registrar os bots na pilha, com o kit de testes do Telegram (que fica fora do repositório público;
  seções 3.1, 3.2 e 6 do roteiro do kit). **[I]** Antes do registro, porque o kit lê as mensagens dos bots pelo `getUpdates`, e a pilha,
  depois do registro, também: os dois juntos dão conflito.
- [ ] Telegram, mensagens ricas no Android: as mensagens do kit (seção 3.1) abertas no aplicativo do Telegram no Android.
  Resultado (renderizou, o que falhou, contorno): ______
- [ ] Telegram, mensagens ricas no iOS: as mesmas mensagens abertas no aplicativo do Telegram no iOS.
  Resultado (renderizou, o que falhou, contorno): ______
- [ ] Telegram, mensagens ricas no desktop: as mesmas mensagens abertas no Telegram para desktop.
  Resultado (renderizou, o que falhou, contorno): ______
- [ ] Telegram, o mesmo `user_id` em dois bots: a sua conta escreve aos bots A e B, e o kit (seção 3.2) compara os dois.
  Resultado (igual ou diferente): ______

  Uma falha num teste do Telegram só segue com o contorno anotado aqui e no diário e com a decisão do Pedro.
- [ ] Criar os dois oferecimentos de teste e registrar cada bot **pelo CLI** (`bots register`, nunca pelo agente).
  No Windows, com o túnel aberto:

  ```powershell
  cq --profile piloto offerings create ENSAIO-A --course "Ensaio A" --term 2026s2 --timezone America/Sao_Paulo
  cq --profile piloto bots register ENSAIO-A --token-file C:\cq-ensaio\bot-a.token
  cq --profile piloto bots activate ENSAIO-A
  cq --profile piloto offerings create ENSAIO-B --course "Ensaio B" --term 2026s2 --timezone America/Sao_Paulo
  cq --profile piloto bots register ENSAIO-B --token-file C:\cq-ensaio\bot-b.token
  cq --profile piloto bots activate ENSAIO-B
  ```

  Conferência: os seis comandos terminam com código 0, e `cq --profile piloto bots show ENSAIO-A` e
  `cq --profile piloto bots show ENSAIO-B` mostram cada bot registrado e ativo.

### Teste de envio de e-mail

- [ ] Teste de envio de e-mail: cada oferecimento de teste recebe uma lista, e os convites saem em duas fases.
  Tudo pelas operações existentes, com uma lista de um único endereço seu. Prepare `C:\cq-ensaio\lista.csv` num editor, com o
  cabeçalho `ra,nome,email` e uma única linha com um RA fictício, o seu nome e um endereço de e-mail seu:

  ```powershell
  cq --profile piloto students import ENSAIO-A --file C:\cq-ensaio\lista.csv
  cq --profile piloto students import ENSAIO-B --file C:\cq-ensaio\lista.csv
  cq --profile piloto invites send prepare ENSAIO-A
  cq --profile piloto invites send confirm ENSAIO-A --confirmation <código mostrado no prepare>
  cq --profile piloto invites send prepare ENSAIO-B
  cq --profile piloto invites send confirm ENSAIO-B --confirmation <código mostrado no prepare>
  ```

  Conferência: `cq --profile piloto students summary ENSAIO-A` (e o mesmo para `ENSAIO-B`) mostra um aluno; cada
  `prepare` mostra o resumo com um destinatário antes do `confirm`.
- [ ] O e-mail de teste chegou: os dois convites chegaram ao seu endereço, vindos da conta do projeto, cada um com o
  link do bot certo. Anote no diário o tempo até chegar.
- [ ] Entrar nos dois oferecimentos pelo link de cada convite, com a mesma conta do Telegram.

  Conferência: cada bot responde à entrada, e `cq --profile piloto students summary ENSAIO-A` (e `ENSAIO-B`) mostra o
  aluno vinculado.

### Disparos, primeira parte

Cada disparo usa uma questão do manifesto [`questoes.lock.json`](questoes.lock.json), no repositório e no commit
que ele fixa, e é feito nos dois bots. O modelo, para o `ENSAIO-A` (repita com `ENSAIO-B`):

```powershell
cq --profile piloto dispatches prepare ENSAIO-A --repo https://github.com/pedroraj/cq-demo-questoes.git --commit ca01df00085b0172af68253aeba93e76cd7f6ecb --qid <qid>
cq --profile piloto dispatches show ENSAIO-A <id do disparo>
cq --profile piloto dispatches confirm ENSAIO-A <id do disparo>
```

Rode o `show` até a validação terminar e confira a prévia antes do `confirm`. Responda pelo Telegram e confira o
retorno do bot contra a resposta esperada da questão.

- [ ] `pl-multiple-choice` (`capital`) nos dois bots: entregue, respondido e corrigido como esperado.
- [ ] `pl-checkbox` (`primos`) nos dois bots: entregue, respondido e corrigido como esperado.
- [ ] `pl-string-input` (`palavra`) nos dois bots: entregue, respondido e corrigido como esperado.

  Conferência: `cq --profile piloto dispatches list ENSAIO-A` (e `ENSAIO-B`) mostra os três disparos; o retorno de
  cada bot bate com o esperado. Anote no diário o que divergir.

## 06/11

### Disparos, segunda parte

- [ ] `pl-number-input` (`velocidade`) nos dois bots: entregue, respondido e corrigido como esperado.
- [ ] `pl-integer-input` (`soma`) nos dois bots: entregue, respondido e corrigido como esperado.
- [ ] Um erro de formato de propósito em cada um dos dois bots: no `pl-number-input` ou no `pl-integer-input`,
  responda primeiro com um texto que não é número (por exemplo, `abc`) e depois com a resposta certa.

  Conferência: o bot avisa o formato inválido sem corrigir a resposta errada como tentativa, e a resposta seguinte é
  corrigida normalmente. Os 5 tipos de campo estão marcados acima, nos dois bots.

### Restauração e recuperação

- [ ] Restaurar a cópia real do Windows numa pilha descartável, em até 30 min: rode
  `C:\cq-backups\copiar-backups.cmd` e siga a seção "Restaurar numa pilha descartável" de
  [`backup-e-restauracao.md`](../backup-e-restauracao.md) com o dump mais novo de `C:\cq-backups\dumps` (um dump do
  `cq-ensaio`, de 05/11 ou de 06/11).

  Conferência: a saída é `Restauração concluída.`, a pilha descartável foi desmontada com `-v`, e o tempo total foi
  anotado no diário (meta: 30 minutos).
- [ ] Ler, sem executar, a seção "Recuperar a pilha numa VM nova" de
  [`backup-e-restauracao.md`](../backup-e-restauracao.md), conferindo que cada comando dela cita arquivos e caminhos que
  existem na VM e no Windows.

  Conferência: a leitura foi anotada no diário, com qualquer passo que ficou obscuro.

### Fim do ensaio

- [ ] Encerrar, arquivar e expurgar os dois oferecimentos de teste, cada fase com o seu código:

  ```powershell
  cq --profile piloto offerings close ENSAIO-A
  cq --profile piloto offerings archive prepare ENSAIO-A
  cq --profile piloto offerings archive confirm ENSAIO-A --confirmation <código do archive prepare>
  cq --profile piloto offerings purge prepare ENSAIO-A
  cq --profile piloto offerings purge confirm ENSAIO-A --confirmation <código do purge prepare>
  ```

  Repita com `ENSAIO-B`. Conferência: depois do expurgo, `cq --profile piloto offerings show ENSAIO-A` (e
  `ENSAIO-B`) não acha o oferecimento ou o mostra expurgado.
- [ ] Apagar a pasta `C:\cq-ensaio` (tokens dos bots e a lista).

  Conferência: a pasta não existe mais.
- [ ] Anotar no diário o resultado de cada item, com os incidentes e as divergências.

## Pronto para convidar

Os voluntários só são convidados com **todos** os itens abaixo cumpridos:

- o `cq doctor` na VM sem "falha", com `backup.age` e `smtp.server` em "ok";
- o e-mail de teste recebido;
- a cópia real restaurada;
- um disparo de cada um dos 5 tipos de campo entregue e corrigido como esperado nos dois bots, com o erro de formato;
- os testes manuais do Telegram com o resultado anotado (uma falha só com o contorno anotado e a decisão do Pedro).

Com qualquer item faltando ou falho, não convide ninguém: o Pedro decide (repetir o item, seguir com um
contorno anotado ou adiar o piloto) e registra a decisão no diário.

O critério vale também para a pilha `cq`, depois da troca abaixo: o `cq doctor` dela tem de passar antes dos
convites.

## Troca para o projeto do piloto

Depois do ensaio aprovado e antes de qualquer convite, nesta ordem:

- [ ] Parar o `cq-ensaio` e remover os volumes dele. Na VM:

  ```bash
  cd /srv/cq/app
  cq down
  docker compose -p cq-ensaio -f compose.yaml --env-file .env down -v
  docker volume ls
  ```

  Conferência: o `docker volume ls` não mostra nenhum volume começado por `cq-ensaio`.
- [ ] Voltar o `compose_project` a `"cq"` no `[profiles.vm-local]`, num editor (`nano ~/.config/cq/config.toml`), sem
  tocar no token.
- [ ] Subir o projeto do piloto com `cq up --timeout 600` e conferir com `cq status`.

  Conferência: o `cq status` mostra `Pilha local (projeto cq):`, com os quatro serviços no ar e a API saudável.
- [ ] Criar o oferecimento do piloto e registrar o bot do piloto (o bot A) **pelo CLI** (`bots register`). Salve
  de novo o token do bot A num arquivo fora do repositório (`C:\cq-piloto\bot.token`), direto do BotFather:

  ```powershell
  cq --profile piloto offerings create <código do piloto> --course "<disciplina>" --term 2026s2 --timezone America/Sao_Paulo
  cq --profile piloto bots register <código do piloto> --token-file C:\cq-piloto\bot.token
  cq --profile piloto bots activate <código do piloto>
  ```

  Conferência: os três comandos terminam com código 0; `cq --profile piloto bots show <código do piloto>` mostra o bot
  registrado e ativo; a pasta `C:\cq-piloto` foi apagada.
- [ ] Trocar o projeto da unidade `cq-backup.service` de volta para `cq`, reinstalando o serviço como na seção 10 do
  guia da VM:

  ```bash
  cd /srv/cq/app
  sed "s/^User=.*/User=$USER/" ops/backup/cq-backup.service | sudo tee /etc/systemd/system/cq-backup.service > /dev/null
  sudo systemctl daemon-reload
  systemctl cat cq-backup.service | grep CQ_BACKUP_PROJECT
  ```

  Conferência: o `grep` mostra `CQ_BACKUP_PROJECT=cq`.
- [ ] Retirar o registro do backup do ensaio: o diretório de estado é o mesmo, e sem isso o `cq doctor` do `cq`
  mostraria o último backup do ensaio.

  ```bash
  rm /var/backups/cq/state/last-backup.json
  ```

  Conferência: `ls /var/backups/cq/state` não mostra o `last-backup.json`.
- [ ] Rodar o backup manual do `cq`: `sudo systemctl start cq-backup.service`.

  Conferência: `journalctl -u cq-backup.service -n 20 --no-pager` mostra `Backup concluído.`.
- [ ] `cq doctor` na VM com `backup.age` em `[ok]` e a idade em 0 h, `smtp.server` em `[ok]` e nenhuma `[falha]`.
- [ ] Rodar a cópia no Windows (`C:\cq-backups\copiar-backups.cmd`) e conferir o dump novo em `C:\cq-backups\dumps`.
- [ ] Conferir o critério de "pronto para convidar" também na pilha `cq` (seção anterior).
- [ ] Convites em lotes pequenos: importar a lista dos voluntários e enviar os convites em duas fases,
  poucos de cada vez, conferindo no `cq --profile piloto doctor` e na caixa da conta do projeto que não houve recusa
  antes do lote seguinte.
- [ ] O questionário ([`questionario.md`](questionario.md)) só é aplicado se o Rodolfo confirmar a dispensa do CEP
  para o piloto; sem essa confirmação, ele não é enviado.

**[I]** Os dumps do `cq-ensaio` ficam na VM e na cópia do Windows até a rotação de 7 dias. Eles só têm os seus próprios
dados, porque as listas do ensaio tinham só o seu endereço.
