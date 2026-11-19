"""Testes da sobreposição pública da exportação, `ops/export/export_public.py`.

A sobreposição é a pasta `ops/export/publico/` da origem, com exatamente `AGENTS.md`, `CLAUDE.md` e `ROADMAP.md`: a
exportação põe esses três arquivos na raiz da árvore exportada, byte a byte, no lugar das versões privadas da raiz, e
a pasta não sai no próprio caminho. Sempre num repositório de fixture (`tests/helpers/git_fixture.py`), com as
versões privadas na raiz e as públicas na sobreposição, cada uma com o seu marcador; nunca contra a árvore real.

O que se confere:
- a raiz exportada igual à sobreposição, byte a byte; nenhum byte das versões privadas no destino; a pasta da
  sobreposição ausente do destino;
- a sobreposição inválida (falta um arquivo, sobra um, uma subpasta, um link simbólico, um nome com outra caixa, a
  pasta inexistente) → a recusa `overlay_invalid` (65), com a mensagem fixa sem caminho nem valor, o destino vazio e a
  origem intacta;
- a sobreposição lida do último commit, não da árvore de trabalho;
- a ordem das conferências: os argumentos, o destino, o `git` e as listas do commit antes da sobreposição, e a
  sobreposição antes de qualquer escrita no destino;
- a varredura cobre os três arquivos na raiz: um token e um e-mail fora da lista de contatos no `AGENTS.md` da
  sobreposição → `findings` (65), com `arquivo:linha: tipo` no caminho da raiz e sem o valor;
- `overlay_entries` sobre a árvore do commit: o id do blob de cada um dos três nomes, ou a recusa.

Vermelho esperado: até a exportação ganhar a sobreposição, ela é a do esqueleto (a pasta sai no próprio caminho,
a raiz fica sem os três arquivos, nenhuma sobreposição é recusada e a lista de contatos não é lida), e
`overlay_entries` lança `NotImplementedError`.

**[I]** Escolhas deste teste:
- a lista positiva da fixture casa `ops/**`, para que a pasta da sobreposição case a lista e a exclusão dela venha da
  exportação, e não casa as versões privadas da raiz;
- o link simbólico é gravado no índice pelo `git update-index --cacheinfo 120000,…` (sem criar o link no disco, o
  que nem sempre é possível no Windows);
- "destino vazio" = o diretório dado continua existindo e vazio (a recusa vem antes da primeira escrita);
- os valores que a varredura acusa são montados em tempo de execução, para o próprio arquivo de teste não ter achado,
  e o caminho da lista privada de instáveis vem da constante da exportação.
"""

from __future__ import annotations

import os
import secrets
import string
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest import mock

import pytest

from tests.helpers.git_fixture import add_untracked, fingerprint, git_env, make_repo, modify
from tests.helpers.ops import load_ops

export_public = load_ops("ops/export/export_public.py")
repo_scan = load_ops("ops/export/repo_scan.py")

MESSAGES = export_public.EXPORT_MESSAGES
LINE = export_public.FINDING_LINE_TEMPLATE
AUTHOR_NAME = "Pessoa Autora da Exportação"
AUTHOR_EMAIL = "autora@exportacao.test"
GIT_TIMEOUT = 60

OVERLAY_DIR = "ops/export/publico/"
OVERLAY_NAMES = ("AGENTS.md", "CLAUDE.md", "ROADMAP.md")
OVERLAY_MESSAGE = "A sobreposição pública deve ter exatamente AGENTS.md, CLAUDE.md e ROADMAP.md."
KNOWN_FLAKY = "ops/ci/known-flaky.txt"
CONTACTS = "ops/export/public-contacts.txt"
FICTITIOUS_LIST = "ops/export/fictitious.txt"
# O caminho da lista privada de instáveis, pela constante da exportação (o arquivo de teste não o escreve).
FLAKY_SOURCE = export_public.FLAKY_SOURCE_PATH


def _join(*parts: str) -> str:
    return "".join(parts)


# As versões públicas (na sobreposição) e as privadas (na raiz da origem), com marcadores distintos.
PUBLIC: dict[str, bytes] = {
    "AGENTS.md": b"# Projeto de fixture\n\nmarcador-publico-agents\n",
    "CLAUDE.md": b"@AGENTS.md\n\nmarcador-publico-claude\n",
    "ROADMAP.md": b"# Rumo\n\nmarcador-publico-roadmap\n",
}
PRIVATE: dict[str, bytes] = {
    "AGENTS.md": b"# Versao privada\n\nmarcador-privado-agents\n",
    "CLAUDE.md": b"# Versao privada\n\nmarcador-privado-claude\n",
    "ROADMAP.md": b"# Versao privada\n\nmarcador-privado-roadmap\n",
}
PRIVATE_MARKER = b"marcador-privado"

LISTED: dict[str, bytes] = {
    "README.md": b"# Projeto de fixture\n",
    "src/app.py": b'print("ola")\n',
    "ops/export/allowlist.txt": b"# lista positiva de teste\nsrc/**\nops/**\nREADME.md\n",
    FICTITIOUS_LIST: b"",
    "ops/export/binary-allowlist.txt": b"",
}
BASE_FILES: dict[str, bytes] = {
    **LISTED,
    **PRIVATE,
    **{OVERLAY_DIR + name: data for name, data in PUBLIC.items()},
    FLAKY_SOURCE: b"# nenhum\n",
    # Fora da lista: nunca sai.
    "notas/privado.md": b"rascunho\n",
}


# --------------------------------------------------------------------------------------------------
# Ajudantes
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def git_environ(tmp_path: Path) -> Iterator[dict[str, str]]:
    """O processo com o ambiente do `git` dos testes (sem a configuração do desenvolvedor) durante a chamada."""
    env = git_env(tmp_path)
    with mock.patch.dict(os.environ, env, clear=True):
        yield env


def _git(cwd: Path, *args: str, stdin: bytes = b"") -> str:
    env = git_env(cwd.parent)
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    done = subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, timeout=GIT_TIMEOUT,
                          check=False)
    assert done.returncode == 0, f"git {args[0]} falhou ({done.returncode})"
    return done.stdout.decode("utf-8")


def _states(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return repo_scan.origin_fingerprint(repo), fingerprint(repo)


def _empty_dest(tmp_path: Path, name: str = "publico") -> Path:
    dest = tmp_path / name
    dest.mkdir()
    return dest


def _still_empty(dest: Path) -> bool:
    return dest.is_dir() and not any(dest.iterdir())


def _tree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/") or not path.is_file():
            continue
        files[rel] = path.read_bytes()
    return files


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _export(repo: Path, dest: Path, *, author_name: str = AUTHOR_NAME, git: str = "git") -> int:
    return export_public.export(repo, dest, author_name, AUTHOR_EMAIL, git=git)


def _without(files: Mapping[str, bytes], *paths: str) -> dict[str, bytes]:
    return {path: data for path, data in files.items() if path not in paths}


def _symlink_in_overlay(repo: Path, name: str) -> None:
    """Troca `name` da sobreposição por um link simbólico no índice e registra num commit."""
    oid = _git(repo, "hash-object", "-w", "--stdin", stdin=b"AGENTS.md").strip()
    _git(repo, "update-index", "--cacheinfo", f"120000,{oid},{OVERLAY_DIR}{name}")
    _git(repo, "-c", "core.hooksPath=", "commit", "-q", "-m", "link na sobreposição")
    assert _git(repo, "ls-tree", "HEAD", "--", OVERLAY_DIR + name).startswith("120000 ")


# Cada sobreposição inválida: os arquivos da fixture e, no caso do link, o passo depois do commit.
INVALID_OVERLAYS: dict[str, dict[str, bytes]] = {
    "falta-agents": _without(BASE_FILES, OVERLAY_DIR + "AGENTS.md"),
    "falta-claude": _without(BASE_FILES, OVERLAY_DIR + "CLAUDE.md"),
    "falta-roadmap": _without(BASE_FILES, OVERLAY_DIR + "ROADMAP.md"),
    "sobra-um": {**BASE_FILES, OVERLAY_DIR + "README.md": b"# a mais\n"},
    "subpasta": {**BASE_FILES, OVERLAY_DIR + "notas/rascunho.md": b"rascunho\n"},
    "outra-caixa": {**_without(BASE_FILES, OVERLAY_DIR + "ROADMAP.md"), OVERLAY_DIR + "roadmap.md": b"# rumo\n"},
    "pasta-inexistente": _without(BASE_FILES, *(OVERLAY_DIR + name for name in OVERLAY_NAMES)),
    "link-simbolico": BASE_FILES,
}


def _invalid_repo(tmp_path: Path, case: str, *, name: str = "origem",
                  extra: Mapping[str, bytes] | None = None) -> Path:
    repo = make_repo(tmp_path, {**INVALID_OVERLAYS[case], **(extra or {})}, name=name)
    if case == "link-simbolico":
        _symlink_in_overlay(repo, "CLAUDE.md")
    return repo


def _assert_overlay_refusal(output: str, code: int, dest: Path) -> None:
    assert code == MESSAGES["overlay_invalid"][0] == 65, output
    assert [line for line in output.splitlines() if line.strip()] == [OVERLAY_MESSAGE], output
    assert _still_empty(dest), "a recusa da sobreposição escreveu no destino"


# --------------------------------------------------------------------------------------------------
# A sobreposição na raiz
# --------------------------------------------------------------------------------------------------


def test_overlay_lands_at_root_byte_for_byte_without_private_versions(tmp_path: Path, git_environ: dict[str, str],
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    """A raiz exportada tem os três arquivos da sobreposição, byte a byte; nenhum byte das versões privadas sai; a
    pasta da sobreposição não aparece no destino; os arquivos da lista saem iguais; a origem fica intacta.
    Vermelho até a exportação ganhar a sobreposição (no esqueleto, a pasta sai no próprio caminho e a raiz fica sem os
    três)."""
    # O que o esqueleto já tem (na mesma função do caso vermelho): as constantes e a mensagem fixa.
    assert export_public.OVERLAY_DIR == OVERLAY_DIR
    assert export_public.OVERLAY_FILES == OVERLAY_NAMES
    assert export_public.PUBLIC_CONTACTS_PATH == CONTACTS
    assert MESSAGES["overlay_invalid"] == (65, OVERLAY_MESSAGE)
    assert issubclass(export_public.OverlayInvalid, Exception)

    repo = make_repo(tmp_path, BASE_FILES)
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == 0, output
    files = _tree_files(dest)
    assert set(files) == set(LISTED) | set(OVERLAY_NAMES) | {KNOWN_FLAKY}, sorted(files)
    for name in OVERLAY_NAMES:
        assert files.get(name) == PUBLIC[name], f"{name}: a raiz exportada difere da sobreposição"
    for path, data in LISTED.items():
        assert files[path] == data, f"{path}: bytes diferentes"
    assert [path for path in files if path.startswith(OVERLAY_DIR)] == []
    assert not (dest / "ops" / "export" / "publico").exists()
    assert [path for path, data in files.items() if PRIVATE_MARKER in data] == []
    assert set(_git(dest, "ls-files", "-z").split("\0")) - {""} == set(files)
    assert _states(repo) == before


def test_overlay_is_read_from_the_commit_not_the_worktree(tmp_path: Path, git_environ: dict[str, str],
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    """A sobreposição vem do último commit: um arquivo dela alterado sem commit sai com os bytes do commit, e um
    arquivo a mais não rastreado na pasta não a torna inválida nem sai. Vermelho até a exportação ganhar a
    sobreposição."""
    repo = make_repo(tmp_path, BASE_FILES)
    modify(repo, OVERLAY_DIR + "AGENTS.md", b"# alterado sem commit\n\nmarcador-nao-commitado\n")
    add_untracked(repo, OVERLAY_DIR + "EXTRA.md", b"marcador-nao-rastreado\n")
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == 0, output
    files = _tree_files(dest)
    assert files.get("AGENTS.md") == PUBLIC["AGENTS.md"], "a raiz exportada não tem a versão do commit"
    assert [path for path, data in files.items()
            if b"marcador-nao-commitado" in data or b"marcador-nao-rastreado" in data] == []
    assert "EXTRA.md" not in files
    assert _states(repo) == before


# --------------------------------------------------------------------------------------------------
# A recusa da sobreposição inválida
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(INVALID_OVERLAYS))
def test_invalid_overlay_is_refused_before_any_write(tmp_path: Path, git_environ: dict[str, str],
                                                     capsys: pytest.CaptureFixture[str], case: str) -> None:
    """Falta um arquivo, sobra um, há uma subpasta, um link simbólico ou um nome com outra caixa, ou a pasta não
    existe → `overlay_invalid` (65), só a mensagem fixa (sem caminho nem valor), o destino vazio e a origem intacta.
    Vermelho até a exportação conferir a sobreposição (no esqueleto, ela exporta com sucesso)."""
    repo = _invalid_repo(tmp_path, case)
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    _assert_overlay_refusal(_output(capsys), code, dest)
    assert _states(repo) == before


def test_overlay_is_checked_after_arguments_destination_git_and_lists(tmp_path: Path, git_environ: dict[str, str],
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """A ordem das conferências, com a sobreposição inválida em todas as origens: o autor inválido, o destino não
    vazio, o `git` ausente e a lista de valores fictícios inválida saem com as próprias recusas; a lista de contatos
    inválida sai com `internal` (1); só com tudo válido a recusa é `overlay_invalid`, e o destino continua vazio.
    Vermelho até a exportação ler a lista de contatos e conferir a sobreposição (as quatro primeiras recusas já
    existem no esqueleto)."""
    bad_overlay = _invalid_repo(tmp_path, "pasta-inexistente", name="sem-sobreposicao")
    states = _states(bad_overlay)

    # O autor inválido, antes de qualquer `git`.
    dest = _empty_dest(tmp_path, "destino-autor")
    code = _export(bad_overlay, dest, author_name="")
    output = _output(capsys)
    assert code == MESSAGES["invalid_arguments"][0] == 64, output
    assert MESSAGES["invalid_arguments"][1] in output and OVERLAY_MESSAGE not in output

    # O destino não vazio.
    dest = _empty_dest(tmp_path, "destino-ocupado")
    (dest / "existente.txt").write_bytes(b"conteudo anterior\n")
    code = _export(bad_overlay, dest)
    output = _output(capsys)
    assert code == MESSAGES["dest_invalid"][0] == 64, output
    assert MESSAGES["dest_invalid"][1] in output and OVERLAY_MESSAGE not in output
    assert _tree_files(dest) == {"existente.txt": b"conteudo anterior\n"}

    # O `git` ausente.
    dest = _empty_dest(tmp_path, "destino-sem-git")
    code = _export(bad_overlay, dest, git=str(tmp_path / "sem-git" / "git-inexistente"))
    output = _output(capsys)
    assert code == MESSAGES["git_missing"][0] == 69, output
    assert OVERLAY_MESSAGE not in output and _still_empty(dest)

    # A lista de valores fictícios inválida.
    bad_list = _invalid_repo(tmp_path, "pasta-inexistente", name="lista-invalida",
                             extra={FICTITIOUS_LIST: b"linha sem tabulacao\n"})
    dest = _empty_dest(tmp_path, "destino-lista")
    code = _export(bad_list, dest)
    output = _output(capsys)
    assert code == MESSAGES["internal"][0] == 1, output
    assert OVERLAY_MESSAGE not in output and _still_empty(dest)

    # A lista de contatos inválida (uma das listas do commit).
    bad_contacts = _invalid_repo(tmp_path, "pasta-inexistente", name="contatos-invalidos",
                                 extra={CONTACTS: b"nao-e-um-endereco\n"})
    dest = _empty_dest(tmp_path, "destino-contatos")
    code = _export(bad_contacts, dest)
    output = _output(capsys)
    assert code == MESSAGES["internal"][0] == 1, output
    assert OVERLAY_MESSAGE not in output and _still_empty(dest)

    # Tudo válido, menos a sobreposição.
    dest = _empty_dest(tmp_path, "destino-valido")
    code = _export(bad_overlay, dest)
    _assert_overlay_refusal(_output(capsys), code, dest)
    assert _states(bad_overlay) == states


# --------------------------------------------------------------------------------------------------
# A varredura cobre a sobreposição
# --------------------------------------------------------------------------------------------------


def _random(alphabet: str, size: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(size))


def test_scan_covers_the_overlay_at_root_without_leaking(tmp_path: Path, git_environ: dict[str, str],
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """Um token e um e-mail fora da lista de contatos no `AGENTS.md` da sobreposição → `findings` (65), as linhas
    `arquivo:linha: tipo` no caminho da raiz (`AGENTS.md`), nenhuma outra, o valor ausente da saída e do destino,
    o destino vazio e a origem intacta. O endereço da lista de contatos, no próprio arquivo da lista, não é acusado.
    Vermelho até a exportação ganhar a sobreposição e a lista de contatos (no esqueleto, os achados saem no caminho
    da pasta, e o endereço da lista é acusado)."""
    token = _join(str(secrets.randbelow(9 * 10 ** 8) + 10 ** 8), ":",
                  _random(string.ascii_letters + string.digits, 35))
    email = _join("maria.souza.", _random(string.ascii_lowercase, 6), "@", "provedor-real.com.br")
    listed_contact = _join("contato.", _random(string.ascii_lowercase, 6), "@", "projeto-aberto.org.br")
    agents = f"# Projeto de fixture\nTOKEN_DO_BOT = {token}\ncontato: {email}\n".encode("utf-8")
    repo = make_repo(tmp_path, {**BASE_FILES, OVERLAY_DIR + "AGENTS.md": agents,
                                CONTACTS: f"# contatos publicos de teste\n{listed_contact}\n".encode("utf-8")})
    before = _states(repo)
    dest = _empty_dest(tmp_path)

    code = _export(repo, dest)

    output = _output(capsys)
    assert code == MESSAGES["findings"][0] == 65, output
    assert MESSAGES["findings"][1] in output
    finding_lines = {line for line in output.splitlines() if line.strip() and line != MESSAGES["findings"][1]}
    assert finding_lines == {LINE.format(path="AGENTS.md", line=2, kind="telegram_bot_token"),
                             LINE.format(path="AGENTS.md", line=3, kind="email")}, sorted(finding_lines)
    for value in (token, email, listed_contact):
        assert value not in output, "um valor saiu na mensagem"
    assert _still_empty(dest)
    assert _states(repo) == before


# --------------------------------------------------------------------------------------------------
# overlay_entries
# --------------------------------------------------------------------------------------------------


def _oid(char: str) -> str:
    return char * 40


VALID_TREE: dict[str, tuple[str, str]] = {
    "README.md": ("100644", _oid("1")),
    "ops/export/allowlist.txt": ("100644", _oid("2")),
    OVERLAY_DIR + "AGENTS.md": ("100644", _oid("a")),
    OVERLAY_DIR + "CLAUDE.md": ("100644", _oid("c")),
    OVERLAY_DIR + "ROADMAP.md": ("100644", _oid("e")),
    # Começa como a pasta, mas é outra pasta: não conta.
    "ops/export/publicos/AGENTS.md": ("100644", _oid("f")),
    "AGENTS.md": ("100644", _oid("9")),
}

INVALID_TREES: dict[str, dict[str, tuple[str, str]]] = {
    "falta-um": {path: entry for path, entry in VALID_TREE.items() if path != OVERLAY_DIR + "CLAUDE.md"},
    "sobra-um": {**VALID_TREE, OVERLAY_DIR + "README.md": ("100644", _oid("b"))},
    "subpasta": {**VALID_TREE, OVERLAY_DIR + "notas/AGENTS.md": ("100644", _oid("d"))},
    "link-simbolico": {**VALID_TREE, OVERLAY_DIR + "ROADMAP.md": ("120000", _oid("e"))},
    "outra-caixa": {**{path: entry for path, entry in VALID_TREE.items() if path != OVERLAY_DIR + "AGENTS.md"},
                    OVERLAY_DIR + "agents.md": ("100644", _oid("a"))},
    "pasta-inexistente": {path: entry for path, entry in VALID_TREE.items() if not path.startswith(OVERLAY_DIR)},
}


def test_overlay_entries_maps_each_name_to_its_blob() -> None:
    """Na árvore do commit (caminho → (modo, id)), o id do blob de cada um dos três nomes da pasta; os caminhos de
    fora da pasta, inclusive os da raiz e os de uma pasta de nome parecido, não contam. Vermelho até a
    implementação (`NotImplementedError`)."""
    assert export_public.overlay_entries(VALID_TREE) == {
        "AGENTS.md": _oid("a"), "CLAUDE.md": _oid("c"), "ROADMAP.md": _oid("e")}


@pytest.mark.parametrize("case", sorted(INVALID_TREES))
def test_overlay_entries_refuses_an_invalid_overlay(case: str) -> None:
    """Falta, sobra, subpasta, link simbólico, outra caixa ou a pasta inexistente → `OverlayInvalid`. Vermelho até
    a implementação (`NotImplementedError`)."""
    with pytest.raises(export_public.OverlayInvalid):
        export_public.overlay_entries(INVALID_TREES[case])
