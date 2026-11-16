@AGENTS.md

# canal-questoes — notas para o Claude Code

A linha acima importa o [`AGENTS.md`](AGENTS.md), que vale para qualquer agente: o que é o projeto, o mapa das pastas,
como rodar as suítes, os princípios e as regras para quem contribui. Abaixo, só o que é próprio do Claude Code.

## O plugin do projeto

O repositório traz um plugin do Claude Code em `plugins/canal-questoes/`: o servidor MCP do `cq` (as ferramentas do
catálogo e as da pilha local) e as skills dos fluxos do professor (oferecimento, lista e convites, disparo,
relatórios, encerramento, pilha e revelação). As operações destrutivas passam por duas fases e pedem a confirmação de
quem opera. O guia completo, com a validação, está em
[`docs/operacao/plugin-e-agente.md`](docs/operacao/plugin-e-agente.md).

O repositório **não** habilita o plugin sozinho, e não há `.mcp.json` na raiz: as ferramentas do servidor precisam de
uma pilha no ar e de um perfil, e o servidor chega pelo plugin.

1. **Instale o `cq`** a partir do repositório: `uv tool install ./client` (o guia de
   [início rápido](docs/operacao/inicio-rapido.md) explica a pilha local e o arquivo de perfil).
2. **Crie um perfil próprio do agente** no arquivo de perfil, fora do repositório, com um token só dele: sem a
   permissão de gerir tokens e com só o que o agente precisa. A
   [seção 1 do guia do plugin](docs/operacao/plugin-e-agente.md#1-um-token-só-do-agente) traz o comando
   `cq tokens create` com as permissões. Abra o Claude Code com `CQ_PROFILE` apontando esse perfil.
3. **Adicione o marketplace local e instale o plugin.** O marketplace é `plugins/.claude-plugin/marketplace.json`
   (`canal-questoes-local`). No terminal, na raiz do repositório:

   ```bash
   claude plugin marketplace add ./plugins
   claude plugin install canal-questoes@canal-questoes-local
   ```

   Ou, dentro de uma sessão do Claude Code (o `/plugin install` abre a ficha do plugin; confirme a instalação ali):

   ```text
   /plugin marketplace add ./plugins
   /plugin install canal-questoes@canal-questoes-local
   ```

4. **Confira a conexão:** `/mcp` mostra o servidor do plugin conectado, e as skills aparecem como
   `/canal-questoes:<nome>`.

**Nunca ponha o token no plugin**, num argumento de ferramenta, num arquivo do repositório ou na conversa: o servidor
lê o token do perfil, e o segredo não passa pelo modelo. Não deixe `CQ_TOKEN` nem `CQ_API_URL` no ambiente em que
abre o Claude Code: elas vencem o campo do perfil. A identidade dos alunos só sai pela ferramenta de revelação,
que pede a sua aprovação a cada chamada.

## Ao trabalhar no código

- Siga as regras do [`AGENTS.md`](AGENTS.md); em especial, não altere um teste existente para o código passar.
- Não leia o `.env` nem o arquivo de perfil, e não rode comandos que contornem as ferramentas do MCP (por exemplo, ler o
  banco direto pelo contêiner): eles expõem segredos e identidades.
- Responda em português do Brasil e mantenha os identificadores de código em inglês.
