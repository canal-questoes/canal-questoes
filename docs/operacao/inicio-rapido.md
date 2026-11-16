# Início rápido: o `cq` e a pilha local

> **Para quem:** o operador do sistema (hoje, o Pedro). **Não** é material para alunos: eles só usam o bot
> do Telegram.
>
> **Verificado em:** 2026-10-28, contra o código do cliente (`client/src/cq_client/`), o `compose.yaml` e
> o contrato dos comandos do `cq`.
> Os comandos com o Docker real ainda
> não foram conferidos. A seção 11
> (operação pelo agente) foi acrescentada em 2026-10-29, contra o
> [guia do plugin e do agente](plugin-e-agente.md);
> o Claude Code real ainda não foi conferido.

O `cq` fala com a API do canal-questoes, local ou remota, e opera a **pilha local** (o projeto Docker
Compose deste computador: `postgres`, `api`, `worker` e `engine`). Este guia leva do repositório recém-clonado
até `cq status` com tudo saudável.

Nenhum valor real aparece aqui: troque `<seu-token>`, `<sua-pasta>` e afins pelos seus.

## 1. Instalar o `cq`

Dentro do repositório, `uv run cq …` já funciona, sem instalar nada. Para ter o `cq` em qualquer pasta
(fora do repositório), rode na raiz do repositório:

```bash
uv tool install ./client       # instala o pacote cq-client (Python 3.12) e o comando cq
uv tool update-shell           # uma vez: põe o diretório dos executáveis do uv no PATH
cq --help
```

Depois de atualizar o repositório, reinstale com `uv tool install --reinstall ./client`.

## 2. Criar o `.env` da pilha

A pilha lê os segredos de um arquivo `.env` na raiz da cópia do repositório, que o git ignora. **O `cq`
nunca lê, cria, altera nem imprime esse arquivo**; ele só confere se o arquivo existe.

```bash
cp .env.example .env           # PowerShell: Copy-Item .env.example .env
```

Preencha o `.env` seguindo os comentários do `.env.example` e do cabeçalho do `compose.yaml`:

- `CQ_DB_PASSWORD` (obrigatória): a senha do Postgres, só com caracteres seguros em URL;
- `CQ_ADMIN_TOKEN`: o token inicial de administração (no mínimo 32 caracteres);
- `CQ_MASTER_KEY`: a chave-mestra dos tokens dos bots (32 bytes aleatórios em base64url, sem `=`);
- `CQ_API_PORT` (opcional, padrão 8000): a porta da api no host, publicada só em `127.0.0.1`;
- `CQ_SMTP_*` (opcionais): o e-mail dos convites. Sem `CQ_SMTP_HOST`, o e-mail fica desligado.

Para gerar valores aleatórios:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"                  # CQ_DB_PASSWORD
python -c "import secrets; print('cq_' + secrets.token_urlsafe(32))"          # CQ_ADMIN_TOKEN
python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"  # CQ_MASTER_KEY
```

Nunca peça ao Docker Compose a configuração resolvida (o subcomando `config`): ela imprime os segredos interpolados.

## 3. Criar o arquivo de perfil (fora do repositório)

O perfil diz ao `cq` onde está a API, qual token usar e, no perfil local, onde está a pilha. O arquivo é
TOML, e o `cq` só o lê (não há comando que o crie ou edite). Fica em:

| Sistema | Caminho padrão |
| --- | --- |
| Windows | `%APPDATA%\cq\config.toml` |
| Linux | `~/.config/cq/config.toml` (ou `$XDG_CONFIG_HOME/cq/config.toml`) |
| macOS | `~/Library/Application Support/cq/config.toml` |

Outro caminho vale com a variável `CQ_CONFIG`. Exemplo com um perfil local e um remoto:

```toml
default_profile = "local"

[profiles.local]
kind = "local"
api_url = "http://127.0.0.1:8000"
token = "<seu-token>"
stack_dir = "C:/Users/<você>/<sua-pasta>/canal-questoes"   # a cópia com o compose.yaml e o .env
compose_project = "cq"                                     # opcional; o padrão já é "cq"

[profiles.piloto]
kind = "remote"
api_url = "http://127.0.0.1:18000"                         # a porta local do túnel SSH até a VM
token = "<token do agente, criado na VM>"
```

O perfil remoto do piloto fala com a API da VM por um túnel SSH (`-L 127.0.0.1:18000:127.0.0.1:8000`): a API da
VM não fica exposta na rede, e o `api_url` aponta a ponta local do túnel. Um perfil remoto não leva `stack_dir` nem
`compose_project` (o `cq` os recusa). O comando do túnel, a troca da porta local e o token do agente estão no guia
da VM, [`implantacao-vm.md`](implantacao-vm.md), seção 12.

- **`stack_dir`:** o caminho absoluto da sua cópia do repositório. No Windows, use `/` (como acima) ou aspas
  simples (`'C:\Users\…'`), porque entre aspas duplas o TOML trata `\` como escape. Sem `stack_dir`, vale
  `CQ_STACK_DIR`; sem as duas, o `cq` procura a cópia no diretório atual e nos ancestrais (pelo `compose.yaml`,
  `engine/Dockerfile`, `server/pyproject.toml` e `client/pyproject.toml`). Um caminho configurado que não existe
  dá erro, sem cair na busca.
- **`compose_project`:** o nome do projeto Compose da sua pilha. `cq-demo` e `cq-systest` são reservados e
  recusados, para que o `cq` nunca toque a demonstração nem os testes de sistema.
- **`kind`:** opcional. Sem ele, o perfil é local quando o host é `127.0.0.1`, `::1` ou `localhost`.
- **Permissões fechadas:** no Linux e no macOS, `chmod 600` no arquivo. No Windows, o `cq doctor` não confere as
  permissões ("não verificado"): mantenha o arquivo na sua pasta de usuário e não o compartilhe. Nunca o
  ponha dentro de uma cópia git; o `doctor` avisa nesse caso.

**Qual perfil vale:** a opção `--profile NOME` (antes do subcomando: `cq --profile piloto status`) > a variável
`CQ_PROFILE` > o `default_profile` do arquivo > o perfil implícito (as chaves `api_url` e `token` no topo do
arquivo, fora de qualquer perfil nomeado). `CQ_API_URL` e `CQ_TOKEN`, se definidas, vencem o campo correspondente do perfil
escolhido.

## 4. Configurar o token uma vez

Como o `cq` não lê o `.env`, o perfil local precisa do token por conta própria. Faça **uma** destas:

- **No arquivo (recomendado):** ponha no campo `token` de `[profiles.local]` o valor que você gerou para
  `CQ_ADMIN_TOKEN`.
- **Na sessão:** `export CQ_TOKEN='<seu-token>'` (PowerShell: `$env:CQ_TOKEN = '<seu-token>'`).

O token inicial dá acesso total. Para o dia a dia, você pode criar depois um token com menos permissões
(`cq tokens create --label … --permission … --secret-file <arquivo-novo>`) e trocá-lo no perfil. Sem token, os
comandos da API recebem o 401 (não autenticado), e o `cq doctor` mostra "sem token".

## 5. Subir, acompanhar e parar a pilha

```bash
cq up                          # sobe os quatro serviços e espera a API ficar saudável (até 180 s)
cq status                      # perfil, serviços e saúde da API; código 0 se tudo está saudável
cq logs                        # registros da api, do worker e do postgres (os do engine ficam de fora)
cq logs --service api --tail 50
cq logs --follow               # segue os registros; Ctrl+C sai com o código 130
cq down                        # para e remove os contêineres; os dados (volumes) ficam
```

- A primeira subida constrói as imagens e pode demorar; use `cq up --timeout 600` se precisar de mais tempo
  (de 1 a 3600 s). `cq up --rebuild` reconstrói as imagens da api e do worker.
- O `up` é idempotente: com a pilha saudável, não muda nada. Com a pilha parcial, sobe só o que falta.
- O `down` nunca apaga volumes: banco e repositórios obtidos continuam.
- Se um comando da API falhar porque a pilha local está parada (perfil local, sem `CQ_API_URL` e sem
  `--json`), o `cq` sugere `cq up` e, num terminal, pergunta `[s/N]` se deve subi-la. Depois do "sim", rode o
  comando de novo.

## 6. Diagnosticar com `cq doctor`

`cq doctor` confere o perfil (permissões, cópia git), a API (alcance, banco, migrações, acesso, chave-mestra,
bots, tarefas atrasadas, relógio) e a pilha local (Docker, versão do Compose, diretório, `.env`, serviços, imagem do motor,
dono da API). Cada linha sai com `[ok]`, `[aviso]`, `[falha]`, `[não verificado]` ou `[não se aplica]`; o código
é 0 sem falha e 1 com alguma. O doctor confere ainda o backup, o volume do Postgres, o espaço em disco, o servidor de e-mail e a retenção.
O Compose 2.26.1 dá "aviso" (o mínimo sem aviso é o 2.27).

## 7. Conviver com a pilha `cq-demo`

Se a pilha de demonstração `cq-demo` estiver no ar, a porta 8000 pode estar ocupada. Use outra porta **no
shell**, só para a sua pilha, e ajuste o perfil:

```bash
export CQ_API_PORT=8001        # PowerShell: $env:CQ_API_PORT = '8001'
cq up
```

```toml
[profiles.local]
api_url = "http://127.0.0.1:8001"
```

Pela documentação do Compose, a variável do shell vence a do `.env` na interpolação (ainda não conferido
no 2.26.1), então o `.env`, que a `cq-demo` pode usar, não muda. Defina-a de novo em cada shell em que rodar
`cq up`. Se a porta publicada e a do `api_url`
divergirem, o `up` sai com o código 1 e pede para ajustar o `api_url`.

## 8. Perfil remoto

`cq --profile piloto status` mostra só a parte da API. `up`, `down` e `logs` recusam um perfil remoto (código 2).
Com `http://` fora do loopback, o `cq` funciona, mas avisa no stderr que o token vai sem cifragem. No piloto, o
perfil remoto é o do túnel (seção 3): o `api_url` fica no loopback (`127.0.0.1`), e o SSH cifra o caminho até a VM.
Com o túnel fechado, o `cq` não alcança a API. A pilha da VM (subir, parar, registros, backup) é operada pelo Pedro
por SSH, com o `cq` instalado na VM e o perfil local de lá; veja o guia [`implantacao-vm.md`](implantacao-vm.md).

## 9. Quando algo dá errado

| Mensagem (stderr) | O que fazer |
| --- | --- |
| `Diretório da pilha não encontrado…` | defina `stack_dir` no perfil local ou `CQ_STACK_DIR` |
| `Arquivo de ambiente ausente no diretório da pilha…` | crie o `.env` (seção 2) |
| `Configuração da pilha incompleta: defina <NOME>…` | preencha a variável no `.env` |
| `O Docker não está disponível neste computador…` | abra o Docker Desktop (ou o serviço) |
| `A porta da API já está em uso…` | outra pilha usa a porta: seção 7 |
| `O Docker aponta para um alvo que não é local…` | tire `DOCKER_HOST` ou `DOCKER_CONTEXT` do shell |
| `A pilha não ficou saudável em <n> s…` | `cq logs` e `cq doctor` |

Códigos de saída: 0 sucesso; 1 falha (da API, do Docker ou da pilha); 2 uso ou configuração incorretos; 3 API
inalcançável (comandos da API); 130 interrompido.

## 10. Rodar a suíte de testes

Rode a suíte **sem** `CQ_PROFILE` e `CQ_STACK_DIR` no ambiente, para que o seu perfil não vaze para
os testes:

```bash
unset CQ_PROFILE CQ_STACK_DIR
# PowerShell: Remove-Item Env:CQ_PROFILE, Env:CQ_STACK_DIR -ErrorAction SilentlyContinue
```

Os testes nunca sobem o projeto `cq` nem tocam a `cq-demo`; o teste de sistema usa o projeto `cq-systest`.

## 11. Operação pelo agente

O `cq` também serve o agente: `cq mcp` é o servidor MCP que o Claude Code inicia, e o plugin do repositório
(`plugins/canal-questoes/`) traz as skills dos fluxos do professor. O guia completo, do token do agente à conferência
da conexão, está no [guia do plugin e do agente](plugin-e-agente.md). Três avisos:

- **Um token só do agente.** Crie-o no **seu** terminal, com a permissão de revelar (`identity:reveal`) e **sem** a de
  gerir tokens (`tokens:manage`), e ponha-o num perfil próprio, com o `stack_dir`. O token nunca vai em argumento de
  ferramenta nem no plugin.
- **A lista de alunos pelo caminho absoluto.** A ferramenta de importação (`import_students`) recebe só o caminho
  absoluto do arquivo; o servidor o lê, e o conteúdo não passa pelo modelo. Não peça ao agente para abrir o arquivo.
- **A brecha do terminal.** A pergunta antes das ações sensíveis, as duas etapas e a redação dos registros valem só
  para as ferramentas MCP. Permissões amplas de terminal no Claude Code (como as de uma sessão de desenvolvimento) deixam o agente, pelo terminal e
  sem perguntar, revelar identidades, ler os registros sem redação, parar a pilha, ler o arquivo de perfil (com o
  token) e, por `docker exec`, `docker run` e `docker compose exec`, ler o banco (as identidades de todos os alunos) e
  o ambiente dos contêineres (a chave-mestra e a senha do banco). **Opere o agente numa sessão sem essas permissões amplas** (a
  mitigação principal) e recuse no prompt os comandos de terminal que contornem as ferramentas.
