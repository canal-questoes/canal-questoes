# Implantação na VM

Guia do Pedro para montar a VM do piloto, do zero até a pilha no ar com backup, cópia no Windows e túnel para o
agente.
O backup em si (como funciona, chaves do age, restauração) está no guia
[`backup-e-restauracao.md`](backup-e-restauracao.md); o início rápido do `cq` está em
[`inicio-rapido.md`](inicio-rapido.md).

Marcas usadas em cada passo:

- **[passo do Pedro]**: algo que só o Pedro faz (decidir, criar uma conta, guardar uma chave, digitar um segredo);
- **[comando pronto]**: um comando para copiar e colar, sem segredo na linha;
- cada passo termina com uma linha `Conferência:`, que diz como saber que deu certo.

Nos comandos, `<vm>` é o endereço da VM, `<usuário-inicial>` é o usuário com `sudo` e login por chave que a VM
traz (seção 1) e `<operador>` é o usuário de operação criado na seção 3. Os comandos de `bash` rodam na VM, logado
como `<operador>`, salvo nas seções 2 e 3, em que cada passo diz com qual usuário entrar (o `<operador>` ainda não
existe na seção 2); os de `powershell` rodam no Windows do Pedro, na pasta da cópia do repositório, salvo quando o
passo diz outra pasta.

Regras que valem para o guia inteiro:

- Os segredos (a `CQ_MASTER_KEY`, a senha do banco, o token de administração, a senha de app do Gmail, os tokens dos
  bots e do agente, a chave privada do age) são gerados ou colados só na VM, nunca no chat do agente, numa mensagem,
  num documento, num registro ou numa linha de comando. Quem os digita é o Pedro, num prompt sem eco.
- As duas chaves que recuperam o piloto (a chave privada do age, que decifra os dumps, e a `CQ_MASTER_KEY`, que
  decifra os tokens dos bots no banco) ficam com o Pedro, fora da VM, em dois lugares sob o controle dele. Uma
  sem a outra não basta.
- O agente opera só a API, pelo túnel SSH da seção 12, com um token próprio e sem permissão de criar tokens. A pilha
  da VM (subir, parar, registros, backup, atualização) é do Pedro, por SSH, com o `cq` instalado na VM e o perfil
  local de lá (`vm-local`, criado na seção 7).
- Se outra pessoa precisar entrar na VM (o orientador, um técnico do IC), ela precisa de acesso SSH dado pelo Pedro:
  a chave pública dela entra no `authorized_keys` de um usuário dela, e o nome entra no `AllowUsers` da seção 2.
  Ninguém recebe senha nem a chave do Pedro.
- Nenhum comando deste guia imprime a configuração interpolada do Compose nem o conteúdo do arquivo de ambiente da
  pilha: os dois mostram segredos. Arquivos com segredo são abertos só num editor, nunca no terminal.
- Toda chamada direta ao Compose leva `-p cq -f compose.yaml --env-file .env` explícitos: sem `-p`, o nome do projeto
  vem da pasta, e uma subida na pasta errada criaria um banco vazio com outro nome. No dia a dia,
  use o `cq` (`cq up`, `cq status`, `cq logs`, `cq down`).

**[I]** Os comandos de instalação de pacotes (Docker, `uv`, `unattended-upgrades`, swap) seguem a documentação
oficial de memória; confira cada um na página citada antes de rodar. Os fatos conferidos estão marcados
com **[F]**.

## 1. Requisitos da VM

A VM do piloto:

| Item | Mínimo |
| --- | --- |
| Sistema | Ubuntu 24.04 LTS (64 bits, x86-64) |
| Processador | 2 vCPU |
| Memória | 4 GB de RAM (mais 2 GB de swap, seção 8) |
| Disco | 40 GB |
| Rede | IP fixo ou nome DNS; entrada só pela porta 22 (SSH); saída para o Docker Hub, o `ghcr.io`, o PyPI, o GitHub e o `smtp.gmail.com` na 587 |
| Acesso | login por chave SSH num usuário com `sudo` |

**[passo do Pedro]** Pedir a VM ao IC (Ubuntu 24.04, 2 vCPU, 4 GB, 40 GB, acesso por chave SSH). Se ela não vier até
02/11, criar uma VM com os mesmos requisitos num provedor de nuvem, na região de São Paulo. Mandar só a chave
**pública** (`C:\Users\<você>\.ssh\id_ed25519.pub`), nunca a privada.

Conferência: do Windows, `ssh <usuário-inicial>@<vm>` entra sem pedir senha, e na VM `lsb_release -d` mostra
`Ubuntu 24.04`, `nproc` mostra `2` (ou mais), `free -h` mostra cerca de 4 GB em `Mem` e `df -h /` mostra cerca de
40 GB.

## 2. Endurecimento do SSH e do firewall

Objetivo: login só por chave, sem senha e sem `root`; só o SSH aberto para fora; atualizações de segurança
automáticas; relógio sincronizado. **[F]** Com o ufw, portas **publicadas** pelo Docker contornam o firewall
(docs.docker.com, instalação no Ubuntu); a API só publica em `127.0.0.1` (`compose.yaml`), por isso o contorno não a
expõe, e o guia confere isso de fora e de dentro.

Toda esta seção roda como `<usuário-inicial>`: o `<operador>` só é criado na seção 3.

**[passo do Pedro]** Antes de mexer no SSH, abrir **uma segunda sessão** SSH na VM, como `<usuário-inicial>`
(`ssh <usuário-inicial>@<vm>`), e deixá-la aberta até o fim desta seção. Se a configuração nova trancar a porta, a
segunda sessão ainda está dentro para desfazer.

Conferência: duas janelas do terminal conectadas à VM ao mesmo tempo, as duas como `<usuário-inicial>`.

**[comando pronto]** Gravar a configuração do SSH em `/etc/ssh/sshd_config.d/10-cq.conf`. Antes de colar, troque
`<usuário-inicial>` pelo usuário com que você entrou e `<operador>` pelo nome que você vai criar na seção 3 (decida-o
agora). O `AllowUsers` leva os três nomes: o `<usuário-inicial>`, que é o único que já existe e tem a chave, fica na
lista até a seção 3, que o tira depois de conferir uma sessão nova do operador; sem ele, a recarga do `sshd` trancaria
de fora o único usuário da VM. O `cqbackup` é o usuário da cópia, criado na seção 10. **[I]** Um nome no `AllowUsers`
sem usuário criado (o `<operador>` até a seção 3, o `cqbackup` até a seção 10) não abre nenhuma entrada: o `sshd` só
autentica usuários que existem, e o `cqbackup` só ganha chave na seção 11.

```bash
sudo tee /etc/ssh/sshd_config.d/10-cq.conf > /dev/null <<'EOF'
# canal-questoes (docs/operacao/implantacao-vm.md, seção 2)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers <usuário-inicial> <operador> cqbackup

Match User cqbackup
    ChrootDirectory /var/backups/cq
    ForceCommand internal-sftp -R
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
EOF
sudo sshd -t && sudo systemctl reload ssh
```

O prefixo `10-` faz este arquivo ser lido antes dos outros do diretório; no `sshd`, vale o primeiro valor lido de
cada opção, então um `50-cloud-init.conf` com `PasswordAuthentication yes` não o desfaz (**[I]** regra do
`sshd_config(5)`, a conferir abaixo). O bloco `Match User cqbackup` prende o usuário da cópia ao SFTP só de leitura
(**[F]** `sshd_config(5)` e `sftp-server(8)`) e, pelo `ChrootDirectory`, ao diretório dos backups,
`/var/backups/cq` (a "jaula"): lá dentro, ele vê esse diretório como `/` e não lê os arquivos legíveis por
todos fora dele, como o código da aplicação. **[F]** O `ChrootDirectory` vale em blocos `Match`.
**[I]** Pelo `sshd_config(5)`, de memória (a página não foi conferida): o `sshd` só aceita a
jaula se o diretório e todos os que estão acima dele forem do `root` e sem escrita para o grupo e os outros (a seção
10 deixa o `/var/backups/cq` assim); o `internal-sftp` não precisa de nenhum arquivo dentro da jaula; e, sem a pasta
pessoal do `cqbackup` lá dentro, a sessão começa no `/` da jaula. O `sshd` só confere o diretório quando o `cqbackup`
abre uma sessão (seção 11), por isso ele ainda não precisa existir agora.

Conferência: o `sshd -t` não imprime nada; `sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication)'`
mostra os três com `no`; `sudo sshd -T -C user=cqbackup,host=x,addr=127.0.0.1 | grep -Ei '^(forcecommand|chrootdirectory)'`
mostra `forcecommand internal-sftp -R` e `chrootdirectory /var/backups/cq` (**[I]** o `-C` só compara o nome com o
`Match User`, e o `cqbackup` ainda não precisa existir); `sudo sshd -T -C user=<usuário-inicial>,host=x,addr=127.0.0.1 | grep -Ei '^(forcecommand|chrootdirectory)'`
mostra `forcecommand none` e `chrootdirectory none` (o bloco do `cqbackup` não pegou outro usuário; a mesma
conferência com o `<operador>` vem na seção 3); e uma **terceira** sessão SSH nova, como `<usuário-inicial>`
(`ssh <usuário-inicial>@<vm>`), entra normalmente. Só então feche a segunda.

**[comando pronto]** Ligar o firewall, deixando entrar só o SSH:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status verbose
```

Conferência: o `status verbose` mostra `Status: active`, `Default: deny (incoming)` e uma só regra de entrada,
`OpenSSH` (`22/tcp`, com a cópia em IPv6). Uma sessão SSH nova, como `<usuário-inicial>`, continua entrando.

**[comando pronto]** Atualizações de segurança automáticas e relógio sincronizado:

```bash
sudo apt-get update
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
timedatectl
```

Conferência: o `dpkg-reconfigure` termina com "Yes" para as atualizações automáticas; `systemctl is-enabled unattended-upgrades`
mostra `enabled`; o `timedatectl` mostra `System clock synchronized: yes` e `NTP service: active`
(o `cq doctor` confere o desvio do relógio).

**[comando pronto]** Conferir as portas abertas, na VM e de fora. Rode agora e de novo depois da seção 9, com a pilha
no ar:

```bash
ss -ltn
```

```powershell
Test-NetConnection <vm> -Port 22
Test-NetConnection <vm> -Port 8000
```

Conferência: na VM, o `ss -ltn` mostra a porta 22 e, depois da seção 9, a 8000 **só** como `127.0.0.1:8000`, nunca
`0.0.0.0:8000` nem `*:8000`. No Windows, o `Test-NetConnection` da porta 22 dá `TcpTestSucceeded : True`, e o da porta
8000 tem de falhar (`TcpTestSucceeded : False`): se ele passar, a API está exposta, e a pilha não pode ficar no ar.

## 3. Usuário de operação

O usuário de operação é quem roda o `cq`, o Docker e a rotina de backup na VM. **[F]** O grupo `docker` dá poderes
equivalentes aos de `root` (docs.docker.com, pós-instalação): por isso ele é um usuário só do piloto, com login só
por chave, e o usuário da cópia (`cqbackup`, seção 10) fica fora desse grupo.

Questões de autores não confiáveis exigem um usuário separado para o código da questão dentro do motor:
é trabalho futuro e não existe nesta versão. No piloto, só entram questões de autores confiáveis, de
repositórios da lista `CQ_REPO_ALLOWED_HOSTS` (seção 5); o usuário de operação não substitui esse isolamento.

**[comando pronto]** Numa sessão como `<usuário-inicial>`, criar o usuário de operação com o mesmo nome posto no
`AllowUsers` da seção 2 (troque `<operador>` por ele) e copiar para ele a chave SSH autorizada do usuário inicial (o
`~` abaixo é o do `<usuário-inicial>`):

```bash
sudo adduser --disabled-password --gecos "" <operador>
sudo usermod -aG sudo <operador>
sudo install -d -m 0700 -o <operador> -g <operador> /home/<operador>/.ssh
sudo install -m 0600 -o <operador> -g <operador> ~/.ssh/authorized_keys /home/<operador>/.ssh/authorized_keys
```

Conferência: do Windows, uma sessão nova como `<operador>` (`ssh <operador>@<vm>`) entra sem senha, e nela `id`
mostra o grupo `sudo`.

**[passo do Pedro]** Como o usuário não tem senha, o `sudo` dele precisa de uma: defina-a, na sessão do
`<usuário-inicial>`, com `sudo passwd <operador>` (digitada só no prompt, sem eco) e guarde-a no seu gerenciador de
senhas. Ela não abre o SSH (seção 2), só o `sudo`.

Conferência: numa sessão como `<operador>`, `sudo -v` pede a senha e aceita.

**[comando pronto]** Tirar o `<usuário-inicial>` do `AllowUsers`, agora que o `<operador>` entra e tem `sudo`. Deixe
aberta, até a conferência abaixo, uma sessão como `<usuário-inicial>` (a reserva para desfazer) e rode os comandos
numa sessão como `<operador>` (troque `<operador>` pelo nome dele antes de colar):

```bash
sudo sed -i 's/^AllowUsers .*/AllowUsers <operador> cqbackup/' /etc/ssh/sshd_config.d/10-cq.conf
sudo sshd -t && sudo systemctl reload ssh
```

Conferência: o `sshd -t` não imprime nada; `sudo sshd -T | grep -i '^allowusers'` mostra só o `<operador>` e o
`cqbackup` (**[I]** uma linha `allowusers` por nome), sem o `<usuário-inicial>`;
`sudo sshd -T -C user=<operador>,host=x,addr=127.0.0.1 | grep -Ei '^(forcecommand|chrootdirectory)'` mostra
`forcecommand none` e `chrootdirectory none` (o bloco do `cqbackup`, com a jaula, não pegou o operador); uma sessão
SSH nova como `<operador>` (`ssh <operador>@<vm>`) entra normalmente; e uma nova como `<usuário-inicial>` (`ssh <usuário-inicial>@<vm>`) é recusada (`Permission denied`). Só então feche a
sessão de reserva. Daqui em diante, todo comando de `bash` roda como `<operador>`.

## 4. Docker Engine e Compose

Versões mínimas: Docker Engine **26.0.0** (a correção da CVE-2024-29018 no DNS da rede interna do motor, citada no
cabeçalho do `compose.yaml`) e Compose **2.27.0** (o mínimo sem aviso do `cq doctor`). O Docker vem do repositório
oficial da Docker, não do pacote `docker.io` do Ubuntu.

**[comando pronto]** Instalar pelo repositório oficial (**[I]** os passos da página "Install Docker Engine on Ubuntu"
de docs.docker.com; confira lá antes de colar):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker <operador>
```

Saia e entre de novo no SSH para o grupo `docker` valer.

Conferência: `docker version --format '{{.Server.Version}}'` mostra 26.0.0 ou mais; `docker compose version` mostra
2.27.0 ou mais; `docker run --rm hello-world` termina com "Hello from Docker!".

## 5. Pacote do sistema

O código chega à VM pelo **pacote de implantação**: um `.tar.gz` gerado no Windows a partir de um commit,
só com os arquivos da lista positiva (`ops/export/allowlist.txt`), sem `.git`, sem arquivo de ambiente e sem dados de
alunos, com `SOURCE_COMMIT` (o commit de origem) e `BUNDLE_SHA256SUMS` (o resumo de cada arquivo). A VM constrói as
imagens das mesmas bases fixadas por digest.

**[comando pronto]** No Windows, gerar o pacote do commit atual, fora do repositório (o script recusa se algum arquivo
da lista tiver alteração não commitada):

```powershell
New-Item -ItemType Directory -Force C:\cq-pacotes | Out-Null
git rev-parse --short HEAD
uv run python ops/deploy/make_bundle.py --out C:\cq-pacotes\cq-bundle-<commit curto>.tar.gz
scp C:\cq-pacotes\cq-bundle-<commit curto>.tar.gz <operador>@<vm>:/tmp/
```

Conferência: o script termina com código 0 (`$LASTEXITCODE`), e o `scp` copia o arquivo sem erro.

**[comando pronto]** Na VM, abrir o pacote em `/srv/cq/app` e conferir os resumos:

```bash
sudo install -d -m 0755 -o "$USER" -g "$USER" /srv/cq /srv/cq/app
tar -xzf /tmp/cq-bundle-<commit curto>.tar.gz -C /srv/cq/app
cd /srv/cq/app
sha256sum --check --quiet BUNDLE_SHA256SUMS && echo resumos-ok
head -c 40 SOURCE_COMMIT; echo
rm /tmp/cq-bundle-<commit curto>.tar.gz
```

Conferência: aparece `resumos-ok`; os 40 caracteres do `SOURCE_COMMIT` são os mesmos de `git rev-parse HEAD` no
Windows; `ls /srv/cq/app` mostra `compose.yaml`, `client`, `server`, `engine` e `ops`. **[I]** Se o pacote tiver uma
pasta no topo, use essa pasta como `/srv/cq/app`.

**[comando pronto]** Instalar o `uv` 0.11.16 (a mesma versão do `Dockerfile`) e o `cq` na VM (**[I]** o instalador
oficial em docs.astral.sh/uv; confira lá):

```bash
curl -LsSf https://astral.sh/uv/0.11.16/install.sh | sh
exec bash -l
cd /srv/cq/app
uv tool install ./client
```

Conferência: `uv --version` mostra `0.11.16`; `which cq` mostra um caminho em `~/.local/bin`; `cq --help` lista os
comandos.

**[passo do Pedro]** Decidir a lista de hosts git permitidos, `CQ_REPO_ALLOWED_HOSTS`: os hosts dos
repositórios do plano de disparos (`docs/operacao/piloto/plano-de-disparos.toml`), separados por vírgula. No piloto,
`github.com` (o repositório de demonstração e, se chegarem, as questões do Rodolfo). O valor entra no arquivo
de ambiente pelo `--repo-hosts` da seção 7; o worker recusa obter questões de outro host.

Conferência: cada `repo` do plano de disparos tem o host na lista.

## 6. Conta de e-mail do projeto

Os convites saem por uma conta Gmail **só do projeto**, não pela conta pessoal do Pedro. **[F]** O SMTP do
Gmail é `smtp.gmail.com`, com TLS na porta 587, autenticado pela conta e uma senha de app (Google Workspace, "Send
email from a printer, scanner, or app", lida em 2026-10-29).

**[passo do Pedro]** Criar a conta Gmail do projeto e ligar nela a verificação em duas etapas (myaccount.google.com,
"Segurança"). Sem a verificação em duas etapas, o Google não oferece a senha de app.

Conferência: a página "Segurança" da conta mostra a verificação em duas etapas ativada.

**[passo do Pedro]** Gerar uma senha de app para o canal-questoes (myaccount.google.com, "Senhas de app"; **[I]** o
nome do menu pode variar). O Google mostra a senha de app uma única vez: não a copie para o chat, para um documento
ou para um arquivo; ela vai direto ao prompt sem eco do `init_vm_env.py` na seção 7. Se ela se perder, revogue e gere
outra.

Conferência: a lista de senhas de app da conta mostra uma entrada com o nome escolhido.

Os valores **não secretos** do e-mail, que o `init_vm_env.py` escreve sozinho no arquivo de ambiente:

```text
CQ_SMTP_HOST=smtp.gmail.com
CQ_SMTP_PORT=587
CQ_SMTP_SECURITY=starttls
```

O usuário da conta vai também a `CQ_SMTP_FROM`. O usuário e a senha de app (`CQ_SMTP_USERNAME` e `CQ_SMTP_PASSWORD`)
são pedidos pelo script, sem eco, e nunca aparecem neste guia.

**[comando pronto]** Conferir, da VM, que a saída para o SMTP do Gmail está aberta:

```bash
timeout 10 bash -c '</dev/tcp/smtp.gmail.com/587' && echo smtp-alcancavel
```

Conferência: aparece `smtp-alcancavel`. Se não aparecer, a rede da VM bloqueia a porta 587: peça a liberação ao IC
antes de seguir.

## 7. Arquivo .env

O arquivo de ambiente da pilha (`/srv/cq/app/.env`) é criado pelo `ops/deploy/init_vm_env.py`, rodado pelo Pedro
**na VM**. O script recusa sobrescrever um arquivo existente; gera a senha do banco e o token
de administração; pede por `getpass` (sem eco) a chave-mestra, o usuário e a senha de app do Gmail; escreve os
valores não secretos (Gmail, hosts permitidos, limites do motor, `CQ_BACKUP_STATUS_DIR=/var/backups/cq/state` e
`CQ_BACKUP_STATUS_FILE=/run/cq-backup/last-backup.json`); cria o arquivo com modo `0600`; e grava o perfil
local da VM, `~/.config/cq/config.toml` (`0600`), com `default_profile = "vm-local"` e o token. Ele só imprime
mensagens fixas: nenhum valor gerado ou digitado aparece.

**[passo do Pedro]** Gerar a chave-mestra no **Windows**, direto num arquivo seu (fora de qualquer cópia git), sem
imprimi-la:

```powershell
New-Item -ItemType Directory -Force C:\cq-chaves | Out-Null
uv run python -c "import secrets, sys; open(sys.argv[1], 'x').write(secrets.token_urlsafe(32))" C:\cq-chaves\cq-master-key.txt
```

Guarde esse arquivo em **dois lugares** seus, fora da VM e fora do git (por exemplo, a pasta `C:\cq-chaves` e um
gerenciador de senhas), junto da chave privada do age ([`backup-e-restauracao.md`](backup-e-restauracao.md), "Chaves
do age"). Na recuperação numa VM nova, a mesma chave-mestra tem de voltar.

Conferência: o arquivo existe e tem 43 caracteres (`(Get-Item C:\cq-chaves\cq-master-key.txt).Length` mostra `43`),
sem que você tenha visto o conteúdo no terminal.

**[passo do Pedro]** Na VM, criar o arquivo de ambiente. Abra o `cq-master-key.txt` no Bloco de Notas do Windows,
copie o conteúdo e cole **só** no prompt sem eco do script; depois feche o Bloco de Notas sem salvar nada:

```bash
cd /srv/cq/app
python3 ops/deploy/init_vm_env.py --stack-dir /srv/cq/app --repo-hosts github.com
```

O script pede, nesta ordem e sem eco: a chave-mestra, o usuário da conta Gmail do projeto e a senha de app. Os limites
do motor já vêm com os valores da VM de 2 vCPU e 4 GB (seção 8); para outros, use `--engine-mem` e `--engine-cpus`.

Conferência: o script imprime `Arquivo .env e perfil local criados.` e termina com código 0;
`stat -c '%a %U' /srv/cq/app/.env` mostra `600 <operador>`; `stat -c '%a' ~/.config/cq/config.toml` mostra `600`.
Se ele disser `A chave-mestra deve ter 43 caracteres base64url; nada foi escrito.`, copie de novo, sem espaço nem
quebra de linha. Se disser `O arquivo .env já existe; nada foi alterado.`, o arquivo de uma tentativa anterior
continua valendo.

As variáveis da implantação e do diagnóstico. Todas são opcionais, nenhuma é segredo, vazia vale como ausente
(o padrão), e um valor inválido faz o serviço recusar a partida com uma mensagem fixa (`config.invalid`). Só as duas do
backup são escritas pelo script; as demais ficam no padrão, e só mudam por decisão explícita do Pedro:

| Variável | Padrão | Quem lê | Para que serve |
| --- | --- | --- | --- |
| `CQ_DOCTOR_BACKUP_WARNING_HOURS` | 26 | `api` (o `doctor` do servidor) | idade do último backup que dá "aviso" |
| `CQ_DOCTOR_BACKUP_FAILURE_HOURS` | 50 | `api` | idade do último backup que dá "falha" |
| `CQ_DOCTOR_MAIL_STALE_HOURS` | 13 | `api` | idade máxima do último teste do servidor de e-mail |
| `CQ_RETENTION_DAYS` | 30 | `api` | prazo de retenção dos oferecimentos arquivados (aviso de prazo vencido) |
| `CQ_BACKUP_STATUS_FILE` | vazio (backup não configurado) | `api` | caminho do registro do último backup dentro do contêiner; na VM, `/run/cq-backup/last-backup.json` |
| `CQ_BACKUP_STATUS_DIR` | `./ops/backup/status-unconfigured` | Compose (montagem só leitura na `api`) | diretório de estado do backup no host; na VM, `/var/backups/cq/state` |
| `CQ_DOCTOR_BOT_POLL_SECONDS` | o do servidor | `api` | atraso máximo da leitura do bot (também chega pelo Compose) |
| `CQ_DOCTOR_TASK_OVERDUE_SECONDS` | o do servidor | `api` | atraso máximo de uma tarefa (idem) |
| `CQ_DOCTOR_CLOCK_SKEW_SECONDS` | o do servidor | `api` | desvio máximo do relógio (idem) |
| `CQ_MAIL_PROBE_INTERVAL_SECONDS` | 21600 | `worker` | intervalo do teste do servidor de e-mail (6 h) |
| `CQ_DOCTOR_DISK_WARNING_PERCENT` | 20 | `cq doctor` (ambiente do `cq`, fora do Compose) | espaço livre que dá "aviso" |
| `CQ_DOCTOR_DISK_FAILURE_PERCENT` | 10 | `cq doctor` (ambiente do `cq`) | espaço livre que dá "falha" |
| `CQ_BACKUP_DIR` | vazio (só o diretório do Docker) | `cq doctor` (ambiente do usuário de operação, seção 10) | diretório dos backups, para a conferência do disco |

## 8. Limites do motor

A VM tem 2 vCPU e 4 GB. O motor (que roda o código das questões) recebe limites próprios pelas variáveis do
`compose.yaml`, que o `init_vm_env.py` já escreve com os valores desta VM:

| Variável | Valor na VM | Padrão do `compose.yaml` |
| --- | --- | --- |
| `CQ_ENGINE_MEM_LIMIT` | 1536m | 2g |
| `CQ_ENGINE_CPUS` | 1.5 (1,5 CPU) | 2 |
| `CQ_ENGINE_WORKERS` | 2 (o padrão; não vai ao arquivo) | 2 |

Sobra memória para a `api`, o `worker` e o `postgres`; o swap de 2 GB evita que um pico de memória derrube a pilha,
ao custo de lentidão. **[F]** A `cq-demo` usa cerca de 630 MB parada.

**[comando pronto]** Criar o swap de 2 GB (**[I]** o procedimento usual do Ubuntu; confira na documentação dele):

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Conferência: `swapon --show` mostra `/swapfile` com `2G`; `free -h` mostra `Swap` com cerca de 2,0 Gi; depois de um
reinício da VM, o swap continua ativo.

**[comando pronto]** Depois da seção 9, com a pilha no ar, medir o uso real e anotar no diário do piloto (fora do
repositório):

```bash
docker stats --no-stream
```

Conferência: a coluna `MEM USAGE / LIMIT` do contêiner do motor mostra o limite `1.5GiB`; a soma do uso dos quatro
contêineres fica abaixo de 4 GB; a linha foi anotada no diário com a data.

## 9. Subida e conferência

**[comando pronto]** Na VM, subir a pilha pela primeira vez (constrói as imagens; pode levar vários minutos):

```bash
cd /srv/cq/app
cq up --rebuild --timeout 600
cq status
```

Conferência: o `cq up` termina com código 0; o `cq status` mostra o perfil `vm-local`, os quatro serviços no ar e a
API saudável.

**[comando pronto]** Diagnosticar:

```bash
cq doctor
```

Até o primeiro backup (seção 10), a única falha esperada é `backup.age`, com a mensagem "Sem registro legível do último
backup." (o diretório de estado ainda não tem registro). Qualquer outra falha precisa ser resolvida antes de
seguir (seção 15). O servidor de e-mail pode aparecer como "aviso" até o primeiro teste do `worker`.

Conferência: o `cq doctor` não tem `[falha]` fora do `backup.age`.

**[comando pronto]** Registrar os IDs das imagens construídas, para saber depois o que estava no ar:

```bash
docker compose -p cq -f compose.yaml --env-file .env images
```

Conferência: a saída lista as imagens da `api`, do `worker`, do `postgres` e do `engine` com o ID de cada uma, e a
lista foi anotada no diário, junto do `SOURCE_COMMIT` do pacote.

**[comando pronto]** Repetir a conferência das portas da seção 2 (`ss -ltn` na VM e os dois `Test-NetConnection` no
Windows).

Conferência: a 8000 aparece só como `127.0.0.1:8000` na VM e falha de fora.

## 10. Backup e agendamento

Como a rotina funciona está em [`backup-e-restauracao.md`](backup-e-restauracao.md), "Como funciona". Aqui ela é
instalada: o `age` do Ubuntu, a chave pública, os diretórios, o usuário da cópia, as unidades do systemd e o primeiro
backup. Com um dump por dia, a perda máxima é de até ~24 h de respostas; depois de cada sessão intensiva, o
Pedro roda o backup manual e a cópia no Windows (plano de disparos).

**[comando pronto]** Instalar o `age` do Ubuntu e conferir a versão (**[F]** `1.1.1` no Ubuntu 24.04; um dump cifrado
por ele decifra no `age` 1.3.2 do Windows):

```bash
sudo apt-get install -y age
age --version
```

Conferência: o `age --version` mostra `1.1.1` (ou mais novo).

**[passo do Pedro]** Gerar o par de chaves do age no Windows, guardar a privada em dois lugares e copiar **só a
pública** para `/etc/cq-backup/recipients.txt`, como manda a seção "Chaves do age" de
[`backup-e-restauracao.md`](backup-e-restauracao.md).

Conferência: `ls -l /etc/cq-backup/recipients.txt` mostra `-rw-r--r-- root root`.

**[comando pronto]** Criar o usuário da cópia, `cqbackup`, fora do grupo `docker` e sem shell (ele só entra pelo SFTP
só de leitura da seção 2):

```bash
sudo adduser --disabled-password --gecos "" --shell /usr/sbin/nologin cqbackup
id cqbackup
```

Conferência: o `id cqbackup` mostra só o grupo `cqbackup` (sem `docker` e sem `sudo`).

**[comando pronto]** Criar o diretório dos backups, `/var/backups/cq`, em `0755` e do `root` (`root:root`): ele é a
jaula do `cqbackup` (seção 2), e o `sshd` só a aceita se ela e todos os diretórios acima dela forem do `root`
e sem escrita para o grupo e os outros (**[I]** `sshd_config(5)`, ver a seção 2). **[F]** A rotina não grava nele:
ela grava os temporários, os dumps e o `SHA256SUMS` em `dumps/`, e o registro e a trava em `state/`, e não cria
diretórios (`ops/backup/cq_backup.py`); esses dois subdiretórios continuam do usuário de operação, nos passos abaixo.
**[I]** A subida da seção 9 pode já ter criado esse diretório como `root` (o Docker cria a origem ausente de uma
montagem); os comandos abaixo valem nos dois casos.

```bash
sudo mkdir -p /var/backups/cq
sudo chown root:root /var/backups/cq
sudo chmod 0755 /var/backups/cq
stat -c '%a %U:%G %n' / /var /var/backups /var/backups/cq
```

Conferência: o `stat -c` mostra `755 root:root` nas quatro linhas (o `/`, o `/var`, o `/var/backups` e o
`/var/backups/cq`). Se alguma mostrar outro dono ou um modo com escrita para o grupo ou os outros, a cópia da seção 11
cai (seção 15).

**[comando pronto]** Ajustar o diretório de estado, `/var/backups/cq/state`, em `0755`: a rotina grava nele o
`last-backup.json`, e a `api` o lê pela montagem só leitura com o uid 999 (`Dockerfile`), por isso ele não pode
fechar a leitura. **[I]** A subida da seção 9 pode já ter criado esse diretório como `root` (o Docker cria a origem
ausente de uma montagem); o `chown` abaixo o corrige.

```bash
sudo mkdir -p /var/backups/cq/state
sudo chown "$USER:$USER" /var/backups/cq/state
sudo chmod 0755 /var/backups/cq/state
stat -c '%a %U:%G' /var/backups/cq/state
```

Conferência: o `stat -c` mostra `755 <operador>:<operador>`.

**[comando pronto]** Criar o diretório dos dumps, `/var/backups/cq/dumps`, com o grupo `cqbackup` e o bit setgid
(`2750`): com o setgid, todo dump novo nasce com o grupo `cqbackup`, que o lê pela cópia; os outros usuários
não entram.

```bash
sudo mkdir -p /var/backups/cq/dumps
sudo chown "$USER:cqbackup" /var/backups/cq/dumps
sudo chmod 2750 /var/backups/cq/dumps
stat -c '%a %U:%G' /var/backups/cq/dumps
```

Conferência: o `stat -c` mostra `2750 <operador>:cqbackup`.

**[F]** O que o `cqbackup` vê dentro da jaula: o `/dumps` (os dumps e o `SHA256SUMS`, lidos pelo grupo `cqbackup`) e
o `/state`, o diretório de estado em `0755`, com o registro `last-backup.json` (`0644`) e a trava `backup.lock`
(`0600`: só o nome fica visível), ou seja, só metadados do backup, sem segredo (`ops/backup/cq_backup.py`). O estado
fica dentro da jaula porque os dois caminhos, `/var/backups/cq/dumps` e `/var/backups/cq/state`, estão fixados pelas
linhas `Environment=` de `ops/backup/cq-backup.service` (e pelos testes do repositório). Fora da jaula, ele não
vê mais nada da VM: nem o código em `/srv/cq/app`, nem as pastas pessoais, nem o `/etc`.

**[comando pronto]** Instalar as unidades do systemd, com o marcador de usuário da linha `User=` do serviço trocado
pelo usuário de operação, e ligar o agendamento (todo dia às 03:30 UTC):

```bash
cd /srv/cq/app
sed "s/^User=.*/User=$USER/" ops/backup/cq-backup.service | sudo tee /etc/systemd/system/cq-backup.service > /dev/null
sudo install -m 0644 ops/backup/cq-backup.timer /etc/systemd/system/cq-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cq-backup.timer
systemctl list-timers cq-backup.timer
```

As unidades não leem o arquivo de ambiente da pilha: cada parâmetro da rotina vem de uma linha `Environment=` do
serviço, com valor público. O arquivo de ambiente da pilha não é tocado nesta seção (o `init_vm_env.py` já escreveu nele
`CQ_BACKUP_STATUS_DIR` e `CQ_BACKUP_STATUS_FILE`).

Conferência: `systemctl cat cq-backup.service | grep '^User='` mostra `User=<operador>`, sem o marcador; o
`list-timers` mostra o `cq-backup.timer` com o próximo disparo.

**[comando pronto]** Definir `CQ_BACKUP_DIR=/var/backups/cq` no ambiente do usuário de operação, para o `cq doctor`
medir também o disco dos backups (`disk.space`). **[F]** O `cq doctor` só mede o disco desse diretório
(`shutil.disk_usage`, em `client/src/cq_client/host_probe.py`), sem gravar nele; por isso o `/var/backups/cq` ser do
`root` não atrapalha. Vai ao `~/.profile`, não ao arquivo de ambiente da pilha:

```bash
echo 'export CQ_BACKUP_DIR=/var/backups/cq' >> ~/.profile
exec bash -l
```

Conferência: `echo "$CQ_BACKUP_DIR"` mostra `/var/backups/cq`, também numa sessão SSH nova.

**[comando pronto]** Rodar o primeiro backup à mão e conferir:

```bash
sudo systemctl start cq-backup.service
journalctl -u cq-backup.service -n 20 --no-pager
ls -l /var/backups/cq/dumps
cq doctor
```

Depois do primeiro backup à mão, o `cq doctor` tem de mostrar `backup.age` em "ok", com a idade em horas próxima de 0
logo depois; a falha "Sem registro legível do último backup." da seção 9 some.

Conferência: o `journalctl` mostra `Backup concluído.`; o `ls -l` mostra um `cq-<carimbo>.dump.age` com
`-rw-r----- <operador> cqbackup` e o `SHA256SUMS`; o `cq doctor` mostra `backup.age` em `[ok]` e nenhuma `[falha]`.

## 11. Cópia no Windows

A cópia traz os dumps da VM para o computador do Pedro todo dia e a cada logon (`ops/backup/pull_backups.py`).
Ela entra como `cqbackup`, com uma chave SSH dedicada só a isso (não a chave pessoal do
Pedro): se ela vazar, só dá leitura do diretório dos backups (os dumps cifrados e os metadados do estado, seção 10),
pelo SFTP só de leitura e preso nesse diretório (seção 2). Dentro da jaula, o `/var/backups/cq` da VM aparece
como `/`, então o diretório dos dumps é `/dumps`: é esse o caminho do `--remote-dir` da cópia e do `ls` da conferência
abaixo. A cópia confere o resumo de cada dump, mantém 14 dias e nunca apaga nada na VM. **[F]** Ela busca o `SHA256SUMS` da VM num diretório
temporário, confere cada dump novo contra ele e descarta o temporário (`ops/backup/pull_backups.py`, função `pull`):
a pasta local `C:\cq-backups\dumps` fica só com os dumps (`cq-<carimbo>.dump.age`), sem o
`SHA256SUMS`.

**[passo do Pedro]** Gerar a chave dedicada no Windows, sem frase-senha (a tarefa agendada roda sem ninguém para
digitá-la; **[I]** aperte Enter duas vezes quando o `ssh-keygen` pedir):

```powershell
ssh-keygen -t ed25519 -f C:\cq-chaves\cq-pull -C cq-pull
scp C:\cq-chaves\cq-pull.pub <operador>@<vm>:/tmp/cq-pull.pub
```

Conferência: existem `C:\cq-chaves\cq-pull` e `C:\cq-chaves\cq-pull.pub`, e o `scp` copiou só o `.pub`.

**[comando pronto]** Na VM, autorizar a chave dedicada no usuário `cqbackup`:

```bash
sudo install -d -m 0700 -o cqbackup -g cqbackup /home/cqbackup/.ssh
sudo install -m 0600 -o cqbackup -g cqbackup /tmp/cq-pull.pub /home/cqbackup/.ssh/authorized_keys
rm /tmp/cq-pull.pub
```

Conferência: `sudo ls -l /home/cqbackup/.ssh/authorized_keys` mostra `-rw------- cqbackup cqbackup`.

**[passo do Pedro]** Fazer a primeira conexão à mão, para aceitar a chave do host da VM (a cópia agendada usa o modo
em lote e não pergunta nada):

```powershell
sftp -i C:\cq-chaves\cq-pull cqbackup@<vm>
```

No prompt `sftp>`, rode `ls /dumps`, depois `ls /` e por fim `bye`.

Conferência: o `ls /dumps` lista o dump da seção 10 e o `SHA256SUMS`; o `ls /` mostra só `dumps` e `state` (se
mostrar `bin`, `etc` ou `srv`, a jaula da seção 2 não pegou); `ssh -i C:\cq-chaves\cq-pull cqbackup@<vm>` **não**
abre um shell (a sessão cai ou fica só no SFTP).

**[passo do Pedro]** Criar o script da cópia, `C:\cq-backups\copiar-backups.cmd`, num editor, com o conteúdo abaixo
(troque `<pasta do repositório>` e `<vm>`):

```bat
@echo off
cd /d "<pasta do repositório>"
uv run python ops/backup/pull_backups.py --host <vm> --identity C:\cq-chaves\cq-pull --remote-dir /dumps --local-dir C:\cq-backups\dumps >> C:\cq-backups\copia.log 2>&1
```

Conferência: `C:\cq-backups\copiar-backups.cmd` rodado à mão termina, `C:\cq-backups\dumps` tem o dump da seção 10
(`cq-<carimbo>.dump.age`; o `SHA256SUMS` não fica nessa pasta, só passa por um temporário), e o `copia.log` mostra
a linha `Cópia atualizada: <n> arquivo(s) novo(s).`, sem mensagem de erro.

**[comando pronto]** Criar as duas tarefas agendadas do Windows, uma diária e outra no logon (**[I]** a do logon pode
exigir um PowerShell aberto como administrador). As duas rodam o `copiar-backups.cmd` acima, que já leva o
`--remote-dir /dumps` (o caminho visto de dentro da jaula):

```powershell
schtasks /Create /TN "cq-copia-diaria" /SC DAILY /ST 09:00 /TR "C:\cq-backups\copiar-backups.cmd"
schtasks /Create /TN "cq-copia-logon" /SC ONLOGON /TR "C:\cq-backups\copiar-backups.cmd"
schtasks /Run /TN "cq-copia-diaria"
```

Conferência: `schtasks /Query /TN "cq-copia-diaria" /V /FO LIST` mostra o último resultado `0`; o mesmo vale para a
`cq-copia-logon` depois do próximo logon; o `copia.log` ganhou uma execução nova.

## 12. Túnel e perfil remoto

O agente (e o `cq` do Windows) fala com a API da VM por um túnel SSH só de encaminhamento: a API continua
publicada só no `127.0.0.1` da VM, e o SSH cifra o caminho. A ponta local do túnel é a porta **18000** do Windows,
própria do piloto, distinta da 8000 da pilha local e da `cq-demo`.

**[comando pronto]** No Windows, abrir o túnel e deixá-lo aberto enquanto o agente ou o `cq` remoto forem usados:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18000:127.0.0.1:8000 <operador>@<vm>
```

O `-N` não abre shell (só encaminha); o `ExitOnForwardFailure=yes` faz o comando sair com erro se a porta local
estiver ocupada, em vez de ficar aberto sem túnel.

Conferência: o comando fica parado, sem prompt; noutro PowerShell, `Test-NetConnection 127.0.0.1 -Port 18000` dá
`TcpTestSucceeded : True`.

Para trocar a porta local do túnel (se a 18000 estiver ocupada no Windows), escolha outro número e troque `18000` pelo
mesmo número novo nos dois lugares: no `-L` do comando do túnel e no `api_url` do perfil `piloto` abaixo. A porta da
VM (a `8000` no fim do `-L`) não muda.

**[comando pronto]** Na VM, criar o token do agente, com as permissões de operar oferecimentos, bots, pessoas e
eventos e de revelar identidades, mas **sem** `tokens:manage` (o agente não cria tokens). O token vai direto a um
arquivo novo, sem aparecer no terminal:

```bash
cq tokens create --label agente-piloto --permission offerings:read --permission offerings:write --permission bots:manage --permission people:read --permission people:write --permission events:read --permission identity:reveal --secret-file /tmp/agente-piloto.token
```

Conferência: o comando mostra só a linha `Token criado: …` com as permissões, sem o segredo; `cq tokens list` mostra
o rótulo `agente-piloto`.

**[comando pronto]** Trazer o arquivo do token para o Windows e apagá-lo na VM:

```powershell
scp <operador>@<vm>:/tmp/agente-piloto.token C:\cq-chaves\agente-piloto.token
ssh <operador>@<vm> shred -u /tmp/agente-piloto.token
```

Conferência: `C:\cq-chaves\agente-piloto.token` existe; `ssh <operador>@<vm> ls /tmp/agente-piloto.token` diz que o
arquivo não existe.

**[passo do Pedro]** Acrescentar o perfil do túnel ao arquivo de perfis do Windows (`%APPDATA%\cq\config.toml`; veja
[`inicio-rapido.md`](inicio-rapido.md), seção 3). Abra o `agente-piloto.token` no Bloco de Notas e cole o valor no
campo `token`, no editor, sem passar pelo chat:

```toml
[profiles.piloto]
kind = "remote"
api_url = "http://127.0.0.1:18000"
token = "<token do agente, criado na VM>"
```

O perfil é `remote`: sem `kind`, um perfil em `127.0.0.1` seria tomado por local, e o `cq` agiria sobre a pilha do
Windows. Ele não leva `stack_dir` nem `compose_project` (o `cq` os recusa no perfil remoto).

Conferência: com o túnel aberto, `cq --profile piloto status` mostra a API saudável; `cq --profile piloto doctor`
mostra as verificações da API e a parte da pilha local como "não se aplica"; `cq --profile piloto up` é recusado
(código 2), porque a pilha da VM é operada na VM.

## 13. Teste de envio de e-mail

Antes do ensaio, o Pedro confere o caminho inteiro do e-mail com um oferecimento de teste e uma lista com um
endereço dele, e depois arquiva e expurga esse oferecimento. Use um **bot de teste** (criado no BotFather só para
isso), nunca o bot do piloto: um bot não entra em dois oferecimentos do mesmo banco.

**[passo do Pedro]** Preparar dois arquivos fora do repositório: o token do bot de teste
(`C:\cq-teste\bot-teste.token`, salvo do BotFather direto no arquivo) e a lista
`C:\cq-teste\lista.csv`, com o cabeçalho `ra,nome,email` e uma única linha com um RA fictício, o seu nome e um
endereço de e-mail seu.

Conferência: os dois arquivos existem, e a lista tem duas linhas.

**[comando pronto]** Com o túnel aberto, criar o oferecimento de teste, registrar e ativar o bot de teste e importar a
lista:

```powershell
cq --profile piloto offerings create TESTE-EMAIL --course "Teste de e-mail" --term 2026s2 --timezone America/Sao_Paulo
cq --profile piloto bots register TESTE-EMAIL --token-file C:\cq-teste\bot-teste.token
cq --profile piloto bots activate TESTE-EMAIL
cq --profile piloto students import TESTE-EMAIL --file C:\cq-teste\lista.csv
```

Conferência: os quatro comandos terminam com código 0, e `cq --profile piloto students summary TESTE-EMAIL` mostra um
aluno.

**[comando pronto]** Enviar o convite em duas fases (preparar, conferir o resumo, confirmar com o código mostrado):

```powershell
cq --profile piloto invites send prepare TESTE-EMAIL
cq --profile piloto invites send confirm TESTE-EMAIL --confirmation <código mostrado no prepare>
```

Conferência: o convite chega ao seu endereço, vindo da conta do projeto, com o link do bot de teste; `cq --profile piloto doctor`
mostra o servidor de e-mail em "ok".

**[comando pronto]** Encerrar, arquivar e expurgar o oferecimento de teste, cada fase com o seu código:

```powershell
cq --profile piloto offerings close TESTE-EMAIL
cq --profile piloto offerings archive prepare TESTE-EMAIL
cq --profile piloto offerings archive confirm TESTE-EMAIL --confirmation <código do archive prepare>
cq --profile piloto offerings purge prepare TESTE-EMAIL
cq --profile piloto offerings purge confirm TESTE-EMAIL --confirmation <código do purge prepare>
```

Conferência: depois do expurgo, `cq --profile piloto offerings show TESTE-EMAIL` não acha o oferecimento (ou o mostra
expurgado); a pasta `C:\cq-teste` foi apagada; o teste foi anotado no diário (data, resultado, tempo até o e-mail
chegar).

## 14. Teste de restauração

O ensaio de restauração usa a cópia **real** do Windows (seção 11) numa pilha descartável, sem tocar no projeto `cq`
de produção. O passo a passo está em [`backup-e-restauracao.md`](backup-e-restauracao.md), seção "Restaurar
numa pilha descartável"; a recuperação de verdade, numa VM nova, está na seção "Recuperar a pilha numa VM nova" do
mesmo guia.

**[passo do Pedro]** Seguir a seção "Restaurar numa pilha descartável" de
[`backup-e-restauracao.md`](backup-e-restauracao.md) com o dump mais novo de `C:\cq-backups\dumps`, e ler (sem
executar) a seção "Recuperar a pilha numa VM nova".

Conferência: a restauração termina com `Restauração concluída.`, a pilha descartável foi desmontada, e o resultado foi
anotado no diário (data, dump usado, tempo gasto).

## 15. Falhas comuns

**[comando pronto]** Primeiro diagnóstico, na VM:

```bash
cd /srv/cq/app
cq status
cq doctor
cq logs --service worker --tail 50
```

Conferência: a linha `[falha]` ou `[aviso]` do `cq doctor` aponta uma das situações da tabela abaixo.

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| O SSH recusa a chave depois da seção 2 ou 3 | `AllowUsers` sem o seu usuário (o `<usuário-inicial>` até a seção 3; o `<operador>` depois), ou erro no `10-cq.conf` | Pela sessão de reserva aberta, corrija o arquivo, rode `sudo sshd -t` e recarregue |
| `Test-NetConnection <vm> -Port 8000` passa | A porta da API publicada fora do loopback | Pare a pilha (`cq down`) e confira o `CQ_API_PORT` e o `compose.yaml` do pacote; não suba de novo até falhar de fora |
| `cq up` sai por tempo | Primeira construção lenta ou falta de memória | Rode de novo com `--timeout 600`; confira o swap (seção 8) e `docker stats --no-stream` |
| `backup.age` com "Sem registro legível do último backup." depois da seção 10 | O backup não rodou, ou o diretório de estado não está em `0755` | `journalctl -u cq-backup.service -n 50 --no-pager`; refaça o `stat -c` da seção 10 |
| `backup.age` em "aviso" ou "falha" pela idade | O agendamento parou ou a VM ficou desligada | `systemctl list-timers cq-backup.timer`; rode o backup à mão |
| A rotina diz que o arquivo de destinatários está ausente ou inválido | `/etc/cq-backup/recipients.txt` sem a linha `age1…` | Refaça a cópia da chave pública ([`backup-e-restauracao.md`](backup-e-restauracao.md), "Chaves do age") |
| A cópia no Windows falha | Host da VM mudou, chave dedicada fora do `authorized_keys` ou VM fora do ar | Rode o `sftp -i …` da seção 11 à mão e leia o `copia.log` |
| A cópia no Windows falha, e o registro do `sshd` na VM (`sudo journalctl -u ssh -n 50 --no-pager`) mostra `bad ownership or modes for chroot directory` | O `/var/backups/cq`, ou um diretório acima dele, não é do `root` ou tem escrita para o grupo ou os outros: o `sshd` recusa a jaula do `cqbackup` (seção 2; **[I]** o texto da mensagem, de memória) | Refaça o passo do `/var/backups/cq` da seção 10 (`chown root:root` e `chmod 0755`) e confira com o `stat -c` de lá; os `dumps/` e `state/` não mudam |
| O `copia.log` diz "A VM não respondeu pela ligação SSH.", mas o `sftp -i …` da seção 11 entra e o `ls /dumps` lista os dumps | O `--remote-dir` do `copiar-backups.cmd` ainda é o caminho completo da VM, que não existe dentro da jaula (**[F]** a cópia dá essa mensagem para qualquer falha do `sftp`, `ops/backup/pull_backups.py`) | Troque o `--remote-dir` para `/dumps` (seção 11) |
| Servidor de e-mail em "falha" | Senha de app revogada, verificação em duas etapas desligada ou porta 587 bloqueada | Confira a conta (seção 6); para trocar a senha de app, recrie o arquivo de ambiente pelo `init_vm_env.py` (veja a nota abaixo) |
| `disk.space` em "aviso" ou "falha" | Disco cheio por imagens antigas ou dumps | `docker image prune` (só imagens sem uso); os dumps se podam sozinhos em 7 dias |
| O túnel sai na hora com erro de encaminhamento | A porta 18000 do Windows está ocupada | Troque a porta no comando e no perfil (seção 12) |
| `cq --profile piloto …` não alcança a API | Túnel fechado | Abra o túnel (seção 12) |

Nota sobre o arquivo de ambiente: ele nunca é editado nem impresso. Para trocar um segredo, renomeie o arquivo atual
(`mv .env .env.anterior`), rode de novo o `init_vm_env.py` com a **mesma** chave-mestra, suba com `cq up`, confira com
`cq doctor` e só então apague o anterior com `shred -u .env.anterior`. **[I]** O `init_vm_env.py` gera senha nova do
banco; com o volume do banco já criado, a senha antiga continua no Postgres, e a troca exige recuperar pelo backup
(seção 16, passo 4) ou manter a senha antiga: decida entre as duas antes de trocar.

## 16. Atualizar o sistema durante o piloto

Uma atualização (código novo) durante o piloto segue sempre esta ordem. O `cq` do Windows e o da VM
são atualizados juntos, do mesmo pacote.

**[passo do Pedro]** Escolher uma janela sem disparo aberto (plano de disparos) e seguir os quatro passos, na ordem:

1. Backup antes de tudo: na VM, `sudo systemctl start cq-backup.service`; no Windows, a cópia
   (`schtasks /Run /TN "cq-copia-diaria"`) com o dump novo em `C:\cq-backups\dumps`; e o `cq doctor` com `backup.age`
   em 0 h (a idade em horas zerada).
2. Pacote novo e reconstrução: gerar no Windows o pacote do commit novo com `ops/deploy/make_bundle.py` (seção 5),
   copiar e conferir os resumos na VM, abrir sobre `/srv/cq/app` (o arquivo de ambiente não vem no pacote e fica como
   está), reinstalar o `cq` (`uv tool install --force ./client`) e reconstruir com `cq up --rebuild --timeout 600`.
   No Windows, atualizar o `cq` da cópia do repositório no mesmo commit.
3. `cq doctor` sem falha: na VM e pelo túnel (`cq --profile piloto doctor`), nenhuma `[falha]`; anotar no diário os
   IDs das imagens novas e o `SOURCE_COMMIT`.
4. Se a atualização falhar e não houver correção rápida: recuperar pelo dump do passo 1, como manda a seção
   "Recuperar a pilha numa VM nova" de [`backup-e-restauracao.md`](backup-e-restauracao.md) (na mesma VM ou numa
   nova), com o pacote do commit anterior e o `cq` do Windows atualizado junto para esse mesmo commit.

Conferência: o diário tem, para a atualização, a hora do backup do passo 1, o commit novo, os IDs das imagens e o
resultado do `cq doctor` do passo 3 (ou a recuperação do passo 4).
