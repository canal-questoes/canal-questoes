"""Conferência das referências das questões do piloto,
com um `runner` de `git` falso: nenhuma rede, nenhum `git` de verdade.

O script (`ops/pilot/check_question_refs.py`, carregado por `tests/helpers/ops.load_ops`), por
`check(manifest, *, git, runner)`:
- para cada repositório do manifesto (uma lista JSON de `{repo, commit, qid, field_type, info_json_sha256}`),
  os argv `git clone --filter=blob:none --no-checkout <repo> <tmp>` e, no clone,
  `git fetch --depth 1 origin <commit>` (um `-C <clone>` no lugar do `cwd` também vale);
- a conferência de `questions/<qid>/info.json` no commit e do SHA-256 dos bytes dele;
- referência ausente → `missing_ref` (66) e SHA-256 divergente → `sha256_mismatch` (65), com a mensagem fixa de
  `CHECK_MESSAGES` (real no esqueleto); tudo certo → `ok` (0);
- o temporário apagado em todo caso;
- repositório que não é `https://` → recusa antes de qualquer `git`, com código ≠ 0 e uma mensagem fixa.

Mensagens: só os textos de `CHECK_MESSAGES`, sem caminho, sem o temporário e sem a saída do `git` (o falso põe uma
sentinela no stderr de cada chamada).

**[I] Leitura do arquivo no commit (escolha deste teste):** pelo `git show <commit>:<caminho>` ou
pelo `git cat-file -p|blob <commit>:<caminho>`, no clone; o falso atende aos dois e devolve bytes (ou texto, se o
chamador pedir `text`, `encoding` ou `universal_newlines`). **[I]** O temporário fica sob `tempfile.gettempdir()`, que
o teste aponta para uma pasta do `tmp_path` (`tempfile.tempdir`), conferida vazia no fim.

No esqueleto, `check` lança `NotImplementedError`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.ops import load_ops

SCRIPT = "ops/pilot/check_question_refs.py"
GIT = "git-falso"
REPO_A = "https://repos.cq.test/curso/questoes-piloto.git"
REPO_B = "https://repos.cq.test/outro/questoes-extra.git"
COMMIT_A = "ca01df00085b0172af68253aeba93e76cd7f6ecb"
COMMIT_B = "5eb0a1c2" * 5
GIT_STDERR_SENTINEL = "saida-do-git-sentinela-3C9E"


def _info(qid: str) -> bytes:
    tail = hashlib.sha256(qid.encode("utf-8")).hexdigest()[:12]
    return json.dumps({"uuid": f"00000000-0000-4000-8000-{tail}", "title": qid,
                       "topic": "Piloto", "type": "v3"}, indent=2).encode("utf-8") + b"\n"


# O "remoto" de cada repositório: commit → caminho → bytes.
REMOTES: dict[str, dict[str, dict[str, bytes]]] = {
    REPO_A: {COMMIT_A: {"questions/velocidade/info.json": _info("velocidade"),
                        "questions/contagem/info.json": _info("contagem")}},
    REPO_B: {COMMIT_B: {"questions/cor/info.json": _info("cor")}},
}


def _entry(repo: str, commit: str, qid: str, field_type: str, *, sha: str | None = None) -> dict[str, str]:
    data = REMOTES.get(repo, {}).get(commit, {}).get(f"questions/{qid}/info.json", b"")
    return {"repo": repo, "commit": commit, "qid": qid, "field_type": field_type,
            "info_json_sha256": sha if sha is not None else hashlib.sha256(data).hexdigest()}


GOOD = [
    _entry(REPO_A, COMMIT_A, "velocidade", "pl-number-input"),
    _entry(REPO_A, COMMIT_A, "contagem", "pl-integer-input"),
    _entry(REPO_B, COMMIT_B, "cor", "pl-string-input"),
]


class FakeGit:
    """O `runner` com a forma do `subprocess.run`. Simula o clone (cria a pasta, com `.git`), o `fetch` do commit e
    a leitura de `<commit>:<caminho>`; registra cada chamada (argv normalizado sem o `-C` e o diretório efetivo)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.clones: dict[Path, str] = {}
        self.fetched: dict[Path, set[str]] = {}

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        assert argv[0] == GIT, f"o binário do git não é o dado: {argv}"
        args = argv[1:]
        cwd = Path(kwargs["cwd"]) if kwargs.get("cwd") is not None else None
        if args[:1] == ["-C"]:
            cwd = Path(args[1])
            args = args[2:]
        self.calls.append((args, cwd))
        text = bool(kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding"))
        if args[:1] == ["clone"]:
            repo, target = args[-2], Path(args[-1])
            if repo not in REMOTES:
                return self._done(argv, 128, b"", text)
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(exist_ok=True)
            (target / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            self.clones[target.resolve()] = repo
            return self._done(argv, 0, b"", text)
        clone = self._clone_of(cwd)
        repo = self.clones[clone]
        if args[:1] == ["fetch"]:
            commit = args[-1]
            if commit not in REMOTES[repo]:
                return self._done(argv, 128, b"", text)
            self.fetched.setdefault(clone, set()).add(commit)
            return self._done(argv, 0, b"", text)
        if args[:1] == ["show"] or args[:1] == ["cat-file"]:
            spec = args[-1]
            commit, _, path = spec.partition(":")
            if commit not in self.fetched.get(clone, set()):
                return self._done(argv, 128, b"", text)
            data = REMOTES[repo][commit].get(path)
            if data is None:
                return self._done(argv, 128, b"", text)
            return self._done(argv, 0, data, text)
        raise AssertionError(f"comando do git inesperado: {args}")

    def _clone_of(self, cwd: Path | None) -> Path:
        assert cwd is not None, "comando do git fora do clone (sem cwd nem -C)"
        resolved = cwd.resolve()
        assert resolved in self.clones, f"comando do git fora de um clone: {cwd}"
        return resolved

    @staticmethod
    def _done(argv: list[str], code: int, out: bytes, text: bool) -> subprocess.CompletedProcess:
        err = f"fatal: {GIT_STDERR_SENTINEL}\n" if code else ""
        if text:
            return subprocess.CompletedProcess(argv, code, out.decode("utf-8"), err)
        return subprocess.CompletedProcess(argv, code, out, err.encode("utf-8"))

    def commands(self, verb: str) -> list[tuple[list[str], Path | None]]:
        return [(args, cwd) for args, cwd in self.calls if args[:1] == [verb]]


@pytest.fixture
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "temporarios"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _manifest(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "questoes.lock.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _check(manifest: Path, fake: FakeGit, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = load_ops(SCRIPT).check(manifest, git=GIT, runner=fake)
    out = capsys.readouterr()
    return code, out.out + out.err


def _only_fixed_messages(output: str, expected_key: str, tmp_path: Path) -> None:
    messages = load_ops(SCRIPT).CHECK_MESSAGES
    lines = [line for line in output.splitlines() if line.strip()]
    texts = {text for _, text in messages.values()}
    assert lines, "nenhuma mensagem"
    assert all(line in texts for line in lines), f"mensagem fora da tabela: {lines}"
    assert messages[expected_key][1] in lines, (expected_key, lines)
    assert GIT_STDERR_SENTINEL not in output and str(tmp_path) not in output and tmp_path.as_posix() not in output


def test_all_references_match_with_one_clone_per_repository(
        tmp_path: Path, temp_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """um clone por repositório (`clone --filter=blob:none --no-checkout <repo> <tmp>`), um
    `fetch --depth 1 origin <commit>` no clone, a leitura de cada `info.json`; tudo confere → 0 e a mensagem `ok`;
    os temporários apagados."""
    fake = FakeGit()
    code, output = _check(_manifest(tmp_path, GOOD), fake, capsys)
    assert code == 0
    _only_fixed_messages(output, "ok", tmp_path)

    clones = fake.commands("clone")
    assert sorted(args[-2] for args, _ in clones) == sorted([REPO_A, REPO_B])
    for args, _ in clones:
        assert args[:4] == ["clone", "--filter=blob:none", "--no-checkout", args[-2]] and len(args) == 5, args
    fetches = fake.commands("fetch")
    assert sorted(args for args, _ in fetches) == sorted([["fetch", "--depth", "1", "origin", COMMIT_A],
                                                         ["fetch", "--depth", "1", "origin", COMMIT_B]])
    read = sorted(args[-1] for args, _ in fake.calls if args[:1] in (["show"], ["cat-file"]))
    assert read == sorted([f"{COMMIT_A}:questions/velocidade/info.json", f"{COMMIT_A}:questions/contagem/info.json",
                           f"{COMMIT_B}:questions/cor/info.json"])
    for clone in fake.clones:
        assert not clone.exists(), f"o clone temporário ficou: {clone}"
    assert list(temp_root.iterdir()) == [], "sobrou algo no diretório temporário"


@pytest.mark.parametrize(("broken", "key"), [
    (_entry(REPO_A, COMMIT_A, "inexistente", "pl-number-input", sha="0" * 64), "missing_ref"),
    (_entry(REPO_A, COMMIT_A, "velocidade", "pl-number-input", sha="f" * 64), "sha256_mismatch"),
], ids=["referencia-ausente", "sha256-divergente"])
def test_missing_reference_or_divergent_sha_fails_with_the_fixed_message_and_cleans_up(
        tmp_path: Path, temp_root: Path, capsys: pytest.CaptureFixture[str], broken: dict, key: str) -> None:
    """a questão ausente no commit → `missing_ref` (66); o `info.json` com outro SHA-256 → `sha256_mismatch`
    (65); a mensagem fixa, sem caminho nem saída do `git`; os temporários apagados também na falha."""
    fake = FakeGit()
    code, output = _check(_manifest(tmp_path, [GOOD[2], broken]), fake, capsys)
    messages = load_ops(SCRIPT).CHECK_MESSAGES
    assert code == messages[key][0] != 0
    _only_fixed_messages(output, key, tmp_path)
    assert fake.clones, "o clone não foi feito"
    for clone in fake.clones:
        assert not clone.exists(), f"o clone temporário ficou: {clone}"
    assert list(temp_root.iterdir()) == [], "sobrou algo no diretório temporário"


@pytest.mark.parametrize("repo", [
    "http://repos.cq.test/curso/questoes-piloto.git",
    "git@github.com:pedroraj/cq-demo-questoes.git",
    "file:///C:/questoes",
    "../cq-demo-questoes",
], ids=["http", "ssh", "file", "caminho"])
def test_non_https_repository_is_refused_before_any_git(
        tmp_path: Path, temp_root: Path, capsys: pytest.CaptureFixture[str], repo: str) -> None:
    """um repositório que não é `https://` → código ≠ 0 e uma mensagem fixa da tabela (não a `ok`), sem
    nenhuma chamada ao `git` e sem temporário."""
    fake = FakeGit()
    entries = [GOOD[0], {**GOOD[2], "repo": repo}]
    code = load_ops(SCRIPT).check(_manifest(tmp_path, entries), git=GIT, runner=fake)
    out = capsys.readouterr()
    output = out.out + out.err
    messages = load_ops(SCRIPT).CHECK_MESSAGES
    assert code != 0
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines and all(line in {text for _, text in messages.values()} for line in lines), lines
    assert messages["ok"][1] not in lines
    assert repo not in output
    assert fake.calls == [], "nenhum git antes da recusa"
    assert list(temp_root.iterdir()) == []
