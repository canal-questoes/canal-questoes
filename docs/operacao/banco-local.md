# Banco de questões local

> **Para quem:** o operador do sistema. **Não** é material para alunos: eles só usam o bot do Telegram.
>
> **Verificado em:** 2026-11-26, contra o código do cliente (`client/src/cq_client/`), o do servidor
> (`server/src/cq_server/`) e os contratos da API e do MCP (`contracts/`). O envio a uma pilha real, local ou pelo
> túnel, ainda não foi conferido.

Além de um repositório git público (a referência `https://...` de sempre), o canal-questoes aceita um **banco de
questões local**: uma pasta de curso do PrairieLearn que está no seu computador, com ou sem git. O `cq` lê a pasta,
confere as regras, monta um pacote `.tar.gz` e o envia à API; o servidor guarda o pacote, e o `worker` o publica para
o motor no mesmo lugar em que publica a árvore de um repositório git. Depois do envio, uma questão do banco é
disparada como qualquer outra, com a referência `bank:<nome>` no lugar do endereço do repositório e a versão do banco
no lugar do commit.

Os exemplos usam o banco fictício `meu-curso` e o oferecimento fictício `meu-oferecimento`; troque pelos seus. Os
comandos `cq` usam o perfil escolhido (`--profile NOME` antes do subcomando, a variável `CQ_PROFILE` ou o
`default_profile` do arquivo de perfis; veja o [início rápido](inicio-rapido.md)). O envio exige um token com a
permissão `offerings:write`; a listagem, `offerings:read`. Todas as opções estão na
[referência do CLI](../referencia/cli.md).

## 1. As duas origens: a pasta ou um commit do git local

O comando é sempre `cq banks upload <pasta> --name <nome>`. A pasta é a raiz de um curso do PrairieLearn: precisa ter
o `infoCourse.json` na raiz (com essa caixa exata), senão o `cq` recusa o banco antes de enviar. O caminho pode ser
relativo ao diretório atual; o `cq` o torna absoluto e nunca o envia à API.

**A pasta como ela está.** Sem `--git`, o `cq` envia os arquivos da pasta, do jeito que estão no disco:

```bash
cq banks upload ~/bancos/meu-curso --name meu-curso
```

- Entra todo arquivo comum, inclusive os ocultos, o `__pycache__` e o lixo de sistema (como `.DS_Store` ou
  `Thumbs.db`): se eles mudarem, a versão muda. Limpe a pasta antes de enviar, se não quiser isso.
- Ficam de fora qualquer pasta `.git` (em qualquer nível e em qualquer caixa) e as pastas vazias.
- São recusados: um link simbólico, uma junção ou outro ponto de reanálise do Windows (também na raiz), um arquivo
  especial (dispositivo, fila, soquete) e um caminho de rede (UNC, como `\\servidor\pasta`) ou de dispositivo.
- Uma pasta que é árvore de trabalho de um git, enviada sem `--git`, vai como pasta (origem `folder`), com as mudanças
  ainda não commitadas.

**Um commit de um git local.** Com `--git <revisão>`, o `cq` envia o que está guardado **naquele commit**, não o que
está na pasta:

```bash
cq banks upload ~/bancos/meu-curso --name meu-curso --git main
```

- A revisão é obrigatória (um ramo, uma etiqueta ou um hash): sem valor, não há padrão. Ela aceita letras, dígitos e
  os sinais `_ . / @ { } ~ ^ -`, sem começar por hífen, até 256 caracteres. Uma etiqueta anotada vale pelo commit dela.
- A pasta pode ser a raiz do repositório ou uma subpasta dele: vai a árvore dessa subpasta no commit, e o
  `infoCourse.json` precisa estar na raiz dela.
- As mudanças não commitadas são ignoradas, sem aviso.
- O conteúdo é o que o git guarda, sem filtros nem atributos: nenhuma conversão de fim de linha e nenhum arquivo do
  LFS (veja a seção 7).
- São recusados um link simbólico e um submódulo no commit. Fica de fora qualquer caminho `.git`.
- O `cq` procura o `git` só nas pastas absolutas do `PATH` (nunca no diretório atual nem dentro do banco) e o roda sem
  as variáveis `GIT_*` herdadas e sem busca de objetos na rede.

A saída do envio diz o nome, a versão, se a versão é nova ou já existia, a origem (com o commit do git, na segunda
origem), as contagens (arquivos, bytes e questões) e a linha pronta do disparo (seção 8):

```text
Banco enviado: meu-curso, versão <versão> (nova).
Origem: folder.
Arquivos: <n>; bytes: <n>; questões: <n>.
Para disparar: cq dispatches prepare <oferecimento> --repo bank:meu-curso --commit <versão> --qid <qid>
```

A contagem de questões é a dos `info.json` dentro de `questions/`, sem contar uma questão que fica dentro da pasta de
outra.

## 2. O nome e a referência

O nome (`--name`) identifica o banco e vira a referência dele nos disparos: o banco `meu-curso` é a referência
`bank:meu-curso`. A regra é a do código do oferecimento: de 3 a 40 caracteres, só letras minúsculas, dígitos e hífen,
começando por letra ou dígito.

- O nome é escolha sua e não precisa ser o do curso nem o da pasta. O mesmo conteúdo enviado com dois nomes vira dois
  bancos, cada um com a sua referência.
- Não use o nome de uma pessoa (de um aluno, por exemplo) no nome do banco: ele sai redigido em toda saída, como o
  endereço de um repositório.
- Para ver os bancos enviados e as versões de cada um, em ordem do primeiro envio:

```bash
cq banks list
cq banks list --name meu-curso
```

Cada linha traz o nome, a versão, a referência, a origem, o commit do git (ou `-`), as contagens e o horário do envio;
a última linha é o cursor da próxima página (`--after` e `--limit`, de 1 a 500, como nas outras listas).

## 3. A versão

A versão do banco é calculada pelo conteúdo: o SHA-256 (64 dígitos hexadecimais) de uma lista com, para cada arquivo,
o caminho relativo, o tamanho e o SHA-256 do conteúdo, em ordem dos bytes do caminho. Ela faz o papel do commit na
referência da questão.

- Nada além disso entra: nem a data, nem o dono, nem as permissões, nem a compressão, nem a origem. Por isso o mesmo
  conteúdo dá a mesma versão pela pasta ou pelo `--git`, em qualquer computador.
- Qualquer byte trocado, um arquivo a mais ou um nome diferente dão outra versão.
- O `cq` calcula a versão e o servidor a calcula de novo a partir do pacote; se as duas divergirem, o envio é
  recusado.
- Reenviar o mesmo conteúdo com o mesmo nome é seguro: a resposta diz "já existia" e devolve o primeiro envio (com a
  origem e o commit dele), sem guardar nada de novo. Um envio que falhou no meio pode ter sido guardado ou não: se
  a conexão caiu durante o envio do pacote, nada fica; se o prazo acabou depois de o pacote chegar inteiro, o
  servidor pode ter terminado a gravação. Nos dois casos, basta repetir: o reenvio responde "nova" ou "já existia".

## 4. Os limites

Os limites valem nos dois lados: o `cq` os confere antes de enviar (e recusa sem falar com a API), e o servidor
os confere de novo, porque não confia no cliente.

| Limite | Valor |
| --- | --- |
| Arquivos | até 20000 |
| Conteúdo (a soma dos tamanhos dos arquivos) | até 100 MiB |
| Pacote `.tar.gz` | até 128 MiB |
| Corpo do pedido (o pacote em base64, dentro do JSON) | até 180 MiB |
| Cada nome de pasta ou de arquivo | até 255 bytes em UTF-8 |
| O caminho inteiro | até 4096 bytes em UTF-8 |

O caminho de cada arquivo precisa estar em UTF-8 válido, sem barra invertida, sem caractere de controle e sem
componente vazio, `.` ou `..`; um arquivo não pode ter o nome de uma pasta do mesmo banco. Na publicação da árvore, o
`worker` confere de novo os arquivos e o conteúdo, com os tetos dele (`CQ_REPO_MAX_FILES` e `CQ_REPO_MAX_BYTES`, os
mesmos da obtenção pelo git; os padrões são os da tabela).

O envio tem um prazo próprio no `cq`, de 300 s para escrever e ler (a conexão continua com 5 s, e os outros comandos
não mudam). A API recebe um envio por vez; um segundo envio simultâneo espera o primeiro acabar.

## 5. Perfil remoto (a VM pelo túnel)

O envio funciona igual no perfil local e no remoto. No remoto, o `cq` lê o banco no **seu** computador e envia o
pacote à API da VM pelo túnel SSH; nada precisa ser copiado para a VM, e a VM não precisa de git para isso. O túnel e
o perfil remoto estão no [guia da VM](implantacao-vm.md), seção 12.

```bash
cq --profile piloto banks upload ~/bancos/meu-curso --name meu-curso
```

- Com o túnel fechado, o `cq` não alcança a API, e nada é enviado.
- Um banco grande por uma conexão lenta pode passar do prazo de 300 s do envio; o reenvio é seguro e diz se a
  versão já tinha ficado guardada (seção 3).
- O banco fica guardado na pilha para a qual foi enviado: um banco enviado à pilha local não existe na VM, e
  vice-versa. Envie-o à pilha onde o disparo vai rodar.

## 6. O que fica guardado e por quanto tempo

O servidor guarda, no banco do Postgres da pilha, o pacote enviado com o nome, a versão, a origem, o commit do git
(quando houver), as contagens, o horário e o token que enviou. O caminho local da pasta nunca sai do seu computador.

- **Imutável.** Cada par de nome e versão é imutável: o mesmo nome com outro conteúdo é uma versão nova, ao lado da
  antiga, e os disparos que já usam uma versão continuam com ela.
- **Por quanto tempo.** Nesta versão, um banco enviado fica guardado enquanto o sistema existir, como as árvores dos
  repositórios git que o `worker` guarda no volume `repos`: não há comando para remover um banco, nem pelo CLI, nem
  pelo agente. Veja a [política de retenção](politica-de-retencao.md).
- **A árvore publicada.** O `worker` monta, a partir do pacote, a árvore que o motor lê, no volume `repos`. Se o
  volume perder a árvore (recriado, ou numa VM nova), o `worker` a publica de novo a partir do pacote guardado, sem
  ação sua.
- **Os backups.** O pacote está no banco do Postgres, e o backup é um dump desse banco inteiro; logo, os bancos
  enviados entram nos backups (veja o [guia de backup e restauração](backup-e-restauracao.md)). Uma restauração de um
  backup anterior ao envio perde o banco: envie-o de novo com o mesmo nome (o mesmo conteúdo dá a mesma versão).
- **Um registro no log de eventos.** Cada envio que cria uma versão grava o evento `bank.uploaded`, com o nome
  (redigido), a versão, a origem, o commit e as contagens; nenhum conteúdo nem nome de arquivo do banco.

## 7. Avisos: LFS, caixa e NFC/NFD

- **Arquivos do Git LFS.** Com `--git`, o `cq` envia o que o commit guarda. Para um arquivo do LFS, o commit guarda só
  o ponteiro (um texto curto que aponta o arquivo grande), e é o ponteiro que vai no lugar do arquivo; a questão que
  depende dele pode quebrar. Sem `--git`, vai o que está no disco: o arquivo de verdade, se o LFS já o baixou, ou o
  ponteiro, se não. O `cq` não trata o LFS.
- **Caixa diferente no mesmo caminho.** Num disco que diferencia maiúsculas (o Linux), `Figura.png` e `figura.png`
  podem conviver; os dois entram no banco, porque o servidor e o motor rodam em Linux. Essa árvore não é portável:
  num disco que não diferencia maiúsculas (o padrão no Windows e no macOS), os dois nomes não cabem na mesma pasta.
  Evite.
- **Acentos em NFC ou NFD.** Um nome com acento pode estar gravado de duas formas Unicode: composta (NFC, o comum no
  Windows e no Linux) ou decomposta (NFD, comum em pastas copiadas do macOS). O `cq` não normaliza os nomes: a versão
  é calculada sobre os bytes do caminho, então a mesma pasta em NFC e em NFD dá duas versões diferentes. Além
  disso, o `qid` do disparo só aceita letras e dígitos ASCII, `_`, `.`, `-` e `/`: uma questão numa pasta com acento
  entra no banco, mas o preparo do disparo a recusa (`qid`, `invalid_format`). Use nomes sem acento nas pastas de
  questões.

## 8. Disparar uma questão do banco

O preparo do disparo é o de sempre, com a referência `bank:<nome>` no `--repo`, a versão do banco no `--commit` e o
caminho da questão dentro de `questions/` no `--qid`. A linha pronta sai no fim do envio; ela tem esta forma:

```bash
cq dispatches prepare meu-oferecimento --repo bank:meu-curso --commit <versão> --qid <qid>
```

Daí em diante, a prévia, a confirmação, a entrega e a correção seguem como num repositório git. Uma questão com
elementos que o sistema não suporta é recusada na validação do disparo, com o motivo de sempre; o envio aceita o banco
inteiro, e a recusa é por questão.

## 9. Pelo agente

O servidor MCP (`cq mcp`) tem duas ferramentas para o banco local: `upload_question_bank`, que recebe o caminho
**absoluto** da pasta (`path`), o nome (`name`) e, opcionalmente, a revisão do git (`git_rev`, a mesma regra do
`--git`), e `list_question_banks`, que lista as versões. O servidor MCP lê a pasta no próprio processo e envia o
pacote; o conteúdo do banco não passa pelo modelo. Numa recusa local, a ferramenta devolve só a mensagem fixa do
motivo, sem o nome do arquivo que a causou (o nome de um arquivo do banco é conteúdo, e não vai ao modelo); para ver
qual arquivo foi recusado, rode o mesmo envio no `cq`. A skill de disparo do plugin envia o banco antes do preparo.
Veja a [operação pelo agente](plugin-e-agente.md) e a [referência do MCP](../referencia/mcp.md).

## 10. Quando o envio é recusado

**Recusa do `cq`, antes de enviar.** O `cq` sai com o código 2, mostra a mensagem fixa do motivo e, quando há, o
caminho do arquivo **dentro** do banco que causou a recusa (nunca o caminho local da pasta). Nada é enviado.

| Situação | O que fazer |
| --- | --- |
| Falta o `infoCourse.json` na raiz | aponte a raiz do curso, não uma pasta acima nem `questions/` |
| Link simbólico, junção ou submódulo | troque o link pelo arquivo, ou envie sem a parte que o tem |
| Junção recusada numa pasta do OneDrive (ou de outro serviço de sincronização) | no Windows, os arquivos guardados só na nuvem devem aparecer como pontos de reanálise (o projeto não conferiu esse caso); baixe a pasta para o disco ("sempre manter neste dispositivo") ou copie o banco para uma pasta local |
| Caminho fora da regra (seção 4) | renomeie o arquivo ou a pasta citada |
| Arquivo ou pasta ilegível, ou trocado durante a leitura | confira as permissões; se um programa estava salvando o arquivo (um editor, uma sincronização), espere terminar e envie de novo |
| Mais de 20000 arquivos, ou mais de 100 MiB, ou pacote acima de 128 MiB | tire do banco o que o curso não usa |
| O `git` não foi encontrado, ou a pasta não está num repositório | instale o `git`, ou envie sem `--git`; se a pasta é um repositório de outro usuário, o `git` pode recusá-la por segurança (a configuração `safe.directory`): rode `git status` nela para ver a mensagem do próprio `git` |
| A revisão não existe, ou está fora da forma | confira o ramo, a etiqueta ou o hash |
| A pasta não existe nessa revisão | a subpasta foi criada depois do commit: faça o commit ou escolha outra revisão |

**Recusa da API.** O `cq` sai com o código 1, e a API responde `invalid_request`, com o campo e o motivo
(`fields[].reason`), sem eco de caminho nem de conteúdo. Entre os motivos do pacote (no campo `archive`) estão
`too_large`, `path_not_allowed`, `entry_not_allowed` e `not_a_course`, e a versão que não confere é `mismatch` (no
campo `version`). Com o `cq`, essas recusas indicam um pacote montado fora do `cq` ou um cliente desatualizado:
atualize o `cq` e envie de novo. A lista completa está na [referência da API](../referencia/api.md).
