# CI e exportação

Guia do Pedro para publicar o canal-questoes num repositório público novo e ligar o CI do GitHub Actions: exportar
para um diretório temporário, revisar o que vai sair, validar os passos do CI num container local, criar o repositório,
dar o push e conferir a primeira execução real
do CI no repositório público.

Marcas usadas em cada passo:

- **[passo do Pedro]**: algo que só o Pedro faz (decidir, criar o repositório, dar o push, olhar a execução);
- **[comando pronto]**: um comando para copiar e colar, sem segredo na linha;
- cada passo termina com uma linha `Conferência:`, que diz como saber que deu certo.

Regras que valem para o guia inteiro:

- Nenhum segredo vai ao repositório público nem ao CI: o CI não usa nenhum segredo do repositório, e nada
  aqui pede token, senha ou chave. Se o GitHub pedir credencial no push, ela é digitada só no prompt do próprio `git`
  ou do navegador, nunca em chat, documento ou linha de comando.
- A exportação **não escreve no repositório de origem** nem muda o histórico dele: ela monta um
  repositório git **novo**, num destino vazio fora da origem, com um único commit e sem linha de coautoria. O
  histórico de desenvolvimento (com as linhas de coautoria) continua só no repositório privado.
- O que sai é só o que está na lista positiva `ops/export/allowlist.txt`. Ficam fora, por padrão,
  as notas internas de desenvolvimento, a base de conhecimento, a monografia, as especificações (nenhum arquivo
  de `specs/` sai) e as configurações do agente e do processo.
- Os comandos abaixo são do PowerShell, na raiz do repositório de origem, no computador do Pedro.

## 1. Exportar para um temporário

**[passo do Pedro]** Escolher o nome e o e-mail públicos do autor do commit inicial. Eles ficam visíveis para
qualquer pessoa no repositório público. **[I]** Uma opção que não expõe o e-mail pessoal é o endereço "noreply" que
o GitHub mostra nas configurações de e-mail da conta. A origem deve estar sem alteração pendente (tudo num commit),
para a exportação refletir o que foi revisado.

Conferência: `git status --short` na origem não mostra nenhum arquivo modificado.

**[comando pronto]** Criar um diretório vazio fora do repositório e exportar para ele (troque os dois valores entre
`< >` pelos escolhidos acima):

```powershell
$dest = Join-Path $env:TEMP ("cq-export-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $dest | Out-Null
uv run python ops/export/export_public.py --dest $dest --author-name "<nome público>" --author-email "<e-mail público>"
```

O script recusa sem deixar nada no destino quando o destino não está vazio ou fica dentro da origem (código 64),
quando a varredura acha um possível segredo ou dado pessoal (código 65; lista `arquivo:linha: tipo`, sem o valor),
quando há alteração pendente num arquivo da lista ou a sobreposição pública (a pasta, só da origem, com as versões
públicas do `AGENTS.md`, do `CLAUDE.md` e do `ROADMAP.md`) não tem exatamente esses três arquivos (também código 65) e
quando o `git` não é encontrado (código 69). A varredura cobre todos os arquivos exportados, inclusive a lista de
instáveis do CI que a exportação gera (`ops/ci/known-flaky.txt`); os valores fictícios aceitos ficam em
`ops/export/fictitious.txt` e os binários permitidos em `ops/export/binary-allowlist.txt`.

Conferência: a saída termina em "Exportação concluída." e o código de saída (`$LASTEXITCODE`) é 0.

**[passo do Pedro]** Se a varredura listar achados: abrir cada `arquivo:linha` na origem, num editor, e decidir. Um
segredo ou dado pessoal real sai da origem num commit novo (e, se for segredo, é trocado, porque já passou pelo git
privado). Um valor que é de fato fictício só entra na lista de fictícios por uma mudança revisada (os testes de
contrato conferem as listas de `ops/export/`). Depois, exportar de novo para um diretório novo.

Conferência: a nova exportação termina em "Exportação concluída.".

## 2. Revisar a árvore e o histórico

**[comando pronto]** Ver o commit único, o autor e a mensagem, e a lista de arquivos:

```powershell
git -C $dest log --format="%H %an <%ae>%n%s%n%b"
git -C $dest ls-files | Measure-Object -Line
git -C $dest ls-files | Select-String -Pattern "^docs/|^specs/|^\.github/"
```

Conferência: há um único commit, com o nome e o e-mail públicos escolhidos, a mensagem "Versão inicial do
canal-questoes" e nenhuma linha `Co-Authored-By`; a lista de `docs/` mostra só o `docs/README.md` e as pastas
`docs/diagramas/`, `docs/explicacao/`, `docs/operacao/`, `docs/referencia/` e `docs/tutorial/`; nenhum caminho começa
com `specs/`; os dois workflows (`.github/workflows/ci.yml` e `.github/workflows/engine.yml`) estão na lista.

**[passo do Pedro]** Percorrer a árvore no Explorador de Arquivos (ou no editor) e conferir que nada pessoal ou
interno foi junto: nenhum arquivo de ambiente real, nenhum dump, nenhum dado de aluno, nenhuma nota do processo de desenvolvimento. A
varredura automática acha os padrões conhecidos; esta revisão é a segunda barreira.

Conferência: nada na árvore que o Pedro não publicaria.

**[comando pronto]** Dar à branch do commit o nome `main`. **[I]** A exportação roda o `git` sem a configuração
global do Pedro, então o nome da branch inicial é o padrão do próprio `git` (em geral `master`); o job inteiro do CI
só roda sozinho em push na `main`.

```powershell
git -C $dest branch -M main
git -C $dest branch --show-current
```

Conferência: o segundo comando imprime `main`.

## 3. Validar o CI localmente

**[comando pronto]** Rodar os passos do job rápido num container Linux descartável e fixado, sobre a exportação
final. Esta validação já rodou durante o desenvolvimento, com a permissão liberada pelo Pedro em 2026-11-03: o job rápido
saiu verde no container. Este passo a repete sobre a pasta que vai ao GitHub.
Precisa de rede (o container baixa a imagem fixada e as dependências Linux) e do Docker Desktop no ar; não instala
nada global no computador.

```powershell
uv run python ops/ci/validate_local.py --export-dir $dest
```

O script cria só recursos Docker com o prefixo `cq-ci-local-`, roda dentro do container o mesmo passo do age Linux
do workflow (`scripts/fetch_age.py`) e o `ops/ci/ci_suite.py fast`, remove só o que criou e nunca monta o socket do
Docker. Ele não toca na pilha `cq-demo` nem em outro recurso.

Conferência: o código de saída (`$LASTEXITCODE`) é 0; o resumo diz "Suíte verde." (ou lista uma repetição única de
instável conhecido que passou); `docker ps -a --filter name=cq-ci-local-` não mostra nada depois.

**[comando pronto]** Conferir que a validação não deixou arquivo novo no repositório exportado:

```powershell
git -C $dest status --short
```

Conferência: o comando não imprime nada. Se imprimir, não faça commit desses arquivos: exporte de novo para um
diretório novo e repita a partir da seção 2.

Os jobs com Docker (o inteiro, com os testes de sistema, e o do motor) não rodam neste container: eles são validados
pelos scripts locais existentes (`scripts/run-suite.sh` e `scripts/run-engine-suite.sh`), como no desenvolvimento.

## 4. Criar o repositório público e dar o push

Na publicação de uma versão, esta seção e a seção 6 dão lugar ao guia [Publicação de versões](publicacao-de-versoes.md):
lá, cada versão entra no público por uma pull request, com o CI conferido antes do merge. Daqui valem as seções 1 a 3
e as conferências da seção 5.

**[passo do Pedro]** Criar no GitHub um repositório **vazio** (sem README, sem licença, sem `.gitignore`, para não
haver commit no remoto) e público, com o nome escolhido. Não configurar nenhum segredo nem variável de Actions: o CI
não usa nenhum.

Conferência: a página do repositório mostra as instruções de "push an existing repository".

**[comando pronto]** Ligar o remoto e dar o push (troque `<dono>` e `<nome>`):

```powershell
git -C $dest remote add origin https://github.com/<dono>/<nome>.git
git -C $dest push -u origin main
```

Conferência: a página do repositório mostra os arquivos e o commit "Versão inicial do canal-questoes", com o autor
público; a aba Actions mostra uma execução do workflow `ci` disparada pelo push.

## 5. Conferir a primeira execução real

**[passo do Pedro]** Abrir a execução do `ci` disparada pelo push na `main`. Como o push foi na `main`, rodam os dois
jobs, o rápido (`fast`) e o inteiro (`full`). **[I]** O do motor (`engine.yml`) deve rodar também, porque o push do
commit inicial traz arquivos de `engine/` (o filtro `paths`); se não rodar, dispare-o à mão pela aba Actions. **[F]** O disparo manual só aparece com o arquivo do workflow na branch
padrão.

Conferência: o job `fast` termina verde.

**[passo do Pedro]** Conferir o serviço Postgres: o passo de inicialização do job mostra a imagem
`postgres:18.6@sha256:…` com o mesmo digest do `scripts/test-postgres.sh`, baixada e saudável. **[I]** A documentação
do GitHub não diz se `services.<id>.image` aceita digest; esta é a primeira prova real.

Conferência: o log de "Initialize containers" mostra o pull pelo digest e o serviço saudável, sem erro de referência
de imagem.

**[passo do Pedro]** Conferir as ações fixadas por SHA: o passo "Set up job" lista cada ação `dono/nome@<SHA>` e as
baixa sem erro.

Conferência: nenhum erro "Unable to resolve action" no log; cada SHA do log é o do workflow.

**[passo do Pedro]** Anotar o tempo do job `fast` (o limite é 45 minutos; o `full` e o `engine` têm 120). Um `fast`
perto do limite pede revisão do número de processos do pytest.

Conferência: o tempo do `fast` está bem abaixo de 45 minutos, e o valor fica anotado para a monografia.

**[passo do Pedro]** Conferir, no job `full`, o passo "Obter a imagem base do PL", que vem antes da suíte: ele lê a
referência da linha `FROM` do `engine/Dockerfile` (o commit e o digest do PL) e baixa essa imagem. **[F]** Na primeira
execução do `full` no repositório público (2026-11-19), o job ainda não tinha esse passo e saiu vermelho: os dois
testes de sistema do motor (`test_engine_effective_isolation` e `test_engine_http_adapter_real_image`, em
`tests/system/test_engine_system.py`) falharam com a mensagem de que a imagem base do PL não estava no host. Eles
exigem a imagem já presente e não a baixam, de propósito. O passo entrou depois dessa execução.

Conferência: o log do passo mostra a linha `imagem base do PL:` com a mesma referência do `engine/Dockerfile` e o
download terminado sem erro; nenhum teste de sistema do motor falha por falta da imagem.

**[passo do Pedro]** Ler o resumo da execução: ele lista as repetições únicas de instáveis conhecidos, se houve
alguma. Uma falha que só aparece no Linux (a suíte nunca tinha rodado inteira em Linux) não é
consertada às pressas no repositório público: ela vai à triagem de falhas (`scripts/run-suite-triage.py` e a
lista de instáveis conhecidos, no repositório privado), e a correção chega numa nova exportação.

Conferência: os jobs `fast`, `full` e `engine` verdes, ou cada falha anotada com o nome do teste e o job, para a
triagem.

## 6. Exportar de novo depois

As versões seguintes também seguem o guia [Publicação de versões](publicacao-de-versoes.md).

**[passo do Pedro]** A exportação não sincroniza histórico: cada versão é uma nova exportação para um diretório
novo. Ela entra por cima do clone do repositório existente, numa branch, e chega
à `main` por uma pull request, como manda o guia de publicação (passos 2 a 7). O script não automatiza essa cópia.

Conferência: a pull request da publicação com o CI verde, como nos passos 6 e 7 do guia de publicação.
