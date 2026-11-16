"""Porta do motor de questões: inspecionar a questão, criar a variante e corrigir a resposta
(o núcleo não depende do transporte).

O núcleo fala com o motor PL (`engine/`) só por esta porta; o adaptador HTTP fica em
`cq_server.adapters.engine` (`HttpQuestionEngine`). A porta é neutra de transporte: três
operações (`inspect`, `create_variant`, `grade`) e `close()`, sem `preferences`.

- **Resultado × exceção:** os desfechos semânticos do motor (HTTP 200 com `outcome`) voltam como
  valores (`Supported`, `Unsupported`, `Variant`, `Broken`, `Graded`); as falhas do pedido, como
  exceções (`EngineError` e subclasses), no molde da porta do Telegram. Nenhuma exceção leva corpo,
  URL, QID nem resposta do aluno: só um `kind` fixo.
- O `snapshot` e a IR da variante são o JSON cru, guardado e devolvido intacto.
- `technical_detail` não existe aqui: o adaptador o descarta na leitura.
- `FormatError` e `Graded` podem trazer texto do aluno: ficam fora do `repr`.

`repo_key` **não** mora aqui: a única definição é `cq_server.questionrefs.repo_key`.
Só tipos, exceções e o `Protocol`; nada de HTTP (já coberto por `CORE_MODULES`, entrada `ports`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# Os `kind` das exceções (conjuntos fechados).
UNAVAILABLE_KINDS = frozenset({"connection", "timeout", "internal_error", "not_ready", "incompatible",
                               "protocol", "unconfigured"})
NOT_FOUND_KINDS = frozenset({"not_found"})
REJECTED_KINDS = frozenset({"invalid_request", "payload_too_large"})

# Códigos de `Broken` (contrato do motor, `broken_reason`) e fases.
BROKEN_CODES = frozenset({"question_code_error", "timeout", "invalid_data", "ir_contract_violation"})
BROKEN_PHASES = frozenset({"generate", "prepare", "render", "file", "parse", "grade"})

# Avisos do motor (`EngineWarning.code`).
WARNING_CODES = frozenset({"console_output", "missing_media", "media_error"})


@dataclass(frozen=True)
class QuestionRef:
    """A referência enviada ao motor: `repo` exatamente como validado, `commit` com 40 ou 64
    hex minúsculos, `qid` no padrão do contrato do motor."""

    repo: str
    commit: str
    qid: str


@dataclass(frozen=True)
class EngineWarning:
    """Aviso do motor (`console_output` · `missing_media` · `media_error`); `message` é texto do motor."""

    code: str
    message: str
    phase: str | None


@dataclass(frozen=True)
class UnsupportedReason:
    """Motivo de "não suportada" (um dos códigos do contrato do motor); `message` é texto de terceiros."""

    code: str
    message: str
    elements: tuple[str, ...]


@dataclass(frozen=True)
class QuestionInfo:
    """Os metadados da questão (`$defs.question_metadata` do contrato do motor)."""

    uuid: str | None
    title: str | None
    topic: str | None
    type: str | None
    tags: tuple[str, ...]
    grading_method: str
    elements: tuple[str, ...]
    show_correct_answer: bool
    single_variant: bool
    partial_credit: bool
    answers_names: tuple[str, ...]
    content_hash: str


@dataclass(frozen=True)
class Supported:
    """Inspeção: a questão é suportada."""

    question: QuestionInfo


@dataclass(frozen=True)
class Unsupported:
    """Não suportada: na inspeção, com a `question`; na criação da variante, sem ela."""

    reasons: tuple[UnsupportedReason, ...]
    question: QuestionInfo | None


@dataclass(frozen=True)
class Variant:
    """A variante: o `snapshot` e a IR como JSON cru e os avisos."""

    snapshot: Mapping[str, Any]
    ir: Mapping[str, Any]
    warnings: tuple[EngineWarning, ...]


@dataclass(frozen=True)
class VersionMismatch:
    """Divergência de versões do motor e do PL (`version_mismatch` do contrato)."""

    engine: bool
    pl: bool


@dataclass(frozen=True)
class Broken:
    """Questão quebrada (código, mensagem do motor, fase); sem `technical_detail`."""

    code: str
    message: str
    phase: str
    warnings: tuple[EngineWarning, ...]
    version_mismatch: VersionMismatch | None


@dataclass(frozen=True)
class FormatError:
    """Erro de formato de um campo (`{html, text}` do motor); pode ecoar a entrada do aluno."""

    html: str = field(repr=False)
    text: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class Graded:
    """Correção (`outcome: graded`). Todo campo pode trazer texto do aluno: `repr` fixo."""

    valid: bool
    format_errors: Mapping[str, FormatError]
    submitted_answers: Any
    partial_scores: Mapping[str, Any] | list[Any] | None
    score: float | None
    feedback: Any
    params: Any
    correct_answers: Any
    version_mismatch: VersionMismatch
    warnings: tuple[EngineWarning, ...]

    def __repr__(self) -> str:
        return "Graded(<oculto>)"

    __str__ = __repr__


# --- erros do pedido -----------------------------------------------------------------------------


class EngineError(Exception):
    """Falha do pedido ao motor, sem corpo nem valor do pedido; `kind` é um código fixo."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class EngineUnavailable(EngineError):
    """Repetível: `connection` · `timeout` · `internal_error` · `not_ready` · `incompatible` ·
    `protocol` · `unconfigured` (motor não configurado no worker)."""


class EngineNotFound(EngineError):
    """404 `not_found`: a árvore `repo@commit` sumiu do volume; repetível depois de nova obtenção
    (da árvore, pelo `RepoFetcher`)."""

    def __init__(self, kind: str = "not_found") -> None:
        super().__init__(kind)


class EngineRequestRejected(EngineError):
    """400 `invalid_request` · 413 `payload_too_large`: não repetível."""


# --- a porta -------------------------------------------------------------------------------------


class QuestionEngine(Protocol):
    """O motor de questões."""

    def inspect(self, ref: QuestionRef) -> Supported | Unsupported: ...

    def create_variant(self, ref: QuestionRef, seed: str) -> Variant | Unsupported | Broken: ...

    def grade(self, ref: QuestionRef, snapshot: Mapping[str, Any],
              raw_answers: Mapping[str, Any]) -> Graded | Broken: ...

    def close(self) -> None: ...
