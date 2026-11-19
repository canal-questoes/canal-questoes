"""Testes da exportação pública, `ops/export/export_public.py`, e da varredura que ela faz antes de
gravar o destino.

Sempre num repositório de fixture (`tests/helpers/git_fixture.py`) com listas de teste em `ops/export/`; nunca contra
a árvore real. O que se confere:
- destino vazio fora da origem → um repositório git novo com exatamente os arquivos da lista (mais o
  `ops/ci/known-flaky.txt` derivado), um único commit com o autor e o e-mail dados (também
  como committer), a mensagem `Versão inicial do canal-questoes`, **sem** `Co-Authored-By`; sem ganchos e sem a
  configuração global ou do sistema, mesmo com uma configuração hostil no ambiente;
- destino não vazio ou dentro da origem → `dest_invalid` (64) sem escrever nada;
- cada detector da varredura com um valor plantado → `findings` (65), a linha `arquivo:linha: tipo`, o valor
  **ausente** de stdout, stderr e destino, e o destino limpo;
- as exceções de `fictitious.txt` e dos domínios reservados aceitas; `${…}`, `<…>` e valor vazio não acusados por
  `env_assignment`; `phone` acusa `(19) 99999-0000` e `+55 19 99999-0000` e não acusa um `sha256:` de 64 hex, um
  carimbo `20261109T100000Z` nem uma sequência de 10 dígitos;
- o valor-padrão literal da expansão (`${CQ_X:-v}`, `${CQ_X-v}`, `${CQ_X:=v}`, `${CQ_X=v}`, `${CQ_X:+v}`) e o valor
  depois de uma aspa de abertura sem fecho acusados por `env_assignment`; `${CQ_X}`, `${CQ_X:-}` e
  `${CQ_X:?mensagem}` não;
- o `ops/ci/known-flaky.txt` com só as entradas por nó da lista de instáveis (sem as `re:`), gravado antes da
  varredura e coberto por ela;
- `git` ausente → `git_missing` (69);
- a origem intacta (`origin_fingerprint` e o `fingerprint` do ajudante, antes e depois, em sucesso e em recusa).

**O caso depende também da varredura**, além da própria exportação.

**[I]** Escolhas deste teste:
- a exportação lê as listas de `<repo>/ops/export/` e a lista de instáveis de
  `<repo>/docs/execucao/instaveis-conhecidos.txt` (as posições da árvore real); o repositório de fixture as traz;
- o `known-flaky.txt` é conferido inteiro: o cabeçalho fixo da exportação, só de comentários e sem código interno
  (pelo detector `tests/helpers/internal_codes.py`), e uma linha por nó da fonte, só com o nó, sem o motivo, sem TAB
  e sem as linhas `re:`;
- a fixture tem as versões privadas de `AGENTS.md`, `CLAUDE.md` e `ROADMAP.md` na raiz e as públicas na sobreposição
  `ops/export/publico/`: a raiz exportada tem as públicas, byte a byte, e a pasta não sai (a recusa da sobreposição
  inválida fica em `tests/unit/test_export_overlay.py`);
- os e-mails dos subdomínios de `example.com/.org/.net` também são reservados (RFC 2606: os três nomes são
  reservados com tudo o que fica abaixo deles), e a comparação do domínio ignora a caixa;
- o valor de `fictitious.txt` é o trecho acusado (ver o cabeçalho de `ops/export/fictitious.txt`): o endereço
  inteiro no `email`, os dígitos no `ra`, o texto casado nos demais;
- "destino limpo" = o diretório dado continua vazio (ou foi removido); as mensagens e as linhas dos achados saem
  pelo `sys.stdout`/`sys.stderr` do momento da chamada, e o teste confere a união dos dois;
- os valores plantados são montados em tempo de execução, para que o próprio arquivo de teste não tenha achado.
"""

from __future__ import annotations

import os
import secrets
import string
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests.helpers.git_fixture import fingerprint, git_env, make_repo
from tests.helpers.internal_codes import find_in_text
from tests.helpers.ops import clean_env, load_ops, run_main

export_public = load_ops("ops/export/export_public.py")
repo_scan = load_ops("ops/export/repo_scan.py")

MESSAGES = export_public.EXPORT_MESSAGES
LINE = export_public.FINDING_LINE_TEMPLATE
AUTHOR_NAME = "Pessoa Autora da Exportação"
AUTHOR_EMAIL = "autora@exportacao.test"
KNOWN_FLAKY = "ops/ci/known-flaky.txt"
FLAKY_SOURCE = "docs/execucao/instaveis-conhecidos.txt"
GIT_TIMEOUT = 60


def _join(*parts: str) -> str:
    return "".join(parts)


# Valores fictícios da fixture (montados aqui, para o arquivo de teste não ter achado).
FICTITIOUS_EMAIL = _join("pessoa.ficticia", "@", "provedor-ficticio.com.br")
FICTITIOUS_RA = "1357913"
FICTITIOUS_BOT_TOKEN = _join("5550009999", ":", "AAFicticioDaFixtura", "_0123456789abcde")
assert len(FICTITIOUS_BOT_TOKEN.split(":", 1)[1]) == 35

ALLOWLIST = (
    b"# lista positiva de teste\n"
    b"src/**\n"
    b"tests/**\n"
    b"ops/**\n"
    b"config/**\n"
    b"docs/operacao/**\n"
    b"README.md\n"
    b".env.example\n"
)
FICTITIOUS = (
    "# valores ficticios de teste\n"
    f"email\t{FICTITIOUS_EMAIL}\n"
    f"ra\t{FICTITIOUS_RA}\n"
    f"telegram_bot_token\t{FICTITIOUS_BOT_TOKEN}\n"
).encode("utf-8")
BINARY_ALLOWLIST = b"tests/fixtures/imagem.png\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"

FLAKY_NODES = (
    "tests/test_app.py::test_corrida_conhecida",
    "tests/test_app.py::test_parametrizado[caso-1]",
)
FLAKY_TEXT = (
    "# Instáveis conhecidos (fixture)\n"
    f"{FLAKY_NODES[0]}\tcorrida do teste\n"
    f"{FLAKY_NODES[1]}\tporta efêmera\n"
    "re:10048|Address already in use\tWindows: portas\n"
)

# Texto aceito pela varredura: domínios reservados, as exceções fictícias, marcadores e valores vazios, e as formas
# parecidas com telefone que o detector não acusa.
ACCEPTED_TEXT = "\n".join([
    "contatos de exemplo:",
    _join("fulana", "@", "turma.example.org"),
    _join("Beltrano", "@", "Docentes.Example.COM"),
    _join("ciclano", "@", "example.net"),
    _join("alguem", "@", "servico.test"),
    _join("outra", "@", "host.invalid"),
    _join("local", "@", "maquina.localhost"),
    FICTITIOUS_EMAIL,
    _join("RA ", FICTITIOUS_RA),
    FICTITIOUS_BOT_TOKEN,
    "CQ_MASTER_KEY=${CQ_MASTER_KEY}",
    "CQ_DB_PASSWORD=<senha do banco>",
    "CQ_ADMIN_TOKEN=<token>",
    "CQ_SMTP_PASSWORD=",
    'CQ_SMTP_USERNAME=""',
    "  CQ_MASTER_KEY: ${CQ_MASTER_KEY:-}",
    "imagem: postgres@sha256:" + "ab" * 32,
    "carimbo: cq-20261109T100000Z.dump.age",
    "numero longo: 1999990000",
    "",
]).encode("utf-8")

BASE_FILES: dict[str, bytes] = {
    "README.md": b"# Projeto de fixture\n",
    "src/app.py": b'print("ola")\n',
    "src/dados/tabela.json": b'{"chave": "valor"}\n',
    "src/aceitos.txt": ACCEPTED_TEXT,
    "tests/test_app.py": b"def test_app():\n    assert True\n",
    "tests/fixtures/imagem.png": PNG,
    "docs/operacao/guia.md": b"# Guia\n\nPasso 1.\n",
    ".env.example": b"CQ_DB_PASSWORD=<senha do banco>\nCQ_ADMIN_TOKEN=<token>\n",
    "ops/export/allowlist.txt": ALLOWLIST,
    "ops/export/fictitious.txt": FICTITIOUS,
    "ops/export/binary-allowlist.txt": BINARY_ALLOWLIST,
    FLAKY_SOURCE: FLAKY_TEXT.encode("utf-8"),
    # Fora da lista: nunca são exportados.
    ".env": b"CQ_API_PORT=8000\n",
    "docs/execucao/estado.md": b"estado do loop\n",
    "docs/base-de-conhecimento/fatos.md": b"fatos\n",
    "docs/monografia/cap1.md": b"capitulo\n",
    "notas/privado.md": b"rascunho\n",
    # As versões privadas da raiz (fora da lista: nunca saem) e as públicas na sobreposição.
    "AGENTS.md": b"# Versao privada\n\nmarcador-privado-agents\n",
    "CLAUDE.md": b"# Versao privada\n\nmarcador-privado-claude\n",
    "ROADMAP.md": b"# Versao privada\n\nmarcador-privado-roadmap\n",
    "ops/export/publico/AGENTS.md": b"# Projeto de fixture\n\nmarcador-publico-agents\n",
    "ops/export/publico/CLAUDE.md": b"@AGENTS.md\n\nmarcador-publico-claude\n",
    "ops/export/publico/ROADMAP.md": b"# Rumo\n\nmarcador-publico-roadmap\n",
}
EXPECTED_SELECTED = frozenset({
    "README.md", "src/app.py", "src/dados/tabela.json", "src/aceitos.txt", "tests/test_app.py",
    "tests/fixtures/imagem.png", "docs/operacao/guia.md", ".env.example", "ops/export/allowlist.txt",
    "ops/export/fictitious.txt", "ops/export/binary-allowlist.txt",
})
# Os três arquivos da sobreposição, na raiz exportada com os bytes da sobreposição (não os da raiz da origem); a
# pasta da sobreposição não sai no próprio caminho.
OVERLAY_DIR = "ops/export/publico/"
EXPECTED_AT_ROOT = {name: BASE_FILES[OVERLAY_DIR + name] for name in ("AGENTS.md", "CLAUDE.md", "ROADMAP.md")}
PRIVATE_MARKER = b"marcador-privado"


# --------------------------------------------------------------------------------------------------
# Ajudantes
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def git_environ(tmp_path: Path) -> Iterator[dict[str, str]]:
    """O processo com o ambiente do `git` dos testes (sem a configuração do desenvolvedor) durante a chamada."""
    env = git_env(tmp_path)
    with mock.patch.dict(os.environ, env, clear=True):
        yield env


def _git(cwd: Path, *args: str) -> str:
    env = git_env(cwd.parent)
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, timeout=GIT_TIMEOUT, check=False)
    assert done.returncode == 0, f"git {args[0]} falhou no destino ({done.returncode})"
    return done.stdout.decode("utf-8")


def _states(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return repo_scan.origin_fingerprint(repo), fingerprint(repo)


def _empty_dest(tmp_path: Path) -> Path:
    dest = tmp_path / "publico"
    dest.mkdir()
    return dest


def _dest_clean(dest: Path) -> bool:
    return not dest.exists() or not any(dest.iterdir())


def _tree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/") or not path.is_file():
            continue
        files[rel] = path.read_bytes()
    return files


def _all_bytes(root: Path) -> bytes:
    if not root.exists():
        return b""
    return b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _flaky_nodes(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return [line.split("\t", 1)[0] for line in lines]


def _export(repo: Path, dest: Path, **kwargs: str) -> int:
    return export_public.export(repo, dest, AUTHOR_NAME, AUTHOR_EMAIL, **kwargs)


# --------------------------------------------------------------------------------------------------
# Sucesso
# --------------------------------------------------------------------------------------------------


def test_export_creates_single_commit_with_listed_files(tmp_path: Path, git_environ: dict[str, str],
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """exatamente os arquivos da lista e o `known-flaky.txt` derivado, com os bytes da origem,
    num único commit do autor dado, com a mensagem fixa e sem coautoria; as exceções e os falsos telefones
    aceitos; a origem intacta."""
    # As constantes reais do esqueleto (na mesma função do caso vermelho).
    assert export_public.COMMIT_MESSAGE == "Versão inicial do canal-questoes"
    assert {key: code for key, (code, _) in MESSAGES.items()} == {
        "ok": 0, "bundle_ok": 0, "dest_invalid": 64, "out_invalid": 64, "invalid_arguments": 64,
        "pending_changes": 65, "findings": 65, "git_missing": 69, "internal": 1, "overlay_invalid": 65}
    assert LINE.format(path="a/b.txt", line=3, kind="email") == "a/b.txt:3: email"
    assert export_public.load_repo_scan() is repo_scan

    repo = make_repo(tmp_path, BASE_FILES)
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == 0, output
    assert MESSAGES["ok"][1] in output
    files = _tree_files(dest)
    assert set(files) == set(EXPECTED_SELECTED) | set(EXPECTED_AT_ROOT) | {KNOWN_FLAKY}
    for path in EXPECTED_SELECTED:
        assert files[path] == BASE_FILES[path], f"{path}: bytes diferentes"
    for name, data in EXPECTED_AT_ROOT.items():
        assert files[name] == data, f"{name}: a raiz exportada difere da sobreposição"
    assert [path for path in files if path.startswith(OVERLAY_DIR)] == []
    assert [path for path, data in files.items() if PRIVATE_MARKER in data] == []
    assert _flaky_nodes(files[KNOWN_FLAKY].decode("utf-8")) == list(FLAKY_NODES)
    assert [line for line in files[KNOWN_FLAKY].decode("utf-8").splitlines() if line.startswith("re:")] == []
    # O derivado: o cabeçalho fixo (só comentários, sem código interno) e só os nós, sem o motivo.
    flaky = files[KNOWN_FLAKY].decode("utf-8")
    assert flaky == export_public.KNOWN_FLAKY_HEADER + "".join(node + "\n" for node in FLAKY_NODES)
    assert all(line.startswith("#") for line in export_public.KNOWN_FLAKY_HEADER.splitlines())
    assert "\t" not in flaky and "corrida do teste" not in flaky and "porta efêmera" not in flaky
    assert find_in_text(KNOWN_FLAKY, flaky) == []

    assert set(_git(dest, "ls-files", "-z").split("\0")) - {""} == set(files)
    assert _git(dest, "status", "--porcelain").strip() == ""
    assert _git(dest, "rev-list", "--count", "HEAD").strip() == "1"
    meta = _git(dest, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").strip().split("\0")
    assert meta == [AUTHOR_NAME, AUTHOR_EMAIL, AUTHOR_NAME, AUTHOR_EMAIL]
    body = _git(dest, "log", "-1", "--format=%B")
    assert body.strip() == export_public.COMMIT_MESSAGE
    assert "co-authored-by" not in body.lower()
    assert _states(repo) == before


def test_export_ignores_hostile_global_config_and_hooks(tmp_path: Path, git_environ: dict[str, str],
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` no nulo do sistema e `core.hooksPath` vazio: com uma
    configuração hostil no ambiente (autor, ganchos, modelo do `init`), o commit sai com o autor dado e nenhum
    gancho roda nem é copiado."""
    repo = make_repo(tmp_path, BASE_FILES)
    before = _states(repo)
    hostile = tmp_path / "hostil"
    marker = hostile / "gancho-rodou.txt"
    hook = f'#!/bin/sh\necho rodou >> "{marker.as_posix()}"\nexit 0\n'.encode("utf-8")
    for hooks_dir in (hostile / "hooks", hostile / "template" / "hooks"):
        hooks_dir.mkdir(parents=True)
        for name in ("pre-commit", "commit-msg", "post-commit", "prepare-commit-msg"):
            (hooks_dir / name).write_bytes(hook)
            (hooks_dir / name).chmod(0o755)
    config = hostile / "gitconfig"
    config.write_text(
        "[user]\n\tname = Autor Hostil\n\temail = hostil@hostil.test\n"
        f"[core]\n\thooksPath = {(hostile / 'hooks').as_posix()}\n"
        f"[init]\n\ttemplateDir = {(hostile / 'template').as_posix()}\n",
        encoding="utf-8")
    hostile_env = {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config),
                   "GIT_AUTHOR_NAME": "Autor Hostil", "GIT_AUTHOR_EMAIL": "hostil@hostil.test",
                   "GIT_COMMITTER_NAME": "Autor Hostil", "GIT_COMMITTER_EMAIL": "hostil@hostil.test"}
    dest = _empty_dest(tmp_path)

    with mock.patch.dict(os.environ, hostile_env):
        os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
        code = _export(repo, dest)

    output = _output(capsys)
    assert code == 0, output
    assert not marker.exists(), "um gancho da configuração hostil rodou"
    hooks = dest / ".git" / "hooks"
    active = [path.name for path in hooks.iterdir() if not path.name.endswith(".sample")] if hooks.is_dir() else []
    assert active == [], "o modelo hostil do `init` copiou ganchos"
    meta = _git(dest, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").strip().split("\0")
    assert meta == [AUTHOR_NAME, AUTHOR_EMAIL, AUTHOR_NAME, AUTHOR_EMAIL]
    assert _git(dest, "log", "-1", "--format=%B").strip() == export_public.COMMIT_MESSAGE
    assert _states(repo) == before


# --------------------------------------------------------------------------------------------------
# Recusas do destino e do ambiente
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["nao-vazio", "dentro-da-origem"])
def test_invalid_destination_is_refused_without_writing(tmp_path: Path, git_environ: dict[str, str],
                                                        capsys: pytest.CaptureFixture[str], where: str) -> None:
    """destino não vazio ou dentro da origem → `dest_invalid` (64), nada escrito."""
    repo = make_repo(tmp_path, BASE_FILES)
    before = _states(repo)
    if where == "nao-vazio":
        dest = _empty_dest(tmp_path)
        (dest / "existente.txt").write_bytes(b"conteudo anterior\n")
        snapshot = _tree_files(dest)
    else:
        dest = repo / "exportado"
        snapshot = None

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == MESSAGES["dest_invalid"][0] == 64
    assert MESSAGES["dest_invalid"][1] in output
    if snapshot is None:
        assert not dest.exists()
    else:
        assert _tree_files(dest) == snapshot
        assert not (dest / ".git").exists()
    assert _states(repo) == before


def test_missing_git_is_refused(tmp_path: Path, git_environ: dict[str, str],
                                capsys: pytest.CaptureFixture[str]) -> None:
    """`git` ausente → `git_missing` (69), o destino limpo."""
    repo = make_repo(tmp_path, BASE_FILES)
    before = fingerprint(repo)
    dest = _empty_dest(tmp_path)
    missing = tmp_path / "sem-git" / "git-inexistente"

    code = _export(repo, dest, git=str(missing))

    output = _output(capsys)
    assert code == MESSAGES["git_missing"][0] == 69
    assert MESSAGES["git_missing"][1] in output
    assert _dest_clean(dest)
    assert fingerprint(repo) == before


def test_main_rejects_missing_arguments() -> None:
    """sem `--dest`, `--author-name` e `--author-email` → `invalid_arguments` (64) no stderr."""
    result = run_main(export_public, [], env=clean_env())
    assert result.code == MESSAGES["invalid_arguments"][0] == 64
    assert MESSAGES["invalid_arguments"][1] in result.stderr.splitlines()


# --------------------------------------------------------------------------------------------------
# Recusas da varredura
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plant:
    """Um valor plantado: o arquivo (caminho e bytes), a linha esperada do achado e o valor que não pode vazar."""

    path: str
    data: bytes
    line: int
    value: str


def _text_plant(line_text: str, value: str) -> Plant:
    data = f"primeira linha\nsegunda linha\n{line_text}\nquarta linha\n".encode("utf-8")
    return Plant("src/plantado.txt", data, 3, value)


def _random(alphabet: str, size: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(size))


def _plant_telegram() -> Plant:
    token = _join(str(secrets.randbelow(9 * 10 ** 8) + 10 ** 8), ":",
                  _random(string.ascii_letters + string.digits, 35))
    return _text_plant(f"TOKEN_DO_BOT = {token}", token)


def _plant_api_token() -> Plant:
    token = _join("cq", "_", _random(string.ascii_letters + string.digits, 43))
    return _text_plant(f"Authorization: Bearer {token}", token)


def _plant_age_key() -> Plant:
    key = _join("AGE-SECRET-KEY-", "1", _random(string.ascii_uppercase + string.digits, 58))
    return _text_plant(key, key)


def _plant_private_key() -> Plant:
    body = _random(string.ascii_letters + string.digits, 64)
    data = ("primeira linha\nsegunda linha\n"
            + _join("-----BEGIN ", "OPENSSH ", "PRIVATE KEY-----") + "\n"
            + body + "\n"
            + _join("-----END ", "OPENSSH ", "PRIVATE KEY-----") + "\n").encode("utf-8")
    return Plant("src/plantado.txt", data, 3, body)


def _plant_app_password() -> Plant:
    groups = [_random(string.ascii_lowercase, 4) for _ in range(4)]
    value = " ".join(groups)
    return _text_plant(_join("sen", "ha de app do Gmail: ", value), value)


def _plant_env_file() -> Plant:
    value = secrets.token_urlsafe(24)
    return Plant("config/.env.producao", f"CQ_API_PORT=8000\nCQ_OUTRA={value}\n".encode("utf-8"), 0, value)


def _plant_env_assignment() -> Plant:
    value = secrets.token_urlsafe(32)
    return _text_plant(_join("CQ_MASTER", "_KEY", "=", value), value)


def _plant_email() -> Plant:
    value = _join("maria.souza.", _random(string.ascii_lowercase, 6), "@", "provedor-real.com.br")
    return _text_plant(f"contato: {value}", value)


def _plant_ra() -> Plant:
    value = str(secrets.randbelow(9 * 10 ** 6) + 10 ** 6)
    return _text_plant(_join("aluna com R", "A ", value), value)


def _plant_phone_parens() -> Plant:
    value = _join("(19) ", "9", _random(string.digits, 4), "-", _random(string.digits, 4))
    return _text_plant(f"telefone: {value}", value)


# as duas formas de telefone, com os valores literais (montados aqui, sem achado no próprio arquivo).
PHONE_FORMS = (_join("(19) ", "99999-0000"), _join("+55 ", "19 ", "99999-0000"))


def _plant_binary() -> Plant:
    marker = secrets.token_hex(12)
    return Plant("src/blob.bin", b"\x00\x01\x02binario" + marker.encode("ascii"), 0, marker)


PLANTS: dict[str, Callable[[], Plant]] = {
    "telegram_bot_token": _plant_telegram,
    "api_token": _plant_api_token,
    "age_secret_key": _plant_age_key,
    "private_key_block": _plant_private_key,
    "app_password": _plant_app_password,
    "env_file": _plant_env_file,
    "env_assignment": _plant_env_assignment,
    "email": _plant_email,
    "ra": _plant_ra,
    "phone": _plant_phone_parens,
    "binary_not_allowed": _plant_binary,
}


def _assert_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, plant: Plant,
                    extra: Mapping[str, bytes] | None = None) -> None:
    repo = make_repo(tmp_path, {**BASE_FILES, plant.path: plant.data, **(extra or {})})
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == MESSAGES["findings"][0] == 65, output
    assert MESSAGES["findings"][1] in output
    assert LINE.format(path=plant.path, line=plant.line, kind=kind) in output.splitlines()
    assert plant.value not in output, "o valor plantado saiu na mensagem"
    assert plant.value.encode("utf-8") not in _all_bytes(dest), "o valor plantado ficou no destino"
    assert _dest_clean(dest)
    assert _states(repo) == before


FIXTURE_FICTITIOUS = {"email": frozenset({FICTITIOUS_EMAIL}), "ra": frozenset({FICTITIOUS_RA}),
                      "telegram_bot_token": frozenset({FICTITIOUS_BOT_TOKEN})}


def test_scan_accepts_reserved_fictitious_and_template_values(tmp_path: Path) -> None:
    """o conjunto fechado dos 11 tipos (um caso plantado por tipo), só os de segredo marcados para a
    árvore versionada inteira; o texto aceito da fixture (domínios reservados, exceções fictícias,
    `${…}`, `<…>`, valor vazio e as formas que o `phone` não acusa) sem achado."""
    # As tabelas reais do esqueleto (na mesma função do caso vermelho).
    assert set(PLANTS) == set(repo_scan.FINDING_KINDS) == set(repo_scan.DETECTORS)
    secret_kinds = {kind for kind, detector in repo_scan.DETECTORS.items() if detector.secret}
    assert {"email", "ra", "phone", "binary_not_allowed"}.isdisjoint(secret_kinds)
    assert not hasattr(repo_scan.Finding("a.txt", 0, "env_file"), "value")

    (tmp_path / "aceitos.txt").write_bytes(ACCEPTED_TEXT)
    (tmp_path / "imagem.png").write_bytes(PNG)
    findings = repo_scan.scan(tmp_path, ["aceitos.txt", "imagem.png"], fictitious=FIXTURE_FICTITIOUS,
                              binary_allow=frozenset({"imagem.png"}))
    assert findings == []


@pytest.mark.parametrize("kind", sorted(PLANTS))
def test_scan_reports_each_kind_with_path_and_line_only(tmp_path: Path, kind: str) -> None:
    """cada detector acusa o seu valor plantado com exatamente um `Finding(caminho, linha, tipo)`
    (linha 0 nos de arquivo inteiro), sem campo de valor."""
    plant = PLANTS[kind]()
    target = tmp_path.joinpath(*plant.path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plant.data)

    findings = repo_scan.scan(tmp_path, [plant.path], fictitious=FIXTURE_FICTITIOUS, binary_allow=frozenset())

    assert findings == [repo_scan.Finding(plant.path, plant.line, kind)]
    assert plant.value not in repr(findings)


# O docstring de `ops/export/repo_scan.py`: o valor-padrão
# literal da expansão do shell e o valor depois de uma aspa de abertura sem fecho são `env_assignment`. Os nomes vêm
# sem o prefixo `CQ_` e as linhas são montadas em pedaços (`_expansion`), para o próprio arquivo não ter achado.
DEFAULT_OPERATORS: dict[str, tuple[str, str]] = {
    "padrao": ("DB_PASSWORD", ":-"),
    "padrao-sem-dois-pontos": ("SMTP_USERNAME", "-"),
    "atribui-padrao": ("MASTER_KEY", ":="),
    "atribui-sem-dois-pontos": ("ADMIN_TOKEN", "="),
    "alternativo": ("SMTP_PASSWORD", ":+"),
}
# O contexto da linha: a chave do compose sem aspas (como no `compose.yaml`) e a atribuição do shell entre aspas.
EXPANSION_CONTEXTS: dict[str, Callable[[str, str], str]] = {
    "compose": lambda name, expansion: _join("      CQ_", name, ": ", expansion),
    "shell": lambda name, expansion: _join("CQ_", name, "=", '"', expansion, '"'),
}


def _expansion(name: str, suffix: str) -> str:
    """A expansão do shell de `CQ_<name>` com `suffix` (operador e valor-padrão, ou nada)."""
    return _join("$", "{", "CQ_", name, suffix, "}")


def _scan_text(tmp_path: Path, data: bytes) -> list[object]:
    (tmp_path / "plantado.txt").write_bytes(data)
    return repo_scan.scan(tmp_path, ["plantado.txt"], fictitious=FIXTURE_FICTITIOUS, binary_allow=frozenset())


@pytest.mark.parametrize("context", sorted(EXPANSION_CONTEXTS))
@pytest.mark.parametrize("form", sorted(DEFAULT_OPERATORS))
def test_scan_reports_literal_default_of_shell_expansion(tmp_path: Path, form: str, context: str) -> None:
    """O valor-padrão literal de `${CQ_X:-v}` (e de `-`, `:=`, `=` e `:+`) é `env_assignment`,
    na chave do compose e na atribuição do shell, com só caminho, linha e tipo."""
    name, operator = DEFAULT_OPERATORS[form]
    value = _random(string.ascii_letters + string.digits, 32)
    line = EXPANSION_CONTEXTS[context](name, _expansion(name, _join(operator, value)))

    findings = _scan_text(tmp_path, f"primeira linha\n{line}\nterceira linha\n".encode("utf-8"))

    assert findings == [repo_scan.Finding("plantado.txt", 2, "env_assignment")]
    assert value not in repr(findings)


@pytest.mark.parametrize("context", sorted(EXPANSION_CONTEXTS))
@pytest.mark.parametrize("form", ["sem-padrao", "padrao-vazio", "mensagem-de-erro"])
def test_scan_accepts_expansion_without_literal_default(tmp_path: Path, form: str, context: str) -> None:
    """`${CQ_X}`, `${CQ_X:-}` e `${CQ_X:?mensagem}` (as formas do `compose.yaml`) não são
    acusados. Controle contra o vazio: na linha seguinte, a mesma linha com um valor-padrão literal é acusada."""
    name = "DB_PASSWORD"
    suffix = {"sem-padrao": "", "padrao-vazio": ":-",
              "mensagem-de-erro": _join(":?defina CQ_", name, " (veja .env.example)")}[form]
    value = _random(string.ascii_letters + string.digits, 32)
    line = EXPANSION_CONTEXTS[context](name, _expansion(name, suffix))
    control = EXPANSION_CONTEXTS[context](name, _expansion(name, _join(":-", value)))

    findings = _scan_text(tmp_path, f"primeira linha\n{line}\n{control}\n".encode("utf-8"))

    assert findings == [repo_scan.Finding("plantado.txt", 3, "env_assignment")]
    assert value not in repr(findings)


@pytest.mark.parametrize("quote", ['"', "'"], ids=["aspas-duplas", "aspas-simples"])
@pytest.mark.parametrize("tail", ["", " e o resto da linha"], ids=["fim-de-linha", "com-texto"])
def test_scan_reports_value_after_unclosed_opening_quote(tmp_path: Path, quote: str, tail: str) -> None:
    """A aspa de abertura sem fecho na linha (`CQ_X="valor…`: o valor multilinha ou a aspa de
    fecho esquecida) não esconde o valor: `env_assignment`, com só caminho, linha e tipo."""
    value = _random(string.ascii_letters + string.digits, 32)
    line = _join("CQ_DB", "_PASSWORD", "=", quote, value, tail)

    findings = _scan_text(tmp_path, f"primeira linha\n{line}\nterceira linha\n".encode("utf-8"))

    assert findings == [repo_scan.Finding("plantado.txt", 2, "env_assignment")]
    assert value not in repr(findings)


@pytest.mark.parametrize("kind", sorted(PLANTS))
def test_planted_value_is_refused_without_leaking(tmp_path: Path, git_environ: dict[str, str],
                                                  capsys: pytest.CaptureFixture[str], kind: str) -> None:
    """cada detector com um valor plantado → `findings` (65), `arquivo:linha: tipo`, o valor ausente
    de toda saída e do destino, o destino limpo, a origem intacta."""
    _assert_refused(tmp_path, capsys, kind, PLANTS[kind]())


@pytest.mark.parametrize("value", PHONE_FORMS, ids=["ddd-entre-parenteses", "com-codigo-do-pais"])
def test_phone_forms_with_area_code_are_refused(tmp_path: Path, git_environ: dict[str, str],
                                         capsys: pytest.CaptureFixture[str], value: str) -> None:
    """`(19) 99999-0000` e `+55 19 99999-0000` são acusados (o `sha256:`, o carimbo e os 10 dígitos ficam no
    texto aceito da fixture, exportado com sucesso)."""
    _assert_refused(tmp_path, capsys, "phone", _text_plant(f"telefone: {value}", value))


def test_known_flaky_is_written_before_the_scan_and_covered_by_it(tmp_path: Path, git_environ: dict[str, str],
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """o `known-flaky.txt` derivado passa pela varredura; um e-mail plantado num nó da lista
    de instáveis é acusado nele, sem o valor; um plantado numa entrada `re:` não é (a entrada não sai)."""
    in_node = _join("pessoa.real.", _random(string.ascii_lowercase, 6), "@", "provedor-real.com.br")
    in_regex = _join("outra.real.", _random(string.ascii_lowercase, 6), "@", "provedor-real.com.br")
    flaky = (FLAKY_TEXT
             + f"tests/test_app.py::test_contato[{in_node}]\tmotivo\n"
             + f"re:{in_regex}\tmotivo\n").encode("utf-8")
    repo = make_repo(tmp_path, {**BASE_FILES, FLAKY_SOURCE: flaky})
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == MESSAGES["findings"][0] == 65, output
    flaky_lines = [line for line in output.splitlines() if line.startswith(KNOWN_FLAKY + ":")]
    assert len(flaky_lines) == 1 and flaky_lines[0].endswith(": email"), flaky_lines
    assert [line for line in output.splitlines() if line.endswith(": email")] == flaky_lines
    assert in_node not in output and in_regex not in output
    assert _dest_clean(dest)
    assert _states(repo) == before
