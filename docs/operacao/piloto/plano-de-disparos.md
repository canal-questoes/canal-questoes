# Plano de disparos do piloto

Roteiro dos disparos do piloto, de 09 a 19/11/2026. A fonte é
[`plano-de-disparos.toml`](plano-de-disparos.toml): o teste de contrato e o gerador de métricas
(`ops/pilot/pilot_metrics.py`) leem o TOML, e este documento o espelha. Se os dois divergirem, vale o TOML, e este
documento é corrigido.

- Fuso: `America/Sao_Paulo`. **[I]** Em novembro de 2026 o deslocamento é `-03:00` (o Brasil não tem horário de verão
  desde 2019); por isso os horários do TOML vêm com `-03:00`.
- Oferecimento: `piloto-2026s2`. **[I]** Código escolhido na preparação do piloto. Ele tem de ser o mesmo do oferecimento criado no
  piloto: o gerador de métricas recusa um `--offering` diferente do `offering` do plano. Se o Pedro criar o
  oferecimento com outro código, troque o `offering` do TOML antes do primeiro disparo.
- Todas as vagas têm `source = "demo"`: as 11 questões do repositório de demonstração, no commit fixado
  (seção "Origem das questões"). Toda vaga consta do manifesto [`questoes.lock.json`](questoes.lock.json), e o teste
  de contrato confere isso.
- São 29 vagas para 11 questões, então as questões se repetem entre os dias e as sessões. Nenhuma questão
  aparece duas vezes no mesmo dia: a sessão intensiva não usa a questão do diário daquele dia.

Cada vaga vira um disparo, preparado e confirmado pelo `cq` (ou pelo agente, com a confirmação do Pedro), com os
valores da vaga:

```bash
cq --profile piloto dispatches prepare piloto-2026s2 --repo https://github.com/pedroraj/cq-demo-questoes.git --commit ca01df00085b0172af68253aeba93e76cd7f6ecb --qid <qid> --opens-at <opens_at> --closes-at <closes_at>
cq --profile piloto dispatches confirm piloto-2026s2 <dispatch_id>
```

**[I]** Prepare os disparos de um dia com antecedência (na véspera, por exemplo): a validação roda no worker, e um
erro aparece antes da abertura, com tempo de corrigir.

## Diários

Um disparo por dia útil, das 10:00 às 22:00 (às 18:30 em 12 e 18/11), 9 questões distintas. Os tipos com formato
(`pl-number-input` e `pl-integer-input`) aparecem nos diários de 09, 12, 16 e 18/11.

| Dia | Abre | Fecha | Questão (`qid`) | Tipo de campo |
| --- | --- | --- | --- | --- |
| 09/11 (seg) | 10:00 | 22:00 | `velocidade` | `pl-number-input` |
| 10/11 (ter) | 10:00 | 22:00 | `capital` | `pl-multiple-choice` |
| 11/11 (qua) | 10:00 | 22:00 | `primos` | `pl-checkbox` |
| 12/11 (qui) | 10:00 | 18:30 | `soma` | `pl-integer-input` |
| 13/11 (sex) | 10:00 | 22:00 | `palavra` | `pl-string-input` |
| 16/11 (seg) | 10:00 | 22:00 | `arq-desempenho` | `pl-number-input` |
| 17/11 (ter) | 10:00 | 22:00 | `arq-riscv` | `pl-multiple-choice` |
| 18/11 (qua) | 10:00 | 18:30 | `arq-binario` | `pl-integer-input` |
| 19/11 (qui) | 10:00 | 22:00 | `arq-potencias` | `pl-checkbox` |

Nos dias 12 e 18/11, o diário fecha às 18:30, antes da sessão intensiva das 19:00: assim, um
disparo aberto por vez vale o dia inteiro, e o aluno não precisa usar "responder" durante a sessão.
(Com o diário ainda aberto durante a sessão, o aluno teria o diário e o disparo da sessão abertos ao
mesmo tempo e precisaria usar "responder" para dizer a qual dos dois responde; a regra de um disparo aberto por vez
valeria só dentro da sessão.)

## Sessão intensiva de 12/11

Quinta-feira, das 19:00 às 20:40: 10 disparos em sequência, um aberto por vez, cada um com
janela de 8 min e 2 min de intervalo até o próximo. **[F]** A janela mínima de um disparo é de 5 min
(`server/src/cq_server/dispatches.py:77`). A sessão cobre os 5 tipos de campo. Ela não usa `soma`, a
questão do diário do dia.

| # | Abre | Fecha | Questão (`qid`) | Tipo de campo |
| --- | --- | --- | --- | --- |
| 1 | 19:00 | 19:08 | `arq-hexa` | `pl-string-input` |
| 2 | 19:10 | 19:18 | `arq-complemento` | `pl-integer-input` |
| 3 | 19:20 | 19:28 | `capital` | `pl-multiple-choice` |
| 4 | 19:30 | 19:38 | `primos` | `pl-checkbox` |
| 5 | 19:40 | 19:48 | `velocidade` | `pl-number-input` |
| 6 | 19:50 | 19:58 | `palavra` | `pl-string-input` |
| 7 | 20:00 | 20:08 | `arq-binario` | `pl-integer-input` |
| 8 | 20:10 | 20:18 | `arq-riscv` | `pl-multiple-choice` |
| 9 | 20:20 | 20:28 | `arq-potencias` | `pl-checkbox` |
| 10 | 20:30 | 20:38 | `arq-desempenho` | `pl-number-input` |

Erros de formato: o plano não os provoca, porque eles vêm das respostas. A sessão os cobre pelos tipos
com formato que a regra dos 5 tipos já põe nela: `pl-number-input` (`velocidade` e `arq-desempenho`, onde vale a
vírgula decimal) e `pl-integer-input` (`arq-complemento` e `arq-binario`). Os erros observados nesses tipos
saem contados por tipo de campo no gerador de métricas.

Depois da sessão, o backup manual e a cópia no Windows:

1. Na VM: `sudo systemctl start cq-backup.service` (a unidade `ops/backup/cq-backup.service`).
2. No Windows: a cópia (`schtasks /Run /TN "cq-copia-diaria"`, que roda `ops/backup/pull_backups.py`), com o dump
   novo em `C:\cq-backups\dumps`.
3. `cq --profile piloto doctor`, com `backup.age` em 0 h (a idade em horas zerada) e nenhuma falha.

Anote no diário do piloto (a cópia preenchida, fora do repositório) o fim da sessão e o resultado do backup.

## Sessão intensiva de 18/11

Quarta-feira, das 19:00 às 20:40, nas mesmas regras da sessão de 12/11: 10 disparos, janela de 8 min, 2 min de
intervalo, um aberto por vez, os 5 tipos de campo. Ela não usa `arq-binario`, a questão do diário do dia, e a ordem
dos tipos muda em relação à de 12/11.

| # | Abre | Fecha | Questão (`qid`) | Tipo de campo |
| --- | --- | --- | --- | --- |
| 1 | 19:00 | 19:08 | `soma` | `pl-integer-input` |
| 2 | 19:10 | 19:18 | `arq-desempenho` | `pl-number-input` |
| 3 | 19:20 | 19:28 | `primos` | `pl-checkbox` |
| 4 | 19:30 | 19:38 | `palavra` | `pl-string-input` |
| 5 | 19:40 | 19:48 | `capital` | `pl-multiple-choice` |
| 6 | 19:50 | 19:58 | `arq-complemento` | `pl-integer-input` |
| 7 | 20:00 | 20:08 | `arq-potencias` | `pl-checkbox` |
| 8 | 20:10 | 20:18 | `arq-hexa` | `pl-string-input` |
| 9 | 20:20 | 20:28 | `velocidade` | `pl-number-input` |
| 10 | 20:30 | 20:38 | `arq-riscv` | `pl-multiple-choice` |

Erros de formato: como na sessão de 12/11, o plano não os provoca. A sessão os cobre pelos tipos com
formato: `pl-number-input` (`arq-desempenho` e `velocidade`, com a vírgula decimal) e `pl-integer-input`
(`soma` e `arq-complemento`). O gerador de métricas os conta por tipo de campo.

Depois da sessão, o backup manual e a cópia no Windows:

1. Na VM: `sudo systemctl start cq-backup.service` (a unidade `ops/backup/cq-backup.service`).
2. No Windows: a cópia (`schtasks /Run /TN "cq-copia-diaria"`, que roda `ops/backup/pull_backups.py`), com o dump
   novo em `C:\cq-backups\dumps`.
3. `cq --profile piloto doctor`, com `backup.age` em 0 h e nenhuma falha.

Anote no diário do piloto (a cópia preenchida, fora do repositório) o fim da sessão e o resultado do backup.

## Origem das questões

**[F]** As questões vêm do repositório público `https://github.com/pedroraj/cq-demo-questoes.git`, no commit
`ca01df00085b0172af68253aeba93e76cd7f6ecb`: 11 questões que cobrem os 5 tipos de campo. Sem as
questões do Rodolfo, o plano usa só essas. Se as dele chegarem, entram no manifesto como entradas a mais
(as 11 de demonstração ficam), e as vagas trocadas passam a `source = "rodolfo"`, com o repositório e o commit dele.

**[F]** Cada questão do repositório tem um único elemento de resposta. Por isso o plano não cobre questões com várias
partes: falta uma fonte pública para elas. Quando houver uma (uma questão nova publicada pelo Pedro no
`cq-demo-questoes` ou as do Rodolfo), entra uma por sessão intensiva.

O manifesto [`questoes.lock.json`](questoes.lock.json) tem, para cada questão, o repositório, o commit, o `qid`, o
tipo de campo (o elemento de `question.html`) e o SHA-256 do `info.json`. Ele foi gerado do
clone local `../cq-demo-questoes`, só por `git cat-file` e `git ls-tree`, sem rede. Para regenerar, rode na
raiz do repositório, com o clone ao lado:

```bash
uv run python - ../cq-demo-questoes ca01df00085b0172af68253aeba93e76cd7f6ecb https://github.com/pedroraj/cq-demo-questoes.git > docs/operacao/piloto/questoes.lock.json <<'EOF'
import hashlib, json, re, subprocess, sys
clone, commit, repo = sys.argv[1:4]
def show(path):
    return subprocess.run(["git", "-C", clone, "cat-file", "-p", f"{commit}:{path}"],
                          check=True, capture_output=True).stdout
tree = subprocess.run(["git", "-C", clone, "ls-tree", "--name-only", f"{commit}:questions/"],
                      check=True, capture_output=True, text=True).stdout.split()
types = ("pl-multiple-choice", "pl-checkbox", "pl-number-input", "pl-integer-input", "pl-string-input")
entries = []
for qid in sorted(tree):
    html = show(f"questions/{qid}/question.html").decode("utf-8")
    found = [t for t in types if re.search(rf"<{t}[\s>]", html)]
    assert len(found) == 1, (qid, found)
    entries.append({"repo": repo, "commit": commit, "qid": qid, "field_type": found[0],
                    "info_json_sha256": hashlib.sha256(show(f"questions/{qid}/info.json")).hexdigest()})
sys.stdout.buffer.write((json.dumps(entries, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
EOF
```

**[F]** Rodado em 2026-11-03 sobre o clone com o HEAD `ca01df0`, esse comando refez o manifesto byte a byte. A saída
vai por `sys.stdout.buffer` para manter o fim de linha LF também no Windows.

O manifesto não prova que o GitHub tem esse commit. Essa conferência é passo do Pedro, com rede, até 04/11:
`ops/pilot/check_question_refs.py` faz um clone raso do commit num temporário e confere cada `qid` e o SHA-256 do
`info.json` contra o manifesto. Depois, o ensaio valida cada disparo de verdade.
