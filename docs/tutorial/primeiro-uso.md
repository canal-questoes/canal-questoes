# Primeiro uso: da pilha local ao primeiro relatório

Este tutorial leva você do repositório recém-clonado até o relatório de uma questão respondida pelo Telegram. No
caminho, você sobe a pilha local, cria um oferecimento de teste, liga a ele um bot do Telegram criado por você, convida
a si mesmo como aluno, dispara uma questão, responde a ela e lê o relatório. No fim, desfaz o que criou.

Você faz os dois papéis: o de quem opera o sistema (pelo comando `cq`) e o de aluno (pela sua conta do Telegram). O
sistema roda como roda de verdade: o bot é real, o convite sai por e-mail real e a questão vem de um repositório git
real. Não há modo de demonstração.

Para a referência completa de cada comando, veja a [referência do CLI](../referencia/cli.md). Para o índice da
documentação, veja o [README da documentação](../README.md).

## Antes de começar

### Sistema

- **Linux**, com o Docker Engine e o plugin do Compose, ou **Windows**, com o Docker Desktop sobre o WSL 2. Outros
  sistemas não foram conferidos.
- O Docker Engine 26.0.2 ou mais novo (ou as versões corrigidas 25.0.5 e 23.0.11), por causa de duas correções de
  segurança de rede do Docker, que valem para a rede interna que isola o motor de questões. O Compose 2.27 ou mais novo: com uma versão anterior, o
  diagnóstico do `cq` mostra um aviso.
- O `git` e o `uv`, que instala o `cq` com o Python 3.12.
- Alguns gigabytes livres em disco: o motor de questões é construído sobre a imagem do PrairieLearn.
- Acesso à internet: a pilha baixa as imagens, busca a questão no repositório git, fala com o Telegram e envia o
  e-mail pelo seu servidor SMTP.

Os comandos estão no formato do bash (Linux ou o terminal do WSL 2). Quando o PowerShell do Windows pede outra forma,
ela vem num comentário ao lado. Os comandos `cq` são os mesmos nos dois.

### O que você precisa ter

- **Uma conta do Telegram**, no celular ou no computador. Com ela, você cria o bot e depois entra no oferecimento como
  aluno.
- **Uma conta de e-mail com envio por SMTP.** O convite do aluno sai por e-mail, e este tutorial o envia pela sua
  própria conta para o seu próprio endereço. O exemplo usa o Gmail, com uma senha de app no lugar da senha da conta.
  Pela [ajuda do Google sobre senhas de app](https://support.google.com/accounts/answer/185833), criar uma exige a
  verificação em duas etapas ligada na Conta do Google; a senha de app se cria na página
  [Senhas de app](https://myaccount.google.com/apppasswords) da conta. Outro provedor serve, com o servidor, a porta
  e o modo de segurança dele.
- **Uma questão do PrairieLearn num repositório git público**, servido por HTTPS. O repositório segue o formato de
  curso do PrairieLearn: cada questão fica em `questions/<qid>/`, com o `info.json` e o `question.html` (e o
  `server.py`, se houver). A questão pode usar só os elementos de resposta suportados: `pl-multiple-choice`,
  `pl-checkbox`, `pl-string-input`, `pl-number-input` e `pl-integer-input` (veja os
  [tipos de questão](../referencia/tipos-de-questao.md)). Anote três coisas: o endereço HTTPS do repositório, o SHA
  completo do commit (40 dígitos hexadecimais; por exemplo, a saída de `git rev-parse HEAD` numa cópia dele) e o
  `qid`, o caminho da questão dentro de `questions/`.

### Seus dados neste tutorial

O aluno do tutorial é você: o RA e o nome são fictícios, e o e-mail é o seu, para o convite chegar. Mantenha o arquivo
da turma, o arquivo de ambiente (`.env`) e o token do bot fora de qualquer commit. O token do bot nunca vai na linha de
comando: o `cq` o pede num prompt que não mostra o que você digita.

## 1. Obter o código e instalar o `cq`

```bash
git clone <endereço deste repositório>
cd <pasta do clone>
uv tool install ./client       # instala o pacote cq-client (Python 3.12) e o comando cq
uv tool update-shell           # uma vez: põe o diretório dos executáveis do uv no PATH
cq --help
```

Abra um terminal novo depois do `uv tool update-shell`, para o `PATH` valer. Dentro do repositório, `uv run cq …` também
funciona, sem instalar nada.

## 2. Criar o arquivo de ambiente

A pilha lê os segredos de um arquivo `.env` na raiz da cópia do repositório, que o git ignora. O `cq` nunca lê, cria,
altera nem imprime esse arquivo: ele só confere se o arquivo existe.

```bash
cp .env.example .env           # PowerShell: Copy-Item .env.example .env
```

Gere três valores aleatórios (qualquer Python 3 serve; no Linux, use `python3` se `python` não existir):

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"                  # CQ_DB_PASSWORD
python -c "import secrets; print('cq_' + secrets.token_urlsafe(32))"          # CQ_ADMIN_TOKEN
python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode())"  # CQ_MASTER_KEY
```

Abra o `.env` num editor. Ele já traz `CQ_DB_PASSWORD`, `CQ_ADMIN_TOKEN` e `CQ_API_PORT`; preencha as duas primeiras
com os valores gerados e deixe `CQ_API_PORT` vazia (a porta padrão é 8000). Depois, acrescente as linhas abaixo,
trocando cada `<…>` pelo seu valor:

```text
CQ_MASTER_KEY=<a chave gerada acima>
CQ_REPO_ALLOWED_HOSTS=github.com
CQ_SMTP_HOST=smtp.gmail.com
CQ_SMTP_PORT=587
CQ_SMTP_SECURITY=starttls
CQ_SMTP_USERNAME=<seu endereço do Gmail>
CQ_SMTP_PASSWORD=<a senha de app>
CQ_SMTP_FROM=<seu endereço do Gmail>
```

O que cada uma faz:

- `CQ_MASTER_KEY` cifra os tokens dos bots no banco. Sem ela, o registro do bot é recusado.
- `CQ_REPO_ALLOWED_HOSTS` é a lista dos hosts git de onde a pilha aceita buscar questões, separados por vírgula, sem
  espaços (nomes em minúsculas, com ponto, sem porta nem IP). Vazia, ela recusa todos os repositórios. Troque
  `github.com` pelo host do seu repositório de questões, se for outro.
- As `CQ_SMTP_*` ligam o e-mail dos convites. Sem `CQ_SMTP_HOST`, o e-mail fica desligado, e o convite não sai.
  `CQ_SMTP_USERNAME` e `CQ_SMTP_PASSWORD` vêm juntas, e `CQ_SMTP_FROM` precisa ser um endereço de e-mail válido; no
  Gmail, use o endereço da própria conta.

Escreva os valores sem aspas e sem os caracteres `#` e `$`: num valor sem aspas, o Compose lê um `#` depois de um
espaço como o começo de um comentário e o `$` como interpolação. Nunca peça ao Docker Compose a configuração
resolvida (o subcomando `config`): ela imprime os segredos.

Se você mudar o `.env` com a pilha no ar, rode `cq down` e depois `cq up`: o `cq up` não recria os contêineres que já
estão rodando, e eles continuariam com os valores antigos.

## 3. Criar o perfil do `cq`

O perfil diz ao `cq` onde está a API, qual token usar e onde está a pilha. É um arquivo TOML, fora do repositório, que
o `cq` só lê:

| Sistema | Caminho |
| --- | --- |
| Linux (e o WSL 2) | `~/.config/cq/config.toml` |
| Windows | `%APPDATA%\cq\config.toml` |

```bash
mkdir -p ~/.config/cq          # PowerShell: New-Item -ItemType Directory -Force "$env:APPDATA\cq"
```

Crie o arquivo num editor, com este conteúdo:

```toml
default_profile = "local"

[profiles.local]
kind = "local"
api_url = "http://127.0.0.1:8000"
token = "<o mesmo valor que você pôs em CQ_ADMIN_TOKEN>"
stack_dir = "/caminho/absoluto/da/sua/copia"   # no Windows, com barras normais: "C:/Users/<você>/<pasta do clone>"
```

No Linux, feche as permissões do arquivo com `chmod 600 ~/.config/cq/config.toml`. Não ponha o perfil dentro de uma
cópia git: ele guarda o token. Os detalhes do perfil (outros caminhos, a variável `CQ_CONFIG`, a ordem de escolha do
perfil) estão no guia de [início rápido](../operacao/inicio-rapido.md).

## 4. Subir a pilha e conferir

```bash
cq up
cq status
cq doctor
```

- O `cq up` sobe os quatro serviços (`postgres`, `api`, `worker` e `engine`) e espera que todos fiquem prontos e que
  a API responda saudável. A primeira subida constrói as imagens e pode demorar; a construção não conta no prazo, que
  só mede a espera pela saúde depois dela (180 s por padrão). Se a pilha não ficar saudável no prazo, rode `cq up` de
  novo (ele sobe só o que falta) ou use `--timeout`, de 1 a 3600 s, para esperar mais.
- O `cq status` mostra o perfil, os serviços e a saúde da API, e sai com o código 0 quando tudo está saudável.
- O `cq doctor` confere o perfil, a API e a pilha. Nenhuma linha deve sair com `[falha]`. Numa pilha de teste, alguns
  `[aviso]` e `[não verificado]` são esperados, como "Backup não configurado nesta pilha.". O `worker` testa o
  servidor de e-mail ao partir: até lá, o aviso é "O servidor de e-mail ainda não foi testado."; rode o `cq doctor` de
  novo em alguns instantes. Uma falha no servidor de e-mail (sem conexão, na negociação TLS ou na autenticação) quer
  dizer que as `CQ_SMTP_*` precisam de conferência.

## 5. Criar o oferecimento de teste

Um oferecimento é uma turma de uma disciplina num período, com o seu bot.

```bash
cq offerings create tutorial --course "Disciplina de teste" --term 2026s2 --timezone America/Sao_Paulo --response-window 30
```

O código do oferecimento (`tutorial`) usa letras minúsculas, dígitos e hífen. A opção `--response-window 30` deixa cada
questão aberta por 30 minutos (o padrão é 1440, um dia), para a limpeza do fim não precisar esperar um dia. O
oferecimento
nasce em rascunho; ele fica ativo no próximo passo, com o bot.

## 6. Criar o bot no Telegram e ligá-lo ao oferecimento

No Telegram, abra a conversa com o `@BotFather` e mande `/newbot`. Ele pede um nome e um nome de usuário para o bot. O
nome de usuário tem de 5 a 32 caracteres (letras latinas, números e sublinhado), termina em `bot` e não muda depois. No
fim, o BotFather mostra o token do bot: quem tem o token controla o bot, então não o cole em chat, documento ou
arquivo do repositório.

Registre o bot no oferecimento e ative-o:

```bash
cq bots register tutorial      # pede o token num prompt que não mostra o que você digita
cq bots activate tutorial
cq bots show tutorial
```

Cole o token no prompt do `bots register`. Fora de um terminal interativo, o comando exige `--token-file` com um
arquivo que contenha só o token. O `bots show` mostra o bot no estado `registered`; em instantes [I], o perfil passa
a `configured` e aparece a posse do worker.

## 7. Importar a turma de um aluno só

Num editor, crie um arquivo CSV em UTF-8, fora da cópia do repositório (por exemplo, `~/cq-tutorial/turma.csv`), com
o cabeçalho `ra,nome,email` e uma linha só: um RA fictício, um nome fictício e o **seu** endereço de e-mail.

```text
ra,nome,email
123456,Aluna Teste,<seu endereço de e-mail>
```

```bash
cq students import tutorial --file ~/cq-tutorial/turma.csv   # PowerShell: --file "$HOME\cq-tutorial\turma.csv"
cq students summary tutorial
```

A importação é tudo ou nada, e só as contagens aparecem na saída. O `students summary` mostra um aluno matriculado,
ainda sem convite.

## 8. Enviar o convite e entrar pelo link

O envio dos convites tem duas fases: o `prepare` mostra o resumo e um código de confirmação, válido por 10 minutos; o
`confirm` usa esse código.

```bash
cq invites send prepare tutorial
cq invites send confirm tutorial --confirmation <código mostrado no prepare>
```

Em alguns instantes, o convite chega ao seu e-mail, com um link do bot
(`https://t.me/<nome de usuário do bot>?start=…`). Abra o link no aparelho em que está a sua conta do Telegram e inicie
a conversa com o bot. O bot mostra um termo de uso (o texto escrito para o piloto do sistema, um TCC da Unicamp); leia
e toque em `/aceito`. O bot responde que você está vinculado ao oferecimento.

```bash
cq students summary tutorial   # agora com 1 em "vinculados"
```

Se o convite não chegar, veja [Quando algo dá errado](#quando-algo-dá-errado).

## 9. Disparar uma questão

O disparo também tem duas fases. O `prepare` registra o pedido, e o `worker` busca a questão e a valida no motor; o
`confirm` envia a questão a quem está vinculado.

```bash
cq dispatches prepare tutorial --repo <endereço HTTPS do repositório> --commit <SHA de 40 dígitos> --qid <qid>
cq dispatches show tutorial <id do disparo>
```

O `prepare` mostra o id do disparo. Se ele for recusado na hora, com a linha `- repo: invalid_value`, nenhum disparo
foi criado: o host do repositório não está em `CQ_REPO_ALLOWED_HOSTS`. Corrija o `.env` e reinicie a pilha com
`cq down` e `cq up`, porque a `api` também lê essa variável; depois, rode o `prepare` de novo.

Com o id em mãos, repita o `show` até o estado sair de `validating`:

- `scheduled`: a questão foi validada. A saída traz a prévia (o enunciado e os campos de resposta) e `alunos-alvo na
  validação: 1`. Neste tutorial, também aparece o aviso de que o oferecimento não tem contato do professor; ele é
  esperado aqui, porque o oferecimento foi criado sem `--contact-email`.
- `rejected`: a questão foi recusada, e a linha `motivo` diz por quê (por exemplo, um commit que não existe, uma
  questão que não existe nesse commit ou um elemento de resposta não suportado). Corrija e prepare um disparo novo.

Com a prévia conferida, confirme:

```bash
cq dispatches confirm tutorial <id do disparo>
```

Como o `prepare` não levou `--opens-at`, o disparo é imediato: a saída diz que ele abre agora e quando fecha. Um
disparo imediato validado precisa ser confirmado em até 24 horas.

**Nota.** Este tutorial dispara pelo repositório de demonstração. Para disparar uma questão de um curso que está numa
pasta do seu computador, sem publicá-lo num repositório, envie a pasta antes com `cq banks upload` e prepare o disparo
com a referência `bank:<nome>` e a versão devolvida; o passo a passo está no
[guia do banco de questões local](../operacao/banco-local.md).

## 10. Responder pelo Telegram

A questão chega na conversa com o bot. Responda pelos botões da questão ou, nos campos de texto e de número,
digitando a resposta numa mensagem (com mais de uma questão aberta, toque em "Responder" na mensagem dela). O bot
devolve a correção, com a sua resposta, o veredito (`✅ correta`, `◐ parcialmente correta` ou `❌ incorreta`) e a nota.

## 11. Ver o relatório

```bash
cq reports show tutorial <id do disparo>
```

Sem `--version`, o relatório é calculado na hora: o alcance (alunos-alvo, entregues, pendentes), as respondidas, as
corretas, as taxas, a nota média e o tempo até responder. Com `--csv`, sai o CSV da API. Quando a questão fecha, o
sistema congela uma versão do relatório; `cq reports versions tutorial <id do disparo>` lista as versões congeladas.

Pronto: você foi do repositório ao relatório.

## 12. Limpar tudo

Espere a questão fechar (30 minutos depois de abrir): o `cq dispatches show tutorial <id do disparo>` mostra o estado
`closed`. Depois, encerre, arquive e expurgue o oferecimento. O arquivamento e o expurgo têm duas fases, cada uma com o
seu código de confirmação, válido por 10 minutos:

```bash
cq offerings close tutorial
cq offerings archive prepare tutorial
cq offerings archive confirm tutorial --confirmation <código do archive prepare>
cq offerings purge prepare tutorial
cq offerings purge confirm tutorial --confirmation <código do purge prepare>
```

O expurgo apaga as identidades do oferecimento (o seu nome, RA, e-mail e a ligação com a sua conta do Telegram) e
mantém as respostas sem elas. Se o `purge prepare` recusar por trabalho pendente (a saída mostra o que falta, como
relatórios por congelar ou envios devidos), espere alguns minutos e rode-o de novo.

Pare a pilha:

```bash
cq down
```

O `cq down` para e remove os contêineres, mas mantém os dados (os volumes do Docker) e as imagens construídas. Para
apagar também o banco, os repositórios obtidos e as imagens construídas pela pilha, rode na raiz da cópia, depois do
`cq down`:

```bash
docker compose -p cq -f compose.yaml --env-file .env down -v --rmi local
docker volume ls
docker image ls
```

O `-v` apaga os volumes, e o `--rmi local` apaga as imagens sem nome próprio no `compose.yaml`, que são as da `api`, do
`worker` e do `engine`. O `-p cq` é o nome do projeto do Compose, o mesmo que o `cq down` mostra; se o seu perfil tem
outro `compose_project`, use esse nome no `-p`. O `docker volume ls` não deve mostrar mais os volumes do projeto, e o
`docker image ls`, as imagens dele. A imagem do PostgreSQL, baixada pronta, fica; apague-a com `docker image rm`, se
não for usá-la.

Por fim, fora do sistema:

- no Telegram, na conversa com o `@BotFather`, apague o bot com `/deletebot` (isso libera o nome de usuário e não se
  desfaz);
- na página [Senhas de app](https://myaccount.google.com/apppasswords) da Conta do Google, remova a senha de app que
  você criou para o tutorial;
- apague o arquivo da turma (`~/cq-tutorial/turma.csv`) e, se não for usar a pilha de novo, o `.env` e o perfil.

## Quando algo dá errado

| Sintoma | O que fazer |
| --- | --- |
| A página [Senhas de app](https://myaccount.google.com/apppasswords) diz que a configuração não está disponível para a sua conta, ou pede para confirmar a sua identidade e não passa disso | ligue a verificação em duas etapas da Conta do Google e volte à página. Pela [ajuda do Google sobre senhas de app](https://support.google.com/accounts/answer/185833), a opção também falta quando a verificação usa só chaves de segurança, numa conta de trabalho, escolar ou de outra organização e numa conta com a Proteção Avançada. Se a página continuar recusando, use outra conta ou outro provedor de e-mail: uma conta criada há poucos dias pode não passar da confirmação de identidade [I] |
| O `cq up` diz que falta uma variável no arquivo de ambiente | preencha a variável no `.env` (passo 2) |
| O `cq up`, o `cq status` ou o `cq doctor` mostram o `worker` parado ou com falha (ou o aviso de e-mail não testado não some) | veja `cq logs --service worker`: uma configuração inválida aparece com a variável `CQ_*` a corrigir (por exemplo, uma das `CQ_SMTP_*`), sem o valor; corrija o `.env` e rode `cq down` e `cq up` |
| O `cq up` diz que a porta da API já está em uso | outro processo usa a porta 8000: ponha outra porta em `CQ_API_PORT` no `.env` e a mesma no `api_url` do perfil, e rode `cq down` e `cq up` |
| O `cq up` diz que o Docker não está disponível | abra o Docker Desktop (Windows) ou inicie o serviço do Docker (Linux) |
| Os comandos da API dizem `Token ausente, inválido ou revogado.`, ou o `cq doctor` diz `Sem acesso à parte da API: token recusado.` | o `token` do perfil não é o valor de `CQ_ADMIN_TOKEN` do `.env` |
| O `bots register` é recusado | se a mensagem fala da chave-mestra dos bots, confira `CQ_MASTER_KEY` no `.env` e reinicie a pilha com `cq down` e `cq up`; se a saída aponta o `token`, confira o token copiado do BotFather |
| O `students summary` mostra o convite em "falha no envio" | confira as `CQ_SMTP_*`, reinicie a pilha com `cq down` e `cq up` e veja `cq doctor` e `cq logs --service worker`; depois, rode de novo o `cq invites send prepare` e o `cq invites send confirm` |
| O `cq doctor` diz que o servidor de e-mail recusou a autenticação | confira `CQ_SMTP_USERNAME` e `CQ_SMTP_PASSWORD`; com o Gmail, tente a senha de app sem os espaços entre os grupos [I] |
| O convite não chega | procure na caixa de spam; confira `CQ_SMTP_FROM` e o `students summary` |
| O `dispatches prepare` é recusado com `- repo: invalid_value` | ponha o host do repositório em `CQ_REPO_ALLOWED_HOSTS` no `.env` e reinicie a pilha com `cq down` e `cq up` |
| O disparo fica em `rejected` | leia a linha `motivo` do `cq dispatches show`; confira o SHA, o `qid` e os elementos de resposta da questão |

Os códigos de saída do `cq`: 0 sucesso; 1 falha (da API, do Docker ou da pilha); 2 uso ou configuração incorretos; 3
API inalcançável; 130 interrompido com Ctrl+C (nos comandos da pilha, no `cq doctor` e no expurgo; nos demais, o
Ctrl+C sai com 1). A lista completa está nos [códigos de saída](../referencia/cli.md#códigos-de-saída) da
referência do CLI.

## Próximos passos

- A [referência do CLI](../referencia/cli.md) descreve todos os comandos, inclusive os da equipe do oferecimento, das
  mensagens livres e da revelação de identidades.
- O guia de [início rápido](../operacao/inicio-rapido.md) detalha a pilha local, os perfis e os registros.
- O guia do [plugin e do agente](../operacao/plugin-e-agente.md) mostra como operar o sistema por um agente, pelo
  servidor MCP do `cq`.
- Os [tipos de questão](../referencia/tipos-de-questao.md) listam os elementos de resposta suportados e o que a inspeção
  recusa.
