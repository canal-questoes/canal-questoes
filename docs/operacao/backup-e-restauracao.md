# Backup e restauração

Guia do Pedro para o backup da pilha do piloto: como a rotina funciona, as chaves do age, o ensaio de restauração
numa pilha descartável, a recuperação numa VM nova e o que fazer se uma chave se perder.
A instalação na VM (diretórios, unidades do systemd, cópia no
Windows) está no [guia da VM](implantacao-vm.md), seções 10, 11 e 14.

Marcas usadas em cada passo:

- **[passo do Pedro]**: algo que só o Pedro faz (decidir, guardar uma chave, editar um arquivo com segredo);
- **[comando pronto]**: um comando para copiar e colar, sem segredo na linha;
- cada passo termina com uma linha `Conferência:`, que diz como saber que deu certo.

Regras que valem para o guia inteiro:

- Nenhum segredo (chave privada do age, `CQ_MASTER_KEY`, senha, token) vai a chat, mensagem, documento, registro
  ou linha de comando. Arquivos com segredo são abertos só num editor, nunca impressos no terminal.
- Nenhum comando deste guia imprime a configuração interpolada do Compose nem o conteúdo de um arquivo de ambiente.
- Toda chamada ao Compose leva `-p <projeto>` explícito: sem ele, o nome do projeto vem da pasta, e uma subida na
  pasta errada criaria um banco vazio com outro nome.

## Como funciona

A rotina `ops/backup/cq_backup.py` roda na VM uma vez por dia, às 03:30 UTC, pelo agendamento do systemd
(`cq-backup.timer`, com atraso aleatório de até 10 minutos). Em cada execução ela:

1. trava o diretório de estado, para dois backups nunca rodarem juntos;
2. acha o contêiner do Postgres do projeto (`cq` no piloto) e tira o dump do banco (`pg_dump -Fc`);
3. cifra o dump com o age para o destinatário público de `/etc/cq-backup/recipients.txt`, num arquivo temporário,
   sem nenhum dump em claro no disco;
4. renomeia o resultado para `cq-<AAAAMMDDTHHMMSSZ>.dump.age` em `/var/backups/cq/dumps/` e atualiza o
   `SHA256SUMS` (o resumo SHA-256 de cada dump retido);
5. grava o registro `last-backup.json` no diretório de estado `/var/backups/cq/state/` (hora de término, nome,
   tamanho e resumo do dump; nada secreto), que a `api` lê só para leitura para o `cq doctor` mostrar a idade do
   último backup (`backup.age`);
6. apaga os dumps com mais de **7 dias** na VM.

Do lado do Windows, a cópia `ops/backup/pull_backups.py` roda pela tarefa agendada (diária e no logon), traz pelo
`sftp` os dumps novos e o `SHA256SUMS`, confere o resumo de cada arquivo antes de aceitá-lo e mantém **14 dias** de
cópia no computador do Pedro. Ela nunca apaga nada na VM. Na VM, ela entra como `cqbackup`, um usuário só de SFTP
e só de leitura, preso por `ChrootDirectory` no diretório dos backups, `/var/backups/cq` (a "jaula"): lá
dentro, esse diretório aparece como `/`, e o caminho dos dumps que a cópia recebe (`--remote-dir`) é `/dumps`
([`implantacao-vm.md`](implantacao-vm.md), seções 2, 10 e 11). **[F]** Dentro da jaula, o `cqbackup` também vê o
diretório de estado (`/state`, com o `last-backup.json` e a trava: só metadados do backup, sem segredo), porque os
caminhos dos dois subdiretórios estão fixados em `ops/backup/cq-backup.service`; fora dela, não vê o resto da VM.

**Perda máxima.** Com um dump por dia, a perda da VM perde **até ~24 h** de respostas (um dia de disparos).
Para encurtar essa janela, depois de cada sessão intensiva o Pedro roda o backup manual e a cópia no Windows (plano
de disparos). Backup contínuo está fora do escopo.

**[comando pronto]** Ver quando a rotina rodou e quando roda de novo (na VM):

```bash
systemctl list-timers cq-backup.timer
sudo systemctl start cq-backup.service      # backup manual, fora do horário
journalctl -u cq-backup.service -n 20 --no-pager
```

Conferência: o `journalctl` mostra a linha `Backup concluído.`; no Windows, `cq --profile piloto doctor` mostra
`backup.age` em "ok", com a idade em horas próxima de 0 depois do backup manual.

## Chaves do age

São duas chaves que ficam **só com o Pedro**: a chave privada do age (decifra os dumps) e a `CQ_MASTER_KEY` (decifra
os tokens dos bots dentro do banco). Uma sem a outra não basta para recuperar o piloto. A VM recebe só a parte
**pública** do age; a privada nunca vai à VM, ao git, ao chat ou a um documento.

**[comando pronto]** Obter o age oficial no Windows (v1.3.2, com o SHA-256 conferido pelo script antes de extrair;
precisa de rede; o destino fica fora do repositório):

```powershell
uv run python scripts/fetch_age.py --platform windows-amd64 --dest "$env:LOCALAPPDATA\cq-tools"
& "$env:LOCALAPPDATA\cq-tools\age\age.exe" --version
```

Conferência: o segundo comando mostra `v1.3.2`.

**[passo do Pedro]** Gerar o par de chaves no Windows, numa pasta sua fora de qualquer cópia git (o exemplo usa
`C:\cq-chaves`):

```powershell
New-Item -ItemType Directory -Force C:\cq-chaves | Out-Null
& "$env:LOCALAPPDATA\cq-tools\age\age-keygen.exe" -o C:\cq-chaves\cq-backup.agekey
& "$env:LOCALAPPDATA\cq-tools\age\age-keygen.exe" -y C:\cq-chaves\cq-backup.agekey > C:\cq-chaves\recipients.txt
```

O `age-keygen -o` grava a privada só no arquivo e mostra no terminal só a linha `Public key: age1…`; o
`age-keygen -y` lê a privada e escreve só a pública (README do age). Nenhum desses comandos imprime
a privada. Nunca abra o arquivo `.agekey` no terminal nem o anexe a nada.

Conferência: `Get-Content C:\cq-chaves\recipients.txt` mostra uma única linha que começa por `age1`; o arquivo
`.agekey` existe e você não viu o conteúdo dele.

**[passo do Pedro]** Guardar a chave privada do age em **dois lugares** sob seu controle, fora da VM e fora do git:
por exemplo, o arquivo `C:\cq-chaves\cq-backup.agekey` e uma cópia num gerenciador de senhas ou num pendrive guardado
em outro lugar. Faça o mesmo com a `CQ_MASTER_KEY`: a mesma que você digitou no `init_vm_env.py` fica também em dois
lugares seus, fora da VM e fora do git (guia da VM, seção 7).

Conferência: você consegue apontar os dois lugares de cada chave, e nenhum deles é a VM, uma pasta com `.git` ou uma
pasta sincronizada com terceiros sem cifra.

**[comando pronto]** Copiar só a pública para a VM (a linha `age1…` não é segredo):

```powershell
scp C:\cq-chaves\recipients.txt <operador>@<vm>:/tmp/recipients.txt
```

```bash
sudo install -d -m 0755 -o root -g root /etc/cq-backup
sudo install -m 0644 -o root -g root /tmp/recipients.txt /etc/cq-backup/recipients.txt
rm /tmp/recipients.txt
```

Conferência: `ls -l /etc/cq-backup/recipients.txt` mostra `-rw-r--r-- root root`, e o primeiro backup manual (guia
da VM, seção 10) termina com `Backup concluído.` (um arquivo inválido daria `O arquivo de destinatários do age está
ausente ou inválido.`).

## Restaurar numa pilha descartável

O ensaio da restauração (06/11, roteiro do ensaio) prova, com a cópia real, que um dump volta a ser um banco que a
API usa. Ele roda no Windows do Pedro, num projeto Compose **separado**, `cq-restauracao`, que nunca é o `cq` nem a
`cq-demo` (o `restore.py` recusa os dois sem `--production`). **Meta: até 30 minutos** do início ao `down -v`
final; anote o tempo no diário.

Por que só o `postgres` e a `api`: o processo de fundo restaurado, com a chave-mestra real, disputaria a
leitura de mensagens do bot do piloto com a pilha da VM e reexecutaria tarefas do dump (envios reais repetidos). Por
isso, nesta pilha:

- nunca rode `cq up` (ele sobe os quatro serviços);
- nunca suba o processo de fundo (o serviço `worker`);
- o arquivo de ambiente desta pilha **não tem nenhuma** `CQ_SMTP_*`.

**[passo do Pedro]** Preparar a pasta da pilha descartável, separada da sua cópia de trabalho (o exemplo usa
`C:\cq-restauracao\app`), com uma cópia do repositório:

```powershell
git clone <sua-cópia-local-do-repositório> C:\cq-restauracao\app
```

Conferência: `Test-Path C:\cq-restauracao\app\compose.yaml` responde `True`.

**[passo do Pedro]** Criar, com um editor, o arquivo de ambiente `C:\cq-restauracao\app\.env` com só estas linhas:
`CQ_DB_PASSWORD=<senha-nova-desta-pilha>`, `CQ_ADMIN_TOKEN=<token-novo-desta-pilha>`,
`CQ_MASTER_KEY=<chave-mestra-real>` e `CQ_API_PORT=18100` (uma porta livre, diferente da
8000 da sua pilha local e da 18000 do túnel). Não copie o arquivo da VM nem inclua nenhuma `CQ_SMTP_*`. A chave-mestra
real é o que prova que os tokens dos bots restaurados ainda se decifram.

Conferência: `Select-String -Quiet -Path C:\cq-restauracao\app\.env -Pattern '^CQ_SMTP_'` responde `False` (o
`-Quiet` só diz sim ou não, sem mostrar linha).

**[passo do Pedro]** Acrescentar ao arquivo de perfil do `cq` (`%APPDATA%\cq\config.toml`) um perfil só desta pilha,
com o token novo:

```toml
[profiles.restauracao]
kind = "local"
api_url = "http://127.0.0.1:18100"
token = "<o token novo desta pilha>"
stack_dir = "C:/cq-restauracao/app"
compose_project = "cq-restauracao"
```

Conferência: `cq --profile restauracao doctor` aponta para `http://127.0.0.1:18100` (a API ainda vai aparecer
inalcançável, porque nada subiu).

**[comando pronto]** Subir **só** o Postgres do projeto descartável (PowerShell, na pasta `C:\cq-restauracao\app`):

```powershell
docker compose -p cq-restauracao -f compose.yaml --env-file .env up -d --wait postgres
docker compose -p cq-restauracao -f compose.yaml --env-file .env ps
```

Conferência: o `ps` lista só o `postgres`, com o estado `healthy`.

**[comando pronto]** Restaurar o dump mais novo da cópia do Windows (o `restore.py` confere que só o Postgres do
projeto está no ar e que o banco está vazio; decifra em fluxo, sem arquivo em claro no disco; e para no primeiro erro,
desfazendo tudo):

```powershell
python ops/backup/restore.py --dump <pasta-da-cópia>\cq-<carimbo>.dump.age --identity C:\cq-chaves\cq-backup.agekey --project cq-restauracao --age-bin "$env:LOCALAPPDATA\cq-tools\age\age.exe"
```

Conferência: a saída é `Restauração concluída.` e o código de saída (`$LASTEXITCODE`) é 0. Qualquer outra linha é
uma das mensagens fixas da restauração (por exemplo, `O banco de destino não está vazio.` ou `A decifração do dump
falhou.`); nesse caso, rode o `down -v` do último passo e recomece.

**[comando pronto]** Subir a `api` sobre o banco restaurado, ainda sem o processo de fundo:

```powershell
docker compose -p cq-restauracao -f compose.yaml --env-file .env up -d postgres api
cq --profile restauracao doctor
```

Conferência: no `doctor`, `database.schema` e `bots.master_key` em "ok" (as migrações estão na versão esperada e os
tokens dos bots se decifram com a chave-mestra real); `cq --profile restauracao offerings list` mostra os
oferecimentos do piloto. São esperados: `service.worker` e `service.engine` em falha (não sobem aqui), e avisos de
backup e de e-mail não configurados nesta pilha.

**[comando pronto]** Derrubar a pilha descartável e apagar os volumes dela (só deste projeto):

```powershell
docker compose -p cq-restauracao -f compose.yaml --env-file .env down -v
docker ps -a --filter label=com.docker.compose.project=cq-restauracao
docker volume ls --filter label=com.docker.compose.project=cq-restauracao
```

Conferência: os dois últimos comandos não listam nada. Depois, apague o arquivo de ambiente desta pilha
(`Remove-Item C:\cq-restauracao\app\.env`), porque ele tem uma cópia da chave-mestra, e retire o perfil
`restauracao` do arquivo de perfil. Anote no diário o tempo total (meta: 30 minutos).

## Recuperar a pilha numa VM nova

Use quando a VM do piloto se perder ou quando uma atualização falhar (guia da VM, seção 16). A ideia: montar uma VM
nova pelo guia da VM, restaurar o dump mais novo da cópia do Windows no projeto `cq` dela e só então subir a pilha. O
`restore.py` só aceita o projeto `cq` com `--production` e continua exigindo o banco vazio e só o `postgres`
do projeto no ar. Perde-se o que entrou depois do último dump (até ~24 h).

**[passo do Pedro]** Preparar a VM nova pelo guia da VM, seções 1 a 8 (requisitos, SSH e firewall, usuário de
operação, Docker, pacote, conta de e-mail, arquivo de ambiente com a **mesma** `CQ_MASTER_KEY` guardada, limites do
motor). Não rode a seção 9 (subida) ainda.

Conferência: `ssh <operador>@<vm-nova> docker version` responde sem erro, e o arquivo de ambiente da pilha existe em
`/srv/cq/app` com modo `0600`.

**[comando pronto]** Na VM nova, subir **só** o Postgres do projeto `cq`:

```bash
cd /srv/cq/app
docker compose -p cq -f compose.yaml --env-file .env up -d --wait postgres
docker compose -p cq -f compose.yaml --env-file .env ps
```

Conferência: o `ps` lista só o `postgres`, com o estado `healthy`.

**[comando pronto]** No Windows, restaurar o dump mais novo da cópia no projeto `cq` da VM nova. O `docker` do
Windows fala com o Docker da VM pela ligação SSH (`DOCKER_HOST=ssh://…`), de modo que o dump e a chave privada nunca
saem do computador do Pedro; só o fluxo já decifrado passa pelo túnel cifrado do SSH. **[I]** Isso exige que o
usuário de operação esteja no grupo `docker` da VM (guia da VM, seção 4).

```powershell
$env:DOCKER_HOST = "ssh://<operador>@<vm-nova>"
docker ps
python ops/backup/restore.py --production --project cq --dump <pasta-da-cópia>\cq-<carimbo>.dump.age --identity C:\cq-chaves\cq-backup.agekey --age-bin "$env:LOCALAPPDATA\cq-tools\age\age.exe"
Remove-Item Env:DOCKER_HOST
```

Conferência: o `docker ps` mostra só o contêiner do Postgres da VM nova; a restauração imprime `Restauração
concluída.` com `$LASTEXITCODE` 0; depois do `Remove-Item`, `docker ps` volta a mostrar os contêineres locais.

**[comando pronto]** Na VM nova, subir a pilha inteira e diagnosticar (o perfil local `vm-local` foi criado pelo
`init_vm_env.py`):

```bash
cq up
cq doctor
```

Conferência: o `cq doctor` não tem falha fora do `backup.age` (na VM nova ainda não há registro de backup legível,
pois o backup ainda não rodou nela). Siga para o guia da VM, seções 10 a 12 (backup e agendamento, cópia no Windows com o host novo, túnel e perfil
remoto com o host novo), rode o backup manual e confira de novo: `backup.age` em "ok". Depois, faça tudo o que diz a
seção seguinte.

## Depois de restaurar

Vale para a recuperação numa VM nova (e, se quiser, para conferir a pilha descartável antes do `down -v`). O dump pode
ser de antes de um expurgo: os dados de um oferecimento expurgado depois do dump voltam com a restauração.

**[passo do Pedro]** Abrir o diário preenchido do piloto, que fica **fora do repositório**, junto da cópia dos
backups no Windows (o arquivo do repositório é só o modelo), e listar os códigos da seção
`## Expurgos realizados` com data igual ou posterior à do dump restaurado.

Conferência: você tem a lista de códigos (pode estar vazia) e não a copiou para nenhum arquivo do repositório nem para
o chat.

**[comando pronto]** Conferir a retenção no `cq doctor` da pilha restaurada:

```bash
cq doctor
```

Conferência: a linha `retention.overdue` mostra "ok" (`Nenhum oferecimento arquivado com prazo de retenção
vencido.`) ou um aviso com o código do oferecimento vencido mais antigo; um aviso aqui é um expurgo que precisa ser
refeito.

**[comando pronto]** Refazer os expurgos, um por código da lista do diário (e por código que o `doctor` apontar como
vencido), em duas fases:

```bash
cq offerings purge prepare <código>
cq offerings purge confirm <código> --confirmation <código-de-confirmação>
```

Conferência: cada `confirm` termina com sucesso (repetido num oferecimento já expurgado, ele só mostra o resultado
gravado); no fim, `cq doctor` mostra `retention.overdue` em "ok". Anote no diário preenchido, fora do repositório,
que os expurgos foram refeitos depois da restauração.

## Se uma chave se perder

**Sem a chave privada do age (nas duas cópias), os dumps cifrados para ela são irrecuperáveis.** Nada no sistema
guarda outra cópia. O banco vivo da VM continua íntegro; o que se perde é a capacidade de restaurar os dumps antigos.

**[passo do Pedro]** Gerar um par novo (seção `## Chaves do age`), guardar a privada nova em dois lugares e trocar o
`/etc/cq-backup/recipients.txt` da VM pela pública nova.

Conferência: o arquivo da VM tem só a linha `age1…` nova, e o backup manual termina com `Backup concluído.`.

**[comando pronto]** Rodar o backup manual e a cópia no Windows já com a chave nova, para voltar a ter um dump
restaurável:

```bash
sudo systemctl start cq-backup.service
journalctl -u cq-backup.service -n 5 --no-pager
```

Conferência: `Backup concluído.` no `journalctl` e, depois da cópia no Windows, `cq --profile piloto doctor` com
`backup.age` em "ok". Os dumps antigos (da chave perdida) podem ser descartados: não se decifram mais.

**Sem a `CQ_MASTER_KEY` (nas duas cópias), os tokens dos bots gravados no banco não se decifram.** O resto do banco
(oferecimentos, alunos, respostas) continua legível. A saída é uma chave-mestra nova e registrar os bots de novo pelo
CLI.

**[passo do Pedro]** Gerar uma chave-mestra nova (32 bytes aleatórios em base64url, sem `=`, 43 caracteres), pô-la no
arquivo de ambiente da VM com um editor (`nano /srv/cq/app/.env`, sem imprimir o arquivo) e guardá-la em dois lugares
seus, fora da VM e fora do git.

Conferência: `stat -c '%a' /srv/cq/app/.env` mostra `600`; a chave nova está nos seus dois lugares.

**[comando pronto]** Recriar a pilha com a chave nova e registrar de novo o bot de cada oferecimento, com o token do
bot obtido no BotFather e gravado num arquivo temporário seu (o token nunca vai na linha de comando):

```bash
cq down
cq up
cq bots register <código> --token-file <arquivo-com-o-token>
cq bots activate <código>
cq doctor
```

Conferência: no `cq doctor`, `bots.master_key` em "ok" e `bot.access` em "ok" para cada oferecimento; apague o
arquivo temporário com o token.
