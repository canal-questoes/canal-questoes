# Publicação de versões

Este guia é do mantenedor: como publicar uma versão do canal-questoes no repositório público. O desenvolvimento
acontece num repositório de origem, privado. O público recebe o código só a cada versão, pela exportação
(`ops/export/export_public.py`): uma cópia só dos arquivos da lista positiva (`ops/export/allowlist.txt`), varrida
contra segredos e dados pessoais. Cada versão entra no público por uma pull request de uma branch `versao/v0.y.z`,
com o CI conferido antes do merge; depois vêm a tag e a release com a seção da versão no
[CHANGELOG](../../CHANGELOG.md). As versões seguem o SemVer, na forma `0.y.z`. Neste guia, "publicação" quer dizer
uma versão.

Na publicação, este guia substitui as seções "Criar o repositório público e dar o push" e "Exportar de novo depois"
do guia [CI e exportação](ci-e-exportacao.md), que mandam a primeira versão direto para a `main`. Daquele guia valem
aqui só a exportação e a revisão da árvore, a validação local do CI e as conferências da primeira execução real.

A exportação roda só no repositório de origem. Numa árvore exportada, como um clone do repositório público, ela
recusa com o código 65, porque ali falta a sobreposição pública: a pasta, só da origem, com as versões públicas do
`AGENTS.md`, do `CLAUDE.md` e do `ROADMAP.md`, que a exportação põe na raiz.

Marcas usadas nos passos:

- **[mantenedor]**: um passo que pede a conta do GitHub ou uma decisão; só o mantenedor o faz;
- **[comando pronto]**: comandos para copiar e colar, sem segredo na linha;
- cada passo termina numa linha `Conferência:`, que diz como saber que deu certo;
- **[I]**: uma escolha ou inferência deste guia ainda não conferida na prática;
- **[F]**: um fato já conferido na prática, com a data da conferência.

Regras do guia inteiro:

- Nada aqui pede token, senha ou chave na linha de comando. Se o `git` ou o GitHub CLI (`gh`) pedirem uma credencial,
  ela vai só no prompt deles ou no navegador.
- A exportação não escreve no repositório de origem nem muda o histórico dele: ela monta um repositório git novo,
  num diretório vazio fora da origem, com um só commit e sem linha de coautoria. O histórico de desenvolvimento fica
  só na origem.
- Os comandos são de bash: no Linux, no WSL 2 ou, no Windows, no Git Bash (os scripts das suítes já são de bash).
  Eles usam o `git`, o `uv` e, no passo 1, o Docker, o `curl` e um Node que leia TypeScript (a versão está no
  [CONTRIBUTING](../../CONTRIBUTING.md)); nos passos 6 e 7, o `gh` autenticado na conta do mantenedor.

Defina as variáveis uma vez, no mesmo terminal em que vai seguir o guia:

```bash
VERSAO=0.1.0                                      # a versão desta publicação, sem o "v"
NOME="<nome público do autor dos commits>"
EMAIL="<e-mail público do autor dos commits>"
ORIGEM="<caminho do repositório de origem>"
PUB="<caminho do clone do repositório público>"  # fora do repositório de origem
```

Defina também os nomes da publicação, que os passos 1, 3, 6 e 7 usam:

```bash
ROTULO="v$VERSAO"                     # o título da pull request e o rótulo das suítes
RAMO="versao/v$VERSAO"                # a branch da pull request
SECAO="$VERSAO"                       # a seção do CHANGELOG que descreve a publicação
MENSAGEM="chore(release): v$VERSAO"   # a mensagem do commit no clone público
```

O nome e o e-mail ficam visíveis para qualquer pessoa no repositório público. **[I]** Para não expor o e-mail pessoal,
use o endereço "noreply" que o GitHub mostra nas configurações de e-mail da conta.

Todo commit e toda tag que o `git` local cria neste guia (o commit inicial do passo 0, o da publicação no passo 6 e a
tag do passo 7) levam `-c user.name="$NOME" -c user.email="$EMAIL"`, para não gravar a identidade da configuração do
`git` da máquina. O commit do merge é a exceção: quem o cria é o GitHub, com a identidade da conta (passo 7).

**Para ensaiar o guia sem publicar nada**, use no lugar do público um repositório local vazio e pare antes do push do
passo 6:

```bash
git init --bare "<pasta de ensaio>/publico.git"
git clone -c core.autocrlf=false "<pasta de ensaio>/publico.git" "$PUB"
```

No ensaio, do passo 0 só valem as linhas do último bloco que vêm depois do `git clone` (o clone já está feito). A
trava do passo 4 para com uma linha que começa por `PARE:` enquanto o contato do projeto for o provisório, e a do passo
5 para sempre no ensaio, porque o clone não aponta para o GitHub. Nesses casos, a parada com a mensagem `PARE:` é o
resultado esperado, e o passo conta como conferido.

## 0. Antes da primeira versão: o repositório público

Só uma vez, antes da primeira exportação.

**[mantenedor]** Criar no GitHub o repositório público **vazio** (sem README, sem licença, sem `.gitignore`), com o
dono e o nome definitivos. Não configurar segredo nem variável de Actions.

Conferência: a página do repositório mostra as instruções para enviar um repositório existente ("Quick setup").

**[comando pronto]** Clonar o repositório vazio e criar nele a `main` com um commit vazio. **[I]** Escolha deste guia:
uma pull request precisa de uma branch de destino, e o repositório vazio não tem nenhuma; o commit vazio a cria sem
pôr arquivo nenhum. O `core.autocrlf=false` faz o clone guardar os arquivos byte a byte, como a exportação.

```bash
git clone -c core.autocrlf=false https://github.com/<dono>/<repositório>.git "$PUB"
cd "$PUB"
git -c user.name="$NOME" -c user.email="$EMAIL" commit --allow-empty -m "chore: início do repositório"
git branch -M main
git push -u origin main
git ls-remote origin
```

Conferência: o `git ls-remote` mostra a `refs/heads/main`, e a página do repositório mostra um commit, sem arquivos.

## 1. Conferir o repositório de origem

**[mantenedor]** Antes das suítes, completar na origem as notas e o número da versão, num commit:

- no `CHANGELOG.md`, trocar o marcador `AAAA-MM-DD` do título da seção da versão pela data da publicação, completar o
  resumo do que ela traz (na 0.1.0, também a linha que diz em que commit o piloto roda) e tirar da seção os
  comentários `<!-- ... -->`, que o passo 6 levaria à pull request e à release;
- no `CITATION.cff`, pôr a versão em `version` e a mesma data em `date-released` (no formato AAAA-MM-DD).

**[comando pronto]** Conferir as notas e o número da versão logo depois do commit, antes das suítes:

```bash
cd "$ORIGEM"
grep -n -E "^## \[$VERSAO\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md
{ awk -v cab="## [$VERSAO]" 'index($0, cab) == 1 { dentro = 1; next } /^## \[/ { dentro = 0 } dentro' CHANGELOG.md
  cat CITATION.cff; } | grep -c -e '<!--' -e 'AAAA'
grep -n -E '^(version|date-released):' CITATION.cff
```

Conferência: o primeiro comando mostra o título da seção da versão com a data; o segundo imprime `0` (nenhum
comentário nem marcador na seção nem no `CITATION.cff`); o terceiro mostra a mesma versão e a mesma data. A seção
resume o que a versão traz (na 0.1.0, também a linha que diz em que commit o piloto roda). Com outro resultado,
corrija as notas num commit novo e confira de novo.

O ambiente da suíte é o da seção "Testes" do [CONTRIBUTING](../../CONTRIBUTING.md): o `age` baixado pelo
`scripts/fetch_age.py` numa pasta fora do repositório (sem ele, os testes do `age` falham, e não são pulados) e o
perfil do `cq` fora do ambiente, para não vazar para os testes. As duas primeiras linhas do bloco abaixo fazem isso.

**[comando pronto]** Na raiz do repositório de origem, no commit da publicação, com o Docker no ar:

```bash
export CQ_TEST_AGE_DIR="<pasta do age, fora do repositório>"
unset CQ_PROFILE CQ_STACK_DIR
cd "$ORIGEM"
git status --short --untracked-files=no
scripts/test-postgres.sh up
scripts/run-suite.sh "$ROTULO"; echo "suíte: $?"
scripts/run-engine-suite.sh "$ROTULO"; echo "motor: $?"
uv run pytest -q -p no:cacheprovider tests/contract/test_public_tree.py; echo "contrato: $?"
scripts/test-postgres.sh down
```

- O `git status --short --untracked-files=no` tem de sair vazio: a exportação lê só o último commit, e a alteração
  pendente num arquivo rastreado ficaria de fora sem aviso. Os arquivos não rastreados (as linhas `??` de um
  `git status` comum) também não vão à exportação e podem ficar na origem. Se um deles estiver num caminho que a
  exportação leva, o teste de contrato abaixo falha, porque ele trata isso como alteração não commitada.
- O `scripts/test-postgres.sh` sobe e derruba o Postgres descartável dos testes.
- A suíte (`scripts/run-suite.sh`) roda os testes do servidor e do cliente por partes, e a parte de sistema sobe a
  pilha pelo Compose. A suíte do motor (`scripts/run-engine-suite.sh`) constrói as imagens do motor, roda os testes na
  imagem restrita e a conformidade contra o oráculo, o contêiner `pl-oracle`, criado como no workflow do motor
  ([`.github/workflows/engine.yml`](../../.github/workflows/engine.yml)). Rode uma suíte de cada vez.
- O teste de contrato dos arquivos públicos exporta o último commit para um diretório temporário e confere a árvore
  que sairia: os arquivos obrigatórios, os links, os contatos e o dono único do repositório, e os princípios.

Conferência: o `git status --short --untracked-files=no` não imprime nada, e as três linhas `suíte: 0`, `motor: 0` e
`contrato: 0` aparecem.

**[mantenedor]** Ler o documento público dos princípios ([principios.md](../explicacao/principios.md)) contra o
documento de princípios do repositório de origem, de onde ele é derivado. O teste de contrato só confere que os títulos
dos sete princípios e o das restrições técnicas aparecem no público; esta leitura confere o sentido.

Conferência: cada princípio e cada restrição técnica do documento de origem tem o mesmo sentido no público. Uma
diferença é corrigida no público, na origem, num commit, e o passo 1 recomeça.

## 2. Exportar para um diretório vazio

**[comando pronto]** Na raiz do repositório de origem:

```bash
cd "$ORIGEM"
DEST="$(mktemp -d)"
uv run python ops/export/export_public.py --dest "$DEST" --author-name "$NOME" --author-email "$EMAIL"; echo "exportação: $?"
git -C "$DEST" log --oneline
```

A exportação lê o último commit da origem, copia para o destino só os arquivos que casam a lista positiva, põe na raiz
as versões públicas do `AGENTS.md`, do `CLAUDE.md` e do `ROADMAP.md`, varre tudo contra segredos e dados pessoais e,
sem achado, cria no destino um repositório git novo, com um só commit. Ela sai sem deixar nada no destino quando:

| Código | Quando |
| --- | --- |
| 64 | o destino não é um diretório vazio fora da origem, ou o nome ou o e-mail do autor são inválidos |
| 65 | a varredura acha um possível segredo ou dado pessoal (a lista `arquivo:linha: tipo`, sem o valor), ou a sobreposição pública está fora do esperado |
| 69 | o `git` não foi encontrado |
| 1 | um erro inesperado: a mensagem fixa `Erro interno da exportação.`, sem detalhe |

Conferência: a saída tem `Exportação concluída.`, depois `exportação: 0`, e o `git log` mostra um só commit. A
mensagem desse commit é fixa e não vai ao público: o passo 3 copia só a árvore.

**[mantenedor]** Se a varredura listar achados: abrir cada `arquivo:linha` na origem e decidir. Um segredo ou dado
pessoal real sai da origem num commit novo (e um segredo é trocado, porque já passou pelo histórico privado). Um valor
de fato fictício entra na lista de fictícios (`ops/export/fictitious.txt`). Depois, recomeçar do passo 1.

Conferência: a nova exportação termina em `Exportação concluída.`.

Para rodar o job rápido do CI num contêiner local sobre a exportação, antes do push, veja a seção "Validar o CI
localmente" do guia [CI e exportação](ci-e-exportacao.md) (`ops/ci/validate_local.py --export-dir "$DEST"`).

## 3. Copiar a exportação por cima do clone público

**[comando pronto]** No clone do repositório público:

```bash
cd "$PUB"
git status --short
git switch main
git pull --ff-only
git switch -c "$RAMO"
git rm -r -q --ignore-unmatch -- .
git fetch -q --no-tags "$DEST" HEAD
git checkout FETCH_HEAD -- .
```

O `git status --short` do começo tem de sair vazio. O `git rm` apaga do índice e da árvore de trabalho todos os
arquivos da publicação anterior (o `.git` fica); o `git fetch` traz a árvore exportada para o clone; e o
`git checkout FETCH_HEAD -- .` a copia para o índice e para a árvore de trabalho. **[I]** Copiar pelo `git`, e não por
um copiador de arquivos, leva os bytes e as permissões de execução exatamente como na exportação, também no Windows.

**[comando pronto]** Conferir a cópia:

```bash
git diff --cached --quiet FETCH_HEAD && echo "Cópia idêntica à exportação."
git status --short | grep '^??'
git diff --cached --stat main
```

Conferência: o primeiro comando imprime `Cópia idêntica à exportação.`; o segundo não imprime nada (nenhum arquivo
fora da exportação ficou no clone); o terceiro lista só os arquivos que a publicação muda em relação à anterior (na
primeira versão, todos), coerentes com a seção `$SECAO` do CHANGELOG.

## 4. Conferir o contato público

Até o projeto ter um e-mail público, o código de conduta e a política de segurança usam o endereço reservado de
exemplo `canal-questoes@example.org`. Esta trava impede publicar com ele.

**[comando pronto]** No clone público:

```bash
if git grep -q -i -F "canal-questoes@example.org" -- '*.md' '*.cff' '.github/' ':(exclude)tests/' ':(exclude)docs/operacao/publicacao-de-versoes.md'; then
  echo "PARE: o endereço reservado de exemplo ainda está nos textos."
elif ! grep -q -v -e '^#' -e '^[[:space:]]*$' ops/export/public-contacts.txt; then
  echo "PARE: a lista de contatos públicos está vazia."
else
  echo "Contato conferido."
fi
```

Conferência: o comando imprime `Contato conferido.`.

Com `PARE:`, a versão não sai. **[mantenedor]** No repositório de origem, as duas coisas, porque a trava exige as
duas: primeiro, pôr o e-mail público do projeto na lista de contatos públicos
([`ops/export/public-contacts.txt`](../../ops/export/public-contacts.txt): uma linha, no máximo um endereço, que tem
de aparecer num arquivo de contato: `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`
ou `CITATION.cff`); depois, trocar o endereço reservado em cada arquivo que o comando abaixo listar. Trocar só os
textos não basta: a trava para de novo, com a mensagem da lista vazia. Os caminhos são os mesmos na origem, salvo os três arquivos da raiz que vêm da sobreposição pública
(`AGENTS.md`, `CLAUDE.md` e `ROADMAP.md`), que se editam na pasta dela. Depois, fazer o commit e recomeçar do passo 1.
A varredura da exportação só aceita o e-mail real do projeto pela lista de contatos públicos; a lista de fictícios
(`ops/export/fictitious.txt`) é só para valores inventados.

```bash
git grep -l -i -F "canal-questoes@example.org" -- '*.md' '*.cff' '.github/' ':(exclude)tests/' ':(exclude)docs/operacao/publicacao-de-versoes.md'
```

Conferência: depois da correção e da nova exportação, este passo imprime `Contato conferido.`.

## 5. Conferir o dono do repositório

Os badges do README e o `CITATION.cff` apontam para um só par de dono e nome do repositório (o teste de contrato do
passo 1 confere isso). Enquanto o endereço definitivo do repositório não estiver decidido, os textos usam um dono
provisório. Esta trava impede publicar num repositório diferente do que os textos citam.

**[comando pronto]** No clone público:

```bash
PAR="$(git remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
CIT="$(sed -n -E 's#^repository-code:[[:space:]]*"?https://github\.com/([^"/[:space:]]+/[^"/[:space:]]+)/?"?[[:space:]]*$#\1#p' CITATION.cff)"
echo "Repositório público: $PAR"
echo "CITATION.cff: $CIT"
minusculas() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
if [ -n "$CIT" ] && [ "$(minusculas "$PAR")" = "$(minusculas "$CIT")" ]; then
  echo "Dono conferido."
else
  echo "PARE: o CITATION.cff e os badges não apontam para o repositório público."
fi
```

O teste compara o par inteiro, por igualdade e sem diferenciar maiúsculas: um repositório público cujo nome seja só o
começo do citado (como `dono/canal` diante de `dono/canal-questoes`) não passa.

Conferência: o comando imprime `Dono conferido.`, e as duas primeiras linhas mostram o mesmo dono e o mesmo nome.

Com `PARE:`, a versão não sai. **[mantenedor]** Para trocar o dono provisório pelo definitivo: ler o par atual na linha
`CITATION.cff:` acima, listar com o comando abaixo os arquivos que o citam e trocar o par em cada um deles no
repositório de origem. O comando roda no clone público e lista os caminhos dele, que são os mesmos na origem, salvo os
três arquivos da raiz que vêm da sobreposição pública (`AGENTS.md`, `CLAUDE.md` e `ROADMAP.md`): esses se editam na
pasta dela, como no passo 4. Depois, fazer o commit e recomeçar do passo 1.

```bash
git grep -n -i -F "<dono atual>/<repositório atual>"
```

Conferência: depois da troca e da nova exportação, este passo imprime `Dono conferido.`.

## 6. Commit, push e pull request

**[comando pronto]** No clone público, um commit só com a publicação. A mensagem é a do `MENSAGEM`, sem trocá-la e
sem acrescentar outro texto. O autor continua o nome e o e-mail públicos. Quando um agente faz o commit, ponha em `COAUTOR`
a linha de atribuição que ele usa nos commits, no formato `Nome <e-mail>`: o commit ganha no fim a linha
`Co-Authored-By` com ela. Quando uma pessoa faz o commit, deixe `COAUTOR` vazio, e o commit sai sem essa linha.

```bash
COAUTOR=""   # vazio, ou a atribuição do agente que faz o commit, no formato "Nome <e-mail>"
git -c user.name="$NOME" -c user.email="$EMAIL" commit -q -m "$MENSAGEM" ${COAUTOR:+--trailer "Co-Authored-By: $COAUTOR"}
git log -1 --format='%an <%ae>%n%s%n%b'
```

Conferência: o `git log` mostra o nome e o e-mail públicos e a mensagem da publicação (a do `MENSAGEM`). Se um agente
fez o commit, a única linha depois dela é a `Co-Authored-By` com a atribuição do agente; se foi uma pessoa, não há
nenhuma linha `Co-Authored-By`.

**[mantenedor]** Enviar a branch e abrir a pull request, com a seção da publicação no CHANGELOG como descrição:

```bash
git push -u origin "$RAMO"
NOTAS="$(mktemp)"
awk -v cab="## [$SECAO]" 'index($0, cab) == 1 { dentro = 1; next } /^## \[/ { dentro = 0 } dentro' CHANGELOG.md > "$NOTAS"
gh pr create --base main --head "$RAMO" --title "$ROTULO" --body-file "$NOTAS"
gh pr checks "$RAMO" --watch
```

Se o `gh pr checks` disser que não há checks, espere alguns segundos e repita a linha: **[I]** logo depois do
`gh pr create`, os checks podem ainda não ter aparecido.

Conferência: a pull request abre com a descrição da publicação, e o `gh pr checks` termina com todos os checks
aprovados.

**[mantenedor]** A partir da segunda versão, rodar também o job inteiro do CI na
branch. Na primeira versão, ele só roda depois do merge (passo 7): o disparo manual pede o workflow já na branch
padrão. Rode só depois que o `gh pr checks` terminar: o workflow cancela a execução em andamento quando outra começa
na mesma branch.

```bash
gh workflow run ci.yml --ref "$RAMO"
sleep 15
RUN="$(gh run list --workflow ci.yml --branch "$RAMO" --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')"
echo "execução: $RUN"
gh run watch "$RUN" --exit-status; echo "ci manual: $?"
gh run view "$RUN" --json jobs --jq '.jobs[] | "\(.name): \(.conclusion)"'
```

Se a linha `execução:` não mostrar um número, a execução ainda não apareceu: espere alguns segundos e repita a partir
da linha do `RUN`. O `gh run list` mostra só o estado de cada execução; os jobs aparecem no `gh run view`.

Conferência: aparece `ci manual: 0`, e o `gh run view` lista os dois jobs do workflow com `success`. **[I]** Ainda
não conferido se eles saem pelo nome de exibição (`Suíte rápida (sem os testes de sistema)` e
`Suíte inteira (com os testes de sistema)`) ou pela chave (`fast` e `full`); as duas formas valem.

## 7. Merge, tag e release

**[mantenedor]** Antes do merge, conferir a identidade que ele vai gravar. O commit do merge é criado pelo GitHub, e
não pelo `git` local, então os `-c user.name` e `-c user.email` não valem para ele. **[I]** Pelo que este guia sabe
do GitHub, e ainda não conferido na prática:

- o nome do autor é o do perfil da conta, que deve ser o nome público;
- o e-mail é o que o `--author-email` do `gh pr merge` indica, e ele precisa ser um dos e-mails da conta (o "noreply"
  serve);
- sem a opção, o GitHub usa o e-mail principal da conta, ou o "noreply" quando a opção "Keep my email addresses
  private" está ligada nas configurações de e-mail.

**[mantenedor]** No clone público, o merge e o CI da `main`:

```bash
gh pr merge "$RAMO" --merge --author-email "$EMAIL"
git switch main
git pull --ff-only
git log -1 --format='%H %P%n%an <%ae>'
sleep 15
RUN="$(gh run list --workflow ci.yml --branch main --event push --commit "$(git rev-parse HEAD)" --limit 1 --json databaseId --jq '.[0].databaseId')"
echo "execução: $RUN"
gh run watch "$RUN" --exit-status; echo "ci da main: $?"
gh run view "$RUN" --json jobs --jq '.jobs[] | "\(.name): \(.conclusion)"'
```

Como no passo 6, se a linha `execução:` não mostrar um número, espere alguns segundos e repita a partir da linha do
`RUN`. Na primeira versão, esta é a primeira execução do job inteiro no repositório público: faça também as
conferências da seção "Conferir a primeira execução real" do guia [CI e exportação](ci-e-exportacao.md).

O workflow do motor ([`.github/workflows/engine.yml`](../../.github/workflows/engine.yml)) também roda no push,
quando ele muda o motor (`engine/` ou `contracts/engine/`); na primeira versão, isso acontece sempre. **[I]** Ainda não
conferido se, no push do merge, o filtro compara com o commit anterior da `main`. Acompanhe-o também:

```bash
MOTOR="$(gh run list --workflow engine.yml --branch main --event push --commit "$(git rev-parse HEAD)" --limit 1 \
  --json databaseId --jq '.[0].databaseId // empty')"
echo "motor: $MOTOR"
[ -n "$MOTOR" ] && { gh run watch "$MOTOR" --exit-status; echo "motor da main: $?"; }
```

Se a linha `motor:` sair vazia numa publicação que muda o motor, espere alguns segundos e repita a partir da linha do
`MOTOR`; numa publicação que não muda o motor, não há execução a conferir.

Conferência: o `git log -1` mostra três identificadores (o commit do merge e os seus dois pais) e, na linha seguinte,
o nome e o e-mail públicos; aparece `ci da main: 0`, e o `gh run view` lista os dois jobs com `success` (**[I]**, como
no passo 6: pelo nome de exibição ou pela chave, `fast` e `full`); com execução do motor, aparece `motor da main: 0`.
Com o CI ou o motor vermelhos, pare antes da tag e da release. **[I]** A volta desse caso (a correção na origem e uma
nova pull request) ainda não foi ensaiada.

**[mantenedor]** A tag e a release:

```bash
git -c user.name="$NOME" -c user.email="$EMAIL" tag -a "v$VERSAO" -m "v$VERSAO"
git cat-file -p "v$VERSAO" | grep '^tagger'
git push origin "v$VERSAO"
gh release create "v$VERSAO" --title "v$VERSAO" --notes-file "$NOTAS" --verify-tag
gh release view "v$VERSAO"
```

A tag fica no commit do merge, e a release leva a mesma seção do CHANGELOG da pull request. Se o terminal do passo 6
foi fechado, defina de novo as variáveis e refaça as duas linhas do `NOTAS` antes do `gh release create`.

Conferência: a linha `tagger` mostra o nome e o e-mail públicos; o `gh release view` mostra a release `v0.y.z` com as
notas da versão.

## Entre as versões: as pull requests do Dependabot

O Dependabot ([`.github/dependabot.yml`](../../.github/dependabot.yml)) abre toda semana, no repositório público,
pull requests de atualização das dependências. Nenhuma delas recebe merge no público: a cópia do passo 3 substitui a
árvore pública inteira, e uma mudança feita só lá voltaria ao conteúdo da origem na versão seguinte. **[mantenedor]**
Para cada uma, decidir se a atualização entra. A aceita é feita no repositório de origem, com os testes de sempre, e
chega ao público na versão seguinte, pela pull request `versao/v0.y.z`. Aceita ou recusada, a pull request do
Dependabot é fechada sem merge, com um comentário curto que diga o destino (por exemplo, "Aceita: a atualização entra
na próxima versão" ou "Fechada sem merge: o projeto fixa o Python 3.12"). As versões que o projeto fixa de propósito
ficam no `ignore` do arquivo, para o Dependabot não as propor de novo.
