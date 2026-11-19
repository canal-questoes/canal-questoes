"""Código comum do pacote de implantação e da exportação pública.


- `Allowlist`: a lista positiva `ops/export/allowlist.txt` (padrões `fnmatch` com `**`; `#` comentário);
- `tracked_files` / `select`: os arquivos rastreados (`git ls-files -z`) que casam a lista;
- `scan`: a varredura de segredos e de dados pessoais; o achado (`Finding`) traz caminho, linha e tipo,
  **nunca** o valor;
- `origin_fingerprint`: o estado da origem, para conferir que nada nela mudou;
- `parse_public_contacts`: a lista `ops/export/public-contacts.txt`, cujos endereços o `scan` aceita
  no tipo `email` (`public_contacts`).

Os dois usuários (`ops/deploy/make_bundle.py` e `ops/export/export_public.py`) carregam este arquivo pelo
caminho (`load_repo_scan` de cada um), sem nome de pacote. Só biblioteca padrão; importável sem efeito
colateral.

**[I]** Escolhas da implementação, não fixadas de antemão:
- a lista positiva casa por `fnmatch.fnmatchcase` (o `*` cruza `/`, então `src/**` leva tudo abaixo de `src/`);
  padrão inválido (espaço nas pontas, começo `/`, `!` ou `./`, barra invertida, `/` no fim, segmento vazio, `.`
  ou `..`) é recusado na carga com `ValueError`, sem ecoar o padrão;
- o binário (NUL nos primeiros 8 KiB ou UTF-8 inválido) não passa pelos detectores de linha: ou está na
  `binary-allowlist.txt`, ou vira `binary_not_allowed`;
- o valor comparado com `fictitious.txt` segue o cabeçalho daquela lista: os dígitos no `ra`; no
  `env_assignment`, o que vem depois do `=`/`:` e dos espaços (sem uma aspa de abertura) até o primeiro espaço,
  aspas, crase ou barra invertida, ou o valor-padrão da expansão `${CQ_X:-…}` sem espaços nas pontas nem aspas
  em volta; o caminho nos achados de arquivo inteiro; o texto casado nos demais;
- o `env_assignment` não acusa valor vazio nem modelo (`${…}`, `$(…)`, `$NOME`, `<…>`, `{NOME}` sozinho; ver
  `_ENV_TEMPLATE`) e varre também o `.env.example`, que a regra mínima (fora dele) deixaria de lado:
  o exemplo versionado só traz marcadores, e um valor literal nele seria tão vazamento quanto em outro arquivo;
- os e-mails dos subdomínios de `example.com/.org/.net` também são reservados (RFC 2606), sem distinguir caixa;
- um caminho dado que não é arquivo na árvore (removido sem commit, ou um diretório) é pulado: quem chama varre
  uma árvore materializada (a cópia da exportação, o temporário do pacote);
- as linhas são contadas pelo `\\n` (um `\\r` final sai), e cada (linha, tipo) gera no máximo um achado;
- o `git` roda com `--no-optional-locks` (o `status` não regrava o índice que a impressão digital confere) e
  sem entrada; uma falha dele vira `GitError` com mensagem fixa (nunca a saída do `git`); o `git` ausente sobe
  como `FileNotFoundError`, para quem chama mapear ao código 69.

**[I]** Escolhas da implementação na lista de contatos públicos, não fixadas de antemão:
- na lista de contatos, a linha em branco é a que só tem espaços, e o comentário é a que começa com `#` (sem
  espaço antes, como na `allowlist.txt`); qualquer `\\r` no texto recusa a lista inteira (CRLF); a linha do
  endereço tem de ser o endereço inteiro, sem espaço nas pontas (`fullmatch` da expressão do `email`);
- a função devolve os endereços em minúsculas, e o `scan` compara o texto casado pelo detector `email`, também
  em minúsculas, com eles (igualdade do endereço inteiro: um prefixo ou outro subdomínio continuam acusados).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

# O conjunto fechado dos tipos de achado.
FindingKind = Literal[
    "telegram_bot_token",
    "api_token",
    "age_secret_key",
    "private_key_block",
    "app_password",
    "env_file",
    "env_assignment",
    "email",
    "ra",
    "phone",
    "binary_not_allowed",
]
FINDING_KINDS: tuple[str, ...] = get_args(FindingKind)


@dataclass(frozen=True)
class Finding:
    """Um achado da varredura: sem campo de valor. `line` é 0 nos achados de arquivo
    inteiro (`env_file`, `binary_not_allowed`)."""

    path: str
    line: int
    kind: FindingKind


@dataclass(frozen=True)
class Detector:
    """Um detector da varredura.

    - `target`: `"line"` (a expressão roda em cada linha do texto), `"path"` (no nome do arquivo) ou
      `"file"` (no conteúdo bruto: `binary_not_allowed`, sem expressão);
    - `context`: expressão que tem de casar **na mesma linha** para o achado valer (`app_password`);
    - `secret`: detector de segredo, que também varre a árvore versionada inteira; os
      de dado pessoal (`email`, `ra`, `phone`) e o de binário não.
    """

    pattern: re.Pattern[str] | None
    target: Literal["line", "path", "file"]
    secret: bool
    context: re.Pattern[str] | None = None


# As expressões literais. [I] As do `api_token`, do `app_password`, do `env_file`, do
# `env_assignment`, do `email` e do `ra` são escolha da implementação (a descrição de cada tipo não fixa a
# expressão); podem ser apertadas, com o teste.
# - `api_token`: o segredo da API é `cq_` + `token_urlsafe(32)` (43 caracteres;
#   `server/src/cq_server/tokens.py:4`); `{43,}` também casa o token fictício dos testes
#   (`tests/helpers/api.py:69`, que vai à `fictitious.txt`).
# - `env_assignment`: valor não vazio (nem `""`/`''`) que não seja `${…}` nem marcador `<…>` (com ou sem
#   espaços dentro, como `<senha do banco>`); o nome não vem logo depois de `${` (nem
#   do `${{` da f-string) nem de `$` (em `${CQ_MASTER_KEY:-}` ele é a referência de
#   shell, não uma atribuição). A segunda alternativa acusa o valor-padrão literal da
#   expansão (`${CQ_X:-…}`, `${CQ_X-…}`, `${CQ_X:=…}`, `${CQ_X=…}` e `${CQ_X:+…}`,
#   com algo antes do `}`): `${CQ_X}`, `${CQ_X:-}` e `${CQ_X:?mensagem}` não casam.
#   A primeira alternativa consome só o primeiro caractere do valor, para o `finditer` ainda achar a expansão
#   dentro dele (`CQ_X="${CQ_X:=…}"`). O valor vazio também se decide no código (`_env_value`): a aspa logo
#   seguida de fim de linha, espaço, `,`, `)`, `]` ou `;` é o fecho da string que cita a atribuição
#   (`"CQ_SMTP_PASSWORD=",`), não valor.
DETECTORS: Mapping[FindingKind, Detector] = {
    "telegram_bot_token": Detector(re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "line", True),
    "api_token": Detector(re.compile(r"(?<![A-Za-z0-9_-])cq_[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])"), "line", True),
    "age_secret_key": Detector(re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{58}"), "line", True),
    "private_key_block": Detector(re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "line", True),
    "app_password": Detector(re.compile(r"(?<![A-Za-z])[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}(?![A-Za-z])"), "line",
                             True, context=re.compile(r"senha|password", re.IGNORECASE)),
    "env_file": Detector(re.compile(r"^\.env(?!\.example$)"), "path", True),
    "env_assignment": Detector(
        re.compile(r"(?<!\$\{)(?<!\$\{\{)(?<!\$)"
                   r"\bCQ_(MASTER_KEY|DB_PASSWORD|ADMIN_TOKEN|SMTP_PASSWORD|SMTP_USERNAME)\s*[=:]\s*"
                   r"(?!\$\{)(?!<[^<>]*>)(?![\"']{2}(?![^\s]))(?=[^\s#])\S"
                   r"|\$\{\{?CQ_(?:MASTER_KEY|DB_PASSWORD|ADMIN_TOKEN|SMTP_PASSWORD|SMTP_USERNAME):?[-=+]"
                   r"(?P<default>[^}]+)\}"),
        "line", True),
    "email": Detector(re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"),
                      "line", False),
    "ra": Detector(re.compile(r"(?<![0-9A-Za-z_])(?:RA|ra)[\"']?\s*[:=]?\s*[\"']?\d{5,8}(?![0-9])"), "line", False),
    "phone": Detector(
        re.compile(r"(?<![0-9A-Za-z_.:/-])(?:\+55\s?)?(?:\(\d{2}\)\s?|\d{2}\s)9?\d{4}-\d{4}(?![0-9A-Za-z_])"),
        "line", False),
    "binary_not_allowed": Detector(None, "file", False),
}

# Domínios de e-mail reservados (fora do achado `email`). Entrada com ponto inicial =
# sufixo (`*.invalid`, `*.test`, `*.localhost`); sem ponto = o domínio exato.
RESERVED_EMAIL_DOMAINS: tuple[str, ...] = ("example.com", "example.org", "example.net", ".invalid", ".test",
                                           ".localhost")

# O binário é o arquivo com NUL nos primeiros 8 KiB ou UTF-8 inválido.
BINARY_SNIFF_BYTES = 8192

# O único arquivo `.env*` que pode sair.
ENV_EXAMPLE = ".env.example"


# O tempo máximo de cada comando do `git` (segundos).
GIT_TIMEOUT_SECONDS = 120


class GitError(RuntimeError):
    """O `git` saiu com erro. A mensagem é fixa: nunca traz a saída do `git`, caminho nem valor."""

    def __init__(self) -> None:
        super().__init__("O git saiu com erro.")


def _pattern_problem(pattern: str) -> bool:
    """`True` quando o padrão da lista positiva é inválido (ver o docstring do módulo)."""
    if not pattern or pattern != pattern.strip():
        return True
    if pattern.startswith(("/", "!", "./")) or "\\" in pattern or pattern.endswith("/"):
        return True
    return any(part in ("", ".", "..") for part in pattern.split("/"))


@dataclass(frozen=True)
class Allowlist:
    """A lista positiva: os padrões na ordem do arquivo."""

    patterns: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        """Lê `path` (um padrão por linha; `#` comentário; vazias ignoradas); padrão inválido recusado."""
        text = Path(path).read_bytes().decode("utf-8")
        patterns: list[str] = []
        for number, line in enumerate(text.split("\n"), start=1):
            if not line.strip() or line.startswith("#"):
                continue
            if _pattern_problem(line):
                raise ValueError(f"padrão inválido na linha {number} da lista positiva")
            patterns.append(line)
        return cls(tuple(patterns))

    def matches(self, path: str) -> bool:
        """`path` (relativo, com `/`) casa algum padrão (`fnmatch` com `**`)."""
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.patterns)


def _git(repo: Path, *args: str, git: str = "git") -> bytes:
    completed = subprocess.run([git, "--no-optional-locks", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise GitError()
    return completed.stdout


def _split_z(output: bytes) -> list[str]:
    return [name for name in output.decode("utf-8").split("\0") if name]


def tracked_files(repo: Path, *, git: str = "git") -> list[str]:
    """Os arquivos rastreados de `repo` (`git ls-files -z`), com `/`."""
    return _split_z(_git(repo, "ls-files", "-z", git=git))


def select(repo: Path, allowlist: Allowlist, *, source: Literal["head", "worktree"] = "head",
           git: str = "git") -> list[str]:
    """Os arquivos que casam a lista: rastreados (`head`) ou rastreados e não rastreados não ignorados
    (`worktree`)."""
    if source == "head":
        candidates = tracked_files(repo, git=git)
    elif source == "worktree":
        listed = _split_z(_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", git=git))
        root = Path(repo)
        # [I] No modo `worktree` vão os bytes da árvore: um rastreado removido sem commit não tem o que levar.
        candidates = sorted({name for name in listed if root.joinpath(*name.split("/")).is_file()})
    else:
        raise ValueError("origem desconhecida")
    return [name for name in candidates if allowlist.matches(name)]


def _is_binary(data: bytes) -> bool:
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _email_reserved(domain: str) -> bool:
    lowered = domain.lower()
    for reserved in RESERVED_EMAIL_DOMAINS:
        if reserved.startswith("."):
            if lowered.endswith(reserved):
                return True
        elif lowered == reserved or lowered.endswith("." + reserved):
            return True
    return False


_ENV_VALUE_START = re.compile(r"[=:]\s*")
_ENV_VALUE_STOP = re.compile(r"[\s\"'`\\]")
# O que vem logo depois da aspa que fecha a string citante (`"CQ_SMTP_PASSWORD=",`), em vez de abrir um valor.
_CITING_CLOSE = frozenset(",)];")
_TRAILING_DIGITS = re.compile(r"\d+$")
# [I] Os valores de `env_assignment` que são modelo, não segredo literal: a expansão do shell (`${…}`,
# `$(…)`, `$NOME`), o marcador `<…>` (mesmo entre aspas) e o campo de formatação do Python `{NOME}` sozinho.
_ENV_TEMPLATE = re.compile(r"(?:\$[({]|<[^<>]*>|\{[A-Za-z_][A-Za-z0-9_]*\}(?![^\s\"'`\\])"
                           r"|\$[A-Za-z_][A-Za-z0-9_]*(?![^\s\"'`\\]))")


def _env_value(match: re.Match[str]) -> tuple[str, str]:
    """O valor de `env_assignment` (ver o docstring do módulo) e o resto da linha a partir dele."""
    default = match.group("default")
    if default is not None:
        # O valor-padrão da expansão `${CQ_X:-…}`, sem espaços nas pontas nem um par de aspas em volta.
        value = default.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]
        return value, value
    start = _ENV_VALUE_START.search(match.group(0))
    rest = match.string[match.start() + (start.end() if start else 0):]
    quote = rest[:1]
    if quote in ("\"", "'"):
        after = rest[1:2]
        # A aspa seguida de fim de linha, espaço, `,`, `)`, `]` ou `;` fecha a string que cita a atribuição
        # (`"CQ_SMTP_PASSWORD=",`): o valor é vazio. Seguida de outro caractere, ela abre o valor, feche ou não
        # na mesma linha (o valor multilinha entre aspas, ou a aspa de fecho esquecida, ainda é acusado).
        if not after or after.isspace() or after in _CITING_CLOSE:
            return "", rest
        rest = rest[1:]
    stop = _ENV_VALUE_STOP.search(rest)
    return (rest[:stop.start()] if stop else rest), rest


def _match_value(kind: str, match: re.Match[str]) -> str:
    """O trecho comparado com `fictitious.txt` (ver o docstring do módulo)."""
    text = match.group(0)
    if kind == "ra":
        digits = _TRAILING_DIGITS.search(text)
        return digits.group(0) if digits else text
    if kind == "env_assignment":
        return _env_value(match)[0]
    return text


def _accepted(kind: str, match: re.Match[str], fictitious: Mapping[str, frozenset[str]],
              contacts: frozenset[str]) -> bool:
    if kind == "email" and (_email_reserved(match.group(1)) or match.group(0).lower() in contacts):
        return True
    if kind == "env_assignment":
        value, rest = _env_value(match)
        if not value or _ENV_TEMPLATE.match(rest):
            return True
    return _match_value(kind, match) in fictitious.get(kind, frozenset())


_LINE_DETECTORS: tuple[tuple[str, Detector], ...] = tuple(
    (kind, detector) for kind, detector in DETECTORS.items() if detector.target == "line")
_PATH_DETECTORS: tuple[tuple[str, Detector], ...] = tuple(
    (kind, detector) for kind, detector in DETECTORS.items() if detector.target == "path")


def _scan_text(path: str, text: str, fictitious: Mapping[str, frozenset[str]],
               contacts: frozenset[str] = frozenset()) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.endswith("\r"):
            line = line[:-1]
        for kind, detector in _LINE_DETECTORS:
            if detector.pattern is None:
                continue
            if detector.context is not None and detector.context.search(line) is None:
                continue
            if any(not _accepted(kind, match, fictitious, contacts) for match in detector.pattern.finditer(line)):
                findings.append(Finding(path, number, kind))  # type: ignore[arg-type]
    return findings


class ListInvalid(ValueError):
    """Uma lista de `ops/export/` inválida. A mensagem é fixa e não repete a linha; por ser
    um `ValueError`, quem já mapeia `ValueError` para `internal` continua mapeando."""

    def __init__(self) -> None:
        super().__init__("lista inválida")


def parse_public_contacts(text: str) -> frozenset[str]:
    """Os endereços de `ops/export/public-contacts.txt`; levanta `ListInvalid`
    (endereço inválido, repetido, de domínio reservado, com CRLF).

    Texto vazio ou só com comentários e linhas em branco → conjunto vazio (a lista ausente, quem chama trata como
    texto vazio). Os endereços saem em minúsculas. A mensagem do erro é fixa: nunca traz a linha."""
    if "\r" in text:
        raise ListInvalid()
    pattern = DETECTORS["email"].pattern
    if pattern is None:  # [I] inalcançável: o detector `email` é de linha, com expressão
        raise ListInvalid()
    contacts: set[str] = set()
    for line in text.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        match = pattern.fullmatch(line) if line.isascii() else None
        if match is None or _email_reserved(match.group(1)):
            raise ListInvalid()
        address = line.lower()
        if address in contacts:
            raise ListInvalid()
        contacts.add(address)
    return frozenset(contacts)


def scan(root: Path, paths: Sequence[str], *, fictitious: Mapping[str, frozenset[str]] | None = None,
         binary_allow: frozenset[str] = frozenset(),
         public_contacts: frozenset[str] = frozenset()) -> list[Finding]:
    """A varredura sobre `paths` (relativos a `root`); nunca devolve o valor.
    `fictitious`: tipo → valores aceitos (`fictitious.txt`); `binary_allow`: `binary-allowlist.txt`;
    `public_contacts`: os endereços de `public-contacts.txt`, que não geram achado de `email` (comparados
    sem caixa; nenhum outro tipo muda). Com a lista vazia, nenhum endereço é dispensado."""
    contacts = frozenset(address.lower() for address in public_contacts)
    accepted: Mapping[str, frozenset[str]] = fictitious or {}
    findings: list[Finding] = []
    base = Path(root)
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        for kind, detector in _PATH_DETECTORS:
            if detector.pattern is not None and detector.pattern.search(name) is not None \
                    and path not in accepted.get(kind, frozenset()):
                findings.append(Finding(path, 0, kind))  # type: ignore[arg-type]
        target = base.joinpath(*path.split("/"))
        if not target.is_file():
            continue
        data = target.read_bytes()
        if _is_binary(data):
            if path not in binary_allow and path not in accepted.get("binary_not_allowed", frozenset()):
                findings.append(Finding(path, 0, "binary_not_allowed"))
            continue
        findings.extend(_scan_text(path, data.decode("utf-8"), accepted, contacts))
    return findings


def origin_fingerprint(repo: Path, *, git: str = "git") -> tuple[str, ...]:
    """O estado da origem: `git status --porcelain=v2`, `git rev-parse HEAD`, `git for-each-ref` e o
    SHA-256 do índice."""
    status = _git(repo, "status", "--porcelain=v2", git=git).decode("utf-8")
    head = _git(repo, "rev-parse", "HEAD", git=git).decode("ascii")
    refs = _git(repo, "for-each-ref", git=git).decode("utf-8")
    index_path = _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index", git=git)
    index = Path(index_path.decode("utf-8").strip())
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else ""
    return status, head, refs, index_sha
