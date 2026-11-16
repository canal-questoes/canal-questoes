"""Adaptador HTTP do motor de questões: o cliente da API do motor, que valida cada resposta e classifica
as falhas sem repetir a chamada.

`HttpQuestionEngine` implementa `ports.engine.QuestionEngine` sobre o contrato HTTP do motor
(`contracts/engine/v1/`): `POST {CQ_ENGINE_URL}/v1/{inspect|variants|grade}` e `GET …/v1/health`.

- **Transporte:** `httpx2.Request` direto pelo `_AbortableTransport` de `adapters/_http.py` (sem
  `httpx2.Client`, portanto sem proxy do ambiente), com `max_keepalive_connections=0` (uma conexão nova
  por chamada) e sem seguir redirecionamento (um 3xx é `protocol`). Um `transport` injetado (qualquer
  `httpx2.BaseTransport`; os testes de paridade) passa pelo mesmo teto e pelo mesmo parse.
- **Prazo total**: `timeout_seconds` cobre a operação inteira da porta (a saúde,
  quando não está em cache, e a operação), contado antes da conexão; ao vencer, o vigia
  (`cq-engine-deadline`) derruba a conexão, e a chamada sai com `EngineUnavailable("timeout")`. Tempos
  por fase: conexão e fila `min(5, prazo)`, escrita `min(10, prazo)`, leitura o próprio prazo.
- **Checagens locais**, sem rede e antes até da saúde: seed `^[0-9a-z]{1,7}$` com valor < 2³² e corpo
  do pedido de até 2 MiB; senão `EngineRequestRejected` com o `kind` do motor.
- **Leitura:** o corpo em fluxo, contando bytes; acima de `MAX_RESPONSE_BYTES` (ou com um
  `Content-Length` acima dele), a conexão é fechada, e a falha é `protocol`. JSON UTF-8 estrito (sem
  `NaN`/`Infinity`).
- **Parse estrito** do envelope, campo a campo (chaves, tipos, enums, a forma de cada `outcome`),
  sem `jsonschema`; qualquer desvio é `protocol`. O `technical_detail` é descartado na leitura
  e nunca entra na porta.
- **Classificação** pelo par (status, `error.code`); a saúde (`GET /v1/health`) é
  conferida antes da primeira operação e fica em cache por `HEALTH_CACHE_SECONDS` no relógio
  monotônico, invalidado a cada `EngineUnavailable`.
- **Nunca repete** (a repetição é da fila de tarefas). **Nada em log.** As
  exceções saem só com o `kind`, levantadas fora de qualquer `except` (sem `__cause__` nem
  `__context__`): nenhuma leva corpo, URL, QID nem resposta do aluno. O `repr` mostra só `host:porta`.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpcore2  # classificação das falhas do transporte
import httpx2  # o transporte do motor

from cq_server.adapters._http import _AbortableTransport, _CallDeadline
from cq_server.config import EngineSettings
from cq_server.ports.engine import (
    BROKEN_CODES,
    BROKEN_PHASES,
    WARNING_CODES,
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

# Tetos e checagens locais.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
HEALTH_CACHE_SECONDS = 60.0
DEADLINE_THREAD_NAME = "cq-engine-deadline"

# As versões que este cliente entende.
CONTRACT_VERSION = "1.0"
IR_VERSION = "1.0"

# Frases internas do transporte (nunca chegam à porta; sem URL nem corpo).
_MSG_ABORTED = "chamada ao motor interrompida (adaptador fechado)"
_MSG_DEADLINE = "prazo total da chamada ao motor vencido"

# Seed no formato do PL (contrato do motor, `$defs.seed`) e o limite superior (2³²).
_SEED = re.compile(r"[0-9a-z]{1,7}")
_SEED_LIMIT = 2 ** 32

# O par (status, código) → a exceção da porta; fora da tabela, `protocol`.
_ERROR_TABLE: dict[tuple[int, str], tuple[type[EngineError], str]] = {
    (400, "invalid_request"): (EngineRequestRejected, "invalid_request"),
    (413, "payload_too_large"): (EngineRequestRejected, "payload_too_large"),
    (404, "not_found"): (EngineNotFound, "not_found"),
    (404, "route_not_found"): (EngineUnavailable, "incompatible"),
    (405, "method_not_allowed"): (EngineUnavailable, "incompatible"),
    (500, "internal_error"): (EngineUnavailable, "internal_error"),
}
# Na saúde: sem executor Python (500) é `not_ready`; rota ou método desconhecido, `incompatible`.
_HEALTH_ERROR_TABLE: dict[tuple[int, str], str] = {
    (500, "internal_error"): "not_ready",
    (404, "route_not_found"): "incompatible",
    (405, "method_not_allowed"): "incompatible",
}
_ERROR_CODES = frozenset({"invalid_request", "not_found", "payload_too_large", "route_not_found",
                          "method_not_allowed", "internal_error"})

# Contrato do motor (`engine-api.schema.json`): enums e padrões.
_REASON_CODES = frozenset({"question_type_not_v3", "grading_method_not_internal", "external_grading",
                           "workspace", "unsupported_elements", "no_answer_fields",
                           "course_element_override", "custom_server_render", "invalid_answers_name",
                           "invalid_question_metadata"})
_ELEMENT_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_CONTENT_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PL_COMMIT = re.compile(r"[0-9a-f]{40}")
_TECHNICAL_DETAIL_MAX = 8000

_METADATA_KEYS = frozenset({"uuid", "title", "topic", "tags", "type", "grading_method", "elements",
                            "show_correct_answer", "single_variant", "partial_credit",
                            "answers_names", "content_hash"})
_SNAPSHOT_KEYS = frozenset({"seed", "params", "correct_answers", "preferences", "engine_version",
                            "pl_commit", "ir_version"})
_IR_KEYS = frozenset({"ir_version", "statement", "fields", "answer_key", "explanation", "media"})
_HEALTH_KEYS = frozenset({"status", "engine_version", "contract_version", "ir_version", "pl_commit",
                          "workers"})
_GRADED_KEYS = frozenset({"outcome", "valid", "format_errors", "submitted_answers", "partial_scores",
                          "score", "feedback", "params", "correct_answers", "version_mismatch",
                          "warnings"})

_PATHS = {"health": "/v1/health", "inspect": "/v1/inspect", "create_variant": "/v1/variants",
          "grade": "/v1/grade"}


class _Protocol(Exception):
    """Desvio do contrato na resposta (interno; vira `EngineUnavailable("protocol")`)."""


# ---------------------------------------------------------------------------------------------
# Parse estrito (`contracts/engine/v1/engine-api.schema.json`)
# ---------------------------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError("constante JSON fora do padrão")


def _parse_json(body: bytes) -> Any:
    """O JSON UTF-8 estrito do corpo (sem `NaN`/`Infinity`); senão `_Protocol`."""
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Protocol from None


def _require(condition: bool) -> None:
    if not condition:
        raise _Protocol


def _obj(value: Any, required: frozenset[str], optional: frozenset[str] = frozenset(),
         ) -> dict[str, Any]:
    """Um objeto JSON com todas as chaves de `required` e nenhuma fora de `required | optional`."""
    _require(isinstance(value, dict))
    keys = set(value)
    _require(required <= keys and keys <= required | optional)
    return value


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _text(value: Any) -> str:
    """Texto não vazio (`minLength: 1`)."""
    _require(isinstance(value, str) and value != "")
    return value


def _opt_str(value: Any) -> str | None:
    _require(value is None or isinstance(value, str))
    return value


def _bool(value: Any) -> bool:
    _require(type(value) is bool)
    return value


def _str_list(value: Any, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    _require(isinstance(value, list) and all(isinstance(item, str) for item in value))
    if pattern is not None:
        _require(all(pattern.fullmatch(item) is not None for item in value))
    return tuple(value)


def _json_object(value: Any) -> Any:
    """`$defs.json_object`: objeto ou lista."""
    _require(isinstance(value, (dict, list)))
    return value


def _number(value: Any) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(value))
    return float(value)


def _warnings(value: Any) -> tuple[EngineWarning, ...]:
    _require(isinstance(value, list))
    result = []
    for item in value:
        warning = _obj(item, frozenset({"code", "message"}), frozenset({"phase"}))
        code = warning["code"]
        _require(_is_str(code) and code in WARNING_CODES)
        phase = warning.get("phase")
        _require(phase is None and "phase" not in warning or _is_str(phase) and phase in BROKEN_PHASES)
        result.append(EngineWarning(code=code, message=_text(warning["message"]), phase=phase))
    return tuple(result)


def _reasons(value: Any, *, minimum: int) -> tuple[UnsupportedReason, ...]:
    _require(isinstance(value, list) and len(value) >= minimum)
    result = []
    for item in value:
        reason = _obj(item, frozenset({"code", "message"}), frozenset({"elements"}))
        code = reason["code"]
        _require(_is_str(code) and code in _REASON_CODES)
        elements = _str_list(reason["elements"]) if "elements" in reason else ()
        result.append(UnsupportedReason(code=code, message=_text(reason["message"]),
                                        elements=elements))
    return tuple(result)


def _question(value: Any) -> QuestionInfo:
    meta = _obj(value, _METADATA_KEYS)
    content_hash = meta["content_hash"]
    _require(_is_str(content_hash) and _CONTENT_HASH.fullmatch(content_hash) is not None)
    grading_method = meta["grading_method"]
    _require(_is_str(grading_method))
    return QuestionInfo(uuid=_opt_str(meta["uuid"]), title=_opt_str(meta["title"]),
                        topic=_opt_str(meta["topic"]), type=_opt_str(meta["type"]),
                        tags=_str_list(meta["tags"]), grading_method=grading_method,
                        elements=_str_list(meta["elements"], _ELEMENT_NAME),
                        show_correct_answer=_bool(meta["show_correct_answer"]),
                        single_variant=_bool(meta["single_variant"]),
                        partial_credit=_bool(meta["partial_credit"]),
                        answers_names=_str_list(meta["answers_names"]),
                        content_hash=content_hash)


def _broken_reason(value: Any) -> tuple[str, str, str]:
    """`$defs.broken_reason` → `(code, message, phase)`; o `technical_detail` é conferido e
    descartado aqui."""
    reason = _obj(value, frozenset({"code", "message", "phase"}), frozenset({"technical_detail"}))
    code, phase = reason["code"], reason["phase"]
    _require(_is_str(code) and code in BROKEN_CODES)
    _require(_is_str(phase) and phase in BROKEN_PHASES)
    if "technical_detail" in reason:
        detail = reason["technical_detail"]
        _require(_is_str(detail) and len(detail) <= _TECHNICAL_DETAIL_MAX)
    return code, _text(reason["message"]), phase


def _version_mismatch(value: Any) -> VersionMismatch:
    mismatch = _obj(value, frozenset({"engine", "pl"}))
    return VersionMismatch(engine=_bool(mismatch["engine"]), pl=_bool(mismatch["pl"]))


def _snapshot(value: Any) -> dict[str, Any]:
    """`$defs.snapshot` (estrito, como o motor o emite); devolvido cru."""
    snapshot = _obj(value, _SNAPSHOT_KEYS)
    seed = snapshot["seed"]
    _require(_is_str(seed) and _seed_ok(seed))
    for name in ("params", "correct_answers", "preferences"):
        _json_object(snapshot[name])
    engine_version, pl_commit = snapshot["engine_version"], snapshot["pl_commit"]
    _require(_is_str(engine_version) and _SEMVER.fullmatch(engine_version) is not None)
    _require(_is_str(pl_commit) and _PL_COMMIT.fullmatch(pl_commit) is not None)
    _require(snapshot["ir_version"] == IR_VERSION)
    return snapshot


def _ir(value: Any) -> dict[str, Any]:
    """A IR crua: só o envelope de cima é conferido aqui (a IR inteira é do núcleo)."""
    ir = _obj(value, _IR_KEYS)
    _require(ir["ir_version"] == IR_VERSION)
    return ir


def _parse_inspect(body: Any) -> Supported | Unsupported:
    payload = _obj(body, frozenset({"outcome", "question", "reasons"}))
    outcome = payload["outcome"]
    question = _question(payload["question"])
    if outcome == "supported":
        _require(_reasons(payload["reasons"], minimum=0) == ())
        return Supported(question=question)
    _require(outcome == "unsupported")
    return Unsupported(reasons=_reasons(payload["reasons"], minimum=1), question=question)


def _parse_variant(body: Any) -> Variant | Unsupported | Broken:
    _require(isinstance(body, dict))
    outcome = body.get("outcome")
    if outcome == "ok":
        payload = _obj(body, frozenset({"outcome", "snapshot", "ir", "warnings"}))
        return Variant(snapshot=_snapshot(payload["snapshot"]), ir=_ir(payload["ir"]),
                       warnings=_warnings(payload["warnings"]))
    if outcome == "broken":
        payload = _obj(body, frozenset({"outcome", "reason", "warnings"}))
        code, message, phase = _broken_reason(payload["reason"])
        return Broken(code=code, message=message, phase=phase,
                      warnings=_warnings(payload["warnings"]), version_mismatch=None)
    _require(outcome == "unsupported")
    payload = _obj(body, frozenset({"outcome", "reasons"}))
    return Unsupported(reasons=_reasons(payload["reasons"], minimum=1), question=None)


def _format_errors(value: Any) -> dict[str, FormatError]:
    _require(isinstance(value, dict))
    result = {}
    for name, item in value.items():
        error = _obj(item, frozenset({"html", "text"}))
        _require(_is_str(error["html"]) and _is_str(error["text"]))
        result[name] = FormatError(html=error["html"], text=error["text"])
    return result


def _parse_grade(body: Any) -> Graded | Broken:
    _require(isinstance(body, dict))
    outcome = body.get("outcome")
    if outcome == "broken":
        payload = _obj(body, frozenset({"outcome", "reason", "version_mismatch", "warnings"}))
        code, message, phase = _broken_reason(payload["reason"])
        return Broken(code=code, message=message, phase=phase,
                      warnings=_warnings(payload["warnings"]),
                      version_mismatch=_version_mismatch(payload["version_mismatch"]))
    _require(outcome == "ok")
    payload = _obj(body, _GRADED_KEYS)
    valid = _bool(payload["valid"])
    format_errors = _format_errors(payload["format_errors"])
    partial_scores = payload["partial_scores"]
    score_raw = payload["score"]
    if valid:
        score: float | None = _number(score_raw)
        _require(isinstance(partial_scores, (dict, list)))
        _require(format_errors == {})
    else:
        _require(score_raw is None and partial_scores is None and len(format_errors) >= 1)
        score = None
    return Graded(valid=valid, format_errors=format_errors,
                  submitted_answers=_json_object(payload["submitted_answers"]),
                  partial_scores=partial_scores, score=score,
                  feedback=_json_object(payload["feedback"]),
                  params=_json_object(payload["params"]),
                  correct_answers=_json_object(payload["correct_answers"]),
                  version_mismatch=_version_mismatch(payload["version_mismatch"]),
                  warnings=_warnings(payload["warnings"]))


def _error_code(body: Any) -> str:
    """`$defs.error_response` → o `code` (a mensagem e os detalhes nunca decidem nada)."""
    envelope = _obj(body, frozenset({"error"}))
    error = _obj(envelope["error"], frozenset({"code", "message"}), frozenset({"details"}))
    code = error["code"]
    _require(_is_str(code) and code in _ERROR_CODES)
    _text(error["message"])
    if "details" in error:
        _str_list(error["details"])
    return code


def _health_problem(status: int, body: Any) -> str | None:
    """`None` se o motor está pronto e é compatível; senão o `kind` de `EngineUnavailable`
    (fora do 200, pela tabela do par status e código; no 200, `incompatible` ou `not_ready`)."""
    if status != 200:
        return _HEALTH_ERROR_TABLE.get((status, _error_code(body)), "protocol")
    health = _obj(body, _HEALTH_KEYS)
    for name in ("status", "contract_version", "ir_version"):
        _require(_is_str(health[name]))
    engine_version, pl_commit, workers = (health["engine_version"], health["pl_commit"],
                                          health["workers"])
    _require(_is_str(engine_version) and _SEMVER.fullmatch(engine_version) is not None)
    _require(_is_str(pl_commit) and _PL_COMMIT.fullmatch(pl_commit) is not None)
    _require(type(workers) is int and workers >= 1)
    if health["contract_version"] != CONTRACT_VERSION or health["ir_version"] != IR_VERSION:
        return "incompatible"
    if health["status"] != "ready":
        return "not_ready"
    return None


def _seed_ok(seed: str) -> bool:
    """`^[0-9a-z]{1,7}$` com valor em base 36 menor que 2³²."""
    return _SEED.fullmatch(seed) is not None and int(seed, 36) < _SEED_LIMIT


def _ref_json(ref: QuestionRef) -> dict[str, str]:
    return {"repo": ref.repo, "commit": ref.commit, "qid": ref.qid}


def _encode(body: Mapping[str, Any]) -> bytes | str:
    """O corpo JSON do pedido, ou o `kind` da recusa local (`invalid_request` para o que não é JSON
    padrão; `payload_too_large` acima de 2 MiB)."""
    try:
        data = json.dumps(body, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        return "invalid_request"
    if len(data) > MAX_REQUEST_BYTES:
        return "payload_too_large"
    return data


def _display(url: str) -> str:
    """`host:porta` da URL do motor, para o `repr` (sem esquema, caminho nem credenciais)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "<inválida>"
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}"


# ---------------------------------------------------------------------------------------------
# O adaptador
# ---------------------------------------------------------------------------------------------


class HttpQuestionEngine:
    """O motor de questões sobre HTTP (ver o docstring do módulo)."""

    def __init__(self, settings: EngineSettings, *, transport: Any = None) -> None:
        self._settings = settings
        self._base = settings.engine_url.rstrip("/")
        self._display = _display(self._base)
        seconds = settings.timeout_seconds
        self._deadline_seconds = seconds
        # Conexão e fila `min(5, prazo)`, escrita `min(10, prazo)`, leitura o
        # prazo (o vigia derruba a chamada ao vencer o prazo total).
        self._timeouts = {"connect": min(5.0, seconds), "pool": min(5.0, seconds),
                          "write": min(10.0, seconds), "read": float(seconds)}
        self._owns_transport = transport is None
        if transport is None:
            https = urlsplit(self._base).scheme.lower() == "https"
            transport = _AbortableTransport(https=https, max_keepalive_connections=0,
                                            aborted_message=_MSG_ABORTED,
                                            deadline_message=_MSG_DEADLINE)
        self._transport = transport
        self._health_until: float | None = None
        self._deadlines_lock = threading.Lock()
        self._deadlines: set[_CallDeadline] = set()
        self._local_deadline = threading.local()
        self._closed = False

    # -- a porta ------------------------------------------------------------------------------------

    def inspect(self, ref: QuestionRef) -> Supported | Unsupported:
        return self._operation("inspect", {"question_ref": _ref_json(ref)}, _parse_inspect)

    def create_variant(self, ref: QuestionRef, seed: str) -> Variant | Unsupported | Broken:
        if not isinstance(seed, str) or not _seed_ok(seed):
            raise EngineRequestRejected("invalid_request")
        return self._operation("create_variant", {"question_ref": _ref_json(ref), "seed": seed},
                               _parse_variant)

    def grade(self, ref: QuestionRef, snapshot: Mapping[str, Any],
              raw_answers: Mapping[str, Any]) -> Graded | Broken:
        body = {"question_ref": _ref_json(ref), "snapshot": snapshot,
                "raw_submitted_answers": raw_answers}
        return self._operation("grade", body, _parse_grade)

    def close(self) -> None:
        """Fecha o transporte próprio (idempotente); um transporte injetado é de quem o passou."""
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            self._transport.close()
        with self._deadlines_lock:
            deadlines = list(self._deadlines)
        for deadline in deadlines:
            deadline.stop()

    def __repr__(self) -> str:
        return f"HttpQuestionEngine(url={self._display})"

    __str__ = __repr__

    # -- operação -----------------------------------------------------------------------------------

    def _operation(self, op: str, body: Mapping[str, Any], parse: Any) -> Any:
        """A operação `op`: checagem local, saúde (se fora do cache) e o pedido, sob um prazo total.
        Toda `EngineUnavailable` invalida o cache da saúde. As exceções são levantadas fora de
        qualquer `except`."""
        data = _encode(body)
        if isinstance(data, str):
            raise EngineRequestRejected(data)
        deadline = self._start_deadline()
        try:
            outcome: Any = self._check_health()
            if outcome is None:
                response = self._request("POST", _PATHS[op], data)
                outcome = (response if isinstance(response, EngineError)
                           else self._decode(response, parse))
        finally:
            self._stop_deadline(deadline)
        if isinstance(outcome, EngineError):
            if isinstance(outcome, EngineUnavailable):
                self._health_until = None
            raise outcome
        return outcome

    def _decode(self, response: tuple[int, bytes], parse: Any) -> Any:
        """O valor da porta para a resposta `(status, corpo)`, ou a exceção a levantar (devolvida,
        não levantada)."""
        status, content = response
        try:
            payload = _parse_json(content)
            if status == 200:
                return parse(payload)
            code = _error_code(payload)
        except _Protocol:
            return EngineUnavailable("protocol")
        cls, kind = _ERROR_TABLE.get((status, code), (EngineUnavailable, "protocol"))
        return cls(kind)

    def _check_health(self) -> EngineUnavailable | None:
        """A saúde em cache ou conferida agora; a falha é devolvida."""
        now = time.monotonic()
        if self._health_until is not None and now < self._health_until:
            return None
        result = self._request("GET", _PATHS["health"], None)
        if isinstance(result, EngineError):
            return result
        status, content = result
        try:
            problem = _health_problem(status, _parse_json(content))
        except _Protocol:
            problem = "protocol"
        if problem is not None:
            return EngineUnavailable(problem)
        self._health_until = time.monotonic() + HEALTH_CACHE_SECONDS
        return None

    # -- troca HTTP ---------------------------------------------------------------------------------

    def _request(self, method: str, path: str, data: bytes | None,
                 ) -> tuple[int, bytes] | EngineUnavailable:
        """Uma troca HTTP: `(status, corpo)` ou a `EngineUnavailable` da falha (devolvida)."""
        if self._closed:
            return EngineUnavailable("connection")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        deadline = self._current_deadline
        kind: str | None = None
        try:
            request = httpx2.Request(method, self._base + path, content=data, headers=headers,
                                     extensions={"timeout": self._timeouts})
            return self._exchange(request, deadline)
        except _Protocol:
            kind = "protocol"
        except Exception as exc:  # noqa: BLE001 - toda falha do transporte vira erro da porta
            kind = self._failure_kind(exc, deadline)
        # Fora do `except`: nada do transporte (que pode citar o pedido) fica na cadeia.
        return EngineUnavailable(kind)

    def _exchange(self, request: httpx2.Request,
                  deadline: _CallDeadline | None) -> tuple[int, bytes]:
        """O status e o corpo inteiro, lido em fluxo até `MAX_RESPONSE_BYTES` (acima, ou com um
        `Content-Length` acima, `_Protocol` e a conexão fechada). Lido o corpo, a chamada acaba para o
        vigia; se ele disparou nesse instante, a resposta já lida vale (como no Telegram)."""
        response = self._transport.handle_request(request)
        try:
            declared = response.headers.get("content-length")
            if declared is not None and (not declared.isdigit()
                                         or int(declared) > MAX_RESPONSE_BYTES):
                raise _Protocol
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _Protocol
                chunks.append(chunk)
        except BaseException:
            response.close()
            raise
        if deadline is not None:
            deadline.finish()
        try:
            response.close()
        except Exception:
            if deadline is None or not deadline.expired:
                raise
        return response.status_code, b"".join(chunks)

    def _failure_kind(self, exc: Exception, deadline: _CallDeadline | None) -> str:
        """O `kind` de uma falha do transporte (só pelo tipo; nada dela é copiado): o prazo total
        vencido e o tempo esgotado são `timeout`; o resto (recusa, reset, desconexão), `connection`."""
        if deadline is not None and deadline.expired:
            return "timeout"
        if isinstance(exc, (httpcore2.TimeoutException, httpx2.TimeoutException)):
            return "timeout"
        return "connection"

    # -- prazo total --------------------------------------------------------------------------------

    @property
    def _current_deadline(self) -> _CallDeadline | None:
        """O prazo da operação que corre nesta thread (se houver)."""
        return getattr(self._local_deadline, "value", None)

    def _start_deadline(self) -> _CallDeadline | None:
        """Liga o vigia do prazo total da operação (só no transporte próprio, que o obedece)."""
        if not self._owns_transport:
            return None
        deadline = _CallDeadline(self._deadline_seconds, thread_name=DEADLINE_THREAD_NAME)
        with self._deadlines_lock:
            self._deadlines.add(deadline)
        self._transport.set_deadline(deadline)
        self._local_deadline.value = deadline
        try:
            deadline.start()
        except BaseException:
            self._stop_deadline(deadline)
            raise
        return deadline

    def _stop_deadline(self, deadline: _CallDeadline | None) -> None:
        """Encerra a operação para o vigia (por qualquer caminho), cancela-o e o espera."""
        if deadline is None:
            return
        self._transport.set_deadline(None)
        self._local_deadline.value = None
        deadline.stop()
        with self._deadlines_lock:
            self._deadlines.discard(deadline)
