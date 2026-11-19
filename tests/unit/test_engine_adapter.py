"""Adaptador HTTP do motor contra o motor falso estrito.


O `HttpQuestionEngine` real fala HTTP com o `engine_fake` (`tests/helpers/engine.py`), que valida cada
pedido e cada resposta contra `contracts/engine/v1/` e exige `protocol_errors() == []` no fim. Cobre:
- a classificação de erros do adaptador, caso a caso: 200 `supported`/`ok`/`unsupported`/`broken`
  → os valores da porta; 404 `not_found` → `EngineNotFound`; 400 `invalid_request` e 413
  `payload_too_large` → `EngineRequestRejected(kind)`; 404 `route_not_found` e 405 `method_not_allowed`
  → `EngineUnavailable("incompatible")`; 500 `internal_error` → `EngineUnavailable("internal_error")`;
  `/v1/health` 500 → `not_ready`; conexão recusada e reset → `connection`; prazo total → `timeout`; não
  JSON, `NaN`, fora do contrato, par (status, código) incoerente, 3xx, outro status e acima de 64 MiB →
  `protocol`; `health` com `contract_version` ou `ir_version` ≠ `"1.0"` → `incompatible` sem chamar a
  operação;
- o prazo total (`slow`, `slow` gotejando e `hang`) em até prazo + 0,5 s reais;
- 3xx não seguido e nenhuma repetição (uma chamada por operação);
- `HTTP(S)_PROXY` do ambiente ignorado; uma conexão nova por chamada;
- as checagens locais sem rede (seed fora de `^[0-9a-z]{1,7}$` ou ≥ 2³², corpo > 2 MiB);
- o `/v1/health` em cache de 60 s, invalidado em `EngineUnavailable`;
- o `technical_detail` ausente de todo atributo, `repr` e exceção (controle positivo: o motor falso o
  envia); exceções sem corpo, URL, QID nem resposta do aluno (controle positivo: o motor falso recebeu
  e devolveu a resposta canário);
- nenhum log, stdout nem stderr;
- `repr(HttpQuestionEngine)` só com `host:porta` e a hierarquia de `EngineError`, na mesma função de um
  caso de classificação.

A falha de DNS (classificada junto da conexão recusada e do reset) não é exercitada: resolver um nome inexistente
consultaria o resolvedor do sistema, fora do loopback. O corpo truncado (`truncated`) também fica de fora:
a classificação de erros do adaptador não o inclui.

No esqueleto, `inspect`, `create_variant`, `grade` e `close` lançam `NotImplementedError`: todos os casos
falham por esse motivo.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import pathlib
import shutil
import time
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from cq_server.adapters import engine as adapter_module
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import EngineSettings
from cq_server.ports.engine import (
    NOT_FOUND_KINDS,
    REJECTED_KINDS,
    UNAVAILABLE_KINDS,
    Broken,
    EngineError,
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
from tests.helpers.engine import (
    GRADE_BROKEN,
    TECHNICAL_DETAIL,
    TECHNICAL_DETAIL_SENTINEL,
    VARIANT_BROKEN,
    FakeMedia,
    FakeReason,
    FakeWarning,
    build_variant,
    canonical,
    monotonic,
    png_bytes,
    preset,
    repo_key,
)
from tests.helpers.logs import capture_logs

COURSE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "course"
REPO = "https://repos.cq.test/curso-teste.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SEED = "1abc"
TIMEOUT = 10.0  # o prazo dos casos que não medem o prazo (folga no Windows)
DEADLINE = 1.0  # o prazo dos casos que medem o prazo total
DEADLINE_SLACK = 0.5  # a folga do prazo total (≤ prazo + 0,5 s reais)

# A resposta canário do aluno e um QID canário (nunca numa exceção, num log ou em fd 1 e 2).
CANARY_ANSWER = "resposta-canario-aluno-5E71"
CANARY_QID = "questao-canario-qid-2b9d"
CANARY_MESSAGE = f"Mensagem do motor com {CANARY_ANSWER}."

# Os metadados válidos de uma questão (`$defs.question_metadata`), para as respostas injetadas.
META = {"uuid": None, "title": "Injetada", "topic": "Teste", "tags": ["teste"], "type": "v3",
        "grading_method": "Internal", "elements": ["pl-number-input"], "show_correct_answer": True,
        "single_variant": False, "partial_credit": True, "answers_names": ["resposta"],
        "content_hash": "sha256:" + "0" * 64}


def _ref(qid: str = "number") -> QuestionRef:
    return QuestionRef(repo=REPO, commit=COMMIT, qid=qid)


def _question_ref(qid: str = "number") -> dict[str, str]:
    return {"repo": REPO, "commit": COMMIT, "qid": qid}


@pytest.fixture
def fake(engine_fake: Any) -> Any:
    """O `engine_fake` com o curso de teste em `<repo_key(REPO)>/<COMMIT>/` e os presets programados."""
    shutil.copytree(COURSE, engine_fake.repos_root / repo_key(REPO) / COMMIT)
    engine_fake.program_presets()
    return engine_fake


@contextlib.contextmanager
def _open(fake: Any, *, timeout: float = TIMEOUT) -> Iterator[HttpQuestionEngine]:
    engine = HttpQuestionEngine(EngineSettings(engine_url=fake.base_url, timeout_seconds=timeout))
    try:
        yield engine
    finally:
        engine.close()


def _ops(fake: Any) -> list[str]:
    return [c.op for c in fake.calls()]


def _info(meta: dict[str, Any]) -> QuestionInfo:
    """O `QuestionInfo` esperado de `$defs.question_metadata` (listas como tuplas)."""
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


def _graded(response: dict[str, Any]) -> Graded:
    return Graded(valid=response["valid"],
                  format_errors={k: FormatError(html=v["html"], text=v["text"])
                                 for k, v in response["format_errors"].items()},
                  submitted_answers=response["submitted_answers"], partial_scores=response["partial_scores"],
                  score=response["score"], feedback=response["feedback"], params=response["params"],
                  correct_answers=response["correct_answers"],
                  version_mismatch=VersionMismatch(**response["version_mismatch"]),
                  warnings=_warnings(response["warnings"]))


def _assert_same_graded(result: Any, response: dict[str, Any]) -> None:
    expected = _graded(response)
    assert type(result) is Graded
    for f in dataclasses.fields(Graded):
        got, want = getattr(result, f.name), getattr(expected, f.name)
        if f.name == "format_errors":
            got = dict(got)
        assert got == want, f.name


def _walk(value: Any, seen: set[int] | None = None) -> Iterator[Any]:
    """Todo valor alcançável de `value`: campos de dataclass, itens de mapas, listas e tuplas."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from _walk(getattr(value, f.name), seen)
    elif isinstance(value, dict) or hasattr(value, "items") and callable(value.items):
        for k, v in value.items():
            yield from _walk(k, seen)
            yield from _walk(v, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk(item, seen)


def _assert_no_technical_detail(result: Any) -> None:
    """Nenhum atributo, valor alcançável, `repr` nem `str` do objeto da porta traz o `technical_detail`."""
    for item in _walk(result):
        assert not hasattr(item, "technical_detail")
        if isinstance(item, str):
            assert TECHNICAL_DETAIL_SENTINEL not in item
            assert "Traceback" not in item
    for text in (repr(result), str(result)):
        assert TECHNICAL_DETAIL_SENTINEL not in text
        assert "Traceback" not in text


# ---------------------------------------------------------------------------------------------
# 200: os desfechos semânticos como valores da porta
# ---------------------------------------------------------------------------------------------


def test_inspect_supported_maps_question_metadata_and_repr_shows_only_host_port(fake: Any) -> None:
    """Inspeção suportada (pedido `POST /v1/inspect` com `application/json`, sem consulta, depois
    do `GET /v1/health`; `repr` só `HttpQuestionEngine(url=<host:porta>)`; 200 `supported` →
    `Supported`): o `QuestionInfo` com todos os campos de `question_metadata` (listas como tuplas);
    o corpo do pedido exato; o `repr` do adaptador, a hierarquia de `EngineError`, os conjuntos de `kind` e
    as constantes do adaptador (reais no esqueleto) afirmados nesta função."""
    with _open(fake) as engine:
        result = engine.inspect(_ref("number"))
        [health, inspect] = fake.calls()
        assert (health.op, health.http_method, health.path, health.has_query, health.request) == (
            "health", "GET", "/v1/health", False, None)
        assert (inspect.op, inspect.http_method, inspect.path, inspect.has_query) == (
            "inspect", "POST", "/v1/inspect", False)
        assert (inspect.content_type or "").split(";")[0].strip().lower() == "application/json"
        assert inspect.request == {"question_ref": _question_ref("number")}
        assert result == Supported(question=_info(inspect.response["question"]))
        assert result.question.title == "Velocidade média"
        assert result.question.answers_names == ("resposta",)
        assert result.question.elements == ("pl-number-input",)
        # `repr` só com host e porta.
        assert repr(engine) == f"HttpQuestionEngine(url=127.0.0.1:{fake.port})"
    other = HttpQuestionEngine(EngineSettings(engine_url="https://motor.example.org:8443/"))
    assert repr(other) == "HttpQuestionEngine(url=motor.example.org:8443)"
    assert "https" not in repr(other)
    # Hierarquia e `kind`.
    assert issubclass(EngineError, Exception)
    for cls in (EngineUnavailable, EngineNotFound, EngineRequestRejected):
        assert issubclass(cls, EngineError)
    assert UNAVAILABLE_KINDS == {"connection", "timeout", "internal_error", "not_ready", "incompatible",
                                 "protocol", "unconfigured"}
    assert NOT_FOUND_KINDS == {"not_found"}
    assert REJECTED_KINDS == {"invalid_request", "payload_too_large"}
    assert EngineNotFound().kind == "not_found"
    assert (adapter_module.MAX_RESPONSE_BYTES, adapter_module.MAX_REQUEST_BYTES,
            adapter_module.HEALTH_CACHE_SECONDS) == (64 * 1024 * 1024, 2 * 1024 * 1024, 60.0)


def test_inspect_unsupported_keeps_reasons_and_question(fake: Any) -> None:
    """Inspeção não suportada (na inspeção, `Unsupported` com a `question`; 200
    `unsupported` → `Unsupported`): o preset `unsupported` → os motivos exatos (com os `elements`) e o
    `QuestionInfo`; um motivo sem `elements` vira a tupla vazia."""
    reason = FakeReason("course_element_override", "O curso sobrescreve um elemento do PL.")
    fake.program("number", preset("number", inspect_reasons=(reason,)))
    with _open(fake) as engine:
        result = engine.inspect(_ref("unsupported"))
        response = fake.calls("inspect")[-1].response
        assert result == Unsupported(reasons=_reasons(response["reasons"]), question=_info(response["question"]))
        assert result.reasons == (UnsupportedReason(
            code="unsupported_elements",
            message="A questão usa elementos que o canal não suporta: pl-matrix-input.",
            elements=("pl-matrix-input",)),)
        no_elements = engine.inspect(_ref("number"))
        assert type(no_elements) is Unsupported
        assert no_elements.reasons == (UnsupportedReason(
            code="course_element_override", message="O curso sobrescreve um elemento do PL.", elements=()),)
        assert no_elements.question == _info(fake.calls("inspect")[-1].response["question"])


def test_create_variant_ok_returns_raw_snapshot_and_ir_intact(fake: Any) -> None:
    """A IR guardada inteira e o snapshot devolvido intacto (JSON
    cru; 200 `ok` → `Variant`): o pedido é exatamente `{question_ref, seed}`; o `snapshot` e a
    `ir` são iguais (e iguais em JSON canônico) aos que o motor falso enviou; os avisos viram
    `EngineWarning` (fase ausente → `None`); uma resposta de vários MiB (acima dos 2 MiB do pedido, abaixo
    dos 64 MiB) é aceita."""
    no_phase = FakeWarning("console_output", "Saída do código da questão.")
    big = png_bytes(6_000_000)
    fake.program("with_media", preset("with_media", media=(FakeMedia("clientFilesQuestion/figura.png",
                                                                     data=big),),
                                       variant_warnings=(no_phase,)))
    with _open(fake) as engine:
        result = engine.create_variant(_ref("number"), SEED)
        call = fake.calls("create_variant")[-1]
        assert call.request == {"question_ref": _question_ref("number"), "seed": SEED}
        assert type(result) is Variant
        assert result.snapshot == call.response["snapshot"]
        assert result.ir == call.response["ir"]
        assert canonical(result.snapshot) == canonical(call.response["snapshot"])
        assert canonical(result.ir) == canonical(call.response["ir"])
        assert result.warnings == ()
        [issued] = fake.issued_variants("number")
        assert result.snapshot == issued.snapshot

        missing = engine.create_variant(_ref("missing_media"), SEED)
        assert type(missing) is Variant
        assert missing.warnings == (EngineWarning(code="missing_media",
                                                  message="Uma imagem do enunciado não foi encontrada.",
                                                  phase="render"),)

        large = engine.create_variant(_ref("with_media"), "2xyz")
        call = fake.calls("create_variant")[-1]
        assert type(large) is Variant
        assert len(json.dumps(call.response).encode("utf-8")) > 4 * 1024 * 1024
        assert canonical(large.ir) == canonical(call.response["ir"])
        assert canonical(large.snapshot) == canonical(call.response["snapshot"])
        assert large.warnings == (EngineWarning(code="console_output", message="Saída do código da questão.",
                                                phase=None),)


def test_create_variant_unsupported_and_broken_without_technical_detail(fake: Any) -> None:
    """Na criação da variante também há "não suportada", e o `technical_detail` nunca sai
    do adaptador: `dynamic_unsupported` → `Unsupported` com os motivos e sem a
    `question`; `broken_on_variant` → `Broken(code, message, phase="generate", warnings, None)`, sem o
    `technical_detail` em nenhum atributo, valor, `repr` ou `str` (controle positivo: o motor falso o
    enviou)."""
    with _open(fake) as engine:
        result = engine.create_variant(_ref("dynamic_unsupported"), SEED)
        response = fake.calls("create_variant")[-1].response
        assert result == Unsupported(reasons=_reasons(response["reasons"]), question=None)
        assert result.reasons[0].message == "A variante gerada usa elementos que o canal não suporta: pl-matrix-input."

        broken = engine.create_variant(_ref("broken_on_variant"), SEED)
        response = fake.calls("create_variant")[-1].response
        assert response["reason"]["technical_detail"] == TECHNICAL_DETAIL  # controle positivo
        assert broken == Broken(code=VARIANT_BROKEN.code, message=VARIANT_BROKEN.message, phase="generate",
                                warnings=(), version_mismatch=None)
        _assert_no_technical_detail(broken)


def test_grade_maps_every_field_and_sends_the_raw_answer_exactly(fake: Any) -> None:
    """A resposta crua chega ao motor por igualdade exata, e o snapshot é devolvido
    intacto (`Graded` com todos os campos; `repr` fixo; 200 `ok` →
    `Graded`): o pedido é exatamente `{question_ref, snapshot, raw_submitted_answers}`; a correção certa,
    a inválida (erro de formato como `FormatError`), a nota acima de 1 (repassada), o erro de formato fora
    de `fields[]`, a diferença de versões e os avisos da correção saem campo a campo iguais ao que o motor
    falso enviou; o `repr` e o `str` do `Graded` são fixos."""
    graded_warning = FakeWarning("console_output", "Saída do grade.", "grade")
    fake.program("multipart", preset("number", grade_warnings=(graded_warning,)))
    with _open(fake) as engine:
        cases = [
            ("number", {"resposta": "6.5"}),
            ("number", {"resposta": "abc"}),
            ("score_above_one", {"resposta": "6.5"}),
            ("server_format_error", {"resposta": "6.5"}),
            ("version_mismatch", {"resposta": "6.5"}),
            ("multipart", {"resposta": "6.5"}),
        ]
        results = {}
        for qid, raw in cases:
            variant = engine.create_variant(_ref(qid), SEED)
            assert type(variant) is Variant
            result = engine.grade(_ref(qid), variant.snapshot, raw)
            call = fake.calls("grade")[-1]
            assert call.request == {"question_ref": _question_ref(qid), "snapshot": variant.snapshot,
                                    "raw_submitted_answers": raw}
            assert canonical(call.request["snapshot"]) == canonical(fake.calls("create_variant")[-1]
                                                                    .response["snapshot"])
            _assert_same_graded(result, call.response)
            assert repr(result) == "Graded(<oculto>)"
            assert str(result) == "Graded(<oculto>)"
            results[(qid, raw["resposta"])] = result
        right = results[("number", "6.5")]
        assert (right.valid, right.score, dict(right.format_errors)) == (True, 1.0, {})
        assert right.version_mismatch == VersionMismatch(engine=False, pl=False)
        wrong = results[("number", "abc")]
        assert (wrong.valid, wrong.score, wrong.partial_scores) == (False, None, None)
        assert set(wrong.format_errors) == {"resposta"}
        assert type(wrong.format_errors["resposta"]) is FormatError
        assert results[("score_above_one", "6.5")].score == 1.5
        assert set(results[("server_format_error", "6.5")].format_errors) == {"verificacao"}
        assert results[("version_mismatch", "6.5")].version_mismatch == VersionMismatch(engine=True, pl=True)
        assert results[("multipart", "6.5")].warnings == (EngineWarning(code="console_output",
                                                                        message="Saída do grade.",
                                                                        phase="grade"),)


def test_grade_broken_discards_technical_detail(fake: Any) -> None:
    """`Broken` na correção com `version_mismatch` (200 `broken` →
    `Broken`): `broken_on_grade` → `Broken(code, message, phase="grade", warnings=(),
    VersionMismatch(False, False))`, sem o `technical_detail` (controle positivo: o motor falso o
    enviou)."""
    with _open(fake) as engine:
        variant = engine.create_variant(_ref("broken_on_grade"), SEED)
        assert type(variant) is Variant
        result = engine.grade(_ref("broken_on_grade"), variant.snapshot, {"resposta": "6.5"})
        response = fake.calls("grade")[-1].response
        assert response["reason"]["technical_detail"] == TECHNICAL_DETAIL  # controle positivo
        assert result == Broken(code=GRADE_BROKEN.code, message=GRADE_BROKEN.message, phase="grade",
                                warnings=(), version_mismatch=VersionMismatch(engine=False, pl=False))
        _assert_no_technical_detail(result)


@pytest.mark.parametrize("missing", ["qid", "commit", "repo"])
def test_reference_missing_on_disk_is_engine_not_found(fake: Any, missing: str) -> None:
    """A árvore que sumiu do volume (404 `not_found` → `EngineNotFound`,
    decidido pelo código, não pela mensagem): o QID, o commit ou o repositório ausentes da área →
    `EngineNotFound` com `kind == "not_found"`, numa única chamada à operação."""
    ref = {"qid": QuestionRef(REPO, COMMIT, "inexistente"),
           "commit": QuestionRef(REPO, "f" * 40, "number"),
           "repo": QuestionRef("https://repos.cq.test/outro.git", COMMIT, "number")}[missing]
    with _open(fake) as engine:
        with pytest.raises(EngineError) as raised:
            engine.inspect(ref)
    assert type(raised.value) is EngineNotFound
    assert raised.value.kind == "not_found"
    assert fake.calls("inspect")[-1].status == 404
    assert len(fake.calls("inspect")) == 1


# ---------------------------------------------------------------------------------------------
# A classificação de erros do adaptador: caso a caso, nas três operações
# ---------------------------------------------------------------------------------------------

Setup = Callable[[Any, str], None]


def _fail(code: str) -> Setup:
    return lambda fake, op: fake.fail(op, error=code)


def _net(kind: str) -> Setup:
    return lambda fake, op: fake.fail_network(op, kind=kind)


def _inject(status: int, body: Any) -> Setup:
    return lambda fake, op: fake.inject(op, status=status, body=copy.deepcopy(body), off_contract=True)


def _error(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": "Mensagem do motor."}}


OFF_CONTRACT_200 = {
    "inspect": {"outcome": "supported", "reasons": []},
    "create_variant": {"outcome": "ok", "ir": {}, "warnings": []},
    "grade": {"outcome": "broken", "reason": {"code": "question_code_error", "message": "Falhou.",
                                             "phase": "grade"}, "warnings": []},
}
OFF_CONTRACT_TYPES = {
    "inspect": {"outcome": "supported", "question": {**META, "tags": "teste"}, "reasons": []},
    "create_variant": {"outcome": "broken", "reason": {"code": "desconhecido", "message": "Falhou.",
                                                      "phase": "generate"}, "warnings": []},
    "grade": {"outcome": "ok", "valid": "sim", "format_errors": {}, "submitted_answers": {},
              "partial_scores": {}, "score": 1.0, "feedback": {}, "params": {}, "correct_answers": {},
              "version_mismatch": {"engine": False, "pl": False}, "warnings": []},
}


def _op_body(table: dict[str, Any]) -> Setup:
    return lambda fake, op: fake.inject(op, status=200, body=copy.deepcopy(table[op]), off_contract=True)


# (id, preparo, exceção, kind)
ROWS: list[tuple[str, Setup, type[EngineError], str]] = [
    ("400-invalid_request", _fail("invalid_request"), EngineRequestRejected, "invalid_request"),
    ("413-payload_too_large", _fail("payload_too_large"), EngineRequestRejected, "payload_too_large"),
    ("404-not_found", _fail("not_found"), EngineNotFound, "not_found"),
    ("404-route_not_found", _fail("route_not_found"), EngineUnavailable, "incompatible"),
    ("405-method_not_allowed", _fail("method_not_allowed"), EngineUnavailable, "incompatible"),
    ("500-internal_error", _fail("internal_error"), EngineUnavailable, "internal_error"),
    ("reset", _net("reset"), EngineUnavailable, "connection"),
    ("nao-json", _net("not_json"), EngineUnavailable, "protocol"),
    ("nan", _net("nan"), EngineUnavailable, "protocol"),
    ("502-html-de-proxy", _net("html_502"), EngineUnavailable, "protocol"),
    ("307-redirecionamento", _net("redirect"), EngineUnavailable, "protocol"),
    ("content-length-acima-de-64-mib", _net("oversize"), EngineUnavailable, "protocol"),
    ("404-com-internal_error", _inject(404, _error("internal_error")), EngineUnavailable, "protocol"),
    ("500-com-not_found", _inject(500, _error("not_found")), EngineUnavailable, "protocol"),
    ("400-com-route_not_found", _inject(400, _error("route_not_found")), EngineUnavailable, "protocol"),
    ("413-com-invalid_request", _inject(413, _error("invalid_request")), EngineUnavailable, "protocol"),
    ("503-internal_error", _inject(503, _error("internal_error")), EngineUnavailable, "protocol"),
    ("200-com-corpo-de-erro", _inject(200, _error("internal_error")), EngineUnavailable, "protocol"),
    ("500-corpo-fora-do-contrato", _inject(500, {"erro": "falha"}), EngineUnavailable, "protocol"),
    ("200-lista-json", _inject(200, []), EngineUnavailable, "protocol"),
    ("200-outcome-desconhecido", _inject(200, {"outcome": "talvez"}), EngineUnavailable, "protocol"),
    ("200-chave-obrigatoria-ausente", _op_body(OFF_CONTRACT_200), EngineUnavailable, "protocol"),
    ("200-tipo-ou-enum-fora-do-contrato", _op_body(OFF_CONTRACT_TYPES), EngineUnavailable, "protocol"),
]
CASES = [pytest.param(op, setup, exc, kind, id=f"{op}-{name}")
         for op in ("inspect", "create_variant", "grade") for name, setup, exc, kind in ROWS]
# Acima de 64 MiB sem `Content-Length` (em blocos): só numa operação (o corpo inteiro é lido até o teto).
CASES.append(pytest.param("create_variant", _net("oversize_chunked"), EngineUnavailable, "protocol",
                          id="create_variant-blocos-acima-de-64-mib"))


def _operation(engine: HttpQuestionEngine, op: str) -> Callable[[], Any]:
    """A chamada da operação `op` (a correção com um snapshot emitido antes pelo motor falso)."""
    if op == "inspect":
        return lambda: engine.inspect(_ref("number"))
    if op == "create_variant":
        return lambda: engine.create_variant(_ref("number"), SEED)
    variant = engine.create_variant(_ref("number"), SEED)
    assert type(variant) is Variant
    return lambda: engine.grade(_ref("number"), variant.snapshot, {"resposta": "6.5"})


@pytest.mark.parametrize(("op", "setup", "exc_type", "kind"), CASES)
def test_error_classification_by_status_and_code(fake: Any, op: str, setup: Setup,
                                                 exc_type: type[EngineError], kind: str) -> None:
    """Decidir pelo código do erro, não só pelo status (a classificação
    pelo par (status, `error.code`); par incoerente, status fora da tabela, 3xx, não JSON,
    `NaN`, fora do contrato e acima de 64 MiB → `protocol`; nunca repete; 3xx não seguido): cada
    linha, em cada operação, → a exceção e o `kind` exatos, com exatamente uma chamada à operação."""
    with _open(fake, timeout=20.0) as engine:
        call = _operation(engine, op)
        setup(fake, op)
        with pytest.raises(EngineError) as raised:
            call()
        assert type(raised.value) is exc_type
        assert raised.value.kind == kind
        assert len(fake.calls(op)) == 1, _ops(fake)


HEALTH_ROWS = [
    pytest.param(lambda fake: fake.set_health(status=500), EngineUnavailable, "not_ready", id="500-not_ready"),
    pytest.param(lambda fake: fake.set_health(contract_version="2.0"), EngineUnavailable, "incompatible",
                 id="contrato-2.0"),
    pytest.param(lambda fake: fake.set_health(ir_version="1.1"), EngineUnavailable, "incompatible",
                 id="ir-1.1"),
    pytest.param(lambda fake: fake.fail("health", error="route_not_found"), EngineUnavailable, "incompatible",
                 id="404-route_not_found"),
    pytest.param(lambda fake: fake.fail_network("health", kind="reset"), EngineUnavailable, "connection",
                 id="reset"),
    pytest.param(lambda fake: fake.fail_network("health", kind="not_json"), EngineUnavailable, "protocol",
                 id="nao-json"),
]


@pytest.mark.parametrize(("setup", "exc_type", "kind"), HEALTH_ROWS)
def test_health_failure_or_incompatible_engine_never_calls_the_operation(
        fake: Any, setup: Callable[[Any], None], exc_type: type[EngineError], kind: str) -> None:
    """Motor incompatível (compatibilidade por `GET /v1/health`:
    `status == "ready"`, `contract_version == "1.0"` e `ir_version == "1.0"`; `health` 500 →
    `not_ready`; versões do contrato ou da IR ≠ `1.0` → `incompatible` sem chamar a operação; cache
    invalidado em `EngineUnavailable`): a falha da saúde → a exceção e o `kind` exatos, sem nenhum pedido à
    operação; com o motor de volta ao normal, a chamada seguinte confere a saúde de novo e dá certo."""
    setup(fake)
    with _open(fake) as engine:
        with pytest.raises(EngineError) as raised:
            engine.inspect(_ref("number"))
        assert type(raised.value) is exc_type
        assert raised.value.kind == kind
        assert _ops(fake) == ["health"]
        fake.set_health()
        fake.clear_failures()
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake) == ["health", "health", "inspect"]


def test_stopped_engine_is_connection_failure(fake: Any) -> None:
    """Conexão recusada → `EngineUnavailable("connection")`, repetível, e o adaptador
    nunca repete: com o motor parado, a inspeção → `connection`, sem pedido registrado; religado na mesma
    porta, a chamada seguinte confere a saúde e dá certo."""
    with _open(fake) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported
        fake.stop()
        with pytest.raises(EngineError) as raised:
            engine.inspect(_ref("number"))
        assert type(raised.value) is EngineUnavailable
        assert raised.value.kind == "connection"
        fake.start()
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake) == ["health", "inspect", "health", "inspect"]


# ---------------------------------------------------------------------------------------------
# Prazo total, proxy, conexões e checagens locais
# ---------------------------------------------------------------------------------------------

DEADLINE_CASES = [
    pytest.param(lambda fake: fake.slow("inspect", 5.0), id="resposta-atrasada"),
    pytest.param(lambda fake: fake.slow("inspect", 5.0, drip=True), id="corpo-gotejando"),
    pytest.param(lambda fake: fake.fail_network("inspect", kind="hang"), id="sem-resposta"),
]


@pytest.mark.parametrize("setup", DEADLINE_CASES)
def test_total_deadline_is_enforced(fake: Any, setup: Callable[[Any], None]) -> None:
    """O vigia do prazo total (`CQ_ENGINE_TIMEOUT_SECONDS` é o prazo
    **total** por chamada; ao vencer, `EngineUnavailable("timeout")`): a resposta atrasada, o corpo que
    goteja (cabeçalhos logo, corpo ao longo de 5 s) e a sem resposta → `timeout` em até prazo + 0,5 s reais,
    numa única chamada; a saúde é conferida de novo depois (cache invalidado)."""
    with _open(fake, timeout=DEADLINE) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported  # a saúde já em cache
        setup(fake)
        started = monotonic()
        with pytest.raises(EngineError) as raised:
            engine.inspect(_ref("number"))
        elapsed = monotonic() - started
        assert type(raised.value) is EngineUnavailable
        assert raised.value.kind == "timeout"
        assert DEADLINE * 0.9 <= elapsed <= DEADLINE + DEADLINE_SLACK, elapsed
        assert len(fake.calls("inspect")) == 2
        fake.release_hangs()
        fake.clear_failures()
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake)[-2:] == ["health", "inspect"]


PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def test_environment_proxies_are_ignored(fake: Any, monkeypatch: pytest.MonkeyPatch,
                                         network_guard: Any) -> None:
    """Sem proxy (`httpx2.Request` direto ao transporte, sem `httpx2.Client`: nenhuma variável
    `HTTP(S)_PROXY` desvia a chamada ao motor): com os proxies do ambiente apontando para uma porta
    fechada do loopback e sem `NO_PROXY`, as três operações chegam ao motor falso, e toda conexão do
    processo vai à porta do motor falso."""
    for name in PROXY_VARS:
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    with _open(fake) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported
        variant = engine.create_variant(_ref("number"), SEED)
        assert type(variant) is Variant
        assert type(engine.grade(_ref("number"), variant.snapshot, {"resposta": "6.5"})) is Graded
    assert _ops(fake) == ["health", "inspect", "create_variant", "grade"]
    ports = {a.port for a in network_guard.attempts("socket.connect")}
    assert ports == {fake.port}, ports


def test_every_call_uses_a_new_connection(fake: Any) -> None:
    """Sem keep-alive (uma conexão nova por chamada, mesmo com o motor respondendo
    `Connection: keep-alive`): cinco pedidos (a saúde e quatro operações) em cinco conexões TCP
    distintas."""
    with _open(fake) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported
        variant = engine.create_variant(_ref("number"), SEED)
        assert type(engine.grade(_ref("number"), variant.snapshot, {"resposta": "6.5"})) is Graded
        assert type(engine.inspect(_ref("single_choice"))) is Supported
    calls = fake.calls()
    assert [c.op for c in calls] == ["health", "inspect", "create_variant", "grade", "inspect"]
    assert len({c.connection_id for c in calls}) == 5
    assert fake.connection_count() == 5


INVALID_SEEDS = [
    ("igual-a-2^32", "1z141z4"),
    ("acima-de-2^32", "zzzzzzz"),
    ("vazia", ""),
    ("maiusculas", "1ABC"),
    ("oito-caracteres", "12345678"),
    ("negativa", "-1"),
    ("com-espaco", "1a b"),
    ("quebra-de-linha", "1ab\n"),
]


@pytest.mark.parametrize("seed", [s for _, s in INVALID_SEEDS], ids=[i for i, _ in INVALID_SEEDS])
def test_invalid_seed_is_rejected_locally_without_network(fake: Any, seed: str, network_guard: Any) -> None:
    """Checagem local antes de enviar (seed `^[0-9a-z]{1,7}$` com valor < 2³²; o que
    falha vira `EngineRequestRejected` com o `kind` do motor, sem tocar a rede): cada seed inválida →
    `EngineRequestRejected("invalid_request")`, zero pedidos ao motor falso e nenhuma conexão."""
    with _open(fake) as engine:
        with pytest.raises(EngineError) as raised:
            engine.create_variant(_ref("number"), seed)
    assert type(raised.value) is EngineRequestRejected
    assert raised.value.kind == "invalid_request"
    assert fake.calls() == []
    assert network_guard.attempts() == []


def test_seed_limits_are_accepted(fake: Any) -> None:
    """Os limites da seed (2³² − 1 = `1z141z3` em base 36 e a seed de um caractere são
    válidas; controle positivo das recusas locais): as duas chegam ao motor e dão `Variant`."""
    with _open(fake) as engine:
        for seed in ("1z141z3", "0"):
            result = engine.create_variant(_ref("number"), seed)
            assert type(result) is Variant
            assert result.snapshot["seed"] == seed
            assert fake.calls("create_variant")[-1].request["seed"] == seed


def test_request_body_above_2_mib_is_rejected_locally_without_network(fake: Any, network_guard: Any) -> None:
    """Checagem local do tamanho (corpo de até 2 MiB, o teto do motor; acima, `EngineRequestRejected
    ("payload_too_large")` sem tocar a rede): uma correção com a resposta de 2 MiB + 1 caractere →
    `payload_too_large`, zero pedidos e nenhuma conexão."""
    snapshot, _ir, _warnings = build_variant(preset("number"), SEED)
    raw = {"resposta": "9" * (2 * 1024 * 1024 + 1)}
    with _open(fake) as engine:
        with pytest.raises(EngineError) as raised:
            engine.grade(_ref("number"), snapshot, raw)
    assert type(raised.value) is EngineRequestRejected
    assert raised.value.kind == "payload_too_large"
    assert fake.calls() == []
    assert network_guard.attempts() == []


# ---------------------------------------------------------------------------------------------
# O cache da saúde
# ---------------------------------------------------------------------------------------------


def test_health_is_cached_and_invalidated_on_unavailable(fake: Any) -> None:
    """Cache da saúde (conferida antes da primeira operação, em cache, e invalidada a cada
    `EngineUnavailable`): as três operações seguidas pedem a saúde uma vez; depois de um 500
    `internal_error`, a chamada seguinte a confere de novo; depois de uma conexão recusada, também."""
    with _open(fake) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported
        variant = engine.create_variant(_ref("number"), SEED)
        assert type(engine.grade(_ref("number"), variant.snapshot, {"resposta": "6.5"})) is Graded
        assert _ops(fake) == ["health", "inspect", "create_variant", "grade"]

        fake.fail("inspect", error="internal_error")
        with pytest.raises(EngineUnavailable):
            engine.inspect(_ref("number"))
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake)[4:] == ["inspect", "health", "inspect"]

        fake.fail_network("create_variant", kind="reset")
        with pytest.raises(EngineUnavailable):
            engine.create_variant(_ref("number"), SEED)
        assert type(engine.create_variant(_ref("number"), SEED)) is Variant
        assert _ops(fake)[7:] == ["create_variant", "health", "create_variant"]


def test_health_cache_expires(fake: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validade do cache da saúde (por um tempo fixo, no relógio monotônico; padrão
    `HEALTH_CACHE_SECONDS = 60`, a constante do adaptador): com a constante encurtada para 1 s, duas
    inspeções seguidas pedem a saúde uma vez, e uma inspeção depois de 1,3 s a pede de novo."""
    assert adapter_module.HEALTH_CACHE_SECONDS == 60.0
    monkeypatch.setattr(adapter_module, "HEALTH_CACHE_SECONDS", 1.0)
    with _open(fake) as engine:
        assert type(engine.inspect(_ref("number"))) is Supported
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake) == ["health", "inspect", "inspect"]
        time.sleep(1.3)
        assert type(engine.inspect(_ref("number"))) is Supported
        assert _ops(fake) == ["health", "inspect", "inspect", "health", "inspect"]


# ---------------------------------------------------------------------------------------------
# Exceções sem corpo, silêncio
# ---------------------------------------------------------------------------------------------


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending = [exc]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend(e for e in (item.__cause__, item.__context__) if e is not None)


NO_BODY_CASES = [
    pytest.param(lambda fake: fake.fail("grade", error="invalid_request", message=CANARY_MESSAGE,
                                        details=[CANARY_ANSWER, TECHNICAL_DETAIL]),
                 EngineRequestRejected, "invalid_request", True, id="400-com-detalhes"),
    pytest.param(lambda fake: fake.fail("grade", error="not_found", message=CANARY_MESSAGE),
                 EngineNotFound, "not_found", True, id="404"),
    pytest.param(lambda fake: fake.fail("grade", error="internal_error", message=CANARY_MESSAGE,
                                        details=[TECHNICAL_DETAIL]),
                 EngineUnavailable, "internal_error", True, id="500"),
    pytest.param(lambda fake: fake.inject("grade", status=502, body={"erro": CANARY_MESSAGE,
                                                                     "detalhe": TECHNICAL_DETAIL},
                                          off_contract=True),
                 EngineUnavailable, "protocol", True, id="502-fora-do-contrato"),
    pytest.param(lambda fake: fake.inject("grade", status=200, body={"outcome": "ok", "valid": CANARY_ANSWER},
                                          off_contract=True),
                 EngineUnavailable, "protocol", True, id="200-fora-do-contrato"),
    pytest.param(lambda fake: fake.fail_network("grade", kind="reset"),
                 EngineUnavailable, "connection", False, id="reset"),
]


@pytest.mark.parametrize(("setup", "exc_type", "kind", "echoed"), NO_BODY_CASES)
def test_exceptions_carry_no_body_url_qid_or_answer(fake: Any, setup: Callable[[Any], None],
                                                    exc_type: type[EngineError], kind: str,
                                                    echoed: bool) -> None:
    """Exceções sem dados (nenhuma exceção leva corpo, URL, QID nem resposta do aluno: só um
    `kind` fixo; exceções levantadas fora do `except` e com `from None`; `str`, `repr`
    e a cadeia `__cause__`/`__context__` sem a resposta canário, com controle positivo): numa correção com a
    resposta canário e um QID canário, cada falha → a exceção e o `kind` exatos, com só o atributo `kind`,
    sem causa, e nenhum texto dela nem da cadeia (nem o traceback formatado) traz a resposta, a mensagem do
    motor, o `technical_detail`, o QID, a URL ou o repositório; controle positivo: o motor falso recebeu a
    resposta canário e, nas respostas com corpo, a devolveu."""
    shutil.copytree(fake.repos_root / repo_key(REPO) / COMMIT / "questions" / "number",
                    fake.repos_root / repo_key(REPO) / COMMIT / "questions" / CANARY_QID)
    fake.program(CANARY_QID, preset("number"))
    ref = _ref(CANARY_QID)
    with _open(fake) as engine:
        variant = engine.create_variant(ref, SEED)
        assert type(variant) is Variant
        setup(fake)
        with pytest.raises(EngineError) as raised:
            engine.grade(ref, variant.snapshot, {"resposta": CANARY_ANSWER})
    exc = raised.value
    assert type(exc) is exc_type
    assert exc.kind == kind
    assert vars(exc) == {"kind": kind}
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    # Controle positivo: o canário passou pelo motor falso.
    [call] = fake.calls("grade")
    assert call.request["raw_submitted_answers"] == {"resposta": CANARY_ANSWER}
    assert call.request["question_ref"]["qid"] == CANARY_QID
    if echoed:
        assert CANARY_ANSWER in json.dumps(call.response, ensure_ascii=False)
    forbidden = (CANARY_ANSWER, CANARY_QID, TECHNICAL_DETAIL_SENTINEL, fake.base_url, f"127.0.0.1:{fake.port}",
                 REPO, "Mensagem do motor")
    texts = ["".join(traceback.format_exception(exc))]
    for item in _chain(exc):
        texts += [str(item), repr(item), *map(repr, item.args)]
    for text in texts:
        for value in forbidden:
            assert value not in text, (value, text[:300])


def test_adapter_is_silent(fake: Any, capfd: Any) -> None:
    """Silêncio (o adaptador não registra nada em log; `capfd` vazio,
    nenhum registro em DEBUG nos loggers `cq_server*`): sucesso nas três operações, `broken`, `not_found`,
    500, resposta não JSON, reset e recusa local → nenhum registro e nada em fd 1 e 2."""
    capfd.readouterr()
    with capture_logs() as cap:
        with _open(fake) as engine:
            assert type(engine.inspect(_ref("number"))) is Supported
            variant = engine.create_variant(_ref("number"), SEED)
            assert type(engine.grade(_ref("number"), variant.snapshot, {"resposta": CANARY_ANSWER})) is Graded
            assert type(engine.create_variant(_ref("broken_on_variant"), SEED)) is Broken
            with pytest.raises(EngineNotFound):
                engine.inspect(_ref("inexistente"))
            fake.fail("inspect", error="internal_error")
            with pytest.raises(EngineUnavailable):
                engine.inspect(_ref("number"))
            fake.fail_network("create_variant", kind="not_json")
            with pytest.raises(EngineUnavailable):
                engine.create_variant(_ref("number"), SEED)
            fake.fail_network("inspect", kind="reset")
            with pytest.raises(EngineUnavailable):
                engine.inspect(_ref("number"))
            with pytest.raises(EngineRequestRejected):
                engine.create_variant(_ref("number"), "zzzzzzz")
    assert cap.records == []
    assert capfd.readouterr() == ("", "")
