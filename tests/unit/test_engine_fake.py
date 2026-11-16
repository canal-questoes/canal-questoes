"""Autoteste do motor falso estrito e paridade com o adaptador real: a forma do motor falso, a
classificação de erros do adaptador, as checagens locais, o prazo total e as formas da resposta crua
que ele aceita.

O motor falso (`tests/helpers/engine.py`) é o oráculo de todos os testes que chamam o motor. Se ele
aceitar pedido fora do esquema, deixar passar uma resposta crua que o `raw_for_engine` não produz,
responder fora do contrato sem acusar, errar os textos do PL ou escrever em stdout/stderr, os testes do
adaptador e do núcleo passam no vazio. Estes testes usam o `http.client` como cliente e conferem o
próprio arnês.

**Paridade dublê × adaptador real**:
em cada teste, o que o dublê responde é decodificado pelo `HttpQuestionEngine` real
(`cq_server.adapters.engine`), com um transporte simulado do `httpx2` (`_Relay`: repassa ao dublê o
método, o caminho, a consulta, o `Content-Type` e o corpo exatos do pedido do adaptador e devolve a
resposta do dublê sem mudança) ou, nos casos de rede e de prazo, pelo loopback real. O resultado da porta
é o que a classificação de erros do adaptador manda (200 → valores; 404 `not_found` → `EngineNotFound`; 400/413 →
`EngineRequestRejected`; 404 `route_not_found`/405 → `incompatible`; 500 → `internal_error`; `health` 500
→ `not_ready`; versões ≠ 1.0 → `incompatible`; rede → `connection`; prazo → `timeout`; o resto →
`protocol`), e o que o adaptador manda é aceito pelo dublê (`protocol_errors() == []`). O dublê e o
adaptador, assim, não divergem em silêncio: o esquema que um aceita o outro decodifica.

No esqueleto, `HttpQuestionEngine.inspect`, `create_variant`, `grade` e
`close` lançam `NotImplementedError`; toda função deste arquivo chama o adaptador. As asserções
sobre o próprio arnês (que já valem no esqueleto) ficam na mesma função de um caso de paridade.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import http.client
import json
import pathlib
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import EngineSettings
from cq_server.ports.engine import (
    Broken,
    EngineNotFound,
    EngineRequestRejected,
    EngineUnavailable,
    EngineWarning,
    FormatError,
    Graded,
    QuestionInfo,
    QuestionRef,
    Supported,
    Unsupported,
    UnsupportedReason,
    Variant,
    VersionMismatch,
)
from tests.helpers import engine as engine_module
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    ERROR_MESSAGES,
    ERROR_STATUS,
    INTEGER_INVALID_TEXT,
    MAX_RESPONSE_BYTES,
    NETWORK_KINDS,
    NOT_FOUND_COMMIT,
    NOT_FOUND_INFO_JSON,
    NOT_FOUND_QUESTION,
    NOT_FOUND_REPOSITORY,
    NUMBER_INVALID_TEXT,
    PL_COMMIT,
    PRESETS,
    TECHNICAL_DETAIL_SENTINEL,
    Reply,
    SENTINEL_KEY,
    TECHNICAL_DETAIL,
    TEXT_BLANK,
    FakeEngine,
    FakeField,
    FakeOption,
    FakeQuestion,
    invalid_option_error,
    monotonic,
    png_bytes,
    preset,
    repo_key,
    schema_errors,
    session_engine,
)

HELPER = pathlib.Path(__file__).resolve().parents[1] / "helpers" / "engine.py"
COURSE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "course"
REPO = "https://repos.cq.test/curso-teste.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CANARY_IDENTITY = "Fulana Canário 99001122 fulana.canario@example.org"
TIMEOUT = 10.0  # prazo do adaptador nos casos que não medem o prazo (folga no Windows)
DEADLINE_SLACK = 0.5  # a folga do prazo total (≤ prazo + 0,5 s reais)

# o erro do contrato do motor → (exceção da porta, `kind`).
PORT_ERROR: dict[str, tuple[type[Exception], str]] = {
    "invalid_request": (EngineRequestRejected, "invalid_request"),
    "not_found": (EngineNotFound, "not_found"),
    "payload_too_large": (EngineRequestRejected, "payload_too_large"),
    "route_not_found": (EngineUnavailable, "incompatible"),
    "method_not_allowed": (EngineUnavailable, "incompatible"),
    "internal_error": (EngineUnavailable, "internal_error"),
}


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server() -> Iterator[FakeEngine]:
    fake = FakeEngine()
    yield fake
    fake.close()


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A área de repositórios com o curso de teste em `<repo_key(REPO)>/<COMMIT>/`."""
    shutil.copytree(COURSE, tmp_path / repo_key(REPO) / COMMIT)
    return tmp_path


@pytest.fixture
def fake(server: FakeEngine, root: pathlib.Path) -> Iterator[FakeEngine]:
    """O dublê do módulo, limpo a cada teste e ligado à área, como a fixture `engine_fake` faz."""
    server.reset(repos_root=root)
    yield server
    server.release_hangs()
    server.reset()


def _ref(qid: str, *, repo: str = REPO, commit: str = COMMIT) -> dict[str, str]:
    return {"repo": repo, "commit": commit, "qid": qid}


def _port_ref(qid: str, *, repo: str = REPO, commit: str = COMMIT) -> QuestionRef:
    return QuestionRef(repo=repo, commit=commit, qid=qid)


def _variant(fake: FakeEngine, qid: str, seed: str = "1abc") -> dict[str, Any]:
    reply = fake.request("create_variant", {"question_ref": _ref(qid), "seed": seed})
    assert reply.status == 200, reply.body
    return reply.json()


def _grade(fake: FakeEngine, qid: str, snapshot: dict[str, Any], raw: dict[str, Any]) -> Reply:
    return fake.request("grade", {"question_ref": _ref(qid), "snapshot": snapshot,
                                  "raw_submitted_answers": raw})


def _correct_raw(question: FakeQuestion) -> dict[str, Any]:
    """A resposta crua certa na forma do `raw_for_engine`."""
    raw: dict[str, Any] = {}
    for f in question.fields:
        if f.type == "multiple_choice":
            raw[f.name] = list(f.correct) if len(f.correct) > 1 else f.correct[0]
        else:
            raw[f.name] = str(f.correct)
    return raw


def _question(name: str) -> FakeQuestion:
    return preset(name, identity=CANARY_IDENTITY) if name == "canary" else preset(name)


def _raw_request(fake: FakeEngine, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", fake.port), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


# --- paridade com o adaptador real ------------------------------------------------------------


class _Relay(httpx2.BaseTransport):
    """Transporte simulado do `httpx2` (o `HttpQuestionEngine` recebe o
    transporte por `transport=`): repassa ao dublê o pedido do adaptador real (método, caminho, consulta,
    `Content-Type` e corpo exatos) e devolve a resposta do dublê sem mudança. `exchanges` guarda
    `(caminho, resposta do dublê)` de cada pedido, na ordem."""

    def __init__(self, fake: FakeEngine) -> None:
        self.fake = fake
        self.exchanges: list[tuple[str, Reply]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        body = request.read()
        query = request.url.query
        reply = self.fake.request(request.url.path, raw=body or None, http_method=request.method,
                                  content_type=request.headers.get("content-type"),
                                  query=query.decode("ascii") if isinstance(query, bytes) else str(query))
        self.exchanges.append((request.url.path, reply))
        headers = [(k, v) for k, v in reply.headers.items()
                   if k not in ("content-length", "transfer-encoding", "connection", "keep-alive")]
        return httpx2.Response(reply.status, headers=headers, content=reply.body, request=request)

    def paths(self) -> list[str]:
        return [path for path, _ in self.exchanges]

    def last(self, path: str) -> Reply:
        return [reply for p, reply in self.exchanges if p == path][-1]


@contextlib.contextmanager
def _adapter(fake: FakeEngine, *, timeout: float = TIMEOUT,
             relay: bool = True) -> Iterator[tuple[HttpQuestionEngine, _Relay]]:
    """O adaptador real ligado ao dublê: pelo `_Relay` (padrão) ou pelo loopback real (`relay=False`,
    para rede e prazo)."""
    transport = _Relay(fake)
    settings = EngineSettings(engine_url=fake.base_url, timeout_seconds=timeout)
    engine = (HttpQuestionEngine(settings, transport=transport) if relay
              else HttpQuestionEngine(settings))
    try:
        yield engine, transport
    finally:
        engine.close()


def _info(meta: dict[str, Any]) -> QuestionInfo:
    return QuestionInfo(uuid=meta["uuid"], title=meta["title"], topic=meta["topic"], type=meta["type"],
                        tags=tuple(meta["tags"]), grading_method=meta["grading_method"],
                        elements=tuple(meta["elements"]), show_correct_answer=meta["show_correct_answer"],
                        single_variant=meta["single_variant"], partial_credit=meta["partial_credit"],
                        answers_names=tuple(meta["answers_names"]), content_hash=meta["content_hash"])


def _warnings(items: list[dict[str, Any]]) -> tuple[EngineWarning, ...]:
    return tuple(EngineWarning(code=w["code"], message=w["message"], phase=w.get("phase")) for w in items)


def _reasons(items: list[dict[str, Any]]) -> tuple[UnsupportedReason, ...]:
    return tuple(UnsupportedReason(code=r["code"], message=r["message"], elements=tuple(r.get("elements", ())))
                 for r in items)


def _port_value(op: str, body: dict[str, Any]) -> Any:
    """O valor da porta que a classificação de erros manda para a resposta 200 `body` de `op`."""
    outcome = body.get("outcome")
    if op == "inspect":
        info = _info(body["question"])
        return Supported(question=info) if outcome == "supported" else Unsupported(
            reasons=_reasons(body["reasons"]), question=info)
    if outcome == "unsupported":
        return Unsupported(reasons=_reasons(body["reasons"]), question=None)
    if outcome == "broken":
        reason = body["reason"]
        mismatch = body.get("version_mismatch")
        return Broken(code=reason["code"], message=reason["message"], phase=reason["phase"],
                      warnings=_warnings(body.get("warnings", [])),
                      version_mismatch=VersionMismatch(**mismatch) if mismatch is not None else None)
    if op == "create_variant":
        return Variant(snapshot=body["snapshot"], ir=body["ir"], warnings=_warnings(body["warnings"]))
    return Graded(valid=body["valid"],
                  format_errors={k: FormatError(html=v["html"], text=v["text"])
                                 for k, v in body["format_errors"].items()},
                  submitted_answers=body["submitted_answers"], partial_scores=body["partial_scores"],
                  score=body["score"], feedback=body["feedback"], params=body["params"],
                  correct_answers=body["correct_answers"],
                  version_mismatch=VersionMismatch(**body["version_mismatch"]),
                  warnings=_warnings(body["warnings"]))


def _assert_same(result: Any, expected: Any) -> None:
    """`result` (do adaptador) igual a `expected`; no `Graded`, campo a campo (o `repr` é fixo)."""
    assert type(result) is type(expected), (type(result), type(expected))
    if isinstance(expected, Graded):
        for f in dataclasses.fields(Graded):
            got, want = getattr(result, f.name), getattr(expected, f.name)
            if f.name == "format_errors":
                got = dict(got)
            assert got == want, f.name
    else:
        assert result == expected
    assert TECHNICAL_DETAIL_SENTINEL not in repr(result)


def _raises(expected: tuple[type[Exception], str], call: Any) -> Exception:
    """`call()` levanta exatamente a exceção da porta com o `kind`; devolve a exceção."""
    cls, kind = expected
    with pytest.raises(cls) as info:
        call()
    assert type(info.value) is cls and info.value.kind == kind, (type(info.value), info.value.kind)
    return info.value


# ---------------------------------------------------------------------------------------------
# Arnês
# ---------------------------------------------------------------------------------------------


def test_listens_on_loopback_with_health_and_keep_alive(fake: FakeEngine) -> None:
    """O arnês (junto da paridade): o dublê só usa a biblioteca padrão e o `jsonschema` (nunca o
    `cq_server`) e escuta no loopback com `health` e keep-alive. Paridade (a classificação de erros, no
    caso da compatibilidade): o adaptador real aceita o `health` do dublê como compatível e segue para a
    operação, com o pedido aceito pelo dublê."""
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "cq_server" not in imported
    assert imported <= {"__future__", "atexit", "base64", "copy", "hashlib", "html", "http", "itertools",
                        "json", "os", "pathlib", "re", "select", "socket", "socketserver", "struct",
                        "threading", "time", "urllib", "zlib", "collections", "dataclasses", "functools",
                        "typing", "jsonschema", "referencing"}

    assert fake.base_url == f"http://127.0.0.1:{fake.port}"
    reply = fake.request("health")
    assert reply.status == 200
    assert reply.json() == {"status": "ready", "engine_version": "1.0.0", "contract_version": "1.0",
                            "ir_version": "1.0", "pl_commit": PL_COMMIT, "workers": 4}
    assert reply.headers["content-type"] == "application/json; charset=utf-8"
    assert reply.headers["connection"] == "keep-alive"
    assert reply.headers["keep-alive"] == "timeout=5"
    assert fake.protocol_errors() == []
    assert session_engine() is session_engine()

    fake.program("number", preset("number"))
    with _adapter(fake) as (engine, relay):
        result = engine.inspect(_port_ref("number"))
    assert relay.paths() == ["/v1/health", "/v1/inspect"]
    assert relay.last("/v1/health").json() == reply.json()
    _assert_same(result, _port_value("inspect", relay.last("/v1/inspect").json()))
    assert fake.calls("inspect")[-1].request == {"question_ref": _ref("number")}
    assert fake.protocol_errors() == []


@pytest.mark.parametrize("name", PRESETS)
def test_each_preset_inspects_creates_and_grades_within_the_contract(fake: FakeEngine, name: str) -> None:
    question = _question(name)
    fake.program(name, question)
    inspected = fake.request("inspect", {"question_ref": _ref(name)})
    assert inspected.status == 200
    body = inspected.json()
    assert body["question"]["answers_names"] == [f.name for f in question.fields]
    assert body["question"]["title"] == question.title
    assert body["question"]["content_hash"].startswith("sha256:")
    variant = _variant(fake, name)
    expected_variant = ("unsupported" if name in ("unsupported", "dynamic_unsupported")
                        else "broken" if name == "broken_on_variant" else "ok")
    assert body["outcome"] == ("unsupported" if name == "unsupported" else "supported")
    assert variant["outcome"] == expected_variant
    result: dict[str, Any] | None = None
    if expected_variant == "ok":
        graded = _grade(fake, name, variant["snapshot"], _correct_raw(question))
        assert graded.status == 200
        result = graded.json()
        if name == "broken_on_grade":
            assert result["outcome"] == "broken"
            assert result["reason"]["technical_detail"] == TECHNICAL_DETAIL
        elif name == "server_format_error":
            assert (result["valid"], result["score"], sorted(result["format_errors"])) == (
                False, None, ["verificacao"])
        else:
            assert result["valid"] is True
            assert result["score"] == (1.5 if name == "score_above_one" else 1.0)
            assert result["version_mismatch"] == ({"engine": True, "pl": True} if name == "version_mismatch"
                                                  else {"engine": False, "pl": False})
            assert result["correct_answers"][SENTINEL_KEY] == ANSWER_SENTINEL
    elif expected_variant == "broken":
        assert variant["reason"]["technical_detail"] == TECHNICAL_DETAIL
    assert fake.protocol_errors() == []

    # Paridade: cada resposta do dublê, pelo adaptador real, vira o valor da porta que a tabela manda (sem o
    # `technical_detail`), e os pedidos do adaptador são aceitos pelo dublê.
    with _adapter(fake) as (engine, relay):
        _assert_same(engine.inspect(_port_ref(name)), _port_value("inspect", body))
        port_variant = engine.create_variant(_port_ref(name), "1abc")
        _assert_same(port_variant, _port_value("create_variant", variant))
        if result is not None:
            assert isinstance(port_variant, Variant)
            port_result = engine.grade(_port_ref(name), port_variant.snapshot, _correct_raw(question))
            _assert_same(port_result, _port_value("grade", result))
    assert relay.paths() == ["/v1/health", "/v1/inspect", "/v1/variants"] + (
        ["/v1/grade"] if result is not None else [])
    assert fake.protocol_errors() == []


def test_media_presets_and_canary_texts(fake: FakeEngine) -> None:
    fake.program_presets()
    fake.program("canary", preset("canary", identity=CANARY_IDENTITY, hint="Fulana 99001122"))
    ok = _variant(fake, "with_media")
    assert [(m["id"], m["status"], m["size_bytes"]) for m in ok["ir"]["media"]] == [("m1", "ok", len(png_bytes()))]
    assert '<img src="cq-media:m1" alt="Figura">' in ok["ir"]["statement"]["html"]
    missing = _variant(fake, "missing_media")
    assert [(m["status"], "data_base64" in m) for m in missing["ir"]["media"]] == [("missing", False)]
    assert [w["code"] for w in missing["warnings"]] == ["missing_media"]
    error = _variant(fake, "media_error")
    assert [(m["origin"], m["status"]) for m in error["ir"]["media"]] == [("dynamic", "missing")]
    assert [(w["code"], w["phase"]) for w in error["warnings"]] == [("media_error", "file")]
    canary = _variant(fake, "canary")
    assert CANARY_IDENTITY in canary["ir"]["statement"]["html"]
    assert CANARY_IDENTITY in canary["ir"]["fields"][0]["label_html"]
    assert canary["ir"]["fields"][0]["hint"] == "Fulana 99001122"
    assert [w["message"] for w in canary["warnings"]] == [CANARY_IDENTITY]
    inspected = fake.request("inspect", {"question_ref": _ref("canary")}).json()
    assert CANARY_IDENTITY in inspected["question"]["title"]
    assert len(png_bytes(10_000_001)) == 10_000_001
    with pytest.raises(ValueError):
        preset("canary")
    assert fake.protocol_errors() == []

    # Paridade: a mídia, os avisos (com a fase) e os textos canário chegam à porta intactos.
    with _adapter(fake) as (engine, _):
        for qid, expected in (("with_media", ok), ("missing_media", missing), ("media_error", error),
                              ("canary", canary)):
            _assert_same(engine.create_variant(_port_ref(qid), "1abc"), _port_value("create_variant", expected))
        _assert_same(engine.inspect(_port_ref("canary")), _port_value("inspect", inspected))
    assert fake.protocol_errors() == []


def test_answer_sentinel_only_in_the_answer_key_and_configurable(fake: FakeEngine, root: pathlib.Path) -> None:
    fake.program_presets()
    variant = _variant(fake, "multipart")
    ir = variant["ir"]
    assert variant["snapshot"]["correct_answers"][SENTINEL_KEY] == ANSWER_SENTINEL
    assert all(ANSWER_SENTINEL in entry["display_html"] for entry in ir["answer_key"])
    assert ANSWER_SENTINEL in ir["explanation"]["html"]
    shown = json.dumps([ir["statement"], ir["fields"]], ensure_ascii=False)
    assert ANSWER_SENTINEL not in shown
    hidden = _variant(fake, "no_show_correct_answer")["ir"]
    assert [e["can_show"] for e in hidden["answer_key"]] == [False]
    assert hidden["explanation"]["can_show"] is False
    # Paridade: a IR e o snapshot chegam à porta intactos, com a sentinela só no gabarito.
    with _adapter(fake) as (engine, _):
        port = engine.create_variant(_port_ref("multipart"), "1abc")
    _assert_same(port, _port_value("create_variant", variant))
    assert isinstance(port, Variant) and port.ir == ir
    fake.reset(repos_root=root, answer_sentinel="OUTRA-SENTINELA")
    fake.program_presets()
    assert _variant(fake, "number")["snapshot"]["correct_answers"][SENTINEL_KEY] == "OUTRA-SENTINELA"
    with _adapter(fake) as (engine, _):
        other = engine.create_variant(_port_ref("number"), "1abc")
    assert isinstance(other, Variant) and other.snapshot["correct_answers"][SENTINEL_KEY] == "OUTRA-SENTINELA"
    assert fake.protocol_errors() == []


def test_variant_is_deterministic_by_seed(fake: FakeEngine) -> None:
    fake.program_presets()
    first = _variant(fake, "number", "1abc")
    again = _variant(fake, "number", "1abc")
    other = _variant(fake, "number", "zz")
    assert first == again
    assert first["snapshot"]["params"]["variante"] == int("1abc", 36)
    assert other["snapshot"]["params"] != first["snapshot"]["params"]
    assert [(v.qid, v.seed) for v in fake.issued_variants()] == [("number", "1abc"), ("number", "zz")]
    assert fake.protocol_errors() == []
    # Paridade: a mesma seed pelo adaptador dá a mesma variante (sem acusação de determinismo) e não
    # cria variante nova no dublê.
    with _adapter(fake) as (engine, _):
        port = engine.create_variant(_port_ref("number"), "1abc")
        assert engine.create_variant(_port_ref("number"), "1abc") == port
    _assert_same(port, _port_value("create_variant", first))
    assert [(v.qid, v.seed) for v in fake.issued_variants()] == [("number", "1abc"), ("number", "zz")]
    assert fake.protocol_errors() == []


def test_nondeterministic_program_is_flagged(fake: FakeEngine) -> None:
    fake.program("number", preset("number"))
    _variant(fake, "number")
    fake.program("number", preset("number", statement="Outro enunciado."))
    reprogrammed = _variant(fake, "number")
    assert fake.protocol_errors() == []  # reprogramar é declarado: a memória recomeça
    # Paridade: o adaptador não guarda variante; a reprogramação chega à porta.
    with _adapter(fake) as (engine, _):
        port = engine.create_variant(_port_ref("number"), "1abc")
    _assert_same(port, _port_value("create_variant", reprogrammed))
    assert isinstance(port, Variant) and "Outro enunciado." in port.ir["statement"]["html"]
    assert fake.protocol_errors() == []
    # Controle positivo: a mesma entrada com outra saída, sem reprogramar, é acusada.
    with fake._cond:
        key = next(iter(fake._memo))
        fake._memo[key] = "{}"
    _variant(fake, "number")
    assert fake.protocol_errors() == ["create_variant: resposta diferente para o mesmo pedido (determinismo)"]


# ---------------------------------------------------------------------------------------------
# Existência em disco
# ---------------------------------------------------------------------------------------------


def test_not_found_without_the_tree_in_disk(fake: FakeEngine, root: pathlib.Path) -> None:
    """Existência em disco; paridade: cada 404 `not_found` do dublê é `EngineNotFound` na porta, sem a mensagem do
    motor na exceção."""
    fake.program_presets()
    cases = [
        (_ref("number", repo="https://repos.cq.test/outro.git"), NOT_FOUND_REPOSITORY),
        (_ref("number", commit="f" * 40), NOT_FOUND_COMMIT),
        (_ref("inexistente"), NOT_FOUND_QUESTION),
    ]
    (root / repo_key(REPO) / COMMIT / "questions" / "number" / "info.json").unlink()
    cases.append((_ref("number"), NOT_FOUND_INFO_JSON))
    with _adapter(fake) as (engine, relay):
        for ref, message in cases:
            reply = fake.request("inspect", {"question_ref": ref})
            assert (reply.status, reply.json()) == (404, {"error": {"code": "not_found", "message": message}})
            exc = _raises(PORT_ERROR["not_found"], lambda ref=ref: engine.inspect(QuestionRef(**ref)))
            assert relay.last("/v1/inspect").json() == reply.json()
            assert message not in str(exc) and message not in repr(exc)
        shutil.rmtree(root / repo_key(REPO))
        reply = fake.request("create_variant", {"question_ref": _ref("text"), "seed": "1"})
        assert reply.status == 404
        _raises(PORT_ERROR["not_found"], lambda: engine.create_variant(_port_ref("text"), "1"))
        fake.set_repos_root(None)
        assert fake.request("inspect", {"question_ref": _ref("text")}).status == 404
        _raises(PORT_ERROR["not_found"], lambda: engine.inspect(_port_ref("text")))
    assert fake.protocol_errors() == []


def test_question_in_disk_without_program_is_a_protocol_error(fake: FakeEngine) -> None:
    reply = fake.request("inspect", {"question_ref": _ref("number")})
    assert (reply.status, reply.json()["error"]["code"]) == (500, "internal_error")
    assert fake.protocol_errors() == ["inspect: questão no disco sem programação no motor falso"]
    # Paridade: o 500 `internal_error` é `EngineUnavailable("internal_error")` (repetível).
    with _adapter(fake) as (engine, _):
        _raises(PORT_ERROR["internal_error"], lambda: engine.inspect(_port_ref("number")))
    assert fake.protocol_errors() == ["inspect: questão no disco sem programação no motor falso"] * 2


def test_exact_ref_program_wins_over_qid(fake: FakeEngine) -> None:
    fake.program("number", preset("number"))
    fake.program((REPO, COMMIT, "number"), preset("number", title="Título da referência"))
    assert fake.request("inspect", {"question_ref": _ref("number")}).json()["question"]["title"] == (
        "Título da referência")
    with _adapter(fake) as (engine, _):
        port = engine.inspect(_port_ref("number"))
    assert isinstance(port, Supported) and port.question.title == "Título da referência"
    with pytest.raises(TypeError):
        fake.program(123, preset("number"))
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Estrito: pedidos e respostas contra o esquema
# ---------------------------------------------------------------------------------------------


def test_request_outside_the_schema_is_flagged_and_refused(fake: FakeEngine) -> None:
    fake.program_presets()
    reply = fake.request("inspect", {"question_ref": {**_ref("number"), "commit": "ABC"}})
    assert reply.status == 400
    assert reply.json()["error"]["code"] == "invalid_request"
    assert reply.json()["error"]["message"] == ERROR_MESSAGES["invalid_request"]
    reply = fake.request("create_variant", {"question_ref": _ref("number"), "seed": "1", "extra": 1})
    assert reply.status == 400
    problems = fake.protocol_errors()
    assert len(problems) == 2
    assert problems[0].startswith("inspect: pedido fora do esquema: question_ref/commit")
    assert problems[1].startswith("create_variant: pedido fora do esquema")
    # Paridade: a referência fora do contrato é `EngineRequestRejected("invalid_request")` na porta, seja
    # pela recusa do dublê (400), seja pela checagem local; nada além do mesmo desvio é acusado.
    with _adapter(fake) as (engine, _):
        _raises(PORT_ERROR["invalid_request"],
                lambda: engine.inspect(_port_ref("number", commit="ABC")))
    extra = fake.protocol_errors()[2:]
    assert all(p.startswith("inspect: pedido fora do esquema: question_ref/commit") for p in extra), extra


def test_injected_response_outside_the_schema_is_flagged_unless_declared(fake: FakeEngine) -> None:
    fake.inject("health", body={"status": "ready"})
    assert fake.request("health").json() == {"status": "ready"}
    assert len(fake.protocol_errors()) == 1
    assert fake.protocol_errors()[0].startswith("health: resposta do motor falso fora do contrato")
    fake.inject("health", body={"status": "ready"}, off_contract=True)
    fake.request("health")
    assert len(fake.protocol_errors()) == 1
    fake.fail("inspect", error="internal_error", message="")
    fake.request("inspect", {"question_ref": _ref("number")})
    assert len(fake.protocol_errors()) == 2
    fake.inject("inspect", status=500, body={"error": {"code": "not_found", "message": "x"}})
    fake.request("inspect", {"question_ref": _ref("number")})
    assert fake.protocol_errors()[2].endswith("status 500 incoerente com o código not_found")
    # Paridade (par (status, código) incoerente e envelope fora do contrato → `protocol`): o que o
    # dublê acusa como fora do contrato o adaptador recusa, sem virar valor da porta.
    fake.program_presets()
    with _adapter(fake) as (engine, relay):
        fake.inject("inspect", status=500, body={"error": {"code": "not_found", "message": "x"}})
        _raises((EngineUnavailable, "protocol"), lambda: engine.inspect(_port_ref("number")))
        fake.inject("inspect", body={"outcome": "supported"}, off_contract=True)
        _raises((EngineUnavailable, "protocol"), lambda: engine.inspect(_port_ref("number")))
        assert relay.last("/v1/inspect").json() == {"outcome": "supported"}
    assert len(fake.protocol_errors()) == 4
    assert fake.protocol_errors()[3].endswith("status 500 incoerente com o código not_found")


def test_http_protocol_deviations_are_flagged(fake: FakeEngine) -> None:
    fake.program_presets()
    assert fake.request("/v1/outra", {"x": 1}).status == 404
    reply = fake.request("/v1/grade", http_method="GET")
    assert (reply.status, reply.headers["allow"]) == (405, "POST")
    assert reply.json() == {"error": {"code": "method_not_allowed", "message": ERROR_MESSAGES["method_not_allowed"]}}
    assert fake.request("inspect", {"question_ref": _ref("number")}, query="a=1").status == 200
    assert fake.request("inspect", {"question_ref": _ref("number")}, content_type="text/plain").status == 200
    reply = fake.request("inspect", raw=b"{nao json")
    assert reply.json()["error"]["message"] == "Pedido inválido: o corpo não é JSON válido."
    assert fake.request("inspect", raw=b'{"question_ref": NaN}').status == 400
    big = fake.request("inspect", raw=b"{" + b" " * (2 * 1024 * 1024) + b"}")
    assert (big.status, big.json()["error"]["code"]) == (413, "payload_too_large")
    seed = fake.request("create_variant", {"question_ref": _ref("number"), "seed": "zzzzzzz"})
    assert (seed.status, seed.json()["error"]["message"]) == (
        400, "Seed inválida: o valor em base 36 precisa ser menor que 2^32 (4294967296).")
    expected_problems = [
        "?: rota fora de GET /v1/health e POST /v1/inspect, /v1/variants e /v1/grade",
        "grade: método HTTP GET (esperado POST)",
        "inspect: parâmetros na consulta da URL",
        "inspect: Content-Type text/plain (esperado application/json)",
        "inspect: corpo não é JSON UTF-8 estrito",
        "inspect: corpo não é JSON UTF-8 estrito",
        "inspect: corpo acima de 2 MiB (o cliente deveria recusar antes de enviar)",
        "create_variant: seed ≥ 2^32 (o cliente deveria recusar antes de enviar)",
    ]
    assert fake.protocol_errors() == expected_problems
    # Paridade (checagens locais): o que o dublê acusa como "o cliente deveria recusar antes de
    # enviar" o adaptador recusa sem tocar a rede, com o `kind` do motor; o pedido válido segue sem
    # consulta, com `application/json`, e não é acusado.
    snapshot = _variant(fake, "number")["snapshot"]
    before = len(fake.calls())
    with _adapter(fake) as (engine, relay):
        _raises(PORT_ERROR["invalid_request"], lambda: engine.create_variant(_port_ref("number"), "zzzzzzz"))
        _raises(PORT_ERROR["invalid_request"], lambda: engine.create_variant(_port_ref("number"), "ABC"))
        _raises(PORT_ERROR["payload_too_large"], lambda: engine.grade(
            _port_ref("number"), snapshot, {"resposta": "9" * (2 * 1024 * 1024)}))
        assert relay.exchanges == [] and len(fake.calls()) == before
        assert isinstance(engine.inspect(_port_ref("number")), Supported)
    calls = fake.calls()[before:]
    assert [(c.op, c.http_method, c.has_query) for c in calls] == [("health", "GET", False),
                                                                  ("inspect", "POST", False)]
    assert (calls[1].content_type or "").split(";")[0].strip().lower() == "application/json"
    assert fake.protocol_errors() == expected_problems


def test_schema_validation_uses_both_ids_and_format(fake: FakeEngine) -> None:
    """O validador do dublê e o parse estrito do adaptador concordam: o envelope que o esquema aceita
    vira valor da porta; o que ele recusa é `protocol`."""
    ir = {"ir_version": "1.0", "statement": {"html": ""}, "fields": [], "answer_key": [],
          "explanation": None, "media": []}
    assert any("fields" in e for e in schema_errors("ir", ir))
    accepted = {"outcome": "unsupported", "reasons": [{"code": "workspace", "message": "x"}]}
    assert schema_errors("create_variant_response", accepted) == []
    assert schema_errors("create_variant_response", {"outcome": "ok", "snapshot": {}, "ir": ir,
                                                     "warnings": []}) != []
    refused = [{"outcome": "ok", "snapshot": {}, "warnings": []}, {"outcome": "unsupported"},
               {"outcome": "talvez", "reasons": []}]
    assert all(schema_errors("create_variant_response", body) != [] for body in refused)
    fake.program_presets()
    with _adapter(fake) as (engine, _):
        fake.inject("create_variant", body=accepted)
        assert engine.create_variant(_port_ref("number"), "1abc") == Unsupported(
            reasons=(UnsupportedReason(code="workspace", message="x", elements=()),), question=None)
        for body in refused:
            fake.inject("create_variant", body=body, off_contract=True)
            _raises((EngineUnavailable, "protocol"), lambda: engine.create_variant(_port_ref("number"), "1abc"))
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Resposta crua e textos reais do PL
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "error_text", "score"), [
    # Seleção pelos botões: vazia (campo ausente), uma marcação (str) e 2+ chaves na ordem das opções.
    ({}, "You must select between 1 and 4 options.", None),
    ({"resposta": "a"}, None, 0.5),
    ({"resposta": ["a", "c"]}, None, 1.0),
    # Múltipla digitada, literal:
    # o `pl-checkbox.py@a193f72:726-752` lê a `str` caractere a caractere (`set(submitted_key)`) e conta
    # `len(submitted_key)` no intervalo; a chave inválida é o caractere que não é chave.
    ({"resposta": "ab"}, None, 0.0),
    ({"resposta": "ca"}, None, 1.0),
    ({"resposta": "aa"}, None, 0.5),
    ({"resposta": "abcda"}, "You must select between 1 and 4 options.", None),
    ({"resposta": "z"}, "You selected an invalid option: z", None),
    ({"resposta": "b,a"}, "You selected an invalid option: ,", None),
    ({"resposta": ","}, "You selected an invalid option: ,", None),
    ({"resposta": "a b"}, "You selected an invalid option:", None),
    ({"resposta": "A"}, "You selected an invalid option: A", None),
    ({"resposta": ""}, "You must select between 1 and 4 options.", None),
])
def test_multiple_choice_raw_forms_accepted_by_the_fake(fake: FakeEngine, raw: dict[str, Any],
                                                   error_text: str | None, score: float | None) -> None:
    fake.program_presets()
    variant = _variant(fake, "multiple_choice")
    result = _grade(fake, "multiple_choice", variant["snapshot"], raw).json()
    if error_text is None:
        assert (result["valid"], result["score"]) == (True, score)
    else:
        assert (result["valid"], result["score"]) == (False, None)
        assert result["format_errors"]["resposta"]["text"] == error_text
        if "invalid option" in error_text:
            bad = next(c for c in raw["resposta"] if c not in "abcd")
            assert result["format_errors"]["resposta"] == invalid_option_error(bad)
    # O PL devolve o `submitted_answers` como veio (a `str` não vira lista).
    assert result["submitted_answers"] == raw
    assert fake.protocol_errors() == []
    # Paridade: a resposta crua passa pelo adaptador sem mudança, e a correção chega à porta igual.
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("multiple_choice"), variant["snapshot"], raw)
    _assert_same(port, _port_value("grade", result))
    assert fake.calls("grade")[-1].request["raw_submitted_answers"] == raw
    assert fake.protocol_errors() == []


def test_single_choice_with_nonexistent_key_is_accepted_with_the_pl_error(fake: FakeEngine) -> None:
    fake.program_presets()
    variant = _variant(fake, "single_choice")
    result = _grade(fake, "single_choice", variant["snapshot"], {"resposta": "zz"}).json()
    assert result["format_errors"] == {"resposta": {
        "html": 'Invalid choice: <code class="user-output-invalid">zz</code>', "text": "Invalid choice: zz"}}
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("single_choice"), variant["snapshot"], {"resposta": "zz"})
    assert isinstance(port, Graded) and dict(port.format_errors) == {"resposta": FormatError(
        html='Invalid choice: <code class="user-output-invalid">zz</code>', text="Invalid choice: zz")}
    _assert_same(port, _port_value("grade", result))
    assert fake.protocol_errors() == []


def test_single_choice_absent_is_accepted_with_the_pl_error_or_blank_zero(fake: FakeEngine) -> None:
    """Fidelidade ao PL: o "Enviar" sem marcação da escolha
    única vai ao motor com o campo ausente, como o PL recebe o formulário sem rádio marcado. O dublê aceita
    a forma (nenhum desvio de protocolo, sem `expect_raw_shape(False)`) e corrige como o
    `pl-multiple-choice.py@a193f72`: sem `allow-blank`, "No answer was submitted." (`SINGLE_NO_ANSWER`);
    com ele, nota 0. Este caso confere só o dublê;
    o produto é conferido num caso da escolha única sem marcação em `test_answers_choice.py`."""
    from tests.helpers.engine import SINGLE_NO_ANSWER, single_choice_field

    fake.program_presets()
    variant = _variant(fake, "single_choice")
    result = _grade(fake, "single_choice", variant["snapshot"], {}).json()
    assert (result["valid"], result["score"], result["partial_scores"]) == (False, None, None)
    assert result["format_errors"] == {"resposta": {"html": SINGLE_NO_ANSWER, "text": SINGLE_NO_ANSWER}}
    assert result["submitted_answers"] == {}
    assert fake.protocol_errors() == []
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("single_choice"), variant["snapshot"], {})
    assert isinstance(port, Graded) and dict(port.format_errors) == {
        "resposta": FormatError(html=SINGLE_NO_ANSWER, text=SINGLE_NO_ANSWER)}
    _assert_same(port, _port_value("grade", result))
    assert fake.calls("grade")[-1].request["raw_submitted_answers"] == {}
    assert fake.protocol_errors() == []

    # Com `allow-blank`: nota 0, válida, e o campo continua ausente no `submitted_answers`.
    fake.program("single_choice", preset("single_choice", fields=(single_choice_field(allow_blank=True),)))
    blank = _variant(fake, "single_choice")
    field = blank["ir"]["fields"][0]
    assert (field["type"], field["constraints"]["allow_blank"]) == ("single_choice", True)
    result = _grade(fake, "single_choice", blank["snapshot"], {}).json()
    assert (result["valid"], result["score"], result["format_errors"]) == (True, 0.0, {})
    assert result["partial_scores"] == {"resposta": {"score": 0.0, "weight": 1}}
    assert result["submitted_answers"] == {}
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("single_choice"), blank["snapshot"], {})
    assert isinstance(port, Graded) and (port.valid, port.score) == (True, 0.0)
    _assert_same(port, _port_value("grade", result))
    assert fake.protocol_errors() == []


_NOT_A_KEY_LIST = "lista com token que não é chave (a múltipla digitada vai como str literal)"


@pytest.mark.parametrize(("qid", "raw", "problem"), [
    ("multiple_choice", {"resposta": ["a"]}, "uma só chave vai como str, não lista"),
    ("multiple_choice", {"resposta": ["c", "a"]}, "chaves fora da ordem das opções"),
    ("multiple_choice", {"resposta": ["a", "a"]}, "chaves repetidas"),
    # A lista com token que não é chave é recusada: a múltipla digitada vai
    # como `str` literal.
    ("multiple_choice", {"resposta": ["a", "z"]}, _NOT_A_KEY_LIST),
    ("multiple_choice", {"resposta": ["ab"]}, _NOT_A_KEY_LIST),
    ("multiple_choice", {"resposta": ["b", ",", "a"]}, _NOT_A_KEY_LIST),
    ("multiple_choice", {"resposta": []}, "esperado ausente, str ou lista de str"),
    ("number", {}, "esperado presente e str"),
    # A única ausente (o "Enviar" sem marcação) é
    # forma aceita (`test_single_choice_absent_is_accepted_with_the_pl_error_or_blank_zero`); presente, só
    # `str`: o `null` (o `None` das `parts` que o `raw_for_engine` pula) e a lista são desvios.
    ("single_choice", {"resposta": None}, "esperado ausente ou str"),
    ("single_choice", {"resposta": ["b"]}, "esperado ausente ou str"),
    ("number", {"resposta": "6.5", "outro": "1"}, "1 campo(s) fora da IR"),
])
def test_raw_forms_refused_by_the_fake_are_flagged(fake: FakeEngine, qid: str, raw: dict[str, Any],
                                              problem: str) -> None:
    fake.program_presets()
    variant = _variant(fake, qid)
    assert _grade(fake, qid, variant["snapshot"], raw).status == 200
    problems = fake.protocol_errors()
    assert len(problems) == 1
    assert problems[0].startswith("grade: resposta crua fora das formas do raw_for_engine: ")
    assert problems[0].endswith(problem)
    fake.expect_raw_shape(False)
    _grade(fake, qid, variant["snapshot"], raw)
    assert len(fake.protocol_errors()) == 1
    # Paridade (a entrada chega ao motor sem mudança; a forma é do `raw_for_engine`, não do
    # adaptador): o adaptador repassa a resposta crua exatamente, e o dublê acusa o mesmo desvio.
    fake.expect_raw_shape(True)
    with _adapter(fake) as (engine, _):
        assert isinstance(engine.grade(_port_ref(qid), variant["snapshot"], raw), Graded)
    assert fake.calls("grade")[-1].request["raw_submitted_answers"] == raw
    assert fake.protocol_errors() == [problems[0], problems[0]]


def test_format_errors_are_the_real_pl_texts(fake: FakeEngine) -> None:
    fake.program_presets()
    number = _variant(fake, "number")["snapshot"]
    integer = _variant(fake, "integer")["snapshot"]
    text = _variant(fake, "text")["snapshot"]
    cases = [
        ("number", number, {"resposta": "3,14"}, NUMBER_INVALID_TEXT),
        ("number", number, {"resposta": ""}, "The submitted answer was blank."),
        ("number", number, {"resposta": "1/0"}, "Your expression resulted in a division by zero."),
        ("integer", integer, {"resposta": "4.2"}, INTEGER_INVALID_TEXT),
        ("integer", integer, {"resposta": " "}, "Invalid format: the submitted answer was blank."),
        ("text", text, {"resposta": ""}, TEXT_BLANK),
    ]
    with _adapter(fake) as (engine, _):
        for qid, snapshot, raw, expected in cases:
            result = _grade(fake, qid, snapshot, raw).json()
            assert (result["valid"], result["score"], result["partial_scores"]) == (False, None, None)
            assert result["format_errors"]["resposta"]["text"] == expected
            port = engine.grade(_port_ref(qid), snapshot, raw)
            assert isinstance(port, Graded)
            assert (port.valid, port.score, port.partial_scores) == (False, None, None)
            assert port.format_errors["resposta"].text == expected
            _assert_same(port, _port_value("grade", result))
    result = _grade(fake, "number", number, {"resposta": "13/2"}).json()
    assert (result["valid"], result["score"]) == (True, 1.0)
    assert fake.protocol_errors() == []


def test_partial_score_and_submitted_answers_as_the_pl_leaves_them(fake: FakeEngine) -> None:
    fake.program_presets()
    variant = _variant(fake, "multipart")
    raw = {"num": "6.5", "int": "31", "txt": "azul", "mc": "a", "cb": ["a", "b"]}
    result = _grade(fake, "multipart", variant["snapshot"], raw).json()
    assert result["valid"] is True
    assert result["partial_scores"] == {"num": {"score": 1.0, "weight": 1}, "int": {"score": 1.0, "weight": 1},
                                        "txt": {"score": 1.0, "weight": 1}, "mc": {"score": 0.0, "weight": 1},
                                        "cb": {"score": 0.0, "weight": 1}}
    assert result["score"] == pytest.approx(0.6)
    assert result["submitted_answers"] == {"num": 6.5, "int": 31, "txt": "azul", "mc": "a", "cb": ["a", "b"]}
    assert result["params"] == variant["snapshot"]["params"]
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("multipart"), variant["snapshot"], raw)
    _assert_same(port, _port_value("grade", result))
    assert fake.protocol_errors() == []


def test_snapshot_not_issued_is_flagged_and_foreign_snapshots_compare_versions(fake: FakeEngine) -> None:
    fake.program_presets()
    snapshot = _variant(fake, "number")["snapshot"]
    foreign = {**snapshot, "engine_version": "0.9.0", "pl_commit": "b" * 40, "extra": 1}
    result = _grade(fake, "number", foreign, {"resposta": "6.5"}).json()
    assert result["version_mismatch"] == {"engine": True, "pl": True}
    assert fake.protocol_errors() == ["grade: snapshot que o motor falso não emitiu para esta referência"]
    fake.accept_foreign_snapshots()
    _grade(fake, "number", foreign, {"resposta": "6.5"})
    assert len(fake.protocol_errors()) == 1
    # Paridade: o snapshot vai ao motor intacto (inclusive a chave desconhecida), e a divergência
    # de versões chega à porta.
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("number"), foreign, {"resposta": "6.5"})
    assert isinstance(port, Graded) and port.version_mismatch == VersionMismatch(engine=True, pl=True)
    assert fake.calls("grade")[-1].request["snapshot"] == foreign
    assert len(fake.protocol_errors()) == 1


def test_version_mismatch_by_versions_and_by_injection(fake: FakeEngine) -> None:
    fake.program_presets()
    snapshot = _variant(fake, "number")["snapshot"]
    fake.set_versions(engine_version="1.1.0")
    assert fake.request("health").json()["engine_version"] == "1.1.0"
    assert _grade(fake, "number", snapshot, {"resposta": "6.5"}).json()["version_mismatch"] == {
        "engine": True, "pl": False}
    # Paridade: a `engine_version` do `health` não entra na compatibilidade; a divergência vem
    # na correção.
    with _adapter(fake) as (engine, _):
        port = engine.grade(_port_ref("number"), snapshot, {"resposta": "6.5"})
        assert isinstance(port, Graded) and port.version_mismatch == VersionMismatch(engine=True, pl=False)
        fake.set_versions(engine_version="1.0.0")
        fake.inject_version_mismatch(engine=False, pl=True, times=1)
        assert _grade(fake, "number", snapshot, {"resposta": "6.5"}).json()["version_mismatch"] == {
            "engine": False, "pl": True}
        fake.inject_version_mismatch(engine=False, pl=True, times=1)
        port = engine.grade(_port_ref("number"), snapshot, {"resposta": "6.5"})
        assert isinstance(port, Graded) and port.version_mismatch == VersionMismatch(engine=False, pl=True)
    assert _grade(fake, "number", snapshot, {"resposta": "6.5"}).json()["version_mismatch"] == {
        "engine": False, "pl": False}
    assert fake.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Falhas injetáveis
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [*ERROR_STATUS, 400, 404, 413, 500])
def test_fail_answers_each_contract_error(fake: FakeEngine, error: str | int) -> None:
    fake.program_presets()
    fake.fail("grade", error=error, times=1)
    snapshot = _variant(fake, "number")["snapshot"]
    reply = _grade(fake, "number", snapshot, {"resposta": "6.5"})
    code = error if isinstance(error, str) else {400: "invalid_request", 404: "not_found",
                                                 413: "payload_too_large", 500: "internal_error"}[error]
    assert (reply.status, reply.json()) == (ERROR_STATUS[code], {"error": {"code": code,
                                                                           "message": ERROR_MESSAGES[code]}})
    assert _grade(fake, "number", snapshot, {"resposta": "6.5"}).status == 200
    assert [c.programmed for c in fake.calls("grade")] == [True, False]
    assert fake.protocol_errors() == []
    # Paridade (a classificação de erros, caso a caso): o erro programado vira a exceção da porta com o `kind`, sem a
    # mensagem do motor, numa única chamada (nunca repete); a seguinte volta a ser a correção.
    fake.fail("grade", error=error, times=1)
    with _adapter(fake) as (engine, _):
        exc = _raises(PORT_ERROR[code], lambda: engine.grade(_port_ref("number"), snapshot, {"resposta": "6.5"}))
        assert ERROR_MESSAGES[code] not in str(exc)
        assert [c.programmed for c in fake.calls("grade")] == [True, False, True]
        assert isinstance(engine.grade(_port_ref("number"), snapshot, {"resposta": "6.5"}), Graded)
    assert fake.protocol_errors() == []


def test_fail_times_when_and_clear(fake: FakeEngine) -> None:
    fake.program_presets()
    fake.fail("create_variant", error="internal_error", times=None,
              when=lambda call: call.request["question_ref"]["qid"] == "text")
    assert fake.request("create_variant", {"question_ref": _ref("text"), "seed": "1"}).status == 500
    assert fake.request("create_variant", {"question_ref": _ref("text"), "seed": "1"}).status == 500
    assert fake.request("create_variant", {"question_ref": _ref("number"), "seed": "1"}).status == 200
    # Paridade: a falha condicional só atinge a referência escolhida, também pelo adaptador.
    with _adapter(fake) as (engine, _):
        _raises(PORT_ERROR["internal_error"], lambda: engine.create_variant(_port_ref("text"), "1"))
        assert isinstance(engine.create_variant(_port_ref("number"), "1"), Variant)
    fake.clear_failures()
    assert fake.request("create_variant", {"question_ref": _ref("text"), "seed": "1"}).status == 200
    with pytest.raises(ValueError):
        fake.fail("outra", error="internal_error")
    with pytest.raises(ValueError):
        fake.fail("grade", error="teapot")
    with pytest.raises(ValueError):
        fake.fail_network("grade", kind="teapot")
    assert fake.protocol_errors() == []


def test_network_failures(fake: FakeEngine) -> None:
    fake.fail_network("health", kind="reset")
    with pytest.raises((ConnectionError, http.client.HTTPException, OSError)):
        fake.request("health")
    fake.fail_network("health", kind="not_json")
    reply = fake.request("health")
    assert (reply.status, reply.body) == (200, b"isto nao e JSON")
    fake.fail_network("health", kind="html_502")
    reply = fake.request("health")
    assert (reply.status, reply.headers["content-type"]) == (502, "text/html")
    fake.fail_network("health", kind="nan")
    assert b"NaN" in fake.request("health").body
    fake.fail_network("health", kind="redirect")
    reply = fake.request("health")
    assert (reply.status, reply.headers["location"]) == (307, f"http://127.0.0.1:{fake.port}/v1/health")
    fake.fail_network("health", kind="truncated")
    with pytest.raises(http.client.IncompleteRead):
        fake.request("health")
    for kind in ("oversize", "oversize_chunked"):
        fake.fail_network("health", kind=kind)
        conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        if kind == "oversize":
            assert int(response.headers["Content-Length"]) == MAX_RESPONSE_BYTES + 1
        else:
            assert response.headers["Transfer-Encoding"] == "chunked"
        assert len(response.read(1 << 20)) == 1 << 20
        conn.close()
    assert [c.outcome for c in fake.calls()] == ["reset", *["network"] * 7]
    assert set(NETWORK_KINDS) == {"reset", "hang", "not_json", "html_502", "truncated", "oversize",
                                  "oversize_chunked", "redirect", "nan"}
    assert fake.protocol_errors() == []
    # Paridade (a classificação de erros, pelo loopback real): reset → `connection`; não JSON, `NaN`, 502 de proxy, 3xx (não
    # seguido) e acima de 64 MiB → `protocol`; uma chamada por caso, sem a operação (o `health` falhou).
    # O `truncated` fica de fora: a classificação de erros não o inclui.
    fake.program_presets()
    expected = {"reset": "connection", "not_json": "protocol", "html_502": "protocol", "nan": "protocol",
                "redirect": "protocol", "oversize": "protocol", "oversize_chunked": "protocol"}
    for kind, port_kind in expected.items():
        before = len(fake.calls())
        fake.fail_network("health", kind=kind)
        with _adapter(fake, relay=False) as (engine, _):
            _raises((EngineUnavailable, port_kind), lambda: engine.inspect(_port_ref("number")))
        assert [c.op for c in fake.calls()[before:]] == ["health"], kind
    assert fake.protocol_errors() == []


def test_hang_until_release_and_client_gone(fake: FakeEngine) -> None:
    fake.fail_network("health", kind="hang", times=None)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            fake.request("health", timeout=10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    fake.wait_for(lambda: fake.calls("health"))
    time.sleep(0.2)
    assert thread.is_alive()
    fake.release_hangs()
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    started = monotonic()
    with pytest.raises((TimeoutError, OSError)):
        fake.request("health", timeout=0.3)
    fake.wait_for(lambda: fake.calls("health")[-1].outcome == "hang", timeout=2)
    assert monotonic() - started < 2
    assert fake.protocol_errors() == []
    # Paridade (prazo total pelo loopback real): o adaptador desiste no prazo com `timeout`, e o
    # dublê vê o cliente ir embora.
    before = len(fake.calls("health"))
    with _adapter(fake, timeout=0.5, relay=False) as (engine, _):
        started = monotonic()
        _raises((EngineUnavailable, "timeout"), lambda: engine.inspect(_port_ref("number")))
        assert monotonic() - started <= 0.5 + DEADLINE_SLACK
    fake.wait_for(lambda: len(fake.calls("health")) == before + 1
                  and fake.calls("health")[-1].outcome == "hang", timeout=2)
    assert fake.protocol_errors() == []


def test_slow_delays_and_drip(fake: FakeEngine) -> None:
    fake.slow("health", 0.4)
    started = monotonic()
    assert fake.request("health").status == 200
    assert monotonic() - started >= 0.4
    fake.slow("health", 0.4, drip=True)
    started = monotonic()
    assert fake.request("health").json()["status"] == "ready"
    assert monotonic() - started >= 0.3
    assert [c.programmed for c in fake.calls()] == [True, True]
    assert fake.protocol_errors() == []
    # Paridade: a resposta que goteja além do prazo é `timeout` no prazo total, e a lenta dentro
    # do prazo é aceita.
    fake.program_presets()
    with _adapter(fake, timeout=0.3, relay=False) as (engine, _):
        fake.slow("health", 1.5, drip=True)
        started = monotonic()
        _raises((EngineUnavailable, "timeout"), lambda: engine.inspect(_port_ref("number")))
        assert monotonic() - started <= 0.3 + DEADLINE_SLACK
    with _adapter(fake, relay=False) as (engine, _):
        fake.slow("health", 0.2)
        assert isinstance(engine.inspect(_port_ref("number")), Supported)
    assert fake.protocol_errors() == []


def test_set_health_500_and_other_contract_versions(fake: FakeEngine) -> None:
    fake.set_health(status=500)
    reply = fake.request("health")
    assert (reply.status, reply.json()["error"]["code"]) == (500, "internal_error")
    fake.set_health(contract_version="2.0", ir_version="2.0")
    assert fake.request("health").json()["contract_version"] == "2.0"
    fake.set_health()
    assert fake.request("health").json()["contract_version"] == "1.0"
    assert fake.protocol_errors() == []
    # Paridade (compatibilidade): `health` 500 → `not_ready`; outro contrato ou outra IR →
    # `incompatible`, sem chamar a operação; o cache se invalida a cada falha.
    fake.program_presets()
    with _adapter(fake) as (engine, relay):
        fake.set_health(status=500)
        _raises((EngineUnavailable, "not_ready"), lambda: engine.inspect(_port_ref("number")))
        fake.set_health(contract_version="2.0")
        _raises((EngineUnavailable, "incompatible"), lambda: engine.inspect(_port_ref("number")))
        fake.set_health(ir_version="2.0")
        _raises((EngineUnavailable, "incompatible"), lambda: engine.inspect(_port_ref("number")))
        assert relay.paths() == ["/v1/health"] * 3
        fake.set_health()
        assert isinstance(engine.inspect(_port_ref("number")), Supported)
        assert relay.paths() == ["/v1/health"] * 4 + ["/v1/inspect"]
    assert fake.protocol_errors() == []


def test_stop_refuses_connections_and_start_reuses_the_port(root: pathlib.Path) -> None:
    fake = FakeEngine(repos_root=root)
    try:
        port = fake.port
        assert fake.request("health").status == 200
        fake.stop()
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=10).close()  # Windows: ~2 s de novas tentativas
        # Paridade (pelo loopback real): o motor parado é `connection`; de volta na mesma porta, a
        # chamada seguinte do mesmo adaptador confere o `health` de novo e segue.
        with _adapter(fake, relay=False) as (engine, _):
            _raises((EngineUnavailable, "connection"), lambda: engine.inspect(_port_ref("number")))
            fake.start()
            assert fake.port == port
            assert fake.request("health").status == 200
            fake.program("number", preset("number"))
            assert isinstance(engine.inspect(_port_ref("number")), Supported)
        assert fake.protocol_errors() == []
        fake.stop()
        fake.reset(repos_root=root)
        assert not fake.stopped and fake.request("health").status == 200
    finally:
        fake.close()


def test_calls_record_bodies_and_connections(fake: FakeEngine) -> None:
    fake.program_presets()
    fake.request("inspect", {"question_ref": _ref("number")})
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    for _ in range(2):
        conn.request("GET", "/v1/health")
        conn.getresponse().read()
    conn.close()
    calls = fake.calls()
    assert [(c.op, c.status, c.outcome) for c in calls] == [("inspect", 200, "ok"), ("health", 200, "ok"),
                                                            ("health", 200, "ok")]
    assert calls[0].request == {"question_ref": _ref("number")}
    assert calls[0].response["outcome"] == "supported"
    assert calls[1].connection_id == calls[2].connection_id != calls[0].connection_id
    assert fake.connection_count() == 2
    assert all(c.ended is not None and c.ended >= c.started for c in calls)
    assert fake.protocol_errors() == []
    # Paridade (pelo loopback real: sem keep-alive, uma conexão nova por chamada; `health` em
    # cache): o dublê registra o corpo exato de cada pedido do adaptador, cada um numa conexão própria.
    with _adapter(fake, relay=False) as (engine, _):
        engine.inspect(_port_ref("number"))
        engine.inspect(_port_ref("number"))
    mine = fake.calls()[3:]
    assert [c.op for c in mine] == ["health", "inspect", "inspect"]
    assert [c.request for c in mine[1:]] == [{"question_ref": _ref("number")}] * 2
    assert len({c.connection_id for c in mine}) == 3
    assert fake.connection_count() == 5
    assert fake.protocol_errors() == []


def test_reset_forgets_everything(fake: FakeEngine, root: pathlib.Path) -> None:
    fake.program_presets()
    _variant(fake, "number")
    fake.fail("health", error="internal_error", times=None)
    fake.request("/v1/x")
    fake.reset(repos_root=root)
    assert (fake.calls(), fake.issued_variants(), fake.protocol_errors()) == ([], [], [])
    assert fake.request("health").status == 200
    assert fake.request("inspect", {"question_ref": _ref("number")}).status == 500
    # Paridade: a programação esquecida pelo `reset` dá `internal_error` na porta (repetível), e a
    # falha esquecida não atinge o `health` do adaptador.
    with _adapter(fake) as (engine, relay):
        _raises(PORT_ERROR["internal_error"], lambda: engine.inspect(_port_ref("number")))
    assert [reply.status for _, reply in relay.exchanges] == [200, 500]
    fake.reset(repos_root=root)


def test_silence_on_stdout_and_stderr(fake: FakeEngine, capfd: pytest.CaptureFixture[str]) -> None:
    fake.program_presets()
    variant = _variant(fake, "broken_on_grade")
    _grade(fake, "broken_on_grade", variant["snapshot"], {"resposta": "6.5"})
    fake.request("/v1/x")
    fake.fail_network("health", kind="reset")
    with pytest.raises((ConnectionError, http.client.HTTPException, OSError)):
        fake.request("health")
    _raw_request(fake, b"GARBAGE\r\n\r\n")
    # Paridade: o adaptador também não escreve nada, nem na
    # questão quebrada nem na falha de rede.
    with _adapter(fake) as (engine, _):
        broken = engine.grade(_port_ref("broken_on_grade"), variant["snapshot"], {"resposta": "6.5"})
    assert isinstance(broken, Broken) and TECHNICAL_DETAIL_SENTINEL not in repr(broken)
    fake.fail_network("health", kind="reset")
    with _adapter(fake, relay=False) as (engine, _):
        _raises((EngineUnavailable, "connection"), lambda: engine.inspect(_port_ref("number")))
    fake.reset()
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_custom_question_fields(fake: FakeEngine) -> None:
    q = FakeQuestion("T", "E", (FakeField("x", "integer", 42, base=16),
                                FakeField("y", "single_choice", "k", options=(FakeOption("k", "K"),))))
    snapshot, ir, _ = engine_module.build_variant(q, "1")
    assert schema_errors("create_variant_response", {"outcome": "ok", "snapshot": snapshot, "ir": ir,
                                                     "warnings": []}) == []
    assert ir["fields"][0]["hint"] == "Número inteiro na base 16; ex.: 2A ou -11"
    assert engine_module.grade_answers(q, snapshot, {"x": "2a", "y": "k"})["score"] == 1.0
    # Paridade: a questão programada à mão chega à porta com a IR e a correção do construtor.
    fake.program((REPO, COMMIT, "integer"), q)
    with _adapter(fake) as (engine, _):
        port = engine.create_variant(_port_ref("integer"), "1")
        assert isinstance(port, Variant) and port.ir == ir and port.snapshot == snapshot
        graded = engine.grade(_port_ref("integer"), port.snapshot, {"x": "2a", "y": "k"})
    assert isinstance(graded, Graded) and graded.score == 1.0
    assert fake.protocol_errors() == []
