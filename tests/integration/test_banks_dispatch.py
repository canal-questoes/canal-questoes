"""O disparo de uma questão de um banco de questões enviado (`repo` = `bank:<nome>`, `commit` = a versão).

Cobre: o disparo com `bank:` até `scheduled` com o motor falso e a mesma prévia, a mesma referência inspecionada e
os mesmos estados de um disparo da mesma questão vinda por `https`; o nome e a versão desconhecidos rejeitados
como `repository_unreachable` e `commit_not_found`, com o corpo igual ao da rejeição equivalente pelo `https`; a
questão que o motor declara não suportada, rejeitada pela validação do disparo com o mesmo corpo do `https`, num
banco aceito inteiro no envio; a versão antiga de um nome que ganhou uma versão nova, ainda validada e publicada com
os próprios bytes; a árvore apagada do volume, publicada de novo a partir do pacote guardado na validação, na
abertura e na correção, sem rede e sem passar pela obtenção do git.

Arnês: o oferecimento e o bot pela API; o aluno por SQL (pessoa pela API, matrícula `student` ativa e endereço
vigente no bot), como `test_engine_failures.py`; o banco enviado pela API (`banks.upload`) com o curso de
fixture (`tests/fixtures/repos/course`), empacotado à mão por `tests/helpers/banks.py`. O worker real
(`run_worker(…, bots=…, engine=…, fetcher=…, bank_fetcher=…, delivery=…)`) roda numa thread, com o adaptador HTTP
real sobre o motor falso estrito (`engine_fake`), a obtenção falsa (`repo_fetcher`) para as referências `https` e,
para as `bank:`, a publicação real montada pela composição do worker (`__main__.build_bank_fetcher`), na mesma
área de repositórios do motor falso, envolta num contador de chamadas do teste. A guarda de rede (`network_guard`)
barra todo destino fora do loopback.

Horários pelo `FakeClock`: com o worker no ar, o relógio anda em passos de até 10 s, esperando a renovação da
posse do bot. Dois disparos do mesmo oferecimento nunca têm o mesmo `closes_at`.

Vermelho esperado no esqueleto: o envio do banco responde 500 (a rota lança `NotImplementedError`); sem envio
(o nome desconhecido), a montagem da publicação dos bancos pela composição do worker lança `NotImplementedError`
(e, depois dela, o preparo ainda recusa a forma `bank:` com `[{repo, invalid_format}]`). Fica assim até o envio, a
publicação, o preparo com `bank:` e o roteador da obtenção no worker existirem.

Escolhas deste arquivo [I]:
- os "textos ao professor" comparados são os que a API devolve no `Dispatch` (a prévia, os avisos e a mensagem da
  rejeição); os avisos da equipe não entram (o oferecimento não tem equipe);
- a nova publicação de uma árvore apagada conta como `Fetched` com `reused` falso no contador.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import itertools
import json
import pathlib
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server.ports.repos import FetchNotFound
from tests.helpers import banks as bk
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import TelegramUser, make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.engine import UNSUPPORTED_REASON

COURSE = "Física Experimental"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
HTTPS_REPO = "https://repos.cq.test/turma/questoes-banco.git"
HTTPS_COMMIT = "b4c0" * 10
BANK_NAME = "banco-disparo"
WORKER_ID = "worker-banco"
TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
ENGINE_TIMEOUT = 10.0  # + 15 <= a posse de 30 s
FETCH_TIMEOUT = "10"  # o prazo da obtenção do worker: + 15 <= a posse de 30 s
DAY = timedelta(days=1)
STEP = 10.0  # salto do relógio com o worker no ar (menor que a margem de renovação da posse)
RENEW_MARGIN = timedelta(seconds=13)  # consulta 1 s + Telegram 2 s + renovação 10 s (bot_settings)
NUMBER_ANSWER = "6.5"  # a resposta certa do preset `number`

# As mensagens pareadas das rejeições (do contrato da API).
REJECTION_MESSAGES = {
    "repository_unreachable": "Não foi possível obter o repositório da questão.",
    "commit_not_found": "O commit informado não existe no repositório.",
    "question_unsupported": "A questão usa recursos que o sistema não suporta.",
}

_codes = itertools.count(1)
_people = itertools.count(1)


def _until(fn: Callable[[], Any], *, what: str, timeout: float = TIMEOUT) -> Any:
    """Espera `fn()` ficar verdadeiro (tempo real) e devolve o valor."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"tempo esgotado esperando {what}")
        time.sleep(0.02)


def _seconds(delta: timedelta) -> float:
    return delta.total_seconds()


def _repo_key(repo: str) -> str:
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class _Offering:
    code: str
    offering_id: str
    row_id: int
    bot: int  # id do bot na Bot API falsa
    bot_row: int


@dataclasses.dataclass(frozen=True)
class _Student:
    user: TelegramUser
    pseudo_id: str
    person_row: int
    enrollment_row: int

    @property
    def chat(self) -> int:
        return self.user.user_id


@dataclasses.dataclass(frozen=True)
class _Bank:
    name: str
    repo: str
    version: str
    files: dict[str, bytes]
    question_count: int


def _one_byte_changed(data: bytes) -> bytes:
    """Os mesmos bytes com um único byte trocado: a primeira letra minúscula ASCII vira maiúscula."""
    index = next(i for i, b in enumerate(data) if 0x61 <= b <= 0x7A)
    return data[:index] + bytes([data[index] ^ 0x20]) + data[index + 1:]


class _CountingFetcher:
    """Envolve a publicação dos bancos e anota cada `ensure`: (repo, commit, tipo do desfecho, `reused`)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._lock = threading.Lock()
        self._calls: list[tuple[str, str, str, bool | None]] = []

    def ensure(self, repo: str, commit: str) -> Any:
        try:
            result = self.inner.ensure(repo, commit)
        except BaseException as exc:
            with self._lock:
                self._calls.append((repo, commit, f"raised:{type(exc).__name__}", None))
            raise
        with self._lock:
            self._calls.append((repo, commit, type(result).__name__, getattr(result, "reused", None)))
        return result

    def calls(self) -> list[tuple[str, str, str, bool | None]]:
        with self._lock:
            return list(self._calls)


class _World:
    """O arnês do arquivo: oferecimento, alunos, banco enviado, worker e disparos (ver o docstring)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, settings: Any, rich: Any,
                 engine_fake: Any, repo_fetcher: Any, start: Any) -> None:
        from cq_server.adapters.engine import HttpQuestionEngine
        from cq_server.config import EngineSettings

        self.client = client
        self.sql = sql
        self.clock = clock
        self.settings = settings
        self.rich = rich
        self.engine_fake = engine_fake
        self.fetcher = repo_fetcher
        self.root = pathlib.Path(repo_fetcher.root)
        self._start = start
        self.engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                                        timeout_seconds=ENGINE_TIMEOUT))
        self._bank: _CountingFetcher | None = None
        self.threads: list[Any] = []

    # -- montagem --------------------------------------------------------------------------

    def upload(self, name: str = BANK_NAME, files: dict[str, bytes] | None = None) -> _Bank:
        """Envia `files` (padrão: o curso de fixture) como o banco `name` pela API; afirma 200 e o esquema."""
        files = bk.course_files() if files is None else files
        version = bk.reference_version(files)
        resp = self.client.post("/v1/question-banks", headers=auth(ADMIN_TOKEN),
                                json=bk.upload_body(name, version, "folder", None, bk.archive_of(files)))
        assert resp.status_code == 200, f"envio do banco: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert_matches("banks.upload", 200, body)
        assert (body["repo"], body["version"]) == (f"bank:{name}", version)
        return _Bank(name=name, repo=body["repo"], version=version, files=files,
                     question_count=body["question_count"])

    def bank_fetcher(self) -> _CountingFetcher:
        """A publicação dos bancos como o worker a monta (`build_bank_fetcher`), na área do motor falso."""
        if self._bank is None:
            from cq_server.__main__ import build_bank_fetcher

            env = {"CQ_REPOS_ROOT": str(self.root), "CQ_REPO_FETCH_TIMEOUT_SECONDS": FETCH_TIMEOUT}
            built = build_bank_fetcher(env, self.settings)
            assert built is not None, "a composição do worker não montou a publicação dos bancos"
            self._bank = _CountingFetcher(built)
        return self._bank

    def offering(self) -> _Offering:
        """Oferecimento `active` com bot `registered` na Bot API falsa (API de oferecimentos)."""
        code = f"banco-oferta-{next(_codes)}"
        body = create_offering(self.client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
        bot = self.rich.add_bot()
        register_bot(self.client, ADMIN_TOKEN, code, self.rich.token_of(bot))
        activate_bot(self.client, ADMIN_TOKEN, code)
        row_id, bot_row = self.sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.code = %s",
            (code,)).fetchone()
        return _Offering(code=code, offering_id=body["offering_id"], row_id=row_id, bot=bot, bot_row=bot_row)

    def student(self, off: _Offering) -> _Student:
        """Aluno vinculado: pessoa pelo cadastro de pessoas; matrícula `student` `active` com o termo aceito e
        endereço `active` no bot, por SQL; o chat privado passa a existir na Bot API falsa por um update de tipo
        fora do `allowed_updates`, que o dublê descarta (nenhum update chega ao worker)."""
        n = next(_people)
        user = make_user(n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Pessoa Aluna Banco {n:02d}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (body["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment_row = self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) "
            "RETURNING id", (person_row, off.row_id, now, now, now)).fetchone()[0]
        self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
            "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
            (person_row, off.bot_row, user.user_id, now, now))
        dropped = self.rich.push_update(off.bot, {"message_reaction": {
            "chat": user.as_private_chat(), "message_id": 1, "user": user.as_user(), "date": 0,
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}})
        assert dropped is None, "o update de apoio não pode chegar ao worker"
        return _Student(user=user, pseudo_id=body["pseudo_id"], person_row=person_row,
                        enrollment_row=enrollment_row)

    # -- worker ----------------------------------------------------------------------------

    def start_worker(self, off: _Offering) -> Any:
        from cq_server.config import DeliverySettings

        thread = self._start(settings=dataclasses.replace(self.settings, worker_id=WORKER_ID), engine=self.engine,
                             fetcher=self.fetcher, bank_fetcher=self.bank_fetcher(), delivery=DeliverySettings())
        self.threads.append(thread)
        self.rich.wait_for(lambda: self.rich.pending_polls(off.bot), timeout=TIMEOUT)
        return thread

    def close(self) -> None:
        self._start.stop()
        with contextlib.suppress(Exception):
            self.engine.close()

    # -- relógio ---------------------------------------------------------------------------

    def _wait_leases(self) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            now = self.clock.now()
            rows = self.sql.execute("SELECT lease_until FROM bots WHERE lease_owner IS NOT NULL").fetchall()
            if all(until - now >= RENEW_MARGIN for (until,) in rows):
                return
            time.sleep(0.02)

    def advance(self, seconds: float) -> None:
        """Anda o `FakeClock` com o worker no ar, em passos de até `STEP`."""
        remaining = float(seconds)
        while remaining > 0:
            step = min(STEP, remaining)
            self.clock.advance(step)
            remaining -= step
            self._wait_leases()

    # -- disparo ---------------------------------------------------------------------------

    def prepare(self, off: _Offering, repo: str, commit: str, qid: str, *, closes_in: timedelta = DAY) -> dict:
        return prepare_dispatch(self.client, ADMIN_TOKEN, off.code, repo=repo, commit=commit, qid=qid,
                                closes_at=iso(self.clock.now() + closes_in))

    def dispatch_row(self, dispatch: dict) -> dict | None:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM dispatches WHERE dispatch_uuid = %s",
                               (dispatch["dispatch_id"],)).fetchone()

    def wait_dispatch(self, dispatch: dict, state: str) -> dict:
        def ready() -> dict | None:
            row = self.dispatch_row(dispatch)
            return row if row is not None and row["state"] == state else None
        return _until(ready, what=f"o disparo em {state}")

    def get(self, off: _Offering, dispatch: dict) -> dict:
        return get_dispatch(self.client, ADMIN_TOKEN, off.code, dispatch["dispatch_id"])

    def ref(self, repo: str, commit: str, qid: str) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT id, supported, show_correct_answer, single_variant, partial_credit, title, answers_names, "
                "content_hash, inspected_at IS NOT NULL AS inspected FROM question_refs "
                "WHERE repo = %s AND commit_sha = %s AND qid = %s", (repo, commit, qid)).fetchone()
        assert row is not None, "referência ausente de question_refs"
        return row

    def tree(self, bank: _Bank, commit: str | None = None) -> pathlib.Path:
        return self.root / _repo_key(bank.repo) / (commit or bank.version)

    def tree_files(self, bank: _Bank) -> dict[str, bytes]:
        top = self.tree(bank)
        return {p.relative_to(top).as_posix(): p.read_bytes() for p in top.rglob("*") if p.is_file()}

    def delete_tree(self, bank: _Bank) -> None:
        top = self.tree(bank)
        assert top.is_dir(), "a árvore do banco não estava publicada"
        shutil.rmtree(top)

    # -- entregas, submissões e tarefas ----------------------------------------------------

    def deliveries(self, dispatch: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT d.* FROM deliveries d JOIN dispatches p ON p.id = d.dispatch_id "
                "WHERE p.dispatch_uuid = %s ORDER BY d.id", (dispatch["dispatch_id"],)).fetchall()

    def delivery_of(self, dispatch: dict, student: _Student) -> dict:
        [row] = [d for d in self.deliveries(dispatch) if d["enrollment_id"] == student.enrollment_row]
        return row

    def wait_delivery(self, dispatch: dict, student: _Student, state: str) -> dict:
        def ready() -> dict | None:
            rows = [d for d in self.deliveries(dispatch) if d["enrollment_id"] == student.enrollment_row]
            if rows and rows[0]["state"] == state and (state != "delivered" or rows[0]["sent_at"]):
                return rows[0]
            return None
        return _until(ready, what=f"a entrega em {state}")

    def submissions(self, delivery: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM submissions WHERE delivery_id = %s ORDER BY id",
                               (delivery["id"],)).fetchall()

    def wait_submissions(self, delivery: dict, count: int) -> list[dict]:
        def ready() -> list[dict] | None:
            rows = self.submissions(delivery)
            return rows if len(rows) == count and rows[-1]["outcome"] != "grading" else None
        return _until(ready, what=f"{count} submissões")

    def tasks(self, task_type: str) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM tasks WHERE task_type = %s ORDER BY id", (task_type,)).fetchall()

    def wait_task(self, task_type: str, check: Callable[[dict], bool], *, what: str) -> dict:
        def ready() -> dict | None:
            rows = self.tasks(task_type)
            return rows[-1] if rows and check(rows[-1]) else None
        return _until(ready, what=what)

    def answer(self, off: _Offering, student: _Student, delivery: dict, text: str = NUMBER_ANSWER) -> None:
        """O aluno responde, pelo "responder", à mensagem da questão."""
        row = self.sql.execute(
            "SELECT o.channel_ref FROM deliveries d JOIN outbox_messages o ON o.id = d.principal_message_id "
            "WHERE d.id = %s", (delivery["id"],)).fetchone()
        assert row is not None and row[0] is not None, "a entrega não tem mensagem principal"
        question = self.rich.current(off.bot, student.chat, int(row[0]))
        assert self.rich.push_text(off.bot, student.user, text, reply_to_message_id=question.message_id) is not None


@pytest.fixture
def world(client: Any, sql: psycopg.Connection, clock: Any, settings: Any, rich_bot_api: Any, engine_fake: Any,
          repo_fetcher: Any, bot_worker_thread: Any, network_guard: Any) -> Iterator[_World]:
    """O arnês, com a guarda de rede ligada (pela fixture): no fim, nenhuma conexão barrada fora do loopback."""
    w = _World(client=client, sql=sql, clock=clock, settings=settings, rich=rich_bot_api, engine_fake=engine_fake,
               repo_fetcher=repo_fetcher, start=bot_worker_thread)
    try:
        yield w
    finally:
        w.close()


def _comparable(body: dict) -> dict:
    """O `Dispatch` sem o que identifica o disparo e a referência (o id, a referência da questão)."""
    out = {k: v for k, v in body.items() if k not in ("dispatch_id", "question")}
    out["qid"] = body["question"]["qid"]
    return out


def _ref_facts(ref: dict) -> dict:
    return {k: v for k, v in ref.items() if k != "id"}


def _validation_fetches(repo: str, commit: str, steps: int = 3) -> list[tuple[str, str, str, bool | None]]:
    """As chamadas à obtenção numa validação, uma por passo (a obtenção, a inspeção e a variante), como no `https`:
    a primeira publica a árvore e as seguintes a reusam; a rejeição na inspeção para no segundo passo."""
    return [(repo, commit, "Fetched", False)] + [(repo, commit, "Fetched", True)] * (steps - 1)


# ---------------------------------------------------------------------------------------------
# O disparo até `scheduled`: a mesma prévia do `https`
# ---------------------------------------------------------------------------------------------


def test_bank_dispatch_reaches_scheduled_with_the_same_preview_as_https(world: _World) -> None:
    """A mesma questão (`number`) do mesmo curso, por `bank:<nome>` com a versão e por `https`: os dois disparos
    chegam a `scheduled` com a mesma prévia, os mesmos avisos e os mesmos estados; as duas referências têm a mesma
    inspeção (suporte, título, campos, conteúdo). O disparo com `bank:` publica a árvore do pacote guardado em
    `<repos>/<sha256(repo)>/<versão>/` (os arquivos do curso, byte a byte) na primeira chamada da validação à
    publicação dos bancos (as seguintes reusam a árvore, como no `https`), e nunca passa pela obtenção do git.
    Vermelho no esqueleto: o envio do banco responde 500."""
    bank = world.upload()
    off = world.offering()
    world.start_worker(off)
    world.engine_fake.program((HTTPS_REPO, HTTPS_COMMIT, "number"), "number")
    world.engine_fake.program((bank.repo, bank.version, "number"), "number")

    by_https = world.prepare(off, HTTPS_REPO, HTTPS_COMMIT, "number")
    by_bank = world.prepare(off, bank.repo, bank.version, "number", closes_in=DAY + timedelta(minutes=1))
    assert by_bank["question"]["repo"] == bank.repo and by_bank["question"]["commit"] == bank.version
    world.wait_dispatch(by_https, "scheduled")
    world.wait_dispatch(by_bank, "scheduled")

    https_body, bank_body = world.get(off, by_https), world.get(off, by_bank)
    assert bank_body["state"] == "scheduled"
    assert bank_body["preview"] is not None
    assert bank_body["preview"] == https_body["preview"]
    assert {k: v for k, v in _comparable(bank_body).items() if k != "closes_at"} == {
        k: v for k, v in _comparable(https_body).items() if k != "closes_at"}
    assert bank_body["question"] == {"question_ref_id": bank_body["question"]["question_ref_id"], "repo": bank.repo,
                                     "commit": bank.version, "qid": "number"}
    assert _ref_facts(world.ref(bank.repo, bank.version, "number")) == _ref_facts(
        world.ref(HTTPS_REPO, HTTPS_COMMIT, "number"))

    assert world.bank_fetcher().calls() == _validation_fetches(bank.repo, bank.version)
    assert world.fetcher.calls(bank.repo) == [], "a referência do banco passou pela obtenção do git"
    assert [(HTTPS_REPO, HTTPS_COMMIT, c.outcome, c.reused) for c in world.fetcher.calls(HTTPS_REPO, HTTPS_COMMIT)] == (
        _validation_fetches(HTTPS_REPO, HTTPS_COMMIT))
    assert world.tree_files(bank) == bank.files


@pytest.mark.parametrize("missing", ["nome", "versao"])
def test_unknown_bank_name_or_version_is_rejected_like_the_https_equivalent(world: _World, missing: str) -> None:
    """O nome de banco que nunca foi enviado → `rejected` com `repository_unreachable`; a versão que o banco não
    tem → `rejected` com `commit_not_found`. O corpo da rejeição (o motivo, a mensagem pareada, os detalhes vazios)
    e os estados são iguais aos do `https` com o repositório ou o commit inexistentes na origem; nenhuma árvore é
    publicada e o motor não é chamado para a referência. Vermelho no esqueleto: com o nome, a montagem da publicação
    dos bancos lança `NotImplementedError` (e o preparo ainda recusa a forma `bank:`); com a versão, o envio do banco
    responde 500."""
    off = world.offering()
    if missing == "nome":
        repo, commit, reason, outcome = "bank:banco-inexistente", "e" * 64, "repository_unreachable", "repository"
    else:
        bank = world.upload()
        repo, commit, reason, outcome = bank.repo, "f" * 64, "commit_not_found", "commit"
    world.fetcher.program(HTTPS_REPO, HTTPS_COMMIT, FetchNotFound(outcome))
    world.start_worker(off)

    by_bank = world.prepare(off, repo, commit, "number")
    by_https = world.prepare(off, HTTPS_REPO, HTTPS_COMMIT, "number", closes_in=DAY + timedelta(minutes=1))
    world.wait_dispatch(by_bank, "rejected")
    world.wait_dispatch(by_https, "rejected")

    bank_body, https_body = world.get(off, by_bank), world.get(off, by_https)
    assert bank_body["rejection"] == {"reason": reason, "message": REJECTION_MESSAGES[reason], "details": []}
    assert {k: v for k, v in _comparable(bank_body).items() if k not in ("closes_at", "target_count")} == {
        k: v for k, v in _comparable(https_body).items() if k not in ("closes_at", "target_count")}
    assert world.bank_fetcher().calls() == [(repo, commit, "FetchNotFound", None)]
    assert not (world.root / _repo_key(repo) / commit).exists()
    assert [c for c in world.engine_fake.calls() if c.op in ("inspect", "create_variant")
            and repo in json.dumps(c.request)] == []
    assert world.fetcher.calls(repo) == []


def test_unsupported_bank_question_is_rejected_at_validation_like_the_https_equivalent(world: _World) -> None:
    """Um banco com uma questão que o motor declara não suportada é aceito inteiro no envio (200, a questão entra na
    contagem). A recusa fica para a validação do disparo, por questão: o disparo por `bank:<nome>` com a versão chega
    a `rejected` com `question_unsupported`, a mensagem pareada e o motivo do motor nos detalhes, o mesmo corpo de
    rejeição e os mesmos estados do disparo da mesma questão por `https`; a referência do banco fica sem registro de
    suporte (nada é gravado para a questão recusada), igual à do `https`. Vermelho no esqueleto: o envio do banco
    responde 500."""
    bank = world.upload()
    assert "questions/unsupported/info.json" in bank.files
    assert bank.question_count == bk.reference_question_count(bank.files)
    off = world.offering()
    world.start_worker(off)
    world.engine_fake.program((HTTPS_REPO, HTTPS_COMMIT, "unsupported"), "unsupported")
    world.engine_fake.program((bank.repo, bank.version, "unsupported"), "unsupported")

    by_bank = world.prepare(off, bank.repo, bank.version, "unsupported")
    by_https = world.prepare(off, HTTPS_REPO, HTTPS_COMMIT, "unsupported", closes_in=DAY + timedelta(minutes=1))
    world.wait_dispatch(by_bank, "rejected")
    world.wait_dispatch(by_https, "rejected")

    bank_body, https_body = world.get(off, by_bank), world.get(off, by_https)
    assert bank_body["rejection"] == {"reason": "question_unsupported",
                                      "message": REJECTION_MESSAGES["question_unsupported"],
                                      "details": [UNSUPPORTED_REASON.message]}
    assert bank_body["rejection"] == https_body["rejection"]
    assert {k: v for k, v in _comparable(bank_body).items() if k not in ("closes_at", "target_count")} == {
        k: v for k, v in _comparable(https_body).items() if k not in ("closes_at", "target_count")}
    bank_ref = world.ref(bank.repo, bank.version, "unsupported")
    assert bank_ref["supported"] is None
    assert _ref_facts(bank_ref) == _ref_facts(world.ref(HTTPS_REPO, HTTPS_COMMIT, "unsupported"))
    assert world.bank_fetcher().calls() == _validation_fetches(bank.repo, bank.version, steps=2)
    assert world.fetcher.calls(bank.repo) == []


# ---------------------------------------------------------------------------------------------
# A versão antiga de um nome com versão nova: ainda utilizável pelos disparos que a citam
# ---------------------------------------------------------------------------------------------


def test_old_bank_version_still_dispatches_after_a_new_version_of_the_same_name(world: _World) -> None:
    """A versão 1 de um banco é validada num disparo; um novo envio com o mesmo nome e um único byte diferente num
    arquivo cria a versão 2, sem trocar a versão 1. Com a árvore da versão 1 apagada do volume, um disparo que cita
    `bank:<nome>` e a versão 1 ainda chega a `scheduled`: a publicação recebe (repo, versão 1) e devolve a árvore em
    `<repos>/<sha256(repo)>/<versão 1>/` com os bytes da versão 1, não os da versão 2. Vermelho no esqueleto: o
    envio do banco responde 500."""
    first = world.upload()
    off = world.offering()
    world.start_worker(off)
    world.engine_fake.program((first.repo, first.version, "number"), "number")
    world.engine_fake.program((first.repo, first.version, "integer"), "integer")
    opening = world.prepare(off, first.repo, first.version, "number")
    world.wait_dispatch(opening, "scheduled")

    changed = dict(first.files)
    target = "questions/integer/question.html"
    changed[target] = _one_byte_changed(changed[target])
    second = world.upload(files=changed)
    assert second.repo == first.repo
    assert second.version != first.version

    world.delete_tree(first)
    later = world.prepare(off, first.repo, first.version, "integer", closes_in=DAY + timedelta(minutes=1))
    world.wait_dispatch(later, "scheduled")

    body = world.get(off, later)
    assert (body["question"]["repo"], body["question"]["commit"]) == (first.repo, first.version)
    assert world.bank_fetcher().calls() == _validation_fetches(first.repo, first.version) * 2
    published = world.tree_files(first)
    assert published == first.files
    assert published[target] != changed[target]
    assert world.fetcher.calls(first.repo) == []


# ---------------------------------------------------------------------------------------------
# A árvore apagada do volume: publicada de novo do pacote guardado
# ---------------------------------------------------------------------------------------------


def test_deleted_bank_tree_is_published_again_at_validation(world: _World) -> None:
    """Depois da validação de um disparo com `bank:`, a árvore some do volume; um novo disparo do mesmo banco e da
    mesma versão (outra questão, `integer`) é validado até `scheduled` com uma nova publicação (`Fetched` sem
    reuso), a árvore volta igual ao curso e nada passa pela obtenção do git. Vermelho no esqueleto: o envio do
    banco responde 500."""
    bank = world.upload()
    off = world.offering()
    world.start_worker(off)
    world.engine_fake.program((bank.repo, bank.version, "number"), "number")
    world.engine_fake.program((bank.repo, bank.version, "integer"), "integer")
    first = world.prepare(off, bank.repo, bank.version, "number")
    world.wait_dispatch(first, "scheduled")
    assert world.bank_fetcher().calls() == _validation_fetches(bank.repo, bank.version)

    world.delete_tree(bank)
    second = world.prepare(off, bank.repo, bank.version, "integer", closes_in=DAY + timedelta(minutes=1))
    world.wait_dispatch(second, "scheduled")

    assert world.bank_fetcher().calls() == _validation_fetches(bank.repo, bank.version) * 2
    assert world.tree_files(bank) == bank.files
    assert world.fetcher.calls(bank.repo) == []


def test_deleted_bank_tree_is_published_again_at_opening(world: _World) -> None:
    """A árvore some depois da validação e antes da preparação da entrega: a criação da variante recebe
    `not_found`, sai a nova obtenção da referência, a publicação dos bancos recria a árvore (`Fetched` sem reuso) e a
    variante sai na tentativa seguinte; a entrega chega a `delivered`. Vermelho no esqueleto: o envio do banco
    responde 500."""
    bank = world.upload()
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    world.engine_fake.program((bank.repo, bank.version, "number"), "number")
    dispatch = world.prepare(off, bank.repo, bank.version, "number")
    world.wait_dispatch(dispatch, "scheduled")
    published = len(world.bank_fetcher().calls())
    variants_before = len(world.engine_fake.calls("create_variant"))

    world.delete_tree(bank)
    confirm_dispatch(world.client, ADMIN_TOKEN, off.code, dispatch["dispatch_id"])
    prepare = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                              what="a preparação da entrega com not_found")
    world.wait_task("question.refetch", lambda t: t["state"] == "done", what="a nova obtenção")
    assert world.bank_fetcher().calls()[published:] == [(bank.repo, bank.version, "Fetched", False)]

    world.advance(max(0.0, _seconds(prepare["available_at"] - world.clock.now())))
    world.wait_delivery(dispatch, alice, "delivered")
    assert [c.status for c in world.engine_fake.calls("create_variant")][variants_before:] == [404, 200]
    assert world.tree_files(bank) == bank.files
    assert world.fetcher.calls(bank.repo) == []


def test_deleted_bank_tree_is_published_again_at_grading(world: _World) -> None:
    """A árvore some depois da entrega: a correção recebe `not_found`, a submissão continua `grading`, sai a nova
    obtenção, a publicação dos bancos recria a árvore (`Fetched` sem reuso) e a correção seguinte sai com nota 1.
    Vermelho no esqueleto: o envio do banco responde 500."""
    bank = world.upload()
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    world.engine_fake.program((bank.repo, bank.version, "number"), "number")
    dispatch = world.prepare(off, bank.repo, bank.version, "number")
    world.wait_dispatch(dispatch, "scheduled")
    confirm_dispatch(world.client, ADMIN_TOKEN, off.code, dispatch["dispatch_id"])
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    published = len(world.bank_fetcher().calls())

    world.delete_tree(bank)
    world.answer(off, alice, delivery)
    grade = world.wait_task("submission.grade", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                            what="a correção com not_found")
    [submission] = world.submissions(delivery)
    assert submission["outcome"] == "grading"
    world.wait_task("question.refetch", lambda t: t["state"] == "done", what="a nova obtenção")
    assert world.bank_fetcher().calls()[published:] == [(bank.repo, bank.version, "Fetched", False)]

    world.advance(max(0.0, _seconds(grade["available_at"] - world.clock.now())))
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    assert [c.status for c in world.engine_fake.calls("grade")] == [404, 200]
    assert world.tree_files(bank) == bank.files
    assert world.fetcher.calls(bank.repo) == []
