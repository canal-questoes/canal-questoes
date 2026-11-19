# Operar o canal-questoes pelo agente

Guia de início rápido do plugin do Claude Code e roteiro de validação. Pressupõe o
[guia de início rápido](inicio-rapido.md): o `cq` instalado, o `.env` da pilha e o arquivo de perfil.

> **Pré-requisito:** o servidor MCP vem no pacote do `cq`, com o SDK oficial `mcp` 2.3.x; a instalação do início
> rápido já o traz. Depois de atualizar o repositório, reinstale o `cq` com `uv tool install --reinstall ./client`.

## 1. Um token só do agente

O agente usa um token próprio, separado do seu: com a permissão de revelar e **sem** a de gerir tokens. No **seu**
terminal (o segredo vai a um arquivo novo, nunca ao modelo):

```bash
cq tokens create --label agente \
  --permission people:read --permission people:write --permission events:read \
  --permission offerings:read --permission offerings:write --permission bots:manage \
  --permission identity:reveal \
  --secret-file ~/cq-token-agente.txt
```

Não dê `tokens:manage` ao agente. Copie o segredo para um perfil próprio no arquivo de perfil (fora do repositório) e
apague o arquivo do segredo:

```toml
[profiles.agente]
kind = "local"
api_url = "http://127.0.0.1:8000"
token = "<segredo do arquivo>"
# recomendado: sem ele, vale o CQ_STACK_DIR ou a busca a partir da pasta de trabalho do servidor
stack_dir = "<caminho absoluto do repositório>"
compose_project = "cq"
```

O servidor escolhe o perfil como o CLI: `CQ_PROFILE`, depois o `default_profile` do arquivo. Abra o Claude Code com
`CQ_PROFILE=agente` no ambiente (ou torne-o o padrão). O token nunca vai em argumento de ferramenta nem no plugin.
Não deixe `CQ_TOKEN` nem `CQ_API_URL` no ambiente em que abre o Claude Code: elas vencem o campo do perfil, e o
servidor usaria, por exemplo, o seu token em vez do token do agente.

## 2. Habilitar o plugin a partir do repositório

Para uma sessão só (desenvolvimento e validação manual):

```bash
claude --plugin-dir ./plugins/canal-questoes
```

Para deixar instalado, pelo marketplace local (carrega no lugar; edições valem após `/reload-plugins`):

```bash
claude plugin marketplace add ./plugins
# dentro do Claude Code (abre a ficha do plugin; confirme a instalação ali):
/plugin install canal-questoes@canal-questoes-local
```

O repositório **não** habilita o plugin sozinho, nem traz configurações de permissão do Claude Code: as seis
ferramentas que pedem a sua aprovação (seção 4) perguntam por uma marca que o próprio servidor põe nelas.

Sem plugin, o mesmo servidor pode ser configurado direto (`claude mcp add cq -- cq mcp`); não use os dois ao mesmo
tempo com o mesmo comando: o Claude Code conecta só um deles.

## 3. Conferir a conexão

1. `/mcp` mostra `plugin:canal-questoes:cq` conectado.
2. Peça ao agente: "consulte a pilha" (`stack_status`) e "liste os oferecimentos" (`list_offerings`). As duas são só
   leitura e não estão entre as seis que pedem aprovação a cada chamada.
3. As skills aparecem como `/canal-questoes:<nome>`. A de revelação (`/canal-questoes:revelacao`) só roda quando
   **você** a chama.

## 4. O que pede a sua aprovação

O Claude Code pergunta **a cada chamada**, mesmo no modo `auto`, em seis ferramentas (as confirmadoras):
`send_invites`, `confirm_dispatch`, `archive_offering`, `purge_offering`, `stack_down` e `reveal_identities`. A
pergunta vem da marca de interação: o servidor põe `_meta["anthropic/requiresUserInteraction"] = true` nessas seis
ferramentas, e o Claude Code então mostra o pedido de permissão em toda chamada, mesmo nos modos `auto`,
`acceptEdits` e `bypassPermissions`, sem a opção de não perguntar de novo (documentação oficial,
code.claude.com/docs/en/mcp, seção "Require approval for a specific tool", lida em 2026-11-12). As quatro primeiras e
a `stack_down` exigem antes a fase de preparo, que mostra o resumo e devolve o código (no disparo, o identificador do
disparo). A identidade revelada entra no contexto da conversa e fica no histórico da sessão.

**Isso vale só para as ferramentas MCP.** Permissões amplas de terminal no Claude Code (como as de uma sessão de
desenvolvimento: `Bash(uv *)`, `Bash(python *)`, `Bash(node *)`, `Bash(docker compose *)`, `Bash(docker exec *)`,
`Bash(docker run *)` e outras; qualquer uma de interpretador equivale a acesso total) deixam o agente, pelo terminal e
sem perguntar, revelar identidades, ler os registros sem redação, derrubar a pilha sem as duas fases, ler o arquivo
de perfil (com o token), ler o banco pelo `psql` do contêiner (`docker exec`, `docker compose exec`: as identidades de
todos os alunos) e ler o ambiente dos contêineres (a chave-mestra e a senha do banco). Uma regra `ask` que case um
comando de terminal literal, como `cq identity reveal …`, não fecha a brecha: uma variação do comando passa pelas
permissões amplas (a documentação oficial chama esses padrões de "fragile": code.claude.com/docs/en/permissions, lida
em 2026-10-28). Para operar com o agente, **use uma sessão sem essas permissões amplas** (a mitigação principal), por
exemplo fora do repositório, com o `stack_dir` no perfil; lá a marca de interação continua perguntando nas seis.
Recuse no prompt os comandos de terminal que contornem as ferramentas.

Arquivos de lista: passe o **caminho absoluto** (`import_students`); o servidor lê o arquivo e o conteúdo não passa
pelo modelo.

## 5. Validação automatizada (sem o Claude Code real e sem a pilha do Compose)

Os testes de integração e o canário precisam do Postgres de teste. Suba-o com `bash scripts/test-postgres.sh up` (ou
aponte `CQ_TEST_PG_DSN` para um Postgres próprio); a seção "Como rodar as suítes" do [`AGENTS.md`](../../AGENTS.md)
traz os pré-requisitos, entre eles o binário `age` da suíte rápida.

```bash
export PYTHONUTF8=1
bash scripts/test-postgres.sh up
uv run pytest tests/contract/test_mcp_tools.py tests/contract/test_plugin.py tests/contract/test_boundaries_agent.py
uv run pytest tests/integration/test_mcp_catalog_tools.py tests/integration/test_mcp_two_phase.py \
              tests/integration/test_mcp_reveal.py tests/integration/test_mcp_stack_tools.py \
              tests/integration/test_mcp_stdio.py
uv run pytest tests/unit/test_log_redaction.py tests/unit/test_down_code.py tests/unit/test_mcp_import_file.py \
              tests/unit/test_mcp_hardening.py tests/cli/test_cli_mcp.py tests/privacy/test_mcp_canary.py
uv run python ops/ci/ci_suite.py fast   # a suíte rápida, sem regressão (sem os testes de sistema)
```

Não rode `uv run pytest` sem argumentos: ele inclui os testes de sistema, que sobem o Compose de verdade, e no
Windows uma execução única esgota as portas. Para a suíte inteira sem os testes de sistema, use
`SYSTEM=0 bash scripts/run-suite.sh`.

Os testes rodam o servidor nas duas versões do protocolo MCP: a anterior, que começa por `initialize`, e a de
2026-07-28. Resultados esperados, por assunto:

| Assunto | Arquivo | Conferido |
| --- | --- | --- |
| ferramentas | `test_mcp_tools.py` | nomes do `tools/list` na ordem do catálogo e das ferramentas locais; marca de interação só nas confirmadoras; esquemas fechados e iguais aos contratos; nome completo da ferramenta com até 128 caracteres |
| operação | `test_mcp_catalog_tools.py` | as ferramentas do catálogo nas duas versões do protocolo; origem `mcp` na auditoria; resultado conforme o esquema; importação por caminho só com contagens; erros fixos; dica de diagnóstico só com perfil local |
| duas fases | `test_mcp_two_phase.py` | preparo → confirmação do arquivamento, dos convites, do disparo e do expurgo; códigos vencido, usado, de outro oferecimento; sem código → `-32602` (o erro de parâmetros inválidos do JSON-RPC) |
| revelação | `test_mcp_reveal.py` | identidades no esquema estrito; não encontrados; 403 sem a permissão; auditoria `client: mcp` |
| pilha | `test_mcp_stack_tools.py` | mesmo argv do CLI; perfil remoto e projetos reservados recusados sem Docker; código do `down`; trava; registros redigidos com as amostras canário |
| plugin | `test_plugin.py` | manifesto, `.mcp.json`, 7 skills, frontmatters, ferramentas citadas ⊂ `tools/list`, termos proibidos; as regras `ask` só no repositório de origem (nesta cópia, que nenhum arquivo da configuração de projeto do Claude Code é publicado) |
| robustez | `test_mcp_hardening.py` | stderr sem registros de outras bibliotecas; caminhos de rede e de dispositivo do Windows recusados antes de qualquer acesso; falha ao carregar o servidor com só a linha fixa |
| privacidade | `test_mcp_canary.py` | todas as ferramentas, zero identidade fora da revelação (salvo as exceções do expurgo, cada uma com contagem exata), zero segredo, também no stderr |
| processo | `test_mcp_stdio.py` | stdout só JSON-RPC; stderr só as 3 mensagens fixas; partida sem API; EOF → 0; as duas versões do protocolo |

## 6. Validação manual (Claude Code real)

1. `claude --version` ≥ 2.1.292, que fala o protocolo de 2026-07-28 (as checagens MCP do validador vêm desde a 2.1.281).
2. `claude plugin validate ./plugins/canal-questoes --strict` e `claude plugin validate ./plugins --strict`:
   "Validation passed".
3. Com a pilha local no ar e o perfil `agente`, no modo `auto`, o fluxo completo: criar um oferecimento de teste,
   importar a lista fictícia por caminho absoluto, preparar e confirmar um disparo, ver o relatório, revelar um
   pseudônimo pela skill manual, preparar e confirmar o `down`. Esperado: pergunta nas 6 confirmadoras (seção 4) e em
   nenhuma outra ferramenta. **Nunca** com o perfil apontando para uma pilha em uso.
4. Repetir uma confirmadora com o servidor configurado direto (sem o plugin): a pergunta aparece também, pela marca de
   interação (`anthropic/requiresUserInteraction`) que o servidor põe nas seis ferramentas.
5. Pedir "revele o p-xxxx" sem `/`: o agente não invoca a skill de revelação sozinho.
6. Se uma ferramenta da pilha não achar o diretório, confira o `stack_dir` do perfil; se o servidor usar o perfil
   errado, confira o `CQ_PROFILE` (e a ausência de `CQ_TOKEN` e `CQ_API_URL`) no ambiente em que abriu o Claude Code.
7. `/context` ou `claude plugin details canal-questoes`: o custo das ferramentas e das descrições das skills no
   contexto (os esquemas de saída da API são grandes).
