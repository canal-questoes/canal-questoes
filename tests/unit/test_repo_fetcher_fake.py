"""Autoteste da obtenção falsa e paridade com o adaptador real: o leiaute da área de repositórios, a
política dos endereços de repositório e a nova obtenção de uma árvore que sumiu.

A obtenção falsa (`tests/helpers/repos.py`) substitui o `GitRepoFetcher` em todo teste do núcleo. Se ela
errar o leiaute `<repo_key>/<commit>`, o reuso, os desfechos programados, a contagem ou o `delete_tree`,
os testes da validação, da abertura e da nova obtenção passam no vazio. Estes testes conferem
o próprio arnês, inclusive junto do motor falso na mesma raiz.

**Paridade dublê × adaptador real** (nenhum destes testes passa
no esqueleto):
- o leiaute: toda árvore que o dublê deixa na área é a que o `GitRepoFetcher` real (pela regra do leiaute:
  `<repos>/<repo_key>/<commit>/` já existe → `Fetched(reused=True)` sem rede) reusa, com o mesmo valor da
  porta, sem rodar `git` (o binário do adaptador aponta para um caminho que não existe) e sem mexer na
  árvore; sem a árvore, o adaptador não a vê (com a lista de hosts vazia, `FetchRefused("host_not_allowed")`
  antes de qualquer rede); e o dublê não deixa sobra de `.staging` para o `sweep_staging` real;
- a política (paridade exata): o dublê acusa exatamente o que a política real
  (`questionrefs.validate_repo`, com a mesma lista de hosts) recusa, com o mesmo motivo (`repo_length` ·
  `repo_syntax`, inclusive o host que é IP literal · `repo_dot_segment` · `host_not_allowed`), e aceita
  sem acusar o que ela aceita; o adaptador real recusa o mesmo (`FetchRefused("host_not_allowed")` ou
  `FetchRefused("repo_syntax")`, o conjunto fechado de `REFUSED_RULES`).

Vermelho no esqueleto: `GitRepoFetcher.ensure`, `GitRepoFetcher.sweep_staging` e
`questionrefs.validate_repo` lançam `NotImplementedError`; toda função deste arquivo chama
pelo menos um deles. As asserções sobre o próprio arnês ficam na mesma função.
"""

from __future__ import annotations

import hashlib
import pathlib
import threading
from datetime import datetime, timezone

import pytest

from cq_server.adapters.git import GitRepoFetcher
from cq_server.config import RepoSettings
from cq_server.ports.repos import (
    REFUSED_RULES,
    Fetched,
    FetchNotFound,
    FetchRefused,
    FetchTemporaryFailure,
    RepoFetcher,
)
from cq_server.questionrefs import validate_repo
from tests.helpers.engine import FakeEngine, preset
from tests.helpers.repos import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_TREE,
    FIXTURES,
    FakeRepoFetcher,
    FetchCall,
    policy_problem,
    tree_source,
)

REPO = "https://repos.cq.test/curso-teste.git"
OTHER_REPO = "https://repos.cq.test/outro-curso.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMMIT_256 = "ab" * 32
HOSTS = frozenset({"repos.cq.test"})
REUSED = Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)

# A política real, caso a caso (o código de `validate_repo` com HOSTS).
POLICY_CASES: list[tuple[str, str | None]] = [
    (REPO, None),
    (OTHER_REPO, None),
    ("https://repos.cq.test/curso/", None),
    ("http://repos.cq.test/x", "repo_syntax"),
    ("https://user@repos.cq.test/x.git", "repo_syntax"),
    ("https://repos.cq.test:8443/x.git", "repo_syntax"),
    ("https://REPOS.cq.test/x.git", "repo_syntax"),
    ("https://repos.cq.test/x.git?a=1", "repo_syntax"),
    ("https://repos.cq.test/x.git#f", "repo_syntax"),
    ("https://repos.cq.test/x%41.git", "repo_syntax"),
    ("https://repos.cq.test", "repo_syntax"),
    ("https://127.0.0.1/x.git", "repo_syntax"),
    ("https://1.2.3.4/x.git", "repo_syntax"),
    ("https://outro.cq.test/x.git", "host_not_allowed"),
    ("https://sub.repos.cq.test/x.git", "host_not_allowed"),
    ("https://repos.cq.test.evil.example/x.git", "host_not_allowed"),
    ("https://repos.cq.test/a/../x.git", "repo_dot_segment"),
    ("https://repos.cq.test/./x.git", "repo_dot_segment"),
    ("https://repos.cq.test/a/..", "repo_dot_segment"),
    ("https://repos.cq.test/a/.x/..b/...", None),
    ("https://repos.cq.test/" + "a" * (2048 - len("https://repos.cq.test/")), None),
    ("https://repos.cq.test/" + "a" * (2049 - len("https://repos.cq.test/")), "repo_length"),
    ("https://repos.cq.test/" + "a" * 2100, "repo_length"),
]


def _policy_message(code: str) -> str:
    return f"ensure: repo recusado pela política (questionrefs.validate_repo: {code})"


def _key(repo: str) -> str:
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()


def _files(root: pathlib.Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _real(root: pathlib.Path, *, hosts: frozenset[str] = HOSTS) -> GitRepoFetcher:
    """O adaptador real na mesma área, com um `git` que não existe: a paridade nunca roda `git`."""
    return GitRepoFetcher(RepoSettings(allowed_hosts=hosts, repos_root=str(root)),
                          git_binary=str(root.parent / "sem-git-na-paridade" / "git"))


def _assert_real_reuses(root: pathlib.Path, repo: str, commit: str) -> None:
    """O adaptador real reusa a árvore que o dublê deixou (o mesmo valor da porta), sem mexer nela."""
    tree = root / _key(repo) / commit
    before = _files(tree)
    assert before, f"o dublê não deixou árvore em {tree}"
    assert _real(root).ensure(repo, commit) == REUSED
    assert _files(tree) == before


def test_copies_the_tree_into_the_engine_layout_atomically(tmp_path: pathlib.Path) -> None:
    """O curso de teste (uma questão por preset do motor falso, no leiaute do PL) é copiado inteiro para
    `<repo_key>/<commit>`, e o adaptador real o reusa; nada sobra no `.staging` para o `sweep_staging`."""
    from tests.helpers.engine import PRESETS

    course = FIXTURES / DEFAULT_TREE
    assert (course / "infoCourse.json").is_file()
    assert sorted(p.name for p in (course / "questions").iterdir()) == sorted(PRESETS)
    for name in PRESETS:
        assert (course / "questions" / name / "info.json").is_file()
        assert (course / "questions" / name / "question.html").is_file()
    assert tree_source(DEFAULT_TREE) == course
    with pytest.raises(ValueError):
        tree_source("inexistente")

    fetcher = FakeRepoFetcher(tmp_path)
    port: RepoFetcher = fetcher  # cumpre a porta real
    result = port.ensure(REPO, COMMIT)
    assert isinstance(result, Fetched)
    final = tmp_path / _key(REPO) / COMMIT
    source = FIXTURES / DEFAULT_TREE
    assert fetcher.tree_path(REPO, COMMIT) == final
    assert _files(final) == _files(source)
    assert result == Fetched(tree_oid=result.tree_oid, file_count=len(_files(source)),
                             byte_count=sum(len(b) for b in _files(source).values()), reused=False)
    assert isinstance(result.tree_oid, str) and len(result.tree_oid) == 40
    assert len(fetcher.ensure(REPO, COMMIT_256).tree_oid or "") == 64
    assert list((tmp_path / ".staging").iterdir()) == []
    assert fetcher.protocol_errors() == []
    _assert_real_reuses(tmp_path, REPO, COMMIT)
    _assert_real_reuses(tmp_path, REPO, COMMIT_256)
    assert _real(tmp_path).sweep_staging(datetime.now(timezone.utc)) == 0
    assert list((tmp_path / ".staging").iterdir()) == []


def test_reuse_returns_fetched_reused_without_copying(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    fetcher.ensure(REPO, COMMIT)
    marker = tmp_path / _key(REPO) / COMMIT / "marcador.txt"
    marker.write_text("fica", encoding="utf-8")
    assert fetcher.ensure(REPO, COMMIT) == Fetched(tree_oid=None, file_count=None, byte_count=None,
                                                   reused=True)
    assert marker.read_text(encoding="utf-8") == "fica"
    assert [(c.outcome, c.reused, c.programmed) for c in fetcher.calls()] == [
        ("Fetched", False, False), ("Fetched", True, False)]
    # Paridade: o reuso do dublê é o valor que o adaptador real devolve na mesma área.
    assert _real(tmp_path).ensure(REPO, COMMIT) == fetcher.ensure(REPO, COMMIT) == REUSED
    assert marker.read_text(encoding="utf-8") == "fica"


def test_add_maps_a_pair_to_another_tree_and_no_default(tmp_path: pathlib.Path) -> None:
    tree = tmp_path / "arvore"
    (tree / "questions" / "q1").mkdir(parents=True)
    (tree / "questions" / "q1" / "info.json").write_text("{}", encoding="utf-8")
    fetcher = FakeRepoFetcher(tmp_path / "area", default_tree=None)
    fetcher.add(OTHER_REPO, COMMIT, tree)
    assert fetcher.ensure(OTHER_REPO, COMMIT).reused is False
    assert _files(tmp_path / "area" / _key(OTHER_REPO) / COMMIT) == {"questions/q1/info.json": b"{}"}
    assert fetcher.ensure(REPO, COMMIT) == FetchNotFound("repository")
    assert not (tmp_path / "area" / _key(REPO)).exists()
    # Paridade: a árvore escolhida por `add` é reusada pelo adaptador real; o par sem árvore, ele não vê
    # (lista de hosts vazia: recusa antes de qualquer rede, sem criar nada na área).
    _assert_real_reuses(tmp_path / "area", OTHER_REPO, COMMIT)
    assert _real(tmp_path / "area", hosts=frozenset()).ensure(REPO, COMMIT) == FetchRefused("host_not_allowed")
    assert not (tmp_path / "area" / _key(REPO)).exists()


@pytest.mark.parametrize("result", [FetchRefused("host_not_allowed"), FetchNotFound("commit"),
                                    FetchTemporaryFailure("timeout"),
                                    Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)])
def test_programmed_outcomes_until_cleared_or_times(tmp_path: pathlib.Path, result: object) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    fetcher.program(REPO, COMMIT, result)
    assert fetcher.ensure(REPO, COMMIT) == result
    assert fetcher.ensure(REPO, COMMIT) == result
    assert fetcher.ensure(OTHER_REPO, COMMIT).reused is False
    assert not (tmp_path / _key(REPO)).exists()
    fetcher.program(REPO, COMMIT, None)
    assert fetcher.ensure(REPO, COMMIT).reused is False
    fetcher.delete_tree(REPO, COMMIT)
    fetcher.program(REPO, COMMIT, result, times=1)
    assert fetcher.ensure(REPO, COMMIT) == result
    assert fetcher.ensure(REPO, COMMIT).reused is False
    assert [c.programmed for c in fetcher.calls(REPO, COMMIT)] == [True, True, False, True, False]
    # Paridade: o `Fetched(reused=True)` programável é o que o adaptador real devolve na árvore do dublê.
    _assert_real_reuses(tmp_path, REPO, COMMIT)
    _assert_real_reuses(tmp_path, OTHER_REPO, COMMIT)


def test_programmed_exception_is_raised_and_counted(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    fetcher.program(REPO, COMMIT, OSError("disco"), times=1)
    with pytest.raises(OSError):
        fetcher.ensure(REPO, COMMIT)
    assert fetcher.ensure(REPO, COMMIT).reused is False
    assert [c.outcome for c in fetcher.calls()] == ["raised:OSError", "Fetched"]
    _assert_real_reuses(tmp_path, REPO, COMMIT)  # a exceção programada não deixa árvore pela metade
    with pytest.raises(TypeError):
        fetcher.program(REPO, COMMIT, "recusado")
    with pytest.raises(ValueError):
        fetcher.program(REPO, COMMIT, FetchNotFound("commit"), times=0)


def test_count_per_pair_and_calls(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    for _ in range(3):
        fetcher.ensure(REPO, COMMIT)
    fetcher.ensure(OTHER_REPO, COMMIT)
    assert (fetcher.count(REPO, COMMIT), fetcher.count(OTHER_REPO, COMMIT), fetcher.count(REPO, "f" * 40)) == (
        3, 1, 0)
    assert [c.seq for c in fetcher.calls()] == [1, 2, 3, 4]
    assert fetcher.calls(OTHER_REPO) == [FetchCall(4, OTHER_REPO, COMMIT, "Fetched", False, False)]
    # Paridade: o adaptador real reusa as duas árvores; o que ele faz não entra na contagem do dublê.
    _assert_real_reuses(tmp_path, REPO, COMMIT)
    _assert_real_reuses(tmp_path, OTHER_REPO, COMMIT)
    assert (fetcher.count(REPO, COMMIT), fetcher.count(OTHER_REPO, COMMIT)) == (3, 1)


def test_delete_tree_and_refetch_with_the_fake_engine(tmp_path: pathlib.Path) -> None:
    """Perda do conteúdo em disco de ponta a ponta: o motor falso lê a mesma raiz; sem a árvore, 404; a nova obtenção a
    recria."""
    fetcher = FakeRepoFetcher(tmp_path)
    engine = FakeEngine(repos_root=tmp_path)
    try:
        engine.program("number", preset("number"))
        ref = {"question_ref": {"repo": REPO, "commit": COMMIT, "qid": "number"}}
        assert engine.request("inspect", ref).status == 404
        fetcher.ensure(REPO, COMMIT)
        assert engine.request("inspect", ref).status == 200
        _assert_real_reuses(tmp_path, REPO, COMMIT)
        assert fetcher.delete_tree(REPO, COMMIT) is True
        assert fetcher.delete_tree(REPO, COMMIT) is False
        assert engine.request("inspect", ref).status == 404
        # Paridade: sem a árvore, o adaptador real também não a vê (recusa pela lista vazia, sem rede).
        assert _real(tmp_path, hosts=frozenset()).ensure(REPO, COMMIT) == FetchRefused("host_not_allowed")
        assert fetcher.ensure(REPO, COMMIT).reused is False
        assert engine.request("inspect", ref).status == 200
        _assert_real_reuses(tmp_path, REPO, COMMIT)
        assert fetcher.count(REPO, COMMIT) == 2
        assert engine.protocol_errors() == []
    finally:
        engine.close()


def test_invalid_repo_or_commit_is_a_protocol_error(tmp_path: pathlib.Path) -> None:
    """O dublê acusa `repo`/`commit` fora da forma validada. Paridade exata com a política real:
    o dublê acusa exatamente os `repo` que
    `validate_repo` recusa com a mesma lista de hosts, com o mesmo motivo (`repo_length`, `repo_syntax`,
    inclusive o host IP literal, `repo_dot_segment` e `host_not_allowed`), e aceita sem acusar todo
    `repo` que a política aceita; o adaptador real recusa todo
    `repo` que a política recusa (`host_not_allowed` para o host fora da lista, `repo_syntax` para o resto,
    no conjunto fechado de `REFUSED_RULES`), sem rodar `git` e sem criar nada na área."""
    fetcher = FakeRepoFetcher(tmp_path)
    fetcher.program("http://repos.cq.test/x", "ABC", FetchRefused("repo_syntax"))
    assert fetcher.ensure("http://repos.cq.test/x", "ABC") == FetchRefused("repo_syntax")
    assert fetcher.protocol_errors() == [
        _policy_message("repo_syntax"),
        "ensure: commit fora da forma validada (questionrefs.COMMIT)",
    ]
    assert DEFAULT_ALLOWED_HOSTS == HOSTS  # o mesmo do TEST_REPO_SETTINGS

    area = tmp_path / "politica"
    for repo, code in POLICY_CASES:
        assert validate_repo(repo, HOSTS) == code, repo[:60]
        assert policy_problem(repo, HOSTS) == code, repo[:60]
        probe = FakeRepoFetcher(tmp_path / "dubles", default_tree=None)
        probe.program(repo, COMMIT, FetchNotFound("repository"))
        probe.ensure(repo, COMMIT)
        assert probe.protocol_errors() == ([] if code is None else [_policy_message(code)]), repo[:60]
        if code is not None:
            refused = _real(area).ensure(repo, COMMIT)
            expected = "host_not_allowed" if code == "host_not_allowed" else "repo_syntax"
            assert refused == FetchRefused(expected) and refused.rule in REFUSED_RULES, repo[:60]
    assert not area.exists() or list(area.iterdir()) == []
    assert validate_repo(REPO, frozenset()) == "host_not_allowed"  # a lista vazia recusa tudo

    # A lista de hosts do dublê é a da política: vazia recusa tudo; outra lista muda o motivo (controle
    # positivo); o host IP literal é `repo_syntax` mesmo com o IP na lista (nenhuma lista válida o tem).
    ip_repo = "https://127.0.0.1/x.git"
    cases = [(frozenset(), REPO, "host_not_allowed"), (frozenset({"outro.cq.test"}), REPO, "host_not_allowed"),
             (frozenset({"outro.cq.test"}), "https://outro.cq.test/x.git", None),
             (HOSTS | {"127.0.0.1"}, ip_repo, "repo_syntax")]
    for hosts, repo, code in cases:
        assert validate_repo(repo, hosts) == code, (sorted(hosts), repo)
        probe = FakeRepoFetcher(tmp_path / "dubles", default_tree=None, allowed_hosts=hosts)
        probe.program(repo, COMMIT, FetchNotFound("repository"))
        probe.ensure(repo, COMMIT)
        assert probe.protocol_errors() == ([] if code is None else [_policy_message(code)]), (sorted(hosts), repo)
        probe.reset(allowed_hosts=HOSTS)
        assert probe.allowed_hosts == HOSTS
        probe.program(repo, COMMIT, FetchNotFound("repository"))
        probe.ensure(repo, COMMIT)
        expected = policy_problem(repo, HOSTS)
        assert probe.protocol_errors() == ([] if expected is None else [_policy_message(expected)])
    fetcher.reset()
    assert fetcher.allowed_hosts == HOSTS  # o `reset` sem lista mantém a lista


def test_concurrent_ensure_copies_once(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(fetcher.ensure(REPO, COMMIT))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(getattr(r, "reused") for r in results) == [False, True, True, True]
    # Paridade: o adaptador real, em quatro threads, reusa a árvore única do dublê.
    real = _real(tmp_path)
    reals: list[object] = []
    errors: list[BaseException] = []

    def ensure_real() -> None:
        try:
            reals.append(real.ensure(REPO, COMMIT))
        except BaseException as exc:  # noqa: BLE001 - relançada abaixo, na thread do teste
            errors.append(exc)

    threads = [threading.Thread(target=ensure_real) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    if errors:
        raise errors[0]
    assert reals == [REUSED] * 4


def test_reset_forgets_programs_and_calls_and_can_switch_root(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path / "a")
    fetcher.program(REPO, COMMIT, FetchNotFound("commit"))
    fetcher.ensure(REPO, COMMIT)
    fetcher.on_ensure(lambda repo, commit: None)
    fetcher.reset(tmp_path / "b")
    assert (fetcher.calls(), fetcher.protocol_errors(), fetcher.root) == ([], [], tmp_path / "b")
    assert fetcher.ensure(REPO, COMMIT).reused is False
    assert (tmp_path / "b" / _key(REPO) / COMMIT / "infoCourse.json").is_file()
    # Paridade: a raiz nova é a do adaptador real; na antiga, sem árvore, ele não reusa nada.
    _assert_real_reuses(tmp_path / "b", REPO, COMMIT)
    assert _real(tmp_path / "a", hosts=frozenset()).ensure(REPO, COMMIT) == FetchRefused("host_not_allowed")


def test_on_ensure_hook_runs_before_each_call(tmp_path: pathlib.Path) -> None:
    fetcher = FakeRepoFetcher(tmp_path)
    seen: list[tuple[str, str]] = []
    fetcher.on_ensure(lambda repo, commit: seen.append((repo, commit)))
    fetcher.ensure(REPO, COMMIT)
    fetcher.on_ensure(None)
    fetcher.ensure(REPO, COMMIT)
    assert seen == [(REPO, COMMIT)]
    _assert_real_reuses(tmp_path, REPO, COMMIT)
    assert seen == [(REPO, COMMIT)]  # o adaptador real não passa pelo gancho do dublê
