"""A publicação de um banco enviado no worker: o roteador da obtenção (`cq_server.banks.RoutingRepoFetcher`), a
montagem dele no `run_worker`, a montagem do adaptador no `__main__` (`build_bank_fetcher`) e o adaptador
(`cq_server.adapters.banks.BankRepoFetcher`), que cumpre a porta de obtenção a partir do pacote guardado, sem rede
e sem `git`.

O que se confere aqui:
- o roteador manda as referências `bank:` ao lado do banco e as demais ao do git, com o mesmo desfecho que o lado
  devolve; o lado escolhido ausente (`None`) → `FetchTemporaryFailure("repos_unavailable")`, sem chamar o outro;
- o `run_worker` sem `bank_fetcher` entrega aos tratadores da validação e da nova obtenção o `fetcher` recebido, o
  mesmo objeto (sem roteador); com os dois, um roteador com o `fetcher` no lado do git e o `bank_fetcher` no do
  banco;
- `build_bank_fetcher`: com a raiz utilizável, o adaptador com os tetos do ambiente, sem conectar ao PostgreSQL;
  sem raiz utilizável, `None`, sem registro próprio;
- o adaptador:
  - a árvore já publicada é reusada sem carregar o pacote (`Fetched(reused=True)`);
  - a publicação em `<raiz>/<sha256 de "bank:<nome>">/<versão>/`, com os mesmos bytes, arquivos 0644 e diretórios
    0755 (fora do Windows), cada arquivo criado com abertura exclusiva (`O_CREAT | O_EXCL`, e `O_NOFOLLOW` onde
    existe) e a área `.staging` vazia no fim; o carregador recebe o nome sem o prefixo e a versão;
  - dois `ensure` em corrida: um publica, o outro reusa, e a árvore fica inteira; o destino que aparece durante a
    publicação vale como reuso;
  - nome desconhecido → `FetchNotFound("repository")`, versão desconhecida → `FetchNotFound("commit")`;
  - a falha do carregador → falha temporária com o nome do tipo da exceção;
  - pacote corrompido (não gzip, truncado no meio de um membro, um link) → `FetchRefused("path_invalid")`;
  - os tetos menores do worker → `FetchRefused("max_files")` e `FetchRefused("max_bytes")`;
  - a versão recalculada que não confere com a pedida → `FetchRefused("path_invalid")`;
  - depois de toda recusa, a área `.staging` vazia e nenhum destino publicado;
- nada do banco sai pelos registros nem pelo desfecho: um marcador único, montado na hora, no conteúdo de um
  arquivo e no nome de outro (nomes aceitos pelas regras de caminho) não aparece em nenhum registro capturado (com o
  formatador real) nem no desfecho, na publicação, nas recusas (o pacote corrompido, os tetos, a versão que não
  confere) e na falha do carregador (uma exceção com o marcador no texto e no nome de arquivo); sob a raiz, ele só
  existe na árvore publicada.

A falha de escrita de um arquivo do banco no volume não tem caso aqui ([I]): as regras de caminho aceitam nomes que
só o Windows recusa (`:`, `CON`, dois nomes que diferem só na caixa), o teto do caminho longo depende da forma de
abrir (relativa a um diretório aberto ou pelo caminho inteiro) e da configuração do Windows, e um diretório só de
leitura não impede a criação no Windows nem para o superusuário; o resto exigiria trocar uma chamada interna, e o
desfecho dessa falha não está fixado.

No esqueleto, `RoutingRepoFetcher.ensure`, `BankRepoFetcher.ensure` e `build_bank_fetcher` lançam
`NotImplementedError`, e o `run_worker` ainda entrega o `fetcher` mesmo com o `bank_fetcher`: todo caso falha.
"""

from __future__ import annotations

import collections
import hashlib
import io
import os
import pathlib
import stat
import threading
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from tests.helpers.banks import (archive_of, course_files, make_archive, members_of, reference_version, regular,
                                 unique_marker)
from tests.helpers.logs import capture_logs

BANK_NAME = "banco-sintetico"
BANK_REPO = f"bank:{BANK_NAME}"
HTTPS_REPO = "https://repos.cq.test/curso/banco.git"
HEX40 = "0123456789abcdef0123456789abcdef01234567"


class _Recorder:
    """Um lado da obtenção que só cumpre a porta: registra cada pedido e devolve o desfecho dado."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    def ensure(self, repo: str, commit: str) -> Any:
        self.calls.append((repo, commit))
        return self.outcome


class _Loader:
    """O carregador do pacote guardado (`load(nome, versão)`), com o desfecho dado, que registra os pedidos."""

    def __init__(self, outcome: Any, *, before: Callable[[], None] | None = None) -> None:
        self.outcome = outcome
        self.before = before
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, name: str, version: str) -> Any:
        with self.lock:
            self.calls.append((name, version))
        if self.before is not None:
            self.before()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _settings(root: pathlib.Path, **limits: int) -> Any:
    from cq_server.config import RepoSettings

    return RepoSettings(repos_root=str(root), **limits)


def _fetcher(root: pathlib.Path, load: Callable[[str, str], Any], **limits: int) -> Any:
    from cq_server.adapters.banks import BankRepoFetcher

    return BankRepoFetcher(_settings(root, **limits), load)


def _destination(root: pathlib.Path, version: str, repo: str = BANK_REPO) -> pathlib.Path:
    return root / hashlib.sha256(repo.encode("utf-8")).hexdigest() / version


def _tree(directory: pathlib.Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _assert_no_staging_left(root: pathlib.Path) -> None:
    staging = root / ".staging"
    if staging.exists():
        assert list(staging.iterdir()) == [], "sobra na área de preparo"


def _assert_refused_clean(root: pathlib.Path, result: Any, rule: str, version: str) -> None:
    from cq_server.ports.repos import FetchRefused

    assert result == FetchRefused(rule)
    assert not _destination(root, version).exists()
    _assert_no_staging_left(root)


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "repos"
    directory.mkdir()
    return directory


@pytest.fixture
def files() -> dict[str, bytes]:
    return course_files()


# --------------------------------------------------------------------------------------------------
# O roteador
# --------------------------------------------------------------------------------------------------


def test_router_sends_bank_references_to_the_bank_side_and_the_rest_to_git() -> None:
    """O roteador: `bank:<nome>` vai ao lado do banco e a `https` ao do git, cada um com o par recebido e o
    desfecho do lado devolvido tal qual; o outro lado não é chamado."""
    from cq_server.banks import RoutingRepoFetcher
    from cq_server.ports.repos import Fetched, FetchNotFound

    version = "ab" * 32
    git = _Recorder(Fetched(tree_oid=HEX40, file_count=3, byte_count=10, reused=False))
    bank = _Recorder(FetchNotFound("commit"))
    router = RoutingRepoFetcher(git=git, bank=bank)
    assert router.ensure(BANK_REPO, version) == FetchNotFound("commit")
    assert router.ensure(HTTPS_REPO, HEX40) == Fetched(tree_oid=HEX40, file_count=3, byte_count=10, reused=False)
    assert router.ensure("bank:abc", version) == FetchNotFound("commit")
    assert bank.calls == [(BANK_REPO, version), ("bank:abc", version)]
    assert git.calls == [(HTTPS_REPO, HEX40)]


def test_router_with_a_missing_side_is_repos_unavailable() -> None:
    """Com `git=None`, uma `https` → `FetchTemporaryFailure("repos_unavailable")` e a `bank:` segue ao banco; com
    `bank=None`, uma `bank:` → a mesma falha e a `https` segue ao git; com os dois `None`, as duas falham."""
    from cq_server.banks import RoutingRepoFetcher
    from cq_server.ports.repos import Fetched, FetchTemporaryFailure

    version = "cd" * 32
    unavailable = FetchTemporaryFailure("repos_unavailable")
    done = Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)

    bank = _Recorder(done)
    only_bank = RoutingRepoFetcher(git=None, bank=bank)
    assert only_bank.ensure(HTTPS_REPO, HEX40) == unavailable
    assert only_bank.ensure(BANK_REPO, version) == done
    assert bank.calls == [(BANK_REPO, version)]

    git = _Recorder(done)
    only_git = RoutingRepoFetcher(git=git, bank=None)
    assert only_git.ensure(BANK_REPO, version) == unavailable
    assert only_git.ensure(HTTPS_REPO, HEX40) == done
    assert git.calls == [(HTTPS_REPO, HEX40)]

    neither = RoutingRepoFetcher(git=None, bank=None)
    assert neither.ensure(BANK_REPO, version) == unavailable
    assert neither.ensure(HTTPS_REPO, HEX40) == unavailable


def _fetchers_given_to_handlers(monkeypatch: pytest.MonkeyPatch, settings: Any, **kwargs: Any) -> dict[str, Any]:
    """A obtenção que o `run_worker(settings, …, console_routes=True, **kwargs)` entrega aos tratadores da validação
    e da nova obtenção: as duas fábricas são envolvidas (registram o `fetcher` e montam o tratador de verdade), e a
    primeira volta da fila de tarefas pede a parada, sem executar tarefa nenhuma."""
    from cq_server import dispatches, grading, worker
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.tasks import TaskQueue

    given: dict[str, Any] = {}
    stop = threading.Event()

    def wrap(module: Any, name: str) -> None:
        original = getattr(module, name)

        def factory(*args: Any, **inner: Any) -> Any:
            given[name] = inner["fetcher"]
            return original(*args, **inner)

        monkeypatch.setattr(module, name, factory)

    wrap(dispatches, "validate_task_handler")
    wrap(grading, "refetch_task_handler")

    def work_once(self: Any, worker_id: str, handlers: Mapping[str, Any]) -> None:
        stop.set()
        return None

    monkeypatch.setattr(worker, "_wait_for_schema", lambda settings, stop: True)
    monkeypatch.setattr(TaskQueue, "work_once", work_once)
    failure: list[BaseException] = []

    def target() -> None:
        try:
            worker.run_worker(settings, stop=stop, channel=ConsoleChannel(io.StringIO()), console_routes=True,
                              **kwargs)
        except BaseException as exc:  # noqa: BLE001 - relançada na thread do teste
            failure.append(exc)

    thread = threading.Thread(target=target, name="cq-test-bank-worker")
    thread.start()
    thread.join(30)
    stop.set()
    thread.join(10)
    assert not thread.is_alive(), "o run_worker não parou"
    if failure:
        raise failure[0]
    assert set(given) == {"validate_task_handler", "refetch_task_handler"}
    return given


def test_run_worker_builds_the_router_only_with_a_bank_fetcher(monkeypatch: pytest.MonkeyPatch,
                                                               settings: Any) -> None:
    """Sem `bank_fetcher`, os tratadores da validação e da nova obtenção recebem o `fetcher` recebido, o mesmo
    objeto (sem roteador), e `None` sem nenhum dos dois; com os dois, recebem um único roteador, com o `fetcher` no
    lado do git e o `bank_fetcher` no do banco; só com o `bank_fetcher`, o roteador com o lado do git ausente."""
    from cq_server.banks import RoutingRepoFetcher

    git = _Recorder(None)
    bank = _Recorder(None)

    alone = _fetchers_given_to_handlers(monkeypatch, settings, fetcher=git)
    assert all(value is git for value in alone.values())
    nothing = _fetchers_given_to_handlers(monkeypatch, settings)
    assert all(value is None for value in nothing.values())

    both = _fetchers_given_to_handlers(monkeypatch, settings, fetcher=git, bank_fetcher=bank)
    router = both["validate_task_handler"]
    assert isinstance(router, RoutingRepoFetcher)
    assert both["refetch_task_handler"] is router
    assert router.git is git and router.bank is bank
    assert git.calls == [] and bank.calls == []

    bank_only = _fetchers_given_to_handlers(monkeypatch, settings, bank_fetcher=bank)
    assert isinstance(bank_only["validate_task_handler"], RoutingRepoFetcher)
    assert bank_only["validate_task_handler"].git is None and bank_only["validate_task_handler"].bank is bank


def test_build_bank_fetcher_uses_the_repos_root_and_ceilings(tmp_path: pathlib.Path) -> None:
    """`build_bank_fetcher`: com a raiz absoluta e existente, o `BankRepoFetcher` com a raiz e os tetos do ambiente
    (`CQ_REPO_MAX_FILES`, `CQ_REPO_MAX_BYTES`), sem conectar ao PostgreSQL na montagem (a URL aponta para um host
    que não existe) e sem registro; sem a raiz, ou com uma raiz que não existe ou é arquivo, `None`, sem registro
    próprio."""
    from cq_server.__main__ import build_bank_fetcher
    from cq_server.adapters.banks import BankRepoFetcher
    from cq_server.config import Settings

    settings = Settings(database_url="postgresql://cq@cq-banco.invalid:9/cq", lease_seconds=60)
    root = tmp_path / "repos"
    root.mkdir()
    not_dir = tmp_path / "arquivo"
    not_dir.write_bytes(b"")
    with capture_logs() as cap:
        built = build_bank_fetcher({"CQ_REPOS_ROOT": str(root), "CQ_REPO_MAX_FILES": "7",
                                    "CQ_REPO_MAX_BYTES": "1234"}, settings)
        assert type(built) is BankRepoFetcher
        assert built.settings.repos_root == str(root)
        assert (built.settings.max_files, built.settings.max_bytes) == (7, 1234)
        assert callable(built.load)
        for env in ({}, {"CQ_REPOS_ROOT": ""}, {"CQ_REPOS_ROOT": str(tmp_path / "nao-existe")},
                    {"CQ_REPOS_ROOT": str(not_dir)}):
            assert build_bank_fetcher(env, settings) is None, env
    assert cap.records == []


# --------------------------------------------------------------------------------------------------
# O adaptador
# --------------------------------------------------------------------------------------------------


def test_existing_tree_is_reused_without_loading(root: pathlib.Path, files: dict[str, bytes]) -> None:
    """A árvore já publicada em `<raiz>/<sha256 de "bank:<nome>">/<versão>/` é reusada sem carregar o pacote:
    `Fetched(reused=True)` com os três números `None`, o carregador não é chamado e a árvore não muda."""
    from cq_server.ports.repos import Fetched

    version = reference_version(files)
    destination = _destination(root, version)
    destination.mkdir(parents=True)
    (destination / "infoCourse.json").write_bytes(b"{}\n")
    load = _Loader(AssertionError("o pacote foi carregado"))
    result = _fetcher(root, load).ensure(BANK_REPO, version)
    assert result == Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
    assert load.calls == []
    assert _tree(destination) == {"infoCourse.json": b"{}\n"}


def test_publish_writes_the_tree_with_modes_and_exclusive_creation(root: pathlib.Path, files: dict[str, bytes],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A publicação: o carregador recebe o nome sem o prefixo e a versão; a árvore aparece em
    `<raiz>/<sha256 de "bank:<nome>">/<versão>/` com os mesmos bytes do pacote; o desfecho é `Fetched(reused=False)`
    (com as contagens certas, quando vêm); fora do Windows, arquivos 0644 e diretórios 0755; cada arquivo foi criado
    com `O_CREAT | O_EXCL` (e `O_NOFOLLOW` onde existe); a área `.staging` fica vazia; um segundo `ensure` reusa
    sem carregar de novo."""
    from cq_server.ports.repos import Fetched

    version = reference_version(files)
    load = _Loader(archive_of(files))
    opened: list[tuple[str, int]] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append((os.fsdecode(path), flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    fetcher = _fetcher(root, load)
    result = fetcher.ensure(BANK_REPO, version)
    monkeypatch.setattr(os, "open", real_open)

    assert isinstance(result, Fetched) and result.reused is False
    assert result.file_count in (None, len(files))
    assert result.byte_count in (None, sum(len(data) for data in files.values()))
    assert load.calls == [(BANK_NAME, version)]
    destination = _destination(root, version)
    assert _tree(destination) == files
    _assert_no_staging_left(root)

    exclusive = os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = collections.Counter(pathlib.PurePath(path).name for path, flags in opened
                                  if flags & exclusive == exclusive)
    wanted = collections.Counter(path.rsplit("/", 1)[-1] for path in files)
    assert all(created[name] >= count for name, count in wanted.items()), (created, wanted)

    if os.name != "nt":
        for path in destination.rglob("*"):
            mode = stat.S_IMODE(path.lstat().st_mode)
            assert not path.is_symlink()
            assert mode == (0o755 if path.is_dir() else 0o644), (path, oct(mode))
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755

    again = fetcher.ensure(BANK_REPO, version)
    assert again == Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
    assert load.calls == [(BANK_NAME, version)]


def test_two_racing_ensures_publish_once_and_reuse_once(root: pathlib.Path, files: dict[str, bytes]) -> None:
    """Dois `ensure` em corrida (os dois passam pela conferência do reuso e carregam o pacote antes de qualquer
    publicação): um publica (`reused=False`), o outro reusa (`reused=True`), a árvore fica inteira e a área
    `.staging` vazia."""
    from cq_server.ports.repos import Fetched

    version = reference_version(files)
    barrier = threading.Barrier(2)

    def both_loaded() -> None:
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass

    load = _Loader(archive_of(files), before=both_loaded)
    fetcher = _fetcher(root, load)
    results: list[Any] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(fetcher.ensure(BANK_REPO, version))
        except BaseException as exc:  # noqa: BLE001 - relançada na thread do teste
            failures.append(exc)

    threads = [threading.Thread(target=run, name=f"cq-test-bank-race-{index}") for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    if failures:
        raise failures[0]
    assert all(isinstance(result, Fetched) for result in results)
    assert sorted(result.reused for result in results) == [False, True]
    assert _tree(_destination(root, version)) == files
    _assert_no_staging_left(root)


def test_destination_that_appears_during_publication_counts_as_reuse(root: pathlib.Path,
                                                                     files: dict[str, bytes]) -> None:
    """O destino que aparece durante a publicação (outro worker publicou entre a conferência do reuso e a troca
    atômica de nome): `Fetched(reused=True)`, a árvore do outro intacta e a área `.staging` vazia."""
    from cq_server.ports.repos import Fetched

    version = reference_version(files)
    destination = _destination(root, version)

    def publish_elsewhere() -> None:
        destination.mkdir(parents=True)
        for path, data in files.items():
            target = destination.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    load = _Loader(archive_of(files), before=publish_elsewhere)
    result = _fetcher(root, load).ensure(BANK_REPO, version)
    assert isinstance(result, Fetched) and result.reused is True
    assert _tree(destination) == files
    _assert_no_staging_left(root)


def test_unknown_name_or_version_is_not_found(root: pathlib.Path) -> None:
    """O carregador sem o banco → `FetchNotFound("repository")`; sem a versão → `FetchNotFound("commit")`; nada é
    publicado e a área `.staging` fica vazia."""
    from cq_server.banks import BankMissing
    from cq_server.ports.repos import FetchNotFound

    version = "ef" * 32
    for what in ("repository", "commit"):
        load = _Loader(BankMissing(what))
        assert _fetcher(root, load).ensure(BANK_REPO, version) == FetchNotFound(what)
        assert load.calls == [(BANK_NAME, version)]
        assert not _destination(root, version).exists()
        _assert_no_staging_left(root)


def test_loader_failure_is_temporary_with_the_exception_type(root: pathlib.Path) -> None:
    """A falha do carregador (o PostgreSQL fora do ar) → `FetchTemporaryFailure` com o nome do tipo da exceção
    (`OperationalError`), sem o texto dela; nada é publicado."""
    import psycopg

    from cq_server.ports.repos import FetchTemporaryFailure

    version = "01" * 32
    load = _Loader(psycopg.OperationalError("servidor fora do ar"))
    result = _fetcher(root, load).ensure(BANK_REPO, version)
    assert result == FetchTemporaryFailure("OperationalError")
    assert not _destination(root, version).exists()
    _assert_no_staging_left(root)


def test_corrupted_archive_is_path_invalid_and_cleans_staging(root: pathlib.Path, files: dict[str, bytes]) -> None:
    """Pacote corrompido no banco de dados → `FetchRefused("path_invalid")`, com a área `.staging` vazia e nenhum
    destino: não gzip, truncado no meio de um membro (depois de membros já escritos na área de preparo) e um pacote
    com um link."""
    version = reference_version(files)
    whole = archive_of(files)
    raw = archive_of(files, gzip=False)
    truncated = make_archive([regular(path, files[path]) for path in sorted(files, key=lambda p: p.encode())],
                             truncate=len(raw) // 2)
    with_link = make_archive([*(regular(path, files[path]) for path in sorted(files)),
                              regular("atalho", type=b"2", linkname="infoCourse.json")])
    assert whole != raw
    for archive in (raw, truncated, with_link):
        result = _fetcher(root, _Loader(archive)).ensure(BANK_REPO, version)
        _assert_refused_clean(root, result, "path_invalid", version)


def test_smaller_worker_ceilings_are_max_files_and_max_bytes(root: pathlib.Path, files: dict[str, bytes]) -> None:
    """Os tetos do worker menores que os da api: mais arquivos que `max_files` → `FetchRefused("max_files")`, mais
    bytes que `max_bytes` → `FetchRefused("max_bytes")`, com a área `.staging` vazia e nenhum destino; no limite
    exato, a publicação acontece."""
    from cq_server.ports.repos import Fetched

    version = reference_version(files)
    archive = archive_of(files)
    total = sum(len(data) for data in files.values())
    assert len(files) > 2 and total > 10

    result = _fetcher(root, _Loader(archive), max_files=len(files) - 1).ensure(BANK_REPO, version)
    _assert_refused_clean(root, result, "max_files", version)
    result = _fetcher(root, _Loader(archive), max_bytes=total - 1).ensure(BANK_REPO, version)
    _assert_refused_clean(root, result, "max_bytes", version)

    result = _fetcher(root, _Loader(archive), max_files=len(files), max_bytes=total).ensure(BANK_REPO, version)
    assert isinstance(result, Fetched) and result.reused is False
    assert _tree(_destination(root, version)) == files


def test_recomputed_version_that_does_not_match_is_path_invalid(root: pathlib.Path,
                                                                files: dict[str, bytes]) -> None:
    """O pacote guardado que não dá a versão pedida (o conteúdo de outra versão) → `FetchRefused("path_invalid")`,
    com a área `.staging` vazia e nenhum destino; nem a versão pedida nem a do conteúdo ficam publicadas."""
    version = reference_version(files)
    other = dict(files)
    other["infoCourse.json"] = other["infoCourse.json"] + b"\n"
    other_version = reference_version(other)
    assert other_version != version
    result = _fetcher(root, _Loader(archive_of(other))).ensure(BANK_REPO, version)
    _assert_refused_clean(root, result, "path_invalid", version)
    assert not _destination(root, other_version).exists()
    leftovers = [path for path in root.iterdir() if path.name != ".staging"]
    for path in leftovers:
        assert path.is_dir() and list(path.iterdir()) == [], path


# --------------------------------------------------------------------------------------------------
# O marcador único: nada do banco sai pelos registros nem pelo desfecho
# --------------------------------------------------------------------------------------------------


def _marked(files: dict[str, bytes], mark: str) -> dict[str, bytes]:
    """O banco sintético com o marcador dentro do conteúdo de um arquivo e no nome de outro arquivo e do diretório
    dele (nomes aceitos pelas regras de caminho); os dois vêm antes dos arquivos do curso na ordem do pacote."""
    return {**files, "anotacoes/leia.txt": f"anotacao interna {mark}\n".encode("ascii"),
            f"anotacoes/{mark}/nota-{mark}.txt": b"nota\n"}


def _assert_marker_not_out(mark: str, captured: Any, outcome: Any) -> None:
    """Nenhum registro capturado (com o formatador real) e nenhum campo do desfecho levam o marcador."""
    assert mark not in captured.text, "o marcador num registro"
    assert mark not in repr(outcome), "o marcador no desfecho"


def _assert_marker_only_inside(root: pathlib.Path, mark: str, tree: pathlib.Path | None = None) -> None:
    """Sob a raiz, nenhum nome nem conteúdo leva o marcador fora de `tree` (a árvore publicada, quando há)."""
    needle = mark.encode("ascii")
    for path in root.rglob("*"):
        if tree is not None and (path == tree or tree in path.parents):
            continue
        assert mark not in path.name, "o marcador num nome fora da árvore publicada"
        if path.is_file():
            assert needle not in path.read_bytes(), "o marcador num conteúdo fora da árvore publicada"


def test_publication_logs_and_outcome_carry_no_bank_content_or_file_name(root: pathlib.Path,
                                                                         files: dict[str, bytes]) -> None:
    """O marcador único, montado na hora, no conteúdo de um arquivo do banco e no nome de outro: a publicação
    bem-sucedida e o reuso seguinte não o levam a nenhum registro capturado nem ao desfecho (`Fetched`, com as
    contagens certas, quando vêm); sob a raiz, ele só existe na árvore publicada em
    `<raiz>/<sha256 de "bank:<nome>">/<versão>/`, com os mesmos bytes, e a área `.staging` fica vazia."""
    from cq_server.ports.repos import Fetched

    mark = unique_marker()
    marked = _marked(files, mark)
    version = reference_version(marked)
    load = _Loader(archive_of(marked))
    fetcher = _fetcher(root, load)
    with capture_logs() as cap:
        result = fetcher.ensure(BANK_REPO, version)
        again = fetcher.ensure(BANK_REPO, version)

    assert isinstance(result, Fetched) and result.reused is False
    assert result.file_count in (None, len(marked))
    assert result.byte_count in (None, sum(len(data) for data in marked.values()))
    assert again == Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
    assert load.calls == [(BANK_NAME, version)]
    destination = _destination(root, version)
    assert _tree(destination) == marked
    _assert_no_staging_left(root)
    _assert_marker_not_out(mark, cap, result)
    _assert_marker_not_out(mark, cap, again)
    _assert_marker_only_inside(root, mark, destination)


def test_refusals_carry_no_bank_content_or_file_name(root: pathlib.Path, files: dict[str, bytes]) -> None:
    """O mesmo marcador nas recusas do adaptador: o pacote corrompido (não gzip; truncado depois dos arquivos
    marcados; com um link de nome e alvo marcados depois de todos os arquivos) → `FetchRefused("path_invalid")`; os
    tetos menores do worker, estourados no último membro → `FetchRefused("max_files")` e `FetchRefused("max_bytes")`;
    a versão recalculada que não confere → `FetchRefused("path_invalid")`. Cada desfecho é a recusa com o motivo
    fixo, sem o marcador; nenhum registro capturado o leva; nenhum destino é publicado, a área `.staging` fica vazia
    e o marcador não fica em nenhum nome nem conteúdo sob a raiz."""
    mark = unique_marker()
    marked = _marked(files, mark)
    version = reference_version(marked)
    members = members_of(marked)
    whole = archive_of(marked)
    raw = archive_of(marked, gzip=False)
    # Os dois arquivos marcados são os dois primeiros membros (cabeçalho e um bloco de conteúdo cada).
    assert sorted(marked, key=lambda path: path.encode())[:2] == [
        "anotacoes/leia.txt", f"anotacoes/{mark}/nota-{mark}.txt"]
    assert len(raw) // 2 > 4 * 512
    truncated = make_archive(members, truncate=len(raw) // 2)
    with_link = make_archive([*members, regular(f"link-{mark}", type=b"2",
                                                linkname=f"anotacoes/{mark}/nota-{mark}.txt")])
    other = dict(marked)
    other["anotacoes/leia.txt"] = other["anotacoes/leia.txt"] + b"\n"
    assert reference_version(other) != version
    total = sum(len(data) for data in marked.values())

    cases: list[tuple[str, dict[str, int], bytes]] = [
        ("path_invalid", {}, raw),
        ("path_invalid", {}, truncated),
        ("path_invalid", {}, with_link),
        ("max_files", {"max_files": len(marked) - 1}, whole),
        ("max_bytes", {"max_bytes": total - 1}, whole),
        ("path_invalid", {}, archive_of(other)),
    ]
    for index, (rule, limits, archive) in enumerate(cases):
        with capture_logs() as cap:
            result = _fetcher(root, _Loader(archive), **limits).ensure(BANK_REPO, version)
        _assert_refused_clean(root, result, rule, version)
        assert not _destination(root, reference_version(other)).exists(), index
        _assert_marker_not_out(mark, cap, result)
        _assert_marker_only_inside(root, mark)


def test_loader_failure_logs_and_outcome_carry_no_exception_text(root: pathlib.Path) -> None:
    """A falha do carregador do pacote com uma exceção que leva o marcador no texto e no nome de arquivo (um
    `OSError` com `filename`) e a do PostgreSQL com o marcador na mensagem: o desfecho é `FetchTemporaryFailure`
    com só o nome do tipo da exceção; nenhum registro capturado leva o marcador; nada é publicado, a área
    `.staging` fica vazia e o marcador não fica sob a raiz."""
    import errno

    import psycopg

    from cq_server.ports.repos import FetchTemporaryFailure

    mark = unique_marker()
    version = "02" * 32
    failures: list[BaseException] = [
        OSError(errno.EIO, f"falha ao ler {mark}", f"anotacoes/{mark}/nota-{mark}.txt"),
        psycopg.OperationalError(f"servidor fora do ar: {mark}"),
    ]
    assert [type(error).__name__ for error in failures] == ["OSError", "OperationalError"]
    assert all(mark in str(error) for error in failures)
    assert mark in failures[0].filename
    for error in failures:
        load = _Loader(error)
        with capture_logs() as cap:
            result = _fetcher(root, load).ensure(BANK_REPO, version)
        assert result == FetchTemporaryFailure(type(error).__name__)
        assert load.calls == [(BANK_NAME, version)]
        assert not _destination(root, version).exists()
        _assert_no_staging_left(root)
        _assert_marker_not_out(mark, cap, result)
        _assert_marker_only_inside(root, mark)
