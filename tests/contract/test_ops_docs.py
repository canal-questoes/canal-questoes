"""Testes dos documentos de operação da implantação e do piloto: os guias, a política de retenção, as skills, o
roteiro do ensaio, o plano de disparos, o manifesto das questões, o diário e o questionário.


Os documentos são **lidos**, nunca escritos; nenhum comando deles é executado. Um teste por
documento: primeiro as regras dele (títulos `##` exatos e na ordem, frases, blocos), depois as regras comuns a
todos (`_check_common`):
- nenhum `docker compose config`, `cat .env`, `printenv`, `Get-Content .env`;
- nenhum achado dos detectores (pelo `scan` de `ops/export/repo_scan.py`, com as exceções de
  `ops/export/fictitious.txt`);
- todo `ops/…` e `scripts/…` citado existe na árvore;
- todo `cq <comando>` citado (em código: crase ou bloco cercado) começa por uma folha da árvore do
  `cq_client.cli` (percorrida como o `_cli_leaves` de `tests/contract/test_catalog.py`);
- toda `CQ_*` citada existe no `compose.yaml`, no código (`server/src`, `client/src`, `ops`, `scripts`) ou na
  tabela das variáveis novas (`DATA_MODEL_VARIABLES`); `CQ_SMTP_*` vale como prefixo.

Os documentos, em grupos (sem o documento, o teste falha com a
mensagem "documento ausente"): o guia de backup, o guia da VM e o início rápido com a skill `pilha`,
a política e a skill `encerramento`, o manifesto e o plano, o roteiro,
o diário e o questionário, o de CI; a varredura pelos detectores depende da implementação.

**[I]** Escolhas deste teste sobre a forma exata:
- um "passo" do guia da VM é o trecho de uma marca `**[passo do Pedro]**` ou `**[comando pronto]**` até a
  próxima marca ou o fim da seção, e cada passo tem uma linha que começa por `Conferência:` (com marcador de
  lista ou negrito na frente, se houver);
- as frases obrigatórias são conferidas por fragmentos num mesmo parágrafo (sem diferenciar maiúsculas, sem as
  marcas `**` e as crases), não pelo texto inteiro;
- os itens do roteiro são conferidos por fragmentos numa linha `- [ ]`;
- o diário e o questionário: um "campo" é uma linha de lista, de tabela ou numerada; o campo que nega (com
  "nenhum", "sem", "não", "nunca") não conta como pedido de identidade;
- `ops/ci/known-flaky.txt` é gerado só na exportação e pode ser citado sem existir na árvore;
- o manifesto e o plano: as entradas do repositório de demonstração são exatamente as 11 questões da
  demonstração, todas no commit fixado; uma entrada de outro repositório (as do Rodolfo, se chegarem antes do ensaio)
  só vale por HTTPS e, no plano, só com `source = "rodolfo"`. Por isso o manifesto nunca perde as 11 de demonstração:
  se as do Rodolfo chegarem, ele vira um superconjunto, e a troca de origem fica só no plano. O repositório é
  comparado sem diferenciar maiúsculas, sem a `/` final e sem o `.git`, para que uma grafia diferente da URL de
  demonstração não passe por "outro".
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import click
import pytest

from tests.helpers.engine import ELEMENT_OF
from tests.helpers.ops import REPO, load_ops

# --------------------------------------------------------------------------------------------------
# Documentos
# --------------------------------------------------------------------------------------------------

VM_GUIDE = "docs/operacao/implantacao-vm.md"
BACKUP_GUIDE = "docs/operacao/backup-e-restauracao.md"
RETENTION_POLICY = "docs/operacao/politica-de-retencao.md"
QUICKSTART = "docs/operacao/inicio-rapido.md"
CI_GUIDE = "docs/operacao/ci-e-exportacao.md"
REHEARSAL = "docs/operacao/piloto/roteiro-ensaio.md"
PLAN_MD = "docs/operacao/piloto/plano-de-disparos.md"
PLAN_TOML = "docs/operacao/piloto/plano-de-disparos.toml"
MANIFEST = "docs/operacao/piloto/questoes.lock.json"
DIARY = "docs/operacao/piloto/diario.md"
SURVEY = "docs/operacao/piloto/questionario.md"
SKILL_STACK = "plugins/canal-questoes/skills/pilha/SKILL.md"
SKILL_CLOSING = "plugins/canal-questoes/skills/encerramento/SKILL.md"

# O grupo de cada documento, pelo nome do principal (vai na mensagem de documento ausente).
WRITTEN_BY: Mapping[str, str] = {
    VM_GUIDE: "guia da VM", BACKUP_GUIDE: "guia de backup", RETENTION_POLICY: "política", QUICKSTART: "início rápido",
    CI_GUIDE: "guia de CI", REHEARSAL: "roteiro", PLAN_MD: "plano", PLAN_TOML: "plano", MANIFEST: "manifesto",
    DIARY: "diário", SURVEY: "diário", SKILL_STACK: "início rápido", SKILL_CLOSING: "skill de encerramento",
}

VM_GUIDE_TITLES = [
    "## 1. Requisitos da VM", "## 2. Endurecimento do SSH e do firewall", "## 3. Usuário de operação",
    "## 4. Docker Engine e Compose", "## 5. Pacote do sistema", "## 6. Conta de e-mail do projeto",
    "## 7. Arquivo .env", "## 8. Limites do motor", "## 9. Subida e conferência", "## 10. Backup e agendamento",
    "## 11. Cópia no Windows", "## 12. Túnel e perfil remoto", "## 13. Teste de envio de e-mail",
    "## 14. Teste de restauração", "## 15. Falhas comuns", "## 16. Atualizar o sistema durante o piloto",
]
BACKUP_GUIDE_TITLES = [
    "## Como funciona", "## Chaves do age", "## Restaurar numa pilha descartável",
    "## Recuperar a pilha numa VM nova", "## Depois de restaurar", "## Se uma chave se perder",
]
RETENTION_TITLES = [
    "## Escopo e base", "## Banco vivo", "## Dumps na VM", "## Cópia no computador do Pedro",
    "## Registros dos contêineres", "## Textos livres de outros oferecimentos", "## Repositório público",
    "## Depois de uma restauração", "## Quando o prazo definitivo for fixado",
]
REHEARSAL_TITLES = ["## 05/11", "## 06/11", "## Pronto para convidar", "## Troca para o projeto do piloto"]
PLAN_TITLES = ["## Diários", "## Sessão intensiva de 12/11", "## Sessão intensiva de 18/11", "## Origem das questões"]
DIARY_TITLES = ["## Incidente", "## Tarefa do agente", "## Expurgos realizados"]

# Os documentos sujeitos às regras comuns (a todos).
COMMON_DOCS = (VM_GUIDE, BACKUP_GUIDE, RETENTION_POLICY, QUICKSTART, CI_GUIDE, REHEARSAL, PLAN_MD, DIARY, SURVEY)

FORBIDDEN_COMMANDS = ("docker compose config", "cat .env", "printenv", "get-content .env")

# as variáveis novas da implantação (nome, padrão citado no guia ou `None`, quem lê). As 3 do CLI
# (`CQ_DOCTOR_BOT_POLL_SECONDS`, …) mantêm o padrão que já tinham; as vazias por padrão, `None`.
DATA_MODEL_VARIABLES: tuple[tuple[str, str | None, str], ...] = (
    ("CQ_DOCTOR_BACKUP_WARNING_HOURS", "26", "api"),
    ("CQ_DOCTOR_BACKUP_FAILURE_HOURS", "50", "api"),
    ("CQ_DOCTOR_MAIL_STALE_HOURS", "13", "api"),
    ("CQ_RETENTION_DAYS", "30", "api"),
    ("CQ_BACKUP_STATUS_FILE", None, "api"),
    ("CQ_BACKUP_STATUS_DIR", None, "compose"),
    ("CQ_DOCTOR_BOT_POLL_SECONDS", None, "api"),
    ("CQ_DOCTOR_TASK_OVERDUE_SECONDS", None, "api"),
    ("CQ_DOCTOR_CLOCK_SKEW_SECONDS", None, "api"),
    ("CQ_MAIL_PROBE_INTERVAL_SECONDS", "21600", "worker"),
    ("CQ_DOCTOR_DISK_WARNING_PERCENT", "20", "cq doctor"),
    ("CQ_DOCTOR_DISK_FAILURE_PERCENT", "10", "cq doctor"),
    ("CQ_BACKUP_DIR", None, "cq doctor"),
)

# Gerado só na exportação: pode ser citado sem existir.
GENERATED_PATHS = frozenset({"ops/ci/known-flaky.txt"})

# **[F]** Os 5 tipos de campo: os elementos do PL do dublê do motor (`tests/helpers/engine.py`, `ELEMENT_OF`), os
# mesmos das 11 questões de `cq-demo-questoes@ca01df0` (`question.html`, conferido em 2026-10-29).
FIELD_TYPES = frozenset(ELEMENT_OF.values())

# **[F]** O repositório e o commit fixados para a demonstração (`pedroraj/cq-demo-questoes@ca01df0`), na forma
# completa.
DEMO_REPO = "https://github.com/pedroraj/cq-demo-questoes.git"
DEMO_COMMIT = "ca01df00085b0172af68253aeba93e76cd7f6ecb"
# **[F]** As 11 questões da demonstração nesse commit, com o elemento de entrada de cada
# `question.html` (o `field_type`): conferidas por `git -C ../cq-demo-questoes ls-tree` e `cat-file` em
# 2026-10-30. O teste não lê o clone.
DEMO_QUESTIONS: Mapping[str, str] = {
    "arq-binario": "pl-integer-input", "arq-complemento": "pl-integer-input", "arq-desempenho": "pl-number-input",
    "arq-hexa": "pl-string-input", "arq-potencias": "pl-checkbox", "arq-riscv": "pl-multiple-choice",
    "capital": "pl-multiple-choice", "palavra": "pl-string-input", "primos": "pl-checkbox", "soma": "pl-integer-input",
    "velocidade": "pl-number-input",
}

PILOT_TIMEZONE = "America/Sao_Paulo"
DAILY_DAYS = frozenset(date(2026, 11, day) for day in (9, 10, 11, 12, 13, 16, 17, 18, 19))  # os dias úteis do piloto
INTENSIVE_SESSIONS = ("2026-11-12", "2026-11-18")
INTENSIVE_PER_SESSION = 10  # 2 sessões com 10 `intensive` cada
MIN_WINDOW = timedelta(minutes=5)  # `dispatches.py:77`
SLOT_KEYS = frozenset({"kind", "session", "opens_at", "closes_at", "repo", "commit", "qid", "field_type", "source"})
MANIFEST_KEYS = frozenset({"repo", "commit", "qid", "field_type", "info_json_sha256"})

MARKER = re.compile(r"\*\*\[(?:passo do Pedro|comando pronto)\]\*\*")
CHECK_LINE = re.compile(r"^\s*(?:[-*>]\s*)*(?:\*\*)?Conferência:")
TUNNEL = re.compile(r"-L\s+127\.0\.0\.1:(\d+):127\.0\.0\.1:8000\b")
# **[F]** A porta local do túnel é a do piloto (`-L 127.0.0.1:18000:127.0.0.1:8000`): própria do piloto,
# distinta da porta padrão da pilha local (`CQ_API_PORT`, padrão 8000, `compose.yaml` l. 85), que é também a da
# `cq-demo` (`docs/operacao/inicio-rapido.md`, seção 7) (o caso da porta local do túnel já ocupada).
PILOT_TUNNEL_PORT = "18000"
LOCAL_STACK_DEFAULT_PORT = "8000"
FENCE = re.compile(r"^\s*(```|~~~)")


# --------------------------------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------------------------------


def _doc(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"documento ausente: {rel} (grupo: {WRITTEN_BY.get(rel, 'tarefa do documento')})"
    return path.read_text(encoding="utf-8")


def _fenced(text: str) -> list[tuple[str, str]]:
    """Os blocos cercados: (linguagem, conteúdo)."""
    blocks: list[tuple[str, str]] = []
    lang: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if FENCE.match(line):
            if lang is None:
                lang = line.strip().lstrip("`~").strip().lower()
                body = []
            else:
                blocks.append((lang, "\n".join(body)))
                lang = None
            continue
        if lang is not None:
            body.append(line)
    return blocks


def _prose_lines(text: str) -> list[str]:
    """As linhas fora dos blocos cercados."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return out


def _titles(text: str) -> list[str]:
    return [line.rstrip() for line in _prose_lines(text) if line.startswith("## ")]


def _sections(text: str) -> dict[str, str]:
    """Título `##` → corpo (até o próximo `##`), com os blocos cercados dentro do corpo."""
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
        if not inside and line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(body)
            current, body = line.rstrip(), []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body)
    return sections


def _section(text: str, title: str) -> str:
    sections = _sections(text)
    assert title in sections, f"seção ausente: {title}"
    return sections[title]


def _norm(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split()).lower()


def _paragraphs(text: str) -> list[str]:
    return [_norm(block) for block in re.split(r"\n\s*\n", text) if block.strip()]


def _has_paragraph(text: str, *fragments: str) -> bool:
    wanted = [_norm(f) for f in fragments]
    return any(all(w in paragraph for w in wanted) for paragraph in _paragraphs(text))


def _assert_paragraph(text: str, *fragments: str, where: str) -> None:
    assert _has_paragraph(text, *fragments), f"{where}: nenhum parágrafo com {list(fragments)}"


def _assert_contains(text: str, *needles: str, where: str) -> None:
    missing = [n for n in needles if n not in text]
    assert missing == [], f"{where}: ausente(s) {missing}"


def _toml_blocks(text: str) -> list[dict]:
    blocks = []
    for lang, body in _fenced(text):
        if lang == "toml":
            blocks.append(tomllib.loads(body))
    return blocks


def _remote_profiles(blocks: Iterable[dict]) -> list[dict]:
    return [profile for block in blocks for profile in (block.get("profiles") or {}).values()
            if isinstance(profile, dict) and profile.get("kind") == "remote"]


# --------------------------------------------------------------------------------------------------
# Regras comuns (a todos os documentos)
# --------------------------------------------------------------------------------------------------


def _cli_leaves() -> frozenset[str]:
    """As folhas da árvore do `cq` (o molde do `_cli_leaves` de `tests/contract/test_catalog.py`)."""
    from cq_client.cli import cli

    leaves: set[str] = set()

    def walk(cmd: click.Command, names: list[str]) -> None:
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd)
            for name in cmd.list_commands(ctx):
                child = cmd.get_command(ctx, name)
                assert child is not None, name
                walk(child, names + [name])
        else:
            leaves.add(" ".join(names))

    walk(cli, [])
    leaves.discard("")
    return frozenset(leaves)


def _code_texts(text: str) -> list[str]:
    """O texto em código: o conteúdo das crases e as linhas dos blocos cercados."""
    spans = re.findall(r"`([^`\n]+)`", "\n".join(_prose_lines(text)))
    lines = [line for _, body in _fenced(text) for line in body.splitlines()]
    return spans + lines


_CQ_TOKEN = re.compile(r"(?:(?<=\s)|^)cq(?=\s)")
_WORD = re.compile(r"[a-z][a-z-]*")


def _cited_cq_commands(text: str) -> list[list[str]]:
    """As palavras depois de cada `cq` em código (sem as opções globais `--json` e `--profile <nome>`); o `cq`
    depois de `-p`/`--project-name` é o projeto do Compose, não o comando."""
    cited: list[list[str]] = []
    for code in _code_texts(text):
        code = code.split(" #", 1)[0]
        for match in _CQ_TOKEN.finditer(code):
            before = code[:match.start()].split()
            if before and before[-1] in ("-p", "--project-name"):
                continue
            tokens = code[match.end():].split()
            words: list[str] = []
            i = 0
            while i < len(tokens):
                token = tokens[i]
                if token == "--json":
                    i += 1
                    continue
                if token == "--profile":
                    i += 2
                    continue
                if token.startswith("--profile="):
                    i += 1
                    continue
                if _WORD.fullmatch(token):
                    words.append(token)
                    i += 1
                    continue
                break
            if words:
                cited.append(words)
    return cited


def _known_variables() -> frozenset[str]:
    names: set[str] = set(re.findall(r"CQ_[A-Z0-9_]*[A-Z0-9]", (REPO / "compose.yaml").read_text(encoding="utf-8")))
    for folder in ("server/src", "client/src", "ops", "scripts"):
        for path in (REPO / folder).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".sh", ".service", ".timer", ".txt", ".yaml", ".yml"):
                names.update(re.findall(r"CQ_[A-Z0-9_]*[A-Z0-9]", path.read_text(encoding="utf-8",
                                                                                    errors="replace")))
    names.update(name for name, _, _ in DATA_MODEL_VARIABLES)
    return frozenset(names)


def _fictitious() -> dict[str, frozenset[str]]:
    """`ops/export/fictitious.txt` (`<tipo>\\t<valor>`), como o `scan` o recebe."""
    path = REPO / "ops" / "export" / "fictitious.txt"
    assert path.is_file(), "lista ausente: ops/export/fictitious.txt"
    table: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        kind, _, value = line.partition("\t")
        table.setdefault(kind, set()).add(value)
    return {kind: frozenset(values) for kind, values in table.items()}


def _self_check() -> None:
    """Os ajudantes acham o que conferem (controle contra o vazio): o comando `cq` em código, a folha da árvore, o
    passo sem `Conferência:`, a frase num parágrafo, o perfil remoto num bloco TOML."""
    sample = ("Rode `cq --profile piloto offerings purge prepare x` e `docker compose -p cq ps`.\n\n"
              "```toml\n[profiles.p]\nkind = \"remote\"\napi_url = \"http://127.0.0.1:18000\"\n```\n")
    assert _cited_cq_commands(sample) == [["offerings", "purge", "prepare", "x"]]
    assert "offerings purge prepare" in _cli_leaves()
    assert _remote_profiles(_toml_blocks(sample)) == [{"kind": "remote", "api_url": "http://127.0.0.1:18000"}]
    assert _has_paragraph("O **agente** opera só a `API`\npelo túnel.", "agente", "só a API", "túnel")
    with pytest.raises(AssertionError, match="Conferência"):
        _check_steps("## A\n**[passo do Pedro]** faça.\n", where="amostra")
    _check_steps("## A\n**[comando pronto]** rode.\n\nConferência: veja.\n", where="amostra")


def _check_common(rel: str) -> None:
    text = _doc(rel)
    _self_check()
    lowered = text.lower()
    assert [c for c in FORBIDDEN_COMMANDS if c in lowered] == [], f"{rel}: comando proibido"

    cited_paths = {m.rstrip(".,;:)") for m in re.findall(r"(?<![\w./-])(?:ops|scripts)/[\w./-]*[\w]", text)}
    missing = sorted(p for p in cited_paths if p not in GENERATED_PATHS and not (REPO / p).exists())
    assert missing == [], f"{rel}: caminho citado inexistente {missing}"

    leaves = _cli_leaves()
    bad = sorted({" ".join(words) for words in _cited_cq_commands(text)
                  if not any(" ".join(words[:n]) in leaves for n in range(1, len(words) + 1))})
    assert bad == [], f"{rel}: `cq` com comando fora da árvore {bad}"

    known = _known_variables()
    unknown = []
    for name in set(re.findall(r"CQ_[A-Z0-9_]+", text)):
        if name.endswith("_"):
            if not any(k.startswith(name) for k in known):
                unknown.append(name)
        elif name not in known:
            unknown.append(name)
    assert sorted(unknown) == [], f"{rel}: variável CQ_* desconhecida {sorted(unknown)}"

    repo_scan = load_ops("ops/export/repo_scan.py")
    findings = repo_scan.scan(REPO, [rel], fictitious=_fictitious())
    assert [(f.path, f.line, f.kind) for f in findings] == [], f"{rel}: achados da varredura"


def _check_steps(text: str, *, where: str) -> None:
    """Cada passo (marca até a próxima marca ou o fim da seção) com uma linha `Conferência:`."""
    for title, body in _sections(text).items():
        lines = body.splitlines()
        starts = [i for i, line in enumerate(lines) if MARKER.search(line)]
        assert starts, f"{where}, {title}: nenhum passo marcado"
        for k, start in enumerate(starts):
            end = starts[k + 1] if k + 1 < len(starts) else len(lines)
            chunk = lines[start:end]
            assert any(CHECK_LINE.match(line) for line in chunk), (
                f"{where}, {title}: passo sem linha `Conferência:`: {lines[start].strip()[:120]}")


# --------------------------------------------------------------------------------------------------
# Guia da VM
# --------------------------------------------------------------------------------------------------


def test_vm_guide_titles_steps_and_rules() -> None:
    """Os 16 títulos exatos e na ordem; cada passo marcado e com `Conferência:`; as regras por seção;
    o bloco TOML do perfil (lido por `tomllib`); as frases das duas chaves, do acesso SSH, do agente só na API, da
    pilha da VM pelo Pedro por SSH com o `cq` e o perfil local de lá, dos segredos só na VM, do usuário
    separado; as versões mínimas; os requisitos (seção 1); a verificação em duas etapas e a senha de app (seção 6); os
    limites do motor (seção 8); o teste de envio de e-mail (seção 13)."""
    text = _doc(VM_GUIDE)
    assert _titles(text) == VM_GUIDE_TITLES
    _check_steps(text, where=VM_GUIDE)
    s = {title: _section(text, title) for title in VM_GUIDE_TITLES}

    # Seção 1 (os requisitos).
    _assert_contains(s[VM_GUIDE_TITLES[0]], "Ubuntu 24.04", "2 vCPU", "4 GB", where="seção 1 (requisitos)")
    # Seção 2.
    s2 = s[VM_GUIDE_TITLES[1]]
    _assert_contains(s2, "sshd_config.d", "PasswordAuthentication no", "PermitRootLogin no",
                     "ufw default deny incoming", "ufw allow OpenSSH", "unattended-upgrades", "Test-NetConnection",
                     "-Port 8000", where="seção 2")
    _assert_paragraph(s2, "Test-NetConnection", "falh", where="seção 2 (a porta 8000 tem de falhar)")
    # Seção 4: as versões mínimas.
    _assert_contains(text, "26.0.0", "2.27.0", where="versões mínimas")
    # Seção 5.
    _assert_contains(s[VM_GUIDE_TITLES[4]], "CQ_REPO_ALLOWED_HOSTS", where="seção 5")
    # Seção 6: os valores não secretos do Gmail e nenhuma senha.
    s6 = s[VM_GUIDE_TITLES[5]]
    _assert_contains(s6, "CQ_SMTP_HOST=smtp.gmail.com", "CQ_SMTP_PORT=587", "CQ_SMTP_SECURITY=starttls",
                     where="seção 6")
    assert re.search(r"CQ_SMTP_PASSWORD\s*=\s*[^\s`]", s6) is None, "seção 6: valor de senha"
    # Seção 6: a conta com verificação em duas etapas e a senha de app.
    _assert_paragraph(s6, "duas etapas", where="seção 6 (verificação em duas etapas)")
    _assert_paragraph(s6, "senha de app", where="seção 6 (senha de app)")
    # Seção 8: os limites do motor para a VM de 2 vCPU e 4 GB, pelas variáveis do
    # `compose.yaml` (l. 167-168), e a conferência com `docker stats --no-stream`.
    s8 = s[VM_GUIDE_TITLES[7]]
    _assert_contains(s8, "CQ_ENGINE_MEM_LIMIT", "CQ_ENGINE_CPUS", "1536m", "docker stats --no-stream",
                     where="seção 8 (limites do motor)")
    assert re.search(r"\b1[.,]5\b", s8), "seção 8: o limite de 1,5 CPU"
    _assert_paragraph(s8, "swap", where="seção 8 (o swap)")
    # Seção 7: toda variável nova, com o padrão e quem a lê, na mesma linha.
    s7_lines = s[VM_GUIDE_TITLES[6]].splitlines()
    cited = set(re.findall(r"CQ_[A-Z0-9_]+", text))
    assert {name for name, _, _ in DATA_MODEL_VARIABLES} <= cited
    for name, default, reader in DATA_MODEL_VARIABLES:
        rows = [line for line in s7_lines if name in line]
        assert rows, f"seção 7: {name} ausente"
        assert any((default is None or default in line) and reader in line.lower() for line in rows), (
            f"seção 7: {name} sem o padrão {default!r} ou sem quem a lê ({reader!r})")
    # Seção 9: até o primeiro backup, a única falha esperada é `backup.age` sem registro legível.
    from cq_server.doctor import DOCTOR_REASON_MESSAGES

    _assert_paragraph(s[VM_GUIDE_TITLES[8]], "backup.age",
                      DOCTOR_REASON_MESSAGES[("backup.age", "failure", "unreadable")], where="seção 9")
    # Seção 10.
    s10 = s[VM_GUIDE_TITLES[9]]
    _assert_contains(s10, "/var/backups/cq", "/var/backups/cq/state", "0755", "2750", "cqbackup", "stat -c",
                     "sudo systemctl start cq-backup.service", "CQ_BACKUP_DIR=/var/backups/cq", "~24 h",
                     where="seção 10")
    _assert_paragraph(s10, "setgid", where="seção 10")
    _assert_paragraph(s10, "cq doctor", "backup.age", "ok", where="seção 10 (o primeiro backup à mão)")
    assert sum(1 for line in s10.splitlines() if CHECK_LINE.match(line)) >= 3, (
        "seção 10: cada diretório com a sua `Conferência:`")
    writes_env = re.compile(r">>?\s*\S*\.env\b|\b(?:nano|vi|vim|tee|sed -i)\b.*\.env\b")
    assert [line for line in s10.splitlines() if writes_env.search(line)] == [], "seção 10: o `.env` é tocado"
    assert [line for line in s10.splitlines() if "CQ_BACKUP_DIR=" in line and ".env" in line] == [], (
        "seção 10: `CQ_BACKUP_DIR` vai ao ambiente do usuário de operação, não ao `.env`")
    # Seção 11.
    _assert_contains(s[VM_GUIDE_TITLES[10]], "schtasks /Create", "/SC DAILY", "/SC ONLOGON", "pull_backups.py",
                     where="seção 11")
    _assert_paragraph(s[VM_GUIDE_TITLES[10]], "chave", "dedicada", where="seção 11")
    # Seção 12: o túnel só de encaminhamento, a troca da porta e o perfil.
    s12 = s[VM_GUIDE_TITLES[11]]
    _assert_contains(s12, "ssh -N", "-o ExitOnForwardFailure=yes", where="seção 12")
    ports = set(TUNNEL.findall(text))
    assert len(ports) == 1, f"o comando do túnel com uma porta só: {sorted(ports)}"
    port = ports.pop()
    assert port != LOCAL_STACK_DEFAULT_PORT, (
        f"a porta local do túnel colide com a padrão da pilha local e da `cq-demo` ({LOCAL_STACK_DEFAULT_PORT})")
    assert port == PILOT_TUNNEL_PORT, f"a porta local do túnel não é a do piloto: {port}"
    _assert_paragraph(s12, "porta", "comando", "perfil", where="seção 12 (como trocar a porta)")
    profiles = _remote_profiles(_toml_blocks(s12))
    assert profiles, "seção 12: nenhum bloco TOML com perfil `kind = \"remote\"`"
    for profile in profiles:
        assert profile.get("api_url") == f"http://127.0.0.1:{port}", profile.get("api_url")
        assert "stack_dir" not in profile and "compose_project" not in profile
    # Seção 13 (os mesmos fragmentos do roteiro): oferecimento de teste, convites em duas fases,
    # arquivamento e expurgo.
    s13 = _norm(s[VM_GUIDE_TITLES[12]])
    _assert_contains(s13, "oferecimento de teste", "duas fases", "arquiv", "expurg", where="seção 13 (teste de envio)")
    # Seção 14: aponta o guia de backup.
    _assert_contains(s[VM_GUIDE_TITLES[13]], "backup-e-restauracao.md", where="seção 14")
    # Seção 16: os 4 passos, nesta ordem.
    items = _numbered_items(s[VM_GUIDE_TITLES[15]])
    assert [n for n, _ in items][:4] == [1, 2, 3, 4], f"seção 16: os passos numerados {[n for n, _ in items]}"
    one, two, three, four = (_norm(body) for _, body in items[:4])
    for needle in ("sudo systemctl start cq-backup.service", "cópia", "cq doctor", "backup.age", "0 h"):
        assert _norm(needle) in one, f"seção 16, passo 1: {needle}"
    assert "make_bundle.py" in two, "seção 16, passo 2: o pacote novo"
    assert "cq doctor" in three and "falha" in three, "seção 16, passo 3"
    assert "recuperar a pilha numa vm nova" in four and "windows" in four, "seção 16, passo 4"

    # Frases do guia inteiro.
    _assert_paragraph(text, "duas chaves", "fora da VM", "dois lugares", where="as duas chaves com o Pedro")
    _assert_paragraph(text, "outra pessoa", "acesso SSH", "Pedro", where="o acesso SSH dado pelo Pedro")
    _assert_paragraph(text, "agente", "só a API", "túnel", where="o agente só na API pelo túnel")
    # A pilha da VM é do Pedro por SSH, "com o `cq` instalado na VM e o perfil local
    # de lá": a outra metade da regra, distinta da frase do acesso SSH.
    _assert_paragraph(text, "pilha da VM", "Pedro", "SSH", "cq", "perfil local",
                      where="a pilha da VM operada pelo Pedro por SSH, com o `cq` e o perfil local da VM")
    _assert_paragraph(text, "segredos", "só na VM", "chat do agente", where="os segredos só na VM")
    _assert_paragraph(text, "usuário separado", "autores não confiáveis", where="o usuário separado")

    _check_common(VM_GUIDE)


def _numbered_items(body: str) -> list[tuple[int, str]]:
    """Os itens numerados de primeiro nível (`N.` na coluna 0), cada um com as linhas de continuação."""
    items: list[tuple[int, list[str]]] = []
    for line in body.splitlines():
        match = re.match(r"^(\d+)\.\s", line)
        if match:
            items.append((int(match.group(1)), [line]))
        elif items and (line.startswith((" ", "\t")) or not line.strip()):
            items[-1][1].append(line)
        elif items and not line.startswith("#"):
            items[-1][1].append(line)
    return [(n, "\n".join(lines)) for n, lines in items]


# --------------------------------------------------------------------------------------------------
# Guia de backup
# --------------------------------------------------------------------------------------------------


def test_backup_guide_titles_and_rules() -> None:
    """Os títulos exatos e na ordem; a pilha descartável só com `postgres` e `api` e o `down -v`; a recuperação com
    `--production` antes do `cq up`; `## Depois de restaurar` com a retenção conferida no `cq doctor` e os expurgos
    refeitos a partir do diário preenchido, fora do repositório."""
    text = _doc(BACKUP_GUIDE)
    assert _titles(text) == BACKUP_GUIDE_TITLES
    _assert_contains(_section(text, "## Chaves do age"), "age-keygen", where="chaves do age")
    _assert_paragraph(_section(text, "## Chaves do age"), "dois lugares", where="chaves do age")

    scratch = _section(text, "## Restaurar numa pilha descartável")
    _assert_contains(scratch, "cq-restauracao", "restore.py", "down -v", "CQ_SMTP_", where="pilha descartável")
    assert re.search(r"docker compose -p cq-restauracao\b[^\n]*\bup -d postgres api\b", scratch), (
        "pilha descartável: `docker compose -p cq-restauracao … up -d postgres api`")
    ups = [line for line in scratch.splitlines() if re.search(r"\bup\b", line) and "docker compose" in line]
    assert [line for line in ups if "worker" in line] == [], "pilha descartável: o worker não sobe"
    assert re.search(r"docker compose -p cq-restauracao\b[^\n]*\bdown -v\b", scratch), (
        "pilha descartável: o `down -v` desse projeto")

    recover = _section(text, "## Recuperar a pilha numa VM nova")
    _assert_contains(recover, "restore.py", "--production", "cq up", "cq doctor", where="recuperar")
    assert recover.index("--production") < recover.index("cq up"), "recuperar: o `--production` antes do `cq up`"

    after = _section(text, "## Depois de restaurar")
    _assert_contains(after, "Expurgos realizados", where="depois de restaurar")
    _assert_paragraph(after, "fora do repositório", where="depois de restaurar (o diário preenchido)")
    # A restauração depois de um expurgo: conferir a retenção no
    # `cq doctor` e refazer os expurgos. **[I]** Em parágrafos separados (podem ser dois passos); nada exige uma
    # ordem em relação à volta à operação, e o teste não a confere.
    _assert_paragraph(after, "cq doctor", "retenção",
                      where="depois de restaurar (conferir a retenção no `cq doctor`)")
    _assert_paragraph(after, "refaz", "expurg", where="depois de restaurar (refazer os expurgos)")
    _check_common(BACKUP_GUIDE)


# --------------------------------------------------------------------------------------------------
# Política de retenção
# --------------------------------------------------------------------------------------------------


def test_retention_policy_titles_and_terms() -> None:
    """Os títulos exatos e na ordem; 30, 7 e 14 dias; a rotação dos registros dos contêineres (10m × 3); o limite dos
    backups (a identidade expurgada sai dos backups em até 14 dias depois do expurgo); refazer os expurgos do
    diário depois de uma restauração; a perda de até ~24 h; `CQ_RETENTION_DAYS`; o diário preenchido fora do
    repositório."""
    text = _doc(RETENTION_POLICY)
    assert _titles(text) == RETENTION_TITLES
    _assert_contains(_section(text, "## Banco vivo"), "30 dias", where="banco vivo")
    _assert_contains(_section(text, "## Dumps na VM"), "7 dias", where="dumps na VM")
    # O prazo de cada lugar (o "10m × 3", a rotação `json-file`): `10m` e o 3 num parágrafo.
    logs = _section(text, "## Registros dos contêineres")
    assert any("10m" in p and re.search(r"(?<![\d.,])3(?!\d|[.,]\d)", p) for p in _paragraphs(logs)), (
        "registros dos contêineres: nenhum parágrafo com a rotação de 10m × 3")
    copy = _section(text, "## Cópia no computador do Pedro")
    _assert_contains(copy, "14 dias", where="cópia do Pedro")
    # Os backups levam a identidade expurgada até saírem da rotação.
    _assert_paragraph(copy, "identidade", "expurg", "backup", "14 dias",
                      where="cópia do Pedro (o limite dos backups)")
    # A restauração depois de um expurgo.
    _assert_paragraph(_section(text, "## Depois de uma restauração"), "refazer", "expurg", "diário",
                      where="depois de uma restauração (refazer os expurgos do diário)")
    _assert_contains(text, "~24 h", "CQ_RETENTION_DAYS", where="política")
    _assert_paragraph(text, "diário", "fora do repositório", where="política (o diário preenchido fora do repositório)")
    _check_common(RETENTION_POLICY)


# --------------------------------------------------------------------------------------------------
# Início rápido e skills
# --------------------------------------------------------------------------------------------------


def test_quickstart_uses_the_tunnel_profile() -> None:
    """O exemplo do perfil remoto é o do túnel (`kind = "remote"`, `api_url = "http://127.0.0.1:<porta>"`, sem
    `stack_dir` nem `compose_project`); nenhum `api_url` com `https://`; aponta o guia da VM."""
    text = _doc(QUICKSTART)
    assert re.search(r"api_url\s*=\s*\"https://", text) is None, "início rápido: `api_url` com `https://`"
    profiles = _remote_profiles(_toml_blocks(text))
    assert profiles, "início rápido: nenhum perfil `kind = \"remote\"` num bloco TOML"
    for profile in profiles:
        api_url = str(profile.get("api_url"))
        assert re.fullmatch(r"http://127\.0\.0\.1:(?:\d+|<[^<>]+>)", api_url), profile
        # Com porta numérica, é a do túnel do guia da VM; o marcador `<porta>` vale.
        port = api_url.rsplit(":", 1)[1]
        assert not port.isdigit() or port == PILOT_TUNNEL_PORT, (
            f"início rápido: o perfil remoto não usa a porta do túnel ({PILOT_TUNNEL_PORT}): {api_url}")
        assert "stack_dir" not in profile and "compose_project" not in profile
    _assert_contains(text, "implantacao-vm.md", where="início rápido")
    _check_common(QUICKSTART)


def test_stack_skill_says_vm_is_operated_by_pedro_over_ssh() -> None:
    """A skill `pilha`: no piloto, a pilha da VM é do Pedro por SSH, com o `cq` da VM; o agente opera só a API pelo
    túnel; o `doctor` mostra a retenção vencida.

    **[I]** O "perfil local de lá" não é conferido na skill: o teste pede só que ela diga "com o `cq` da
    VM"."""
    text = _doc(SKILL_STACK)
    _assert_paragraph(text, "Pedro", "SSH", "VM", where="pilha")
    _assert_paragraph(text, "Pedro", "SSH", "cq", "VM", where="pilha (com o `cq` da VM)")
    _assert_paragraph(text, "agente", "só a API", "túnel", where="pilha")
    _assert_paragraph(text, "doctor", "retenção", where="pilha")


def test_closing_skill_cites_retention_as_purge_reminder() -> None:
    """A skill `encerramento`: a verificação de retenção do `doctor` como lembrete do expurgo."""
    text = _doc(SKILL_CLOSING)
    _assert_paragraph(text, "retenção", "doctor", "expurgo", where="encerramento")


# --------------------------------------------------------------------------------------------------
# Piloto: roteiro, plano, manifesto, diário e questionário
# --------------------------------------------------------------------------------------------------


def _checkboxes(text: str) -> list[str]:
    return [_norm(line) for line in text.splitlines() if re.match(r"^\s*- \[ \]", line)]


def _assert_checkbox(boxes: list[str], *fragments: str, where: str) -> int:
    wanted = [_norm(f) for f in fragments]
    hits = [i for i, box in enumerate(boxes) if all(w in box for w in wanted)]
    assert hits, f"{where}: nenhuma caixa `- [ ]` com {list(fragments)}"
    return hits[0]


def test_rehearsal_script_boxes_and_project_switch() -> None:
    """Os títulos; as caixas de cada item do ensaio e do teste de envio (o `doctor` com o backup e o e-mail, o e-mail de
    teste, a restauração da cópia real, os 5 tipos nos dois bots com um erro de formato, os testes iniciais do Telegram, o
    teste de envio pelas operações existentes); o critério de "pronto para convidar"; a troca de projeto, com o
    backup manual e o `backup.age` **antes** dos convites."""
    text = _doc(REHEARSAL)
    assert _titles(text) == REHEARSAL_TITLES
    boxes = _checkboxes(text)
    _assert_checkbox(boxes, "cq doctor", "backup.age", "smtp.server", where="ensaio (doctor)")
    _assert_checkbox(boxes, "e-mail de teste", where="ensaio (e-mail recebido)")
    _assert_checkbox(boxes, "restaur", "cópia", where="ensaio (cópia real restaurada)")
    _assert_checkbox(boxes, "dois bots", where="ensaio (os dois bots)")
    for field_type in sorted(FIELD_TYPES):
        _assert_checkbox(boxes, field_type, where=f"ensaio ({field_type})")
    _assert_checkbox(boxes, "erro de formato", where="ensaio (erro de formato de propósito)")
    for device in ("Android", "iOS", "desktop", "user_id"):
        _assert_checkbox(boxes, device, where=f"teste de envio (dispositivo: {device})")
    for fragment in ("oferecimento de teste", "duas fases", "arquiv", "expurg"):
        _assert_checkbox(boxes, fragment, where=f"teste de envio ({fragment})")
    _assert_checkbox(boxes, "bots register", where="bots pelo CLI")
    _assert_checkbox(boxes, "cq-ensaio", where="o projeto do ensaio")
    _assert_checkbox(boxes, "check_question_refs.py", where="conferência das questões")
    _assert_checkbox(boxes, "Recuperar a pilha numa VM nova", where="leitura da recuperação")
    _assert_checkbox(boxes, "fora do repositório", where="o diário preenchido fora do repositório")

    ready = _section(text, "## Pronto para convidar")
    _assert_paragraph(ready, "não convid", where="pronto para convidar")
    _assert_paragraph(ready, "Pedro", "decid", where="pronto para convidar")

    switch = _section(text, "## Troca para o projeto do piloto")
    switch_boxes = _checkboxes(switch)
    stop = _assert_checkbox(switch_boxes, "cq-ensaio", where="troca (parar o ensaio)")
    up = _assert_checkbox(switch_boxes, "cq up", where="troca (subir o cq)")
    _assert_checkbox(switch_boxes, "bots register", where="troca (bot do piloto pelo CLI)")
    _assert_checkbox(switch_boxes, "cq-backup", where="troca (projeto da unidade do backup)")
    backup = _assert_checkbox(switch_boxes, "systemctl start cq-backup.service", where="troca (backup manual)")
    doctor = _assert_checkbox(switch_boxes, "backup.age", "0 h", where="troca (backup.age em 0 h)")
    assert stop < up < backup <= doctor, "troca: parar, subir, backup manual, doctor, nessa ordem"
    invites = [i for i, box in enumerate(switch_boxes) if "convite" in box]
    assert invites and min(invites) > doctor, "troca: o backup e o doctor antes dos convites"
    _assert_paragraph(text, "convites", "lotes", where="convites em lotes pequenos")
    _assert_paragraph(text, "questionário", "CEP", where="o questionário só com a dispensa do CEP")
    _check_common(REHEARSAL)


def _manifest() -> list[dict]:
    data = json.loads(_doc(MANIFEST))
    assert isinstance(data, list) and data, "manifesto: lista não vazia"
    return data


def _repo_key(url: str) -> str:
    """A URL do repositório sem diferença de maiúsculas, sem a `/` final e sem o `.git` (só para comparar)."""
    return url.strip().lower().rstrip("/").removesuffix(".git")


def test_manifest_entries_and_field_types() -> None:
    """O manifesto `questoes.lock.json`: `{repo, commit, qid, field_type, info_json_sha256}`,
    repositório público por HTTPS, commit de 40 hex, SHA-256 de 64 hex, sem repetição, e os 5 tipos; as entradas do
    repositório de demonstração são exatamente as 11 questões fixadas, com a URL e o commit fixados.
    """
    # Controle das constantes: as 11 questões cobrem os 5 tipos.
    assert len(DEMO_QUESTIONS) == 11 and set(DEMO_QUESTIONS.values()) == FIELD_TYPES
    entries = _manifest()
    for entry in entries:
        assert set(entry) == MANIFEST_KEYS, entry
        assert entry["repo"].startswith("https://"), entry["repo"]
        assert re.fullmatch(r"[0-9a-f]{40}", entry["commit"]), entry["commit"]
        assert re.fullmatch(r"[0-9a-f]{64}", entry["info_json_sha256"]), entry["qid"]
        assert entry["field_type"] in FIELD_TYPES, entry["field_type"]
    refs = [(e["repo"], e["commit"], e["qid"]) for e in entries]
    assert len(refs) == len(set(refs)), "manifesto: referência repetida"
    assert {e["field_type"] for e in entries} == FIELD_TYPES

    # o repositório de demonstração só na URL e no commit fixados, com as 11 questões fixadas.
    demo = [e for e in entries if _repo_key(e["repo"]) == _repo_key(DEMO_REPO)]
    off_pin = [(e["qid"], e["repo"], e["commit"]) for e in demo if (e["repo"], e["commit"]) != (DEMO_REPO, DEMO_COMMIT)]
    assert off_pin == [], (
        f"manifesto: entrada do repositório de demonstração fora da URL ou do commit fixados {off_pin}")
    assert len(demo) == len(DEMO_QUESTIONS), f"manifesto: {len(demo)} entradas de demonstração, não as 11 fixadas"
    assert {e["qid"]: e["field_type"] for e in demo} == dict(DEMO_QUESTIONS), (
        "manifesto: as entradas de demonstração não são as 11 questões fixadas (qid e field_type)")


def _local(value: datetime) -> datetime:
    assert isinstance(value, datetime) and value.tzinfo is not None, f"data e hora sem fuso: {value!r}"
    return value.astimezone(ZoneInfo(PILOT_TIMEZONE))


def test_dispatch_plan_toml() -> None:
    """O plano: 9 `daily` nos dias úteis certos, um por dia, com 9 questões
    distintas; 2 sessões de 10 `intensive` em 12/11 e 18/11, sem sobreposição dentro da sessão e com janela ≥ 5 min;
    os 5 tipos em cada sessão; cada `(repo, commit, qid, field_type)` no manifesto; toda vaga `demo` com a URL e o
    commit fixados, e toda vaga `rodolfo` de outro repositório."""
    plan = tomllib.loads(_doc(PLAN_TOML))
    assert set(plan) == {"timezone", "offering", "slot"}, sorted(plan)
    assert plan["timezone"] == PILOT_TIMEZONE
    assert isinstance(plan["offering"], str) and plan["offering"].strip()
    slots = plan["slot"]
    manifest = {(e["repo"], e["commit"], e["qid"], e["field_type"]) for e in _manifest()}
    for slot in slots:
        assert set(slot) == SLOT_KEYS, slot
        assert slot["kind"] in ("daily", "intensive"), slot["kind"]
        assert slot["source"] in ("rodolfo", "demo"), slot["source"]
        assert slot["repo"].startswith("https://"), slot["repo"]
        assert (slot["repo"], slot["commit"], slot["qid"], slot["field_type"]) in manifest, slot["qid"]
        # a origem `demo` só na URL e no commit fixados; `rodolfo`, só de outro repositório.
        if slot["source"] == "demo":
            assert (slot["repo"], slot["commit"]) == (DEMO_REPO, DEMO_COMMIT), (
                f"plano: vaga `demo` fora da URL ou do commit fixados: {slot['qid']} {slot['repo']} "
                f"{slot['commit']}")
        else:
            assert _repo_key(slot["repo"]) != _repo_key(DEMO_REPO), (
                f"plano: vaga `rodolfo` no repositório de demonstração: {slot['qid']}")
        opens, closes = _local(slot["opens_at"]), _local(slot["closes_at"])
        assert closes - opens >= MIN_WINDOW, (slot["qid"], opens, closes)

    daily = [s for s in slots if s["kind"] == "daily"]
    assert all(s["session"] == "" for s in daily)
    days = sorted(_local(s["opens_at"]).date() for s in daily)
    assert days == sorted(DAILY_DAYS), days
    assert all(day.weekday() < 5 for day in days)
    assert all(_local(s["closes_at"]).date() == _local(s["opens_at"]).date() for s in daily)
    daily_refs = [(s["repo"], s["commit"], s["qid"]) for s in daily]
    assert len(set(daily_refs)) == len(daily_refs), f"plano: os 9 diários sem 9 questões distintas {daily_refs}"

    intensive = [s for s in slots if s["kind"] == "intensive"]
    sessions: dict[str, list[dict]] = {}
    for slot in intensive:
        sessions.setdefault(slot["session"], []).append(slot)
    assert sorted(sessions) == list(INTENSIVE_SESSIONS)
    for session, members in sessions.items():
        assert len(members) == INTENSIVE_PER_SESSION, (session, len(members))
        ordered = sorted(members, key=lambda s: s["opens_at"])
        for slot in ordered:
            assert _local(slot["opens_at"]).date().isoformat() == session, slot["qid"]
        for previous, following in zip(ordered, ordered[1:], strict=False):
            assert previous["closes_at"] <= following["opens_at"], (session, previous["qid"], following["qid"])
        assert {s["field_type"] for s in members} == FIELD_TYPES, session


def test_dispatch_plan_markdown() -> None:
    """O Markdown do plano: os títulos; em cada sessão intensiva, a frase dos erros de formato e o backup
    manual com a cópia no Windows depois da sessão; a origem das questões com o comando de `git cat-file`.
    """
    text = _doc(PLAN_MD)
    assert _titles(text) == PLAN_TITLES
    for title in PLAN_TITLES[1:3]:
        body = _section(text, title)
        _assert_paragraph(body, "erros de formato", where=title)
        _assert_contains(body, "cq-backup.service", "pull_backups.py", where=title)
    _assert_contains(_section(text, "## Origem das questões"), "git cat-file", where="origem das questões")
    _check_common(PLAN_MD)


_IDENTITY_FIELD = re.compile(r"\b(?:nome|e-mail|email|telefone|celular|matr[ií]cula|username|user_id|usu[aá]rio|"
                             r"pseud[oô]nimo|pseudo_id|apelido)\b", re.IGNORECASE)
_RA = re.compile(r"\bRA\b")
_NEGATION = re.compile(r"\b(?:nenhum|nenhuma|sem|não|nunca)\b", re.IGNORECASE)


def _field_lines(text: str) -> list[str]:
    return [line for line in _prose_lines(text) if re.match(r"^\s*(?:[-*|]|\d+\.)", line)]


def test_diary_template_has_no_identity_field() -> None:
    """O modelo do diário: as três seções, com os campos de cada uma; nenhum campo de identidade; a frase da cópia
    preenchida fora do repositório."""
    text = _doc(DIARY)
    assert _titles(text) == DIARY_TITLES
    incident = _norm(_section(text, "## Incidente"))
    for field_name in ("início", "fim", "efeito", "tempo até recuperar", "ação"):
        assert field_name in incident, f"incidente: {field_name}"
    task = _norm(_section(text, "## Tarefa do agente"))
    for field_name in ("pedido", "ferramentas", "confirmações", "resultado"):
        assert field_name in task, f"tarefa do agente: {field_name}"
    purges = _norm(_section(text, "## Expurgos realizados"))
    assert "data" in purges and "código do oferecimento" in purges
    offenders = [line for line in _field_lines(text)
                 if (_IDENTITY_FIELD.search(line) or _RA.search(line)) and not _NEGATION.search(line)]
    assert offenders == [], f"diário: campo de identidade {offenders}"
    _assert_paragraph(text, "preenchida", "fora do repositório", where="diário (a cópia preenchida fora do repositório)")
    _check_common(DIARY)


def test_survey_is_short_anonymous_and_optional() -> None:
    """O questionário: de 1 a 10 perguntas numeradas em sequência; a abertura com "anônimo" e
    "opcional"; nenhuma pergunta de nome, e-mail, RA, telefone ou usuário; a condição da dispensa do CEP.
    """
    text = _doc(SURVEY)
    lines = _prose_lines(text)
    numbered = [(i, int(m.group(1))) for i, line in enumerate(lines) if (m := re.match(r"^\s*(\d+)\.\s+\S", line))]
    assert 1 <= len(numbered) <= 10, f"questionário: {len(numbered)} perguntas"
    assert [n for _, n in numbered] == list(range(1, len(numbered) + 1))
    opening = _norm("\n".join(lines[:numbered[0][0]]))
    assert "anônimo" in opening and "opcional" in opening, "questionário: a abertura"
    questions = [lines[i] for i, _ in numbered]
    offenders = [q for q in questions if _IDENTITY_FIELD.search(q) or _RA.search(q)]
    assert offenders == [], f"questionário: pergunta de identificação {offenders}"
    _assert_paragraph(text, "dispensa", "CEP", where="questionário")
    _check_common(SURVEY)


# --------------------------------------------------------------------------------------------------
# CI e exportação (as regras comuns a todos)
# --------------------------------------------------------------------------------------------------


def test_ci_and_export_guide_common_rules() -> None:
    """O guia de CI e exportação passa pelas regras comuns, com os passos marcados."""
    text = _doc(CI_GUIDE)
    assert MARKER.search(text), "ci-e-exportacao: nenhum passo marcado"
    _assert_contains(text, "export_public.py", "validate_local.py", where="ci-e-exportacao")
    _check_common(CI_GUIDE)
